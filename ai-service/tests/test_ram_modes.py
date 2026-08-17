"""One-engine modes and RAM preflight — small-RAM Pis degrade gracefully
instead of OOM-ing."""

from __future__ import annotations

import pytest
from conftest import open_link, run
from fake_firmware import make_wav

from hw1_ai_service.config import Config, load as load_config
from hw1_ai_service.mem import estimate_llm_bytes, preflight, read_meminfo
from hw1_ai_service.pipeline import VoicePipeline
from hw1_ai_service.stt.fake import FakeSTT


def test_ask_without_llm_delivers_transcript(firmware):
    """llm.engine: none -> voice notes: the transcript IS the delivery."""
    firmware.wav_bytes = make_wav(seconds=1.0)

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            cfg = Config()
            cfg.audio.record_seconds = 0.05
            pipeline = VoicePipeline(session, FakeSTT(), None, cfg)
            try:
                answer = await pipeline.run_ask()
            finally:
                await pipeline.close()
            assert answer.startswith("Heard:")
            assert firmware.oled_texts, "transcript delivered to the display"
        finally:
            transport.close()
    run(main())


def test_chat_without_llm_explains(firmware):
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            pipeline = VoicePipeline(session, None, None, Config())
            try:
                answer = await pipeline.run_chat("hello")
            finally:
                await pipeline.close()
            assert "disabled" in answer
            assert not firmware.oled_texts, "nothing delivered for a no-op"
        finally:
            transport.close()
    run(main())


def test_ask_without_stt_explains(firmware):
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            pipeline = VoicePipeline(session, None, None, Config())
            try:
                answer = await pipeline.run_ask()
            finally:
                await pipeline.close()
            assert "disabled" in answer
            assert not firmware.command_log or all(
                not c.startswith("micrecord") for c in firmware.command_log
            ), "no recording attempted without an STT engine"
        finally:
            transport.close()
    run(main())


def test_llm_crash_degrades_to_transcript(firmware):
    """A dying/unreachable LLM (OOM-killed llama-server) must not fail the
    exchange — the transcript is delivered with an offline marker."""
    firmware.wav_bytes = make_wav(seconds=0.5)

    class DyingLlm:
        async def ask_stream(self, prompt):
            raise ConnectionError("connection refused")
            yield  # pragma: no cover

        async def close(self):
            pass

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            cfg = Config()
            cfg.audio.record_seconds = 0.05
            pipeline = VoicePipeline(session, FakeSTT(), DyingLlm(), cfg)
            try:
                answer = await pipeline.run_ask()
            finally:
                await pipeline.close()
            assert answer.startswith("(assistant offline)")
            assert "fake transcript" in answer
            assert firmware.oled_texts
        finally:
            transport.close()
    run(main())


def test_config_accepts_none_modes(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("llm:\n  engine: none\n")
    cfg = load_config(p)
    assert cfg.llm.engine == "none"

    p.write_text("llm:\n  engine: none\nstt:\n  engine: none\n")
    with pytest.raises(ValueError, match="nothing to run"):
        load_config(p)


def test_mem_preflight_warn_and_strict(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:  3900000 kB\nMemAvailable:  1000000 kB\n")
    model = tmp_path / "big.gguf"
    model.write_bytes(b"\0" * (2 * 1024 * 1024))   # tiny stand-in file
    # Inflate the estimate by pretending it's huge via a real big number:
    # instead, test the arithmetic path with the small file (fits) and an
    # absent file (skip), then force strict failure with stt alone.
    cfg = Config()
    cfg.llm.engine = "server"
    cfg.llm.model = str(model)
    msgs = preflight(cfg, meminfo_path=str(meminfo))
    assert any("RAM:" in m for m in msgs)

    # 1GB available vs moonshine small (0.6GB) + headroom + LLM: force an
    # over-budget by shrinking availability.
    meminfo.write_text("MemTotal:  3900000 kB\nMemAvailable:  400000 kB\n")
    msgs = preflight(cfg, meminfo_path=str(meminfo))
    assert any("WARNING" in m for m in msgs), "warn mode proceeds with a warning"

    cfg.service.ram_check = "strict"
    with pytest.raises(RuntimeError, match="preflight failed"):
        preflight(cfg, meminfo_path=str(meminfo))


def test_mem_reader_handles_missing_file():
    assert read_meminfo("/nonexistent/meminfo") is None
    assert estimate_llm_bytes("/nonexistent/model.gguf") is None
