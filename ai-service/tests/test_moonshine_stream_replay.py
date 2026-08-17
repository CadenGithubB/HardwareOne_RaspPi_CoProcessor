from __future__ import annotations

import contextlib
from dataclasses import dataclass
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
import wave


_TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "moonshine_stream_replay.py"
_SPEC = importlib.util.spec_from_file_location("moonshine_stream_replay", _TOOL_PATH)
assert _SPEC is not None and _SPEC.loader is not None
replay = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = replay
_SPEC.loader.exec_module(replay)


@dataclass
class FakeLine:
    text: str
    line_id: int = 1
    is_complete: bool = False
    last_transcription_latency_ms: int = 0
    start_time: float = 0.0


@dataclass
class FakeTranscript:
    lines: list[FakeLine]


@dataclass
class LineTextChanged:
    line: FakeLine


@dataclass
class LineCompleted:
    line: FakeLine


class FakeSampler:
    def __init__(self) -> None:
        self.calls = 0

    def sample(self):
        self.calls += 1
        return {
            "rss_bytes": 1000 + self.calls,
            "temperature_c": 40.0 + self.calls,
            "frequency_khz_min": 1_500_000,
            "frequency_khz_max": 2_400_000,
            "governors": ["schedutil"],
            "host_cpu_total_ticks": 1000 + self.calls * 100,
            "host_cpu_idle_ticks": 500 + self.calls * 40,
            "mem_available_bytes": 2_000_000,
            "swap_used_bytes": 0,
        }


class FakeStream:
    def __init__(self, owner, *, stop_returns_none=False, add_delay=0.0):
        self.owner = owner
        self.stop_returns_none = stop_returns_none
        self.add_delay = add_delay
        self.listener = None
        self.add_count = 0

    def _record(self, name):
        self.owner.calls.append(name)
        self.owner.thread_ids.append(threading.get_ident())

    def add_listener(self, listener):
        self._record("add_listener")
        self.listener = listener

    def start(self):
        self._record("start")

    def add_audio(self, audio, sample_rate=16_000):
        self._record("add_audio")
        self.owner.chunk_sizes.append(len(audio))
        self.owner.sample_rates.append(sample_rate)
        self.add_count += 1
        if self.add_delay:
            time.sleep(self.add_delay)
        if self.add_count == 1:
            self.listener(LineTextChanged(FakeLine(
                "hello wur", last_transcription_latency_ms=31)))
        elif self.add_count == 2:
            self.listener(LineTextChanged(FakeLine(
                "hello world", last_transcription_latency_ms=42)))

    def stop(self):
        self._record("stop")
        self.listener(LineCompleted(FakeLine(
            "hello world", is_complete=True, last_transcription_latency_ms=45)))
        if self.stop_returns_none:
            return None
        return FakeTranscript([FakeLine("hello world", is_complete=True)])

    def close(self):
        self._record("close")


class FakeTranscriber:
    def __init__(self, *, stop_returns_none=False, add_delay=0.0):
        self.stop_returns_none = stop_returns_none
        self.add_delay = add_delay
        self.calls = []
        self.thread_ids = []
        self.chunk_sizes = []
        self.sample_rates = []
        self.update_intervals = []
        self._model_path = "/fake/medium-streaming-en"
        self._model_arch = "MEDIUM_STREAMING"

    def create_stream(self, update_interval=None):
        self.calls.append("create_stream")
        self.thread_ids.append(threading.get_ident())
        self.update_intervals.append(update_interval)
        return FakeStream(
            self,
            stop_returns_none=self.stop_returns_none,
            add_delay=self.add_delay,
        )

    def transcribe_without_streaming(self, audio, sample_rate=16_000):
        self.calls.append("batch")
        self.thread_ids.append(threading.get_ident())
        return FakeTranscript([FakeLine("hello world", is_complete=True)])


def write_wav(path: Path, chunks: int, *, channels=1, rate=16_000,
              width=2) -> None:
    sample_count = chunks * replay.CHUNK_SAMPLES * channels
    raw = (b"\x00\x01" * sample_count) if width == 2 else (b"\x01" * sample_count)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(raw)


class MoonshineStreamReplayTests(unittest.TestCase):
    def test_paced_pipeline_tracks_chunks_revisions_accuracy_and_thread_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            wav_path = Path(directory) / "case.wav"
            write_wav(wav_path, 3)
            wav = replay.read_xiao_wav(wav_path)
            fake = FakeTranscriber()
            records = []
            main_thread = threading.get_ident()

            result = replay.run_replay(
                fake,
                wav,
                model_dir="/fake/medium-streaming-en",
                model_arch="medium-streaming",
                update_interval_s=0.5,
                queue_chunks=8,
                pace=0,
                expected_text="Hello, world!",
                expected_source="test",
                include_batch=True,
                sink=records.append,
                sampler=FakeSampler(),
                worker_timeout_s=2.0,
                run_id="run",
                case_id="case",
            )

        self.assertTrue(result["ok"], result["failure_reasons"])
        self.assertEqual(result["audio"]["chunks_total"], 3)
        self.assertEqual(result["audio"]["chunks_processed"], 3)
        self.assertEqual(fake.chunk_sizes, [replay.CHUNK_SAMPLES] * 3)
        self.assertEqual(fake.sample_rates, [replay.SAMPLE_RATE] * 3)
        self.assertEqual(fake.update_intervals, [0.5])
        self.assertEqual(result["stream"]["text"], "hello world")
        self.assertEqual(result["stream"]["partial_updates"], 2)
        self.assertEqual(result["stream"]["revision_updates"], 1)
        self.assertEqual(result["stream"]["max_retracted_chars"], 2)
        self.assertEqual(result["stream"]["native_latency_ms_max"], 42.0)
        self.assertEqual(result["accuracy"]["word_errors"], 0)
        self.assertEqual(result["accuracy"]["wer"], 0.0)
        self.assertEqual(result["batch"]["text"], "hello world")
        self.assertEqual(result["batch"]["stream_vs_batch"]["word_errors"], 0)
        self.assertEqual(set(fake.thread_ids).__len__(), 1)
        self.assertNotEqual(fake.thread_ids[0], main_thread)
        self.assertLess(fake.calls.index("stop"), fake.calls.index("close"))
        self.assertLess(fake.calls.index("close"), fake.calls.index("batch"))
        self.assertEqual(records[-1]["type"], "case_summary")
        event_records = [item for item in records if item["type"] == "stt_event"]
        self.assertTrue(any(item.get("retracted_chars") == 2
                            for item in event_records))

    def test_none_from_stop_is_a_failure_not_an_empty_success(self):
        with tempfile.TemporaryDirectory() as directory:
            wav_path = Path(directory) / "case.wav"
            write_wav(wav_path, 2)
            result = replay.run_replay(
                FakeTranscriber(stop_returns_none=True),
                replay.read_xiao_wav(wav_path),
                model_dir="/fake/medium-streaming-en",
                model_arch="medium-streaming",
                queue_chunks=4,
                pace=0,
                include_batch=False,
                sampler=FakeSampler(),
                worker_timeout_s=2.0,
            )
        self.assertFalse(result["ok"])
        self.assertIn("missing_stop_result", result["failure_reasons"])
        self.assertFalse(result["stream"]["stop_returned_transcript"])

    def test_bounded_queue_overflow_is_visible_and_drops_no_silent_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            wav_path = Path(directory) / "overload.wav"
            write_wav(wav_path, 12)
            result = replay.run_replay(
                FakeTranscriber(add_delay=0.03),
                replay.read_xiao_wav(wav_path),
                model_dir="/fake/medium-streaming-en",
                model_arch="medium-streaming",
                queue_chunks=1,
                pace=0,
                include_batch=False,
                sampler=FakeSampler(),
                worker_timeout_s=3.0,
            )
        self.assertFalse(result["ok"])
        self.assertIn("audio_queue_overflow", result["failure_reasons"])
        self.assertTrue(result["queue"]["overflowed"])
        self.assertGreater(result["audio"]["chunks_dropped"], 0)
        self.assertEqual(result["audio"]["chunks_processed"],
                         result["audio"]["chunks_enqueued"])

    def test_empty_reference_flags_hallucinated_final(self):
        score = replay.score_words("", "unexpected words")
        self.assertIsNone(score["wer"])
        self.assertTrue(score["hallucinated_final"])
        self.assertEqual(score["word_errors"], 2)

    def test_wav_shape_validation_rejects_non_xiao_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            wav_path = Path(directory) / "stereo.wav"
            write_wav(wav_path, 1, channels=2)
            with self.assertRaisesRegex(ValueError, "need mono"):
                replay.read_xiao_wav(wav_path)

    def test_sidecar_distinguishes_missing_from_empty_negative_label(self):
        with tempfile.TemporaryDirectory() as directory:
            wav_path = Path(directory) / "negative.wav"
            write_wav(wav_path, 1)
            self.assertEqual(replay.sidecar_reference(wav_path), (None, None))
            sidecar = wav_path.with_suffix(".txt")
            sidecar.write_text("\n", encoding="utf-8")
            expected, source = replay.sidecar_reference(wav_path)
            self.assertEqual(expected, "")
            self.assertEqual(source, str(sidecar))

    def test_transcript_lines_are_sorted_by_start_time_then_line_id(self):
        transcript = FakeTranscript([
            FakeLine("third", line_id=3, start_time=2.0),
            FakeLine("second", line_id=2, start_time=1.0),
            FakeLine("first", line_id=1, start_time=1.0),
        ])
        self.assertEqual(replay._transcript_text(transcript),
                         "first second third")

    def test_cli_requires_exact_model_identity_and_defaults_to_eight_chunks(self):
        parser = replay.build_parser()
        args = parser.parse_args([
            "corpus.wav",
            "--model-dir", "/models/medium",
            "--model-arch", "medium-streaming",
        ])
        self.assertEqual(args.model_dir, "/models/medium")
        self.assertEqual(args.model_arch, "medium-streaming")
        self.assertEqual(args.queue_chunks, replay.DEFAULT_QUEUE_CHUNKS)
        self.assertEqual(args.update_interval, 0.5)
        self.assertFalse(args.allow_non_performance)

    def test_governor_guard_requires_all_discovered_policies_to_be_performance(self):
        self.assertIsNone(replay.governor_guard_error(
            {"governors": ["performance"]}))
        self.assertIn("schedutil", replay.governor_guard_error(
            {"governors": ["performance", "schedutil"]}))
        self.assertIn("could not discover", replay.governor_guard_error(
            {"governors": []}))

    def test_direct_loader_passes_explicit_arch_and_disables_returned_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_dir = root / "model"
            model_dir.mkdir()
            (model_dir / "encoder.ort").write_bytes(b"model-data")
            native = root / "libmoonshine.so"
            native.write_bytes(b"native")
            captured = {}

            class ModelArch:
                TINY_STREAMING = 2
                SMALL_STREAMING = 4
                MEDIUM_STREAMING = 5

            class DirectTranscriber:
                def __init__(self, *, model_path, model_arch, options):
                    captured.update(model_path=model_path, model_arch=model_arch,
                                    options=options)
                    self._model_path = model_path
                    self._model_arch = model_arch
                    self._lib = types.SimpleNamespace(_name=str(native))
                    self.closed = False

                def get_version(self):
                    return 30_000

                def close(self):
                    self.closed = True

            module = types.ModuleType("moonshine_voice")
            module.__file__ = __file__
            module.__version__ = "0.1.1"
            module.ModelArch = ModelArch
            module.Transcriber = DirectTranscriber
            with mock.patch.dict(sys.modules, {"moonshine_voice": module}), \
                    mock.patch.object(replay.importlib_metadata, "version",
                                      return_value="0.1.1"):
                transcriber = replay.load_real_transcriber(
                    str(model_dir), "medium-streaming")

            self.assertEqual(captured["model_path"], str(model_dir.resolve()))
            self.assertEqual(captured["model_arch"], ModelArch.MEDIUM_STREAMING)
            self.assertEqual(captured["options"], {"return_audio_data": "false"})
            identity = transcriber._hw1_probe_identity
            self.assertEqual(identity["native_library"]["api_version"], 30_000)
            self.assertEqual(identity["model"]["file_count"], 1)
            self.assertEqual(len(identity["model"]["tree_sha256"]), 64)
            transcriber.close()

    def test_existing_output_is_not_replaced_without_force(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wav_path = root / "case.wav"
            write_wav(wav_path, 1)
            model_dir = root / "model"
            model_dir.mkdir()
            output = root / "result.jsonl"
            output.write_text("keep-me\n", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                replay.main([
                    str(wav_path),
                    "--model-dir", str(model_dir),
                    "--model-arch", "medium-streaming",
                    "--output", str(output),
                ])
            self.assertEqual(output.read_text(encoding="utf-8"), "keep-me\n")


if __name__ == "__main__":
    unittest.main()
