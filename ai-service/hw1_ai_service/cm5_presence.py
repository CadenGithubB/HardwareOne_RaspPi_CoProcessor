"""CM5 daemon readiness heartbeat for the HardwareOne UART peer.

One asyncio actor owns all heartbeat commands, preserving Session's strict
one-command-at-a-time reply discipline.  The firmware binds every accepted
update to the current UART login epoch and expires it independently.
"""

from __future__ import annotations

import asyncio
import logging
import re
from enum import StrEnum

from .link.session import (
    CommandCancelled,
    CommandTimeout,
    LinkClosed,
    LoginFailed,
)


log = logging.getLogger("cm5.presence")

PROTOCOL_VERSION = 1
HEARTBEAT_INTERVAL_S = 5.0
HEARTBEAT_TIMEOUT_S = 10.0
LEGACY_REPROBE_INTERVAL_S = 60.0
NORMAL_LEASE_MS = 15_000
BUSY_LEASE_MS = 75_000

# Trailing UNKNOWN fields are tolerated; the four load-bearing ones are still
# mandatory, positional, and fully validated below. Without the tail clause a
# firmware that appends one field puts every deployed daemon into a permanent
# reconnect loop — a mismatch here raises LinkClosed, which tears down the task
# group, and the very first heartbeat of the new epoch fails the same way. That
# is a total outage for an additive change, so the tolerance is the point.
_REPLY_RE = re.compile(
    r"^OK: cm5 heartbeat version=1 seq=([0-9]+) "
    r"state=(starting|ready|busy|degraded) "
    r"session_epoch=([0-9]+) lease_ms=([0-9]+)(?: .*)?$")


class Cm5PresenceMode(StrEnum):
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    DEGRADED = "degraded"


class Cm5Presence:
    """Single-writer heartbeat actor with acknowledged state transitions."""

    def __init__(self, session, *, interval_s: float = HEARTBEAT_INTERVAL_S,
                 timeout_s: float = HEARTBEAT_TIMEOUT_S,
                 legacy_reprobe_s: float = LEGACY_REPROBE_INTERVAL_S) -> None:
        self._session = session
        self._interval_s = interval_s
        self._timeout_s = timeout_s
        self._legacy_reprobe_s = legacy_reprobe_s
        self._desired = Cm5PresenceMode.STARTING
        self._desired_generation = 1
        self._acknowledged_generation = 0
        self._sequence = 0
        self._state_changed = asyncio.Event()
        self._ack_changed = asyncio.Event()
        self._failure: BaseException | None = None
        self._supported: bool | None = None
        self._running = False
        # BUSY is a REFCOUNT with named holders, not a flag. STT and a
        # CM5-routed generation overlap freely (a wearer wake while the web UI
        # drives `cm5:<model>`), and whichever finished first would otherwise
        # drop the SHARED lease back to READY while the other was still
        # working — which is precisely the stale lease the firmware abandons a
        # live generation for. The names never reach the wire: the firmware's
        # heartbeat grammar is closed at starting|ready|busy|degraded
        # (System_Cm5Presence.cpp), so they exist for this host's logs and
        # diagnostics only.
        self._busy_holds: dict[int, str] = {}
        self._busy_seq = 0
        add_reboot_listener = getattr(session, "add_reboot_listener", None)
        if callable(add_reboot_listener):
            add_reboot_listener(self._reboot_suspected)

    @property
    def mode(self) -> Cm5PresenceMode:
        return self._desired

    @property
    def supported(self) -> bool | None:
        return self._supported

    @property
    def busy_reasons(self) -> tuple[str, ...]:
        """What this host is currently busy doing, for logs and diagnostics."""
        return tuple(self._busy_holds.values())

    def _busy_summary(self) -> str:
        return ", ".join(sorted(self._busy_holds.values())) or "idle"

    async def acquire_busy(self, reason: str) -> int:
        """Take a named share of the BUSY lease; wait for the device to ack it.

        Acknowledged before the caller starts its long work, so the device has
        already widened the lease by the time the host goes quiet on it.
        STARTING and DEGRADED are stronger statements than "working" and are
        left alone.
        """
        self._busy_seq += 1
        token = self._busy_seq
        self._busy_holds[token] = reason
        log.info("CM5 busy: %s", self._busy_summary())
        if self._desired in (Cm5PresenceMode.READY, Cm5PresenceMode.BUSY):
            await self.set_mode(Cm5PresenceMode.BUSY)
        return token

    def release_busy(self, token: int, *,
                     fallback: Cm5PresenceMode | str | None = None) -> None:
        """Drop one named share.

        Never awaits: callers release inside finally blocks that may already be
        running under TaskGroup cancellation, where awaiting a sibling actor
        would not complete anyway.
        """
        fallback = (Cm5PresenceMode.READY if fallback is None
                    else Cm5PresenceMode(fallback))
        if self._busy_holds.pop(token, None) is None:
            return
        if fallback is Cm5PresenceMode.DEGRADED:
            # A fault outranks the refcount. Staying BUSY because a sibling is
            # still working would hide that this host is impaired.
            log.info("CM5 degraded (was: %s)", self._busy_summary())
            self.set_mode_nowait(Cm5PresenceMode.DEGRADED)
            return
        if self._busy_holds:
            log.info("CM5 busy: %s", self._busy_summary())
            return
        if self._desired is Cm5PresenceMode.BUSY:
            log.info("CM5 idle")
            self.set_mode_nowait(fallback)

    def set_mode_nowait(self, mode: Cm5PresenceMode | str) -> int:
        mode = Cm5PresenceMode(mode)
        if mode != self._desired:
            self._desired = mode
            self._desired_generation += 1
            # A legacy peer is reprobed on its own slow cadence. Ordinary
            # pipeline state changes must not turn that into command spam.
            if self._supported is not False:
                self._state_changed.set()
        return self._desired_generation

    async def set_mode(self, mode: Cm5PresenceMode | str) -> bool:
        """Publish `mode`; return False only when peer firmware is legacy."""
        generation = self.set_mode_nowait(mode)
        while self._acknowledged_generation < generation:
            if self._supported is False:
                return False
            if self._failure is not None:
                raise self._failure
            self._ack_changed.clear()
            if (self._acknowledged_generation >= generation or
                    self._supported is False or self._failure is not None):
                continue
            await self._ack_changed.wait()
        return self._supported is not False

    def link_reset(self) -> None:
        """Invalidate the old login epoch before a supervisor reconnect."""
        # Every holder's work is bound to the epoch that just ended, so the
        # refcount goes with it rather than pinning BUSY into the new one.
        self._busy_holds.clear()
        self._desired = Cm5PresenceMode.STARTING
        self._desired_generation += 1
        self._acknowledged_generation = 0
        self._failure = None
        self._supported = None
        self._state_changed.set()
        self._ack_changed.set()

    def _reboot_suspected(self) -> None:
        """Fail closed before Session can re-login/replay a stale READY."""
        self.link_reset()

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("CM5 presence actor is already running")
        self._running = True
        loop = asyncio.get_running_loop()
        next_deadline = loop.time()
        last_sent_generation = 0
        try:
            while True:
                generation = self._desired_generation
                mode = self._desired
                now = loop.time()
                state_due = generation != last_sent_generation
                if not state_due and now < next_deadline:
                    self._state_changed.clear()
                    if self._desired_generation != generation:
                        continue
                    try:
                        await asyncio.wait_for(
                            self._state_changed.wait(), next_deadline - now)
                        continue
                    except asyncio.TimeoutError:
                        pass

                sequence = self._next_sequence()
                command = (
                    f"cm5 heartbeat {PROTOCOL_VERSION} {sequence} {mode.value}")
                if self._reboot_fences(mode):
                    raise LinkClosed(
                        "CM5 presence fenced by suspected device reboot")
                try:
                    reply = await self._session.command(
                        command, expect="status", timeout=self._timeout_s,
                        replay=False,
                        cancel_guard=lambda: (
                            self._desired_generation != generation or
                            self._reboot_fences(mode)))
                except CommandCancelled:
                    # A mode edge (especially reboot -> STARTING) invalidated
                    # this exact heartbeat. Session drained any admitted reply
                    # and prevented stale auth replay at its safe boundaries.
                    if self._reboot_fences(mode):
                        raise LinkClosed(
                            "CM5 presence fenced by suspected device reboot")
                    continue
                except LinkClosed:
                    raise
                except (CommandTimeout, LoginFailed) as exc:
                    raise LinkClosed(f"CM5 heartbeat failed: {exc}") from exc

                if not reply.ok:
                    if reply.text.startswith("Unknown command"):
                        first_legacy_observation = self._supported is not False
                        self._supported = False
                        self._acknowledged_generation = self._desired_generation
                        self._ack_changed.set()
                        if first_legacy_observation:
                            log.warning(
                                "firmware does not support cm5-presence-v1; "
                                "continuing with legacy UART behavior and "
                                "reprobing every %.0fs",
                                self._legacy_reprobe_s)
                        # Stay inert between bounded compatibility probes. A
                        # link reset wakes this wait immediately, so a daemon-
                        # first rollout discovers newly upgraded firmware.
                        self._state_changed.clear()
                        try:
                            await asyncio.wait_for(
                                self._state_changed.wait(),
                                self._legacy_reprobe_s)
                        except asyncio.TimeoutError:
                            pass
                        self._supported = None
                        continue
                    raise LinkClosed(
                        f"CM5 heartbeat rejected by firmware: {reply.text}")

                self._validate_reply(reply.text, sequence, mode)
                self._supported = True
                last_sent_generation = generation
                if generation > self._acknowledged_generation:
                    self._acknowledged_generation = generation
                    self._ack_changed.set()

                # A state edge resets the cadence. Slow replies do not cause a
                # burst of catch-up heartbeats; the next deadline is absolute
                # from this acknowledged send.
                next_deadline = loop.time() + self._interval_s
        except asyncio.CancelledError:
            # Wake any acknowledged-mode waiter owned by a sibling task. A
            # TaskGroup cancels the heartbeat actor and pipeline together; a
            # silent cancellation here would otherwise strand that sibling.
            self._failure = LinkClosed("CM5 presence actor stopped")
            self._ack_changed.set()
            raise
        except BaseException as exc:
            self._failure = exc
            self._ack_changed.set()
            raise
        finally:
            self._running = False

    def _next_sequence(self) -> int:
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        if self._sequence == 0:
            self._sequence = 1
        return self._sequence

    def _reboot_fences(self, mode: Cm5PresenceMode) -> bool:
        return (mode is not Cm5PresenceMode.STARTING and
                bool(getattr(self._session, "reboot_suspected", False)))

    @staticmethod
    def _validate_reply(text: str, sequence: int,
                        mode: Cm5PresenceMode) -> None:
        match = _REPLY_RE.fullmatch(text)
        if match is None:
            raise LinkClosed(f"malformed CM5 heartbeat reply: {text!r}")
        reply_sequence = int(match.group(1), 10)
        reply_mode = match.group(2)
        session_epoch = int(match.group(3), 10)
        lease_ms = int(match.group(4), 10)
        expected_lease = (BUSY_LEASE_MS if mode is Cm5PresenceMode.BUSY
                          else NORMAL_LEASE_MS)
        if (reply_sequence != sequence or reply_mode != mode.value or
                session_epoch == 0 or lease_ms != expected_lease):
            raise LinkClosed(
                "CM5 heartbeat reply did not match request/session lease")
