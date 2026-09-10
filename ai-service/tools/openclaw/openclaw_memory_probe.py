#!/usr/bin/env python3
"""Run and grade the isolated OpenClaw + Obsidian-memory scenario.

This is deliberately a one-device evidence probe, not a unit benchmark. It
starts the configured llama-server once, invokes pinned stable `openclaw agent
--local` in three fresh sessions, proves tool calls from each session JSONL,
and treats the disposable vault contents as canonical truth.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import pwd
import re
import secrets
import shutil
import signal
import stat as stat_module
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

SCHEMA_VERSION = 1
EXPECTED_CASE_IDS = ["write_read", "cross_session_recall", "archive_verify"]
BENCH_ROOT_PREFIX = pathlib.Path("/var/lib/hw1-openclaw-bench")


class ProbeError(RuntimeError):
    pass


class ProbeTermination(ProbeError):
    pass


def handle_termination(signum: int, _frame: Any) -> None:
    """Turn wrapper/timeout signals into an exception so child groups are reaped."""
    raise ProbeTermination(f"probe interrupted by signal {signum}")


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: pathlib.Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_cases(path: pathlib.Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ProbeError(f"unsupported cases schema: {data.get('schema_version')!r}")
    cases = data.get("cases")
    if not isinstance(cases, list) or [item.get("id") for item in cases] != EXPECTED_CASE_IDS:
        raise ProbeError(f"cases must be exactly {EXPECTED_CASE_IDS}")
    scenario = data.get("scenario")
    required = {"lookup_key", "answer_code", "note_title", "archive_title", "tags", "note_body"}
    if not isinstance(scenario, dict) or not required.issubset(scenario):
        raise ProbeError("scenario metadata is incomplete")
    for case in cases:
        if not isinstance(case.get("prompt"), str) or not case["prompt"].strip():
            raise ProbeError(f"case {case.get('id')} has no prompt")
        if not isinstance(case.get("expected_calls"), list) or not case["expected_calls"]:
            raise ProbeError(f"case {case.get('id')} has no expected_calls")
    return data


def is_relative_to(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def assert_private_directory(path: pathlib.Path, root: pathlib.Path | None = None) -> pathlib.Path:
    if not path.is_absolute():
        raise ProbeError(f"directory path must be absolute: {path}")
    current = pathlib.Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            current_stat = current.lstat()
        except FileNotFoundError as exc:
            raise ProbeError(f"directory component is missing: {current}") from exc
        if stat_module.S_ISLNK(current_stat.st_mode):
            raise ProbeError(f"symlinked directory component is forbidden: {current}")
    resolved = path.resolve(strict=True)
    directory_stat = resolved.stat()
    if not stat_module.S_ISDIR(directory_stat.st_mode):
        raise ProbeError(f"not a directory: {resolved}")
    if directory_stat.st_uid != os.getuid():
        raise ProbeError(f"directory is not owned by uid {os.getuid()}: {resolved}")
    if directory_stat.st_mode & 0o077:
        raise ProbeError(f"directory is not private (mode {directory_stat.st_mode & 0o777:o}): {resolved}")
    if root is not None and not is_relative_to(resolved, root.resolve(strict=True)):
        raise ProbeError(f"directory escaped benchmark root: {resolved}")
    return resolved


def tree_manifest(root: pathlib.Path) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = pathlib.Path(current)
        for name in list(dirs) + list(files):
            item = current_path / name
            rel = item.relative_to(root).as_posix()
            if item.is_symlink():
                output[rel] = {"type": "symlink", "target": os.readlink(item)}
            elif item.is_file():
                output[rel] = {
                    "type": "file",
                    "bytes": item.stat().st_size,
                    "sha256": sha256_file(item),
                }
            elif item.is_dir():
                output[rel] = {"type": "dir"}
    return dict(sorted(output.items()))


def transcript_snapshot(state_dir: pathlib.Path) -> dict[pathlib.Path, tuple[int, int]]:
    base = state_dir / "agents" / "bench" / "sessions"
    if not base.exists():
        return {}
    return {
        path: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in base.glob("*.jsonl")
        if path.is_file() and not path.is_symlink()
    }


def changed_transcript(
    before: dict[pathlib.Path, tuple[int, int]], after: dict[pathlib.Path, tuple[int, int]]
) -> pathlib.Path:
    changed = [path for path, facts in after.items() if before.get(path) != facts]
    if len(changed) != 1:
        raise ProbeError(f"expected one changed transcript, found {[str(p) for p in changed]}")
    return changed[0]


def json_arg(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ProbeError(f"tool arguments are not JSON: {value!r}") from exc
        if isinstance(parsed, dict):
            return parsed
    raise ProbeError(f"tool arguments are not an object: {value!r}")


def result_detail(message: dict[str, Any]) -> dict[str, Any]:
    details = message.get("details")
    if isinstance(details, dict):
        return details
    for part in message.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            try:
                parsed = json.loads(part["text"])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise ProbeError("tool result has no parseable JSON domain result")


def parse_transcript(path: pathlib.Path) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    results: dict[str, dict[str, Any]] = {}
    seen_result_ids: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProbeError(f"invalid JSONL at {path}:{line_no}") from exc
            if record.get("type") != "message" or not isinstance(record.get("message"), dict):
                continue
            message = record["message"]
            if any(record.get(key) is True or message.get(key) is True for key in ("synthetic", "repaired", "isSynthetic")):
                raise ProbeError(f"synthetic/repaired transcript record at line {line_no}")
            if message.get("role") == "assistant":
                for part in message.get("content") or []:
                    if not isinstance(part, dict) or part.get("type") != "toolCall":
                        continue
                    call_id = part.get("id") or part.get("toolCallId")
                    name = part.get("name")
                    if not isinstance(call_id, str) or not isinstance(name, str):
                        raise ProbeError(f"malformed tool call at line {line_no}")
                    calls.append(
                        {
                            "id": call_id,
                            "name": name,
                            "args": json_arg(part.get("arguments", part.get("input", {}))),
                            "line": line_no,
                        }
                    )
            elif message.get("role") == "toolResult":
                result_id = message.get("toolCallId") or message.get("id")
                if not isinstance(result_id, str):
                    raise ProbeError(f"tool result without id at line {line_no}")
                if result_id in seen_result_ids:
                    raise ProbeError(f"duplicate tool result id {result_id}")
                seen_result_ids.add(result_id)
                results[result_id] = {
                    "name": message.get("toolName") or message.get("name"),
                    "detail": result_detail(message),
                    "line": line_no,
                }
    call_ids = [call["id"] for call in calls]
    if len(call_ids) != len(set(call_ids)):
        raise ProbeError("duplicate tool call ids")
    if set(results) - set(call_ids):
        raise ProbeError(f"orphan tool results: {sorted(set(results) - set(call_ids))}")
    normalized = []
    for ordinal, call in enumerate(calls, 1):
        result = results.get(call["id"])
        if result is None:
            raise ProbeError(f"missing result for tool call {call['id']}")
        if result["line"] <= call["line"]:
            raise ProbeError(f"tool result precedes call {call['id']}")
        if result["name"] not in (None, call["name"]):
            raise ProbeError(f"tool result name mismatch for {call['id']}")
        detail = result["detail"]
        success = detail.get("found") is True if call["name"] == "note_read" else detail.get("ok") is True
        normalized.append(
            {
                "ordinal": ordinal,
                "id": call["id"],
                "name": call["name"],
                "args": call["args"],
                "result": detail,
                "result_success": success,
            }
        )
    return normalized


def subset_matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and subset_matches(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and actual == expected
    return actual == expected


def extract_final_text(payload: dict[str, Any]) -> str:
    candidates: list[str] = []
    for root in (payload, payload.get("result") if isinstance(payload.get("result"), dict) else {}):
        for item in root.get("payloads") or []:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                candidates.append(item["text"])
    if not candidates:
        raise ProbeError("CLI JSON has no text payload")
    return "\n".join(candidates).strip()


def extract_meta(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("meta"), dict):
        return payload["meta"]
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("meta"), dict):
        return result["meta"]
    return {}


def parse_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    output: dict[str, Any] = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            output[key.strip()] = [item.strip() for item in value[1:-1].split(",") if item.strip()]
        else:
            output[key.strip()] = value
    return output


def assertion(assertions: list[dict[str, Any]], ident: str, ok: bool, expected: Any, actual: Any) -> None:
    assertions.append({"id": ident, "ok": bool(ok), "expected": expected, "actual": actual})


def grade_case(
    case: dict[str, Any],
    scenario: dict[str, Any],
    calls: list[dict[str, Any]],
    final_text: str,
    before_manifest: dict[str, Any],
    after_manifest: dict[str, Any],
    vault: pathlib.Path,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    expected_calls = case["expected_calls"]
    assertion(out, "tool_sequence", [call["name"] for call in calls] == [item["name"] for item in expected_calls], [item["name"] for item in expected_calls], [call["name"] for call in calls])
    assertion(out, "tool_count", len(calls) == len(expected_calls), len(expected_calls), len(calls))
    for index, expected in enumerate(expected_calls):
        if index >= len(calls):
            break
        assertion(out, f"call_{index + 1}_args", subset_matches(calls[index]["args"], expected.get("args_subset", {})), expected.get("args_subset", {}), calls[index]["args"])
        assertion(out, f"call_{index + 1}_result", calls[index]["result_success"], True, calls[index]["result"])
    assertion(out, "final_text", final_text == case["final_equals"], case["final_equals"], final_text)
    assertion(out, "no_symlinks", all(item.get("type") != "symlink" for item in after_manifest.values()), True, [key for key, item in after_manifest.items() if item.get("type") == "symlink"])

    note_rel = scenario["note_title"] + ".md"
    archive_rel = scenario["archive_title"] + ".md"
    index_path = vault / "Index.md"
    if case["id"] == "write_read":
        note_path = vault / note_rel
        text = note_path.read_text(encoding="utf-8") if note_path.is_file() else ""
        front = parse_frontmatter(text)
        assertion(out, "note_created", note_path.is_file(), True, note_path.is_file())
        assertion(out, "note_body_exact", scenario["note_body"] in text, scenario["note_body"], text)
        assertion(out, "note_tags", front.get("tags") == scenario["tags"], scenario["tags"], front.get("tags"))
        index_text = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
        assertion(out, "index_link", f"[[{scenario['note_title']}]]" in index_text, scenario["note_title"], index_text)
        assertion(out, "no_pending_folders", not (vault / "Pending-Folders.md").exists(), False, (vault / "Pending-Folders.md").exists())
        allowed_files = {".hw1-openclaw-benchmark-vault", note_rel, "Index.md"}
        actual_files = {key for key, value in after_manifest.items() if value.get("type") == "file"}
        assertion(out, "expected_vault_files", actual_files == allowed_files, sorted(allowed_files), sorted(actual_files))
        first_search = calls[0]["result"] if calls else {}
        assertion(out, "initial_search_empty", first_search.get("count") == 0, 0, first_search.get("count"))
    elif case["id"] == "cross_session_recall":
        assertion(out, "vault_unchanged", before_manifest == after_manifest, before_manifest, after_manifest)
        search = calls[0]["result"] if calls else {}
        matches = search.get("matches") if isinstance(search.get("matches"), list) else []
        snippet = " ".join(str(item.get("snippet", "")) for item in matches if isinstance(item, dict))
        assertion(out, "search_found_one", search.get("count") == 1, 1, search.get("count"))
        assertion(out, "search_snippet_hides_answer", scenario["answer_code"] not in snippet, "answer absent", snippet)
        read = calls[1]["result"] if len(calls) > 1 else {}
        assertion(out, "read_contains_answer", scenario["answer_code"] in str(read.get("content", "")), scenario["answer_code"], read.get("content"))
    elif case["id"] == "archive_verify":
        source = vault / note_rel
        archived = vault / archive_rel
        text = archived.read_text(encoding="utf-8") if archived.is_file() else ""
        front = parse_frontmatter(text)
        assertion(out, "source_removed", not source.exists(), False, source.exists())
        assertion(out, "archive_created", archived.is_file(), True, archived.is_file())
        assertion(out, "archive_body_preserved", scenario["note_body"] in text, scenario["note_body"], text)
        expected_tags = scenario["tags"] + ["archived"]
        assertion(out, "archive_tags", front.get("tags") == expected_tags, expected_tags, front.get("tags"))
        index_text = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
        assertion(out, "archive_not_indexed", scenario["note_title"] not in index_text and scenario["archive_title"] not in index_text, "neither path indexed", index_text)
        search = calls[1]["result"] if len(calls) > 1 else {}
        assertion(out, "post_archive_search_empty", search.get("count") == 0, 0, search.get("count"))
        allowed_files = {".hw1-openclaw-benchmark-vault", archive_rel, "Index.md"}
        actual_files = {key for key, value in after_manifest.items() if value.get("type") == "file"}
        assertion(out, "expected_vault_files", actual_files == allowed_files, sorted(allowed_files), sorted(actual_files))
    return out


def model_health(url: str, timeout: float = 0.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return False


def model_auth_ready(health_url: str, api_key: str, timeout: float = 1.0) -> bool:
    url = health_url.removesuffix("/health") + "/tokenize"
    payload = json.dumps({"content": "auth-probe"}).encode("utf-8")
    plain = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(plain, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            return False
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return False
    else:
        return False
    authorized = urllib.request.Request(
        url,
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(authorized, timeout=timeout) as response:
            body = response.read(65537)
            if not 200 <= response.status < 300 or len(body) > 65536:
                return False
        value = json.loads(body)
        return isinstance(value, dict) and isinstance(value.get("tokens"), list)
    except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
        return False


def start_model(
    config: dict[str, Any], run_dir: pathlib.Path, timeout: int, api_key: str
) -> tuple[subprocess.Popen[bytes], float, str]:
    if re.fullmatch(r"[0-9a-f]{64}", api_key) is None:
        raise ProbeError("benchmark model API key is malformed")
    provider = config["models"]["providers"]["hw1local"]
    local = provider["localService"]
    command = local["command"]
    args = local["args"]
    health_url = local["healthUrl"]
    if model_health(health_url):
        raise ProbeError(f"benchmark model endpoint was already live: {health_url}")
    log = (run_dir / "llama-server.log").open("wb")
    started = time.monotonic()
    model_env = os.environ.copy()
    model_env["LLAMA_API_KEY"] = api_key
    try:
        process = subprocess.Popen(
            [command, *args],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=model_env,
            start_new_session=True,
        )
    finally:
        log.close()
    deadline = started + timeout
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ProbeError(f"llama-server exited during startup with {process.returncode}")
            if model_health(health_url) and model_auth_ready(health_url, api_key):
                return process, (time.monotonic() - started) * 1000.0, health_url
            time.sleep(0.5)
        stop_process(process)
        raise ProbeError(f"llama-server startup was right-censored at {timeout}s")
    except BaseException:
        stop_process(process)
        raise


def run_model_speed_probe(
    config: dict[str, Any], api_key: str, samples: int, max_tokens: int
) -> dict[str, Any]:
    """Measure direct local-model streaming latency separately from agent/tool latency."""
    provider = config["models"]["providers"]["hw1local"]
    base_url = str(provider["baseUrl"]).rstrip("/")
    model_id = str(provider["models"][0]["id"])
    endpoint = f"{base_url}/chat/completions"
    output: dict[str, Any] = {
        "status": "SKIPPED" if samples == 0 else "PASS",
        "endpoint": endpoint,
        "model": model_id,
        "samples": [],
    }
    if samples == 0:
        return output
    prompt = "Reply with exactly the single word benchmark-ok."
    for sample_no in range(1, samples + 1):
        request_body = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(request_body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )
        started = time.monotonic()
        first_token: float | None = None
        finished: float | None = None
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        chunks = 0
        text_parts: list[str] = []
        try:
            with urllib.request.urlopen(request, timeout=900) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        finished = time.monotonic()
                        break
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    usage = event.get("usage")
                    if isinstance(usage, dict):
                        if isinstance(usage.get("prompt_tokens"), int):
                            prompt_tokens = usage["prompt_tokens"]
                        if isinstance(usage.get("completion_tokens"), int):
                            completion_tokens = usage["completion_tokens"]
                    choices = event.get("choices")
                    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                        continue
                    delta = choices[0].get("delta")
                    if not isinstance(delta, dict):
                        continue
                    content = delta.get("content")
                    reasoning = delta.get("reasoning_content")
                    tool_calls = delta.get("tool_calls")
                    if content or reasoning or tool_calls:
                        chunks += 1
                        first_token = first_token or time.monotonic()
                        if isinstance(content, str):
                            text_parts.append(content)
                finished = finished or time.monotonic()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            output["status"] = "ERROR"
            output.setdefault("errors", []).append(f"sample {sample_no}: {type(exc).__name__}: {exc}")
            continue
        total_ms = (finished - started) * 1000.0
        ttft_ms = (first_token - started) * 1000.0 if first_token is not None else None
        decode_ms = (finished - first_token) * 1000.0 if first_token is not None else None
        measured_tokens = completion_tokens if completion_tokens and completion_tokens > 0 else chunks
        tokens_per_second = measured_tokens / (decode_ms / 1000.0) if decode_ms and measured_tokens else None
        output["samples"].append(
            {
                "sample": sample_no,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "stream_chunks": chunks,
                "ttft_ms": round(ttft_ms, 3) if ttft_ms is not None else None,
                "e2e_ms": round(total_ms, 3),
                "decode_ms": round(decode_ms, 3) if decode_ms is not None else None,
                "tokens_per_second": round(tokens_per_second, 3) if tokens_per_second is not None else None,
                "text_sha256": hashlib.sha256("".join(text_parts).encode("utf-8")).hexdigest(),
            }
        )
    if not output["samples"] and output["status"] == "PASS":
        output["status"] = "ERROR"
    return output


def signal_process_group(process: subprocess.Popen[bytes], signum: signal.Signals) -> None:
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def stop_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        signal_process_group(process, signal.SIGTERM)
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        signal_process_group(process, signal.SIGKILL)
        process.wait(timeout=10)


def invoke_case(
    *,
    node: pathlib.Path,
    openclaw: pathlib.Path,
    config: pathlib.Path,
    state: pathlib.Path,
    home: pathlib.Path,
    vault: pathlib.Path,
    bench_root: pathlib.Path,
    case: dict[str, Any],
    case_dir: pathlib.Path,
    timeout: int,
) -> dict[str, Any]:
    prompt_path = case_dir / "prompt.txt"
    prompt_path.write_text(case["prompt"] + "\n", encoding="utf-8")
    os.chmod(prompt_path, 0o600)
    session_key = f"case-{case['id']}-{uuid.uuid4().hex}"
    cmd = [
        str(node), str(openclaw), "agent", "--agent", "bench", "--session-key", session_key,
        "--message-file", str(prompt_path), "--thinking", "off", "--timeout", str(timeout), "--local", "--json",
    ]
    env = {
        "HOME": str(home),
        "USER": "openclaw-bench",
        "LOGNAME": "openclaw-bench",
        "PATH": f"{node.parent}:/usr/bin:/bin",
        "LC_ALL": "C",
        "OPENCLAW_STATE_DIR": str(state),
        "OPENCLAW_CONFIG_PATH": str(config),
        "OPENCLAW_NO_RESPAWN": "1",
        "OPENCLAW_DISABLE_BONJOUR": "1",
        "OPENCLAW_DISABLE_PLUGIN_REGISTRY_MIGRATION": "1",
        "AGENT_NOTES_VAULT": str(vault),
        "AGENT_NOTES_MAX_VAULT_BYTES": "67108864",
        "AGENT_NOTES_DESTRUCTIVE_APPROVALS": "0",
        "AGENT_NOTES_BENCHMARK_ROOT": str(bench_root),
    }
    before_transcripts = transcript_snapshot(state)
    started = time.monotonic()
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)
    censored = False
    try:
        stdout, stderr = process.communicate(timeout=timeout + 30)
    except subprocess.TimeoutExpired:
        censored = True
        signal_process_group(process, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            signal_process_group(process, signal.SIGKILL)
            stdout, stderr = process.communicate()
    except BaseException:
        if process.poll() is None:
            signal_process_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                signal_process_group(process, signal.SIGKILL)
                process.wait(timeout=10)
        raise
    wall_ms = (time.monotonic() - started) * 1000.0
    (case_dir / "stdout.json").write_bytes(stdout)
    (case_dir / "stderr.log").write_bytes(stderr)
    if censored:
        raise ProbeError(f"case {case['id']} was right-censored at {timeout + 30}s")
    if process.returncode != 0:
        raise ProbeError(f"case {case['id']} CLI exit {process.returncode}; see stderr.log")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"case {case['id']} stdout is not JSON") from exc
    meta = extract_meta(payload)
    if meta.get("transport") not in (None, "embedded") or meta.get("fallbackFrom") is not None:
        raise ProbeError(f"unexpected transport/fallback metadata: {meta}")
    transcript = changed_transcript(before_transcripts, transcript_snapshot(state))
    archived_transcript = case_dir / "session.jsonl"
    shutil.copy2(transcript, archived_transcript)
    calls = parse_transcript(archived_transcript)
    return {
        "session_key": session_key,
        "session_file": str(transcript),
        "prompt_sha256": sha256_file(prompt_path),
        "stdout_sha256": sha256_file(case_dir / "stdout.json"),
        "transcript_sha256": sha256_file(archived_transcript),
        "wall_ms": round(wall_ms, 3),
        "right_censored": False,
        "cli_exit": process.returncode,
        "transport": meta.get("transport", "embedded"),
        "tool_summary": meta.get("toolSummary"),
        "final_text": extract_final_text(payload),
        "calls": calls,
    }


def prepare_config(
    source: pathlib.Path, output: pathlib.Path, workspace: pathlib.Path, model_api_key: str
) -> dict[str, Any]:
    config = json.loads(source.read_text(encoding="utf-8"))
    provider = config["models"]["providers"]["hw1local"]
    if provider.get("apiKey") != "benchmark-run-injected":
        raise ProbeError("benchmark config has an unexpected model API-key marker")
    if re.fullmatch(r"[0-9a-f]{64}", model_api_key) is None:
        raise ProbeError("benchmark model API key is malformed")
    provider["apiKey"] = model_api_key
    config["agents"]["defaults"]["workspace"] = str(workspace)
    for agent in config["agents"]["list"]:
        agent["workspace"] = str(workspace)
    write_json(output, config)
    return config


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", type=pathlib.Path, default=pathlib.Path("/opt/hw1-openclaw/node/bin/node"))
    parser.add_argument("--openclaw", type=pathlib.Path, default=pathlib.Path("/opt/hw1-openclaw/current/openclaw.mjs"))
    parser.add_argument("--config", type=pathlib.Path, default=pathlib.Path("/etc/hw1-openclaw-bench/openclaw.json"))
    parser.add_argument("--cases", type=pathlib.Path, required=True)
    parser.add_argument("--run-dir", type=pathlib.Path, required=True)
    parser.add_argument(
        "--production-vault",
        type=pathlib.Path,
        default=pathlib.Path("/srv/hw1-openclaw-vaults/main"),
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--case-timeout", type=int, default=600)
    parser.add_argument("--model-start-timeout", type=int, default=300)
    parser.add_argument("--model-probes", type=int, default=1, help="direct streaming speed probes; 0 disables")
    parser.add_argument("--model-max-tokens", type=int, default=64)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not 1 <= args.repeats <= 20:
        raise ProbeError("--repeats must be 1..20")
    if not 30 <= args.case_timeout <= 1800:
        raise ProbeError("--case-timeout must be 30..1800")
    if not 0 <= args.model_probes <= 10:
        raise ProbeError("--model-probes must be 0..10")
    if not 8 <= args.model_max_tokens <= 512:
        raise ProbeError("--model-max-tokens must be 8..512")
    account = pwd.getpwuid(os.getuid()).pw_name
    if account != "openclaw-bench":
        raise ProbeError(f"probe must run as openclaw-bench, not {account}")
    benchmark_root = BENCH_ROOT_PREFIX.resolve(strict=True)
    run_dir = assert_private_directory(args.run_dir, benchmark_root)
    for required in (args.node, args.openclaw, args.config, args.cases):
        if not required.is_file() or required.is_symlink():
            raise ProbeError(f"required input is missing or symlinked: {required}")
    if args.production_vault.exists() and any(
        os.access(args.production_vault, mode) for mode in (os.R_OK, os.W_OK, os.X_OK)
    ):
        raise ProbeError(f"benchmark uid can access production vault: {args.production_vault}")

    cases_data = load_cases(args.cases)
    config_dir = run_dir / "config"
    workspace = run_dir / "workspace"
    home = run_dir / "home"
    for path in (config_dir, workspace, home):
        path.mkdir(mode=0o700)
    rendered_config = config_dir / "openclaw.json"
    model_api_key = secrets.token_hex(32)
    config = prepare_config(args.config, rendered_config, workspace, model_api_key)

    run_result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "benchmark": "hw1-openclaw-obsidian-memory",
        "status": "FAIL",
        "uid": os.getuid(),
        "account": account,
        "run_dir": str(run_dir),
        "production_vault_accessible": False,
        "openclaw_sha256": sha256_file(args.openclaw),
        "config_sha256": sha256_file(rendered_config),
        "cases_sha256": sha256_file(args.cases),
        "repeats": [],
    }
    model_process: subprocess.Popen[bytes] | None = None
    try:
        model_process, startup_ms, health_url = start_model(
            config, run_dir, args.model_start_timeout, model_api_key
        )
        run_result["model_startup_ms"] = round(startup_ms, 3)
        run_result["model_health_url"] = health_url
        run_result["model_speed"] = run_model_speed_probe(
            config, model_api_key, args.model_probes, args.model_max_tokens
        )
        for repeat_no in range(1, args.repeats + 1):
            repeat_dir = run_dir / f"repeat-{repeat_no:02d}"
            repeat_dir.mkdir(mode=0o700)
            state = repeat_dir / "state"
            vault = repeat_dir / "vault"
            repeat_home = repeat_dir / "home"
            for path in (state, vault, repeat_home):
                path.mkdir(mode=0o700)
            for folder in (vault / "reference", vault / "projects", vault / "log", vault / "archive"):
                folder.mkdir(mode=0o700)
            sentinel = vault / ".hw1-openclaw-benchmark-vault"
            sentinel.write_text("disposable benchmark vault\n", encoding="utf-8")
            os.chmod(sentinel, 0o600)
            repeat_result: dict[str, Any] = {"repeat": repeat_no, "status": "PASS", "cases": []}
            for case in cases_data["cases"]:
                case_dir = repeat_dir / case["id"]
                case_dir.mkdir(mode=0o700)
                before = tree_manifest(vault)
                case_result: dict[str, Any] = {"id": case["id"], "status": "FAIL"}
                try:
                    evidence = invoke_case(
                        node=args.node,
                        openclaw=args.openclaw,
                        config=rendered_config,
                        state=state,
                        home=repeat_home,
                        vault=vault,
                        bench_root=repeat_dir,
                        case=case,
                        case_dir=case_dir,
                        timeout=args.case_timeout,
                    )
                    after = tree_manifest(vault)
                    assertions = grade_case(
                        case, cases_data["scenario"], evidence["calls"], evidence["final_text"], before, after, vault
                    )
                    case_result.update(evidence)
                    case_result["assertions"] = assertions
                    case_result["vault_before"] = before
                    case_result["vault_after"] = after
                    case_result["status"] = "PASS" if all(item["ok"] for item in assertions) else "FAIL"
                except ProbeTermination:
                    raise
                except Exception as exc:  # retain every partial artifact and continue dependent cases
                    case_result["error"] = f"{type(exc).__name__}: {exc}"
                    case_result["vault_before"] = before
                    case_result["vault_after"] = tree_manifest(vault)
                write_json(case_dir / "case.json", case_result)
                repeat_result["cases"].append(case_result)
                if case_result["status"] != "PASS":
                    repeat_result["status"] = "FAIL"
            repeat_result["final_vault_manifest"] = tree_manifest(vault)
            write_json(repeat_dir / "repeat.json", repeat_result)
            run_result["repeats"].append(repeat_result)
        run_result["status"] = "PASS" if all(item["status"] == "PASS" for item in run_result["repeats"]) else "FAIL"
    finally:
        stop_process(model_process)
        run_result["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        write_json(run_dir / "run.json", run_result)
    print(json.dumps({"status": run_result["status"], "run_dir": str(run_dir)}, sort_keys=True))
    return 0 if run_result["status"] == "PASS" else 1


if __name__ == "__main__":
    for termination_signal in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(termination_signal, handle_termination)
    try:
        raise SystemExit(main(sys.argv[1:]))
    except ProbeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
