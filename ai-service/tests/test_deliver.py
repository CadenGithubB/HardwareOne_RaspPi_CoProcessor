from __future__ import annotations

from conftest import open_link, run

from hw1_ai_service.config import DeliverConfig
from hw1_ai_service.deliver import chunk_text, deliver


def test_chunk_text_respects_limit():
    text = "word " * 1000
    chunks = chunk_text(text, 100)
    assert chunks
    assert all(len(c.encode()) <= 100 for c in chunks)
    assert " ".join(chunks).split() == text.split()


def test_chunk_text_hard_splits_monster_word():
    chunks = chunk_text("x" * 250, 100)
    assert all(len(c.encode()) <= 100 for c in chunks)
    assert "".join(chunks) == "x" * 250


def test_deliver_oledstart_recovery(firmware):
    """OLED not running -> one oledstart attempt -> retry succeeds."""
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


def test_deliver_g2(firmware):
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            cfg = DeliverConfig(targets=["g2"])
            ok = await deliver(session, cfg, "short answer")
            assert ok
            assert firmware.g2_texts == ["short answer"]
        finally:
            transport.close()
    run(main())
