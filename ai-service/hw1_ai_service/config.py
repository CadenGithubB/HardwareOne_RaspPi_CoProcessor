"""Typed configuration: YAML file -> dataclasses with defaults.

Credentials live OUTSIDE the config file (they cross the wire in plaintext
and must not end up in casual copies of the YAML): `credentials_file` names
a mode-600 file whose first line is `<username> <password>`.
"""

from __future__ import annotations

import math
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

import yaml


DEFAULT_SYSTEM_PROMPT = (
    "You are HardwareOne, a local offline assistant for these smart glasses, "
    "replacing the cloud-backed Even AI response path. Do not claim to be Even "
    "AI or an official Even Realities service. You receive text, usually a "
    "possibly imperfect speech transcript, and return plain text for a small "
    "display. You can answer "
    # Translation was removed 2026-08-13. It was an advertised capability the
    # deployed model failed at: asked "how do I say X in Japanese" it replied
    # ~80% in Japanese instead of giving the phrase (7 of 8 measured runs), and
    # that reply then dragged the NEXT unrelated English turn into Japanese in 5
    # of 8. Promising a capability the model cannot deliver is worse than not
    # offering it. Re-add only alongside a model measured to handle it.
    "questions, explain, brainstorm, help with writing, summarize, "
    "and reason from built-in knowledge and recent conversation; that knowledge "
    "may be outdated. Always reply in the same language the user wrote in. "
    "You have no tools or persistent memory. You receive no "
    "camera, image, or raw-audio input and cannot access the internet, live data, "
    "current time or location, files, accounts, device or sensor state, or "
    "perform actions beyond replying. Use information supplied in conversation "
    "without claiming you observed or retrieved it. Mention limitations only "
    "when relevant. Answer directly and naturally, normally in one to three "
    "concise sentences; add detail when requested or needed for correctness. "
    "Avoid Markdown, headings, tables, filler, and long lists. Correct only "
    "obvious transcript errors; if ambiguity would change the answer, ask one "
    "brief question. Never invent current facts, observations, memories, "
    "capabilities, or completed actions."
)


@dataclass
class LinkConfig:
    port: str = "/dev/ttyAMA2"
    baud: int = 2000000
    credentials_file: str = "~/.config/hw1-ai-service/credentials"


@dataclass
class AudioConfig:
    record_seconds: float = 4.0        # fixed window when vad is off (no endpointing)
    # Silence endpointing (device-side VAD). When on, the CM5 issues
    # `micrecord start vad <vad_silence_ms>`: the XIAO auto-stops the recording
    # after that much trailing silence, and the CM5 polls `micrecord` until it
    # stops (or vad_max_seconds elapses) instead of sleeping a fixed window.
    # This is opt-in ON THE DEVICE too — only the STT flow's `start vad`
    # arms it; the glasses/OLED/manual recorders are byte-for-byte unchanged.
    # Firmware without VAD support (pre-reflash) rejects the arg; we detect
    # that and fall back to the fixed window automatically.
    vad: bool = True
    vad_silence_ms: int = 1200         # trailing silence that ends a recording (firmware clamps 200..10000)
    # Drop the recorded trailing silence from the WAV without shortening the
    # detection window above. Measured 38% of every capture is silence, and it
    # costs both UART frames and STT time. Set false to get a byte-exact capture
    # for tools/stt/vad_replay.py, which replays the device's own chunk trace.
    # Firmware without the `trim` token ignores it (the arg parser skips
    # unknown trailing words), so this is safe against an older device.
    vad_trim: bool = True
    vad_max_seconds: float = 15.0      # safety cap while waiting for auto-stop (device also caps at 60s)
    vad_poll_s: float = 0.25           # how often the CM5 asks the device "still recording?"
    # Ask HIGH per fileread: the firmware clamps each reply to its own
    # rawCap (~2.9KB) — requesting 4096 yields max-size chunks and ~30%
    # fewer round trips than the old 2048 (each round trip costs a full
    # XIAO loop lap, which is the real bottleneck).
    chunk_request_bytes: int = 4096
    # Every fetched utterance is saved here for playback/diagnosis ("" = off):
    # distinguishes "mic heard nothing" from "STT failed".
    save_last_path: str = "~/.cache/hw1-ai-service/last-utterance.wav"
    # Audio transfer path: auto (voicefetch, fall back to fileread on older
    # firmware) | voicefetch (P2 binary frames, ~10x faster) | fileread (A0
    # base64, universal but slow).
    transfer: str = "auto"


@dataclass
class SttConfig:
    engine: str = "fake"               # moonshine | zipformer | fake
    model: str = ""                    # engine-specific name or path
    threads_idle: int = 4
    threads_contended: int = 2
    # Live streaming STT over the UART live-pcm-v1 shadow (Gate E). When the
    # daemon can arm the firmware's recorder shadow, wake exchanges transcribe
    # the capture AS IT STREAMS and skip the voicefetch round trip; any live
    # failure falls back to the batch engine above, which stays load-bearing.
    # Enabled only when live_model_dir names the exact downloaded streaming
    # model directory (the one tools/link/run_native_live_stt_gate.sh validated).
    live_enabled: bool = True
    live_model_dir: str = ""
    live_model_arch: str = "medium-streaming"
    live_update_interval_s: float = 1.0
    live_queue_chunks: int = 16
    live_final_timeout_s: float = 2.0
    live_wake_stream_timeout_s: float = 2.0
    # Debug corpus capture (OFF by default): when set, each wake exchange —
    # live AND batch — writes its audio WAV + a JSON sample (worker snapshot
    # for live, plus ask/reply delivery timing for both) to live_debug_dir so
    # the offline replay bench can reproduce STT results and the ask-display
    # timing can be analyzed per exchange. Local-only, purgeable.
    live_debug_capture: bool = False
    live_debug_dir: str = ""  # default: <cache>/hw1-ai-service/live-corpus


@dataclass
class LlmConfig:
    server_bin: str = ""               # empty = assume server already running
    model: str = ""
    host: str = "127.0.0.1"
    port: int = 8080
    extra_args: list[str] = field(default_factory=list)
    max_tokens: int = 250              # output-length cap only; thinking is off
                                       # (client.py) so this is all answer
                                       # tokens, and it does NOT affect TTFT
    # Preserve longer conversational context by default. 6 or 4 remains a
    # reversible TTFT lever if prompt-cache measurements later justify it.
    history_turns: int = 8
    startup_timeout_s: float = 120.0
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    engine: str = "server"             # server | fake
    # Publish this host's GGUF catalog into the firmware's LLM model registry
    # and serve `llm_ask` generations over the UART link, so the device's
    # pickers offer `cm5:<model>` beside their on-device engine. Costs one
    # command per model at link-up and nothing while idle.
    serve_firmware: bool = True
    # Where to look for selectable *.gguf files. Empty = the directory holding
    # `model`, which is the common single-directory layout and needs no config.
    # `model` is always offered even when it lives elsewhere.
    model_dir: str = ""
    # Deliberately SHORT, and bounded by the CM5 presence lease rather than by
    # what a slow command might need. Every one of this bridge's commands is a
    # firmware intrinsic answered ahead of cmd_exec, so a reply is milliseconds;
    # but they share Session's single command lock with the 5s presence
    # heartbeat, and the firmware abandons a live generation the moment that
    # lease goes stale. A long timeout here therefore does not buy patience —
    # it starves the heartbeat and kills the answer it was waiting for.
    # test_cm5_llm.py pins the arithmetic against the real presence constants.
    uart_timeout_s: float = 3.0


@dataclass
class DeliverConfig:
    targets: list[str] = field(default_factory=lambda: ["oled"])  # oled | g2
    allow_oledstart: bool = True
    g2_seconds: int = 60               # firmware honors 1..599 only (validated)
    chunk_bytes: int = 1800            # margin under the 2047B firmware line cap
    chunk_dwell_s: float = 3.0         # pause between chunks (displays REPLACE, not append)
    # The G2 draws the ASK text progressively and the first reply chunk REPLACES
    # it, so a fast answer truncates the question mid-word. CALIBRATED by the
    # 2026-08-11 ask-threshold gate at production streamSpeed=40: 98 chars
    # usually complete by ~2s (~49 cps) but one trial was still cut at 3s and a
    # real wake was cut at 2.73s for 69 chars — paint-START latency jitters
    # ~1s, worse under BLE/UART load. Budget = start_margin + len/cps, held to
    # at least min_dwell so short questions get screen time at all (before the
    # floor, hold_remain was negative on EVERY field wake — the hold never
    # engaged once). cps=0 disables the whole hold (kill switch).
    g2_ask_render_cps: float = 30.0
    # Fixed paint-start allowance covering BLE delivery + render-start jitter
    # (the non-monotonic complete@2s / cut@3s trial pair).
    g2_ask_render_start_margin_s: float = 0.6
    # Minimum on-lens time for the question once the ask is ACKed, even when
    # fully drawn — reading time, not just draw time.
    g2_ask_min_dwell_s: float = 1.2
    # Daemon startup attempts only EvenAI CONFIG field 2 with this value. Hardware
    # A/B/A established that 40 completes native REPLY rendering about 2.3x
    # faster than the captured stock value 80. Zero preserves the current G2
    # runtime state and disables the automatic submission.
    g2_stream_speed: int = 40


@dataclass
class ServiceConfig:
    poll_hz: float = 0.0               # 0 in P0 (no hostjobs yet); 2-5 from P1
    socket_path: str = "~/.local/run/hw1-ai-service.sock"
    ram_check: str = "warn"            # warn | strict | off (startup RAM preflight)


@dataclass
class PowerConfig:
    # Disabled by default until the root-owned helper + sudoers policy are
    # installed.  The daemon still consumes v1 events and reports deterministic
    # failures, so a disabled host never turns a firmware retry into execution.
    enabled: bool = False
    helper_path: str = "/usr/local/libexec/hw1-power-helper"
    use_sudo: bool = True
    initial_profile: str = "auto"       # eco | balanced | performance | auto
    auto_active_profile: str = "performance"
    auto_idle_profile: str = "eco"
    auto_idle_delay_s: float = 30.0
    # Pi 5/CM5 suspend/wake remains platform/image dependent.  Even when true,
    # the helper must positively advertise kernel support before it is invoked.
    allow_suspend: bool = False
    min_sleep_minutes: int = 1
    max_sleep_minutes: int = 1440
    helper_timeout_s: float = 20.0
    uart_timeout_s: float = 20.0
    event_queue_size: int = 32
    request_cache_size: int = 128


@dataclass
class FanConfig:
    # The root-owned hw1-fan-controller is installed separately and owns the
    # actual temperature curve, PWM writes, tach checks, and safety overrides.
    # Keeping this disabled until that service is installed makes a partial
    # deployment fail explicitly instead of pretending a requested mode stuck.
    enabled: bool = False
    socket_path: str = "/run/hw1-fan-controller/control.sock"
    socket_timeout_s: float = 5.0
    uart_timeout_s: float = 20.0
    event_queue_size: int = 16
    request_cache_size: int = 64


@dataclass
class Config:
    link: LinkConfig = field(default_factory=LinkConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    deliver: DeliverConfig = field(default_factory=DeliverConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)
    power: PowerConfig = field(default_factory=PowerConfig)
    fan: FanConfig = field(default_factory=FanConfig)


def _coerce(name: str, current, value):
    """Coerce a YAML value to the dataclass field's type, loudly. Natural
    YAML mistakes (`targets: oled` as a bare string, quoted numbers) must
    die at load time, not mid-exchange (review finding)."""
    try:
        if isinstance(current, bool):
            if isinstance(value, bool):
                return value
            raise ValueError("expected true/false")
        if isinstance(current, int):
            if isinstance(value, bool) or isinstance(value, (dict, list)):
                raise ValueError("expected an integer")
            return int(value)
        if isinstance(current, float):
            if isinstance(value, bool) or isinstance(value, (dict, list)):
                raise ValueError("expected a number")
            return float(value)
        if isinstance(current, str):
            if isinstance(value, (dict, list)):
                raise ValueError("expected a string")
            return "" if value is None else str(value)
        if isinstance(current, list):
            if isinstance(value, str):
                return [value]           # `targets: oled` -> ["oled"]
            if isinstance(value, list):
                return [str(v) for v in value]
            raise ValueError("expected a list")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"config value {name} = {value!r}: {exc}") from None
    return value


def _merge(dc, data: dict):
    for key, value in (data or {}).items():
        if not hasattr(dc, key):
            raise ValueError(f"unknown config key: {type(dc).__name__}.{key}")
        current = getattr(dc, key)
        if hasattr(current, "__dataclass_fields__"):
            _merge(current, value)
        else:
            setattr(dc, key, _coerce(f"{type(dc).__name__}.{key}", current, value))


def _validate(cfg: "Config") -> None:
    d = cfg.deliver
    if not (1 <= d.g2_seconds <= 599):
        raise ValueError(
            f"deliver.g2_seconds = {d.g2_seconds}: firmware honors 1..599 only "
            f"(>=600 is silently treated as display text)")
    if d.chunk_bytes < 16:
        raise ValueError(f"deliver.chunk_bytes = {d.chunk_bytes}: minimum 16")
    if d.chunk_dwell_s < 0:
        raise ValueError("deliver.chunk_dwell_s must be >= 0")
    if d.g2_stream_speed not in (0, 40, 80):
        raise ValueError(
            "deliver.g2_stream_speed must be 0 (preserve), 40, or 80")
    for target in d.targets:
        if target not in ("oled", "g2"):
            raise ValueError(f"deliver.targets: unknown target {target!r} (oled|g2)")
    if cfg.audio.record_seconds <= 0:
        raise ValueError("audio.record_seconds must be > 0")
    if not (200 <= cfg.audio.vad_silence_ms <= 10000):
        raise ValueError(
            f"audio.vad_silence_ms = {cfg.audio.vad_silence_ms}: firmware honors "
            f"200..10000 (out of range is silently reset to 1200)")
    if cfg.audio.vad_max_seconds <= 0:
        raise ValueError("audio.vad_max_seconds must be > 0")
    if cfg.audio.vad_poll_s <= 0:
        raise ValueError("audio.vad_poll_s must be > 0")
    if cfg.audio.transfer not in ("auto", "voicefetch", "fileread"):
        raise ValueError(f"audio.transfer = {cfg.audio.transfer!r} "
                         f"(auto|voicefetch|fileread)")
    if cfg.stt.engine not in ("moonshine", "zipformer", "fake", "none"):
        raise ValueError(
            f"stt.engine = {cfg.stt.engine!r} (moonshine|zipformer|fake|none)")
    s = cfg.stt
    if s.live_model_arch not in (
            "tiny-streaming", "small-streaming", "medium-streaming"):
        raise ValueError(
            f"stt.live_model_arch = {s.live_model_arch!r} "
            f"(tiny-streaming|small-streaming|medium-streaming)")
    if s.live_update_interval_s <= 0:
        raise ValueError("stt.live_update_interval_s must be > 0")
    if not (4 <= s.live_queue_chunks <= 256):
        raise ValueError("stt.live_queue_chunks must be in 4..256")
    if s.live_final_timeout_s <= 0:
        raise ValueError("stt.live_final_timeout_s must be > 0")
    if s.live_wake_stream_timeout_s <= 0:
        raise ValueError("stt.live_wake_stream_timeout_s must be > 0")
    if cfg.llm.engine not in ("server", "fake", "none"):
        raise ValueError(f"llm.engine = {cfg.llm.engine!r} (server|fake|none)")
    # Ceiling derived from cm5_presence: worst case is one command timeout plus
    # its idempotent replay (2x) holding the shared Session lock, and the 5s
    # heartbeat must still land inside the 15s normal lease — (15 - 5) / 2.
    # Above this, one stuck push starves the heartbeat and the firmware
    # abandons the generation with "session epoch mismatch".
    if (not math.isfinite(cfg.llm.uart_timeout_s) or
            not 0 < cfg.llm.uart_timeout_s <= 5.0):
        raise ValueError(
            "llm.uart_timeout_s must be in (0, 5]: two of them plus the 5s CM5 "
            "presence heartbeat have to fit inside the firmware's 15s lease")
    if (cfg.stt.engine == "none" and cfg.llm.engine == "none" and
            not cfg.power.enabled and not cfg.fan.enabled):
        raise ValueError("stt.engine and llm.engine are both 'none' and "
                         "power.enabled/fan.enabled are false — nothing to run")
    if cfg.service.ram_check not in ("warn", "strict", "off"):
        raise ValueError(f"service.ram_check = {cfg.service.ram_check!r} "
                         f"(warn|strict|off)")
    p = cfg.power
    profiles = ("eco", "balanced", "performance", "auto")
    if p.initial_profile not in profiles:
        raise ValueError(f"power.initial_profile = {p.initial_profile!r} "
                         f"(eco|balanced|performance|auto)")
    concrete_profiles = ("eco", "balanced", "performance")
    if p.auto_active_profile not in concrete_profiles:
        raise ValueError(f"power.auto_active_profile = {p.auto_active_profile!r} "
                         f"(eco|balanced|performance)")
    if p.auto_idle_profile not in concrete_profiles:
        raise ValueError(f"power.auto_idle_profile = {p.auto_idle_profile!r} "
                         f"(eco|balanced|performance)")
    if p.auto_idle_delay_s < 0:
        raise ValueError("power.auto_idle_delay_s must be >= 0")
    helper_path = Path(os.path.expanduser(p.helper_path))
    if not helper_path.is_absolute():
        raise ValueError("power.helper_path must be absolute")
    if not (1 <= p.min_sleep_minutes <= p.max_sleep_minutes <= 1440):
        raise ValueError("power sleep range must satisfy "
                         "1 <= min_sleep_minutes <= max_sleep_minutes <= 1440")
    if p.helper_timeout_s <= 0:
        raise ValueError("power.helper_timeout_s must be > 0")
    if p.uart_timeout_s <= 0:
        raise ValueError("power.uart_timeout_s must be > 0")
    if not (1 <= p.event_queue_size <= 1024):
        raise ValueError("power.event_queue_size must be in 1..1024")
    if not (p.event_queue_size <= p.request_cache_size <= 4096):
        raise ValueError("power.request_cache_size must be between "
                         "event_queue_size and 4096")
    f = cfg.fan
    expected_fan_socket = "/run/hw1-fan-controller/control.sock"
    if f.socket_path != expected_fan_socket:
        raise ValueError(
            f"fan.socket_path must be {expected_fan_socket}")
    if (not math.isfinite(f.socket_timeout_s) or
            not 0 < f.socket_timeout_s <= 30):
        raise ValueError("fan.socket_timeout_s must be in (0, 30]")
    if (not math.isfinite(f.uart_timeout_s) or
            not 0 < f.uart_timeout_s <= 60):
        raise ValueError("fan.uart_timeout_s must be in (0, 60]")
    if not (1 <= f.event_queue_size <= 1024):
        raise ValueError("fan.event_queue_size must be in 1..1024")
    if not (f.event_queue_size <= f.request_cache_size <= 4096):
        raise ValueError("fan.request_cache_size must be between "
                         "event_queue_size and 4096")


def load(path: str | Path | None) -> Config:
    cfg = Config()
    if path:
        with open(path) as fh:
            data = yaml.safe_load(fh) or {}
        for section, value in data.items():
            if not hasattr(cfg, section):
                raise ValueError(f"unknown config section: {section}")
            _merge(getattr(cfg, section), value)
    _validate(cfg)
    return cfg


def read_credentials(path_str: str) -> tuple[str, str]:
    path = Path(os.path.expanduser(path_str))
    if not path.exists():
        raise FileNotFoundError(
            f"credentials file not found: {path} "
            f"(create it with one line: '<username> <password>', chmod 600)")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(
            f"credentials file {path} is group/world readable (mode {oct(mode)}); "
            f"chmod 600 it")
    first = path.read_text().strip().splitlines()[0]
    parts = first.split(None, 1)
    if len(parts) != 2:
        raise ValueError(f"credentials file {path}: expected '<username> <password>'")
    return parts[0], parts[1]
