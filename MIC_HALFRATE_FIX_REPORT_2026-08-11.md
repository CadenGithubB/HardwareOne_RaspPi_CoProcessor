# G2 mic half-rate / frame-loss — root cause & fix report

**2026-08-11 · investigation only, no code changed.** Read-only trace of the firmware
mic path + BLE conn-interval arbiter + R1-ring contention, cross-checked against the
2026-08-10 triage docs and today's field run, with an adversarial pass over the leading
hypothesis. Continues [`LIVE_STT_TRIAGE_2026-08-10.md`](LIVE_STT_TRIAGE_2026-08-10.md)
and [`LINK_TRIAGE_RUNBOOK_2026-08-10.md`](LINK_TRIAGE_RUNBOOK_2026-08-10.md).

## TL;DR

The mic delivers fewer frames than the 20 packets/s it should. There are **two distinct
regimes**, and they need **opposite fixes**, so *diagnose before coding*:

- **H-late (interval coalescing).** The BLE link rests at a slow connection interval
  (105 ms) during capture, so notifications arrive at ~half cadence. Audio is *complete
  but late*; the on-glasses sequence counter stays contiguous (`lost` flat). This is the
  **prior clean-2:1 session** (16.03 → 7.996 ksamples/s) and is well-evidenced *for that
  session*.
- **H-loss (real frame loss).** The link is at an *adequate* ~30 ms interval but packets
  are genuinely dropped on-air; the sequence counter skips (`lost` climbs). This is
  **today's run** (~14–16 fps with `lost` 110 → 691), most likely driven by **R1-ring
  radio contention** — the 16:48 ring auto-reconnect that churned the radio and then
  failed `clock-unavailable`.

**Do the one decisive test first (§3). It needs no code change and it tells you which
regime you're in — the two predict opposite values of both the interval and the loss
counter.** The SD card is *not* involved (§1).

## 1. How the mic path works (and what it rules out)

Mic audio arrives as BLE GATT notifications on characteristic `…6402`
(`CHAR_AUDIO_NOTIFY`), handled in `handleAudioNotify`
([G2_Glasses.cpp:2646](../components/hardwareone/G2_Glasses.cpp:2646)):

- **One packet = 205 B** = 5 LC3 frames × 40 B (32 kbps, 10 ms/frame) + a 5 B trailer.
  Each packet carries **50 ms** of audio → **20 packets/s nominal** (the watchdog's
  "20.0/s nominal").
- **`frames`** increments per notify callback (`m.frameCount++`, :2651). **`rate`** is a
  rolling 2 s window of delivered frames (:2658-2662). Both fall directly with callback
  cadence.
- **`lost` / `gap_events`** come *only* from the trailer sequence byte `data[204]`, which
  is stamped **on the glasses** (:2666-2687): `delta>1 ⇒ lost += delta-1`. So `lost`
  measures **emitted-but-undelivered** packets — a true on-wire/source discontinuity.
- **`stalls`** fire on a >500 ms inter-arrival gap and *re-baseline* (:2668-2679), so a
  brief outage reads as `stalls+1` with **no** phantom loss.

**Critical consequence:** every stat is computed **inline in the notify callback, before**
the SD-record ring, WAV writer, and ESP-SR AFE consume the packet. A full SD ring, a WAV
FS-lock miss, or an AFE mutex drop **cannot** reduce `frames`/`rate`/`lost`. **The halving
must originate at or above BLE notify delivery — not in the SD/decode pipeline.** (This is
why the SD card, though on the *batch* voicefetch critical path per the perf record §4c,
is not implicated here.)

The two counters are a **built-in discriminator**:

| Symptom | `rate` | `lost` | Meaning |
|---|---|---|---|
| Halved cadence, contiguous seq | ~half | **flat** | **H-late** — BLE coalescing / doubled interval (audio late, complete) |
| Reduced cadence, skipping seq | low | **climbing** | **H-loss** — real dropped packets (radio contention / source drops) |

## 2. The two regimes (honest split)

The adversarial pass **weakened** the "one dominant cause = slow interval" framing. The
interval-coalescing story is strong **for the prior clean-2:1 session** but today's data
cuts against it on its two hardest numbers:

- **Interval:** today's links sat "mostly at int=24 (30 ms)." At 30 ms a link fires ~33
  connection events/s against a 20-frame/s source — the interval is **not** the binding
  constraint, so it cannot *mechanically* halve delivery.
- **Loss:** today `lost` climbed 110 → 691 and `gap_events` climbed steadily. By the
  discriminator above, that is **real loss, not lateness**. Pure coalescing keeps `lost`
  flat.

So the prior session and today are most likely **different regimes**, and treating the
slow-interval hypothesis as the single cause **overfits the earlier run**.

### Ranked root causes

| # | Cause | Confidence | Regime | Key evidence (file:line) |
|---|---|---|---|---|
| 1 | **Link rests at slow 105 ms interval during capture** → half cadence | High *(prior session)* | H-late | 2.000:1 halving + 15 ms=~20fps / 105 ms=~9.5fps correlation; arbiter asserts nothing when no FAST depth held ([G2_Glasses.cpp:12659](../components/hardwareone/G2_Glasses.cpp:12659)) |
| 2 | **R1-ring reconnect churn → radio-contention on-air loss** | Medium *(today)* | H-loss | `lost` 110→691; 16:48 ring auto-reconnect forced both temples to BALANCED then failed clock-unavailable; ring holds BALANCED for the whole attempt ([G2_Ring.cpp:2636](../components/hardwareone/G2_Ring.cpp:2636)); 3 HIGH links ≈83% radio occupancy ([G2_Glasses.cpp:1474](../components/hardwareone/G2_Glasses.cpp:1474)) |
| 3 | **FAST hold is *recording*-scoped, not *capture*-scoped** — live-STT never acquires FAST | Medium | H-late (sub-cause of #1) | `g2MicLinkFastAcquire` only under `gRecWasG2Source` ([System_Microphone.cpp:1498](../components/hardwareone/System_Microphone.cpp:1498)); mid-capture re-assert gated on `FastDepth>0` ([G2_Glasses.cpp:10832](../components/hardwareone/G2_Glasses.cpp:10832)); link-up no longer requests HIGH ([:11494](../components/hardwareone/G2_Glasses.cpp:11494)) |
| 4 | **3 links up → controller refuses 12-12 FAST (status=19), link stays slow** | Low | H-late | min=12 with 3 links → HCI 0x12/status 19, never sent ([:12448](../components/hardwareone/G2_Glasses.cpp:12448)); requester treats queued `ESP_OK` as success, ignores async GAP verdict ([:12757](../components/hardwareone/G2_Glasses.cpp:12757) vs [:12480](../components/hardwareone/G2_Glasses.cpp:12480)) |
| 5 | **`g2LinkIsSlow` conflates 30 ms and 105 ms** → re-assert wastes refused round-trips | Low | efficiency | `kConnIntFastMaxTicks=16` marks anything >20 ms SLOW ([:19980](../components/hardwareone/G2_Glasses.cpp:19980)); re-assert re-fires the same refused 12-12 ([:10832](../components/hardwareone/G2_Glasses.cpp:10832)) |

Note #2's tie-in: the ring failing **`clock-unavailable`** is the *same* clock gap seen
in the R1 setup handshake — fixing it kills two birds (ring connects **and** stops
churning the mic radio).

## 3. Decisive test — DO THIS FIRST (no code change)

During a **degraded capture**, read `g2connpri` for the mic temple **at the same instant**
`g2micstats` reports the halved rate, and note whether `seqPacketsLost` is climbing:

| Reading | Verdict | Fix path |
|---|---|---|
| interval **~84 ticks/105 ms**, `lost` **flat**, trailer seq **contiguous** | **H-late** (#1) | §4 fixes A + B |
| interval **≤24 ticks/30 ms**, `lost` **climbing**, trailer seq **skips** | **H-loss** (alt/#2) | §4 fix C |

This is *diagnostic, not merely consistent* — the two hypotheses predict **opposite**
values of both the interval and the loss counter. Confirm with a `g2micrec` dump and diff
trailer bytes 200-204: contiguous-at-half-rate = late; skipped = lost.

Supporting cheap checks: compare `g2micstats` **idle** vs a healthy session at preflight
(~14 vs ~19–20 fps ⇒ degraded predates capture, gateable); correlate the `lost` inflection
against the 16:48 ring reconnect in the journal; grep the GAP log for `status=19
CTRL-REFUSED` (confirms cause #4).

## 4. Fixes (ranked; each maps to a cause + regime)

Order of operations: **telemetry first** (E — makes the failure observable and protects
accuracy regardless of regime), then the regime-specific fix the §3 test selects.

**A. Hold FAST for *all* live-STT capture, not just `micRecording`.** *(cause #3, mitigates
#1 · effort low · risk medium)* — route live-STT capture through the recorder's FAST
acquire, or call `g2MicLinkFastAcquire` for idle-open capture too, so `FastDepth>0` and the
mid-capture re-assert can fire. *Where:*
[System_Microphone.cpp:1498](../components/hardwareone/System_Microphone.cpp:1498),
[G2_Glasses.cpp:10832](../components/hardwareone/G2_Glasses.cpp:10832). *Verify:* during
capture `g2connpri` shows 12-x ticks (not 84), `g2micstats` ~20/s, `lost` flat.

**B. On a 3-link 12-12 refusal (status=19), fall back to 12-24 (30 ms) and read the async
GAP verdict instead of trusting `ESP_OK`.** *(causes #4, #5 · effort medium · risk medium)*
— 12-24 lands at 24 ticks (30 ms), which the controller admits with 3 links and which still
sustains 20 fps. *Where:*
[G2_Glasses.cpp:1462](../components/hardwareone/G2_Glasses.cpp:1462),
[:12757](../components/hardwareone/G2_Glasses.cpp:12757),
[:12480](../components/hardwareone/G2_Glasses.cpp:12480). *Verify:* both temples + ring up,
force re-assert, GAP log shows applied interval 24 and no status=19.

**C. Gate R1-ring auto-reseek on a valid clock.** *(cause #2 — today's H-loss · effort low ·
risk medium)* — ring setup provably cannot pass without a clock, so a clock-unavailable
attempt only churns the radio. Gate `bleAutoReconnectTick` for `BLE_PEER_R1_RING` on
`Clock::isValidEpoch()`. *Where:*
[BLE_Peers.cpp:412](../components/hardwareone/BLE_Peers.cpp:412) (existing capture pre-empt
at [G2_Ring.cpp:2660](../components/hardwareone/G2_Ring.cpp:2660)). *Verify:* with no valid
clock, no BALANCED requests during capture; `lost`/`gap` flat across a 2-min session.

**D. Split the mic-cadence slow threshold from the image-gap threshold.** *(cause #5 · effort
medium · risk low)* — treat ≤24 ticks (30 ms) as adequate for mic; escalate the genuine
105 ms case by requesting 12-24 instead of re-firing 12-12. *Where:*
[G2_Glasses.cpp:19980](../components/hardwareone/G2_Glasses.cpp:19980),
[:10832](../components/hardwareone/G2_Glasses.cpp:10832).

**E. Production telemetry (do first).** *(observability + accuracy guard · effort medium ·
risk low)* — parse the trailer sequence with per-packet inter-arrival, expose a rolling
delivered-rate watchdog in `liveaudio status`, stamp a `degraded_rate` flag into the UART
live metadata so **the Pi gates STT (falls back to WAV + warning) instead of transcribing
degraded audio**, and add a PLC/`lc3_decode==1` counter. *Where:*
[G2_Glasses.cpp:2646](../components/hardwareone/G2_Glasses.cpp:2646),
watchdog [:10848](../components/hardwareone/G2_Glasses.cpp:10848). This directly protects
against the L→M-style mishears: today the pipeline silently transcribed a lossy stream.

## 5. Caveats (what we do NOT yet know)

- **No interval was captured at the original 09:51 clean-2:1 event** — the interval
  attribution there is inference-by-consistency, not a direct reading.
- **The first confirming A/B read zero frames** because no display container was active
  (G2 ignores mic-enable without one). The §3 A/B must run with an active container.
- **Clean-2:1 (prior) vs ~14 fps-with-loss (today)** strongly suggests **both** mechanisms
  are real and situational — not one universal cause. Fix E lands first precisely so the
  next occurrence self-identifies its regime.
- Whether today's live-STT path actually routes through `micRecording` (which *would*
  acquire FAST) is an open question that fix A resolves either way.

**Nothing here has been changed.** Recommended next action is the §3 read on the next
degraded capture, then fix E, then the regime-specific fix.
