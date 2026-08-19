#!/usr/bin/env python3
"""No-camera diagnostics for the native G2 EvenAI display path.

Run this with the same Python environment as ``hw1-ai-service`` after stopping
the daemon so this process has exclusive ownership of the ESP32 UART.  The
probe does not modify the service pipeline or firmware.  An explicit CONFIG
speed changes the glasses' runtime value, and ``render-ab`` uses the ESP32
system logger (whose normal behavior remembers its most recent log path).

The human-observation tests use a wearer complete/cut report.  The render A/B
also records the glasses' protocol-level STREAM_COMPLETE event in a ESP32 log,
so it does not require a camera or a screen recording.

All built-in native-session actions resolve the active firmware-issued
exchange ID first and use the tagged EvenAI mutation verbs.  The generic
``cmd`` action remains an intentionally raw firmware-command passthrough.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import dataclass
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    # Runtime imports stay lazy inside run() so --help works without the Pi
    # runtime extras; this block only resolves the type annotations.
    from hw1_ai_service.link.session import Session

CONFIGS = {
    "40": "g2probe 07 10 6A06080010282000",
    "80": "g2probe 07 10 6A06080010502000",
    "160": "g2probe 07 10 6A07080010A0012000",
}

SEQUENCES = {
    "single": [
        "How quickly can these glasses display this complete recognized "
        "question before showing the answer?",
    ],
    "short": [
        "one",
        "one two",
        "one two three four",
        "one two free four",
        "one two",
        "one two",
    ],
    "natural": [
        "what",
        "what is the tallest mountain",
        "what is the tallest mountain in the world",
        "what is the tallest mountain in the world and how high",
        "what is the tallest mountain in the world and how high is it above sea level",
        "what is the tallest mountain in the world and how high is it above sea level "
        "in both feet and meters",
    ],
}

DEFAULT_THRESHOLD_QUESTION = SEQUENCES["single"][0]

# Exactly 180 printable ASCII characters.  It fits one protocol message, and
# is long enough for the direct-vs-progressive renderer difference to produce
# a useful STREAM_COMPLETE interval.
RENDER_TEXT_180 = (
    "Fixed display benchmark text measures how quickly the glasses render a complete answer. "
    "Every trial uses exactly the same characters so one-shot and streamed paths can be compared!"
)
assert len(RENDER_TEXT_180) == 180

_ACTIVE_LOG_RE = re.compile(r"^\s*File:\s*(\S.*?)\s*$", re.MULTILINE)
_PROTOCOL_MARKERS = (
    "EvenAI CONFIG",
    "EvenAI COMM_RSP",
    "EvenAI ASK",
    "EvenAI ANALYSE",
    "EvenAI REPLY",
    "EvenAI CTRL status=EXIT",
    "STREAM_COMPLETE",
    "temple plugin silent",
    "DISPLAY_AUTO_REFLASH",
    "direct display mode",
)

_COMMAND_MAGIC_RE = re.compile(r"\bmagic=(\d+)\b")
_EVENAI_STATUS_RE = re.compile(
    r"\bEvenAI session:\s*(?P<state>active|idle)\s+id=(?P<id>\S+)",
    re.IGNORECASE,
)
_EXCHANGE_ID_RE = re.compile(r"^[0-9A-Fa-f]{16}$")
_CONFIG_RE = re.compile(r"\[(?P<ms>\d+)\].*EvenAI CONFIG magic=(?P<magic>\d+)")
_CONFIG_BODY_RE = re.compile(r"body f13 bytes\(\d+\)=\[(?P<body>[0-9A-Fa-f ]+)\]")
_COMM_RSP_RE = re.compile(r"\[(?P<ms>\d+)\].*EvenAI COMM_RSP magic=(?P<magic>\d+)")
_ASK_RE = re.compile(r"\[(?P<ms>\d+)\].*EvenAI ASK magic=(?P<magic>\d+)")
_ANALYSE_RE = re.compile(r"\[(?P<ms>\d+)\].*EvenAI ANALYSE magic=(?P<magic>\d+)")
_REPLY_RE = re.compile(r"\[(?P<ms>\d+)\].*EvenAI REPLY magic=(?P<magic>\d+)")
_COMPLETE_RE = re.compile(r"\[(?P<ms>\d+)\].*STREAM_COMPLETE")
_EXIT_RE = re.compile(r"\[(?P<ms>\d+)\].*EvenAI CTRL status=EXIT")
_SID07_TX_RE = re.compile(
    r"\[(?P<ms>\d+)\].*TX env total=(?P<total>\d+).*"
    r"sid=0x07 flag=0x20"
)
_CMD_REPLY_RE = re.compile(
    r"\[(?P<ms>\d+)\].*\[CMD\]\s+[^:]+:\s+"
    r"g2evenai (?:reply |replyid [0-9A-Fa-f]{16} )"
    r"(?P<text>.*?) -> OK\s*$"
)
_PLUGIN_SILENT_RE = re.compile(
    r"\b(?P<side>LEFT|RIGHT) temple plugin silent\b", re.IGNORECASE
)
_PLUGIN_ALIVE_RE = re.compile(
    r"\b(?P<side>LEFT|RIGHT) temple plugin alive again\b", re.IGNORECASE
)
_STATUS_SIDE_RE = {
    "L": re.compile(r'"L"\s*:\s*"(?P<state>up|dead|down)"', re.IGNORECASE),
    "R": re.compile(r'"R"\s*:\s*"(?P<state>up|dead|down)"', re.IGNORECASE),
}


@dataclass(frozen=True)
class SpeedRequest:
    speed: int
    magic: int
    reply_text: str | None = None


@dataclass(frozen=True)
class SpeedEvidence:
    request: SpeedRequest
    echoed_speed: int | None
    logged_reply_bytes: int | None
    reply_tx_ms: int | None
    reply_tx_total: int | None
    reply_response_ms: int | None
    completion_ms: int | None
    tx_to_completion_ms: int | None
    response_to_completion_ms: int | None
    issues: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.issues

    @property
    def clean(self) -> bool:
        return self.valid and not self.warnings


@dataclass(frozen=True)
class SpeedMatrixEvidence:
    trials: tuple[SpeedEvidence, ...]
    issues: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.issues and all(trial.valid for trial in self.trials)

    @property
    def clean(self) -> bool:
        return self.valid and all(trial.clean for trial in self.trials)


@dataclass(frozen=True)
class RenderEvidence:
    mode: str
    reply_responses: int
    completions: int
    final_response_to_completion_ms: int | None
    issues: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class RenderMatrixEvidence:
    trials: tuple[RenderEvidence, ...]
    issues: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.issues and all(trial.valid for trial in self.trials)


def ts() -> str:
    """Wall-clock timestamp for the runner transcript."""
    return time.strftime("%H:%M:%S") + f".{time.time_ns() // 1_000_000 % 1000:03d}"


def validate_text(text: str, *, label: str, max_bytes: int = 220) -> str:
    """Reject text that cannot safely ride one firmware command/message."""
    if not text.strip():
        raise ValueError(f"{label} must not be empty")
    if "\n" in text or "\r" in text:
        raise ValueError(f"{label} must be a single line")
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError(
            f"{label} is {len(encoded)} UTF-8 bytes; keep it at or below {max_bytes}"
        )
    return text


def parse_delays(value: str) -> tuple[int, ...]:
    """argparse converter for comma-separated positive millisecond delays."""
    try:
        delays = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("delays must be comma-separated integers") from exc
    if not delays or any(delay < 0 for delay in delays):
        raise argparse.ArgumentTypeError("provide one or more non-negative delays")
    return delays


def parse_speeds(value: str) -> tuple[int, ...]:
    """argparse converter for a comma-separated nonzero CONFIG speed matrix."""
    try:
        speeds = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("speeds must be comma-separated integers") from exc
    if not speeds or any(speed <= 0 or speed > 1000 for speed in speeds):
        raise argparse.ArgumentTypeError("provide speeds from 1 through 1000; zero is unsafe")
    return speeds


def _status_is_connected_both_up(text: str) -> bool:
    """Require the terminal connection state, not merely two live links."""
    lowered = text.lower()
    return (
        re.search(r"\bstate=connected\b", lowered) is not None
        and re.search(r"\bl=up\b", lowered) is not None
        and re.search(r"\br=up\b", lowered) is not None
    )


def parse_active_exchange_id(status_text: str) -> str | None:
    """Parse the exchange ID from one authoritative ``g2evenai status``.

    ``None`` means the firmware explicitly reported an idle session.  Any
    unrecognized or malformed active status raises instead of falling back to
    the old untagged mutation verbs.
    """
    match = _EVENAI_STATUS_RE.search(status_text)
    if not match:
        raise RuntimeError(
            "g2evenai status did not contain the expected EvenAI session state"
        )
    state = match.group("state").lower()
    token = match.group("id")
    if state == "idle":
        if token != "-":
            raise RuntimeError("idle g2evenai status reported an unexpected exchange ID")
        return None
    if not _EXCHANGE_ID_RE.fullmatch(token):
        raise RuntimeError(
            "active g2evenai status did not report a strict 16-hex exchange ID"
        )
    exchange_id = token.lower()
    if int(exchange_id[:8], 16) == 0 or int(exchange_id[8:], 16) == 0:
        raise RuntimeError("active EvenAI exchange ID has a zero nonce or counter")
    return exchange_id


async def active_exchange_id(session: Session) -> str | None:
    """Query and parse the currently active native EvenAI exchange."""
    status = await send(session, "g2evenai status")
    return parse_active_exchange_id(status.text)


async def _discard_exchange_capture(session: Session, exchange_id: str) -> bool:
    """Best-effort exact-owner cleanup that cannot stop a newer recording."""
    try:
        reply = await send(
            session,
            f"micrecord stopid {exchange_id} discard",
            required=False,
        )
    except Exception as exc:
        print(
            "WARNING: exchange-owned microphone cleanup could not be completed "
            f"for {exchange_id}: {exc}",
            flush=True,
        )
        return False
    if not reply.ok:
        print(
            "WARNING: exchange-owned microphone cleanup was rejected for "
            f"{exchange_id}: {reply.text}",
            flush=True,
        )
    return bool(reply.ok)


async def exit_active_evenai(
    session: Session,
    *,
    expected_id: str | None = None,
    strict: bool = True,
) -> bool:
    """Exit the active exchange without allowing a stale cleanup to hit a new one."""
    cleanup_id = expected_id
    try:
        exchange_id = await active_exchange_id(session)
        if cleanup_id is None:
            cleanup_id = exchange_id
        if exchange_id is None:
            return False
        if expected_id is not None and exchange_id != expected_id:
            print(
                "WARNING: cleanup skipped: active EvenAI exchange changed from "
                f"{expected_id} to {exchange_id}",
                flush=True,
            )
            return False
        reply = await send(
            session, f"g2evenai exitid {exchange_id}", required=False
        )
        return bool(reply.ok)
    except Exception as exc:
        if strict:
            raise
        print(
            f"WARNING: tagged EvenAI cleanup could not be completed: {exc}",
            flush=True,
        )
        return False
    finally:
        if cleanup_id is not None:
            await _discard_exchange_capture(session, cleanup_id)


async def send(
    session: Session,
    line: str,
    *,
    expect: str = "status",
    required: bool = True,
):
    """Send one command and print its Pi-to-ESP32 command interval."""
    print(f"[{ts()}] > {line}", flush=True)
    reply = await session.command(line, expect=expect, timeout=20, replay=False)
    print(f"[{ts()}] < {reply.text}", flush=True)
    if required and not reply.ok:
        raise RuntimeError(f"command failed: {line}: {reply.text}")
    return reply


async def reconnect_g2(session: Session) -> None:
    await exit_active_evenai(session)
    await asyncio.sleep(0.5)
    await send(session, "closeg2")
    await asyncio.sleep(1.0)
    reply = await send(session, "openg2 saved", required=False)
    if not reply.ok:
        await send(session, "openg2")
    for _ in range(30):
        status = await send(session, "g2status")
        if _status_is_connected_both_up(status.text):
            return
        await asyncio.sleep(1.0)
    raise RuntimeError(
        "G2 did not reach state=connected with both temples up within 30 seconds"
    )


async def preflight(session: Session) -> None:
    await send(session, "status json", expect="json", required=False)
    await send(session, "uartlink status", expect="auto", required=False)
    await send(session, "g2info", expect="auto", required=False)
    await send(session, "g2status", required=False)
    probe = await send(session, "g2probe 07 9", required=False)
    evenai = await send(session, "g2evenai status", required=False)
    capabilities = await send(session, "g2evenai capabilities", required=False)
    if not probe.ok or not evenai.ok or not capabilities.ok:
        raise RuntimeError("required firmware commands are missing; inspect the replies above")
    parse_active_exchange_id(evenai.text)
    if "exchange-id-v1" not in capabilities.text:
        raise RuntimeError("firmware does not advertise tagged EvenAI exchange IDs")


async def wait_for_native_wake(session: Session, *, attempt: str = "") -> str:
    """Ask the wearer to wake EvenAI, then verify the firmware saw the wake."""
    suffix = f" for {attempt}" if attempt else ""
    await asyncio.to_thread(
        input,
        f"Say Hey Even{suffix}, wait for the native listening popup, then press Enter: ",
    )
    exchange_id = await active_exchange_id(session)
    if exchange_id is None:
        raise RuntimeError("EvenAI is not active; no ASK was sent")
    return exchange_id


async def require_healthy_temples(session: Session) -> None:
    status = await send(session, "g2status")
    if not _status_is_connected_both_up(status.text):
        raise RuntimeError(
            "G2 must be state=connected with both temples up before each measured "
            "trial; reconnect and rerun"
        )


async def maybe_set_speed(session: Session, speed: str) -> None:
    if speed != "none":
        await send(session, CONFIGS[speed])
        await asyncio.sleep(1.0)


async def trial(session: Session, args: argparse.Namespace) -> None:
    """Run the original partial-ASK trial with an ESP32-OK-based delay."""
    await reconnect_g2(session)
    await maybe_set_speed(session, args.speed)
    exchange_id: str | None = None
    try:
        exchange_id = await wait_for_native_wake(session)
        for number in (3, 2, 1):
            print(f"Starting in {number}...", flush=True)
            await asyncio.sleep(1.0)

        loop = asyncio.get_running_loop()
        cadence = args.cadence_ms / 1000.0
        start = loop.time()
        final_ask_ok = start
        for index, text in enumerate(SEQUENCES[args.sequence]):
            target = start + index * cadence
            await asyncio.sleep(max(0.0, target - loop.time()))
            ask_start = loop.time()
            late_ms = max(0.0, (ask_start - target) * 1000.0)
            print(
                f"ASK {index + 1} start +{ask_start-start:.3f}s late={late_ms:.1f}ms",
                flush=True,
            )
            await send(session, f"g2evenai askid {exchange_id} {text}")
            final_ask_ok = loop.time()
            print(
                f"ASK {index + 1} ESP32 command OK +{final_ask_ok-start:.3f}s",
                flush=True,
            )

        # This anchor is later than command start but is still only the ESP32's
        # command result.  It is not a G2 receipt or optical-render event; the
        # protocol log must be used to recover the native ASK echo separately.
        target = final_ask_ok + args.reply_delay_ms / 1000.0
        await asyncio.sleep(max(0.0, target - loop.time()))
        actual_ms = (loop.time() - final_ask_ok) * 1000.0
        print(
            f"REPLY command start {actual_ms:.1f}ms after final ASK ESP32 command OK",
            flush=True,
        )
        await send(session, f"g2evenai replyid {exchange_id} Probe complete")
        await asyncio.sleep(3.0)
    finally:
        await exit_active_evenai(
            session, expected_id=exchange_id, strict=False
        )


async def ask_threshold(session: Session, args: argparse.Namespace) -> None:
    """Find a no-camera ASK-to-REPLY delay that does not cut the question."""
    question = validate_text(args.question, label="question")
    await reconnect_g2(session)
    await maybe_set_speed(session, args.speed)

    results: list[tuple[int, str, float]] = []
    loop = asyncio.get_running_loop()
    stopped = False
    exchange_id: str | None = None
    try:
        for index, delay_ms in enumerate(args.delays_ms, start=1):
            if index > 1:
                await exit_active_evenai(session, expected_id=exchange_id)
                await asyncio.sleep(0.5)
            exchange_id = await wait_for_native_wake(
                session, attempt=f"{delay_ms} ms trial"
            )

            print(
                f"TRIAL {index}/{len(args.delays_ms)}: {len(question)} chars, "
                f"reply after ASK ESP32 command OK + {delay_ms} ms",
                flush=True,
            )
            await send(session, f"g2evenai askid {exchange_id} {question}")
            ask_ok = loop.time()
            target = ask_ok + delay_ms / 1000.0
            await asyncio.sleep(max(0.0, target - loop.time()))
            actual_ms = (loop.time() - ask_ok) * 1000.0
            print(
                f"REPLY start {actual_ms:.1f}ms after ASK ESP32 command OK",
                flush=True,
            )
            await send(
                session, f"g2evenai replyid {exchange_id} {args.reply_text}"
            )

            while True:
                verdict = (
                    await asyncio.to_thread(
                        input,
                        "Was the complete question visible before the answer replaced it? "
                        "[c]omplete / [x]cut / [q]uit: ",
                    )
                ).strip().lower()
                if verdict in {"c", "complete"}:
                    results.append((delay_ms, "complete", actual_ms))
                    break
                if verdict in {"x", "cut"}:
                    results.append((delay_ms, "cut", actual_ms))
                    break
                if verdict in {"q", "quit"}:
                    print("Threshold trial stopped by wearer.", flush=True)
                    stopped = True
                    break
                print("Enter c, x, or q.", flush=True)
            if stopped:
                break
    finally:
        await exit_active_evenai(
            session, expected_id=exchange_id, strict=False
        )
    _print_threshold_results(question, results)


def _print_threshold_results(
    question: str,
    results: Sequence[tuple[int, str, float]],
) -> None:
    print(
        "\nASK threshold results "
        "(all delays start at ASK ESP32 command OK, not G2 receipt):",
        flush=True,
    )
    print(f"  question: {len(question)} chars / {len(question.encode('utf-8'))} bytes")
    for requested, verdict, actual in results:
        print(f"  {requested:4d} ms requested  {actual:7.1f} ms actual  {verdict}")
    complete = [requested for requested, verdict, _ in results if verdict == "complete"]
    if complete:
        print(
            f"  lowest observed complete delay: {min(complete)} ms "
            "(repeat near this boundary before using it as a production margin)"
        )
    else:
        print("  no complete delay was observed")


def default_device_log_path() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"/logging_captures/system/evenai-render-ab-{stamp}.log"


def default_speed_log_path() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"/logging_captures/system/evenai-speed-ab-{stamp}.log"


async def start_protocol_log(session: Session, requested_path: str) -> tuple[str, bool]:
    """Start a fresh log whose final close gives a complete evidence file."""
    status = await send(session, "log status", expect="auto")
    level = await send(session, "loglevel", expect="auto")
    if "debug (3)" not in level.text.lower():
        raise RuntimeError(
            "ESP32 loglevel is not debug (3), so STREAM_COMPLETE evidence may be "
            "suppressed; set and later restore loglevel explicitly before this test"
        )
    active = "System logging ACTIVE" in status.text
    if active:
        match = _ACTIVE_LOG_RE.search(status.text)
        path = match.group(1) if match else "(unknown path)"
        raise RuntimeError(
            f"ESP32 system logging is already active at {path}. Stop or finish that "
            "capture before render-ab. Reusing it would mix old trials into this A/B, "
            "and fetching an open log can miss the logger's unflushed tail."
        )

    path = requested_path
    validate_text(path, label="device log path", max_bytes=240)
    # Do not pass tags= or flags=: both are persistent logging preferences.
    # The logger still remembers this path as part of its normal start logic.
    await send(session, f"log start {json.dumps(path)}", expect="auto")
    try:
        # log start may restore a persisted flag mask, so enable the two G2
        # flags only after the new log has opened.
        await send(session, "debugg2 1 temp", expect="auto")
        await send(session, "debugg2protocol 1 temp", expect="auto")
    except BaseException:
        await stop_protocol_log(session, True)
        raise
    return path, True


async def stop_protocol_log(session: Session, owns_log: bool) -> bool:
    """Undo probe-owned verbosity and close/flush the evidence log."""
    if not owns_log:
        return True

    # These are the two runtime bits this probe explicitly enables.  Current
    # firmware has no read/restore command for the full live mask, and log start
    # can itself restore a persisted log mask.  Turn our loud bits off so the
    # normal daemon does not inherit protocol tracing after the probe.
    for line in ("debugg2protocol 0 temp", "debugg2 0 temp"):
        try:
            await send(session, line, expect="auto", required=False)
        except Exception as exc:
            print(f"WARNING: debug cleanup command {line!r} failed: {exc}", flush=True)
    try:
        stopped = await send(session, "log stop", expect="auto", required=False)
    except Exception as exc:
        print(f"ERROR: ESP32 log stop raised {exc!r}", flush=True)
        return False
    if not stopped.ok:
        print(
            "ERROR: ESP32 log stop failed. The log may still be open or have an "
            "unflushed tail; it must not be analyzed as complete evidence.",
            flush=True,
        )
        return False
    print(
        "Debug cleanup: disabled the probe-owned G2/protocol runtime bits. "
        "Firmware log start may also have restored other persisted log-mask "
        "bits; use debugflags if unrelated verbose logging remains.",
        flush=True,
    )
    return True


async def fetch_file(session: Session, device_path: str, output: Path) -> int:
    offset = 0
    chunks: list[bytes] = []
    while True:
        line = f"fileread {json.dumps(device_path)} {offset} 2800 b64"
        reply = await session.command(line, expect="json", timeout=20, replay=False)
        envelope = reply.json
        if not isinstance(envelope, dict) or not envelope.get("success"):
            raise RuntimeError(f"fileread failed: {reply.text}")
        chunks.append(base64.b64decode(envelope.get("data", "")))
        chunk_len = int(envelope.get("len", 0))
        if chunk_len <= 0 and not envelope.get("eof"):
            raise RuntimeError("fileread made no progress before EOF")
        offset += chunk_len
        if envelope.get("eof"):
            break
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"".join(chunks))
    print(f"Fetched {offset} bytes from {device_path} to {output}", flush=True)
    return offset


def print_protocol_markers(output: Path) -> dict[str, int]:
    """Print a small evidence excerpt; retain the full fetched log on disk."""
    print("\nProtocol markers from fetched ESP32 log:", flush=True)
    found = 0
    counts = {marker: 0 for marker in _PROTOCOL_MARKERS}
    with output.expanduser().open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            matched = [marker for marker in _PROTOCOL_MARKERS if marker in line]
            for marker in matched:
                counts[marker] += 1
            if matched:
                print(line.rstrip())
                found += 1
    if not found:
        print("  none found; inspect the full log and verify debugG2 was active")
    return counts


def _decode_varint(data: bytes, start: int) -> tuple[int, int]:
    value = 0
    shift = 0
    offset = start
    while offset < len(data) and shift <= 63:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("truncated protobuf varint")


def _config_speed(body: bytes) -> int | None:
    """Read field 2 (streamSpeed) from an echoed nested CONFIG body."""
    offset = 0
    while offset < len(body):
        key, offset = _decode_varint(body, offset)
        field = key >> 3
        wire = key & 0x07
        if wire == 0:
            value, offset = _decode_varint(body, offset)
            if field == 2:
                return value
        elif wire == 2:
            size, offset = _decode_varint(body, offset)
            offset += size
            if offset > len(body):
                raise ValueError("truncated length-delimited CONFIG field")
        else:
            raise ValueError(f"unsupported CONFIG protobuf wire type {wire}")
    return None


def _plugin_health_timeline(lines: Sequence[str]) -> list[dict[str, str]]:
    """Return the last observed plugin state immediately before each log line.

    BLE link state and plugin liveness are different in the current firmware:
    ``g2status`` can still say ``L=up`` after the plugin watchdog has published
    ``L=dead``.  The evidence analyzer therefore carries the explicit watchdog
    state forward until an alive/up observation clears it.
    """
    state = {"L": "unknown", "R": "unknown"}
    before: list[dict[str, str]] = []
    for line in lines:
        before.append(dict(state))

        if match := _PLUGIN_SILENT_RE.search(line):
            state[match.group("side")[0].upper()] = "dead"
        if match := _PLUGIN_ALIVE_RE.search(line):
            state[match.group("side")[0].upper()] = "up"

        if "g2-status-TX" in line:
            for side, pattern in _STATUS_SIDE_RE.items():
                if match := pattern.search(line):
                    state[side] = match.group("state").lower()
    return before


def _condition_health_warnings(
    lines: Sequence[str],
    before: Sequence[dict[str, str]],
    start: int,
    boundary: int,
) -> tuple[str, ...]:
    affected: set[str] = {
        side for side, state in before[start].items() if state in {"dead", "down"}
    }
    for line in lines[start:boundary]:
        if match := _PLUGIN_SILENT_RE.search(line):
            affected.add(match.group("side")[0].upper())
        if "g2-status-TX" in line:
            for side, pattern in _STATUS_SIDE_RE.items():
                if (match := pattern.search(line)) and match.group("state").lower() in {
                    "dead",
                    "down",
                }:
                    affected.add(side)
    names = {"L": "LEFT", "R": "RIGHT"}
    return tuple(
        f"{names[side]} temple plugin was silent/dead during this condition"
        for side in sorted(affected)
    )


def analyze_speed_matrix(
    output: Path, requests: Sequence[SpeedRequest]
) -> SpeedMatrixEvidence:
    """Pair each requested CONFIG with its own reply/completion session.

    Aggregate marker counts are unsafe here: a late completion can otherwise be
    attributed to the next speed.  Each trial is bounded by its first native
    EXIT response (or, if that is missing, the next CONFIG) and matched to the
    CONFIG command's magic value.
    """
    lines = output.expanduser().read_text(encoding="utf-8", errors="replace").splitlines()
    configs: list[tuple[int, int, int]] = []
    for index, line in enumerate(lines):
        if match := _CONFIG_RE.search(line):
            configs.append((index, int(match.group("magic")), int(match.group("ms"))))
    health_before = _plugin_health_timeline(lines)
    supports_logged_reply_text = any(_CMD_REPLY_RE.search(line) for line in lines)

    requested_magics = {request.magic for request in requests}
    matrix_issues: list[str] = []
    if len(requested_magics) != len(requests):
        matrix_issues.append("CONFIG request magic values were not unique")
    unexpected = [(magic, index) for index, magic, _ in configs if magic not in requested_magics]
    if unexpected:
        matrix_issues.append(
            "unexpected CONFIG echo(es): "
            + ", ".join(f"magic={magic}@line{index + 1}" for magic, index in unexpected)
        )

    trials: list[SpeedEvidence] = []
    prior_config_index = -1
    for request in requests:
        issues: list[str] = []
        warnings: list[str] = []
        matches = [(index, ms) for index, magic, ms in configs if magic == request.magic]
        if len(matches) != 1:
            issues.append(
                f"expected exactly one CONFIG echo for magic={request.magic}; got {len(matches)}"
            )
            if any(
                int(match.group("magic")) == request.magic
                for line in lines
                if (match := _COMM_RSP_RE.search(line))
            ):
                issues.append(f"COMM_RSP rejected/answered CONFIG magic={request.magic}")
            trials.append(
                SpeedEvidence(
                    request=request,
                    echoed_speed=None,
                    logged_reply_bytes=None,
                    reply_tx_ms=None,
                    reply_tx_total=None,
                    reply_response_ms=None,
                    completion_ms=None,
                    tx_to_completion_ms=None,
                    response_to_completion_ms=None,
                    issues=tuple(issues),
                )
            )
            continue

        config_index, _config_ms = matches[0]
        if config_index <= prior_config_index:
            issues.append("CONFIG echoes were not in request order")
        prior_config_index = config_index

        next_configs = [index for index, _magic, _ms in configs if index > config_index]
        next_config = min(next_configs, default=len(lines))
        asks = [
            index
            for index in range(config_index + 1, next_config)
            if _ASK_RE.search(lines[index])
        ]
        if len(asks) != 1:
            issues.append(f"expected one ASK response before the next CONFIG; got {len(asks)}")
        ask_index = asks[0] if asks else config_index
        exits = [
            index
            for index in range(ask_index + 1, next_config)
            if _EXIT_RE.search(lines[index])
        ]
        if not exits:
            issues.append("no native EXIT boundary before the next CONFIG/end of log")
            boundary = next_config
        else:
            boundary = exits[0]
        warnings.extend(
            _condition_health_warnings(
                lines, health_before, config_index, boundary
            )
        )

        echoed_speed: int | None = None
        for index in range(config_index + 1, min(config_index + 8, next_config)):
            if body_match := _CONFIG_BODY_RE.search(lines[index]):
                try:
                    body = bytes.fromhex(body_match.group("body"))
                    echoed_speed = _config_speed(body)
                except ValueError as exc:
                    issues.append(f"could not decode echoed CONFIG body: {exc}")
                break
        if echoed_speed is None:
            issues.append("CONFIG echo had no decodable field-2 streamSpeed")
        elif echoed_speed != request.speed:
            issues.append(
                f"CONFIG echoed streamSpeed={echoed_speed}, expected {request.speed}"
            )

        if any(
            int(match.group("magic")) == request.magic
            for line in lines
            if (match := _COMM_RSP_RE.search(line))
        ):
            issues.append(f"COMM_RSP rejected/answered CONFIG magic={request.magic}")

        analyses = [
            (index, int(match.group("ms")))
            for index in range(ask_index + 1, boundary)
            if (match := _ANALYSE_RE.search(lines[index]))
        ]
        replies = [
            (index, int(match.group("ms")))
            for index in range(ask_index + 1, boundary)
            if (match := _REPLY_RE.search(lines[index]))
        ]
        completions = [
            (index, int(match.group("ms")))
            for index in range(ask_index + 1, boundary)
            if (match := _COMPLETE_RE.search(lines[index]))
        ]
        if asks and any(
            _COMPLETE_RE.search(lines[index])
            for index in range(config_index + 1, ask_index)
        ):
            issues.append("a prior STREAM_COMPLETE arrived after this CONFIG but before ASK")
        if len(analyses) != 1:
            issues.append(f"expected one ANALYSE response before EXIT; got {len(analyses)}")
        if len(replies) != 1:
            issues.append(f"expected one REPLY response before EXIT; got {len(replies)}")
        if len(completions) != 1:
            issues.append(f"expected one STREAM_COMPLETE before EXIT; got {len(completions)}")

        logged_reply_bytes: int | None = None
        logged_replies = [
            (index, match.group("text"))
            for index in range(ask_index + 1, boundary)
            if (match := _CMD_REPLY_RE.search(lines[index]))
        ]
        if request.reply_text is not None:
            if supports_logged_reply_text:
                if len(logged_replies) != 1:
                    issues.append(
                        "expected one logged g2evenai reply command before EXIT; "
                        f"got {len(logged_replies)}"
                    )
                else:
                    logged_text = logged_replies[0][1]
                    logged_reply_bytes = len(logged_text.encode("utf-8"))
                    expected_bytes = len(request.reply_text.encode("utf-8"))
                    if logged_text != request.reply_text:
                        issues.append(
                            "logged reply text mismatch: "
                            f"expected {request.reply_text!r} ({expected_bytes} B), "
                            f"got {logged_text!r} ({logged_reply_bytes} B)"
                        )
            else:
                warnings.append(
                    "log has no g2evenai reply command records; reply text/byte "
                    "length could not be validated"
                )
        elif len(logged_replies) == 1:
            logged_reply_bytes = len(logged_replies[0][1].encode("utf-8"))

        reply_tx_ms: int | None = None
        reply_tx_total: int | None = None
        if len(analyses) == 1 and len(replies) == 1:
            tx_candidates = [
                (index, int(match.group("ms")), int(match.group("total")))
                for index in range(analyses[0][0] + 1, replies[0][0])
                if (match := _SID07_TX_RE.search(lines[index]))
                and int(match.group("total")) > 19
            ]
            if len(tx_candidates) != 1:
                issues.append(
                    "expected one non-control outbound sid07 TX between ANALYSE and "
                    f"REPLY response; got {len(tx_candidates)}"
                )
            if tx_candidates:
                _tx_index, reply_tx_ms, reply_tx_total = tx_candidates[-1]

        reply_ms = replies[0][1] if len(replies) == 1 else None
        completion_ms = completions[0][1] if len(completions) == 1 else None
        response_interval = None
        tx_interval = None
        if len(replies) == 1 and len(completions) == 1:
            if len(analyses) == 1 and replies[0][0] <= analyses[0][0]:
                issues.append("REPLY response did not follow the trial ANALYSE response")
            if completions[0][0] <= replies[0][0]:
                issues.append("STREAM_COMPLETE did not follow the trial REPLY response")
            else:
                response_interval = completion_ms - reply_ms
        if reply_tx_ms is not None and completion_ms is not None:
            if completion_ms < reply_tx_ms:
                issues.append("STREAM_COMPLETE timestamp preceded outbound REPLY TX")
            else:
                tx_interval = completion_ms - reply_tx_ms

        if exits:
            late_completions = [
                index
                for index in range(exits[0] + 1, next_config)
                if _COMPLETE_RE.search(lines[index])
            ]
            if late_completions:
                issues.append("STREAM_COMPLETE arrived after EXIT and was not assigned")

        trials.append(
            SpeedEvidence(
                request=request,
                echoed_speed=echoed_speed,
                logged_reply_bytes=logged_reply_bytes,
                reply_tx_ms=reply_tx_ms,
                reply_tx_total=reply_tx_total,
                reply_response_ms=reply_ms,
                completion_ms=completion_ms,
                tx_to_completion_ms=tx_interval,
                response_to_completion_ms=response_interval,
                issues=tuple(issues),
                warnings=tuple(warnings),
            )
        )

    return SpeedMatrixEvidence(tuple(trials), tuple(matrix_issues))


def print_speed_matrix(evidence: SpeedMatrixEvidence) -> None:
    print("\nPer-condition G2 protocol evidence:", flush=True)
    print(
        "  speed  magic  echoed  textB  REPLY-TX(ms/B)  TX->complete  "
        "REPLY-response->complete  result"
    )
    for trial in evidence.trials:
        response_interval = (
            f"{trial.response_to_completion_ms} ms"
            if trial.response_to_completion_ms is not None
            else "--"
        )
        tx_interval = (
            f"{trial.tx_to_completion_ms} ms"
            if trial.tx_to_completion_ms is not None
            else "--"
        )
        echoed = str(trial.echoed_speed) if trial.echoed_speed is not None else "--"
        text_bytes = (
            str(trial.logged_reply_bytes)
            if trial.logged_reply_bytes is not None
            else "--"
        )
        tx_marker = (
            f"{trial.reply_tx_ms}/{trial.reply_tx_total}"
            if trial.reply_tx_ms is not None and trial.reply_tx_total is not None
            else "--"
        )
        result_parts: list[str] = []
        if trial.valid:
            result_parts.append("valid")
        else:
            result_parts.append("INVALID: " + "; ".join(trial.issues))
        result_parts.extend(f"WARNING: {warning}" for warning in trial.warnings)
        result = "; ".join(result_parts)
        print(
            f"  {trial.request.speed:5d}  {trial.request.magic:5d}  "
            f"{echoed:>6s}  {text_bytes:>5s}  {tx_marker:>14s}  "
            f"{tx_interval:>12s}  "
            f"{response_interval:>24s}  {result}",
            flush=True,
        )
    for issue in evidence.issues:
        print(f"  MATRIX INVALID: {issue}", flush=True)


def analyze_render_matrix(
    output: Path, modes: Sequence[str]
) -> RenderMatrixEvidence:
    """Pair every expected render attempt with its ASK-to-EXIT session."""
    lines = output.expanduser().read_text(encoding="utf-8", errors="replace").splitlines()
    asks = [index for index, line in enumerate(lines) if _ASK_RE.search(line)]
    matrix_issues: list[str] = []
    if len(asks) != len(modes):
        matrix_issues.append(f"expected {len(modes)} ASK responses; got {len(asks)}")

    trials: list[RenderEvidence] = []
    for trial_index, mode in enumerate(modes):
        issues: list[str] = []
        if trial_index >= len(asks):
            trials.append(RenderEvidence(mode, 0, 0, None, ("missing ASK response",)))
            continue
        start = asks[trial_index]
        next_ask = asks[trial_index + 1] if trial_index + 1 < len(asks) else len(lines)
        exits = [index for index in range(start + 1, next_ask) if _EXIT_RE.search(lines[index])]
        if not exits:
            issues.append("no native EXIT boundary before the next ASK/end of log")
            boundary = next_ask
        else:
            boundary = exits[0]

        replies = [
            (index, int(match.group("ms")))
            for index in range(start + 1, boundary)
            if (match := _REPLY_RE.search(lines[index]))
        ]
        completions = [
            (index, int(match.group("ms")))
            for index in range(start + 1, boundary)
            if (match := _COMPLETE_RE.search(lines[index]))
        ]
        expected_replies = 1 if mode == "one-shot" else 3
        if len(replies) != expected_replies:
            issues.append(
                f"expected {expected_replies} REPLY response(s) before EXIT; got {len(replies)}"
            )
        if len(completions) != 1:
            issues.append(f"expected one STREAM_COMPLETE before EXIT; got {len(completions)}")

        interval = None
        if replies and len(completions) == 1:
            if completions[0][0] <= replies[-1][0]:
                issues.append("STREAM_COMPLETE did not follow the final REPLY response")
            else:
                interval = completions[0][1] - replies[-1][1]

        if exits and any(
            _COMPLETE_RE.search(lines[index])
            for index in range(exits[0] + 1, next_ask)
        ):
            issues.append("STREAM_COMPLETE arrived after EXIT and was not assigned")

        trials.append(
            RenderEvidence(mode, len(replies), len(completions), interval, tuple(issues))
        )

    return RenderMatrixEvidence(tuple(trials), tuple(matrix_issues))


def print_render_matrix(evidence: RenderMatrixEvidence) -> None:
    print("\nPer-session render protocol evidence:", flush=True)
    print("  trial  mode       REPLY responses  completions  final-response->complete  result")
    for index, trial in enumerate(evidence.trials, start=1):
        interval = (
            f"{trial.final_response_to_completion_ms} ms"
            if trial.final_response_to_completion_ms is not None
            else "--"
        )
        result = "valid" if trial.valid else "; ".join(trial.issues)
        print(
            f"  {index:5d}  {trial.mode:9s}  {trial.reply_responses:15d}  "
            f"{trial.completions:11d}  {interval:>24s}  {result}",
            flush=True,
        )
    for issue in evidence.issues:
        print(f"  MATRIX INVALID: {issue}", flush=True)


async def render_ab(session: Session, args: argparse.Namespace) -> None:
    """Compare one-shot final REPLY against two immediate streamed parts."""
    await reconnect_g2(session)
    await maybe_set_speed(session, args.speed)

    device_path, owns_log = await start_protocol_log(
        session, args.device_log or default_device_log_path()
    )
    first_round = (
        ("one-shot", "one final REPLY with all 180 characters"),
        ("two-parts", "two immediate REPLY parts, then an empty finalizer"),
    )
    if args.order == "stream-first":
        first_round = tuple(reversed(first_round))

    split = RENDER_TEXT_180.rfind(" ", 0, len(RENDER_TEXT_180) // 2 + 1)
    part_one = RENDER_TEXT_180[:split]
    part_two = RENDER_TEXT_180[split:]  # leading space is required stream glue
    assert part_one + part_two == RENDER_TEXT_180

    loop = asyncio.get_running_loop()
    attempt = 0
    total_attempts = args.repetitions * 2
    attempt_modes: list[str] = []
    exchange_id: str | None = None
    try:
        for repetition in range(args.repetitions):
            variants = (
                first_round if repetition % 2 == 0 else tuple(reversed(first_round))
            )
            for variant, description in variants:
                attempt += 1
                if attempt > 1:
                    await exit_active_evenai(session, expected_id=exchange_id)
                    await asyncio.sleep(0.5)
                label = f"{variant} repetition {repetition + 1}/{args.repetitions}"
                await require_healthy_temples(session)
                exchange_id = await wait_for_native_wake(session, attempt=label)
                print(
                    f"RENDER A/B {attempt}/{total_attempts} — {label}: {description}",
                    flush=True,
                )
                attempt_modes.append(variant)
                await send(
                    session,
                    f"g2evenai askid {exchange_id} Render comparison ready.",
                )
                ask_ok = loop.time()
                target = ask_ok + args.ask_settle_ms / 1000.0
                await asyncio.sleep(max(0.0, target - loop.time()))

                reply_start = loop.time()
                if variant == "one-shot":
                    await send(
                        session, f"g2evenai replyid {exchange_id} {RENDER_TEXT_180}"
                    )
                else:
                    await send(
                        session, f"g2evenai replypartid {exchange_id} {part_one}"
                    )
                    # part_two begins with a space.  The command therefore has
                    # two spaces after the ID and the firmware preserves the
                    # second one as the append glue.
                    await send(
                        session, f"g2evenai replypartid {exchange_id} {part_two}"
                    )
                    await send(session, f"g2evenai replyendid {exchange_id}")
                reply_ok = loop.time()
                print(
                    f"{variant} submission: start "
                    f"+{(reply_start-ask_ok)*1000.0:.1f} ms after ASK ESP32 command OK; "
                    f"final ESP32 command OK took "
                    f"{(reply_ok-reply_start)*1000.0:.1f} ms",
                    flush=True,
                )
                print(
                    f"Waiting {args.render_wait_ms} ms for the glasses to emit "
                    "STREAM_COMPLETE...",
                    flush=True,
                )
                await asyncio.sleep(args.render_wait_ms / 1000.0)
    finally:
        try:
            await exit_active_evenai(
                session, expected_id=exchange_id, strict=False
            )
        finally:
            log_closed = await stop_protocol_log(session, owns_log)

    if not log_closed:
        raise RuntimeError(
            f"evidence log {device_path} was not closed; refusing to fetch/analyze it"
        )

    output = (
        Path(args.output).expanduser()
        if args.output
        else Path.home() / "g2-prefx" / Path(device_path).name
    )
    await fetch_file(session, device_path, output)
    print_protocol_markers(output)
    evidence = analyze_render_matrix(output, attempt_modes)
    print_render_matrix(evidence)
    if not evidence.valid:
        print(
            "\nWARNING: the render matrix is incomplete or ambiguous. "
            "Do not calculate a renderer rate or mode winner from timeout "
            "windows or aggregate marker counts.",
            flush=True,
        )
    else:
        print(
            "\nRender matrix is structurally valid. Timings above start at the "
            "final G2 REPLY response, not text TX. Each mode ran in a fresh wake "
            f"session {args.repetitions} times; order alternated after a "
            f"{args.order} first round.",
            flush=True,
        )


async def speed_ab(session: Session, args: argparse.Namespace) -> None:
    """Test whether field-only streamSpeed changes short-reply completion."""
    question = validate_text(args.question, label="speed-control question")
    reply_text = validate_text(args.reply_text, label="speed-control reply")
    await reconnect_g2(session)
    device_path, owns_log = await start_protocol_log(
        session, args.device_log or default_speed_log_path()
    )
    requests: list[SpeedRequest] = []
    config_was_submitted = False
    log_closed = False
    exchange_id: str | None = None

    try:
        for index, speed in enumerate(args.speeds, start=1):
            if index > 1:
                await exit_active_evenai(session, expected_id=exchange_id)
                await asyncio.sleep(0.5)
            print(
                f"SPEED A/B {index}/{len(args.speeds)} — field-only streamSpeed={speed}",
                flush=True,
            )
            await require_healthy_temples(session)
            config_was_submitted = True
            config_reply = await send(session, f"g2aiconfig - {speed} -")
            magic_match = _COMMAND_MAGIC_RE.search(config_reply.text)
            if not magic_match:
                raise RuntimeError(
                    "g2aiconfig result did not report its magic value; cannot correlate "
                    "the asynchronous CONFIG response"
                )
            requests.append(
                SpeedRequest(speed, int(magic_match.group(1)), reply_text)
            )
            await asyncio.sleep(0.5)
            exchange_id = await wait_for_native_wake(
                session, attempt=f"streamSpeed={speed}"
            )
            await send(session, f"g2evenai askid {exchange_id} {question}")
            await asyncio.sleep(args.ask_settle_ms / 1000.0)
            await send(session, f"g2evenai replyid {exchange_id} {reply_text}")
            print(
                f"Waiting {args.render_wait_ms} ms for streamSpeed={speed} "
                "STREAM_COMPLETE...",
                flush=True,
            )
            await asyncio.sleep(args.render_wait_ms / 1000.0)
    finally:
        try:
            await exit_active_evenai(
                session, expected_id=exchange_id, strict=False
            )
        finally:
            try:
                log_closed = await stop_protocol_log(session, owns_log)
            finally:
                if config_was_submitted:
                    print(
                        "RESET-STATE WARNING: this matrix submitted EvenAI CONFIG. "
                        "Power-cycle the glasses, then verify whether the faster "
                        "pre-CONFIG baseline returns; another CONFIG packet is not "
                        "a validated reset.",
                        flush=True,
                    )

    if not log_closed:
        raise RuntimeError(
            f"evidence log {device_path} was not closed; refusing to fetch/analyze it"
        )
    if len(requests) != len(args.speeds):
        raise RuntimeError("not every CONFIG request was recorded; matrix is incomplete")

    output = (
        Path(args.output).expanduser()
        if args.output
        else Path.home() / "g2-prefx" / Path(device_path).name
    )
    await fetch_file(session, device_path, output)
    print_protocol_markers(output)
    evidence = analyze_speed_matrix(output, requests)
    print_speed_matrix(evidence)
    if not evidence.valid:
        print(
            "WARNING: speed matrix is invalid. Do not infer field direction; "
            "inspect the per-condition failures above.",
            flush=True,
        )
    elif not evidence.clean:
        print(
            "Speed matrix protocol structure is valid, but one or more health/"
            "observability warnings make the optical comparison non-clean. "
            "Treat timing as device-side protocol evidence and rerun for a clean "
            "dual-temple result.",
            flush=True,
        )
    else:
        print(
            "Speed matrix is structurally valid: every request magic has the "
            "expected CONFIG echo, one ASK/ANALYSE/REPLY sequence, one outbound "
            "REPLY TX, and one pre-EXIT STREAM_COMPLETE. Both TX-to-completion "
            "and G2-response-to-completion are reported.",
            flush=True,
        )


async def restore(session: Session) -> None:
    await exit_active_evenai(session)
    print(
        "No protocol-level CONFIG reset is known. The old restore action sent "
        "streamSpeed=80, which was correlated with slower completion. Power-cycle "
        "the glasses, then verify whether the faster pre-CONFIG baseline returns; "
        "do not assume reset until that control reproduces it."
    )


async def commands(session: Session, args: argparse.Namespace) -> None:
    for line in args.lines:
        await send(session, line, expect="auto", required=False)


async def fetch(session: Session, args: argparse.Namespace) -> None:
    await fetch_file(session, args.path, Path(args.output))


async def run(args: argparse.Namespace) -> None:
    # Keep service imports lazy so ``--help`` and argument validation remain
    # usable on a development Mac that does not have the Pi runtime extras.
    from hw1_ai_service import config as config_mod
    from hw1_ai_service.link.session import Session
    from hw1_ai_service.link.transport import SerialTransport

    config_path = Path(os.path.expanduser(args.config))
    cfg = config_mod.load(config_path)
    username, password = config_mod.read_credentials(cfg.link.credentials_file)
    transport = SerialTransport(cfg.link.port, cfg.link.baud)
    transport.open()
    session = Session(transport, username, password)
    try:
        await session.login()
        if args.action == "preflight":
            await preflight(session)
        elif args.action == "trial":
            await trial(session, args)
        elif args.action == "ask-threshold":
            await ask_threshold(session, args)
        elif args.action == "render-ab":
            await render_ab(session, args)
        elif args.action == "speed-ab":
            await speed_ab(session, args)
        elif args.action == "restore":
            await restore(session)
        elif args.action == "cmd":
            await commands(session, args)
        elif args.action == "fetch":
            await fetch(session, args)
        else:  # pragma: no cover - argparse makes this unreachable
            raise RuntimeError(f"unknown action: {args.action}")
    finally:
        transport.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="No-camera G2 EvenAI timing and protocol diagnostics.",
        epilog=(
            "Stop hw1-ai-service before running this tool; both processes cannot "
            "own the ESP32 UART at once. Delays reported by trial and ask-threshold "
            "start after the ASK ESP32 command result (not a G2 receipt)."
        ),
    )
    parser.add_argument("--config", default="~/.config/hw1-ai-service/config.yaml")
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("preflight", help="verify UART, G2 connection, and probe commands")

    trial_parser = sub.add_parser(
        "trial", help="run the original partial/final ASK timing sequence"
    )
    trial_parser.add_argument("speed", choices=("none", *CONFIGS))
    trial_parser.add_argument("sequence", choices=SEQUENCES)
    trial_parser.add_argument("--cadence-ms", type=int, default=500)
    trial_parser.add_argument("--reply-delay-ms", type=int, default=5000)

    threshold_parser = sub.add_parser(
        "ask-threshold",
        help="wearer complete/cut trials for an ESP32-OK-based ASK grace window",
    )
    threshold_parser.add_argument(
        "--delays-ms",
        type=parse_delays,
        default=parse_delays("2000,2500,3000,3500,4000"),
        metavar="LIST",
        help="comma-separated ESP32-command-OK-to-REPLY delays (default: %(default)s)",
    )
    threshold_parser.add_argument("--question", default=DEFAULT_THRESHOLD_QUESTION)
    threshold_parser.add_argument("--reply-text", default="Probe complete")
    threshold_parser.add_argument("--speed", choices=("none", *CONFIGS), default="none")

    render_parser = sub.add_parser(
        "render-ab",
        help="log one-shot versus two-part 180-character render completion",
    )
    render_parser.add_argument("--speed", choices=("none", *CONFIGS), default="none")
    render_parser.add_argument(
        "--order", choices=("one-shot-first", "stream-first"), default="one-shot-first"
    )
    render_parser.add_argument("--ask-settle-ms", type=int, default=1500)
    render_parser.add_argument("--render-wait-ms", type=int, default=20000)
    render_parser.add_argument(
        "--repetitions",
        type=int,
        default=3,
        help="fresh wake sessions per render mode; order alternates (default: %(default)s)",
    )
    render_parser.add_argument(
        "--device-log",
        help="ESP32 log path; default is a timestamped /logging_captures/system file",
    )
    render_parser.add_argument(
        "--output",
        help="Pi destination for fetched log (default: ~/g2-prefx/<device filename>)",
    )

    speed_parser = sub.add_parser(
        "speed-ab",
        help="field-only CONFIG speed A/B/A using short reply completion events",
    )
    speed_parser.add_argument(
        "--speeds",
        type=parse_speeds,
        default=parse_speeds("80,40,80"),
        metavar="LIST",
        help="comma-separated field-2 values (default: %(default)s)",
    )
    speed_parser.add_argument("--question", default="Ready.")
    speed_parser.add_argument("--reply-text", default="Probe complete")
    speed_parser.add_argument("--ask-settle-ms", type=int, default=2000)
    speed_parser.add_argument("--render-wait-ms", type=int, default=3500)
    speed_parser.add_argument(
        "--device-log",
        help="ESP32 log path; default is a timestamped /logging_captures/system file",
    )
    speed_parser.add_argument(
        "--output",
        help="Pi destination for fetched log (default: ~/g2-prefx/<device filename>)",
    )

    sub.add_parser(
        "restore",
        help="exit EvenAI and explain how to run a power-cycle reset-state control",
    )

    command_parser = sub.add_parser("cmd", help="send one or more raw firmware commands")
    command_parser.add_argument("lines", nargs="+")

    fetch_parser = sub.add_parser("fetch", help="fetch a file from the ESP32")
    fetch_parser.add_argument("path")
    fetch_parser.add_argument("output")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if getattr(args, "reply_delay_ms", 0) < 0:
        raise ValueError("--reply-delay-ms must be non-negative")
    if getattr(args, "cadence_ms", 0) < 0:
        raise ValueError("--cadence-ms must be non-negative")
    if getattr(args, "ask_settle_ms", 0) < 0:
        raise ValueError("--ask-settle-ms must be non-negative")
    if getattr(args, "render_wait_ms", 0) < 0:
        raise ValueError("--render-wait-ms must be non-negative")
    if getattr(args, "repetitions", 1) < 1:
        raise ValueError("--repetitions must be at least 1")
    if hasattr(args, "reply_text"):
        validate_text(args.reply_text, label="reply text")
    asyncio.run(run(args))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
