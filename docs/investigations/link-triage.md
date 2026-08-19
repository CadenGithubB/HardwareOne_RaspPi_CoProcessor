# Link triage — is the audio late, or lost?

**Question:** a streaming source is delivering fewer frames per second than its
nominal rate. Is the audio *complete but delayed* (the link is coalescing
delivery into fewer, larger windows), or is it *damaged* (frames are genuinely
being dropped)? These need opposite fixes, so diagnose before coding.

**Profile fields:** `HW_HOST`, `HW_USER`, `HW_SERVICE`, `HW_LINK_DEV`,
`HW_PROBE`, `HW_CFG`, `HW_AUDIO_SRC`, `HW_EVID_ROOT`.

**Applies to:** any packetized audio/telemetry source with (a) a negotiable
delivery cadence and (b) a sequence counter on the wire. BLE connection interval
is the worked example; the same structure fits USB polling intervals, Wi-Fi
power-save beacons, or a UART with flow control.

**Assumes the link rate itself is sound.** Everything below diagnoses *delivery*
over a link that basically works. If the underlying rate is marginal — corruption
that comes and goes, errors that scale with throughput rather than with what the
peripheral is doing — that is a different question, and it has a harness rather
than a runbook: [`uart-baud-test/`](../../uart-baud-test/README.md) sweeps rates
with CRC32-framed, sequence-numbered traffic and grades each one PASS /
MARGINAL / FAIL / UNSUPPORTED per direction. Settle that before spending a
session on cadence.

## 1. The hypothesis pair

| | **H-late** — coalescing | **H-loss** — real frame loss |
|---|---|---|
| Delivery interval | slow (multiple frame-periods per event) | adequate |
| Loss counter | **flat** | **climbing** |
| On-wire sequence | contiguous | skips |
| Audio content | complete, arrives in bursts | gaps, damaged |
| Fix direction | make the link faster / hold a fast mode | remove the interferer |

They predict opposite values of both the interval and the loss counter. That is
what makes the test in §3 decisive rather than merely consistent.

## 2. Before touching hardware — is the audio actually damaged?

Costs nothing and can end the investigation. Run the recognizer in batch mode
over captures you already have: a known-good capture, the anomalous capture, and
gain-boosted copies of both. Add any "reinterpret the sample rate" variant
you are suspicious of — that is the cheapest way to kill a wrong theory.

If batch recognition of the anomalous capture returns the correct transcript,
the samples survived: you are in **H-late**, and no amount of link work will
change accuracy — only latency. If the transcript is degraded in proportion to
the missing frames, you are in **H-loss**.

Have a human listen to both captures too. "Are words missing, or is it merely
quiet and robotic?" is a distinction the WER number does not give you.

## 3. The decisive test — do this first

During a **degraded capture**, read the delivery interval and the loss counter
**at the same instant** the rate is observed to be halved:

| Reading | Verdict | Go to |
|---|---|---|
| interval **slow**, loss **flat**, sequence **contiguous** | **H-late** | §5 A/B |
| interval **adequate**, loss **climbing**, sequence **skips** | **H-loss** | §5 C |

Confirm with a raw packet dump and diff the on-wire sequence field across
packets: contiguous-at-half-rate = late; skipped = lost. The sequence trailer is
usually the only *real* counter — host-side frame counts cannot distinguish the
two, because both look like "fewer frames arrived".

Cheap supporting checks:

- Compare the **idle** cadence against a healthy session at preflight. If the
  source is already degraded before capture starts, the condition predates your
  pipeline and can be gated on.
- Correlate the loss inflection against anything else that touched the radio or
  bus in the journal — a reconnect, a scan, another peer coming up.
- Grep the link log for controller-refused parameter requests: a request that
  returns "queued OK" locally may still be rejected asynchronously by the
  controller, leaving the link slow while your code believes it is fast.

## 4. Controlled cadence measurement

When you need the interval↔cadence correlation itself, measure four windows of
equal length (12 s is enough), each bracketed by link-state reads:

| Window | State |
|---|---|
| A | as found |
| B | interval forced to the fastest the controller will accept |
| C | interval restored to default |
| D | after a disconnect/reconnect cycle |

Reset the frame counters at the start of each window. Window D matters: if a
reconnect clears the degraded state, the problem is a *stuck* negotiation rather
than a structural limit, which changes the fix from "request faster" to "detect
and re-request".

Skeleton (fill in your own probe commands):

```bash
EVID="$(cat "$HW_EVID_ROOT/link-triage-latest.txt")"

SERVICE_WAS_ACTIVE=0
systemctl --user is-active --quiet "$HW_SERVICE" && SERVICE_WAS_ACTIVE=1
restore() { [ "$SERVICE_WAS_ACTIVE" -eq 1 ] && systemctl --user start "$HW_SERVICE"; }
trap restore EXIT
[ "$SERVICE_WAS_ACTIVE" -eq 1 ] && { systemctl --user stop "$HW_SERVICE"; sleep 2; }

fuser "$HW_LINK_DEV" >/dev/null 2>&1 && { echo "link held — aborting"; exit 1; }

probe() { local log="$1"; shift; "$HW_PY" "$HW_PROBE" --config "$HW_CFG" cmd "$@" 2>&1 | tee -a "$EVID/$log"; }

probe window-A.log '<open-source>' '<read-link-state>' '<reset-counters>'
sleep 12
probe window-A.log '<read-frame-stats>' '<read-link-state>'
```

## 5. Fix families

Pick from the regime the test selected. Order of operations is telemetry first —
it protects accuracy regardless of which regime you are in.

**Telemetry (do first, both regimes).** Parse the on-wire sequence with
per-packet inter-arrival; expose a rolling delivered-rate watchdog in the
service's status output; and stamp a `degraded_rate` flag into the metadata the
consumer sees, so the recognizer **falls back to the offline capture with a
warning instead of transcribing a degraded stream**. Silent transcription of a
lossy stream is how a mishear reaches the user with full confidence.

**A — hold the fast mode for the whole capture, not just part of it.** A common
bug: the fast-link request is scoped to one feature (recording) while another
path (live streaming) opens the source without it, so the link never leaves its
resting interval. Verify by reading the interval *during* capture, not before.

**B — handle asynchronous parameter refusal.** With several links up, a
controller may refuse the most aggressive interval outright. Fall back to the
next admissible value and read the asynchronous verdict rather than trusting the
local queue-accepted return code.

**C — remove the contender (H-loss).** Find what else is using the radio/bus and
gate it: a peer whose auto-reconnect churns the medium during capture, a retry
loop that cannot succeed because a precondition is missing, a scan running
concurrently with the stream. Gate the retry on the precondition it actually
needs instead of letting it fire and fail.

**D — split conflated thresholds.** If one "is the link slow?" predicate serves
two consumers with different requirements, an adequate-for-audio interval gets
flagged slow and triggers pointless re-requests. Give each consumer its own
threshold.

**E — buy margin at the wire (serial links).** If the fix is to slow the rate or
add stop bits, do not guess at the new value: sweep it. `uart-baud-test` grades a
candidate rate MARGINAL on a nonzero byte-error rate *below* its failure
threshold, which is exactly the regime that ships and then bites — and it soaks
the survivors, on the principle that a rate is only trustworthy at the duration
you actually tested.

## 6. What a run cannot tell you

- A single session establishes a regime *for that session*. Two sessions with
  different numbers are two regimes, not one theory with noise; do not average
  them, and do not let a strong result from an earlier session define the
  current one.
- Host-side counters cannot separate late from lost. Only the on-wire sequence
  can.
- Byte-level equality checks downstream of a decoder cannot reveal decode
  failures or drops that happened *upstream* of where the counter is kept.
