from __future__ import annotations

import asyncio
from contextlib import suppress
import time

import pytest

from conftest import open_link, run
from hw1_ai_service.cm5_presence import (
    BUSY_LEASE_MS,
    NORMAL_LEASE_MS,
    Cm5Presence,
    Cm5PresenceMode,
)
from hw1_ai_service.link.session import CommandCancelled, LinkClosed


class _Reply:
    def __init__(self, text: str, ok: bool = True) -> None:
        self.text = text
        self.ok = ok


class _Session:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.reply_override: _Reply | None = None

    async def command(self, line: str, **kwargs):
        self.calls.append((line, kwargs))
        if self.reply_override is not None:
            return self.reply_override
        _cm5, _heartbeat, _version, seq, mode = line.split()
        lease = BUSY_LEASE_MS if mode == "busy" else NORMAL_LEASE_MS
        return _Reply(
            f"OK: cm5 heartbeat version=1 seq={seq} state={mode} "
            f"session_epoch=7 lease_ms={lease}")


class _BlockingSession:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.never = asyncio.Event()

    async def command(self, _line: str, **_kwargs):
        self.entered.set()
        await self.never.wait()


class _UpgradeSession(_Session):
    def __init__(self) -> None:
        super().__init__()
        self.legacy = True

    async def command(self, line: str, **kwargs):
        if self.legacy:
            self.calls.append((line, kwargs))
            self.legacy = False
            return _Reply("Unknown command: cm5", ok=False)
        return await super().command(line, **kwargs)


class _RebootDuringReadySession(_Session):
    def __init__(self) -> None:
        super().__init__()
        self._reboot_listeners = []
        self.reboot_fired = False
        self.reboot_suspected = False

    def add_reboot_listener(self, listener) -> None:
        self._reboot_listeners.append(listener)

    async def command(self, line: str, **kwargs):
        mode = line.split()[-1]
        if mode == "ready" and not self.reboot_fired:
            self.calls.append((line, kwargs))
            self.reboot_fired = True
            self.reboot_suspected = True
            for listener in tuple(self._reboot_listeners):
                listener()
            # Models Session's safe pre-relogin boundary: the synchronous
            # reboot listener must invalidate this exact READY generation.
            assert kwargs["cancel_guard"]()
            raise CommandCancelled("reboot invalidated ready heartbeat")
        return await super().command(line, **kwargs)


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0)


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def test_actor_starts_as_starting_then_acknowledges_ready_and_busy():
    async def main() -> None:
        session = _Session()
        presence = Cm5Presence(session, interval_s=60)
        task = asyncio.create_task(presence.run())
        try:
            await _wait_until(lambda: len(session.calls) == 1)
            assert session.calls[0][0].endswith(" starting")
            assert session.calls[0][1]["expect"] == "status"
            assert session.calls[0][1]["timeout"] == 10.0
            assert session.calls[0][1]["replay"] is False
            assert await presence.set_mode(Cm5PresenceMode.READY)
            assert session.calls[-1][0].endswith(" ready")
            assert await presence.set_mode(Cm5PresenceMode.BUSY)
            assert session.calls[-1][0].endswith(" busy")
            assert [int(call[0].split()[3]) for call in session.calls] == [1, 2, 3]
        finally:
            await _cancel(task)

    run(main())


def test_actor_renews_on_deadline_without_command_overlap():
    async def main() -> None:
        session = _Session()
        presence = Cm5Presence(session, interval_s=0.01)
        task = asyncio.create_task(presence.run())
        try:
            await _wait_until(lambda: len(session.calls) >= 3)
            assert all(call[0].endswith(" starting") for call in session.calls[:3])
        finally:
            await _cancel(task)

    run(main())


def test_unknown_command_disables_actor_without_blocking_pipeline():
    async def main() -> None:
        session = _Session()
        session.reply_override = _Reply(
            "Unknown command: cm5\nType 'help' for available commands", ok=False)
        presence = Cm5Presence(session, interval_s=60)
        task = asyncio.create_task(presence.run())
        try:
            await _wait_until(lambda: presence.supported is False)
            assert await presence.set_mode(Cm5PresenceMode.READY) is False
            assert len(session.calls) == 1
        finally:
            await _cancel(task)

    run(main())


def test_link_reset_reprobes_after_legacy_firmware_upgrade():
    async def main() -> None:
        session = _UpgradeSession()
        presence = Cm5Presence(
            session, interval_s=60, legacy_reprobe_s=60)
        task = asyncio.create_task(presence.run())
        try:
            await _wait_until(lambda: presence.supported is False)
            assert await presence.set_mode(Cm5PresenceMode.READY) is False
            assert len(session.calls) == 1

            # Models a firmware OTA/re-login without restarting the daemon.
            presence.link_reset()
            await _wait_until(lambda: presence.supported is True)
            assert len(session.calls) == 2
            assert session.calls[-1][0].endswith(" starting")
        finally:
            await _cancel(task)

    run(main())


def test_actor_cancellation_wakes_acknowledged_mode_waiter():
    async def main() -> None:
        session = _BlockingSession()
        presence = Cm5Presence(session, interval_s=60)
        actor = asyncio.create_task(presence.run())
        await session.entered.wait()
        waiter = asyncio.create_task(
            presence.set_mode(Cm5PresenceMode.READY))
        await asyncio.sleep(0)

        await _cancel(actor)
        with pytest.raises(LinkClosed, match="actor stopped"):
            await asyncio.wait_for(waiter, 0.2)

    run(main())


def test_reboot_callback_prevents_ready_replay_into_new_epoch():
    async def main() -> None:
        session = _RebootDuringReadySession()
        presence = Cm5Presence(session, interval_s=60)
        task = asyncio.create_task(presence.run())
        await _wait_until(lambda: len(session.calls) == 1)
        assert session.calls[0][0].endswith(" starting")

        # READY reaches the old link just as reboot is detected. The callback
        # invalidates the captured generation before Session's auth replay
        # boundary, then the actor fails the supervised link closed. STARTING
        # is the only mode the replacement task group may publish.
        with pytest.raises(LinkClosed, match="suspected device reboot"):
            await presence.set_mode(Cm5PresenceMode.READY)
        with pytest.raises(LinkClosed, match="suspected device reboot"):
            await task
        assert [call[0].split()[-1] for call in session.calls] == [
            "starting", "ready"]
        assert presence.mode is Cm5PresenceMode.STARTING

    run(main())


def test_pending_reboot_rejects_late_ready_cleanup_before_write():
    async def main() -> None:
        session = _Session()
        session.reboot_suspected = True
        presence = Cm5Presence(session, interval_s=60)
        # Models a late exchange finalizer racing the synchronous reboot
        # listener and changing STARTING back to READY before cancellation.
        presence.set_mode_nowait(Cm5PresenceMode.READY)

        with pytest.raises(LinkClosed, match="suspected device reboot"):
            await presence.run()
        assert session.calls == []

    run(main())


def test_malformed_success_fails_waiters_closed():
    async def main() -> None:
        session = _Session()
        session.reply_override = _Reply("OK: wrong")
        presence = Cm5Presence(session, interval_s=60)
        task = asyncio.create_task(presence.run())
        with pytest.raises(LinkClosed, match="malformed"):
            await task
        with pytest.raises(LinkClosed, match="malformed"):
            await presence.set_mode(Cm5PresenceMode.READY)

    run(main())


def test_real_session_serializes_presence_and_foreground_commands(firmware):
    async def main() -> None:
        transport, session = open_link(firmware)
        presence_task = None
        try:
            await session.login()
            presence = Cm5Presence(session, interval_s=60)
            presence_task = asyncio.create_task(presence.run())
            assert await presence.set_mode(Cm5PresenceMode.READY)
            capabilities = await session.command(
                "cm5 capabilities", expect="status")
            assert capabilities.text == (
                "OK: cm5-presence-v1 heartbeat_modes="
                "starting,ready,busy,degraded interval_ms=5000 "
                "lease_ms=15000 busy_lease_ms=75000 "
                "cmd_grace_ms=5000")
            reply = await session.command("cm5 status", expect="status")
            assert reply.ok
            assert "state=ready" in reply.text
            assert "cmd_busy=0 cmd_grace=0" in reply.text
            assert "monitor=1 stale_n=0 stack_free_min=1024" in reply.text
            assert firmware.cm5_mode == "ready"
            assert firmware.cm5_sequence >= 1
        finally:
            if presence_task is not None:
                await _cancel(presence_task)
            transport.close()

    run(main())


def test_foreground_command_gets_bounded_presence_bridge(firmware):
    async def main() -> None:
        transport, session = open_link(firmware)
        try:
            await session.login()
            heartbeat = await session.command(
                "cm5 heartbeat 1 1 ready", expect="status", replay=False)
            assert heartbeat.ok

            # The ordinary command enters the same serialized path used by
            # voicefetch and other potentially long CM5 work.  Make the raw
            # heartbeat expire while that command is still in flight.
            firmware.delay_once["uartlink status"] = 0.2
            command_task = asyncio.create_task(session.command(
                "uartlink status", expect="status"))
            await _wait_until(lambda: firmware.cm5_command_in_flight)
            with firmware._lock:
                firmware.cm5_last_seen = time.monotonic() - 20.0
                during = firmware._cm5_snapshot_locked()
            assert during["fresh"] is True
            assert during["command_in_flight"] is True
            assert during["command_grace"] is False

            command_reply = await command_task
            assert command_reply.ok
            await _wait_until(lambda: firmware.cm5_command_grace)

            # CM5 status is deliberately excluded from the bridge, so it can
            # observe the post-reply grace without extending it itself.
            grace = await session.command("cm5 status", expect="status")
            assert "fresh=1" in grace.text
            assert "cmd_busy=0 cmd_grace=1" in grace.text

            with firmware._lock:
                firmware.cm5_command_finished = time.monotonic() - 6.0
            expired = await session.command("cm5 status", expect="status")
            assert "fresh=0" in expired.text
            assert "cmd_busy=0 cmd_grace=0" in expired.text

            # The in-flight bridge has an absolute 75-second cap.  It may
            # still be diagnostically busy after the cap, but it is no longer
            # fresh and finishing it cannot create another grace window.
            await session.command(
                "cm5 heartbeat 1 2 ready", expect="status", replay=False)
            firmware.delay_once["uartlink status"] = 0.2
            capped_task = asyncio.create_task(session.command(
                "uartlink status", expect="status"))
            await _wait_until(lambda: firmware.cm5_command_in_flight)
            with firmware._lock:
                firmware.cm5_last_seen = time.monotonic() - 20.0
                firmware.cm5_command_started = time.monotonic() - 76.0
                capped = firmware._cm5_snapshot_locked()
            assert capped["fresh"] is False
            assert capped["command_in_flight"] is True
            assert (await capped_task).ok
            assert firmware.cm5_command_grace is False

            # A second command cannot chain from grace after the real
            # heartbeat is stale.  Starting it consumes the old grace but is
            # not allowed to mint a new bridge.
            await session.command(
                "cm5 heartbeat 1 3 ready", expect="status", replay=False)
            assert (await session.command(
                "uartlink status", expect="status")).ok
            await _wait_until(lambda: firmware.cm5_command_grace)
            with firmware._lock:
                firmware.cm5_last_seen = time.monotonic() - 20.0
            assert (await session.command(
                "uartlink status", expect="status")).ok
            non_chained = await session.command("cm5 status", expect="status")
            assert "fresh=0" in non_chained.text
            assert "cmd_busy=0 cmd_grace=0" in non_chained.text

            # A failed reply admission never grants grace.
            await session.command(
                "cm5 heartbeat 1 4 ready", expect="status", replay=False)
            with firmware._lock:
                firmware._cm5_command_started_locked()
                firmware.cm5_last_seen = time.monotonic() - 20.0
                firmware._cm5_command_finished_locked(False)
                failed_reply = firmware._cm5_snapshot_locked()
            assert failed_reply["fresh"] is False
            assert failed_reply["command_grace"] is False
        finally:
            transport.close()

    run(main())


def test_liveaudio_maintenance_never_marks_cm5_command_busy(firmware):
    async def main() -> None:
        transport, session = open_link(firmware)
        controller = "c0dec0de00000001"
        try:
            await session.login()
            command = f"liveaudio ready 1 {controller}"
            # Initial acquisition remains ordinary lifecycle work. Do it
            # before the first presence heartbeat so it cannot inherit a
            # command bridge, then publish fresh presence for the renewal.
            initial = await session.command(
                command, expect="status", replay=False)
            assert initial.ok
            heartbeat = await session.command(
                "cm5 heartbeat 1 1 ready", expect="status", replay=False)
            assert heartbeat.ok

            prior_count = firmware.command_log.count(command)
            firmware.delay_once[command] = 0.2
            renewal = asyncio.create_task(session.command(
                command, expect="status", replay=False))
            await _wait_until(
                lambda: firmware.command_log.count(command) > prior_count)
            assert firmware.cm5_command_in_flight is False
            renewed = await renewal
            assert renewed.ok
            assert renewed.text == initial.text
            assert firmware.cm5_command_grace is False

            status = await session.command(
                "liveaudio status", expect="status", replay=False)
            assert status.ok
            assert firmware.cm5_command_in_flight is False
            assert firmware.cm5_command_grace is False
        finally:
            transport.close()

    run(main())


def test_liveaudio_relogin_forces_ready_through_busy_accounted_repair(firmware):
    async def main() -> None:
        transport, session = open_link(firmware)
        controller = "c0dec0de00000001"
        command = f"liveaudio ready 1 {controller}"
        try:
            await session.login()
            assert (await session.command(
                command, expect="status", replay=False)).ok
            assert (await session.command(
                "cm5 heartbeat 1 1 ready", expect="status",
                replay=False)).ok

            assert (await session.command(
                "logout", expect="status", replay=False,
                auth_replay=False)).ok
            await session.login()
            assert (await session.command(
                "cm5 heartbeat 1 2 ready", expect="status",
                replay=False)).ok

            prior_count = firmware.command_log.count(command)
            firmware.delay_once[command] = 0.2
            repair = asyncio.create_task(session.command(
                command, expect="status", replay=False))
            await _wait_until(
                lambda: firmware.command_log.count(command) > prior_count)
            assert firmware.cm5_command_in_flight is True
            assert (await repair).ok
            assert firmware.live_lease_session_epoch == 2
        finally:
            transport.close()

    run(main())


def test_liveaudio_expired_lease_forces_busy_accounted_repair(firmware):
    async def main() -> None:
        transport, session = open_link(firmware)
        controller = "c0dec0de00000001"
        command = f"liveaudio ready 1 {controller}"
        try:
            await session.login()
            assert (await session.command(
                command, expect="status", replay=False)).ok
            assert (await session.command(
                "cm5 heartbeat 1 1 ready", expect="status",
                replay=False)).ok
            with firmware._lock:
                firmware.live_lease_expires_at = time.monotonic() - 0.001

            prior_count = firmware.command_log.count(command)
            firmware.delay_once[command] = 0.2
            repair = asyncio.create_task(session.command(
                command, expect="status", replay=False))
            await _wait_until(
                lambda: firmware.command_log.count(command) > prior_count)
            assert firmware.cm5_command_in_flight is True
            assert (await repair).ok
            assert firmware.live_lease_expires_at > time.monotonic()
        finally:
            transport.close()

    run(main())


def test_liveaudio_reboot_clears_reused_epoch_authority(firmware):
    async def main() -> None:
        transport, session = open_link(firmware)
        controller = "c0dec0de00000001"
        command = f"liveaudio ready 1 {controller}"
        try:
            await session.login()
            assert (await session.command(
                command, expect="status", replay=False)).ok
            assert (await session.command(
                f"liveaudio shadow 1 {controller} on native",
                expect="status", replay=False)).ok
            assert firmware._cm5_bridges_command(command) is False

            firmware.reboot()
            assert firmware.live_controller_id is None
            assert firmware.live_lease_session_epoch == 0
            assert firmware.live_shadow_armed is False
            assert firmware._cm5_bridges_command(command) is True

            # Firmware generations restart at one after a device reset. The
            # reused number must not make this initial acquisition look like a
            # healthy intrinsic renewal of the pre-reset lease.
            await session.login()
            assert firmware.cm5_session_epoch == 1
            assert (await session.command(
                "cm5 heartbeat 1 1 ready", expect="status",
                replay=False)).ok
            prior_count = firmware.command_log.count(command)
            firmware.delay_once[command] = 0.2
            acquisition = asyncio.create_task(session.command(
                command, expect="status", replay=False))
            await _wait_until(
                lambda: firmware.command_log.count(command) > prior_count)
            assert firmware.cm5_command_in_flight is True
            assert (await acquisition).ok
            assert firmware.live_lease_session_epoch == 1
        finally:
            transport.close()

    run(main())


def test_auth_disabled_link_still_requires_named_login_for_heartbeat(firmware):
    async def main() -> None:
        firmware.require_auth = False
        transport, session = open_link(firmware)
        try:
            rejected = await session.command(
                "cm5 heartbeat 1 1 ready", expect="status", replay=False)
            assert not rejected.ok
            assert rejected.text == (
                "Error: cm5 heartbeat requires a named authenticated UART session")

            await session.login()
            accepted = await session.command(
                "cm5 heartbeat 1 2 ready", expect="status", replay=False)
            assert accepted.ok
            assert "session_epoch=1 lease_ms=15000" in accepted.text
        finally:
            transport.close()

    run(main())


def test_auth_disabled_link_still_requires_named_login_for_liveaudio(firmware):
    async def main() -> None:
        firmware.require_auth = False
        transport, session = open_link(firmware)
        controller = "c0dec0de00000001"
        command = f"liveaudio ready 1 {controller}"
        try:
            # Read-only diagnostics retain AuthBypass behavior, but no bypass
            # identity may create live PCM authority with session epoch zero.
            assert (await session.command(
                "liveaudio capabilities", expect="status",
                replay=False, auth_replay=False)).ok
            rejected = await session.command(
                command, expect="status", replay=False, auth_replay=False)
            assert rejected.text == (
                "Error: liveaudio control requires a real logged-in UART session")
            assert firmware.live_controller_id is None
            assert firmware.live_lease_session_epoch == 0

            await session.login()
            accepted = await session.command(
                command, expect="status", replay=False)
            assert accepted.ok
            assert "session_epoch=1" in accepted.text

            await session.command(
                "logout", expect="status", replay=False, auth_replay=False)
            deadline = firmware.live_lease_expires_at
            logged_out = await session.command(
                command, expect="status", replay=False, auth_replay=False)
            assert logged_out.text == rejected.text
            assert firmware.live_lease_session_epoch == 1
            assert firmware.live_lease_expires_at == deadline
        finally:
            transport.close()

    run(main())


def test_liveaudio_mutations_reject_a_lease_from_an_older_login(firmware):
    async def main() -> None:
        transport, session = open_link(firmware)
        controller = "c0dec0de00000001"
        try:
            await session.login()
            assert (await session.command(
                f"liveaudio ready 1 {controller}", expect="status",
                replay=False)).ok
            assert (await session.command(
                f"liveaudio shadow 1 {controller} on native",
                expect="status", replay=False)).ok

            await session.command(
                "logout", expect="status", replay=False, auth_replay=False)
            await session.login()
            stale_shadow = await session.command(
                f"liveaudio shadow 1 {controller} off",
                expect="status", replay=False)
            stale_release = await session.command(
                f"liveaudio release 1 {controller}",
                expect="status", replay=False)
            assert stale_shadow.text == (
                "Error: liveaudio lease does not match controller")
            assert stale_release.text == stale_shadow.text
            assert firmware.live_controller_id == int(controller, 16)
            assert firmware.live_lease_session_epoch == 1
            assert firmware.live_shadow_armed is True

            repaired = await session.command(
                f"liveaudio ready 1 {controller}", expect="status",
                replay=False)
            assert repaired.ok
            assert "session_epoch=2" in repaired.text
            assert firmware.live_lease_session_epoch == 2
            assert firmware.live_shadow_armed is False
        finally:
            transport.close()

    run(main())


def test_cm5_heartbeat_grammar_and_registry_whitespace(firmware):
    async def main() -> None:
        transport, session = open_link(firmware)
        try:
            await session.login()
            usage = ("Error: Usage: cm5 heartbeat 1 <sequence> "
                     "<starting|ready|busy|degraded>")
            malformed = (
                "cm5 heartbeat",
                "cm5 heartbeat 2 1 ready",
                "cm5 heartbeat 1 0 ready",
                "cm5 heartbeat 1 nope ready",
                "cm5 heartbeat 1 4294967296 ready",
                "cm5 heartbeat 1 1 unavailable",
                "cm5 heartbeat 1 1 ready extra",
            )
            for line in malformed:
                reply = await session.command(
                    line, expect="status", replay=False)
                assert reply.text == usage
                assert firmware.cm5_mode is None

            heartbeat = await session.command(
                "CM5\tHEARTBEAT   1  7  READY",
                expect="status", replay=False)
            assert heartbeat.text == (
                "OK: cm5 heartbeat version=1 seq=7 state=ready "
                "session_epoch=1 lease_ms=15000")

            status = await session.command(
                "cm5    status", expect="status")
            assert status.ok and "state=ready" in status.text
            capabilities = await session.command(
                "CM5\tCAPABILITIES", expect="status")
            assert capabilities.text.startswith("OK: cm5-presence-v1 ")
        finally:
            transport.close()

    run(main())


def test_cm5_presence_is_bound_to_exact_named_login(firmware):
    async def main() -> None:
        firmware.require_auth = False
        transport, session = open_link(firmware)
        try:
            await session.login()
            first = await session.command(
                "cm5 heartbeat 1 1 ready", expect="status", replay=False)
            assert "session_epoch=1" in first.text

            await session.command("logout", expect="status", replay=False)
            logged_out = await session.command("cm5 status", expect="status")
            assert "task=running state=ready fresh=0 seen=0 epoch=1" in logged_out.text

            await session.login()
            replacement = await session.command("cm5 status", expect="status")
            assert "task=running state=ready fresh=0 seen=0 epoch=1" in replacement.text

            second = await session.command(
                "cm5 heartbeat 1 2 ready", expect="status", replay=False)
            assert "session_epoch=2" in second.text
            current = await session.command("cm5 status", expect="status")
            assert "fresh=1 seen=1 epoch=2 seq=2" in current.text
        finally:
            transport.close()

    run(main())


def test_guest_uart_session_cannot_publish_cm5_presence():
    async def main() -> None:
        from fake_firmware import FakeFirmware

        firmware = FakeFirmware(role="guest")
        firmware.start()
        transport, session = open_link(firmware)
        try:
            await session.login()
            before = (firmware.cm5_mode, firmware.cm5_sequence,
                      firmware.cm5_last_seen)
            rejected = await session.command(
                "cm5 heartbeat 1 1 ready", expect="status", replay=False)
            assert rejected.text == (
                "Error: Guest accounts are view-only. "
                "Only login/logout are allowed.")
            assert (firmware.cm5_mode, firmware.cm5_sequence,
                    firmware.cm5_last_seen) == before
            status = await session.command("cm5 status", expect="status")
            capabilities = await session.command(
                "cm5 capabilities", expect="status")
            assert status.text == rejected.text
            assert capabilities.text == rejected.text
            malformed = await session.command(
                "cm5 heartbeat 1 0 ready", expect="status", replay=False)
            assert malformed.text == (
                "Error: Usage: cm5 heartbeat 1 <sequence> "
                "<starting|ready|busy|degraded>")
        finally:
            transport.close()
            firmware.stop()

    run(main())


def test_guest_uart_session_cannot_use_liveaudio_namespace():
    async def main() -> None:
        from fake_firmware import FakeFirmware

        firmware = FakeFirmware(role="guest")
        firmware.start()
        transport, session = open_link(firmware)
        denied = (
            "Error: Guest accounts are view-only. "
            "Only local login/logout and whoami are allowed.")
        try:
            await session.login()
            for command in (
                    "liveaudio capabilities",
                    "liveaudio status",
                    "liveaudio ready 1 c0dec0de00000001"):
                reply = await session.command(
                    command, expect="status", replay=False)
                assert reply.text == denied
            assert firmware.live_controller_id is None
            assert firmware.live_lease_session_epoch == 0
        finally:
            transport.close()
            firmware.stop()

    run(main())


def test_cm5_registry_usage_errors_match_firmware(firmware):
    async def main() -> None:
        transport, session = open_link(firmware)
        try:
            await session.login()
            cases = {
                "cm5 status extra": "Error: Usage: cm5 status",
                "cm5 capabilities extra": "Error: Usage: cm5 capabilities",
                "cm5 nonsense": (
                    "Error: Usage: cm5 <status|capabilities> "
                    "(heartbeat is UART control-plane only)"),
                "cm5 heartbeatX": (
                    "Error: Usage: cm5 <status|capabilities> "
                    "(heartbeat is UART control-plane only)"),
            }
            for line, expected in cases.items():
                reply = await session.command(line, expect="status")
                assert reply.text == expected
        finally:
            transport.close()

    run(main())


@pytest.mark.parametrize("role", ["unknown", "", "operator"])
def test_unrecognized_account_role_cannot_publish_cm5_presence(role):
    async def main() -> None:
        from fake_firmware import FakeFirmware

        firmware = FakeFirmware(role=role)
        firmware.start()
        transport, session = open_link(firmware)
        try:
            await session.login()
            rejected = await session.command(
                "cm5 heartbeat 1 1 ready", expect="status", replay=False)
            assert rejected.text == (
                "Error: Guest accounts are view-only. "
                "Only login/logout are allowed.")
            assert firmware.cm5_mode is None
        finally:
            transport.close()
            firmware.stop()

    run(main())
