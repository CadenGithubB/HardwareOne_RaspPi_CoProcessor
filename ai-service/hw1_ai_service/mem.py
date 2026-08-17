"""RAM preflight: know whether the configured engines fit BEFORE loading
them, so a small-RAM Pi fails with an actionable message (or a warning)
instead of an OOM-killed process mid-exchange.

Estimates are deliberately rough-but-conservative:
  - LLM: GGUF file size x 1.2 + ~200MB (mmap'd weights become resident as
    they're touched; KV at -c 2048 is tens of MB; runtime overhead).
  - STT: static table by engine/model tier.
  - +300MB headroom for the service itself, page cache churn, and the OS
    not strangling.

On non-Linux dev machines (/proc/meminfo absent) the check is skipped.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .config import Config

log = logging.getLogger("mem")

_GB = 1024 ** 3
_MB = 1024 ** 2

_HEADROOM = 300 * _MB

# (engine, substring-of-model-name) -> resident estimate. First match wins;
# the bare-engine fallback covers unknown model names.
_STT_ESTIMATES: list[tuple[str, str, int]] = [
    ("moonshine", "tiny", 350 * _MB),
    ("moonshine", "medium", 1024 * _MB),
    ("moonshine", "", 600 * _MB),          # small/default
    ("zipformer", "", 400 * _MB),
    ("fake", "", 0),
    ("none", "", 0),
]


def read_meminfo(path: str = "/proc/meminfo") -> tuple[int, int] | None:
    """(MemTotal, MemAvailable) in bytes, or None when unreadable (macOS)."""
    try:
        text = Path(path).read_text()
    except OSError:
        return None
    vals: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            key = parts[0].rstrip(":")
            if key in ("MemTotal", "MemAvailable"):
                vals[key] = int(parts[1]) * 1024
    if "MemTotal" in vals and "MemAvailable" in vals:
        return vals["MemTotal"], vals["MemAvailable"]
    return None


def estimate_stt_bytes(engine: str, model: str) -> int:
    for eng, sub, size in _STT_ESTIMATES:
        if engine == eng and sub in (model or ""):
            return size
    return 600 * _MB


def estimate_llm_bytes(model_path: str) -> int | None:
    """None = cannot estimate (no local model file to size)."""
    if not model_path:
        return None
    try:
        size = os.path.getsize(os.path.expanduser(model_path))
    except OSError:
        return None
    return int(size * 1.2) + 200 * _MB


def preflight(cfg: Config, meminfo_path: str = "/proc/meminfo") -> list[str]:
    """Returns human-readable budget messages. In ram_check=strict mode an
    over-budget config raises; in warn mode it logs and proceeds (the user
    asked to try — the runtime degradation path is the safety net)."""
    mode = cfg.service.ram_check
    if mode == "off":
        return []
    mem = read_meminfo(meminfo_path)
    if mem is None:
        return []                          # non-Linux dev box: nothing to check
    total, avail = mem

    stt_need = estimate_stt_bytes(cfg.stt.engine, cfg.stt.model)
    # The live streaming worker is a SECOND resident transcriber alongside the
    # batch engine (Gate E). Size it by its arch tier so a config that only
    # just fits the batch model does not silently OOM when the gate warms up.
    live_need = 0
    if (cfg.stt.engine != "none" and cfg.stt.live_enabled
            and cfg.stt.live_model_dir):
        live_need = estimate_stt_bytes("moonshine", cfg.stt.live_model_arch)
    llm_need = estimate_llm_bytes(cfg.llm.model) if cfg.llm.engine == "server" else 0
    msgs = [f"RAM: {total / _GB:.1f}GB total, {avail / _GB:.1f}GB available"]
    if llm_need is None:
        msgs.append(f"LLM: cannot size model file {cfg.llm.model!r} — check skipped "
                    f"(external server or missing file)")
        llm_need = 0
    else:
        if cfg.llm.engine == "server":
            msgs.append(f"LLM estimate: {llm_need / _GB:.1f}GB ({cfg.llm.model})")
    if stt_need:
        msgs.append(f"STT estimate: {stt_need / _GB:.1f}GB "
                    f"({cfg.stt.engine} {cfg.stt.model})".rstrip())
    if live_need:
        msgs.append(f"live STT estimate: {live_need / _GB:.1f}GB "
                    f"(moonshine {cfg.stt.live_model_arch})")

    need = stt_need + live_need + llm_need + _HEADROOM
    if need > avail:
        over = (need - avail) / _GB
        advice = (
            f"engines need ~{need / _GB:.1f}GB but only {avail / _GB:.1f}GB is "
            f"available ({over:.1f}GB short). Options: a smaller GGUF "
            f"(Qwen3-0.6B-class is ~0.8GB), stt.model: moonshine/tiny, or run "
            f"one engine only (llm.engine: none / stt.engine: none).")
        if mode == "strict":
            raise RuntimeError(f"RAM preflight failed: {advice}")
        msgs.append(f"WARNING: {advice} Proceeding anyway (ram_check: warn) — "
                    f"if llama-server keeps dying, the service degrades to "
                    f"transcript-only delivery.")
    else:
        msgs.append(f"RAM budget OK: ~{need / _GB:.1f}GB needed of "
                    f"{avail / _GB:.1f}GB available")
    return msgs
