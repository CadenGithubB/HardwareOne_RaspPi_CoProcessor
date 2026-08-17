"""STT engine interface + factory.

P0 uses only utterance-batch `transcribe`. The streaming half of the
interface (P4, when the firmware streams frames live) is declared here so
engines opt in without a redesign — Moonshine v2 and Zipformer both
support incremental feed; whisper.cpp simply won't implement it.

Engines run inside a ThreadPoolExecutor (they release the GIL in native
code); nothing here is async on purpose.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class STTEngine(Protocol):
    def transcribe(self, pcm: bytes, rate: int) -> str:
        """Full-utterance decode. pcm is int16 little-endian mono."""
        ...

    def set_threads(self, n: int) -> None:
        """Best-effort thread cap for LLM-contention windows."""
        ...


def create_engine(engine: str, model: str) -> STTEngine | None:
    """None means STT is deliberately disabled (stt.engine: none) — the
    pipeline then runs chat-only and `ask` explains itself instead of
    crashing (small-RAM one-engine mode)."""
    if engine == "none":
        return None
    if engine == "fake":
        from .fake import FakeSTT
        return FakeSTT()
    if engine == "moonshine":
        from .moonshine import MoonshineSTT
        return MoonshineSTT(model or "moonshine/small")
    if engine == "zipformer":
        from .zipformer import ZipformerSTT
        return ZipformerSTT(model)
    raise ValueError(f"unknown stt.engine: {engine!r} (moonshine|zipformer|fake|none)")
