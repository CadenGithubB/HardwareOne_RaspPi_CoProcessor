from __future__ import annotations

import asyncio

import pytest

from conftest import open_link, run
from hw1_ai_service.link.session import (
    CommandCancelled,
    LinkClosed,
    LoginFailed,
    Session,
    _firmware_cli_token,
)
from hw1_ai_service.link.transport import LinkEvent


def test_login_and_status(firmware):
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            rep = await session.command("uartlink status", expect="auto", timeout=10)
            assert rep.ok
            assert "UART link: running" in rep.text
        finally:
            transport.close()
    run(main())


def test_firmware_cli_token_quotes_whitespace_and_rejects_unrepresentable():
    assert _firmware_cli_token("simple") == "simple"
    assert _firmware_cli_token("two words") == '"two words"'
    with pytest.raises(ValueError):
        _firmware_cli_token('two "words"')
    for forbidden in ("line\nfeed", "line\rreturn", "nul\x00byte"):
        with pytest.raises(ValueError):
            _firmware_cli_token(forbidden)
    with pytest.raises(ValueError):
        _firmware_cli_token('"leading-quote')


def test_login_with_spaced_password_uses_firmware_quoting(firmware):
    async def main():
        firmware.password = "two words"
        transport, _ = open_link(firmware)
        session = Session(transport, firmware.user, firmware.password)
        try:
            await session.login()
            assert firmware.authed_user == firmware.user
            assert firmware.command_log[-1] == (
                f'login {firmware.user} "two words"')
        finally:
            transport.close()

    run(main())


def test_wrong_password_raises(firmware):
    from hw1_ai_service.link.session import LoginFailed, Session

    async def main():
        transport, good = open_link(firmware)
        try:
            bad = Session(transport, firmware.user, "nope")
            try:
                await bad.login()
                raise AssertionError("login should have failed")
            except LoginFailed:
                pass
        finally:
            transport.close()
    run(main())


def test_relogin_after_reboot(firmware):
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            firmware.reboot()   # drops auth, sprays garbage
            # Next command: firmware nags "authentication required" (or stays
            # silent inside the nag window -> timeout path). Session must
            # re-login and replay transparently either way.
            rep = await session.command("uartlink status", expect="auto", timeout=10)
            assert rep.ok
            assert session.reboot_suspected  # garbage burst was noticed
        finally:
            transport.close()
    run(main())


def test_reboot_hint_is_sticky_generation_and_wakes_listeners():
    class TransportStub:
        def __init__(self) -> None:
            self.rx = asyncio.Queue()

    async def main() -> None:
        session = Session(TransportStub(), "user", "password")
        seen: list[int] = []
        session.add_reboot_listener(
            lambda: seen.append(session.reboot_generation))
        token = session.reboot_generation
        waiter = asyncio.create_task(session.wait_for_reboot_after(token))

        session._note_stray(LinkEvent("garbage"))
        await asyncio.wait_for(waiter, 0.1)
        assert session.reboot_suspected
        assert session.reboot_generation == token + 1
        assert seen == [token + 1]

        # A burst is one episode, not repeated listener/link-reset churn.
        session._note_stray(LinkEvent("garbage"))
        assert session.reboot_generation == token + 1
        assert seen == [token + 1]

        session.clear_reboot_flag()
        next_waiter = asyncio.create_task(
            session.wait_for_reboot_after(token + 1))
        await asyncio.sleep(0)
        assert not next_waiter.done()  # retained generation is not re-fired
        session._note_stray(LinkEvent("garbage"))
        await asyncio.wait_for(next_waiter, 0.1)
        assert session.reboot_generation == token + 2
        assert seen == [token + 1, token + 2]

    run(main())


def test_auth_required_marks_reboot_before_relogin_replay():
    class TransportStub:
        def __init__(self) -> None:
            self.rx = asyncio.Queue()
            self.writes: list[str] = []

        def write_line(self, line: str) -> None:
            self.writes.append(line)
            self.rx.put_nowait(LinkEvent(
                "line",
                "Error: authentication required. Use: login <username> "
                "<password>"))

    async def main() -> None:
        transport = TransportStub()
        session = Session(transport, "user", "password")
        fenced = False

        def fence() -> None:
            nonlocal fenced
            fenced = True

        session.add_reboot_listener(fence)
        with pytest.raises(CommandCancelled):
            await session.command(
                "cm5 heartbeat 1 7 ready", expect="status", replay=False,
                cancel_guard=lambda: fenced)

        assert session.reboot_suspected
        assert transport.writes == ["cm5 heartbeat 1 7 ready"]
        assert not any(line.startswith("login ") for line in transport.writes)

    run(main())


def test_epoch_bound_command_never_replays_after_authentication_loss():
    class TransportStub:
        def __init__(self) -> None:
            self.rx = asyncio.Queue()
            self.writes: list[str] = []

        def write_line(self, line: str) -> None:
            self.writes.append(line)
            self.rx.put_nowait(LinkEvent(
                "line",
                "Error: authentication required. Use: login <username> "
                "<password>"))

    async def main() -> None:
        transport = TransportStub()
        session = Session(transport, "user", "password")

        with pytest.raises(LinkClosed, match="epoch-bound"):
            await session.command(
                "cm5 fan ack 1 deadbeef00000001 accepted",
                expect="status", replay=False, auth_replay=False)

        assert session.reboot_suspected
        assert transport.writes == [
            "cm5 fan ack 1 deadbeef00000001 accepted"]
        assert not any(line.startswith("login ") for line in transport.writes)

    run(main())


def test_silent_unauth_drop_recovers(firmware):
    """Commands sent inside the 2s nag window get NO reply — the timeout
    path must re-login and replay."""
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            firmware.authed_user = None      # simulate idle logout, no garbage
            # Burn the nag: this command gets the one rate-limited nag line.
            rep = await session.command("uartlink status", expect="auto", timeout=10)
            assert rep.ok                    # auto re-login handled it
        finally:
            transport.close()
    run(main())


def test_command_line_cap_enforced(firmware):
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            try:
                await session.command("oledtext " + "x" * 3000, expect="status")
                raise AssertionError("oversize line should be refused client-side")
            except ValueError as exc:
                assert "2047" in str(exc)
        finally:
            transport.close()
    run(main())
