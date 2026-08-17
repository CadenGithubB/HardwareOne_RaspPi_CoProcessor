# Live STT to native G2 question: execution plan

**Status:** counter-reviewed plan of record, 2026-08-09. The five-state recorder,
exact native ownership/dismissal, synthetic transport, and Phase 2B default-off
recorder tee are implemented and XIAO-built. Capabilities are
`synthetic=1 recorder_shadow=1 shadow_default=off`. The standalone real-audio
probe and host simulation exist. Physical PDM recorder-shadow parity passed.
The post-fix G2 rerun deliberately filled the 32,768-sample decoded ring, then
passed exact live/WAV parity at 100,000 samples and CRC32 `56ebd586`, proving
the capture-boundary flush removed the stale 2.048-second prefix without a
shadow overflow. The physical four-fault matrix also passed for host overflow,
host gap, host ABORT, and lease expiry while preserving a canonical WAV. The
native no-STT provenance smoke then passed for one real `Hey Even` capture,
with exact ID/epoch/event/path correlation, valid zero-drop LIVE END,
independent canonical trimmed WAV, and exact cleanup. The paced Moonshine v3
mixed-slice replay has now run physically at 0.5- and 1.0-second update floors.
Both eight-slot runs preserved every PCM chunk without throttling or overflow;
the streaming final stayed empty for all four reviewed static/no-speech files,
while batch reproducibly hallucinated `Yeah.` on neg001 at both cadences. A
default-off native live-STT shadow worker is implemented next, but its physical
real-G2 run, full Gate 0A, production streaming STT, and revisioned partial
questions remain pending.

This plan supersedes the live-STT / G2 question-display assumptions in
`CM5_AI_SERVICE_PLAN.md` and `investigation/findings_sttResearch.md`. Those
documents remain useful background, but their latency estimates and Moonshine
endpointing claims predate the current source and this review.

## Objective and fixed constraints

Move STT work underneath speech and the XIAO's existing VAD tail, then put the
recognized question into the native G2 listening window as early as the G2
protocol safely permits.

The following are fixed:

- The G2 must finish its native question transition before answer text replaces
  it. The Pi may run the LLM concurrently, but answer delivery remains gated.
- Conversation history remains eight turns. LLM model/answer-length changes are
  outside this work.
- The finalized transcript, not a mutable partial, is the LLM prompt.
- The finalized WAV/`voicefetch` path remains the correctness fallback until
  live transport and streaming STT pass field validation.
- A late frame, partial, stop, cancel, or reply must never affect a newer wake.

## Counter-review findings

1. **Repeated cumulative ASK is protocol-intended, not yet hardware-proven here.**
   Both bundled phone-role references send every partial STT hypothesis as a
   complete cumulative `ASK{text=...}`. They keep `cmdCnt=0` and
   `streamEnable=0`; ASK has no delta/end protocol like REPLY. Our hardware
   record validates one ASK only. Repeated ASK may preserve the common prefix,
   restart the renderer, or queue repaints. This is the first G2 proof gate.

2. **The stock phone sends EvenAI CONFIG before streaming, and 80 is confirmed
   slower than 40 for final-answer rendering.** A captured stock
   packet contains wrapper field 13 with `voiceSwitch=0`, `streamSpeed=80`, and
   `duplexMode=0`. A raw copy of that shape was accepted and echoed by G2
   firmware 2.2.7. The typed builder now emits field 13 and includes the newer
   duplex field; its golden test uses the exact accepted payload. Field-only
   80 -> 40 -> 80 reversals completed at 1,131 -> 566 -> 1,126 ms for a
   14-character final reply and 2,493 -> 1,061 -> 2,467 ms for a 30-character
   final reply, measured from text TX to `STREAM_COMPLETE`. The reversal proves
   field causality and larger-is-slower direction for answers. It does not prove
   literal milliseconds-per-character units or any ASK/question effect; the
   test question was only `Ready.` and ANALYSE timing was host-controlled.

3. **Moonshine streaming is real, but its partials are mutable.** Version 0.1.1
   emits optional `LineTextChanged` events and immutable `LineCompleted` events.
   A completed line is not necessarily a completed user turn. The nominal
   500 ms update interval can widen when a native inference pass is slow, and
   `stop()` can return no transcript after an internally reported error.

4. **The earlier 1.4 s saving is a hypothesis, not a result.** The measured
   removable post-capture work is about 0.98 s transfer plus 0.95 s batch STT.
   Live streaming could hide roughly 1.1-1.9 s under speech and the 1.8-1.92 s
   XIAO VAD tail. Actual END-to-final latency, load, and WER decide the result.

5. **Post-stop burst streaming is not an end-to-end live-question proof.** It
   can overlap `voicefetch` and STT by at most the shorter measured stage
   (~0.95 s). `voicefetch` owns the XIAO command executor and the Pi Session
   command lock until the burst finishes, so partial ASK commands cannot reach
   the G2 during that transfer.

6. **Lifecycle, control, and optional recorder shadow are exactly scoped.** The recorder remains busy through
   FINALIZING/close/events,
   and a new start atomically claims STARTING before resetting capture globals.
   One boot-nonce/counter exchange ID now owns wake, autostop, cancel,
   status/stop/discard/delete, final ASK, and every reply mutation. EXIT is
   arm/generation/UART-epoch bound and fences later BLE fragments. Recorder
   shadow consumes a one-shot exact `{exchange, controller, UART login epoch}`
   authorization and routes post-DSP/VAD PDM or G2 samples through a strict
   16 KiB PSRAM SPSC. The standalone probe compares untrimmed live PCM with the
   finalized WAV. The PDM source passed physical parity. A corrected G2 run
   isolated a pre-capture AFE backlog, and the resulting boundary fix passed
   its deliberate full-ring physical rerun. The controlled four-fault physical
   matrix also passed. The first native live-STT run subsequently proved the
   transport/queue/finalization path but failed its pinned-text accuracy check:
   Moonshine returned `Haitian is the capital difference.` for `what is the
   capital of france` (3 word errors).

7. **The display and transport caps differ.** The Pi permits a 1900-byte ASK,
   but current protobuf overhead leaves only 236 text bytes in the single G2
   envelope. At 237 bytes the builder fails rather than safely truncating at its
   apparent 250-byte cap. Live question text will use a separate,
   UTF-8/word-safe projection capped at 220 bytes. The LLM still receives the
   full final transcript, and an unsuccessful ASK aborts answer delivery.

8. **The August 9 bundle captured the wrong Pi source and no benchmark.** The
   virtual environment points at `/home/$CM5_USER/hw1-ai-service`, while the copied
   `/home/$CM5_USER/ai-service` tree shadowed imports and rejected the live config's
   `power` section. The batch JSONL is empty. Converge the deployment using
   `CM5_DEPLOYMENT_PATHS.md` before measuring or editing production behavior.

## Target data flow

```text
G2/PDM PCM                              (implemented, explicit shadow arm only)
  -> XIAO recorder (post source processing)
       -> canonical WAV saved/discarded/failed result
       -> bounded nonblocking live queue
            -> UART LIVE_BEGIN / LIVE_PCM / LIVE_END
                 -> Pi capture inbox keyed by exchange_id
                      -> standalone live/WAV parity + voicefetch (implemented)
                      -> one bounded Moonshine worker (diagnostic implemented;
                                                       production disconnected)
                           -> hypotheses (telemetry first)
                           -> final transcript after END + CRC + stop()
                                -> final native ASK + LLM request in parallel
                                -> answer waits only for remaining G2 render
```

Provisional partial ASK is an optional later branch, never part of the initial
correctness path.

## Phase 0A - real-time-paced STT replay (no firmware change)

Use [`ai-service/tools/moonshine_stream_replay.py`](ai-service/tools/moonshine_stream_replay.py)
to feed saved XIAO WAVs in 4096-byte /
128 ms chunks at wall-clock cadence. Every condition supplies an existing model
directory and an explicit `ModelArch`; the probe must never resolve or download
a model implicitly. The first guarded run is deliberately narrower: deployed
medium-streaming, a 0.5-second update floor, default real-time pace, an
eight-chunk / 32 KiB / 1.024-second Pi worker queue, batch baseline enabled,
trusted labeled cases 001, 002, and 005, and human-audited static/no-speech
controls neg001 through neg004. The queue is a FIFO jitter
buffer: the paced producer keeps enqueuing while synchronous native inference
runs, and the sole Moonshine worker drains the backlog immediately when
`add_audio()` returns. This default does not change the XIAO shadow queue or
the Pi UART receiver inbox.
Grade it on the CM5 with
[`ai-service/tools/moonshine_stream_replay_check.py`](ai-service/tools/moonshine_stream_replay_check.py)
and the hash-pinned
[`ai-service/tools/moonshine_gate0a_medium_slice.json`](ai-service/tools/moonshine_gate0a_medium_slice.json)
before pulling artifacts, because the JSONL contains absolute Pi paths.

The manifest pins its semantic contract hash, exact deployed model
directory/architecture/enum, Moonshine 0.1.1 runtime, policy, corpus directory,
hashes/counts, and per-case absolute error ceilings. The checker independently
recomputes WER from the sidecars and
fails closed on run/schema/event/PCM topology, collector score consistency,
absolute and stream-vs-batch accuracy, END-to-final latency, temporally covered
pre-END partials, queue age/accounting, governor, temperature, swap excursion,
and before/after throttle state. Even a clean report is intentionally scoped as
`provisional_deployed_medium_mixed_slice`, sets
`full_gate0a_complete=false`, and warns that the manifest is provisional. It
has only three positive cases and four static/no-speech controls, so it remains
insufficient for model selection. Stop and review that 0.5-second result before running the
broader matrix. The superseded four-chunk v1 contract was physically measured:
a clean 0.5-second run processed every chunk but reached 507.6 ms queue age and
a 639.0 ms native call; a clean 1.0-second run reached 582.5 ms, overflowed,
and processed only 20/33 chunks of case 005. Those periodic stalls drained
after native inference returned, so contract v2 raised only the Pi worker
buffer to eight chunks and the maximum admitted queue age to 1024 ms. Contract
v2 physical reruns preserved every chunk at both cadences; contract v3 adds the
four confirmed no-speech controls without changing the queue.

### Physical v3 decision record — 2026-08-10

The retained v3 evidence is
`.scratch/gate0a-v3-results-Pn5hEAy0/{0500,1000}`. Both reports are correctly
`ok=false`: the checker found policy/accuracy issues, not a runtime crash.

- At 0.5 seconds, positive streaming and batch each made 2 errors across 26
  reference words. Maximum END-to-final was 0.459 s; case 005 reached queue
  high-water 4/8 and 510 ms maximum age. All chunks were processed. The strict
  partial-coverage gaps were 1.384 s on 002 and 1.430 s on 005, and streaming
  regressed case 005 by one word versus batch.
- At 1.0 seconds, streaming made 1/26 errors versus batch 2/26. Maximum
  END-to-final was 0.712 s; case 005 reached high-water 5/8 and 711 ms maximum
  age. All chunks were processed. The remaining strict partial findings were a
  1.389 s gap on 002 and only 3/4 useful updates plus a 1.406 s gap on 005.
- Streaming returned an empty final for all four static controls at both
  cadences. Batch returned `Yeah.` on neg001 in both processes and also
  prepended `Yeah` to positive case 002. This repeatability makes batch
  no-speech disagreement the next correctness issue; it is not evidence for a
  literal-word blacklist.
- Governors remained `performance`, `get_throttled` stayed `0x0`, the maximum
  observed temperature was 52.9 C, and neither queue overflow nor input-chunk
  loss occurred.

Decision: keep the Pi FIFO at eight 4096-byte chunks (32 KiB / 1.024 s). Start
the first real-G2 live-STT shadow at a 1.0-second update floor because it had the
better positive final accuracy while remaining below the 0.8-second soft final
target. Use 0.8 s as telemetry and 2.0 s as a hard post-END wait; neither value
changes Moonshine execution speed. Do not gate silence on RMS/peak or reject the
word `Yeah`: the reviewed noise and quiet-speech levels overlap, and a real
one-word utterance is valid. Only a valid streaming result may adjudicate an
empty/no-speech outcome; transport/model failure retains the WAV fallback.

Then run the broader matrix:

- The deployed Moonshine medium-streaming model, then explicitly downloaded and
  selected small- and tiny-streaming models, at 0.5 s and 1.0 s update floors
  (0.25 s diagnostic only).
- The current Moonshine batch path.
- A genuinely incremental Zipformer session only if Moonshine misses the gate
  or an engine control remains useful after the Moonshine matrix.
- Trimmed production WAVs and untrimmed captures matching the future live tap.

Record JSONL events containing audio time, wall time, line id/state, complete
snapshot, longest common prefix with the final transcript, native-pass time,
CPU time, RSS, queue age/high-water, and final status.

**Gate 0A:** no material final-accuracy regression versus the labeled/batch
corpus; p95 END-to-final <= 0.8 s; useful changed partials at least once per
second on ordinary >2 s utterances; no throttling, OOM, or event-loop work.
Choose Moonshine versus Zipformer from the repaired full corpus, not API
aesthetics. Passing the provisional mixed 0.5-second slice does not
close this gate.

## Phase 0B - native G2 renderer probe (no audio-transport change)

No camera recording is required. Use protocol timestamps for REPLY rendering
and simple wearer `complete` / `cut` observations for ASK, which has no known
completion event. Use one fresh wake session per condition.

The canonical runner is
[`ai-service/tools/g2_evenai_probe.py`](ai-service/tools/g2_evenai_probe.py);
CM5 commands are in
[`CM5_DEPLOYMENT_PATHS.md`](CM5_DEPLOYMENT_PATHS.md#no-camera-g2-render-diagnostics).

### Immediate renderer-state gate

The answer-render reversal is complete, and the production decision is to
submit field-only speed 40 when the Pi daemon starts:

1. Keep `deliver.g2_stream_speed: 40` for production. Set it to `0` only when a
   controlled test must preserve the glasses' current/no-CONFIG state; zero is
   an opt-out and is not transmitted.
2. At speed 40, run a long indexed-question threshold with
   randomized or reversed delays and record the exact last visible word.
3. Optionally power-cycle and collect a no-CONFIG baseline later with the
   daemon stopped or opted out. It remains useful for identifying the glasses'
   native default, but it is no longer a prerequisite for the production
   choice.

This gate decides question timing. `STREAM_COMPLETE` proves answer completion
only, and the 80/40/80 reversal's fixed `Ready.` ASK cannot substitute for a
long-question measurement.

### Direct versus progressive answer matrix

Alternate the same fixed 180-character answer between:

- one final REPLY;
- two immediate 87- and 93-character `replypart` messages plus `replyend`.

Run at least three fresh sessions per mode and measure first REPLY TX to
`STREAM_COMPLETE`. This directly tests whether multipart delivery selects a
slower native path. Do not vary CONFIG during this matrix.

**Initial-run result:** all 180 bytes reached the glasses in all six trials,
but the old probe forced EXIT after only 12.567-13.452 seconds from first text
TX. The completed two-length reversal confirms that CONFIG 80 selects the
slower answer-render state, but its two lengths do not justify an exact
180-character completion prediction. The missing events remain compatible
with the test having been too short and do not establish a stall, cap, or mode
winner. The immediate multipart finalizer was transmitted within
0.794-0.940 seconds, much faster than prior successful production streams.
Retry with at least a 20-second wait and add a paced multipart condition.

### Repeated cumulative ASK matrix

Send at both 500 ms and 1000 ms cadence:

```text
one
one two
one two three four
one two free four          # correction inside the suffix
one two                    # shrink
one two                    # duplicate
```

Repeat with a natural 100-character question. The initial fixed 98-character
ASK was still cut with native ASK-to-ANALYSE opportunities of 2.204, 2.712,
3.244, 3.717, and 4.240 seconds. More text appeared as delay increased, so it
was still progressing; no fixed ceiling was shown. The repeat must randomize
or reverse condition order and record the exact last visible indexed word,
not only wearer `complete` / `cut`.

Accept provisional ASK only if the wearer can repeatedly confirm that:

- an unchanged prefix does not redraw or flicker;
- corrections and shrink replace cleanly rather than append;
- stale/duplicate updates do not queue visible work; and
- final remaining render time follows the unrendered suffix, not full length.

If any condition fails, production sends exactly one finalized ASK. Live STT is
still worthwhile because it moves that final ASK earlier.

### CONFIG matrix

The raw field-13 bodies below are the protocol goldens used for the pre-fix
test. The corrected typed command accepts the equivalent three values, e.g.
`g2aiconfig 0 80 0`; bare `g2aiconfig` sends that captured stock shape. The
typed command uses a fresh magic value for every trial.

```text
g2probe 07 10 6A06080010502000      # exact stock: speed 80
g2probe 07 10 6A06080010282000      # speed 40
g2probe 07 10 6A07080010A0012000    # speed 160
g2probe 07 10 6A06080010002000      # explicit zero control
```

The causal test isolated field 2 in one instrumented connection:

```text
g2aiconfig - 80 -
g2aiconfig - 40 -
g2aiconfig - 80 -
```

The matrix used a fresh wake and the same final reply for each value, then
repeated the sequence with a second reply length:

| Reply | Speed 80 A | Speed 40 | Speed 80 B |
| --- | ---: | ---: | ---: |
| 14 characters, TX to complete | 1,131 ms | 566 ms | 1,126 ms |
| 30 characters, TX to complete | 2,493 ms | 1,061 ms | 2,467 ms |

The returned speed-80 endpoints are close enough to rule out simple drift, and
both lengths show that speed 40 is materially faster. This completes the
answer-render causality gate. It does not prove a millisecond-per-step model:
the extra 16 characters added about 84.47 ms each at speed 80 but 30.94 ms each
at speed 40 on the TX-anchored metric. Do not transmit a numeric speed of zero;
it may be a sentinel or special mode. The Pi configuration value zero means
skip the startup command. Do not send any CONFIG packet as a purported reset.

**2026-08-09 status:** stock speed 80 passed the protocol-acceptance gate. The
corrected typed builder also passed its post-flash smoke test: bare
`g2aiconfig` used fresh `magic=201`, and G2 firmware 2.2.7.14 returned matching
CONFIG field 13 `[10 50]` about 60 ms later. An empty HEARTBEAT probe in the
same session was accepted and echoed rather than returning COMM_RSP, so it was
not an error-decoder test. Pre-CONFIG streamed answers rendered at a consistent
~27.5 characters/second through `STREAM_COMPLETE`. A 14-character pre-CONFIG
control completed 497 ms after TX; five post-CONFIG-80 controls averaged
1,144.8 ms. From G2 response to completion, the same-length control changed
from 422 to 1,083.8 ms. The completed reversal now proves that the manually
submitted speed field caused the answer slowdown, and speed 40's 566 ms
14-character result is much closer to the old 497 ms baseline. Exact units and
the reason for the remaining 69 ms difference are unknown.

Following this result, the selected daemon policy is a best-effort startup
submission of `g2aiconfig - 40 -`. It changes only field 2; configuration value
zero disables the submission for a controlled no-CONFIG run. The next required
gate is a long indexed question at speed 40. ASK has no known
`STREAM_COMPLETE`, and this reversal used only `Ready.` with a fixed wait, so
answer timing must not be reused as a question barrier without that test. A
power-cycle/no-CONFIG baseline remains an optional default/persistence
experiment. Full trial tables are in
[`G2_EVENAI_RENDER_TEST_RECORD.md`](G2_EVENAI_RENDER_TEST_RECORD.md).

## Phase 1 - capture and session correctness (implemented; hardware regression test pending)

The five-state recorder lifecycle is now implemented and XIAO-built:

```text
IDLE -> STARTING -> CAPTURING -> STOPPING -> FINALIZING -> IDLE
```

- STARTING is claimed before any capture-global reset, so rejected overlapping
  starts cannot corrupt the incumbent VAD/path/file state.
- `stopRequested` is separate from lifecycle state; status remains busy through
  WAV header rewrite, close, terminal events, and only then publishes IDLE.
- Source loss, zero/short writes, empty PCM, allocation/setup failure, and
  normal reboot/OTA drains converge on bounded finalization. The effective
  source rate is latched per capture.
- HAL audio now has its own STARTING/ACTIVE/STOPPING ownership phases so a G2
  disconnect cannot let a cancelled backend startup re-arm ownerless.
- A 64-bit ID combines a nonzero random boot nonce and nonzero per-boot counter.
  It is also the recorder owner and deterministic WAV name.
- Native wake/cancel/autostop, recorder status/stop/discard/delete, ASK, every
  reply part/end, and conditional completion carry the same ID. Untagged native
  mutations fail closed: firmware does not apply their text, best-effort EXITs,
  and terminalizes the active exchange as `legacy_command`.
- Native EXIT/disconnect is accepted only from the initiating arm and BLE
  connection generation. Firmware clears local session state before sending
  advisory cancellation and guards each later physical BLE fragment.
- A failed tagged ASK/reply/part/end best-effort EXITs and terminalizes locally
  as `send_failed`. A CM5 job that does not reach complete reply delivery is
  canceled and gets one bounded five-second, non-replayed exact-ID `exitid`
  attempt (`host_incomplete` when no earlier reason exists).
- UART stop or authentication loss terminalizes the active XIAO exchange as
  `host_link_lost` and discards its owned recording. A crashed daemon on a
  still-open authenticated UART remains bounded only by the 60-second cap;
  a renewable host lease/heartbeat is still required for live transport.
- CM5 cancel-before-wake/duplicate/late events are tombstoned by ID. Already-
  written UART traffic is drained safely; native batch STT may finish but its
  result, persistence, LLM history, and all later lens mutations are discarded.

Recorder shadow is now source/build/simulation complete: it carries
post-processing PCM on one bounded nonblocking queue under the recorder's exact
ID/controller/login epoch, while `voicefetch` plus batch STT remain
authoritative. Physical PDM parity and the post-flush exact-owned G2 happy path
passed. The G2 run began with a full pre-capture ring yet ended with exact
live/WAV parity, so the former reason-6 burst was a stale-buffer boundary fault
rather than a UART continuity fault. The physical host-overflow, host-gap,
host-abort, and lease-expire fallbacks passed. Native provenance and explicit
link/auth/TX failure evidence remain required.

Wake-only `askactive <exchange_id> <revision> <cumulative-text>` semantics are
Phase 4 work, not a prerequisite for recorder shadowing. The first production
live-STT path still sends exactly one finalized ID-scoped ASK.

**Gate 1 is source/test complete for the current finalized-WAV pipeline:** old
ID operations cannot affect N+1, and terminal state prevents a zombie card.
It remains a hardware regression gate until the coordinated image/service pair
is deployed and dismissal is exercised during capture, fetch, STT, ASK hold,
and streamed reply. Also force a tagged-send failure, a stale-daemon legacy
command, an unsuccessful host exchange, and explicit UART auth/link teardown.
The first two must terminalize with the documented reasons, the host's exact-ID
EXIT must not touch the next wake, and link teardown must terminalize as
`host_link_lost` while discarding its owned WAV. Gate 1 does not claim a real
production live-STT path; the synthetic and recorder-shadow transports remain
explicit diagnostic layers.

## Phase 2 - live PCM shadow transport

### Phase 2A checkpoint — dormant synthetic transport

The first transport-only slice is implemented in source. It intentionally
proves the cross-language wire and receiver boundaries without changing the
recorder, Moonshine, pipeline, native G2 ASK, or finalized-WAV authority:

- Firmware registers `liveaudio capabilities|status|ready|release|synth|abort`.
  `ready` creates a low-priority Core-0 worker lazily and grants one controller
  a 3,000 ms lease; the host renews it every 1,000 ms. Mutable operations require
  a real logged-in UART session and an effective baud of at least 921600.
- Capabilities advertise
  `live-pcm-v1 synthetic=1 recorder_shadow=1 shadow_default=off`. `synth`
  schedules 16 kHz mono S16LE deterministic samples for 1..60000 ms and returns
  immediately. Nothing starts at boot or on a real microphone capture.
- Outer frame types are BEGIN `0x10`, PCM `0x11`, END `0x12`, and ABORT `0x13`.
  BEGIN carries version/flags/source/format/rate, exchange and controller IDs,
  and the 2,048-sample logical cadence. PCM carries both IDs, an absolute sample
  offset, and at most 500 samples. END/ABORT carry admitted sample count, IEEE
  CRC32, and dropped-sample count. Version 1 requires END reason 0 and ABORT
  reason 1..7; the host validates the ABORT prefix count and CRC before keeping
  the device reason. Existing voicefetch types are unchanged.
- The firmware emits one 128 ms logical chunk at a time, split into physical
  frames that fit the existing 1,024-byte payload ceiling. Each physical write
  uses a nonblocking UART admission API with a finite 100 ms retry window;
  lease/auth/link loss and backpressure fail the synthetic stream closed.
- The CM5 transport installs an immutable frame sink before opening the serial
  link. Live frames are parsed once on the reader thread and claimed before the
  generic asyncio event queue. A controller-bound inbox admits one pre-wake
  stream and bounds queued PCM to 16 KiB and 32 frames, with 0.5 s first-PCM,
  3 s inter-frame, and 65 s absolute deadlines. Identity, flags, offsets,
  terminal totals, CRC32, duplicates, stale IDs, link close, and overflow are
  all fail-closed.
- `tools/live_pcm_transport_probe.py` acquires/renews the lease, starts one
  asynchronous synthetic stream, drains concurrently, and compares every byte
  with the deterministic pattern before releasing the lease. It does not load
  Moonshine or enter the production pipeline.

The Phase-2A baseline full host suite passed 300 tests with 1 skipped and 7
subtests. The final XIAO app is `0x4fbcb0` = 5,225,648 bytes, with `0x39350` =
234,320 bytes (4%) free; binary SHA-256 is
`c306bb476f487df192632b388d193f33045f94b000f74c1a09d1507371f13341`.
Synthetic physical UART 2,048 ms and 10 s pattern/CRC/lease-renewal runs passed.
Those do not validate real recorder shadow.

### Phase 2B — recorder shadow (happy path and four-fault matrix passed)

- Command grammar is `liveaudio shadow 1 <controller> on
  <exchange_hex16|native>` and `... off`; default is off and daemon/YAML never
  enable it.
- A five-second one-shot exact `{exchange, controller, UART login epoch}` arm
  is consumed in `micrecord startid` command context. Native mode additionally
  proves the active G2 owner and epoch. Arbitrary/manual starts remain batch-only.
- After open WAV/header + CAPTURING, the recorder first calls exact-owner
  `audioTrimBufferedPcm("mic", 0)`, then admits BEGIN before the first read.
  PDM has no software history and is unchanged. G2 discards decoded samples
  accumulated before this recording claim, preventing its 32,768-sample /
  2.048-second AFE ring from burst-draining into the shadow queue.
  Each chunk is offered after source processing/VAD and before the FS lock.
- The high-priority recorder performs one bounded nonblocking copy into a
  strict four-slot x 4,096-byte PSRAM SPSC, with no DRAM fallback. The
  low-priority Core-0 TX worker is the sole consumer/UART writer.
- END occurs only after a retained SAVED WAV closes and the queue drains.
  Discard/failure/cancel/overflow/lease/auth/session/link/TX failure yields
  ABORT; shadow failure never fails the WAV.
- `voicefetch` atomically claims the bulk framed lane. After nonblocking shadow
  off/release, the host polls `liveaudio status` for `active=0 exchange=-`
  before fetching.
- `tools/live_pcm_shadow_probe.py` runs untrimmed exact-owned PDM/G2 source
  preflight, live collection, canonical WAV fetch/parity, terminal validation,
  artifacts, and exact cleanup without entering production STT/LLM/lens paths.
  Its explicit `--fault` modes now grade bounded host-queue overflow, one-frame
  host gap, exact host-request ABORT, and lease expiry as successful diagnostics
  only when the independent canonical WAV survives and current-exchange
  terminal/status accounting matches.
- Its separate `native --wake-timeout 30 [--capture-timeout N]` mode observes
  one real `Hey Even` capture without starting STT, LLM, ASK, or REPLY. It binds
  `evenai_wake`, LIVE BEGIN, the active native owner/UART epoch,
  `mic_autostop`, exact recorder path, LIVE terminal, WAV fetch/delete, and
  `g2evenai exitid` to one exchange. Native VAD trimming intentionally makes
  live/WAV byte parity inapplicable; the result records
  `parity.reason=native_capture_trim_enabled` instead.
- Its `native-stt` mode keeps the same transport/identity/WAV/cleanup contract
  but additionally feeds a bounded isolated Moonshine worker. It remains
  default-off and sends no ASK, REPLY, or LLM request.

Post-native software validation completed with clean Python `compileall`, 31
native shadow tests under `-W error`, and an independent 94-test
EvenAI/cancel/fetch/shadow review under `-W error`. The paced
collector/checker slice passes 31 focused tests under `-W error`; the complete
current CM5 service suite passes 348 with 1 skipped and 7 subtests under
`-W error`. Software tests remain distinct from the physical evidence below.

Run shadow mode with `voicefetch` + batch STT authoritative. For G2, wake the
glasses and keep an active lens container open (for example a `g2show` test
page) before enabling/capturing the mic; without it the glasses can accept
AudioCtrCmd while sending no LC3 frames. Include `g2micstats` in cleanup even
when the probe fails. `g2micreset` clears raw-frame, AFE mutex-drop, and decode-
fail counters, but does not clear the decoded ring or AFE overrun; a fresh AFE
feed start resets those. Live/WAV CRC equality cannot reveal upstream LC3
decode failures or zero-wait AFE-mutex drops.

**Physical status:** PDM passed exact live/WAV parity (112,640 samples, CRC32
`2e53eb16`). The pre-fix G2 run preserved a canonical 137,568-sample WAV but
ABORTed reason 6 when the stale 32,768-sample AFE prefix filled all four shadow
slots. The post-fix rerun deliberately restored that full-ring precondition
(`depth=32768`, cumulative pre-capture `overrun=71`) and passed: live, terminal,
and canonical WAV were exactly 100,000 samples / 200,000 PCM bytes with CRC32
`56ebd586`; END reason was 0, dropped samples were 0, device queue high-water
was 2/4, shadow overflows and host fault/late-frame counts were 0, and all three
parity checks were true. Final G2 counters were `mutex_drop=0 decode_fail=0`;
the later cumulative `overrun=607` only reflects the unattended ring refilling
during fetch/cleanup. First host-observed PCM latency was 100.539 ms for this
one run, not yet a p95 measurement. The service was restored active with the
idle microphone source confirmed as G2.

The 2026-08-10 physical four-fault matrix also passed. Host overflow and the
host-observed physical-frame gap invalidated only the bounded Pi receiver while
the XIAO retained exact current-exchange END count/CRC metadata. Exact host
ABORT and lease expiry returned reasons 5 and 1 with admitted prefix count/CRC
matching each canonical WAV. Every run exited zero with the requested outcome,
a canonical owner WAV, no control/lease errors, and STT disabled. Final
`mutex_drop=0 decode_fail=0`, source G2 idle, and active service restoration
were confirmed. Post-invalidation late traffic in the two host-only cases was
tombstoned as designed and did not increment `fault_count`.

The 2026-08-10 native no-STT smoke also passed. Controller
`05dae575e2e7a154`, firmware exchange `6bda87ea00000002`, and UART session epoch
19 correlated the non-synthetic G2 LIVE BEGIN, native wake/active state,
`mic_autostop`, exact recorder path, terminal/status, and cleanup. LIVE END was
valid reason 0 at 46,400 samples / CRC32 `931acca0`, with zero drops, inbox
faults, or late frames. The independently fetched canonical WAV contained
35,200 trimmed samples / CRC32 `82c81ade`; the intentional difference was
reported as `parity.reason=native_capture_trim_enabled`, not a parity failure.
STT, LLM, ASK, and REPLY all remained false. Lease/control error lists were
empty; exact shadow-off/release/fetch/delete/EXIT cleanup succeeded; native and
live state returned idle; final G2 `mutex_drop=0 decode_fail=0`; and the service
again owned the UART. This is one provenance/correctness smoke, not latency,
reliability, or streaming-STT evidence.

**Gate 2:** zero missed 128 ms recorder reads; zero unexplained offset/CRC gaps;
no G2/BLE, SD, watchdog, or command-latency regression; capture-claim/first-PCM
to first-live-frame p95 <= 250 ms; wake-to-first-PCM is recorded separately;
every injected failure falls back to an intact WAV. The exact-owned G2
happy-path, controlled host-overflow/host-gap/host-abort/lease-expire, and one
native `on native` admission/correlation smoke are now physically met. Gate 2
remains open for repeated/long latency evidence and auth/link/TX failures.
Treat AFE overrun as a
baseline/delta diagnostic rather than requiring a naive final zero: deliberate
prefill and the post-stop `voicefetch` interval can advance that cumulative
counter without corrupting admitted live/WAV parity.

## Phase 3 - streaming STT shadow, then final-only production

- `ai-service/hw1_ai_service/stt/live.py` now supplies one dedicated worker
  that owns model creation and all Moonshine stream calls. Physical UART frames
  are coalesced into 4096-byte logical chunks before entering an eight-chunk
  FIFO. `tools/live_pcm_shadow_probe.py native-stt` attaches it only to the
  existing standalone native recorder-shadow diagnostic; the daemon still has
  no live frame sink or shadow lease.
- Disable returned line audio (`return_audio_data=false`) and pin/log the tested
  package/native version.
- Treat partial text as a replacement hypothesis and log it; do not send it to
  G2 initially.
- At END, validate continuity/CRC, call `stop()`, assemble lines by start time,
  and compare against batch STT.
- On any error or missing final, discard the live result and batch the WAV.
- After parity, make the live final authoritative: send one id-scoped final ASK
  and launch the LLM concurrently. Use the calibrated render barrier, not the
  unvalidated zero-margin 44-char/s rule.

**Gate 3:** field-corpus WER/edit-distance parity; p95 END-to-final <= 0.8 s;
fallback rate and cause visible in metrics; no partial ever reaches the LLM.

The first `native-stt` hardware smoke is narrower than Gate 3: one fixed spoken
question, exact normalized final words, valid zero-drop live transport, no
model/audio/text queue overflow, soft END-to-final target 0.8 s, hard final
timeout 2.0 s, canonical WAV retained/fetched, and exact native cleanup. It
must report `llm_started=false`, `ask_sent=false`, and `reply_sent=false`.
The worker/native-shadow focused suite passes 41 tests under `-W error`.
The first physical invocation on 2026-08-10 did not reach the probe: a broad
process guard matched the runner's own source-tree pathname, returned
`probe_rc=99`, and restored the service. That guard is fixed; the event is an
operator-runner preflight finding, not a Gate 3 result.

The corrected physical invocation reached the full native path for exchange
`ae13f08400000001`. UART/live transport ended cleanly at 55,200 samples and
CRC32 `1cd5979a`; every one of 110,400 PCM bytes entered and left the Moonshine
worker; the eight-slot queue reached only 3/8 and 537.6 ms; END-to-final was
51.0 ms; no queue, transport, lease, throttle, or cleanup fault occurred. The
canonical trimmed WAV contained 44,800 samples. The run nevertheless failed
the intended accuracy gate because the final was `Haitian is the capital
difference.` instead of the six-word pinned question (3 word errors). This is
physical proof of the bounded live plumbing, not a Gate 3 accuracy pass;
raising the 0.8/2.0-second final waits would not have changed a result already
returned 51 ms after END.
Post-pull audio review exposed an upstream sample-clock anomaly: the prior
native no-STT run delivered 58 decoded 800-sample packets over 2.895 s
(20.04 packets/s, 16.03 ksamples/s), whereas this run delivered 69 over
6.903 s (9.995 packets/s, 7.996 ksamples/s) while still stamping the PCM
16 kHz. The wearer describes the result as robotic/low-bitrate. CRC, queue,
and decode-error counters cannot detect missing or differently timed source
notifications. Resolve the G2 LC3 frame-duration/notification-cadence question
before judging Moonshine accuracy or advancing Gate 3.

## Phase 4 - optional live question mirror

Only enter if Gate 0B proves prefix-preserving cumulative ASK.

- Latest-wins coalescer; never queue every ASR update.
- At most 2 updates/s, cumulative snapshot, changed word boundary only.
- UTF-8/word-safe 220-byte display projection.
- Exchange ID + monotonic revision on every command.
- Track render progress from the longest common prefix and measured G2 rate.
- Send the exact final ASK only when it differs from the last displayed snapshot.
- Cancel immediately on EXIT, lease loss, stream abort, or id change.

Start the LLM only from the final transcript. Answer delivery waits for the
measured remaining render, not a guessed full repaint.

**Gate 4:** visible question-first-content improves, final question is correct,
answer never truncates it, and cancellation produces no zombie UI in fault
injection or field use. If the renderer restarts, this phase stays disabled.

## Phase 5 - endpointing and later LLM delivery work

Keep the XIAO 1800 ms silence window until live transport/STT has a field corpus.
Moonshine line completion is telemetry, not permission to stop: a speaker may
pause and continue. Later, compare id-scoped host-assisted endpointing against
the current XIAO result and clipping rate.

Only after the question path is measured should earlier answer chunking be
revisited. It cannot outrun the native question barrier.

## Required timing record

Every field exchange needs one controller + exchange + UART-login-epoch
correlated timeline (the epoch is authority metadata, not a frame field):

```text
wake RX -> capture claimed -> first PCM -> first live frame
-> first STT hypothesis -> first ASK ACK -> XIAO speech/VAD END
-> live END -> final STT -> final ASK ACK -> estimated/observed question done
-> LLM request -> TTFT -> first reply ACK -> STREAM_COMPLETE / EXIT
```

Report success-conditioned and failure/fallback distributions separately. G2
command ACK is not optical first paint; retain that distinction in metric names.
`STREAM_COMPLETE` is currently only a XIAO debug log. Add a capture-ID-correlated
host event before treating it as an automatic production metric; until then the
renderer probe must fetch the XIAO log explicitly.
