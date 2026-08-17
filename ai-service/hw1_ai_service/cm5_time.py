"""CM5 clock-anchor push for the HardwareOne UART peer.

The CM5 carrier owns a battery-backed hardware RTC and (when online) NTP, so on
the carrier firmware build — where the ESP has no DS3231 and NTP needs WiFi — it
is the only accurate wall-clock source on a dark boot. This actor periodically
pushes the Pi's own UTC time to the firmware as::

    cm5 time set 1 <epoch_sec_utc> <flags_u8>

One asyncio actor owns all such commands, preserving Session's strict
one-command-at-a-time reply discipline. The firmware validates + stashes the
value and applies it on its main loop (source=cm5), never on the RX stack; the
push is idempotent and self-quenching (the firmware only steps on >120 s drift).

Flags byte — the Pi's confidence in its own clock (only bit0/bit1 defined):
  bit0 pi_clock_synced — timedatectl reports the clock NTP-disciplined now.
  bit1 pi_rtc_valid    — a real hardware RTC (/dev/rtc*) read back a plausible
                         time (i.e. the battery is alive and was disciplined).
The firmware adopts a dark clock on (bit0 || bit1) and only *corrects* an
already-valid clock on bit0, so a dead-battery/pre-NTP Pi cannot seed or push a
plausible-but-wrong time into dated files.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import time

from .link.session import (
    CommandCancelled,
    CommandTimeout,
    LinkClosed,
    LoginFailed,
)


log = logging.getLogger("cm5.time")

PROTOCOL_VERSION = 1
# Steady-state re-push cadence. The value of a periodic push is only drift
# correction, and the ESP crystal drifts ~4 s/day worst-case — far under the
# firmware's 120 s correction threshold — so once a day is ample. The important
# events (dark-boot adopt, post-reboot re-anchor) are driven by the immediate
# push on start and on link_reset, NOT by this interval.
PUSH_INTERVAL_S = 86_400.0  # 24 h
# Until a *confident* anchor has actually landed, retry on this fast cadence so a
# Pi that is briefly unconfident at connect (RTC not yet read, NTP not yet up)
# re-pushes within seconds of gaining confidence instead of waiting a full day.
RETRY_INTERVAL_S = 60.0
PUSH_TIMEOUT_S = 10.0

# Confidence flag bits — mirror CM5_TIME_FLAG_* in System_Cm5Presence.h.
FLAG_PI_SYNCED = 0x01
FLAG_PI_RTC_VALID = 0x02

# Epoch plausibility window — mirror Clock::isPlausibleEpoch (2020..2100).
PLAUSIBLE_MIN = 1_577_836_800   # 2020-01-01T00:00:00Z
PLAUSIBLE_MAX = 4_102_444_800   # 2100-01-01T00:00:00Z

_REPLY_RE = re.compile(
    r"^OK: cm5 time set epoch=([0-9]+) flags=([0-9]+) "
    r"action=stashed session_epoch=([0-9]+)$")


def _timedatectl_value(prop: str) -> str | None:
    """Return the raw value of a timedatectl property, or None on any failure."""
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", prop, "--value"],
            capture_output=True, text=True, timeout=2.0)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def _hardware_rtc_plausible() -> bool:
    """True iff a real hardware RTC device exists AND its time is >= 2020.

    Distinguishes a battery-backed RTC from systemd fake-hwclock: the latter has
    no /dev/rtc backing and reports RTCTimeUSec=0. A dead-battery RTC reads back
    an implausible (pre-2020) time and is correctly rejected here.
    """
    if not (os.path.exists("/dev/rtc0") or os.path.exists("/dev/rtc")):
        return False
    raw = _timedatectl_value("RTCTimeUSec")
    if not raw:
        return False
    try:
        rtc_epoch = int(raw) // 1_000_000
    except ValueError:
        return False
    return PLAUSIBLE_MIN <= rtc_epoch < PLAUSIBLE_MAX


def read_local_confidence() -> int:
    """Compute the confidence flags byte from the Pi's own clock state.

    Fully defensive: any probe failure simply leaves its bit clear, which makes
    the firmware more conservative (won't adopt/correct), never less.
    """
    flags = 0
    if _timedatectl_value("NTPSynchronized") == "yes":
        flags |= FLAG_PI_SYNCED
    if _hardware_rtc_plausible():
        flags |= FLAG_PI_RTC_VALID
    return flags


class Cm5Time:
    """Single-writer clock-push actor.

    Pushes immediately on start (fast dark-boot time-to-first-fix as soon as the
    UART session is authenticated) and then every ``interval_s``. A link reset
    or suspected device reboot forces an immediate re-push on reconnect.
    """

    def __init__(self, session, *, interval_s: float = PUSH_INTERVAL_S,
                 retry_interval_s: float = RETRY_INTERVAL_S,
                 timeout_s: float = PUSH_TIMEOUT_S,
                 confidence_fn=read_local_confidence,
                 clock_fn=time.time) -> None:
        self._session = session
        self._interval_s = interval_s
        self._retry_interval_s = retry_interval_s
        self._timeout_s = timeout_s
        self._confidence_fn = confidence_fn
        self._clock_fn = clock_fn
        self._wake = asyncio.Event()
        self._running = False
        self._settled = False
        self._supported: bool | None = None
        add_reboot_listener = getattr(session, "add_reboot_listener", None)
        if callable(add_reboot_listener):
            add_reboot_listener(self._reboot_suspected)

    @property
    def supported(self) -> bool | None:
        return self._supported

    @property
    def settled(self) -> bool:
        """True once a confident anchor has landed (or the peer is legacy);
        the actor is then on the lazy 24 h drift-correction cadence."""
        return self._settled

    def link_reset(self) -> None:
        """Re-anchor fast after a reconnect: the device may have rebooted dark,
        so drop back to the retry cadence until a confident push lands again."""
        self._supported = None
        self._settled = False
        self._wake.set()

    def _reboot_suspected(self) -> None:
        self.link_reset()

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("CM5 time actor is already running")
        self._running = True
        try:
            while True:
                if await self._push_once():
                    self._settled = True
                # Retry fast until a confident anchor has landed (or the peer is
                # legacy); then fall back to the lazy drift-correction cadence.
                interval = (self._interval_s if self._settled
                            else self._retry_interval_s)
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._running = False

    async def _push_once(self) -> bool:
        """Push the current time once. Returns True when the actor may settle to
        the lazy cadence: a *confident* (flags != 0) push was accepted, or the
        peer firmware is legacy. Returns False to keep retrying (Pi not yet
        confident, transient reject, or the local clock isn't real yet)."""
        epoch = int(self._clock_fn())
        if not (PLAUSIBLE_MIN <= epoch < PLAUSIBLE_MAX):
            # The Pi's own clock is not yet real (cold boot, no RTC, no NTP).
            # Sending would only earn a range rejection; keep retrying.
            log.debug("skip cm5 time push: local clock implausible (%d)", epoch)
            return False
        flags = self._confidence_fn() & 0xFF
        command = f"cm5 time set {PROTOCOL_VERSION} {epoch} {flags}"
        try:
            reply = await self._session.command(
                command, expect="status", timeout=self._timeout_s,
                replay=False)
        except CommandCancelled:
            return False
        except LinkClosed:
            raise
        except (CommandTimeout, LoginFailed) as exc:
            # Let the supervisor reconnect (the heartbeat actor is the primary
            # liveness detector; this keeps the wire resynchronized).
            raise LinkClosed(f"CM5 time push failed: {exc}") from exc

        if not reply.ok:
            if reply.text.startswith("Unknown command"):
                if self._supported is not False:
                    self._supported = False
                    log.warning(
                        "firmware does not support cm5 time; clock push "
                        "disabled for this link")
                # No point hammering a legacy peer; a reconnect re-probes.
                return True
            # A rejected push (e.g. range) is not fatal, and not confident:
            # keep retrying so a corrected Pi clock lands promptly.
            log.warning("cm5 time push rejected: %s", reply.text)
            return False

        self._validate_reply(reply.text, epoch, flags)
        self._supported = True
        log.debug("cm5 time push accepted epoch=%d flags=%d", epoch, flags)
        # Settle only once a CONFIDENT anchor has landed — a flags=0 push was
        # stashed but the firmware won't act on it, so keep retrying until the
        # Pi gains NTP/RTC confidence.
        return flags != 0

    @staticmethod
    def _validate_reply(text: str, epoch: int, flags: int) -> None:
        match = _REPLY_RE.fullmatch(text)
        if match is None:
            raise LinkClosed(f"malformed CM5 time reply: {text!r}")
        reply_epoch = int(match.group(1), 10)
        reply_flags = int(match.group(2), 10)
        session_epoch = int(match.group(3), 10)
        if reply_epoch != epoch or reply_flags != flags or session_epoch == 0:
            raise LinkClosed(
                "CM5 time reply did not match request/session")
