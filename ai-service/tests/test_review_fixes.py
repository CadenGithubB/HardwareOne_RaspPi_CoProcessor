"""Regression tests for the adversarial-review findings — each one pins a
behavior that passed on the old (divergent) fake but would have failed on
real hardware."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from conftest import open_link, run

from hw1_ai_service.config import Config, DeliverConfig, load as load_config
from hw1_ai_service.deliver import chunk_text, deliver
from hw1_ai_service.link.session import CommandTimeout


def test_uppercase_error_classified_fast(firmware):
    """Bare/uppercase ERROR replies terminate status collection immediately
    (the CRITICAL classifier finding — was a 2x-timeout hang)."""
    firmware.mic_disabled = True

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            t0 = time.monotonic()
            rep = await session.command("openmic", expect="status", timeout=10)
            assert time.monotonic() - t0 < 2.0, "ERROR reply should be instant"
            assert not rep.ok
            assert rep.text.startswith("ERROR")
        finally:
            transport.close()
    run(main())


def test_unknown_command_fails_fast(firmware):
    """The two-line unprefixed unknown-command reply is a terminal error,
    not a pair of strays followed by a timeout."""
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            t0 = time.monotonic()
            rep = await session.command("bogus", expect="status", timeout=10)
            assert time.monotonic() - t0 < 2.0
            assert not rep.ok
            assert "Unknown command" in rep.text
        finally:
            transport.close()
    run(main())


def test_straggler_reply_does_not_fail_login(firmware):
    """A late reply from a timed-out command must be skipped as a stray
    during the recovery login, and the replay must succeed (was: the
    straggler was consumed as the login reply -> spurious LoginFailed)."""
    firmware.delay_once["oledstart"] = 1.2

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            # timeout < delay -> CommandTimeout -> re-login (during which the
            # straggler 'OK' arrives) -> replay (no delay now) -> success.
            rep = await session.command("oledstart", expect="status", timeout=0.4)
            assert rep.ok
            assert firmware.oled_running
        finally:
            transport.close()
    run(main())


def test_nonreplayable_timeout_surfaces_and_recovers(firmware):
    """replay=False commands surface CommandTimeout instead of blind
    re-execution; the session stays healthy for the next command."""
    firmware.delay_once["micrecord start"] = 1.0

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            await session.command("openmic", expect="status")
            with pytest.raises(CommandTimeout):
                await session.command("micrecord start", expect="status",
                                      timeout=0.3, replay=False)
            # Straggler 'OK: Recording started' is drained as a stray; the
            # next command still works.
            rep = await session.command("uartlink status", expect="status", timeout=10)
            assert rep.ok
        finally:
            transport.close()
    run(main())


def test_garbage_flood_honors_deadline(firmware):
    """A break-noise flood must not extend reply collection past the
    command deadline (was: quiet-gap reset forever -> wedged daemon)."""
    firmware.delay_once["uartlink status"] = 5.0   # no real reply in the window

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            stop = threading.Event()

            def flood():
                while not stop.is_set():
                    firmware.inject_garbage(50)
                    time.sleep(0.05)

            flooder = threading.Thread(target=flood, daemon=True)
            flooder.start()
            try:
                t0 = time.monotonic()
                with pytest.raises(CommandTimeout):
                    await session.command("uartlink status", expect="auto",
                                          timeout=1.0, replay=False)
                assert time.monotonic() - t0 < 3.0, "deadline must bound the flood"
            finally:
                stop.set()
                flooder.join(timeout=2)
        finally:
            transport.close()
    run(main())


def test_auto_mode_collects_multiline_stamped_reply(firmware):
    """Multi-line successes arrive stamped 'OK: ...' on line one — auto mode
    must keep collecting instead of fast-returning after the status line."""
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            rep = await session.command("miclist", expect="auto", timeout=10)
            assert rep.ok
            assert len(rep.lines) >= 3
            assert "rec_1.wav" in rep.text
        finally:
            transport.close()
    run(main())


def test_oled_recovery_from_bare_error(firmware):
    """The oledstart recovery must fire on the REAL wire reply (bare
    'ERROR'), not on descriptive text that never crosses the channel."""
    firmware.oled_running = False

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            cfg = DeliverConfig(targets=["oled"], allow_oledstart=True)
            ok = await deliver(session, cfg, "hello from the CM5")
            assert ok
            assert firmware.oled_running
            assert firmware.oled_texts == ["hello from the CM5"]
        finally:
            transport.close()
    run(main())


def test_chunk_text_rejects_tiny_limit():
    with pytest.raises(ValueError):
        chunk_text("日本語 text", 2)


def test_config_coercions_and_validation(tmp_path):
    good = tmp_path / "good.yaml"
    good.write_text(
        "deliver:\n  targets: oled\n  g2_seconds: '30'\n"
        "audio:\n  record_seconds: '4'\n")
    cfg = load_config(good)
    assert cfg.deliver.targets == ["oled"]          # bare string coerced to list
    assert cfg.deliver.g2_seconds == 30             # quoted int coerced
    assert cfg.audio.record_seconds == 4.0          # quoted float coerced

    bad = tmp_path / "bad.yaml"
    bad.write_text("deliver:\n  g2_seconds: 600\n")
    with pytest.raises(ValueError, match="g2_seconds"):
        load_config(bad)                            # firmware honors 1..599 only


def test_wav_rejects_lying_data_header():
    from fake_firmware import make_wav
    from hw1_ai_service.audio import wav
    data = make_wav(seconds=0.5)
    truncated = data[:-1000]                        # header now over-declares
    with pytest.raises(wav.WavError, match="truncated"):
        wav.parse(truncated)


def test_transport_reopen_cycle(firmware):
    """close() -> open() is a supported cycle with a fresh queue — the
    daemon's reconnect supervisor depends on it."""
    from hw1_ai_service.link.session import Session
    from hw1_ai_service.link.transport import SerialTransport

    async def main():
        transport = SerialTransport(firmware._slave_path, 115200)
        transport.open()
        session = Session(transport, firmware.user, firmware.password)
        await session.login()
        transport.close()

        transport.open()                            # fresh queue, fresh reader
        firmware.authed_user = None                 # firmware side forgot us too
        await session.login()
        rep = await session.command("uartlink status", expect="status", timeout=10)
        assert rep.ok
        transport.close()
    run(main())
