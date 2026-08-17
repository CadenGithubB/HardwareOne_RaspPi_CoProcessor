"""EvenAI wake flow: EVT push -> job -> fetch-the-device's-capture -> STT ->
g2evenai ask/reply into the native windows.

The firmware double simulates the device side of Phase 2: a wake auto-starts
a VAD-armed recording and pushes ONE evenai_wake EVT frame only once the
recording is live (begin_wake_capture). These tests cover the client
contract: routing (idle pump AND mid-command), dedupe, the wake-fetch that
never sends micrecord start, native-window delivery, and the
don't-reopen-the-card rule for empty transcripts.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from conftest import open_link, run
from fake_firmware import TEST_EVENAI_ID, make_wav

from hw1_ai_service.config import Config
from hw1_ai_service.jobs import (
    EvenAiCancelled, EvenAiExchange, ManualTrigger, route_link_event)
from hw1_ai_service.llm.fake import FakeLlm
from hw1_ai_service.pipeline import VoicePipeline
from hw1_ai_service.stt.fake import FakeSTT


class _SilentSTT:
    """STT that hears nothing — the empty-transcript branch."""

    def transcribe(self, pcm: bytes, rate: int) -> str:
        return ""


class _WordyLlm:
    """Streams a fixed multi-sentence answer word by word."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.prompts: list[str] = []

    async def ask_stream(self, prompt: str):
        self.prompts.append(prompt)
        for word in self.text.split(" "):
            await asyncio.sleep(0)
            yield word + " "

    async def close(self) -> None:
        pass

    def clear_history(self) -> None:
        pass


def _exchange() -> EvenAiExchange:
    return EvenAiExchange(TEST_EVENAI_ID)


def test_evenai_dedupe_gate():
    """At most one evenai job queued/running until evenai_done re-arms."""
    async def main():
        trigger = ManualTrigger()
        trigger.submit_evenai(TEST_EVENAI_ID)
        trigger.submit_evenai(TEST_EVENAI_ID)  # duplicate: dropped
        assert trigger._queue.qsize() == 1
        job = await trigger.next_job()
        assert job.kind == "evenai"
        trigger.submit_evenai(TEST_EVENAI_ID)  # still pending: dropped
        assert trigger._queue.qsize() == 0
        trigger.evenai_done(TEST_EVENAI_ID)
        # A repeated ID is terminal forever within this boot; a new counter is
        # the only valid next exchange.
        trigger.submit_evenai("a1b2c3d400000002")
        assert trigger._queue.qsize() == 1
    run(main())


def test_route_link_event_only_handles_known_events():
    async def main():
        trigger = ManualTrigger()
        route_link_event(f"evenai_wake {TEST_EVENAI_ID}".encode(), trigger)
        route_link_event(b"someday_maybe 42", trigger)   # unknown: ignored
        route_link_event(b"\xff\xfe", trigger)           # undecodable: ignored
        # Telemetry events (2026-08-10 firmware): parsed, but must not queue
        # jobs or raise — the daemon just acknowledges them.
        route_link_event(
            f"evenai_timing {TEST_EVENAI_ID} wake_ms=1000 claim_ms=1200 "
            f"firstpcm_ms=1450 vadend_ms=4000 closed_ms=4200 samples=44000 "
            f"rate=16000 degraded=0".encode(), trigger)
        route_link_event(
            f"evenai_stream_complete {TEST_EVENAI_ID}".encode(), trigger)
        assert trigger._queue.qsize() == 1
        assert (await trigger.next_job()).kind == "evenai"
    run(main())


def test_parse_event_timing_and_stream_complete():
    from hw1_ai_service import evenai_protocol as wire

    ev = wire.parse_event(
        f"evenai_timing {TEST_EVENAI_ID} wake_ms=1000 claim_ms=1200 "
        f"firstpcm_ms=1450 vadend_ms=4000 closed_ms=4200 samples=44000 "
        f"rate=16000 degraded=1 preroll_ms=800 future_field=7 not_kv")
    assert isinstance(ev, wire.TimingEvent)
    assert ev.exchange_id == TEST_EVENAI_ID
    assert ev.stamps_ms == {"wake_ms": 1000, "claim_ms": 1200,
                            "firstpcm_ms": 1450, "vadend_ms": 4000,
                            "closed_ms": 4200, "preroll_ms": 800}
    assert ev.samples == 44000 and ev.rate == 16000 and ev.degraded

    manual = wire.parse_event("evenai_timing - wake_ms=0 samples=100 rate=16000")
    assert isinstance(manual, wire.TimingEvent)
    assert manual.exchange_id is None and not manual.degraded

    done = wire.parse_event(f"evenai_stream_complete {TEST_EVENAI_ID}")
    assert isinstance(done, wire.StreamCompleteEvent)
    assert done.exchange_id == TEST_EVENAI_ID

    import pytest
    with pytest.raises(wire.EvenAiProtocolError):
        wire.parse_event("evenai_timing")                    # missing owner
    with pytest.raises(wire.EvenAiProtocolError):
        wire.parse_event("evenai_stream_complete")           # missing ID
    with pytest.raises(wire.EvenAiProtocolError):
        wire.parse_event("evenai_timing not-an-id wake_ms=1")


def test_legacy_evenai_mutations_terminate_live_card_fail_closed(firmware):
    """The integration double must enforce the production ID boundary.

    Historic log parsers may still recognize old command records, but an
    untagged live mutation must never change the active card in current tests.
    """
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            for index, command in enumerate((
                    "g2evenai ask",
                    "g2evenai ask legacy question",
                    "g2evenai reply",
                    "g2evenai reply legacy answer",
                    "g2evenai replypart",
                    "g2evenai replypart legacy delta",
                    "g2evenai replyend",
                    "g2evenai exit")):
                exchange_id = f"a1b2c3d4{index + 1:08x}"
                firmware.begin_wake_capture(
                    push=False, exchange_id=exchange_id)
                rep = await session.command(command, expect="status")
                assert not rep.ok
                assert "tagged EvenAI exchange ID required" in rep.text
                assert "active exchange terminated" in rep.text
                assert not firmware.evenai_active
            # Every legacy verb gets its own live exchange above; each must
            # terminate without applying any legacy payload.
            assert firmware.evenai_asks == []
            assert firmware.evenai_replies == []
            assert firmware.evenai_reply_parts == []
            assert not firmware.evenai_reply_ended
        finally:
            transport.close()
    run(main())


def test_owned_stop_discard_is_exact_retained_and_idempotent(firmware):
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            firmware.begin_wake_capture(push=False)

            first = await session.command(
                f"micrecord stopid {TEST_EVENAI_ID} discard",
                expect="status", replay=False)
            assert first.ok and "discarded" in first.text.lower()
            assert not firmware.recording
            assert firmware.files == {}

            status = await session.command(
                f"micrecord statusid {TEST_EVENAI_ID}", expect="status")
            assert status.ok and "discarded" in status.text.lower()

            next_id = "a1b2c3d400000002"
            firmware.begin_wake_capture(push=False, exchange_id=next_id)
            again = await session.command(
                f"micrecord stopid {TEST_EVENAI_ID} discard",
                expect="status", replay=False)
            assert again.ok and "discarded" in again.text.lower()
            assert firmware.recording
            assert firmware._recording_owner == next_id

            next_status = await session.command(
                f"micrecord statusid {next_id}", expect="status")
            assert next_status.ok and "active" in next_status.text.lower()
        finally:
            transport.close()

    run(main())


def test_evt_during_command_is_routed_not_lost(firmware):
    """A wake push landing mid-reply reaches on_event and never corrupts the
    in-flight command's reply."""
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            got: list[bytes] = []
            session.on_event = got.append
            firmware.delay_once["uartlink status"] = 0.6

            async def push_mid_command():
                await asyncio.sleep(0.2)
                firmware.push_event("evenai_wake")

            pusher = asyncio.create_task(push_mid_command())
            rep = await session.command("uartlink status", expect="status")
            await pusher
            assert rep.ok and "UART link" in rep.text
            assert got == [b"evenai_wake"]
        finally:
            transport.close()
    run(main())


def test_full_evenai_exchange(firmware):
    """run_evenai against a live wake capture: awaits the device's VAD stop,
    fetches WITHOUT ever sending micrecord start, transcribes, and answers
    into the native windows only (no oled/g2notify delivery)."""
    firmware.wav_bytes = make_wav(seconds=1.0)

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            firmware.begin_wake_capture(push=False)   # device-side auto-start
            cfg = Config()
            stt, llm = FakeSTT(), FakeLlm()
            pipeline = VoicePipeline(session, stt, llm, cfg)
            try:
                answer = await pipeline.run_evenai(_exchange())
            finally:
                await pipeline.close()
            assert firmware.evenai_asks == ["fake transcript of 1.0s audio"]
            assert firmware.evenai_replies == ["echo: fake transcript of 1.0s audio"]
            assert answer == "echo: fake transcript of 1.0s audio"
            # Short sentence-less answer: the stream never opens — wire shape
            # is byte-identical to pre-streaming builds.
            assert firmware.evenai_reply_parts == []
            assert not firmware.evenai_reply_ended
            # Native window IS the surface: the C0 targets stay untouched.
            assert firmware.oled_texts == [] and firmware.g2_texts == []
            # The client never started the recording, and it cleaned up.
            assert not any(c.startswith("micrecord start")
                           for c in firmware.command_log)
            assert firmware.deleted, "wake WAV cleaned up"
        finally:
            transport.close()
    run(main())


def test_evenai_daemon_end_to_end(firmware):
    """The whole Phase 2 loop: EVT push while the daemon idles -> pump routes
    it -> job -> exchange -> native reply."""
    firmware.wav_bytes = make_wav(seconds=1.0)

    async def main():
        transport, session = open_link(firmware)
        pipeline = None
        tasks: list[asyncio.Task] = []
        try:
            await session.login()
            trigger = ManualTrigger()
            session.on_event = lambda payload: route_link_event(payload, trigger)
            pipeline = VoicePipeline(session, FakeSTT(), FakeLlm(), Config())
            tasks = [asyncio.create_task(pipeline.daemon(trigger)),
                     asyncio.create_task(session.pump_events())]
            firmware.begin_wake_capture()             # pushes evenai_wake
            for _ in range(200):
                if firmware.evenai_replies:
                    break
                await asyncio.sleep(0.05)
            assert firmware.evenai_asks == ["fake transcript of 1.0s audio"]
            assert firmware.evenai_replies == ["echo: fake transcript of 1.0s audio"]
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with suppress(asyncio.CancelledError):
                    await t
            if pipeline is not None:
                await pipeline.close()
            transport.close()
    run(main())


def test_evenai_long_answer_streams_in_parts(firmware):
    """Multi-sentence answers stream as replypart deltas + replyend, with the
    inter-chunk glue spaces surviving verbatim; no one-shot reply is sent."""
    long_answer = ("Halloween falls on October 31st every year. "
                   "It began as the ancient festival of Samhain. "
                   "People carve pumpkins and wear costumes. "
                   "Children go door to door collecting candy.")

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            firmware.begin_wake_capture(push=False)
            pipeline = VoicePipeline(
                session, FakeSTT(), _WordyLlm(long_answer), Config())
            try:
                answer = await pipeline.run_evenai(_exchange())
            finally:
                await pipeline.close()
            assert answer == long_answer
            assert firmware.evenai_replies == []           # never one-shot
            assert len(firmware.evenai_reply_parts) >= 2   # actually streamed
            assert firmware.evenai_reply_ended
            reconstructed = "".join(firmware.evenai_reply_parts)
            assert " ".join(reconstructed.split()) == long_answer
        finally:
            transport.close()
    run(main())


def test_evenai_ask_overlaps_generation_but_still_precedes_the_reply(firmware):
    """The `g2evenai ask` round trip runs alongside generation instead of ahead
    of it — but the question must still be ON the lens before the first reply
    chunk replaces it. Asserted on wire ORDER, which is the property that makes
    the overlap safe; the latency win itself is a host-side measurement.

    The terminal command is now exchange-owned and completes before return.
    """
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            firmware.begin_wake_capture(push=False)
            pipeline = VoicePipeline(
                session, FakeSTT(),
                _WordyLlm("One sentence here. And a second sentence here. "
                          "And a third sentence here."),
                Config())
            try:
                await pipeline.run_evenai(_exchange())
                mid = list(firmware.command_log)
            finally:
                await pipeline.close()     # drains backgrounded work

            def idx(log, needle):
                return next(i for i, ln in enumerate(log) if ln.startswith(needle))

            assert idx(mid, "g2evenai ask") < idx(mid, "g2evenai replypart")
            assert firmware.evenai_reply_ended
            # replyend is now owned and drained by this exchange, not a global
            # fire-and-forget tail that can bleed into the next session.
            assert any(ln.startswith("g2evenai replyendid") for ln in mid)
        finally:
            transport.close()
    run(main())


def test_evenai_empty_transcript_nags_only_in_open_session(firmware):
    """Silence heard + session still open -> a gentle native nag."""
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            firmware.begin_wake_capture(push=False)   # evenai_active = True
            pipeline = VoicePipeline(session, _SilentSTT(), FakeLlm(), Config())
            try:
                answer = await pipeline.run_evenai(_exchange())
            finally:
                await pipeline.close()
            assert answer == ""
            assert firmware.evenai_asks == []
            assert firmware.evenai_replies == ["Sorry, I didn't catch that."]
        finally:
            transport.close()
    run(main())


def test_evenai_empty_transcript_stays_silent_after_exit(firmware):
    """Silence heard + session already EXITed -> nothing is sent; a reply
    would reopen the card as a zombie popup after the user walked away."""
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            firmware.begin_wake_capture(push=False)
            firmware.evenai_active = False            # glasses ended it
            exchange = _exchange()
            exchange.cancel("dismiss")
            pipeline = VoicePipeline(session, _SilentSTT(), FakeLlm(), Config())
            try:
                try:
                    await pipeline.run_evenai(exchange)
                except EvenAiCancelled:
                    answer = ""
            finally:
                await pipeline.close()
            assert answer == ""
            assert firmware.evenai_asks == []
            assert firmware.evenai_replies == []
        finally:
            transport.close()
    run(main())
