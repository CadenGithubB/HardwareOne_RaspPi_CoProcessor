"""Gate E: the daemon-side live-STT gate and its batch-fallback guarantee."""

from __future__ import annotations

import asyncio
import struct
import time

from conftest import open_link, run
from fake_firmware import TEST_EVENAI_ID, make_wav

from hw1_ai_service.audio import wav
from hw1_ai_service.audio.live import LivePcmInbox
from hw1_ai_service.config import Config, SttConfig
from hw1_ai_service.jobs import EvenAiExchange
from hw1_ai_service.link import protocol
from hw1_ai_service.llm.fake import FakeLlm
from hw1_ai_service.pipeline import VoicePipeline
from hw1_ai_service.stt.fake import FakeSTT
from hw1_ai_service.stt.live_gate import (
    LiveSttGate,
    LiveSttOutcome,
    strip_leading_wake_fragment,
)

CONTROLLER = 0xC0DEC0DE00000001
EXCHANGE_INT = int(TEST_EVENAI_ID, 16)


# -- wire builders (real G2 shape: flags=0, source=2) ----------------------

def _begin(*, exchange=EXCHANGE_INT, controller=CONTROLLER,
           flags=0, source=2, rate=16_000) -> bytes:
    return struct.pack(
        "<BBBBIQQHH", 1, flags, source, 1, rate,
        exchange, controller, 2048, 0)


def _pcm_frame(offset: int, pcm: bytes, *, exchange=EXCHANGE_INT,
               controller=CONTROLLER, flags=0) -> bytes:
    return struct.pack(
        "<BBQQIH", 1, flags, exchange, controller,
        offset, len(pcm) // 2) + pcm


def _end(total: int, crc32: int, *, exchange=EXCHANGE_INT,
         controller=CONTROLLER, reason=0, dropped=0) -> bytes:
    return struct.pack(
        "<BBQQIII", 1, reason, exchange, controller, total, crc32, dropped)


def _feed_stream(inbox: LivePcmInbox, pcm: bytes, *,
                 exchange=EXCHANGE_INT, end=True) -> None:
    assert inbox.offer_frame(
        protocol.FRAME_LIVE_BEGIN, 0, _begin(exchange=exchange))
    seq = 1
    offset = 0
    crc = 0
    for i in range(0, len(pcm), 1000):
        chunk = pcm[i:i + 1000]
        assert inbox.offer_frame(
            protocol.FRAME_LIVE_PCM, seq,
            _pcm_frame(offset, chunk, exchange=exchange))
        crc = protocol.crc32_ieee(chunk, crc)
        offset += len(chunk) // 2
        seq += 1
    if end:
        assert inbox.offer_frame(
            protocol.FRAME_LIVE_END, seq, _end(offset, crc, exchange=exchange))


# -- fakes ------------------------------------------------------------------

class _FakeWorker:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.begins: list[dict] = []
        self.offered = bytearray()
        self.ended = False
        self.aborts: list[str] = []

    def on_begin(self, begin: dict) -> None:
        self.begins.append(begin)

    def offer_pcm(self, pcm: bytes) -> bool:
        self.offered.extend(pcm)
        return True

    def end_input(self) -> None:
        self.ended = True

    def abort(self, reason: str) -> None:
        self.aborts.append(reason)

    def wait(self, timeout: float) -> dict:
        return self.result

    def join(self, timeout: float) -> bool:
        return True


class _Reply:
    def __init__(self, text: str) -> None:
        self.text = text

    @property
    def ok(self) -> bool:
        return self.text.startswith("OK")


class _FakeSession:
    """Scripted liveaudio replies keyed by the first two command words."""

    def __init__(self, replies: dict[str, list[str]]) -> None:
        self.replies = {k: list(v) for k, v in replies.items()}
        self.commands: list[str] = []

    async def command(self, line: str, **_kw) -> _Reply:
        self.commands.append(line)
        key = " ".join(line.split()[:2])
        queue = self.replies.get(key)
        if not queue:
            raise AssertionError(f"unscripted command: {line}")
        text = queue[0] if len(queue) == 1 else queue.pop(0)
        return _Reply(text)


class _StubGate:
    """Duck-typed pipeline-facing gate for run_evenai tests."""

    def __init__(self, outcome: LiveSttOutcome) -> None:
        self.outcome = outcome
        self.captures = 0
        self.aborted: list[str] = []

    async def capture(self, exchange, *, vad_max_seconds: float) -> LiveSttOutcome:
        self.captures += 1
        return self.outcome

    async def abort_stream(self, session, exchange) -> None:
        self.aborted.append(exchange.exchange_id)

    async def ensure_armed(self, session) -> bool:
        return True

    def link_reset(self) -> None:
        pass


def _gate(tmp_path, **cfg_kw) -> tuple[LiveSttGate, LivePcmInbox]:
    cfg = SttConfig(live_model_dir=str(tmp_path), **cfg_kw)
    inbox = LivePcmInbox(CONTROLLER)
    gate = LiveSttGate(cfg, inbox)
    # Unit scope: no real model builds; capture tests inject _FakeWorker.
    gate._spawn_warm = lambda retiring=None: None
    return gate, inbox


_READY_OK = ("OK: liveaudio ready version=1 controller=c0dec0de00000001 "
             "session_epoch=7 renew_direct=1 lease_ttl_ms=3000 "
             "renew_ms=1000 baud=2000000")
_STATUS_OFF = "OK: liveaudio task=ready shadow=off shadow_mode=exact active=0"
_STATUS_ON = "OK: liveaudio task=ready shadow=on shadow_mode=native active=0"


# -- wake-fragment strip ----------------------------------------------------

def _worker_result(stop_lines, *, recovered=False, text=None):
    joined = " ".join(
        ln["text"].strip() for ln in stop_lines if ln["text"].strip())
    return {
        "text": text if text is not None else joined,
        "stream": {"stop_lines": stop_lines, "final_recovered": recovered},
    }


def test_strip_drops_validated_hardware_artifact_shape():
    result = _worker_result([
        {"text": "even.", "start_time": 0.0},
        {"text": "What is the capital of France?", "start_time": 0.45},
    ])
    text, dropped = strip_leading_wake_fragment(result["text"], result)
    assert text == "What is the capital of France?"
    assert dropped == "even."


def test_strip_never_touches_multiword_first_sentences():
    # The prefix rule alone would delete "Turn on the lights." — the strip
    # must not (adversarial-review blocker).
    result = _worker_result([
        {"text": "Turn on the lights.", "start_time": 0.0},
        {"text": "Then dim them.", "start_time": 2.2},
    ])
    text, dropped = strip_leading_wake_fragment(result["text"], result)
    assert text == "Turn on the lights. Then dim them."
    assert dropped is None


def test_strip_requires_capture_head_start_time():
    result = _worker_result([
        {"text": "then.", "start_time": 0.4},
        {"text": "What is a potato?", "start_time": 0.9},
    ])
    assert strip_leading_wake_fragment(result["text"], result)[1] is None


def test_strip_requires_content_within_preroll_window():
    result = _worker_result([
        {"text": "then.", "start_time": 0.0},
        {"text": "What is a potato?", "start_time": 3.0},
    ])
    assert strip_leading_wake_fragment(result["text"], result)[1] is None


def test_strip_skips_recovered_finals_and_single_lines():
    recovered = _worker_result(
        [{"text": "", "start_time": 0.5}], recovered=True,
        text="even. What is a potato?")
    assert strip_leading_wake_fragment(
        recovered["text"], recovered)[0] == "even. What is a potato?"
    single = _worker_result([{"text": "What is a potato?", "start_time": 0.3}])
    assert strip_leading_wake_fragment(single["text"], single)[1] is None


# -- wav builder ------------------------------------------------------------

def test_wav_build_round_trips_canonical():
    pcm = bytes(range(256)) * 4
    data = wav.build(pcm, 16000)
    parsed = wav.parse(data)
    wav.require_canonical(parsed)
    assert parsed.pcm == pcm


# -- arming -----------------------------------------------------------------

def test_ensure_armed_verifies_before_arming(tmp_path):
    async def main():
        gate, _ = _gate(tmp_path)
        session = _FakeSession({
            "liveaudio ready": [_READY_OK],
            "liveaudio status": [_STATUS_OFF],
            "liveaudio shadow": ["OK: shadow armed"],
        })
        assert await gate.ensure_armed(session)
        assert gate._armed and gate._armed_epoch == 7
        kinds = [" ".join(c.split()[:2]) for c in session.commands]
        assert kinds == ["liveaudio ready", "liveaudio status", "liveaudio shadow"]
        await gate.close()
    run(main())


def test_ensure_armed_skips_arm_when_shadow_already_native(tmp_path):
    # A blind re-arm clears a pending per-wake capture arm on the firmware —
    # verification-first is load-bearing, not politeness.
    async def main():
        gate, _ = _gate(tmp_path)
        session = _FakeSession({
            "liveaudio ready": [_READY_OK],
            "liveaudio status": [_STATUS_ON],
        })
        assert await gate.ensure_armed(session)
        assert not any("shadow 1" in c and " on " in c for c in session.commands)
        await gate.close()
    run(main())


def test_arm_failures_disable_until_link_reset(tmp_path):
    async def main():
        gate, _ = _gate(tmp_path)
        session = _FakeSession({
            "liveaudio ready": ["Error: liveaudio lease busy"],
        })
        for _ in range(3):
            assert not await gate.ensure_armed(session)
        assert gate._disabled
        outcome = await gate.capture(
            EvenAiExchange(TEST_EVENAI_ID), vad_max_seconds=1.0)
        assert not outcome.valid and outcome.reason == "not_armed"
        assert not outcome.lane_active
        gate.link_reset()
        assert not gate._disabled
        await gate.close()
    run(main())


# -- capture ----------------------------------------------------------------

def test_capture_streams_pcm_through_worker(tmp_path):
    async def main():
        gate, inbox = _gate(tmp_path, live_wake_stream_timeout_s=1.0)
        gate._armed = True
        pcm = bytes(4000)
        _feed_stream(inbox, pcm)
        worker = _FakeWorker(_worker_result([
            {"text": "been.", "start_time": 0.0},
            {"text": "What is a potato?", "start_time": 0.6},
        ]) | {"valid": True, "failure_reasons": []})
        gate._warm = worker
        outcome = await gate.capture(
            EvenAiExchange(TEST_EVENAI_ID), vad_max_seconds=2.0)
        assert outcome.valid, outcome.reason
        assert outcome.text == "What is a potato?"
        assert outcome.dropped_leading == "been."
        assert outcome.raw_text == "been. What is a potato?"
        assert bytes(worker.offered) == pcm
        assert outcome.pcm == pcm
        assert worker.ended and worker.begins == [{"sample_rate": 16000}]
        # The capture thread finalized+discarded its worker either way.
        assert worker.aborts == []
        await gate.close()
    run(main())


def test_capture_aborted_stream_is_invalid_not_fatal(tmp_path):
    async def main():
        gate, inbox = _gate(tmp_path, live_wake_stream_timeout_s=1.0)
        gate._armed = True
        assert inbox.offer_frame(protocol.FRAME_LIVE_BEGIN, 0, _begin())
        assert inbox.offer_frame(
            protocol.FRAME_LIVE_ABORT, 1,
            _end(0, 0, reason=protocol.LIVE_ABORT_REASON_HOST_REQUEST))
        worker = _FakeWorker({"valid": False})
        gate._warm = worker
        outcome = await gate.capture(
            EvenAiExchange(TEST_EVENAI_ID), vad_max_seconds=2.0)
        assert not outcome.valid
        assert outcome.reason.startswith("stream_abort")
        assert not worker.ended
        assert worker.aborts, "unfinished worker must be aborted"
        await gate.close()
    run(main())


def test_capture_never_invalidates_a_mismatched_stream(tmp_path):
    async def main():
        gate, inbox = _gate(tmp_path, live_wake_stream_timeout_s=0.4)
        gate._armed = True
        other = 0xA1B2C3D400000099
        assert inbox.offer_frame(
            protocol.FRAME_LIVE_BEGIN, 0, _begin(exchange=other))
        worker = _FakeWorker({"valid": False})
        gate._warm = worker
        outcome = await gate.capture(
            EvenAiExchange(TEST_EVENAI_ID), vad_max_seconds=1.0)
        assert not outcome.valid and outcome.reason == "begin_timeout"
        # The successor/stale stream was left for its own consumer.
        assert inbox._active is not None
        assert inbox._active.terminal is None
        await gate.close()
    run(main())


def test_two_begin_timeouts_force_a_verified_rearm(tmp_path):
    async def main():
        gate, inbox = _gate(tmp_path, live_wake_stream_timeout_s=0.1)
        gate._armed = True
        for _ in range(2):
            gate._warm = _FakeWorker({"valid": False})
            outcome = await gate.capture(
                EvenAiExchange(TEST_EVENAI_ID), vad_max_seconds=1.0)
            assert outcome.reason == "begin_timeout"
        assert not gate._armed
        await gate.close()
    run(main())


def test_capture_cancel_mid_stream_invalidates_and_aborts(tmp_path):
    async def main():
        gate, inbox = _gate(tmp_path, live_wake_stream_timeout_s=1.0)
        gate._armed = True
        assert inbox.offer_frame(protocol.FRAME_LIVE_BEGIN, 0, _begin())
        assert inbox.offer_frame(
            protocol.FRAME_LIVE_PCM, 1, _pcm_frame(0, bytes(1000)))
        worker = _FakeWorker({"valid": False})
        gate._warm = worker
        exchange = EvenAiExchange(TEST_EVENAI_ID)
        task = asyncio.create_task(
            gate.capture(exchange, vad_max_seconds=10.0))
        await asyncio.sleep(0.3)
        exchange.cancel("dismissed")
        outcome = await task
        assert not outcome.valid
        assert "host_cancelled" in outcome.reason
        assert "host_cancelled" in worker.aborts
        await gate.close()
    run(main())


# -- pipeline integration ---------------------------------------------------

def _live_outcome(text: str, *, valid: bool = True, reason: str = "",
                  seconds: float = 1.0) -> LiveSttOutcome:
    pcm = bytes(int(16000 * seconds) * 2)
    return LiveSttOutcome(
        valid=valid, reason=reason, text=text, raw_text=text, pcm=pcm,
        sample_rate=16000, worker_result={"valid": valid, "text": text},
        end_to_final_s=0.05)


def test_run_evenai_live_success_skips_voicefetch(firmware):
    firmware.wav_bytes = make_wav(seconds=1.0)

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            firmware.begin_wake_capture(push=False)
            cfg = Config()
            stub = _StubGate(_live_outcome("what is a potato"))
            pipeline = VoicePipeline(
                session, FakeSTT(), FakeLlm(), cfg, live_gate=stub)
            try:
                exchange = EvenAiExchange(TEST_EVENAI_ID)
                answer = await pipeline.run_evenai(exchange)
                await exchange.drain_tasks()
            finally:
                await pipeline.close()
            assert stub.captures == 1
            assert firmware.evenai_asks == ["what is a potato"]
            assert answer == "echo: what is a potato"
            # The transcript came off the live stream: no audio transfer at all.
            assert not any(c.startswith(("voicefetch", "fileread"))
                           for c in firmware.command_log)
            assert stub.aborted == [], "valid live outcome must not abort the lane"
            # The device-side WAV was still cleaned up (stopid fallback leg).
            assert firmware.deleted, "live path cleaned up the device WAV"
        finally:
            transport.close()
    run(main())


def test_run_evenai_live_failure_falls_back_to_batch(firmware):
    firmware.wav_bytes = make_wav(seconds=1.0)

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            firmware.begin_wake_capture(push=False)
            cfg = Config()
            stub = _StubGate(_live_outcome(
                "", valid=False, reason="stream_abort:6"))
            pipeline = VoicePipeline(
                session, FakeSTT(), FakeLlm(), cfg, live_gate=stub)
            try:
                answer = await pipeline.run_evenai(EvenAiExchange(TEST_EVENAI_ID))
            finally:
                await pipeline.close()
            # Fallback = the pre-Gate-E path, including the lane release.
            assert stub.aborted == [TEST_EVENAI_ID]
            assert answer == "echo: fake transcript of 1.0s audio"
            assert firmware.evenai_replies == ["echo: fake transcript of 1.0s audio"]
            assert firmware.deleted
        finally:
            transport.close()
    run(main())


def test_foreign_stream_delivery_is_not_consumed():
    # Rapid re-wake: exchange A polls next_stream for its own (never-arriving)
    # BEGIN and must NOT consume exchange B's one delivery slot.
    inbox = LivePcmInbox(CONTROLLER)
    other = 0xB0000000B0000001
    assert inbox.offer_frame(
        protocol.FRAME_LIVE_BEGIN, 0, _begin(exchange=other))
    # A's filtered poll wakes on B's BEGIN, skips it, and times out.
    try:
        inbox.next_stream(timeout=0.1, for_exchange=EXCHANGE_INT)
        assert False, "should have timed out on a foreign-only stream"
    except TimeoutError:
        pass
    # B's own consumer still gets its stream — the slot was preserved.
    stream = inbox.next_stream(timeout=0.1, for_exchange=other)
    assert stream.exchange_id == other


def test_warm_build_failures_disable_the_gate(tmp_path, monkeypatch):
    # A model that can never build (missing package / OOM) must stop arming
    # and stop respawning after the failure limit, not retry every wake.
    from hw1_ai_service.stt import live_gate as lg

    def boom():
        raise RuntimeError("no moonshine_voice")

    monkeypatch.setattr(lg, "LiveMoonshineWorker",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    cfg = SttConfig(live_model_dir=str(tmp_path))
    gate = LiveSttGate(cfg, LivePcmInbox(CONTROLLER))
    for _ in range(3):
        gate._warm_build(None)
    assert gate._disabled

    async def check():
        outcome = await gate.capture(
            EvenAiExchange(TEST_EVENAI_ID), vad_max_seconds=1.0)
        assert outcome.reason == "not_armed"
        await gate.close()
    run(check())


def test_power_only_daemon_does_not_build_the_gate(tmp_path):
    from hw1_ai_service import __main__ as main_mod

    args = type("A", (), {"cmd": "daemon"})()
    cfg = Config()
    cfg.stt.live_enabled = True
    cfg.stt.live_model_dir = str(tmp_path)
    cfg.stt.engine = "none"          # strict-RAM / power-only degraded mode
    inbox, gate = main_mod._build_live_gate(args, cfg)
    assert inbox is None and gate is None


def test_run_evenai_without_gate_is_unchanged(firmware):
    firmware.wav_bytes = make_wav(seconds=1.0)

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            firmware.begin_wake_capture(push=False)
            pipeline = VoicePipeline(
                session, FakeSTT(), FakeLlm(), Config())
            try:
                answer = await pipeline.run_evenai(EvenAiExchange(TEST_EVENAI_ID))
            finally:
                await pipeline.close()
            assert answer == "echo: fake transcript of 1.0s audio"
        finally:
            transport.close()
    run(main())
