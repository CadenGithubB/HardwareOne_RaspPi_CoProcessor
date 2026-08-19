#!/usr/bin/env python3
"""Grade one paced Moonshine JSONL run without overstating Gate 0A.

The replay collector's ``case_summary.ok`` field covers runtime and queue
integrity.  It intentionally does not make product-policy decisions about
accuracy, finalization latency, partial usefulness, corpus identity, or host
thermal state.  This checker applies those decisions to one cadence and emits
one machine-readable report.

Passing the bundled three-positive/four-negative manifest is a *provisional
deployed medium replay slice*.  It can never report the full Gate 0A complete
because the positive slice is too small for model selection and the negative
slice covers only static-like background noise.  Cases marked
``kind: negative`` pin an empty sidecar; they are excluded from WER and
partial-cadence aggregation and instead must produce empty streaming and batch
finals.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Sequence


_THROTTLED_RE = re.compile(r"throttled=(0x[0-9a-fA-F]+)")
_WORD_RE = re.compile(r"[^\W_]+(?:['\N{RIGHT SINGLE QUOTATION MARK}][^\W_]+)*",
                      re.UNICODE)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CANONICAL_MANIFEST_NAME = "hardwareone-gate0a-medium-mixed-slice-v3"
_CANONICAL_CONTRACT_SHA256 = (
    "f6feac3d89e4cec05a0a8f4c586ff067ef53ab3d3b2757bc8c9fce504cfa832e"
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _file_evidence(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": _sha256_file(path),
    }


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read JSONL {path}: {exc}") from exc
    for line_number, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid JSONL {path}:{line_number}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise ValueError(f"JSONL record is not an object: {path}:{line_number}")
        records.append(value)
    if not records:
        raise ValueError(f"JSONL is empty: {path}")
    return records


def _parse_throttled(path: Path) -> int:
    try:
        text = path.read_text(encoding="ascii")
    except OSError as exc:
        raise ValueError(f"cannot read throttle evidence {path}: {exc}") from exc
    matches = _THROTTLED_RE.findall(text)
    if not matches:
        raise ValueError(f"missing throttled=0x... in {path}")
    combined = 0
    for match in matches:
        combined |= int(match, 16)
    return combined


def _contract_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _model_fingerprint(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "model_dir": record.get("model_dir"),
        "model_arch_requested": record.get("model_arch_requested"),
        "model_path": record.get("model_path"),
        "model_arch": record.get("model_arch"),
        "runtime_identity": record.get("runtime_identity"),
    }


def _word_error_count(score: Any) -> int | None:
    if not isinstance(score, dict):
        return None
    value = score.get("word_errors")
    return (value if isinstance(value, int) and not isinstance(value, bool)
            and value >= 0 else None)


def _reference_word_count(score: Any) -> int | None:
    if not isinstance(score, dict):
        return None
    value = score.get("reference_words")
    return (value if isinstance(value, int) and not isinstance(value, bool)
            and value >= 0 else None)


def _finite_number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _nonnegative_number(value: Any) -> float | None:
    result = _finite_number(value)
    return result if result is not None and result >= 0 else None


def _word_tokens(text: str) -> list[str]:
    return [token.replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")
            for token in _WORD_RE.findall(text.casefold())]


def _edit_distance(reference: Sequence[Any], hypothesis: Sequence[Any]) -> int:
    if len(reference) < len(hypothesis):
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


def _score_words(reference: str, hypothesis: str) -> dict[str, Any]:
    ref = _word_tokens(reference)
    hyp = _word_tokens(hypothesis)
    errors = _edit_distance(ref, hyp)
    return {
        "reference_text": reference,
        "reference_words": len(ref),
        "hypothesis_words": len(hyp),
        "word_errors": errors,
        "wer": errors / len(ref) if ref else None,
        "hallucinated_final": not ref and bool(hyp),
    }


def _score_matches(actual: Any, expected: Mapping[str, Any]) -> bool:
    if not isinstance(actual, dict):
        return False
    for key, expected_value in expected.items():
        actual_value = actual.get(key)
        if isinstance(expected_value, float):
            if (_finite_number(actual_value) is None
                    or not math.isclose(float(actual_value), expected_value,
                                        rel_tol=1e-12, abs_tol=1e-12)):
                return False
        elif actual_value != expected_value:
            return False
    return True


def _case_key(record: Mapping[str, Any]) -> tuple[str, str]:
    return str(record.get("run_id", "")), str(record.get("case_id", ""))


def check_replay(
    records: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    *,
    expected_update_interval_s: float,
    expected_model_dir: Path,
    max_end_to_final_s: float | None = None,
    max_queue_age_ms: float | None = None,
    max_temperature_c: float | None = None,
    min_partial_updates_per_s: float | None = None,
    throttle_before: int = 0,
    throttle_after: int = 0,
) -> dict[str, Any]:
    reasons: list[str] = []
    warnings: list[str] = []

    def fail(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    model_records = [record for record in records
                     if record.get("type") == "model_identity"]
    summaries = [record for record in records
                 if record.get("type") == "case_summary"]
    error_records = [record for record in records
                     if record.get("type") in {"model_error", "case_error"}]
    if len(model_records) != 1:
        fail(f"model_identity_count:{len(model_records)}")
    model_positions = [index for index, record in enumerate(records)
                       if record.get("type") == "model_identity"]
    if model_positions != [0]:
        fail(f"model_identity_position:{model_positions}")
    if error_records:
        fail(f"error_records:{len(error_records)}")

    manifest_cases = manifest.get("cases")
    if not isinstance(manifest_cases, dict) or not manifest_cases:
        raise ValueError("manifest cases must be a nonempty object")
    expected_stems = set(str(stem) for stem in manifest_cases)
    manifest_corpus_directory = manifest.get("corpus_directory")
    if (not isinstance(manifest_corpus_directory, str)
            or not manifest_corpus_directory):
        raise ValueError("manifest corpus_directory must be a path")
    contract_corpus_directory = Path(
        manifest_corpus_directory).expanduser().resolve()
    policy = manifest.get("policy")
    if not isinstance(policy, dict):
        raise ValueError("manifest policy must be an object")
    manifest_max_end_to_final_s = _nonnegative_number(
        policy.get("max_end_to_final_seconds"))
    manifest_max_queue_age_ms = _nonnegative_number(
        policy.get("max_queue_age_ms"))
    manifest_max_temperature_c = _nonnegative_number(
        policy.get("max_temperature_c"))
    manifest_min_partial_updates_per_s = _nonnegative_number(
        policy.get("min_partial_updates_per_second"))
    manifest_max_stream_start_s = _nonnegative_number(
        policy.get("max_stream_start_seconds"))
    manifest_max_stream_wall_over_audio_s = _nonnegative_number(
        policy.get("max_stream_wall_over_audio_seconds"))
    manifest_audio_queue_chunks = policy.get("audio_queue_chunks")
    manifest_text_queue_events = policy.get("text_queue_events")
    allowed_update_intervals_raw = policy.get(
        "allowed_update_intervals_seconds")
    if (not isinstance(allowed_update_intervals_raw, list)
            or not allowed_update_intervals_raw):
        raise ValueError("manifest update intervals are incomplete")
    allowed_update_intervals: list[float] = []
    for raw_interval in allowed_update_intervals_raw:
        interval = _finite_number(raw_interval)
        if interval is None or interval <= 0:
            raise ValueError("manifest update interval is invalid")
        allowed_update_intervals.append(interval)
    max_aggregate_wer = _nonnegative_number(policy.get("max_aggregate_wer"))
    max_partial_gap_s = _nonnegative_number(
        policy.get("max_partial_gap_seconds"))
    partial_end_tolerance_s = _nonnegative_number(
        policy.get("partial_post_end_tolerance_seconds"))
    if (manifest_max_end_to_final_s is None
            or manifest_max_end_to_final_s <= 0
            or manifest_max_queue_age_ms is None
            or manifest_max_queue_age_ms <= 0
            or manifest_max_temperature_c is None
            or manifest_max_temperature_c <= 0
            or manifest_min_partial_updates_per_s is None
            or manifest_min_partial_updates_per_s <= 0
            or manifest_max_stream_start_s is None
            or manifest_max_stream_start_s <= 0
            or manifest_max_stream_wall_over_audio_s is None
            or manifest_max_stream_wall_over_audio_s <= 0
            or not isinstance(manifest_audio_queue_chunks, int)
            or isinstance(manifest_audio_queue_chunks, bool)
            or manifest_audio_queue_chunks <= 0
            or not isinstance(manifest_text_queue_events, int)
            or isinstance(manifest_text_queue_events, bool)
            or manifest_text_queue_events <= 0
            or max_aggregate_wer is None
            or max_partial_gap_s is None
            or max_partial_gap_s <= 0
            or partial_end_tolerance_s is None):
        raise ValueError("manifest replay policy is incomplete")

    def effective_maximum(
        requested: float | None, contract: float, name: str
    ) -> float:
        if requested is None:
            return contract
        value = _finite_number(requested)
        if value is None or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
        if value > contract:
            fail(f"weakened_policy:{name}:{value}>{contract}")
            return contract
        return value

    def effective_minimum(
        requested: float | None, contract: float, name: str
    ) -> float:
        if requested is None:
            return contract
        value = _finite_number(requested)
        if value is None or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
        if value < contract:
            fail(f"weakened_policy:{name}:{value}<{contract}")
            return contract
        return value

    max_end_to_final_s = effective_maximum(
        max_end_to_final_s, manifest_max_end_to_final_s,
        "max_end_to_final_seconds")
    max_queue_age_ms = effective_maximum(
        max_queue_age_ms, manifest_max_queue_age_ms, "max_queue_age_ms")
    max_temperature_c = effective_maximum(
        max_temperature_c, manifest_max_temperature_c, "max_temperature_c")
    min_partial_updates_per_s = effective_minimum(
        min_partial_updates_per_s, manifest_min_partial_updates_per_s,
        "min_partial_updates_per_second")
    invoked_update_interval = _finite_number(expected_update_interval_s)
    if (invoked_update_interval is None
            or invoked_update_interval not in allowed_update_intervals):
        fail(f"update_interval_not_contract:{expected_update_interval_s}")

    manifest_model = manifest.get("model")
    if not isinstance(manifest_model, dict):
        raise ValueError("manifest model must be an object")
    manifest_model_dir_text = manifest_model.get("directory")
    manifest_model_arch = manifest_model.get("requested_arch")
    manifest_model_enum = manifest_model.get("enum_name")
    if (not isinstance(manifest_model_dir_text, str)
            or not manifest_model_dir_text
            or not isinstance(manifest_model_arch, str)
            or not manifest_model_arch
            or not isinstance(manifest_model_enum, str)
            or not manifest_model_enum):
        raise ValueError("manifest model contract is incomplete")
    manifest_model_dir = Path(manifest_model_dir_text).expanduser()
    manifest_runtime = manifest.get("runtime")
    if not isinstance(manifest_runtime, dict):
        raise ValueError("manifest runtime must be an object")
    required_distribution_version = manifest_runtime.get(
        "moonshine_distribution_version")
    if (not isinstance(required_distribution_version, str)
            or not required_distribution_version):
        raise ValueError("manifest runtime contract is incomplete")
    manifest_contract_sha256 = _contract_sha256(manifest)
    canonical_manifest = (
        manifest.get("schema") == 1
        and manifest.get("name") == _CANONICAL_MANIFEST_NAME
        and manifest_contract_sha256 == _CANONICAL_CONTRACT_SHA256
    )
    if not canonical_manifest:
        warnings.append("noncanonical_manifest_custom_diagnostic_only")
    try:
        invoked_model_dir = expected_model_dir.resolve()
        contract_model_dir = manifest_model_dir.resolve()
    except (OSError, RuntimeError):
        invoked_model_dir = expected_model_dir
        contract_model_dir = manifest_model_dir
    if invoked_model_dir != contract_model_dir:
        fail(f"invocation_model_dir_not_contract:{invoked_model_dir}")

    run_ids = {record.get("run_id") for record in records}
    if (len(run_ids) != 1 or not all(isinstance(value, str) and value
                                     for value in run_ids)):
        fail("mixed_or_empty_run_id")
        run_id = None
    else:
        run_id = next(iter(run_ids))
    if any(record.get("schema") != 1 for record in records):
        fail("record_schema_mismatch")
    allowed_record_types = {
        "model_identity",
        "model_error",
        "case_start",
        "stream_started",
        "audio_chunk",
        "stt_event",
        "case_summary",
        "case_error",
    }
    unknown_record_types = sorted({
        str(record.get("type")) for record in records
        if record.get("type") not in allowed_record_types
    })
    if unknown_record_types:
        fail("unknown_record_types:" + ",".join(unknown_record_types))

    summaries_by_stem: dict[str, Mapping[str, Any]] = {}
    for summary in summaries:
        wav_value = summary.get("wav")
        if not isinstance(wav_value, str):
            fail("summary_missing_wav")
            continue
        stem = Path(wav_value).stem
        if stem in summaries_by_stem:
            fail(f"duplicate_case:{stem}")
        summaries_by_stem[stem] = summary
    actual_stems = set(summaries_by_stem)
    if actual_stems != expected_stems:
        fail(
            "case_set:expected=" + ",".join(sorted(expected_stems))
            + ":actual=" + ",".join(sorted(actual_stems))
        )
    summary_case_ids = [summary.get("case_id") for summary in summaries]
    if (any(not isinstance(value, str) or not value for value in summary_case_ids)
            or len(set(summary_case_ids)) != len(summary_case_ids)):
        fail("duplicate_or_empty_case_id")
    raw_observed_case_ids = [
        record.get("case_id") for record in records
        if record.get("case_id") is not None
    ]
    if any(not isinstance(value, str) or not value
           for value in raw_observed_case_ids):
        fail("invalid_record_case_id")
    observed_case_ids = {
        value for value in raw_observed_case_ids if isinstance(value, str)
    }
    if observed_case_ids != set(summary_case_ids):
        fail("case_id_set_mismatch")

    model_fingerprint = (_model_fingerprint(model_records[0])
                         if len(model_records) == 1 else None)
    if model_fingerprint is not None:
        if model_fingerprint.get("model_arch_requested") != manifest_model_arch:
            fail(
                "model_arch_not_contract:"
                f"{model_fingerprint.get('model_arch_requested')}"
            )
        try:
            actual_model_dir = Path(str(model_fingerprint.get("model_dir"))).resolve()
        except (OSError, RuntimeError):
            actual_model_dir = Path(str(model_fingerprint.get("model_dir")))
        if actual_model_dir != contract_model_dir:
            fail(f"model_dir:{actual_model_dir}")
        try:
            actual_model_path = Path(
                str(model_fingerprint.get("model_path"))).resolve()
        except (OSError, RuntimeError):
            actual_model_path = Path(str(model_fingerprint.get("model_path")))
        if actual_model_path != contract_model_dir:
            fail(f"model_path:{actual_model_path}")
        identity = model_fingerprint.get("runtime_identity")
        if not isinstance(identity, dict):
            fail("missing_runtime_identity")
        else:
            model_identity = identity.get("model")
            native_identity = identity.get("native_library")
            if not isinstance(model_identity, dict) or not _is_sha256(
                    model_identity.get("tree_sha256")):
                fail("missing_model_tree_identity")
            else:
                try:
                    identity_model_dir = Path(
                        str(model_identity.get("directory"))).resolve()
                except (OSError, RuntimeError):
                    identity_model_dir = Path(
                        str(model_identity.get("directory")))
                if identity_model_dir != contract_model_dir:
                    fail(f"runtime_model_dir:{identity_model_dir}")
                if (not isinstance(model_identity.get("file_count"), int)
                        or isinstance(model_identity.get("file_count"), bool)
                        or model_identity["file_count"] <= 0
                        or not isinstance(model_identity.get("total_bytes"), int)
                        or isinstance(model_identity.get("total_bytes"), bool)
                        or model_identity["total_bytes"] <= 0):
                    fail("invalid_model_tree_size")
            if not isinstance(native_identity, dict) or not _is_sha256(
                    native_identity.get("sha256")):
                fail("missing_native_library_identity")
            elif (not isinstance(native_identity.get("api_version"), int)
                  or isinstance(native_identity.get("api_version"), bool)
                  or native_identity["api_version"] <= 0):
                fail("invalid_native_api_version")
            if identity.get(
                    "moonshine_distribution_version") != required_distribution_version:
                fail(
                    "moonshine_distribution_version:"
                    f"{identity.get('moonshine_distribution_version')}"
                )
            for key in (
                "moonshine_package_file_sha256",
                "moonshine_transcriber_file_sha256",
            ):
                value = identity.get(key)
                if not _is_sha256(value):
                    fail(f"missing_runtime_identity:{key}")
            if identity.get("transcriber_options") != {
                    "return_audio_data": "false"}:
                fail("transcriber_options_not_contract")
            if identity.get("model_arch_requested") != manifest_model_arch:
                fail("runtime_model_arch_not_contract")
            if model_fingerprint.get("model_arch") != manifest_model_enum:
                fail("model_arch_enum_not_contract")
            if (identity.get("model_arch_enum") != manifest_model_enum
                    or identity.get("model_arch_enum") != model_fingerprint.get(
                        "model_arch")):
                fail("runtime_model_arch_enum_mismatch")

    events_by_case: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for record in records:
        if record.get("type") == "stt_event":
            events_by_case.setdefault(_case_key(record), []).append(record)

    records_by_type_case: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for record in records:
        case_id = record.get("case_id")
        if isinstance(case_id, str) and case_id:
            key = (str(record.get("run_id", "")), case_id,
                   str(record.get("type", "")))
            records_by_type_case.setdefault(key, []).append(record)

    positions_by_case: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for index, record in enumerate(records):
        case_id = record.get("case_id")
        if isinstance(case_id, str) and case_id:
            positions_by_case.setdefault(case_id, []).append((index, record))
        elif index != 0 and record.get("type") not in {"model_error"}:
            fail(f"unscoped_record:{index}:{record.get('type')}")
    lifecycle_intervals: list[tuple[int, int, str]] = []
    for case_id in summary_case_ids:
        if not isinstance(case_id, str) or not case_id:
            continue
        positioned = positions_by_case.get(case_id, [])
        start_positions = [index for index, record in positioned
                           if record.get("type") == "case_start"]
        stream_positions = [index for index, record in positioned
                            if record.get("type") == "stream_started"]
        summary_positions = [index for index, record in positioned
                             if record.get("type") == "case_summary"]
        if (len(start_positions) != 1 or len(stream_positions) != 1
                or len(summary_positions) != 1):
            continue
        start_position = start_positions[0]
        stream_position = stream_positions[0]
        summary_position = summary_positions[0]
        if not start_position < stream_position < summary_position:
            fail(f"case_lifecycle_order:{case_id}")
        if (not positioned or positioned[0][0] != start_position
                or positioned[-1][0] != summary_position):
            fail(f"case_lifecycle_bounds:{case_id}")
        lifecycle_intervals.append((start_position, summary_position, case_id))
    lifecycle_intervals.sort()
    for (_, prior_end, prior_id), (next_start, _, next_id) in zip(
            lifecycle_intervals, lifecycle_intervals[1:]):
        if prior_end >= next_start:
            fail(f"case_lifecycle_overlap:{prior_id}:{next_id}")

    case_reports: dict[str, dict[str, Any]] = {}
    aggregate_stream_errors = 0
    aggregate_batch_errors = 0
    aggregate_reference_words = 0
    negative_controls = 0
    negative_stream_hallucinations = 0
    negative_batch_hallucinations = 0
    end_latencies: list[float] = []

    for stem in sorted(expected_stems):
        summary = summaries_by_stem.get(stem)
        expected = manifest_cases.get(stem)
        if summary is None or not isinstance(expected, dict):
            continue
        prefix = f"case_{stem}"
        case_kind = expected.get("kind", "positive")
        if case_kind not in {"positive", "negative"}:
            raise ValueError(
                f"manifest case kind invalid for {stem}: {case_kind!r}"
            )
        wav_path = Path(str(summary.get("wav", "")))
        expected_source = summary.get("expected_source")
        reference_path = (Path(expected_source)
                          if isinstance(expected_source, str) else None)
        expected_wav_path = contract_corpus_directory / f"{stem}.wav"
        expected_reference_path = contract_corpus_directory / f"{stem}.txt"
        try:
            resolved_wav_path = wav_path.resolve()
        except (OSError, RuntimeError):
            resolved_wav_path = wav_path
        if resolved_wav_path != expected_wav_path:
            fail(f"{prefix}_wav_path:{resolved_wav_path}")
        if reference_path is not None:
            try:
                resolved_reference_path = reference_path.resolve()
            except (OSError, RuntimeError):
                resolved_reference_path = reference_path
            if resolved_reference_path != expected_reference_path:
                fail(f"{prefix}_reference_path:{resolved_reference_path}")

        wav_hash = None
        reference_hash = None
        reference_text = None
        try:
            wav_hash = _sha256_file(wav_path)
        except OSError as exc:
            fail(f"{prefix}_wav_unreadable:{exc}")
        if reference_path is None:
            fail(f"{prefix}_missing_reference_source")
        else:
            try:
                reference_hash = _sha256_file(reference_path)
                reference_text = reference_path.read_text(
                    encoding="utf-8").strip()
            except OSError as exc:
                fail(f"{prefix}_reference_unreadable:{exc}")
        if wav_hash != expected.get("wav_sha256"):
            fail(f"{prefix}_wav_sha256:{wav_hash}")
        if reference_hash != expected.get("reference_sha256"):
            fail(f"{prefix}_reference_sha256:{reference_hash}")
        reference_tokens = (_word_tokens(reference_text)
                            if isinstance(reference_text, str) else [])
        if case_kind == "positive" and not reference_tokens:
            fail(f"{prefix}_empty_reference")
        if case_kind == "negative" and reference_tokens:
            fail(f"{prefix}_negative_reference_not_empty")

        if summary.get("ok") is not True:
            fail(f"{prefix}_collector_not_ok")
        if summary.get("failure_reasons") not in ([], ()):
            fail(f"{prefix}_collector_failures")
        if _finite_number(summary.get("update_interval_s")) != expected_update_interval_s:
            fail(f"{prefix}_update_interval")
        if _finite_number(summary.get("pace")) != 1.0:
            fail(f"{prefix}_pace")
        if summary.get("model_arch_requested") != manifest_model_arch:
            fail(f"{prefix}_model_arch")
        if model_fingerprint is not None and _model_fingerprint(summary) != model_fingerprint:
            fail(f"{prefix}_model_identity_mismatch")

        audio = summary.get("audio")
        queue_metrics = summary.get("queue")
        stream = summary.get("stream")
        system = summary.get("system")
        batch = summary.get("batch")
        accuracy = summary.get("accuracy")
        if not all(isinstance(value, dict) for value in
                   (audio, queue_metrics, stream, system, batch, accuracy)):
            fail(f"{prefix}_missing_metrics")
            continue

        expected_frames = expected.get("frames")
        expected_chunks = expected.get("chunks")
        chunks_total = audio.get("chunks_total")
        expected_seconds = (expected_frames / 16_000
                            if isinstance(expected_frames, int) else None)
        if audio.get("frames") != expected_frames:
            fail(f"{prefix}_frames:{audio.get('frames')}")
        if audio.get("sample_rate") != 16_000 or audio.get("channels") != 1:
            fail(f"{prefix}_pcm_rate_channels")
        if audio.get("sample_width_bytes") != 2:
            fail(f"{prefix}_pcm_width")
        if audio.get("bytes") != expected_frames * 2:
            fail(f"{prefix}_pcm_bytes:{audio.get('bytes')}")
        if audio.get("chunk_bytes") != 4096:
            fail(f"{prefix}_chunk_bytes")
        if audio.get("partial_last_chunk_bytes") != 0:
            fail(f"{prefix}_partial_last_chunk")
        collected_seconds = _nonnegative_number(audio.get("seconds"))
        enqueued_seconds = _nonnegative_number(audio.get("enqueued_seconds"))
        if (expected_seconds is None or collected_seconds is None
                or not math.isclose(collected_seconds, expected_seconds,
                                    rel_tol=0.0, abs_tol=1e-9)
                or enqueued_seconds is None
                or not math.isclose(enqueued_seconds, expected_seconds,
                                    rel_tol=0.0, abs_tol=1e-9)):
            fail(f"{prefix}_audio_duration")
        if chunks_total != expected_chunks:
            fail(f"{prefix}_chunks:{chunks_total}")
        if not (audio.get("chunks_enqueued") == chunks_total
                and audio.get("chunks_processed") == chunks_total
                and audio.get("chunks_dropped") == 0):
            fail(f"{prefix}_audio_accounting")
        case_key = _case_key(summary)
        case_start_records = records_by_type_case.get(
            (*case_key, "case_start"), [])
        stream_started_records = records_by_type_case.get(
            (*case_key, "stream_started"), [])
        audio_chunk_records = records_by_type_case.get(
            (*case_key, "audio_chunk"), [])
        if len(case_start_records) != 1:
            fail(f"{prefix}_case_start_count:{len(case_start_records)}")
        if len(stream_started_records) != 1:
            fail(f"{prefix}_stream_started_count:{len(stream_started_records)}")
        if len(case_start_records) == 1:
            case_start = case_start_records[0]
            try:
                case_start_wav = Path(str(case_start.get("wav"))).resolve()
            except (OSError, RuntimeError):
                case_start_wav = Path(str(case_start.get("wav")))
            if case_start_wav != expected_wav_path:
                fail(f"{prefix}_case_start_wav:{case_start_wav}")
            if (_model_fingerprint(case_start) != model_fingerprint
                    or _model_fingerprint(case_start) != _model_fingerprint(summary)):
                fail(f"{prefix}_case_start_model_identity")
            if (_finite_number(case_start.get("update_interval_s"))
                    != expected_update_interval_s):
                fail(f"{prefix}_case_start_update_interval")
            if _finite_number(case_start.get("pace")) != 1.0:
                fail(f"{prefix}_case_start_pace")
            if case_start.get(
                    "queue_capacity_chunks") != manifest_audio_queue_chunks:
                fail(f"{prefix}_case_start_audio_queue")
            if case_start.get(
                    "text_queue_capacity_events") != manifest_text_queue_events:
                fail(f"{prefix}_case_start_text_queue")
            case_start_seconds = _nonnegative_number(
                case_start.get("audio_seconds"))
            if (case_start_seconds is None
                    or not math.isclose(case_start_seconds, expected_seconds,
                                        rel_tol=0.0, abs_tol=1e-9)):
                fail(f"{prefix}_case_start_audio_seconds")
            if case_start.get("audio_chunks") != expected_chunks:
                fail(f"{prefix}_case_start_audio_chunks")
            if case_start.get("partial_last_chunk_bytes") != 4096:
                fail(f"{prefix}_case_start_last_chunk")
        if len(stream_started_records) == 1:
            stream_started_s = _nonnegative_number(
                stream_started_records[0].get("t_s"))
            if (stream_started_s is None
                    or stream_started_s > manifest_max_stream_start_s):
                fail(f"{prefix}_stream_started_s:{stream_started_s}")
        ordered_audio_chunks = list(audio_chunk_records)
        chunk_indexes = [
            record.get("chunk_index") for record in ordered_audio_chunks
            if isinstance(record.get("chunk_index"), int)
            and not isinstance(record.get("chunk_index"), bool)
        ]
        if chunk_indexes != list(range(expected_chunks)):
            fail(f"{prefix}_audio_chunk_records")
        chunk_queue_ages: list[float] = []
        for chunk_index, record in enumerate(ordered_audio_chunks):
            if chunk_index >= expected_chunks:
                break
            expected_chunk_bytes = min(
                4096, expected_frames * 2 - chunk_index * 4096)
            if record.get("bytes") != expected_chunk_bytes:
                fail(f"{prefix}_chunk_{chunk_index}_bytes")
            expected_audio_end_s = min(
                expected_seconds, (chunk_index + 1) * 2048 / 16_000)
            audio_end_s = _nonnegative_number(record.get("audio_end_s"))
            if (audio_end_s is None
                    or not math.isclose(audio_end_s, expected_audio_end_s,
                                        rel_tol=0.0, abs_tol=1e-9)):
                fail(f"{prefix}_chunk_{chunk_index}_audio_end")
            queue_age = _nonnegative_number(record.get("queue_age_ms"))
            add_audio_ms = _nonnegative_number(record.get("add_audio_ms"))
            queue_depth = record.get("queue_depth_after")
            if queue_age is None:
                fail(f"{prefix}_chunk_{chunk_index}_queue_age")
            else:
                chunk_queue_ages.append(queue_age)
            if add_audio_ms is None:
                fail(f"{prefix}_chunk_{chunk_index}_add_audio")
            if (not isinstance(queue_depth, int) or isinstance(queue_depth, bool)
                    or not 0 <= queue_depth <= manifest_audio_queue_chunks):
                fail(f"{prefix}_chunk_{chunk_index}_queue_depth")
        if queue_metrics.get("capacity_chunks") != manifest_audio_queue_chunks:
            fail(f"{prefix}_queue_capacity")
        if queue_metrics.get("overflowed") is not False:
            fail(f"{prefix}_queue_overflow")
        high_water = _finite_number(queue_metrics.get("high_water_chunks"))
        if (high_water is None
                or high_water > manifest_audio_queue_chunks):
            fail(f"{prefix}_queue_high_water:{high_water}")
        queue_age_max = _nonnegative_number(queue_metrics.get("age_ms_max"))
        if queue_age_max is None or queue_age_max > max_queue_age_ms:
            fail(f"{prefix}_queue_age_ms:{queue_age_max}")
        elif (not chunk_queue_ages
              or not math.isclose(queue_age_max, max(chunk_queue_ages),
                                  rel_tol=0.0, abs_tol=1e-9)):
            fail(f"{prefix}_queue_age_summary_mismatch")

        if stream.get("stop_returned_transcript") is not True:
            fail(f"{prefix}_missing_stop_transcript")
        if (case_kind == "positive"
                and not str(stream.get("text") or "").strip()):
            fail(f"{prefix}_empty_stream_text")
        if stream.get("text_event_drops") != 0:
            fail(f"{prefix}_text_event_drops")
        stream_wall_s = _nonnegative_number(stream.get("wall_seconds"))
        if (stream_wall_s is None
                or stream_wall_s < expected_seconds - 0.01
                or stream_wall_s > (
                    expected_seconds + manifest_max_stream_wall_over_audio_s)):
            fail(f"{prefix}_stream_wall_seconds:{stream_wall_s}")
        end_to_final = _nonnegative_number(stream.get("end_to_final_seconds"))
        if end_to_final is None or end_to_final > max_end_to_final_s:
            fail(f"{prefix}_end_to_final:{end_to_final}")
        elif end_to_final is not None:
            end_latencies.append(end_to_final)

        stream_text = str(stream.get("text") or "").strip()
        batch_text = str(batch.get("text") or "").strip()
        recomputed_stream = (_score_words(reference_text, stream_text)
                             if reference_text is not None else None)
        recomputed_batch = (_score_words(reference_text, batch_text)
                            if reference_text is not None else None)
        recomputed_stream_vs_batch = (_score_words(batch_text, stream_text)
                                      if batch_text else None)
        if recomputed_stream is not None and not _score_matches(
                accuracy, recomputed_stream):
            fail(f"{prefix}_collector_stream_score_mismatch")
        if recomputed_batch is not None and not _score_matches(
                batch.get("accuracy"), recomputed_batch):
            fail(f"{prefix}_collector_batch_score_mismatch")
        if recomputed_stream_vs_batch is not None and not _score_matches(
                batch.get("stream_vs_batch"), recomputed_stream_vs_batch):
            fail(f"{prefix}_collector_stream_batch_score_mismatch")

        stream_errors = (_word_error_count(recomputed_stream)
                         if recomputed_stream is not None else None)
        reference_words = (_reference_word_count(recomputed_stream)
                           if recomputed_stream is not None else None)
        batch_accuracy = batch.get("accuracy") if isinstance(batch, dict) else None
        batch_errors = (_word_error_count(recomputed_batch)
                        if recomputed_batch is not None else None)
        batch_reference_words = (_reference_word_count(recomputed_batch)
                                 if recomputed_batch is not None else None)
        if stream_errors is None or reference_words is None:
            fail(f"{prefix}_stream_accuracy_unscored")
        if (batch.get("enabled") is not True
                or (case_kind == "positive"
                    and not str(batch.get("text") or "").strip())):
            fail(f"{prefix}_batch_missing")
        if batch_errors is None or batch_reference_words != reference_words:
            fail(f"{prefix}_batch_accuracy_unscored")
        if (case_kind == "positive"
                and stream_errors is not None
                and batch_errors is not None):
            if stream_errors > batch_errors:
                fail(f"{prefix}_accuracy_regression:{stream_errors}>{batch_errors}")
            aggregate_stream_errors += stream_errors
            aggregate_batch_errors += batch_errors
            max_word_errors = expected.get("max_word_errors")
            if (not isinstance(max_word_errors, int)
                    or isinstance(max_word_errors, bool)
                    or max_word_errors < 0):
                raise ValueError(f"manifest max_word_errors invalid for {stem}")
            if stream_errors > max_word_errors:
                fail(f"{prefix}_absolute_stream_errors:{stream_errors}>{max_word_errors}")
            if batch_errors > max_word_errors:
                fail(f"{prefix}_absolute_batch_errors:{batch_errors}>{max_word_errors}")
        elif (case_kind == "negative"
              and stream_errors is not None
              and batch_errors is not None):
            negative_controls += 1
            stream_hallucinated = bool(
                recomputed_stream and recomputed_stream["hallucinated_final"])
            batch_hallucinated = bool(
                recomputed_batch and recomputed_batch["hallucinated_final"])
            if stream_hallucinated or stream_errors:
                negative_stream_hallucinations += 1
                fail(f"{prefix}_negative_stream_hallucination")
            if batch_hallucinated or batch_errors:
                negative_batch_hallucinations += 1
                fail(f"{prefix}_negative_batch_hallucination")
        if case_kind == "positive" and reference_words is not None:
            aggregate_reference_words += reference_words

        governors = system.get("governors")
        if governors != ["performance"]:
            fail(f"{prefix}_governors:{governors}")
        temperature_max = _nonnegative_number(system.get("temperature_c_max"))
        if temperature_max is None or temperature_max > max_temperature_c:
            fail(f"{prefix}_temperature_c:{temperature_max}")
        swap_start = _nonnegative_number(system.get("swap_used_bytes_start"))
        swap_end = _nonnegative_number(system.get("swap_used_bytes_end"))
        swap_max = _nonnegative_number(system.get("swap_used_bytes_max"))
        if swap_start is None or swap_end is None or swap_max is None:
            fail(f"{prefix}_swap_metrics_missing")
        elif swap_end > swap_start or swap_max > swap_start:
            fail(f"{prefix}_swap_growth:{swap_start}->{swap_max}->{swap_end}")

        audio_seconds = expected_seconds or 0.0
        useful_partials: list[Mapping[str, Any]] = []
        final_text = stream_text
        final_lcps: list[int] = []
        final_prefix_token_counts: list[int] = []
        previous_hypothesis = ""
        previous_normalized_tokens: tuple[str, ...] = ()
        max_final_aligned_token_progress = 0
        ordered_events = sorted(
            enumerate(events_by_case.get(_case_key(summary), [])),
            key=lambda item: (
                _nonnegative_number(item[1].get("t_s"))
                if _nonnegative_number(item[1].get("t_s")) is not None
                else math.inf,
                item[0],
            ),
        )
        for _, event in ordered_events:
            t_s = _nonnegative_number(event.get("t_s"))
            hypothesis = str(event.get("hypothesis") or "")
            changed = hypothesis != previous_hypothesis
            if event.get("hypothesis_changed") is not changed:
                fail(f"{prefix}_hypothesis_changed_mismatch")
            previous_hypothesis = hypothesis
            partial_tokens = _word_tokens(hypothesis)
            final_tokens = _word_tokens(final_text)
            normalized_tokens = tuple(partial_tokens)
            normalized_changed = normalized_tokens != previous_normalized_tokens
            previous_normalized_tokens = normalized_tokens
            normalized_partial = " ".join(partial_tokens)
            normalized_final = " ".join(final_tokens)
            final_char_progress = _common_prefix_chars(
                normalized_partial, normalized_final)
            final_token_progress = 0
            for partial_token, final_token in zip(partial_tokens, final_tokens):
                if partial_token != final_token:
                    break
                final_token_progress += 1
            final_related = final_token_progress >= 1
            progress_advanced = (
                final_token_progress > max_final_aligned_token_progress)
            if (normalized_changed and final_related and progress_advanced):
                max_final_aligned_token_progress = final_token_progress
            if (changed
                    and normalized_changed
                    and event.get("line_complete") is not True
                    and hypothesis.strip()
                    and final_related
                    and progress_advanced
                    and t_s is not None
                    and t_s <= audio_seconds + partial_end_tolerance_s):
                useful_partials.append(event)
                final_lcps.append(final_char_progress)
                final_prefix_token_counts.append(final_token_progress)
        required_partials = (
            math.floor(audio_seconds * min_partial_updates_per_s)
            if case_kind == "positive" and audio_seconds > 2.0 else 0
        )
        if len(useful_partials) < required_partials:
            fail(
                f"{prefix}_useful_partials:{len(useful_partials)}"
                f"<{required_partials}"
            )
        useful_times = [min(audio_seconds, float(event["t_s"]))
                        for event in useful_partials]
        if required_partials:
            coverage_points = [0.0, *useful_times, audio_seconds]
            max_partial_gap = max(
                right - left for left, right in zip(
                    coverage_points, coverage_points[1:]))
            if max_partial_gap > max_partial_gap_s:
                fail(f"{prefix}_partial_gap:{max_partial_gap}>{max_partial_gap_s}")
        else:
            max_partial_gap = None

        case_reports[stem] = {
            "kind": case_kind,
            "wav_sha256": wav_hash,
            "reference_sha256": reference_hash,
            "audio_seconds": audio_seconds,
            "stream_text": stream.get("text"),
            "batch_text": batch.get("text"),
            "stream_word_errors": stream_errors,
            "batch_word_errors": batch_errors,
            "reference_words": reference_words,
            "end_to_final_seconds": end_to_final,
            "stream_wall_seconds": stream_wall_s,
            "useful_pre_end_partial_updates": len(useful_partials),
            "required_pre_end_partial_updates": required_partials,
            "max_partial_coverage_gap_seconds": max_partial_gap,
            "final_lcp_chars_min": min(final_lcps) if final_lcps else None,
            "final_lcp_chars_last": final_lcps[-1] if final_lcps else None,
            "final_prefix_tokens_last": (
                final_prefix_token_counts[-1]
                if final_prefix_token_counts else None),
            "queue_age_ms_max": queue_age_max,
            "queue_high_water_chunks": high_water,
            "temperature_c_max": temperature_max,
            "process_cpu_percent": system.get("process_cpu_percent"),
            "process_max_rss_bytes": system.get("process_max_rss_bytes"),
        }

    if aggregate_stream_errors > aggregate_batch_errors:
        fail(
            f"aggregate_accuracy_regression:{aggregate_stream_errors}"
            f">{aggregate_batch_errors}"
        )
    if aggregate_reference_words:
        aggregate_stream_wer = aggregate_stream_errors / aggregate_reference_words
        aggregate_batch_wer = aggregate_batch_errors / aggregate_reference_words
        if aggregate_stream_wer > max_aggregate_wer:
            fail(f"aggregate_stream_wer:{aggregate_stream_wer}>{max_aggregate_wer}")
        if aggregate_batch_wer > max_aggregate_wer:
            fail(f"aggregate_batch_wer:{aggregate_batch_wer}>{max_aggregate_wer}")
    else:
        aggregate_stream_wer = None
        aggregate_batch_wer = None
    if throttle_before != 0:
        fail(f"throttle_before:0x{throttle_before:x}")
    if throttle_after != 0:
        fail(f"throttle_after:0x{throttle_after:x}")

    limitations = manifest.get("limitations")
    if not isinstance(limitations, list):
        limitations = []
    if manifest.get("full_gate") is not True:
        warnings.append("manifest_is_provisional_not_full_gate0a")

    effective_policy = {
        "allowed_update_intervals_seconds": allowed_update_intervals,
        "audio_queue_chunks": manifest_audio_queue_chunks,
        "text_queue_events": manifest_text_queue_events,
        "max_stream_start_seconds": manifest_max_stream_start_s,
        "max_stream_wall_over_audio_seconds": (
            manifest_max_stream_wall_over_audio_s),
        "max_end_to_final_seconds": max_end_to_final_s,
        "max_queue_age_ms": max_queue_age_ms,
        "max_temperature_c": max_temperature_c,
        "min_partial_updates_per_second": min_partial_updates_per_s,
        "max_aggregate_wer": max_aggregate_wer,
        "max_partial_gap_seconds": max_partial_gap_s,
        "partial_post_end_tolerance_seconds": partial_end_tolerance_s,
    }

    return {
        "schema": 1,
        "type": "moonshine_replay_gate_report",
        "scope": ("provisional_deployed_medium_mixed_slice"
                  if canonical_manifest else "custom_replay_diagnostic"),
        "canonical_manifest": canonical_manifest,
        "ok": not reasons,
        "run_id": run_id,
        "full_gate0a_complete": False,
        "failure_reasons": reasons,
        "warnings": warnings,
        "limitations": limitations,
        "manifest": {
            "name": manifest.get("name"),
            "contract_sha256": manifest_contract_sha256,
        },
        "effective_policy": effective_policy,
        "update_interval_s": expected_update_interval_s,
        "model": model_fingerprint,
        "throttled_before": f"0x{throttle_before:x}",
        "throttled_after": f"0x{throttle_after:x}",
        "max_end_to_final_seconds": max(end_latencies) if end_latencies else None,
        "aggregate": {
            "reference_words": aggregate_reference_words,
            "stream_word_errors": aggregate_stream_errors,
            "batch_word_errors": aggregate_batch_errors,
            "stream_wer": aggregate_stream_wer,
            "batch_wer": aggregate_batch_wer,
            "negative_controls": negative_controls,
            "negative_stream_hallucinations": (
                negative_stream_hallucinations),
            "negative_batch_hallucinations": negative_batch_hallucinations,
        },
        "cases": case_reports,
    }


def _common_prefix_chars(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("jsonl", help="one cadence's replay JSONL")
    parser.add_argument("--manifest", required=True,
                        help="trusted corpus manifest JSON")
    parser.add_argument("--expected-update-interval", required=True, type=float)
    parser.add_argument("--expected-model-dir", required=True)
    parser.add_argument("--throttled-before", required=True)
    parser.add_argument("--throttled-after", required=True)
    parser.add_argument("--max-end-to-final", type=float,
                        help="optional tightening of the manifest maximum")
    parser.add_argument("--max-queue-age-ms", type=float,
                        help="optional tightening of the manifest maximum")
    parser.add_argument("--max-temperature-c", type=float,
                        help="optional tightening of the manifest maximum")
    parser.add_argument("--min-partials-per-second", type=float,
                        help="optional tightening of the manifest minimum")
    parser.add_argument(
        "--allow-noncanonical-manifest", action="store_true",
        help="emit a custom diagnostic instead of the bundled provisional gate",
    )
    parser.add_argument("--output", default="-",
                        help="report JSON destination, or - for stdout")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for name in ("expected_update_interval", "max_end_to_final",
                 "max_queue_age_ms", "max_temperature_c",
                 "min_partials_per_second"):
        value = getattr(args, name)
        if value is not None and (not math.isfinite(value) or value <= 0):
            parser.error(f"--{name.replace('_', '-')} must be finite and positive")

    jsonl_path = Path(args.jsonl).expanduser().resolve()
    manifest_path = Path(args.manifest).expanduser().resolve()
    throttled_before_path = Path(args.throttled_before).expanduser().resolve()
    throttled_after_path = Path(args.throttled_after).expanduser().resolve()
    input_paths = {
        jsonl_path,
        manifest_path,
        throttled_before_path,
        throttled_after_path,
    }
    try:
        throttled_evidence_reused = os.path.samefile(
            throttled_before_path, throttled_after_path)
    except OSError:
        throttled_evidence_reused = (
            throttled_before_path == throttled_after_path)
    if throttled_evidence_reused:
        parser.error("before/after throttle evidence must be distinct files")
    try:
        if (throttled_after_path.stat().st_mtime_ns
                < throttled_before_path.stat().st_mtime_ns):
            parser.error("after throttle evidence predates before evidence")
    except OSError as exc:
        parser.error(f"cannot stat throttle evidence: {exc}")
    output: Path | None = None
    if args.output != "-":
        output = Path(args.output).expanduser().resolve()
        if output in input_paths:
            parser.error(f"--output aliases an input evidence file: {output}")
        if output.exists():
            parser.error(f"--output already exists: {output}")
    try:
        records = _read_jsonl(jsonl_path)
        manifest = _read_json(manifest_path)
        report = check_replay(
            records,
            manifest,
            expected_update_interval_s=args.expected_update_interval,
            expected_model_dir=Path(args.expected_model_dir).expanduser(),
            max_end_to_final_s=args.max_end_to_final,
            max_queue_age_ms=args.max_queue_age_ms,
            max_temperature_c=args.max_temperature_c,
            min_partial_updates_per_s=args.min_partials_per_second,
            throttle_before=_parse_throttled(throttled_before_path),
            throttle_after=_parse_throttled(throttled_after_path),
        )
    except ValueError as exc:
        parser.error(str(exc))

    if (report.get("canonical_manifest") is not True
            and not args.allow_noncanonical_manifest):
        parser.error(
            "manifest is not the bundled canonical medium slice; "
            "use --allow-noncanonical-manifest only for a custom diagnostic"
        )

    checker_path = Path(__file__).resolve()
    report["evidence"] = {
        "jsonl": _file_evidence(jsonl_path),
        "manifest": _file_evidence(manifest_path),
        "throttled_before": _file_evidence(throttled_before_path),
        "throttled_after": _file_evidence(throttled_after_path),
        "checker": _file_evidence(checker_path),
    }

    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is None:
        sys.stdout.write(encoded)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as sink:
            sink.write(encoded)
        sys.stdout.write(encoded)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
