from __future__ import annotations

import asyncio

from conftest import open_link, run
from fake_firmware import make_wav

from hw1_ai_service.cm5_presence import Cm5PresenceMode
from hw1_ai_service.config import Config
from hw1_ai_service.jobs import Job
from hw1_ai_service.llm.fake import FakeLlm
from hw1_ai_service.pipeline import VoicePipeline
from hw1_ai_service.stt.fake import FakeSTT


def test_full_ask_exchange(firmware):
    """The whole P0 loop against the fake firmware: record -> fetch -> STT
    -> LLM -> deliver, with the answer landing on the (auto-started) OLED."""
    firmware.wav_bytes = make_wav(seconds=1.0)

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            cfg = Config()
            cfg.audio.record_seconds = 0.05
            stt, llm = FakeSTT(), FakeLlm()
            pipeline = VoicePipeline(session, stt, llm, cfg)
            try:
                answer = await pipeline.run_ask()
            finally:
                await pipeline.close()
            assert stt.calls, "STT was invoked"
            assert llm.prompts == ["fake transcript of 1.0s audio"]
            assert answer.startswith("echo:")
            assert firmware.oled_texts, "answer delivered to OLED"
        finally:
            transport.close()
    run(main())


def test_chat_only_exchange(firmware):
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            cfg = Config()
            llm = FakeLlm()
            pipeline = VoicePipeline(session, None, llm, cfg)
            try:
                answer = await pipeline.run_chat("hello device")
            finally:
                await pipeline.close()
            assert answer == "echo: hello device"
            assert firmware.oled_texts
        finally:
            transport.close()
    run(main())


def test_reboot_probation_rearms_before_ready_and_cleanup_is_nonblocking():
    events: list[str] = []

    class SessionStub:
        reboot_suspected = True

        async def quiesce(self, _seconds: float) -> None:
            events.append("quiet")

        def clear_reboot_flag(self) -> None:
            self.reboot_suspected = False
            events.append("clear_reboot")

        async def settle(self) -> None:
            events.append("settled")

    class LiveGateStub:
        def link_reset(self) -> None:
            events.append("live_reset")

        async def ensure_armed(self, _session) -> None:
            events.append("live_armed")

    class PresenceStub:
        def link_reset(self) -> None:
            events.append("presence_starting")

        async def set_mode(self, mode: Cm5PresenceMode) -> bool:
            events.append(f"presence_await_{mode.value}")
            return True

        def set_mode_nowait(self, mode: Cm5PresenceMode) -> int:
            events.append(f"presence_nowait_{mode.value}")
            return 1

        async def acquire_busy(self, reason: str) -> int:
            events.append(f"presence_acquire_{reason}")
            return 1

        def release_busy(self, token: int, *, fallback=None) -> None:
            name = getattr(fallback, "value", fallback)
            events.append(f"presence_release_{token}_{name}")

    class SourceStub:
        def evenai_done(self, _exchange_id: str) -> None:
            raise AssertionError("chat jobs have no EvenAI owner")

    async def main() -> None:
        pipeline = VoicePipeline(
            SessionStub(), None, None, Config(),
            live_gate=LiveGateStub(), cm5_presence=PresenceStub())

        async def run_chat(_text: str) -> str:
            events.append("job")
            return ""

        pipeline.run_chat = run_chat  # type: ignore[method-assign]
        try:
            await asyncio.wait_for(
                pipeline._dispatch(Job("chat", "hello"), SourceStub()), 0.5)
        finally:
            await pipeline.close()

        # The job takes a NAMED share of the busy lease and releases exactly
        # that share, so a concurrent CM5-routed generation keeps the lease it
        # still needs instead of being dropped back to READY under it.
        assert events == [
            "presence_starting", "presence_await_starting", "live_reset",
            "quiet", "clear_reboot", "settled", "live_armed",
            "presence_acquire_voice:chat", "job", "presence_release_1_ready",
        ]

    run(main())
