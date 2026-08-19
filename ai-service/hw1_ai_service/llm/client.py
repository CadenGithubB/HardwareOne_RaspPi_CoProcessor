"""Streaming chat client for llama-server's OpenAI-compatible API.

History lives HERE (the firmware sends no history — plan §Gap C1); the
static system prompt is always message zero so llama-server's prefix KV
cache turns per-turn prefill into transcript-only work. History is trimmed
oldest-first to keep the prefix stable.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from typing import AsyncIterator

import httpx

from ..config import LlmConfig

log = logging.getLogger("llm.client")


class LlmClient:
    def __init__(self, cfg: LlmConfig, base_url: str):
        self._cfg = cfg
        self._base = base_url
        # (user, assistant) turn pairs; deque maxlen trims oldest-first
        self._history: deque[tuple[str, str]] = deque(maxlen=max(1, cfg.history_turns))
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=300.0))

    async def close(self) -> None:
        await self._client.aclose()

    def clear_history(self) -> None:
        self._history.clear()

    async def ask_stream(self, prompt: str, *,
                         commit_history: bool = True,
                         max_tokens: int | None = None,
                         temperature: float | None = None,
                         top_p: float | None = None) -> AsyncIterator[str]:
        """Yield answer deltas and optionally commit the completed turn.

        Native EvenAI uses ``commit_history=False`` and commits only after its
        correlated display transaction succeeds. A wearer-dismissed answer
        therefore cannot silently bias the next exchange.

        The sampling overrides exist for turns the FIRMWARE owns: a CM5-routed
        `llm_ask` carries the device's centrally clamped maxTokens/temperature/
        top_p, and silently ignoring them would void the shared override
        contract that the web and BLE surfaces rely on. Local callers pass
        nothing and keep the configured defaults.
        """
        messages = [{"role": "system", "content": self._cfg.system_prompt}]
        for user, assistant in self._history:
            messages.append({"role": "user", "content": user})
            messages.append({"role": "assistant", "content": assistant})
        messages.append({"role": "user", "content": prompt})

        body = {
            "messages": messages,
            "max_tokens": (self._cfg.max_tokens if max_tokens is None
                           else max_tokens),
            "stream": True,
            "cache_prompt": True,
            # Qwen3-style thinking burns the token budget invisibly before
            # the first answer token (measured on-device: ~100 of 120 tokens
            # in <think> -> 6s turns, and occasionally an EMPTY answer when
            # thinking ate the whole budget). Voice answers need tokens on
            # the wire, not deliberation. Templates without a thinking
            # switch simply ignore the kwarg, so this is model-safe.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if temperature is not None:
            body["temperature"] = temperature
        if top_p is not None:
            body["top_p"] = top_p
        answer_parts: list[str] = []
        # Prefill and decode are bound by different things — prefill is
        # compute-bound (tracks CPU clock), decode is memory-bandwidth-bound —
        # so they respond to different changes and must be reported separately.
        # Measured client-side rather than read from the server's `timings`
        # field: this works on any OpenAI-compatible backend and cannot silently
        # go missing when llama.cpp changes what it reports. Time-to-first-token
        # is prefill plus one token, which is close enough to attribute with.
        t_req = time.monotonic()
        t_first: float | None = None
        t_last = t_req
        async with self._client.stream(
                "POST", f"{self._base}/v1/chat/completions", json=body) as resp:
            resp.raise_for_status()
            async for raw in resp.aiter_lines():
                if not raw.startswith("data:"):
                    continue
                payload = raw[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    delta = json.loads(payload)["choices"][0]["delta"]
                except (json.JSONDecodeError, LookupError):
                    log.debug("unparsed stream line: %r", raw)
                    continue
                piece = delta.get("content")
                if piece:
                    if t_first is None:
                        t_first = time.monotonic()
                    t_last = time.monotonic()
                    answer_parts.append(piece)
                    yield piece
        answer = "".join(answer_parts)
        if t_first is not None:
            decode_s = t_last - t_first
            # Deltas, not tokens — llama.cpp emits one delta per token for this
            # endpoint, so they coincide in practice, but say what is counted.
            n = len(answer_parts)
            log.info("llm: prompt~%d ch, ttft=%.2fs, %d deltas in %.2fs (%.1f/s), "
                     "answer %d ch, total=%.2fs",
                     sum(len(m["content"]) for m in messages), t_first - t_req,
                     n, decode_s, (n - 1) / decode_s if decode_s > 0 and n > 1 else 0.0,
                     len(answer), t_last - t_req)
        if commit_history:
            self.commit_turn(prompt, answer)

    def commit_turn(self, prompt: str, answer: str) -> None:
        self._history.append((prompt, answer))

    async def ask(self, prompt: str) -> str:
        return "".join([piece async for piece in self.ask_stream(prompt)])
