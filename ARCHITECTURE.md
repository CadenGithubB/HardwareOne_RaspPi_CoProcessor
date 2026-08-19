# hw1-ai-service — program architecture

> **Current-state note (2026-08-09):** Sections labeled P0 preserve the
> original walking-skeleton architecture and are not an as-built native
> EvenAI protocol reference. The service now has a finalized-WAV native wake
> path with exchange-ID-scoped recorder ownership, cancellation, and tagged
> G2 delivery. A default-off exact-owner recorder-shadow transport and
> standalone live/WAV parity probe are implemented, but no physical shadow run
> has occurred and production streaming STT remains unimplemented. Use
> [`ai-service/README.md`](ai-service/README.md) for current operations,
> [`../docs/G2_NATIVE_EVENAI_SESSION.md`](../docs/G2_NATIVE_EVENAI_SESSION.md)
> for the current wire/lifecycle contract, and
> [`LIVE_STT_G2_EXECUTION_PLAN.md`](LIVE_STT_G2_EXECUTION_PLAN.md) for future
> live-STT work.

The CM5-side companion program: one long-lived user daemon that speaks the UART
link, provides independent Linux power and fan-control bridges, receives
voice/prompts from the XIAO, runs speech-to-text and LLM generation when those
engines are available, and returns answers to the XIAO's display surfaces. A
separate, narrowly privileged system service owns the persistent fan curve and
its fail-safe sysfs writes.

Deployment has one canonical source tree and virtual environment; see
[`CM5_DEPLOYMENT_PATHS.md`](CM5_DEPLOYMENT_PATHS.md) before syncing, installing,
or benchmarking the service.

This document is the program-level design. The system-level plan that preceded
it (why UART, firmware gaps, phasing, decisions) and the adversarial audit that
hardened it were session records for one specific rig; they are kept locally
rather than tracked here. The reusable procedures distilled from that work are
in [`docs/investigations/`](docs/investigations/README.md). Code lives in
`ai-service/`.

Historical scope note: everything below targets **P0 — the walking skeleton** that runs
against today's firmware with zero firmware changes, but every module is
placed where the P1-P4 firmware phases will need it (see §8, the evolution
map — later phases ADD modules and states; they do not restructure).

---

## 1. Process model

```
hw1-ai-service (Python 3.11+, one systemd unit)
│
├── main thread — asyncio event loop
│     orchestrator (pipeline FSM), job scheduling, LLM HTTP client
│     (streaming), answer delivery, llama-server health supervision,
│     typed power/fan-event workers, serial TX (small writes only,
│     single-writer lock), CM5 presence actor, systemd watchdog keep-alive
│
├── serial reader thread (daemon)            ← THE hard rule lives here
│     blocking pyserial reads → line assembly → loop.call_soon_threadsafe
│     into an asyncio.Queue. It also performs outer text/COBS demux and offers
│     immutable live frames to a direct sink. It NEVER runs inference, parses
│     command JSON, or touches the pipeline.
│
├── STT executor — ThreadPoolExecutor(max_workers=1)
│     ONNX Runtime / whisper.cpp calls (they release the GIL); the event
│     loop awaits run_in_executor and stays responsive during the 0.2-3s
│     an utterance takes to decode.
│
├── one retained STT-load worker
│     synchronous native model construction runs off-loop. The task is
│     shielded and reused across UART reconnects so a link flap cannot launch
│     overlapping model loads; the UART pump and control workers remain responsive.
│
└── llama-server — supervised CHILD PROCESS (not a thread)
      llama.cpp server on 127.0.0.1:<port>, OpenAI-compatible API,
      cache_prompt prefix reuse, spawned/health-checked/restarted by the
      service. Crash-isolated: a ggml abort cannot take the link down.
```

Why this shape (established by the plan + audit):

- **The wire has no flow control and drops bytes silently.** The kernel tty
  buffer absorbs ~64KB ≈ 0.7s at full burst rate; anything that stalls the
  reader longer than that during an A1/A2 audio burst corrupts audio with
  no error anywhere. Hence the dedicated reader thread with a never-block
  contract, inference in an executor, and no synchronous HTTP on the event
  loop. A P0 soak test proves the property before it matters (§7).
- **One command in flight at a time.** The firmware drain executes one line
  per loop lap and replies are unframed text — pipelining buys nothing and
  breaks reply attribution. The Session layer enforces strict
  request→response with an asyncio.Lock.
- **Both engines resident, phases sequential.** STT then LLM per exchange,
  each free to use all 4 cores in its phase (fastest total path on 4
  cores). Thread caps (STT=2) apply only when a new utterance arrives
  while the LLM is still streaming an answer.

### Service watchdog and restart boundary

The user unit remains `Type=exec`, uses `Restart=always`, and enables a
60-second systemd service watchdog with `NotifyAccess=main`. At the beginning
of `_run()`, before credentials are read or UART login can consume its retry
window, `SystemdWatchdog` reads `NOTIFY_SOCKET`, `WATCHDOG_USEC`, and the
optional `WATCHDOG_PID`. It immediately sends `WATCHDOG=1`, then repeats at
half the manager-provided timeout. The notification variables are consumed so
the supervised `llama-server` child cannot inherit the ability to keep the
unit alive.

The sender is an asyncio task, deliberately not a thread. A blocked main loop
therefore misses the deadline; systemd uses `SIGKILL` to avoid a large model
process core, `KillMode=control-group` removes the child, and the unit restarts
after five seconds. Properly off-loop STT/model work may remain busy while the
responsive control loop continues pinging. This watchdog does not detect a
kernel/systemd hang and does not provide a hardware reset path. A service
restart also does not relax the host-power FSM: same-boot committed ambiguity
remains fail-closed and requires the existing recovery procedure.

## 2. Module map

```
ai-service/
├── pyproject.toml            # package: hw1_ai_service; deps: pyserial, httpx, pyyaml
├── README.md                 # install + first-run runbook (Pi side)
├── config.example.yaml
├── systemd/hw1-ai-service.service
├── systemd/hw1-power-helper  # self-contained root-owned typed helper
├── systemd/hw1-power-helper.sudoers
├── systemd/install-power-helper.sh
├── systemd/hw1-fan-controller # persistent root curve/safety/socket daemon
├── systemd/hw1-fan-controller.service
├── systemd/hw1-fan-controller.example.json
├── systemd/install-fan-controller.sh
├── hw1_ai_service/
│   ├── __main__.py           # CLI: daemon | ask | chat | probe | bench-soak
│   ├── config.py             # YAML → typed Config (dataclasses), validation
│   ├── log.py                # logging setup (journald-friendly, no timestamps under systemd)
│   ├── systemd_watchdog.py   # stdlib sd_notify keep-alive on the asyncio loop
│   ├── cm5_presence.py       # acknowledged starting/ready/busy/degraded
│   │                         # heartbeat actor; legacy-firmware reprobe
│   ├── link/
│   │   ├── transport.py      # SerialTransport: reader thread, text/COBS demux,
│   │   │                     #   direct immutable live sink, resync, TX
│   │   ├── session.py        # Session: login/re-login, command(), reply
│   │   │                     #   collection (json-line / status-line / quiet-gap),
│   │   │                     #   lockout backoff, OTA-probation quiet mode
│   │   └── protocol.py       # constants, COBS/CRC frame codec, strict live
│   │                         #   payload codecs, reply/quoting helpers
│   ├── audio/
│   │   ├── live.py           # bounded controller stream inbox + validation
│   │   ├── fetch.py          # A0 fetch: openmic → micrecord start/stop →
│   │   │                     #   chunked fileread b64 → reassemble → micdelete
│   │   │                     #   [P2: +voicefetch burst path]
│   │   └── wav.py            # minimal WAV parse/validate → (rate, pcm bytes)
│   ├── stt/
│   │   ├── base.py           # STTEngine protocol: transcribe(pcm16, rate) -> str
│   │   ├── moonshine.py      # Moonshine v2 wrapper (import-guarded)
│   │   ├── zipformer.py      # sherpa-onnx streaming wrapper (import-guarded)
│   │   └── fake.py           # deterministic fake for tests/dry-run
│   ├── llm/
│   │   ├── server.py         # LlamaServerSupervisor: spawn, /health, backoff restart
│   │   ├── client.py         # streaming chat completion; history (deque, trimmed);
│   │   │                     #   static system prompt (prefix-cache friendly)
│   │   └── fake.py           # echo fake for tests/dry-run
│   ├── deliver.py            # answer → oledtext/g2notify command chunks
│   │                         #   [P3: replaced by llmpush streaming]
│   ├── jobs.py               # P0: manual trigger source. [P1: hostjobs poller —
│   │                         #   same JobSource interface, different impl]
│   ├── power.py              # finite host-power EVT parser, ACK/report FSM,
│   │                         #   request-ID dedupe, helper client, auto policy
│   ├── fan.py                # finite host-fan EVT parser, ACK/report FSM,
│   │                         #   epoch-bound bridge to the root Unix socket
│   └── pipeline.py           # VoicePipeline: trigger → fetch → stt → llm → deliver;
│                             #   also text-only path (chat)
└── tests/
    ├── fake_firmware.py      # firmware double speaking the real drain protocol
    │                         #   over a pty (login gate, micrecord, fileread b64,
    │                         #   oledtext, garbage/ROM-burst injection)
    ├── test_transport.py     # resync, line cap, reader-thread queue behavior
    ├── test_session.py       # login, re-login, timeout, lockout backoff
    ├── test_fetch.py         # end-to-end WAV round-trip vs fake firmware
    ├── test_deliver.py       # chunking under the 2047B line cap
    ├── test_pipeline.py      # full exchange with fakes
    ├── test_live_pcm_shadow_probe.py # PDM/G2 fake parity/fault/cleanup probe
    ├── test_fan.py           # strict UART/socket bridge + dedupe/epoch tests
    ├── test_fan_daemon.py    # fake-sysfs curve, safety, discovery, rollback
    └── test_soak.py          # stall-injection burst soak (slow marker)

tools/live_pcm_transport_probe.py     # standalone deterministic UART probe
tools/live_pcm_shadow_probe.py        # standalone exact-owned live/WAV probe
```

Dependency rule: `link/` knows nothing about audio/STT/LLM. `pipeline.py`
is the only module that knows the whole story. Engines are behind
protocols (`STTEngine`, LLM client interface) with fake implementations —
the entire pipeline is testable on a dev Mac with no Pi, no models, no
firmware.

## 3. The link layer (the part that must be right)

### transport.py — SerialTransport

- Owns the serial fd and the reader thread. Reader loop: `ser.read(4096)`
  with a 50ms timeout, feed a byte-accumulator, emit complete lines
  (`\n`-terminated, `\r` stripped) into the RX queue via
  `loop.call_soon_threadsafe`. Lines longer than 8KB without a newline are
  discarded as garbage (defensive symmetric of the firmware's 2047 cap —
  legitimate replies are ≤4095B + newline).
- Resync is implicit and free: garbage (ROM boot burst at wrong baud,
  break noise, partial lines from a mid-line connect) either fails UTF-8
  decode (replaced, flagged) or fails the reply classifiers upstream; the
  next `\n` restores line alignment. That is the firmware's own recovery
  story, mirrored.
- TX: `write_line(str)` — asserts ≤2047 bytes encoded, appends `\n`, one
  `ser.write()` call, `write_timeout=2`. Called only under the Session
  command lock (single-writer discipline; matches the firmware's
  loop-task-only writer).
- Reconnect: on serial exceptions the transport closes, backs off
  (1s→2s→5s cap), reopens, and signals the Session to re-login. The XIAO
  resetting (ROM burst, then silence, then a live drain) is a NORMAL
  event, not an error path.
- The reader already demultiplexes 0x00-delimited COBS frames interleaved with
  text. An immutable live sink is installed before open and claims live types
  synchronously on the reader thread before the generic asyncio queue; this
  prevents high-rate PCM from inheriting that queue's drop-oldest behavior.

### session.py — Session

- `await session.command(line, *, timeout=65, expect="auto") -> Reply`.
  One in flight (lock). 65s default timeout outlasts the firmware's 62s
  worst case (2s queue + 60s semaphore).
- **Reply collection** — the honest P0 answer to "replies are unframed":
  - `json` mode: collect lines until one parses as a complete JSON
    document (the plan's json-token convention; used for fileread and every
    structured command). Non-parsing lines before it are logged as stray
    output and skipped.
  - `status` mode: first line beginning `OK` / `Error:` terminates (used
    for micrecord/openmic/oledtext-class commands with single-line replies).
  - `auto` (default): status line terminates; any other line starts
    quiet-gap collection — reply is complete after 150ms with no further
    lines. Correct-by-construction for single-line replies, honest
    best-effort for multi-line ones; P2 reply framing retires the gap
    heuristic entirely.
- Login FSM: send `login <user> <pass>`, expect `OK: logged in as`.
  On any reply containing `Authentication required` → re-login once, then
  replay the command. On `login locked out` → parse retry seconds, back
  off exactly that long. Credentials come from config (file mode 600) —
  they cross the wire in plaintext, same trust level as the channel
  (documented in the plan §7).
- OTA-probation quiet mode: `session.quiesce(seconds)` — pipeline calls it
  when a reboot is detected (ROM burst seen); no commands until the timer
  expires or the firmware answers a gentle `uartlink status` probe.

### cm5_presence.py — application readiness actor

One asyncio actor owns every `cm5 heartbeat 1 <seq> <mode>` command and uses
the ordinary Session lock. It never creates a second serial writer. Mode
changes use acknowledged generations: `starting` covers model load and is
explicitly ACKed before reboot probation; `ready` means native-wake admission
is safe; `busy` brackets one owned pipeline job; `degraded` keeps the control
plane live while declining AI admission. The actor renews every five seconds
and validates sequence, mode, nonzero firmware epoch, and fixed lease in every
reply.

Firmware has two authenticated UART control-plane routes that avoid unrelated
executor latency. Heartbeat renewal always uses its small intrinsic. A
canonical lowercase `liveaudio ready 1 <16-hex-controller>` line uses a second
intrinsic only while it extends an unexpired lease owned by that controller and
the current named-login epoch. Initial acquisition, expiry, mismatch, and
repair fall through to the ordinary registry path, which remains responsible
for authorization, CRC self-test, worker creation, and lease replacement. The
wire request and reply are unchanged, so an older firmware simply continues to
process every ready line through the registry; there is no feature negotiation
or host-side migration.

On Linux both actors still use `Session.command()` and its single
writer/reply collector; these are firmware routing distinctions, not a second
UART channel. Heartbeat and healthy-ready authority require a named, known,
unbanned, non-guest UART login and are captured with that session's boot-local
generation. Read-only `cm5 status`, `cm5 capabilities`, `liveaudio status`, and
`liveaudio capabilities` remain ordinary registry commands. Firmware keeps the
case-insensitive LiveAudio inspection leading-token forms out of the shared CLI
feed and CM5 command-busy/grace accounting, but they still use normal
authorization and command audit.

A command or login failure becomes `LinkClosed` so the existing TaskGroup
supervisor reconnects every component together. Cancellation wakes any mode
waiter, preventing sibling-cancellation deadlock. Firmware without the
CM5-presence heartbeat intrinsic selects legacy behavior; the actor reprobes
every 60 seconds and on
link reset so daemon-first deployment discovers a later firmware OTA.

Reboot fencing is immediate even while the pipeline is idle: ROM garbage or
an unexplained authentication-epoch loss synchronously changes presence to
`starting`, invalidates any captured non-starting heartbeat before Session can
re-login/replay it, and advances a generation watched by the TaskGroup. The
watcher wakes a pipeline blocked in `next_job()`, forces a clean reconnect,
and leaves the sticky reboot hint for startup probation to clear before
`ready` is advertised again.

The systemd watchdog remains independent: it asks whether the asyncio process
needs restart. CM5 presence asks whether the authenticated UART application
recently answered and whether it is ready for a new native G2 exchange.

### power.py — independent host power control plane

Power control is deliberately independent of the model plane. In daemon mode,
`__main__.py` installs the EVT callback and starts `PowerController` plus
`Session.pump_events()` before STT/LLM construction. Native STT construction
runs in one retained `asyncio.to_thread` task; a canceled link TaskGroup shields
and reuses that task. Model load failure degrades jobs but leaves status,
profiles, reboot, halt, suspend rejection, and timed sleep responsive. A strict
RAM-preflight failure similarly selects control-only mode when power or fan
control is enabled.

The wire direction is finite and versioned:

- Firmware → CM5 EVT frames: `cm5_power_<operation> 1 <16-hex-id> [minutes]`.
  Operations are exactly status, four profiles, reboot, halt, suspend, and
  bounded `sleep_for`; arbitrary text is rejected before any process API.
- CM5 → firmware authenticated Session commands: `cm5 power ack 1 ...` and
  `cm5 power report 1 ... <32-hex-linux-boot-id>`. A disruptive helper action
  cannot begin until both its `accepted` and `committed` ACKs receive OK.

An explicit queued/in-flight/accepted/committed/terminal FSM and bounded ID
cache make firmware retries at-most-once. Cancellation or link loss before
commitment requeues the original; an uncertain committed ACK is reconfirmed
before the helper runs. After execution starts, duplicates can only replay the
cached ACK. `LinkClosed` always propagates to the outer supervisor. Serial
reconnect replays only uncertain callbacks, newest first, and does **not**
originate a new host-wake claim. It may finish an undelivered startup/resume
report, but retires that claim as soon as a destructive EVT arrives so it cannot
race recovery of Accepted. `cm5 power report 1 0 awake ... <boot-id>` is reserved
for a new controller process/cold boot and actual suspend resume. Firmware snapshots the
kernel boot ID at acceptance: same-boot startup safely fails Accepted, changed
boot completes Committed/Applied, and same-boot Committed remains fail-closed.
A superadmin can clear that exceptional state with `cm5 power recover confirm`
only after independently verifying no host transition remains queued.

Privilege is a separate fixed boundary. The user daemon launches only
`/usr/local/libexec/hw1-power-helper` (optionally through fixed `/usr/bin/sudo
-n`) with enum-derived argv via `create_subprocess_exec`; there is no shell or
remote command string. The helper is self-contained and root-owned rather than
importing the user-writable virtualenv. Sudoers grants a dedicated `hw1-power`
group only the enumerated actions and bounded numeric sleep form. The installer
validates a root-owned temporary sudoers snapshot before atomically activating
it.

Profiles are `eco|balanced|performance|auto`. Manual concrete selection persists
across jobs. Auto serializes policy decisions and sysfs writes: the active
profile brackets each pipeline job, and the idle profile is restored after a
debounce. The helper preflights all cpufreq policies, writes an available fixed
governor, and verifies readback before reporting success.

Suspend is disabled by default and requires both explicit config opt-in and
`/sys/power/state` support. Timed sleep verifies a relative RTC wakealarm before
issuing asynchronous halt; Raspberry Pi 5/CM5 low-power alarm wake additionally
requires EEPROM `POWER_OFF_ON_HALT=1` and `WAKE_ON_GPIO=0`, which software cannot
infer from the sysfs readback alone.

### fan.py + hw1-fan-controller — independent host fan control plane

`fan.py` is an unprivileged, nonblocking bridge. It accepts only the versioned
EVTs `cm5_fan_status 1 <id>` and `cm5_fan_mode_auto|quiet|max 1 <id>`, queues
them outside the Session event callback, and returns finite authenticated
`cm5 fan ack` / `cm5 fan report` commands. The XIAO supplies no path, threshold,
PWM, or shell fragment. IDs are cached for dedupe within one authenticated UART
epoch; link reset discards old records because firmware deliberately rejects a
callback after a replacement login. An idempotent mode may already have reached
Linux when the link breaks, so the next epoch reconciles it with a fresh status
request rather than inheriting old callback authority.

The persistent root `hw1-fan-controller` exposes one group-restricted Unix
socket with the exact grammar `status` or `mode auto|quiet|max`. It discovers a
unique hwmon device named `pwmfan`, a unique `pwm-fan` cooling device, and the
thermal zone actually binding them; `hwmonN` indexes and caller-provided paths
are never trusted. It drives `pwm1` directly while leaving the kernel
`step_wise` governor in charge of that zone — Pi kernels ship no `user_space`
governor, and disabling the zone would forfeit its critical trip. The kernel
writes the cooling device only on a trip-target change, so the poll loop's
re-read and re-assert bounds any override to one interval, and a duty that does
not stick is retried once before counting as I/O failure. Releasing the fan
bounces the zone (`mode` `disabled` then `enabled`) so the thermal core resumes
control, since rewriting the already-current policy would strand the last
commanded duty. It verifies every policy/PWM write, and runs the root-owned
step curve with downward hysteresis. Auto follows the curve, Quiet holds
configured PWM, and Max holds 255. Tach RPM is bounded telemetry, not the
controlled variable.

Normal curve writes cannot override Quiet or Max. Independent critical-
temperature and sustained-zero-RPM latches can override Quiet to effective Max,
with start boost and explicit `safety_temp` / `safety_stall` health. Missing
tach is reported but does not invent a stall. Startup always requests Auto;
remote Quiet is not persisted. Activation is transactional, sysfs I/O failure
attempts maximum PWM, and graceful stop restores `step_wise` through the
verified handle or attempts verified Max if that policy write fails. A
critical fault is logged if neither action can be confirmed. A separate
systemd watchdog plus best-effort `ExecStartPre`/`ExecStopPost --restore-kernel`
attempts recovery for a wedged or `SIGKILL`ed controller while the unique sysfs
topology remains discoverable. The user AI daemon never receives sysfs write
permission and cannot widen the controller's finite command vocabulary.

## 4. Audio path (P0 = A0 fetch)

`audio/fetch.py`, driven by the pipeline:

1. `openmic` (`status` reply; tolerate "already open" phrasing).
2. `micrecord start` → sleep `record_seconds` (config, default 4.0 — fixed
   window; there is no endpointing in A0) → `micrecord stop` → parse the
   WAV path out of the reply.
3. Chunk loop: `fileread "<path>" <off> <len> b64` (`json` replies),
   `len` chosen so the b64 envelope fits the 4095B reply (≈2.8KB raw per
   trip, ~56 trips for 5s — 4-8s total; this is the known A0 cost).
   Reassemble, base64-decode, verify byte length against the file size the
   first chunk reports.
4. `wav.py` parses the header (rate/channels/bits must be 16k/1/16 —
   anything else is a hard error, not a resample).
5. `micdelete <name>` cleanup (admin — P0 runs on the temporary admin
   bench account per plan D2; delete failure is a warning, not fatal).

Known and accepted (plan §Gap A0 / audit): this audio has firmware
processing baked in (~24x gain, HPF, pre-emphasis). It is fine for
proving the pipeline and for engine robustness comparison; it is NOT
evidence for the raw-vs-processed decision (that waits for the A1 path).

P2 evolution: `fetch.py` gains `voicefetch` burst mode (framed chunks,
CRC16 per chunk, whole-utterance refetch on any CRC failure); the A0 path
stays as the fallback and the firmware-compat probe.

Current native operation uses owner-scoped `micrecord` plus `voicefetch`; the
finalized WAV remains authoritative. Separately, the command-only/default-off
recorder-shadow diagnostic binds exact `{exchange, controller, UART login
epoch}` authority. Firmware tees each post-DSP/VAD chunk through a strict
16 KiB PSRAM SPSC while preserving the WAV outcome. `audio/live.py` validates
identity, continuity, bounds, CRC, and terminal state on the reader thread.
`tools/live_pcm_shadow_probe.py` runs an untrimmed exact-owned capture, waits
for live quiescence, fetches the canonical WAV, compares bytes/terminal, and
cleans up. It is not a production audio source and has not yet run physically.

## 5. Engines

### STT (`stt/`)

`STTEngine` protocol: `transcribe(pcm: bytes, rate: int) -> str` (P0,
utterance-batch) — plus `stream_begin/feed/finish` reserved on the
protocol for P4 streaming (Moonshine v2 and Zipformer both support
incremental feed; whisper.cpp does not — it simply won't implement the
streaming half).

- `moonshine.py`: primary. pip `moonshine-voice`, ONNX Runtime; session
  options cap intra-op threads from config (2 while LLM busy, else 4).
  English models are MIT (non-English are non-commercial-licensed — out of
  scope).
- `zipformer.py`: sherpa-onnx streaming Zipformer en int8 — fallback and
  the natural pick if the service ever goes C++.
- Selection by config `stt.engine`; import-guarded so a missing package
  fails at startup with a clear message, not at first utterance.

### LLM (`llm/`)

- `server.py` — LlamaServerSupervisor: spawns the configured llama-server
  binary (`-t 4 -c 2048 --cache-reuse 256`, single slot, model from
  config), polls `/health` until ready (or fails startup), restarts with
  backoff on exit, kills the child on shutdown. Logs the server's stderr
  through our logger at DEBUG. Startup asserts the prompt-cache actually
  hits (issue-#12701/#15082 history): after the first two exchanges it
  checks the server's reported prompt-eval token counts and logs a WARNING
  if turn 2 re-prefilled the full prefix.
- `client.py` — `ask(text) -> AsyncIterator[str]`: streaming
  `/v1/chat/completions` via httpx; maintains history (config-capped turn
  count, trimmed oldest-first; the static system prompt is always message
  0 so prefix caching holds). The default prompt is a truthful capability
  contract: transcript/text input and display text output, offline built-in
  knowledge plus recent context, no visual/raw-audio input, live data,
  device state, persistent memory, or action tools. Answer length is bounded
  separately by `max_tokens` (config, default ~120 — voice answers, ~1-3
  sentences per plan D6).
- Model is a config path — the P0 bench (plan §8) decides between
  Qwen3-1.7B, LFM2.5-1.2B, and the Qwen2.5-1.5B baseline; nothing in the
  code cares which GGUF it is.

## 6. Pipeline and delivery

`pipeline.py` — the one module that sees everything. One exchange:

```
trigger ──► fetch audio ──► STT ──► LLM (stream) ──► deliver ──► done
   │            │             │          │               │
  jobs.py   audio/fetch    stt/*      llm/client      deliver.py
```

Recorder shadow is deliberately outside this graph. The daemon does not arm
or renew it, and no live frame enters STT/LLM/delivery; production still feeds
the batch engine only after canonical WAV finalization.

- P0 triggers (`jobs.py` `ManualTrigger`): CLI `ask` (one-shot voice
  exchange), CLI `chat "<text>"` (skip audio — text prompt straight to the
  LLM; proves the LLM+delivery half without the mic), and a `daemon` mode
  that waits on a FIFO/socket for ask/chat requests so you can drive it
  while it runs as a service. P1 swaps in the `HostJobsPoller` (2-5Hz
  `hostjobs json since=<seq>`) behind the same `JobSource` interface.
- Serialization: one exchange at a time end-to-end in P0 (matches the
  firmware chat layer's own one-generation rule). A queue absorbs a
  second trigger arriving mid-exchange.
- `deliver.py`: chunk the final text to fit `oledtext`/`g2notify` command
  lines (≤1800B payload per line for margin under 2047), targets from
  config (`oled`, `g2`, both). Handles the audit-found preconditions:
  "OLED display not running" → one `oledstart` attempt if config allows,
  then retry once; "G2 not connected" → log and drop that target.
  P3 replaces this with `llmpush <session> <seq> <chunk>` streaming (live
  typing on the XIAO surfaces); `deliver.py` keeps the fallback path.

Reboot/absence handling: ROM-burst garbage on the RX queue → transport
flags a probable reboot → pipeline aborts any in-flight exchange cleanly,
session quiesces (OTA probation rule), re-login, resume polling.

## 7. Testing strategy (dev-Mac first, Pi second)

- `tests/fake_firmware.py` — a firmware double bound to a pty pair,
  speaking the REAL drain protocol as implemented in System_UartLink.cpp:
  login gate with `OK: logged in as`, auth-required nag, one-command-
  at-a-time, `openmic`/`micrecord`/`fileread ... b64` (serves a generated
  sine-wave WAV in the real chunk-envelope shape), `oledtext`, and fault
  injection: ROM-boot garbage bursts, mid-reply reboots, lockout replies,
  stray broadcast-looking lines before a reply.
- Unit/integration tests as listed in §2 — all runnable with
  `pytest` on any POSIX dev machine, no hardware, no models (fake
  engines), no network.
- `test_soak.py` (slow marker) — the audit's required proof: fake firmware
  floods ~92KB/s through the pty while the event loop is deliberately
  stalled 500ms at a time; assert zero byte loss in the reader path.
  (A pty is not a UART — the kernel-buffer numbers differ — but the test
  pins the PROPERTY: the reader thread keeps draining while the loop
  stalls. The wire-level version reruns on the Pi with a real stall.)
- `test_systemd_watchdog.py` uses a local datagram socket pair to verify the
  immediate/periodic keep-alive, environment/PID validation, child-environment
  consumption, non-systemd no-op behavior, startup ordering, shutdown cleanup,
  and the matching user-unit policy without requiring a running systemd.
- On-Pi validation (P0 exit criteria): login + `uartlink status` probe;
  one full `ask` exchange against the bench XIAO; the §8-plan benches
  (STT RTF solo/contended, llama-bench ladder, prompt-cache hit check,
  stall soak on the real tty); answer visible via `oledtext` reply — all
  before any firmware work starts.
- Phase 2B fake-firmware coverage includes PDM/G2 source tags, exact untrimmed
  live/WAV parity, source mismatch, corrupt terminal CRC, missing-frame
  quiescence, artifacts, and exact cleanup. The full host suite passes 291
  tests with 1 skipped and 7 subtests. The final XIAO app is `0x4fbab0`
  (5,225,136 bytes), leaving `0x39550` (234,832 bytes, 4.30%), SHA-256
  `f897087cc12110c793ef45a023e40e6fadba59c9b9c1b6abd18708485b6f2ff6`.
  These are simulation/build results; physical recorder-shadow remains unrun.

## 8. Phase evolution map (what changes, per firmware phase)

| Phase | Firmware adds | This program changes |
|---|---|---|
| P0 | — | everything above; temporary admin account |
| P1 | `hostjobs` + voice-job FSM | `jobs.py`: HostJobsPoller replaces ManualTrigger; drop to 'user'-tier account if D2 resolved |
| P2 | frame writer, `voicefetch`, default-off recorder shadow | COBS/live demux, bounded inbox, burst fetch, synthetic and exact-owned standalone probes are built; production pipeline remains batch |
| P3 | remote LLM backend + `llmpush` | `deliver.py`: llmpush streaming (2-5Hz chunks); pipeline reports session/seq; multi-turn context stays here |
| P4 | production live-audio enable + push lines | connect the already-built recorder transport to one streaming STT worker; `jobs.py`: push-line doorbell replaces polling |
| P5 | wake word on XIAO | no change here — triggers just arrive without a button |

The interfaces that make this additive: `JobSource` (manual → poll →
push), `STTEngine` (batch → streaming), fetch (files → burst → stream),
deliver (commands → llmpush). Those four seams are the architecture.

## 9. Config (config.example.yaml shape)

```yaml
link:
  port: /dev/ttyAMA2        # bench Pi 5; carrier CM5 identical
  baud: 921600              # 2000000 after the D3 bench test passes
  credentials_file: ~/.config/hw1-ai-service/credentials  # "user password", mode 600
audio:
  record_seconds: 4.0       # A0 fixed window (no endpointing until A1/A2)
stt:
  engine: moonshine         # moonshine | zipformer | fake
  model: en                 # language code, or exact downloaded model directory
  threads_idle: 4
  threads_contended: 2
llm:
  server_bin: /opt/llama.cpp/build/bin/llama-server
  model: /opt/models/<bench-winner>.gguf
  port: 8080
  max_tokens: 120
  system_prompt: >-
    You are HardwareOne, a local offline assistant for these smart glasses,
    replacing the cloud-backed Even AI response path. Do not claim to be Even AI
    or an official Even Realities service. You receive text, usually a possibly
    imperfect speech transcript, and return plain text for a small display. You
    can answer questions, explain, brainstorm, help with writing, summarize,
    translate, and
    reason from built-in knowledge and recent conversation; that knowledge may
    be outdated. You have no tools or persistent memory. You receive no camera,
    image, or raw-audio input and cannot access the internet, live data, current
    time or location, files, accounts, device or sensor state, or perform actions
    beyond replying. Use information supplied in conversation without claiming
    you observed or retrieved it. Mention limitations only when relevant. Answer
    directly and naturally, normally in one to three concise sentences; add
    detail when requested or needed for correctness. Avoid Markdown, headings,
    tables, filler, and long lists. Correct only obvious transcript errors; if
    ambiguity would change the answer, ask one brief question. Never invent
    current facts, observations, memories, capabilities, or completed actions.
  history_turns: 8
deliver:
  targets: [oled]           # oled | g2
  allow_oledstart: true
  g2_ask_render_cps: 44.0   # provisional ASK barrier; 0 disables
  g2_stream_speed: 40       # daemon-start field-only CONFIG; 0 preserves state
service:
  poll_hz: 0                # 0 in P0 (no hostjobs yet); 2-5 from P1
power:
  enabled: false            # true after installing root helper + sudoers
  initial_profile: auto     # eco | balanced | performance | auto
  auto_active_profile: performance
  auto_idle_profile: eco
  allow_suspend: false      # experimental; kernel capability checked too
  min_sleep_minutes: 1
  max_sleep_minutes: 1440
```

There is intentionally no recorder-shadow YAML key. It is enabled only through
explicit authenticated `liveaudio ready` + `liveaudio shadow` diagnostic
commands, so daemon startup cannot silently change production capture behavior.

## 10. Getting started (the actual steps, in order)

On the XIAO (bench, once):
1. Keep `uartlink on` + `uartRequireAuth=1` as already validated.
2. Create the temporary P0 admin account (fileread needs it; demoted to a
   'user'-tier account at P2 per plan D2): `useradd cm5svc <pass> 0 admin`.

On the Pi 5:
3. Enable uart2 on GPIO4/5: add `dtoverlay=uart2-pi5` to
   /boot/firmware/config.txt and reboot — this creates /dev/ttyAMA2.
   (The overlay name is Pi-5-specific; plain `uart2` targets the Pi 4 and
   silently does nothing. No raspi-config serial toggling needed — uart2
   never carries the Linux console or Bluetooth.)
4. Install: python3.11+, `pipx` or venv; `pip install -e .` from
   `ai-service/`; `pip install moonshine-voice` (or sherpa-onnx).
5. Build/install llama.cpp (`llama-server`) once; download the three §5
   candidate GGUFs.
6. `hw1-ai-service probe` — opens the port, logs in, runs `uartlink
   status`, reports round-trip health. This is the first-light command.
7. `hw1-ai-service chat "hello"` — LLM + delivery half (no mic).
8. `hw1-ai-service ask` — the full voice exchange.
9. Run the bench suite (plan §8) and record numbers in the plan.
10. `systemd/hw1-ai-service.service` → `systemctl --user enable --now`
    once 6-8 are green.

Dev-Mac loop (no Pi needed): `pytest` in `ai-service/` — fake firmware,
fake engines, full pipeline.
