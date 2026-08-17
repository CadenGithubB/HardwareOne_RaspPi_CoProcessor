#!/usr/bin/env python3
"""Measure one llama-server arm through the service's own supervisor and client.

Why this exists alongside llama-bench: llama-bench drives llama_decode directly
and never starts a server, so it structurally cannot see speculative decoding.
An MTP arm and a plain arm produce identical llama-bench numbers. It also uses a
synthetic 128-token prompt, which says nothing about time-to-first-token behind
the real system prompt with the prefix KV cache warm.

So this probe drives the production path: the same LlamaServerSupervisor flags
(-t 4 -c 2048 --cache-reuse 256 --parallel 1) and the same LlmClient request
shape (streaming /v1/chat/completions, cache_prompt, enable_thinking=False,
history trimmed oldest-first). Timings are measured client-side exactly the way
LlmClient measures them, so these numbers are comparable to the daemon's own
"llm: ... ttft=..." log lines.

One JSON document is written to --out. Sampling is production's, not greedy:
answers vary run to run, which is why rates are reported as medians.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import pathlib
import statistics
import sys
import time

from hw1_ai_service.config import load as load_config
from hw1_ai_service.llm.client import LlmClient
from hw1_ai_service.llm.server import LlamaServerSupervisor

# Fixed prompt set. These are shaped like what moonshine actually hands the LLM:
# lower-case, unpunctuated voice transcripts, spanning the capabilities the
# system prompt claims (knowledge, translation, brainstorming, summarizing,
# reasoning from conversation). Kept identical across arms so answers.md can be
# read side by side for quality, not just speed.
# Translation prompts were removed 2026-08-13: translation is not a use case the
# wearer wants yet, and it has been dropped from the system prompt's capability
# list, so benchmarking it would grade a claim the system no longer makes. The
# replacements cover the capabilities that remain — explain, brainstorm,
# summarize, help with writing, reason from conversation.
PROMPTS: tuple[str, ...] = (
    "what's the difference between a capacitor and a resistor",
    "how long do i boil an egg for a soft yolk",
    "give me three ideas for dinner with chicken and rice",
    "summarize why the sky is blue in one sentence",
    "i have a meeting at 3 and another at 4 30 which should i prep first",
    "help me word a message asking my landlord to fix the heating",
)

# Discarded. Forces the weights off disk into page cache and builds the system
# prompt's KV prefix, so turn 1 of the measured set is not paying for both.
#
# Run TWICE. MEASURED 2026-08-13: one warmup was not enough for a 3.5 GiB MoE —
# LFM2's warmup took 5.96 s and the first MEASURED turn still took 6.16 s, while
# every later turn-1 took 0.21 s. With repeats=2 the median of {6.16, 0.21} is
# their mean, 3.18 s, which got reported as a TTFT regression that does not
# exist. Two warmups plus the cold/steady split below stop that recurring.
WARMUP_PROMPT = "say hello in one short sentence"
WARMUP_GENERATIONS = 2


def peak_rss_kib(pid: int) -> int | None:
    try:
        status = pathlib.Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("VmHWM:"):
            with contextlib.suppress(ValueError, IndexError):
                return int(line.split()[1])
    return None


def mem_total_kib() -> int | None:
    try:
        meminfo = pathlib.Path("/proc/meminfo").read_text()
    except OSError:
        return None
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            with contextlib.suppress(ValueError, IndexError):
                return int(line.split()[1])
    return None


async def timed_turn(client: LlmClient, prompt: str) -> dict:
    """Run one turn, returning the same quantities LlmClient logs."""
    t_req = time.monotonic()
    t_first: float | None = None
    t_last = t_req
    parts: list[str] = []
    async for piece in client.ask_stream(prompt):
        if t_first is None:
            t_first = time.monotonic()
        t_last = time.monotonic()
        parts.append(piece)
    if t_first is None:
        return {
            "prompt": prompt,
            "ttft_s": None,
            "deltas": 0,
            "decode_s": None,
            "decode_per_s": None,
            "total_s": time.monotonic() - t_req,
            "answer": "",
            "answer_chars": 0,
            "empty": True,
        }
    decode_s = t_last - t_first
    deltas = len(parts)
    answer = "".join(parts)
    return {
        "prompt": prompt,
        "ttft_s": t_first - t_req,
        "deltas": deltas,
        "decode_s": decode_s,
        # Same expression LlmClient logs: the first delta is attributed to
        # prefill, so the rate covers the deltas after it.
        "decode_per_s": ((deltas - 1) / decode_s) if decode_s > 0 and deltas > 1 else None,
        "total_s": t_last - t_req,
        "answer": answer,
        "answer_chars": len(answer),
        "empty": False,
    }


async def run_arm(args: argparse.Namespace) -> dict:
    cfg = load_config(args.config)
    llm_cfg = dataclasses.replace(
        cfg.llm,
        engine="server",
        server_bin=args.server_bin,
        model=args.model,
        port=args.port,
        extra_args=list(cfg.llm.extra_args) + list(args.extra_arg or []),
    )

    supervisor = LlamaServerSupervisor(llm_cfg)
    load_started = time.monotonic()
    await supervisor.start()
    load_s = time.monotonic() - load_started

    # Private attribute: the supervisor exposes no pid accessor, and peak RSS is
    # the number that decides whether this model can be co-resident with
    # moonshine in production. Degrades to null rather than failing the arm.
    proc = getattr(supervisor, "_proc", None)
    pid = getattr(proc, "pid", None)

    client = LlmClient(llm_cfg, supervisor.base_url)
    turns: list[dict] = []
    warmups: list[dict] = []
    try:
        for _ in range(WARMUP_GENERATIONS):
            warmups.append(await timed_turn(client, WARMUP_PROMPT))
            client.clear_history()
        warmup = warmups[-1]
        for repeat in range(args.repeats):
            client.clear_history()
            for turn_index, prompt in enumerate(PROMPTS, start=1):
                record = await timed_turn(client, prompt)
                record["repeat"] = repeat
                record["turn"] = turn_index
                turns.append(record)
    finally:
        rss_kib = peak_rss_kib(pid) if isinstance(pid, int) else None
        await client.close()
        await supervisor.stop()

    # The very first measured generation can still be cold even after warmups —
    # a big MoE keeps faulting in expert tensors. It is reported on its own as
    # cold_first_generation_s and EXCLUDED from every steady-state aggregate,
    # because averaging a one-off cold sample into a two-sample median produces
    # a number that describes neither state.
    cold = next((t for t in turns if not t["empty"]), None)
    steady = [t for t in turns
              if not t["empty"] and not (t["repeat"] == 0 and t["turn"] == 1)]

    ok = steady
    ttfts = [turn["ttft_s"] for turn in ok if turn["ttft_s"] is not None]
    # Turn 1 pays for a cold history prefix; later turns run behind
    # --cache-reuse. Reporting them merged would hide exactly the effect
    # history_turns: 8 is there to trade against.
    first_ttfts = [t["ttft_s"] for t in ok if t["turn"] == 1 and t["ttft_s"] is not None]
    later_ttfts = [t["ttft_s"] for t in ok if t["turn"] > 1 and t["ttft_s"] is not None]
    rates = [turn["decode_per_s"] for turn in ok if turn["decode_per_s"] is not None]
    totals = [turn["total_s"] for turn in ok if turn["total_s"] is not None]

    def median(values: list[float]) -> float | None:
        return statistics.median(values) if values else None

    total_kib = mem_total_kib()
    return {
        "label": args.label,
        "model": args.model,
        "server_bin": args.server_bin,
        "extra_args": list(args.extra_arg or []),
        "server_args": llm_cfg.extra_args,
        "max_tokens": llm_cfg.max_tokens,
        "history_turns": llm_cfg.history_turns,
        "repeats": args.repeats,
        "prompts": len(PROMPTS),
        "load_s": load_s,
        "warmup": warmup,
        "warmups": warmups,
        "warmup_generations": WARMUP_GENERATIONS,
        # One-off cost of the first real generation after the server starts. In
        # production the daemon starts llama-server once and keeps it, so this
        # is paid at boot, not per turn — never fold it into a latency median.
        "cold_first_generation_s": None if cold is None else cold["ttft_s"],
        "steady_turns": len(steady),
        "turns": turns,
        "empty_answers": sum(1 for turn in turns if turn["empty"]),
        "ttft_median_s": median(ttfts),
        "ttft_first_turn_median_s": median(first_ttfts),
        "ttft_later_turn_median_s": median(later_ttfts),
        "decode_median_per_s": median(rates),
        "total_median_s": median(totals),
        "answer_chars_median": median([float(t["answer_chars"]) for t in ok]),
        "peak_rss_kib": rss_kib,
        "mem_total_kib": total_kib,
        "status": "ok" if rates and not any(turn["empty"] for turn in turns) else "failed",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--server-bin", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--extra-arg", action="append", default=[])
    args = parser.parse_args()

    try:
        result = asyncio.run(run_arm(args))
    except Exception as exc:  # noqa: BLE001 - one arm must not kill the sweep
        result = {
            "label": args.label,
            "model": args.model,
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
        pathlib.Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
        print(f"arm {args.label} failed: {result['error']}", file=sys.stderr)
        return 1

    pathlib.Path(args.out).write_text(json.dumps(result, indent=2) + "\n")

    def show(value: float | None, digits: int = 2) -> str:
        return "n/a" if value is None else f"{value:.{digits}f}"

    print(
        f"{args.label}: ttft median {show(result['ttft_median_s'])}s "
        f"(turn1 {show(result['ttft_first_turn_median_s'])}s / "
        f"later {show(result['ttft_later_turn_median_s'])}s), "
        f"decode {show(result['decode_median_per_s'])}/s, "
        f"peak RSS {show(None if result['peak_rss_kib'] is None else result['peak_rss_kib'] / 1024, 0)} MiB, "
        f"empty answers {result['empty_answers']}"
    )
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
