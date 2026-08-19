# LLM serving benchmark on a small SBC

**Question:** what generation speed, time-to-first-token, and memory footprint
does this model/quantization/server combination actually deliver on this board —
and does it hold under sustained load?

**Profile fields:** `HW_LLM_MODEL`, `HW_HOST`, `HW_SERVICE`, `HW_EVID_ROOT`.

## 1. Measurement hygiene — most bad numbers come from here

A benchmark is invalid unless all of these are recorded *in the run*:

- **Nothing else resident.** Stop the service; confirm no server process from a
  previous run is still holding weights. A second resident model changes both
  speed and memory.
- **CPU governor and clock.** A run captured in `powersave` at a reduced clock
  is not a production baseline. Select the performance profile, verify it took
  effect, and restore the automatic policy afterwards.
- **Thermal state before and after.** Small boards throttle within tens of
  seconds of sustained all-core load without a heatsink, and the throttled clock
  can cost around 40% of generation speed. Capture the throttle/temperature
  state at both ends; a benchmark that did not thermally saturate is a
  burst number, and should be labelled one.
- **Memory and swap.** Record both. Weights that fit only because something got
  swapped are not a fit.
- **The run completed.** A run that ended in a dropped connection or a timeout
  is discarded, not reported.

If any of these is missing, the *relative* comparison between quantizations may
still be suggestive, but the absolute tokens/second must not be quoted as a
production figure. Say so in the report.

## 2. Budget prefill and generation separately

They are bound by different resources — generation by memory bandwidth, prefill
by compute — and conflating them hides the real latency problem.

| Measure | Why |
|---|---|
| Generation tok/s | steady-state speed the user reads at |
| Prefill tok/s | usually a small multiple of generation on CPU-only boards |
| Time to first token | prompt length ÷ prefill rate, plus scheduling |
| Prompt-cache hit behaviour | the single biggest TTFT lever |

The practical consequence: a long static system prompt can cost seconds before
the first token on a small board. With prefix caching enabled and a stable
system prefix, per-turn prefill collapses to just the new user text. Verify the
cache is actually being hit — and verify any architecture-specific fast path
your runtime claims is really active, by reading the server's startup log rather
than assuming.

## 3. Quantization sweep

Run the same prompt/generation lengths across candidate quantizations in one
session, under identical governor and thermal conditions:

| Quantization | Prefill (tok/s) | Generate (tok/s) |
|---|---:|---:|
| … | … | … |

Expect the ranking to *differ between the two columns* — one format can prefill
noticeably faster while another decodes faster. Which one wins depends on your
traffic shape: with a well-reused cached prefix and short prompts, decode speed
dominates; with long uncached prompts, prefill does. Compute the crossover for
your actual prompt/answer lengths instead of picking the winner of one column.

## 4. Serving shape

Decide, and record the reasoning:

- **Separate server process over localhost** — crash isolation from the rest of
  the pipeline, prompt caching, health endpoint, and memory-mapped weights that
  stay warm in the page cache across restarts.
- **In-process bindings** — fewer moving parts, but the overhead is real, and a
  long-lived daemon inherits any leak.
- **A wrapper daemon** — convenient, usually slower, and watch for an idle
  unload policy that silently adds a cold reload after quiet periods.

Also record thread allocation. If the pipeline is naturally sequential
(capture → recognize → generate), each stage can use all cores; only genuine
overlap forces a split, and running two saturated engines on the same small core
count roughly halves both.

## 5. Fit table

Compute rather than guess, and state the assumption set:

- weights at the chosen quantization,
- KV cache per token × the context you actually use (short exchanges need far
  less than the maximum context),
- co-resident recognizer,
- OS overhead.

Give the verdict per RAM SKU, and note which SKU the recommendation assumes.

## 6. Report template

State the recommendation as a primary choice, a fallback for the smaller SKU,
and a step-up if quality demands it — each with the accepted cost. Close with
the conditions under which the numbers must be re-taken (governor, thermals, and
any co-resident process).
