from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from conftest import open_link, run
from fake_firmware import TEST_EVENAI_ID, make_wav

from hw1_ai_service import bg
from hw1_ai_service.audio import fetch, wav
from hw1_ai_service.config import AudioConfig
from hw1_ai_service.jobs import (
    EvenAiCancelled, EvenAiExchange, ManualTrigger, route_link_event)
from hw1_ai_service.link.session import EventLatch, Reply


class _StoppedPollSession:
    """Small helper double for the post-poll latch race.

    The normal wait times out, the status command reports stopped, and an
    optional mic_autostop is routed only after that reply. This isolates the
    grace semantics from PTY scheduling; the integration test below covers the
    real Session event pump.
    """

    def __init__(self, *, event_path: str | None = None,
                 event_delay_s: float = 0.0,
                 command_delay_s: float = 0.0,
                 event_during_poll: bool = False,
                 status_text: str = "Recording: stopped") -> None:
        self.mic_autostop = EventLatch()
        self.event_path = event_path
        self.event_delay_s = event_delay_s
        self.command_delay_s = command_delay_s
        self.event_during_poll = event_during_poll
        self.status_text = status_text
        self.command_seen = asyncio.Event()
        self.commands: list[str] = []

    async def command(self, line: str, **_kwargs) -> Reply:
        assert line == "micrecord"
        self.commands.append(line)
        if self.command_delay_s:
            await asyncio.sleep(self.command_delay_s)
        if self.event_path is not None:
            if self.event_during_poll:
                self.mic_autostop.fire(self.event_path)
            else:
                asyncio.get_running_loop().call_later(
                    self.event_delay_s, self.mic_autostop.fire, self.event_path)
        self.command_seen.set()
        return Reply(lines=[f"OK: {self.status_text}"])


class _StatusSequenceSession:
    """Status-only session for lifecycle compatibility tests.

    New firmware keeps the recording busy while it is stopping/finalizing. The
    Pi must continue polling those states and use only IDLE's ``stopped`` reply
    as the no-EVT backstop.
    """

    def __init__(self, statuses: list[str]) -> None:
        self.mic_autostop = _TrackingLatch()
        self.statuses = list(statuses)
        self.commands: list[str] = []

    async def command(self, line: str, **_kwargs) -> Reply:
        assert line == "micrecord"
        self.commands.append(line)
        assert self.statuses, "host polled past the supplied lifecycle trace"
        return Reply(lines=[f"OK: Recording: {self.statuses.pop(0)}"])


class _TrackingLatch:
    """Immediate-timeout latch that records every requested wait window."""

    payload: str | None = None

    def __init__(self) -> None:
        self.wait_timeouts: list[float] = []

    def arm(self) -> int:
        return 0

    async def wait(self, _token: int, timeout: float) -> bool:
        self.wait_timeouts.append(timeout)
        return False


class _ManualWindowSession:
    def __init__(self) -> None:
        self.mic_autostop = _TrackingLatch()
        self.commands: list[str] = []

    async def command(self, line: str, **_kwargs) -> Reply:
        self.commands.append(line)
        if line.startswith("micrecord start"):
            return Reply(lines=["OK: Recording started"])
        if line == "micrecord":
            return Reply(lines=["OK: Recording: stopped"])
        assert line == "micrecord stop"
        return Reply(lines=[
            "OK: Recording stopped — /sd/recordings/rec_manual.wav"])


class _DiscardedOwnedSession:
    """Owned recorder status after dismissal when cancel EVT was lost."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def command(self, line: str, **_kwargs) -> Reply:
        self.commands.append(line)
        return Reply(lines=[
            f"OK: Recording {TEST_EVENAI_ID}: discarded"])


class _RejectedOwnedSession:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def command(self, line: str, **_kwargs) -> Reply:
        self.commands.append(line)
        return Reply(lines=["Error: recorder owner mismatch"])


class _WaitProbe:
    def __init__(self) -> None:
        self.active = 0
        self._never = asyncio.Event()

    def is_set(self) -> bool:
        return False

    async def wait(self) -> None:
        self.active += 1
        try:
            await self._never.wait()
        finally:
            self.active -= 1


def test_full_fetch_roundtrip(firmware):
    """Record + chunked fileread reassembles the exact WAV, then deletes."""
    firmware.wav_bytes = make_wav(seconds=2.5)   # ~80KB -> ~28 chunks

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            cfg = AudioConfig(record_seconds=0.05)
            data = await fetch.record_utterance(session, cfg)
            await bg.drain()
            assert data == firmware.wav_bytes
            parsed = wav.parse(data)
            wav.require_canonical(parsed)
            assert len(parsed.pcm) == 2.5 * 16000 * 2
            assert firmware.deleted, "recording should be cleaned up"
            assert not firmware.files, "no stray files"
        finally:
            transport.close()
    run(main())


def test_owned_discard_status_is_lost_cancel_event_backstop():
    """A dropped evenai_cancel must not cost 15s or fetch deleted audio."""

    async def main():
        session = _DiscardedOwnedSession()
        exchange = EvenAiExchange(TEST_EVENAI_ID)
        cfg = AudioConfig(vad_poll_s=0.001, vad_max_seconds=1.0)

        with pytest.raises(EvenAiCancelled):
            await fetch._await_exchange_stop(session, cfg, exchange)

        assert exchange.cancelled
        assert exchange.cancel_reason == "recorder_discarded"
        assert session.commands == [f"micrecord statusid {TEST_EVENAI_ID}"]

    run(main())


def test_owned_status_rejection_fails_immediately_instead_of_polling_cap():
    async def main():
        session = _RejectedOwnedSession()
        exchange = EvenAiExchange(TEST_EVENAI_ID)
        cfg = AudioConfig(vad_poll_s=0.001, vad_max_seconds=10.0)

        with pytest.raises(fetch.FetchError, match="owner mismatch"):
            await fetch._await_exchange_stop(session, cfg, exchange)

        assert session.commands == [f"micrecord statusid {TEST_EVENAI_ID}"]

    run(main())


def test_owned_wait_parent_cancel_collects_both_event_waiters():
    async def main():
        exchange = EvenAiExchange(TEST_EVENAI_ID)
        terminal, cancelled = _WaitProbe(), _WaitProbe()
        exchange.terminal_event = terminal
        exchange.cancel_event = cancelled
        cfg = AudioConfig(vad_poll_s=10.0, vad_max_seconds=10.0)

        task = asyncio.create_task(fetch._await_exchange_stop(
            object(), cfg, exchange))
        while terminal.active != 1 or cancelled.active != 1:
            await asyncio.sleep(0)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert terminal.active == cancelled.active == 0

    run(main())


def test_vad_auto_stop(firmware):
    """VAD on: the client arms `start vad`, polls until the device auto-stops,
    then still gets the path and the WAV back."""
    firmware.wav_bytes = make_wav(seconds=1.0)
    firmware.vad_auto_stop_after = 2

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            cfg = AudioConfig(vad=True, vad_silence_ms=1200, vad_poll_s=0.05,
                              vad_max_seconds=5.0, transfer="voicefetch")
            data = await fetch.record_utterance(session, cfg)
            await bg.drain()
            assert data == firmware.wav_bytes
            assert "micrecord start vad 1200 trim" in firmware.command_log
            # It polled the bare status at least until auto-stop.
            assert firmware.command_log.count("micrecord") >= 2
            assert firmware.deleted
        finally:
            transport.close()
    run(main())


def test_vad_falls_back_on_pre_vad_firmware(firmware):
    """Firmware without `start vad` support: reject -> plain fixed window."""
    firmware.support_vad = False
    firmware.wav_bytes = make_wav(seconds=1.0)

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            cfg = AudioConfig(vad=True, record_seconds=0.05, transfer="voicefetch")
            data = await fetch.record_utterance(session, cfg)
            await bg.drain()
            assert data == firmware.wav_bytes
            # Tried VAD, got rejected, fell back to the plain fixed window.
            assert "micrecord start vad 1200 trim" in firmware.command_log
            assert "micrecord start" in firmware.command_log
        finally:
            transport.close()
    run(main())


def test_stopped_poll_grace_collects_delayed_autostop_path():
    """A stopped poll can beat the post-close EVT; the wake-only grace must
    recheck the same epoch and use the volunteered path."""
    async def main():
        session = _StoppedPollSession(
            event_path="/sd/recordings/rec_grace.wav", event_delay_s=0.01)
        token = session.mic_autostop.arm()
        cfg = AudioConfig(vad_poll_s=0.001, vad_max_seconds=0.5)
        path = await fetch._await_auto_stop(
            session, cfg, token, stopped_evt_grace_s=0.05)
        assert path == "/sd/recordings/rec_grace.wav"
        assert session.commands == ["micrecord"]
    run(main())


def test_stopped_poll_grace_rechecks_event_fired_inside_poll():
    """The latch is sticky: an EVT routed while the status command is in
    flight must be observed immediately after its stopped reply."""
    async def main():
        session = _StoppedPollSession(
            event_path="/sd/recordings/rec_during_poll.wav",
            event_during_poll=True)
        token = session.mic_autostop.arm()
        cfg = AudioConfig(vad_poll_s=0.001, vad_max_seconds=0.5)
        path = await fetch._await_auto_stop(
            session, cfg, token, stopped_evt_grace_s=0.05)
        assert path == "/sd/recordings/rec_during_poll.wav"
        assert session.commands == ["micrecord"]
    run(main())


def test_stopping_and_finalizing_statuses_are_not_treated_as_stopped():
    """The ESP32 now remains busy until the WAV is closed and reports its
    intermediate FSM states. Neither state is permission to fetch the file or
    start the wake-only post-stop grace window."""
    async def main():
        session = _StatusSequenceSession(
            ["stopping", "finalizing", "stopped"])
        token = session.mic_autostop.arm()
        cfg = AudioConfig(vad_poll_s=0.001, vad_max_seconds=0.5)
        path = await fetch._await_auto_stop(
            session, cfg, token, stopped_evt_grace_s=0.05)
        assert path is None
        assert session.commands == ["micrecord", "micrecord", "micrecord"]
        assert session.statuses == []
    run(main())


def test_closed_wav_event_wins_while_status_poll_says_finalizing():
    """Firmware closes the WAV and emits mic_autostop before its final IDLE
    publication. A sticky event routed during a FINALIZING poll is authoritative
    on the following wait; it must not require a redundant stop/status poll."""
    async def main():
        session = _StoppedPollSession(
            event_path="/sd/recordings/rec_closed.wav",
            event_during_poll=True,
            status_text="Recording: finalizing")
        token = session.mic_autostop.arm()
        cfg = AudioConfig(vad_poll_s=0.001, vad_max_seconds=0.5)
        path = await fetch._await_auto_stop(
            session, cfg, token, stopped_evt_grace_s=0.05)
        assert path == "/sd/recordings/rec_closed.wav"
        assert session.commands == ["micrecord"]
    run(main())


def test_stopped_poll_grace_rejects_stale_event_and_falls_back():
    """An event delivered before arm belongs to the previous capture; a lost
    current EVT still returns None so the caller can issue micrecord stop."""
    async def main():
        session = _StoppedPollSession()
        session.mic_autostop.fire("/sd/recordings/rec_old.wav")
        token = session.mic_autostop.arm()
        cfg = AudioConfig(vad_poll_s=0.001, vad_max_seconds=0.5)
        path = await fetch._await_auto_stop(
            session, cfg, token, stopped_evt_grace_s=0.01)
        assert path is None
        assert session.commands == ["micrecord"]
    run(main())


def test_stopped_poll_does_not_extend_expired_host_wait_deadline():
    """A poll that returns after the host wait deadline (the observed EXIT case) must
    skip grace rather than charging time for an EVT that cannot exist."""
    async def main():
        session = _StoppedPollSession(
            event_path="/sd/recordings/too_late.wav",
            event_delay_s=0.001, command_delay_s=0.02)
        token = session.mic_autostop.arm()
        cfg = AudioConfig(vad_poll_s=0.001, vad_max_seconds=0.01)
        path = await fetch._await_auto_stop(
            session, cfg, token, stopped_evt_grace_s=0.25)
        assert path is None
    run(main())


def test_status_poll_is_not_started_after_host_wait_deadline():
    """If the final latch wait consumes the remaining host budget, do not
    begin a status command with the session's much longer default timeout."""
    async def main():
        session = _StoppedPollSession()
        token = session.mic_autostop.arm()
        cfg = AudioConfig(vad_poll_s=0.05, vad_max_seconds=0.005)
        assert await fetch._await_auto_stop(session, cfg, token) is None
        assert session.commands == []
    run(main())


def test_manual_default_has_no_post_stopped_grace():
    """The shared helper's default stays immediate: the manual ask path always
    sends an explicit stop afterward and gains nothing from waiting."""
    async def main():
        session = _StoppedPollSession(
            event_path="/sd/recordings/manual_late.wav", event_delay_s=0.01)
        token = session.mic_autostop.arm()
        cfg = AudioConfig(vad_poll_s=0.001, vad_max_seconds=0.5)
        assert await fetch._await_auto_stop(session, cfg, token) is None
    run(main())


def test_manual_record_window_skips_grace_and_still_stops_once():
    """Pin the actual ask/manual caller: it uses the helper's zero-grace
    default and always obtains the authoritative path with one explicit stop."""
    async def main():
        session = _ManualWindowSession()
        cfg = AudioConfig(vad_poll_s=0.001, vad_max_seconds=0.5)
        path = await fetch._record_window(session, cfg)
        assert path == "/sd/recordings/rec_manual.wav"
        assert session.mic_autostop.wait_timeouts == [cfg.vad_poll_s]
        assert session.commands == [
            "micrecord start vad 1200 trim", "micrecord", "micrecord stop"]
    run(main())


def test_wake_cancellation_during_grace_propagates_without_stop():
    """Cancellation is not shielded or converted into the explicit-stop
    fallback; callers retain prompt control of the device-owned wake flow."""
    async def main():
        exchange = EvenAiExchange(TEST_EVENAI_ID)
        exchange.cancel("dismiss")
        try:
            exchange.raise_if_cancelled()
        except EvenAiCancelled:
            pass
        else:
            raise AssertionError("cooperative cancellation should propagate")
    run(main())


def test_wake_grace_routes_late_evt_and_skips_redundant_stop(firmware):
    """Full PTY path: the stopped status reply wins, then the EVT arrives via
    pump_events during grace. The WAV is fetched without micrecord stop."""
    firmware.wav_bytes = make_wav(seconds=1.0)

    async def main():
        transport, session = open_link(firmware)
        pump = None
        try:
            await session.login()
            firmware.begin_wake_capture(push=False)
            firmware.push_mic_autostop = False
            trigger = ManualTrigger()
            exchange = trigger.submit_evenai(TEST_EVENAI_ID)
            assert exchange is not None
            session.on_event = lambda payload: route_link_event(
                payload, trigger, session)
            pump = asyncio.create_task(session.pump_events())

            original_command = session.command
            pushed = False

            async def command_and_push_after_stopped(line: str, **kwargs):
                nonlocal pushed
                rep = await original_command(line, **kwargs)
                if (line == f"micrecord statusid {TEST_EVENAI_ID}" and not pushed
                        and "stopped" in rep.text.lower()):
                    pushed = True
                    firmware.push_event(
                        f"mic_autostop {TEST_EVENAI_ID} {firmware._last_path}")
                return rep

            session.command = command_and_push_after_stopped  # type: ignore[method-assign]
            cfg = AudioConfig(vad_poll_s=0.01, vad_max_seconds=1.0,
                              transfer="voicefetch")
            data = await fetch.fetch_wake_utterance(session, cfg, exchange)
            await bg.drain()
            assert data == firmware.wav_bytes
            assert pushed
            assert f"micrecord stopid {TEST_EVENAI_ID}" not in firmware.command_log
        finally:
            if pump is not None:
                pump.cancel()
                with suppress(asyncio.CancelledError):
                    await pump
            transport.close()
    run(main())


def test_wake_grace_missing_evt_keeps_explicit_stop_fallback(firmware):
    """No EVT is a supported case (EXIT, link loss, CRC drop). After the short
    grace, wake fetch still obtains the path with exactly one explicit stop."""
    firmware.wav_bytes = make_wav(seconds=1.0)

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            firmware.begin_wake_capture(push=False)
            firmware.push_mic_autostop = False
            exchange = EvenAiExchange(TEST_EVENAI_ID)
            cfg = AudioConfig(vad_poll_s=0.01, vad_max_seconds=1.0,
                              transfer="voicefetch")
            data = await fetch.fetch_wake_utterance(session, cfg, exchange)
            await bg.drain()
            assert data == firmware.wav_bytes
            assert firmware.command_log.count(
                f"micrecord stopid {TEST_EVENAI_ID}") == 1
        finally:
            transport.close()
    run(main())


def test_fetch_missing_file_fails_loudly(firmware):
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            try:
                await fetch.read_file_b64(session, "/recordings/nope.wav", 2048)
                raise AssertionError("expected FetchError")
            except fetch.FetchError as exc:
                assert "Not found" in str(exc)
        finally:
            transport.close()
    run(main())


def test_wav_rejects_wrong_rate():
    data = make_wav(seconds=0.1, rate=8000)
    parsed = wav.parse(data)
    try:
        wav.require_canonical(parsed)
        raise AssertionError("8kHz should be rejected")
    except wav.WavError as exc:
        assert "8000" in str(exc)
