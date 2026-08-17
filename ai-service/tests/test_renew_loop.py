"""LiveSttGate._renew_loop: the lease-lapse defense, previously untested.

The firmware's 'liveaudio ready' on a LAPSED lease silently re-mints with the
shadow flags WIPED while replying plain OK (System_LiveAudio.cpp, verified
2026-08-11). The renew loop is the daemon's only defense: suspect-gap verify,
periodic verify, verified-loss re-arm, epoch tracking, and error disarm. These
tests pin each behavior deterministically via the gate's injectable clock/sleep,
and one end-to-end test proves the whole defense against FakeFirmware — which
now models the silent wipe (fidelity fix shipped with these tests).
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import open_link, run
from hw1_ai_service.audio.live import LivePcmInbox
from hw1_ai_service.config import SttConfig
from hw1_ai_service.link import protocol
from hw1_ai_service.stt import live_gate as live_gate_mod
from hw1_ai_service.stt.live_gate import LiveSttGate

CONTROLLER = 0xC0DEC0DE00000001
CTL_HEX = f"{CONTROLLER:016x}"

_READY_E7 = (f"OK: liveaudio ready version=1 controller={CTL_HEX} "
             "session_epoch=7 lease_ttl_ms=3000 renew_ms=1000 baud=2000000")
_READY_E9 = _READY_E7.replace("session_epoch=7", "session_epoch=9")
_READY_DIRECT_E7 = _READY_E7.replace(
    "session_epoch=7 ", "session_epoch=7 renew_direct=1 ")
_READY_DIRECT_TTL4_E7 = _READY_DIRECT_E7.replace(
    "lease_ttl_ms=3000", "lease_ttl_ms=4000")
_STATUS_ON = "OK: liveaudio task=ready shadow=on shadow_mode=native active=0"
_STATUS_OFF = "OK: liveaudio task=ready shadow=off shadow_mode=exact active=0"
_ARM_OK = (f"OK: liveaudio shadow version=1 controller={CTL_HEX} state=on "
           "mode=native target=native abort=0")


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _Session:
    """Scripted replies keyed by the first two command words, with a per-key
    RTT queue that advances the fake clock — so each renew's measured gap is
    sleep-interval + scripted RTT, exactly like the wire."""

    def __init__(self, clock: _Clock, replies: dict[str, list[str]],
                 rtts: dict[str, list[float]] | None = None) -> None:
        self._clock = clock
        self.replies = {k: list(v) for k, v in replies.items()}
        self.rtts = {k: list(v) for k, v in (rtts or {}).items()}
        self.commands: list[str] = []
        self.send_times: list[tuple[str, float]] = []

    async def command(self, line: str, **_kw):
        await asyncio.sleep(0)  # real await point, like the wire
        self.commands.append(line)
        self.send_times.append((line, self._clock()))
        key = " ".join(line.split()[:2])
        rtt_q = self.rtts.get(key)
        self._clock.advance(
            rtt_q[0] if rtt_q and len(rtt_q) == 1
            else rtt_q.pop(0) if rtt_q else 0.1)

        class _Reply:
            def __init__(self, text: str) -> None:
                self.text = text
                self.ok = text.startswith("OK")

        queue = self.replies.get(key)
        if not queue:
            raise AssertionError(f"unscripted command: {line}")
        return _Reply(queue[0] if len(queue) == 1 else queue.pop(0))


def _instant_sleep(clock: _Clock):
    async def _sleep(dt: float) -> None:
        clock.advance(dt)
        await asyncio.sleep(0)
    return _sleep


def _unit_gate(tmp_path, clock: _Clock) -> LiveSttGate:
    gate = LiveSttGate(SttConfig(live_model_dir=str(tmp_path)),
                       LivePcmInbox(CONTROLLER),
                       clock=clock, sleep=_instant_sleep(clock))
    gate._spawn_warm = lambda retiring=None: None  # unit scope: no model
    return gate


def _renews(session: _Session, since: int = 0) -> int:
    return sum(1 for c in session.commands[since:]
               if c.startswith("liveaudio ready"))


def _statuses(session: _Session, since: int = 0) -> int:
    return sum(1 for c in session.commands[since:]
               if c == "liveaudio status")


def _arms(session: _Session, since: int = 0) -> int:
    return sum(1 for c in session.commands[since:]
               if c.startswith("liveaudio shadow"))


async def _spin_until(predicate, what: str, spins: int = 4000) -> None:
    for _ in range(spins):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"never reached: {what}")


async def _armed_gate(tmp_path, clock, replies, rtts=None):
    session = _Session(clock, replies, rtts)
    gate = _unit_gate(tmp_path, clock)
    assert await gate.ensure_armed(session)
    return gate, session, len(session.commands)


# -- unit: the five renew-loop behaviors ------------------------------------

def test_healthy_cadence_verifies_only_on_periodic_tick(tmp_path):
    async def main():
        clock = _Clock()
        gate, session, base = await _armed_gate(
            tmp_path, clock,
            {"liveaudio ready": [_READY_E7], "liveaudio status": [_STATUS_ON]})
        # 7 healthy renews (gap = 2.0 sleep + 0.1 rtt < 2.75): no verify.
        await _spin_until(lambda: _renews(session, base) >= 7, "7 renews")
        assert _statuses(session, base) == 0
        # Tick 8 is the periodic verify even on a perfectly healthy cadence.
        await _spin_until(lambda: _statuses(session, base) >= 1,
                          "periodic verify")
        assert _renews(session, base) == 8
        assert _arms(session, base) == 0  # status said on/native: no re-arm
        await gate.close()
    run(main())


def test_direct_grant_uses_one_second_but_keeps_16_second_verify(tmp_path):
    async def main():
        clock = _Clock()
        gate, session, base = await _armed_gate(
            tmp_path, clock,
            {"liveaudio ready": [_READY_DIRECT_E7],
             "liveaudio status": [_STATUS_ON]})
        assert gate._lease_timing.direct
        assert gate._lease_timing.renew_interval_s == 1.0

        # Faster direct renewals must not double the ordinary status traffic.
        await _spin_until(lambda: _renews(session, base) >= 15, "15 renews")
        assert _statuses(session, base) == 0
        await _spin_until(lambda: _statuses(session, base) >= 1,
                          "elapsed-time verify")
        assert _renews(session, base) == 16

        sends = [t for line, t in session.send_times
                 if line.startswith("liveaudio ready")]
        gaps = [b - a for a, b in zip(sends[-5:], sends[-4:])]
        assert all(abs(gap - 1.0) < 1e-6 for gap in gaps)
        await gate.close()
    run(main())


def test_legacy_reply_keeps_two_seconds_despite_advertised_one_second(tmp_path):
    ready = protocol.parse_live_ready(
        _READY_E7, expected_controller=CONTROLLER)
    timing = live_gate_mod._lease_timing_from_ready(ready)
    assert not timing.direct
    assert timing.renew_interval_s == 2.0


@pytest.mark.parametrize("ready_text", [
    _READY_DIRECT_E7.replace("renew_ms=1000", "renew_ms=499"),
    _READY_DIRECT_E7.replace("renew_ms=1000", "renew_ms=5001"),
    _READY_DIRECT_E7.replace("lease_ttl_ms=3000", "lease_ttl_ms=1999"),
    _READY_DIRECT_E7.replace("lease_ttl_ms=3000", "lease_ttl_ms=60001"),
    _READY_DIRECT_E7.replace("renew_ms=1000", "renew_ms=1600"),
    _READY_DIRECT_E7.replace("renew_ms=1000", "renew_ms=1500"),
    _READY_DIRECT_E7.replace("renew_ms=1000", "renew_ms=01"),
    _READY_DIRECT_E7.replace("renew_direct=1 ", "renew_direct=1 renew_direct=1 "),
    _READY_DIRECT_E7.replace(" renew_ms=1000", ""),
])
def test_invalid_marked_timing_fails_closed(ready_text):
    with pytest.raises(ValueError):
        ready = protocol.parse_live_ready(
            ready_text, expected_controller=CONTROLLER)
        live_gate_mod._lease_timing_from_ready(ready)


def test_suspect_gap_triggers_verify_and_exactly_one_rearm(tmp_path):
    async def main():
        clock = _Clock()
        gate, session, base = await _armed_gate(
            tmp_path, clock,
            {"liveaudio ready": [_READY_E7],
             # ensure_armed consumes the first status (ON: no setup arm);
             # the suspect verify then finds the arm LOST (the silent-wipe
             # outcome); later verifies see it restored.
             "liveaudio status": [_STATUS_ON, _STATUS_OFF, _STATUS_ON],
             "liveaudio shadow": [_ARM_OK]},
            # renew RTTs: arm-time, tick1 healthy, tick2 SLOW (gap 3.0 > 2.75),
            # then healthy forever.
            rtts={"liveaudio ready": [0.1, 0.1, 1.0, 0.1]})
        await _spin_until(lambda: _arms(session, base) >= 1, "re-arm")
        assert _statuses(session, base) == 1   # one suspect verify
        assert _arms(session, base) == 1       # exactly one re-arm
        assert gate._armed                     # loop kept running
        # Cadence returns to healthy: more renews, no further arms.
        await _spin_until(lambda: _renews(session, base) >= 5, "recovery")
        assert _arms(session, base) == 1
        await gate.close()
    run(main())


def test_direct_nondefault_ttl_drives_lapse_detection_and_rearm(tmp_path):
    async def main():
        clock = _Clock()
        gate, session, base = await _armed_gate(
            tmp_path, clock,
            {"liveaudio ready": [_READY_DIRECT_TTL4_E7],
             "liveaudio status": [_STATUS_ON, _STATUS_OFF, _STATUS_ON],
             "liveaudio shadow": [_ARM_OK]},
            rtts={"liveaudio ready": [0.1, 4.0, 0.1]})
        assert gate._lease_timing.suspect_gap_s == 3.75
        await _spin_until(lambda: _arms(session, base) >= 1,
                          "direct-TTL re-arm")
        assert _statuses(session, base) == 1
        assert gate._armed
        await gate.close()
    run(main())


def test_epoch_change_disarms_session_local_timing(tmp_path):
    async def main():
        clock = _Clock()
        gate, session, base = await _armed_gate(
            tmp_path, clock,
            {"liveaudio ready": [_READY_E7, _READY_E7, _READY_E9],
             "liveaudio status": [_STATUS_ON]})
        assert gate._armed_epoch == 7
        task = gate._renew_task
        await _spin_until(lambda: task.done(), "epoch-change disarm")
        assert not gate._armed
        assert _statuses(session, base) == 0
        assert _arms(session, base) == 0
        await gate.close()
    run(main())


def test_renew_error_disarms_and_stops_the_loop(tmp_path):
    async def main():
        clock = _Clock()
        gate, session, base = await _armed_gate(
            tmp_path, clock,
            {"liveaudio ready": [_READY_E7, _READY_E7,
                                 "Error: liveaudio lease busy"],
             "liveaudio status": [_STATUS_ON]})
        task = gate._renew_task
        await _spin_until(lambda: task.done(), "loop exit on Error")
        assert not gate._armed
        frozen = len(session.commands)
        for _ in range(50):
            await asyncio.sleep(0)
        assert len(session.commands) == frozen  # no zombie renewals
        await gate.close()
    run(main())


def test_renew_contract_change_disarms_instead_of_inheriting(tmp_path):
    async def main():
        clock = _Clock()
        gate, session, base = await _armed_gate(
            tmp_path, clock,
            {"liveaudio ready": [_READY_DIRECT_E7, _READY_E7],
             "liveaudio status": [_STATUS_ON]})
        task = gate._renew_task
        await _spin_until(lambda: task.done(), "contract-change disarm")
        assert not gate._armed
        assert _renews(session, base) == 1
        await gate.close()
    run(main())


def test_link_reset_kills_the_loop_without_further_commands(tmp_path):
    async def main():
        clock = _Clock()
        gate, session, base = await _armed_gate(
            tmp_path, clock,
            {"liveaudio ready": [_READY_E7], "liveaudio status": [_STATUS_ON]})
        await _spin_until(lambda: _renews(session, base) >= 2, "2 renews")
        task = gate._renew_task
        gate.link_reset()
        await _spin_until(lambda: task.done(), "loop exit on link_reset")
        assert not gate._armed
        frozen = len(session.commands)
        for _ in range(50):
            await asyncio.sleep(0)
        assert len(session.commands) == frozen
        await gate.close()
    run(main())


def test_link_reset_during_initial_ready_cannot_continue_arming(tmp_path):
    async def main():
        entered = asyncio.Event()
        release = asyncio.Event()

        class _PausedInitialSession:
            def __init__(self):
                self.commands: list[str] = []

            async def command(self, line: str, **_kw):
                self.commands.append(line)
                entered.set()
                await release.wait()

                class _Reply:
                    ok = True
                    text = _READY_DIRECT_E7

                return _Reply()

        gate = _unit_gate(tmp_path, _Clock())
        session = _PausedInitialSession()
        arm_task = asyncio.create_task(gate.ensure_armed(session))
        await entered.wait()
        gate.link_reset()
        release.set()
        assert not await arm_task
        assert not gate._armed
        assert len(session.commands) == 1
        await gate.close()
    run(main())


def test_link_reset_during_failed_ready_does_not_spend_new_failure_budget(
        tmp_path):
    async def main():
        entered = asyncio.Event()
        release = asyncio.Event()

        class _PausedErrorSession:
            async def command(self, _line: str, **_kw):
                entered.set()
                await release.wait()
                raise RuntimeError("old session closed")

        gate = _unit_gate(tmp_path, _Clock())
        arm_task = asyncio.create_task(gate.ensure_armed(_PausedErrorSession()))
        await entered.wait()
        gate.link_reset()
        release.set()
        assert not await arm_task
        assert gate._arm_failures == 0
        assert not gate._disabled
        await gate.close()
    run(main())


def test_link_reset_during_inflight_renew_cannot_verify_or_rearm(tmp_path):
    async def main():
        clock = _Clock()
        entered = asyncio.Event()
        release = asyncio.Event()

        class _PausedRenewSession:
            def __init__(self):
                self.commands: list[str] = []
                self.ready_calls = 0

            async def command(self, line: str, **_kw):
                self.commands.append(line)
                if line.startswith("liveaudio ready"):
                    self.ready_calls += 1
                    if self.ready_calls == 2:
                        entered.set()
                        await release.wait()
                    text = _READY_DIRECT_E7
                elif line == "liveaudio status":
                    text = _STATUS_ON
                else:
                    raise AssertionError(f"unexpected command: {line}")

                class _Reply:
                    def __init__(self, reply_text):
                        self.text = reply_text
                        self.ok = reply_text.startswith("OK")

                return _Reply(text)

        gate = _unit_gate(tmp_path, clock)
        session = _PausedRenewSession()
        assert await gate.ensure_armed(session)
        baseline = len(session.commands)
        task = gate._renew_task
        await entered.wait()
        gate.link_reset()
        release.set()
        await _spin_until(lambda: task.done(), "stale renew exit")
        assert not gate._armed
        assert session.commands[baseline:] == [
            f"liveaudio ready 1 {CTL_HEX}"]
        await gate.close()
    run(main())


def test_verify_cycles_never_starve_the_lease(tmp_path):
    """Regression for the 2026-08-11 field failure: with realistic ~0.45 s
    command RTTs (the firmware's measured INPUT-stall tax), a suspect
    verify+re-arm cycle must NOT push the next renew past the 3 s lease TTL.
    The pre-fix loop slept a fixed interval AFTER the cycle's 3 round-trips,
    spacing renews ~3.35 s apart — a self-sustaining lapse->wipe->re-arm loop
    on real hardware. Deadline scheduling keeps spacing ~interval + one RTT."""
    async def main():
        clock = _Clock()
        gate, session, base = await _armed_gate(
            tmp_path, clock,
            {"liveaudio ready": [_READY_E7],
             "liveaudio status": [_STATUS_ON, _STATUS_OFF, _STATUS_ON],
             "liveaudio shadow": [_ARM_OK]},
            # Every command costs the measured stall tax; one slow renew
            # forces a full suspect verify+re-arm cycle mid-run.
            rtts={"liveaudio ready": [0.45, 0.45, 1.0, 0.45],
                  "liveaudio status": [0.45],
                  "liveaudio shadow": [0.45]})
        await _spin_until(lambda: _arms(session, base) >= 1, "re-arm cycle")
        await _spin_until(lambda: _renews(session, base) >= 6, "post-cycle")
        sends = [t for line, t in session.send_times
                 if line.startswith("liveaudio ready")]
        gaps = [b - a for a, b in zip(sends, sends[1:])]
        assert max(gaps) < 3.0, f"renew spacing crossed the lease TTL: {gaps}"
        # And the cycle stayed a one-off, not a self-sustaining loop.
        assert _arms(session, base) == 1
        await gate.close()
    run(main())


# -- end-to-end: lapse -> silent wipe -> detect -> re-arm -------------------

class _SpySession:
    """Delegating wrapper recording every command line."""

    def __init__(self, real) -> None:
        self._real = real
        self.lines: list[str] = []

    async def command(self, line: str, **kw):
        self.lines.append(line)
        return await self._real.command(line, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_lapse_recovery_end_to_end(firmware, tmp_path, monkeypatch):
    """Real gate + real Session/transport + FakeFirmware modeling the real
    silent-wipe: a stalled renew lets the lease lapse; the next renew re-mints
    with the shadow WIPED (plain OK); the suspect-gap verify detects the loss
    and re-arms; the firmware ends verifiably armed native again."""
    monkeypatch.setattr(live_gate_mod, "_RENEW_INTERVAL_S", 0.1)
    monkeypatch.setattr(live_gate_mod, "_RENEW_SUSPECT_GAP_S", 0.3)
    firmware.live_lease_ttl_s = 0.4
    firmware.live_renew_direct = False

    async def main():
        inbox = LivePcmInbox(CONTROLLER)
        transport, session = open_link(firmware)
        stall = {"pending": False}

        async def sleep(dt: float) -> None:
            if stall["pending"]:
                stall["pending"] = False
                await asyncio.sleep(0.6)  # > lease TTL: renewals go silent
            else:
                await asyncio.sleep(dt)

        gate = LiveSttGate(
            SttConfig(live_model_dir=str(tmp_path)),
            LivePcmInbox(CONTROLLER), sleep=sleep)
        gate._spawn_warm = lambda retiring=None: None
        try:
            await session.login()
            spy = _SpySession(session)
            assert await gate.ensure_armed(spy)
            assert firmware.live_shadow_armed and firmware.live_shadow_native
            arms_before = sum(
                1 for l in spy.lines if l.startswith("liveaudio shadow"))
            assert arms_before == 1

            stall["pending"] = True  # next tick sleeps through the TTL

            def recovered() -> bool:
                return sum(1 for l in spy.lines
                           if l.startswith("liveaudio shadow")) >= 2

            deadline = asyncio.get_event_loop().time() + 8.0
            while not recovered():
                assert asyncio.get_event_loop().time() < deadline, \
                    "renew loop never re-armed after the silent wipe"
                await asyncio.sleep(0.05)

            # The re-arm landed on the firmware and the gate stayed up.
            assert firmware.live_shadow_armed and firmware.live_shadow_native
            assert gate._armed
        finally:
            await gate.close()
            transport.close()
    run(main())


# -- fidelity guard: the fake must keep modeling the wipe -------------------

def test_fake_firmware_wipes_shadow_on_lapsed_ready(firmware):
    """Regression guard for the FakeFirmware fidelity fix itself: a lapsed
    'ready' re-mints WITH the shadow wiped and replies plain OK, and the
    status reply speaks shadow=on|off + shadow_mode=native|exact."""
    firmware.live_lease_ttl_s = 0.2

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            ok = await session.command(
                f"liveaudio ready 1 {CTL_HEX}", expect="status", timeout=5)
            assert ok.ok
            armed = await session.command(
                f"liveaudio shadow 1 {CTL_HEX} on native",
                expect="status", timeout=5, replay=False)
            assert armed.ok
            st = await session.command(
                "liveaudio status", expect="status", timeout=5)
            assert "shadow=on" in st.text and "shadow_mode=native" in st.text

            await asyncio.sleep(0.3)  # let the lease lapse

            remint = await session.command(
                f"liveaudio ready 1 {CTL_HEX}", expect="status", timeout=5)
            assert remint.ok  # plain OK — nothing reveals the wipe
            st2 = await session.command(
                "liveaudio status", expect="status", timeout=5)
            assert "shadow=off" in st2.text  # ...but the arm is gone
        finally:
            transport.close()
    run(main())
