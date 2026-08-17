"""Finite host-fan UART bridge and local socket protocol tests."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress

import pytest

import hw1_ai_service.fan as fan_mod
from conftest import open_link, run
from hw1_ai_service.config import FanConfig, load
from hw1_ai_service.fan import (
    FanAction,
    FanController,
    FanHealth,
    FanMode,
    FanProtocolError,
    FanServiceClient,
    FanServiceError,
    FanServiceResult,
    FanStatus,
    parse_fan_event,
)
from hw1_ai_service.link.session import CommandTimeout, LinkClosed, Reply
from hw1_ai_service.jobs import ManualTrigger, route_link_event


REQUEST_ID = "deadbeef00000001"


def test_config_allows_a_fan_only_control_daemon(tmp_path):
    path = tmp_path / "fan-only.yaml"
    path.write_text(
        "stt:\n  engine: none\n"
        "llm:\n  engine: none\n"
        "fan:\n  enabled: true\n",
        encoding="utf-8",
    )
    cfg = load(path)
    assert cfg.fan.enabled is True
    assert cfg.power.enabled is False


@pytest.mark.parametrize(
    ("fan_yaml", "error"),
    [
        ("socket_path: relative.sock", "fan.socket_path"),
        ("socket_path: /tmp/fake-fan.sock", "fan.socket_path"),
        ("socket_path: /run/hw1-fan-controller/control.sock/", "fan.socket_path"),
        ("socket_timeout_s: 0", "fan.socket_timeout_s"),
        ("socket_timeout_s: .nan", "fan.socket_timeout_s"),
        ("socket_timeout_s: 31", "fan.socket_timeout_s"),
        ("socket_timeout_s: true", "socket_timeout_s"),
        ("uart_timeout_s: 0", "fan.uart_timeout_s"),
        ("uart_timeout_s: .inf", "fan.uart_timeout_s"),
        ("uart_timeout_s: 61", "fan.uart_timeout_s"),
        ("uart_timeout_s: false", "uart_timeout_s"),
        ("event_queue_size: 0", "fan.event_queue_size"),
        (
            "event_queue_size: 8\n  request_cache_size: 7",
            "fan.request_cache_size",
        ),
    ],
)
def test_config_rejects_invalid_fan_control_bounds(tmp_path, fan_yaml, error):
    path = tmp_path / "invalid-fan.yaml"
    path.write_text(
        "fan:\n  enabled: true\n  " + fan_yaml + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=error):
        load(path)


def _cfg(**changes) -> FanConfig:
    cfg = FanConfig(
        enabled=True,
        socket_timeout_s=0.5,
        uart_timeout_s=0.5,
        event_queue_size=8,
        request_cache_size=16,
    )
    for key, value in changes.items():
        setattr(cfg, key, value)
    return cfg


class FakeSession:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.command_options: list[dict] = []
        self.hook = None

    async def command(self, line: str, **kwargs) -> Reply:
        self.commands.append(line)
        self.command_options.append(kwargs)
        if self.hook is not None:
            result = await self.hook(line)
            if result is not None:
                return result
        return Reply(["OK"])


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[FanAction, FanMode | None]] = []
        self.hook = None

    async def request(self, action: FanAction, *, mode=None) -> FanServiceResult:
        self.calls.append((action, mode))
        if self.hook is not None:
            result = await self.hook(action, mode)
            if result is not None:
                return result
        selected = mode or FanMode.AUTO
        pwm = 255 if selected is FanMode.MAX else 75
        return FanServiceResult(True, "ok", FanStatus(
            selected, selected, 52500, pwm, pwm, 2100, FanHealth.OK))


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition not reached before timeout")
        await asyncio.sleep(0.005)


async def _stop(worker, controller) -> None:
    worker.cancel()
    with suppress(asyncio.CancelledError):
        await worker
    await controller.close()


@pytest.mark.parametrize(
    ("payload", "action", "mode"),
    [
        (f"cm5_fan_status 1 {REQUEST_ID}".encode(), FanAction.STATUS, None),
        (f"cm5_fan_mode_auto 1 {REQUEST_ID}".encode(),
         FanAction.MODE, FanMode.AUTO),
        (f"cm5_fan_mode_quiet 1 {REQUEST_ID}".encode(),
         FanAction.MODE, FanMode.QUIET),
        (f"cm5_fan_mode_max 1 {REQUEST_ID}".encode(),
         FanAction.MODE, FanMode.MAX),
    ],
)
def test_parse_fan_event_accepts_only_the_finite_v1_grammar(
        payload, action, mode):
    request = parse_fan_event(payload)
    assert request is not None
    assert (request.request_id, request.action, request.mode) == (
        REQUEST_ID, action, mode)
    assert parse_fan_event(b"evenai_wake deadbeef00000001") is None


@pytest.mark.parametrize("payload", [
    b"cm5_fan_status 2 deadbeef00000001",
    b"cm5_fan_status 1 deadbeef00000000",
    b"cm5_fan_status 1 deadbeef00000001 extra",
    b"cm5_fan_mode_100 1 deadbeef00000001",
    b"cm5_fan_mode_quiet 1 '$(reboot)'",
    b"cm5_fan_mode_quiet 1 \xff",
    b" cm5_fan_status 1 deadbeef00000001",
    b"cm5_fan_status 1 deadbeef00000001 ",
    b"cm5_fan_status  1 deadbeef00000001",
    b"cm5_fan_status\t1 deadbeef00000001",
    b"cm5_fan_status 1\tdeadbeef00000001",
    b"cm5_fan_status 1 deadbeef00000001\r",
    b"cm5_fan_status 1 deadbeef00000001\n",
])
def test_parse_fan_event_rejects_malformed_or_injectable_input(payload):
    with pytest.raises(FanProtocolError):
        parse_fan_event(payload)


def test_socket_client_uses_fixed_command_and_parses_bounded_status(monkeypatch):
    async def main():
        seen = []
        reply = (json.dumps({
                "ok": True,
                "code": "ok",
                "requested_mode": "quiet",
                "effective_mode": "quiet",
                "temp_mc": 51000,
                "target_pwm": 75,
                "pwm": 255,
                "rpm": 4200,
                "health": "boosting",
            }) + "\n").encode()

        class Reader:
            async def readline(self):
                return reply

        class Writer:
            def write(self, data):
                seen.append(data)

            async def drain(self):
                pass

            def close(self):
                pass

            async def wait_closed(self):
                pass

        async def open_connection(path, *, limit):
            assert path == "/run/hw1-fan-controller/control.sock"
            assert limit == 4096
            return Reader(), Writer()

        monkeypatch.setattr(asyncio, "open_unix_connection", open_connection)
        client = FanServiceClient(
            "/run/hw1-fan-controller/control.sock", timeout_s=1)
        result = await client.request(FanAction.MODE, mode=FanMode.QUIET)
        assert result.ok and result.status is not None
        assert result.status.health is FanHealth.BOOSTING
        assert result.status.pwm == 255
        assert seen == [b"mode quiet\n"]

    run(main())


@pytest.mark.parametrize("path", [
    "/run/test-fan.sock",
    "/run/hw1-fan-controller/control.sock/",
])
def test_socket_client_rejects_noncanonical_socket_path(path):
    with pytest.raises(ValueError, match="socket path must be"):
        FanServiceClient(path)


def test_socket_client_rejects_unbounded_or_unknown_fields(monkeypatch):
    async def main():
        reply = (json.dumps({
                "ok": True, "code": "ok",
                "requested_mode": "quiet", "effective_mode": "quiet",
                "temp_mc": 51000, "target_pwm": 999, "pwm": 75,
                "rpm": 2000, "health": "trust_me",
            }) + "\n").encode()

        class Reader:
            async def readline(self):
                return reply

        class Writer:
            def write(self, data):
                pass

            async def drain(self):
                pass

            def close(self):
                pass

            async def wait_closed(self):
                pass

        async def open_connection(path, *, limit):
            return Reader(), Writer()

        monkeypatch.setattr(asyncio, "open_unix_connection", open_connection)
        with pytest.raises(FanServiceError):
            await FanServiceClient(
                "/run/hw1-fan-controller/control.sock", timeout_s=1
            ).request(FanAction.STATUS)

    run(main())


def test_socket_client_cleanup_is_bounded_when_wait_closed_stalls(monkeypatch):
    async def main():
        reply = (json.dumps({
            "ok": True,
            "code": "ok",
            "requested_mode": "auto",
            "effective_mode": "auto",
            "temp_mc": 45000,
            "target_pwm": 0,
            "pwm": 0,
            "rpm": 0,
            "health": "ok",
        }) + "\n").encode()

        class Reader:
            async def readline(self):
                return reply

        class Writer:
            def write(self, _data):
                pass

            async def drain(self):
                pass

            def close(self):
                pass

            async def wait_closed(self):
                await asyncio.Event().wait()

        async def open_connection(_path, *, limit):
            assert limit == 4096
            return Reader(), Writer()

        monkeypatch.setattr(asyncio, "open_unix_connection", open_connection)
        async with asyncio.timeout(0.2):
            result = await FanServiceClient(
                "/run/hw1-fan-controller/control.sock", timeout_s=0.01
            ).request(FanAction.STATUS)
        assert result.ok

    run(main())


def test_controller_acks_then_applies_and_reports_measured_rpm():
    async def main():
        session, service = FakeSession(), FakeService()
        controller = FanController(session, _cfg(), service=service)
        worker = asyncio.create_task(controller.run())
        try:
            assert controller.submit_event(
                f"cm5_fan_mode_quiet 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: (
                f"cm5 fan ack 1 {REQUEST_ID} applied" in session.commands))
            assert service.calls == [(FanAction.MODE, FanMode.QUIET)]
            assert session.commands[0] == (
                f"cm5 fan ack 1 {REQUEST_ID} accepted")
            assert session.commands[1] == (
                f"cm5 fan report 1 {REQUEST_ID} quiet quiet "
                "52500 75 75 2100 ok")
            assert session.commands[2] == (
                f"cm5 fan ack 1 {REQUEST_ID} applied")
            assert all(options["replay"] is False
                       for options in session.command_options)
            assert all(options["auth_replay"] is False
                       for options in session.command_options)
        finally:
            await _stop(worker, controller)

    run(main())


def test_controller_treats_mismatched_root_requested_mode_as_failed():
    async def main():
        session, service = FakeSession(), FakeService()

        async def mismatch(_action, _mode):
            return FanServiceResult(True, "ok", FanStatus(
                FanMode.AUTO, FanMode.AUTO, 51000, 75, 75, 2100,
                FanHealth.OK))

        service.hook = mismatch
        controller = FanController(session, _cfg(), service=service)
        worker = asyncio.create_task(controller.run())
        try:
            controller.submit_event(
                f"cm5_fan_mode_quiet 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: (
                f"cm5 fan ack 1 {REQUEST_ID} failed" in session.commands))
            assert not any(line.startswith("cm5 fan report")
                           for line in session.commands)
            assert f"cm5 fan ack 1 {REQUEST_ID} applied" not in session.commands
        finally:
            await _stop(worker, controller)

    run(main())


def test_controller_retries_one_lost_idempotent_mode_reply():
    async def main():
        session, service = FakeSession(), FakeService()
        attempts = 0

        async def lose_first_reply(_action, _mode):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise FanServiceError("reply lost after possible apply")
            return None

        service.hook = lose_first_reply
        controller = FanController(session, _cfg(), service=service)
        worker = asyncio.create_task(controller.run())
        try:
            controller.submit_event(
                f"cm5_fan_mode_quiet 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: (
                f"cm5 fan ack 1 {REQUEST_ID} applied" in session.commands))
            assert attempts == 2
            assert service.calls == [
                (FanAction.MODE, FanMode.QUIET),
                (FanAction.MODE, FanMode.QUIET),
            ]
        finally:
            await _stop(worker, controller)

    run(main())


def test_controller_fails_after_two_unconfirmed_mode_replies():
    async def main():
        session, service = FakeSession(), FakeService()

        async def lose_reply(_action, _mode):
            raise FanServiceError("reply lost after possible apply")

        service.hook = lose_reply
        controller = FanController(session, _cfg(), service=service)
        worker = asyncio.create_task(controller.run())
        try:
            controller.submit_event(
                f"cm5_fan_mode_quiet 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: (
                f"cm5 fan ack 1 {REQUEST_ID} failed" in session.commands))
            assert service.calls == [
                (FanAction.MODE, FanMode.QUIET),
                (FanAction.MODE, FanMode.QUIET),
            ]
            assert f"cm5 fan ack 1 {REQUEST_ID} applied" not in session.commands
        finally:
            await _stop(worker, controller)

    run(main())


def test_epoch_bound_callback_timeout_uses_outer_finite_retry(monkeypatch):
    async def main():
        monkeypatch.setattr(fan_mod, "_CALLBACK_RETRY_DELAYS_S", (0.01,))
        session, service = FakeSession(), FakeService()
        attempts = 0

        async def timeout_once(line):
            nonlocal attempts
            if line.endswith(" accepted"):
                attempts += 1
                if attempts == 1:
                    raise CommandTimeout("injected")
            return None

        session.hook = timeout_once
        controller = FanController(session, _cfg(), service=service)
        worker = asyncio.create_task(controller.run())
        try:
            controller.submit_event(
                f"cm5_fan_mode_max 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: attempts == 2)
            assert service.calls == [(FanAction.MODE, FanMode.MAX)]
            assert all(options["replay"] is False
                       for options in session.command_options)
            assert all(options["auth_replay"] is False
                       for options in session.command_options)
        finally:
            await _stop(worker, controller)

    run(main())


def test_link_reset_cancels_retry_for_rejected_detached_event(monkeypatch):
    async def main():
        monkeypatch.setattr(fan_mod, "_CALLBACK_RETRY_DELAYS_S", (0.03,))
        session, service = FakeSession(), FakeService()
        attempts = 0

        async def reject_failed(line):
            nonlocal attempts
            if line == f"cm5 fan ack 1 {REQUEST_ID} failed":
                attempts += 1
                return Reply(["Error: transient"])
            return None

        session.hook = reject_failed
        controller = FanController(session, _cfg(), service=service)
        worker = asyncio.create_task(controller.run())
        try:
            assert controller.submit_event(
                f"cm5_fan_mode_100 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: attempts == 1)
            controller.link_reset()
            await asyncio.sleep(0.08)
            assert attempts == 1
            assert service.calls == []
        finally:
            await _stop(worker, controller)

    run(main())


def test_exhausted_callback_records_do_not_exceed_cache_bound(monkeypatch):
    async def main():
        monkeypatch.setattr(fan_mod, "_CALLBACK_RETRY_DELAYS_S", (0.001,))
        session, service = FakeSession(), FakeService()

        async def reject_every_callback(_line):
            return Reply(["Error: unavailable"])

        session.hook = reject_every_callback
        controller = FanController(
            session,
            _cfg(event_queue_size=4, request_cache_size=4),
            service=service,
        )
        worker = asyncio.create_task(controller.run())
        try:
            for counter in range(1, 25):
                request_id = f"deadbeef{counter:08x}"
                controller.submit_event(
                    f"cm5_fan_status 1 {request_id}".encode())
                await asyncio.sleep(0.004)
                assert len(controller._records) <= 4
            await asyncio.sleep(0.02)
            assert len(controller._records) <= 4
            assert service.calls == []
        finally:
            await _stop(worker, controller)

    run(main())


def test_record_cache_evicts_oldest_clean_terminal_record():
    async def main():
        controller = FanController(
            FakeSession(),
            _cfg(event_queue_size=2, request_cache_size=2),
            service=FakeService(),
        )
        try:
            first = parse_fan_event(b"cm5_fan_status 1 deadbeef00000001")
            second = parse_fan_event(b"cm5_fan_status 1 deadbeef00000002")
            assert first is not None and second is not None
            controller._records[first.request_id] = fan_mod._Record(
                first, stage=fan_mod._RecordStage.TERMINAL)
            controller._records[second.request_id] = fan_mod._Record(
                second, stage=fan_mod._RecordStage.TERMINAL)

            assert controller.submit_event(
                b"cm5_fan_status 1 deadbeef00000003")
            assert tuple(controller._records) == (
                "deadbeef00000002", "deadbeef00000003")
            assert controller._queue.qsize() == 1
        finally:
            await controller.close()

    run(main())


def test_record_cache_refuses_new_event_when_every_record_is_pending():
    async def main():
        controller = FanController(
            FakeSession(),
            _cfg(event_queue_size=2, request_cache_size=2),
            service=FakeService(),
        )
        try:
            first = parse_fan_event(b"cm5_fan_status 1 deadbeef00000001")
            second = parse_fan_event(b"cm5_fan_status 1 deadbeef00000002")
            assert first is not None and second is not None
            controller._records[first.request_id] = fan_mod._Record(first)
            controller._records[second.request_id] = fan_mod._Record(second)

            assert controller.submit_event(
                b"cm5_fan_status 1 deadbeef00000003")
            assert tuple(controller._records) == (
                "deadbeef00000001", "deadbeef00000002")
            assert controller._queue.empty()
        finally:
            await controller.close()

    run(main())


def test_controller_deduplicates_request_id_without_reapplying_mode():
    async def main():
        session, service = FakeSession(), FakeService()
        controller = FanController(session, _cfg(), service=service)
        worker = asyncio.create_task(controller.run())
        payload = f"cm5_fan_mode_max 1 {REQUEST_ID}".encode()
        try:
            controller.submit_event(payload)
            await _wait_for(lambda: (
                f"cm5 fan ack 1 {REQUEST_ID} applied" in session.commands))
            controller.submit_event(payload)
            await _wait_for(lambda: session.commands.count(
                f"cm5 fan ack 1 {REQUEST_ID} applied") >= 2)
            assert service.calls == [(FanAction.MODE, FanMode.MAX)]
        finally:
            await _stop(worker, controller)

    run(main())


def test_controller_never_calls_root_service_until_accepted_ack_is_confirmed(
        monkeypatch):
    async def main():
        monkeypatch.setattr(fan_mod, "_CALLBACK_RETRY_DELAYS_S", (0.01, 0.02))
        session, service = FakeSession(), FakeService()
        attempts = 0

        async def hook(line):
            nonlocal attempts
            if line.endswith(" accepted"):
                attempts += 1
                if attempts == 1:
                    return Reply(["Error: transient"])
            return None

        session.hook = hook
        controller = FanController(session, _cfg(), service=service)
        worker = asyncio.create_task(controller.run())
        try:
            controller.submit_event(f"cm5_fan_mode_max 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: attempts == 1)
            assert service.calls == []
            await _wait_for(lambda: bool(service.calls))
            assert attempts == 2
        finally:
            await _stop(worker, controller)

    run(main())


def test_controller_discards_callbacks_from_old_uart_epoch():
    async def main():
        session, service = FakeSession(), FakeService()
        controller = FanController(session, _cfg(), service=service)
        worker = asyncio.create_task(controller.run())
        close_once = True

        async def hook(line):
            nonlocal close_once
            if line.startswith("cm5 fan report") and close_once:
                close_once = False
                raise LinkClosed("test disconnect")
            return None

        session.hook = hook
        try:
            controller.submit_event(f"cm5_fan_status 1 {REQUEST_ID}".encode())
            with pytest.raises(LinkClosed):
                await worker
            assert service.calls == [(FanAction.STATUS, None)]
            command_count = len(session.commands)
            session.hook = None
            controller.link_reset()
            worker = asyncio.create_task(controller.run())
            await asyncio.sleep(0.03)
            assert len(session.commands) == command_count
            assert f"cm5 fan ack 1 {REQUEST_ID} applied" not in session.commands

            next_id = "deadbeef00000002"
            controller.submit_event(f"cm5_fan_status 1 {next_id}".encode())
            await _wait_for(lambda: (
                f"cm5 fan ack 1 {next_id} applied" in session.commands))
            assert service.calls == [
                (FanAction.STATUS, None),
                (FanAction.STATUS, None),
            ]
        finally:
            if not worker.done():
                await _stop(worker, controller)
            else:
                await controller.close()

    run(main())


def test_disabled_bridge_returns_failed_without_touching_socket():
    async def main():
        session, service = FakeSession(), FakeService()
        controller = FanController(
            session, _cfg(enabled=False), service=service)
        worker = asyncio.create_task(controller.run())
        try:
            controller.submit_event(f"cm5_fan_status 1 {REQUEST_ID}".encode())
            await _wait_for(lambda: (
                f"cm5 fan ack 1 {REQUEST_ID} failed" in session.commands))
            assert service.calls == []
        finally:
            await _stop(worker, controller)

    run(main())


def test_fan_evt_end_to_end_over_session_and_fake_firmware(firmware):
    async def main():
        transport, session = open_link(firmware)
        service = FakeService()
        controller = FanController(session, _cfg(), service=service)
        trigger = ManualTrigger()
        tasks = []
        try:
            await session.login()
            session.on_event = lambda payload: route_link_event(
                payload, trigger, session, fan=controller)
            tasks = [asyncio.create_task(controller.run()),
                     asyncio.create_task(session.pump_events())]
            firmware.push_event(f"cm5_fan_mode_max 1 {REQUEST_ID}")
            await _wait_for(lambda: (REQUEST_ID, "applied")
                            in firmware.cm5_fan_acks)
            assert firmware.cm5_fan_acks[:2] == [
                (REQUEST_ID, "accepted"), (REQUEST_ID, "applied")]
            assert firmware.cm5_fan_reports == [
                (REQUEST_ID, "max", "max", 52500, 255, 255, 2100, "ok")]
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            await controller.close()
            transport.close()

    run(main())
