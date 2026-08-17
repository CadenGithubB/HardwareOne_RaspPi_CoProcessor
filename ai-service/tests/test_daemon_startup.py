"""Daemon-only G2 EvenAI startup configuration regressions."""

from __future__ import annotations

import argparse
import asyncio
import importlib
from contextlib import suppress

import pytest

from conftest import open_link, run
from fake_firmware import TEST_EVENAI_ID

from hw1_ai_service import config as config_mod
from hw1_ai_service import pipeline as pipeline_mod
from hw1_ai_service.jobs import ManualTrigger
from hw1_ai_service.link.session import CommandTimeout, LinkClosed, LoginFailed

main_mod = importlib.import_module("hw1_ai_service.__main__")


class _Reply:
    def __init__(self, ok: bool, text: str = "") -> None:
        self.ok = ok
        self.text = text


class _Session:
    def __init__(self, outcomes=()) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict]] = []
        self.on_event = object()
        self.login_calls = 0

    async def command(self, line: str, **kwargs):
        self.calls.append((line, kwargs))
        outcome = self.outcomes.pop(0) if self.outcomes else _Reply(True, "OK")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def login(self) -> None:
        self.login_calls += 1

    async def pump_events(self) -> None:
        return


def test_startup_submit_uses_only_field_two_and_precise_logging_contract():
    session = _Session([_Reply(True, "OK: sent")])

    assert run(main_mod._submit_g2_stream_speed(session, 40)) is True
    assert session.calls == [(
        "g2aiconfig - 40 -",
        {
            "expect": "status",
            "timeout": main_mod._G2_CONFIG_TIMEOUT_S,
            "replay": True,
        },
    )]


def test_startup_submit_zero_preserves_g2_state_without_a_command():
    session = _Session()
    assert run(main_mod._submit_g2_stream_speed(session, 0)) is False
    assert session.calls == []


@pytest.mark.parametrize("outcome", [
    _Reply(False, "Error: no reachable G2 temple"),
    _Reply(False, "Unknown command: g2aiconfig"),
    CommandTimeout("no reply"),
    LoginFailed("re-login rejected"),
])
def test_startup_submit_failure_is_nonfatal(outcome):
    session = _Session([outcome])
    assert run(main_mod._submit_g2_stream_speed(session, 40)) is False


def test_startup_submit_does_not_swallow_link_closed():
    session = _Session([LinkClosed("gone")])
    with pytest.raises(LinkClosed):
        run(main_mod._submit_g2_stream_speed(session, 40))


def test_daemon_submits_after_callback_install_and_before_pipeline_tasks():
    events: list[str] = []

    class Session(_Session):
        async def command(self, line: str, **kwargs):
            assert self.on_event is not None
            events.append("config")
            return await super().command(line, **kwargs)

        async def pump_events(self) -> None:
            assert events[0] == "config"
            events.append("pump")

    class Pipeline:
        async def daemon(self, _trigger) -> None:
            assert events[0] == "config"
            events.append("pipeline")

    class Power:
        async def run(self) -> None:
            assert events[0] == "config"
            events.append("power")

    session = Session([_Reply(True, "OK")])
    run(main_mod._daemon_supervised(
        Pipeline(), object(), object(), session, Power(), 40))
    assert events[0] == "config"
    assert set(events[1:]) == {"pipeline", "pump", "power"}


def test_link_closed_during_startup_config_reconnects_then_retries(monkeypatch):
    events: list[str] = []

    async def no_sleep(_seconds: float) -> None:
        return

    monkeypatch.setattr(main_mod.asyncio, "sleep", no_sleep)

    class Session(_Session):
        async def command(self, line: str, **kwargs):
            events.append("config")
            return await super().command(line, **kwargs)

        async def login(self) -> None:
            events.append("login")

    class Pipeline:
        async def daemon(self, _trigger) -> None:
            events.append("pipeline")

    class Power:
        async def run(self) -> None:
            return

        def replay_pending_callbacks(self) -> None:
            events.append("replay")

    class Transport:
        def close(self) -> None:
            events.append("close")

        def open(self) -> None:
            events.append("open")

    session = Session([LinkClosed("gone"), _Reply(True, "OK")])
    run(main_mod._daemon_supervised(
        Pipeline(), object(), Transport(), session, Power(), 40))
    assert events == [
        "config", "close", "open", "login", "replay", "config", "pipeline"
    ]


def test_idle_reboot_generation_wakes_supervisor_and_reconnects(monkeypatch):
    events: list[str] = []

    async def no_sleep(_seconds: float) -> None:
        return

    monkeypatch.setattr(main_mod.asyncio, "sleep", no_sleep)

    async def main() -> None:
        never = asyncio.Event()
        restarted = asyncio.Event()

        class Session(_Session):
            def __init__(self) -> None:
                super().__init__()
                self.reboot_generation = 0
                self.reboot_changed = asyncio.Event()

            async def wait_for_reboot_after(self, generation: int) -> None:
                while self.reboot_generation <= generation:
                    self.reboot_changed.clear()
                    if self.reboot_generation > generation:
                        return
                    await self.reboot_changed.wait()

            async def pump_events(self) -> None:
                if self.reboot_generation == 0:
                    events.append("idle_reboot")
                    self.reboot_generation = 1
                    self.reboot_changed.set()
                await never.wait()

            async def login(self) -> None:
                events.append("login")

        class Pipeline:
            calls = 0

            async def daemon(self, _trigger) -> None:
                self.calls += 1
                events.append(f"pipeline_{self.calls}")
                if self.calls == 2:
                    restarted.set()
                await never.wait()

        class Power:
            async def run(self) -> None:
                await never.wait()

            def replay_pending_callbacks(self) -> None:
                events.append("replay")

        class Transport:
            def close(self) -> None:
                events.append("close")

            def open(self) -> None:
                events.append("open")

        task = asyncio.create_task(main_mod._daemon_supervised(
            Pipeline(), object(), Transport(), Session(), Power(), 0))
        try:
            await asyncio.wait_for(restarted.wait(), 0.5)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        assert events[:6] == [
            "pipeline_1", "idle_reboot", "close", "open", "login", "replay"
        ]
        assert "pipeline_2" in events

    run(main())


def test_default_and_validated_auto_speed_policy(tmp_path):
    assert config_mod.Config().deliver.g2_stream_speed == 40
    for value in (0, 40, 80):
        path = tmp_path / f"good-{value}.yaml"
        path.write_text(f"deliver:\n  g2_stream_speed: {value}\n")
        assert config_mod.load(path).deliver.g2_stream_speed == value

    path = tmp_path / "bad.yaml"
    path.write_text("deliver:\n  g2_stream_speed: 41\n")
    with pytest.raises(ValueError, match="g2_stream_speed"):
        config_mod.load(path)


@pytest.mark.parametrize("raw, expected", [
    ("0", 0.0),
    ("0.05", 0.05),
    ("0.10", 0.10),
    ("10", 10.0),
])
def test_cancel_marker_cli_interval_accepts_bounded_finite_values(raw, expected):
    assert main_mod._cancel_marker_interval_arg(raw) == expected


@pytest.mark.parametrize("raw", ["-1", "0.01", "10.1", "nan", "inf", "nope"])
def test_cancel_marker_cli_interval_rejects_unsafe_values(raw):
    with pytest.raises(argparse.ArgumentTypeError):
        main_mod._cancel_marker_interval_arg(raw)


def test_lazy_pipeline_carries_daemon_only_cancel_marker_interval():
    lazy = main_mod._LazyDaemonPipeline(
        _Session(), config_mod.Config(), object(),
        cancel_marker_interval_s=0.10)
    assert lazy._cancel_marker_interval_s == 0.10


def test_cancel_marker_is_daemon_only_and_defaults_off():
    parser = main_mod._build_parser()
    assert parser.parse_args(["daemon"]).evenai_cancel_marker_interval_s == 0.0
    assert parser.parse_args([
        "daemon", "--evenai-cancel-marker-interval-s", "0.10",
    ]).evenai_cancel_marker_interval_s == 0.10
    for command in (["ask"], ["probe"], ["chat", "hello"]):
        with pytest.raises(SystemExit):
            parser.parse_args([
                *command, "--evenai-cancel-marker-interval-s", "0.10",
            ])


def test_run_pipeline_forwards_daemon_cancel_marker(monkeypatch):
    seen = {}

    async def fake_run_daemon(cfg, transport, session, *, live_gate=None,
                              cancel_marker_interval_s=0.0):
        seen.update(cfg=cfg, transport=transport, session=session,
                    live_gate=live_gate, interval=cancel_marker_interval_s)

    monkeypatch.setattr(main_mod, "_run_daemon", fake_run_daemon)
    cfg = config_mod.Config()
    transport, session = object(), object()
    args = argparse.Namespace(
        cmd="daemon", evenai_cancel_marker_interval_s=0.10)
    run(main_mod._run_pipeline(args, cfg, transport, session))
    assert seen == {
        "cfg": cfg, "transport": transport, "session": session,
        "live_gate": None, "interval": 0.10,
    }


def test_lazy_initialization_forwards_cancel_marker_to_voice_pipeline(
        monkeypatch):
    from hw1_ai_service import stt as stt_mod

    seen = {}

    class Pipeline:
        def __init__(self, session, stt_engine, llm_client, cfg, **kwargs):
            seen.update(session=session, stt=stt_engine, llm=llm_client,
                        cfg=cfg, kwargs=kwargs)

    async def fake_make_llm(_cfg):
        return None, None

    stt_engine = object()
    monkeypatch.setattr(stt_mod, "create_engine", lambda *_args: stt_engine)
    monkeypatch.setattr(main_mod, "_make_llm", fake_make_llm)
    monkeypatch.setattr(main_mod, "VoicePipeline", Pipeline)
    session, cfg, power = _Session(), config_mod.Config(), object()
    lazy = main_mod._LazyDaemonPipeline(
        session, cfg, power, cancel_marker_interval_s=0.10)
    run(lazy._initialize())
    assert seen == {
        "session": session,
        "stt": stt_engine,
        "llm": None,
        "cfg": cfg,
        "kwargs": {
            "power_activity": power,
            "live_gate": None,
            "cancel_marker_interval_s": 0.10,
            "cm5_presence": None,
        },
    }


def test_real_session_fake_firmware_records_startup_speed(firmware):
    async def main() -> None:
        transport, session = open_link(firmware)
        try:
            await session.login()
            assert await main_mod._submit_g2_stream_speed(session, 40)
            assert firmware.evenai_stream_speeds == [40]
        finally:
            transport.close()

    run(main())


def test_lazy_pipeline_init_failure_sends_tagged_exit_before_drop():
    async def main() -> None:
        session = _Session()
        source = ManualTrigger()
        exchange = source.submit_evenai(TEST_EVENAI_ID)
        assert exchange is not None
        lazy = main_mod._LazyDaemonPipeline(
            session, config_mod.Config(), object())
        lazy._initialization_failed = True
        task = asyncio.create_task(lazy.daemon(source))
        try:
            deadline = asyncio.get_running_loop().time() + 1
            while not session.calls:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0)
            assert session.calls == [(
                f"g2evenai exitid {TEST_EVENAI_ID}",
                {
                    "expect": "status",
                    "timeout": pipeline_mod._EVENAI_ABORT_TIMEOUT_S,
                    "replay": False,
                },
            )]
            assert TEST_EVENAI_ID not in source._evenai
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await lazy.close()

    run(main())
