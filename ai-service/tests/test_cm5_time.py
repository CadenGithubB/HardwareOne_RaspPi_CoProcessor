from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from conftest import run
from hw1_ai_service import cm5_time as cm5_time_mod
from hw1_ai_service.cm5_time import (
    FLAG_PI_RTC_VALID,
    FLAG_PI_SYNCED,
    PLAUSIBLE_MIN,
    Cm5Time,
)
from hw1_ai_service.link.session import LinkClosed


GOOD_EPOCH = 1_700_000_000  # 2023-11-14, comfortably plausible


class _Reply:
    def __init__(self, text: str, ok: bool = True) -> None:
        self.text = text
        self.ok = ok


class _Session:
    def __init__(self, *, session_epoch: int = 7) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.reply_override: _Reply | None = None
        self.session_epoch = session_epoch
        self._reboot_listeners: list = []

    def add_reboot_listener(self, listener) -> None:
        self._reboot_listeners.append(listener)

    async def command(self, line: str, **kwargs):
        self.calls.append((line, kwargs))
        if self.reply_override is not None:
            return self.reply_override
        # cm5 time set 1 <epoch> <flags>  ->  the firmware's stashed ACK.
        parts = line.split()
        epoch, flags = parts[4], parts[5]
        return _Reply(
            f"OK: cm5 time set epoch={epoch} flags={flags} "
            f"action=stashed session_epoch={self.session_epoch}")


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0)


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def _actor(session, **kw):
    kw.setdefault("clock_fn", lambda: float(GOOD_EPOCH))
    kw.setdefault("confidence_fn", lambda: FLAG_PI_SYNCED | FLAG_PI_RTC_VALID)
    return Cm5Time(session, **kw)


def test_push_sends_wellformed_command_and_marks_supported():
    async def main() -> None:
        session = _Session()
        actor = _actor(session)
        settle = await actor._push_once()
        assert len(session.calls) == 1
        line, kwargs = session.calls[0]
        assert line == f"cm5 time set 1 {GOOD_EPOCH} 3"
        assert kwargs["expect"] == "status"
        assert kwargs["timeout"] == 10.0
        assert kwargs["replay"] is False
        assert actor.supported is True
        assert settle is True  # confident push -> may settle to lazy cadence

    run(main())


def test_unconfident_push_sends_but_does_not_settle():
    async def main() -> None:
        session = _Session()
        actor = _actor(session, confidence_fn=lambda: 0)
        settle = await actor._push_once()
        assert session.calls[0][0] == f"cm5 time set 1 {GOOD_EPOCH} 0"
        assert actor.supported is True   # firmware understood + stashed it
        assert settle is False           # but flags=0 -> keep retrying fast

    run(main())


def test_push_carries_only_the_flags_the_probe_reports():
    async def main() -> None:
        session = _Session()
        actor = _actor(session, confidence_fn=lambda: FLAG_PI_RTC_VALID)
        await actor._push_once()
        assert session.calls[0][0] == f"cm5 time set 1 {GOOD_EPOCH} 2"

    run(main())


def test_implausible_local_clock_is_never_pushed():
    async def main() -> None:
        session = _Session()
        actor = _actor(session, clock_fn=lambda: 100.0)  # pre-2020
        settle = await actor._push_once()
        assert session.calls == []
        assert actor.supported is None
        assert settle is False  # no real time yet -> keep retrying

    run(main())


def test_malformed_reply_closes_link():
    async def main() -> None:
        session = _Session()
        session.reply_override = _Reply("OK: cm5 time set wrong")
        actor = _actor(session)
        with pytest.raises(LinkClosed, match="malformed"):
            await actor._push_once()

    run(main())


def test_session_epoch_zero_reply_closes_link():
    async def main() -> None:
        session = _Session(session_epoch=0)
        actor = _actor(session)
        with pytest.raises(LinkClosed, match="did not match"):
            await actor._push_once()

    run(main())


def test_unknown_command_marks_unsupported_and_settles():
    async def main() -> None:
        session = _Session()
        session.reply_override = _Reply(
            "Unknown command: cm5\nType 'help'", ok=False)
        actor = _actor(session)
        settle = await actor._push_once()  # must not raise
        assert actor.supported is False
        assert settle is True  # legacy peer: stop hammering, reprobe on reconnect

    run(main())


def test_range_rejection_is_non_fatal_and_keeps_retrying():
    async def main() -> None:
        session = _Session()
        session.reply_override = _Reply(
            "Error: cm5 time timestamp must be within 2020-2099", ok=False)
        actor = _actor(session)
        settle = await actor._push_once()  # must not raise
        # Not marked supported=True (rejected) nor False (it IS understood).
        assert actor.supported is None
        assert settle is False  # transient reject -> keep retrying

    run(main())


def test_stays_on_retry_cadence_until_confident_then_settles():
    async def main() -> None:
        conf = [0]  # Pi starts unconfident (no NTP, RTC not yet read)
        session = _Session()
        actor = _actor(session, confidence_fn=lambda: conf[0],
                       interval_s=3600, retry_interval_s=0.01)
        task = asyncio.create_task(actor.run())
        try:
            # Unconfident: keeps retrying fast, never settles.
            await _wait_until(lambda: len(session.calls) >= 3)
            assert actor.settled is False
            unconfident_calls = len(session.calls)
            # Pi gains confidence -> the next push settles to the lazy cadence.
            conf[0] = FLAG_PI_SYNCED
            await _wait_until(lambda: actor.settled is True)
            assert session.calls[-1][0] == f"cm5 time set 1 {GOOD_EPOCH} 1"
            # It stops hammering once settled (interval jumped to 3600s).
            settled_calls = len(session.calls)
            await asyncio.sleep(0)
            assert len(session.calls) == settled_calls
            assert settled_calls > unconfident_calls
        finally:
            await _cancel(task)

    run(main())


def test_link_reset_resets_settled_to_re_anchor_fast():
    async def main() -> None:
        session = _Session()
        actor = _actor(session)
        assert await actor._push_once() is True
        actor._settled = True  # models run() having settled to the 24 h cadence
        actor.link_reset()     # a reconnect may mean the device rebooted dark
        assert actor.settled is False

    run(main())


def test_actor_pushes_immediately_on_start_then_link_reset_repushes():
    async def main() -> None:
        session = _Session()
        actor = _actor(session, interval_s=60)
        task = asyncio.create_task(actor.run())
        try:
            await _wait_until(lambda: len(session.calls) == 1)
            actor.link_reset()  # models a supervisor reconnect
            await _wait_until(lambda: len(session.calls) == 2)
            assert all(
                c[0] == f"cm5 time set 1 {GOOD_EPOCH} 3" for c in session.calls)
        finally:
            await _cancel(task)

    run(main())


# --- confidence probe --------------------------------------------------------

class _CompletedProc:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def _install_probe(monkeypatch, *, ntp: str, rtc_dev: bool,
                   rtc_usec: str | None) -> None:
    def fake_run(argv, **_kw):
        prop = argv[argv.index("-p") + 1]
        if prop == "NTPSynchronized":
            return _CompletedProc(ntp)
        if prop == "RTCTimeUSec":
            if rtc_usec is None:
                return _CompletedProc("", returncode=1)
            return _CompletedProc(rtc_usec)
        return _CompletedProc("", returncode=1)

    monkeypatch.setattr(cm5_time_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        cm5_time_mod.os.path, "exists",
        lambda p: rtc_dev and p in ("/dev/rtc0", "/dev/rtc"))


def test_confidence_synced_and_valid_rtc(monkeypatch):
    _install_probe(monkeypatch, ntp="yes", rtc_dev=True,
                   rtc_usec=str(PLAUSIBLE_MIN * 1_000_000))
    assert cm5_time_mod.read_local_confidence() == (
        FLAG_PI_SYNCED | FLAG_PI_RTC_VALID)


def test_confidence_rtc_only_when_not_ntp_synced(monkeypatch):
    _install_probe(monkeypatch, ntp="no", rtc_dev=True,
                   rtc_usec=str((PLAUSIBLE_MIN + 1000) * 1_000_000))
    assert cm5_time_mod.read_local_confidence() == FLAG_PI_RTC_VALID


def test_confidence_dead_battery_rtc_is_not_valid(monkeypatch):
    # RTC device present but its time is pre-2020 (dead battery / never set).
    _install_probe(monkeypatch, ntp="no", rtc_dev=True,
                   rtc_usec=str(1_000_000_000 * 1_000_000))  # 2001
    assert cm5_time_mod.read_local_confidence() == 0


def test_confidence_fake_hwclock_has_no_rtc_device(monkeypatch):
    # No /dev/rtc backing (systemd fake-hwclock) -> bit1 never set even though
    # RTCTimeUSec would parse; and NTP not yet synced -> zero confidence.
    _install_probe(monkeypatch, ntp="no", rtc_dev=False,
                   rtc_usec=str(PLAUSIBLE_MIN * 1_000_000))
    assert cm5_time_mod.read_local_confidence() == 0


def test_confidence_probe_failures_are_zero(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("timedatectl missing")

    monkeypatch.setattr(cm5_time_mod.subprocess, "run", boom)
    monkeypatch.setattr(cm5_time_mod.os.path, "exists", lambda _p: False)
    assert cm5_time_mod.read_local_confidence() == 0
