"""Link-health counters — the one thing the text channel cannot prove alone.

The TEXT command channel carries no integrity check; only the P2 binary frames
get a CRC. Damage on it is therefore never reported as damage — it arrives as a
strange reply, and every actor that recovers from one does so quietly. These
counters are the running tally of what was noticed and survived, and
``cm5_linkhealth`` pushes them to the firmware so the device can report them
from its own CLI (`cm5 linkhealth`) without anyone SSHing into the Pi.

Counters, not rates, on purpose: the ESP32 renders them, and a monotonic count
next to its denominator (``tx``/``rx``) is something a human can reason about
at whatever interval they happened to look.

Lifetime is the DAEMON's, not the link's — they deliberately survive a
reconnect, because "12 damaged verbs since Tuesday" is the shape of the
question being asked. ``resets`` is what makes that readable.

Threading: ``garbage`` and ``rx`` are bumped on the reader thread, everything
else on the asyncio loop. ``+= 1`` is a read-modify-write, so a simultaneous
pair can lose one — accepted, as it already was for ``garbage_count``, because
these are a diagnostic tally and never a control input. Nothing branches on
them.
"""

from __future__ import annotations

import time


class LinkHealth:
    __slots__ = ("garbage", "corrupt", "timeouts", "strays", "logins",
                 "resets", "tx", "rx", "_started", "fault_generation")

    def __init__(self) -> None:
        self.garbage = 0      # framing/COBS incidents seen by the reader
        self.corrupt = 0      # replies echoing a verb we never wrote
        self.timeouts = 0     # commands that got no reply in time
        self.strays = 0       # unmatched reply lines dropped
        self.logins = 0       # successful UART logins
        self.resets = 0       # link reconnects
        self.tx = 0           # command lines written
        self.rx = 0           # text lines read
        self._started = time.monotonic()
        # Bumped by every FAULT counter (never by tx/rx), so a reporter can
        # tell "something just went wrong" from "nothing has changed" without
        # diffing a whole snapshot.
        self.fault_generation = 0

    # -- reader thread -----------------------------------------------------

    def note_garbage(self) -> None:
        self.garbage += 1
        self.fault_generation += 1

    def note_rx_line(self) -> None:
        self.rx += 1

    # -- loop thread -------------------------------------------------------

    def note_corrupt(self) -> None:
        self.corrupt += 1
        self.fault_generation += 1

    def note_timeout(self) -> None:
        self.timeouts += 1
        self.fault_generation += 1

    def note_stray(self) -> None:
        self.strays += 1
        self.fault_generation += 1

    def note_login(self) -> None:
        self.logins += 1

    def note_reset(self) -> None:
        self.resets += 1
        self.fault_generation += 1

    def note_tx(self) -> None:
        self.tx += 1

    # -- reporting ---------------------------------------------------------

    @property
    def uptime_s(self) -> int:
        return int(time.monotonic() - self._started)

    def fields(self) -> dict[str, int]:
        """The wire form. Key order is stable so the pushed line is diffable
        by eye in a log; the firmware parses by key and ignores what it does
        not know, so adding one here does not need a firmware flash."""
        return {
            "garbage": self.garbage,
            "corrupt": self.corrupt,
            "timeouts": self.timeouts,
            "strays": self.strays,
            "logins": self.logins,
            "resets": self.resets,
            "tx": self.tx,
            "rx": self.rx,
            "up": self.uptime_s,
        }


# Tolerant accessors. The actors and the supervisor accept plain doubles in
# tests, and a missing tally must never be the reason a recovery path changes
# shape — counting is the least important thing any of these call sites does.

def _health_of(obj: object) -> LinkHealth | None:
    health = getattr(obj, "health", None)
    return health if isinstance(health, LinkHealth) else None


def note_corrupt(session: object) -> None:
    """Count a damaged verb against ``session``'s link."""
    health = _health_of(session)
    if health is not None:
        health.note_corrupt()


def note_reset(link: object) -> None:
    """Count a reconnect against ``link`` (a transport or a session)."""
    health = _health_of(link)
    if health is not None:
        health.note_reset()
