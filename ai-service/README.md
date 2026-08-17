# hw1-ai-service

CM5-side AI companion for hardwareone: speaks the UART link to the XIAO,
runs speech-to-text and LLM generation on the CM5 (both resident in one
program), and returns answers to the device's display surfaces.

- System plan and phasing: [../CM5_AI_SERVICE_PLAN.md](../CM5_AI_SERVICE_PLAN.md)
- Program architecture: [../ARCHITECTURE.md](../ARCHITECTURE.md)
- Adversarial audit record: [../CM5_AI_SERVICE_AUDIT.md](../CM5_AI_SERVICE_AUDIT.md)
- Canonical CM5 paths and sync commands: [../CM5_DEPLOYMENT_PATHS.md](../CM5_DEPLOYMENT_PATHS.md)

Current state: the service supports manual voice/chat plus the firmware-owned
native "Hey Even" path. Native work is correlated by a firmware-issued
exchange ID from capture through recorder cleanup and lens delivery; wearer
dismissal cancels later host stages and the firmware ID fence prevents a
zombie card even if the advisory cancel EVT is lost. Audio still arrives as a
finalized WAV and Moonshine runs batch inference. `live-pcm-v1` now supports
both deterministic synthetic PCM and an explicitly armed exact-owner recorder
shadow (`synthetic=1 recorder_shadow=1 shadow_default=off`). The shadow is
source/build/host-simulation complete and absent from daemon/YAML defaults. A
separate `native-stt` diagnostic can now feed it to an isolated Moonshine
worker, but production still does not. Production streaming STT and mutable
partial questions remain future work. The physical recorder-shadow
probe has passed exact PDM parity. The post-fix G2 rerun deliberately filled
the 2.048-second AFE backlog and passed exact 100,000-sample live/WAV parity at
CRC32 `56ebd586`, with END reason 0 and zero drops/overflow/integrity faults.
The physical host-overflow, host-gap, host-abort, and lease-expire matrix also
passed while preserving a canonical owner WAV in every case. The native no-STT
admission/correlation smoke then passed for one real `Hey Even` capture with
exact ID/epoch/event/path correlation, valid zero-drop LIVE END, an independent
canonical trimmed WAV, and exact cleanup. It started no STT, LLM, ASK, or
REPLY. Production streaming STT remains disabled.

The daemon also publishes an authenticated `cm5-presence-v1` heartbeat over
the existing serialized Session. It sends `starting` during model load or
reboot probation, `ready` only when a new native wake can be consumed, `busy`
while an owned job runs, and `degraded` when the AI plane is unavailable.
Steady state renews every five seconds; firmware fixes the normal lease at 15
seconds and the busy lease at 75 seconds. This is separate from the systemd
watchdog: it gives the XIAO recent UART/application readiness, while systemd
still owns process-loop recovery. Older firmware is tolerated and reprobed
slowly, so a daemon-first rolling upgrade does not require a service restart.
If the device reboots while no job is active, ROM garbage or an unexpected
authentication reset immediately fences the daemon to `starting`; a
generation wakeup restarts the supervised link group, and `ready` is not sent
in the new login epoch until reboot probation and live re-arming complete.

The model-facing interface is deliberately narrower than the surrounding
firmware: it receives transcript/text messages and returns display text. The
default static system prompt identifies HardwareOne as a local offline
assistant and states that this build has no visual/raw-audio input, internet or
live data, persistent memory, device context, or action tools. The active
`llm.system_prompt` in `~/.config/hw1-ai-service/config.yaml` overrides that
code default and is read only at service startup.

## Dev-machine loop (no Pi, no hardware, no models)

```bash
cd ai-service
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest                    # fake firmware over a pty, fake engines
pytest -m slow            # + the reader-stall soak
```

## Pi 5 / CM5 install

The following is a **fresh-install-only** bootstrap. It deliberately refuses
to overwrite an existing credential or tuned configuration file.

```bash
set -e
# 1. Serial: create /dev/ttyAMA2 on GPIO4/5 (Pi 5 family overlay — the name
#    is uart2-pi5; plain 'uart2' is the Pi 4 overlay and does nothing here):
#      echo 'dtoverlay=uart2-pi5' | sudo tee -a /boot/firmware/config.txt
#      sudo reboot
#    The carrier CM5 image ships the same overlay. No raspi-config needed —
#    uart2 never carries the Linux console or Bluetooth.

# 2. This package (the deploy script syncs into the SSH user's home):
cd ~/hw1-ai-service
python3 -m venv ~/hw1ai && . ~/hw1ai/bin/activate
pip install -e '.[moonshine]'         # or .[zipformer]

# 3. Credentials (created on the XIAO with: useradd cm5svc <pass> 0 admin —
#    temporary admin for P0's fileread; demoted to user tier at P2):
install -d -m 0700 ~/.config/hw1-ai-service
test ! -e ~/.config/hw1-ai-service/credentials
printf 'cm5svc YOURPASS\n' > ~/.config/hw1-ai-service/credentials
chmod 600 ~/.config/hw1-ai-service/credentials

# 4. Config:
test ! -e ~/.config/hw1-ai-service/config.yaml
install -m 0600 config.example.yaml ~/.config/hw1-ai-service/config.yaml
$EDITOR ~/.config/hw1-ai-service/config.yaml

# 5. llama.cpp (once):
#    build llama-server, download the known-good Qwen3-1.7B Q4_0 GGUF, and
#    point llm.model at it. The optional current 2B/3B-class follow-up ladder
#    below does not change this live baseline.
```

## First light, in order

```bash
hw1-ai-service -c ~/.config/hw1-ai-service/config.yaml probe   # link + login + status
hw1-ai-service -c ... chat "hello"                             # LLM + display, no mic
hw1-ai-service -c ... ask                                      # the full voice exchange
```

Then the original P0 validation suite (plan §8): STT RTF solo and while
llama.cpp decodes, prompt-cache hit verification, and the stall soak on the
real tty. The model ladder below is the newer, reproducible follow-up screen.

## Reproducible CM5 LLM throughput ladder (up to 3B-class)

`tools/benchmark_llm_models.sh` compares the active configured GGUF against a
pinned Q4_0 ladder without changing `config.yaml` or replacing the live model.
The compact ladder is the active model, Qwen3.5-2B Q4_0, and Granite 4.1 3B
Q4_0. Granite's GGUF reports about 3.4 billion parameters despite its `3B`
marketing name, so it is the upper 3B-class bound rather than a strict ≤3.0B
model. Exact revisions, byte counts, and hashes for the
[Qwen3.5 quant](https://huggingface.co/bartowski/Qwen_Qwen3.5-2B-GGUF) and
[official Granite GGUF](https://huggingface.co/ibm-granite/granite-4.1-3b-GGUF)
are kept in `tools/llm_benchmark_models.tsv`. The script uses the historical
comparison command exactly:

```bash
llama-bench -m MODEL.gguf -p 128 -n 64 -t 4
```

Run it while logged into the CM5 service account:

```bash
cd ~/hw1-ai-service
./tools/benchmark_llm_models.sh
```

The runner downloads about 3.3 GB of pinned, SHA-256-verified candidates into
`~/models/hw1-llm-bench`, derives `llama-bench` from the active YAML's
`llm.server_bin`, measures the active baseline both before and after the
candidate sweep, and writes raw logs plus `summary.md`/`summary.csv` beneath
`~/llm-bench-results`. Downloads resume through a `.part` file. An existing
file with the wrong size or checksum is never overwritten.

If a retained `.part` reaches the pinned byte count with the wrong checksum,
move it aside or delete that one named `.part` before retrying; the script will
not destroy it automatically.

For a fair comparison, the script stops the user service, rejects competing
`llama-server` processes, requires all CPU policies to be `performance`, and
rejects sticky power/throttle flags, a net swap-occupancy increase at any run
boundary, an ARM clock below 2.3 GHz, or a temperature above 80 C. Its exit
trap restores the prior concrete power profile and restarts the
service only when it was active before the run. If the root-owned power helper
has not been installed and the CM5 is not already in `performance`, install it
once before the benchmark:

```bash
cd ~/hw1-ai-service
sudo ./systemd/install-power-helper.sh "$USER"
```

Use a dedicated, correctly rated CM5 supply. The historical Pi 5 sweep hard
reset under a shared-dock undervoltage event; no cleanup trap can run after a
physical power loss, and any nonzero `get_throttled` value invalidates the run.

Download and benchmark can be split across sessions:

```bash
./tools/benchmark_llm_models.sh --download-only
./tools/benchmark_llm_models.sh --benchmark-only
```

The result is a throughput screen, not a final model decision. `llama-bench`
uses generated benchmark tokens and excludes production tokenization,
sampling, chat-template, server, and prompt-cache behavior. Its `pp128` and
`tg64` rows are independent tests: `tg64` starts at effectively empty context;
it is not 64 generated tokens after the 128-token prompt. A faster percentage
therefore does not mean a better assistant or guarantee the same multi-turn
latency. The reported max RSS is for standalone `llama-bench`; the stopped
daemon means it does not include resident Moonshine. Close candidates still
need the same production system prompt, output cap, a small fixed quality
prompt set, and a production-depth TTFT/cache test. Qwen3.5 must specifically
pass an explicit multi-turn llama-server cache-reuse test before any switch
because its hybrid architecture may cache differently from the current plain
Qwen3 model. `tools/benchmark_llm_serve.sh`, below, is that test.

## Production-depth llama-server latency sweep

`tools/benchmark_llm_serve.sh` is the follow-up screen the throughput ladder
above explicitly defers to. It answers "how fast does a turn feel" rather than
"how fast does this GGUF decode", and it exists because `llama-bench` cannot
answer two questions that now matter:

* **Speculative decoding is invisible to it.** `llama-bench` drives
  `llama_decode` directly and never starts a server, so a multi-token-prediction
  arm and a plain arm produce identical numbers. llama.cpp merged MTP support in
  [ggml-org/llama.cpp#22673](https://github.com/ggml-org/llama.cpp/pull/22673)
  (2026-05-16); a Qwen3.5 MTP GGUF drafts its own next tokens with no second
  model and no extra RAM, and only a server run can measure it.
* **Its 128-token synthetic prompt is not the production prompt.** Real TTFT is
  the system prompt plus grown history behind `cache_prompt` and
  `--cache-reuse 256`.

So this script drives the real path: the same `LlamaServerSupervisor` flags
(`-t 4 -c 2048 --cache-reuse 256 --parallel 1`) and the same `LlmClient`
request shape (streaming `/v1/chat/completions`, `cache_prompt`,
`enable_thinking=False`) that the daemon uses, with the config's own system
prompt, `max_tokens`, and `history_turns`. Timings are measured client-side
exactly the way `LlmClient` measures them, so they are directly comparable to
the daemon's own `llm: ... ttft=...` log lines.

`llama-server` is a cmake target, not an installed binary. Building only the
`llama-bench` target leaves it missing, and the failure then reads as a `PATH`
problem rather than a build one, so build it first:

```bash
cmake --build /opt/llama.cpp/build --target llama-server -j4
```

Then, on the CM5 service account:

```bash
cd ~/hw1-ai-service
./tools/benchmark_llm_serve.sh
```

Candidates are pinned in `tools/llm_serve_models.tsv` with revisions, byte
counts, and SHA-256 values, and are fetched by delegating to
`benchmark_llm_models.sh --download-only` so there is exactly one audited
download path. They share `~/models/hw1-llm-bench`, so anything either tool has
already fetched is reused rather than downloaded twice. The default rows are
[Qwen3.5-2B MTP](https://huggingface.co/unsloth/Qwen3.5-2B-MTP-GGUF),
[LFM2-8B-A1B](https://huggingface.co/LiquidAI/LFM2-8B-A1B-GGUF) (8.3B total,
1.5B active — an MoE reads only its routed experts, so file size stops
predicting decode rate), and
[Granite 4.0 H-Tiny](https://huggingface.co/ibm-granite/granite-4.0-h-tiny-GGUF)
(7B total, 1B active, hybrid Mamba, small KV cache). Two further rows are
commented out in the manifest with the reasons to enable them.

A manifest id containing `-mtp-` is measured twice, `:plain` and `:mtp`, and
`summary.md` reports the ratio between them. The MTP arms are skipped with a
warning if the built `llama-server` has no `--spec-type` flag.

Results land under `~/llm-serve-results`: `summary.md` (TTFT split into turn 1
versus later turns, median decode rate, median turn duration, peak RSS, and
whether that RSS plus resident Moonshine still fits 8 GB), `summary`-adjacent
`answers.md` with every arm's actual answers, and per-arm JSON under `arms/`.

Read `answers.md`. A reasoning-tuned model can post an excellent decode rate
while spending the whole `max_tokens` budget on chain-of-thought before emitting
anything the lenses can display; `summary.md` flags any arm that returned an
empty answer, and that is a rejection, not a footnote.

The same safety envelope as the throughput ladder applies: it stops the user
service, rejects competing `llama-server` processes, requires `performance` on
every CPU policy, and rejects sticky throttle flags, swap growth, a sub-2.3 GHz
ARM clock, or over-80 C at any arm boundary. Its exit trap restores the prior
power profile and restarts the service only if it was active before the run.
Download and sweep split the same way:

```bash
./tools/benchmark_llm_serve.sh --download-only
./tools/benchmark_llm_serve.sh --serve-only
```

Sampling is production's, not greedy, so answers differ between runs and only
the rates are comparable. The sweep runs with the daemon stopped, so peak RSS
excludes resident Moonshine; the fits-8 GB column adds an estimate back, but
real co-residency is still its own test.

## Overclock stepping and stability (`tools/oc_step.sh`)

Walks the CM5 up an overclock ladder one rung at a time, with a verdict per
rung instead of a vibe.

```bash
./tools/oc_step.sh status              # configured vs measured vs kernel ceiling
sudo ./systemd/install-oc-helper.sh "$USER"  # one-time finite privilege boundary
sudo -n /usr/local/libexec/hw1-oc-helper normalize-stock
sudo -n /usr/local/libexec/hw1-oc-helper stage 2600 0
sudo -n /usr/local/libexec/hw1-oc-helper reboot-try
./tools/oc_step.sh soak --minutes 15 --expected-mhz 2600 --expected-tryboot 1
./tools/oc_step.sh ladder              # every rung tried and its verdict
```

**Set expectations first.** On Pi 5 / CM5 the SDRAM clock is not configurable,
so memory-bandwidth-bound decode may gain much less than compute-heavy prefill.
There is not yet a valid stock-versus-overclock A/B on this host. Section 2 of
[../CM5_PI5_PERFORMANCE_RECORD.md](../CM5_PI5_PERFORMANCE_RECORD.md) compares an
under-volted run with a power-clean run; its `pp128`/`tg64` split must not be
attributed to clock-only overclocking. Measure the same model's absolute prefill,
decode, and end-to-end metrics at stock and at every accepted rung.

The root-owned `hw1-oc-helper` keeps normal `/boot/firmware/config.txt` at
recorded stock and builds each complete candidate as `tryboot.txt`. Raspberry
Pi's one-shot `reboot '0 tryboot'` applies that file for exactly the next boot;
the flag is consumed before Linux starts, so a later reset or power-cycle falls
back to normal stock. The helper refuses unfamiliar boot files, conflicting
clock settings, changed stock hashes, paths supplied by a caller, and values
outside its finite allowlist. Never run the user-writable `oc_step.sh` with
sudo.

The helper uses the CM5-only config filter and, only when explicitly requested,
`over_voltage_delta` (microvolts added to the DVFS-computed voltage curve)
rather than legacy `over_voltage`, which disables firmware automatic voltage
selection. Start each rung with no manual voltage; after clean power is proven,
use 10000 µV increments only if compute correctness fails. The tool refuses
frequencies outside its 2400–3000 MHz ladder and deltas above 50000 µV. `status` warns if
`force_turbo=1` is present because a sustained all-core workload does not need
the added idle power and heat.

`soak` runs two phases with the daemon stopped and telemetry sampling
throughout:

1. `stress-ng --cpu 4 --cpu-method all --verify` for the requested
   duration. `--verify` is what makes this a correctness test rather than a
   heater. Install it with `sudo apt install -y stress-ng`; a missing binary
   fails closed before the soak starts.
2. N identical greedy completions (`temperature: 0`, `top_k: 1`, fixed seed,
   `cache_prompt: false`) against a private `llama-server` on port 8099, hashed
   and compared. CPU inference is deterministic under a fixed thread count, so
   a differing hash means the NEON/i8mm GEMM kernels are computing wrong
   numbers. That is the failure mode that never crashes and quietly corrupts
   every benchmark you run afterwards — and it is exactly the code path
   `stress-ng` does not exercise.

The verdict is `PASS` or `FAIL`, appended to `~/oc-results/ladder.tsv`. Missing
telemetry, any live or sticky throttle bit, sampled EXT5V below the CM5's 4.75 V
floor, temperature over 80 °C, a missing/failed `stress-ng` phase,
nondeterministic output, or failure to sustain the requested clock all fail the
rung and return a nonzero exit. EXT5V is sampled at 1 Hz, so a clean trace still
cannot rule out faster transients; `get_throttled=0x0` before and after remains
mandatory. The soak refuses to start when sticky bits are already set or when
the kernel ceiling shows that firmware did not accept the requested clock.

For a hard hang, power-cycle the CM5. Because a trial boot is one-shot, firmware
returns to the untouched normal config. The CM5 Lite's removable SD card remains
the last-resort recovery path: mount its boot partition elsewhere and restore
the helper's root-owned stock backup.

`benchmark_llm_serve.sh` re-bases its per-arm ARM-clock floor on this host's
`cpuinfo_max_freq` rather than a fixed 2.3 GHz, so an overclocked box that
throttles back to stock is caught instead of silently passing.

## Synthetic live-PCM transport probe

The standalone synthetic diagnostic installs a
direct reader-thread sink before opening the UART, acquires and renews a
3-second controller lease, requests an asynchronous deterministic 16 kHz mono
S16LE stream, and verifies identity, offsets, terminal totals, IEEE CRC32, and
every generated byte. The inbox is bounded to 16 KiB and 32 PCM frames. This
probe does **not** start a microphone, fetch a WAV, run STT, create a G2 card, or
change daemon behavior. It strictly parses the ready grant and uses the same
negotiated renewal policy as the daemon: one second only with a valid
`renew_direct=1` contract, otherwise the two-second legacy cadence. Renewal
deadlines are measured from send time rather than after each reply completes.

Only one process may own the serial port. With matching firmware already
flashed, stop the service and run:

```bash
systemctl --user stop hw1-ai-service.service
sleep 2
fuser -v /dev/ttyAMA2

~/hw1ai/bin/python \
  ~/hw1-ai-service/tools/live_pcm_transport_probe.py \
  -c ~/.config/hw1-ai-service/config.yaml \
  --duration-ms 10000

systemctl --user start hw1-ai-service.service
systemctl --user is-active hw1-ai-service.service
```

The probe exits zero only when its JSON result has `"ok":true`. Firmware must
advertise `live-pcm-v1` and `synthetic=1`, and both
configured/effective link rates must be at least 921600 baud. A successful run
validates the synthetic UART path only; it is not evidence about either mic,
recorder timing, SD/WAV parity, Moonshine latency, or lens behavior. See the
canonical evidence-capture form in
[../CM5_DEPLOYMENT_PATHS.md](../CM5_DEPLOYMENT_PATHS.md#synthetic-live-pcm-transport-probe).

## Recorder-shadow PCM/WAV probe

`tools/live_pcm_shadow_probe.py` is a separate standalone diagnostic for the
default-off recorder tee. In `owned` mode it preflights the exact PDM or G2
source and canonical 16 kHz mono S16LE format, acquires/renews the controller
lease, arms one exact owned exchange, records untrimmed PCM, waits for the live
worker to quiesce, fetches the canonical WAV, compares live/WAV bytes, CRC and
terminal fields, then deletes only that exchange's file. It never starts STT,
the LLM, or G2 lens delivery. Owned and native modes share the same strict,
deadline-scheduled ready-grant renewal policy as the synthetic probe.

`owned --fault host-overflow|host-gap|host-abort|lease-expire` turns an
expected live-path failure into a passing diagnostic only when the failure is
exactly the requested one and the independently fetched owner-scoped WAV is
still canonical. Host faults also require exact current-exchange END metadata;
device ABORTs require their admitted prefix count/CRC to match the WAV prefix.
The default is `--fault none`, which preserves the exact parity gate.

`native --wake-timeout 30 [--capture-timeout N]` is the next standalone
hardware gate. It arms the native one-shot, waits for a real `Hey Even` wake,
and correlates that firmware exchange ID across LIVE BEGIN, the current UART
login epoch, `mic_autostop`, the exact recorder path, LIVE END, canonical WAV
fetch/delete, and exact `g2evenai exitid` cleanup. It never starts STT, the LLM,
ASK, or REPLY. The optional capture timeout defaults to
`audio.vad_max_seconds`; reaching it is a failed missing-autostop gate followed
by bounded cleanup, not a successful host-forced stop. Native VAD trimming
means live/WAV byte equality is deliberately not asserted: successful JSON
uses mode `native_recorder_shadow_smoke` and parity reason
`native_capture_trim_enabled`.

With the daemon stopped and the intended source already selected. For G2, wear
and tap the glasses awake and keep an active lens container open (for example
`g2show "G2 MIC SHADOW TEST - KEEP THIS PAGE OPEN"`) before capture; accepting
the stream-enable command without such a page does not prove LC3 frames are
flowing.

```bash
~/hw1ai/bin/python \
  ~/hw1-ai-service/tools/live_pcm_shadow_probe.py \
  -c ~/.config/hw1-ai-service/config.yaml \
  owned --expected-source g2 --record-seconds 6 \
  --output-dir ~/g2-prefx/live-pcm-shadow-$(date +%Y%m%d-%H%M%S)
```

Use `--expected-source pdm` for the onboard microphone. The probe requires
`recorder_shadow=1 shadow_default=off`; exact authorization is
`{exchange, controller, UART login epoch}`, although the epoch is an internal
admission/TX fence rather than a frame field. For G2, capture `g2micstats`
before and after because live/WAV equality cannot expose LC3 decode failures or
AFE-mutex drops that happened upstream. Collect the final stats in cleanup even
when the probe fails. `g2micreset` does not empty the decoded ring or reset AFE
overrun; a fresh AFE-feed start does.

The pre-native software baseline was a full host run of 300 passed, 1 skipped,
7 subtests and a 58-test focused live/shadow/transport run. Post-native
validation is clean Python `compileall`; 31 native shadow tests under
`-W error`; and an independent 94-test EvenAI/cancel/fetch/shadow review under
`-W error`. The paced collector/checker slice passes 31 focused tests under
`-W error`, and the complete current CM5 service suite passes 348 with 1
skipped and 7 subtests under `-W error`. The final
XIAO app remains `0x4fbcb0` (5,225,648 bytes) with `0x39350` (234,320 bytes,
4%) free, SHA-256
`c306bb476f487df192632b388d193f33045f94b000f74c1a09d1507371f13341`.
Physical PDM shadow parity passed at 112,640 samples / CRC32 `2e53eb16`. The
corrected pre-fix G2 run ABORTed reason 6 at queue high-water 4 / overflow 1
after a full 32,768-sample AFE backlog burst, while its canonical 137,568-sample
WAV remained intact (CRC32 `1fed8e52`) and UART fault/late-frame counts stayed
zero. The post-fix G2 rerun then began with a deliberately full 32,768-sample
ring and passed at 100,000 samples / CRC32 `56ebd586`: all parity booleans were
true, device queue high-water was 2/4, END reason was 0,
dropped/overflow/fault/late counts were zero, and final
`mutex_drop=0 decode_fail=0`. The earlier synthetic UART probe also passed
2,048 ms and 10 s pattern/CRC/lease-renewal runs; that is transport evidence,
not microphone/WAV evidence. The physical four-fault matrix then passed for
host overflow, host gap, exact host ABORT, and lease expiry: each requested
failure was observed, its canonical owner WAV survived, and no control or lease
error occurred. The native hardware mode then passed for controller
`05dae575e2e7a154`, firmware exchange `6bda87ea00000002`, and UART epoch 19:
LIVE END was valid reason 0 at 46,400 samples / CRC32 `931acca0`, with zero
drops/inbox faults/late frames; the independent canonical trimmed WAV was
35,200 samples / CRC32 `82c81ade`; parity reason was
`native_capture_trim_enabled`; exact cleanup returned live/native state idle;
and final `mutex_drop=0 decode_fail=0`. This is a single no-STT provenance
smoke, not latency or production-streaming evidence. See the canonical
capture, acceptance, and regression steps in
[../CM5_DEPLOYMENT_PATHS.md](../CM5_DEPLOYMENT_PATHS.md#native-hey-even-no-stt-recorder-shadow-smoke).

## Paced Moonshine replay diagnostic

`tools/moonshine_stream_replay.py` is a standalone saved-WAV measurement tool,
not the production pipeline. It paces 4096-byte/128 ms PCM chunks through a
bounded eight-chunk / 32 KiB / 1.024-second FIFO owned by one Moonshine worker,
freezes streaming
metrics before the default batch baseline, and emits model identity, event,
accuracy, queue, latency, resource, and case-summary JSONL records.

`tools/moonshine_stream_replay_check.py` grades one cadence against a trusted
manifest. The first guarded run uses
`tools/moonshine_gate0a_medium_slice.json`: hash-pinned positive pairs 001, 002,
and 005 plus human-audited static/no-speech controls neg001 through neg004, the
exact corpus directory and deployed medium-streaming model/enum,
Moonshine 0.1.1 runtime, pinned policy and absolute error ceilings, 0.5-second
update floor, default pace 1.0/queue eight/batch enabled, and before/after
throttle evidence. The checker independently
recomputes positive WER, requires empty streaming and batch finals for every
negative control, and verifies record/PCM topology, partial timing coverage,
queue/resource policy, throttle state, and evidence hashes. Run it on
the Pi before pulling the JSONL because it re-hashes the absolute WAV and
sidecar paths recorded there.

A clean report is deliberately only scope
`provisional_deployed_medium_mixed_slice`; it always sets
`full_gate0a_complete=false` and warns that the manifest is provisional. Three
positive cases plus four static/no-speech controls still have no model-selection
power and cannot close Gate 0A. The collector/checker slice passes 31 focused
tests under `-W error`, and the current full CM5 suite passes 348 with 1 skipped
and 7 subtests. A
physical replay under the superseded four-chunk v1 contract found that 0.5
seconds completed with only about one chunk of timing margin, while 1.0
seconds overflowed during a 710.3 ms native pass. The new v2 contract makes the
Pi worker queue eight chunks and raises its queue-age ceiling to 1024 ms; its
physical 0.5- and 1.0-second reruns preserved every input chunk. Contract v3
adds the four confirmed no-speech controls without changing the XIAO or
UART-inbox queues. Use the guarded command and acceptance fields in
[../CM5_DEPLOYMENT_PATHS.md](../CM5_DEPLOYMENT_PATHS.md#real-time-paced-moonshine-replay).

The physical v3 mixed-slice evidence is retained at
`.scratch/gate0a-v3-results-Pn5hEAy0`. At 0.5 seconds, streaming and batch each
made 2/26 positive word errors, max END-to-final was 0.459 s, and the Pi FIFO
reached 4/8 chunks / 510 ms. At 1.0 seconds, streaming made 1/26 errors versus
batch 2/26, max END-to-final was 0.712 s, and the FIFO reached 5/8 / 711 ms.
Neither cadence dropped or overflowed PCM. Streaming returned empty for all
four static/no-speech controls; batch reproducibly returned `Yeah.` on neg001
at both cadences and also prefixed `Yeah` to positive case 002. Both reports
correctly remain policy failures and `full_gate0a_complete=false`.

`hw1_ai_service/stt/live.py` and the separate
`tools/live_pcm_shadow_probe.py native-stt` mode form the current default-off
gate.
The worker coalesces small UART PCM frames into 4096-byte chunks, queues eight
chunks (32 KiB / 1.024 s), and drains FIFO immediately after each synchronous
Moonshine call. One worker thread owns model creation and every stream call.
The first hardware mode uses 1.0-second updates, a soft 0.8-second final target,
and a 2.0-second hard wait. It never starts the LLM or sends ASK/REPLY; model
failure cannot stop transport draining or invalidate the canonical WAV.
The live worker plus native-shadow focused suite passes 41 tests under
`-W error`; the complete CM5 suite passes 348 with 1 skipped and 7 subtests.
Use `tools/run_native_live_stt_gate.sh` for the physical run; it restores the
prior power profile and service/UART ownership on every exit.

The first corrected physical native-STT run completed the full G2/live/WAV
path with all 110,400 PCM bytes processed, queue high-water 3/8, 537.6 ms
maximum queue age, no overflow, and a 51.0 ms END-to-final result. It failed
only the pinned transcript assertion: `Haitian is the capital difference.`
versus `what is the capital of france` (3 word errors). This confirms the
larger bounded FIFO behaves as intended, while leaving the live-STT accuracy
gate open. Increasing the 0.8/2.0-second wait would not repair this observed
wrong-but-prompt final.
The pulled audio also revealed that this specific capture supplied only
7.996 ksamples/s over live wall time while declaring 16 kHz; the earlier native
no-STT capture supplied 16.03 ksamples/s. The wearer reports a robotic,
low-bitrate sound. Treat the transcript as downstream of an unresolved G2 LC3
frame-duration/notification-cadence fault. The next gate is raw/cadence audio
diagnosis, not more FIFO or a longer STT wait.

## Run as a service

```bash
install -Dm0644 systemd/hw1-ai-service.service \
  "$HOME/.config/systemd/user/hw1-ai-service.service"
systemctl --user daemon-reload
systemctl --user enable hw1-ai-service
systemctl --user restart hw1-ai-service
sudo loginctl enable-linger "$USER"
# trigger exchanges while it runs:
echo ask  | nc -U ~/.local/run/hw1-ai-service.sock
echo 'chat what time is it' | nc -U ~/.local/run/hw1-ai-service.sock
```

The installed unit uses `Restart=always` and a 60-second systemd service
watchdog. A stdlib-only task on the daemon's main asyncio loop sends a
keep-alive every 30 seconds, beginning before UART login. If the Python process
exits unexpectedly or that loop stops scheduling, systemd kills the entire
unit control group (including `llama-server`) and restarts it after five
seconds. Explicit `systemctl --user stop` remains stopped. The watchdog is a
process/control-loop recovery mechanism; it does not detect or reboot a hung
Linux kernel, test UART health, or detect a wedged native worker while the
asyncio loop itself remains responsive.

Verify that the deployed unit enabled the watchdog and is receiving pings:

```bash
systemctl --user show hw1-ai-service \
  -p ActiveState -p SubState -p NRestarts -p Result \
  -p WatchdogUSec -p WatchdogTimestampMonotonic
```

`WatchdogUSec=1min` and a nonzero, advancing watchdog timestamp indicate that
the unit and Python notifier agree. Manual CLI runs have no systemd notification
environment and use the same code as a no-op.

With matching firmware, inspect the application lease from either side:

```text
cm5 status
uartlink status
g2evenai status
```

`cm5 status` includes state/epoch/sequence, direct freshness, command grace,
stale-transition count, and the ESP32 task's measured minimum free stack. The
first valid heartbeat lazily creates the 2 KiB low-priority `cm5_presence`
task; it sleeps indefinitely when absent or stale and does not own UART,
audio, microphone, G2, or host-power work.

Firmware has two UART control-plane shortcuts. It always handles the
five-second heartbeat there, so executor traffic from another interface cannot
queue in front of the heartbeat itself. It also handles the canonical
lowercase `liveaudio ready 1 <16-hex-controller>` form there only when renewing
an unexpired lease owned by that controller and the current named-login epoch.
Initial acquisition, expiry, mismatch, and repair still use the ordinary
registry path. Both actors continue to send the same text request through the
existing `Session.command()` request/reply bridge—there is no second UART
writer or parallel reply parser. Firmware with the shortcut advertises
`renew_direct=1` alongside its lease timing. The service strictly validates
that grant and renews at the advertised one-second cadence; older firmware
lacks the marker and retains the legacy two-second cadence so its registry
path is not driven twice as often. Status verification remains elapsed-time
based at roughly 16 seconds under either cadence. A link reset or login-epoch
change discards the negotiated timing and requires a fresh grant.

If the Pi's own foreground command holds Python's serialized `Session` lock,
the heartbeat actor still cannot send a renewal; firmware then preserves only
the already-fresh presence lease through the bounded 75-second command
extension and five-second post-reply grace. `cm5 status`, `cm5 capabilities`,
`liveaudio status`, and `liveaudio capabilities` remain ordinary authorized
registry commands and can be inspected from another authenticated interface
even when no G2 is connected. Firmware suppresses the case-insensitive
LiveAudio inspection leading-token forms from the shared CLI feed and CM5
command-busy/grace accounting, but not from command audit. The service account
must be a known, unbanned account with a recognized non-guest role; guest or
fail-closed account records cannot publish readiness or use the healthy-renewal
shortcut.

When updating an already-running installation, rerun the install/reload/restart
sequence above. Merely running `enable --now` does not restart an active old
process.

```bash
install -Dm0644 systemd/hw1-ai-service.service \
  "$HOME/.config/systemd/user/hw1-ai-service.service"
systemd-analyze --user verify ~/.config/systemd/user/hw1-ai-service.service
systemctl --user daemon-reload
systemctl --user restart hw1-ai-service
```

For a Python/YAML-only prompt update, no firmware flash, package reinstall, or
`daemon-reload` is required. Sync the canonical source tree, edit only the
existing `llm.system_prompt` value in the live configuration—or remove that
key to inherit the tracked default—without copying the example over a tuned
configuration, validate it with
`hw1_ai_service.config.load`, and restart the service.

## CM5 power profiles and shutdown control

Power control is disabled until its isolated root helper is installed. The AI
service itself remains an unprivileged user process; sudo permits only the
helper's finite actions, and the root-owned helper parses typed arguments and
uses fixed argv without a shell.

```bash
# From this directory, for the Linux account that runs hw1-ai-service:
sudo ./systemd/install-power-helper.sh "$USER"
# Re-login so the new hw1-power supplementary group is present, then set:
#   power.enabled: true
systemctl --user restart hw1-ai-service
```

Keep `/usr/local/libexec/hw1-power-helper` owned by root and not writable by the
service account. The installer uses mode 0755 for it and 0440 for the sudoers
file. `sudo visudo -cf /etc/sudoers.d/hw1-power-helper` rechecks the policy.

From an authenticated HardwareOne CLI, use `cm5 power status`, `cm5 power
profile <eco|balanced|performance|auto>`, and `cm5 power show`. Destructive
superadmin forms require the literal same-line confirmation:
`cm5 power reboot confirm`, `cm5 power halt confirm`, `cm5 power suspend
confirm`, or `cm5 power sleep_for <1..1440> confirm`.

The version-1 UART contract accepts only these firmware events:

- `cm5_power_status 1 <id>`
- `cm5_power_profile_eco|balanced|performance|auto 1 <id>`
- `cm5_power_reboot|halt|suspend 1 <id>`
- `cm5_power_sleep_for 1 <id> <minutes>` (bounded to 1..1440, and optionally
  narrowed in config)

`<id>` is the firmware's exact 16-hex boot-nonce/counter ID. Replies are the
finite `cm5 power ack 1 ...` and `cm5 power report 1 ...` commands. Every report
ends with the normalized 32-hex Linux `/proc/sys/kernel/random/boot_id`. An
in-memory ID cache makes retries at-most-once for the life of the daemon: a
repeated ID only replays its cached ACK. Every disruptive action must receive
OK replies for both `accepted` and `committed` before the helper is invoked.
That second boundary lets firmware safely discard an accepted request after a
same-boot daemon restart; a changed boot ID completes a committed reboot/halt
whose final reply was lost during shutdown. Same-boot committed transitions
stay fail-closed. After checking that the CM5 is stable and no systemd power
job remains pending, a superadmin may clear that exceptional state with
`cm5 power recover confirm`.

Profiles map to Linux CPU-frequency governors. `auto` applies the configured
active profile around each AI job, then returns to the idle profile after the
debounce; selecting eco, balanced, or performance manually disables those
automatic transitions until auto is selected again.

`cm5_power_halt` uses the Raspberry Pi 5 low-power halt path. Timed `sleep_for`
writes a relative `+seconds` alarm to `/sys/class/rtc/rtc0/wakealarm`, then
requests asynchronous halt; it does not keep a helper process alive for the
requested minutes. For the documented low-power (~mA) halt and RTC cold-boot
behavior, the Pi EEPROM must have `POWER_OFF_ON_HALT=1` and `WAKE_ON_GPIO=0`.
The helper can verify the kernel RTC interface, but cannot prove those EEPROM
settings or carrier rail behavior, so check them with `rpi-eeprom-config` and
bench-test wake before relying on it unattended. The next boot sends
`cm5 power report 1 0 awake <mode> <linux-boot-id>`.

Plain suspend is more hardware/image dependent on Pi 5/CM5. It is rejected
deterministically unless both `power.allow_suspend: true` and the helper's
`/sys/power/state` capability check succeed. A synchronous suspend reports
awake again after resume when the UART link survives.

## CM5 fan curve and manual modes

Fan control is disabled until the isolated root controller is installed. The
controller, rather than the unprivileged AI service or the XIAO, owns fan
sysfs discovery, the temperature-to-PWM curve, tachometer checks, and safety
overrides.

```bash
# From this directory, name the unprivileged Linux account that actually runs
# hw1-ai-service (the installer rejects root):
sudo ./systemd/install-fan-controller.sh <service-user>
# Reboot so the lingering user systemd manager receives the new hw1-fan
# supplementary group. Verify the printed/systemd health is neither
# unavailable nor io_error, bench-check the fan, and only then set:
#   fan.enabled: true
systemctl --user restart hw1-ai-service
```

The installer preserves an existing `/etc/hw1-fan-controller.json`. Edit that
root-owned file to tune `quiet_pwm`, the ordered `curve` steps, hysteresis,
start boost, and the critical-temperature/stall thresholds, then restart with
`sudo systemctl restart hw1-fan-controller`. The default curve is:

| CPU temperature | Requested PWM |
|---:|---:|
| below 50 °C | 0 |
| 50 °C | 75 |
| 60 °C | 125 |
| 67.5 °C | 175 |
| 75 °C | 250 |

From an authenticated HardwareOne admin CLI:

- `cm5 fan quiet` holds the configured quiet PWM.
- `cm5 fan max` holds PWM 255 and may supersede a pending non-Max request.
- `cm5 fan auto` returns to the configured temperature curve.
- `cm5 fan status` fetches current temperature, requested/effective mode,
  requested and measured PWM, tachometer RPM when present, and health.
- `cm5 fan` or `cm5 fan show` reads the XIAO's local request/last-report state
  without starting a new transaction.

The controlled value is PWM duty (`0..255`), not a promised RPM. RPM is
read-only tachometer telemetry; a target-RPM controller would require
per-fan calibration and a separate closed loop.

Quiet and Max suppress normal curve updates, but Quiet is not allowed to
defeat cooling safety. The controller forces Max at the configured critical
temperature or after a sustained zero-RPM stall, reports `safety_temp` or
`safety_stall`, and releases those overrides only after recovery/hysteresis.
It starts in Auto after every service start and does not persist a remote
Quiet request across reboot. Its systemd watchdog restarts a wedged process;
graceful exit restores the kernel `step_wise` thermal policy through the
verified handle, or attempts verified PWM 255 if that policy write fails. A
critical log records the rare case where neither action can be confirmed.
`ExecStartPre` and `ExecStopPost` also attempt a best-effort
rediscovery/restore, including after watchdog `SIGKILL`, while the unique
sysfs topology remains available.

Discovery keys on hwmon name `pwmfan`, cooling-device type `pwm-fan`, and the
actual thermal-zone binding—never an unstable `hwmonN` number. If exactly one
supported topology or the `user_space` thermal policy is not present, mode
requests fail explicitly. Inspect it with:

```bash
systemctl status hw1-fan-controller
systemctl show hw1-fan-controller --property=StatusText --value
journalctl -u hw1-fan-controller -n 100 --no-pager
```

## RAM profiles (any Pi size works)

The service checks the RAM budget at startup (`service.ram_check`: warn |
strict | off) and degrades gracefully at runtime — if llama-server dies
repeatedly (the OOM signature) the supervisor stops thrashing and voice
exchanges deliver the transcript with an "(assistant offline)" marker
instead of failing.

| Pi RAM | Config |
|---|---|
| 8GB+ | defaults; LLM up to 3B-class Q4 |
| 4GB | Measure the deployed medium + LLM first. For a smaller tier, download it explicitly and set `stt.model` to the returned model directory; `moonshine/tiny` is not a valid current alias. |
| 2GB / minimal | one engine: `llm.engine: none` (voice → transcript delivery) or `stt.engine: none` (text chat only) |

## Operating notes

- Native EvenAI wake/cancel is correlated by one firmware-issued 16-hex
  exchange ID, which also owns the recorder. The accepted EVT grammar is
  `evenai_wake <id>`, `evenai_cancel <id> <reason>`, and
  `mic_autostop <id> <absolute-path>`. Untagged wakes fail closed; the legacy
  path-only `mic_autostop <path>` remains valid only for manual `ask` captures.
  Lens delivery uses `g2evenai askid|replyid|replypartid|replyendid|exitid`; recorder
  backstops use `micrecord statusid|stopid`, and cleanup uses `micdeleteid`.
  An ID-matched `discarded` recorder result is also treated as cancellation,
  covering loss of the unacknowledged cancel EVT without a 15-second wait.
  All spellings are centralized in `hw1_ai_service/evenai_protocol.py` while
  the firmware side of the protocol settles.
- Recorder-shadow authorization additionally binds the exchange to the
  renewable liveaudio controller lease and exact UART login epoch. The epoch is
  not a new BEGIN/PCM/END/ABORT payload field. Lease/auth/link loss aborts only
  the shadow; the independent recorder continues to its canonical saved,
  discarded, or failed result.
- Pending native EvenAI work has priority over queued manual `ask`/`chat`
  jobs (a job already executing is not preempted). Owner-scoped WAV deletion
  overlaps STT but has a five-second ceiling before dispatch releases the
  exchange and power lease; failure leaves a logged stray file.
- Dismissal is cooperative and fail-closed. An already-written UART operation
  is drained before the lock is released, but no later stage or lens mutation
  begins. Native STT is allowed to finish on its sole worker and its result is
  discarded; LLM streaming is closed. Canceled turns do not commit LLM history
  or diagnostic WAV persistence.
- Foreground wearer tests can opt into repeated, non-gating terminal cues with
  `daemon --evenai-cancel-marker-interval-s 0.10`. While each naturally
  occurring `capture/fetch`, `stt`, `question`, or streamed `answer_tail`
  cancellation window is open, the daemon prints `>>> TAP NOW <<<`; its one
  closing record includes monotonic `start_ns`/`stop_ns`, elapsed milliseconds,
  and the advance/cancel outcome. The option is daemon-CLI-only, defaults off,
  never adds a sleep or wire command, and is intentionally absent from YAML and
  systemd. Enabled markers still add synchronous terminal/log I/O, so do not
  use their run as a clean latency benchmark.
- A native job is marked delivered only after its matching complete reply was
  accepted. Every other exit path makes one bounded five-second, non-replayed
  exact-ID `exitid` attempt before releasing its registry entry and power
  lease. This covers stale wakes, disabled/failed STT, fetch failures, and
  model-initialization drops. A wearer-dismissed or superseded ID rejects
  harmlessly at the firmware fence. Firmware send failures are terminal, and
  a stale daemon's untagged mutation is rejected and closes the active
  legacy-mismatched exchange instead of leaving it on heartbeats.
- If the XIAO loses the UART or CM5-service host gate, it best-effort EXITs, terminalizes the
  exact exchange, and discards that exchange's owned recording. The reason
  identifies the observed boundary:

  - `host_link_lost_runtime`: the UART runtime is stopped.
  - `host_link_lost_never`: the runtime is up, but no UART login has succeeded
    during this firmware boot (`active_epoch=0`, `last_epoch=0`).
  - `host_link_lost_cleared`: a login succeeded earlier in this boot, but the
    active session was cleared (`active_epoch=0`, `last_epoch!=0`).
  - `host_link_lost_epoch`: the current nonzero login epoch differs from the
    nonzero epoch bound to the exchange; a replacement login cannot inherit the
    old exchange.
  - `host_service_never`: this UART epoch has not advertised CM5 presence.
  - `host_service_stale`: the exact-session presence lease expired.
  - `host_service_unready`: the fresh mode is `starting` or `degraded`.
  - `host_service_busy`: the fresh daemon is busy, so a new wake is declined;
    an already-bound exchange treats busy as healthy.

  `g2evenai status` and `uartlink status` expose the runtime, active/last epoch,
  and `last_event` diagnostics. Last epoch/event are evidence only, never
  authorization. The service handles every valid cancellation reason the same
  way: tombstone and cooperatively cancel only the named ID, preserve the reason
  for logging, and prevent further work or lens mutation for that exchange. It
  does not use a reason to replay the exchange or to repair the transport;
  ordinary link/session reconnect and login own recovery, and the next wake
  receives a new ID. Because EVT delivery is best-effort, firmware's local
  terminal fence and exact-owner discard remain authoritative if the cancel
  itself cannot cross the failed link.
- A daemon crash that leaves the UART open is detected when its presence lease
  expires; the G2 owner is woken on that deadline and terminates an active
  exchange. This is application liveness, not proof that the Linux kernel or
  every native worker is healthy. Ordinary batch EvenAI does not require the liveaudio lease. Native
  recorder shadow consumes it only after an explicit `shadow ... on native`
  diagnostic arm; the production daemon never arms or renews it.
- `audio.vad_max_seconds` (default 15 seconds) is still the host's bounded
  auto-stop wait for manual `ask` and the lost-event/never-ended backstop for
  native batch capture. A matching `evenai_cancel` or ID-scoped `discarded`
  result exits early; the service no longer burns the remaining 15 seconds
  after a wearer dismissal. The native wake's 1,800 ms trailing-silence value
  is selected by firmware and is separate from the manual-ask
  `audio.vad_silence_ms` setting.
  Recorder shadow does not change this batch-WAV wait or either VAD setting.
- On daemon startup, `deliver.g2_stream_speed: 40` attempts the field-only
  command `g2aiconfig - 40 -` before model work begins. This does not change
  `voiceSwitch` or `duplexMode`. Set it to `0` to preserve the G2's current
  runtime state for a controlled test; `80` is the other hardware-validated
  automatic value. A successful XIAO result confirms only that the BLE write
  was submitted, not that the asynchronous G2 CONFIG echo was received. This is a
  startup policy, not a reconciler: a glasses-only power cycle is not detected,
  so restart the daemon if 40 must be reapplied.
- Every XIAO reset sprays a ROM boot burst that arrives as garbage — the
  session treats it as a reboot hint, immediately fences CM5 presence to
  `starting`, wakes the reconnect supervisor, quiesces (OTA-probation rule),
  and re-logs in. An unexplained authentication-epoch loss takes the same
  fail-closed path. This is normal recovery, not a stale `ready` replay.
- P0 audio is firmware-processed (~24x gain/HPF/pre-emphasis). Good for
  pipeline proving and engine comparison; the raw-vs-processed STT
  decision waits for the P2 burst path (plan §Gap A0).
- The Moonshine pip API has shifted between releases; if startup rejects
  it, `stt/moonshine.py` documents the adapter to touch up (or switch
  `stt.engine: zipformer`).
- `stt.model: en` currently resolves to the first English catalog model,
  `medium-streaming-en`. Use the exact downloaded directory when an experiment
  must pin small, tiny, or medium reproducibly.
