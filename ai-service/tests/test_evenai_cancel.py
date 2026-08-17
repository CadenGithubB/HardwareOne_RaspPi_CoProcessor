from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from contextlib import suppress
from types import SimpleNamespace

import pytest

from conftest import open_link, run
from fake_firmware import TEST_EVENAI_ID

from hw1_ai_service.audio import fetch
from hw1_ai_service.config import Config
from hw1_ai_service.evenai_protocol import reply_command
from hw1_ai_service.jobs import (
    EvenAiCancelled, EvenAiExchange, Job, ManualTrigger, route_link_event)
from hw1_ai_service.link.session import CommandCancelled, CommandTimeout
from hw1_ai_service.pipeline import VoicePipeline, _EvenAiDelivery
from hw1_ai_service.stt.fake import FakeSTT


NEXT_ID = "a1b2c3d400000002"


async def _wait_for_log(caplog, needle: str, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while needle not in caplog.text:
        assert asyncio.get_running_loop().time() < deadline, needle
        await asyncio.sleep(0.005)


class _BlockingLlm:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.commits: list[tuple[str, str]] = []

    async def ask_stream(self, _prompt: str, *, commit_history: bool = True):
        assert not commit_history
        self.started.set()
        await self.release.wait()
        yield "This answer must never be shown."

    def commit_turn(self, prompt: str, answer: str) -> None:
        self.commits.append((prompt, answer))


class _FirstPartThenBlockLlm:
    def __init__(self) -> None:
        self.waiting_after_opener = asyncio.Event()
        self.release = asyncio.Event()
        self.commits: list[tuple[str, str]] = []

    async def ask_stream(self, _prompt: str, *, commit_history: bool = True):
        assert not commit_history
        yield "This opening sentence is deliberately longer than thirty characters. "
        self.waiting_after_opener.set()
        await self.release.wait()
        yield "This second sentence must never reach the glasses."

    def commit_turn(self, prompt: str, answer: str) -> None:
        self.commits.append((prompt, answer))


class _ImmediateLlm:
    def __init__(self) -> None:
        self.commits: list[tuple[str, str]] = []

    async def ask_stream(self, _prompt: str, *, commit_history: bool = True):
        assert not commit_history
        yield "A short answer."

    def commit_turn(self, prompt: str, answer: str) -> None:
        self.commits.append((prompt, answer))


class _BlockingSTT:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def transcribe(self, _pcm: bytes, _rate: int) -> str:
        self.started.set()
        assert self.release.wait(5)
        return "a transcript completed after dismissal"


class _ExplodingSTT:
    def transcribe(self, _pcm: bytes, _rate: int) -> str:
        raise RuntimeError("simulated STT failure")


class _NoRebootSession:
    reboot_suspected = False


class _SplitPartSession:
    """Hold physical part two so the first-part cue can be inspected."""

    reboot_suspected = False

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.second_entered = asyncio.Event()
        self.release_second = asyncio.Event()

    async def command(self, line: str, **_kwargs):
        self.calls.append(line)
        if len(self.calls) == 2:
            self.second_entered.set()
            await self.release_second.wait()
        return SimpleNamespace(ok=True, text="OK")


class _RecordingSession:
    reboot_suspected = False

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def command(self, line: str, **_kwargs):
        self.calls.append(line)
        return SimpleNamespace(ok=True, text="OK")


class _TimeoutCleanupSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float, bool]] = []

    async def command(self, line: str, *, timeout: float,
                      replay: bool, **_kwargs):
        self.calls.append((line, timeout, replay))
        await asyncio.sleep(timeout)
        raise CommandTimeout("simulated missing cleanup ACK")


class _BlockingAbortSession:
    reboot_suspected = False

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.never = asyncio.Event()

    async def command(self, line: str, **_kwargs):
        assert line == f"g2evenai exitid {TEST_EVENAI_ID}"
        self.entered.set()
        await self.never.wait()


class _PowerLeaseProbe:
    def __init__(self) -> None:
        self.started = 0
        self.finished = 0

    async def activity_started(self) -> None:
        self.started += 1

    async def activity_finished(self) -> None:
        self.finished += 1


def test_tap_window_measures_with_markers_off(caplog):
    """New contract (ask-display plan item 3): stage windows ALWAYS measure;
    only the wearer '>>> TAP NOW <<<' markers stay behind the interval flag.
    The END line logs at INFO in this mode (WARNING is marker/triage mode)."""
    async def main():
        pipeline = VoicePipeline(_NoRebootSession(), None, None, Config())
        exchange = EvenAiExchange(TEST_EVENAI_ID)
        try:
            window = pipeline._start_tap_window(exchange, "question")
            assert window is not None and window.task is not None
            await asyncio.sleep(0.02)
            pipeline._stop_tap_window(window, "before_first_reply_write")
            await asyncio.wait_for(window.task, 0.25)
            assert "TAP NOW" not in caplog.text  # markers stay opt-in
            assert caplog.text.count("<<< TAP WINDOW END >>>") == 1
            assert "stage=question" in caplog.text
            assert "outcome=before_first_reply_write" in caplog.text
            end_recs = [r for r in caplog.records
                        if "TAP WINDOW END" in r.message]
            assert end_recs and all(
                r.levelno == logging.INFO for r in end_recs)
        finally:
            await exchange.drain_tasks()
            await pipeline.close()

    caplog.set_level(logging.INFO, logger="pipeline")
    run(main())


def test_tap_marker_repeats_and_logs_measured_natural_stop(caplog):
    async def main():
        pipeline = VoicePipeline(
            _NoRebootSession(), None, None, Config(),
            cancel_marker_interval_s=0.05)
        exchange = EvenAiExchange(TEST_EVENAI_ID)
        try:
            window = pipeline._start_tap_window(exchange, "question")
            assert window is not None
            await asyncio.sleep(0.13)
            pipeline._stop_tap_window(window, "before_first_reply_write")
            assert window.task is not None
            await asyncio.wait_for(window.task, 0.25)
            text = caplog.text
            assert text.count(">>> TAP NOW <<<") >= 3
            assert text.count("<<< TAP WINDOW END >>>") == 1
            assert "stage=question" in text
            assert "start_ns=" in text
            assert "stop_ns=" in text
            assert "elapsed_ms=" in text
            assert "outcome=before_first_reply_write" in text
            start_ns = int(re.search(r"start_ns=(\d+)", text).group(1))
            end = re.search(
                r"stop_ns=(\d+) elapsed_ms=(\d+) "
                r"outcome=before_first_reply_write",
                text)
            assert end is not None
            stop_ns, elapsed_ms = map(int, end.groups())
            assert abs(round((stop_ns - start_ns) / 1_000_000) - elapsed_ms) <= 1
        finally:
            await exchange.drain_tasks()
            await pipeline.close()

    caplog.set_level(logging.WARNING, logger="pipeline")
    run(main())


def test_tap_marker_cancel_wakes_immediately_without_waiting_for_interval(caplog):
    async def main():
        pipeline = VoicePipeline(
            _NoRebootSession(), None, None, Config(),
            cancel_marker_interval_s=10.0)
        exchange = EvenAiExchange(TEST_EVENAI_ID)
        try:
            window = pipeline._start_tap_window(exchange, "stt")
            assert window is not None and window.task is not None
            await asyncio.sleep(0)
            exchange.cancel("dismiss")
            await asyncio.wait_for(window.task, 0.25)
            assert "stage=stt" in caplog.text
            assert "outcome=cancel:dismiss" in caplog.text
            assert caplog.text.count("<<< TAP WINDOW END >>>") == 1
        finally:
            await exchange.drain_tasks()
            await pipeline.close()

    caplog.set_level(logging.WARNING, logger="pipeline")
    run(main())


def test_tap_marker_natural_stop_does_not_wait_for_long_interval(caplog):
    async def main():
        pipeline = VoicePipeline(
            _NoRebootSession(), None, None, Config(),
            cancel_marker_interval_s=10.0)
        exchange = EvenAiExchange(TEST_EVENAI_ID)
        try:
            window = pipeline._start_tap_window(exchange, "question")
            assert window is not None
            await asyncio.sleep(0)
            started = time.monotonic()
            pipeline._stop_tap_window(window, "before_first_reply_write")
            assert time.monotonic() - started < 0.25
            assert window.task is not None
            await asyncio.wait_for(window.task, 0.25)
            assert "outcome=before_first_reply_write" in caplog.text
        finally:
            await exchange.drain_tasks()
            await pipeline.close()

    caplog.set_level(logging.WARNING, logger="pipeline")
    run(main())


def test_answer_marker_starts_after_first_physical_part_not_logical_delta(
        caplog):
    async def main():
        session = _SplitPartSession()
        pipeline = VoicePipeline(
            session, None, None, Config(), cancel_marker_interval_s=10.0)
        exchange = EvenAiExchange(TEST_EVENAI_ID)
        delivery = _EvenAiDelivery()
        task = asyncio.create_task(
            pipeline._send_part("🙂" * 60, exchange, delivery))
        try:
            await asyncio.wait_for(session.second_entered.wait(), 0.25)
            assert len(session.calls) == 2
            assert "stage=answer_tail window=start" in caplog.text
            session.release_second.set()
            first_paint = await asyncio.wait_for(task, 0.25)
            assert first_paint is not None
            tap, delivery.answer_tap = delivery.answer_tap, None
            pipeline._stop_tap_window(tap, "before_replyend_attempt")
            assert tap is not None and tap.task is not None
            await asyncio.wait_for(tap.task, 0.25)
        finally:
            session.release_second.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await exchange.drain_tasks()
            await pipeline.close()

    caplog.set_level(logging.WARNING, logger="pipeline")
    run(main())


def test_enabled_markers_do_not_change_successful_multipart_wire_commands():
    async def deliver(interval: float) -> list[str]:
        session = _RecordingSession()
        pipeline = VoicePipeline(
            session, None, None, Config(),
            cancel_marker_interval_s=interval)
        exchange = EvenAiExchange(TEST_EVENAI_ID)
        delivery = _EvenAiDelivery()
        try:
            await pipeline._send_reply_whole(
                "word " * 100, exchange, delivery)
            await exchange.drain_tasks()
            return session.calls
        finally:
            await exchange.drain_tasks()
            await pipeline.close()

    assert run(deliver(0.0)) == run(deliver(0.05))


def test_cancel_before_wake_is_a_sticky_bounded_tombstone():
    async def main():
        trigger = ManualTrigger()
        route_link_event(
            f"evenai_cancel {TEST_EVENAI_ID} dismiss".encode(), trigger)
        route_link_event(f"evenai_wake {TEST_EVENAI_ID}".encode(), trigger)
        assert trigger._queue.empty()
        assert TEST_EVENAI_ID in trigger._evenai_tombstones

        # A different correlated capture remains valid.
        route_link_event(f"evenai_wake {NEXT_ID}".encode(), trigger)
        assert (await trigger.next_job()).exchange.exchange_id == NEXT_ID
    run(main())


def test_evenai_priority_slot_jumps_pending_manual_fifo_without_reordering_it():
    async def main():
        trigger = ManualTrigger()
        trigger.submit(Job("ask"))
        trigger.submit(Job("chat", "first"))
        trigger.submit(Job("chat", "second"))
        exchange = trigger.submit_evenai(TEST_EVENAI_ID)
        assert exchange is not None

        assert (await trigger.next_job()).exchange is exchange
        trigger.evenai_done(TEST_EVENAI_ID)
        assert (await trigger.next_job()).kind == "ask"
        assert (await trigger.next_job()).text == "first"
        assert (await trigger.next_job()).text == "second"

    run(main())


def test_late_s1_cancel_and_completion_cannot_touch_s2():
    async def main():
        trigger = ManualTrigger()
        s1 = trigger.submit_evenai(TEST_EVENAI_ID)
        assert s1 is not None
        assert (await trigger.next_job()).exchange is s1

        s2 = trigger.submit_evenai(NEXT_ID)
        assert s2 is not None
        assert s1.cancelled and s1.cancel_reason == "superseded"
        trigger.cancel_evenai(TEST_EVENAI_ID, "dismiss")
        trigger.evenai_done(TEST_EVENAI_ID)

        assert not s2.cancelled
        assert trigger._evenai[NEXT_ID] is s2
        assert (await trigger.next_job()).exchange is s2

    run(main())


def test_duplicate_wake_and_cancel_are_idempotent_and_terminal():
    async def main():
        trigger = ManualTrigger()
        first = trigger.submit_evenai(TEST_EVENAI_ID)
        assert first is not None
        assert trigger.submit_evenai(TEST_EVENAI_ID) is first
        assert trigger._queue.qsize() == 1

        trigger.cancel_evenai(TEST_EVENAI_ID, "dismiss")
        trigger.cancel_evenai(TEST_EVENAI_ID, "disconnect")
        assert first.cancel_reason == "dismiss"
        # next_job consumes the one cancelled queue entry and tombstones it.
        waiter = asyncio.create_task(trigger.next_job())
        await asyncio.sleep(0)
        assert not waiter.done()
        waiter.cancel()
        with suppress(asyncio.CancelledError):
            await waiter
        assert trigger.submit_evenai(TEST_EVENAI_ID) is None
        assert trigger._queue.empty()

    run(main())


def test_timed_out_owned_cleanup_releases_registry_and_power(monkeypatch):
    async def main():
        source = ManualTrigger()
        exchange = source.submit_evenai(TEST_EVENAI_ID)
        assert exchange is not None
        job = await source.next_job()
        cleanup_session = _TimeoutCleanupSession()
        power = _PowerLeaseProbe()
        pipeline = VoicePipeline(
            _NoRebootSession(), FakeSTT(), None, Config(),
            power_activity=power)
        monkeypatch.setattr(fetch, "_EVENAI_CLEANUP_TIMEOUT_S", 0.01)

        async def run_evenai(current):
            current.start_task(
                fetch._cleanup(
                    cleanup_session, "/recordings/owned.wav",
                    current.exchange_id),
                name="cleanup-timeout-probe")
            current.mark_delivered()
            return ""

        pipeline.run_evenai = run_evenai
        try:
            await asyncio.wait_for(pipeline._dispatch(job, source), 0.5)
        finally:
            await pipeline.close()

        assert cleanup_session.calls == [(
            f'micdeleteid {TEST_EVENAI_ID} "owned.wav"', 0.01, False)]
        assert TEST_EVENAI_ID not in source._evenai
        assert power.started == power.finished == 1

    run(main())


def test_stale_wake_sends_tagged_exit_before_registry_release(firmware):
    async def main():
        transport, session = open_link(firmware)
        pipeline = None
        try:
            await session.login()
            source = ManualTrigger()
            exchange = source.submit_evenai(TEST_EVENAI_ID)
            assert exchange is not None
            job = await source.next_job()
            job.created -= 30.0
            firmware.begin_wake_capture(push=False)
            pipeline = VoicePipeline(session, FakeSTT(), None, Config())

            await pipeline._dispatch(job, source)

            assert not firmware.evenai_active
            assert f"g2evenai exitid {TEST_EVENAI_ID}" in firmware.command_log
            assert TEST_EVENAI_ID not in source._evenai
        finally:
            if pipeline is not None:
                await pipeline.close()
            transport.close()

    run(main())


def test_stt_disabled_wake_sends_tagged_exit(firmware):
    async def main():
        transport, session = open_link(firmware)
        pipeline = None
        try:
            await session.login()
            source = ManualTrigger()
            exchange = source.submit_evenai(TEST_EVENAI_ID)
            assert exchange is not None
            job = await source.next_job()
            firmware.begin_wake_capture(push=False)
            pipeline = VoicePipeline(session, None, None, Config())

            await pipeline._dispatch(job, source)

            assert not firmware.evenai_active
            assert f"g2evenai exitid {TEST_EVENAI_ID}" in firmware.command_log
            assert firmware.evenai_asks == firmware.evenai_replies == []
        finally:
            if pipeline is not None:
                await pipeline.close()
            transport.close()

    run(main())


def test_stt_exception_before_display_sends_tagged_exit(firmware, caplog):
    async def main():
        transport, session = open_link(firmware)
        pipeline = None
        try:
            await session.login()
            source = ManualTrigger()
            exchange = source.submit_evenai(TEST_EVENAI_ID)
            assert exchange is not None
            job = await source.next_job()
            firmware.begin_wake_capture(push=False)
            pipeline = VoicePipeline(
                session, _ExplodingSTT(), None, Config(),
                cancel_marker_interval_s=10.0)

            with pytest.raises(RuntimeError, match="simulated STT failure"):
                await pipeline._dispatch(job, source)

            assert not firmware.evenai_active
            assert f"g2evenai exitid {TEST_EVENAI_ID}" in firmware.command_log
            assert firmware.evenai_asks == firmware.evenai_replies == []
            assert TEST_EVENAI_ID not in source._evenai
            assert "stage=stt" in caplog.text
            assert "outcome=interrupted" in caplog.text
        finally:
            if pipeline is not None:
                await pipeline.close()
            transport.close()

    caplog.set_level(logging.WARNING, logger="pipeline")
    run(main())


def test_fetch_exception_before_display_sends_tagged_exit(firmware, monkeypatch):
    async def fail_fetch(_session, _cfg, _exchange):
        raise fetch.FetchError("simulated owner failure")

    monkeypatch.setattr(fetch, "fetch_wake_utterance", fail_fetch)

    async def main():
        transport, session = open_link(firmware)
        pipeline = None
        try:
            await session.login()
            source = ManualTrigger()
            exchange = source.submit_evenai(TEST_EVENAI_ID)
            assert exchange is not None
            job = await source.next_job()
            firmware.begin_wake_capture(push=False)
            pipeline = VoicePipeline(session, FakeSTT(), None, Config())

            with pytest.raises(fetch.FetchError, match="owner failure"):
                await pipeline._dispatch(job, source)

            assert not firmware.evenai_active
            assert f"g2evenai exitid {TEST_EVENAI_ID}" in firmware.command_log
            assert firmware.evenai_asks == firmware.evenai_replies == []
            assert TEST_EVENAI_ID not in source._evenai
        finally:
            if pipeline is not None:
                await pipeline.close()
            transport.close()

    run(main())


def test_successful_dispatch_does_not_send_terminal_abort(firmware):
    async def main():
        transport, session = open_link(firmware)
        pipeline = None
        try:
            await session.login()
            source = ManualTrigger()
            exchange = source.submit_evenai(TEST_EVENAI_ID)
            assert exchange is not None
            job = await source.next_job()
            firmware.begin_wake_capture(push=False)
            cfg = Config()
            cfg.deliver.g2_ask_render_cps = 1000.0
            pipeline = VoicePipeline(session, FakeSTT(), None, cfg)

            await pipeline._dispatch(job, source)

            assert exchange.delivered
            assert firmware.evenai_replies
            assert not any(c.startswith("g2evenai exitid ")
                           for c in firmware.command_log)
        finally:
            if pipeline is not None:
                await pipeline.close()
            transport.close()

    run(main())


def test_external_stream_parent_cancel_closes_active_iterator():
    async def main():
        entered = asyncio.Event()
        closed = asyncio.Event()
        never = asyncio.Event()

        async def stream():
            try:
                entered.set()
                await never.wait()
                yield "unreachable"
            finally:
                closed.set()

        pipeline = VoicePipeline(
            _NoRebootSession(), FakeSTT(), None, Config())
        exchange = ManualTrigger().submit_evenai(TEST_EVENAI_ID)
        assert exchange is not None

        async def consume():
            async for _piece in pipeline._cancel_aware_stream(
                    stream(), exchange):
                pass

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            assert closed.is_set()
        finally:
            await pipeline.close()

    run(main())


def test_parent_cancel_during_abort_still_releases_power_and_registry():
    async def main():
        session = _BlockingAbortSession()
        power = _PowerLeaseProbe()
        source = ManualTrigger()
        exchange = source.submit_evenai(TEST_EVENAI_ID)
        assert exchange is not None
        job = await source.next_job()
        pipeline = VoicePipeline(
            session, FakeSTT(), None, Config(), power_activity=power)

        async def incomplete(_exchange):
            return ""

        pipeline.run_evenai = incomplete
        task = asyncio.create_task(pipeline._dispatch(job, source))
        try:
            await asyncio.wait_for(session.entered.wait(), 1)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            assert TEST_EVENAI_ID not in source._evenai
            assert power.started == power.finished == 1
        finally:
            await pipeline.close()

    run(main())


@pytest.mark.parametrize("payload", [
    b"evenai_wake",
    b"evenai_wake xyz",
    b"evenai_cancel a1b2c3d400000001",
    b"evenai_cancel a1b2c3d400000001 BAD!",
    b"mic_autostop a1b2c3d400000001 relative.wav",
])
def test_malformed_correlated_event_never_queues(payload):
    async def main():
        trigger = ManualTrigger()
        route_link_event(payload, trigger)
        assert trigger._queue.empty()
        assert not trigger._evenai
    run(main())


@pytest.mark.parametrize("reason", [
    "host_link_lost_runtime",
    "host_link_lost_never",
    "host_link_lost_cleared",
    "host_link_lost_epoch",
])
def test_host_link_loss_reason_variants_cancel_exact_exchange(reason):
    """Firmware diagnostics stay valid cancellation reasons end to end."""
    async def main():
        trigger = ManualTrigger()
        exchange = trigger.submit_evenai(TEST_EVENAI_ID)
        assert exchange is not None

        route_link_event(
            f"evenai_cancel {TEST_EVENAI_ID} {reason}".encode(), trigger)

        assert exchange.cancelled
        assert exchange.cancel_reason == reason
        assert trigger._evenai_tombstones[TEST_EVENAI_ID] == reason

    run(main())


def test_prewrite_guard_routes_queued_cancel_before_mutation(firmware):
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            firmware.begin_wake_capture(push=False)
            trigger = ManualTrigger()
            exchange = trigger.submit_evenai(TEST_EVENAI_ID)
            assert exchange is not None
            session.on_event = lambda payload: route_link_event(payload, trigger)

            firmware.push_event(
                f"evenai_cancel {TEST_EVENAI_ID} dismiss")
            for _ in range(100):
                if not transport.rx.empty():
                    break
                await asyncio.sleep(0.005)
            assert not transport.rx.empty()  # queued; no pump consumed it
            command = reply_command(TEST_EVENAI_ID, "zombie")
            with pytest.raises(CommandCancelled):
                await session.command(
                    command, expect="status", replay=False,
                    cancel_guard=lambda: exchange.cancelled)
            assert command not in firmware.command_log
        finally:
            transport.close()
    run(main())


def test_login_prewrite_guard_observes_cancel_routed_by_stale_drain(firmware):
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            trigger = ManualTrigger()
            exchange = trigger.submit_evenai(TEST_EVENAI_ID)
            assert exchange is not None
            session.on_event = lambda payload: route_link_event(payload, trigger)

            login_count = sum(c.startswith("login ") for c in firmware.command_log)
            firmware.push_event(
                f"evenai_cancel {TEST_EVENAI_ID} dismiss")
            for _ in range(100):
                if not transport.rx.empty():
                    break
                await asyncio.sleep(0.005)
            assert not transport.rx.empty()

            with pytest.raises(CommandCancelled):
                await session._login_locked(
                    cancel_guard=lambda: exchange.cancelled)

            assert exchange.cancelled
            assert sum(c.startswith("login ") for c in firmware.command_log) == login_count
        finally:
            transport.close()

    run(main())


def test_cancel_during_written_command_drains_ack_before_next_command(firmware):
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            firmware.begin_wake_capture(push=False)
            trigger = ManualTrigger()
            exchange = trigger.submit_evenai(TEST_EVENAI_ID)
            assert exchange is not None
            session.on_event = lambda payload: route_link_event(payload, trigger)
            command = reply_command(TEST_EVENAI_ID, "in flight")
            firmware.delay_once[command] = 0.2

            task = asyncio.create_task(session.command(
                command, expect="status", replay=False,
                cancel_guard=lambda: exchange.cancelled))
            while command not in firmware.command_log:
                await asyncio.sleep(0.005)
            firmware.push_event(
                f"evenai_cancel {TEST_EVENAI_ID} dismiss")
            with pytest.raises(CommandCancelled):
                await task

            # The delayed reply was drained by the cancelled command and cannot
            # be mistaken for this next request.
            rep = await session.command("uartlink status", expect="status")
            assert rep.ok and "UART link" in rep.text
        finally:
            transport.close()
    run(main())


def test_cancel_during_timeout_skips_relogin_and_replay(firmware):
    """A dismissed exchange must not sit through the timeout recovery login.

    The original write is still allowed to reach its own timeout boundary, so
    the untagged reply stream is not abandoned. The cancellation guard then
    wins before any login write or replay can be admitted.
    """
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            firmware.begin_wake_capture(push=False)
            trigger = ManualTrigger()
            exchange = trigger.submit_evenai(TEST_EVENAI_ID)
            assert exchange is not None
            session.on_event = lambda payload: route_link_event(payload, trigger)
            command = reply_command(TEST_EVENAI_ID, "will time out")
            firmware.delay_once[command] = 0.25
            login_count = sum(c.startswith("login ")
                              for c in firmware.command_log)

            async def dismiss_in_flight():
                while command not in firmware.command_log:
                    await asyncio.sleep(0.002)
                firmware.push_event(
                    f"evenai_cancel {TEST_EVENAI_ID} dismiss")

            dismiss = asyncio.create_task(dismiss_in_flight())
            with pytest.raises(CommandCancelled):
                await session.command(
                    command, expect="status", timeout=0.08, replay=True,
                    cancel_guard=lambda: exchange.cancelled)
            await dismiss
            assert sum(c.startswith("login ")
                       for c in firmware.command_log) == login_count
            assert firmware.command_log.count(command) == 1
        finally:
            transport.close()
    run(main())


def test_voicefetch_cancel_discards_remaining_frames_and_drains_status(firmware):
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            path = "/recordings/cancel-stream.wav"
            firmware.files[path] = b"x" * (128 * 1024)
            firmware.voicefetch_frame_delay_s = 0.001
            firmware.voicefetch_event_after_frames = (
                8, f"evenai_cancel {TEST_EVENAI_ID} dismiss")
            trigger = ManualTrigger()
            exchange = trigger.submit_evenai(TEST_EVENAI_ID)
            assert exchange is not None
            session.on_event = lambda payload: route_link_event(payload, trigger)

            task = asyncio.create_task(fetch.fetch_frames(
                session, path, cancel_guard=lambda: exchange.cancelled))
            with pytest.raises(CommandCancelled):
                await asyncio.wait_for(task, 5)
            assert exchange.cancelled
            # The cancelled collector drained every remaining binary frame and
            # the terminal status; no stream tail can poison this reply.
            rep = await session.command("uartlink status", expect="status")
            assert rep.ok and "UART link" in rep.text
        finally:
            transport.close()

    run(main())


def test_dismiss_during_llm_closes_stream_and_rolls_back_history(firmware):
    async def main():
        transport, session = open_link(firmware)
        pump = None
        pipeline = None
        try:
            await session.login()
            trigger = ManualTrigger()
            exchange = trigger.submit_evenai(TEST_EVENAI_ID)
            assert exchange is not None
            session.on_event = lambda payload: route_link_event(payload, trigger)
            pump = asyncio.create_task(session.pump_events())
            firmware.begin_wake_capture(push=False)
            llm = _BlockingLlm()
            pipeline = VoicePipeline(session, FakeSTT(), llm, Config())
            task = asyncio.create_task(pipeline.run_evenai(exchange))
            await asyncio.wait_for(llm.started.wait(), 5)
            firmware.dismiss_evenai()
            with pytest.raises(EvenAiCancelled):
                await asyncio.wait_for(task, 5)
            await exchange.drain_tasks()
            assert firmware.evenai_replies == []
            assert firmware.evenai_reply_parts == []
            assert llm.commits == []
        finally:
            if pump is not None:
                pump.cancel()
                with suppress(asyncio.CancelledError):
                    await pump
            if pipeline is not None:
                await pipeline.close()
            transport.close()
    run(main())


def test_dismiss_after_first_replypart_stops_tail_and_rolls_back_history(
        firmware, caplog):
    async def main():
        transport, session = open_link(firmware)
        pump = None
        pipeline = None
        exchange = None
        llm = _FirstPartThenBlockLlm()
        try:
            await session.login()
            trigger = ManualTrigger()
            exchange = trigger.submit_evenai(TEST_EVENAI_ID)
            assert exchange is not None
            session.on_event = lambda payload: route_link_event(payload, trigger)
            pump = asyncio.create_task(session.pump_events())
            firmware.begin_wake_capture(push=False)
            cfg = Config()
            cfg.deliver.g2_ask_render_cps = 1000.0
            pipeline = VoicePipeline(
                session, FakeSTT(), llm, cfg,
                cancel_marker_interval_s=0.05)
            task = asyncio.create_task(pipeline.run_evenai(exchange))

            await asyncio.wait_for(llm.waiting_after_opener.wait(), 5)
            while not firmware.evenai_reply_parts:
                await asyncio.sleep(0.005)
            assert len(firmware.evenai_reply_parts) == 1
            await _wait_for_log(
                caplog, "stage=answer_tail window=start")
            firmware.dismiss_evenai()
            with pytest.raises(EvenAiCancelled):
                await asyncio.wait_for(task, 5)
            await exchange.drain_tasks()

            assert len(firmware.evenai_reply_parts) == 1
            assert not firmware.evenai_reply_ended
            assert not any(c.startswith("g2evenai replyendid")
                           for c in firmware.command_log)
            assert llm.commits == []
            assert ">>> TAP NOW <<<" in caplog.text
            assert "stage=answer_tail" in caplog.text
            assert "outcome=cancel:dismiss" in caplog.text
            marker_lines = [line for line in caplog.text.splitlines()
                            if "answer_tail" in line]
            assert sum("TAP WINDOW END" in line for line in marker_lines) == 1
            end_at = next(i for i, line in enumerate(marker_lines)
                          if "TAP WINDOW END" in line)
            assert not any(">>> TAP NOW <<<" in line
                           for line in marker_lines[end_at + 1:])
        finally:
            llm.release.set()
            if exchange is not None:
                await exchange.drain_tasks()
            if pump is not None:
                pump.cancel()
                with suppress(asyncio.CancelledError):
                    await pump
            if pipeline is not None:
                await pipeline.close()
            transport.close()

    caplog.set_level(logging.WARNING, logger="pipeline")
    run(main())


def test_dismiss_during_ask_render_hold_never_writes_first_reply(
        firmware, caplog):
    async def main():
        transport, session = open_link(firmware)
        pump = None
        pipeline = None
        exchange = None
        llm = _ImmediateLlm()
        try:
            await session.login()
            trigger = ManualTrigger()
            exchange = trigger.submit_evenai(TEST_EVENAI_ID)
            assert exchange is not None
            session.on_event = lambda payload: route_link_event(payload, trigger)
            pump = asyncio.create_task(session.pump_events())
            firmware.begin_wake_capture(push=False)
            cfg = Config()
            cfg.deliver.g2_ask_render_cps = 0.5  # long, cancellable barrier
            pipeline = VoicePipeline(
                session, FakeSTT(), llm, cfg,
                cancel_marker_interval_s=0.05)
            task = asyncio.create_task(pipeline.run_evenai(exchange))

            deadline = asyncio.get_running_loop().time() + 5
            while not firmware.evenai_asks:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.005)
            await _wait_for_log(caplog, "stage=question window=start")
            # ASK has ACKed and generation is complete; the unfinished task is
            # specifically waiting for the lens render deadline.
            await asyncio.sleep(0.05)
            assert not task.done()
            firmware.dismiss_evenai()
            with pytest.raises(EvenAiCancelled):
                await asyncio.wait_for(task, 5)
            await exchange.drain_tasks()

            assert firmware.evenai_replies == []
            assert firmware.evenai_reply_parts == []
            assert not any(c.startswith((
                "g2evenai replyid", "g2evenai replypartid",
                "g2evenai replyendid")) for c in firmware.command_log)
            assert llm.commits == []
            assert ">>> TAP NOW <<<" in caplog.text
            assert "stage=question" in caplog.text
            assert "outcome=cancel:dismiss" in caplog.text
            marker_lines = [line for line in caplog.text.splitlines()
                            if "stage=question" in line]
            assert sum("TAP WINDOW END" in line for line in marker_lines) == 1
            end_at = next(i for i, line in enumerate(marker_lines)
                          if "TAP WINDOW END" in line)
            assert not any(">>> TAP NOW <<<" in line
                           for line in marker_lines[end_at + 1:])
        finally:
            if exchange is not None:
                await exchange.drain_tasks()
            if pump is not None:
                pump.cancel()
                with suppress(asyncio.CancelledError):
                    await pump
            if pipeline is not None:
                await pipeline.close()
            transport.close()

    caplog.set_level(logging.WARNING, logger="pipeline")
    run(main())


def test_dismiss_during_cancel_opaque_stt_discards_completed_result(
        firmware, tmp_path):
    async def main():
        transport, session = open_link(firmware)
        pump = None
        pipeline = None
        stt = _BlockingSTT()
        try:
            await session.login()
            trigger = ManualTrigger()
            exchange = trigger.submit_evenai(TEST_EVENAI_ID)
            assert exchange is not None
            session.on_event = lambda payload: route_link_event(payload, trigger)
            pump = asyncio.create_task(session.pump_events())
            firmware.begin_wake_capture(push=False)
            cfg = Config()
            saved = tmp_path / "last-utterance.wav"
            cfg.audio.save_last_path = str(saved)
            pipeline = VoicePipeline(session, stt, _BlockingLlm(), cfg)
            task = asyncio.create_task(pipeline.run_evenai(exchange))
            assert await asyncio.to_thread(stt.started.wait, 5)
            firmware.dismiss_evenai()
            await asyncio.sleep(0.1)
            stt.release.set()
            with pytest.raises(EvenAiCancelled):
                await asyncio.wait_for(task, 5)
            assert firmware.evenai_asks == []
            assert firmware.evenai_replies == []
            assert firmware.evenai_reply_parts == []
            assert not saved.exists()
            assert not (tmp_path / "failed").exists()
        finally:
            stt.release.set()
            if pump is not None:
                pump.cancel()
                with suppress(asyncio.CancelledError):
                    await pump
            if pipeline is not None:
                await pipeline.close()
            transport.close()
    run(main())
