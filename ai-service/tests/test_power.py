"""Finite CM5 host-power protocol, privilege boundary, and policy tests."""

from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import json
import threading
import time
from contextlib import suppress
from pathlib import Path

import pytest

from conftest import open_link, run

import hw1_ai_service.power as power_mod
from hw1_ai_service.config import Config, PowerConfig, load
from hw1_ai_service.jobs import Job, ManualTrigger, route_link_event
from hw1_ai_service.link.session import LinkClosed, Reply
from hw1_ai_service.pipeline import VoicePipeline
from hw1_ai_service.power import (
    AckState,
    HelperResult,
    HelperStatus,
    HostPowerState,
    PowerAction,
    PowerController,
    PowerHelperClient,
    PowerProfile,
    PowerProtocolError,
    helper_argv,
    parse_power_event,
    read_linux_boot_tag,
)


REQUEST_ID = "deadbeef00000001"
OTHER_ID = "deadbeef00000002"
BOOT_TAG = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def _stable_linux_boot_id(tmp_path, monkeypatch):
    path = tmp_path / "boot_id"
    path.write_text("01234567-89ab-cdef-0123-456789abcdef\n")
    monkeypatch.setattr(power_mod, "_BOOT_ID_PATH", path)


def _cfg(**changes) -> PowerConfig:
    cfg = PowerConfig(
        enabled=True,
        use_sudo=False,
        initial_profile="balanced",
        auto_idle_delay_s=0,
        uart_timeout_s=0.5,
    )
    for key, value in changes.items():
        setattr(cfg, key, value)
    return cfg


class FakeSession:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.hook = None
        self.reboot_suspected = False

    async def command(self, line: str, **kwargs) -> Reply:
        self.commands.append(line)
        if self.hook is not None:
            result = await self.hook(line)
            if result is not None:
                return result
        return Reply(["OK"])


class FakeHelper:
    def __init__(self, *, suspend_supported: bool = True,
                 rtc_sleep_supported: bool = True) -> None:
        self.calls: list[tuple[PowerAction, PowerProfile | None, int | None]] = []
        self.suspend_supported = suspend_supported
        self.rtc_sleep_supported = rtc_sleep_supported
        self.hook = None

    async def execute(self, action: PowerAction, *, profile=None,
                      minutes=None) -> HelperResult:
        self.calls.append((action, profile, minutes))
        if self.hook is not None:
            result = await self.hook(action, profile, minutes)
            if result is not None:
                return result
        status = HelperStatus(
            state=HostPowerState.AWAKE,
            profile=profile or PowerProfile.BALANCED,
            suspend_supported=self.suspend_supported,
            rtc_sleep_supported=self.rtc_sleep_supported,
        )
        return HelperResult(True, "ok", status)


async def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition not reached before timeout")
        await asyncio.sleep(0.005)


async def _stop(task: asyncio.Task | None, controller: PowerController | None = None):
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    if controller is not None:
        await controller.close()


# -- strict wire grammar -----------------------------------------------------


@pytest.mark.parametrize(("payload", "action", "profile", "minutes"), [
    (f"cm5_power_status 1 {REQUEST_ID}", PowerAction.STATUS, None, None),
    (f"cm5_power_profile_eco 1 {REQUEST_ID}", PowerAction.PROFILE,
     PowerProfile.ECO, None),
    (f"cm5_power_profile_balanced 1 {REQUEST_ID}", PowerAction.PROFILE,
     PowerProfile.BALANCED, None),
    (f"cm5_power_profile_performance 1 {REQUEST_ID}", PowerAction.PROFILE,
     PowerProfile.PERFORMANCE, None),
    (f"cm5_power_profile_auto 1 {REQUEST_ID}", PowerAction.PROFILE,
     PowerProfile.AUTO, None),
    (f"cm5_power_reboot 1 {REQUEST_ID}", PowerAction.REBOOT, None, None),
    (f"cm5_power_halt 1 {REQUEST_ID}", PowerAction.HALT, None, None),
    (f"cm5_power_suspend 1 {REQUEST_ID}", PowerAction.SUSPEND, None, None),
    (f"cm5_power_sleep_for 1 {REQUEST_ID} 45", PowerAction.SLEEP_FOR, None, 45),
])
def test_parse_exact_v1_events(payload, action, profile, minutes):
    request = parse_power_event(payload.encode("ascii"))
    assert request is not None
    assert (request.request_id, request.action, request.profile, request.minutes) == (
        REQUEST_ID, action, profile, minutes)


@pytest.mark.parametrize("payload", [
    f"cm5_power_status {REQUEST_ID}",                   # missing version
    f"cm5_power_status 2 {REQUEST_ID}",                 # future version
    "cm5_power_status 1 0000000000000001",              # zero nonce
    "cm5_power_status 1 deadbeef00000000",              # zero counter
    "cm5_power_status 1 not-an-id",
    f"cm5_power_status 1 {REQUEST_ID} extra",
    f"cm5_power_profile_turbo 1 {REQUEST_ID}",
    f"cm5_power_reboot 1 {REQUEST_ID};reboot",
    f"cm5_power_sleep_for 1 {REQUEST_ID} -1",
    f"cm5_power_sleep_for 1 {REQUEST_ID} 1441",
    f"cm5_power_sleep_for 1 {REQUEST_ID} 1;reboot",
])
def test_parser_rejects_cross_version_malformed_and_injection(payload):
    with pytest.raises(PowerProtocolError):
        parse_power_event(payload.encode("ascii"))


def test_parser_ignores_unrelated_events_and_enforces_configured_sleep_bound():
    assert parse_power_event(b"evenai_wake") is None
    with pytest.raises(PowerProtocolError):
        parse_power_event(
            f"cm5_power_sleep_for 1 {REQUEST_ID} 9".encode(),
            min_sleep_minutes=10,
            max_sleep_minutes=30,
        )


def test_linux_boot_tag_is_stable_normalized_and_fails_closed(tmp_path,
                                                               monkeypatch):
    path = tmp_path / "boot-id"
    monkeypatch.setattr(power_mod, "_BOOT_ID_PATH", path)
    path.write_text("ABCDEF01-2345-6789-ABCD-EF0123456789\n")
    assert read_linux_boot_tag() == "abcdef0123456789abcdef0123456789"
    path.write_text("not-a-kernel-boot-id\n")
    assert read_linux_boot_tag() == "0" * 32
    path.unlink()
    assert read_linux_boot_tag() == "0" * 32


# -- fixed helper argv -------------------------------------------------------


def test_helper_argv_is_typed_and_exact():
    assert helper_argv("/helper", True, PowerAction.PROFILE,
                       profile=PowerProfile.ECO) == (
        "/usr/bin/sudo", "-n", "/helper", "profile", "eco")
    assert helper_argv("/helper", False, PowerAction.SLEEP_FOR,
                       minutes=15) == ("/helper", "sleep_for", "15")
    assert helper_argv("/helper", False, PowerAction.POWEROFF) == (
        "/helper", "poweroff")
    with pytest.raises(TypeError):
        helper_argv("/helper", False, "reboot")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        helper_argv("/helper", False, PowerAction.PROFILE,
                    profile=PowerProfile.UNKNOWN)
    with pytest.raises(ValueError):
        helper_argv("/helper", False, PowerAction.SLEEP_FOR, minutes=1441)


def test_helper_client_uses_exec_argv_and_validates_json(monkeypatch):
    calls = []

    class Process:
        returncode = 0

        async def communicate(self):
            return (json.dumps({
                "ok": True,
                "code": "ok",
                "state": "awake",
                "profile": "eco",
                "suspend_supported": False,
                "rtc_sleep_supported": True,
            }).encode(), b"")

    async def create(*argv, **kwargs):
        calls.append((argv, kwargs))
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    async def main():
        client = PowerHelperClient("/opt/hw1-power-helper", use_sudo=True)
        result = await client.execute(PowerAction.PROFILE,
                                      profile=PowerProfile.ECO)
        assert result.ok and result.status is not None
        assert result.status.profile is PowerProfile.ECO
        assert calls[0][0] == (
            "/usr/bin/sudo", "-n", "/opt/hw1-power-helper", "profile", "eco")
        assert "shell" not in calls[0][1]

    run(main())


# -- controller protocol, ACK gate, dedupe, and profiles --------------------


def test_start_applies_auto_idle_and_sends_unsolicited_ready_report():
    async def main():
        session, helper = FakeSession(), FakeHelper()
        controller = PowerController(
            session,
            _cfg(initial_profile="auto", auto_idle_profile="eco"),
            helper=helper,
        )
        await controller.start()
        assert helper.calls == [(PowerAction.PROFILE, PowerProfile.ECO, None)]
        assert session.commands == [f"cm5 power report 1 0 awake auto {BOOT_TAG}"]
        await controller.close()

    run(main())


def test_start_reports_unknown_when_initial_profile_apply_fails():
    async def main():
        session, helper = FakeSession(), FakeHelper()

        async def fail_profile(action, profile, minutes):
            if action is PowerAction.PROFILE:
                return HelperResult(False, "governor_write_failed")
            return None

        helper.hook = fail_profile
        controller = PowerController(
            session, _cfg(initial_profile="auto"), helper=helper)
        await controller.start()
        assert session.commands == [
            f"cm5 power report 1 0 awake unknown {BOOT_TAG}"]
        await controller.close()

    run(main())


def test_boot_ready_report_retries_transient_rejection_until_confirmed(monkeypatch):
    async def main():
        monkeypatch.setattr(power_mod, "_READY_RETRY_DELAYS_S", (0.01, 0.01))
        session, helper = FakeSession(), FakeHelper()
        attempts = 0

        async def reject_twice(line):
            nonlocal attempts
            if line.startswith("cm5 power report 1 0 "):
                attempts += 1
                if attempts <= 2:
                    return Reply(["Error: transient rejection"])
            return None

        session.hook = reject_twice
        controller = PowerController(session, _cfg(), helper=helper)
        await controller.start()
        assert controller._ready_pending
        worker = asyncio.create_task(controller.run())
        try:
            await _wait_for(lambda: attempts == 3)
            assert not controller._ready_pending
            assert controller._ready_retry_task is None
        finally:
            await _stop(worker, controller)

    run(main())


def test_reconnect_repairs_only_an_undelivered_original_ready_report(monkeypatch):
    async def main():
        monkeypatch.setattr(power_mod, "_READY_RETRY_DELAYS_S", (60.0,))
        session, helper = FakeSession(), FakeHelper()
        reject = True

        async def reject_first(line):
            nonlocal reject
            if reject and line.startswith("cm5 power report 1 0 "):
                reject = False
                return Reply(["Error: transient rejection"])
            return None

        session.hook = reject_first
        controller = PowerController(session, _cfg(), helper=helper)
        await controller.start()
        scheduled = controller._ready_retry_task
        assert scheduled is not None and controller._ready_pending

        # Link repair may flush the still-undelivered startup claim, but after
        # delivery another reconnect must not originate a new id=0 report.
        controller.replay_pending_callbacks()
        worker = asyncio.create_task(controller.run())
        try:
            await _wait_for(lambda: not controller._ready_pending)
            assert scheduled.cancelled()
            count = sum(command.startswith("cm5 power report 1 0 ")
                        for command in session.commands)
            assert count == 2
            controller.replay_pending_callbacks()
            await asyncio.sleep(0.02)
            assert sum(command.startswith("cm5 power report 1 0 ")
                       for command in session.commands) == count
        finally:
            await _stop(worker, controller)

    run(main())


def test_destructive_event_retires_pending_ready_before_accepted_reconnect(
        monkeypatch):
    async def main():
        monkeypatch.setattr(power_mod, "_READY_RETRY_DELAYS_S", (60.0,))
        session, helper = FakeSession(), FakeHelper()
        reject_ready = True
        close_accepted = True

        async def fail_in_order(line):
            nonlocal reject_ready, close_accepted
            if reject_ready and line.startswith("cm5 power report 1 0 "):
                reject_ready = False
                return Reply(["Error: transient rejection"])
            if close_accepted and line.endswith(" accepted"):
                close_accepted = False
                raise LinkClosed("accepted reply lost")
            return None

        session.hook = fail_in_order
        controller = PowerController(session, _cfg(), helper=helper)
        await controller.start()
        assert controller._ready_pending
        controller.submit_event(f"cm5_power_reboot 1 {REQUEST_ID}".encode())
        assert not controller._ready_pending

        worker = asyncio.create_task(controller.run())
        with pytest.raises(LinkClosed):
            await worker

        session.hook = None
        controller.replay_pending_callbacks()
        worker = asyncio.create_task(controller.run())
        try:
            await _wait_for(lambda: (PowerAction.REBOOT, None, None)
                            in helper.calls)
            # The rejected original claim is not replayed between the two
            # Accepted attempts, so firmware cannot mistake it for a restart.
            assert sum(command.startswith("cm5 power report 1 0 ")
                       for command in session.commands) == 1
            assert session.commands.count(
                f"cm5 power ack 1 {REQUEST_ID} accepted") == 2
        finally:
            await _stop(worker, controller)

    run(main())


def test_status_event_ack_report_and_applied_sequence():
    async def main():
        session, helper = FakeSession(), FakeHelper()
        controller = PowerController(session, _cfg(), helper=helper)
        worker = asyncio.create_task(controller.run())
        try:
            assert controller.submit_event(
                f"cm5_power_status 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: session.commands[-1:].count(
                f"cm5 power ack 1 {REQUEST_ID} applied") == 1)
            assert session.commands == [
                f"cm5 power ack 1 {REQUEST_ID} accepted",
                f"cm5 power report 1 {REQUEST_ID} awake balanced {BOOT_TAG}",
                f"cm5 power ack 1 {REQUEST_ID} applied",
            ]
            assert helper.calls == [(PowerAction.STATUS, None, None)]
        finally:
            await _stop(worker, controller)

    run(main())


def test_status_reports_observed_governor_instead_of_stale_logical_mode():
    async def main():
        session, helper = FakeSession(), FakeHelper()

        async def external_change(action, profile, minutes):
            if action is PowerAction.STATUS:
                return HelperResult(True, "ok", HelperStatus(
                    state=HostPowerState.AWAKE,
                    profile=PowerProfile.ECO,
                    suspend_supported=True,
                    rtc_sleep_supported=True,
                ))
            return None

        helper.hook = external_change
        controller = PowerController(session, _cfg(initial_profile="balanced"),
                                     helper=helper)
        worker = asyncio.create_task(controller.run())
        try:
            controller.submit_event(
                f"cm5_power_status 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: f"cm5 power ack 1 {REQUEST_ID} applied"
                            in session.commands)
            assert (f"cm5 power report 1 {REQUEST_ID} awake eco {BOOT_TAG}"
                    in session.commands)
        finally:
            await _stop(worker, controller)

    run(main())


def test_failed_status_in_auto_mode_reports_observed_concrete_profile():
    async def main():
        session, helper = FakeSession(), FakeHelper()
        controller = PowerController(
            session,
            _cfg(initial_profile="auto", auto_idle_profile="eco"),
            helper=helper,
        )
        await controller.start()

        async def failed_read(action, profile, minutes):
            if action is PowerAction.STATUS:
                return HelperResult(False, "partial_status", HelperStatus(
                    state=HostPowerState.AWAKE,
                    profile=PowerProfile.ECO,
                ))
            return None

        helper.hook = failed_read
        worker = asyncio.create_task(controller.run())
        try:
            controller.submit_event(
                f"cm5_power_status 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: f"cm5 power ack 1 {REQUEST_ID} failed"
                            in session.commands)
            assert (f"cm5 power report 1 {REQUEST_ID} error eco {BOOT_TAG}"
                    in session.commands)
        finally:
            await _stop(worker, controller)

    run(main())


def test_profile_failure_reports_helper_readback_not_previous_mode():
    async def main():
        session, helper = FakeSession(), FakeHelper()

        async def partial_failure(action, profile, minutes):
            if action is PowerAction.PROFILE:
                return HelperResult(False, "governor_write_failed_rollback_failed",
                                    HelperStatus(profile=PowerProfile.UNKNOWN))
            return None

        helper.hook = partial_failure
        controller = PowerController(session, _cfg(initial_profile="balanced"),
                                     helper=helper)
        worker = asyncio.create_task(controller.run())
        try:
            controller.submit_event(
                f"cm5_power_profile_performance 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: f"cm5 power ack 1 {REQUEST_ID} failed"
                            in session.commands)
            assert (f"cm5 power report 1 {REQUEST_ID} error unknown {BOOT_TAG}"
                    in session.commands)
            assert controller.current_mode is PowerProfile.BALANCED
        finally:
            await _stop(worker, controller)

    run(main())


def test_request_id_dedupe_never_reexecutes_and_replays_terminal_ack():
    async def main():
        session, helper = FakeSession(), FakeHelper()
        controller = PowerController(session, _cfg(), helper=helper)
        event = f"cm5_power_profile_eco 1 {REQUEST_ID}".encode()
        assert controller.submit_event(event)
        assert controller.submit_event(event)       # duplicate while queued
        worker = asyncio.create_task(controller.run())
        try:
            await _wait_for(lambda: controller.current_mode is PowerProfile.ECO)
            assert controller.submit_event(event)   # duplicate after completion
            await _wait_for(lambda: session.commands.count(
                f"cm5 power ack 1 {REQUEST_ID} applied") == 2)
            assert helper.calls.count(
                (PowerAction.PROFILE, PowerProfile.ECO, None)) == 1
        finally:
            await _stop(worker, controller)

    run(main())


def test_disruptive_action_requires_confirmed_accepted_ack():
    async def main():
        session, helper = FakeSession(), FakeHelper()

        async def reject_accepted(line):
            if line.endswith(" accepted"):
                return Reply(["Error: rejected"])
            return None

        session.hook = reject_accepted
        controller = PowerController(session, _cfg(), helper=helper)
        worker = asyncio.create_task(controller.run())
        try:
            controller.submit_event(f"cm5_power_reboot 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: session.commands[-1:].count(
                f"cm5 power ack 1 {REQUEST_ID} failed") == 1)
            assert helper.calls == []
            assert session.commands[:2] == [
                f"cm5 power ack 1 {REQUEST_ID} accepted",
                f"cm5 power ack 1 {REQUEST_ID} failed",
            ]
            record = controller._records[REQUEST_ID]
            assert record.ack is AckState.FAILED
            assert not record.ack_pending
            assert record.ack_retry_count == 0
            assert record.ack_retry_task is None
        finally:
            await _stop(worker, controller)

    run(main())


def test_disruptive_action_requires_confirmed_committed_ack():
    async def main():
        session, helper = FakeSession(), FakeHelper()

        async def reject_committed(line):
            if line.endswith(" committed"):
                return Reply(["Error: rejected"])
            return None

        session.hook = reject_committed
        controller = PowerController(session, _cfg(), helper=helper)
        worker = asyncio.create_task(controller.run())
        try:
            controller.submit_event(f"cm5_power_reboot 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: f"cm5 power ack 1 {REQUEST_ID} failed"
                            in session.commands)
            assert helper.calls == []
            assert f"cm5 power ack 1 {REQUEST_ID} committed" in session.commands
        finally:
            await _stop(worker, controller)

    run(main())


@pytest.mark.parametrize(("event", "action", "transition"), [
    ("cm5_power_reboot", PowerAction.REBOOT, "rebooting"),
    ("cm5_power_halt", PowerAction.HALT, "halting"),
])
def test_reboot_and_low_power_halt_are_exact_typed_operations(
        event, action, transition):
    async def main():
        session, helper = FakeSession(), FakeHelper()

        async def inspect(action_seen, profile, minutes):
            if action_seen is action:
                assert session.commands[-3:] == [
                    f"cm5 power ack 1 {REQUEST_ID} accepted",
                    f"cm5 power report 1 {REQUEST_ID} {transition} balanced {BOOT_TAG}",
                    f"cm5 power ack 1 {REQUEST_ID} committed",
                ]
            return None

        helper.hook = inspect
        controller = PowerController(session, _cfg(), helper=helper)
        worker = asyncio.create_task(controller.run())
        try:
            controller.submit_event(f"{event} 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: (action, None, None) in helper.calls)
            await _wait_for(lambda: f"cm5 power ack 1 {REQUEST_ID} applied"
                            in session.commands)
        finally:
            await _stop(worker, controller)

    run(main())


def test_timed_sleep_is_bounded_acknowledged_then_armed_without_waiting():
    async def main():
        session, helper = FakeSession(), FakeHelper(rtc_sleep_supported=True)
        snapshots = []

        async def inspect(action, profile, minutes):
            if action is PowerAction.SLEEP_FOR:
                snapshots.append(list(session.commands))
            return None

        helper.hook = inspect
        controller = PowerController(session, _cfg(), helper=helper)
        worker = asyncio.create_task(controller.run())
        started = time.monotonic()
        try:
            controller.submit_event(
                f"cm5_power_sleep_for 1 {REQUEST_ID} 60".encode())
            await _wait_for(lambda: (PowerAction.SLEEP_FOR, None, 60)
                            in helper.calls)
            assert time.monotonic() - started < 1.0
            assert snapshots[0][-3:] == [
                f"cm5 power ack 1 {REQUEST_ID} accepted",
                f"cm5 power report 1 {REQUEST_ID} sleeping balanced {BOOT_TAG}",
                f"cm5 power ack 1 {REQUEST_ID} committed",
            ]
            assert helper.calls[:2] == [
                (PowerAction.STATUS, None, None),
                (PowerAction.SLEEP_FOR, None, 60),
            ]
        finally:
            await _stop(worker, controller)

    run(main())


def test_suspend_is_opt_in_and_capability_checked():
    async def disabled():
        session, helper = FakeSession(), FakeHelper()
        controller = PowerController(
            session, _cfg(allow_suspend=False), helper=helper)
        worker = asyncio.create_task(controller.run())
        try:
            controller.submit_event(f"cm5_power_suspend 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: f"cm5 power ack 1 {REQUEST_ID} failed"
                            in session.commands)
            assert helper.calls == []
            assert f"cm5 power ack 1 {REQUEST_ID} accepted" not in session.commands
        finally:
            await _stop(worker, controller)

    async def unsupported():
        session = FakeSession()
        helper = FakeHelper(suspend_supported=False)
        controller = PowerController(
            session, _cfg(allow_suspend=True), helper=helper)
        worker = asyncio.create_task(controller.run())
        try:
            controller.submit_event(f"cm5_power_suspend 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: f"cm5 power ack 1 {REQUEST_ID} failed"
                            in session.commands)
            assert helper.calls == [(PowerAction.STATUS, None, None)]
        finally:
            await _stop(worker, controller)

    async def supported():
        session, helper = FakeSession(), FakeHelper(suspend_supported=True)
        controller = PowerController(
            session, _cfg(allow_suspend=True), helper=helper)
        worker = asyncio.create_task(controller.run())
        try:
            controller.submit_event(f"cm5_power_suspend 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: (PowerAction.SUSPEND, None, None)
                            in helper.calls)
            await _wait_for(lambda: f"cm5 power report 1 0 awake unknown {BOOT_TAG}"
                            in session.commands)
            assert f"cm5 power ack 1 {REQUEST_ID} accepted" in session.commands
            assert (f"cm5 power report 1 {REQUEST_ID} suspending balanced "
                    f"{BOOT_TAG}") \
                in session.commands
        finally:
            await _stop(worker, controller)

    run(disabled())
    run(unsupported())
    run(supported())


def test_auto_transitions_and_manual_mode_retention():
    async def main():
        session, helper = FakeSession(), FakeHelper()
        controller = PowerController(
            session,
            _cfg(initial_profile="auto", auto_active_profile="performance",
                 auto_idle_profile="eco", auto_idle_delay_s=0),
            helper=helper,
        )
        await controller.activity_started()
        await controller.activity_finished()
        assert helper.calls == [
            (PowerAction.PROFILE, PowerProfile.PERFORMANCE, None),
            (PowerAction.PROFILE, PowerProfile.ECO, None),
        ]

        worker = asyncio.create_task(controller.run())
        try:
            controller.submit_event(
                f"cm5_power_profile_balanced 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: controller.current_mode is PowerProfile.BALANCED)
            before = len(helper.calls)
            await controller.activity_started()
            await controller.activity_finished()
            assert len(helper.calls) == before, "manual profile must survive jobs"

            controller.submit_event(
                f"cm5_power_profile_auto 1 {OTHER_ID}".encode())
            await _wait_for(lambda: controller.current_mode is PowerProfile.AUTO)
            await controller.activity_started()
            assert helper.calls[-1] == (
                PowerAction.PROFILE, PowerProfile.PERFORMANCE, None)
            await controller.activity_finished()
        finally:
            await _stop(worker, controller)

    run(main())


def test_profile_request_and_activity_transition_are_serialized():
    """A job starting while profile-auto is in sysfs cannot be left in eco."""
    async def main():
        session, helper = FakeSession(), FakeHelper()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def block_auto_idle(action, profile, minutes):
            if action is PowerAction.PROFILE and profile is PowerProfile.ECO:
                entered.set()
                await release.wait()
            return None

        helper.hook = block_auto_idle
        controller = PowerController(session, _cfg(initial_profile="balanced"),
                                     helper=helper)
        worker = asyncio.create_task(controller.run())
        try:
            controller.submit_event(
                f"cm5_power_profile_auto 1 {REQUEST_ID}".encode())
            await entered.wait()
            activity = asyncio.create_task(controller.activity_started())
            await asyncio.sleep(0)
            release.set()
            await activity
            await _wait_for(lambda: controller.current_mode is PowerProfile.AUTO)
            assert helper.calls[-1] == (
                PowerAction.PROFILE, PowerProfile.PERFORMANCE, None)
            await controller.activity_finished()
        finally:
            await _stop(worker, controller)

    run(main())


def test_cancel_during_active_profile_apply_rolls_back_activity_lease():
    async def main():
        session, helper = FakeSession(), FakeHelper()
        entered = asyncio.Event()
        first_active = True

        async def block_first_active(action, profile, minutes):
            nonlocal first_active
            if (first_active and action is PowerAction.PROFILE and
                    profile is PowerProfile.PERFORMANCE):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    first_active = False
            return None

        helper.hook = block_first_active
        controller = PowerController(
            session,
            _cfg(initial_profile="auto", auto_active_profile="performance",
                 auto_idle_profile="eco", auto_idle_delay_s=0),
            helper=helper,
        )
        acquisition = asyncio.create_task(controller.activity_started())
        await entered.wait()
        acquisition.cancel()
        with suppress(asyncio.CancelledError):
            await acquisition

        assert controller._activity_count == 0
        await _wait_for(lambda: helper.calls[-1:] == [
            (PowerAction.PROFILE, PowerProfile.ECO, None)])

        # A later job must again be the first active lease, promote, and return
        # fully to idle; the canceled acquisition cannot bias the counter.
        await controller.activity_started()
        assert controller._activity_count == 1
        assert helper.calls[-1] == (
            PowerAction.PROFILE, PowerProfile.PERFORMANCE, None)
        await controller.activity_finished()
        assert controller._activity_count == 0
        assert helper.calls[-1] == (
            PowerAction.PROFILE, PowerProfile.ECO, None)
        await controller.close()

    run(main())


# -- terminal callback retry / reconnect FSM --------------------------------


def test_terminal_ack_transient_errors_retry_then_reset_on_success(monkeypatch):
    monkeypatch.setattr(power_mod, "_ACK_RETRY_DELAYS_S", (0.005, 0.01, 0.02))

    async def main():
        session, helper = FakeSession(), FakeHelper()
        attempts = 0

        async def fail_twice(line):
            nonlocal attempts
            if line == f"cm5 power ack 1 {REQUEST_ID} applied":
                attempts += 1
                if attempts < 3:
                    return Reply(["Error: transient callback failure"])
            return None

        session.hook = fail_twice
        controller = PowerController(session, _cfg(), helper=helper)
        worker = asyncio.create_task(controller.run())
        try:
            controller.submit_event(f"cm5_power_status 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: attempts == 3)
            record = controller._records[REQUEST_ID]
            assert not record.ack_pending
            assert record.ack_retry_count == 0
            assert not record.ack_retry_exhausted
            assert record.ack_retry_task is None
            assert helper.calls == [(PowerAction.STATUS, None, None)]
        finally:
            await _stop(worker, controller)

    run(main())


def test_terminal_ack_retry_budget_is_finite(monkeypatch):
    monkeypatch.setattr(power_mod, "_ACK_RETRY_DELAYS_S", (0.005, 0.01))

    async def main():
        session, helper = FakeSession(), FakeHelper()
        attempts = 0

        async def always_fail_applied(line):
            nonlocal attempts
            if line == f"cm5 power ack 1 {REQUEST_ID} applied":
                attempts += 1
                return Reply(["Error: still unavailable"])
            return None

        session.hook = always_fail_applied
        controller = PowerController(session, _cfg(), helper=helper)
        worker = asyncio.create_task(controller.run())
        try:
            controller.submit_event(f"cm5_power_status 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: attempts == 3)  # initial + two delayed retries
            await asyncio.sleep(0.03)
            assert attempts == 3
            record = controller._records[REQUEST_ID]
            assert record.ack_pending
            assert record.ack_retry_count == 2
            assert record.ack_retry_exhausted
            assert record.ack_retry_task is None
        finally:
            await _stop(worker, controller)

    run(main())


def test_duplicate_events_do_not_duplicate_retry_timer_and_close_cancels_it(
        monkeypatch):
    monkeypatch.setattr(power_mod, "_ACK_RETRY_DELAYS_S", (1.0,))

    async def main():
        session, helper = FakeSession(), FakeHelper()

        async def always_fail_applied(line):
            if line == f"cm5 power ack 1 {REQUEST_ID} applied":
                return Reply(["Error: retry later"])
            return None

        session.hook = always_fail_applied
        controller = PowerController(session, _cfg(), helper=helper)
        event = f"cm5_power_status 1 {REQUEST_ID}".encode()
        worker = asyncio.create_task(controller.run())
        controller.submit_event(event)
        await _wait_for(
            lambda: controller._records[REQUEST_ID].ack_retry_task is not None)
        record = controller._records[REQUEST_ID]
        scheduled = record.ack_retry_task
        assert scheduled is not None

        controller.submit_event(event)
        controller.submit_event(event)
        controller.submit_event(event)
        await _wait_for(lambda: session.commands.count(
            f"cm5 power ack 1 {REQUEST_ID} applied") == 4)
        assert record.ack_retry_task is scheduled
        assert record.ack_retry_count == 1

        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        await controller.close()
        assert scheduled.done()
        assert record.ack_retry_task is None

    run(main())


def test_reconnect_replay_preempts_pending_backoff(monkeypatch):
    monkeypatch.setattr(power_mod, "_ACK_RETRY_DELAYS_S", (1.0,))

    async def main():
        session, helper = FakeSession(), FakeHelper()
        fail = True

        async def fail_once(line):
            if fail and line == f"cm5 power ack 1 {REQUEST_ID} applied":
                return Reply(["Error: transient callback failure"])
            return None

        session.hook = fail_once
        controller = PowerController(session, _cfg(), helper=helper)
        worker = asyncio.create_task(controller.run())
        try:
            controller.submit_event(f"cm5_power_status 1 {REQUEST_ID}".encode())
            await _wait_for(
                lambda: controller._records[REQUEST_ID].ack_retry_task is not None)
            record = controller._records[REQUEST_ID]
            scheduled = record.ack_retry_task
            fail = False
            controller.replay_pending_callbacks()
            await _wait_for(lambda: not record.ack_pending)
            assert record.ack_retry_count == 0
            assert record.ack_retry_task is None
            assert scheduled is not None and scheduled.done()
            assert not any(command.startswith("cm5 power report 1 0 ")
                           for command in session.commands)
        finally:
            await _stop(worker, controller)

    run(main())


# -- reconnect/cancellation FSM ---------------------------------------------


def test_linkclosed_before_accepted_requeues_and_executes_once_after_reconnect():
    async def main():
        session, helper = FakeSession(), FakeHelper()
        first = True

        async def close_once(line):
            nonlocal first
            if first and line.endswith(" accepted"):
                first = False
                raise LinkClosed("test close")
            return None

        session.hook = close_once
        controller = PowerController(session, _cfg(), helper=helper)
        controller.submit_event(f"cm5_power_reboot 1 {REQUEST_ID}".encode())
        worker = asyncio.create_task(controller.run())
        with pytest.raises(LinkClosed):
            await worker

        session.hook = None
        worker = asyncio.create_task(controller.run())
        try:
            await _wait_for(lambda: (PowerAction.REBOOT, None, None)
                            in helper.calls)
            assert helper.calls.count((PowerAction.REBOOT, None, None)) == 1
        finally:
            await _stop(worker, controller)

    run(main())


def test_disconnect_after_accepted_before_execution_resumes_without_loss():
    async def main():
        session, helper = FakeSession(), FakeHelper()
        first_report = True

        async def close_on_transition(line):
            nonlocal first_report
            if first_report and " rebooting " in line:
                first_report = False
                raise LinkClosed("transition report lost")
            return None

        session.hook = close_on_transition
        controller = PowerController(session, _cfg(), helper=helper)
        controller.submit_event(f"cm5_power_reboot 1 {REQUEST_ID}".encode())
        worker = asyncio.create_task(controller.run())
        with pytest.raises(LinkClosed):
            await worker
        assert helper.calls == []

        session.hook = None
        worker = asyncio.create_task(controller.run())
        try:
            await _wait_for(lambda: (PowerAction.REBOOT, None, None)
                            in helper.calls)
            assert helper.calls.count((PowerAction.REBOOT, None, None)) == 1
            assert session.commands.count(
                f"cm5 power ack 1 {REQUEST_ID} accepted") == 2
        finally:
            await _stop(worker, controller)

    run(main())


def test_disconnect_during_committed_ack_reconfirms_before_single_execution():
    async def main():
        session, helper = FakeSession(), FakeHelper()
        first = True

        async def close_once(line):
            nonlocal first
            if first and line.endswith(" committed"):
                first = False
                raise LinkClosed("committed reply lost")
            return None

        session.hook = close_once
        controller = PowerController(session, _cfg(), helper=helper)
        controller.submit_event(f"cm5_power_reboot 1 {REQUEST_ID}".encode())
        worker = asyncio.create_task(controller.run())
        with pytest.raises(LinkClosed):
            await worker
        assert helper.calls == []

        session.hook = None
        controller.replay_pending_callbacks()
        worker = asyncio.create_task(controller.run())
        try:
            await _wait_for(lambda: (PowerAction.REBOOT, None, None)
                            in helper.calls)
            assert session.commands.count(
                f"cm5 power ack 1 {REQUEST_ID} committed") == 2
            assert helper.calls.count((PowerAction.REBOOT, None, None)) == 1
        finally:
            await _stop(worker, controller)

    run(main())


def test_linkclosed_during_precommit_failure_report_replays_failed():
    async def main():
        session, helper = FakeSession(), FakeHelper()
        report_close = True

        async def close_on_error_report(line):
            nonlocal report_close
            if report_close and " error " in line:
                report_close = False
                raise LinkClosed("failure report lost")
            return None

        session.hook = close_on_error_report
        controller = PowerController(
            session, _cfg(allow_suspend=False), helper=helper)
        controller.submit_event(f"cm5_power_suspend 1 {REQUEST_ID}".encode())
        worker = asyncio.create_task(controller.run())
        with pytest.raises(LinkClosed):
            await worker

        session.hook = None
        controller.replay_pending_callbacks()
        worker = asyncio.create_task(controller.run())
        try:
            await _wait_for(lambda: f"cm5 power ack 1 {REQUEST_ID} failed"
                            in session.commands)
            assert helper.calls == []
        finally:
            await _stop(worker, controller)

    run(main())


def test_committed_helper_failure_stays_uncertain_and_never_reexecutes():
    async def main():
        session, helper = FakeSession(), FakeHelper()

        async def fail_reboot(action, profile, minutes):
            if action is PowerAction.REBOOT:
                return HelperResult(False, "helper_timeout")
            return None

        helper.hook = fail_reboot
        controller = PowerController(session, _cfg(), helper=helper)
        event = f"cm5_power_reboot 1 {REQUEST_ID}".encode()
        controller.submit_event(event)
        worker = asyncio.create_task(controller.run())
        try:
            await _wait_for(lambda: (PowerAction.REBOOT, None, None)
                            in helper.calls)
            assert f"cm5 power ack 1 {REQUEST_ID} committed" in session.commands
            assert f"cm5 power ack 1 {REQUEST_ID} failed" not in session.commands
            assert not any(" error " in command for command in session.commands)

            controller.submit_event(event)
            await _wait_for(lambda: session.commands.count(
                f"cm5 power ack 1 {REQUEST_ID} committed") >= 2)
            assert helper.calls.count((PowerAction.REBOOT, None, None)) == 1
        finally:
            await _stop(worker, controller)

    run(main())


def test_cancel_after_dequeue_before_accepted_requeues_original():
    async def main():
        session, helper = FakeSession(), FakeHelper()
        entered = asyncio.Event()
        block = True

        async def wait_once(line):
            nonlocal block
            if block and line.endswith(" accepted"):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    block = False
            return None

        session.hook = wait_once
        controller = PowerController(session, _cfg(), helper=helper)
        controller.submit_event(f"cm5_power_reboot 1 {REQUEST_ID}".encode())
        worker = asyncio.create_task(controller.run())
        await entered.wait()
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker

        session.hook = None
        worker = asyncio.create_task(controller.run())
        try:
            await _wait_for(lambda: (PowerAction.REBOOT, None, None)
                            in helper.calls)
            assert helper.calls.count((PowerAction.REBOOT, None, None)) == 1
        finally:
            await _stop(worker, controller)

    run(main())


def test_cancel_after_execution_started_makes_duplicate_ack_only():
    async def main():
        session, helper = FakeSession(), FakeHelper()
        entered = asyncio.Event()

        async def block_reboot(action, profile, minutes):
            if action is PowerAction.REBOOT:
                entered.set()
                await asyncio.Future()
            return None

        helper.hook = block_reboot
        controller = PowerController(session, _cfg(), helper=helper)
        event = f"cm5_power_reboot 1 {REQUEST_ID}".encode()
        controller.submit_event(event)
        worker = asyncio.create_task(controller.run())
        await entered.wait()
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker

        controller.submit_event(event)
        worker = asyncio.create_task(controller.run())
        try:
            await _wait_for(lambda: session.commands.count(
                f"cm5 power ack 1 {REQUEST_ID} committed") >= 2)
            assert helper.calls.count((PowerAction.REBOOT, None, None)) == 1
        finally:
            await _stop(worker, controller)

    run(main())


def test_reconnect_replays_only_pending_acks_newest_first_without_ready_report():
    async def main():
        session, helper = FakeSession(), FakeHelper()
        cfg = _cfg(event_queue_size=4, request_cache_size=128)
        controller = PowerController(session, cfg, helper=helper)
        worker = asyncio.create_task(controller.run())
        try:
            ids = [f"deadbeef{i:08x}" for i in range(1, 41)]
            for request_id in ids:
                controller.submit_event(
                    f"cm5_power_status 1 {request_id}".encode())
                await _wait_for(lambda rid=request_id: (
                    f"cm5 power ack 1 {rid} applied" in session.commands))
            # Simulate callbacks becoming uncertain across a disconnect. The
            # queue is smaller than history; current/newest must win.
            for record in controller._records.values():
                record.ack_pending = True
            session.commands.clear()
            controller.replay_pending_callbacks()
            await _wait_for(lambda: bool(session.commands))
            assert session.commands[0] == (
                f"cm5 power ack 1 {ids[-1]} applied")
            assert not any(command.startswith("cm5 power report 1 0 ")
                           for command in session.commands)
        finally:
            await _stop(worker, controller)

    run(main())


# -- route/pipeline/real Session integration --------------------------------


def test_route_link_event_gives_power_events_to_controller_without_job():
    class Controller:
        def __init__(self):
            self.seen = []

        def submit_event(self, payload):
            self.seen.append(payload)
            return True

    async def main():
        trigger, controller = ManualTrigger(), Controller()
        route_link_event(b"cm5_power_status 1 deadbeef00000001",
                         trigger, power=controller)
        assert controller.seen
        assert trigger._queue.empty()

    run(main())


def test_power_evt_end_to_end_over_session_and_fake_firmware(firmware):
    async def main():
        transport, session = open_link(firmware)
        helper = FakeHelper()
        controller = PowerController(session, _cfg(), helper=helper)
        trigger = ManualTrigger()
        tasks = []
        try:
            await session.login()
            session.on_event = lambda payload: route_link_event(
                payload, trigger, session, controller)
            tasks = [asyncio.create_task(controller.run()),
                     asyncio.create_task(session.pump_events())]
            firmware.push_event(f"cm5_power_status 1 {REQUEST_ID}")
            await _wait_for(lambda: (REQUEST_ID, "applied")
                            in firmware.cm5_power_acks)
            assert firmware.cm5_power_acks[:2] == [
                (REQUEST_ID, "accepted"), (REQUEST_ID, "applied")]
            assert (REQUEST_ID, "awake", "balanced", BOOT_TAG) \
                in firmware.cm5_power_reports
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            await controller.close()
            transport.close()

    run(main())


def test_pipeline_wraps_jobs_with_active_idle_hooks_even_on_failure():
    class SessionStub:
        reboot_suspected = False

    class Source:
        def evenai_done(self):
            pass

    class Activity:
        def __init__(self):
            self.events = []

        async def activity_started(self):
            self.events.append("active")

        async def activity_finished(self):
            self.events.append("idle")

    async def main():
        activity = Activity()
        pipeline = VoicePipeline(SessionStub(), None, None, Config(),
                                 power_activity=activity)

        async def fail(_text):
            activity.events.append("job")
            raise RuntimeError("boom")

        pipeline.run_chat = fail  # type: ignore[method-assign]
        try:
            with pytest.raises(RuntimeError):
                await pipeline._dispatch(Job("chat", "hello"), Source())
            assert activity.events == ["active", "job", "idle"]
        finally:
            await pipeline.close()

    run(main())


def test_slow_stt_load_does_not_block_power_and_is_reused_after_cancel(monkeypatch):
    """A link flap cannot start a second cancel-opaque native model load."""
    import importlib

    main_mod = importlib.import_module("hw1_ai_service.__main__")
    stt_mod = importlib.import_module("hw1_ai_service.stt")
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def slow_create(engine, model):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(5)
        return None

    async def no_llm(_cfg):
        return None, None

    monkeypatch.setattr(stt_mod, "create_engine", slow_create)
    monkeypatch.setattr(main_mod, "_make_llm", no_llm)

    async def main():
        cfg = Config()
        cfg.stt.engine = "fake"
        cfg.llm.engine = "none"
        session, helper = FakeSession(), FakeHelper()
        power = PowerController(session, _cfg(), helper=helper)
        source = ManualTrigger()
        lazy = main_mod._LazyDaemonPipeline(session, cfg, power)
        power_worker = asyncio.create_task(power.run())
        first = asyncio.create_task(lazy.daemon(source))
        try:
            await _wait_for(entered.is_set)
            power.submit_event(f"cm5_power_status 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: f"cm5 power ack 1 {REQUEST_ID} applied"
                            in session.commands)

            first.cancel()
            with suppress(asyncio.CancelledError):
                await first
            second = asyncio.create_task(lazy.daemon(source))
            release.set()
            await _wait_for(lambda: lazy._pipeline is not None)
            assert calls == 1
            second.cancel()
            with suppress(asyncio.CancelledError):
                await second
        finally:
            release.set()
            await lazy.close()
            await _stop(power_worker, power)

    run(main())


# -- config and root helper --------------------------------------------------


@pytest.mark.parametrize("yaml_text", [
    "power:\n  initial_profile: turbo\n",
    "power:\n  auto_active_profile: auto\n",
    "power:\n  helper_path: relative/helper\n",
    "power:\n  min_sleep_minutes: 0\n",
    "power:\n  min_sleep_minutes: 20\n  max_sleep_minutes: 10\n",
    "power:\n  max_sleep_minutes: 1441\n",
    "power:\n  event_queue_size: 64\n  request_cache_size: 32\n",
])
def test_invalid_power_config_is_rejected(tmp_path, yaml_text):
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text)
    with pytest.raises(ValueError):
        load(path)


def test_power_only_daemon_config_is_valid(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "stt:\n  engine: none\n"
        "llm:\n  engine: none\n"
        "power:\n  enabled: true\n")
    cfg = load(path)
    assert cfg.power.enabled
    assert cfg.stt.engine == cfg.llm.engine == "none"


def test_strict_ram_failure_degrades_power_daemon_but_not_oneshot(monkeypatch):
    import importlib
    import sys

    main_mod = importlib.import_module("hw1_ai_service.__main__")
    cfg = Config()
    cfg.power.enabled = True
    cfg.service.ram_check = "strict"
    seen = []

    monkeypatch.setattr(main_mod.config_mod, "load", lambda _path: cfg)
    monkeypatch.setattr(main_mod.log_mod, "setup", lambda _verbose: None)
    monkeypatch.setattr(main_mod.mem, "preflight",
                        lambda _cfg: (_ for _ in ()).throw(RuntimeError("too big")))

    def fake_run(coro):
        seen.append(True)
        coro.close()

    monkeypatch.setattr(main_mod.asyncio, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["hw1-ai-service", "daemon"])
    main_mod.main()
    assert seen and cfg.stt.engine == cfg.llm.engine == "none"

    cfg.stt.engine = "fake"
    cfg.llm.engine = "fake"
    monkeypatch.setattr(sys, "argv", ["hw1-ai-service", "ask"])
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code == 2


def _load_root_helper():
    path = Path(__file__).resolve().parent.parent / "systemd" / "hw1-power-helper"
    loader = importlib.machinery.SourceFileLoader("hw1_power_helper_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_root_helper_applies_and_verifies_governors(tmp_path, monkeypatch):
    helper = _load_root_helper()
    cpufreq = tmp_path / "cpufreq"
    for name in ("policy0", "policy4"):
        policy = cpufreq / name
        policy.mkdir(parents=True)
        (policy / "scaling_available_governors").write_text(
            "performance powersave schedutil")
        (policy / "scaling_governor").write_text("performance")
    monkeypatch.setattr(helper, "_CPUFREQ_ROOT", cpufreq)
    monkeypatch.setattr(helper, "_POWER_STATE", tmp_path / "missing-state")
    monkeypatch.setattr(helper, "_RTC_WAKEALARM", tmp_path / "missing-alarm")
    monkeypatch.setattr(helper, "_SYSTEMCTL_PATHS", ())

    result = helper._apply_profile("balanced")
    assert result["ok"]
    assert [(cpufreq / name / "scaling_governor").read_text()
            for name in ("policy0", "policy4")] == ["schedutil", "schedutil"]

    for name in ("policy0", "policy4"):
        (cpufreq / name / "scaling_available_governors").write_text("powersave")
    assert helper._apply_profile("balanced")["code"] == "governor_unsupported"


def test_root_helper_rolls_back_partial_governor_write(tmp_path, monkeypatch):
    helper = _load_root_helper()
    cpufreq = tmp_path / "cpufreq"
    for name in ("policy0", "policy4"):
        policy = cpufreq / name
        policy.mkdir(parents=True)
        (policy / "scaling_available_governors").write_text(
            "performance powersave schedutil")
        (policy / "scaling_governor").write_text("performance")
    monkeypatch.setattr(helper, "_CPUFREQ_ROOT", cpufreq)
    monkeypatch.setattr(helper, "_POWER_STATE", tmp_path / "missing-state")
    monkeypatch.setattr(helper, "_RTC_WAKEALARM", tmp_path / "missing-alarm")
    monkeypatch.setattr(helper, "_SYSTEMCTL_PATHS", ())

    real_write = helper._write_governor
    target_writes = 0

    def fail_second_target(policy, governor):
        nonlocal target_writes
        if governor == "schedutil":
            target_writes += 1
            if target_writes == 2:
                raise OSError("simulated policy write failure")
        real_write(policy, governor)

    monkeypatch.setattr(helper, "_write_governor", fail_second_target)
    result = helper._apply_profile("balanced")
    assert result["code"] == "governor_write_failed_rolled_back"
    assert result["profile"] == "performance"
    assert [(cpufreq / name / "scaling_governor").read_text()
            for name in ("policy0", "policy4")] == ["performance", "performance"]


def test_root_helper_reports_unknown_when_any_policy_read_is_incomplete(
        tmp_path, monkeypatch):
    helper = _load_root_helper()
    cpufreq = tmp_path / "cpufreq"
    policies = []
    for name in ("policy0", "policy4"):
        policy = cpufreq / name
        policy.mkdir(parents=True)
        (policy / "scaling_governor").write_text("powersave")
        policies.append(policy)
    monkeypatch.setattr(helper, "_CPUFREQ_ROOT", cpufreq)
    monkeypatch.setattr(helper, "_POWER_STATE", tmp_path / "missing-state")
    monkeypatch.setattr(helper, "_RTC_WAKEALARM", tmp_path / "missing-alarm")
    monkeypatch.setattr(helper, "_SYSTEMCTL_PATHS", ())
    real_read = helper._read_governor

    def incomplete(policy):
        return None if policy.name == "policy4" else real_read(policy)

    monkeypatch.setattr(helper, "_read_governor", incomplete)
    status = helper.dispatch("status")
    assert status["ok"]
    assert status["profile"] == "unknown"
    assert status["governors"] == ["powersave"]


def test_root_helper_verifies_rtc_alarm_before_low_power_halt(tmp_path, monkeypatch):
    helper = _load_root_helper()
    systemctl = tmp_path / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n")
    systemctl.chmod(0o755)

    class Alarm:
        def __init__(self, valid=True):
            self.valid = valid
            self.value = "0"
            self.writes = []

        def exists(self):
            return True

        def write_text(self, value):
            self.writes.append(value)
            if value.startswith("+"):
                self.value = str(1000 + int(value[1:])) if self.valid else "1"
            else:
                self.value = value

        def read_text(self):
            return self.value

    commands = []
    monkeypatch.setattr(helper, "_SYSTEMCTL_PATHS", (systemctl,))
    monkeypatch.setattr(helper, "_CPUFREQ_ROOT", tmp_path / "cpufreq")
    monkeypatch.setattr(helper, "_POWER_STATE", tmp_path / "power-state")
    monkeypatch.setattr(helper.time, "time", lambda: 1000.0)
    monkeypatch.setattr(helper, "_run_fixed",
                        lambda argv: (commands.append(argv) or (True, "ok")))

    alarm = Alarm(valid=True)
    monkeypatch.setattr(helper, "_RTC_WAKEALARM", alarm)
    result = helper._sleep_for(5)
    assert result["ok"]
    assert alarm.writes == ["0", "+300"]
    assert commands == [[str(systemctl), "--no-block", "halt"]]

    commands.clear()
    bad_alarm = Alarm(valid=False)
    monkeypatch.setattr(helper, "_RTC_WAKEALARM", bad_alarm)
    result = helper._sleep_for(5)
    assert result["code"] == "rtc_verify_failed"
    assert bad_alarm.writes[-1] == "0"
    assert commands == [], "halt must not run without verified alarm readback"


def test_root_helper_suspend_is_explicitly_capability_checked(tmp_path, monkeypatch):
    helper = _load_root_helper()
    systemctl = tmp_path / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n")
    systemctl.chmod(0o755)
    state = tmp_path / "state"
    state.write_text("freeze disk")
    monkeypatch.setattr(helper, "_SYSTEMCTL_PATHS", (systemctl,))
    monkeypatch.setattr(helper, "_POWER_STATE", state)
    monkeypatch.setattr(helper, "_CPUFREQ_ROOT", tmp_path / "cpufreq")
    monkeypatch.setattr(helper, "_RTC_WAKEALARM", tmp_path / "alarm")
    assert not helper._suspend_supported()
    assert helper.dispatch("suspend")["code"] == "suspend_unsupported"
    state.write_text("freeze mem disk")
    assert helper._suspend_supported()


def test_installer_is_executable_and_validates_before_atomic_activation():
    path = Path(__file__).resolve().parent.parent / "systemd" / "install-power-helper.sh"
    text = path.read_text()
    assert path.stat().st_mode & 0o111
    assert text.index('visudo -cf "$sudoers_tmp"') < text.index(
        'mv -f "$sudoers_tmp" /etc/sudoers.d/hw1-power-helper')
