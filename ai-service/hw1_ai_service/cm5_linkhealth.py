"""Push this host's link-health tally to the firmware.

Direction of initiative is the usual one: the XIAO is the UART server and the
daemon is the client, so the device cannot ask. The daemon pushes a snapshot on
a slow cadence, and promptly after a fault, and the firmware keeps the latest
one for `cm5 linkhealth` to print. That is the whole point of the exercise —
the counters that would have named the 2026-08-25 outage lived only in this
process, on a Pi with no network, and were unreachable from the bench.

Wire form (host -> firmware), forward-compatible by construction:

    cm5 linkhealth 1 garbage=<n> corrupt=<n> timeouts=<n> strays=<n> \
        logins=<n> resets=<n> tx=<n> rx=<n> up=<n>

The firmware parses by KEY and ignores keys it does not know, so a counter
added to LinkHealth.fields() reaches a `cm5 linkhealth`-capable build without a
flash. Values are canonical decimal and clamped to uint32 here rather than at
the parser: a host that has been up for months must degrade to a saturated
number, never to a wrapped one that reads as healthy.
"""

from __future__ import annotations

import asyncio
import logging

from .link import protocol
from .link.health import note_corrupt
from .link.session import (
    CommandCancelled,
    CommandTimeout,
    LinkClosed,
    LoginFailed,
)

log = logging.getLogger("cm5.linkhealth")

PROTOCOL_VERSION = 1

# Slow by design. This is a diagnostic tally, not telemetry: every push is a
# real command on a link whose whole problem is that commands are expensive
# (the firmware pays a full-FS scan per audited command). 30s costs ~2 lines a
# minute and still gives a bench operator a live-enough number.
PUSH_INTERVAL_S = 30.0

# A fault should not wait out the cadence — that is exactly the moment someone
# is staring at the device wondering what just happened. Bounded by a floor so
# a damaged link cannot turn its own damage into a command flood.
FAULT_PUSH_DELAY_S = 2.0
MIN_PUSH_SPACING_S = 5.0

UINT32_MAX = 0xFFFFFFFF


class Cm5LinkHealth:
    """Periodic, best-effort reporter. Never fails a link on its own account.

    Nothing in the daemon reads these counters back, and nothing branches on
    them, so every failure here is logged and dropped rather than raised —
    with one exception: LinkClosed is the supervisor's business and passes
    through untouched.
    """

    def __init__(self, session, *, interval_s: float = PUSH_INTERVAL_S,
                 timeout_s: float = 10.0) -> None:
        self._session = session
        self._interval_s = interval_s
        self._timeout_s = timeout_s
        self._supported: bool | None = None
        self._last_fault_generation = 0

    @property
    def supported(self) -> bool | None:
        return self._supported

    def link_reset(self) -> None:
        """Re-probe after a reconnect: the replacement peer may be a different
        build, and the tally itself deliberately survives."""
        self._supported = None
        self._last_fault_generation = 0

    async def run(self) -> None:
        health = getattr(self._session, "health", None)
        if health is None:                      # a session double in tests
            return
        # Report once at link-up so the device has a number before anything
        # goes wrong, then settle into the cadence.
        await self._push_once()
        while True:
            await self._wait_for_next(health)
            await self._push_once()

    async def _wait_for_next(self, health) -> None:
        """Sleep out the cadence, but cut it short for a fresh fault.

        Polled rather than event-driven on purpose: the counters are bumped
        from the reader THREAD as well as the loop, and an asyncio.Event set
        from another thread is a race. A 1s poll against a 30s cadence is
        free, and the fault path is still reported within a couple of seconds.
        """
        waited = 0.0
        while waited < self._interval_s:
            await asyncio.sleep(1.0)
            waited += 1.0
            if (health.fault_generation != self._last_fault_generation and
                    waited >= MIN_PUSH_SPACING_S):
                # Let a burst finish landing before snapshotting it.
                await asyncio.sleep(FAULT_PUSH_DELAY_S)
                return

    async def _push_once(self) -> bool:
        if self._supported is False:
            return False
        health = getattr(self._session, "health", None)
        if health is None:
            return False
        fields = health.fields()
        self._last_fault_generation = health.fault_generation
        command = " ".join(
            [f"cm5 linkhealth {PROTOCOL_VERSION}"] +
            [f"{k}={min(int(v), UINT32_MAX)}" for k, v in fields.items()])
        try:
            reply = await self._session.command(
                command, expect="status", timeout=self._timeout_s,
                replay=False)
        except CommandCancelled:
            return False
        except LinkClosed:
            raise
        except (CommandTimeout, LoginFailed) as exc:
            # Diagnostics must never be the reason a link is declared dead —
            # the heartbeat actor is the liveness detector.
            log.debug("link-health push failed: %s", exc)
            return False

        if reply.ok:
            self._supported = True
            return True
        verdict = protocol.classify_unknown_command(reply.text, command)
        if verdict == protocol.UNKNOWN_CORRUPT:
            note_corrupt(self._session)
            log.warning(
                "link damaged the link-health push in transit (device parsed "
                "the verb as %r) — retrying on the next tick",
                protocol.unknown_command_echo(reply.text))
            return False
        if verdict == protocol.UNKNOWN_MISSING:
            if self._supported is None:
                log.info("firmware has no `cm5 linkhealth` — link counters "
                         "stay host-side only (journalctl)")
            self._supported = False
            return False
        log.warning("link-health push rejected: %s", reply.text)
        return False
