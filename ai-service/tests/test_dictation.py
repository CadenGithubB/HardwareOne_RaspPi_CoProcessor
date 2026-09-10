"""Keyboard-dictation protocol, actor, and routing regressions."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from conftest import open_link, run
from fake_firmware import FakeFirmware

from hw1_ai_service.audio import fetch
from hw1_ai_service.audio import wav
from hw1_ai_service.config import Config
from hw1_ai_service.dictation import (
    DictationController,
    DictationProtocolError,
    parse_cancel,
    parse_request,
    sanitize_transcript,
)
from hw1_ai_service.jobs import ManualTrigger, route_link_event
from hw1_ai_service.link.session import CommandTimeout, LinkClosed
from hw1_ai_service.pipeline import VoicePipeline


DICT_ID = "a1b2c3d400000001"
DICT_PATH = f"/sd/recordings/rec_{DICT_ID}.wav"
DICT_EVT = f"dictate_request {DICT_ID} {DICT_PATH}".encode()


class _Reply:
    def __init__(self, ok=True, text="OK") -> None:
        self.ok = ok
        self.text = text


class _Session:
    def __init__(self, *, capability=True) -> None:
        self.capability = capability
        self.calls: list[tuple[str, dict]] = []
        self.terminal = asyncio.Event()

    async def command(self, line: str, **kwargs):
        self.calls.append((line, kwargs))
        if line == "dictate hostready v1":
            return _Reply(self.capability, "OK" if self.capability else
                          "Error: invalid arguments")
        if line.startswith(("dictate result ", "dictate fail ")):
            self.terminal.set()
        return _Reply()


class _Pipeline:
    def __init__(self, text="hello world", *, available=True) -> None:
        self.batch_stt_available = available
        self.text = text
        self.calls: list[bytes] = []

    async def transcribe_dictation(self, wav_bytes: bytes) -> str:
        self.calls.append(wav_bytes)
        return self.text


class _Power:
    def __init__(self) -> None:
        self.started = self.finished = 0

    async def activity_started(self) -> None:
        self.started += 1

    async def activity_finished(self) -> None:
        self.finished += 1


class _Presence:
    def __init__(self) -> None:
        self.acquired: list[str] = []
        self.released: list[int] = []

    async def acquire_busy(self, reason: str) -> int:
        self.acquired.append(reason)
        return len(self.acquired)

    def release_busy(self, token: int) -> None:
        self.released.append(token)


@pytest.mark.parametrize("root", ["/recordings", "/sd/recordings"])
def test_request_parser_binds_canonical_path_to_nonzero_id(root):
    request = parse_request(
        f"dictate_request {DICT_ID.upper()} {root}/rec_{DICT_ID}.wav".encode())
    assert request.request_id == DICT_ID
    assert request.path == f"{root}/rec_{DICT_ID}.wav"


@pytest.mark.parametrize("event", [
    b"dictate_request",
    b"dictate_request 0000000000000001 /recordings/rec_0000000000000001.wav",
    b"dictate_request a1b2c3d400000000 /recordings/rec_a1b2c3d400000000.wav",
    f"dictate_request {DICT_ID} /tmp/rec_{DICT_ID}.wav".encode(),
    f"dictate_request {DICT_ID} /recordings/../rec_{DICT_ID}.wav".encode(),
    f"dictate_request {DICT_ID} /recordings/rec_a1b2c3d400000002.wav".encode(),
    DICT_EVT + b" extra",
    DICT_EVT + b"\n",
    DICT_EVT + b"\0",
    b"dictate_request \xff",
])
def test_request_parser_rejects_malformed_or_unbound_events(event):
    with pytest.raises(DictationProtocolError):
        parse_request(event)


def test_transcript_sanitizer_is_one_line_printable_ascii_and_bounded():
    assert sanitize_transcript(" hello\r\nworld\0  again ") == "hello world again"
    assert sanitize_transcript("\x00\x01\n") == ""
    assert sanitize_transcript("café — ‘ready’…") == "cafe - 'ready'..."
    assert sanitize_transcript("中文") == ""
    clipped = sanitize_transcript("é" * 300)
    assert len(clipped.encode("ascii")) == 256
    assert clipped == "e" * 256


def test_cancel_parser_and_cancel_before_request_tombstone():
    assert parse_cancel(f"dictate_cancel {DICT_ID}".encode()) == DICT_ID
    for malformed in (b"dictate_cancel", b"dictate_cancel 0", b"dictate_cancel " +
                      DICT_ID.encode() + b" extra"):
        with pytest.raises(DictationProtocolError):
            parse_cancel(malformed)

    controller = DictationController(_Session())
    assert controller.submit_event(f"dictate_cancel {DICT_ID}".encode())
    assert controller.submit_event(DICT_EVT)
    assert controller._queue.empty()


def test_router_consumes_dictation_before_generic_event_path():
    class Sink:
        def __init__(self) -> None:
            self.payloads = []

        def submit_event(self, payload) -> bool:
            self.payloads.append(payload)
            return True

    sink = Sink()
    route_link_event(DICT_EVT, ManualTrigger(), dictation=sink)
    assert sink.payloads == [DICT_EVT]


def test_controller_happy_path_balances_leases_and_never_deletes(monkeypatch):
    async def main() -> None:
        session = _Session()
        power = _Power()
        presence = _Presence()
        pipeline = _Pipeline(" hello\nfrom\0 glasses ")

        async def fake_fetch(_session, path, **kwargs):
            assert path == DICT_PATH
            assert not kwargs["cancel_guard"]()
            return b"canonical-wav"

        monkeypatch.setattr(fetch, "fetch_frames", fake_fetch)
        controller = DictationController(
            session, power=power, cm5_presence=presence)
        await controller.attach(pipeline)
        assert controller.submit_event(DICT_EVT)
        task = asyncio.create_task(controller.run())
        await asyncio.wait_for(session.terminal.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        terminal = [(line, kw) for line, kw in session.calls
                    if line.startswith("dictate result ")]
        assert len(terminal) == 1
        assert terminal[0][0] == (
            f"dictate result {DICT_ID} hello from glasses")
        assert terminal[0][1]["expect"] == "status"
        assert terminal[0][1]["timeout"] == 5.0
        assert terminal[0][1]["replay"] is False
        assert terminal[0][1]["auth_replay"] is False
        assert callable(terminal[0][1]["cancel_guard"])
        assert not any("micdelete" in line for line, _ in session.calls)
        assert pipeline.calls == [b"canonical-wav"]
        assert power.started == power.finished == 1
        assert len(presence.acquired) == len(presence.released) == 1

    run(main())


def test_controller_fails_fast_when_batch_stt_is_unavailable(monkeypatch):
    async def main() -> None:
        session = _Session()
        controller = DictationController(session)
        await controller.attach(_Pipeline(available=False))
        assert controller.submit_event(DICT_EVT)
        task = asyncio.create_task(controller.run())
        await asyncio.wait_for(session.terminal.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert any(line == f"dictate fail {DICT_ID} host_not_ready"
                   for line, _ in session.calls)

    run(main())


def test_mandatory_capability_rejection_never_runs_stt(monkeypatch):
    async def main() -> None:
        async def should_not_fetch(*_args, **_kwargs):
            raise AssertionError("capability-rejected controller fetched audio")

        monkeypatch.setattr(fetch, "fetch_frames", should_not_fetch)
        session = _Session(capability=False)
        pipeline = _Pipeline("must not run")
        controller = DictationController(session)
        await controller.attach(pipeline)
        controller.submit_event(DICT_EVT)
        task = asyncio.create_task(controller.run())
        await asyncio.wait_for(session.terminal.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert pipeline.calls == []
        assert any(line == f"dictate fail {DICT_ID} host_not_ready"
                   for line, _ in session.calls)

    run(main())


def test_capability_timeout_forces_reconnect_instead_of_limping():
    class TimeoutSession(_Session):
        async def command(self, line: str, **kwargs):
            if line == "dictate hostready v1":
                raise CommandTimeout("ambiguous capability reply")
            return await super().command(line, **kwargs)

    async def main() -> None:
        controller = DictationController(TimeoutSession())
        controller._pipeline = _Pipeline()
        with pytest.raises(LinkClosed, match="readiness lost UART"):
            await controller.run()

    run(main())


def test_successful_relogin_republishes_session_bound_capability():
    class Session(_Session):
        def __init__(self) -> None:
            super().__init__()
            self.listener = None

        def add_login_listener(self, listener) -> None:
            self.listener = listener

    async def main() -> None:
        session = Session()
        controller = DictationController(session)
        await controller.attach(_Pipeline())
        assert sum(line == "dictate hostready v1"
                   for line, _ in session.calls) == 1
        session.listener(2)
        deadline = asyncio.get_running_loop().time() + 1
        while sum(line == "dictate hostready v1"
                  for line, _ in session.calls) < 2:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("capability was not republished")
            await asyncio.sleep(0)
        await controller.close()

    run(main())


def test_dictation_and_pipeline_calls_share_one_stt_worker():
    class CountingStt:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.active = 0
            self.max_active = 0

        def transcribe(self, _pcm: bytes, _rate: int) -> str:
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.05)
                return "ok"
            finally:
                with self.lock:
                    self.active -= 1

    async def main() -> None:
        engine = CountingStt()
        pipeline = VoicePipeline(_Session(), engine, None, Config())
        capture = wav.build(b"\0\0" * 160, 16000)
        try:
            assert await asyncio.gather(
                pipeline.transcribe_dictation(capture),
                pipeline.transcribe_dictation(capture)) == ["ok", "ok"]
            assert engine.max_active == 1
        finally:
            await pipeline.close()

    run(main())


def test_link_reset_fences_a_late_native_stt_result(monkeypatch):
    async def main() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        class SlowPipeline(_Pipeline):
            async def transcribe_dictation(self, wav_bytes: bytes) -> str:
                entered.set()
                await release.wait()
                return "must not land"

        async def fake_fetch(*_args, **_kwargs):
            return b"wav"

        monkeypatch.setattr(fetch, "fetch_frames", fake_fetch)
        session = _Session()
        controller = DictationController(session)
        await controller.attach(SlowPipeline())
        controller.submit_event(DICT_EVT)
        task = asyncio.create_task(controller.run())
        await asyncio.wait_for(entered.wait(), 1)
        controller.link_reset()
        release.set()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not any(line.startswith(("dictate result ", "dictate fail "))
                       for line, _ in session.calls)

    run(main())


def test_device_cancel_during_stt_suppresses_late_terminal(monkeypatch):
    async def main() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        class SlowPipeline(_Pipeline):
            async def transcribe_dictation(self, wav_bytes: bytes) -> str:
                entered.set()
                await release.wait()
                return "must not land"

        async def fake_fetch(*_args, **_kwargs):
            return b"wav"

        monkeypatch.setattr(fetch, "fetch_frames", fake_fetch)
        session = _Session()
        controller = DictationController(session)
        await controller.attach(SlowPipeline())
        controller.submit_event(DICT_EVT)
        task = asyncio.create_task(controller.run())
        await asyncio.wait_for(entered.wait(), 1)
        assert controller.submit_event(f"dictate_cancel {DICT_ID}".encode())
        release.set()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not any(line.startswith(("dictate result ", "dictate fail "))
                       for line, _ in session.calls)

    run(main())


def test_real_session_event_fetch_stt_result_and_exact_cleanup():
    async def main() -> None:
        firmware = FakeFirmware()
        firmware.start()
        transport = None
        controller = None
        tasks = []
        try:
            transport, session = open_link(firmware)
            await session.login()
            controller = DictationController(session)
            await controller.attach(_Pipeline("spoken field text"))
            trigger = ManualTrigger()
            session.on_event = lambda payload: route_link_event(
                payload, trigger, session, dictation=controller)
            tasks = [
                asyncio.create_task(controller.run()),
                asyncio.create_task(session.pump_events()),
            ]
            path = firmware.begin_dictation_capture(DICT_ID)
            deadline = asyncio.get_running_loop().time() + 3
            while firmware.dictation_text is None:
                if asyncio.get_running_loop().time() >= deadline:
                    actor = tasks[0]
                    detail = (repr(actor.exception()) if actor.done() and
                              not actor.cancelled() else "still running")
                    raise AssertionError(
                        "dictation did not reach fake firmware; "
                        f"actor={detail} commands={firmware.command_log!r}")
                await asyncio.sleep(0.01)

            assert firmware.dictation_text == "spoken field text"
            assert path not in firmware.files
            assert sum(line.startswith("voicefetch ")
                       for line in firmware.command_log) == 1
            assert sum(line.startswith("dictate result ")
                       for line in firmware.command_log) == 1
            assert not any("micdelete" in line for line in firmware.command_log)
            # The framed fetch tail was fully drained: a following ordinary
            # command must still parse as its own reply.
            reply = await session.command("micread json", expect="json")
            assert reply.json["source"] == "pdm"
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if controller is not None:
                await controller.close()
            if transport is not None:
                transport.close()
            firmware.stop()

    run(main())


def test_real_session_cancel_mid_voicefetch_drains_tail_and_keeps_actor_live():
    async def main() -> None:
        firmware = FakeFirmware()
        firmware.voicefetch_frame_delay_s = 0.002
        firmware.voicefetch_event_after_frames = (
            2, f"dictate_cancel {DICT_ID}")
        firmware.start()
        transport = None
        controller = None
        tasks = []
        pipeline = _Pipeline("must not run")
        try:
            transport, session = open_link(firmware)
            await session.login()
            controller = DictationController(session)
            await controller.attach(pipeline)
            trigger = ManualTrigger()
            session.on_event = lambda payload: route_link_event(
                payload, trigger, session, dictation=controller)
            tasks = [
                asyncio.create_task(controller.run()),
                asyncio.create_task(session.pump_events()),
            ]
            firmware.begin_dictation_capture(DICT_ID)

            deadline = asyncio.get_running_loop().time() + 1
            while not any(line.startswith("voicefetch ")
                          for line in firmware.command_log):
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("voicefetch did not start")
                await asyncio.sleep(0.001)

            # This command queues behind voicefetch. Its valid JSON reply proves
            # the cancelled frame stream drained to its status boundary rather
            # than leaking a tail into the next command.
            reply = await session.command("micread json", expect="json")
            assert reply.json["source"] == "pdm"
            await asyncio.sleep(0)
            assert pipeline.calls == []
            assert not any(line.startswith(("dictate result ", "dictate fail "))
                           for line in firmware.command_log)
            assert not tasks[0].done()
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if controller is not None:
                await controller.close()
            if transport is not None:
                transport.close()
            firmware.stop()

    run(main())
