"""Finite CM5 power control driven by authenticated HardwareOne EVT frames.

The UART is deliberately *not* a remote shell.  This module recognizes one
versioned, finite protocol and maps it to a root-owned helper using
``asyncio.create_subprocess_exec`` with a fixed executable and typed argv.
Neither event text nor helper output is ever used as a command line.

Protocol v1 (firmware -> CM5 EVT payloads)::

    cm5_power_status 1 <16-hex-id>
    cm5_power_profile_<eco|balanced|performance|auto> 1 <16-hex-id>
    cm5_power_reboot 1 <16-hex-id>
    cm5_power_halt 1 <16-hex-id>
    cm5_power_suspend 1 <16-hex-id>
    cm5_power_sleep_for 1 <16-hex-id> <1..1440-minutes>

CM5 -> firmware authenticated commands::

    cm5 power ack 1 <id> <accepted|committed|applied|failed>
    cm5 power report 1 <id|0> <state> <profile> <linux-boot-id>

The request cache is an at-most-once boundary.  A duplicate ID only replays
the most recent ACK; it never invokes the helper again.  In particular a
disruptive action crosses confirmed ``accepted`` and ``committed`` ACK phases
before the helper is called.  Reports carry the stable kernel boot UUID so the
firmware can distinguish a daemon restart from a real reboot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum, StrEnum
from pathlib import Path
from typing import Protocol

from .link.session import LinkClosed, Reply

log = logging.getLogger("power")

PROTOCOL_VERSION = "1"
MIN_SLEEP_MINUTES = 1
MAX_SLEEP_MINUTES = 1440
_REQUEST_ID_RE = re.compile(r"^[0-9a-fA-F]{16}$")
# Finite retry budget for ACK callbacks that reach a live Session but receive a
# transient Error/timeout. LinkClosed is handled by the reconnect supervisor.
# Tests replace this tuple with millisecond-scale delays.
_ACK_RETRY_DELAYS_S = (1.0, 2.0, 4.0, 8.0, 16.0)
# Boot/resume readiness establishes the Linux boot-tag safety baseline. Keep a
# separate finite budget because it has no firmware request ID to trigger a
# retransmission when a live Session transiently rejects the callback.
_READY_RETRY_DELAYS_S = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0)
_BOOT_TAG_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
UNKNOWN_BOOT_TAG = "0" * 32


def read_linux_boot_tag() -> str:
    """Return the normalized kernel boot UUID, or an all-zero fail-closed tag."""
    try:
        compact = _BOOT_ID_PATH.read_text(encoding="ascii").strip().replace("-", "")
    except OSError as exc:
        log.error("cannot read Linux boot ID: %s", exc)
        return UNKNOWN_BOOT_TAG
    if not _BOOT_TAG_RE.fullmatch(compact) or int(compact, 16) == 0:
        log.error("Linux boot ID is malformed; destructive power control is unavailable")
        return UNKNOWN_BOOT_TAG
    return compact.lower()


class PowerProfile(StrEnum):
    UNKNOWN = "unknown"
    ECO = "eco"
    BALANCED = "balanced"
    PERFORMANCE = "performance"
    AUTO = "auto"


class HostPowerState(StrEnum):
    UNKNOWN = "unknown"
    AWAKE = "awake"
    SLEEPING = "sleeping"
    SUSPENDING = "suspending"
    REBOOTING = "rebooting"
    HALTING = "halting"
    ERROR = "error"


class PowerAction(StrEnum):
    STATUS = "status"
    PROFILE = "profile"
    REBOOT = "reboot"
    HALT = "halt"
    # There is no separate v1 wire event for poweroff.  It remains a typed
    # helper operation for local callers/deployment tests; the firmware's
    # cm5_power_halt maps to the distinct Linux halt operation.
    POWEROFF = "poweroff"
    SUSPEND = "suspend"
    SLEEP_FOR = "sleep_for"


class AckState(StrEnum):
    ACCEPTED = "accepted"
    COMMITTED = "committed"
    APPLIED = "applied"
    FAILED = "failed"


@dataclass(frozen=True)
class PowerRequest:
    request_id: str
    action: PowerAction
    profile: PowerProfile | None = None
    minutes: int | None = None


class PowerProtocolError(ValueError):
    """A recognized host-power event was malformed.

    ``request_id`` is populated only for a syntactically valid v1 ID, allowing
    the controller to return a finite failure without reflecting unsafe text.
    """

    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id


def _canonical_request_id(token: str) -> str:
    if not _REQUEST_ID_RE.fullmatch(token):
        raise PowerProtocolError("request ID must be exactly 16 hexadecimal digits")
    # Firmware IDs are <nonzero boot nonce><nonzero counter>, each uint32.
    if int(token[:8], 16) == 0 or int(token[8:], 16) == 0:
        raise PowerProtocolError("request ID halves must both be nonzero")
    return token.lower()


_EVENTS: dict[str, tuple[PowerAction, PowerProfile | None]] = {
    "cm5_power_status": (PowerAction.STATUS, None),
    "cm5_power_profile_eco": (PowerAction.PROFILE, PowerProfile.ECO),
    "cm5_power_profile_balanced": (PowerAction.PROFILE, PowerProfile.BALANCED),
    "cm5_power_profile_performance": (PowerAction.PROFILE, PowerProfile.PERFORMANCE),
    "cm5_power_profile_auto": (PowerAction.PROFILE, PowerProfile.AUTO),
    "cm5_power_reboot": (PowerAction.REBOOT, None),
    "cm5_power_halt": (PowerAction.HALT, None),
    "cm5_power_suspend": (PowerAction.SUSPEND, None),
    "cm5_power_sleep_for": (PowerAction.SLEEP_FOR, None),
}


def parse_power_event(payload: bytes, *, min_sleep_minutes: int = MIN_SLEEP_MINUTES,
                      max_sleep_minutes: int = MAX_SLEEP_MINUTES) -> PowerRequest | None:
    """Parse one EVT payload, returning ``None`` for a non-power event.

    Recognized ``cm5_power_`` events are strict: ASCII only, protocol version
    1, exact token counts, an exact firmware-shaped request ID, and a decimal
    sleep duration inside both the firmware and configured bounds.
    """
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError:
        return None if not payload.startswith(b"cm5_power_") else _raise_protocol(
            "host-power event is not ASCII")
    tokens = text.strip().split()
    if not tokens or not tokens[0].startswith("cm5_power_"):
        return None

    name = tokens[0]
    if len(tokens) < 2 or tokens[1] != PROTOCOL_VERSION:
        # Never answer a different protocol version using v1 semantics.
        raise PowerProtocolError("missing or unsupported host-power protocol version")

    request_id: str | None = None
    if len(tokens) >= 3:
        try:
            request_id = _canonical_request_id(tokens[2])
        except PowerProtocolError:
            request_id = None

    event = _EVENTS.get(name)
    if event is None:
        raise PowerProtocolError("unknown host-power event", request_id=request_id)
    action, profile = event
    expected = 4 if action is PowerAction.SLEEP_FOR else 3
    if len(tokens) != expected:
        raise PowerProtocolError("wrong host-power event arity", request_id=request_id)
    if request_id is None:
        # Re-run for the more specific ID diagnostic.
        _canonical_request_id(tokens[2])
        raise AssertionError("unreachable")

    minutes: int | None = None
    if action is PowerAction.SLEEP_FOR:
        raw = tokens[3]
        if not raw.isascii() or not raw.isdecimal() or len(raw) > 4:
            raise PowerProtocolError("sleep duration must be decimal minutes",
                                     request_id=request_id)
        minutes = int(raw)
        low = max(MIN_SLEEP_MINUTES, min_sleep_minutes)
        high = min(MAX_SLEEP_MINUTES, max_sleep_minutes)
        if not low <= minutes <= high:
            raise PowerProtocolError(
                f"sleep duration must be in configured range {low}..{high} minutes",
                request_id=request_id)
    return PowerRequest(request_id, action, profile, minutes)


def _raise_protocol(message: str):
    raise PowerProtocolError(message)


@dataclass(frozen=True)
class HelperStatus:
    state: HostPowerState = HostPowerState.UNKNOWN
    profile: PowerProfile = PowerProfile.UNKNOWN
    suspend_supported: bool = False
    rtc_sleep_supported: bool = False


@dataclass(frozen=True)
class HelperResult:
    ok: bool
    code: str
    status: HelperStatus | None = None


class Helper(Protocol):
    async def execute(self, action: PowerAction, *, profile: PowerProfile | None = None,
                      minutes: int | None = None) -> HelperResult: ...


class SessionLike(Protocol):
    async def command(self, line: str, *, timeout: float, expect: str,
                      replay: bool = True) -> Reply: ...


def helper_argv(helper_path: str, use_sudo: bool, action: PowerAction, *,
                profile: PowerProfile | None = None,
                minutes: int | None = None) -> tuple[str, ...]:
    """Build the complete fixed argv for one typed helper operation."""
    if not isinstance(action, PowerAction):
        raise TypeError("action must be a PowerAction")
    if action is PowerAction.PROFILE:
        if not isinstance(profile, PowerProfile) or profile is PowerProfile.UNKNOWN:
            raise ValueError("profile action requires an allowlisted profile")
        args = (action.value, profile.value)
    elif action is PowerAction.SLEEP_FOR:
        if not isinstance(minutes, int) or isinstance(minutes, bool):
            raise ValueError("sleep_for requires integer minutes")
        if not MIN_SLEEP_MINUTES <= minutes <= MAX_SLEEP_MINUTES:
            raise ValueError("sleep_for minutes outside hard bounds")
        args = (action.value, str(minutes))
    else:
        if profile is not None or minutes is not None:
            raise ValueError(f"{action.value} takes no argument")
        args = (action.value,)

    base = (helper_path,)
    if use_sudo:
        base = ("/usr/bin/sudo", "-n", helper_path)
    return base + args


class PowerHelperClient:
    """Async client for the small root-owned helper.

    The executable comes from trusted local configuration and must be absolute.
    All remaining argv is produced from enums/validated integers above.  There
    is intentionally no shell, environment expansion, or arbitrary extra args.
    """

    def __init__(self, helper_path: str, *, use_sudo: bool = True,
                 timeout_s: float = 20.0) -> None:
        expanded = os.path.expanduser(helper_path)
        if not Path(expanded).is_absolute():
            raise ValueError("power helper path must be absolute")
        self._path = expanded
        self._sudo = use_sudo
        self._timeout_s = timeout_s

    async def execute(self, action: PowerAction, *, profile: PowerProfile | None = None,
                      minutes: int | None = None) -> HelperResult:
        argv = helper_argv(self._path, self._sudo, action,
                           profile=profile, minutes=minutes)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return HelperResult(False, f"helper_start:{type(exc).__name__}")

        timeout: float | None = self._timeout_s
        if action is PowerAction.SUSPEND:
            # An opted-in suspend is intentionally unbounded: the helper's
            # synchronous systemctl call can return only after a human wakes it.
            timeout = None
        try:
            if timeout is None:
                stdout, stderr = await proc.communicate()
            else:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            await _kill_process_group(proc)
            return HelperResult(False, "helper_timeout")
        except asyncio.CancelledError:
            await _kill_process_group(proc)
            raise

        if len(stdout) > 8192 or len(stderr) > 8192:
            return HelperResult(False, "helper_output_too_large")
        try:
            obj = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = stderr.decode("utf-8", "replace")[:160].strip()
            log.error("power helper returned invalid JSON (rc=%s): %s",
                      proc.returncode, detail)
            return HelperResult(False, "helper_protocol")
        if not isinstance(obj, dict) or not isinstance(obj.get("ok"), bool):
            return HelperResult(False, "helper_protocol")

        code = obj.get("code", "ok" if obj["ok"] else "helper_failed")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_:-]{1,80}", code):
            code = "helper_protocol"
        status = _parse_helper_status(obj)
        ok = bool(obj["ok"]) and proc.returncode == 0
        return HelperResult(ok, code if ok or code != "ok" else "helper_exit", status)


async def _kill_process_group(proc) -> None:
    """Best-effort kill of sudo + helper descendants created in one session."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except (AttributeError, OSError):
        # Fallback still stops the direct child on platforms without killpg.
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()


def _parse_helper_status(obj: dict) -> HelperStatus | None:
    if not any(key in obj for key in
               ("state", "profile", "suspend_supported", "rtc_sleep_supported")):
        return None
    try:
        state = HostPowerState(obj.get("state", "unknown"))
    except (TypeError, ValueError):
        state = HostPowerState.UNKNOWN
    try:
        profile = PowerProfile(obj.get("profile", "unknown"))
    except (TypeError, ValueError):
        profile = PowerProfile.UNKNOWN
    return HelperStatus(
        state=state,
        profile=profile,
        suspend_supported=obj.get("suspend_supported") is True,
        rtc_sleep_supported=obj.get("rtc_sleep_supported") is True,
    )


@dataclass
class _Record:
    request: PowerRequest
    ack: AckState | None = None
    ack_pending: bool = False
    ack_retry_count: int = 0
    ack_retry_exhausted: bool = False
    ack_retry_task: asyncio.Task | None = None
    execution_started: bool = False
    stage: "_RecordStage" = None  # type: ignore[assignment]
    enqueued: bool = False

    def __post_init__(self) -> None:
        if self.stage is None:
            self.stage = _RecordStage.QUEUED


class _RecordStage(Enum):
    QUEUED = "queued"
    IN_FLIGHT = "in_flight"
    ACCEPTED = "accepted"
    COMMITTED = "committed"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class _QueueItem:
    record: _Record
    replay: bool = False


@dataclass(frozen=True)
class _ReadyQueueItem:
    pass


class PowerController:
    """UART event consumer, request deduper, and automatic profile policy."""

    def __init__(self, session: SessionLike, cfg, *, helper: Helper | None = None,
                 boot_tag: str | None = None) -> None:
        self._session = session
        self._cfg = cfg
        self._helper: Helper = helper or PowerHelperClient(
            cfg.helper_path,
            use_sudo=cfg.use_sudo,
            timeout_s=cfg.helper_timeout_s,
        )
        tag = read_linux_boot_tag() if boot_tag is None else boot_tag
        if not _BOOT_TAG_RE.fullmatch(tag):
            raise ValueError("Linux boot tag must be exactly 32 hexadecimal digits")
        self._boot_tag = tag.lower()
        self._queue: asyncio.Queue[_QueueItem | _ReadyQueueItem] = asyncio.Queue(
            maxsize=cfg.event_queue_size)
        self._records: OrderedDict[str, _Record] = OrderedDict()
        self._mode = (PowerProfile(cfg.initial_profile)
                      if cfg.enabled else PowerProfile.UNKNOWN)
        self._applied_profile = PowerProfile.UNKNOWN
        self._activity_count = 0
        self._idle_generation = 0
        self._idle_task: asyncio.Task | None = None
        self._helper_lock = asyncio.Lock()
        # Serializes policy decisions with their sysfs apply + state commit.
        # The helper lock alone is insufficient: a job can otherwise start
        # between an auto target decision and the eventual mode assignment.
        self._policy_lock = asyncio.Lock()
        self._ready_pending = False
        self._ready_enqueued = False
        self._ready_retry_count = 0
        self._ready_retry_exhausted = False
        self._ready_retry_task: asyncio.Task | None = None
        self._closed = False

    @property
    def current_mode(self) -> PowerProfile:
        return self._mode

    def submit_event(self, payload: bytes) -> bool:
        """Non-blocking Session.on_event callback.  Returns whether consumed."""
        try:
            request = parse_power_event(
                payload,
                min_sleep_minutes=self._cfg.min_sleep_minutes,
                max_sleep_minutes=self._cfg.max_sleep_minutes,
            )
        except PowerProtocolError as exc:
            if not payload.startswith(b"cm5_power_"):
                return False
            log.warning("rejected host-power event: %s", exc)
            # A valid v1 ID gets a finite failed ACK.  Cross-version/missing-ID
            # events are only consumed; reflecting v1 into another version is
            # deliberately forbidden.
            if exc.request_id is not None:
                rejected = _Record(PowerRequest(exc.request_id, PowerAction.STATUS),
                                   ack=AckState.FAILED,
                                   ack_pending=True,
                                   stage=_RecordStage.TERMINAL)
                self._enqueue(_QueueItem(rejected, replay=True))
            return True
        if request is None:
            return False

        if request.action in (PowerAction.REBOOT, PowerAction.HALT,
                              PowerAction.SUSPEND, PowerAction.SLEEP_FOR):
            # Firmware refuses to emit a destructive request until it already
            # has a valid boot-tag baseline. Retire any still-undelivered ready
            # claim before accepting the event: replaying id=0 after an Accepted
            # reply is lost would look like a same-boot controller restart and
            # make firmware correctly (but spuriously) fail that request.
            self._ready_delivered()

        existing = self._records.get(request.request_id)
        if existing is not None:
            self._records.move_to_end(request.request_id)
            if existing.request != request:
                log.error("host-power request-ID conflict for %s; replaying cached ACK",
                          request.request_id)
            if existing.stage is _RecordStage.QUEUED:
                # The original is already queued in the common case.  If a
                # cancellation could not requeue it because the queue filled,
                # this retry repairs that state without creating two originals.
                self._requeue_record(existing)
            else:
                self._enqueue(_QueueItem(existing, replay=True))
            return True

        record = _Record(request)
        self._records[request.request_id] = record
        if not self._enqueue(_QueueItem(record)):
            self._records.pop(request.request_id, None)
            return True
        record.enqueued = True
        self._trim_records()
        return True

    def _enqueue(self, item: _QueueItem | _ReadyQueueItem) -> bool:
        try:
            self._queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            log.error("host-power event queue full; event dropped")
            return False

    def _trim_records(self) -> None:
        while len(self._records) > self._cfg.request_cache_size:
            evicted = False
            for request_id, record in self._records.items():
                if (record.stage is _RecordStage.TERMINAL and
                        not record.ack_pending):
                    del self._records[request_id]
                    evicted = True
                    break
            if not evicted:
                # Never evict a queued/accepted operation: that would reopen an
                # at-most-once ID while it can still execute.
                break

    async def start(self) -> None:
        """Apply the configured baseline, then publish the boot-ready report."""
        if self._cfg.enabled:
            target = self._target_for_mode(self._mode)
            result = await self._apply_concrete_profile(target)
            if not result.ok:
                log.error("initial power profile %s failed: %s", target, result.code)
        await self.report_ready()

    async def report_ready(self) -> bool:
        """Unsolicited finite boot/resume handshake (not a serial reconnect)."""
        self._cancel_ready_retry()
        self._ready_pending = True
        self._ready_retry_count = 0
        self._ready_retry_exhausted = False
        ok = await self._send_ready_report()
        if ok:
            self._ready_delivered()
        else:
            self._schedule_ready_retry()
        # Flush request-correlated callbacks without turning this direct attempt
        # into a second immediate readiness attempt.
        self._replay_record_callbacks()
        return ok

    async def _send_ready_report(self) -> bool:
        reported_mode = (self._mode if self._applied_profile is not PowerProfile.UNKNOWN
                         else PowerProfile.UNKNOWN)
        return await self._send_report("0", HostPowerState.AWAKE, reported_mode)

    def replay_pending_callbacks(self) -> None:
        """Queue undelivered ACKs after link repair without claiming a boot."""
        # Do not originate a new readiness claim on serial reconnect. If the
        # controller's original boot/resume report never received OK, however,
        # this is delivery repair for that same claim and remains boot-correct.
        if self._ready_pending:
            self._queue_ready_retry_now()
        self._replay_record_callbacks()

    def _replay_record_callbacks(self) -> None:
        # A link may disappear after a helper result but before its final ACK.
        # Reconnect never re-runs execution_started work; it only flushes the
        # cached finite state back to firmware.
        records = reversed(tuple(self._records.values()))
        for record in records:
            if record.stage is _RecordStage.QUEUED and not record.enqueued:
                self._requeue_record(record)
        # Newest first: even if many callbacks accumulated while disconnected,
        # the firmware's one current request is never displaced by stale IDs.
        for record in reversed(tuple(self._records.values())):
            if (record.ack is not None and
                    (record.ack_pending or
                     (record.stage is _RecordStage.COMMITTED and
                      not record.execution_started))):
                self._enqueue(_QueueItem(record, replay=True))

    def _cancel_ready_retry(self) -> None:
        task = self._ready_retry_task
        if task is not None and not task.done():
            task.cancel()
        self._ready_retry_task = None

    def _ready_delivered(self) -> None:
        self._ready_pending = False
        self._ready_enqueued = False
        self._ready_retry_count = 0
        self._ready_retry_exhausted = False
        self._cancel_ready_retry()

    def _queue_ready_retry_now(self) -> None:
        if self._closed or not self._ready_pending or self._ready_enqueued:
            return
        self._cancel_ready_retry()
        if self._enqueue(_ReadyQueueItem()):
            self._ready_enqueued = True
        else:
            self._schedule_ready_retry()

    def _schedule_ready_retry(self) -> None:
        if (self._closed or not self._ready_pending or self._ready_enqueued or
                (self._ready_retry_task is not None and
                 not self._ready_retry_task.done())):
            return
        if self._ready_retry_count >= len(_READY_RETRY_DELAYS_S):
            if not self._ready_retry_exhausted:
                log.error("host-power boot-ready report retry budget exhausted")
                self._ready_retry_exhausted = True
            return

        delay = _READY_RETRY_DELAYS_S[self._ready_retry_count]
        self._ready_retry_count += 1

        async def later() -> None:
            reschedule = False
            try:
                await asyncio.sleep(delay)
                if self._closed or not self._ready_pending or self._ready_enqueued:
                    return
                if self._enqueue(_ReadyQueueItem()):
                    self._ready_enqueued = True
                else:
                    reschedule = True
            except asyncio.CancelledError:
                return
            finally:
                if self._ready_retry_task is asyncio.current_task():
                    self._ready_retry_task = None
            if reschedule:
                self._schedule_ready_retry()

        self._ready_retry_task = asyncio.create_task(
            later(), name="power-ready-retry")

    async def run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if isinstance(item, _ReadyQueueItem):
                    self._ready_enqueued = False
                    if not self._ready_pending:
                        continue
                    if await self._send_ready_report():
                        self._ready_delivered()
                    else:
                        self._schedule_ready_retry()
                    continue
                if item.replay:
                    if item.record.ack is not None:
                        confirmed = await self._replay_record_ack(item.record)
                        if (confirmed and
                                item.record.stage is _RecordStage.COMMITTED and
                                not item.record.execution_started):
                            await self._execute_committed_transition(item.record)
                    continue
                item.record.enqueued = False
                if item.record.stage is not _RecordStage.QUEUED:
                    if item.record.ack is not None:
                        await self._replay_record_ack(item.record)
                    continue
                item.record.stage = _RecordStage.IN_FLIGHT
                await self._execute_record(item.record)
            except LinkClosed:
                if isinstance(item, _QueueItem) and not item.replay:
                    self._recover_unaccepted(item.record)
                raise
            except asyncio.CancelledError:
                if isinstance(item, _QueueItem) and not item.replay:
                    self._recover_unaccepted(item.record)
                raise
            except Exception:
                # A malformed request cannot reach this point; nevertheless a
                # local/helper fault must not kill the UART control plane.
                log.exception("host-power request failed unexpectedly")
                if isinstance(item, _ReadyQueueItem):
                    self._schedule_ready_retry()
                elif item.record.stage is _RecordStage.COMMITTED:
                    # Once firmware confirmed commitment, an unexpected local
                    # exception makes execution ambiguous. Never clear the
                    # latch or re-run; operator recovery is the safe boundary.
                    log.error("committed host-power operation is now uncertain")
                else:
                    item.record.stage = _RecordStage.TERMINAL
                    await self._send_record_ack(item.record, AckState.FAILED)
            finally:
                self._queue.task_done()
                self._trim_records()

    def _recover_unaccepted(self, record: _Record) -> None:
        # Accepted is an idempotent promise, not permission to run. Re-send it
        # after reconnect until the separate Committed boundary is confirmed.
        if (not record.execution_started and
                record.stage in (_RecordStage.IN_FLIGHT, _RecordStage.ACCEPTED)):
            record.stage = _RecordStage.QUEUED
            self._requeue_record(record)

    def _requeue_record(self, record: _Record) -> None:
        if record.stage is not _RecordStage.QUEUED or record.enqueued:
            return
        if self._enqueue(_QueueItem(record)):
            record.enqueued = True

    async def _execute_record(self, record: _Record) -> None:
        req = record.request
        if not self._cfg.enabled:
            await self._fail(record, PowerProfile.UNKNOWN, "power control disabled")
            return

        if req.action is PowerAction.SUSPEND:
            if not self._cfg.allow_suspend:
                await self._fail(record, self._mode, "suspend disabled by configuration")
                return
            status = await self._helper_status()
            if not status.ok or status.status is None or not status.status.suspend_supported:
                await self._fail(record, self._observed_profile(status),
                                 "suspend unsupported")
                return
        elif req.action is PowerAction.SLEEP_FOR:
            status = await self._helper_status()
            if not status.ok or status.status is None or not status.status.rtc_sleep_supported:
                await self._fail(record, self._observed_profile(status),
                                 "RTC sleep unsupported")
                return

        # Every operation gets an accepted ACK.  For disruptive operations this
        # is the hard safety gate: no successful ACK, no process invocation.
        if not await self._send_record_ack(record, AckState.ACCEPTED):
            record.stage = _RecordStage.TERMINAL
            await self._send_record_ack(record, AckState.FAILED)
            log.error("host-power %s not invoked: accepted ACK was not confirmed",
                      req.action.value)
            return
        record.stage = _RecordStage.ACCEPTED

        if req.action is PowerAction.STATUS:
            async with self._policy_lock:
                result = await self._helper_status()
                reported_profile = self._status_profile(result)
            if not result.ok:
                await self._fail(record, self._observed_profile(result), result.code)
                return
            await self._send_report(req.request_id, HostPowerState.AWAKE,
                                    reported_profile)
            record.stage = _RecordStage.TERMINAL
            await self._send_record_ack(record, AckState.APPLIED)
            return

        if req.action is PowerAction.PROFILE:
            assert req.profile is not None
            await self._apply_requested_profile(record, req.profile)
            return

        transition = {
            PowerAction.REBOOT: HostPowerState.REBOOTING,
            PowerAction.HALT: HostPowerState.HALTING,
            PowerAction.POWEROFF: HostPowerState.HALTING,
            PowerAction.SUSPEND: HostPowerState.SUSPENDING,
            PowerAction.SLEEP_FOR: HostPowerState.SLEEPING,
        }[req.action]
        # The accepted ACK above is authoritative; a best-effort transition
        # report may be retried before commitment, but no helper may run until
        # the firmware has also confirmed the finite Committed boundary.
        await self._send_report(req.request_id, transition, self._mode)
        record.stage = _RecordStage.COMMITTED
        if not await self._send_record_ack(record, AckState.COMMITTED):
            await self._fail(record, self._mode,
                             "committed ACK was not confirmed")
            return
        await self._execute_committed_transition(record)

    async def _execute_committed_transition(self, record: _Record) -> None:
        """Invoke one destructive helper only after Committed received OK."""
        req = record.request
        async with self._helper_lock:
            # Set this inside the lock: cancellation while waiting for another
            # helper operation is still safely recoverable after reconnect.
            record.execution_started = True
            result = await self._helper.execute(req.action, minutes=req.minutes)
        if not result.ok:
            # The helper process existed, so a timeout/error cannot prove that
            # systemd did not already queue the transition. Keep Committed and
            # the firmware latch intact; never authorize a second destructive
            # operation from an ambiguous local result.
            log.error("committed host-power %s id=%s is uncertain: %s; "
                      "inspect the host, then use firmware recovery if safe",
                      req.action.value, req.request_id, result.code)
            return

        record.stage = _RecordStage.TERMINAL
        await self._send_record_ack(record, AckState.APPLIED)
        if req.action is PowerAction.SUSPEND:
            # Synchronous helper returns only after resume.
            await self._send_report(req.request_id, HostPowerState.AWAKE, self._mode)
            await self.report_ready()

    async def _apply_requested_profile(self, record: _Record,
                                       requested: PowerProfile) -> None:
        async with self._policy_lock:
            self._invalidate_idle_timer()
            target = self._target_for_mode(requested)
            result = await self._apply_concrete_profile(target)
            if not result.ok:
                observed_profile = self._observed_profile(result)
                if self._mode is PowerProfile.AUTO and self._activity_count == 0:
                    self._schedule_idle()
            else:
                self._mode = requested
                observed_profile = requested
        if not result.ok:
            await self._fail(record, observed_profile, result.code)
            return
        record.stage = _RecordStage.TERMINAL
        await self._send_report(record.request.request_id,
                                HostPowerState.AWAKE, requested)
        await self._send_record_ack(record, AckState.APPLIED)

    async def _helper_status(self) -> HelperResult:
        async with self._helper_lock:
            return await self._helper.execute(PowerAction.STATUS)

    async def _apply_concrete_profile(self, profile: PowerProfile) -> HelperResult:
        # Auto is a policy owned by this controller; the privileged helper sees
        # only a concrete allowlisted governor profile during transitions.
        assert profile in (PowerProfile.ECO, PowerProfile.BALANCED,
                           PowerProfile.PERFORMANCE)
        async with self._helper_lock:
            result = await self._helper.execute(PowerAction.PROFILE, profile=profile)
        if result.ok:
            self._applied_profile = profile
        else:
            # The helper reports its post-rollback readback. Track that
            # physical state (or unknown) instead of retaining a stale logical
            # success from an earlier request.
            self._applied_profile = self._observed_profile(result)
        return result

    @staticmethod
    def _observed_profile(result: HelperResult) -> PowerProfile:
        return (result.status.profile if result.status is not None
                else PowerProfile.UNKNOWN)

    def _status_profile(self, result: HelperResult) -> PowerProfile:
        observed = self._observed_profile(result)
        # Auto is a controller policy, not a kernel governor. Preserve that
        # logical label only when complete helper readback matches the concrete
        # profile this controller last applied.
        if (self._mode is PowerProfile.AUTO and
                observed is not PowerProfile.UNKNOWN and
                observed is self._applied_profile):
            return PowerProfile.AUTO
        return observed

    def _target_for_mode(self, mode: PowerProfile) -> PowerProfile:
        if mode is not PowerProfile.AUTO:
            return mode
        value = (self._cfg.auto_active_profile if self._activity_count
                 else self._cfg.auto_idle_profile)
        return PowerProfile(value)

    async def _fail(self, record: _Record, profile: PowerProfile, reason: str) -> None:
        log.error("host-power %s id=%s failed: %s", record.request.action.value,
                  record.request.request_id, reason)
        record.stage = _RecordStage.TERMINAL
        # Cache terminal failure *before* the best-effort report. If the report
        # consumes the link-close indication, reconnect can still replay the
        # finite failed ACK without ever re-running an executed operation.
        self._cache_record_ack(record, AckState.FAILED)
        await self._send_report(record.request.request_id, HostPowerState.ERROR, profile)
        await self._replay_record_ack(record)

    @staticmethod
    def _cancel_ack_retry(record: _Record) -> None:
        task = record.ack_retry_task
        if task is not None and not task.done():
            task.cancel()
        record.ack_retry_task = None

    def _cache_record_ack(self, record: _Record, state: AckState) -> None:
        if record.ack is not state:
            self._cancel_ack_retry(record)
            record.ack_retry_count = 0
            record.ack_retry_exhausted = False
        record.ack = state
        record.ack_pending = True

    def _ack_delivered(self, record: _Record, state: AckState) -> None:
        if record.ack is not state:
            return
        record.ack_pending = False
        record.ack_retry_count = 0
        record.ack_retry_exhausted = False
        self._cancel_ack_retry(record)

    def _schedule_ack_retry(self, record: _Record) -> None:
        if self._closed or not record.ack_pending or record.ack is None:
            return
        task = record.ack_retry_task
        if task is not None and not task.done():
            return
        if record.ack_retry_count >= len(_ACK_RETRY_DELAYS_S):
            if not record.ack_retry_exhausted:
                log.error("host-power ACK retry budget exhausted id=%s state=%s",
                          record.request.request_id, record.ack.value)
                record.ack_retry_exhausted = True
            return

        state = record.ack
        delay = _ACK_RETRY_DELAYS_S[record.ack_retry_count]
        record.ack_retry_count += 1

        async def later() -> None:
            reschedule = False
            try:
                await asyncio.sleep(delay)
                if (self._closed or not record.ack_pending or
                        record.ack is not state):
                    return
                if not self._enqueue(_QueueItem(record, replay=True)):
                    reschedule = True
            except asyncio.CancelledError:
                return
            finally:
                if record.ack_retry_task is asyncio.current_task():
                    record.ack_retry_task = None
            if reschedule:
                self._schedule_ack_retry(record)

        record.ack_retry_task = asyncio.create_task(
            later(), name=f"power-ack-retry-{record.request.request_id}")

    async def _send_record_ack(self, record: _Record, state: AckState) -> bool:
        self._cache_record_ack(record, state)
        ok = await self._send_ack(record.request.request_id, state)
        if ok:
            self._ack_delivered(record, state)
        else:
            self._schedule_ack_retry(record)
        return ok

    async def _replay_record_ack(self, record: _Record) -> bool:
        state = record.ack
        if state is None:
            return False
        record.ack_pending = True
        ok = await self._send_ack(record.request.request_id, state)
        if ok:
            self._ack_delivered(record, state)
        else:
            self._schedule_ack_retry(record)
        return ok

    async def _send_ack(self, request_id: str, state: AckState) -> bool:
        line = f"cm5 power ack {PROTOCOL_VERSION} {request_id} {state.value}"
        return await self._send_uart(line)

    async def _send_report(self, request_id: str, state: HostPowerState,
                           profile: PowerProfile) -> bool:
        line = (f"cm5 power report {PROTOCOL_VERSION} {request_id} "
                f"{state.value} {profile.value} {self._boot_tag}")
        return await self._send_uart(line)

    async def _send_uart(self, line: str) -> bool:
        try:
            reply = await self._session.command(
                line,
                timeout=self._cfg.uart_timeout_s,
                expect="status",
                replay=True,
            )
        except LinkClosed:
            # The serial transport emits one closed event.  Swallowing it here
            # can leave the pump waiting forever and prevent supervisor repair.
            raise
        except Exception as exc:
            log.error("host-power UART callback failed: %s", exc)
            return False
        if not reply.ok:
            log.error("host-power UART callback rejected: %s", reply.text)
            return False
        return True

    async def activity_started(self) -> None:
        """Promote auto mode for one pipeline job; manual modes are retained."""
        if not self._cfg.enabled:
            return
        async with self._policy_lock:
            self._activity_count += 1
            self._invalidate_idle_timer()
            try:
                if self._mode is PowerProfile.AUTO and self._activity_count == 1:
                    target = PowerProfile(self._cfg.auto_active_profile)
                    result = await self._apply_concrete_profile(target)
                    if not result.ok:
                        log.error("automatic active profile failed: %s", result.code)
            except BaseException:
                # VoicePipeline marks its activity lease acquired only after
                # this method returns. Cancellation while the helper is awaited
                # must therefore roll back here or no finally block owns the
                # increment, leaving auto mode permanently active.
                self._activity_count -= 1
                if self._activity_count == 0 and self._mode is PowerProfile.AUTO:
                    self._schedule_idle()
                raise

    async def activity_finished(self) -> None:
        """Debounce the auto-mode return to idle after the last pipeline job."""
        if not self._cfg.enabled:
            return
        async with self._policy_lock:
            if self._activity_count == 0:
                log.warning("unbalanced power activity_finished call")
                return
            self._activity_count -= 1
            if self._activity_count == 0 and self._mode is PowerProfile.AUTO:
                if self._cfg.auto_idle_delay_s == 0:
                    result = await self._apply_concrete_profile(
                        PowerProfile(self._cfg.auto_idle_profile))
                    if not result.ok:
                        log.error("automatic idle profile failed: %s", result.code)
                else:
                    self._schedule_idle()

    def _invalidate_idle_timer(self) -> None:
        self._idle_generation += 1
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    def _schedule_idle(self) -> None:
        self._invalidate_idle_timer()
        generation = self._idle_generation

        async def later() -> None:
            try:
                await asyncio.sleep(self._cfg.auto_idle_delay_s)
                if (generation != self._idle_generation or
                        self._activity_count != 0 or
                        self._mode is not PowerProfile.AUTO):
                    return
                async with self._policy_lock:
                    if (generation != self._idle_generation or
                            self._activity_count != 0 or
                            self._mode is not PowerProfile.AUTO):
                        return
                    result = await self._apply_concrete_profile(
                        PowerProfile(self._cfg.auto_idle_profile))
                    if not result.ok:
                        log.error("automatic idle profile failed: %s", result.code)
            except asyncio.CancelledError:
                return

        self._idle_task = asyncio.create_task(later(), name="power-auto-idle")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._invalidate_idle_timer()
        ready_retry_task = self._ready_retry_task
        self._cancel_ready_retry()
        if ready_retry_task is not None and not ready_retry_task.done():
            await asyncio.gather(ready_retry_task, return_exceptions=True)
        retry_tasks = []
        for record in self._records.values():
            task = record.ack_retry_task
            if task is not None and not task.done():
                retry_tasks.append(task)
                task.cancel()
            record.ack_retry_task = None
        if retry_tasks:
            await asyncio.gather(*retry_tasks, return_exceptions=True)
        # Never leave a one-shot invocation pinned at the active governor.
        if self._cfg.enabled:
            async with self._policy_lock:
                if self._mode is PowerProfile.AUTO:
                    result = await self._apply_concrete_profile(
                        PowerProfile(self._cfg.auto_idle_profile))
                    if not result.ok:
                        log.error("final automatic idle profile failed: %s", result.code)
