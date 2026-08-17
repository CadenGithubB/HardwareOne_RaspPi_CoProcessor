#!/usr/bin/env python3
"""Validate recorder-shadow PCM against its finalized WAV, without STT.

This is deliberately a standalone diagnostic.  It never imports the daemon,
pipeline, Moonshine, LLM, or G2 delivery modules.  ``owned`` validates an
explicitly owned, untrimmed capture with byte parity.  ``native`` observes one
real Hey-Even wake and validates its firmware-issued identity and trimmed WAV
without ever starting STT or painting ASK/REPLY content.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import posixpath
import re
import secrets
import sys
import threading
import time
from pathlib import Path

from hw1_ai_service import config as config_mod
from hw1_ai_service import evenai_protocol as evenai_wire
from hw1_ai_service import log as log_mod
from hw1_ai_service.audio import fetch, wav
from hw1_ai_service.audio.live import (
    DEFAULT_PCM_QUEUE_BYTES,
    DEFAULT_PCM_QUEUE_FRAMES,
    LivePcmChunk,
    LivePcmInbox,
    LiveStreamTerminal,
)
from hw1_ai_service.link import protocol
from hw1_ai_service.link.session import Session
from hw1_ai_service.link.transport import SerialTransport
from hw1_ai_service.stt.live import (
    DEFAULT_QUEUE_CHUNKS as DEFAULT_STT_QUEUE_CHUNKS,
    DEFAULT_TEXT_QUEUE_EVENTS as DEFAULT_STT_TEXT_QUEUE_EVENTS,
    LiveMoonshineWorker,
    exact_moonshine_factory,
    performance_governors,
)


log = logging.getLogger("tools.live_pcm_shadow_probe")
MIN_BAUD = 921_600
QUIESCE_TIMEOUT_S = 5.0
NATIVE_AUTOSTOP_GRACE_S = 0.75
SOURCE_CODES = {"pdm": protocol.LIVE_SOURCE_PDM,
                "g2": protocol.LIVE_SOURCE_G2}
FAULT_NONE = "none"
FAULT_HOST_OVERFLOW = "host-overflow"
FAULT_HOST_GAP = "host-gap"
FAULT_HOST_ABORT = "host-abort"
FAULT_LEASE_EXPIRE = "lease-expire"
FAULT_CHOICES = (
    FAULT_NONE,
    FAULT_HOST_OVERFLOW,
    FAULT_HOST_GAP,
    FAULT_HOST_ABORT,
    FAULT_LEASE_EXPIRE,
)
_PATH_RE = re.compile(r"(/(?:sd/)?recordings/[^\s]+\.wav)")
_STATUS_ACTIVE_RE = re.compile(r"(?:^|\s)active=([^\s]+)")
_STATUS_EXCHANGE_RE = re.compile(r"(?:^|\s)exchange=([^\s]+)")
_EVENAI_STATUS_RE = re.compile(
    r"EvenAI session:\s+(active|idle)\s+id=([^\s]+)\s+"
    r"arm=([^\s]+)\s+gen=(\d+)\s+uart_epoch=(\d+)")
_WORD_RE = re.compile(r"[^\W_]+(?:['\N{RIGHT SINGLE QUOTATION MARK}][^\W_]+)*",
                      re.UNICODE)


def _status_tokens(text: str) -> dict[str, str]:
    return {
        key: value
        for key, value in re.findall(r"(?:^|\s)([a-z0-9_]+)=([^\s]+)", text)
    }


def _words(text: str) -> list[str]:
    return [item.replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")
            for item in _WORD_RE.findall(text.casefold())]


def _word_errors(reference: str, hypothesis: str) -> int:
    left = _words(reference)
    right = _words(hypothesis)
    previous = list(range(len(right) + 1))
    for row, left_word in enumerate(left, start=1):
        current = [row]
        for column, right_word in enumerate(right, start=1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left_word != right_word),
            ))
        previous = current
    return previous[-1]


class _FaultInjectingSink:
    """Reader-thread-safe, nonblocking fault shim installed before UART open."""

    def __init__(self, inbox: LivePcmInbox, fault: str) -> None:
        self._inbox = inbox
        self._fault = fault
        self._pcm_frames = 0
        self.first_pcm = threading.Event()
        self.injected = False

    def offer_frame(self, ftype: int, seq: int, payload: bytes) -> bool:
        if ftype == protocol.FRAME_LIVE_PCM:
            self._pcm_frames += 1
            self.first_pcm.set()
            if self._fault == FAULT_HOST_GAP and self._pcm_frames == 2:
                # Claim exactly one already-validated outer frame without
                # forwarding it. The next physical frame must fail the inbox's
                # wire-sequence/offset fence deterministically.
                self.injected = True
                return True
            if self._fault == FAULT_HOST_OVERFLOW and self._pcm_frames == 1:
                self.injected = True
        return self._inbox.offer_frame(ftype, seq, payload)

    def link_closed(self) -> None:
        self._inbox.link_closed()


class _NativeEventRecorder:
    """Loop-thread-only strict EvenAI event journal for the native probe."""

    def __init__(self, started_ns: int) -> None:
        self._started_ns = started_ns
        self._queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=32)
        self.observed: list[dict[str, object]] = []
        self.errors: list[str] = []
        self.prearm_errors: list[str] = []
        self.bound_exchange: str | None = None
        self.cancel_reason: str | None = None
        self._armed_ns: int | None = None
        self._cleanup_exit_exchange: str | None = None

    def __call__(self, payload: bytes) -> None:
        received_ns = time.monotonic_ns()
        try:
            text = payload.decode("ascii").strip()
        except UnicodeDecodeError:
            self.errors.append("non-ASCII device event during native gate")
            return
        try:
            event = evenai_wire.parse_event(text)
        except evenai_wire.EvenAiProtocolError as exc:
            self.errors.append(f"malformed native event {text!r}: {exc}")
            return
        if event is None:
            return
        entry: dict[str, object] = {
            "text": text,
            "at_ms": (received_ns - self._started_ns) / 1_000_000.0,
            "received_ns": received_ns,
            "phase": "armed" if self._armed_ns is not None else "prearm",
            "event": event,
        }
        self.observed.append(entry)
        if self.bound_exchange is not None:
            if (isinstance(event, evenai_wire.CancelEvent) and
                    event.exchange_id == self.bound_exchange):
                expected_cleanup = bool(
                    self._cleanup_exit_exchange == self.bound_exchange and
                    event.reason == "host_exit")
                if not expected_cleanup:
                    self.cancel_reason = event.reason
            elif (isinstance(event, evenai_wire.WakeEvent) and
                  event.exchange_id != self.bound_exchange):
                self.cancel_reason = "superseded"
            elif (isinstance(event, evenai_wire.MicAutostopEvent) and
                  event.exchange_id != self.bound_exchange):
                self.errors.append(
                    "mic_autostop exchange does not match bound native wake")
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            if "native event queue overflow" not in self.errors:
                self.errors.append("native event queue overflow")

    def arm(self) -> None:
        """Drop pre-arm recognized events after an explicit idle preflight."""
        self._armed_ns = time.monotonic_ns()
        self.prearm_errors.extend(self.errors)
        self.errors.clear()
        self.cancel_reason = None
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    def bind(self, exchange_id: str) -> None:
        eid = evenai_wire.exchange_id(exchange_id)
        if self.bound_exchange is not None and self.bound_exchange != eid:
            raise RuntimeError(
                "native identity candidates disagree: "
                f"{self.bound_exchange} != {eid}")
        self.bound_exchange = eid
        for entry in self.observed:
            if entry.get("phase") != "armed":
                continue
            event = entry["event"]
            if (isinstance(event, evenai_wire.CancelEvent) and
                    event.exchange_id == eid):
                self.cancel_reason = event.reason

    def begin_cleanup_exit(self, exchange_id: str) -> None:
        eid = evenai_wire.exchange_id(exchange_id)
        if self.bound_exchange != eid:
            raise RuntimeError("cleanup EXIT does not match bound native exchange")
        self._cleanup_exit_exchange = eid

    async def next(self, timeout: float) -> dict[str, object] | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    @property
    def cancelled(self) -> bool:
        return self.cancel_reason is not None

    def public_observations(self) -> list[dict[str, object]]:
        return [
            {"text": str(entry["text"]), "at_ms": entry["at_ms"],
             "phase": entry["phase"]}
            for entry in self.observed
        ]

    def armed_wake_ids(self) -> set[str]:
        return {
            entry["event"].exchange_id
            for entry in self.observed
            if entry.get("phase") == "armed" and
            isinstance(entry.get("event"), evenai_wire.WakeEvent)
        }


class _NativeStreamCollector:
    """Drain native PCM immediately while exposing its BEGIN identity."""

    def __init__(self, inbox: LivePcmInbox, expected_source: int,
                 wake_timeout: float, capture_timeout: float,
                 pcm_observer=None) -> None:
        self._inbox = inbox
        self._expected_source = expected_source
        self._wake_timeout = wake_timeout
        self._capture_timeout = capture_timeout
        self._pcm_observer = pcm_observer
        self.begin_ready = threading.Event()
        self.begin: dict[str, object] | None = None
        self.error: BaseException | None = None

    def run(self) -> tuple[bytes, LiveStreamTerminal, dict]:
        try:
            stream = self._inbox.next_stream(timeout=self._wake_timeout)
            self.begin = {
                "exchange_id": protocol.live_id_hex(stream.exchange_id),
                "controller_id": protocol.live_id_hex(stream.controller_id),
                "source": stream.begin.source,
                "sample_rate": stream.begin.sample_rate,
                "synthetic": stream.begin.synthetic,
                "received_ns": stream.begin_received_ns,
            }
            self.begin_ready.set()
            if self._pcm_observer is not None:
                self._pcm_observer.on_begin(self.begin)
            if stream.begin.synthetic:
                stream.invalidate("probe_received_synthetic_begin")
            elif stream.begin.source != self._expected_source:
                stream.invalidate(
                    f"probe_source:{stream.begin.source}!={self._expected_source}")

            pcm = bytearray()
            deadline = time.monotonic() + self._capture_timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stream.invalidate("probe_native_capture_deadline")
                    remaining = 0.1
                item = stream.next_item(timeout=remaining)
                if isinstance(item, LiveStreamTerminal):
                    if self._pcm_observer is not None:
                        self._pcm_observer.end_input()
                    return bytes(pcm), item, stream.snapshot()
                assert isinstance(item, LivePcmChunk)
                pcm.extend(item.pcm)
                if self._pcm_observer is not None:
                    # Streaming STT is deliberately best-effort. A full model
                    # queue makes its own result invalid but never stops this
                    # collector from draining and preserving transport/WAV.
                    self._pcm_observer.offer_pcm(item.pcm)
        except BaseException as exc:
            self.error = exc
            self.begin_ready.set()
            if self._pcm_observer is not None:
                self._pcm_observer.abort(
                    f"native_collector:{type(exc).__name__}:{exc}")
            raise


def _fresh_id() -> int:
    high = secrets.randbits(32) or 1
    low = secrets.randbits(32) or 1
    return (high << 32) | low


def _id_arg(raw: str) -> int:
    try:
        value = int(raw, 16)
        protocol.live_id_hex(value)
        return value
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be 16 hex digits with nonzero high and low halves") from exc


def _record_seconds_arg(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(value) or not (0.05 <= value <= 30.0):
        raise argparse.ArgumentTypeError("must be between 0.05 and 30 seconds")
    return value


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def _nonnegative_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return value


def _native_timeout_arg(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(value) or not (1.0 <= value <= 60.0):
        raise argparse.ArgumentTypeError("must be between 1 and 60 seconds")
    return value


def _positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def _parse_evenai_status(text: str) -> dict[str, object]:
    match = _EVENAI_STATUS_RE.search(text)
    if match is None:
        raise RuntimeError(f"malformed g2evenai status: {text}")
    state, exchange_id, arm, generation, uart_epoch = match.groups()
    if state == "active":
        exchange_id = evenai_wire.exchange_id(exchange_id)
        if arm not in ("L", "R"):
            raise RuntimeError(f"active g2evenai status has invalid arm: {text}")
    elif exchange_id != "-" or arm != "-" or int(uart_epoch) != 0:
        raise RuntimeError(f"idle g2evenai status retains identity/epoch: {text}")
    return {
        "state": state,
        "exchange_id": exchange_id,
        "arm": arm,
        "generation": int(generation),
        "uart_epoch": int(uart_epoch),
        "text": text,
    }


async def _await_native_wake(
    events: _NativeEventRecorder,
    timeout: float,
) -> tuple[evenai_wire.WakeEvent, dict[str, object]]:
    deadline = time.monotonic() + timeout
    while True:
        if events.errors:
            raise RuntimeError(events.errors[0])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("timed out waiting for a native evenai_wake")
        entry = await events.next(remaining)
        if entry is None:
            raise RuntimeError("timed out waiting for a native evenai_wake")
        event = entry["event"]
        if isinstance(event, evenai_wire.WakeEvent):
            for prior in events.observed:
                if prior is entry or prior.get("phase") != "armed":
                    continue
                if int(prior["received_ns"]) >= int(entry["received_ns"]):
                    continue
                prior_event = prior["event"]
                if (isinstance(prior_event, evenai_wire.CancelEvent) and
                        prior_event.exchange_id == event.exchange_id):
                    raise RuntimeError(
                        "native wake ID was already cancelled before wake "
                        f"correlation: {prior_event.reason}")
                if (isinstance(prior_event, evenai_wire.MicAutostopEvent) and
                        prior_event.exchange_id == event.exchange_id):
                    raise RuntimeError(
                        "native wake ID already had mic_autostop before wake")
            return event, entry
        # Old session tombstones may retry for up to eight seconds. Before a
        # wake supplies the new identity, journal but do not let a foreign ID
        # poison admission.


async def _await_native_begin(
    collector: _NativeStreamCollector,
    timeout: float,
) -> dict[str, object]:
    ready = await asyncio.to_thread(collector.begin_ready.wait, timeout)
    if not ready:
        raise RuntimeError("timed out waiting for native LIVE_BEGIN")
    if collector.error is not None:
        raise RuntimeError(
            "native LIVE_BEGIN collector failed: "
            f"{type(collector.error).__name__}: {collector.error}")
    if collector.begin is None:
        raise RuntimeError("native collector became ready without LIVE_BEGIN")
    return collector.begin


async def _await_native_autostop(
    session: Session,
    events: _NativeEventRecorder,
    exchange_id: str,
    timeout: float,
) -> tuple[evenai_wire.MicAutostopEvent, dict[str, object], list[str]]:
    """Require the exact terminal event; statusid is only its safe backstop."""
    deadline = time.monotonic() + timeout
    status_history: list[str] = []
    while True:
        if events.errors:
            raise RuntimeError(events.errors[0])
        if events.cancel_reason is not None:
            raise RuntimeError(
                f"native exchange {exchange_id} cancelled: "
                f"{events.cancel_reason}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f"timed out waiting for mic_autostop {exchange_id}")
        entry = await events.next(min(0.25, remaining))
        if entry is not None:
            event = entry["event"]
            if isinstance(event, evenai_wire.MicAutostopEvent):
                if event.exchange_id != exchange_id:
                    raise RuntimeError(
                        "mic_autostop belongs to a different exchange")
                return event, entry, status_history
            if isinstance(event, evenai_wire.CancelEvent):
                if event.exchange_id == exchange_id:
                    raise RuntimeError(
                        f"native exchange {exchange_id} cancelled: "
                        f"{event.reason}")
                continue
            if isinstance(event, evenai_wire.WakeEvent):
                if event.exchange_id != exchange_id:
                    raise RuntimeError(
                        "replacement native wake superseded the bound exchange")
                continue

        reply = await session.command(
            evenai_wire.mic_status_command(exchange_id),
            expect="status", timeout=5.0, replay=False)
        status_history.append(reply.text)
        if not reply.ok:
            raise RuntimeError(
                f"native recording status rejected for {exchange_id}: "
                f"{reply.text}")
        reply_lower = reply.text.lower()
        if "discarded" in reply_lower:
            raise RuntimeError(
                f"native recording {exchange_id} was discarded")
        if "stopped" in reply_lower or "idle" in reply_lower:
            # Current firmware emits mic_autostop only after close/finalize.
            # A stopped status can win the UART race by milliseconds, but a
            # missing terminal EVT is still a gate failure, not permission to
            # poll through the entire capture timeout.
            grace_deadline = min(
                deadline, time.monotonic() + NATIVE_AUTOSTOP_GRACE_S)
            while True:
                grace_remaining = grace_deadline - time.monotonic()
                if grace_remaining <= 0:
                    raise RuntimeError(
                        "native recording stopped without exact "
                        f"mic_autostop {exchange_id}")
                grace_entry = await events.next(grace_remaining)
                if grace_entry is None:
                    raise RuntimeError(
                        "native recording stopped without exact "
                        f"mic_autostop {exchange_id}")
                grace_event = grace_entry["event"]
                if isinstance(grace_event, evenai_wire.MicAutostopEvent):
                    if grace_event.exchange_id == exchange_id:
                        return grace_event, grace_entry, status_history
                    continue
                if (isinstance(grace_event, evenai_wire.CancelEvent) and
                        grace_event.exchange_id == exchange_id):
                    raise RuntimeError(
                        f"native exchange {exchange_id} cancelled: "
                        f"{grace_event.reason}")
                if (isinstance(grace_event, evenai_wire.WakeEvent) and
                        grace_event.exchange_id != exchange_id):
                    raise RuntimeError(
                        "replacement native wake superseded the bound exchange")


def _collect(
    inbox: LivePcmInbox,
    exchange_id: int,
    expected_source: int,
    record_seconds: float,
) -> tuple[bytes, LiveStreamTerminal, dict]:
    stream = inbox.next_stream(timeout=3.0)
    if stream.exchange_id != exchange_id:
        stream.invalidate(
            f"probe_expected_exchange:{exchange_id:016x}")
    elif stream.begin.synthetic:
        stream.invalidate("probe_received_synthetic_begin")
    elif stream.begin.source != expected_source:
        stream.invalidate(
            f"probe_source:{stream.begin.source}!={expected_source}")

    pcm = bytearray()
    deadline = time.monotonic() + max(8.0, record_seconds + 8.0)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stream.invalidate("probe_deadline")
            remaining = 0.1
        item = stream.next_item(timeout=remaining)
        if isinstance(item, LiveStreamTerminal):
            return bytes(pcm), item, stream.snapshot()
        assert isinstance(item, LivePcmChunk)
        pcm.extend(item.pcm)


async def _renew_lease(session: Session, controller_id: int,
                       stop: asyncio.Event, errors: list[str],
                       timing: protocol.LiveLeaseTiming,
                       session_epoch: int) -> None:
    command = f"liveaudio ready 1 {controller_id:016x}"
    interval_s = timing.renew_ms / 1000.0
    loop = asyncio.get_running_loop()
    next_send = loop.time() + interval_s
    while True:
        try:
            await asyncio.wait_for(
                stop.wait(), max(0.05, next_send - loop.time()))
            return
        except asyncio.TimeoutError:
            pass
        sent_at = loop.time()
        try:
            reply = await session.command(
                command, expect="status", timeout=5.0, replay=True)
            if not reply.ok:
                errors.append(f"lease renewal rejected: {reply.text}")
                return
            parsed = protocol.parse_live_ready(
                reply.text, expected_controller=controller_id)
            renewed_timing = protocol.live_lease_timing_from_ready(parsed)
            if parsed.session_epoch != session_epoch:
                errors.append("lease renewal changed session epoch")
                return
            if renewed_timing != timing:
                errors.append("lease renewal timing contract changed")
                return
            next_send = sent_at + interval_s
        except Exception as exc:
            errors.append(
                f"lease renewal failed: {type(exc).__name__}: {exc}")
            return


async def _inject_control_fault(
    session: Session,
    sink: _FaultInjectingSink,
    fault: str,
    fault_after_ms: int,
    controller_text: str,
    exchange_text: str,
    lease_stop: asyncio.Event,
) -> dict[str, object]:
    """Inject one exact fault only after physical PCM has begun flowing."""
    saw_pcm = await asyncio.to_thread(sink.first_pcm.wait, 3.0)
    if not saw_pcm:
        raise RuntimeError(f"{fault} fault never observed first PCM")
    if fault_after_ms:
        await asyncio.sleep(fault_after_ms / 1000.0)

    if fault == FAULT_LEASE_EXPIRE:
        lease_stop.set()
        sink.injected = True
        return {"command": None, "reply": None}
    if fault == FAULT_HOST_ABORT:
        command = f"liveaudio abort 1 {controller_text} {exchange_text}"
        reply = await session.command(
            command, expect="status", timeout=5.0, replay=False)
        if not reply.ok:
            raise RuntimeError(f"host-abort injection rejected: {reply.text}")
        sink.injected = True
        return {"command": command, "reply": reply.text}
    raise RuntimeError(f"unsupported control fault {fault!r}")


async def _best_effort_command(session: Session, command: str) -> None:
    try:
        await session.command(
            command, expect="status", timeout=5.0, replay=False)
    except BaseException as exc:
        log.warning("best-effort %r failed: %s", command, exc)


async def _wait_live_quiescent(
    session: Session,
    exchange_id: int,
    *,
    timeout: float = QUIESCE_TIMEOUT_S,
) -> dict[str, str]:
    """Wait until the async producer has relinquished the framed lane.

    Shadow off/release only request an abort; their text ACK can precede the
    worker's terminal frame.  Starting voicefetch during that wind-down would
    mix two framed producers.  Poll the read-only status contract and require
    both authoritative idle fields before any bulk transfer.
    """
    expected = protocol.live_id_hex(exchange_id)
    deadline = time.monotonic() + timeout
    last: dict[str, str] = {}
    while True:
        reply = await session.command(
            "liveaudio status", expect="status", timeout=5.0, replay=True)
        if not reply.ok:
            raise RuntimeError(f"liveaudio status rejected: {reply.text}")
        active_match = _STATUS_ACTIVE_RE.search(reply.text)
        exchange_match = _STATUS_EXCHANGE_RE.search(reply.text)
        if active_match is None or exchange_match is None:
            raise RuntimeError(
                f"liveaudio status missing active/exchange: {reply.text}")
        last = {
            "active": active_match.group(1),
            "exchange": exchange_match.group(1),
            "text": reply.text,
        }
        if last["active"] == "0" and last["exchange"] == "-":
            return last
        if last["exchange"] not in (expected, "-"):
            raise RuntimeError(
                "foreign live stream became active while waiting for "
                f"{expected}: {reply.text}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f"live PCM did not quiesce before voicefetch: {reply.text}")
        await asyncio.sleep(min(0.05, remaining))


def _recording_path(reply_text: str, exchange_id: int) -> str:
    paths = list(dict.fromkeys(_PATH_RE.findall(reply_text)))
    if len(paths) != 1:
        raise RuntimeError(
            f"expected one finalized WAV path in stopid reply: {reply_text!r}")
    path = paths[0]
    expected_name = f"rec_{exchange_id:016x}.wav"
    if posixpath.basename(path) != expected_name:
        raise RuntimeError(
            f"WAV path {path!r} does not belong to exchange "
            f"{exchange_id:016x}")
    return path


def _validate_mic_status(status: object, expected_source: str) -> dict:
    if not isinstance(status, dict):
        raise RuntimeError("micread json did not return an object")
    problems: list[str] = []
    if status.get("source") != expected_source:
        problems.append(
            f"active source={status.get('source')!r}, expected={expected_source!r}")
    if status.get("sampleRate") != 16_000:
        problems.append(f"sampleRate={status.get('sampleRate')!r}")
    if status.get("bitDepth") != 16:
        problems.append(f"bitDepth={status.get('bitDepth')!r}")
    if status.get("channels") != 1:
        problems.append(f"channels={status.get('channels')!r}")
    if status.get("enabled") is not True or status.get("connected") is not True:
        problems.append("microphone is not enabled and connected")
    if status.get("recording") is not False:
        problems.append("recorder is already busy")
    state = status.get("recordingState")
    if state not in (None, "idle"):
        problems.append(f"recordingState={state!r}")
    if problems:
        raise RuntimeError("mic preflight failed: " + "; ".join(problems))
    return status


async def run_owned_probe(args) -> dict:
    cfg = config_mod.load(args.config)
    if cfg.link.baud < MIN_BAUD:
        raise RuntimeError(
            f"live-pcm-v1 requires link.baud >= {MIN_BAUD}; got {cfg.link.baud}")
    user, password = config_mod.read_credentials(cfg.link.credentials_file)
    controller_id = args.controller_id or _fresh_id()
    exchange_id = args.exchange_id or _fresh_id()
    controller_text = protocol.live_id_hex(controller_id)
    exchange_text = protocol.live_id_hex(exchange_id)
    fault = getattr(args, "fault", FAULT_NONE)
    fault_after_ms = getattr(args, "fault_after_ms", 250)
    queue_bytes = 1 if fault == FAULT_HOST_OVERFLOW else args.max_queue_bytes
    inbox = LivePcmInbox(
        controller_id,
        max_queue_bytes=queue_bytes,
        max_queue_frames=args.max_queue_frames,
    )
    sink = _FaultInjectingSink(inbox, fault)
    transport = SerialTransport(
        cfg.link.port, cfg.link.baud, frame_sink=sink)
    session = Session(transport, user, password)
    lease_stop = asyncio.Event()
    lease_errors: list[str] = []
    control_errors: list[str] = []
    renew_task: asyncio.Task | None = None
    collect_task: asyncio.Task | None = None
    fault_task: asyncio.Task | None = None
    fault_control: dict[str, object] = {"command": None, "reply": None}
    early_collection: tuple[bytes, LiveStreamTerminal, dict] | None = None
    lease_ttl_ms = 3000
    lease_acquired = False
    shadow_armed = False
    recording_started = False
    recording_stopped = False
    recording_attempted = False
    recording_cleanup_done = False
    started = time.monotonic()
    local_paths: dict[str, str] = {}
    try:
        transport.open()
        await session.login()

        capability_reply = await session.command(
            "liveaudio capabilities", expect="status", timeout=5.0)
        if not capability_reply.ok:
            raise RuntimeError(
                f"liveaudio capabilities rejected: {capability_reply.text}")
        try:
            capabilities = protocol.parse_live_capabilities(
                capability_reply.text)
        except ValueError as exc:
            raise RuntimeError(
                f"malformed liveaudio capabilities: {exc}: "
                f"{capability_reply.text}") from exc
        required = {"recorder_shadow": "1", "shadow_default": "off"}
        missing = {
            key: (value, capabilities.get(key))
            for key, value in required.items()
            if capabilities.get(key) != value
        }
        if missing:
            raise RuntimeError(
                f"recorder shadow capability unavailable: {missing}; "
                f"reply={capability_reply.text}")

        if (fault in (FAULT_HOST_ABORT, FAULT_LEASE_EXPIRE) and
                fault_after_ms >= args.record_seconds * 1000):
            raise RuntimeError(
                "fault-after-ms must fall inside the recording window")
        ready = await session.command(
            f"liveaudio ready 1 {controller_text}",
            expect="status", timeout=5.0, replay=True)
        if not ready.ok:
            raise RuntimeError(f"live ready rejected: {ready.text}")
        try:
            ready_fields = protocol.parse_live_ready(
                ready.text, expected_controller=controller_id)
            lease_timing = protocol.live_lease_timing_from_ready(ready_fields)
        except ValueError as exc:
            raise RuntimeError(
                f"invalid live ready contract: {exc}: {ready.text}") from exc
        # Fault-only accelerated fake firmware may expose a short marker-less
        # TTL. Renewal stays on the legacy cadence, but the injection window
        # must still use the grant's actual reported expiry.
        lease_ttl_ms = (ready_fields.lease_ttl_ms
                        if ready_fields.lease_ttl_ms is not None
                        else lease_timing.lease_ttl_ms)
        if fault == FAULT_LEASE_EXPIRE:
            required_ms = fault_after_ms + lease_ttl_ms + 100
            if args.record_seconds * 1000 < required_ms:
                raise RuntimeError(
                    "lease-expire recording window must extend at least 100ms "
                    f"past injection + lease TTL ({required_ms}ms required)")
        lease_acquired = True
        renew_task = asyncio.create_task(
            _renew_lease(
                session, controller_id, lease_stop, lease_errors,
                lease_timing, ready_fields.session_epoch),
            name="live-shadow-lease-renew")

        opened = await session.command(
            "openmic", expect="status", timeout=5.0, replay=True)
        if not opened.ok:
            raise RuntimeError(f"openmic failed: {opened.text}")
        mic_reply = await session.command(
            "micread json", expect="json", timeout=5.0, replay=True)
        # ``micread json`` is a status object, not the fileread-style
        # ``{"success": ...}`` envelope.  Reply.ok is intentionally false
        # for such bare JSON, so validate the parsed object itself below.
        mic_status = _validate_mic_status(
            mic_reply.json, args.expected_source)

        # The exact recorder arm is one-shot and has a short TTL.  Consume no
        # part of it on openmic/source preflight: arm only after the actual
        # source is proven, immediately before installing the collector and
        # admitting the exact startid write.
        arm = await session.command(
            f"liveaudio shadow 1 {controller_text} on {exchange_text}",
            expect="status", timeout=5.0, replay=False)
        if not arm.ok:
            raise RuntimeError(f"recorder shadow arm rejected: {arm.text}")
        shadow_armed = True

        collect_task = asyncio.create_task(
            asyncio.to_thread(
                _collect, inbox, exchange_id,
                SOURCE_CODES[args.expected_source], args.record_seconds),
            name="live-shadow-collector")
        if fault in (FAULT_HOST_ABORT, FAULT_LEASE_EXPIRE):
            fault_task = asyncio.create_task(
                _inject_control_fault(
                    session, sink, fault, fault_after_ms,
                    controller_text, exchange_text, lease_stop),
                name=f"live-shadow-fault-{fault}")
        # Once the non-replayable write is admitted, a timeout is ambiguous:
        # the recorder may be live even though no ACK reached us.  Mark the
        # exact fresh ID first so finally always closes and deletes only it.
        recording_attempted = True
        recording_started = True
        record = await session.command(
            f"micrecord startid {exchange_text}",
            expect="status", timeout=5.0, replay=False)
        if not record.ok:
            raise RuntimeError(f"owned recording start failed: {record.text}")
        log.warning(
            "SHADOW ONLY — SPEAK NOW for %.2fs; no STT/LLM/G2 reply is running",
            args.record_seconds)
        await asyncio.sleep(args.record_seconds)
        if fault_task is not None:
            fault_control = await fault_task
            fault_task = None
        if fault == FAULT_LEASE_EXPIRE:
            # Do not infer expiry from a fixed sleep. Observe the reason-1
            # terminal while the independent WAV recorder is still running,
            # then stop/finalize that recorder normally below.
            assert collect_task is not None
            early_collection = await asyncio.wait_for(
                collect_task, timeout=lease_ttl_ms / 1000.0 + 1.0)
            collect_task = None

        stop = await session.command(
            f"micrecord stopid {exchange_text}",
            expect="status", timeout=5.0, replay=False)
        if not stop.ok:
            raise RuntimeError(f"owned recording stop failed: {stop.text}")
        recording_stopped = True
        recording_started = False
        device_path = _recording_path(stop.text, exchange_id)

        live_pcm: bytes = b""
        terminal: LiveStreamTerminal | None = None
        stream_snapshot: dict = {}
        live_error: str | None = None
        try:
            if early_collection is not None:
                live_pcm, terminal, stream_snapshot = early_collection
            else:
                assert collect_task is not None
                live_pcm, terminal, stream_snapshot = await collect_task
                collect_task = None
        except Exception as exc:
            live_error = f"{type(exc).__name__}: {exc}"
            collect_task = None

        # Freeze every controller outcome before evaluating the gate.  A late
        # renewal/off/release failure must never coexist with ok=true.
        lease_stop.set()
        if renew_task is not None:
            await renew_task
            renew_task = None

        quiescence: dict[str, str] | None = None
        if fault in (FAULT_HOST_GAP, FAULT_HOST_OVERFLOW):
            # The host receiver has already failed closed, but the device must
            # still finish with END. Do not let cleanup turn this into a device
            # ABORT before recording fallback has been finalized.
            quiescence = await _wait_live_quiescent(session, exchange_id)

        if fault != FAULT_LEASE_EXPIRE:
            off = await session.command(
                f"liveaudio shadow 1 {controller_text} off",
                expect="status", timeout=5.0, replay=False)
            if off.ok:
                shadow_armed = False
            else:
                control_errors.append(f"shadow off rejected: {off.text}")

        release = await session.command(
            f"liveaudio release 1 {controller_text}",
            expect="status", timeout=5.0, replay=False)
        if release.ok:
            lease_acquired = False
            shadow_armed = False
        else:
            control_errors.append(f"lease release rejected: {release.text}")

        if quiescence is None:
            quiescence = await _wait_live_quiescent(session, exchange_id)

        wav_bytes = await fetch.fetch_frames(session, device_path)
        parsed = wav.parse(wav_bytes)
        wav.require_canonical(parsed)
        live_crc32 = protocol.crc32_ieee(live_pcm)
        wav_crc32 = protocol.crc32_ieee(parsed.pcm)
        pcm_equal = live_pcm == parsed.pcm
        receiver_matches_wav = bool(
            stream_snapshot.get("received_samples") == len(parsed.pcm) // 2 and
            stream_snapshot.get("pcm_crc32") == wav_crc32)
        terminal_ok = terminal is not None and terminal.valid
        terminal_matches_wav = bool(
            terminal is not None and
            terminal.total_samples == len(parsed.pcm) // 2 and
            terminal.pcm_crc32 == wav_crc32 and
            terminal.dropped_samples == 0)
        fallback_prefix_ok = parsed.pcm.startswith(live_pcm)
        terminal_prefix_matches_wav = bool(
            terminal is not None and
            terminal.total_samples * 2 <= len(parsed.pcm) and
            protocol.crc32_ieee(
                parsed.pcm[:terminal.total_samples * 2]) == terminal.pcm_crc32)
        status_fields = _status_tokens(quiescence["text"])
        device_end_matches_wav = bool(
            status_fields.get("last") == "end" and
            status_fields.get("last_exchange") == exchange_text and
            status_fields.get("last_sent") == str(len(parsed.pcm) // 2) and
            status_fields.get("last_dropped") == "0" and
            status_fields.get("last_crc32") == f"{wav_crc32:08x}" and
            status_fields.get("last_terminal") == "1")

        if fault == FAULT_NONE:
            expected_outcome = bool(
                terminal_ok and terminal_matches_wav and pcm_equal)
        elif fault == FAULT_HOST_OVERFLOW:
            expected_outcome = bool(
                terminal is not None and terminal.kind == "invalid" and
                terminal.reason == "pcm_queue_overflow" and
                device_end_matches_wav)
        elif fault == FAULT_HOST_GAP:
            expected_outcome = bool(
                terminal is not None and terminal.kind == "invalid" and
                terminal.reason == "wire_seq:3!=2" and
                device_end_matches_wav)
        elif fault == FAULT_HOST_ABORT:
            expected_outcome = bool(
                terminal is not None and terminal.kind == "abort" and
                terminal.reason == protocol.LIVE_ABORT_REASON_HOST_REQUEST and
                stream_snapshot.get("received_samples") == terminal.total_samples and
                stream_snapshot.get("pcm_crc32") == terminal.pcm_crc32 and
                terminal_prefix_matches_wav)
        elif fault == FAULT_LEASE_EXPIRE:
            expected_outcome = bool(
                terminal is not None and terminal.kind == "abort" and
                terminal.reason == protocol.LIVE_ABORT_REASON_LEASE_EXPIRED and
                stream_snapshot.get("received_samples") == terminal.total_samples and
                stream_snapshot.get("pcm_crc32") == terminal.pcm_crc32 and
                terminal_prefix_matches_wav)
        else:
            raise RuntimeError(f"unsupported fault mode {fault!r}")

        if args.output_dir:
            out_dir = Path(os.path.expanduser(args.output_dir)).resolve()
            out_dir.mkdir(parents=True, exist_ok=True)
            live_path = out_dir / f"live-{exchange_text}.pcm"
            wav_path = out_dir / f"recording-{exchange_text}.wav"
            live_path.write_bytes(live_pcm)
            wav_path.write_bytes(wav_bytes)
            local_paths = {"live_pcm": str(live_path), "wav": str(wav_path)}

        delete = await session.command(
            f"micdeleteid {exchange_text} "
            f"{protocol.quote_path(posixpath.basename(device_path))}",
            expect="status", timeout=5.0, replay=False)
        if delete.ok:
            recording_cleanup_done = True
        else:
            control_errors.append(f"exact cleanup rejected: {delete.text}")

        ok = bool(
            expected_outcome and sink.injected and fallback_prefix_ok and
            live_error is None and not lease_errors and not control_errors)
        if fault == FAULT_NONE:
            # Preserve the original happy-path contract exactly; no injection
            # marker is expected in default mode.
            ok = bool(
                expected_outcome and live_error is None and
                not lease_errors and not control_errors)
        result = {
            "schema": 1,
            "mode": "owned_recorder_shadow",
            "ok": ok,
            "stt_started": False,
            "controller_id": controller_text,
            "exchange_id": exchange_text,
            "expected_source": args.expected_source,
            "record_seconds": args.record_seconds,
            "fault": {
                "kind": fault,
                "injected": sink.injected,
                "after_ms": fault_after_ms,
                "command": fault_control.get("command"),
                "reply": fault_control.get("reply"),
                "expected_outcome": expected_outcome,
                "fallback_wav_prefix": fallback_prefix_ok,
                "terminal_prefix_matches_wav": terminal_prefix_matches_wav,
                "device_end_matches_wav": device_end_matches_wav,
            },
            "capabilities": capabilities,
            "mic": mic_status,
            "device_path": device_path,
            "live": {
                "error": live_error,
                "bytes": len(live_pcm),
                "samples": len(live_pcm) // 2,
                "crc32": f"{live_crc32:08x}",
                "terminal": ({
                    "kind": terminal.kind,
                    "valid": terminal.valid,
                    "reason": terminal.reason,
                    "total_samples": terminal.total_samples,
                    "crc32": f"{terminal.pcm_crc32:08x}",
                    "dropped_samples": terminal.dropped_samples,
                } if terminal is not None else None),
                "stream": stream_snapshot,
                "inbox": inbox.snapshot(),
            },
            "wav": {
                "bytes": len(wav_bytes),
                "pcm_bytes": len(parsed.pcm),
                "samples": len(parsed.pcm) // 2,
                "crc32": f"{wav_crc32:08x}",
                "canonical": True,
            },
            "parity": {
                "pcm_equal": pcm_equal,
                "receiver_matches_wav": receiver_matches_wav,
                "terminal_matches_wav": terminal_matches_wav,
            },
            "lease_errors": lease_errors,
            "control_errors": control_errors,
            "quiescence": quiescence,
            "local_paths": local_paths,
            "wall_seconds": time.monotonic() - started,
        }
        if args.output_dir:
            result_path = Path(args.output_dir).expanduser().resolve() / "result.json"
            result["local_paths"]["result"] = str(result_path)
            result_path.write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n",
                encoding="utf-8")
        return result
    finally:
        if fault_task is not None:
            fault_task.cancel()
            await asyncio.gather(fault_task, return_exceptions=True)
        if recording_started and not recording_stopped:
            await _best_effort_command(
                session, f"micrecord stopid {exchange_text}")
        lease_stop.set()
        if renew_task is not None:
            await renew_task
        if shadow_armed:
            await _best_effort_command(
                session, f"liveaudio shadow 1 {controller_text} off")
        if lease_acquired:
            await _best_effort_command(
                session, f"liveaudio release 1 {controller_text}")
        if recording_attempted and not recording_cleanup_done:
            await _best_effort_command(
                session,
                f"micdeleteid {exchange_text} "
                f"{protocol.quote_path(f'rec_{exchange_text}.wav')}")
        if collect_task is not None:
            collect_task.cancel()
            await asyncio.gather(collect_task, return_exceptions=True)
        transport.close()


async def run_native_probe(args, *, pcm_observer=None) -> dict:
    """Observe one firmware-owned Hey-Even capture outside production.

    The default ``native`` mode remains transport-only. ``native-stt`` passes a
    bounded observer whose worker consumes the same PCM concurrently; transport
    validation, WAV fallback, exact cleanup, and the no-LLM/no-delivery boundary
    remain unchanged.
    """
    cfg = config_mod.load(args.config)
    if getattr(args, "expected_source", "g2") != "g2":
        raise RuntimeError("native Hey-Even shadow smoke requires the G2 source")
    capture_timeout = getattr(args, "capture_timeout", None)
    if capture_timeout is None:
        capture_timeout = float(cfg.audio.vad_max_seconds)
    if not math.isfinite(capture_timeout) or not (1.0 <= capture_timeout <= 60.0):
        raise RuntimeError(
            "configured audio.vad_max_seconds must be between 1 and 60 seconds")
    if cfg.link.baud < MIN_BAUD:
        raise RuntimeError(
            f"live-pcm-v1 requires link.baud >= {MIN_BAUD}; got {cfg.link.baud}")
    user, password = config_mod.read_credentials(cfg.link.credentials_file)
    controller_id = args.controller_id or _fresh_id()
    controller_text = protocol.live_id_hex(controller_id)
    inbox = LivePcmInbox(
        controller_id,
        max_queue_bytes=args.max_queue_bytes,
        max_queue_frames=args.max_queue_frames,
    )
    # The native exchange ID does not exist yet. The inbox must nevertheless
    # own LIVE frame dispatch before UART open so BEGIN cannot race admission.
    transport = SerialTransport(
        cfg.link.port, cfg.link.baud, frame_sink=inbox)
    session = Session(transport, user, password)
    started_ns = time.monotonic_ns()
    started = time.monotonic()
    events = _NativeEventRecorder(started_ns)
    session.on_event = events

    lease_stop = asyncio.Event()
    lease_errors: list[str] = []
    control_errors: list[str] = []
    cleanup_order: list[str] = []
    renew_task: asyncio.Task | None = None
    pump_task: asyncio.Task | None = None
    collect_task: asyncio.Task | None = None
    collector: _NativeStreamCollector | None = None
    lease_acquired = False
    shadow_armed = False
    exchange_text: str | None = None
    begin_exchange_text: str | None = None
    device_path: str | None = None
    exact_file_deleted = False
    native_exited = False
    success = False
    local_paths: dict[str, str] = {}
    native_idle_preflight = False
    ready_session_epoch: int | None = None
    streaming_stt: dict | None = None

    try:
        transport.open()
        await session.login()
        pump_task = asyncio.create_task(
            session.pump_events(), name="native-shadow-event-pump")

        capability_reply = await session.command(
            "liveaudio capabilities", expect="status", timeout=5.0)
        if not capability_reply.ok:
            raise RuntimeError(
                f"liveaudio capabilities rejected: {capability_reply.text}")
        try:
            capabilities = protocol.parse_live_capabilities(
                capability_reply.text)
        except ValueError as exc:
            raise RuntimeError(
                f"malformed liveaudio capabilities: {exc}: "
                f"{capability_reply.text}") from exc
        required = {"recorder_shadow": "1", "shadow_default": "off"}
        missing = {
            key: (value, capabilities.get(key))
            for key, value in required.items()
            if capabilities.get(key) != value
        }
        if missing:
            raise RuntimeError(
                f"recorder shadow capability unavailable: {missing}; "
                f"reply={capability_reply.text}")

        pre_live_reply = await session.command(
            "liveaudio status", expect="status", timeout=5.0, replay=True)
        if not pre_live_reply.ok:
            raise RuntimeError(
                f"liveaudio preflight status rejected: {pre_live_reply.text}")
        pre_live_fields = _status_tokens(pre_live_reply.text)
        if (pre_live_fields.get("active") != "0" or
                pre_live_fields.get("exchange") != "-" or
                pre_live_fields.get("bulk") != "0"):
            raise RuntimeError(
                "liveaudio preflight is not idle/bulk-free: "
                f"{pre_live_reply.text}")

        pre_native_reply = await session.command(
            "g2evenai status", expect="status", timeout=5.0, replay=False)
        if not pre_native_reply.ok:
            raise RuntimeError(
                f"g2evenai preflight rejected: {pre_native_reply.text}")
        pre_native_status = _parse_evenai_status(pre_native_reply.text)
        if pre_native_status["state"] != "idle":
            raise RuntimeError(
                "native EvenAI session already active; dismiss it before probe")
        native_idle_preflight = True

        ready = await session.command(
            f"liveaudio ready 1 {controller_text}",
            expect="status", timeout=5.0, replay=True)
        if not ready.ok:
            raise RuntimeError(f"live ready rejected: {ready.text}")
        try:
            ready_fields = protocol.parse_live_ready(
                ready.text, expected_controller=controller_id)
            lease_timing = protocol.live_lease_timing_from_ready(ready_fields)
        except ValueError as exc:
            raise RuntimeError(
                f"invalid live ready contract: {exc}: {ready.text}") from exc
        ready_session_epoch = ready_fields.session_epoch
        lease_acquired = True
        renew_task = asyncio.create_task(
            _renew_lease(
                session, controller_id, lease_stop, lease_errors,
                lease_timing, ready_session_epoch),
            name="native-shadow-lease-renew")

        opened = await session.command(
            "openmic", expect="status", timeout=5.0, replay=True)
        if not opened.ok:
            raise RuntimeError(f"openmic failed: {opened.text}")
        mic_reply = await session.command(
            "micread json", expect="json", timeout=5.0, replay=True)
        mic_status = _validate_mic_status(
            mic_reply.json, args.expected_source)

        # Establish the post-preflight observation epoch before the arm write;
        # a wake routed while its ACK drains must remain visible.
        events.arm()
        arm = await session.command(
            f"liveaudio shadow 1 {controller_text} on native",
            expect="status", timeout=5.0, replay=False)
        if not arm.ok:
            raise RuntimeError(f"native recorder shadow arm rejected: {arm.text}")
        shadow_armed = True

        collector = _NativeStreamCollector(
            inbox, SOURCE_CODES[args.expected_source],
            args.wake_timeout, capture_timeout, pcm_observer=pcm_observer)
        collect_task = asyncio.create_task(
            asyncio.to_thread(collector.run), name="native-shadow-collector")
        wake_task = asyncio.create_task(
            _await_native_wake(events, args.wake_timeout),
            name="native-shadow-wake")
        begin_task = asyncio.create_task(
            _await_native_begin(collector, args.wake_timeout),
            name="native-shadow-begin")
        identity_tasks = (wake_task, begin_task)
        if pcm_observer is None:
            log.warning(
                "NATIVE SHADOW ONLY — SAY 'HEY EVEN', ask one question, then "
                "stay silent; no STT/LLM/ASK/REPLY is running")
        else:
            log.warning(
                "LIVE STT SHADOW — SAY 'HEY EVEN', ask exactly %r, then stay "
                "silent; no LLM/ASK/REPLY is running",
                getattr(args, "expected_text", ""))
        wake: evenai_wire.WakeEvent | None = None
        wake_entry: dict[str, object] | None = None
        begin: dict[str, object] | None = None
        pending: set[asyncio.Task] = {wake_task, begin_task}
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task is wake_task:
                        wake, wake_entry = task.result()
                        exchange_text = wake.exchange_id
                        events.bind(exchange_text)
                    else:
                        begin = task.result()
                        begin_exchange_text = str(begin["exchange_id"])
                        events.bind(begin_exchange_text)
        except BaseException:
            # A single wait can return both tasks in `done`. If the first
            # `.result()` raises, the sibling exception must still be
            # retrieved or asyncio reports an unhandled task after cleanup.
            for task in identity_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*identity_tasks, return_exceptions=True)
            raise

        assert wake is not None and wake_entry is not None and begin is not None
        assert exchange_text is not None and begin_exchange_text is not None
        if events.cancel_reason is not None:
            raise RuntimeError(
                f"native wake {exchange_text} was already cancelled: "
                f"{events.cancel_reason}")
        if begin_exchange_text != exchange_text:
            raise RuntimeError(
                "LIVE_BEGIN exchange does not match evenai_wake: "
                f"{begin_exchange_text} != {exchange_text}")
        if begin["controller_id"] != controller_text:
            raise RuntimeError(
                "LIVE_BEGIN controller does not match native lease")

        active_native_reply = await session.command(
            "g2evenai status", expect="status", timeout=5.0, replay=False)
        if not active_native_reply.ok:
            raise RuntimeError(
                f"g2evenai active status rejected: {active_native_reply.text}")
        active_native_status = _parse_evenai_status(active_native_reply.text)
        if (active_native_status["state"] != "active" or
                active_native_status["exchange_id"] != exchange_text or
                active_native_status["uart_epoch"] != ready_session_epoch):
            raise RuntimeError(
                "active native status does not match wake/login epoch: "
                f"{active_native_reply.text}")

        autostop, autostop_entry, status_history = await _await_native_autostop(
            session, events, exchange_text, capture_timeout)
        device_path = _recording_path(autostop.path, int(exchange_text, 16))
        if events.cancel_reason is not None:
            raise RuntimeError(
                f"native exchange cancelled before final status: "
                f"{events.cancel_reason}")
        final_record_reply = await session.command(
            evenai_wire.mic_status_command(exchange_text),
            expect="status", timeout=5.0, replay=False)
        if not final_record_reply.ok:
            raise RuntimeError(
                f"final native recording status rejected: "
                f"{final_record_reply.text}")
        status_path = _recording_path(
            final_record_reply.text, int(exchange_text, 16))
        if status_path != device_path:
            raise RuntimeError(
                "mic_autostop path does not match exact statusid path")

        assert collect_task is not None
        live_pcm, terminal, stream_snapshot = await asyncio.wait_for(
            collect_task, timeout=5.0)
        collect_task = None
        terminal_ok = bool(
            terminal.kind == "end" and terminal.valid and
            terminal.reason == protocol.LIVE_END_REASON_OK and
            terminal.dropped_samples == 0 and
            terminal.total_samples > 0 and len(live_pcm) > 0 and
            stream_snapshot.get("received_samples") == terminal.total_samples and
            stream_snapshot.get("pcm_crc32") == terminal.pcm_crc32 and
            len(live_pcm) == terminal.total_samples * 2 and
            protocol.crc32_ieee(live_pcm) == terminal.pcm_crc32)
        if not terminal_ok:
            raise RuntimeError(
                "native live stream did not end with exact count/CRC parity")

        if pcm_observer is not None:
            final_timeout = float(getattr(args, "stt_final_timeout", 2.0))
            streaming_stt = await asyncio.to_thread(
                pcm_observer.wait, final_timeout)
            expected_text = str(getattr(args, "expected_text", "") or "")
            text = str(streaming_stt.get("text", "") or "")
            errors = _word_errors(expected_text, text)
            # Leading-artifact tolerance (2026-08-11). STRUCTURAL rule, not a
            # word blacklist: the since-wake preroll can carry a wake-phrase
            # tail that Moonshine renders as its own short LEADING stop-line
            # (observed: line1="then" complete, line2=the exact question).
            # Tolerated only when dropping exactly the FIRST stop-transcript
            # line yields an exact match — any other insertion still fails.
            stop_lines = streaming_stt.get("stream", {}).get("stop_lines") or []
            leading_artifact = None
            exact_ignoring_leading = errors == 0
            if errors and len(stop_lines) >= 2:
                rest = " ".join(
                    str(ln.get("text", "") or "") for ln in stop_lines[1:])
                if _word_errors(expected_text, rest) == 0:
                    exact_ignoring_leading = True
                    leading_artifact = str(stop_lines[0].get("text", "") or "")
            soft_target = float(getattr(args, "stt_soft_final_target", 0.8))
            end_to_final = streaming_stt.get("stream", {}).get(
                "end_to_final_seconds")
            streaming_stt["accuracy"] = {
                "expected_text": expected_text,
                "reference_words": len(_words(expected_text)),
                "hypothesis_words": len(_words(text)),
                "word_errors": errors,
                "exact_words": errors == 0,
                "exact_words_ignoring_leading_line": exact_ignoring_leading,
                "leading_artifact_line": leading_artifact,
            }
            streaming_stt["final_policy"] = {
                "soft_target_seconds": soft_target,
                "hard_timeout_seconds": final_timeout,
                "soft_target_met": bool(
                    isinstance(end_to_final, (int, float)) and
                    end_to_final <= soft_target),
            }

        quiescence = await _wait_live_quiescent(
            session, int(exchange_text, 16))
        inbox_snapshot = inbox.snapshot()
        inbox_clean = bool(
            inbox_snapshot.get("fault_count") == 0 and
            inbox_snapshot.get("late_frame_count") == 0 and
            inbox_snapshot.get("last_fault") is None)
        if not inbox_clean:
            raise RuntimeError(
                "native live inbox recorded a pre-BEGIN, malformed, or late "
                f"frame fault: {inbox_snapshot}")
        status_fields = _status_tokens(quiescence["text"])
        live_status_ok = bool(
            status_fields.get("last") == "end" and
            status_fields.get("last_exchange") == exchange_text and
            status_fields.get("last_sent") == str(terminal.total_samples) and
            status_fields.get("last_dropped") == "0" and
            status_fields.get("last_crc32") == f"{terminal.pcm_crc32:08x}" and
            status_fields.get("last_terminal") == "1")
        if not live_status_ok:
            raise RuntimeError(
                "native liveaudio status does not match terminal identity/count/CRC")

        # End every short-lease obligation before voicefetch monopolizes the
        # framed lane. The native card and exact recorder result remain valid.
        lease_stop.set()
        if renew_task is not None:
            await renew_task
            renew_task = None
        if lease_errors:
            raise RuntimeError(lease_errors[0])
        off = await session.command(
            f"liveaudio shadow 1 {controller_text} off",
            expect="status", timeout=5.0, replay=False)
        cleanup_order.append("shadow_off")
        if not off.ok:
            control_errors.append(f"shadow off rejected: {off.text}")
        else:
            shadow_armed = False
        release = await session.command(
            f"liveaudio release 1 {controller_text}",
            expect="status", timeout=5.0, replay=False)
        cleanup_order.append("lease_release")
        if not release.ok:
            control_errors.append(f"lease release rejected: {release.text}")
        else:
            lease_acquired = False
        if control_errors:
            raise RuntimeError(control_errors[0])

        wav_bytes = await fetch.fetch_frames(
            session, device_path, cancel_guard=lambda: events.cancelled)
        cleanup_order.append("voicefetch")
        parsed = wav.parse(wav_bytes)
        wav.require_canonical(parsed)
        if not parsed.pcm:
            raise RuntimeError("native canonical WAV contains no PCM")
        if events.errors:
            raise RuntimeError(events.errors[0])
        if events.cancel_reason is not None:
            raise RuntimeError(
                f"native exchange cancelled during WAV fetch: "
                f"{events.cancel_reason}")

        if args.output_dir:
            out_dir = Path(os.path.expanduser(args.output_dir)).resolve()
            out_dir.mkdir(parents=True, exist_ok=True)
            live_path = out_dir / f"live-{exchange_text}.pcm"
            wav_path = out_dir / f"recording-{exchange_text}.wav"
            live_path.write_bytes(live_pcm)
            wav_path.write_bytes(wav_bytes)
            local_paths = {"live_pcm": str(live_path), "wav": str(wav_path)}

        delete = await session.command(
            evenai_wire.mic_delete_command(
                exchange_text, posixpath.basename(device_path)),
            expect="status", timeout=5.0, replay=False)
        cleanup_order.append("micdeleteid")
        if not delete.ok:
            raise RuntimeError(f"exact native cleanup rejected: {delete.text}")
        exact_file_deleted = True

        # Firmware publishes/retries `evenai_cancel <id> host_exit` as part of
        # this exact terminal command, potentially before its text reply.
        events.begin_cleanup_exit(exchange_text)
        exit_reply = await session.command(
            evenai_wire.exit_command(exchange_text),
            expect="status", timeout=5.0, replay=False)
        cleanup_order.append("g2evenai_exitid")
        if not exit_reply.ok:
            raise RuntimeError(f"exact native EXIT rejected: {exit_reply.text}")
        native_exited = True
        idle_native_reply = await session.command(
            "g2evenai status", expect="status", timeout=5.0, replay=False)
        if not idle_native_reply.ok:
            raise RuntimeError(
                f"post-exit g2evenai status rejected: {idle_native_reply.text}")
        idle_native_status = _parse_evenai_status(idle_native_reply.text)
        if (idle_native_status["state"] != "idle" or
                idle_native_status["exchange_id"] != "-"):
            raise RuntimeError(
                f"native exchange remained active after exact EXIT: "
                f"{idle_native_reply.text}")
        if events.errors:
            raise RuntimeError(events.errors[0])
        if events.cancel_reason is not None:
            raise RuntimeError(
                f"unexpected native cancellation: {events.cancel_reason}")

        stt_gate_ok = bool(
            pcm_observer is None or
            (streaming_stt is not None and streaming_stt.get("valid") and
             (streaming_stt.get("accuracy", {}).get("exact_words") or
              streaming_stt.get("accuracy", {}).get(
                  "exact_words_ignoring_leading_line"))))
        result = {
            "schema": 1,
            "mode": ("native_live_stt_shadow" if pcm_observer is not None
                     else "native_recorder_shadow_smoke"),
            "ok": stt_gate_ok,
            "stt_started": pcm_observer is not None,
            "llm_started": False,
            "ask_sent": False,
            "reply_sent": False,
            "controller_id": controller_text,
            "exchange_id": exchange_text,
            "expected_source": args.expected_source,
            "capabilities": capabilities,
            "session_epoch": ready_session_epoch,
            "pre_live_status": pre_live_reply.text,
            "mic": mic_status,
            "begin": {
                key: value for key, value in begin.items()
                if key != "received_ns"
            },
            "native": {
                "preflight": pre_native_status,
                "active": active_native_status,
                "idle_after_exit": idle_native_status,
                "wake": wake_entry["text"],
                "mic_autostop": autostop.path,
                "status_history": status_history,
                "final_record_status": final_record_reply.text,
                "events": events.public_observations(),
            },
            "timing": {
                "live_begin_ms": (
                    int(begin["received_ns"]) - started_ns) / 1_000_000.0,
                "evenai_wake_ms": wake_entry["at_ms"],
                "live_terminal_ms": (
                    terminal.received_ns - started_ns) / 1_000_000.0,
                "mic_autostop_ms": autostop_entry["at_ms"],
            },
            "device_path": device_path,
            "live": {
                "bytes": len(live_pcm),
                "samples": len(live_pcm) // 2,
                "crc32": f"{protocol.crc32_ieee(live_pcm):08x}",
                "terminal": {
                    "kind": terminal.kind,
                    "valid": terminal.valid,
                    "reason": terminal.reason,
                    "total_samples": terminal.total_samples,
                    "crc32": f"{terminal.pcm_crc32:08x}",
                    "dropped_samples": terminal.dropped_samples,
                },
                "stream": stream_snapshot,
                "inbox": inbox_snapshot,
                "status_matches_terminal": live_status_ok,
            },
            "wav": {
                "bytes": len(wav_bytes),
                "pcm_bytes": len(parsed.pcm),
                "samples": len(parsed.pcm) // 2,
                "crc32": f"{protocol.crc32_ieee(parsed.pcm):08x}",
                "canonical": True,
            },
            "parity": {
                "applicable": False,
                "reason": "native_capture_trim_enabled",
                "pcm_equal": None,
            },
            "streaming_stt": streaming_stt,
            "quiescence": quiescence,
            "lease_errors": lease_errors,
            "control_errors": control_errors,
            "cleanup_order": cleanup_order,
            "cleanup": {
                "wav_deleted": exact_file_deleted,
                "evenai_exited": native_exited,
                "shadow_disarmed": not shadow_armed,
                "lease_released": not lease_acquired,
            },
            "local_paths": local_paths,
            "wall_seconds": (time.monotonic_ns() - started_ns) / 1_000_000_000.0,
        }
        if args.output_dir:
            result_path = Path(args.output_dir).expanduser().resolve() / "result.json"
            result["local_paths"]["result"] = str(result_path)
            result_path.write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n",
                encoding="utf-8")
        # Exact native cleanup succeeded even if the isolated model missed its
        # transcript gate; do not discard an already-clean exchange twice.
        success = True
        return result
    finally:
        lease_stop.set()
        if renew_task is not None:
            await renew_task
        cleanup_candidates = events.armed_wake_ids()
        for candidate in (exchange_text, begin_exchange_text):
            if candidate is not None:
                cleanup_candidates.add(candidate)
        if collector is not None and collector.begin is not None:
            cleanup_candidates.add(str(collector.begin["exchange_id"]))
        cleanup_exchange = exchange_text
        if cleanup_exchange is None and collector is not None and \
                collector.begin is not None:
            cleanup_exchange = str(collector.begin["exchange_id"])
        should_exit = False
        if (native_idle_preflight and ready_session_epoch is not None and
                not native_exited):
            try:
                cleanup_status_reply = await session.command(
                    "g2evenai status", expect="status", timeout=5.0,
                    replay=False)
                if cleanup_status_reply.ok:
                    cleanup_status = _parse_evenai_status(
                        cleanup_status_reply.text)
                    if cleanup_status["state"] == "active":
                        status_exchange = str(cleanup_status["exchange_id"])
                        status_epoch = int(cleanup_status["uart_epoch"])
                        if status_epoch != ready_session_epoch:
                            log.warning(
                                "native cleanup refuses active epoch %s "
                                "(ready epoch %s)", status_epoch,
                                ready_session_epoch)
                        elif (cleanup_candidates and
                              status_exchange not in cleanup_candidates):
                            log.warning(
                                "native cleanup refuses foreign active exchange %s "
                                "(candidates %s)", status_exchange,
                                sorted(cleanup_candidates))
                        else:
                            # This is also the safe recovery path when both
                            # Wake and LIVE_BEGIN observations were lost: the
                            # idle preflight plus exact ready login epoch makes
                            # the active firmware identity authoritative for
                            # cleanup only.
                            cleanup_exchange = status_exchange
                            cleanup_candidates.add(status_exchange)
                            should_exit = True
                    elif cleanup_exchange is not None:
                        log.warning(
                            "native cleanup candidate %s is already idle",
                            cleanup_exchange)
            except BaseException as exc:
                log.warning("native cleanup status unavailable: %s", exc)
        if cleanup_exchange is not None and not native_exited and should_exit:
            if events.bound_exchange == cleanup_exchange:
                events.begin_cleanup_exit(cleanup_exchange)
            await _best_effort_command(
                session, evenai_wire.exit_command(cleanup_exchange))
        if not success:
            for cleanup_recording in sorted(cleanup_candidates):
                await _best_effort_command(
                    session, f"micrecord stopid {cleanup_recording} discard")
        if shadow_armed:
            await _best_effort_command(
                session, f"liveaudio shadow 1 {controller_text} off")
        if lease_acquired:
            await _best_effort_command(
                session, f"liveaudio release 1 {controller_text}")
        if (exchange_text is not None and device_path is not None and
                not exact_file_deleted):
            await _best_effort_command(
                session, evenai_wire.mic_delete_command(
                    exchange_text, posixpath.basename(device_path)))
        if collect_task is not None:
            collect_task.cancel()
            await asyncio.gather(collect_task, return_exceptions=True)
        if pcm_observer is not None and streaming_stt is None:
            pcm_observer.abort("native_probe_cleanup")
            await asyncio.to_thread(pcm_observer.wait, 2.0)
        if pump_task is not None:
            pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)
        transport.close()


async def run_native_stt_probe(args) -> dict:
    """Run one exact native capture through isolated live Moonshine STT."""
    governors = performance_governors()
    if not args.allow_non_performance and governors != ["performance"]:
        raise RuntimeError(
            "live STT gate requires every CPU governor to be performance; "
            f"observed {governors or ['unknown']}")
    if not _words(args.expected_text):
        raise RuntimeError("native-stt --expected-text must contain speech words")
    if args.stt_final_timeout < args.stt_soft_final_target:
        raise RuntimeError(
            "hard STT final timeout cannot be shorter than the soft target")

    factory = exact_moonshine_factory(args.model_dir, args.model_arch)
    worker = LiveMoonshineWorker(
        factory,
        update_interval_s=args.update_interval,
        queue_chunks=args.stt_queue_chunks,
        text_queue_events=args.stt_text_queue_events,
    )
    await asyncio.to_thread(worker.start, args.model_startup_timeout)
    return await run_native_probe(args, pcm_observer=worker)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Validate recorder-shadow transport, or opt into isolated "
                     "live-STT shadow; never starts LLM or production delivery"))
    parser.add_argument("-c", "--config", default=None,
                        help="hw1-ai-service config YAML")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="mode", required=True)
    owned = sub.add_parser(
        "owned", help="host-owned untrimmed recording with exact PCM/WAV parity")
    owned.add_argument("--expected-source", choices=tuple(SOURCE_CODES), required=True)
    owned.add_argument("--record-seconds", type=_record_seconds_arg, default=6.0)
    owned.add_argument("--controller-id", type=_id_arg, default=None)
    owned.add_argument("--exchange-id", type=_id_arg, default=None)
    owned.add_argument("--max-queue-bytes", type=_positive_int,
                       default=DEFAULT_PCM_QUEUE_BYTES)
    owned.add_argument("--max-queue-frames", type=_positive_int,
                       default=DEFAULT_PCM_QUEUE_FRAMES)
    owned.add_argument(
        "--fault", choices=FAULT_CHOICES, default=FAULT_NONE,
        help=("expected fault gate (default: none); host-overflow and host-gap "
              "fault only the bounded Pi receiver, host-abort and lease-expire "
              "exercise firmware ABORT while retaining the WAV"))
    owned.add_argument(
        "--fault-after-ms", type=_nonnegative_int, default=250,
        help="inject control faults this long after first PCM (default: 250)")
    owned.add_argument("--output-dir", default=None,
                       help="optional directory for live PCM, WAV, and result JSON")
    native = sub.add_parser(
        "native", help=("observe one real Hey-Even wake with native trimmed "
                        "recording; never starts STT/LLM/ASK/REPLY"))
    native.set_defaults(expected_source="g2")
    native.add_argument("--controller-id", type=_id_arg, default=None)
    native.add_argument("--wake-timeout", type=_native_timeout_arg, default=30.0,
                        help="seconds to say Hey Even after the prompt (default: 30)")
    native.add_argument(
        "--capture-timeout", type=_native_timeout_arg, default=None,
        help=("seconds for question plus native VAD finalization "
              "(default: config audio.vad_max_seconds)"))
    native.add_argument("--max-queue-bytes", type=_positive_int,
                        default=DEFAULT_PCM_QUEUE_BYTES)
    native.add_argument("--max-queue-frames", type=_positive_int,
                        default=DEFAULT_PCM_QUEUE_FRAMES)
    native.add_argument("--output-dir", default=None,
                        help="optional directory for live PCM, WAV, and result JSON")
    native_stt = sub.add_parser(
        "native-stt", help=("one real Hey-Even capture through bounded live "
                            "Moonshine; no LLM/ASK/REPLY"))
    native_stt.set_defaults(expected_source="g2")
    native_stt.add_argument("--controller-id", type=_id_arg, default=None)
    native_stt.add_argument(
        "--wake-timeout", type=_native_timeout_arg, default=30.0,
        help="seconds to say Hey Even after the prompt (default: 30)")
    native_stt.add_argument(
        "--capture-timeout", type=_native_timeout_arg, default=None,
        help=("seconds for question plus native VAD finalization "
              "(default: config audio.vad_max_seconds)"))
    native_stt.add_argument("--max-queue-bytes", type=_positive_int,
                            default=DEFAULT_PCM_QUEUE_BYTES)
    native_stt.add_argument("--max-queue-frames", type=_positive_int,
                            default=DEFAULT_PCM_QUEUE_FRAMES)
    native_stt.add_argument("--model-dir", required=True,
                            help="exact downloaded Moonshine model directory")
    native_stt.add_argument(
        "--model-arch", default="medium-streaming",
        choices=("tiny-streaming", "small-streaming", "medium-streaming"))
    native_stt.add_argument(
        "--update-interval", type=_positive_float, default=1.0,
        help="Moonshine update floor in seconds (default: 1.0)")
    native_stt.add_argument(
        "--stt-queue-chunks", type=_positive_int,
        default=DEFAULT_STT_QUEUE_CHUNKS,
        help=("bounded 4096-byte PCM FIFO depth "
              f"(default: {DEFAULT_STT_QUEUE_CHUNKS} / 1.024 s)"))
    native_stt.add_argument(
        "--stt-text-queue-events", type=_positive_int,
        default=DEFAULT_STT_TEXT_QUEUE_EVENTS)
    native_stt.add_argument(
        "--stt-soft-final-target", type=_positive_float, default=0.8,
        help="telemetry target after END, not a kill switch (default: 0.8)")
    native_stt.add_argument(
        "--stt-final-timeout", type=_positive_float, default=2.0,
        help="hard wait for a final result after END (default: 2.0)")
    native_stt.add_argument(
        "--model-startup-timeout", type=_positive_float, default=120.0)
    native_stt.add_argument(
        "--expected-text", required=True,
        help="exact words to ask after Hey Even (punctuation/case ignored)")
    native_stt.add_argument(
        "--allow-non-performance", action="store_true",
        help="diagnostic only; canonical gate requires performance governors")
    native_stt.add_argument(
        "--output-dir", required=True,
        help="directory for live PCM, WAV, and result JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_mod.setup(args.verbose)
    try:
        if args.mode == "owned":
            result = asyncio.run(run_owned_probe(args))
        elif args.mode == "native":
            result = asyncio.run(run_native_probe(args))
        elif args.mode == "native-stt":
            result = asyncio.run(run_native_stt_probe(args))
        else:
            raise RuntimeError(f"unsupported shadow mode {args.mode!r}")
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(json.dumps({
            "schema": 1,
            "mode": getattr(args, "mode", None),
            "ok": False,
            "stt_started": getattr(args, "mode", None) == "native-stt",
            "error": f"{type(exc).__name__}: {exc}",
        }, separators=(",", ":")))
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
