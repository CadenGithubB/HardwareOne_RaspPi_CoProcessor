# Live STT pipeline — triage and forward plan (2026-08-10)

Written after independent review of the pulled evidence, the four cm5/ planning
docs, and the firmware audio path. Supersedes the "reinterpret as 8 kHz /
half-tempo" framing in the earlier hand-off; the audio forensics below refute
that framing. The cm5/ tree remains untracked and is not to be committed.

---

## 1. Where things actually left off

The pasted Codex conversation ends at the gate0a-medium replay compare
(~07:31). Work continued past that point the same morning:

| Time | Event | Evidence |
|---|---|---|
| ~08:05–08:17 | v2 replay pair (0.5 s / 1.0 s) with the 4-chunk FIFO | `.scratch/gate0a-v2-*` |
| ~08:41 | **v3 replay pair with the FIFO widened to 8 chunks / 32 KiB** — the 1.0 s overflow disappeared; decision recorded: keep 8 chunks, run real-G2 shadow at 1.0 s | `.scratch/gate0a-v3-results-Pn5hEAy0`, PLAN:178-231 |
| ~09:27 | First native live-STT invocation aborted pre-probe (`probe_rc=99`, self-matching process guard — fixed) | DEPLOY:1560-1565 |
| 09:51 | **First full native "Hey Even" → live PCM → streaming Moonshine hardware run.** Transport perfect; transcript wrong | `.scratch/native-live-stt-GBMcqNF1` |
| 10:00–10:07 | Diagnostic listening derivatives generated (+14 dB, 8 kHz-reinterp, half-tempo) | same dir |
| 10:09 | Docs updated with the sample-clock anomaly and a prescribed listening/raw-capture diagnostic; **work stopped here** | PLAN, ASSESS, DEPLOY, README |

### The 09:51 native run in one paragraph

Wake, capture, live shadow, UART, Pi FIFO, Moonshine worker, finalization, and
cleanup all worked: 55,200 samples end-to-end with valid END, CRC `1cd5979a`,
zero drops/faults, queue high-water 3/8, max queue age 537.6 ms, END-to-final
**51 ms**, throttle 0x0, exact cleanup. The only failure: Moonshine's final was
`Haitian is the capital difference.` against the pinned
`what is the capital of france` (3 word errors). Post-pull review found the
anomaly: 69 packets × 800 samples spanned 6.903 s of wall time — **7,996
samples/s under a 16 kHz label**, exactly half the healthy no-STT run's
16,030 samples/s.

## 2. Phase/gate status ledger (condensed)

| Phase / Gate | State |
|---|---|
| 0A replay gate | Initial slice done; gate OPEN (corpus repair + small/tiny matrix pending) |
| 0B renderer | Answer-causality PASSED (CONFIG 80→40 reversal proven); question-timing NOT started |
| 1 capture/session | Source-complete; HW dismissal regression PENDING |
| 2A synthetic transport | PASSED |
| 2B recorder shadow / Gate D | PASSED with caveats (latency p95 + auth/link/TX faults open) |
| **3 streaming STT** | **Transport PASSED / accuracy FAILED — blocked on the cadence anomaly below** |
| 4 question mirror, 5 endpointing, E, F | Not started (gated) |

Other standing items: batch hallucinates `Yeah.` on silence (streaming does
not); ASK envelope hard-fails ≥237 bytes; 98-char question truncation at
CONFIG 80; `g2aiconfig - 40 -` startup submission decided; deployed-source
hash capture still due after next sync.

## 3. The cadence anomaly: what the evidence now says

New forensics run on the Mac against both captures (healthy no-STT run
`native-shadow-LANSCdQ1` vs anomalous `native-live-stt-GBMcqNF1`):

| Measurement | Healthy | Anomalous | Implication |
|---|---|---|---|
| Wall-verified delivery | 16.03 ksamples/s | 7.996 ksamples/s (2.000:1) | Deterministic halving, not ragged loss |
| Apparent F0 (pitch) | 119.0 Hz | 119.5 Hz | **Identical.** Any 8 kHz-reinterpretation would double pitch — refuted |
| Spectral cliff at ~4 kHz | Present | Present | Inherent to G2's LC3 voice encoding, **not** evidence of an 8 kHz mode |
| Envelope modulation (syllable band) | 5.8 Hz | 5.3 Hz | Normal tempo in sample domain |
| Syllable nuclei | 6 (complete question) | 7 (plausibly complete) | Content looks **complete**, not halved |
| Splice discontinuities at packet bounds | — | none (1.02× background) | No hard seams — but see PLC note below |

**Consequences:**

1. **The prescribed listening comparison will mislead.** Both "corrections"
   (8 kHz-reinterp, half-tempo) transform pitch or tempo that the measurements
   say are already correct. Neither will sound natural, and that outcome would
   wrongly point at the codec. The useful listening question is different:
   *"Is any word of the question missing, or is it merely quiet/robotic?"*
2. Two hypotheses survive, and they have opposite fixes:
   - **H-late (favored):** the G2 encoded continuously but *delivered* at half
     pace (throttled BLE link — e.g. connection interval stuck long). Audio is
     complete, just late. Fix = link management; STT itself may be fine.
   - **H-loss:** alternate notifications were dropped in flight and the LC3
     decoder's overlap-add state smoothed the seams (which my splice test
     cannot see through). Audio is genuinely damaged. Fix = link + retransmit/
     detection strategy.
   Content-completeness evidence (syllables, modulation, near-complete
   transcript) leans H-late; the exact 2:1 during capture is the H-loss point.
3. The session was degraded **before capture began**: pre-run `g2micstats`
   showed ~14 fps idle vs ~19–20 fps in healthy sessions. Whatever the state
   is, it predates the capture and is observable at preflight — which makes it
   gateable.

### Firmware facts that reframe the debugging (from today's code recon)

- The 205-byte notification is `[5 × 40 B LC3 frames][5 B trailer]`
  (G2_Glasses.cpp:1923-1928). The mic stats parser still reads `data[0]`/
  `data[1]` as type/seq — those are **compressed-audio entropy**, which is why
  `gaps ≈ frames` in every run and `CC/CD` are noise. **The only real on-wire
  counter is in trailer bytes 200–204, and it is parsed nowhere.**
- LC3 decode is hardcoded 16 kHz/10 ms/40 B (G2_Glasses.cpp:1946-1952). Decode
  emits 800 samples per arrived notification *unconditionally* — PLC
  (return 1) is not counted in the AFE path, and `decode_fail` only counts
  parameter errors (effectively never). So `decode_fail=0` proves nothing
  about bitstream health, and delivered rate is purely arrival-rate × 800.
- **Nothing anywhere verifies delivered rate against the 16 kHz stamp** — not
  the recorder, not the shadow, not the WAV header, not VAD. This is why the
  failure was silent end-to-end (and why VAD's 1800 ms sample-counted window
  stretched to 3.6 s of wall time).
- Existing tools we can use immediately, no new firmware:
  - `g2micrec start` — dumps raw 205 B packets to SD (no timestamps).
  - `g2micverbose on` (+ DEBUG_G2 flag) — per-frame metadata log.
  - `g2connpri` / `g2envgap` — report the **measured** BLE connection interval
    per temple; `g2GapEventHandler` already logs peer-initiated renegotiation.
    A link stuck at BALANCED (40–60 ms) instead of HIGH (15 ms) is a concrete,
    already-observable mechanism for exactly this throttling.
  - `g2connpri <min> <max>` — force the interval.

## 4. Debugging test matrix (ranked by cost × information)

### Tier 0 — no hardware, runnable today

**T1. Batch-STT the two existing WAVs on the Pi.** Run the batch path against
the healthy no-STT WAV *and* the anomalous WAV (both already on disk on both
machines).
- healthy → correct: G2-quality audio is STT-viable at all → the entire
  accuracy question collapses onto the cadence bug.
- anomalous → correct: content is complete → **H-late confirmed**, pure link
  fix, no audio was lost.
- anomalous → wrong, healthy → correct: content genuinely damaged → H-loss.
This single test is the cheapest discriminator we have. Also run the
`+14 dB` variants to test the quiet-audio contribution separately.

**T2. One human listen with the right question.** Normal-label
`recording-…-listen-plus14db.wav`: are all eight syllables of the question
audible in order, or are pieces missing? (Ignore robotic timbre — that is
32 kbps LC3.)

### Tier 1 — existing CLI only, next bench session

**T3. Preflight link-state capture.** Before any capture: `g2connpri`,
`g2envgap`, `g2micstats` (idle fps), battery, WiFi/ESP-NOW radio state
(`wifistatus` — note the closewifi-leaves-radio-hunting history). Compare
degraded vs healthy sessions. If degraded sessions show a long measured conn
interval → root cause candidate found, and the fix already exists
(`g2connpri` force-HIGH before capture, restore after).

**T4. Repetition/stickiness matrix.** Native no-STT smoke ×5 back-to-back,
logging packets/s + conn interval each run; then glasses power-cycle and BLE
reconnect between runs. Determines whether the half-rate state is sticky
per-connection (cleared by reconnect?) and what triggers it.

**T5. Raw packet dump via `g2micrec`.** During one healthy and one degraded
session, capture ~5 s of raw 205 B packets; decode offline
(`docs/randomscripts/decode_g2_mic.py`) and diff the **trailer bytes 200–204**
across packets. If a counter is contiguous at half rate → G2 is *sending*
slower (H-late at source). If it skips → loss. No timestamps in this dump, but
counter continuity alone discriminates sender-vs-link.

### Tier 2 — small firmware additions (the permanent instruments)

**T6. Parse the trailer + timestamp arrivals.** In `handleAudioNotify`
(G2_Glasses.cpp:2341): log/count trailer bytes and per-packet `millis()`
inter-arrival; replace the bogus `seq/gaps/CC/CD` parsing with trailer-based
gap detection. Optionally extend the `g2micrec` write block with an 8-byte
`{millis, len, flags}` record header (new extension).

**T7. Delivered-rate watchdog.** Rolling ~500 ms delivered-rate during
capture; expose in `liveaudio` status and stamp a `degraded_rate` flag into
the UART live BEGIN/terminal metadata so the Pi *knows* the stream is bad
instead of transcribing garbage silently. Gate STT on it: fall back to WAV +
warning instead of feeding a degraded stream. This is required for production
regardless of root cause.

**T8. Count PLC events.** Count `lc3_decode == 1` in the AFE path (it is
already counted on the WAV path). Direct measure of bitstream corruption; a
degraded-link session with zero PLC strongly favors clean-but-slow delivery.

### Tier 3 — pipeline validation resumption (after cadence is explained)

**T9. PDM live-STT control run.** Same probe, PDM source: validates the
STT leg on known-good audio through the identical live plumbing; isolates the
G2 BLE hop as the only unproven segment.

**T10. Gain normalization experiment.** Speech sat at −31…−34 dBFS. Normalize
on the Pi before Moonshine (or fix capture gain) and A/B the corpus + real
captures. Cheap accuracy lever independent of everything else.

**T11. Re-run the native gate with the fix + preflight gate.** Require
healthy idle fps and HIGH conn interval before capture; then the 1.0 s /
8-chunk / 0.8 s-soft / 2.0 s-hard decision parameters stand as recorded.

**T12. Then the planned matrix.** Corpus repair (003/004, negatives), medium/
small/tiny at 0.5/1.0 s, batch `Yeah.` no-speech issue, Zipformer only if
Moonshine misses — per PLAN:211-218 / ASSESS §9. Unchanged.

## 5. Is live STT feasible? — verdict

**Yes, with one leg still unproven.** Every stage except G2→XIAO BLE delivery
is now hardware-proven end-to-end:

- XIAO capture/shadow/UART: byte-exact parity (PDM and G2), four-fault matrix
  passed, zero loss at 2 Mbaud, first-PCM latency 250 ms.
- Pi ingest: 8-chunk FIFO reached only 3/8 under the real run; no overflow.
- Moonshine medium-streaming: on the replay corpus at 1.0 s it *beat* batch
  (1 vs 2 errors / 26 words), zero hallucinated finals on negatives
  (batch, not streaming, is the one that hallucinates `Yeah.`), and on
  hardware it returned its final 51 ms after END.

The accuracy failure that stopped Gate 3 is downstream of a transport-rate
anomaly that (a) was measurable at preflight before the run started,
(b) has a plausible, already-instrumented mechanism (BLE conn interval), and
(c) per the pitch/content forensics may not even have damaged the audio —
possibly only delayed it. None of that is structural. The structural risks
that remain are the ordinary ones already on the books: G2 audio is quiet and
32 kbps-robotic (T1/T10 quantify whether that matters), the partial-update
cadence misses the 1.35 s interactivity policy by ~40 ms (telemetry, not
retention), and the G2 renderer question-path gates are still open for the
production display leg.

## 6. Immediate next actions

1. **T1** — batch-STT both existing WAVs on the Pi (minutes, decisive).
2. **T2** — the one listening question: anything missing?
3. **T3** — add `g2connpri`/`g2envgap`/`wifistatus`/idle-fps to preflight and
   capture them for the next healthy and next degraded session.
4. Based on T1/T5: either fix link management (H-late) or add loss
   detection/retransmit strategy (H-loss), then T6/T7 as the permanent
   instruments, then resume the gate ladder at T11.

Evidence inventory for this report: `.scratch/native-live-stt-GBMcqNF1`
(anomalous), `.scratch/native-shadow-LANSCdQ1` (healthy),
`.scratch/gate0a-v3-results-Pn5hEAy0` (replay decision basis),
`.scratch/gate0a-medium-compare-9IZdJLvv` (4-chunk era, superseded).
