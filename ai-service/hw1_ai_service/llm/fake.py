"""Echo fake for tests and --dry-run: no server, no network."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator


class FakeLlm:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def ask_stream(self, prompt: str, *, commit_history: bool = True,
                         max_tokens: int | None = None,
                         temperature: float | None = None,
                         top_p: float | None = None) -> AsyncIterator[str]:
        # Accepts (and ignores) the real client's per-turn overrides so a
        # firmware-owned `llm_ask` can run against llm.engine: fake.
        self.prompts.append(prompt)
        for word in f"echo: {prompt}".split(" "):
            await asyncio.sleep(0)
            yield word + " "

    async def ask(self, prompt: str) -> str:
        return "".join([p async for p in self.ask_stream(prompt)]).strip()

    async def close(self) -> None:
        pass

    def clear_history(self) -> None:
        pass
