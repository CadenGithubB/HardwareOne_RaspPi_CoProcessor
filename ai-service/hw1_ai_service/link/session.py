"""Session: authenticated command channel on top of SerialTransport.

Strict request/response — exactly one command in flight (asyncio.Lock),
matching the firmware drain's one-command-per-lap reality. Reply collection
handles the three shapes the text protocol actually has:

  expect="json"    collect until a line parses as a complete JSON document
                   (the plan's json-token convention; stampOkStatus-exempt),
                   or an error status line arrives.
  expect="status"  first OK/Error/ERROR/Unknown-command line terminates
                   (single-line replies, fast path).
  expect="auto"    quiet-gap collection: the reply blob is written in one
                   piece, so 150ms of line silence ends it. A status FIRST
                   line does NOT fast-return — multi-line successes are
                   stamped 'OK: ...' on their first line and the body would
                   be lost (review finding). Costs one quiet gap per reply.

Hard rules from the adversarial review:
  - Every wait is bounded by the command deadline, including quiet-gap and
    garbage handling — a break-noise flood must never extend collection
    (the wedged-daemon finding).
  - Login collection terminates ONLY on login-shaped lines; a straggler
    reply from a timed-out command is a stray, never a login failure.
  - Timeout->re-login->replay is opt-out (replay=False) for commands that
    are not idempotent (micrecord start/stop). Epoch-bound callbacks can also
    set auth_replay=False so an authentication loss cannot transplant an old
    request ID into the replacement login epoch.

Device pushes: the firmware may emit spontaneous EVT frames (e.g. the
"evenai_wake" wake push) at ANY point — mid-reply, mid-login, or while the
link is idle. Every consumption path therefore routes EVT frames to the
`on_event` callback instead of dropping them, and `pump_events()` (run as a
daemon-mode background task) consumes the queue while no command is in
flight so an idle-time push is seen promptly rather than at the next
command's stale-drain. EVT frames never terminate or corrupt a reply — they
are binary frames, invisible to the text collector by construction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from dataclasses import dataclass, field
from typing import Callable

from . import protocol
from .transport import LinkEvent, SerialTransport

log = logging.getLogger("link.session")

# Bound on collected reply lines — protects memory against a line flood
# (legitimate replies are <= 4095B, i.e. a few dozen lines).
_MAX_REPLY_LINES = 500


def _firmware_cli_token(value: str) -> str:
    """Encode one token for firmware CommandArgs (which has no escapes)."""
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("UART credentials cannot contain NUL, CR, or LF")
    if value.startswith('"'):
        raise ValueError("UART credentials cannot begin with a quote")
    if not value or any(ch.isspace() for ch in value):
        if '"' in value:
            raise ValueError(
                "UART credentials cannot contain both whitespace and a quote")
        return f'"{value}"'
    return value


class LinkClosed(Exception):
    pass


class CommandTimeout(Exception):
    pass


class LoginFailed(Exception):
    pass


class CommandCancelled(Exception):
    """A guarded command was cancelled before a write or after safe drain."""


CancelGuard = Callable[[], bool]


@dataclass
class Reply:
    lines: list[str] = field(default_factory=list)
    json: dict | list | None = None

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def ok(self) -> bool:
        if isinstance(self.json, dict) and "success" in self.json:
            return bool(self.json["success"])
        return bool(self.lines) and protocol.is_ok_line(self.lines[0])


class EventLatch:
    """Sticky, epoch-stamped latch for a named device EVT, with a payload.

    Usage is arm-then-act: take a token BEFORE doing the thing that can produce
    the event, then wait on that token. A fire that happened before the arm
    cannot satisfy the next wait, so a stale push from a previous exchange is
    never mistaken for this one's.
    """

    def __init__(self) -> None:
        self._count = 0
        self._payload: str | None = None
        self._event = asyncio.Event()

    def arm(self) -> int:
        """Snapshot the counter before the action that can produce the event."""
        self._event.clear()
        return self._count

    def fire(self, payload: str = "") -> None:
        """Loop-thread only (route_link_event) — non-blocking by construction."""
        self._count += 1
        self._payload = payload
        self._event.set()

    @property
    def payload(self) -> str | None:
        return self._payload

    async def wait(self, token: int, timeout: float) -> bool:
        """True if the latch fired after `token`, False on timeout."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._count <= token:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self._event.wait(), remaining)
            except asyncio.TimeoutError:
                return False
            self._event.clear()
        return True


class Session:
    def __init__(self, transport: SerialTransport, username: str, password: str):
        self._t = transport
        self._user = username
        self._password = password
        self._lock = asyncio.Lock()
        self._quiet_until = 0.0
        self._reboot_suspected = False
        self._reboot_generation = 0
        self._reboot_changed = asyncio.Event()
        self._reboot_listeners: list[Callable[[], None]] = []
        # Spontaneous EVT frame payloads land here (loop thread, may fire from
        # any consumption path — including mid-command). Keep handlers cheap
        # and non-blocking: queue a job, never run one.
        self.on_event: object | None = None  # Callable[[bytes], None]
        # Sticky latch for the device's `mic_autostop <path>` push. Sticky is
        # the whole point: on_event fires on the loop thread from whatever path
        # happens to drain rx, which can easily run BEFORE the coroutine that
        # wants to wait has reached its await. A bare asyncio.Event would lose
        # that push and cost a full backstop interval.
        self.mic_autostop = EventLatch()

    # -- public API --------------------------------------------------------

    async def login(self) -> None:
        async with self._lock:
            await self._login_locked()

    async def command(self, line: str, *, timeout: float = protocol.DEFAULT_CMD_TIMEOUT_S,
                      expect: str = "auto", replay: bool = True,
                      auth_replay: bool = True,
                      cancel_guard: CancelGuard | None = None) -> Reply:
        """Run one command. On timeout: re-login and replay once — unless
        replay=False (non-idempotent commands), which surfaces the timeout
        to the caller for explicit recovery. If auth_replay is false, an
        authentication-required reply raises LinkClosed instead of re-login
        and replay; this is required for IDs bound to the prior login epoch."""
        async with self._lock:
            await self._respect_quiet()
            self._check_cancel(cancel_guard)
            try:
                reply = await self._command_once(
                    line, timeout, expect, auth_replay=auth_replay,
                    cancel_guard=cancel_guard)
                # A cancellation EVT can arrive while the already-written
                # command is draining. The wire remains synchronized, but its
                # caller must not start a subsequent stage.
                self._check_cancel(cancel_guard)
                return reply
            except CommandTimeout:
                if not replay:
                    raise
                # Possibly a silently-dropped unauth command (the firmware's
                # nag is rate-limited to one per 2s) or a device reboot.
                log.info("timeout on %r — re-login and replay once", _redact(line))
                # A dismissal may have arrived while the timed-out command
                # drained. Do not hold the serialized link through up to three
                # 30-second login attempts for work that is already terminal.
                # Once a login write is admitted we still drain that attempt's
                # outcome before observing the guard, preserving wire sync.
                self._check_cancel(cancel_guard)
                await self._login_locked(cancel_guard=cancel_guard)
                self._check_cancel(cancel_guard)
                reply = await self._command_once(
                    line, timeout, expect, auth_replay=auth_replay,
                    cancel_guard=cancel_guard)
                self._check_cancel(cancel_guard)
                return reply

    async def command_with_frames(
            self, line: str, *, timeout: float,
            cancel_guard: CancelGuard | None = None) -> tuple[Reply, list[bytes]]:
        """Send a command that streams binary frames BEFORE its text reply
        (voicefetch). Returns (reply, [frame_body, ...]). The reply's OK/Error
        status line terminates collection; frames arriving first are gathered.
        No replay: the stream is not idempotent."""
        async with self._lock:
            await self._respect_quiet()
            self._drain_stale()
            self._check_cancel(cancel_guard)
            self._write(line)
            frames: list[bytes] = []
            reply = Reply()
            cancelled = False
            deadline = time.monotonic() + timeout
            while True:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    raise CommandTimeout(f"no reply within {timeout:.0f}s")
                try:
                    ev = await asyncio.wait_for(self._t.rx.get(), wait)
                except asyncio.TimeoutError:
                    raise CommandTimeout(f"no reply within {timeout:.0f}s")
                if ev.kind == "closed":
                    raise LinkClosed("serial link closed")
                if ev.kind == "garbage":
                    self._mark_reboot_suspected()
                    continue
                if ev.kind == "frame":
                    # EVT pushes may interleave with a voicefetch stream —
                    # route them out so they are neither lost nor mistaken
                    # for audio data.
                    if not self._route_frame(ev.frame):
                        if not cancelled:
                            frames.append(ev.frame)
                    if cancel_guard is not None and cancel_guard():
                        # Do not abandon an untagged frame stream halfway: its
                        # remaining frames/status could corrupt the next reply.
                        # Drain to the status boundary while discarding data.
                        cancelled = True
                        frames.clear()
                    continue
                # text line: the terminating status reply
                if protocol.is_status_line(ev.text):
                    reply.lines.append(ev.text)
                    if cancelled or (cancel_guard is not None and cancel_guard()):
                        raise CommandCancelled("guard cancelled during frame stream")
                    return reply, frames
                log.debug("stray line during frame stream: %r", ev.text)

    async def pump_events(self) -> None:
        """Idle event pump (daemon-mode background task): consume link events
        while no command is in flight so EVT pushes are seen promptly rather
        than at the next command's stale-drain. Skips whenever the command
        lock is held — the in-flight command's collector routes events
        itself, and this task must never steal its reply (asyncio.Lock is
        FIFO-fair, so a waiting command is served before the pump re-enters).
        Raises LinkClosed when the link dies while idle, so the daemon
        supervisor reconnects even with no exchange running."""
        while True:
            if self._lock.locked():
                await asyncio.sleep(0.1)
                continue
            async with self._lock:
                try:
                    # 0.10s slice: a command arriving mid-slice queues behind
                    # it, so this bounds the latency the pump can add to any
                    # command (measured: 0.2s stretched micrecord polls from
                    # 250ms cadence to ~650ms during wake exchanges).
                    ev = await asyncio.wait_for(self._t.rx.get(), 0.10)
                except asyncio.TimeoutError:
                    continue
                self._note_stray(ev)

    async def quiesce(self, seconds: float) -> None:
        """Go quiet (OTA probation / reboot settling): no commands until then."""
        self._quiet_until = max(self._quiet_until, time.monotonic() + seconds)

    async def settle(self) -> None:
        """Block until any pending quiet period has elapsed.

        quiesce() only arms the deadline; the wait otherwise surfaces inside
        whatever command happens to run next, which charges it to that
        exchange. Callers with idle time to spend await this instead so the
        cost lands there rather than on a user-facing request."""
        await self._respect_quiet()

    @property
    def reboot_suspected(self) -> bool:
        return self._reboot_suspected

    @property
    def reboot_generation(self) -> int:
        """Monotonic token for distinct suspected-reboot episodes."""
        return self._reboot_generation

    def add_reboot_listener(self, listener: Callable[[], None]) -> None:
        """Register a cheap loop-thread callback for the first reboot hint."""
        if listener not in self._reboot_listeners:
            self._reboot_listeners.append(listener)

    async def wait_for_reboot_after(self, generation: int) -> None:
        """Wait until a reboot episode newer than ``generation`` is seen."""
        while self._reboot_generation <= generation:
            self._reboot_changed.clear()
            if self._reboot_generation > generation:
                return
            await self._reboot_changed.wait()

    def clear_reboot_flag(self) -> None:
        self._reboot_suspected = False

    def _mark_reboot_suspected(self) -> None:
        if self._reboot_suspected:
            return
        self._reboot_suspected = True
        self._reboot_generation += 1
        self._reboot_changed.set()
        # Callbacks run on this asyncio loop and must only flip local state.
        # CM5 presence moves to STARTING synchronously so a READY heartbeat
        # cannot be re-login/replayed into the new UART epoch.
        for listener in tuple(self._reboot_listeners):
            try:
                listener()
            except Exception:
                log.exception("reboot listener failed")

    # -- internals ---------------------------------------------------------

    async def _respect_quiet(self) -> None:
        delay = self._quiet_until - time.monotonic()
        if delay > 0:
            log.info("quiet period: waiting %.1fs", delay)
            await asyncio.sleep(delay)

    def _write(self, line: str) -> None:
        """TX with link-failure mapping: any transport error is LinkClosed
        so the daemon's reconnect supervisor handles it uniformly."""
        try:
            self._t.write_line(line)
        except ValueError:
            raise                     # client-side cap violation — a bug, not a link state
        except Exception as exc:
            raise LinkClosed(f"write failed: {exc}") from exc

    async def _command_once(self, line: str, timeout: float, expect: str, *,
                            auth_replay: bool = True,
                            cancel_guard: CancelGuard | None = None) -> Reply:
        self._drain_stale()
        # This check is intentionally after stale drain: the cancellation EVT
        # may already be queued while this command waited for the lock.
        self._check_cancel(cancel_guard)
        self._write(line)
        reply = await self._collect(timeout, expect)
        if any(protocol.is_auth_required(ln) for ln in reply.lines):
            # The command did NOT execute (rejected at the auth gate), so a
            # replay after login is safe even for non-idempotent commands.
            # Treat an unexplained epoch loss as a reboot hint before the
            # first replay boundary. Presence listeners synchronously fence a
            # captured READY heartbeat so it cannot authorize the new epoch.
            self._mark_reboot_suspected()
            if not auth_replay:
                raise LinkClosed(
                    "authenticated UART epoch ended during epoch-bound command")
            log.info("session lost — re-login and replay %r", _redact(line))
            self._check_cancel(cancel_guard)
            await self._login_locked(cancel_guard=cancel_guard)
            self._drain_stale()
            self._check_cancel(cancel_guard)
            self._write(line)
            reply = await self._collect(timeout, expect)
        return reply

    @staticmethod
    def _check_cancel(cancel_guard: CancelGuard | None) -> None:
        if cancel_guard is not None and cancel_guard():
            raise CommandCancelled("command cancelled before write")

    async def _login_locked(
            self, *, cancel_guard: CancelGuard | None = None) -> None:
        attempt = 0
        while True:
            # Safe pre-write cancellation boundary. If a prior login attempt
            # was already written, _collect_login drained its response/timeout
            # before control returned here.
            self._check_cancel(cancel_guard)
            attempt += 1
            self._drain_stale()
            # Stale drain can itself route the cancellation EVT that flips the
            # guard. Recheck at the actual pre-write boundary so a queued
            # dismissal cannot admit an unnecessary login attempt.
            self._check_cancel(cancel_guard)
            try:
                login_user = _firmware_cli_token(self._user)
                login_password = _firmware_cli_token(self._password)
            except ValueError as exc:
                raise LoginFailed(str(exc)) from exc
            self._write(f"login {login_user} {login_password}")
            outcome = await self._collect_login(30.0)
            self._check_cancel(cancel_guard)
            if outcome == "ok":
                # Deliberately does NOT clear reboot_suspected: a successful
                # re-login proves the session works again, not that no reboot
                # happened — the pipeline still needs the hint to quiesce.
                return
            if outcome == "timeout":
                if attempt >= 3:
                    raise LoginFailed("no reply to login (3 attempts)")
                await asyncio.sleep(1.0)
                continue
            # outcome is a lockout duration in seconds
            log.warning("login locked out — backing off %ds", outcome + 1)
            await asyncio.sleep(outcome + 1)

    async def _collect_login(self, timeout: float) -> str | int:
        """Collect the login reply. ONLY login-shaped lines terminate:
        success, explicit auth failure, lockout, usage. Anything else —
        including a straggler status reply from a previously timed-out
        command — is a stray and is skipped (review finding: a late
        'OK: Recording stopped' must not become a LoginFailed)."""
        deadline = time.monotonic() + timeout
        while True:
            wait = deadline - time.monotonic()
            if wait <= 0:
                return "timeout"
            try:
                ev = await asyncio.wait_for(self._t.rx.get(), wait)
            except asyncio.TimeoutError:
                return "timeout"
            if ev.kind == "closed":
                raise LinkClosed("serial link closed")
            if ev.kind == "garbage":
                self._mark_reboot_suspected()
                continue
            if ev.kind == "frame":
                self._route_frame(ev.frame)   # EVT routed; other frames stray
                continue
            line = ev.text
            if line.startswith("OK: logged in as"):
                log.info("logged in: %s", line[4:])
                return "ok"
            if "authentication failed" in line.lower():
                raise LoginFailed("login rejected: invalid credentials")
            if line.startswith("Usage: login"):
                raise LoginFailed("login rejected: malformed login line")
            secs = protocol.lockout_seconds(line)
            if secs is not None:
                return secs
            log.debug("stray line during login: %r", line)

    def _drain_stale(self) -> None:
        while True:
            try:
                ev = self._t.rx.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._note_stray(ev)

    def _note_stray(self, ev: LinkEvent) -> None:
        if ev.kind == "garbage":
            self._mark_reboot_suspected()
        elif ev.kind == "line":
            log.debug("stray line: %r", ev.text)
        elif ev.kind == "frame":
            if not self._route_frame(ev.frame):
                log.debug("stray frame (%d bytes)", len(ev.frame))
        elif ev.kind == "closed":
            raise LinkClosed("serial link closed")

    def _route_frame(self, body: bytes) -> bool:
        """Route an EVT frame's payload to on_event. Returns True when the
        frame was an EVT (consumed here); False for any other frame type —
        the caller decides what a data frame means in its context."""
        try:
            ftype, _seq, payload = protocol.parse_frame_body(body)
        except ValueError:
            return False   # transport already validated; stay tolerant
        if ftype != protocol.FRAME_EVT:
            return False
        cb = self.on_event
        if cb is None:
            log.info("device event (no handler): %r", payload)
            return True
        try:
            cb(payload)  # type: ignore[operator]
        except Exception:
            log.exception("on_event handler failed for %r", payload)
        return True

    async def _collect(self, timeout: float, expect: str) -> Reply:
        reply = Reply()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            in_gap = bool(reply.lines) and expect == "auto"
            if in_gap:
                # Quiet-gap mode — but ALWAYS bounded by the command
                # deadline, or a garbage/line flood extends collection
                # forever (the wedged-daemon review finding).
                wait = min(protocol.QUIET_GAP_S, remaining)
                if remaining <= 0:
                    return reply           # deadline: partial reply beats a hang
            else:
                wait = remaining
                if wait <= 0:
                    raise CommandTimeout(f"no reply within {timeout:.0f}s")
            try:
                ev = await asyncio.wait_for(self._t.rx.get(), wait)
            except asyncio.TimeoutError:
                if in_gap:
                    return reply           # quiet gap elapsed: reply complete
                raise CommandTimeout(f"no reply within {timeout:.0f}s")

            if ev.kind == "closed":
                raise LinkClosed("serial link closed")
            if ev.kind == "garbage":
                self._mark_reboot_suspected()
                continue
            if ev.kind == "frame":
                self._route_frame(ev.frame)   # EVT routed; other frames stray
                continue

            line = ev.text
            if expect == "json":
                if protocol.is_error_line(line):
                    reply.lines.append(line)
                    return reply
                try:
                    reply.json = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("non-JSON line while expecting json: %r", line)
                    continue
                reply.lines.append(line)
                return reply

            if expect == "status":
                if protocol.is_status_line(line):
                    reply.lines.append(line)
                    return reply
                log.debug("stray line while expecting status: %r", line)
                continue

            # auto: accumulate with quiet-gap termination. No fast return on
            # a status first line — multi-line successes arrive stamped
            # 'OK: ...' on line one and the body would be silently dropped.
            reply.lines.append(line)
            if len(reply.lines) >= _MAX_REPLY_LINES:
                log.warning("reply exceeded %d lines — truncating collection",
                            _MAX_REPLY_LINES)
                return reply


def _redact(line: str) -> str:
    stripped = line.lstrip()
    verb = stripped.split(maxsplit=1)[0].casefold() if stripped else ""
    if verb == "login":
        return "login <redacted>"
    return line if len(line) < 60 else line[:57] + "..."
