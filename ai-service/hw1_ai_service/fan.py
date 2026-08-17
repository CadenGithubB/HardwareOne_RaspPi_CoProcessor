"""Finite CM5 fan control driven by authenticated HardwareOne EVT frames.

The XIAO selects one of three product modes; it never supplies a path, shell
fragment, PWM value, or temperature threshold.  A separately installed,
root-owned fan controller owns sysfs discovery, the temperature curve, and all
safety overrides.  This module is only the unprivileged UART/socket bridge.

Firmware -> CM5 EVT payloads (protocol v1)::

    cm5_fan_status 1 <16-hex-id>
    cm5_fan_mode_auto 1 <16-hex-id>
    cm5_fan_mode_quiet 1 <16-hex-id>
    cm5_fan_mode_max 1 <16-hex-id>

CM5 -> firmware authenticated commands::

    cm5 fan ack 1 <id> <accepted|applied|failed>
    cm5 fan report 1 <id> <requested> <effective> <temp_mc|-1>
                   <target_pwm> <pwm> <rpm|-1> <health>

The local privileged boundary is an AF_UNIX socket with an even smaller
grammar: ``status`` or ``mode auto|quiet|max``.  There is no shell invocation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Protocol

from .link.session import LinkClosed, Reply

log = logging.getLogger("fan")

PROTOCOL_VERSION = "1"
_REQUEST_ID_RE = re.compile(r"^[0-9a-fA-F]{16}$")
_RESULT_CODE_RE = re.compile(r"^[a-z0-9_:-]{1,80}$")
_CALLBACK_RETRY_DELAYS_S = (1.0, 2.0, 4.0, 8.0, 16.0)
_MAX_SOCKET_REPLY_BYTES = 4096
FAN_CONTROLLER_SOCKET_PATH = "/run/hw1-fan-controller/control.sock"


class FanMode(StrEnum):
    AUTO = "auto"
    QUIET = "quiet"
    MAX = "max"


class FanHealth(StrEnum):
    OK = "ok"
    BOOSTING = "boosting"
    TACH_UNAVAILABLE = "tach_unavailable"
    SAFETY_TEMP = "safety_temp"
    SAFETY_STALL = "safety_stall"
    UNAVAILABLE = "unavailable"
    IO_ERROR = "io_error"


class FanAction(StrEnum):
    STATUS = "status"
    MODE = "mode"


class FanAck(StrEnum):
    ACCEPTED = "accepted"
    APPLIED = "applied"
    FAILED = "failed"


@dataclass(frozen=True)
class FanRequest:
    request_id: str
    action: FanAction
    mode: FanMode | None = None


@dataclass(frozen=True)
class FanStatus:
    requested_mode: FanMode
    effective_mode: FanMode
    temp_mc: int | None
    target_pwm: int
    pwm: int
    rpm: int | None
    health: FanHealth


@dataclass(frozen=True)
class FanServiceResult:
    ok: bool
    code: str
    status: FanStatus | None = None


class FanProtocolError(ValueError):
    """A recognized ``cm5_fan_`` event is malformed."""

    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id


class FanServiceError(RuntimeError):
    """The local root fan service could not return a valid bounded reply."""


def _canonical_request_id(token: str) -> str:
    if not _REQUEST_ID_RE.fullmatch(token):
        raise FanProtocolError(
            "request ID must be exactly 16 hexadecimal characters")
    if int(token[:8], 16) == 0 or int(token[8:], 16) == 0:
        raise FanProtocolError("request ID halves must both be nonzero")
    return token.lower()


_EVENTS: dict[str, tuple[FanAction, FanMode | None]] = {
    "cm5_fan_status": (FanAction.STATUS, None),
    "cm5_fan_mode_auto": (FanAction.MODE, FanMode.AUTO),
    "cm5_fan_mode_quiet": (FanAction.MODE, FanMode.QUIET),
    "cm5_fan_mode_max": (FanAction.MODE, FanMode.MAX),
}


def parse_fan_event(payload: bytes) -> FanRequest | None:
    """Strictly parse one fan EVT, or return ``None`` for another subsystem."""
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError:
        if payload.startswith(b"cm5_fan_"):
            raise FanProtocolError("host-fan event is not ASCII")
        return None

    if not text.startswith("cm5_fan_"):
        if text.lstrip(" \t\r\n").startswith("cm5_fan_"):
            raise FanProtocolError("host-fan event has non-canonical whitespace")
        return None
    tokens = text.split(" ")

    request_id: str | None = None
    if len(tokens) >= 3:
        try:
            request_id = _canonical_request_id(tokens[2])
        except FanProtocolError:
            request_id = None

    if (len(tokens) != 3 or any(not token for token in tokens) or
            any(char in text for char in "\t\r\n")):
        raise FanProtocolError(
            "host-fan event has non-canonical whitespace or arity",
            request_id=request_id,
        )
    if len(tokens) < 2 or tokens[1] != PROTOCOL_VERSION:
        raise FanProtocolError("missing or unsupported host-fan protocol version")

    event = _EVENTS.get(tokens[0])
    if event is None:
        raise FanProtocolError("unknown host-fan event", request_id=request_id)
    if request_id is None:
        _canonical_request_id(tokens[2])
        raise AssertionError("unreachable")
    action, mode = event
    return FanRequest(request_id, action, mode)


class SessionLike(Protocol):
    async def command(self, line: str, *, timeout: float, expect: str,
                      replay: bool = True,
                      auth_replay: bool = True) -> Reply: ...


class FanService(Protocol):
    async def request(self, action: FanAction, *,
                      mode: FanMode | None = None) -> FanServiceResult: ...


class FanServiceClient:
    """Bounded client for the root controller's finite Unix-socket grammar."""

    def __init__(self, socket_path: str, *, timeout_s: float = 5.0) -> None:
        expanded = os.path.expanduser(socket_path)
        if expanded != FAN_CONTROLLER_SOCKET_PATH:
            raise ValueError(
                f"fan controller socket path must be {FAN_CONTROLLER_SOCKET_PATH}")
        if not math.isfinite(timeout_s) or not 0 < timeout_s <= 30:
            raise ValueError("fan controller socket timeout must be in (0, 30]")
        self._path = expanded
        self._timeout_s = timeout_s

    async def request(self, action: FanAction, *,
                      mode: FanMode | None = None) -> FanServiceResult:
        if not isinstance(action, FanAction):
            raise TypeError("action must be a FanAction")
        if action is FanAction.STATUS:
            if mode is not None:
                raise ValueError("status takes no mode")
            line = "status\n"
        else:
            if not isinstance(mode, FanMode):
                raise ValueError("mode action requires an allowlisted FanMode")
            line = f"mode {mode.value}\n"

        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(self._timeout_s):
                reader, writer = await asyncio.open_unix_connection(
                    self._path, limit=_MAX_SOCKET_REPLY_BYTES)
                writer.write(line.encode("ascii"))
                await writer.drain()
                raw = await reader.readline()
                if not raw or not raw.endswith(b"\n"):
                    raise FanServiceError("fan controller returned no complete line")
                if len(raw) > _MAX_SOCKET_REPLY_BYTES:
                    raise FanServiceError("fan controller reply is too large")
        except TimeoutError as exc:
            raise FanServiceError("fan controller request timed out") from exc
        except (OSError, ValueError, asyncio.LimitOverrunError) as exc:
            raise FanServiceError(
                f"fan controller connection failed: {type(exc).__name__}") from exc
        finally:
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(
                        writer.wait_closed(), timeout=min(0.25, self._timeout_s))
                except (OSError, TimeoutError):
                    pass

        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FanServiceError("fan controller returned invalid JSON") from exc
        return _parse_service_result(obj)


def _parse_service_result(obj) -> FanServiceResult:
    if not isinstance(obj, dict) or not isinstance(obj.get("ok"), bool):
        raise FanServiceError("fan controller reply lacks a boolean ok field")
    expected_keys = {
        "ok", "code", "requested_mode", "effective_mode", "temp_mc",
        "target_pwm", "pwm", "rpm", "health",
    }
    if set(obj) != expected_keys:
        raise FanServiceError("fan controller returned an unexpected schema")
    code = obj.get("code", "ok" if obj["ok"] else "controller_failed")
    if not isinstance(code, str) or not _RESULT_CODE_RE.fullmatch(code):
        raise FanServiceError("fan controller returned an invalid result code")
    if not obj["ok"]:
        return FanServiceResult(False, code)

    try:
        requested = FanMode(obj["requested_mode"])
        effective = FanMode(obj["effective_mode"])
        health = FanHealth(obj["health"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FanServiceError("fan controller returned an invalid enum field") from exc

    # Firmware represents an unavailable sensor as -1 and otherwise accepts a
    # non-negative temperature. Keep the root-service contract aligned so a
    # locally accepted reply cannot later be rejected by the XIAO callback.
    temp_mc = _bounded_optional_int(obj, "temp_mc", 0, 150000)
    target_pwm = _bounded_int(obj, "target_pwm", 0, 255)
    pwm = _bounded_int(obj, "pwm", 0, 255)
    rpm = _bounded_optional_int(obj, "rpm", 0, 100000)
    return FanServiceResult(
        True,
        code,
        FanStatus(requested, effective, temp_mc, target_pwm, pwm, rpm, health),
    )


def _bounded_int(obj: dict, key: str, low: int, high: int) -> int:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise FanServiceError(f"fan controller returned invalid {key}")
    return value


def _bounded_optional_int(obj: dict, key: str, low: int,
                          high: int) -> int | None:
    value = obj.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise FanServiceError(f"fan controller returned invalid {key}")
    return value


class _RecordStage(Enum):
    QUEUED = "queued"
    ACCEPTED = "accepted"
    TERMINAL = "terminal"


@dataclass
class _Record:
    request: FanRequest
    stage: _RecordStage = _RecordStage.QUEUED
    execution_started: bool = False
    accepted_pending: bool = False
    report: FanStatus | None = None
    report_pending: bool = False
    final_ack: FanAck | None = None
    final_ack_pending: bool = False
    enqueued: bool = False
    retry_count: int = 0
    retry_task: asyncio.Task | None = None
    retry_allowed: bool = True


@dataclass(frozen=True)
class _QueueItem:
    record: _Record
    replay: bool = False


class FanController:
    """Nonblocking EVT consumer and reconnect-safe fan request worker."""

    def __init__(self, session: SessionLike, cfg, *,
                 service: FanService | None = None) -> None:
        self._session = session
        self._cfg = cfg
        self._service: FanService = service or FanServiceClient(
            cfg.socket_path, timeout_s=cfg.socket_timeout_s)
        self._queue: asyncio.Queue[_QueueItem] = asyncio.Queue(
            maxsize=cfg.event_queue_size)
        self._records: OrderedDict[str, _Record] = OrderedDict()
        # Some malformed-but-correlatable events use a detached finite FAILED
        # record rather than entering the dedupe cache. Track every retry task
        # independently so link reset/close can still cancel it.
        self._retry_tasks: set[asyncio.Task] = set()
        self._closed = False

    def submit_event(self, payload: bytes) -> bool:
        """Parse/enqueue on the Session event thread; never perform I/O here."""
        try:
            request = parse_fan_event(payload)
        except FanProtocolError as exc:
            if not payload.lstrip(b" \t\r\n").startswith(b"cm5_fan_"):
                return False
            log.warning("rejected host-fan event: %s", exc)
            if exc.request_id is not None:
                rejected = _Record(
                    FanRequest(exc.request_id, FanAction.STATUS),
                    stage=_RecordStage.TERMINAL,
                    final_ack=FanAck.FAILED,
                    final_ack_pending=True,
                    retry_allowed=False,
                )
                self._enqueue(_QueueItem(rejected, replay=True))
            return True
        if request is None:
            return False

        existing = self._records.get(request.request_id)
        if existing is not None:
            self._records.move_to_end(request.request_id)
            if existing.request != request:
                log.error("host-fan request-ID conflict for %s",
                          request.request_id)
            if existing.stage is _RecordStage.TERMINAL:
                # An explicit firmware retry/duplicate gets the cached finite
                # outcome even if the first delivery received OK. It never
                # re-enters the root controller.
                existing.report_pending = existing.report is not None
                existing.final_ack_pending = existing.final_ack is not None
            self._queue_record(existing, replay=True)
            return True

        if not self._make_record_room():
            log.error("host-fan request cache full; event dropped")
            return True
        record = _Record(request)
        self._records[request.request_id] = record
        if not self._queue_record(record):
            self._records.pop(request.request_id, None)
        self._trim_records()
        return True

    def _enqueue(self, item: _QueueItem) -> bool:
        try:
            self._queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            log.error("host-fan event queue full; event dropped")
            return False

    def _queue_record(self, record: _Record, *, replay: bool = False) -> bool:
        if record.enqueued:
            return True
        if self._enqueue(_QueueItem(record, replay=replay)):
            record.enqueued = True
            return True
        return False

    def _trim_records(self) -> None:
        while len(self._records) > self._cfg.request_cache_size:
            for request_id, record in tuple(self._records.items()):
                if (record.stage is _RecordStage.TERMINAL and
                        not self._callbacks_pending(record)):
                    del self._records[request_id]
                    break
            else:
                break

    def _make_record_room(self) -> bool:
        """Keep the ID cache a hard bound, even while callbacks are failing."""
        if len(self._records) < self._cfg.request_cache_size:
            return True
        for request_id, record in tuple(self._records.items()):
            if (record.stage is _RecordStage.TERMINAL and
                    not self._callbacks_pending(record)):
                del self._records[request_id]
                return True
        return False

    @staticmethod
    def _callbacks_pending(record: _Record) -> bool:
        return (record.accepted_pending or record.report_pending or
                record.final_ack_pending)

    async def run(self) -> None:
        while True:
            item = await self._queue.get()
            record = item.record
            record.enqueued = False
            try:
                if item.replay:
                    await self._replay(record)
                elif record.stage is _RecordStage.QUEUED:
                    await self._begin(record)
                else:
                    await self._replay(record)
            except LinkClosed:
                raise
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("host-fan request failed unexpectedly")
                if not record.execution_started:
                    record.stage = _RecordStage.TERMINAL
                    record.final_ack = FanAck.FAILED
                    record.final_ack_pending = True
                    self._schedule_retry(record)
            finally:
                self._queue.task_done()
                self._trim_records()

    async def _begin(self, record: _Record) -> None:
        record.accepted_pending = True
        if not await self._send_ack(record.request.request_id, FanAck.ACCEPTED):
            self._schedule_retry(record)
            return
        record.accepted_pending = False
        record.stage = _RecordStage.ACCEPTED
        await self._execute(record)

    async def _execute(self, record: _Record) -> None:
        if not self._cfg.enabled:
            log.error("host-fan request rejected: fan control is disabled")
            await self._finish_failed(record)
            return

        record.execution_started = True
        attempts = 2 if record.request.action is FanAction.MODE else 1
        for attempt in range(attempts):
            try:
                result = await self._service.request(
                    record.request.action, mode=record.request.mode)
                break
            except asyncio.CancelledError:
                # Both operations are idempotent. If TaskGroup cancellation
                # lands after the root service applied a mode but before its
                # reply, a future fresh request can safely select it again.
                record.execution_started = False
                raise
            except (FanServiceError, OSError, TimeoutError) as exc:
                if attempt + 1 < attempts:
                    log.warning(
                        "host-fan mode reply unavailable; retrying the same "
                        "idempotent mode once: %s", exc)
                    continue
                log.error("host-fan controller request failed: %s", exc)
                await self._finish_failed(record)
                return

        if not result.ok or result.status is None:
            log.error("host-fan controller rejected %s: %s",
                      record.request.action.value, result.code)
            await self._finish_failed(record)
            return

        if (record.request.action is FanAction.MODE and
                result.status.requested_mode is not record.request.mode):
            log.error(
                "host-fan controller returned requested mode %s for %s",
                result.status.requested_mode.value,
                record.request.mode.value if record.request.mode else "none",
            )
            await self._finish_failed(record)
            return

        record.stage = _RecordStage.TERMINAL
        record.report = result.status
        record.report_pending = True
        record.final_ack = FanAck.APPLIED
        record.final_ack_pending = True
        await self._flush_terminal(record)

    async def _finish_failed(self, record: _Record) -> None:
        record.stage = _RecordStage.TERMINAL
        record.final_ack = FanAck.FAILED
        record.final_ack_pending = True
        if await self._send_ack(record.request.request_id, FanAck.FAILED):
            record.final_ack_pending = False
            self._reset_retry(record)
        else:
            self._schedule_retry(record)

    async def _replay(self, record: _Record) -> None:
        if record.stage is _RecordStage.QUEUED:
            if record.accepted_pending:
                if not await self._send_ack(
                        record.request.request_id, FanAck.ACCEPTED):
                    self._schedule_retry(record)
                    return
                record.accepted_pending = False
                record.stage = _RecordStage.ACCEPTED
                await self._execute(record)
                return
            await self._begin(record)
            return
        if record.stage is _RecordStage.ACCEPTED and not record.execution_started:
            await self._execute(record)
            return
        await self._flush_terminal(record)

    async def _flush_terminal(self, record: _Record) -> None:
        if record.report_pending:
            assert record.report is not None
            if not await self._send_report(record.request.request_id, record.report):
                self._schedule_retry(record)
                return
            record.report_pending = False
        if record.final_ack_pending:
            assert record.final_ack is not None
            if not await self._send_ack(
                    record.request.request_id, record.final_ack):
                self._schedule_retry(record)
                return
            record.final_ack_pending = False
        self._reset_retry(record)

    def link_reset(self) -> None:
        """Discard work tied to the authenticated UART epoch that just ended.

        Firmware deliberately binds each request to the named UART session that
        emitted it.  A login creates a new epoch, so replaying an old callback
        after reconnect would both fail and weaken that security boundary.  A
        mode operation may already have reached the idempotent root controller;
        the caller can reconcile its readback with a fresh ``cm5 fan status``.

        The supervised worker has been cancelled before this method is called,
        so no queue consumer can race this teardown.
        """
        for record in self._records.values():
            task = record.retry_task
            if task is not None and not task.done():
                task.cancel()
            record.retry_task = None
        for task in tuple(self._retry_tasks):
            if not task.done():
                task.cancel()
        self._retry_tasks.clear()
        self._records.clear()
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._queue.task_done()

    def _schedule_retry(self, record: _Record) -> None:
        if self._closed or not self._callbacks_pending(record):
            return
        if not record.retry_allowed:
            record.accepted_pending = False
            record.report_pending = False
            record.final_ack_pending = False
            return
        if record.retry_task is not None and not record.retry_task.done():
            return
        if record.retry_count >= len(_CALLBACK_RETRY_DELAYS_S):
            log.error("host-fan callback retry budget exhausted id=%s",
                      record.request.request_id)
            record.accepted_pending = False
            record.report_pending = False
            record.final_ack_pending = False
            # Modes/status are idempotent. Forgetting the exhausted record
            # bounds memory and lets a later firmware duplicate re-enter as a
            # fresh reconciliation attempt instead of retaining an impossible
            # callback forever.
            cached = self._records.get(record.request.request_id)
            if cached is record:
                del self._records[record.request.request_id]
            return
        delay = _CALLBACK_RETRY_DELAYS_S[record.retry_count]
        record.retry_count += 1

        async def later() -> None:
            reschedule = False
            try:
                await asyncio.sleep(delay)
                if self._closed or not self._callbacks_pending(record):
                    return
                if not self._queue_record(record, replay=True):
                    reschedule = True
            except asyncio.CancelledError:
                return
            finally:
                if record.retry_task is asyncio.current_task():
                    record.retry_task = None
            if reschedule:
                self._schedule_retry(record)

        record.retry_task = asyncio.create_task(
            later(), name=f"fan-callback-retry-{record.request.request_id}")
        self._retry_tasks.add(record.retry_task)
        record.retry_task.add_done_callback(self._retry_tasks.discard)

    @staticmethod
    def _reset_retry(record: _Record) -> None:
        task = record.retry_task
        if task is not None and not task.done():
            task.cancel()
        record.retry_task = None
        record.retry_count = 0

    async def _send_ack(self, request_id: str, state: FanAck) -> bool:
        return await self._send_uart(
            f"cm5 fan ack {PROTOCOL_VERSION} {request_id} {state.value}")

    async def _send_report(self, request_id: str, status: FanStatus) -> bool:
        temp = -1 if status.temp_mc is None else status.temp_mc
        rpm = -1 if status.rpm is None else status.rpm
        line = (
            f"cm5 fan report {PROTOCOL_VERSION} {request_id} "
            f"{status.requested_mode.value} {status.effective_mode.value} "
            f"{temp} {status.target_pwm} {status.pwm} {rpm} "
            f"{status.health.value}"
        )
        return await self._send_uart(line)

    async def _send_uart(self, line: str) -> bool:
        try:
            reply = await self._session.command(
                line,
                timeout=self._cfg.uart_timeout_s,
                expect="status",
                replay=False,
                auth_replay=False,
            )
        except LinkClosed:
            raise
        except Exception as exc:
            log.error("host-fan UART callback failed: %s", exc)
            return False
        if not reply.ok:
            log.error("host-fan UART callback rejected: %s", reply.text)
            return False
        return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = [task for task in self._retry_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        self._retry_tasks.clear()
        for record in self._records.values():
            record.retry_task = None
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
