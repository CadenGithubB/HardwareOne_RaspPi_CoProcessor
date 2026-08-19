#!/usr/bin/env python3
"""Offline replay of the firmware's micrecord VAD on a captured WAV.

The recording task writes the SAME processed samples it feeds the silence
detector (System_Microphone.cpp: micProcessForSource runs before both the
level math and the file write), so replaying the algorithm on the saved WAV
is bit-exact with what the device decided in real time. Point this at the
Pi's save_last_path file (default ~/.cache/hw1-ai-service/last-utterance.wav)
after a run that hit "max window reached" and it names the exact chunks that
kept resetting the silence window.

Algorithm mirror (constants from System_Microphone.cpp + the loop that starts
at the `if (gRecSilenceStopMs > 0)` guard — keep in lockstep with the firmware):
    chunk    = 2048 samples (RECORDING_CHUNK_SIZE 4096 B) = 128 ms @ 16 kHz
    avg      = mean(|sample|) per chunk
    peakAvg  = max chunk avg so far
    speech   latches once avg >= 120 (kRecSpeechFloorAvg)
    floor    = minimum of avg over a bounded trailing window
               (kRecFloorWinChunks), updated with the CURRENT chunk BEFORE
               cut is computed
    cut      = max(2*floor, peakAvg / 8, 45) (kRecSilenceFloorAvg)
    silence += chunk ms while speech-heard and avg < cut; ANY chunk >= cut
               resets it to 0
    elapsed  = ci * chunk ms — the firmware reads recordingSamples, which is
               incremented AFTER the VAD block, so at chunk ci it still holds
               ci chunks' worth. Being one chunk ahead here would let the tool
               stop where the firmware could not.
    stop     when silence >= silence_ms (kEvenAiVadSilenceMs, 1800 for
               wake captures) and elapsed >= 800 ms

Note the seeding invariant this replays: a chunk that lowers the window
minimum gets floor <= avg, hence cut >= 2*avg > avg, hence "silence" — no
matter how loud it was. Chunk 0 (empty window) always takes that path.

Usage:
    python3 vad_replay.py <file.wav> [--silence-ms 1800] [--verbose] [--legacy]

Stdlib-only on purpose: runs with any python3 on the Pi, no venv needed.
"""

from __future__ import annotations

import argparse
import struct
import sys
import wave

CHUNK_SAMPLES = 2048          # RECORDING_CHUNK_SIZE / sizeof(int16_t)
SPEECH_FLOOR_AVG = 120        # kRecSpeechFloorAvg
SILENCE_FLOOR_AVG = 45        # kRecSilenceFloorAvg
VAD_MIN_MS = 800              # kRecVadMinMs
FLOOR_WIN_CHUNKS = 40         # kRecFloorWinChunks (~5.1 s @ 128 ms)


def firmware_cut(floor: int | None, peak_avg: int) -> int:
    """The SHIPPED firmware threshold: noise-floor-based margin.
    cut = max(2*floor, peak/8, 45). Replaced the older peak/5 rule, which
    landed inside the ambient band (G2 mic: speech-to-ambient ~7x, so
    20%-of-peak ~= noise floor and every ambient wobble reset the silence
    window). Use --legacy to replay that retired rule for comparison."""
    base = SILENCE_FLOOR_AVG
    if floor is not None:
        base = max(base, 2 * floor)
    return max(base, peak_avg // 8)


def replay(path: str, silence_stop_ms: int, verbose: bool,
           legacy: bool = False) -> int:
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        width = w.getsampwidth()
        if channels != 1 or width != 2:
            print(f"unsupported WAV shape: {channels}ch {width * 8}-bit "
                  "(expected mono 16-bit)", file=sys.stderr)
            return 2
        pcm = w.readframes(w.getnframes())

    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm[:n * 2])
    chunk_ms = CHUNK_SAMPLES * 1000 // rate

    peak_avg = 0
    silence_ms = 0
    heard_speech = False
    speech_at_ms: int | None = None
    would_stop_ms: int | None = None
    resets: list[tuple[int, int, int]] = []   # (ms, avg, cut) after speech
    post_speech_quiet: list[int] = []          # sub-cut chunk avgs after speech
    floor: int | None = None                   # firmware mode: tracked noise floor
    floor_win: list[int] = []                  # bounded window the floor is min() of
    seeded: list[int] = []                     # chunks where this chunk set the floor

    for ci in range(0, n // CHUNK_SAMPLES):
        chunk = samples[ci * CHUNK_SAMPLES:(ci + 1) * CHUNK_SAMPLES]
        avg = sum(abs(s) for s in chunk) // len(chunk)
        t_ms = ci * chunk_ms

        if avg > peak_avg:
            peak_avg = avg
        if legacy:
            cut = max(peak_avg // 5, SILENCE_FLOOR_AVG)
            # Legacy latched on the absolute gate alone.
            if not heard_speech and avg >= SPEECH_FLOOR_AVG:
                heard_speech = True
                speech_at_ms = t_ms
        else:
            # Updated with THIS chunk before cut is computed, exactly as the
            # firmware does — that ordering is what makes a new minimum score
            # as silence regardless of its level.
            floor_before = floor
            # Minimum over a bounded trailing window (kRecFloorWinChunks).
            # Replaced a running-min-with-leak, which climbed through
            # continuous speech; see System_Microphone.cpp for why gating
            # the leak cannot fix that.
            floor_win.append(avg)
            if len(floor_win) > FLOOR_WIN_CHUNKS:
                floor_win.pop(0)
            floor = min(floor_win)
            if floor_before is None or floor < floor_before:
                seeded.append(ci)
            cut = firmware_cut(floor, peak_avg)
            # Speech must clear the measured floor AND the absolute gate, and
            # only once a floor exists — so room tone above SPEECH_FLOOR_AVG
            # can no longer latch, and chunk 0 (which seeds the floor) never
            # can. Mirrors System_Microphone.cpp.
            if (not heard_speech and floor_before is not None
                    and avg >= cut and avg >= SPEECH_FLOOR_AVG):
                heard_speech = True
                speech_at_ms = t_ms

        if heard_speech and avg < cut:
            silence_ms += chunk_ms
            post_speech_quiet.append(avg)
        else:
            if heard_speech and silence_ms > 0:
                resets.append((t_ms, avg, cut))
            silence_ms = 0

        if verbose:
            mark = " <RESET" if resets and resets[-1][0] == t_ms else ""
            print(f"{t_ms / 1000:7.2f}s avg={avg:6d} cut={cut:5d} "
                  f"sil={silence_ms:5d}ms{mark}")

        # NOT (ci + 1): the firmware's recordingSamples is incremented after
        # the VAD block, so at chunk ci it still reflects ci chunks.
        elapsed_ms = ci * chunk_ms
        if (would_stop_ms is None and heard_speech
                and silence_ms >= silence_stop_ms and elapsed_ms >= VAD_MIN_MS):
            would_stop_ms = elapsed_ms

    dur_s = n / rate
    algo = "RETIRED peak/5 cut" if legacy else "shipped firmware algorithm"
    print(f"\n{path}: {dur_s:.1f}s @ {rate} Hz, {n // CHUNK_SAMPLES} chunks "
          f"of {chunk_ms} ms [{algo}]")
    if not legacy and floor is not None:
        print(f"tracked noise floor ended at {floor}")
        if seeded:
            print(f"floor re-seeded on {len(seeded)} chunk(s): "
                  f"{seeded[:12]}{' ...' if len(seeded) > 12 else ''}")
            if 0 in seeded and speech_at_ms == 0:
                print("  !! chunk 0 both latched speech AND seeded the floor — "
                      "the capture opened mid-utterance, so every chunk within "
                      "6 dB of it scores as silence (mis-seeded capture)")
    if speech_at_ms is None:
        why = (f"no chunk reached avg >= {SPEECH_FLOOR_AVG}" if legacy else
               f"no chunk cleared both the floor-relative cut and "
               f"avg >= {SPEECH_FLOOR_AVG}")
        print(f"speech NEVER latched ({why}) — auto-stop can never fire, so "
              "the capture rides the caller's max window instead of "
              "truncating. Nothing here stood clearly above room tone.")
        return 1
    final_cut = (max(peak_avg // 5, SILENCE_FLOOR_AVG) if legacy
                 else firmware_cut(floor, peak_avg))
    print(f"speech latched at {speech_at_ms / 1000:.2f}s; "
          f"peakAvg={peak_avg} -> final cut={final_cut}")
    if post_speech_quiet:
        q = sorted(post_speech_quiet)
        print(f"quiet-chunk avg after speech: median={q[len(q) // 2]}, "
              f"p90={q[int(len(q) * 0.9)]} (margin below cut matters)")
    if would_stop_ms is not None:
        print(f"VAD would auto-stop at {would_stop_ms / 1000:.2f}s "
              f"({silence_stop_ms} ms trailing silence)")
    else:
        print(f"VAD would NEVER auto-stop in this file ({silence_stop_ms} ms "
              "window) — the resets below are the culprits")
    if resets:
        print(f"\n{len(resets)} silence-window reset(s) after speech:")
        for t_ms, avg, cut in resets[-20:]:
            print(f"  {t_ms / 1000:7.2f}s  avg={avg:6d} >= cut={cut:5d}")
        if len(resets) > 20:
            print(f"  (first {len(resets) - 20} omitted)")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("wav", help="captured recording (Pi: "
                    "~/.cache/hw1-ai-service/last-utterance.wav)")
    ap.add_argument("--silence-ms", type=int, default=1800,
                    help="armed trailing-silence window (kEvenAiVadSilenceMs, "
                         "currently 1800 for wake captures)")
    ap.add_argument("--verbose", action="store_true",
                    help="print every chunk, not just the summary")
    ap.add_argument("--legacy", action="store_true",
                    help="replay the RETIRED peak/5 threshold instead of the "
                         "shipped floor-based one (comparison only)")
    args = ap.parse_args()
    sys.exit(replay(args.wav, args.silence_ms, args.verbose, args.legacy))


if __name__ == "__main__":
    main()
