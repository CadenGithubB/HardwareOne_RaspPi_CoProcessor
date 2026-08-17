# CM5 AI Service Plan — voice → STT → LLM over the UART link

> **Historical-plan note (2026-08-09):** this file records the original phased
> proposal; it is not the current native EvenAI grammar. The working tree now
> implements finalized-WAV native wake plus exchange-ID-scoped recorder
> ownership, dismissal cancellation, and tagged G2 delivery. It does not
> implement live PCM or production streaming STT. Current behavior is in
> [`../docs/G2_NATIVE_EVENAI_SESSION.md`](../docs/G2_NATIVE_EVENAI_SESSION.md),
> deployment is in [`CM5_DEPLOYMENT_PATHS.md`](CM5_DEPLOYMENT_PATHS.md), and
> remaining live-STT work is in
> [`LIVE_STT_G2_EXECUTION_PLAN.md`](LIVE_STT_G2_EXECUTION_PLAN.md).

**Operational references:** use
[`CM5_DEPLOYMENT_PATHS.md`](CM5_DEPLOYMENT_PATHS.md) for the one canonical
Mac/CM5 source layout and sync workflow. The current live-STT and G2-rendering
assessment is
[`LIVE_STT_G2_ASSESSMENT_2026-08-09.md`](LIVE_STT_G2_ASSESSMENT_2026-08-09.md).

**Goal:** one long-lived CM5-side program (the "AI service") that talks to the
XIAO over the already-implemented UART link, receives voice/prompts from the
firmware, performs speech-to-text AND LLM generation (both engines resident in
the same process, eventually active at the same time), and returns answers to
the XIAO's own display surfaces.

**Status: PLAN ONLY — investigated 2026-08-07** by a 6-agent workflow (UART
transport / audio capture / LLM surface / initiative reversal / host STT
research / host LLM research); all firmware file:line refs verified against
the working tree (which carries the uncommitted UART-link implementation).
**Adversarially verified same day** by a 4-skeptic workflow (firmware
feasibility / protocol arithmetic / host stack / security+conventions):
no claim was refuted outright; the 14 corrections it produced are folded
into the sections below, and §10 records the verdicts. Builds directly on
docs/UART_HOST_LINK_PLAN.md — read that first; nothing here re-litigates it.

**Hardware context:** XIAO ESP32-S3 Sense (8MB flash / 8MB octal PSRAM) ↔
Raspberry Pi 5 today, CM5 on the carrier (docs/CM5_CARRIER_DESIGN_BRIEF.md)
later. UART0 GPIO43/44 ↔ Pi ttyAMA2 @ 921600, link HW-validated 2026-08-07
(login, uartlink status, command round-trips). The carrier is UART-only by
security design — no USB data, no I2C/SPI, no reset line between the boards
(CM5_CARRIER_DESIGN_BRIEF.md:145-159, 193-196). Everything must fit this one
wire. USB/TinyUSB ideas are dead on arrival: the S3's single USB PHY would
sacrifice the USB-Serial-JTAG console AND the carrier deliberately routes no
USB between the modules.

---

## 1. Headline findings (what the investigation established)

1. **Throughput is not the problem.** 8N1 = 10 wire bits/byte, so 921600 baud
   = 92,160 B/s. Canonical mic PCM (HAL_Audio: 16kHz/16-bit mono) = 32,000 B/s
   = 35% of the wire raw, 46% base64. 2,000,000 baud (divider-exact both ends,
   PL011 caps at 3M) drops that to 16-21%. UART is full duplex — outbound
   audio and inbound text don't share budget. The engineering work is framing,
   TX ownership, and initiative — not bandwidth. (Classic-ESP32 boards cap at
   230,400 = 23,040 B/s < 32,000: live PCM is structurally S3-only.)
2. **The audio capture layer already exists and has a proven fork point.**
   HAL_Audio is a single-owner destructive-pull stream (one lease: "mic"
   module or "sr" ESP-SR — HAL_Audio.cpp:184); a CM5 feed must fork inside
   the owner's read loop, never open a second capture. The ESP-SR "snip"
   subsystem (System_ESPSR.cpp:337ff) is a ready-made utterance recorder:
   800ms PSRAM pre-roll ring + up-to-6s PSRAM session buffer + async writer
   task, auto-started on wake detection, with the fork point at
   System_ESPSR.cpp:2133 (raw pre-gain PCM; the AFE copy gets
   applyMicAudioProcessing at :2183). micrecord → WAV also works today
   (System_Microphone.cpp:243).
3. **The LLM chat layer has a real remote-engine seam — with sharper edges
   than "9 functions" suggests (verification-corrected).** All surfaces
   (web/OLED/G2/CLI) converge on chatBeginTurn → llmStartAsync, and every
   consumer pulls from one result buffer (drainEngineLocked,
   System_LLMChat.cpp:175-223). A remote backend that feeds CM5-generated
   text into that buffer inherits every display surface byte-identically;
   nothing pushes — all sinks poll — exactly the shape a UART-fed backend
   needs. BUT: gLLMResultBuf/gLLMResultLen/gLLMResultDone are file-static
   (System_LLM.cpp:156-158), so the backend dispatch and the append helper
   must live INSIDE System_LLM.cpp (or behind new accessors), not in a
   free-standing System_LLM_Remote.cpp; the append ordering to preserve is
   the token-callback ordering at System_LLM.cpp:211-215 (write, then
   publish len), while :2532-2547 is the session-START ordering only
   llmStartAsync needs. chatBeginTurn AND chatRetryLast hardwire
   llmFramePrompt (System_LLMChat.cpp:335, :421) — both call sites need a
   remote-mode bypass or prompts arrive Q:/A:-scaffolded. The full contract
   also includes llmGetStatus (referenced from six files) and an
   llmTokenize stub. Caveat unchanged: ENABLE_ONDEVICE_LLM=0 on today's
   XIAO build excludes ALL of this code including the chat layer and UI
   (CMakeLists.txt:441-457), so using it requires a build-flag decision
   (§6 D5).
4. **Initiative reversal has an in-repo precedent to clone.** The link is
   strictly CM5-as-client. The proven ask-now/answer-later shape is
   espnowmessages: cursor-paged JSON polling of async results
   (System_ESPNow.cpp:16821). A `hostjobs`-style poll command is a pure
   command addition — no framing or routing changes, 'user'-tier, auditable.
   Push lines are a later optimization: a loop-task-owned push queue drained
   in uartLinkTick is structurally interleave-safe (same task writes replies)
   but changes the client parsing contract. The MSG_ROUTE_UART sink stays
   rejected (burns the last routing bit 0x80, 255B clamp — reconfirmed at
   System_Debug.h:25-32, System_Debug.cpp:974-978).
5. **The CM5 can answer to real display surfaces today, and TTS is
   impossible.** oledtext (OLED_Utils.cpp:6589) and g2notify
   (G2_Glasses.cpp:17939) are non-admin display commands reachable over the
   link right now. The XIAO has no audio output path at all — HAL_Audio is
   capture-only — so spoken answers are permanently out of scope for the
   XIAO (a CM5-side speaker would be a carrier/hardware question, not
   firmware).
6. **Host engines fit one process comfortably on an 8GB CM5.** STT:
   Moonshine v2 small — 123M params, MIT, in-process Python, the only engine
   with vendor-published Pi 5 numbers (527ms post-utterance; tiny=237ms).
   LLM: llama-server subprocess (Qwen2.5-1.5B-Instruct Q4_K_M: ~13.8 tok/s
   gen, ~62 tok/s prefill on Pi 5) with KV-prefix caching making <2s
   first-token realistic (per-turn prefill = transcript only, ~0.3-0.7s).
   Generation is memory-bandwidth-bound (~17GB/s LPDDR4X): ~24 tok/s ceiling
   for 1B-class, ~9 for 3B — model size IS the latency choice.
7. **Thermals are a real carrier constraint.** A bare CM5 throttles to
   1.5GHz within 15-28s of sustained all-core load (−40%); active cooling
   holds ~58°C indefinitely. Budget ~12-15W and a fan (or oversized coupled
   heatsink) on the carrier for sustained generation.
8. **Login is fast on real hardware — on this board.** The ~12s PBKDF2
   estimate in the link plan did not survive contact: bench login on the
   XIAO S3 completed in well under 1s (2026-08-07, observed via monitor
   timestamps; not precision-measured). Credible because
   CONFIG_MBEDTLS_HARDWARE_SHA=y makes 10k PBKDF2 iterations tens-to-low-
   hundreds of ms on S3 silicon — which also means the number does NOT
   transfer to classic-ESP32 boards (unmeasured) and would regress if
   HW-SHA were ever disabled. One less constraint on the CM5 client's
   reconnect logic (still: log in once per boot, re-login on
   "Authentication required").

## 2. Architecture

```
XIAO (firmware)                              CM5 (one program: hw1-ai-service)
┌───────────────────────────┐                ┌──────────────────────────────────┐
│ PDM mic → HAL_Audio       │   UART link    │ Link client (async serial)       │
│  └ owner loop fork ──┐    │ 921600/2M 8N1  │  ├ session mgr (login, resync,   │
│ Voice-job FSM (loop   │   │◄──────────────►│  │  reconnect, OTA-probation idle)│
│  tick, no new task)   │   │  text cmds +   │  ├ job poller (hostjobs json)    │
│  ├ trigger: wake/button│  │  framed audio  │  ├ audio rx (burst or stream)    │
│  └ job registry ◄─────┘   │                │  ├ STT engine (in-process,       │
│ Chat layer (remote        │                │  │   Moonshine v2 / zipformer)   │
│  backend writes result    │                │  ├ LLM client → llama-server     │
│  buffer) → OLED/G2/web    │                │  │   subprocess (localhost HTTP) │
│ oledtext / g2notify       │                │  └ answer delivery (llmpush /    │
│  (v1 answer path)         │                │      oledtext / g2notify)        │
└───────────────────────────┘                └──────────────────────────────────┘
```

The CM5 program is ONE supervised daemon (systemd unit) holding both engines:
STT in-process, LLM as a supervised llama-server child spoken to over
localhost HTTP. That satisfies "same program / both at the same time":
both models stay resident; a voice exchange pipelines them (STT then LLM,
each free to use all 4 cores in its phase); true overlap (streaming STT while
the LLM prefills) is a tuning option, not a structural change.

**Why llama-server as a child instead of in-process bindings:** no Python in
the LLM hot path, prefix-KV reuse from cache_prompt (default-on, single
slot) with --cache-reuse 256 as an optional extra for chunk reuse
(verification note: --cache-reuse itself defaults OFF and has had
regressions — verify cache-hit counters on the bench, don't trust the
flag), /health for supervision, crash isolation (a ggml abort can't take
the UART session down), and mmap'd weights stay warm in page cache across
restarts. llama-cpp-python reports 0-28% overhead depending on
configuration plus long-daemon memory-leak reports (the isolation argument
carries the decision, not the percentage); Ollama measures ~10% slower
overall with less control over threads/quant/repack — its 5-min idle
unload is trivially configurable, so the rejection rests on overhead and
control, not keep_alive. If the service is ever rewritten in C++, the
talk-llama pattern (whisper.cpp + llama.cpp, one process, per-engine
thread counts) is the proven shape — same architecture, different linkage.

**Language: Python first — with one hard rule (verification-found).** Every
recommended STT engine is pip-installable with aarch64 wheels and runs
in-process; the link client is pyserial + asyncio; the LLM is HTTP.
Throughput is trivial (92KB/s ≈ 23 wakeups/s at 4KB reads), but the
failure mode is silent byte LOSS, not slowness: no flow control exists,
the kernel tty buffer holds ~64KB ≈ 0.7s at full burst rate, and beyond
that the RP1 UART FIFO overruns silently. The service's own design puts
527ms-blocking STT calls in the same process. Hard rule: a dedicated
reader thread (pyserial → asyncio queue) that NEVER runs inference, all
STT via ThreadPoolExecutor (ONNX Runtime releases the GIL during Run), no
sync HTTP on the event loop, and a P0 soak test that injects deliberate
500ms stalls during a simulated 92KB/s burst to prove zero loss.

## 3. The three firmware gaps (and their phased closure)

The link as shipped does request/response text only. Three gaps stand between
that and the voice pipeline — each has a cheap v1 and a better v2.

### Gap A — getting audio to the CM5

Phased; each phase is independently shippable and useful.

- **A0 (zero firmware change — the walking skeleton):** CM5 drives the
  existing commands: `openmic` → `micrecord start` → (VAD-less fixed window
  or CM5-commanded stop) → `micrecord stop` (reply carries the WAV path) →
  chunked `fileread <path> <off> <len> b64` (~2,880 raw B per 4095B reply,
  ~56 round trips for 5s of audio ≈ 4-8s) → `micdelete`. Costs: admin-tier
  session (fileread is admin-gated, System_Filesystem.cpp:1277), multi-second
  latency, no wake trigger (SR and micrecord are mutually exclusive owners).
  Worth building FIRST anyway — it exercises the entire CM5 program
  (session, poller, STT, LLM, answer delivery) against the validated link
  with zero firmware risk, and the program structure survives into A1/A2.
  One evidentiary caveat (verification-found): A0's WAVs are NOT raw audio —
  recordingTask runs micProcessForSource before every write
  (System_Microphone.cpp:280 → applyMicAudioProcessing: DC removal, ~50Hz
  HPF, pre-emphasis, ~24x software gain), while A1/A2 ship the snip tap's
  raw pre-gain PCM. P0's WER data therefore characterizes processed audio
  and cannot settle the ship-RAW decision (§8) — that answer comes from
  A1-path audio at P2.
- **A1 (burst transfer — the v1 target):** reuse the snip pattern: capture
  the utterance into PSRAM (160KB for 5s; trivial in 8MB), then ship it with
  ONE new command's framed reply stream (`voicefetch` returning consecutive
  framed chunks, or a dedicated bulk-send state the drain enters). Transfer
  cost after end-of-utterance: 1.74s raw / 2.31s base64 @921600; 0.80/1.07s
  @2M. No per-chunk exec round trips. Needs: bulk framing (the thing
  UART_HOST_LINK_PLAN.md:330 deferred), a non-admin scoped read of audio
  buffers (NOT fileread), CRC per chunk.
- **A2 (live streaming — the v2 target):** fork each ~32-128ms chunk at the
  owner loop (System_ESPSR.cpp:2133 when SR owns; the recordingTask loop when
  mic owns) into a PSRAM staging ring; a frame writer emits type-tagged
  frames on the wire as they arrive. CM5 STT decodes DURING speech
  (streaming engines) — answer latency collapses to ~endpoint-timeout +
  LLM TTFT. Needs everything A1 needs plus the TX-mux decision (below).

**TX ownership decision (prerequisite for A1/A2):** today every TX sequence
is two write() calls from the loop task; the HAL mutex is per-call only
(esp32-hal-uart.c:1229-1237), so ANY second writer can split a reply. The
investigation's recommendation, adopted here as the plan of record:
introduce `uartLinkWriteFrame()` owning a link TX mutex; convert the three
existing TX sites to single-write framed output; audio producers call it
from their own task; uartLinkStop takes the same mutex (fixes the
lifecycle-vs-writer race that sPending alone no longer covers). Frame
format: type byte + length + payload + CRC16, delimited COBS-style with
0x00 — a byte the reply pipeline structurally cannot emit (every reply
transits NUL-terminated C-string chokepoints, so an embedded 0x00
truncates before reaching the wire; verified through System_Utils.cpp:5026
and the write sites) — so the existing text protocol keeps working
verbatim during migration. Resync discipline (verification-corrected):
between sessions the wire DOES carry 0x00s (ROM boot bursts read at the
wrong baud, break conditions decode as 0x00), so the demux must resync by
CRC-rejecting garbage frames, not by trusting 0x00 scarcity. Stall math
(verification-corrected): ≤1KB audio frames bound a writer's HAL-mutex
wait to ~11ms against a full TX ring, but while replies stay UNFRAMED the
reply site's single write() of up to 4096B holds the link TX mutex for up
to ~44ms at 921600 — so the A2 staging ring must absorb ≥2-3 frame
periods of writer stall, or reply writes get sliced into ≤1KB mutex
sections when the frame writer lands. Replies keep their current shape in
A0/A1 (the CM5 client already standardizes on json-token whole-document
replies); framing replies too is an A2-time option that would close the
multi-line-reply delimitation hole for free.

**Audio processing decision:** the snip tap carries RAW pre-gain PCM;
applyMicAudioProcessing (DC/HPF/pre-emphasis/~24x gain) is applied only to
the AFE copy. Ship RAW to the CM5 and let the STT side own gain/AGC —
Whisper-family and Moonshine both prefer unmangled input, the CM5 has
infinite CPU for it by XIAO standards, and skipping firmware-side
processing avoids double-driving the shared filter state
(System_Microphone.cpp:154). Revisit only if real-device WER says otherwise.

### Gap B — the XIAO asking for something (initiative reversal)

- **B1 (v1): `hostjobs` cursor-paged poll command**, cloned from
  espnowmessages (System_ESPNow.cpp:16821, 17428-17496). A small job
  registry (NOT the 48-slot event ring — it needs redaction and is too
  small) holds pending host-work items: `{seq, kind: voice|prompt|…, state,
  payload-ref}`. Voice flow: trigger → FSM captures utterance → registry
  posts a job → CM5's next poll sees it → fetches audio (A0/A1/A2) → STT →
  LLM → pushes the answer back (`llmpush`, §Gap C) → job closed. Poll
  cadence 2-5Hz idle is safe: SOURCE_UART is excluded from
  powerSaveNoteActivity (shipped, System_Utils.cpp:4585-4590), each poll is
  a fast command, and the CM5 must go quiet during OTA probation (existing
  client rule). Latency cost of polling (verification-corrected to show the
  tail, not just the mean): mean = half the poll interval (~100-250ms at
  2-5Hz), but worst case = one full interval PLUS cmd_exec occupancy —
  the poll reply serializes behind whatever cmd_exec_task is running
  (unbounded behind a long command; 200ms+ loop stalls are documented).
  Acceptable against STT+LLM times; just don't promise snappy discovery
  while a long command runs.
- **B2 (v2): push lines from uartLinkTick.** A bounded push queue drained by
  the tick after the command drain — same task writes replies and pushes, so
  interleaving is structurally impossible even before the frame writer
  exists. Prefix-disciplined lines (`EVT {json}`). Cuts job-discovery
  latency to one loop lap (~2-16ms) and lets the CM5 sleep on read().
  Client parser must then separate pushes from replies — do it when A2's
  framing lands, not before.
- The voice-job FSM lives in a new sibling module (working name
  `System_HostLink`), ticked from loop() beside uartLinkTick — states
  ARMED → CAPTURING → PENDING_HOST → ANSWERED, flag/queue handoffs from
  sr_task/button, command submission via submitCommandAsync only
  (System_Utils.cpp:5041). No new task — standing project practice
  (avoid per-action tasks); System_TaskUtils.h:148-178 is the core-
  PLACEMENT policy, and it matters here for a different reason: loop()
  shares Core 0 with WiFi/BLE/ESP-NOW, so the FSM tick must stay
  flag-checks-only cheap. UartLink stays pure transport.

### Gap C — answers reaching XIAO surfaces

- **C0 (v1, zero firmware change):** CM5 renders the answer via `oledtext`
  and/or `g2notify [secs] <text>` — both non-admin, both work today.
  Preconditions (verification-found): oledtext hard-errors unless the OLED
  is running (OLED_Utils.cpp:4219-4222 — client probes or runs `oledstart`
  first), and g2notify needs a connected G2 and is a FULL-SCREEN
  placeholder, not an overlay (G2_Glasses.cpp:17612-17613; duration
  clamped 1-599s). Good enough to demo the whole loop; clamped, transient,
  no history.
- **C1 (the real integration): remote LLM backend behind the chat-layer
  seam — scoped per the verification round.** The backend switch, the
  remote dispatch, and the `llmpush` append helper live INSIDE
  System_LLM.cpp (gLLMResultBuf/gLLMResultLen/gLLMResultDone are
  file-static at :156-158 — a free-standing System_LLM_Remote.cpp cannot
  reach them, and under D5(a) the engine symbols already exist). The CM5
  feeds generated text back with `llmpush <session> <seq> <text-chunk>`,
  appending with the token-callback publish ordering (write bytes, then
  bump len — System_LLM.cpp:211-215); the remote llmStartAsync path uses
  the session-START ordering at :2532-2547. Two chat-layer edits are
  required, not optional: remote-mode bypasses of llmFramePrompt at BOTH
  call sites (chatBeginTurn System_LLMChat.cpp:335, chatRetryLast :421) —
  the Q:/A: scaffold is local-model-specific (System_LLM.cpp:1219). The
  remote contract also implements llmGetStatus honestly (six files read
  it) and stubs llmTokenize (retry's suppress-tokens degrade to plain
  re-ask). Every surface — OLED chat page, G2 lens tail, web chat,
  `llmresult json` — then renders CM5 output unchanged, including
  incremental "typing" as llmpush chunks arrive at 2-5Hz. Prompt
  direction rides Gap B: a user question typed/spoken on the XIAO posts a
  `prompt` job; the CM5 poller picks it up. Multi-turn context lives on
  the CM5 (the firmware ring sends no history anyway —
  System_LLMChat.cpp:335 — because on-device ctx≈41 tokens made it
  pointless; the CM5 has no such limit and simply keeps its own).
  Build-flag prerequisite: today ENABLE_ONDEVICE_LLM=0 excludes the chat
  layer and all LLM UI from the XIAO build — §6 D5 decides between
  flipping it on (needs a trial build against ~1.3MB factory headroom) or
  splitting an ENABLE_LLM_CHAT flag (cleaner, bigger refactor, both flag
  combinations must be build-verified per the board-gating footgun).

## 4. The CM5 program (hw1-ai-service) — concrete design

One Python package, one systemd unit, one config file. Components:

1. **Link client.** pyserial(-asyncio) on /dev/ttyAMA2 (bench: same on Pi 5),
   921600 8N1. Owns: garbage-tolerant line reader (resync on newline; ROM
   boot burst at 115200 arrives as garbage on every XIAO reset — expected),
   login/session state (login once, re-login on "Authentication required"),
   the json-token convention for all structured commands, client-side caps
   (cmd ≤2047B, reply ≤4095B), 65s command timeout, crash-history query
   after unexplained silence, OTA-probation quiet rule. Later: COBS frame
   demux for A2 audio/push frames alongside text lines.
2. **Job poller.** Polls `hostjobs json since=<seq>` at 2-5Hz (configurable;
   drop to 0.5Hz when a job is in flight to free the wire). Dispatches by
   job kind to the pipeline.
3. **Audio receiver.** A0: micrecord+fileread orchestration; A1: voicefetch
   burst reassembly + CRC check; A2: streaming frame consumer feeding the
   STT engine incrementally. Always materializes/validates 16kHz/16-bit
   mono PCM.
4. **STT engine (in-process).** Primary: Moonshine v2 small
   (pip moonshine-voice, ONNX Runtime) + silero-vad for endpointing sanity.
   License note: MIT covers the English models; non-English Moonshine
   models are under a non-commercial community license (irrelevant for
   English-first, flagged for later). Fallback (and the C++-future
   option): sherpa-onnx streaming Zipformer en int8 — built-in
   endpointing, smallest RAM, best aarch64 packaging. Robustness escape
   hatch for noisy audio: whisper.cpp base.en-q5_0 (built-in Silero VAD;
   the VAD model is a separate downloaded file). Thread cap 2 via session
   options when the LLM is mid-generation; otherwise 4.
5. **LLM client + supervised llama-server child.** Candidate models
   (verification re-anchored — Qwen2.5-1.5B is a Sept-2024 model; the P0
   bench picks the model of record from): Qwen3-1.7B Q4_K_M (plain
   transformer, llama.cpp-mature, strictly newer/better than Qwen2.5-1.5B
   at ~the same footprint; thinking off), LFM2.5-1.2B (vendor-benched
   10-20 tok/s on Pi 5), and Qwen2.5-1.5B-Instruct Q4_K_M (known-good
   published baseline: 13.8 tok/s gen on Pi 5 8GB). Watch item:
   Qwen3.5-2B (Mar 2026) once its hybrid architecture lands in mainline
   llama.cpp. Server shape regardless of model: `-t 4 -c 2048
   --cache-reuse 256`, single slot, static system prefix so per-turn
   prefill is transcript-only. 4GB-SKU fallback: Qwen3-0.6B (thinking
   off). Quality step-up on 8GB: 3B-class at ~5-9 tok/s. Supervise via
   /health; restart is cheap (mmap keeps weights in page cache). Verify
   ARM Q4_0 runtime repack fires in the server build (ggml issue #12701
   history) — bench Q4_0 vs Q4_K_M prefill once on-device.
6. **Answer delivery.** v1: chunk the reply into `oledtext`/`g2notify`
   commands (mind the 2047B line cap; ~2-3 chunks typical). C1: stream
   `llmpush` chunks at 2-5Hz for live typing on the XIAO surfaces. Always
   ends with a job-close command carrying status.
7. **Ops.** systemd unit (Restart=on-failure), journald logging, one YAML
   config (port, baud, credentials ref, model paths, thread caps, poll
   rates). Credentials: a dedicated device account (per link-plan D2/D3);
   tier decision in §6 D2.

**Concurrency model:** the pipeline per exchange is sequential
(capture→transfer→STT→LLM→deliver) — each stage gets all 4 cores, which is
also the fastest total path on 4 cores. "Both at the same time" =
both engines resident + the service accepting a new STT job while a long
LLM generation streams (thread-capped 2+2 in that window). One LLM
generation in flight at a time (matches the firmware chat layer's own
serialization); a job queue absorbs bursts.

**Expected end-to-end latency** (5s utterance, 8GB CM5, active cooling):

| Phase | A0 today | A1 burst @921600 | A2 stream @921600 |
|---|---|---|---|
| audio transfer | 4-8s | 1.7-2.3s | overlaps speech |
| STT (Moonshine small) | ~0.5s | ~0.5s | ~0.5s after end |
| LLM first token (cached prefix) | 0.3-0.7s | 0.3-0.7s | 0.3-0.7s |
| **first text on XIAO** | **~6-10s** | **~3-4s** | **~1-2s** |

(2M baud roughly halves the A1 transfer row. A 60-token answer streams out
over ~4.5s at 13.8 tok/s regardless of phase.)

## 5. Implementation order

1. **P0 — CM5 walking skeleton (no firmware changes).** Build hw1-ai-service
   with A0 audio + C0 delivery + manual trigger (CM5-initiated "record now"
   instead of hostjobs). Requires an admin-tier account for fileread (bench
   only — revisit at D2). Proves: link client, session handling, STT, LLM,
   delivery, systemd. Also produces the first real-device WER data (PDM mic
   quality vs benchmark audio) and on-device STT/LLM benches that harden §4's
   engine choices.
2. **P1 — firmware: hostjobs + voice-job FSM (Gap B1) + trigger.** Button or
   CLI-triggered capture first; wake word behind D4. CM5 switches from
   manual trigger to polling.
3. **P2 — firmware: frame writer + voicefetch burst (Gap A1).** Drops the
   admin requirement and the multi-second transfer. This is the moment the
   TX mux lands; HW-test the frame CRC path at 921600 and 2M on the bench
   wire.
4. **P3 — firmware: remote LLM backend (Gap C1, per D5) + llmpush.** XIAO
   surfaces now render CM5 answers natively; multi-turn context lives on
   the CM5. File plan per the verification round: dispatch + llmpush
   append inside System_LLM.cpp; framing bypasses in System_LLMChat.cpp
   (:335, :421); llmGetStatus synthesized honestly for remote;
   llmask/guided-menu hidden (llmMenuGroupCount==0 path); engine-only
   commands (llmload/llmkvprec/…) error cleanly under backend=remote.
5. **P4 — streaming (Gap A2) + push lines (B2), only if P2 latency
   disappoints.** Streaming STT engines are already in place from P0; this
   is purely firmware transport work.
6. **P5 — wake word (D4): ENABLE_ESP_SR=1 trial build**, partition-layout
   decision (3008K model partition shrinks LittleFS to 128K → recordings to
   SD, or cut a WN9-only ~320K model partition variant), WakeNet-only mode
   (MultiNet can be dropped — startESPSR already tolerates its absence,
   System_ESPSR.cpp:2630). SR has never been HW-validated on current
   boards (docs/ESPSR_VOICE_TABLE_PLAN.md:70) — this phase carries real
   bring-up risk and sits deliberately last: everything before it works
   with button/CLI/CM5-initiated triggers.

Each phase is independently testable on the bench (FeatherS3 TX/RX header or
XIAO + Pi 5) and nothing depends on the carrier existing.

## 6. Decisions needed before/while implementing

- **D1 — CM5 RAM SKU: 8GB recommended.** 4GB pins the LLM to the 0.6-1.7B
  tier (a 3B Q4 technically fits but mmap-thrashes); STT is never the
  binding constraint at any SKU (verification-corrected — Moonshine small
  is ~0.5GB). 8GB is justified by 3B-class step-up headroom and page-cache
  comfort; 16GB buys nothing interactive (7-8B runs 2-3 tok/s). Decide
  before buying; nothing in the design branches on it except model choice.
- **D2 — CM5 account tier.** P0 needs admin (fileread). From P2 on, 'user'
  tier suffices IF voicefetch/llmpush/hostjobs are registered non-admin
  (they should be: no filesystem exposure, job-scoped payloads). Decide:
  temporary admin bench account now + demote at P2, or scoped-read
  capability work earlier. Recommendation: temporary admin on the bench,
  'user' from P2.
- **D3 — target baud.** 921600 is validated; 2M is divider-exact both ends
  and halves A1 transfer time. HW-test 2M early (one `uartlinkbaud 2000000`
  + loopback soak); adopt if clean, else stay.
- **D4 — trigger UX.** Wake word needs P5 (SR bring-up + partition change +
  flash-fit trial build, all unvalidated). Button/G2-tap/CLI triggers cost
  nothing and ship in P1. Recommendation: ship P1-P4 on button/CLI, decide
  wake word after P2 latency is known.
- **D5 — remote-LLM build shape on the XIAO.** (a) flip ENABLE_ONDEVICE_LLM=1
  and add backend=remote inside it — smallest diff, costs engine+esp-dsp
  flash against ~1.3MB headroom (trial build needed; the engine would sit
  unused unless a local model is ever loaded on XIAO); or (b) split
  ENABLE_LLM_CHAT so chat layer + surfaces + the remote shim compile
  without the engine — cleaner but MUCH bigger than "9 functions": the
  compiled surfaces reference llmGetStatus from six files plus the
  llmLoad/llmMenu/llmContext families (full symbol inventory needed), any
  new flag needs the same CMake literal-grep plumbing as
  ENABLE_ONDEVICE_LLM (CMake does not evaluate the preprocessor —
  System_BuildConfig.h:302-305), and both flag combinations must be
  build-verified (board-gating footgun). Two headroom caveats
  (verification-found): the ~1.3MB figure holds only for the no-SR
  partition layout — the SR layout's factory is 340K smaller (4992K vs
  5332K), so D5(a) and wake word (P5) compete if ever combined.
  Recommendation: (a) trial build first; if it fits, ship P3 with (a) and
  defer (b) to a cleanup pass; if it doesn't fit, (b) is forced.
- **D6 — answer length policy.** Firmware turn cap is 2048B/turn and 8KB
  result buffer — plenty for voice answers; the CM5 system prompt should
  target 1-3 sentence answers anyway (a 60-token answer already takes
  ~4.5s to stream). Keep caps; truncate-with-marker at the CM5 side.
- **D7 — carrier thermal provision.** Add the fan header's actual fan (or
  an oversized coupled heatsink) before sustained-LLM use; a bare CM5
  loses 40% clock within 30s of sustained generation.

## 7. Security posture (deltas on top of the link plan)

- hostjobs/voicefetch/llmpush register non-admin but session-bound; job
  payloads never embed credentials; ORIGIN_UART stays excluded from
  physical-presence surfaces (unchanged). All three follow the uniform
  OK:/Error: return contract (stampOkStatus) and the cliHint dead-end
  convention like every other command.
- **voicefetch is a deliberate privilege reduction — say it plainly**
  (verification-corrected framing): today raw mic bytes are only readable
  over the link via admin-gated fileread; voicefetch moves live room
  audio below the admin boundary to 'user' tier. That is the point (D2's
  isolation story), but it makes two things HARD prerequisites, not
  recommendations: uartRequireAuth=1 with a dedicated non-admin account,
  and voicefetch refusing AuthBypass callers outright. IMPLEMENTED &
  adversarially confirmed 2026-08-07: cmd_voicefetch gates unconditionally
  on gUartAuthed (so AuthBypass — gUartAuthed=false — is refused even in
  auth-off mode), enforces transport==SOURCE_UART, and double-guards path
  traversal (indexOf("..") + prefix check, then normalizeFsPath rebuild +
  canRead); the reviewer could not break the boundary.
- **voicefetch access is device-global, by design and by precedent**
  (recorded decision, not an oversight): recordings live flat in
  /recordings or /sd/recordings with no per-user ownership, so any
  authenticated UART session can pull any recording — identical to the
  existing fileread/miclist exposure. This firmware has no per-user
  recording isolation anywhere; recordings are the device's, not a
  user's. If per-user scoping is ever wanted it is a filesystem-wide
  change (per-user prefixes + ctx.scope), not a voicefetch-local one.
- The job registry is CM5-facing by design — do NOT back it with the typed
  event ring: SYSEVT_REMOTE_CMD_RX detail[] carries raw remote command
  text including plaintext credentials (confirmed: System_ESPNow.cpp:6686
  and System_MQTT.cpp:543 post raw command text;
  System_Automation.cpp:3788). Separate registry, separate contract.
- Utterance audio transits the wire unencrypted — the honest anchor is
  that this equals the channel's existing trust level: `login <user>
  <pass>` already crosses the same wire in plaintext
  (System_UartLink.cpp:181-206). The carrier's physical-isolation argument
  only applies once the carrier exists; P0-P4 run on bench jumper wires.
  PCM buffers in PSRAM are established practice (the snip subsystem
  already allocates PCM there) and outside the no-secrets-in-PSRAM rule's
  wording (crypto keys/session secrets) — though voice audio is
  privacy-sensitive and PSRAM is externally probeable with flash
  encryption off, so this leans on the same physical-access assumptions
  as the rest of the device.
- The CM5 answers as text injected into display surfaces — treat llmpush
  content as untrusted display data (length-clamped, no command
  interpretation anywhere on its path). Verified: the OLED/G2 render
  paths are bounds-safe glyph renderers, and the web chat renders via
  textContent, not innerHTML (WebPage_LLM.h:424) — no new injection class
  beyond what non-admin oledtext/g2notify already permit today.
- Streaming frames check gUartAuthed && sStarted per frame. Verification
  caveat: both are plain non-volatile bools, so a cross-task producer
  gets TOCTOU + stale-read windows — the check BOUNDS post-revocation
  leakage to frames-in-flight rather than eliminating it, and both flags
  need volatile/atomic semantics (or a mutex-guarded read) before A2's
  cross-task writer treats them as a security gate. Not a v1 blocker
  (A2 is P4).

## 8. Bench/validation plan

Firmware-side (per phase): frame CRC soak at 921600/2M (P2), voicefetch
integrity vs known WAV (P2), llmpush publish-ordering vs a polling web
client (P3), wizard-parking and OTA-probation interplay with an active
poller (P1), revocation mid-stream (P2+).

CM5-side (P0, ~an afternoon of benches that harden every §4 choice):
- Moonshine v2 small + tiny: solo RTF and 2-thread-capped RTF while
  llama.cpp decodes (the real operating condition — no published numbers
  exist for it anywhere).
- sherpa-onnx Zipformer int8 RTF on A76 (docs' RTF platform is unstated).
- llama-bench the §4.5 candidate ladder: Qwen3-1.7B (thinking off),
  LFM2.5-1.2B, Qwen2.5-1.5B baseline; Q4_K_M vs Q4_0 (repack delta);
  confirm llama-server's prompt-cache HIT counters, not just the flags.
- The reader-stall soak: inject 500ms event-loop stalls during a simulated
  92KB/s inbound burst; zero byte loss required (silent tty overflow is
  the failure mode being hunted).
- WER on actual XIAO-PDM-captured audio vs clean benchmarks. Caveat from
  the verification round: P0's WAVs are firmware-PROCESSED audio (24x
  gain/HPF/pre-emphasis); they qualify engine robustness on this mic but
  the raw-vs-processed shipping decision waits for A1-path audio at P2.
- Thermal: sustained-generation clock trace on the bench Pi 5 (proxy) and
  later the CM5+carrier.

## 9. Out of scope / explicitly rejected

- **TTS on the XIAO** — no audio output hardware path exists; permanently
  out unless the carrier grows a CM5-side speaker (separate question).
- **CM5→XIAO audio streaming** — RX ring is 4096B (=128ms) against
  documented 200ms+ loop stalls; silent overflow guaranteed. Not needed by
  this design (answers are text).
- **USB between the modules** — carrier security invariant; single-PHY
  console cost. Closed permanently by CM5_CARRIER_DESIGN_BRIEF.md.
- **MSG_ROUTE_UART sink bit** — stays deferred per link-plan D4; nothing
  here needs it (hostjobs/push-queue cover the async plane without burning
  routing bit 0x80).
- **MQTT loopback as the transport** — defeats the UART-only offline
  isolation model; per-message plaintext credentials; 2047B silent
  truncation. The wire exists; use it.
- **Classic-ESP32 live audio** — 230,400 baud ceiling < PCM rate;
  compressed (ADPCM) support only if a classic board ever actually needs
  voice (none is a carrier candidate).

## 10. Adversarial verification summary (2026-08-07)

Four skeptics (firmware feasibility / protocol arithmetic / host stack /
security+conventions) attacked the draft. Zero claims REFUTED; the
architecture, phasing, and every load-bearing number survived. What the
attack proved out: the HAL_Audio lease model and snip fork point are
exactly as claimed; the TX-mux necessity is real (per-call HAL mutex,
two-write replies); the espnowmessages clone source, MSG_ROUTE_UART
rejection grounds, partition/headroom numbers (1.30MB computed), all
throughput/transfer arithmetic (fileread rawCap computes to 2,904B/reply),
2M-baud divider exactness on BOTH ends (S3 XTAL 40e6/2e6=20; RP1 PL011
48MHz IBRD 1+FBRD 32/64), the 0x00-free reply pipeline, CRC16 adequacy,
the event-ring credential leak, llmpush's injection-safety (web renders
via textContent), and the CM5 throttling/thermal numbers.

Fourteen corrections were folded into the sections above, the substantive
ones being: (1) the C1 remote-backend work lives inside System_LLM.cpp
(file-static result globals) with framing bypasses at both
System_LLMChat.cpp call sites — the "9-function seam" understated the
surface; (2) A0 audio is firmware-processed, so P0 WER data cannot settle
the ship-RAW decision; (3) worst-case TX-mutex hold is ~44ms (unframed
4095B reply), not 11ms — sizes the A2 staging ring; (4) poll-latency
figures were mean-only — the tail includes cmd_exec occupancy; (5) the
Python service needs a dedicated never-blocking reader thread or the
no-flow-control wire drops bytes silently past ~0.7s of stall; (6) the
model of record was two generations stale — P0 benches Qwen3-1.7B and
LFM2.5-1.2B against the Qwen2.5-1.5B baseline; (7) voicefetch is a
deliberate privilege reduction on mic audio and its auth prerequisites
are hard requirements; (8) gUartAuthed/sStarted need atomic semantics
before A2's cross-task writer can rely on them.
