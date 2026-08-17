"""Deterministic fake engine for tests and --dry-run."""

from __future__ import annotations


class FakeSTT:
    def __init__(self) -> None:
        self.threads = 0
        self.calls: list[tuple[int, int]] = []   # (n_bytes, rate)

    def transcribe(self, pcm: bytes, rate: int) -> str:
        self.calls.append((len(pcm), rate))
        seconds = len(pcm) / 2 / rate if rate else 0.0
        return f"fake transcript of {seconds:.1f}s audio"

    def set_threads(self, n: int) -> None:
        self.threads = n
