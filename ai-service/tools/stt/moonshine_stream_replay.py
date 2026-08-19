#!/usr/bin/env python3
"""Replay XIAO PCM through Moonshine's streaming API at capture speed.

This is a measurement tool, not a production STT path.  It deliberately
models the concurrency boundary required by live UART audio:

    paced PCM producer -> bounded audio queue -> one Moonshine worker
                                             -> small text-event queue

The producer never calls Moonshine.  A single worker creates, starts, feeds,
stops, and closes each stream.  Moonshine listeners only snapshot text into a
small queue; JSON serialization and metric work happen after the native call
returns.  A queue overflow is reported as a failed case instead of silently
blocking or dropping audio.

The XIAO recorder emits 4096-byte PCM16 chunks (2048 samples, 128 ms at
16 kHz).  By default the first chunk is delivered after its 128 ms capture
period, just as live transport would deliver it.  Each case emits JSONL event
records followed by one ``case_summary`` record.  Optional batch decoding is
performed only after streaming metrics have been frozen, so it cannot improve
or contaminate the streaming result.

Typical CM5 use (from ~/hw1-ai-service):

    ~/hw1ai/bin/python tools/stt/moonshine_stream_replay.py \
      ~/stt-corpus/001.wav \
      ~/stt-corpus/002.wav \
      ~/stt-corpus/005.wav \
      --model-dir ~/.cache/moonshine_voice/download.moonshine.ai/model/medium-streaming-en/quantized_26_07_30 \
      --model-arch medium-streaming --update-interval 0.5 \
      --output ~/stt-results/moonshine-medium-0500ms.jsonl

Run a separate process/output for every model and update interval.  Then grade
the retained JSONL with ``tools/stt/moonshine_stream_replay_check.py``; this
collector's exit status covers runtime integrity, not transcript quality or
the Gate 0A latency/partial policy.  The model directory and streaming
architecture are required: the language-code catalog
resolver is intentionally not used because ``en`` can select a different tier
as the downloaded catalog changes.  A WAV's expected text is read from a
same-stem ``.txt`` file.  An empty sidecar marks a negative control; any
non-empty final transcript is then reported as a harmful hallucination and
makes the command fail.  Missing sidecars leave WER unset.

Production measurements also require every discovered CPU policy to use the
``performance`` governor.  ``--allow-non-performance`` exists for diagnostic
runs, and the selected governor is still captured in each summary.

This file imports ``moonshine_voice`` only when the CLI loads a real model.
Unit tests inject a fake transcriber and therefore run on machines without the
native AArch64 Moonshine package.
"""

from __future__ import annotations

import argparse
import array
from dataclasses import dataclass
import glob
import hashlib
from importlib import metadata as importlib_metadata
import json
import math
import os
from pathlib import Path
import platform
import queue
import re
import resource
import sys
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
import uuid
import wave


SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
PCM_BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH_BYTES
CHUNK_BYTES = 4096
CHUNK_SAMPLES = CHUNK_BYTES // SAMPLE_WIDTH_BYTES
DEFAULT_QUEUE_CHUNKS = 8
RecordSink = Callable[[dict[str, Any]], None]
Clock = Callable[[], float]


@dataclass(frozen=True)
class WavData:
    path: Path
    pcm: bytes
    sample_rate: int
    channels: int
    sample_width: int
    frames: int
    chunks: tuple[bytes, ...]

    @property
    def duration_s(self) -> float:
        return self.frames / self.sample_rate


@dataclass(frozen=True)
class _AudioChunk:
    index: int
    pcm: bytes
    enqueued_at: float
    audio_end_s: float


@dataclass(frozen=True)
class _EndOfAudio:
    enqueued_at: float
    enqueued_audio_s: float


@dataclass(frozen=True)
class _TextSnapshot:
    event_type: str
    occurred_at: float
    line_id: int | str | None = None
    text: str = ""
    is_complete: bool = False
    native_latency_ms: int = 0
    error: str | None = None


def read_xiao_wav(path: str | os.PathLike[str]) -> WavData:
    """Read and validate the mono PCM16/16-kHz shape used by the XIAO."""
    wav_path = Path(path).expanduser().resolve()
    try:
        with wave.open(str(wav_path), "rb") as source:
            channels = source.getnchannels()
            width = source.getsampwidth()
            rate = source.getframerate()
            frames = source.getnframes()
            compression = source.getcomptype()
            pcm = source.readframes(frames)
    except (OSError, EOFError, wave.Error) as exc:
        raise ValueError(f"cannot read WAV {wav_path}: {exc}") from exc

    problems: list[str] = []
    if channels != CHANNELS:
        problems.append(f"{channels} channels (need mono)")
    if width != SAMPLE_WIDTH_BYTES:
        problems.append(f"{width * 8}-bit samples (need PCM16)")
    if rate != SAMPLE_RATE:
        problems.append(f"{rate} Hz (need {SAMPLE_RATE} Hz)")
    if compression != "NONE":
        problems.append(f"compression={compression!r} (need PCM)")
    if len(pcm) != frames * channels * width:
        problems.append(
            f"truncated data: header says {frames * channels * width} bytes, "
            f"read {len(pcm)}"
        )
    if problems:
        raise ValueError(f"unsupported WAV {wav_path}: " + "; ".join(problems))

    chunks = tuple(pcm[offset:offset + CHUNK_BYTES]
                   for offset in range(0, len(pcm), CHUNK_BYTES))
    return WavData(
        path=wav_path,
        pcm=pcm,
        sample_rate=rate,
        channels=channels,
        sample_width=width,
        frames=frames,
        chunks=chunks,
    )


def pcm16_to_floats(pcm: bytes) -> list[float]:
    """Convert little-endian PCM16 bytes to Moonshine's [-1, 1) floats."""
    if len(pcm) % SAMPLE_WIDTH_BYTES:
        raise ValueError(f"odd PCM byte count: {len(pcm)}")
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    return [sample / 32768.0 for sample in samples]


_WORD_RE = re.compile(r"[^\W_]+(?:['\N{RIGHT SINGLE QUOTATION MARK}][^\W_]+)*",
                      re.UNICODE)


def word_tokens(text: str) -> list[str]:
    """Normalize text for reproducible, punctuation-insensitive WER."""
    return [token.replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")
            for token in _WORD_RE.findall(text.casefold())]


def edit_distance(reference: Sequence[Any], hypothesis: Sequence[Any]) -> int:
    """Levenshtein distance using O(min(n, m)) memory."""
    if len(reference) < len(hypothesis):
        # The metric is symmetric, and using the shorter row bounds memory.
        reference, hypothesis = hypothesis, reference
    previous = list(range(len(hypothesis) + 1))
    for row, ref_item in enumerate(reference, start=1):
        current = [row]
        for column, hyp_item in enumerate(hypothesis, start=1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (ref_item != hyp_item),
            ))
        previous = current
    return previous[-1]


def score_words(reference: str | None, hypothesis: str) -> dict[str, Any]:
    """Return WER fields, including explicit handling for negative controls."""
    if reference is None:
        return {
            "reference_text": None,
            "reference_words": None,
            "hypothesis_words": len(word_tokens(hypothesis)),
            "word_errors": None,
            "wer": None,
            "hallucinated_final": False,
        }
    ref = word_tokens(reference)
    hyp = word_tokens(hypothesis)
    errors = edit_distance(ref, hyp)
    return {
        "reference_text": reference,
        "reference_words": len(ref),
        "hypothesis_words": len(hyp),
        "word_errors": errors,
        "wer": errors / len(ref) if ref else None,
        "hallucinated_final": not ref and bool(hyp),
    }


def longest_common_prefix_chars(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def _transcript_text(transcript: Any) -> str:
    if transcript is None:
        return ""
    lines = getattr(transcript, "lines", None)
    if lines is None:
        return str(transcript).strip()

    def order(item: tuple[int, Any]) -> tuple[float, int, int | str, int]:
        index, line = item
        try:
            start_time = float(getattr(line, "start_time", 0.0) or 0.0)
        except (TypeError, ValueError):
            start_time = 0.0
        line_id = getattr(line, "line_id", 0)
        try:
            return start_time, 0, int(line_id), index
        except (TypeError, ValueError):
            return start_time, 1, str(line_id), index

    # Moonshine may return the newest line first.  Optical question text and
    # WER must use speech order, not native list order.
    ordered_lines = (line for _, line in sorted(enumerate(lines), key=order))
    return " ".join(
        str(getattr(line, "text", "")).strip()
        for line in ordered_lines
        if str(getattr(line, "text", "")).strip()
    ).strip()


def _json_scalar(value: Any) -> str | int | float | bool | None:
    name = getattr(value, "name", None)
    if name is not None:
        return str(name)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class SystemSampler:
    """Best-effort, stdlib-only Pi resource sampler."""

    def __init__(self) -> None:
        cpufreq = Path("/sys/devices/system/cpu/cpufreq")
        self._freq_paths = tuple(sorted(cpufreq.glob("policy*/scaling_cur_freq")))
        self._governor_paths = tuple(sorted(cpufreq.glob("policy*/scaling_governor")))
        self._temp_paths = tuple(sorted(Path("/sys/class/thermal").glob(
            "thermal_zone*/temp")))

    @staticmethod
    def _read_number(path: Path) -> int | None:
        try:
            return int(path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _read_text(path: Path) -> str | None:
        try:
            return path.read_text(encoding="ascii").strip()
        except OSError:
            return None

    @staticmethod
    def _rss_bytes() -> int | None:
        try:
            resident_pages = int(Path("/proc/self/statm").read_text(
                encoding="ascii").split()[1])
            return resident_pages * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            return None

    @staticmethod
    def _cpu_ticks() -> tuple[int | None, int | None]:
        try:
            fields = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0]
            numbers = [int(value) for value in fields.split()[1:]]
            total = sum(numbers)
            idle = numbers[3] + (numbers[4] if len(numbers) > 4 else 0)
            return total, idle
        except (OSError, ValueError, IndexError):
            return None, None

    @staticmethod
    def _memory() -> tuple[int | None, int | None]:
        try:
            values: dict[str, int] = {}
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                key, raw = line.split(":", 1)
                values[key] = int(raw.strip().split()[0]) * 1024
            swap_used = values.get("SwapTotal", 0) - values.get("SwapFree", 0)
            return values.get("MemAvailable"), swap_used
        except (OSError, ValueError, IndexError):
            return None, None

    def sample(self) -> dict[str, Any]:
        frequencies = [value for path in self._freq_paths
                       if (value := self._read_number(path)) is not None]
        temperatures = [value / 1000.0 for path in self._temp_paths
                        if (value := self._read_number(path)) is not None]
        governors = sorted({value for path in self._governor_paths
                           if (value := self._read_text(path))})
        total_ticks, idle_ticks = self._cpu_ticks()
        mem_available, swap_used = self._memory()
        return {
            "rss_bytes": self._rss_bytes(),
            "temperature_c": max(temperatures) if temperatures else None,
            "frequency_khz_min": min(frequencies) if frequencies else None,
            "frequency_khz_max": max(frequencies) if frequencies else None,
            "governors": governors,
            "host_cpu_total_ticks": total_ticks,
            "host_cpu_idle_ticks": idle_ticks,
            "mem_available_bytes": mem_available,
            "swap_used_bytes": swap_used,
        }


def summarize_system_samples(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    def numbers(key: str) -> list[float]:
        return [float(sample[key]) for sample in samples
                if isinstance(sample.get(key), (int, float))]

    summary: dict[str, Any] = {"sample_count": len(samples)}
    for key in ("rss_bytes", "temperature_c", "frequency_khz_min",
                "frequency_khz_max", "mem_available_bytes", "swap_used_bytes"):
        values = numbers(key)
        if values:
            summary[f"{key}_start"] = values[0]
            summary[f"{key}_end"] = values[-1]
            summary[f"{key}_min"] = min(values)
            summary[f"{key}_max"] = max(values)

    governors = sorted({str(governor)
                        for sample in samples
                        for governor in (sample.get("governors") or [])})
    summary["governors"] = governors

    if len(samples) >= 2:
        first, last = samples[0], samples[-1]
        total_first = first.get("host_cpu_total_ticks")
        total_last = last.get("host_cpu_total_ticks")
        idle_first = first.get("host_cpu_idle_ticks")
        idle_last = last.get("host_cpu_idle_ticks")
        if all(isinstance(value, int) for value in
               (total_first, total_last, idle_first, idle_last)):
            total_delta = total_last - total_first
            idle_delta = idle_last - idle_first
            if total_delta > 0:
                summary["host_cpu_percent"] = 100.0 * (
                    total_delta - idle_delta) / total_delta
    return summary


class _HypothesisTracker:
    def __init__(self, *, started_at: float, emit: RecordSink) -> None:
        self._started_at = started_at
        self._emit = emit
        self._lines: dict[int | str | None, tuple[int, str, bool]] = {}
        self._next_order = 0
        self.previous_text = ""
        self.partial_updates = 0
        self.revision_updates = 0
        self.total_retracted_chars = 0
        self.max_retracted_chars = 0
        self.first_text_at: float | None = None
        self.last_text_at: float | None = None
        self.native_latencies_ms: list[float] = []
        self.error_events: list[str] = []

    def consume(self, snapshot: _TextSnapshot) -> None:
        relative_s = snapshot.occurred_at - self._started_at
        if snapshot.error is not None:
            self.error_events.append(snapshot.error)
            self._emit({
                "type": "stt_event",
                "event": snapshot.event_type,
                "t_s": relative_s,
                "error": snapshot.error,
            })
            return

        line_id = snapshot.line_id
        if line_id not in self._lines:
            self._lines[line_id] = (self._next_order, snapshot.text,
                                    snapshot.is_complete)
            self._next_order += 1
        else:
            order, _, _ = self._lines[line_id]
            self._lines[line_id] = (order, snapshot.text, snapshot.is_complete)

        hypothesis = " ".join(
            text.strip() for _, text, _ in sorted(self._lines.values())
            if text.strip()
        ).strip()
        changed = hypothesis != self.previous_text
        common = longest_common_prefix_chars(self.previous_text, hypothesis)
        retracted = len(self.previous_text) - common if changed else 0
        added = len(hypothesis) - common if changed else 0
        if changed:
            self.partial_updates += 1
            if retracted:
                self.revision_updates += 1
                self.total_retracted_chars += retracted
                self.max_retracted_chars = max(self.max_retracted_chars, retracted)
            if hypothesis and self.first_text_at is None:
                self.first_text_at = snapshot.occurred_at
            self.last_text_at = snapshot.occurred_at
            self.previous_text = hypothesis
            if snapshot.native_latency_ms > 0:
                self.native_latencies_ms.append(float(snapshot.native_latency_ms))

        self._emit({
            "type": "stt_event",
            "event": snapshot.event_type,
            "t_s": relative_s,
            "line_id": _json_scalar(line_id),
            "line_text": snapshot.text,
            "line_complete": snapshot.is_complete,
            "native_latency_ms": snapshot.native_latency_ms,
            "hypothesis": hypothesis,
            "hypothesis_changed": changed,
            "lcp_chars": common,
            "retracted_chars": retracted,
            "added_chars": added,
        })


def _event_snapshot(event: Any, occurred_at: float) -> _TextSnapshot:
    event_type = type(event).__name__
    error = getattr(event, "error", None)
    line = getattr(event, "line", None)
    if error is not None and line is None:
        return _TextSnapshot(
            event_type=event_type,
            occurred_at=occurred_at,
            error=f"{type(error).__name__}: {error}",
        )
    if line is None:
        return _TextSnapshot(event_type=event_type, occurred_at=occurred_at)
    return _TextSnapshot(
        event_type=event_type,
        occurred_at=occurred_at,
        line_id=_json_scalar(getattr(line, "line_id", None)),
        text=str(getattr(line, "text", "") or ""),
        is_complete=bool(getattr(line, "is_complete", False)),
        native_latency_ms=int(getattr(line, "last_transcription_latency_ms", 0) or 0),
    )


def _model_description(transcriber: Any) -> dict[str, Any]:
    return {
        "model_path": _json_scalar(getattr(transcriber, "_model_path", None)),
        "model_arch": _json_scalar(getattr(transcriber, "_model_arch", None)),
        "runtime_identity": getattr(transcriber, "_hw1_probe_identity", None),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _model_tree_identity(model_dir: Path) -> dict[str, Any]:
    """Hash paths and contents after model load, outside measured replay time."""
    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    for path in sorted(item for item in model_dir.rglob("*") if item.is_file()):
        relative = path.relative_to(model_dir).as_posix()
        size = path.stat().st_size
        file_hash = _sha256_file(path)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
        file_count += 1
        total_bytes += size
    return {
        "directory": str(model_dir),
        "file_count": file_count,
        "total_bytes": total_bytes,
        "tree_sha256": digest.hexdigest(),
        "tree_sha256_basis": "relative_path,NUL,size,NUL,file_sha256,LF",
    }


def _max_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes; Linux (the target) and most BSDs report KiB.
    return int(value if sys.platform == "darwin" else value * 1024)


def run_replay(
    transcriber: Any,
    wav: WavData,
    *,
    model_dir: str,
    model_arch: str,
    update_interval_s: float = 0.5,
    queue_chunks: int = DEFAULT_QUEUE_CHUNKS,
    text_queue_events: int = 64,
    pace: float = 1.0,
    expected_text: str | None = None,
    expected_source: str | None = None,
    include_batch: bool = True,
    worker_timeout_s: float | None = None,
    sink: RecordSink | None = None,
    sampler: Any | None = None,
    clock: Clock = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    run_id: str | None = None,
    case_id: str | None = None,
) -> dict[str, Any]:
    """Run one paced replay with an injected real or fake transcriber.

    ``pace=1`` is real time; ``pace=0`` disables sleeping for unit tests and
    API smoke tests.  Values above one intentionally accelerate the producer.
    """
    if update_interval_s <= 0:
        raise ValueError("update_interval_s must be positive")
    if queue_chunks <= 0:
        raise ValueError("queue_chunks must be positive")
    if text_queue_events <= 0:
        raise ValueError("text_queue_events must be positive")
    if pace < 0:
        raise ValueError("pace cannot be negative")

    run_id = run_id or uuid.uuid4().hex
    case_id = case_id or uuid.uuid4().hex
    sampler = sampler or SystemSampler()
    sink = sink or (lambda _record: None)
    base = {"schema": 1, "run_id": run_id, "case_id": case_id}

    def emit(record: dict[str, Any]) -> None:
        sink({**base, **record})

    failures: list[str] = []
    failure_lock = threading.Lock()

    def fail(reason: str) -> None:
        with failure_lock:
            if reason not in failures:
                failures.append(reason)

    audio_queue: queue.Queue[_AudioChunk | _EndOfAudio] = queue.Queue(
        maxsize=queue_chunks)
    text_queue: queue.Queue[_TextSnapshot] = queue.Queue(maxsize=text_queue_events)
    stream_metrics_ready = threading.Event()
    metrics_captured = threading.Event()
    state: dict[str, Any] = {
        "chunks_processed": 0,
        "queue_hwm": 0,
        "queue_ages_ms": [],
        "add_audio_ms": [],
        "callback_drops": 0,
        "stream_text": "",
        "stop_returned_transcript": False,
        "batch_text": None,
        "batch_seconds": None,
        "end_to_final_s": None,
        "stop_seconds": None,
    }

    started_at = clock()
    tracker = _HypothesisTracker(started_at=started_at, emit=emit)
    system_samples: list[Mapping[str, Any]] = [sampler.sample()]
    process_cpu_started = time.process_time()
    max_rss_started = _max_rss_bytes()

    emit({
        "type": "case_start",
        "wav": str(wav.path),
        "model_dir": model_dir,
        "model_arch_requested": model_arch,
        **_model_description(transcriber),
        "update_interval_s": update_interval_s,
        "queue_capacity_chunks": queue_chunks,
        "text_queue_capacity_events": text_queue_events,
        "pace": pace,
        "audio_seconds": wav.duration_s,
        "audio_chunks": len(wav.chunks),
        "partial_last_chunk_bytes": len(wav.chunks[-1]) if wav.chunks else 0,
    })

    def drain_text_events() -> None:
        while True:
            try:
                snapshot = text_queue.get_nowait()
            except queue.Empty:
                return
            tracker.consume(snapshot)

    def listener(event: Any) -> None:
        # This callback is synchronous inside Moonshine's native inference.
        # Keep it allocation-light and never serialize JSON or touch UART here.
        snapshot = _event_snapshot(event, clock())
        try:
            text_queue.put_nowait(snapshot)
        except queue.Full:
            state["callback_drops"] += 1

    def worker() -> None:
        stream = None
        stream_started = False
        end_item: _EndOfAudio | None = None
        try:
            stream = transcriber.create_stream(update_interval=update_interval_s)
            stream.add_listener(listener)
            stream.start()
            stream_started = True
            emit({"type": "stream_started", "t_s": clock() - started_at})

            while True:
                item = audio_queue.get()
                if isinstance(item, _EndOfAudio):
                    end_item = item
                    break
                queue_age_ms = max(0.0, (clock() - item.enqueued_at) * 1000.0)
                state["queue_ages_ms"].append(queue_age_ms)
                call_started = clock()
                stream.add_audio(pcm16_to_floats(item.pcm), wav.sample_rate)
                call_ms = max(0.0, (clock() - call_started) * 1000.0)
                state["add_audio_ms"].append(call_ms)
                state["chunks_processed"] += 1
                drain_text_events()
                emit({
                    "type": "audio_chunk",
                    "chunk_index": item.index,
                    "audio_end_s": item.audio_end_s,
                    "bytes": len(item.pcm),
                    "queue_age_ms": queue_age_ms,
                    "add_audio_ms": call_ms,
                    "queue_depth_after": audio_queue.qsize(),
                })
        except BaseException as exc:
            fail(f"worker_error:{type(exc).__name__}:{exc}")
        finally:
            if stream is not None and stream_started:
                stop_started = clock()
                try:
                    result = stream.stop()
                    state["stop_returned_transcript"] = result is not None
                    state["stream_text"] = _transcript_text(result)
                    if result is None:
                        fail("missing_stop_result")
                except BaseException as exc:
                    fail(f"stop_error:{type(exc).__name__}:{exc}")
                finally:
                    state["stop_seconds"] = max(0.0, clock() - stop_started)
                    drain_text_events()
                final_ready_at = clock()
                if end_item is not None:
                    state["end_to_final_s"] = max(
                        0.0, final_ready_at - end_item.enqueued_at)
            elif stream is None:
                fail("stream_not_created")

            if state["callback_drops"]:
                fail("text_event_queue_overflow")
            for error_text in tracker.error_events:
                fail(f"moonshine_event_error:{error_text}")

            if stream is not None:
                try:
                    stream.close()
                except BaseException as exc:
                    fail(f"stream_close_error:{type(exc).__name__}:{exc}")

            # Freeze streaming resource metrics before the optional batch pass.
            stream_metrics_ready.set()
            metrics_captured.wait(timeout=5.0)

            if include_batch:
                batch_started = clock()
                try:
                    batch = transcriber.transcribe_without_streaming(
                        pcm16_to_floats(wav.pcm), sample_rate=wav.sample_rate)
                    state["batch_text"] = _transcript_text(batch)
                except BaseException as exc:
                    fail(f"batch_error:{type(exc).__name__}:{exc}")
                finally:
                    state["batch_seconds"] = max(0.0, clock() - batch_started)
    if worker_timeout_s is None:
        worker_timeout_s = max(30.0, wav.duration_s * 10.0 + 10.0)
    elif worker_timeout_s <= 0:
        raise ValueError("worker_timeout_s must be positive")
    thread = threading.Thread(target=worker, name="moonshine-replay-worker", daemon=True)
    thread.start()

    chunks_enqueued = 0
    audio_enqueued_s = 0.0
    overflowed = False
    next_system_sample_s = 0.5
    for index, pcm_chunk in enumerate(wav.chunks):
        if stream_metrics_ready.is_set():
            fail("worker_ended_before_audio_complete")
            break
        chunk_duration = len(pcm_chunk) / (
            wav.sample_rate * wav.channels * wav.sample_width)
        audio_enqueued_s += chunk_duration
        if pace > 0:
            target = started_at + audio_enqueued_s / pace
            delay = target - clock()
            if delay > 0:
                sleeper(delay)
        now = clock()
        try:
            audio_queue.put_nowait(_AudioChunk(
                index=index,
                pcm=pcm_chunk,
                enqueued_at=now,
                audio_end_s=audio_enqueued_s,
            ))
        except queue.Full:
            overflowed = True
            audio_enqueued_s -= chunk_duration
            fail("audio_queue_overflow")
            break
        chunks_enqueued += 1
        state["queue_hwm"] = max(state["queue_hwm"], audio_queue.qsize())
        if audio_enqueued_s >= next_system_sample_s:
            system_samples.append(sampler.sample())
            next_system_sample_s += 0.5

    producer_end_at = clock()
    end_item = _EndOfAudio(
        enqueued_at=producer_end_at,
        enqueued_audio_s=audio_enqueued_s,
    )
    sentinel_deadline = time.monotonic() + worker_timeout_s
    sentinel_sent = False
    while not sentinel_sent and not stream_metrics_ready.is_set():
        remaining = sentinel_deadline - time.monotonic()
        if remaining <= 0:
            fail("worker_timeout_while_ending_audio")
            break
        try:
            audio_queue.put(end_item, timeout=min(0.05, remaining))
            sentinel_sent = True
        except queue.Full:
            continue

    if not stream_metrics_ready.wait(timeout=worker_timeout_s):
        fail("worker_timeout")
    system_samples.append(sampler.sample())
    process_cpu_stream_s = max(0.0, time.process_time() - process_cpu_started)
    max_rss_stream_bytes = max(max_rss_started, _max_rss_bytes())
    stream_wall_s = max(0.0, clock() - started_at)
    metrics_captured.set()

    thread.join(timeout=worker_timeout_s)
    if thread.is_alive():
        fail("worker_timeout_after_stream")

    # If the final result includes lines that did not produce a text-change
    # callback, the returned transcript remains authoritative.
    stream_text = str(state["stream_text"] or "").strip()
    stream_accuracy = score_words(expected_text, stream_text)
    if stream_accuracy["hallucinated_final"]:
        fail("hallucinated_final")

    batch_text = state["batch_text"]
    batch_accuracy = (score_words(expected_text, batch_text)
                      if isinstance(batch_text, str) else None)
    stream_vs_batch = (score_words(batch_text, stream_text)
                       if isinstance(batch_text, str) else None)

    queue_ages = state["queue_ages_ms"]
    add_times = state["add_audio_ms"]
    native_latencies = tracker.native_latencies_ms
    first_partial_s = (tracker.first_text_at - started_at
                       if tracker.first_text_at is not None else None)
    end_to_first_partial_s = (tracker.first_text_at - producer_end_at
                              if tracker.first_text_at is not None else None)
    system_summary = summarize_system_samples(system_samples)
    system_summary.update({
        "process_cpu_s": process_cpu_stream_s,
        "process_cpu_percent": (
            100.0 * process_cpu_stream_s / stream_wall_s if stream_wall_s > 0 else None
        ),
        "process_max_rss_bytes": max_rss_stream_bytes,
    })

    result: dict[str, Any] = {
        **base,
        "type": "case_summary",
        "ok": not failures,
        "failure_reasons": list(failures),
        "wav": str(wav.path),
        "expected_source": expected_source,
        "model_dir": model_dir,
        "model_arch_requested": model_arch,
        **_model_description(transcriber),
        "update_interval_s": update_interval_s,
        "pace": pace,
        "audio": {
            "sample_rate": wav.sample_rate,
            "channels": wav.channels,
            "sample_width_bytes": wav.sample_width,
            "frames": wav.frames,
            "bytes": len(wav.pcm),
            "seconds": wav.duration_s,
            "chunk_bytes": CHUNK_BYTES,
            "chunks_total": len(wav.chunks),
            "chunks_enqueued": chunks_enqueued,
            "chunks_processed": state["chunks_processed"],
            "chunks_dropped": len(wav.chunks) - chunks_enqueued,
            "enqueued_seconds": audio_enqueued_s,
            "partial_last_chunk_bytes": (
                len(wav.chunks[-1]) if wav.chunks and
                len(wav.chunks[-1]) != CHUNK_BYTES else 0
            ),
        },
        "queue": {
            "capacity_chunks": queue_chunks,
            "high_water_chunks": state["queue_hwm"],
            "overflowed": overflowed,
            "age_ms_p50": _percentile(queue_ages, 0.50),
            "age_ms_p95": _percentile(queue_ages, 0.95),
            "age_ms_max": max(queue_ages) if queue_ages else None,
        },
        "stream": {
            "text": stream_text,
            "stop_returned_transcript": state["stop_returned_transcript"],
            "wall_seconds": stream_wall_s,
            "end_to_final_seconds": state["end_to_final_s"],
            "stop_seconds": state["stop_seconds"],
            "first_partial_seconds": first_partial_s,
            "end_to_first_partial_seconds": end_to_first_partial_s,
            "partial_updates": tracker.partial_updates,
            "revision_updates": tracker.revision_updates,
            "total_retracted_chars": tracker.total_retracted_chars,
            "max_retracted_chars": tracker.max_retracted_chars,
            "text_event_drops": state["callback_drops"],
            "native_latency_ms_p50": _percentile(native_latencies, 0.50),
            "native_latency_ms_p95": _percentile(native_latencies, 0.95),
            "native_latency_ms_max": (
                max(native_latencies) if native_latencies else None
            ),
            "add_audio_ms_p50": _percentile(add_times, 0.50),
            "add_audio_ms_p95": _percentile(add_times, 0.95),
            "add_audio_ms_max": max(add_times) if add_times else None,
        },
        "accuracy": stream_accuracy,
        "batch": {
            "enabled": include_batch,
            "text": batch_text,
            "seconds": state["batch_seconds"],
            "accuracy": batch_accuracy,
            "stream_vs_batch": stream_vs_batch,
        },
        "system": system_summary,
    }
    emit(result)
    return result


class JsonlWriter:
    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def __call__(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._stream.write(json.dumps(record, ensure_ascii=False,
                                          separators=(",", ":")) + "\n")
            self._stream.flush()


def discover_wavs(inputs: Iterable[str]) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for raw in inputs:
        expanded = Path(raw).expanduser()
        candidates: Iterable[Path]
        if any(character in raw for character in "*?["):
            candidates = (Path(match) for match in glob.glob(
                str(Path(raw).expanduser()), recursive=True))
        elif expanded.is_dir():
            candidates = expanded.rglob("*.wav")
        else:
            candidates = (expanded,)
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.suffix.casefold() == ".wav" and resolved not in seen:
                found.append(resolved)
                seen.add(resolved)
    return sorted(found)


def sidecar_reference(wav_path: Path) -> tuple[str | None, str | None]:
    sidecar = wav_path.with_suffix(".txt")
    if not sidecar.is_file():
        return None, None
    return sidecar.read_text(encoding="utf-8").strip(), str(sidecar)


def governor_guard_error(sample: Mapping[str, Any]) -> str | None:
    governors = sorted({str(value) for value in (sample.get("governors") or [])})
    if not governors:
        return "could not discover any CPU scaling governor"
    if governors != ["performance"]:
        return "CPU scaling governor is " + ",".join(governors) + ", not performance"
    return None


def load_real_transcriber(model_dir: str, model_arch: str) -> Any:
    """Load one exact model without consulting Moonshine's catalog resolver."""
    import moonshine_voice

    path = Path(model_dir).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"--model-dir is not an existing directory: {path}")
    architectures = {
        "tiny-streaming": moonshine_voice.ModelArch.TINY_STREAMING,
        "small-streaming": moonshine_voice.ModelArch.SMALL_STREAMING,
        "medium-streaming": moonshine_voice.ModelArch.MEDIUM_STREAMING,
    }
    arch = architectures[model_arch]
    options = {"return_audio_data": "false"}
    transcriber = moonshine_voice.Transcriber(
        model_path=str(path),
        model_arch=arch,
        options=options,
    )
    try:
        try:
            distribution_version = importlib_metadata.version("moonshine-voice")
        except importlib_metadata.PackageNotFoundError:
            distribution_version = None
        package_file = Path(moonshine_voice.__file__).resolve()
        binding_module = sys.modules.get(type(transcriber).__module__)
        binding_name = getattr(binding_module, "__file__", None)
        binding_file = Path(binding_name).resolve() if binding_name else None
        native_name = str(getattr(getattr(transcriber, "_lib", None), "_name", ""))
        native_path = Path(native_name).resolve() if native_name else None
        native_identity: dict[str, Any] = {
            "path": str(native_path) if native_path else native_name or None,
            "api_version": int(transcriber.get_version()),
        }
        if native_path is not None and native_path.is_file():
            native_identity.update({
                "bytes": native_path.stat().st_size,
                "sha256": _sha256_file(native_path),
            })
        transcriber._hw1_probe_identity = {
            "python": sys.version,
            "platform": platform.platform(),
            "moonshine_voice_version": getattr(moonshine_voice, "__version__", None),
            "moonshine_distribution_version": distribution_version,
            "moonshine_package_file": str(package_file),
            "moonshine_package_file_sha256": _sha256_file(package_file),
            "moonshine_transcriber_file": (
                str(binding_file) if binding_file is not None else None
            ),
            "moonshine_transcriber_file_sha256": (
                _sha256_file(binding_file)
                if binding_file is not None and binding_file.is_file() else None
            ),
            "native_library": native_identity,
            "model": _model_tree_identity(path),
            "model_arch_requested": model_arch,
            "model_arch_enum": _json_scalar(arch),
            "transcriber_options": options,
        }
    except BaseException:
        transcriber.close()
        raise
    return transcriber


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("inputs", nargs="+",
                        help="WAV files, directories, or quoted glob patterns")
    parser.add_argument("--model-dir", required=True,
                        help="existing downloaded model directory (required; "
                             "language-code/catalog resolution is forbidden)")
    parser.add_argument(
        "--model-arch", required=True,
        choices=("tiny-streaming", "small-streaming", "medium-streaming"),
        help="architecture matching --model-dir (required)",
    )
    parser.add_argument("--update-interval", type=float, default=0.5,
                        help="Moonshine update floor in seconds (default: 0.5)")
    parser.add_argument(
        "--queue-chunks", type=int, default=DEFAULT_QUEUE_CHUNKS,
        help=("bounded PCM queue capacity "
              f"(default: {DEFAULT_QUEUE_CHUNKS} chunks / "
              f"{DEFAULT_QUEUE_CHUNKS * CHUNK_BYTES * 1000 // PCM_BYTES_PER_SECOND}ms)"),
    )
    parser.add_argument("--text-queue-events", type=int, default=64,
                        help="bounded listener snapshot queue (default: 64)")
    parser.add_argument("--pace", type=float, default=1.0,
                        help="producer speed, 1.0 = real time (default: 1.0)")
    parser.add_argument("--no-pace", action="store_true",
                        help="feed as fast as possible; not a real-time benchmark")
    parser.add_argument("--no-batch", action="store_true",
                        help="skip the post-stream batch accuracy baseline")
    parser.add_argument("--expected-text",
                        help="expected transcript for a single input; otherwise "
                             "same-stem .txt sidecars are used")
    parser.add_argument("--worker-timeout", type=float,
                        help="per-phase worker timeout in seconds")
    parser.add_argument("--output", default="-",
                        help="JSONL destination, or - for stdout (default: -)")
    parser.add_argument("--force", action="store_true",
                        help="allow --output to replace an existing file")
    parser.add_argument(
        "--allow-non-performance", action="store_true",
        help="allow diagnostic runs when CPU governors are not all performance",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    wav_paths = discover_wavs(args.inputs)
    if not wav_paths:
        parser.error("no WAV files found")
    if args.expected_text is not None and len(wav_paths) != 1:
        parser.error("--expected-text requires exactly one WAV")
    if args.queue_chunks <= 0 or args.text_queue_events <= 0:
        parser.error("queue capacities must be positive")
    if args.pace < 0:
        parser.error("--pace cannot be negative")

    if args.update_interval <= 0:
        parser.error("--update-interval must be positive")
    model_dir = Path(args.model_dir).expanduser().resolve()
    if not model_dir.is_dir():
        parser.error(f"--model-dir is not an existing directory: {model_dir}")
    pace = 0.0 if args.no_pace else args.pace
    run_id = uuid.uuid4().hex

    output_path = None if args.output == "-" else Path(args.output).expanduser()
    if output_path is not None:
        if output_path.exists() and not args.force:
            parser.error(f"--output already exists: {output_path} (use --force to replace it)")

    if not args.allow_non_performance:
        governor_error = governor_guard_error(SystemSampler().sample())
        if governor_error:
            parser.error(governor_error + " (use --allow-non-performance only for diagnostics)")

    owned_output = None
    if output_path is None:
        output = sys.stdout
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        owned_output = output_path.open("w" if args.force else "x", encoding="utf-8")
        output = owned_output
    writer = JsonlWriter(output)

    failures = 0
    case_number = 0
    try:
        try:
            transcriber = load_real_transcriber(str(model_dir), args.model_arch)
        except Exception as exc:
            writer({
                "schema": 1,
                "type": "model_error",
                "run_id": run_id,
                "model_dir": str(model_dir),
                "model_arch_requested": args.model_arch,
                "error": f"{type(exc).__name__}: {exc}",
            })
            return 1
        try:
            writer({
                "schema": 1,
                "type": "model_identity",
                "run_id": run_id,
                "model_dir": str(model_dir),
                "model_arch_requested": args.model_arch,
                **_model_description(transcriber),
            })
            for wav_path in wav_paths:
                case_number += 1
                try:
                    wav = read_xiao_wav(wav_path)
                    if args.expected_text is not None:
                        expected = args.expected_text
                        expected_source = "--expected-text"
                    else:
                        expected, expected_source = sidecar_reference(wav_path)
                    result = run_replay(
                        transcriber,
                        wav,
                        model_dir=str(model_dir),
                        model_arch=args.model_arch,
                        update_interval_s=args.update_interval,
                        queue_chunks=args.queue_chunks,
                        text_queue_events=args.text_queue_events,
                        pace=pace,
                        expected_text=expected,
                        expected_source=expected_source,
                        include_batch=not args.no_batch,
                        worker_timeout_s=args.worker_timeout,
                        sink=writer,
                        run_id=run_id,
                        case_id=f"{case_number:04d}",
                    )
                    if not result["ok"]:
                        failures += 1
                except Exception as exc:
                    failures += 1
                    writer({
                        "schema": 1,
                        "type": "case_error",
                        "run_id": run_id,
                        "case_id": f"{case_number:04d}",
                        "wav": str(wav_path),
                        "model_dir": str(model_dir),
                        "model_arch_requested": args.model_arch,
                        "update_interval_s": args.update_interval,
                        "error": f"{type(exc).__name__}: {exc}",
                    })
        finally:
            transcriber.close()
    finally:
        if owned_output is not None:
            owned_output.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
