# Audio parity — did the host get exactly what the device captured?

**Question:** a device captures audio and simultaneously streams it to a host.
Are the streamed samples byte-identical to the capture the device kept? And when
something goes wrong mid-stream, does the retained capture survive intact?

**Profile fields:** `HW_HOST`, `HW_USER`, `HW_SERVICE`, `HW_LINK_DEV`,
`HW_PROBE`, `HW_CFG`, `HW_AUDIO_SRC`, `HW_EVID_ROOT`.

**Applies to:** any "shadow" or tee transport where a device writes a canonical
artifact (a file) and concurrently ships the same samples over a link. The
canonical artifact is the referee.

## 1. The invariant

> The live stream and the retained capture are the same samples, and the
> retained capture survives every failure of the live stream.

Both halves matter. The second is what makes the feature safe to enable by
default-off in production: a shadow transport that can damage the recording it
shadows is worse than no shadow at all.

## 2. Preconditions that invalidate a parity run

Check these *before* collecting, because each produces a confusing failure that
looks like a transport bug:

- **Stale buffered samples.** If the source keeps a decoded ring or an
  echo-cancellation buffer, samples captured *before* the recording claim can
  burst into the stream at open, so the stream legitimately contains more
  samples than the file. Trim the buffer to the exact owner at capture claim,
  before admitting the first frame. A source with no software history (a direct
  hardware mic) needs no trim — but say which case you are in.
- **Any trimming applied to one side only.** Voice-activity trimming on the
  retained file makes byte parity *inapplicable*, not *failed*. Record that
  distinction explicitly in the result rather than logging a parity failure.
- **Exclusive ownership.** Confirm the capture owner is who you think it is, and
  that the link is not held by the running service.
- **The far device is actually sending.** A peripheral can accept a
  start-capture command and send no frames at all if a precondition is unmet
  (no active session, display asleep). Verify frames are flowing before you
  start timing anything.

## 3. Happy path

1. Preflight: read source, owner, link state, and frame counters. Record them.
2. Claim the recording, trim the buffer to the exact owner, then admit the
   stream's BEGIN before the first read.
3. Collect for a fixed sample count, not a fixed wall time.
4. Stop. The stream's END must occur only *after* the retained file is closed
   and the queue has drained.
5. Fetch the retained file independently over your bulk/file path.
6. Compare: sample count, byte count, and a CRC/hash over the PCM payload of
   both sides. All three, not just one.

Record for each run: sample count, byte count, CRC, END reason code, dropped
samples, device queue high-water (as `n/depth`), overflow count, host-observed
late-frame count, and first-PCM latency.

A pass looks like: identical counts and CRC on both sides, END reason nominal,
zero drops, queue high-water below depth, zero overflows.

## 4. The fault matrix — the part that actually earns trust

The happy path proves the feature works. The fault matrix proves it fails
safely. Inject each of these deliberately and grade the run a success only when
the retained capture survives *and* the terminal accounting matches:

| Fault | Injected by | Required outcome |
|---|---|---|
| Host queue overflow | starve the host consumer | only the host receiver is invalidated; device retains exact count/CRC metadata for the exchange |
| Host-observed frame gap | drop a frame at the host | same as above; the gap is reported, not hidden |
| Host-requested abort | explicit abort mid-stream | distinct terminal reason; admitted prefix count/CRC matches the retained file |
| Lease/ownership expiry | let the capture lease lapse | distinct terminal reason; retained file intact |
| Link/auth loss | drop the link mid-stream | abort, never a corrupted retained file |

Two properties to check in every fault case:

- **Post-invalidation traffic is tombstoned.** Frames arriving after the
  receiver was invalidated must be discarded silently rather than counted as new
  faults — otherwise one fault inflates into a cascade in the metrics.
- **The failure is scoped.** A shadow failure must never fail the recording.

## 5. Reading the counters honestly

- **Cumulative counters are baselines, not assertions.** A device-side overrun
  counter can advance during the unattended interval between stop and fetch
  without any corruption of the admitted samples. Take a delta around the window
  of interest instead of demanding a final zero.
- **Know what your counter reset does *not* clear.** A reset that clears
  raw-frame and decode-failure counters may leave the decoded ring and overrun
  untouched; only a fresh feed start resets those.
- **CRC equality has a blind spot.** Equal live/file CRCs cannot reveal a decode
  failure or a zero-wait buffer drop *upstream* of the point where both sides
  were tapped. Pair the parity check with the upstream counters.
- **One clean run is a correctness smoke, not a latency or reliability result.**
  A single first-PCM latency figure is one sample; p95 needs a repeated run.

## 6. Gate

Define the exit criteria before running, e.g.:

- zero missed reads at the recorder's chunk period;
- zero unexplained offset/CRC gaps;
- no regression in the peripheral link, storage, watchdog, or command latency;
- capture-claim-to-first-frame p95 within budget (record wake-to-first-frame
  separately — it is a different clock);
- every injected failure leaves an intact retained capture.

State which criteria are met physically and which are still open. "Implemented
and unit-tested" is not "physically met" — keep software test counts and
hardware evidence in separate paragraphs so nobody reads one as the other.
