"""Ask-display instrumentation (docs/EVENAI_ASK_DISPLAY_DEBUG_PLAN.md step 1).

Pins: the 236-byte shown-ask clip (a 237+ byte ask would TERMINALIZE the whole
exchange at the firmware — verified 2026-08-11), the retained delivery stamps
on the `evenai timings:` line, and corpus capture for BOTH paths with the
delivery-timing block.
"""

from __future__ import annotations

import asyncio
import json
import logging

from conftest import open_link, run
from fake_firmware import TEST_EVENAI_ID, make_wav
from hw1_ai_service.config import Config
from hw1_ai_service.jobs import EvenAiExchange
from hw1_ai_service.llm.fake import FakeLlm
from hw1_ai_service.pipeline import VoicePipeline, _ASK_MAX_BYTES, _clip_ask
from hw1_ai_service.stt.fake import FakeSTT
from hw1_ai_service.stt.live_gate import LiveSttOutcome


class _StubGate:
    def __init__(self, outcome: LiveSttOutcome) -> None:
        self.outcome = outcome

    async def capture(self, exchange, *, vad_max_seconds: float):
        return self.outcome

    async def abort_stream(self, session, exchange) -> None:
        pass

    async def ensure_armed(self, session) -> bool:
        return True

    def link_reset(self) -> None:
        pass


def _live_outcome(text: str, *, valid: bool = True, reason: str = "",
                  seconds: float = 1.0) -> LiveSttOutcome:
    pcm = bytes(int(16000 * seconds) * 2)
    return LiveSttOutcome(
        valid=valid, reason=reason, text=text, raw_text=text, pcm=pcm,
        sample_rate=16000, worker_result={"valid": valid, "text": text},
        end_to_final_s=0.05)


# -- _clip_ask boundaries ---------------------------------------------------

def test_clip_ask_at_exact_ceiling_passes_untouched():
    text = "a" * _ASK_MAX_BYTES
    shown, clipped = _clip_ask(text)
    assert (shown, clipped) == (text, False)


def test_clip_ask_over_ceiling_clips_under_236_with_ellipsis():
    text = "word " * 60  # 300 bytes
    shown, clipped = _clip_ask(text)
    assert clipped
    assert shown.endswith("…")
    assert len(shown.encode("utf-8")) <= _ASK_MAX_BYTES


def test_clip_ask_never_splits_a_multibyte_codepoint():
    text = "é" * 200  # 400 UTF-8 bytes of 2-byte codepoints
    shown, clipped = _clip_ask(text)
    assert clipped
    shown.encode("utf-8").decode("utf-8")  # round-trips: no torn codepoint
    assert len(shown.encode("utf-8")) <= _ASK_MAX_BYTES


# -- on-the-wire clip + timings line + corpus -------------------------------

def _pipeline_cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.stt.live_debug_capture = True
    cfg.stt.live_debug_dir = str(tmp_path / "corpus")
    # Keep the ask-render hold sub-millisecond so tests never sleep a real
    # render budget; hold_remain is still computed and recorded.
    cfg.deliver.g2_ask_render_cps = 100000.0
    cfg.deliver.g2_ask_render_start_margin_s = 0.0
    cfg.deliver.g2_ask_min_dwell_s = 0.0
    return cfg


def test_long_live_transcript_is_clipped_on_the_wire(firmware, tmp_path,
                                                     caplog):
    long_text = ("what is the thing called that people mark dates on and "
                 "also the other thing and " * 5).strip()  # >> 236 bytes
    assert len(long_text.encode()) > _ASK_MAX_BYTES

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            firmware.begin_wake_capture(push=False)
            pipeline = VoicePipeline(
                session, FakeSTT(), FakeLlm(), _pipeline_cfg(tmp_path),
                live_gate=_StubGate(_live_outcome(long_text)))
            try:
                exchange = EvenAiExchange(TEST_EVENAI_ID)
                await pipeline.run_evenai(exchange)
                await exchange.drain_tasks()
            finally:
                await pipeline.close()
            # The wire ask never exceeds the firmware ceiling that would
            # terminalize the exchange; the LLM still saw the full text.
            assert len(firmware.evenai_asks) == 1
            wire_ask = firmware.evenai_asks[0]
            assert len(wire_ask.encode("utf-8")) <= _ASK_MAX_BYTES
            assert wire_ask.endswith("…")
            assert "ask clipped for the lens" in caplog.text
            # The LLM answered from the FULL transcript (streamed as parts —
            # only the lens copy of the question was clipped).
            answer_text = "".join(firmware.evenai_reply_parts) \
                or (firmware.evenai_replies[-1] if firmware.evenai_replies
                    else "")
            assert answer_text.startswith("echo: what is the")
        finally:
            transport.close()

    caplog.set_level(logging.INFO, logger="pipeline")
    run(main())


def test_timings_line_reports_ask_gap_and_hold_remain(firmware, tmp_path,
                                                      caplog):
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            firmware.begin_wake_capture(push=False)
            pipeline = VoicePipeline(
                session, FakeSTT(), FakeLlm(), _pipeline_cfg(tmp_path),
                live_gate=_StubGate(_live_outcome("what is a potato")))
            try:
                exchange = EvenAiExchange(TEST_EVENAI_ID)
                await pipeline.run_evenai(exchange)
                await exchange.drain_tasks()
            finally:
                await pipeline.close()
            timing_lines = [r.message for r in caplog.records
                            if "evenai timings:" in r.message]
            assert len(timing_lines) == 1
            line = timing_lines[0]
            assert "ask_gap=" in line and "ask_gap=n/a" not in line
            assert "hold_remain=" in line
            assert "question=16B" in line  # len("what is a potato")
        finally:
            transport.close()

    caplog.set_level(logging.INFO, logger="pipeline")
    run(main())


def test_dwell_floor_engages_for_fast_answers(firmware, tmp_path, caplog):
    """The 2026-08-11 calibration: budget = max(margin + len/cps, min_dwell).
    A fast LLM must now be HELD until the floor elapses — before this fix
    hold_remain was negative on every field wake (the hold never engaged)."""
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            firmware.begin_wake_capture(push=False)
            cfg = _pipeline_cfg(tmp_path)
            cfg.deliver.g2_ask_render_cps = 1000.0
            cfg.deliver.g2_ask_render_start_margin_s = 0.1
            cfg.deliver.g2_ask_min_dwell_s = 0.4
            pipeline = VoicePipeline(
                session, FakeSTT(), FakeLlm(), cfg,
                live_gate=_StubGate(_live_outcome("what is a potato")))
            try:
                exchange = EvenAiExchange(TEST_EVENAI_ID)
                await pipeline.run_evenai(exchange)
                await exchange.drain_tasks()
            finally:
                await pipeline.close()
            assert "holding first reply" in caplog.text
            line = next(r.message for r in caplog.records
                        if "evenai timings:" in r.message)
            import re
            remain = float(
                re.search(r"hold_remain=(-?\d+\.\d+)s", line).group(1))
            assert remain > 0, line  # the floor engaged
        finally:
            transport.close()

    caplog.set_level(logging.INFO, logger="pipeline")
    run(main())


def _read_corpus(tmp_path) -> list[dict]:
    corpus = tmp_path / "corpus"
    return [json.loads(p.read_text()) for p in sorted(corpus.glob("*.json"))]


def test_corpus_live_sample_carries_delivery_block(firmware, tmp_path):
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            firmware.begin_wake_capture(push=False)
            pipeline = VoicePipeline(
                session, FakeSTT(), FakeLlm(), _pipeline_cfg(tmp_path),
                live_gate=_StubGate(_live_outcome("what is a potato")))
            try:
                exchange = EvenAiExchange(TEST_EVENAI_ID)
                await pipeline.run_evenai(exchange)
                await exchange.drain_tasks()
            finally:
                await pipeline.close()
        finally:
            transport.close()
    run(main())

    samples = _read_corpus(tmp_path)
    assert len(samples) == 1
    s = samples[0]
    assert s["schema_version"] == 2
    assert s["path"] == "live"
    assert s["snapshot"] is not None
    assert s["question"] == {"shown_bytes": 16, "clipped": False}
    d = s["delivery"]
    assert d["ask_rtt_s"] is not None and d["ask_rtt_s"] >= 0
    assert d["ask_gap_s"] is not None and d["ask_gap_s"] >= 0
    assert d["hold_remain_s"] is not None


def test_corpus_batch_sample_captured_without_snapshot(firmware, tmp_path):
    firmware.wav_bytes = make_wav(seconds=1.0)

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            firmware.begin_wake_capture(push=False)
            # Invalid live outcome -> batch fallback path end to end.
            pipeline = VoicePipeline(
                session, FakeSTT(), FakeLlm(), _pipeline_cfg(tmp_path),
                live_gate=_StubGate(_live_outcome(
                    "", valid=False, reason="stream_abort:6")))
            try:
                exchange = EvenAiExchange(TEST_EVENAI_ID)
                await pipeline.run_evenai(exchange)
                await exchange.drain_tasks()
            finally:
                await pipeline.close()
        finally:
            transport.close()
    run(main())

    samples = _read_corpus(tmp_path)
    assert len(samples) == 1
    s = samples[0]
    assert s["schema_version"] == 2
    assert s["path"] == "batch"
    assert s["snapshot"] is None
    assert s["transcript"] == "fake transcript of 1.0s audio"
    assert s["audio_seconds"] > 0.9
    assert s["delivery"]["ask_gap_s"] is not None
