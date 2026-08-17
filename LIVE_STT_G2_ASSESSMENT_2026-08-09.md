# Live STT and G2 rendering assessment — 2026-08-09

## Decision summary

Live XIAO audio plus Moonshine streaming STT is a valid direction and remains
the best structural way to move the recognized question earlier. It is not
ready to enable, and no production live-STT exchange has been tried. The copied
benchmark did not run, the small corpus has an identity gap, exact active-source
hashes were not captured, and no real capture PCM reaches a streaming-STT
worker. The transport now advertises
`synthetic=1 recorder_shadow=1 shadow_default=off`: synthetic framing and the
exact-owner recorder tee are implemented. The synthetic diagnostic has
physical UART happy-path coverage, and the standalone recorder-shadow probe
has now passed exact physical PDM parity. The post-fix G2 rerun deliberately
filled the 2.048-second AFE backlog and then passed exact 100,000-sample
live/WAV parity at CRC32 `56ebd586`, with END reason 0, zero drops, queue
high-water 2/4, and zero shadow overflow. This physically proves the
owner-scoped capture-boundary flush. The four-fault physical matrix also passed
for host queue overflow, a host-observed frame gap, exact host ABORT, and lease
expiry, with a canonical owner WAV retained in every case. The native no-STT
admission/correlation smoke then passed for one real `Hey Even` capture.
Auth/link/TX coverage, repeated latency, and production streaming STT remain
open. The hash-pinned paced v3 mixed slice has now run at 0.5 and 1.0 seconds:
both eight-chunk runs retained all PCM without overflow or throttling, and
streaming produced zero hallucinations on four reviewed no-speech controls.
Batch reproducibly returned `Yeah.` on neg001 at both cadences. This closes the
initial deployed-medium replay experiment, not full Gate 0A. A default-off
real-G2 `native-stt` shadow is implemented and has now completed one physical
run, but that run failed its pinned-text accuracy requirement.
Its first attempted physical invocation stopped before capture with
`probe_rc=99` because the competing-process guard matched the runner's own
source-tree pathname; cleanup restored the service. The narrowed executable
guard then allowed the rerun to exercise the complete live path. Transport,
the eight-slot queue, finalization, WAV fallback, power, and cleanup were
healthy, but Moonshine returned `Haitian is the capital difference.` for `what
is the capital of france` (3 word errors). The native live-STT gate therefore
remains failed on accuracy, not unrun and not failed on transport capacity.
The pulled 2.8-second canonical WAV is an exact prefix of the live PCM with
only 0.65 seconds of trailing live audio trimmed. It measured -36.9 dBFS mean
and -17.7 dBFS peak; speech-active windows were roughly -31 to -34 dBFS over a
roughly -56 dBFS quiet floor. The capture is quiet but neither clipped nor
UART-framing-corrupt. It does have an upstream timing anomaly: 55,200 samples
arrived over 6.903 seconds, or 7.996 ksamples/s, despite the 16 kHz label. The
prior native no-STT run delivered 16.03 ksamples/s. The wearer describes this
file as robotic/low-bitrate. Therefore raw G2 notification cadence and 10 ms
versus 20 ms LC3 decoding must be resolved before same-WAV batch versus
streaming Moonshine; a larger queue or longer final wait cannot restore source
audio that arrived at half cadence.

The prerequisite ownership revision has now landed in the working tree: one
boot-nonce/counter exchange ID follows the current native wake through recorder
status/stop/discard/delete, host cancellation, final ASK and reply delivery.
The five-state recorder prevents overlap, and both XIAO and CM5 reject a delayed
old ID instead of falling back to the newest session/path. That closes the
existing batch path's zombie-card race. Phase 2B binds a one-shot recorder
shadow to exact `{exchange, controller, UART login epoch}` authority and carries
the recorder's existing owner through BEGIN/PCM/END/ABORT. Physical PDM and
exact-owned G2 happy-path parity, one native admission smoke, and the four
controlled fault modes are physically closed.

The fail-closed boundary also covers partial deployment and local failures. A
newer XIAO treats any legacy untagged production mutation as `legacy_command`:
it does not apply the requested text, best-effort sends EXIT, and terminalizes
the active exchange. A failed tagged ASK/reply/part/end similarly becomes
terminal `send_failed`. On the CM5, every exchange that does not reach complete
reply delivery is canceled locally and gets one bounded five-second,
non-replayed exact-ID `exitid` attempt (`host_incomplete` when no earlier reason
exists). These are containment paths, not mixed-version compatibility; XIAO and
CM5 must still be deployed as a coordinated pair.

The XIAO also terminalizes an active ID as `host_link_lost` when its UART
runtime stops or UART authentication is revoked, and discards the exact owned
capture. This is not daemon-health detection: a crashed process can leave the
UART open/authenticated, in which case the 60-second native cap is still the
fallback. The synthetic transport now supplies a 3-second renewable controller
lease, renewed by a probe every second. Recorder shadow consumes that lease
only when explicitly armed; ordinary production batch EvenAI does not, and the
daemon has no shadow default/YAML enable. A crashed authenticated daemon still
falls back to the independent 60-second native cap.

The current question-cutoff report is a separate, more immediate issue. The
`len(question) / 44` barrier was never validated as a safe hardware rate and is
disproven as safe in the current renderer state. The field-only CONFIG reversal
has now established that `streamSpeed` causally controls final-answer render
time and that 80 is slower than 40. Two same-connection 80 -> 40 -> 80 matrices
reversed from 1,131 -> 566 -> 1,126 ms for 14 characters and from
2,493 -> 1,061 -> 2,467 ms for 30 characters, measured from text TX to
`STREAM_COMPLETE`. Speed 40 is much closer to the earlier 497 ms no-CONFIG
14-character control, although it remains 69 ms slower in that comparison.

This result proves answer-render direction, not the field's units or its effect
on ASK/question rendering. The two lengths do not fit one exact
milliseconds-per-character model, and the reversal used only the six-character
question `Ready.` with a fixed host wait before ANALYSE. After this measurement,
the production choice was made to have the Pi daemon submit field-only speed 40
at startup. `deliver.g2_stream_speed: 0` preserves the current state for a
controlled no-CONFIG experiment. This deliberate choice accepts the still-open
ASK uncertainty; the next renderer gate is a long indexed-question test in the
speed-40 state.

No camera-based validation is required by this plan. G2 answer rendering is
measured from `STREAM_COMPLETE`; question safety uses exact command timestamps
plus a simple wearer pass/fail threshold because ASK exposes no completion
event.

Canonical CM5 paths and all future sync commands are defined in
[`CM5_DEPLOYMENT_PATHS.md`](CM5_DEPLOYMENT_PATHS.md).

## Confidence labels

- **Confirmed:** directly established by current source, copied artifacts, or
  captured protocol timestamps.
- **Strong inference:** multiple facts agree, but one required observation is
  unavailable.
- **Unknown:** must be measured; no production decision should depend on it.

## 1. Deployment identity

### Confirmed

The Pi had at least two AI-service source directories:

- `/home/$CM5_USER/hw1-ai-service` — target of the virtual environment's editable
  install;
- `/home/$CM5_USER/ai-service` — an older source copy used accidentally by the
  failed benchmark.

The benchmark ran while its current directory was the older tree. Python
therefore imported that local package first. Its config schema predates the
`power` section and raised `unknown config section: power` before loading an
STT engine. This was not a Moonshine error.

The prior systemd unit used `~/.local/bin/hw1-ai-service`, but the installation
transcript shows that path was explicitly symlinked to
`/home/$CM5_USER/hw1ai/bin/hw1-ai-service`. The virtual environment was then
editable-installed from `/home/$CM5_USER/hw1-ai-service`. This was not a third
deployment. The bundle still omitted that canonical source tree and did not
record its module hashes, so byte-for-byte parity with the Mac source remains
unproven.

### Resolution in the repository

The tracked unit and install documentation now define one layout:

```text
Mac source    $REPO_ROOT/ai-service
CM5 source    /home/$CM5_USER/hw1-ai-service
CM5 venv      /home/$CM5_USER/hw1ai
service bin   /home/$CM5_USER/hw1ai/bin/hw1-ai-service
```

The old `/home/$CM5_USER/ai-service` directory is explicitly marked stale. The
remote CM5 is not changed until the documented sync/install workflow is run.

## 2. What the new evidence did and did not measure

### It did establish

- Four canonical mono PCM16, 16 kHz XIAO WAV files were copied.
- `moonshine-voice==0.1.1` is installed and the daemon can initialize it.
- The installed default English model is `medium-streaming-en`.
- The Pi was cool and reported no firmware thermal throttling in the snapshot.
- The G2 CONFIG builder sends accepted field-13 CONFIG messages.
- Five post-CONFIG 14-character replies completed in 1.125-1.178 seconds
  after text TX; their mean after the G2 response was 1,083.8 ms.
- Two field-only, two-length 80/40/80 reversals established that speed 40
  materially reduces final-answer render time and that returning to 80 restores
  the slower timing within 5 ms at 14 characters and 26 ms at 30 characters.
- All 98- and 180-byte test payloads reached the right temple intact.

### It did not establish in the original bundle

- The batch benchmark output is empty; config loading failed first.
- No real-time-paced streaming benchmark ran in that bundle; the later v3
  mixed-slice measurement is recorded below.
- The copied journal contains no exchanges.
- The bundle copied the stale source tree rather than the canonical tree used
  by the editable install.
- No post-reversal power-cycle/no-CONFIG baseline has been captured.
- The numeric `streamSpeed` value is not proven to be literal milliseconds per
  character or per display step.
- The reversal did not measure whether speed 40 accelerates a long ASK/question
  or makes the question barrier safe.

Accordingly, the original evidence bundle contains no new measured STT speed,
WER, partial stability, or current-build G2 latency. The later dedicated G2
trials are recorded separately in `G2_EVENAI_RENDER_TEST_RECORD.md`.

## 3. Corpus assessment

| Capture | Duration | Firmware chunks | Level / peak | Assessment |
| --- | ---: | ---: | ---: | --- |
| `001.wav` | 1.536 s | 12 | -41.5 / -21.9 dBFS | Valid quiet/hard case |
| `002.wav` | 2.560 s | 20 | -37.3 / -14.2 dBFS | Usable labeled case |
| `004.wav` | 3.072 s | 24 | -30.3 / -9.6 dBFS | Identity uncertain; onset begins loud |
| `005.wav` | 4.224 s | 33 | -32.0 / -8.8 dBFS | Usable long case |

All four are exact multiples of the recorder's 4096-byte / 128 ms chunk and
none clips.

`003.wav` is absent. The second attempt at prompt 004 created only
`0042.txt`; it did not copy a `0042.wav`. The next capture overwrote
`last-utterance.wav` and was saved as 005. The command record does not establish
whether `004.wav` contains prompt 003 or the first prompt-004 attempt. Exclude
004 from WER until it is listened to or transcribed and relabeled; re-record
prompts 003 and 004.

Capture 004 also begins with strong speech rather than the three retained
room-tone chunks present in the normal files. Treat it as an onset-clipping
stress case even after its identity is resolved.

Human review confirmed four archived empty-transcript WAVs contain only
static-like background noise; v3 pins those as negative controls and scores
them for hallucinated text and false finalization. The 1.536-second archived
file contains extremely quiet speech, so it remains unscored and must not be
treated as a negative without a known transcript.

The new `moonshine_gate0a_medium_slice.json` hash-pins the three trustworthy
positive pairs 001, 002, and 005 plus the four reviewed static/no-speech
controls, including exact frame/chunk counts. This is a provisional
deployed-medium smoke slice, not an engine/model decision: it is too small for
a meaningful p95 or model choice. A
field decision needs a larger set of short, long, quiet, paused, number-heavy,
proper-name, abandoned, and negative utterances.

### Later v3 paced replay result — 2026-08-10

The Pi ran the deployed `medium-streaming-en` model in separate processes at
0.5- and 1.0-second update floors with the eight-chunk / 32 KiB FIFO. Retained
evidence is `.scratch/gate0a-v3-results-Pn5hEAy0`.

| Measure | 0.5 s | 1.0 s |
| --- | ---: | ---: |
| Positive stream word errors / 26 | 2 | 1 |
| Positive batch word errors / 26 | 2 | 2 |
| Max END-to-final | 0.459 s | 0.712 s |
| Max positive queue high-water | 4 / 8 | 5 / 8 |
| Max positive queue age | 510 ms | 711 ms |
| Dropped/overflowed PCM chunks | 0 | 0 |
| Streaming hallucinations / 4 negatives | 0 | 0 |
| Batch hallucinations / 4 negatives | 1 | 1 |

Both reports intentionally failed the complete provisional policy. At 0.5 s,
002/005 exceeded the 1.35-second partial-gap ceiling and 005 regressed one word
against batch. At 1.0 s, 002/005 again narrowly exceeded that gap ceiling and
005 produced 3 useful pre-END updates where the tiny manifest required 4.
Those partial-frequency findings affect interactivity telemetry, not PCM
retention. The repeated no-speech result is more consequential: streaming was
empty on all four controls, while batch produced `Yeah.` on neg001 in both
fresh processes and prefixed `Yeah` to positive 002. A volume threshold is not
a safe correction because quiet valid speech overlaps the reviewed noise
levels; a literal `Yeah` blacklist would also reject valid speech.

The chosen next diagnostic therefore uses 1.0-second updates, the existing
eight-chunk FIFO, a soft 0.8-second END-to-final target, and a separate 2.0-second
hard wait. These thresholds observe/contain latency; increasing them does not
make native inference faster. A valid empty streaming final is admissible as
no-speech evidence, while queue overflow, timeout, transport error, or missing
`stop()` output is a distinct failure that preserves the WAV fallback.

## 4. Moonshine feasibility

### Confirmed API capability

The installed 0.1.1 package already exposes the required streaming surface:

- `Transcriber.create_stream()`;
- `Stream.start()` / `add_audio()` / `stop()` / `close()`;
- mutable `LineTextChanged` events;
- immutable line-completion events;
- per-line native transcription latency.

No package upgrade is required. Production `MoonshineSTT` remains batch-only,
but `hw1_ai_service/stt/live.py` now provides the isolated streaming-session
worker used by the default-off `native-stt` diagnostic. It is deliberately not
wired into the daemon or pipeline until the physical shadow gate passes.

The copied native package cannot be executed on the Mac as bundled:
`libmoonshine.so` is AArch64 and its sibling `moonshine_voice.libs` directory
was omitted. This does not indicate a broken Pi install—the daemon initialized
the native runtime—but the paced benchmark must run on the Pi or use a complete
architecture-matched package copy.

### Required worker model

`add_audio()` may synchronously run native inference when the update floor is
reached. Listener callbacks run synchronously on that same caller. Therefore:

```text
UART reader -> bounded queue -> one dedicated Moonshine worker
                                 -> tiny text-event queue -> asyncio pipeline
```

The UART reader must never call Moonshine. Moonshine callbacks must never send
G2/UART commands. One worker owns one stream from start through close.

The nominal 0.5-second update interval is a floor, not a fixed cadence. The
library widens the next interval to at least the duration of the preceding
native pass, capped at ten times the configured floor. A loaded Pi can
therefore emit fewer than two hypotheses per second.

`stop()` forces a final update but catches final-update exceptions and can
return `None`. Every live job must preserve the PCM/WAV and fall back to batch
decode on a missing result, error, gap, bad CRC, or queue overflow.

### Actual deployed model

`stt.model: en` does not mean small in this installed package. With no explicit
architecture, its model catalog chooses `medium-streaming-en` first. The cache
contains that model and no small/tiny alternative.

The documented aliases `moonshine/small` and `moonshine/tiny` are not valid for
the current wrapper: a model spec must be an existing directory or a short
language code. Reproducible streaming tests must pass both an existing model
directory and an explicit `ModelArch`; they must not call a resolver that can
pick catalog order or download implicitly. The example configuration has been
corrected to `en`, but the code's blank-model fallback still names the invalid
`moonshine/small` alias and remains a production cleanup item.

This exposes two current configuration defects:

1. the RAM preflight sees the string `en` and estimates 600 MiB, although its
   own medium estimate is 1024 MiB;
2. `threads_idle` and `threads_contended` do not control the native engine.
   The wrapper's setter only stores an integer and the production pipeline does
   not call it.

Do not describe current Moonshine results as 2-thread or 4-thread controlled.

### Historical compute evidence

A separate August 8 batch run processed 14.22 seconds of speech in 5.78
seconds: weighted real-time factor 0.406 and about 0.96 seconds STT per case.
That is encouraging headroom for overlap with real-time audio. It does not
prove streaming-pass cost, partial stability, or accuracy; several historical
transcripts were visibly wrong.

## 5. LLM benchmark correction

The new `llama-bench` run is not a clean production-speed measurement. The
service status and command chronology show the service was stopped, so the
earlier concern about a second resident Moonshine/LLM instance was unsupported.
The captured governor was nevertheless `powersave` at 1.5 GHz, whereas normal
voice jobs request the performance profile. The Q5 run also ended with an SSH
timeout. Absolute tokens/second from this run must not be used as a production
baseline.

The relative result is still suggestive:

| Quantization | Prompt 128 | Generate 64 |
| --- | ---: | ---: |
| IQ4_NL | 50.58 tok/s | 9.04 tok/s |
| Q4_0 | 65.01 tok/s | 7.65 tok/s |
| Q4_K_M | 40.17 tok/s | 8.24 tok/s |

Q4_0 prefills about 28.5% faster than IQ4_NL in that state, while IQ4_NL
decodes about 18.2% faster. For a 128-token prompt and roughly 29 generated
tokens the two estimates nearly cancel; with a well-reused prompt prefix,
IQ4_NL's decode advantage may matter more.

Both the current config and the August 8 live run use Q4_0. There is no recent
model switch that explains slower G2 text. LLM token availability and native
G2 glyph rendering are separate clocks.

Rerun model tests only after stopping the service, confirming no
`llama-server`, selecting/verifying performance mode, recording memory/swap and
thermal state, then restoring automatic power policy. Keep eight history turns
for now; four or six remains a documented later option after prompt-cache
measurement.

## 6. G2 render regression

### Host completion is not wearer completion

For five long streamed answers, the host marked the exchange done an average
of 3.75 seconds before G2 `STREAM_COMPLETE`:

| Answer chars | First REPLY to `STREAM_COMPLETE` | Effective rate |
| ---: | ---: | ---: |
| 217 | 7.960 s | 27.26 chars/s |
| 134 | 4.860 s | 27.57 chars/s |
| 130 | 4.740 s | 27.43 chars/s |
| 198 | 7.260 s | 27.27 chars/s |
| 100 | 3.560 s | 28.09 chars/s |

This consistency establishes a pre-CONFIG protocol drain rate near 27.5
characters/second. Duration versus characters is nearly perfectly linear
(`R²=0.99988`). `STREAM_COMPLETE` is the best available wearer-terminal proxy,
although protocol logs alone do not literally observe pixels. Backgrounding
`replyend` improved a host metric, not full visible completion.

`STREAM_COMPLETE` is currently only written to XIAO debug logs, not forwarded
to the Pi service. Production metrics need an ID-correlated host event before
they can record it automatically.

The logged second chunks arrived before the preceding text would drain at
27.5 chars/s, so those examples did not starve the renderer. Streaming did not
make its raw character rate slower in that record. It can still feel slower
because the animation begins earlier and continues for several seconds after
the host says it is done.

The new post-CONFIG control is materially slower. The same 14-character
`Probe complete` reply took 497 ms from TX to `STREAM_COMPLETE` before an
explicit HardwareOne CONFIG in the earlier run. After accepted
`streamSpeed=80` probes, five repeats averaged 1,144.8 ms (sample SD 20.4 ms).
The strongest device-side delta is `1083.8 - 422 = 661.8 ms` after the G2
response.

The completed field-only reversal establishes causality and direction for
final-answer rendering:

| Reply | Speed 80 A | Speed 40 | Speed 80 B |
| --- | ---: | ---: | ---: |
| 14 chars, TX to complete | 1,131 ms | 566 ms | 1,126 ms |
| 14 chars, response to complete | 1,099 ms | 474 ms | 1,049 ms |
| 30 chars, TX to complete | 2,493 ms | 1,061 ms | 2,467 ms |
| 30 chars, response to complete | 2,424 ms | 1,024 ms | 2,425 ms |

Every CONFIG echo carried the requested value, all six replies produced a
pre-EXIT `STREAM_COMPLETE`, and no COMM_RSP or loop stall overlapped a render
interval. The 80 endpoints returned within 0.44% for 14 characters and 1.04%
for 30 characters on the TX-anchored metric, defeating simple session drift as
an explanation. Speed 40 reduced TX-to-completion by about 49.8% at 14
characters and 57.2% at 30 characters versus the mean of the two speed-80
endpoints.

The XIAO declared the left plugin silent during the 14-character matrix even
though its BLE link still reported up. The right temple completed every event,
and the separate 30-character matrix reproduced the reversal before any plugin
silence, so this does not plausibly create the measured effect; it remains a
health caveat on the first matrix.

The field's units remain unknown. Adding 16 characters increased the
TX-anchored interval by about 84.47 ms per added character at speed 80, but
only 30.94 ms per added character at speed 40. The older conditional model
`105 + (characters - 1) * streamSpeed` is therefore not a validated universal
law; punctuation, layout, renderer mode, or nonlinear pacing may contribute.
The reversal also did not test a long ASK: the runner used `Ready.` and
deliberately submitted ANALYSE about 2.25 seconds after ASK. Do not transfer the
answer result directly into a question barrier without a long-question test.

The attempted 180-character one-shot/multipart A/B was right-censored. All
payloads were acknowledged without error, but every session was exited
12.567-13.452 seconds after first text TX. The completed reversal confirms that
CONFIG 80 selects the slower answer-render state, but the two measured lengths
do not support an exact 180-character completion prediction. The missing events
remain compatible with the test having been too short and do not prove a
stall, cap, event issue, or mode winner.
The multipart condition also finalized much faster than historical production
streams, so a valid retry needs both a longer wait and a paced condition.

The stable trial tables, protocol accounting, evidence hashes, and next gates
are in [`G2_EVENAI_RENDER_TEST_RECORD.md`](G2_EVENAI_RENDER_TEST_RECORD.md).

### Why questions now look cut off

The intended host barrier is:

```text
hold until ASK acknowledgement time + len(displayed_question) / 44
```

Forty-four chars/s came from one approximate question observation. It has no
fixed margin and no G2 completion signal. The new controlled test sent the same
98-byte question in five fresh wakes. It was still cut with native ASK-to-
ANALYSE opportunities of 2.204, 2.712, 3.244, 3.717, and 4.240 seconds. The
wearer saw more text at each longer delay, favoring continued time-based
progress rather than a character ceiling already reached in that range. Exact
last-visible words were not recorded, so a later page boundary remains unknown.

The 98-byte question receives only 2.227 seconds under the current formula.
That is disproven as safe in the current state. It is also now clear that Pi
command completion is not a glasses render-start event: one A/B ASK took 617
ms to echo, and the XIAO had returned command OK 510 ms before that echo.
Before replacing the bare formula, restore/choose the intended G2 renderer
state, then use:

```text
fixed safety margin + rendered character count / calibrated ASK rate
```

Log displayed characters, UTF-8 bytes, ASK acknowledgement, scheduled hold,
actual first-REPLY submission, and whether the hold engaged.

### Separate hard truncation

The host currently permits a 1900-byte question. For the current 200–250 magic
range, protobuf overhead leaves exactly 236 text bytes in a single ASK envelope.
At 237 bytes or more the builder returns zero and the ASK fails outright; it
does not safely truncate at the apparent 250-byte `strnlen` cap. The host logs
that failure, still arms a render deadline, and eventually sends the answer.
This can look like a fully missing or cut question. Use a UTF-8- and word-safe
220-byte display projection while the LLM retains the full transcript, and do
not send a REPLY after an unsuccessful ASK acknowledgement.

### CONFIG 80 is the confirmed answer-render regression trigger

The typed builder repair changed only CONFIG encoding; it did not modify
ASK/REPLY code. The completed 80 -> 40 -> 80 reversal proves that the manually
submitted field changes answer rendering and that the larger value is slower.
The two tested lengths also show that 40 is much closer to the earlier
no-CONFIG behavior, but they do not support treating the numeric value as an
exact delay unit.

The subsequent production decision is to submit only field 2 with value 40 at
daemon startup. This is best-effort and startup-only; it does not claim an
observed CONFIG echo, detect a glasses-only reconnect, or establish how ASK
responds. Configuration value `0` means **do not submit CONFIG**—it is a host
opt-out, not a speed sent to the glasses. Do not describe a speed-80 packet as
a reset. Test a long, indexed question at speed 40; ASK has no
`STREAM_COMPLETE`, so record exact last-visible markers and use a randomized or
reversed order. Only that result can calibrate the question barrier.

## 7. What live PCM changes on each device

### XIAO

The universal tap is the recorder's processed PCM after source processing and
before the filesystem lock. G2 audio is already LC3-decoded there; PDM uses the
same post-processing signal as the WAV path.

- PCM is 32 kB/s at 16 kHz mono S16.
- Framed traffic is about 33 kB/s, roughly 16% of the 2 Mbaud 8N1 link.
- A 16 KiB queue represents about 0.5 seconds of audio.
- Copy/CRC work at this rate should be small, but must be measured.
- The high-priority recorder performs one nonblocking copy only.
- A lower-priority TX task owns UART framing and mutex waits.
- Overflow/gap expires the live path; it never blocks recording or damages the
  fallback WAV.

The lifecycle and control-plane ownership prerequisites are implemented: IDLE,
STARTING, CAPTURING, STOPPING, FINALIZING, then IDLE, plus one 64-bit
boot-nonce/counter exchange ID. The device stays busy through WAV close and
terminal events, rejects overlapping starts before resetting shared capture
state, latches the effective source rate, and converges source/write/setup
failures on finalization. Exact-ID stop/discard/delete and tagged native
ASK/reply commands reject mismatches; a matching EXIT also fences each later
BLE fragment. HAL startup cancellation is serialized.

Any legacy untagged native display mutation is rejected and also closes the
active exchange as `legacy_command`; any failed tagged display send closes it
as `send_failed`. Conversely, a CM5 job that exits before complete reply
delivery makes one five-second, non-replayed exact-ID EXIT attempt. These
guards keep a stale daemon or a host exception from leaving a heartbeating
partial card, but they do not replace coordinated deployment.

The default-off transport provides both synthetic and exact recorder-shadow
producers. Fixed outer types remain BEGIN `0x10`, PCM `0x11`, END `0x12`, and
ABORT `0x13`; version-1 payloads carry controller/exchange IDs, offsets,
admitted/dropped counts, and IEEE CRC32. Exact recorder authority is
`{exchange, controller, UART login epoch}`; the epoch is an internal
admission/TX fence, not another payload field.

`liveaudio ready 1 <controller>` acquires the 3,000 ms lease. `synth` remains
the deterministic framing path. `liveaudio shadow 1 <controller> on
<exchange|native>` installs a five-second one-shot: exact mode admits only a
matching real-UART `startid`, and native mode admits only the active G2 owner
bound to that UART epoch. Manual/fabricated starts remain batch-only.

After WAV/header + CAPTURING, the recorder establishes a fresh sample-zero
boundary with exact-owner `audioTrimBufferedPcm("mic", 0)` and then admits
BEGIN before the first read. PDM has no HAL software ring, so this is a no-op.
For G2 it drops decoded history accumulated before the recording claim instead
of prepending it to the WAV and burst-offering it to the live queue.
Each chunk is offered once after DSP/VAD and before FS locking into a strict
four x 4,096-byte PSRAM SPSC with no DRAM fallback. The recorder does one
nonblocking copy and the low-priority Core-0 worker alone writes UART. END
requires a retained closed SAVED WAV and queue drain. Discard, failure, cancel,
overflow, lease/auth/session/link/TX loss yields ABORT while the WAV continues
under canonical recorder policy. `voicefetch` holds an atomic bulk claim that
excludes live BEGIN.

On the Pi, `SerialTransport` installs one immutable direct frame sink before
open/login/readiness. `LivePcmInbox` bounds the stream and fails closed on
metadata, identity/flag, offset, CRC/terminal, overflow, deadline, and link
errors. The synthetic probe checks the generated pattern. The standalone
`live_pcm_shadow_probe.py` owned mode preflights PDM/G2, records one untrimmed
exact-owned capture, waits for live quiescence, fetches the canonical WAV,
compares bytes/CRC/terminal, and performs exact cleanup. Its native mode instead
observes one real G2 wake and correlates exchange/controller/login epoch,
autostop path, terminal, canonical WAV, delete, and EXIT without asserting
post-VAD live/WAV byte equality. The new third `native-stt` mode attaches only
an isolated bounded Moonshine worker. None enters the production pipeline,
LLM, or lens delivery path.

The earlier transport baseline passed 300 host tests with 1 skipped and 7
subtests, plus 58 focused live/shadow/transport tests. After the native probe
landed, Python `compileall` was clean; the native shadow file passed 31 tests
under `-W error`; and an independent EvenAI/cancel/fetch/shadow review passed
94 under `-W error`. The paced replay collector/checker slice passes 31 tests
under `-W error`, and the complete current CM5 service suite passes 348 with 1
skipped and 7 subtests under `-W error`. The standalone shadow probe has
explicit expected-fault modes for
bounded host overflow, one
physical-frame host gap, exact host-request ABORT, and lease expiry. Each mode
keeps STT disabled and passes only after fetching a canonical owner-scoped WAV
and validating the relevant exact exchange/prefix/terminal accounting.
The final XIAO app is `0x4fbcb0` = 5,225,648 bytes, leaving
`0x39350` = 234,320 bytes (4%), SHA-256
`c306bb476f487df192632b388d193f33045f94b000f74c1a09d1507371f13341`.
Physical synthetic 2,048 ms and 10 s pattern/CRC/lease-renewal runs passed.
Physical PDM shadow parity also passed at 112,640 samples / CRC32 `2e53eb16`.
The corrected pre-fix G2 run delivered 66 LEFT notifications at about 20 fps,
with no UART fault or late-frame evidence, but the prefilled ring caused
reason-6 ABORT at queue high-water 4 / overflow 1. Its independent canonical
WAV remained valid at 137,568 samples / CRC32 `1fed8e52`. The post-fix rerun
then passed exact 100,000-sample live/WAV parity at CRC32 `56ebd586` with END
reason 0 and no drops, overflow, inbox faults, or late frames.

The 2026-08-10 four-fault physical matrix passed. Host overflow and the injected
host frame gap invalidated the bounded receiver while exact XIAO END count/CRC
remained available. Host ABORT reason 5 and lease-expiry ABORT reason 1 matched
their canonical WAV prefixes. All four owner WAVs were canonical, every
requested outcome was observed, and control/lease error lists were empty.
The native no-STT mode then passed for controller `05dae575e2e7a154`, exchange
`6bda87ea00000002`, and UART epoch 19. The non-synthetic G2 stream ended valid
at 46,400 samples / CRC32 `931acca0`, reason 0, with zero drops, inbox faults,
or late frames. Its independent canonical trimmed WAV was 35,200 samples /
CRC32 `82c81ade`; parity was correctly marked inapplicable with reason
`native_capture_trim_enabled`. STT/LLM/ASK/REPLY remained false, all exact
cleanup booleans were true, native/live state returned idle, final
`mutex_drop=0 decode_fail=0`, and the service regained the UART. This closes
one native provenance smoke only; it is not a live-STT or latency result.

Recorder-shadow BEGIN is admitted before the first recorder read under an
already-active exact lease/one-shot arm. Native G2 publishes that arm
synchronously before its internal `startid`; it does not wait for the later
`evenai_wake` event.

### Pi/CM5

During capture the same exchange's LLM is idle, so streaming STT can use the
four cores without competing with LLM decode. Repeated streaming passes may use
more aggregate CPU than one batch pass, so compare 0.5- and 1.0-second floors.

Do not construct a second Transcriber. One resident model plus one per-capture
Stream keeps RAM bounded. Measure real RSS because the current preflight
undercounts medium.

The service's automatic power policy promotes at the beginning of the voice
job, during speech. Standalone benchmark scripts do not invoke that policy.
Every benchmark must explicitly select/verify its governor and restore the
prior policy afterward.

### G2

The safe first production step sends one final ASK earlier. It does not require
repeated partial ASK.

The reference implementations do send cumulative mutable hypotheses as
repeated complete ASK messages. Current HardwareOne final ASK/reply commands
are exchange-ID scoped and cannot reopen a dismissed card, but no
revision-ordered partial-ASK command exists. Partial display additionally
requires:

- exchange ID and monotonically increasing revision;
- rejection of inactive, stale, duplicate, or mismatched updates;
- no `EnsureCard` behavior on a late partial;
- latest-wins coalescing, at most about two updates per second;
- cumulative replacements, never token deltas;
- the 220-byte display projection;
- final transcript only for the LLM.

If repeated ASK redraws the stable prefix, queues repaint work, or handles
corrections poorly, leave partial display disabled. Live final STT still moves
the complete question earlier.

## 8. Corrected latency picture

The latest six successful historical exchanges averaged approximately:

```text
capture/wait/transfer     5.65 s
batch STT                 0.95 s
LLM + host reply ACKs     4.17 s
host-reported total      10.75 s
```

The fetch clock begins at the Pi's delayed `evenai_wake`, not at the physical
G2 wake. Physical wake preceded that event by roughly 0.42–1.40 seconds, so
5.65 seconds must not be described as the complete wake-to-fetch span.

For the five streamed long answers, the unreported G2 render tail averaged
3.75 seconds. The corresponding mean wake-to-`STREAM_COMPLETE` endpoint was
about 15.45 seconds, versus the 10.75-second host-reported mean over all six
successful exchanges. These are operational, not cleanly isolated, stage
metrics.

The removable post-capture work is about 0.98 seconds of transfer plus 0.95
seconds of batch STT. True live PCM plus streaming STT could hide much of both
under speech and the existing 1.8-1.92-second VAD tail. A reasonable target
range is 1.1-1.9 seconds saved from end-of-speech to final question, not a
promise.

This work intentionally leaves:

- the eight-turn conversation window unchanged; four or six remains a later,
  reversible TTFT option;
- the 1800 ms endpoint window unchanged; lowering it without a larger corpus
  previously clipped speech;
- answer length and the current 1.7B model unchanged.

Live STT improves when the question can start. It does not make the G2 paint an
already-received answer faster; that native time depends materially on the
active CONFIG state. The earlier no-CONFIG long-answer record was about
27.5 characters/second, while the completed reversal shows speed 40 is much
faster than speed 80 for the tested final replies.

### EVT grace remains a compatibility heuristic

The old “four of eight EVT races” conclusion included one user EXIT, which is
not supposed to emit a VAD autostop event. Current firmware now reports
STOPPING/FINALIZING until the WAV is closed and publishes IDLE last. Native
terminal events and the status/stop fallback carry the exact exchange ID, so a
delayed native event cannot satisfy a later wake. The 250 ms grace remains only
for legacy timing compatibility; the path-only legacy `mic_autostop` latch is
used by manual `ask`, not as native ownership evidence.

## 9. Clean, no-camera execution order

### Gate A — deployment identity

Use `CM5_DEPLOYMENT_PATHS.md` to converge the remote install, print exact module
paths and hashes, and capture the real source. Do not edit the render barrier
until this passes.

### Gate B — answer causality complete; question timing pending

Use [`ai-service/tools/g2_evenai_probe.py`](ai-service/tools/g2_evenai_probe.py);
the exact CM5 commands are centralized in
[`CM5_DEPLOYMENT_PATHS.md`](CM5_DEPLOYMENT_PATHS.md#no-camera-g2-render-diagnostics).

1. The two-length field-only 80/40/80 reversal is complete: speed 40 was much
   faster and both speed-80 endpoints returned to the same slower range.
2. Production now deliberately submits field-only speed 40 when the daemon
   starts. Keep `deliver.g2_stream_speed: 0` as the opt-out for any future
   power-cycle/no-CONFIG baseline; that baseline is no longer a prerequisite
   for using 40.
3. At speed 40, repeat a long indexed-question threshold in
   randomized or reversed order and record the exact last visible marker. The
   answer reversal does not calibrate ASK.
4. Retry the 180-character answer A/B with at least a 20-second wait and add a
   paced multipart condition.
5. Set the barrier only after the desired G2 renderer state is stable.

The prior 98-character and 180-character numeric results are preserved in
`G2_EVENAI_RENDER_TEST_RECORD.md`. No recording or camera is required.

### Gate C — streaming replay

Use
[`ai-service/tools/moonshine_stream_replay.py`](ai-service/tools/moonshine_stream_replay.py);
the collector regression suite is
[`ai-service/tests/test_moonshine_stream_replay.py`](ai-service/tests/test_moonshine_stream_replay.py).
Grade the first run with
[`ai-service/tools/moonshine_stream_replay_check.py`](ai-service/tools/moonshine_stream_replay_check.py)
and
[`ai-service/tools/moonshine_gate0a_medium_slice.json`](ai-service/tools/moonshine_gate0a_medium_slice.json).

1. Re-record the missing/mislabeled corpus items.
2. Use the implemented paced producer, which feeds exact 4096-byte/128 ms
   chunks into a bounded queue and one Moonshine worker.
3. Run only deployed medium at the 0.5-second floor first, using positive cases
   001, 002, and 005 plus confirmed static/no-speech controls neg001 through
   neg004, default pace 1.0, the eight-chunk / 32 KiB / 1.024-second Pi
   worker queue, and the batch baseline. This FIFO absorbs bounded synchronous
   native-inference stalls and drains immediately afterward; it does not alter
   the separate XIAO shadow or Pi UART-inbox bounds.
   Run the checker on the Pi while the JSONL's absolute source paths resolve.
   The canonical manifest pins the exact corpus directory, model/enum,
   Moonshine 0.1.1 runtime, semantic contract/policy, hashes/counts, and
   absolute error ceilings; the checker independently recomputes positive WER,
   requires empty negative finals, and verifies record/PCM/partial/resource
   integrity.
4. Require the checker to report `ok=true`, scope
   `provisional_deployed_medium_mixed_slice`, and
   `full_gate0a_complete=false`; then stop and review before 1.0 seconds or
   other models.
5. Record final transcript, edit distance/WER, partial revisions and longest
   common prefix, native latency, END-to-final, CPU, RSS, frequency,
   temperature, queue age/high-water, and failure reason.
6. Repair the full positive corpus and broaden negative conditions beyond
   static noise; require zero harmful hallucinated finals before comparing
   medium/small/tiny or closing Gate 0A.

Gate target: final accuracy comparable to batch, p95 END-to-final at most 0.8
seconds, no event-loop blocking, and no thermal/power instability. The bundled
three-positive/four-negative manifest cannot close this gate even if its
provisional report passes.

The superseded four-chunk v1 contract has physical evidence. With clean power,
0.5-second medium replay processed all 33 chunks of case 005 but reached queue
high-water 4, 507.6 ms maximum queue age, and a 639.0 ms synchronous native
call; it narrowly failed partial timing and per-case accuracy. At 1.0 seconds,
the native call reached 710.3 ms, the four-slot queue overflowed, and only
20/33 chunks were processed. The queue backlog drained rapidly once each
native call returned, supporting a larger bounded jitter buffer rather than an
unbounded queue. Contract v2 therefore defaults the Pi worker queue to eight
chunks and admits at most 1024 ms queue age while retaining the 0.8-second
END-to-final limit. Its physical replay reruns preserved every chunk, and the
first real-G2 native run used only 3/8 slots with 537.6 ms maximum age while
processing all 110,400 bytes. That native run returned its final 51.0 ms after
END but failed exact transcript accuracy (3 word errors), so extra final-wait
allowance is not the remedy for the observed miss.

### Gate D — XIAO correctness and shadow PCM

The five-state lifecycle, exact batch ownership, default-off recorder tee,
direct bounded inbox, renewable lease, bulk/live arbitration, and standalone
parity probe are source-implemented and software-tested. Physical PDM parity
has passed. The exact-owned G2 rerun has also passed with a deliberately full
ring: 100,000 live/WAV samples, CRC32 `56ebd586`, valid END reason 0, zero
drops/overflow/faults, and final `mutex_drop=0 decode_fail=0`. One physical
native admission/correlation smoke also passed with exact ID/epoch/event/path
correlation, valid zero-drop END, canonical trimmed WAV, and exact cleanup.
Gate D now means exercise the remaining auth/link/TX faults and repeated/long
latency while the finalized-WAV/batch result remains authoritative. The physical
host-overflow, host-gap, host-abort, and lease-expire matrix is closed: all four
runs exited zero, observed their requested failure, retained a canonical owner
WAV, and reported no control or lease errors. Include
`g2micstats` before/after G2 because downstream live/WAV equality cannot prove
there were no LC3 decode or AFE-mutex drops. Collect final stats in cleanup even
on probe failure. `g2micreset` does not empty the decoded ring or reset AFE
overrun.

That happy-path requirement is closed. Compare AFE overrun as a baseline/delta:
the passing run began at 71 and ended at 607 because the deliberately full ring
continued refilling during the post-stop fetch/cleanup interval; its admitted
PCM remained exact and both integrity counters stayed zero.

The four-fault and native no-STT passes are recorder/fallback provenance
evidence, not streaming-STT results. Preserve them as regression baselines;
the canonical operator runbook and native acceptance record are in
[`CM5_DEPLOYMENT_PATHS.md`](CM5_DEPLOYMENT_PATHS.md#native-hey-even-no-stt-recorder-shadow-smoke).

Continue the coordinated dismissal regression during capture, fetch, STT, ASK
hold, and streamed reply. Include a forced tagged-send failure, a stale-daemon
untagged mutation, an unsuccessful host job, and explicit UART auth/link
teardown; verify `send_failed`, `legacy_command`, the bounded exact-ID host EXIT,
and `host_link_lost` discard without affecting the next wake.

### Gate E — streaming final in production

Feed live PCM to Moonshine, validate END/CRC, call `stop()`, and use the live
final only after parity. Send one final ID-scoped ASK and begin LLM prefill in
parallel; hold the first answer until the calibrated question barrier expires.

### Gate F — optional partial question

Only after the final-only path is stable, try cumulative ASK updates on-eye.
Enable them only if stable prefixes do not visibly restart, corrections replace
cleanly, and stale updates cannot reopen or mutate another session.

## 10. Current priority order

1. Converge and fingerprint the CM5 deployment.
2. Fix/measure the G2 question barrier and visible completion metric.
3. Run the guarded deployed-medium 0.5-second provisional replay slice and
   checker on the Pi; review it before any 1.0-second/model expansion. Repair
   the corpus and add negative controls before claiming full Gate 0A.
4. Cover the remaining auth/link/TX fallbacks and repeated native latency;
   retain the passing exact-owned G2, four-fault, and native no-STT results as
   regression baselines.
5. Deploy and hardware-regress the completed exchange-ID dismissal contract.
6. Keep recorder shadow default-off while analyzing native/fallback evidence.
7. Promote streaming final STT only after the native, replay, and parity gates.
8. Consider partial ASK only as an optional final layer.

This ordering isolates the present display regression before coupling it to a
new audio protocol, and it preserves the existing WAV path as a correctness
backstop throughout.
