"""Offline replay bench for the live-STT debug corpus.

The debug corpus (stt.live_debug_capture) writes, per live wake exchange, a
``<ts>_<id>.wav`` + ``<ts>_<id>.json`` (audio + the full worker snapshot). This
module replays those samples deterministically so a fix can be *proven* on the
exact audio that produced a defect, instead of re-speaking and grepping.

Two modes:

- ``finalize`` (no model, runs anywhere): reconstruct the raw stop() transcript
  from the captured stop_lines and re-run ``finalize_transcript`` old-vs-new on
  the captured partials. This isolates the transcript-assembly logic — it proves
  the erasure fix on captured data with zero inference.
- ``full`` (needs the model, runs on the Pi): re-feed the captured WAV through a
  fresh ``LiveMoonshineWorker`` with a chosen arch/interval, so model/config
  choices (medium vs base, update_interval, timeouts) can be A/B'd on one corpus.

Usage:
    python -m hw1_ai_service.stt.replay CORPUS_DIR [--mode finalize|full]
        [--arch medium-streaming] [--update-interval 1.0]
        [--expected-text "one two three ... ten"]

Per-sample expected text may also live beside the sample as
``<base>.expected.txt`` and takes precedence over --expected-text.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import wave
from pathlib import Path
from typing import Any

from .live import LOGICAL_CHUNK_BYTES, finalize_transcript


def _norm(text: str) -> list[str]:
    return re.sub(r"[^0-9a-z]+", " ", (text or "").lower()).split()


def stop_text_from_snapshot(stream: dict[str, Any]) -> str:
    """Reconstruct the raw stop() transcript from captured stop_lines.

    Mirrors ``_transcript_text``'s ordering (start_time, numeric-id-first,
    stable id, index) and non-empty join, so the reconstructed stop text equals
    what the worker fed to finalize_transcript at capture time."""
    lines = stream.get("stop_lines") or []
    indexed = list(enumerate(lines))

    def key(item: tuple[int, dict]) -> tuple:
        idx, ln = item
        try:
            start = float(ln.get("start_time") or 0.0)
        except (TypeError, ValueError):
            start = 0.0
        lid = ln.get("line_id", idx)
        numeric = isinstance(lid, int) and not isinstance(lid, bool)
        return (start, 0 if numeric else 1, lid if numeric else str(lid), idx)

    return " ".join(
        str(ln.get("text") or "").strip()
        for _, ln in sorted(indexed, key=key)
        if str(ln.get("text") or "").strip()
    ).strip()


def word_recall(produced: str, expected: str) -> float:
    """Fraction of expected words present, in order (longest common
    subsequence over normalized tokens). 1.0 == full transcript recovered."""
    exp = _norm(expected)
    got = _norm(produced)
    if not exp:
        return 1.0
    # LCS length
    prev = [0] * (len(got) + 1)
    for e in exp:
        cur = [0]
        for j, g in enumerate(got, 1):
            cur.append(prev[j - 1] + 1 if e == g else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1] / len(exp)


def _expected_for(sample_path: Path, fallback: str | None) -> str | None:
    sidecar = sample_path.with_suffix(".expected.txt")
    if sidecar.exists():
        return sidecar.read_text().strip()
    return fallback


def load_samples(corpus_dir: Path) -> list[tuple[Path, dict]]:
    out: list[tuple[Path, dict]] = []
    for jpath in sorted(corpus_dir.glob("*.json")):
        if jpath.name.endswith(".expected.txt"):
            continue
        try:
            out.append((jpath, json.loads(jpath.read_text())))
        except Exception as exc:  # noqa: BLE001 - report and skip bad files
            print(f"  ! skip {jpath.name}: {exc}", file=sys.stderr)
    return out


def replay_finalize(corpus_dir: Path, expected_text: str | None) -> int:
    samples = load_samples(corpus_dir)
    if not samples:
        print(f"no samples in {corpus_dir}", file=sys.stderr)
        return 1
    changed = 0
    print(f"=== finalize replay: {len(samples)} sample(s) in {corpus_dir} ===")
    for jpath, sample in samples:
        if not (sample.get("snapshot") or {}).get("stream"):
            # Batch-path samples (schema v2) carry no worker snapshot — they
            # exist for delivery-timing/ceiling analysis, not finalize replay.
            print(f"\n· {jpath.stem}  [skipped: no stream snapshot "
                  f"(path={sample.get('path')})]")
            continue
        stream = (sample.get("snapshot") or {}).get("stream") or {}
        partials = stream.get("partials") or []
        ended_t = stream.get("input_ended_t")
        stop_text = stop_text_from_snapshot(stream)
        old = finalize_transcript(stop_text, partials, legacy=True)["text"]
        new_res = finalize_transcript(
            stop_text, partials, input_ended_t=ended_t, legacy=False)
        new = new_res["text"]
        expected = _expected_for(jpath, expected_text)
        diff = old != new
        changed += 1 if diff else 0
        print(f"\n· {jpath.stem}")
        print(f"    stop()   : {stop_text!r}")
        print(f"    OLD final: {old!r}")
        print(f"    NEW final: {new!r}   [{new_res['mode']}]"
              + ("  <-- CHANGED" if diff else ""))
        if expected is not None:
            print(f"    expected : {expected!r}")
            print(f"    recall   : OLD {word_recall(old, expected):.2f}"
                  f"  ->  NEW {word_recall(new, expected):.2f}")
    print(f"\n{changed}/{len(samples)} sample(s) changed by the new "
          f"finalization.")
    return 0


def replay_full(corpus_dir: Path, arch: str | None,
                update_interval: float | None, expected_text: str | None,
                final_timeout: float) -> int:
    # Model-dependent: imported lazily so finalize mode needs no moonshine.
    from .live import LiveMoonshineWorker, exact_moonshine_factory

    samples = load_samples(corpus_dir)
    if not samples:
        print(f"no samples in {corpus_dir}", file=sys.stderr)
        return 1
    print(f"=== full replay: {len(samples)} sample(s), arch={arch} "
          f"interval={update_interval} ===")
    rc = 0
    for jpath, sample in samples:
        wav_path = jpath.with_suffix(".wav")
        if not wav_path.exists():
            print(f"  ! {jpath.stem}: no .wav, skipping", file=sys.stderr)
            continue
        with wave.open(str(wav_path), "rb") as w:
            rate = w.getframerate()
            pcm = w.readframes(w.getnframes())
        cfg = sample.get("config") or {}
        use_arch = arch or cfg.get("model_arch") or "medium-streaming"
        use_interval = (update_interval if update_interval is not None
                        else float(cfg.get("update_interval_s") or 1.0))
        model_dir = sample.get("model_dir") or cfg.get("model_dir")
        if not model_dir:
            print(f"  ! {jpath.stem}: sample has no model_dir; pass the model "
                  f"path via HW1_LIVE_MODEL_DIR", file=sys.stderr)
            import os
            model_dir = os.environ.get("HW1_LIVE_MODEL_DIR")
        if not model_dir:
            rc = 1
            continue
        # Queue sized to hold the whole utterance so the offline dump can never
        # overflow (replay is not latency-bound, unlike the live transport).
        n_chunks = max(16, math.ceil(len(pcm) / LOGICAL_CHUNK_BYTES) + 8)
        worker = LiveMoonshineWorker(
            exact_moonshine_factory(model_dir, use_arch),
            update_interval_s=use_interval, queue_chunks=n_chunks)
        worker.start(120.0)
        worker.on_begin({"sample_rate": rate})
        for i in range(0, len(pcm), LOGICAL_CHUNK_BYTES):
            worker.offer_pcm(pcm[i:i + LOGICAL_CHUNK_BYTES])
        worker.end_input()
        snap = worker.wait(final_timeout + 5.0)
        text = snap.get("text", "")
        expected = _expected_for(jpath, expected_text)
        print(f"\n· {jpath.stem}  ({len(pcm) / 2 / rate:.2f}s @ {rate}Hz)")
        print(f"    captured : {sample.get('transcript')!r}")
        print(f"    replay   : {text!r}   [{(snap.get('stream') or {}).get('final_rescue_mode')}]")
        if expected is not None:
            print(f"    recall   : {word_recall(text, expected):.2f}"
                  f"   (expected {expected!r})")
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Live-STT corpus replay bench")
    ap.add_argument("corpus_dir", type=Path)
    ap.add_argument("--mode", choices=("finalize", "full"), default="finalize")
    ap.add_argument("--arch", default=None,
                    help="override model arch (full mode)")
    ap.add_argument("--update-interval", type=float, default=None,
                    help="override update_interval_s (full mode)")
    ap.add_argument("--final-timeout", type=float, default=2.0)
    ap.add_argument("--expected-text", default=None,
                    help="expected transcript applied to all samples that lack "
                         "a <base>.expected.txt sidecar")
    args = ap.parse_args(argv)
    if args.mode == "finalize":
        return replay_finalize(args.corpus_dir, args.expected_text)
    return replay_full(args.corpus_dir, args.arch, args.update_interval,
                       args.expected_text, args.final_timeout)


if __name__ == "__main__":
    raise SystemExit(main())
