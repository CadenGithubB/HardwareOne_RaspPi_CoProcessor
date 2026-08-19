# STT benchmarking — accuracy, cadence, and hallucination

**Question:** which recognizer and which streaming cadence, and does it fabricate
text when nobody is speaking?

**Profile fields:** `HW_STT_MODEL`, `HW_PY`, `HW_HOST`, `HW_EVID_ROOT`,
`HW_AUDIO_SRC`.

## 1. Build the corpus before you benchmark anything

A benchmark inherits every defect of its corpus. Requirements:

- **Known identity.** Every file maps to a known prompt. If a capture's identity
  is uncertain — an overwritten file, a missing index — exclude it from scoring
  and re-record it. Do not guess from content.
- **Coverage.** Short, long, quiet, paused mid-sentence, number-heavy,
  proper-name-heavy, abandoned mid-utterance.
- **Negative controls.** Files with *no speech* — room tone, static — with a
  known-empty reference. These are scored for fabricated text and for false
  finalization.
- **Not a negative control:** a very quiet file that does contain speech. Quiet
  valid speech overlaps the noise level of true negatives, which is exactly why
  a loudness threshold is not a safe hallucination filter.
- **Hash-pinned manifest.** Record path, duration, chunk count, level/peak dBFS,
  and hash per file. Note whether durations are exact multiples of the capture
  chunk size — a partial chunk at the boundary is its own edge case.

Record level and peak for each file. A corpus where nothing clips and everything
sits in a narrow band will not tell you how the engine behaves at the edges.

## 2. What to measure

Per file, per engine, per cadence:

| Measure | Notes |
|---|---|
| Word errors against reference | count them; a rate over a tiny corpus is false precision |
| Streaming vs batch on the same file | they fail differently — see §4 |
| End-of-audio to final transcript | the latency the user feels |
| Partial-update gap (max) | interactivity: the longest silence between visible updates |
| Useful partial updates before end | too few and streaming buys nothing |
| Queue high-water, queue age | `n/depth`; how close the pipeline ran to overflow |
| Dropped/overflowed chunks | must be zero for the run to count |
| Hallucinations on negatives | fabricated text on no-speech files |

## 3. Cadence sweep

Run the same corpus at several update floors (e.g. 0.5 s and 1.0 s) in
**separate processes**, and size the input FIFO explicitly. A too-small FIFO
shows up as overflow at the slower cadence first; widening it is usually the
correct fix, and it changes nothing about accuracy.

Set targets before the run — a soft target for end-to-final and a separate hard
timeout — and record them alongside the numbers. Note plainly that these
thresholds *observe and contain* latency; raising a threshold does not make
inference faster.

## 4. Streaming and batch fail differently

This is the most reusable finding from past runs, and it inverts the intuition
that batch is the safe fallback:

- **Streaming** tends to return an empty final on true silence.
- **Batch** over the same silence can fabricate a short plausible token — and
  can prefix that token onto an adjacent valid utterance.

So: treat a valid empty streaming final as *admissible no-speech evidence*, and
score batch hallucination separately rather than assuming batch is the referee.
Distinguish it from real failures — queue overflow, timeout, transport error, or
a missing final from the stream — which must fall back to the retained capture.

Neither a loudness threshold nor a blacklist of the specific fabricated token is
a safe correction: the first rejects quiet valid speech, the second rejects the
word when someone genuinely says it.

## 5. Reporting

Report per-file counts, not just aggregates, and mark each run against the
policy you set:

> Both reports intentionally failed the complete policy — two files exceeded the
> partial-gap ceiling and one produced fewer pre-end updates than required. Those
> are interactivity findings, not retention failures: zero chunks were dropped.

That sentence shape — what failed, what class of failure it is, what it does not
imply — is what makes a partial result usable by the next person.

State explicitly when the corpus is too small for a model decision. A handful of
utterances is a smoke slice; it cannot support a p95 or an engine choice.
