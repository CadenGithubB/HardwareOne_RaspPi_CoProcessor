from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROBE_PATH = ROOT / "tools" / "openclaw" / "openclaw_memory_probe.py"
CASES_PATH = ROOT / "tools" / "openclaw" / "openclaw_memory_cases.json"
BENCHMARK_TEMPLATE = ROOT / "openclaw" / "benchmark.json.in"


def _load_probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hw1_openclaw_memory_probe", PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")
    return path


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def _assistant_call(call_id: str, name: str, arguments: object) -> dict:
    return {
        "type": "message",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "id": call_id,
                    "name": name,
                    "arguments": arguments,
                }
            ],
        },
    }


def _tool_result(call_id: str, name: str, detail: dict, *, text_envelope: bool = False) -> dict:
    message: dict = {"role": "toolResult", "toolCallId": call_id, "toolName": name}
    if text_envelope:
        message["content"] = [{"type": "text", "text": json.dumps(detail)}]
    else:
        message["details"] = detail
    return {"type": "message", "message": message}


def _calls_for(case: dict, results: list[dict]) -> list[dict]:
    calls = []
    for ordinal, (expected, result) in enumerate(zip(case["expected_calls"], results, strict=True), 1):
        calls.append(
            {
                "ordinal": ordinal,
                "id": f"call-{ordinal}",
                "name": expected["name"],
                "args": copy.deepcopy(expected.get("args_subset", {})),
                "result": result,
                "result_success": True,
            }
        )
    return calls


def _assert_all_grade_assertions_pass(assertions: list[dict]) -> None:
    failures = [item for item in assertions if not item["ok"]]
    assert failures == []


def _note_text(tags: list[str], body: str) -> str:
    return f"---\ntags: [{', '.join(tags)}]\n---\n{body}\n"


def test_load_cases_accepts_the_pinned_scenario() -> None:
    data = probe.load_cases(CASES_PATH)
    assert data["schema_version"] == probe.SCHEMA_VERSION
    assert [case["id"] for case in data["cases"]] == probe.EXPECTED_CASE_IDS
    assert data["scenario"]["answer_code"] not in data["cases"][1]["expected_calls"][0]["args_subset"]["query"]


def test_load_cases_rejects_schema_order_and_incomplete_records(tmp_path: Path) -> None:
    original = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    invalid: list[dict] = []

    wrong_schema = copy.deepcopy(original)
    wrong_schema["schema_version"] += 1
    invalid.append(wrong_schema)

    wrong_order = copy.deepcopy(original)
    wrong_order["cases"] = list(reversed(wrong_order["cases"]))
    invalid.append(wrong_order)

    incomplete_scenario = copy.deepcopy(original)
    del incomplete_scenario["scenario"]["note_body"]
    invalid.append(incomplete_scenario)

    blank_prompt = copy.deepcopy(original)
    blank_prompt["cases"][0]["prompt"] = "  "
    invalid.append(blank_prompt)

    no_expected_calls = copy.deepcopy(original)
    no_expected_calls["cases"][1]["expected_calls"] = []
    invalid.append(no_expected_calls)

    for index, value in enumerate(invalid):
        with pytest.raises(probe.ProbeError):
            probe.load_cases(_write_json(tmp_path / f"invalid-{index}.json", value))


def test_subset_matching_is_recursive_but_lists_remain_exact() -> None:
    actual = {
        "query": "cobalt-sparrow-731",
        "filters": {"folder": "reference", "limit": 25},
        "exclude": ["log", "archive"],
        "extra": True,
    }
    assert probe.subset_matches(
        actual,
        {
            "query": "cobalt-sparrow-731",
            "filters": {"folder": "reference"},
            "exclude": ["log", "archive"],
        },
    )
    assert not probe.subset_matches(actual, {"filters": {"folder": "projects"}})
    assert not probe.subset_matches(actual, {"exclude": ["archive", "log"]})
    assert not probe.subset_matches(actual, {"missing": None})


def test_parse_transcript_pairs_calls_and_both_result_envelopes(tmp_path: Path) -> None:
    transcript = _write_jsonl(
        tmp_path / "session.jsonl",
        [
            {"type": "session", "id": "ignored"},
            _assistant_call(
                "search-1",
                "note_search",
                json.dumps({"query": "cobalt-sparrow-731", "folder": "reference"}),
            ),
            _tool_result("search-1", "note_search", {"ok": True, "count": 1}),
            _assistant_call("read-1", "note_read", {"title": "reference/memory"}),
            _tool_result(
                "read-1",
                "note_read",
                {"found": True, "title": "reference/memory", "content": "delta-4829"},
                text_envelope=True,
            ),
        ],
    )

    calls = probe.parse_transcript(transcript)
    assert [call["name"] for call in calls] == ["note_search", "note_read"]
    assert calls[0]["args"] == {"query": "cobalt-sparrow-731", "folder": "reference"}
    assert calls[0]["result"] == {"ok": True, "count": 1}
    assert calls[1]["result"]["content"] == "delta-4829"
    assert all(call["result_success"] for call in calls)


@pytest.mark.parametrize(
    "records",
    [
        [
            {
                "type": "message",
                "synthetic": True,
                "message": {"role": "assistant", "content": []},
            }
        ],
        [_tool_result("orphan", "note_search", {"ok": True})],
        [_assistant_call("missing", "note_search", {})],
        [
            _assistant_call("duplicate", "note_search", {}),
            _tool_result("duplicate", "note_search", {"ok": True}),
            _tool_result("duplicate", "note_search", {"ok": True}),
        ],
        [
            _assistant_call("mismatch", "note_search", {}),
            _tool_result("mismatch", "note_read", {"ok": True}),
        ],
    ],
)
def test_parse_transcript_rejects_unverifiable_evidence(tmp_path: Path, records: list[dict]) -> None:
    with pytest.raises(probe.ProbeError):
        probe.parse_transcript(_write_jsonl(tmp_path / "bad.jsonl", records))


def test_parse_transcript_rejects_invalid_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "invalid.jsonl"
    path.write_text('{"type":"message"}\nnot-json\n', encoding="utf-8")
    with pytest.raises(probe.ProbeError, match="invalid JSONL"):
        probe.parse_transcript(path)


def test_embedded_and_gateway_json_envelopes_are_normalized() -> None:
    embedded = {
        "payloads": [{"text": "WRITE_READ_OK"}],
        "meta": {"transport": "embedded", "toolSummary": {"calls": 4}},
    }
    gateway = {
        "result": {
            "payloads": [{"text": "delta-4829"}],
            "meta": {"transport": "gateway", "toolSummary": {"calls": 2}},
        }
    }
    assert probe.extract_final_text(embedded) == "WRITE_READ_OK"
    assert probe.extract_meta(embedded)["toolSummary"]["calls"] == 4
    assert probe.extract_final_text(gateway) == "delta-4829"
    assert probe.extract_meta(gateway)["transport"] == "gateway"
    with pytest.raises(probe.ProbeError, match="no text payload"):
        probe.extract_final_text({"payloads": []})


def test_changed_transcript_requires_exactly_one_new_or_modified_file(tmp_path: Path) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text("{}\n", encoding="utf-8")
    before = {first: (first.stat().st_size, first.stat().st_mtime_ns)}
    second.write_text("{}\n", encoding="utf-8")
    after = {
        first: (first.stat().st_size, first.stat().st_mtime_ns),
        second: (second.stat().st_size, second.stat().st_mtime_ns),
    }
    assert probe.changed_transcript(before, after) == second
    with pytest.raises(probe.ProbeError, match="expected one changed transcript"):
        probe.changed_transcript(before, before)


def test_prepare_config_rehomes_every_benchmark_agent(tmp_path: Path) -> None:
    raw = BENCHMARK_TEMPLATE.read_text(encoding="utf-8")
    raw = raw.replace("@@LLAMA_SERVER@@", "/opt/llama-server")
    raw = raw.replace("@@LLAMA_MODEL@@", "/opt/model.gguf")
    raw = raw.replace("@@LLAMA_PORT@@", "18081")
    raw = raw.replace("@@CONTEXT_TOKENS@@", "16384")
    source = tmp_path / "source.json"
    source.write_text(raw, encoding="utf-8")
    output = tmp_path / "rendered.json"
    workspace = tmp_path / "workspace"

    model_api_key = "a" * 64
    config = probe.prepare_config(source, output, workspace, model_api_key)
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert config["models"]["providers"]["hw1local"]["apiKey"] == model_api_key
    assert config["agents"]["defaults"]["workspace"] == str(workspace)
    assert {agent["workspace"] for agent in config["agents"]["list"]} == {str(workspace)}
    assert persisted == config
    assert output.stat().st_mode & 0o777 == 0o600
    assert "/var/lib/hw1-openclaw-bench/workspace" in source.read_text(encoding="utf-8")


def test_private_directory_rejects_modes_escape_and_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    private = root / "private"
    private.mkdir(mode=0o700)
    assert probe.assert_private_directory(private, root) == private.resolve()

    private.chmod(0o750)
    with pytest.raises(probe.ProbeError, match="not private"):
        probe.assert_private_directory(private, root)
    private.chmod(0o700)

    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    with pytest.raises(probe.ProbeError, match="escaped benchmark root"):
        probe.assert_private_directory(outside, root)

    link = root / "link"
    link.symlink_to(private, target_is_directory=True)
    with pytest.raises(probe.ProbeError, match="symlinked"):
        probe.assert_private_directory(link, root)


def test_tree_manifest_records_files_directories_and_symlinks(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    folder = vault / "reference"
    folder.mkdir(parents=True)
    note = folder / "note.md"
    note.write_text("hello\n", encoding="utf-8")
    (vault / "link.md").symlink_to(note)

    manifest = probe.tree_manifest(vault)
    assert manifest["reference"] == {"type": "dir"}
    assert manifest["reference/note.md"]["type"] == "file"
    assert manifest["reference/note.md"]["bytes"] == 6
    assert len(manifest["reference/note.md"]["sha256"]) == 64
    assert manifest["link.md"]["type"] == "symlink"


def test_grade_cases_against_a_disposable_vault(tmp_path: Path) -> None:
    data = probe.load_cases(CASES_PATH)
    scenario = data["scenario"]
    cases = {case["id"]: case for case in data["cases"]}
    vault = tmp_path / "vault"
    for folder in (vault / "reference", vault / "projects", vault / "log", vault / "archive"):
        folder.mkdir(parents=True, exist_ok=True)
    sentinel = vault / ".hw1-openclaw-benchmark-vault"
    sentinel.write_text("disposable benchmark vault\n", encoding="utf-8")
    sentinel.chmod(0o600)

    before_write = probe.tree_manifest(vault)
    note = vault / f"{scenario['note_title']}.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(_note_text(scenario["tags"], scenario["note_body"]), encoding="utf-8")
    index = vault / "Index.md"
    index.write_text(f"# Index\n\n[[{scenario['note_title']}]]\n", encoding="utf-8")
    write_calls = _calls_for(
        cases["write_read"],
        [
            {"ok": True, "count": 0, "matches": []},
            {"ok": True, "folders": [{"folder": "reference"}]},
            {"ok": True, "savedAs": scenario["note_title"]},
            {"found": True, "content": scenario["note_body"]},
        ],
    )
    write_grade = probe.grade_case(
        cases["write_read"],
        scenario,
        write_calls,
        cases["write_read"]["final_equals"],
        before_write,
        probe.tree_manifest(vault),
        vault,
    )
    _assert_all_grade_assertions_pass(write_grade)

    before_recall = probe.tree_manifest(vault)
    recall_calls = _calls_for(
        cases["cross_session_recall"],
        [
            {
                "ok": True,
                "count": 1,
                "matches": [{"note": scenario["note_title"], "snippet": scenario["lookup_key"]}],
            },
            {"found": True, "content": scenario["note_body"]},
        ],
    )
    recall_grade = probe.grade_case(
        cases["cross_session_recall"],
        scenario,
        recall_calls,
        scenario["answer_code"],
        before_recall,
        probe.tree_manifest(vault),
        vault,
    )
    _assert_all_grade_assertions_pass(recall_grade)

    before_archive = probe.tree_manifest(vault)
    note.unlink()
    archived = vault / f"{scenario['archive_title']}.md"
    archived.parent.mkdir(parents=True, exist_ok=True)
    archived.write_text(
        _note_text([*scenario["tags"], "archived"], scenario["note_body"]),
        encoding="utf-8",
    )
    index.write_text("# Index\n", encoding="utf-8")
    archive_calls = _calls_for(
        cases["archive_verify"],
        [
            {"ok": True, "archivedAs": scenario["archive_title"]},
            {"ok": True, "count": 0, "matches": []},
        ],
    )
    archive_grade = probe.grade_case(
        cases["archive_verify"],
        scenario,
        archive_calls,
        cases["archive_verify"]["final_equals"],
        before_archive,
        probe.tree_manifest(vault),
        vault,
    )
    _assert_all_grade_assertions_pass(archive_grade)


def test_grade_case_reports_bad_arguments_results_and_final_text(tmp_path: Path) -> None:
    data = probe.load_cases(CASES_PATH)
    scenario = data["scenario"]
    case = data["cases"][0]
    vault = tmp_path / "vault"
    vault.mkdir()
    calls = _calls_for(case, [{"ok": False}] * len(case["expected_calls"]))
    calls[0]["args"]["query"] = "wrong"
    calls[0]["result_success"] = False

    assertions = probe.grade_case(case, scenario, calls, "wrong", {}, {}, vault)
    by_id = {item["id"]: item for item in assertions}
    assert by_id["call_1_args"]["ok"] is False
    assert by_id["call_1_result"]["ok"] is False
    assert by_id["final_text"]["ok"] is False
