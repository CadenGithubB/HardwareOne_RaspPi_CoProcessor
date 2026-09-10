from __future__ import annotations

import re
import subprocess
from pathlib import Path

from hw1_ai_service.config import load


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "bootstrap.sh"


def _assignments() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in BOOTSTRAP.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=([^\s#]+)", line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def _manifest_row(path: Path, row_id: str) -> list[str]:
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw and not raw.startswith("#"):
            fields = raw.split("\t")
            if fields[0] == row_id:
                return fields
    raise AssertionError(f"missing manifest row {row_id!r} in {path}")


def test_bootstrap_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(BOOTSTRAP)], check=True)


def test_managed_model_pins_match_benchmark_manifests() -> None:
    values = _assignments()
    assert values["LLAMA_REF"] == "b10516"
    assert values["LLAMA_COMMIT"] == "b95502b"
    lfm = _manifest_row(
        ROOT / "tools/llm/llm_serve_models.tsv",
        "lfm2-8b-a1b-ud-q3-k-xl",
    )
    assert lfm[1:] == [
        values["LFM_REPO"],
        values["LFM_REV"],
        values["LFM_FILE"],
        values["LFM_BYTES"],
        values["LFM_SHA"],
    ]

    qwen = _manifest_row(
        ROOT / "tools/llm/llm_serve_oc_pi5_4gb.tsv",
        "qwen3.5-2b-q4_0",
    )
    assert qwen[1:] == [
        values["QWEN_REPO"],
        values["QWEN_REV"],
        values["QWEN_FILE"],
        values["QWEN_BYTES"],
        values["QWEN_SHA"],
    ]


def test_fresh_8gb_config_uses_lfm2_profile() -> None:
    values = _assignments()
    cfg = load(ROOT / "config.example.yaml")
    assert cfg.llm.model == f"/opt/models/{values['LFM_FILE']}"
    assert cfg.llm.server_bin == values["DEFAULT_SERVER_BIN"]


def test_incomplete_install_cannot_enable_service() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    success_gate = source.index('if [ "${#TODO[@]}" -eq 0 ]')
    enable = "systemctl --user enable hw1-ai-service.service"
    assert source.count(enable) == 1
    assert source.index(enable) > success_gate
    assert "service activation was left unchanged" in source


def test_guided_prerequisites_are_present() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    assert 'read -r -s -p "  ESP32 UART password: "' in source
    assert "moonshine-voice\" download --stt --language" in source
    assert "curl -fL --retry 5 --retry-all-errors -C -" in source
    assert "--llm-model auto|lfm2-8b-a1b|qwen3.5-2b" in source
    assert "command -v llama-server" not in source
