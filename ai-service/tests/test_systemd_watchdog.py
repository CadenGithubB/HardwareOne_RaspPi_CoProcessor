"""Systemd watchdog integration without requiring a running systemd."""

from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path
import socket
import tempfile

import pytest

from conftest import run

from hw1_ai_service.systemd_watchdog import (
    SystemdWatchdog,
    _notification_address,
)


main_mod = importlib.import_module("hw1_ai_service.__main__")


def test_watchdog_sends_immediate_and_periodic_keepalives(monkeypatch):
    sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    sender.setblocking(False)
    receiver.setblocking(False)
    monkeypatch.setenv("NOTIFY_SOCKET", "/unused-in-injected-socket-test")
    monkeypatch.setenv("WATCHDOG_USEC", "20000")  # 10 ms half interval
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))

    async def scenario() -> None:
        watchdog = SystemdWatchdog.from_environment()
        assert watchdog.enabled
        assert "NOTIFY_SOCKET" not in os.environ
        assert "WATCHDOG_USEC" not in os.environ
        assert "WATCHDOG_PID" not in os.environ

        monkeypatch.setattr(watchdog, "_open_socket", lambda: sender)
        await watchdog.start()
        loop = asyncio.get_running_loop()
        first = await asyncio.wait_for(loop.sock_recv(receiver, 64), 0.5)
        second = await asyncio.wait_for(loop.sock_recv(receiver, 64), 0.5)
        assert first == b"WATCHDOG=1"
        assert second == b"WATCHDOG=1"
        await watchdog.close()
        assert watchdog._task is None

    try:
        run(scenario())
    finally:
        receiver.close()
        sender.close()


def test_watchdog_connects_to_a_real_pathname_socket(monkeypatch):
    temporary = tempfile.TemporaryDirectory(prefix="hw1-wd-", dir="/tmp")
    path = str(Path(temporary.name) / "notify.sock")
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        try:
            receiver.bind(path)
        except PermissionError:
            pytest.skip("sandbox forbids binding a pathname Unix socket")
        receiver.setblocking(False)
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        monkeypatch.setenv("WATCHDOG_USEC", "60000000")
        monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))

        async def scenario() -> None:
            watchdog = SystemdWatchdog.from_environment()
            await watchdog.start()
            message = await asyncio.wait_for(
                asyncio.get_running_loop().sock_recv(receiver, 64), 0.5)
            assert message == b"WATCHDOG=1"
            await watchdog.close()

        run(scenario())
    finally:
        receiver.close()
        temporary.cleanup()


@pytest.mark.parametrize("environment", [
    {},
    {"NOTIFY_SOCKET": "/tmp/notify"},
    {"NOTIFY_SOCKET": "/tmp/notify", "WATCHDOG_USEC": "0"},
    {"NOTIFY_SOCKET": "relative", "WATCHDOG_USEC": "1000"},
    {"NOTIFY_SOCKET": "/tmp/notify", "WATCHDOG_USEC": "not-a-number"},
])
def test_missing_or_malformed_watchdog_environment_is_a_noop(
        monkeypatch, environment):
    for name in ("NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    watchdog = SystemdWatchdog.from_environment()
    assert not watchdog.enabled
    run(watchdog.start())
    run(watchdog.close())


def test_watchdog_pid_must_match_the_service_process(monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/notify")
    monkeypatch.setenv("WATCHDOG_USEC", "60000000")
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid() + 1))
    assert not SystemdWatchdog.from_environment().enabled


def test_notification_address_supports_systemd_abstract_spelling():
    assert _notification_address("/run/user/1000/systemd/notify") == \
        "/run/user/1000/systemd/notify"
    assert _notification_address("@abc") == "\0abc"
    with pytest.raises(ValueError):
        _notification_address("relative")


def test_missing_notification_socket_is_nonfatal(monkeypatch, tmp_path):
    missing = tmp_path / "missing.sock"
    monkeypatch.setenv("NOTIFY_SOCKET", str(missing))
    monkeypatch.setenv("WATCHDOG_USEC", "10000")
    watchdog = SystemdWatchdog.from_environment()
    assert watchdog.enabled
    run(watchdog.start())
    run(watchdog.close())


def test_main_starts_watchdog_before_credentials_and_always_closes(
        monkeypatch):
    events: list[str] = []

    class Watchdog:
        @classmethod
        def from_environment(cls):
            events.append("create")
            return cls()

        async def start(self) -> None:
            events.append("start")

        async def close(self) -> None:
            events.append("close")

    def fail_credentials(_path):
        events.append("credentials")
        raise RuntimeError("test failure")

    monkeypatch.setattr(main_mod, "SystemdWatchdog", Watchdog)
    monkeypatch.setattr(main_mod.config_mod, "read_credentials", fail_credentials)
    cfg = main_mod.config_mod.Config()
    args = type("Args", (), {"cmd": "daemon"})()

    with pytest.raises(RuntimeError, match="test failure"):
        run(main_mod._run(args, cfg))
    assert events == ["create", "start", "credentials", "close"]


def test_service_unit_enables_main_process_watchdog():
    unit = (Path(__file__).resolve().parent.parent /
            "systemd" / "hw1-ai-service.service").read_text()
    assert "Type=exec" in unit
    assert "Restart=always" in unit
    assert "NotifyAccess=main" in unit
    assert "WatchdogSec=60s" in unit
    assert "WatchdogSignal=SIGKILL" in unit
    assert "KillMode=control-group" in unit
    assert "After=default.target" not in unit
    assert "ExecStart=%h/hw1ai/bin/hw1-ai-service" in unit
    assert "/home/" not in unit


def test_deploy_targets_the_remote_account_home():
    deploy = (Path(__file__).resolve().parents[2] / "deploy_cm5.sh").read_text()
    assert 'CM5_HOST="${CM5_HOST:-xiaocm5}"' in deploy
    assert 'CM5_USER="${CM5_USER:-cm5}"' in deploy
    assert 'DEST="${CM5_SSH}:hw1-ai-service/"' in deploy
    assert '"--verify-only"' in deploy
    assert '_SYSTEMD_INVOCATION_ID="$invocation"' in deploy
    assert 'ControlSocket=responsive' in deploy
    assert "caden" not in deploy
    assert "/home/" not in deploy
