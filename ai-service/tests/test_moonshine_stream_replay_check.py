from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


_TOOL_PATH = (Path(__file__).resolve().parents[1] / "tools"
              / "moonshine_stream_replay_check.py")
_SPEC = importlib.util.spec_from_file_location(
    "moonshine_stream_replay_check", _TOOL_PATH)
assert _SPEC is not None and _SPEC.loader is not None
check = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = check
_SPEC.loader.exec_module(check)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MoonshineReplayCheckTests(unittest.TestCase):
    def make_fixture(self, root: Path, *, interval=0.5):
        model_dir = root / "model"
        model_dir.mkdir()
        manifest_cases = {}
        records = [{
            "schema": 1,
            "type": "model_identity",
            "run_id": "run",
            "model_dir": str(model_dir.resolve()),
            "model_arch_requested": "medium-streaming",
            "model_path": str(model_dir.resolve()),
            "model_arch": "MEDIUM_STREAMING",
            "runtime_identity": {
                "model": {
                    "directory": str(model_dir.resolve()),
                    "file_count": 1,
                    "total_bytes": 1,
                    "tree_sha256": "a" * 64,
                },
                "native_library": {
                    "api_version": 1,
                    "sha256": "b" * 64,
                },
                "moonshine_distribution_version": "0.1.1",
                "moonshine_package_file_sha256": "c" * 64,
                "moonshine_transcriber_file_sha256": "d" * 64,
                "model_arch_requested": "medium-streaming",
                "model_arch_enum": "MEDIUM_STREAMING",
                "transcriber_options": {"return_audio_data": "false"},
            },
        }]
        for index, (stem, frames, chunks, seconds) in enumerate((
            ("001", 24576, 12, 1.536),
            ("002", 40960, 20, 2.560),
            ("005", 67584, 33, 4.224),
        ), start=1):
            wav = root / f"{stem}.wav"
            label = root / f"{stem}.txt"
            reference = f"reference phrase for sample {stem}"
            wav.write_bytes((stem.encode("ascii") + b"-wav") * 7)
            label.write_text(reference + "\n", encoding="utf-8")
            manifest_cases[stem] = {
                "wav_sha256": sha256(wav),
                "reference_sha256": sha256(label),
                "frames": frames,
                "chunks": chunks,
                "max_word_errors": 1,
            }
            case_id = f"{index:04d}"
            required = int(seconds)
            partial_records = []
            for partial in range(required):
                partial_time = (partial + 1) * seconds / (required + 1)
                partial_records.append({
                    "schema": 1,
                    "type": "stt_event",
                    "run_id": "run",
                    "case_id": case_id,
                    "t_s": partial_time,
                    "line_complete": False,
                    "hypothesis": " ".join(
                        reference.split()[:partial + 1]),
                    "hypothesis_changed": True,
                })
            records.append({
                "schema": 1,
                "type": "case_summary",
                "run_id": "run",
                "case_id": case_id,
                "ok": True,
                "failure_reasons": [],
                "wav": str(wav),
                "expected_source": str(label),
                "model_dir": str(model_dir.resolve()),
                "model_arch_requested": "medium-streaming",
                "model_path": str(model_dir.resolve()),
                "model_arch": "MEDIUM_STREAMING",
                "runtime_identity": {
                    "model": {
                        "directory": str(model_dir.resolve()),
                        "file_count": 1,
                        "total_bytes": 1,
                        "tree_sha256": "a" * 64,
                    },
                    "native_library": {
                        "api_version": 1,
                        "sha256": "b" * 64,
                    },
                    "moonshine_distribution_version": "0.1.1",
                    "moonshine_package_file_sha256": "c" * 64,
                    "moonshine_transcriber_file_sha256": "d" * 64,
                    "model_arch_requested": "medium-streaming",
                    "model_arch_enum": "MEDIUM_STREAMING",
                    "transcriber_options": {"return_audio_data": "false"},
                },
                "update_interval_s": interval,
                "pace": 1.0,
                "audio": {
                    "sample_rate": 16000,
                    "channels": 1,
                    "sample_width_bytes": 2,
                    "frames": frames,
                    "bytes": frames * 2,
                    "seconds": seconds,
                    "enqueued_seconds": seconds,
                    "chunk_bytes": 4096,
                    "chunks_total": chunks,
                    "chunks_enqueued": chunks,
                    "chunks_processed": chunks,
                    "chunks_dropped": 0,
                    "partial_last_chunk_bytes": 0,
                },
                "queue": {
                    "capacity_chunks": 8,
                    "high_water_chunks": 1,
                    "overflowed": False,
                    "age_ms_max": 2.0,
                },
                "stream": {
                    "text": reference,
                    "stop_returned_transcript": True,
                    "text_event_drops": 0,
                    "wall_seconds": seconds + 0.2,
                    "end_to_final_seconds": 0.2,
                },
                "batch": {
                    "enabled": True,
                    "text": reference,
                    "accuracy": {
                        "reference_text": reference,
                        "reference_words": 5,
                        "hypothesis_words": 5,
                        "word_errors": 0,
                        "wer": 0.0,
                        "hallucinated_final": False,
                    },
                    "stream_vs_batch": {
                        "reference_text": reference,
                        "reference_words": 5,
                        "hypothesis_words": 5,
                        "word_errors": 0,
                        "wer": 0.0,
                        "hallucinated_final": False,
                    },
                },
                "accuracy": {
                    "reference_text": reference,
                    "reference_words": 5,
                    "hypothesis_words": 5,
                    "word_errors": 0,
                    "wer": 0.0,
                    "hallucinated_final": False,
                },
                "system": {
                    "governors": ["performance"],
                    "temperature_c_max": 50.0,
                    "swap_used_bytes_start": 0,
                    "swap_used_bytes_end": 0,
                    "swap_used_bytes_max": 0,
                    "process_cpu_percent": 75.0,
                    "process_max_rss_bytes": 1234,
                },
            })
            records.insert(-1, {
                "schema": 1,
                "type": "case_start",
                "run_id": "run",
                "case_id": case_id,
                "wav": str(wav),
                "model_dir": str(model_dir.resolve()),
                "model_arch_requested": "medium-streaming",
                "model_path": str(model_dir.resolve()),
                "model_arch": "MEDIUM_STREAMING",
                "runtime_identity": records[0]["runtime_identity"],
                "update_interval_s": interval,
                "queue_capacity_chunks": 8,
                "text_queue_capacity_events": 64,
                "pace": 1.0,
                "audio_seconds": seconds,
                "audio_chunks": chunks,
                "partial_last_chunk_bytes": 4096,
            })
            records.insert(-1, {
                "schema": 1,
                "type": "stream_started",
                "run_id": "run",
                "case_id": case_id,
                "t_s": 0.01,
            })
            for chunk_index in range(chunks):
                records.insert(-1, {
                    "schema": 1,
                    "type": "audio_chunk",
                    "run_id": "run",
                    "case_id": case_id,
                    "chunk_index": chunk_index,
                    "audio_end_s": (chunk_index + 1) * 0.128,
                    "bytes": 4096,
                    "queue_age_ms": 2.0,
                    "add_audio_ms": 1.0,
                    "queue_depth_after": 0,
                })
            for partial_record in partial_records:
                records.insert(-1, partial_record)
        manifest = {
            "schema": 1,
            "name": "test",
            "full_gate": False,
            "corpus_directory": str(root.resolve()),
            "limitations": ["no_negative_control"],
            "model": {
                "directory": str(model_dir.resolve()),
                "requested_arch": "medium-streaming",
                "enum_name": "MEDIUM_STREAMING",
            },
            "runtime": {
                "moonshine_distribution_version": "0.1.1",
            },
            "policy": {
                "allowed_update_intervals_seconds": [0.5, 1.0],
                "audio_queue_chunks": 8,
                "text_queue_events": 64,
                "max_stream_start_seconds": 2.0,
                "max_stream_wall_over_audio_seconds": 2.0,
                "max_end_to_final_seconds": 0.8,
                "max_queue_age_ms": 1024.0,
                "max_temperature_c": 80.0,
                "max_aggregate_wer": 0.2,
                "max_partial_gap_seconds": 1.35,
                "min_partial_updates_per_second": 1.0,
                "partial_post_end_tolerance_seconds": 0.35,
            },
            "cases": manifest_cases,
        }
        return records, manifest, model_dir

    def add_negative_fixture(self, root, records, manifest):
        """Append one empty-reference control with a clean empty final."""
        wav = root / "neg001.wav"
        label = root / "neg001.txt"
        wav.write_bytes((b"neg001-wav") * 7)
        label.write_text("", encoding="utf-8")
        manifest["cases"]["neg001"] = {
            "kind": "negative",
            "wav_sha256": sha256(wav),
            "reference_sha256": sha256(label),
            "frames": 24576,
            "chunks": 12,
        }

        source = [
            record for record in records
            if record.get("case_id") == "0001"
            and record.get("type") != "stt_event"
        ]
        cloned = copy.deepcopy(source)
        for record in cloned:
            record["case_id"] = "0004"
            if record.get("type") == "case_start":
                record["wav"] = str(wav)
            elif record.get("type") == "case_summary":
                empty_score = check._score_words("", "")
                record["wav"] = str(wav)
                record["expected_source"] = str(label)
                record["stream"]["text"] = ""
                record["accuracy"] = empty_score
                record["batch"]["text"] = ""
                record["batch"]["accuracy"] = dict(empty_score)
                record["batch"]["stream_vs_batch"] = dict(empty_score)
        records.extend(cloned)
        return wav, label

    def run_check(self, records, manifest, model_dir, **overrides):
        kwargs = {
            "expected_update_interval_s": 0.5,
            "expected_model_dir": model_dir,
            "throttle_before": 0,
            "throttle_after": 0,
        }
        kwargs.update(overrides)
        return check.check_replay(records, manifest, **kwargs)

    def test_happy_slice_is_explicitly_provisional(self):
        with tempfile.TemporaryDirectory() as directory:
            records, manifest, model = self.make_fixture(Path(directory))
            report = self.run_check(records, manifest, model)
        self.assertTrue(report["ok"], report["failure_reasons"])
        self.assertFalse(report["full_gate0a_complete"])
        self.assertEqual(report["aggregate"]["stream_word_errors"], 0)
        self.assertEqual(report["cases"]["005"][
            "useful_pre_end_partial_updates"], 4)

    def test_empty_reference_negative_control_passes_without_affecting_wer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records, manifest, model = self.make_fixture(root)
            self.add_negative_fixture(root, records, manifest)
            report = self.run_check(records, manifest, model)
        self.assertTrue(report["ok"], report["failure_reasons"])
        self.assertEqual(report["cases"]["neg001"]["kind"], "negative")
        self.assertEqual(report["cases"]["neg001"][
            "required_pre_end_partial_updates"], 0)
        self.assertEqual(report["aggregate"]["reference_words"], 15)
        self.assertEqual(report["aggregate"]["negative_controls"], 1)
        self.assertEqual(report["aggregate"][
            "negative_stream_hallucinations"], 0)
        self.assertEqual(report["aggregate"][
            "negative_batch_hallucinations"], 0)

    def test_negative_stream_and_batch_hallucinations_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records, manifest, model = self.make_fixture(root)
            self.add_negative_fixture(root, records, manifest)
            summary = next(
                record for record in records
                if record.get("type") == "case_summary"
                and Path(record["wav"]).stem == "neg001"
            )
            hallucination = "phantom command"
            summary["stream"]["text"] = hallucination
            summary["accuracy"] = check._score_words("", hallucination)
            summary["batch"]["text"] = hallucination
            summary["batch"]["accuracy"] = check._score_words(
                "", hallucination)
            summary["batch"]["stream_vs_batch"] = check._score_words(
                hallucination, hallucination)
            report = self.run_check(records, manifest, model)
        self.assertFalse(report["ok"])
        self.assertIn("case_neg001_negative_stream_hallucination",
                      report["failure_reasons"])
        self.assertIn("case_neg001_negative_batch_hallucination",
                      report["failure_reasons"])

    def test_stale_collector_accuracy_cannot_hide_behind_collector_ok(self):
        with tempfile.TemporaryDirectory() as directory:
            records, manifest, model = self.make_fixture(Path(directory))
            summary = next(record for record in records
                           if record.get("type") == "case_summary"
                           and Path(record["wav"]).stem == "002")
            summary["accuracy"]["word_errors"] = 2
            report = self.run_check(records, manifest, model)
        self.assertFalse(report["ok"])
        self.assertTrue(any("case_002_collector_stream_score_mismatch" in reason
                            for reason in report["failure_reasons"]))

    def test_equally_wrong_stream_and_batch_fail_absolute_accuracy(self):
        with tempfile.TemporaryDirectory() as directory:
            records, manifest, model = self.make_fixture(Path(directory))
            summary = next(record for record in records
                           if record.get("type") == "case_summary"
                           and Path(record["wav"]).stem == "002")
            reference = "reference phrase for sample 002"
            wrong = "unrelated answer"
            summary["stream"]["text"] = wrong
            summary["accuracy"] = check._score_words(reference, wrong)
            summary["batch"]["text"] = wrong
            summary["batch"]["accuracy"] = check._score_words(reference, wrong)
            summary["batch"]["stream_vs_batch"] = check._score_words(wrong, wrong)
            report = self.run_check(records, manifest, model)
        self.assertFalse(report["ok"])
        self.assertTrue(any("case_002_absolute_stream_errors" in reason
                            for reason in report["failure_reasons"]))

    def test_mixed_run_ids_and_clustered_partials_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            records, manifest, model = self.make_fixture(Path(directory))
            summary = next(record for record in records
                           if record.get("type") == "case_summary"
                           and Path(record["wav"]).stem == "005")
            summary["run_id"] = "other-run"
            for event in records:
                if event.get("type") == "stt_event" and event.get("case_id") == "0003":
                    event["t_s"] = 0.5
            report = self.run_check(records, manifest, model)
        self.assertFalse(report["ok"])
        self.assertIn("mixed_or_empty_run_id", report["failure_reasons"])
        self.assertTrue(any("case_005_partial_gap" in reason
                            for reason in report["failure_reasons"]))

    def test_missing_duration_boolean_interval_and_swap_excursion_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            records, manifest, model = self.make_fixture(Path(directory))
            summary = next(record for record in records
                           if record.get("type") == "case_summary"
                           and Path(record["wav"]).stem == "005")
            summary["audio"]["seconds"] = None
            summary["update_interval_s"] = True
            summary["stream"]["end_to_final_seconds"] = -0.1
            summary["system"]["swap_used_bytes_max"] = 4096
            report = self.run_check(records, manifest, model)
        self.assertFalse(report["ok"])
        reasons = report["failure_reasons"]
        self.assertIn("case_005_update_interval", reasons)
        self.assertIn("case_005_audio_duration", reasons)
        self.assertIn("case_005_end_to_final:None", reasons)
        self.assertIn("case_005_swap_growth:0.0->4096.0->0.0", reasons)

    def test_weakened_override_is_failed_and_contract_remains_effective(self):
        with tempfile.TemporaryDirectory() as directory:
            records, manifest, model = self.make_fixture(Path(directory))
            report = self.run_check(
                records, manifest, model, max_end_to_final_s=99.0,
                min_partial_updates_per_s=0.1,
            )
        self.assertFalse(report["ok"])
        self.assertIn(
            "weakened_policy:max_end_to_final_seconds:99.0>0.8",
            report["failure_reasons"],
        )
        self.assertEqual(report["effective_policy"][
            "max_end_to_final_seconds"], 0.8)
        self.assertEqual(report["effective_policy"][
            "min_partial_updates_per_second"], 1.0)

    def test_cadence_must_be_one_of_the_manifest_intervals(self):
        with tempfile.TemporaryDirectory() as directory:
            records, manifest, model = self.make_fixture(
                Path(directory), interval=0.25)
            report = self.run_check(
                records, manifest, model,
                expected_update_interval_s=0.25,
            )
        self.assertFalse(report["ok"])
        self.assertIn("update_interval_not_contract:0.25",
                      report["failure_reasons"])

    def test_one_second_cadence_is_an_allowed_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            records, manifest, model = self.make_fixture(
                Path(directory), interval=1.0)
            report = self.run_check(
                records, manifest, model,
                expected_update_interval_s=1.0,
            )
        self.assertTrue(report["ok"], report["failure_reasons"])

    def test_case_and_punctuation_toggles_are_not_useful_partials(self):
        with tempfile.TemporaryDirectory() as directory:
            records, manifest, model = self.make_fixture(Path(directory))
            toggle = 0
            for event in records:
                if event.get("type") == "stt_event" and event.get("case_id") == "0003":
                    event["hypothesis"] = (
                        "reference" if toggle % 2 == 0 else "Reference!")
                    event["hypothesis_changed"] = True
                    toggle += 1
            report = self.run_check(records, manifest, model)
        self.assertFalse(report["ok"])
        self.assertTrue(any("case_005_useful_partials" in reason
                            for reason in report["failure_reasons"]))

    def test_single_character_advances_are_not_completed_word_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            records, manifest, model = self.make_fixture(Path(directory))
            hypotheses = iter(("ref", "refe", "refer", "refere"))
            for event in records:
                if event.get("type") == "stt_event" and event.get("case_id") == "0003":
                    event["hypothesis"] = next(hypotheses)
                    event["hypothesis_changed"] = True
            report = self.run_check(records, manifest, model)
        self.assertFalse(report["ok"])
        self.assertTrue(any("case_005_useful_partials:0<4" in reason
                            for reason in report["failure_reasons"]))

    def test_case_start_lifecycle_and_file_order_are_authoritative(self):
        with tempfile.TemporaryDirectory() as directory:
            records, manifest, model = self.make_fixture(Path(directory))
            case_start = next(record for record in records
                              if record.get("type") == "case_start"
                              and record.get("case_id") == "0002")
            case_start["pace"] = 0.0
            case_start["queue_capacity_chunks"] = 999
            case_start["model_arch_requested"] = "tiny-streaming"
            stream_started = next(record for record in records
                                  if record.get("type") == "stream_started"
                                  and record.get("case_id") == "0002")
            stream_started["t_s"] = 999.0
            chunk_positions = [index for index, record in enumerate(records)
                               if record.get("type") == "audio_chunk"
                               and record.get("case_id") == "0002"]
            records[chunk_positions[0]], records[chunk_positions[1]] = (
                records[chunk_positions[1]], records[chunk_positions[0]])
            event_position = next(index for index, record in enumerate(records)
                                  if record.get("type") == "stt_event"
                                  and record.get("case_id") == "0002")
            event = records.pop(event_position)
            start_position = next(index for index, record in enumerate(records)
                                  if record is case_start)
            records.insert(start_position, event)
            report = self.run_check(records, manifest, model)
        self.assertFalse(report["ok"])
        reasons = report["failure_reasons"]
        self.assertIn("case_lifecycle_bounds:0002", reasons)
        self.assertIn("case_002_case_start_model_identity", reasons)
        self.assertIn("case_002_case_start_pace", reasons)
        self.assertIn("case_002_case_start_audio_queue", reasons)
        self.assertIn("case_002_stream_started_s:999.0", reasons)
        self.assertIn("case_002_audio_chunk_records", reasons)

    def test_stream_wall_time_must_demonstrate_real_time_pacing(self):
        with tempfile.TemporaryDirectory() as directory:
            records, manifest, model = self.make_fixture(Path(directory))
            for record in records:
                if record.get("type") == "case_summary":
                    record["stream"]["wall_seconds"] = 0.001
            report = self.run_check(records, manifest, model)
        self.assertFalse(report["ok"])
        self.assertTrue(any("stream_wall_seconds:0.001" in reason
                            for reason in report["failure_reasons"]))

    def test_actual_model_path_and_runtime_model_must_match_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records, manifest, model = self.make_fixture(root)
            wrong = root / "wrong-model"
            wrong.mkdir()
            for record in records:
                if record.get("type") in {"model_identity", "case_summary"}:
                    record["model_path"] = str(wrong)
                    record["model_arch"] = "TINY_STREAMING"
                    identity = record["runtime_identity"]
                    identity["model"]["directory"] = str(wrong)
                    identity["model_arch_enum"] = "TINY_STREAMING"
            report = self.run_check(records, manifest, model)
        self.assertFalse(report["ok"])
        self.assertTrue(any(reason.startswith("model_path:")
                            for reason in report["failure_reasons"]))
        self.assertTrue(any(reason.startswith("runtime_model_dir:")
                            for reason in report["failure_reasons"]))

    def test_latency_and_final_only_partial_do_not_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            records, manifest, model = self.make_fixture(Path(directory))
            summary = next(record for record in records
                           if record.get("type") == "case_summary"
                           and Path(record["wav"]).stem == "005")
            summary["stream"]["end_to_final_seconds"] = 0.9
            for event in records:
                if event.get("type") == "stt_event" and event.get("case_id") == "0003":
                    event["line_complete"] = True
            report = self.run_check(records, manifest, model)
        self.assertFalse(report["ok"])
        self.assertTrue(any("case_005_end_to_final" in reason
                            for reason in report["failure_reasons"]))
        self.assertTrue(any("case_005_useful_partials" in reason
                            for reason in report["failure_reasons"]))

    def test_corpus_hash_and_throttle_are_authoritative(self):
        with tempfile.TemporaryDirectory() as directory:
            records, manifest, model = self.make_fixture(Path(directory))
            (Path(directory) / "001.wav").write_bytes(b"different")
            report = self.run_check(
                records, manifest, model, throttle_after=0x50005)
        self.assertFalse(report["ok"])
        self.assertTrue(any("case_001_wav_sha256" in reason
                            for reason in report["failure_reasons"]))
        self.assertIn("throttle_after:0x50005", report["failure_reasons"])

    def test_missing_label_and_queue_lag_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records, manifest, model = self.make_fixture(root)
            (root / "002.txt").unlink()
            summary = next(record for record in records
                           if record.get("type") == "case_summary"
                           and Path(record["wav"]).stem == "005")
            summary["queue"]["age_ms_max"] = 1200.0
            report = self.run_check(records, manifest, model)
        self.assertFalse(report["ok"])
        self.assertTrue(any("case_002_reference_unreadable" in reason
                            for reason in report["failure_reasons"]))
        self.assertIn("case_005_queue_age_ms:1200.0",
                      report["failure_reasons"])

    def test_bundled_manifest_contract_hash_is_pinned(self):
        manifest_path = _TOOL_PATH.with_name(
            "moonshine_gate0a_medium_slice.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            check._contract_sha256(manifest),
            check._CANONICAL_CONTRACT_SHA256,
        )
        self.assertEqual(manifest["name"], check._CANONICAL_MANIFEST_NAME)

    def test_cli_refuses_output_alias_and_implicit_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jsonl = root / "run.jsonl"
            manifest = root / "manifest.json"
            before = root / "before.txt"
            after = root / "after.txt"
            output = root / "report.json"
            jsonl.write_text("{}\n", encoding="utf-8")
            manifest.write_text("{}\n", encoding="utf-8")
            before.write_text("throttled=0x0\n", encoding="ascii")
            after.write_text("throttled=0x0\n", encoding="ascii")
            output.write_text("keep\n", encoding="utf-8")
            hardlink = root / "hardlink-report.json"
            os.link(jsonl, hardlink)
            base = [
                str(jsonl), "--manifest", str(manifest),
                "--expected-update-interval", "0.5",
                "--expected-model-dir", str(root / "model"),
                "--throttled-before", str(before),
                "--throttled-after", str(after),
            ]
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    check.main([*base, "--output", str(manifest)])
                with self.assertRaises(SystemExit):
                    check.main([*base, "--output", str(output)])
                with self.assertRaises(SystemExit):
                    check.main([*base, "--output", str(hardlink)])
                reused_throttle = [
                    str(jsonl), "--manifest", str(manifest),
                    "--expected-update-interval", "0.5",
                    "--expected-model-dir", str(root / "model"),
                    "--throttled-before", str(before),
                    "--throttled-after", str(before),
                    "--output", str(root / "new-report.json"),
                ]
                with self.assertRaises(SystemExit):
                    check.main(reused_throttle)
            self.assertEqual(output.read_text(encoding="utf-8"), "keep\n")


if __name__ == "__main__":
    unittest.main()
