"""Bounded, single-owner Moonshine streaming worker.

The UART reader/PCM collector must never call Moonshine or block behind native
inference.  ``LiveMoonshineWorker`` therefore coalesces small physical UART
frames into the recorder's 4096-byte/128-ms logical chunks and offers those
chunks to one bounded FIFO.  One persistent worker thread owns model creation,
stream creation, every ``add_audio`` call, ``stop``, and close.

Queue overflow is durable and explicit.  It invalidates only the streaming-STT
shadow result; callers must keep draining transport PCM and may retain/fetch the
firmware WAV as the independent fallback.
"""

from __future__ import annotations

import array
from dataclasses import dataclass
import hashlib
from importlib import metadata as importlib_metadata
import math
from pathlib import Path
import platform
import queue
import re
import sys
import threading
import time
from typing import Any, Callable


SAMPLE_RATE = 16_000
SAMPLE_WIDTH_BYTES = 2
LOGICAL_CHUNK_BYTES = 4096
DEFAULT_QUEUE_CHUNKS = 8
DEFAULT_TEXT_QUEUE_EVENTS = 64


@dataclass(frozen=True)
class _AudioChunk:
    pcm: bytes
    enqueued_at: float


@dataclass(frozen=True)
class _TextSnapshot:
    line_id: int | str | None
    text: str
    complete: bool
    error: str | None = None


def _pcm16_floats(pcm: bytes) -> list[float]:
    if len(pcm) % SAMPLE_WIDTH_BYTES:
        raise ValueError(f"odd PCM byte count: {len(pcm)}")
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    return [sample / 32768.0 for sample in samples]


# Bound on recorded hypothesis-history entries and stop-transcript lines; a
# normal exchange produces a handful, so hitting this means something is
# pathologically chatty — counted via partials_dropped, never silent.
_PARTIAL_HISTORY_MAX = 256


def _transcript_text(transcript: Any) -> str:
    if transcript is None:
        return ""
    lines = getattr(transcript, "lines", None)
    if lines is None:
        return str(transcript).strip()
    indexed = list(enumerate(lines))

    def order(item: tuple[int, Any]) -> tuple[float, int, int | str, int]:
        index, line = item
        try:
            start = float(getattr(line, "start_time", 0.0) or 0.0)
        except (TypeError, ValueError):
            start = 0.0
        line_id = getattr(line, "line_id", index)
        numeric = isinstance(line_id, int) and not isinstance(line_id, bool)
        stable_id: int | str = line_id if numeric else str(line_id)
        return (start, 0 if numeric else 1, stable_id, index)

    return " ".join(
        str(getattr(line, "text", "") or "").strip()
        for _, line in sorted(indexed, key=order)
        if str(getattr(line, "text", "") or "").strip()
    ).strip()


def _norm_words(text: str) -> list[str]:
    """Case/punctuation-insensitive word list for prefix comparison."""
    return re.sub(r"[^0-9a-z]+", " ", (text or "").lower()).split()


def _best_complete_partial(
        partials: list[dict[str, Any]]) -> tuple[str | None, Any]:
    """The last non-empty COMPLETE running hypothesis (empty-stop rescue).

    ``_partial_history`` is append-only, so it preserves a completed line even
    after stop() erases it. Returns (text, t) or (None, None)."""
    for entry in reversed(partials or []):
        if entry.get("line_complete") and str(entry.get("text") or "").strip():
            return str(entry["text"]).strip(), entry.get("t")
    return None, None


def _richest_extension(
        stop_words: list[str],
        partials: list[dict[str, Any]]) -> tuple[str | None, Any]:
    """The richest running hypothesis that strictly EXTENDS the stop text.

    "Extends" == has ``stop_words`` as a normalized word-prefix AND more words,
    so a trailing completed line that stop() erased is recovered while a
    revised-down or divergent hypothesis can never win. Selecting the *richest*
    (not the latest) matters: stop()'s erasure also lands in the history as a
    later, shorter entry, which must not be chosen. A COMPLETE hypothesis is
    preferred; only if none extends do we fall back to the richest in-progress
    one (still strictly prefix-bounded to this utterance). Returns (text, t)."""
    n = len(stop_words)
    best_c: tuple[int, str, Any] | None = None
    best_any: tuple[int, str, Any] | None = None
    for entry in partials or []:
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        words = _norm_words(text)
        if len(words) <= n or words[:n] != stop_words:
            continue
        cand = (len(words), text, entry.get("t"))
        if best_any is None or cand[0] > best_any[0]:
            best_any = cand
        if entry.get("line_complete") and (
                best_c is None or cand[0] > best_c[0]):
            best_c = cand
    chosen = best_c or best_any
    return (chosen[1], chosen[2]) if chosen else (None, None)


def _last_standing_partial(
        partials: list[dict[str, Any]],
        input_ended_t: float) -> tuple[str | None, Any]:
    """The last non-empty hypothesis IF it was still standing at end-of-input.

    Recovers the 2026-08-11 13:14 field case: moonshine transcribed the whole
    utterance but never marked any line complete, then stop() erased it all —
    the complete-only rescue had nothing to work with. Timing is the
    discriminator: an emptying update at/after input_ended_t is stop-time
    erasure (recover); one BEFORE it is the model genuinely retracting
    mid-audio (e.g. cleaning up a silence hallucination — do NOT recover)."""
    last_idx = None
    for idx, entry in enumerate(partials or []):
        if str(entry.get("text") or "").strip():
            last_idx = idx
    if last_idx is None:
        return None, None
    for entry in partials[last_idx + 1:]:
        t = entry.get("t")
        if not isinstance(t, (int, float)) or t < input_ended_t - 0.05:
            return None, None  # emptied while audio still flowed: retraction
    chosen = partials[last_idx]
    return str(chosen["text"]).strip(), chosen.get("t")


def finalize_transcript(
        stop_text: str, partials: list[dict[str, Any]], *,
        input_ended_t: float | None = None,
        legacy: bool = False) -> dict[str, Any]:
    """Reconcile Moonshine's stop() transcript against the running partials.

    Moonshine's stop() can hand back a completed line as an empty string
    (2026-08-11 erasure class). Two recoveries, both drawn from the append-only
    partial history:

    - empty_rescue: the WHOLE stop transcript is empty -> the last non-empty
      complete hypothesis (original behaviour; active in both modes so
      ``legacy`` reproduces the pre-fix result).
    - erasure_rescue: the stop transcript is a strict word-PREFIX of a richer
      hypothesis -> a trailing completed line was erased; recover the richest
      such extension. Prefix-bounded, so a legitimately revised-down / divergent
      partial can never clobber a clean stop. Disabled when ``legacy`` (the
      pre-fix path, kept so the replay bench can A/B old vs new on one snapshot).

    Returns {text, rescued_from_t, mode}. mode in
    {stop, empty, empty_rescue, erasure_rescue}."""
    stop_text = (stop_text or "").strip()
    if not stop_text:
        best, best_t = _best_complete_partial(partials)
        if best:
            return {"text": best, "rescued_from_t": best_t,
                    "mode": "empty_rescue"}
        if not legacy and input_ended_t is not None:
            # No COMPLETE hypothesis exists — recover an in-progress one only
            # if it was still standing when the audio ended (timing rule in
            # _last_standing_partial). Legacy mode keeps the pre-fix result
            # so the replay bench can A/B on captured snapshots.
            standing, standing_t = _last_standing_partial(
                partials, input_ended_t)
            if standing:
                return {"text": standing, "rescued_from_t": standing_t,
                        "mode": "empty_rescue_incomplete"}
        return {"text": "", "rescued_from_t": None, "mode": "empty"}
    if not legacy:
        rich, rich_t = _richest_extension(_norm_words(stop_text), partials)
        if rich is not None:
            return {"text": rich, "rescued_from_t": rich_t,
                    "mode": "erasure_rescue"}
    return {"text": stop_text, "rescued_from_t": None, "mode": "stop"}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _model_tree_identity(model_dir: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    total = 0
    for item in sorted(path for path in model_dir.rglob("*") if path.is_file()):
        relative = item.relative_to(model_dir).as_posix()
        size = item.stat().st_size
        file_hash = _sha256_file(item)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
        count += 1
        total += size
    return {
        "directory": str(model_dir),
        "file_count": count,
        "total_bytes": total,
        "tree_sha256": digest.hexdigest(),
    }


def exact_moonshine_factory(model_dir: str, model_arch: str) -> Callable[[], Any]:
    """Return a worker-thread factory for one exact downloaded model."""
    path = Path(model_dir).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"model directory does not exist: {path}")
    if model_arch not in {
            "tiny-streaming", "small-streaming", "medium-streaming"}:
        raise ValueError(f"unsupported streaming model architecture: {model_arch}")

    def create() -> Any:
        import moonshine_voice  # type: ignore

        architectures = {
            "tiny-streaming": moonshine_voice.ModelArch.TINY_STREAMING,
            "small-streaming": moonshine_voice.ModelArch.SMALL_STREAMING,
            "medium-streaming": moonshine_voice.ModelArch.MEDIUM_STREAMING,
        }
        transcriber = moonshine_voice.Transcriber(
            model_path=str(path),
            model_arch=architectures[model_arch],
            options={"return_audio_data": "false"},
        )
        try:
            try:
                version = importlib_metadata.version("moonshine-voice")
            except importlib_metadata.PackageNotFoundError:
                version = None
            package_file = Path(moonshine_voice.__file__).resolve()
            binding_module = sys.modules.get(type(transcriber).__module__)
            binding_name = getattr(binding_module, "__file__", None)
            binding_file = Path(binding_name).resolve() if binding_name else None
            native_name = str(
                getattr(getattr(transcriber, "_lib", None), "_name", ""))
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
            transcriber._hw1_live_identity = {
                "python": sys.version,
                "platform": platform.platform(),
                "moonshine_package_file": str(package_file),
                "moonshine_package_file_sha256": _sha256_file(package_file),
                "moonshine_transcriber_file": (
                    str(binding_file) if binding_file is not None else None),
                "moonshine_transcriber_file_sha256": (
                    _sha256_file(binding_file)
                    if binding_file is not None and binding_file.is_file()
                    else None),
                "model_arch_requested": model_arch,
                "model_arch_enum": str(architectures[model_arch]),
                "moonshine_distribution_version": version,
                "native_library": native_identity,
                "model": _model_tree_identity(path),
                "transcriber_options": {"return_audio_data": "false"},
            }
            return transcriber
        except BaseException:
            transcriber.close()
            raise

    return create


def performance_governors() -> list[str]:
    values: set[str] = set()
    for path in Path("/sys/devices/system/cpu/cpufreq").glob(
            "policy*/scaling_governor"):
        try:
            value = path.read_text(encoding="ascii").strip()
        except OSError:
            continue
        if value:
            values.add(value)
    return sorted(values)


class LiveMoonshineWorker:
    """Nonblocking PCM producer plus one native-inference owner thread."""

    def __init__(
            self,
            transcriber_factory: Callable[[], Any],
            *,
            update_interval_s: float = 1.0,
            queue_chunks: int = DEFAULT_QUEUE_CHUNKS,
            text_queue_events: int = DEFAULT_TEXT_QUEUE_EVENTS,
            clock: Callable[[], float] = time.monotonic) -> None:
        if not math.isfinite(update_interval_s) or update_interval_s <= 0:
            raise ValueError("update_interval_s must be positive")
        if queue_chunks <= 0 or text_queue_events <= 0:
            raise ValueError("queue capacities must be positive")
        self._factory = transcriber_factory
        self._update_interval_s = float(update_interval_s)
        self._queue_chunks = int(queue_chunks)
        self._clock = clock
        self._audio: queue.Queue[_AudioChunk] = queue.Queue(maxsize=queue_chunks)
        self._text: queue.Queue[_TextSnapshot] = queue.Queue(
            maxsize=text_queue_events)
        self._ready = threading.Event()
        self._input_done = threading.Event()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._producer_lock = threading.Lock()
        self._staging = bytearray()
        self._accepting = True
        self._failures: list[str] = []
        self._failure_lock = threading.Lock()
        self._started_at: float | None = None
        self._input_ended_at: float | None = None
        self._startup_error: BaseException | None = None
        self._identity: dict[str, Any] | None = None
        # Guards the diagnostic containers below (_lines, _partial_history,
        # _stop_lines) against the cross-thread reader in snapshot(): the
        # wait()-timeout path snapshots from the caller's thread while the
        # worker thread is still appending, so an unguarded iteration would
        # raise "changed size during iteration". Held only briefly.
        self._view_lock = threading.Lock()
        self._lines: dict[int | str | None, tuple[int, str]] = {}
        self._line_order = 0
        self._partial_updates = 0
        self._last_partial = ""
        self._first_partial_s: float | None = None
        # Full hypothesis history (2026-08-10): the counters alone proved
        # blind — a hallucinated trailing line ("France.") could not be
        # attributed without the intermediate texts. Bounded; overflow is
        # counted, never silently dropped.
        self._partial_history: list[dict[str, Any]] = []
        self._partial_history_dropped = 0
        # Line structure of the stop() transcript — the FINAL text comes from
        # Moonshine's own stop result, not from the live line table, so both
        # are recorded to localize where an artifact line entered.
        self._stop_lines: list[dict[str, Any]] = []
        # 2026-08-11 artifact class: stop() can ERASE a completed line — the
        # history shows the full correct hypothesis (line_complete=True), then
        # a "" update on the same line ~0.3s later, and the stop transcript
        # carries the empty line. When that happens the last non-empty
        # COMPLETE hypothesis is recovered as the final text, flagged below.
        # True no-speech runs never have a non-empty complete hypothesis, so
        # valid_empty keeps its no-speech meaning.
        self._stop_text_empty = False
        self._final_recovered_from_t: float | None = None
        self._final_rescue_mode = ""
        self._queue_hwm = 0
        self._queue_ages_ms: list[float] = []
        self._offered_bytes = 0
        self._enqueued_bytes = 0
        self._processed_bytes = 0
        self._text_event_drops = 0
        self._stop_returned = False
        self._text_result = ""
        self._end_to_final_s: float | None = None

    def _fail(self, reason: str) -> None:
        with self._failure_lock:
            if reason not in self._failures:
                self._failures.append(reason)

    def start(self, timeout: float = 120.0) -> None:
        if timeout <= 0:
            raise ValueError("startup timeout must be positive")
        if self._thread is not None:
            raise RuntimeError("live Moonshine worker already started")
        self._started_at = self._clock()
        self._thread = threading.Thread(
            target=self._run, name="moonshine-live-worker", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self._fail("model_startup_timeout")
            raise TimeoutError("Moonshine live worker did not become ready")
        if self._startup_error is not None:
            raise RuntimeError(
                "Moonshine live worker failed to start: "
                f"{type(self._startup_error).__name__}: {self._startup_error}")

    def on_begin(self, begin: dict[str, object]) -> None:
        sample_rate = int(begin.get("sample_rate", 0))
        if sample_rate != SAMPLE_RATE:
            self._fail(f"sample_rate:{sample_rate}!={SAMPLE_RATE}")
            self._accepting = False

    def offer_pcm(self, pcm: bytes) -> bool:
        """Offer physical-frame PCM without waiting for inference."""
        if not pcm:
            return True
        with self._producer_lock:
            self._offered_bytes += len(pcm)
            if len(pcm) % SAMPLE_WIDTH_BYTES:
                self._fail("odd_pcm_bytes")
                self._accepting = False
                return False
            if not self._accepting or self._input_done.is_set():
                return False
            self._staging.extend(pcm)
            while len(self._staging) >= LOGICAL_CHUNK_BYTES:
                chunk = bytes(self._staging[:LOGICAL_CHUNK_BYTES])
                del self._staging[:LOGICAL_CHUNK_BYTES]
                if not self._publish(chunk):
                    self._staging.clear()
                    return False
            return True

    def _publish(self, pcm: bytes) -> bool:
        try:
            self._audio.put_nowait(_AudioChunk(pcm, self._clock()))
        except queue.Full:
            self._fail("audio_queue_overflow")
            self._accepting = False
            return False
        self._enqueued_bytes += len(pcm)
        self._queue_hwm = max(self._queue_hwm, self._audio.qsize())
        return True

    def end_input(self) -> None:
        with self._producer_lock:
            if self._input_done.is_set():
                return
            if self._accepting and self._staging:
                self._publish(bytes(self._staging))
            self._staging.clear()
            self._input_ended_at = self._clock()
            self._input_done.set()

    def abort(self, reason: str) -> None:
        self._fail(f"input_abort:{reason}")
        with self._producer_lock:
            self._accepting = False
            self._staging.clear()
            if self._input_ended_at is None:
                self._input_ended_at = self._clock()
            self._input_done.set()

    def join(self, timeout: float) -> bool:
        """Wait for the inference thread to finish, without result semantics.

        Unlike wait(), this never stamps a final_timeout failure — it exists
        for lifecycle owners (warm-slot recycling) that only need to know the
        native transcriber has been released."""
        return self._done.wait(timeout)

    def wait(self, timeout: float) -> dict[str, Any]:
        if timeout <= 0:
            raise ValueError("final timeout must be positive")
        if not self._done.wait(timeout):
            self._fail("final_timeout")
            return self.snapshot(done=False)
        assert self._thread is not None
        self._thread.join(timeout=0)
        return self.snapshot(done=True)

    def snapshot(self, *, done: bool | None = None) -> dict[str, Any]:
        if done is None:
            done = self._done.is_set()
        with self._failure_lock:
            failures = list(self._failures)
        valid = bool(done and self._stop_returned and not failures)
        return {
            "valid": valid,
            "valid_empty": bool(valid and not self._text_result.strip()),
            "done": bool(done),
            "text": self._text_result,
            "failure_reasons": failures,
            "identity": self._identity,
            "update_interval_s": self._update_interval_s,
            "audio": {
                "offered_bytes": self._offered_bytes,
                "enqueued_bytes": self._enqueued_bytes,
                "processed_bytes": self._processed_bytes,
            },
            "queue": {
                "logical_chunk_bytes": LOGICAL_CHUNK_BYTES,
                "capacity_chunks": self._queue_chunks,
                "capacity_bytes": self._queue_chunks * LOGICAL_CHUNK_BYTES,
                "capacity_ms": (
                    self._queue_chunks * LOGICAL_CHUNK_BYTES * 1000
                    // (SAMPLE_RATE * SAMPLE_WIDTH_BYTES)),
                "high_water_chunks": self._queue_hwm,
                "age_ms_max": (
                    max(self._queue_ages_ms) if self._queue_ages_ms else None),
                "overflowed": "audio_queue_overflow" in failures,
            },
            "stream": {
                "stop_returned_transcript": self._stop_returned,
                "partial_updates": self._partial_updates,
                "first_partial_seconds": self._first_partial_s,
                "end_to_final_seconds": self._end_to_final_s,
                "text_event_drops": self._text_event_drops,
                # Moonshine's intermediate "thinking", in order: every changed
                # hypothesis with its trigger line, the live line table at the
                # end, and the stop() transcript's own line structure. This is
                # what localizes artifact words (which line, live vs stop).
                **self._diagnostic_containers(),
                "stop_text_empty": self._stop_text_empty,
                "final_recovered": self._final_recovered_from_t is not None,
                "final_recovered_from_t": self._final_recovered_from_t,
                "final_rescue_mode": self._final_rescue_mode,
                # Same time basis as the partials' "t"; lets the replay bench
                # re-run finalize_transcript with the timing rule intact.
                "input_ended_t": (
                    self._input_ended_at - self._started_at
                    if self._input_ended_at is not None
                    and self._started_at is not None else None),
            },
        }

    def _diagnostic_containers(self) -> dict[str, Any]:
        """Copy the worker-thread-mutated diagnostic containers under the view
        lock so a snapshot() racing an in-flight _drain_text/stop cannot raise
        'changed size during iteration'."""
        with self._view_lock:
            return {
                "partials": list(self._partial_history),
                "partials_dropped": self._partial_history_dropped,
                "live_lines": [
                    {"order": order, "line_id": line_id, "text": text}
                    for line_id, (order, text) in sorted(
                        self._lines.items(), key=lambda kv: kv[1][0])
                ],
                "stop_lines": list(self._stop_lines),
            }

    def _listener(self, event: Any) -> None:
        error = getattr(event, "error", None)
        line = getattr(event, "line", None)
        snapshot = _TextSnapshot(
            line_id=getattr(line, "line_id", None) if line is not None else None,
            text=str(getattr(line, "text", "") or "") if line is not None else "",
            complete=bool(getattr(line, "is_complete", False))
            if line is not None else False,
            error=(f"{type(error).__name__}: {error}"
                   if error is not None else None),
        )
        try:
            self._text.put_nowait(snapshot)
        except queue.Full:
            self._text_event_drops += 1

    def _drain_text(self) -> None:
        while True:
            try:
                snapshot = self._text.get_nowait()
            except queue.Empty:
                return
            if snapshot.error:
                self._fail(f"moonshine_event_error:{snapshot.error}")
            line_id = snapshot.line_id
            # _view_lock guards the containers snapshot() reads cross-thread.
            with self._view_lock:
                if line_id not in self._lines:
                    self._lines[line_id] = (self._line_order, snapshot.text)
                    self._line_order += 1
                else:
                    order, _ = self._lines[line_id]
                    self._lines[line_id] = (order, snapshot.text)
                hypothesis = " ".join(
                    text.strip() for _, text in sorted(self._lines.values())
                    if text.strip()).strip()
                if hypothesis != self._last_partial:
                    self._last_partial = hypothesis
                    self._partial_updates += 1
                    if hypothesis and self._first_partial_s is None:
                        assert self._started_at is not None
                        self._first_partial_s = self._clock() - self._started_at
                    if len(self._partial_history) < _PARTIAL_HISTORY_MAX:
                        started = self._started_at
                        self._partial_history.append({
                            "t": (round(self._clock() - started, 3)
                                  if started is not None else None),
                            "line_id": line_id,
                            "line_complete": snapshot.complete,
                            "text": hypothesis,
                        })
                    else:
                        self._partial_history_dropped += 1

    def _run(self) -> None:
        transcriber = None
        stream = None
        stream_started = False
        try:
            transcriber = self._factory()
            self._identity = getattr(transcriber, "_hw1_live_identity", None)
            stream = transcriber.create_stream(
                update_interval=self._update_interval_s)
            stream.add_listener(self._listener)
            stream.start()
            stream_started = True
            self._ready.set()

            while True:
                try:
                    item = self._audio.get(timeout=0.05)
                except queue.Empty:
                    if self._input_done.is_set():
                        break
                    continue
                age_ms = max(0.0, (self._clock() - item.enqueued_at) * 1000.0)
                self._queue_ages_ms.append(age_ms)
                stream.add_audio(_pcm16_floats(item.pcm), SAMPLE_RATE)
                self._processed_bytes += len(item.pcm)
                self._drain_text()

            stop_text = ""
            result = stream.stop()
            self._stop_returned = result is not None
            if result is None:
                self._fail("missing_stop_result")
            else:
                stop_text = _transcript_text(result)
                stop_lines = getattr(result, "lines", None) or []
                with self._view_lock:
                    for ln in list(stop_lines)[:_PARTIAL_HISTORY_MAX]:
                        self._stop_lines.append({
                            "line_id": getattr(ln, "line_id", None),
                            "start_time": getattr(ln, "start_time", None),
                            "complete": bool(getattr(ln, "is_complete", False)),
                            "text": str(getattr(ln, "text", "") or ""),
                        })
            # Drain post-stop events BEFORE finalizing: stop() itself can emit
            # the erasing "" update, so the partial history must be complete.
            self._drain_text()
            if self._stop_returned:
                with self._view_lock:
                    history = list(self._partial_history)
                ended_t = (self._input_ended_at - self._started_at
                           if self._input_ended_at is not None
                           and self._started_at is not None else None)
                final = finalize_transcript(
                    stop_text, history, input_ended_t=ended_t)
                self._text_result = final["text"]
                self._final_rescue_mode = final["mode"]
                self._stop_text_empty = final["mode"] in (
                    "empty", "empty_rescue", "empty_rescue_incomplete")
                if final["rescued_from_t"] is not None:
                    self._final_recovered_from_t = final["rescued_from_t"]
            if self._text_event_drops:
                self._fail("text_event_queue_overflow")
            if self._input_ended_at is not None:
                self._end_to_final_s = max(
                    0.0, self._clock() - self._input_ended_at)
        except BaseException as exc:
            if not self._ready.is_set():
                self._startup_error = exc
            self._fail(f"worker_error:{type(exc).__name__}:{exc}")
        finally:
            self._ready.set()
            if stream is not None and stream_started:
                try:
                    stream.close()
                except BaseException as exc:
                    self._fail(f"stream_close_error:{type(exc).__name__}:{exc}")
            if transcriber is not None:
                try:
                    transcriber.close()
                except BaseException as exc:
                    self._fail(
                        f"transcriber_close_error:{type(exc).__name__}:{exc}")
            self._done.set()
