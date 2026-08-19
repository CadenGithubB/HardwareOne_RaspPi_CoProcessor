# CM5 deployment paths and sync workflow

This is the canonical path record for the `hw1-ai-service` deployment on the
carrier CM5 named by `$CM5_HOST`. Use these paths in commands, diagnostics, benchmarks, and
future documentation. Do not create a second service source tree.

## Environment

Commands and paths in this repo's docs use these variables so nobody's host,
account or checkout location is baked into the text. Set them once per shell:

```bash
export CM5_HOST=<ip-or-hostname-of-your-cm5>
export CM5_USER=<ssh-user-on-the-cm5>
export REPO_ROOT=<path-to-this-repo>           # HardwareOne_RaspPi_CoProcessor
export FIRMWARE_ROOT=<path-to-the-firmware-repo>
```

`$ESP_ROOT` is the parent holding both checkouts; `$HOME` is your normal home
directory. Remote paths are written `/home/$CM5_USER/...`.

## Canonical layout

| Purpose | Canonical path |
| --- | --- |
| Mac repository root | `$FIRMWARE_ROOT` |
| Mac AI-service source | `$REPO_ROOT/ai-service` |
| CM5 SSH target | `$CM5_USER@$CM5_HOST` (`$CM5_HOST` currently) |
| CM5 AI-service source | `/home/$CM5_USER/hw1-ai-service` |
| CM5 Python environment | `/home/$CM5_USER/hw1ai` |
| Service executable | `/home/$CM5_USER/hw1ai/bin/hw1-ai-service` |
| Service config | `/home/$CM5_USER/.config/hw1-ai-service/config.yaml` |
| UART credentials | `/home/$CM5_USER/.config/hw1-ai-service/credentials` |
| User systemd unit | `/home/$CM5_USER/.config/systemd/user/hw1-ai-service.service` |
| Runtime socket | `/home/$CM5_USER/.local/run/hw1-ai-service.sock` |
| Last captured utterance | `/home/$CM5_USER/.cache/hw1-ai-service/last-utterance.wav` |
| LLM models | `/home/$CM5_USER/models` |
| llama.cpp checkout | `/home/$CM5_USER/llama.cpp` |
| XIAO UART | `/dev/ttyAMA2` at 2,000,000 baud |

`/home/$CM5_USER/hw1ai` is a virtual environment, not another source checkout.
The only current CM5 source directory is `/home/$CM5_USER/hw1-ai-service`.

`cm5/deploy_cm5.sh` defaults to `$CM5_USER@$CM5_HOST`, so normal DHCP address changes
need no repository edit. Override the host or account for one invocation with
`CM5_HOST=$CM5_HOST` or `CM5_USER=another-user` if needed.
After a sync has already completed, `cm5/deploy_cm5.sh --verify-only` reruns
only the remote health check. On images without journal storage, that check
uses the live user control socket together with an unchanged PID/restart count
and an advancing systemd watchdog instead of requiring journal records.

The August 9 installation transcript describes the older Pi 5 deployment under
`/home/$CM5_USER`; it is historical evidence, not evidence that the current CM5
tree is installed. The current deployment must independently verify that
`$HOME/hw1ai` imports from `$HOME/hw1-ai-service` after the next sync.

> **Current-versus-historical path rule:** commands in the installation and
> lifecycle sections immediately below use the current `cm5` account. Later
> physical-test records retain `/home/$CM5_USER` when they describe an already-run
> Pi 5 experiment; do not paste those historical absolute paths on the CM5.

## Known stale path

The old Pi 5 paths under `/home/$CM5_USER` are historical and are not valid on the
current `cm5` account. If an obsolete `/home/$CM5_USER/ai-service` copy exists, do
not `cd` into it, run Python from it, sync into it, or include it in evidence
bundles. Python puts
the current directory first on `sys.path`, so running a benchmark there can
silently import the stale package even when the virtual environment is
editable-installed from `$HOME/hw1-ai-service`.

The August 9 STT benchmark failed for exactly this reason: it imported
`/home/$CM5_USER/ai-service/hw1_ai_service/config.py`, which predates the `power`
configuration section. That failure was not a Moonshine failure.

After the canonical deployment is verified, quarantine the stale tree rather
than deleting it:

```bash
mv "$HOME/ai-service" \
  "$HOME/ai-service.STALE-$(date +%Y%m%d-%H%M%S)"
```

## One Mac-to-CM5 sync command

Run this on the Mac from any directory:

```bash
rsync -av --itemize-changes \
  --exclude '.pytest_cache/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  $REPO_ROOT/ai-service/ \
  $CM5_USER@$CM5_HOST:hw1-ai-service/
```

The trailing slashes are intentional: they sync the contents of the one Mac
source directory into the one CM5 source directory. This command does not
delete remote files.

## Prompt-only AI-service update

A system-prompt change is CM5 Python/YAML work only. It does not change the
UART grammar and needs no XIAO flash. The package is installed editable from
the canonical source directory, so syncing `.py` files and restarting the user
service is sufficient; no package reinstall or `daemon-reload` is needed.

The running unit loads
`$HOME/.config/hw1-ai-service/config.yaml` once at startup. If that file
contains `llm.system_prompt`, it overrides the tracked Python default. Never
copy `config.example.yaml` wholesale over the tuned live configuration.

First test and dry-run the non-deleting sync on the Mac:

```bash
cd $REPO_ROOT/ai-service
python3 -m pytest -q

rsync -avni --itemize-changes \
  --relative \
  ./hw1_ai_service/config.py \
  ./config.example.yaml \
  ./tests/test_system_prompt.py \
  ./README.md \
  $CM5_USER@$CM5_HOST:hw1-ai-service/
```

Then stop the service and back up its active configuration on the Pi:

```bash
ssh $CM5_USER@$CM5_HOST

PROMPT_STAMP="$(date +%Y%m%d-%H%M%S)"
PROMPT_BACKUP="$HOME/deploy-backups/prompt-${PROMPT_STAMP}"
install -d -m 0700 "$PROMPT_BACKUP"
ln -sfn "$PROMPT_BACKUP" "$HOME/deploy-backups/prompt-latest"
install -m 0644 "$HOME/hw1-ai-service/hw1_ai_service/config.py" \
  "$PROMPT_BACKUP/config.py"
install -m 0600 "$HOME/.config/hw1-ai-service/config.yaml" \
  "$PROMPT_BACKUP/config.yaml"
systemctl --user stop hw1-ai-service.service
systemctl --user is-active hw1-ai-service.service
fuser -v /dev/ttyAMA2
exit
```

`is-active` should print `inactive`, and `fuser` should print no holder. From
the same Mac directory, run the actual prompt-only sync:

```bash
rsync -av --itemize-changes \
  --relative \
  ./hw1_ai_service/config.py \
  ./config.example.yaml \
  ./tests/test_system_prompt.py \
  ./README.md \
  $CM5_USER@$CM5_HOST:hw1-ai-service/
```

Finally, remove the existing `llm.system_prompt` key from the active YAML to
inherit the tracked code default (recommended), or edit only that value so it
matches `config.example.yaml`. Validate the exact effective prompt without
opening the UART or loading models, then start the service:

```bash
ssh $CM5_USER@$CM5_HOST
${EDITOR:-nano} "$HOME/.config/hw1-ai-service/config.yaml"
chmod 0600 "$HOME/.config/hw1-ai-service/config.yaml"

cd "$HOME"
"$HOME/hw1ai/bin/python" - <<'PY'
import os
import hw1_ai_service
from hw1_ai_service.config import DEFAULT_SYSTEM_PROMPT, load

path = os.path.expanduser("~/.config/hw1-ai-service/config.yaml")
cfg = load(path)
assert cfg.llm.system_prompt == DEFAULT_SYSTEM_PROMPT, (
    "active llm.system_prompt does not match the deployed code default")
print("package:", hw1_ai_service.__file__)
print("prompt:", repr(cfg.llm.system_prompt))
print("max_tokens:", cfg.llm.max_tokens)
print("history_turns:", cfg.llm.history_turns)
PY

systemctl --user start hw1-ai-service.service
systemctl --user is-active hw1-ai-service.service
systemctl --user --no-pager -l status hw1-ai-service.service
test -S "$HOME/.local/run/hw1-ai-service.sock"
printf 'chat Reply with: prompt update active\n' \
  | nc -U "$HOME/.local/run/hw1-ai-service.sock"
```

The printed package path must begin with
`$HOME/hw1-ai-service/hw1_ai_service/`, `max_tokens` should remain 120,
and the socket request should return `queued`; its answer appears on the
configured display. If validation or startup fails, restore the timestamped
`config.py` and `config.yaml` with modes 0644 and 0600 before restarting the
service. The stable
`$HOME/deploy-backups/prompt-latest` link points to that backup.

## Coordinated exchange-ID cancellation deployment

The native dismissal contract changes both ends of the UART grammar. Deploy the
CM5 service and XIAO image as one maintenance operation. A mismatched pair is
designed to fail closed rather than mutate an unrelated exchange, but it may
ignore a wake or produce no answer. In particular, new firmware rejects a stale
daemon's untagged mutation and terminalizes the active exchange as
`legacy_command`. Stop the daemon before either half changes; do not test
through a partially synchronized source tree.

First, on the Mac, verify both artifacts from their canonical directories:

```bash
cd $FIRMWARE_ROOT
set -o pipefail
source $ESP_ROOT/esp-idf/export.sh

./tools/build_board.sh xiao_s3 build

cd $REPO_ROOT/ai-service
python3 -m pytest -q
```

The per-board build cache is `build-xiao_s3/`; `flash` will also perform an
incremental build if source changed. Do not run `fullclean`, `erase-flash`, or
`erase_flash` for this application update.

Stop the CM5 daemon and prove it released the UART:

```bash
ssh $CM5_USER@$CM5_HOST

systemctl --user stop hw1-ai-service.service
systemctl --user is-active hw1-ai-service.service
fuser -v /dev/ttyAMA2

exit
```

`is-active` should print `inactive`, and `fuser` should print no holder. Now
sync the one service tree from the Mac:

```bash
rsync -av --itemize-changes \
  --exclude '.pytest_cache/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  $REPO_ROOT/ai-service/ \
  $CM5_USER@$CM5_HOST:hw1-ai-service/
```

Flash the XIAO from the Mac. Substitute the exact port printed by the first
command; do not leave the wildcard in the flash command:

```bash
cd $FIRMWARE_ROOT
set -o pipefail
source $ESP_ROOT/esp-idf/export.sh

CANCEL_LOCAL_DIR="$PWD/.scratch/evenai-cancel-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$CANCEL_LOCAL_DIR"
ln -sfn "$CANCEL_LOCAL_DIR" "$PWD/.scratch/evenai-cancel-latest"

find /dev -maxdepth 1 \
  \( -name 'cu.usbmodem*' -o -name 'cu.usbserial*' \) \
  -print
XIAO_PORT=/dev/cu.usbmodem_REPLACE_WITH_EXACT_PORT

./tools/build_board.sh xiao_s3 -p "$XIAO_PORT" flash monitor \
  2>&1 | tee "$CANCEL_LOCAL_DIR/xiao-flash-monitor.log"
```

Leave the serial monitor running for the dismissal evidence. In another
terminal, start only the canonical service and verify its import path and
exchange capability:

```bash
ssh $CM5_USER@$CM5_HOST

systemctl --user start hw1-ai-service.service
systemctl --user is-active hw1-ai-service.service
systemctl --user --no-pager -l status hw1-ai-service.service

cd "$HOME"
"$HOME/hw1ai/bin/python" - <<'PY'
import hw1_ai_service
from hw1_ai_service import evenai_protocol

print('package:', hw1_ai_service.__file__)
print('exchange parser:', evenai_protocol.__file__)
PY
```

Both printed module paths must begin with
`$HOME/hw1-ai-service/hw1_ai_service/`. The next native wake should show
a strict 16-hex ID in the XIAO log. The maintained probe's `preflight` command
also requires the XIAO capability `exchange-id-v1`; run it only with the daemon
stopped, as documented below.

### Physical dismissal matrix

This revision is not complete on hardware until double-tap dismissal is tried
at each observable stage. Use one fresh wake per row and confirm a normal fresh
wake still works after it:

Do not rely on journald being present: this Pi has previously returned
`No journal files were found`. For a complete CM5-side record, stop the user
unit and run the same canonical executable in the foreground. Keep this SSH
terminal open while performing the matrix; press Control-C only after the last
fresh-wake recovery check:

```bash
set -o pipefail
systemctl --user stop hw1-ai-service.service
fuser -v /dev/ttyAMA2

CANCEL_STAMP="$(date +%Y%m%d-%H%M%S)"
CANCEL_DIR="$HOME/g2-prefx/evenai-cancel-${CANCEL_STAMP}"
mkdir -p "$CANCEL_DIR"
ln -sfn "$CANCEL_DIR" "$HOME/g2-prefx/evenai-cancel-latest"

cd "$HOME"
"$HOME/hw1ai/bin/hw1-ai-service" \
  -c "$HOME/.config/hw1-ai-service/config.yaml" \
  daemon --evenai-cancel-marker-interval-s 0.10 \
  2>&1 | tee "$CANCEL_DIR/cm5-daemon.log"
```

The marker option is diagnostic, daemon-CLI-only and default-off. It does not
hold the pipeline or send a command: it repeatedly prints `>>> TAP NOW <<<`
during each naturally open capture/fetch, STT, question, and streamed-answer
tail window. The closing line records monotonic `start_ns`/`stop_ns`, elapsed
milliseconds and whether the real next action or a dismissal ended the window.
Enabled marker logging adds terminal/tee I/O, so use the run for cancellation
evidence rather than clean latency benchmarking.

| Stage | When to double tap | Required result |
| --- | --- | --- |
| Capture | While still speaking or during the silence tail | Card stays dismissed; owned WAV is finalized then discarded. |
| Stop/fetch | Just after speech ends, before the question appears | Transfer drains safely; no question or answer appears. |
| Batch STT | After fetch activity, before the question appears | Inference may finish internally, but its transcript is discarded. |
| ASK render hold | While the recognized question is drawing | No reply replaces or reopens the dismissed card. |
| Streamed answer | Immediately after the first answer text appears | No later part or finalizer appears; history is not committed. |

After Control-C, summarize the full foreground record, restart the service, and
save `systemctl status` in the same timestamped directory. The status capture
is also the minimum fallback when a matrix was accidentally run under systemd
on a host without persistent journal files:

```bash
grep -Ei 'evenai|cancel|discard|wake|reply|history|cleanup|legacy_command|send_failed|host_link_lost' \
  "$CANCEL_DIR/cm5-daemon.log" \
  | tee "$CANCEL_DIR/cm5-cancel-summary.log"

systemctl --user start hw1-ai-service.service
systemctl --user --no-pager -l status hw1-ai-service.service \
  2>&1 | tee "$CANCEL_DIR/systemd-status-after.log"
```

For each dismissal, correlate one ID across the two logs. The XIAO should show
`terminal id=<id> reason=dismiss`, advisory cancel-copy delivery, and
`stop+discard` when that ID still owned the recorder. The CM5 should log the
same ID as canceled and must not issue any later tagged ASK/reply mutation for
it. Because native Moonshine batch inference is cancel-opaque, CPU work may
continue briefly after a dismissal; visible delivery, persistence, and history
commit must not.

Pull the CM5 run into the same Mac evidence directory that already contains
the XIAO flash/serial log. The stable links on both machines avoid relying on a
shell variable surviving across terminals and cannot silently produce the
empty `evenai-cancel-` path seen with an unset timestamp variable:

```bash
CANCEL_PULL_DIR="$(readlink $FIRMWARE_ROOT/.scratch/evenai-cancel-latest)"
test -n "$CANCEL_PULL_DIR"
mkdir -p "$CANCEL_PULL_DIR/cm5"

rsync -avL \
  $CM5_USER@$CM5_HOST:g2-prefx/evenai-cancel-latest/ \
  "$CANCEL_PULL_DIR/cm5/"

find "$CANCEL_PULL_DIR" -maxdepth 2 -type f -print
```

## Install or refresh the CM5 environment

Run on the CM5 after syncing. This refresh deliberately requires the existing
live config and UART credentials; it never creates or overwrites either one.
It stops the daemon before changing its environment and remains stopped if a
validation step fails.

```bash
set -Eeuo pipefail
cd "$HOME"

CONFIG="$HOME/.config/hw1-ai-service/config.yaml"
test -r "$CONFIG"
test -r "$HOME/.config/hw1-ai-service/credentials"

systemctl --user stop hw1-ai-service.service

test -x "$HOME/hw1ai/bin/python" || \
  python3 -m venv "$HOME/hw1ai"

"$HOME/hw1ai/bin/python" -m pip install -e \
  "$HOME/hw1-ai-service[moonshine]"

package_path=$("$HOME/hw1ai/bin/python" -c \
  'import hw1_ai_service; print(hw1_ai_service.__file__)')
case "$package_path" in
  "$HOME"/hw1-ai-service/hw1_ai_service/*) ;;
  *) echo "unexpected package path: $package_path" >&2; exit 1 ;;
esac

"$HOME/hw1ai/bin/python" - "$CONFIG" <<'PY'
from hw1_ai_service import config
import sys

cfg = config.load(sys.argv[1])
config.read_credentials(cfg.link.credentials_file)
print("config and credential permissions valid")
PY

install -Dm0644 \
  "$HOME/hw1-ai-service/systemd/hw1-ai-service.service" \
  "$HOME/.config/systemd/user/hw1-ai-service.service"

systemd-analyze --user verify \
  "$HOME/.config/systemd/user/hw1-ai-service.service"
systemctl --user daemon-reload
systemctl --user enable --now hw1-ai-service.service
systemctl --user is-active --quiet hw1-ai-service.service
systemctl --user --no-pager status hw1-ai-service.service
```

The tracked unit uses the virtual-environment executable directly. Do not
install or invoke a second copy from `~/.local/bin`.

The daemon now defaults `deliver.g2_stream_speed` to `40`. Each daemon start
attempts the exact field-only command `g2aiconfig - 40 -` before loading the AI
models; it does not write `voiceSwitch` or `duplexMode`, and it does not require
another XIAO flash. Confirm a locally successful host-side attempt with:

```bash
journalctl --user -u hw1-ai-service.service -n 100 --no-pager \
  | grep 'G2 EvenAI streamSpeed=40 submitted'
```

That line means the XIAO accepted the BLE write, not that the Pi observed the
G2's asynchronous CONFIG echo. A local XIAO error, unknown command, timeout,
failed re-login, or no reachable temple is logged and does not prevent daemon
startup. An older G2 can still reject the write asynchronously without that
rejection reaching this daemon log. This is startup-only: it does not detect a
glasses-only reconnect or power cycle. Restart the daemon to reapply 40. For a
controlled no-CONFIG baseline, set
`deliver.g2_stream_speed: 0` **before** starting the daemon, stop the daemon
before power-cycling the glasses, and keep it stopped while the probe owns the
UART. Restarting with the default after a probe deliberately returns the
runtime policy to 40.

## Verify what systemd will actually run

Run this before performance measurements or after any deployment change:

```bash
systemctl --user show hw1-ai-service.service \
  -p FragmentPath -p ExecStart -p ActiveState -p SubState \
  -p WatchdogUSec -p WatchdogTimestampMonotonic -p NRestarts \
  --no-pager

head -n 1 "$HOME/hw1ai/bin/hw1-ai-service"

cd "$HOME"
"$HOME/hw1ai/bin/python" - <<'PY'
import hashlib
import hw1_ai_service
from hw1_ai_service import config, pipeline

print('package:', hw1_ai_service.__file__)
for module in (config, pipeline):
    path = module.__file__
    digest = hashlib.sha256(open(path, 'rb').read()).hexdigest()
    print(f'{module.__name__}: {path} sha256={digest}')
PY
```

Every printed package path must begin with
`$HOME/hw1-ai-service/hw1_ai_service/`. If it does not, stop there and
fix deployment identity before interpreting a benchmark.

The tracked unit should report `WatchdogUSec=1min` and a nonzero watchdog
timestamp that advances at approximately 30-second intervals. The notifier is
on the Python asyncio loop: a stalled loop causes systemd to kill the entire
service control group and restart it, while correctly off-loop STT/model work
does not interrupt the keep-alive.

## Service lifecycle

```bash
systemctl --user start hw1-ai-service.service
systemctl --user stop hw1-ai-service.service
systemctl --user restart hw1-ai-service.service
systemctl --user is-active hw1-ai-service.service
journalctl --user -u hw1-ai-service.service -n 100 --no-pager
```

Before opening `/dev/ttyAMA2` from a diagnostic script, stop the service and
verify the device has no holder:

```bash
systemctl --user stop hw1-ai-service.service
fuser -v /dev/ttyAMA2
```

Always restart the service when the diagnostic is finished.

## No-camera G2 render diagnostics

The canonical runner is
`$HOME/hw1-ai-service/tools/g2_evenai_probe.py`. It measures all reply
delays from the XIAO's UART command result, not command submission. That result
is **not** a G2 receipt or optical-render event; the fetched protocol log
contains the separate G2 echo and `STREAM_COMPLETE` timestamps. The runner
never requires a camera or video.

After syncing the Mac source, run on the CM5:

```bash
systemctl --user stop hw1-ai-service.service
fuser -v /dev/ttyAMA2

cd "$HOME"
"$HOME/hw1ai/bin/python" \
  "$HOME/hw1-ai-service/tools/g2_evenai_probe.py" \
  preflight
```

Calibrate the fixed 98-character question. The runner asks for five fresh
“Hey Even” wakes and records only `complete` or `cut` at 2.0–4.0 seconds:

```bash
"$HOME/hw1ai/bin/python" \
  "$HOME/hw1-ai-service/tools/g2_evenai_probe.py" \
  ask-threshold
```

Compare the exact same 180-character answer through the one-shot and multipart
render paths. It runs three fresh sessions per mode, alternates order, closes
its own XIAO log, fetches it, and prints the `STREAM_COMPLETE` markers. The
default wait is now 20 seconds because the old 12-second run was shorter than
the conditional CONFIG-80 per-step prediction:

```bash
"$HOME/hw1ai/bin/python" \
  "$HOME/hw1-ai-service/tools/g2_evenai_probe.py" \
  render-ab
```

`render-ab` refuses to reuse an active XIAO system log because its unflushed
tail could omit the terminal marker. Check first; stop it only if it is an old
diagnostic log that no longer needs to remain active:

```bash
"$HOME/hw1ai/bin/python" \
  "$HOME/hw1-ai-service/tools/g2_evenai_probe.py" \
  cmd 'log status'

# Run only when the preceding status says ACTIVE and that old log may be closed.
"$HOME/hw1ai/bin/python" \
  "$HOME/hw1-ai-service/tools/g2_evenai_probe.py" \
  cmd 'log stop'
```

The initial baseline is complete; its stable IDs and numbers were recorded for
one specific firmware/glasses pair and are kept locally. To take the equivalent
baseline on your own hardware, follow
[`docs/investigations/render-timing.md`](docs/investigations/render-timing.md).
Before
rerunning it, isolate the CONFIG regression with the field-only no-camera
80/40/80 matrix:

```bash
"$HOME/hw1ai/bin/python" \
  "$HOME/hw1-ai-service/tools/g2_evenai_probe.py" \
  speed-ab
```

The command uses one fresh wake per value, captures and fetches its own XIAO
log, and correlates every typed CONFIG request by magic value with its echoed
field-2 value, one ANALYSE and REPLY response, one pre-EXIT
`STREAM_COMPLETE`, and no COMM_RSP. It reports both native REPLY-TX-to-
completion and G2-response-to-completion, checks the logged UART reply text and
byte length, and separates structural failures from temple-plugin health
warnings. It sends `g2aiconfig - 80 -`, then `- 40 -`, then `- 80 -`, so voice
and duplex fields are omitted. Power-cycle the glasses after the matrix. Do not
use `restore` as a CONFIG packet: there is no validated protocol-level reset to
the pre-CONFIG renderer state.

```bash
systemctl --user start hw1-ai-service.service
systemctl --user is-active hw1-ai-service.service
```

## Synthetic live-PCM transport probe

This is the Phase 2A transport-only hardware gate. It requires a matching XIAO
image and CM5 source tree, but it does not use PDM, the G2 microphone, the
recorder, Moonshine, the AI pipeline, or the lens. Firmware must advertise
`live-pcm-v1` and `synthetic=1`; current firmware may also advertise the
separate default-off recorder-shadow capability. Any claim that this tests live STT
or real audio is incorrect.

The probe installs its bounded direct frame sink before UART open/login,
acquires one 3-second controller lease, renews once per second, schedules a
deterministic 16 kHz mono S16LE stream, and checks every offset, terminal field,
IEEE CRC32, and sample byte. Ten seconds is deliberate: it crosses multiple
lease-renewal periods while remaining a short first hardware run.

Stop the daemon so exactly one process owns `/dev/ttyAMA2`, then run on the CM5:

```bash
systemctl --user stop hw1-ai-service.service
sleep 2
systemctl --user is-active hw1-ai-service.service || true
fuser -v /dev/ttyAMA2

LIVE_PCM_STAMP="$(date +%Y%m%d-%H%M%S)"
LIVE_PCM_DIR="$HOME/g2-prefx/live-pcm-${LIVE_PCM_STAMP}"
mkdir -p "$LIVE_PCM_DIR"

set -o pipefail
"$HOME/hw1ai/bin/python" \
  "$HOME/hw1-ai-service/tools/live_pcm_transport_probe.py" \
  -c "$HOME/.config/hw1-ai-service/config.yaml" \
  --duration-ms 10000 \
  2>&1 | tee "$LIVE_PCM_DIR/live-pcm-probe.log"
LIVE_PCM_RC=${PIPESTATUS[0]}

systemctl --user start hw1-ai-service.service
systemctl --user is-active hw1-ai-service.service
printf 'probe_rc=%s evidence=%s\n' "$LIVE_PCM_RC" "$LIVE_PCM_DIR"
test "$LIVE_PCM_RC" -eq 0
```

The `fuser` command should print no owner after the stop. Success is both exit
status zero and a final JSON object with `"ok":true`, `"pattern_ok":true`, a
valid `end` terminal, equal expected/received sample counts, zero dropped
samples, and no lease errors. Always restart the service after the probe,
including after a failure.

A success proves the physical synthetic UART framing/lease/receiver path. The
2,048 ms and 10 s happy paths have passed with pattern/CRC and lease renewal.
That alone does **not** satisfy recorder-shadow Gate 2. Physical PDM parity and
the later post-fix exact-owned G2 happy path are now closed. The four controlled
host-overflow/host-gap/host-abort/lease-expire fallbacks are also physically
closed; native admission/correlation, auth/link/TX fallback, and repeated
recorder/BLE/SD/watchdog latency evidence remain open.

## Recorder-shadow PCM/WAV probe

This is the standalone physical gate for the new default-off tee. It never
starts STT, the LLM, or lens delivery. The probe itself requires
`recorder_shadow=1 shadow_default=off`, verifies the selected source and
16 kHz mono S16LE format, acquires/renews the controller lease, arms one exact
exchange, records untrimmed PCM, waits for `active=0 exchange=-` before
`voicefetch`, compares live bytes/CRC/terminal with the canonical WAV, and
deletes only that owner's WAV.

For G2, first wear/tap the glasses awake and keep an active lens container open.
The command below uses `g2show`; without an active container the glasses can
accept `AudioCtrCmd{en=1}` while sending no LC3 notifications. It deliberately
waits three seconds after `openmic`, longer than the 2.048-second AFE ring, so
the post-CAPTURING boundary flush is exercised against a full backlog. The PDM
variant substitutes `pdm` for both source arguments and omits the G2 page/stats
steps.

```bash
(
systemctl --user stop hw1-ai-service.service
sleep 2
systemctl --user is-active hw1-ai-service.service || true
fuser -v /dev/ttyAMA2 || true

set -Eeuo pipefail

PY="$HOME/hw1ai/bin/python"
G2_PROBE="$HOME/hw1-ai-service/tools/g2_evenai_probe.py"
SHADOW_PROBE="$HOME/hw1-ai-service/tools/live_pcm_shadow_probe.py"
CFG="$HOME/.config/hw1-ai-service/config.yaml"

SHADOW_STAMP="$(date +%Y%m%d-%H%M%S)"
SHADOW_DIR="$HOME/g2-prefx/live-pcm-shadow-${SHADOW_STAMP}"
mkdir -p "$SHADOW_DIR"
SHADOW_RC=99

cleanup_shadow_probe() {
  rc=$?
  trap - EXIT
  set +e
  if ! fuser /dev/ttyAMA2 >/dev/null 2>&1; then
    "$PY" "$G2_PROBE" --config "$CFG" cmd \
      'g2micstats' \
      'closemic' \
      'micsource g2' \
      'openmic' \
      'micsource' \
      'micread json' \
      >"$SHADOW_DIR/final-g2-cleanup.log" 2>&1
  fi
  systemctl --user start hw1-ai-service.service
  systemctl --user is-active hw1-ai-service.service \
    >"$SHADOW_DIR/systemd-active-after.log" 2>&1
  printf 'probe_rc=%s shell_rc=%s evidence=%s\n' \
    "$SHADOW_RC" "$rc" "$SHADOW_DIR"
  exit "$rc"
}
trap cleanup_shadow_probe EXIT

"$PY" "$G2_PROBE" --config "$CFG" cmd \
  'g2status' \
  'g2show "G2 MIC SHADOW TEST - KEEP THIS PAGE OPEN"' \
  'g2micreset' \
  'micsource g2' \
  'openmic' \
  'micsource' \
  'micread json'

sleep 3
"$PY" "$G2_PROBE" --config "$CFG" cmd 'g2micstats' \
  >"$SHADOW_DIR/g2micstats-before.log" 2>&1

set -o pipefail
set +e
"$PY" "$SHADOW_PROBE" \
  -c "$CFG" \
  owned \
  --expected-source g2 \
  --record-seconds 6 \
  --output-dir "$SHADOW_DIR" \
  2>&1 | tee "$SHADOW_DIR/probe.log"
SHADOW_RC=${PIPESTATUS[0]}
set -e

test "$SHADOW_RC" -eq 0
)
```

After the stop, `fuser` must show no UART owner. Success requires exit zero,
final JSON `"ok":true`, equal PCM, receiver/terminal parity, and zero live
drops. Exact authority is `{exchange, controller, UART login epoch}`; the epoch
is enforced inside firmware/session TX and is not an added frame field. On G2,
interpret the before/after `g2micstats` mutex-drop and decode-fail counters:
live/WAV equality alone cannot prove upstream LC3 integrity.

For the post-fix G2 happy path, require exact live/WAV equality, valid END
reason 0, zero dropped samples, no shadow-overflow increment, and zero AFE
mutex-drop/decode-fail. Record AFE overrun before and after rather than
requiring final `overrun=0`: the deliberate three-second ring prefill and the
post-stop `voicefetch` window can legitimately increment that cumulative
counter. Any delta must be interpreted alongside exact admitted parity and the
timing interval in which it occurred.

`g2micreset` does **not** empty the decoded AFE ring or reset its overrun count;
it resets raw per-arm counters plus AFE mutex-drop/decode-fail. Starting a fresh
AFE feed resets ring, overrun, mutex-drop, and decode-fail. Because this command
deliberately lets the finite ring fill, a nonzero pre-capture overrun is not by
itself a shadow-parity failure. The cleanup trap always captures final stats and
restores the service, including when the probe exits nonzero.

**Physical status:** PDM passed exact live/WAV parity at 112,640 samples and
CRC32 `2e53eb16`. The first G2 attempt stalled because no lens container was
active. The pre-fix active-page run then ABORTed reason 6 at queue high-water
4/4 while preserving its canonical WAV. The post-fix rerun deliberately
prefilled the same 32,768-sample ring and passed: live, terminal, and WAV were
100,000 samples / 200,000 PCM bytes at CRC32 `56ebd586`; END reason was 0;
dropped, shadow-overflow, host-fault, and late-frame counts were zero; device
queue high-water was 2/4; final `mutex_drop=0 decode_fail=0`; and the service
was restored active with source G2. Cumulative AFE overrun rose from 71 to 607
while the unattended ring refilled during fetch/cleanup, which is expected and
does not contradict admitted parity. The remaining Gate-2 work is native
admission/correlation, repeated/long latency, and auth/link/TX fallback.

The current build is `0x4fbcb0` = 5,225,648 bytes with `0x39350` = 234,320
bytes free (4%), SHA-256
`c306bb476f487df192632b388d193f33045f94b000f74c1a09d1507371f13341`.

Post-native-probe software validation is clean: Python `compileall`; 31 native
shadow tests under `-W error`; and an independent 94-test
EvenAI/cancel/fetch/shadow review under `-W error`. The paced
collector/checker slice passes 31 focused tests under `-W error`, and the
complete current CM5 service suite passes 348 with 1 skipped and 7 subtests
under `-W error`. These software results do not replace any of the physical
PDM/G2/parity/fault evidence recorded above and below.

### Recorder-shadow controlled fault matrix

After syncing the current CM5 service tree, this matrix exercises four
non-destructive live failures. It does not require another XIAO flash and never
starts STT, the LLM, or answer delivery. Each invocation exits zero only when
the requested live failure occurred and the exact owner's independently
fetched WAV remained canonical. The service is restored by the shell trap.

```bash
bash <<'BASH'
set -Eeuo pipefail

PY="$HOME/hw1ai/bin/python"
G2_PROBE="$HOME/hw1-ai-service/tools/g2_evenai_probe.py"
SHADOW_PROBE="$HOME/hw1-ai-service/tools/live_pcm_shadow_probe.py"
CFG="$HOME/.config/hw1-ai-service/config.yaml"
FAULT_ROOT="$HOME/g2-prefx/live-shadow-faults-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$FAULT_ROOT"

cleanup_fault_matrix() {
  rc=$?
  trap - EXIT
  set +e
  if ! fuser /dev/ttyAMA2 >/dev/null 2>&1; then
    "$PY" "$G2_PROBE" --config "$CFG" cmd \
      'g2micstats' 'closemic' 'micsource g2' 'openmic' \
      'micsource' 'micread json' \
      >"$FAULT_ROOT/final-g2-cleanup.log" 2>&1
  fi
  systemctl --user start hw1-ai-service.service
  systemctl --user is-active hw1-ai-service.service \
    >"$FAULT_ROOT/systemd-active-after.log" 2>&1
  printf 'fault_matrix_rc=%s evidence=%s\n' "$rc" "$FAULT_ROOT"
  exit "$rc"
}
trap cleanup_fault_matrix EXIT

systemctl --user stop hw1-ai-service.service
sleep 2
systemctl --user is-active hw1-ai-service.service || true
if fuser /dev/ttyAMA2 >"$FAULT_ROOT/fuser-holder.log" 2>&1; then
  echo 'STOP: /dev/ttyAMA2 still has an owner' >&2
  exit 1
fi

"$PY" "$G2_PROBE" --config "$CFG" cmd \
  'g2status' \
  'g2show "G2 SHADOW FAULT MATRIX - KEEP OPEN"' \
  'g2micreset' 'micsource g2' 'openmic' 'micsource' 'micread json' \
  2>&1 | tee "$FAULT_ROOT/preflight.log"

for FAULT in host-overflow host-gap host-abort lease-expire; do
  RUN_DIR="$FAULT_ROOT/$FAULT"
  mkdir -p "$RUN_DIR"
  "$PY" "$G2_PROBE" --config "$CFG" cmd \
    'g2show "G2 SHADOW FAULT MATRIX - KEEP OPEN"' \
    >"$RUN_DIR/page-refresh.log" 2>&1
  "$PY" "$SHADOW_PROBE" \
    -c "$CFG" \
    owned --expected-source g2 --record-seconds 6 \
    --fault "$FAULT" --fault-after-ms 250 \
    --output-dir "$RUN_DIR" \
    2>&1 | tee "$RUN_DIR/probe.log"
  PROBE_RC=${PIPESTATUS[0]}
  printf '%s\n' "$PROBE_RC" >"$RUN_DIR/exit-code"
  test "$PROBE_RC" -eq 0
done

"$PY" - "$FAULT_ROOT" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
results = []
for path in sorted(root.glob("*/result.json")):
    result = json.loads(path.read_text())
    row = {
        "fault": result["fault"]["kind"],
        "ok": result["ok"],
        "expected": result["fault"]["expected_outcome"],
        "canonical_wav": result["wav"]["canonical"],
        "terminal": result["live"]["terminal"],
        "control_errors": result["control_errors"],
        "lease_errors": result["lease_errors"],
    }
    print(row)
    results.append(row)
assert {row["fault"] for row in results} == {
    "host-overflow", "host-gap", "host-abort", "lease-expire"
}
assert all(row["ok"] and row["expected"] and row["canonical_wav"]
           and not row["control_errors"] and not row["lease_errors"]
           for row in results)
PY

printf '%s\n' "$FAULT_ROOT" \
  | tee "$HOME/g2-prefx/live-shadow-faults-latest.txt"
BASH

systemctl --user is-active hw1-ai-service.service
```

Expected terminal evidence is local invalidation `pcm_queue_overflow` for
`host-overflow`, exact `wire_seq:3!=2` for `host-gap`, ABORT reason 5 for
`host-abort`, and ABORT reason 1 for `lease-expire`. Host-only failures must
still find exact current-exchange `last=end` count/CRC metadata on the XIAO;
ABORT modes must match the canonical WAV prefix count and CRC. In every case
the XIAO WAV is fetched before exact-ID deletion and retained under the printed
Pi evidence directory. Final G2 `mutex_drop` and `decode_fail` must remain zero;
cumulative AFE overrun may rise while the ring is unattended.

**Physical status (2026-08-10): PASS.** All four invocations exited zero with
`ok=true`, the requested failure observed, a canonical owner WAV, no control or
lease errors, and no STT. Host overflow invalidated locally as
`pcm_queue_overflow` while the XIAO retained exact END metadata for exchange
`cd74f8237804041c` (102,400 samples, CRC32 `47b0c4eb`, zero drops). Host gap
invalidated as `wire_seq:3!=2` with exact END for `cb04bb4a07723fad` (100,800
samples, CRC32 `89b23a88`, zero drops). Host ABORT returned reason 5 for
`1c9c3efa35bf8d68`; its admitted 9,600-sample prefix matched the canonical
108,800-sample WAV. Lease expiry returned reason 1 for `5a63d626eb2a2180`;
its admitted 51,200-sample prefix matched the canonical 100,000-sample WAV.
The 256/249 late frames after host overflow/gap were expected traffic for an
already tombstoned stream, not an unexplained inbox fault; both results had
`fault_count=0`. Final G2 integrity counters were
`mutex_drop=0 decode_fail=0`, source G2 was restored idle, and the service was
active. Evidence is retained on the Pi at
`/home/$CM5_USER/g2-prefx/live-shadow-faults-20260810-045912` and in the Mac's
ignored `.scratch/live-shadow-faults-yDWYIwPr` directory.

## Native Hey-Even no-STT recorder-shadow smoke

This is the isolated physical-gate runbook and regression form. It observes one
firmware-owned native G2 capture through the same default-off recorder tee, but it never starts
Moonshine, the LLM, ASK, or REPLY. It must not be described as a live-STT test.
The XIAO firmware is already built/flashed for this grammar; sync only the CM5
service tree before running it.

The probe's `--wake-timeout` is how long the operator has to say `Hey Even`
after the terminal prompt. `--capture-timeout` is the deadline for the question
and native VAD finalization; omit it to use `audio.vad_max_seconds` (normally
15 seconds), or pass an explicit 1..60-second value. Reaching that deadline is
a failed missing-`mic_autostop` gate with bounded exact-ID cleanup. It is not a
successful host-forced stop.

### 1. Sync from the Mac

Run from the Mac. This stops the service before replacing its editable source
and refuses to continue if another process still owns the UART:

```bash
cd $FIRMWARE_ROOT

ssh $CM5_USER@$CM5_HOST '
set -eu
systemctl --user stop hw1-ai-service.service
sleep 2
systemctl --user is-active hw1-ai-service.service || true
if fuser -v /dev/ttyAMA2; then
  echo "STOP: /dev/ttyAMA2 still has an owner" >&2
  exit 1
fi
echo "UART is free"
'

rsync -av --itemize-changes \
  --exclude '.pytest_cache/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  $REPO_ROOT/ai-service/ \
  $CM5_USER@$CM5_HOST:hw1-ai-service/

ssh $CM5_USER@$CM5_HOST
```

### 2. Run on the Pi and perform the one human action

Wear the connected glasses. Paste the entire block on the Pi. Wait until the
terminal prints the exact `NATIVE SHADOW ONLY — SAY 'HEY EVEN'...` prompt; only
then say `Hey Even`, ask one short question, and remain silent for at least two
seconds so native VAD can finalize. The diagnostic may show the native
listening surface, but it sends no transcribed question and no host answer.

```bash
bash <<'BASH'
set -Eeuo pipefail

PY="$HOME/hw1ai/bin/python"
G2_PROBE="$HOME/hw1-ai-service/tools/g2_evenai_probe.py"
SHADOW_PROBE="$HOME/hw1-ai-service/tools/live_pcm_shadow_probe.py"
CFG="$HOME/.config/hw1-ai-service/config.yaml"
NATIVE_ROOT="$HOME/g2-prefx/native-shadow-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$NATIVE_ROOT"
printf '%s\n' "$NATIVE_ROOT" \
  > "$HOME/g2-prefx/native-shadow-latest.txt"
NATIVE_RC=99

cleanup_native_shadow() {
  shell_rc=$?
  trap - EXIT
  set +e
  cleanup_rc=0

  if ! fuser /dev/ttyAMA2 >/dev/null 2>&1; then
    "$PY" "$G2_PROBE" --config "$CFG" cmd \
      'g2evenai status' 'g2micstats' 'closemic' \
      'micsource g2' 'openmic' 'micsource' 'micread json' \
      >"$NATIVE_ROOT/final-g2-cleanup.log" 2>&1 \
      || cleanup_rc=1
  else
    fuser -v /dev/ttyAMA2 \
      >"$NATIVE_ROOT/final-uart-holder-before-service.log" 2>&1
    cleanup_rc=1
  fi

  systemctl --user start hw1-ai-service.service || cleanup_rc=1
  sleep 3
  systemctl --user is-active hw1-ai-service.service \
    >"$NATIVE_ROOT/systemd-active-after.log" 2>&1 \
    || cleanup_rc=1

  uart_restored=0
  for _ in 1 2 3 4 5; do
    if fuser -v /dev/ttyAMA2 \
        >"$NATIVE_ROOT/uart-holder-after.log" 2>&1; then
      uart_restored=1
      break
    fi
    sleep 1
  done
  test "$uart_restored" -eq 1 || cleanup_rc=1

  if [ "$shell_rc" -eq 0 ] && [ "$cleanup_rc" -ne 0 ]; then
    shell_rc=$cleanup_rc
  fi
  printf 'native_probe_rc=%s shell_rc=%s cleanup_rc=%s evidence=%s\n' \
    "$NATIVE_RC" "$shell_rc" "$cleanup_rc" "$NATIVE_ROOT"
  exit "$shell_rc"
}
trap cleanup_native_shadow EXIT

systemctl --user stop hw1-ai-service.service
sleep 2
systemctl --user is-active hw1-ai-service.service || true
if fuser /dev/ttyAMA2 >"$NATIVE_ROOT/fuser-holder.log" 2>&1; then
  echo 'STOP: /dev/ttyAMA2 still has an owner' >&2
  exit 1
fi

"$PY" "$G2_PROBE" --config "$CFG" cmd \
  'g2status' \
  'g2evenai status' \
  'g2show "NATIVE SHADOW SMOKE - WAIT FOR TERMINAL PROMPT"' \
  'g2micreset' 'micsource g2' 'openmic' 'micsource' 'micread json' \
  2>&1 | tee "$NATIVE_ROOT/preflight.log"

grep -Fq 'state=connected L=up R=up' "$NATIVE_ROOT/preflight.log"
grep -Fq 'EvenAI session: idle id=-' "$NATIVE_ROOT/preflight.log"
grep -Fq 'preference=g2, active=g2' "$NATIVE_ROOT/preflight.log"
grep -Fq '"recording":false' "$NATIVE_ROOT/preflight.log"
grep -Fq '"source":"g2"' "$NATIVE_ROOT/preflight.log"
grep -Fq '"sampleRate":16000' "$NATIVE_ROOT/preflight.log"
grep -Fq '"bitDepth":16' "$NATIVE_ROOT/preflight.log"
grep -Fq '"channels":1' "$NATIVE_ROOT/preflight.log"

sleep 3
"$PY" "$G2_PROBE" --config "$CFG" cmd 'g2micstats' \
  >"$NATIVE_ROOT/g2micstats-before.log" 2>&1

set +e
"$PY" "$SHADOW_PROBE" \
  -c "$CFG" \
  native --wake-timeout 30 \
  --output-dir "$NATIVE_ROOT" \
  2>&1 | tee "$NATIVE_ROOT/probe.log"
NATIVE_RC=${PIPESTATUS[0]}
set -e
printf '%s\n' "$NATIVE_RC" >"$NATIVE_ROOT/exit-code"
test "$NATIVE_RC" -eq 0

"$PY" - "$NATIVE_ROOT/result.json" <<'PY'
import json
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
r = json.loads(path.read_text())
eid = r["exchange_id"]
assert re.fullmatch(r"[0-9a-f]{16}", eid)
assert int(eid[:8], 16) and int(eid[8:], 16)
assert r["schema"] == 1
assert r["mode"] == "native_recorder_shadow_smoke"
assert r["ok"] is True
assert r["stt_started"] is False
assert r["llm_started"] is False
assert r["ask_sent"] is False
assert r["reply_sent"] is False
assert r["expected_source"] == "g2"
assert r["session_epoch"] > 0
assert r["begin"]["exchange_id"] == eid
assert r["begin"]["controller_id"] == r["controller_id"]
assert r["begin"]["synthetic"] is False
assert r["begin"]["source"] == 2
assert r["begin"]["sample_rate"] == 16000
assert r["native"]["preflight"]["state"] == "idle"
assert r["native"]["preflight"]["exchange_id"] == "-"
assert r["native"]["active"]["state"] == "active"
assert r["native"]["active"]["exchange_id"] == eid
assert r["native"]["active"]["uart_epoch"] == r["session_epoch"]
assert r["native"]["idle_after_exit"]["state"] == "idle"
assert r["native"]["idle_after_exit"]["exchange_id"] == "-"
assert r["native"]["wake"] == f"evenai_wake {eid}"
assert r["native"]["mic_autostop"] == r["device_path"]
assert pathlib.PurePosixPath(r["device_path"]).name == f"rec_{eid}.wav"

terminal = r["live"]["terminal"]
assert terminal["kind"] == "end"
assert terminal["valid"] is True
assert terminal["reason"] == 0
assert terminal["dropped_samples"] == 0
assert terminal["total_samples"] > 0
assert r["live"]["samples"] == terminal["total_samples"]
assert r["live"]["bytes"] == terminal["total_samples"] * 2
assert r["live"]["crc32"] == terminal["crc32"]
assert r["live"]["status_matches_terminal"] is True
assert r["live"]["stream"]["received_samples"] == terminal["total_samples"]
assert r["live"]["stream"]["pcm_crc32"] == int(terminal["crc32"], 16)
assert r["live"]["inbox"]["fault_count"] == 0
assert r["live"]["inbox"]["late_frame_count"] == 0
assert r["live"]["inbox"]["last_fault"] is None
assert r["wav"]["canonical"] is True
assert r["live"]["samples"] > 0
assert r["wav"]["samples"] > 0
assert r["parity"] == {
    "applicable": False,
    "reason": "native_capture_trim_enabled",
    "pcm_equal": None,
}
assert r["quiescence"]["active"] == "0"
assert r["quiescence"]["exchange"] == "-"
assert r["lease_errors"] == []
assert r["control_errors"] == []
assert r["cleanup_order"] == [
    "shadow_off", "lease_release", "voicefetch",
    "micdeleteid", "g2evenai_exitid",
]
assert r["cleanup"] == {
    "wav_deleted": True,
    "evenai_exited": True,
    "shadow_disarmed": True,
    "lease_released": True,
}
events = [entry["text"] for entry in r["native"]["events"]]
assert f"evenai_wake {eid}" in events
assert f"mic_autostop {eid} {r['device_path']}" in events
for artifact in ("live_pcm", "wav", "result"):
    assert pathlib.Path(r["local_paths"][artifact]).is_file()

print({
    "ok": r["ok"],
    "exchange_id": eid,
    "live_samples": r["live"]["samples"],
    "wav_samples": r["wav"]["samples"],
    "parity": r["parity"],
    "cleanup": r["cleanup"],
})
PY
BASH

systemctl --user is-active hw1-ai-service.service
```

Do not impose an event-order assertion: firmware may emit LIVE BEGIN before
`evenai_wake`, and LIVE END before `mic_autostop`. Correlation is by the exact
exchange/controller/epoch/path fields, not arrival order. Native VAD trimming
also means the pre-trim live stream and canonical trimmed WAV need not have the
same sample count or bytes. Equality is allowed if trimming retains everything;
neither equality nor a particular count ordering is this gate. A cleanup
`evenai_cancel <id> host_exit` is accepted and usually observed, but it is an
unacknowledged advisory event and therefore is not required for success.

The result contains additional diagnostics, but its required shape is:

```json
{
  "schema": 1,
  "mode": "native_recorder_shadow_smoke",
  "ok": true,
  "stt_started": false,
  "llm_started": false,
  "ask_sent": false,
  "reply_sent": false,
  "expected_source": "g2",
  "parity": {
    "applicable": false,
    "reason": "native_capture_trim_enabled",
    "pcm_equal": null
  },
  "lease_errors": [],
  "control_errors": [],
  "cleanup": {
    "wav_deleted": true,
    "evenai_exited": true,
    "shadow_disarmed": true,
    "lease_released": true
  }
}
```

### 3. Acceptance matrix

| Boundary | Required evidence |
|---|---|
| Safe preflight | Service stopped; no UART owner; both glasses arms up; EvenAI idle; source G2, enabled/connected, idle, 16 kHz mono S16 |
| Native identity | One nonzero 16-hex exchange matches `evenai_wake`, non-synthetic G2 LIVE BEGIN, active `g2evenai status`, current UART login epoch, `mic_autostop`, and exact `rec_<exchange>.wav` |
| Live integrity | Valid END reason 0; positive exact count and CRC across terminal, receiver snapshot, and firmware `last_*`; zero drops, inbox faults, and late frames; final `active=0 exchange=-` |
| WAV policy | Independently fetched WAV is canonical and nonempty; `parity.applicable=false` solely because native capture trimming is enabled |
| No production work | `stt_started`, `llm_started`, `ask_sent`, and `reply_sent` are all false |
| Exact cleanup | Order is shadow off, lease release, WAV fetch, exact-ID delete, exact-ID EXIT; all four cleanup booleans are true and native state returns idle |
| Operational restore | Final G2 `mutex_drop=0 decode_fail=0`; source G2 is restored idle; service is active; post-start `fuser` records its UART holder. Cumulative AFE `overrun` may be nonzero |

Any failed assertion keeps this gate **unrun/failed**; do not advance to a
streaming-STT worker from a partial result. The evidence directory contains the
wearer's speech PCM/WAV. Keep it under the ignored Pi evidence tree and Mac
`.scratch`; never copy it into `docs2/` or commit it.

### 4. Pull and hash the evidence on the Mac

After the Pi block restores the service, exit SSH and run:

```bash
cd $FIRMWARE_ROOT
REPO=$FIRMWARE_ROOT
mkdir -p "$REPO/.scratch"

NATIVE_REMOTE="$(ssh $CM5_USER@$CM5_HOST '
  path=$(cat "$HOME/g2-prefx/native-shadow-latest.txt")
  case "$path" in
    "$HOME"/g2-prefx/native-shadow-*) printf "%s\n" "${path#"$HOME"/}" ;;
    *) echo "Unexpected evidence path: $path" >&2; exit 1 ;;
  esac
')"
case "$NATIVE_REMOTE" in
  g2-prefx/native-shadow-*) ;;
  *) echo "Unexpected evidence path: $NATIVE_REMOTE" >&2; false ;;
esac

NATIVE_LOCAL="$(mktemp -d "$REPO/.scratch/native-shadow-XXXXXXXX")"
rsync -av \
  "$CM5_USER@$CM5_HOST:${NATIVE_REMOTE}/" \
  "$NATIVE_LOCAL/"

ln -sfn "$NATIVE_LOCAL" "$REPO/.scratch/native-shadow-latest"
find "$NATIVE_LOCAL" -type f ! -name SHA256SUMS \
  -exec shasum -a 256 {} + \
  >"$NATIVE_LOCAL/SHA256SUMS"
find "$NATIVE_LOCAL" -maxdepth 2 -type f -print
printf 'Evidence: %s\n' "$NATIVE_LOCAL"
```

### 5. Accepted physical result — 2026-08-10

One physical run satisfies the matrix above. The accepted result used
controller `05dae575e2e7a154`, firmware exchange `6bda87ea00000002`, and UART
session epoch 19. Its non-synthetic G2 LIVE BEGIN, native wake/active state,
`mic_autostop`, recorder path, terminal/status, and cleanup all correlated to
that exact exchange. LIVE END was valid reason 0 at 46,400 samples / CRC32
`931acca0`, with zero dropped samples, inbox faults, and late frames. The
independently fetched canonical trimmed WAV was 35,200 samples / CRC32
`82c81ade`; the result correctly reported
`parity.reason=native_capture_trim_enabled` rather than requiring equality.

The same result recorded STT, LLM, ASK, and REPLY as false; empty lease/control
error lists; exact shadow-off/release/fetch/delete/EXIT cleanup with all four
cleanup booleans true; and final live/native idle state. Cleanup then recorded
G2 `mutex_drop=0 decode_fail=0` (cumulative AFE `overrun=515`), restored the G2
source idle at 16 kHz mono S16, and restored the active service as the UART
owner. The ignored evidence is retained at `.scratch/native-shadow-latest`;
the PCM/WAV and any wearer speech must remain untracked and out of `docs2/`.

This closes one native admission/provenance smoke only. It does not establish
STT accuracy, latency percentiles, repeated reliability, production streaming,
or the remaining auth/link/TX fault cases. Recorder shadow remains absent from
daemon/YAML defaults, and production streaming STT remains disabled.

## Real-time-paced Moonshine replay

The replay probe does not use the UART, XIAO, or G2. It feeds saved XIAO WAVs
at the recorder's real 4096-byte / 128 ms cadence through a bounded
eight-chunk / 32 KiB / 1.024-second Pi worker queue and the installed Moonshine
streaming API. It refuses an ambiguous model
alias, a non-performance governor, and accidental output overwrite. The
separate checker re-hashes the JSONL's WAV and sidecar paths, so run it on the
Pi before pulling artifacts.

The run is intentionally only a guarded **provisional deployed-medium mixed
slice**: trusted speech pairs 001, 002, and 005 plus human-audited no-speech
static controls neg001 through neg004; exact cached
medium-streaming model; 0.5-second update floor; default pace 1.0; default
eight-chunk queue; and the default post-stream batch baseline. Do not run the
1.0-second cadence, other models, or an unlabeled file until this first result
has been reviewed.

Sync the current `ai-service/` tree first so the collector, checker, and bundled
manifest are the same revision. Then paste this entire block on the Pi. The
EXIT trap restores the prior power profile and then verifies both service and
UART ownership even when collection or grading fails:

```bash
bash <<'BASH'
set -Eeuo pipefail

PY="$HOME/hw1ai/bin/python"
COLLECTOR="$HOME/hw1-ai-service/tools/moonshine_stream_replay.py"
CHECKER="$HOME/hw1-ai-service/tools/moonshine_stream_replay_check.py"
MANIFEST_SOURCE="$HOME/hw1-ai-service/tools/moonshine_gate0a_medium_slice.json"
CORPUS="$HOME/stt-corpus"
MODEL="$HOME/.cache/moonshine_voice/download.moonshine.ai/model/medium-streaming-en/quantized_26_07_30"
POWER_HELPER=/usr/local/libexec/hw1-power-helper
UART=/dev/ttyAMA2
RESULT_ROOT="$HOME/stt-results"

mkdir -p "$RESULT_ROOT"
OUT="$(mktemp -d \
  "$RESULT_ROOT/gate0a-medium-0500ms-$(date +%Y%m%d-%H%M%S)-XXXXXXXX")"
printf '%s\n' "$OUT" \
  > "$RESULT_ROOT/gate0a-medium-0500ms-latest.txt"
JSONL=$OUT/medium-0500ms.jsonl
REPORT=$OUT/medium-0500ms-check.json
RUN_MANIFEST=$OUT/canonical-manifest.json

PREVIOUS_PROFILE=
POWER_CHANGED=0
SERVICE_WAS_ACTIVE=0
COLLECTOR_RC=99
CHECKER_RC=99

uart_matches_mainpid() {
  local expected="$1" holders
  local -a pids=()
  holders="$(fuser "$UART" 2>/dev/null || true)"
  read -r -a pids <<< "$holders" || true
  [[ "$expected" =~ ^[1-9][0-9]*$ ]] \
    && (( ${#pids[@]} == 1 )) \
    && [[ "${pids[0]}" == "$expected" ]]
}

restore_replay_service() {
  shell_rc=$?
  trap - EXIT
  set +e
  restore_rc=0

  if [ "$POWER_CHANGED" -eq 1 ]; then
    sudo -n "$POWER_HELPER" profile "$PREVIOUS_PROFILE" \
      >"$OUT/power-restore.log" 2>&1 || restore_rc=1
  fi

  service_restored=0
  uart_restored=0
  if [ "$SERVICE_WAS_ACTIVE" -eq 1 ]; then
    systemctl --user start hw1-ai-service.service \
      >"$OUT/service-start.log" 2>&1 || restore_rc=1
    for _ in $(seq 1 30); do
      if systemctl --user is-active --quiet hw1-ai-service.service; then
        main_pid="$(systemctl --user show hw1-ai-service.service \
          -p MainPID --value 2>/dev/null)"
        if [ "${main_pid:-0}" -gt 1 ] 2>/dev/null; then
          service_restored=1
          uart_matches_mainpid "$main_pid" && uart_restored=1
        fi
      fi
      if [ "$service_restored" -eq 1 ] && [ "$uart_restored" -eq 1 ]; then
        break
      fi
      sleep 1
    done
    test "$service_restored" -eq 1 || restore_rc=1
    test "$uart_restored" -eq 1 || restore_rc=1
  fi

  systemctl --user is-active hw1-ai-service.service \
    >"$OUT/service-active-after.log" 2>&1 || true
  systemctl --user --no-pager -l status hw1-ai-service.service \
    >"$OUT/service-status-after.log" 2>&1 || true
  fuser -v "$UART" >"$OUT/uart-holder-after.log" 2>&1 || true

  if [ "$shell_rc" -eq 0 ] && [ "$restore_rc" -ne 0 ]; then
    shell_rc=$restore_rc
  fi
  printf 'collector_rc=%s checker_rc=%s shell_rc=%s restore_rc=%s evidence=%s\n' \
    "$COLLECTOR_RC" "$CHECKER_RC" "$shell_rc" "$restore_rc" "$OUT"
  exit "$shell_rc"
}

trap restore_replay_service EXIT

systemctl --user is-active hw1-ai-service.service \
  | tee "$OUT/service-active-before.log"
grep -Fxq active "$OUT/service-active-before.log"
SERVICE_WAS_ACTIVE=1
main_pid_before="$(systemctl --user show hw1-ai-service.service \
  -p MainPID --value)"
fuser -v "$UART" >"$OUT/uart-holder-before-stop.log" 2>&1 || true
uart_matches_mainpid "$main_pid_before" || {
  echo "STOP: UART holder is not exactly service MainPID $main_pid_before" >&2
  exit 1
}

systemctl --user stop hw1-ai-service.service
for _ in $(seq 1 20); do
  state="$(systemctl --user show hw1-ai-service.service \
    -p ActiveState --value)"
  [ "$state" = inactive ] && break
  sleep 1
done
printf '%s\n' "$state" > "$OUT/service-state-after-stop.log"
test "$state" = inactive

# Match only the installed daemon entry point or the real llama executable.
# A broad "hw1-ai-service" match also sees this repository/runner path.
if pgrep -af '[/]hw1ai/bin/hw1-ai-service|[/]llama-server([[:space:]]|$)' \
    >"$OUT/processes-after-stop.log"; then
  echo 'STOP: competing AI process is still running' >&2
  cat "$OUT/processes-after-stop.log" >&2
  exit 1
fi
if fuser -v "$UART" >"$OUT/uart-holder-before.log" 2>&1; then
  echo "STOP: $UART still has an owner" >&2
  cat "$OUT/uart-holder-before.log" >&2
  exit 1
fi

test -x "$PY"
test -f "$COLLECTOR"
test -f "$CHECKER"
test -f "$MANIFEST_SOURCE"
test -x "$POWER_HELPER"
test -d "$MODEL"
for stem in 001 002 005 neg001 neg002 neg003 neg004; do
  test -f "$CORPUS/$stem.wav"
  test -f "$CORPUS/$stem.txt"
done

cp -- "$MANIFEST_SOURCE" "$RUN_MANIFEST"
sha256sum "$COLLECTOR" "$CHECKER" "$MANIFEST_SOURCE" "$RUN_MANIFEST" \
  > "$OUT/tool-sha256.txt"

sudo -n "$POWER_HELPER" status | tee "$OUT/power-before.json"
PREVIOUS_PROFILE="$("$PY" - "$OUT/power-before.json" <<'PY_PROFILE'
import json
import pathlib
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_text())["profile"]
if value not in {"eco", "balanced", "performance"}:
    raise SystemExit(f"unsafe/unknown prior profile: {value!r}")
print(value)
PY_PROFILE
)"

POWER_CHANGED=1
sudo -n "$POWER_HELPER" profile performance \
  | tee "$OUT/power-profile.log"
for governor in /sys/devices/system/cpu/cpufreq/policy*/scaling_governor; do
  grep -Fxq performance "$governor"
done
vcgencmd get_throttled | tee "$OUT/throttled-before.txt"
grep -Fxq 'throttled=0x0' "$OUT/throttled-before.txt"

set +e
"$PY" "$COLLECTOR" \
  "$CORPUS/001.wav" \
  "$CORPUS/002.wav" \
  "$CORPUS/005.wav" \
  "$CORPUS/neg001.wav" \
  "$CORPUS/neg002.wav" \
  "$CORPUS/neg003.wav" \
  "$CORPUS/neg004.wav" \
  --model-dir "$MODEL" \
  --model-arch medium-streaming \
  --update-interval 0.5 \
  --queue-chunks 8 \
  --text-queue-events 64 \
  --pace 1.0 \
  --output "$JSONL" \
  2>&1 | tee "$OUT/collector.log"
pipe_rc=("${PIPESTATUS[@]}")
COLLECTOR_RC="${pipe_rc[0]}"
if (( pipe_rc[1] != 0 && COLLECTOR_RC == 0 )); then
  COLLECTOR_RC="${pipe_rc[1]}"
fi
set -e

vcgencmd get_throttled | tee "$OUT/throttled-after.txt"

if [ -s "$JSONL" ]; then
  set +e
  "$PY" "$CHECKER" \
    "$JSONL" \
    --manifest "$RUN_MANIFEST" \
    --expected-update-interval 0.5 \
    --expected-model-dir "$MODEL" \
    --throttled-before "$OUT/throttled-before.txt" \
    --throttled-after "$OUT/throttled-after.txt" \
    --output "$REPORT" \
    2>&1 | tee "$OUT/checker.log"
  pipe_rc=("${PIPESTATUS[@]}")
  CHECKER_RC="${pipe_rc[0]}"
  if (( pipe_rc[1] != 0 && CHECKER_RC == 0 )); then
    CHECKER_RC="${pipe_rc[1]}"
  fi
  set -e
else
  echo 'Collector produced no JSONL evidence.' | tee "$OUT/checker.log"
fi

if [ "$CHECKER_RC" -eq 0 ]; then
"$PY" - "$REPORT" <<'PY'
import json
import pathlib
import sys

report = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert report["schema"] == 1
assert report["type"] == "moonshine_replay_gate_report"
assert report["scope"] == "provisional_deployed_medium_mixed_slice"
assert report["canonical_manifest"] is True
assert report["ok"] is True
assert report["full_gate0a_complete"] is False
assert report["failure_reasons"] == []
assert "manifest_is_provisional_not_full_gate0a" in report["warnings"]
assert report["manifest"]["name"] == \
    "hardwareone-gate0a-medium-mixed-slice-v3"
assert report["manifest"]["contract_sha256"] == \
    "7243e7a7051b09d4d5264ee93496674eb63dd5e09a64676f1f1952d7a4e1a53d"
policy = report["effective_policy"]
assert policy["allowed_update_intervals_seconds"] == [0.5, 1.0]
assert policy["audio_queue_chunks"] == 8
assert policy["text_queue_events"] == 64
assert policy["max_stream_start_seconds"] == 2.0
assert policy["max_stream_wall_over_audio_seconds"] == 2.0
assert policy["max_aggregate_wer"] == 0.2
assert policy["max_end_to_final_seconds"] == 0.8
assert policy["max_partial_gap_seconds"] == 1.35
assert policy["max_queue_age_ms"] == 1024.0
assert policy["max_temperature_c"] == 80.0
assert policy["min_partial_updates_per_second"] == 1.0
assert policy["partial_post_end_tolerance_seconds"] == 0.35
assert report["update_interval_s"] == 0.5
assert report["throttled_before"] == "0x0"
assert report["throttled_after"] == "0x0"
assert set(report["cases"]) == {
    "001", "002", "005", "neg001", "neg002", "neg003", "neg004",
}
assert report["aggregate"]["negative_controls"] == 4
assert report["aggregate"]["negative_stream_hallucinations"] == 0
assert report["aggregate"]["negative_batch_hallucinations"] == 0
assert set(report["evidence"]) == {
    "jsonl", "manifest", "throttled_before", "throttled_after", "checker",
}
print({
    "ok": report["ok"],
    "scope": report["scope"],
    "full_gate0a_complete": report["full_gate0a_complete"],
    "failure_reasons": report["failure_reasons"],
})
PY
fi

test "$COLLECTOR_RC" -eq 0
test "$CHECKER_RC" -eq 0
BASH
```

The checker exits nonzero on a failed report. The canonical manifest pins its
own semantic contract hash, exact model directory/architecture/enum, policy, corpus
directory/hashes/counts, Moonshine 0.1.1 runtime, and per-case absolute error
ceilings. The checker independently
recomputes stream/batch WER from the pinned sidecars, verifies one run/schema
and complete PCM/event topology, requires the exact 8-audio/64-text queue
shape, bounds stream start and wall-over-audio at 2 seconds, rejects accuracy
regression and any absolute or aggregate WER above policy, measures partial
timing coverage, and requires END-to-final at most 0.8 seconds, queue age at
most 1024 ms with no drops/overflow, performance governors, temperature at most
80 C, no swap
excursion, and zero before/after throttle bits. CLI threshold options may only
tighten the manifest; the canonical block above intentionally supplies none.
It also records hashes for the JSONL, manifest, throttle files, and checker.

The prior four-chunk v1 and three-positive v2 physical evidence is intentionally
not accepted by this v3 mixed-corpus contract. On clean power, medium/0.5
processed every input chunk but
reached 507.6 ms queue age and a 639.0 ms native pass; medium/1.0 reached
582.5 ms, overflowed, and processed 20/33 chunks of case 005. Because the
worker drained queued PCM immediately after each synchronous pass, v3 keeps
eight chunks as a bounded jitter buffer rather than dropping speech. A v2
physical rerun proved the eight-slot queue. The later v3 mixed-corpus runs also
preserved every chunk: 0.5 s reached 4/8 and 510 ms max queue age; 1.0 s reached
5/8 and 711 ms. Streaming positive errors were 2/26 and 1/26 respectively,
versus batch 2/26 at both cadences. Maximum END-to-final was 0.459 s and 0.712 s.
Streaming returned empty for all four static controls, but batch returned
`Yeah.` on neg001 at both cadences. The reports therefore correctly returned
nonzero for accuracy/partial/no-speech policy findings even though runtime,
power, queue, and evidence integrity passed. Retained local evidence:
`.scratch/gate0a-v3-results-Pn5hEAy0/{0500,1000}`.

Even if every assertion passes, the report always sets
`full_gate0a_complete=false`. The bundled manifest has three positive cases and
four static/no-speech controls, but is still insufficient for model selection
or a robust p95.
Do not score `004.wav`; its spoken prompt is unknown. Repair/expand the corpus
with more speech/noise conditions before the full cadence/model matrix.

## Native Hey-Even live-STT shadow (no LLM or display mutation)

This is the next gate after the v3 replay. It is still a standalone diagnostic:
the daemon does not advertise a live lease, the production pipeline remains
batch-only, and no partial/final transcript is sent to the glasses or LLM.

The preferred physical runner is
`$HOME/hw1-ai-service/tools/run_native_live_stt_gate.sh`. It owns the
service/UART/power/G2 preflight and EXIT restoration, validates `result.json`,
and writes the latest evidence path to
`$HOME/g2-prefx/native-live-stt-latest.txt`. Sync the complete
`cm5/ai-service/` tree before invoking it.

The first 2026-08-10 invocation stopped before the probe with `probe_rc=99`
and restored the service successfully. Its broad competing-process guard had
matched the runner's own `/home/$CM5_USER/hw1-ai-service/...` pathname. The guard
now matches only `/home/$CM5_USER/hw1ai/bin/hw1-ai-service` and the actual
`llama-server` executable; this aborted preflight is not streaming-STT
hardware evidence.

The corrected invocation created
`/home/$CM5_USER/g2-prefx/native-live-stt-20260810-095106-sbJW7l5d` and exercised
the complete native path. Live transport ended validly with 55,200 samples,
CRC32 `1cd5979a`, zero drops/faults, and every 110,400 PCM byte processed by
Moonshine. The eight-slot queue reached 3/8 and 537.6 ms; END-to-final was
51.0 ms; the canonical trimmed WAV held 44,800 samples; throttle remained
`0x0`; and exact cleanup reported success. The gate result was still nonzero:
the final `Haitian is the capital difference.` had 3 word errors against `what
is the capital of france`. Preserve this as a transport/queue success and an
STT-accuracy failure. Do not relax the final timeout to reinterpret it—the
wrong final was already available 51 ms after END.
The pulled canonical WAV SHA256 is
`35efadca3600799e2b4c05af7954712f9f4fdbf4d1743dca54d188bb707a0615`.
It is an exact byte slice from live sample zero with 10,400 live tail samples
(0.65 s) removed by native trim. The 2.8-second WAV measured -36.9 dBFS mean,
-17.7 dBFS peak; active speech windows were roughly -31 to -34 dBFS over a
roughly -56 dBFS quiet floor. This is quiet but not clipped or transport-
corrupted. However, its source clock is anomalous: 69 decoded 800-sample G2
packets span 6.903 seconds of live wall time, only 9.995 packets/s and
7.996 ksamples/s under a 16 kHz label. The prior native no-STT result delivered
58 packets in 2.895 seconds (20.04 packets/s / 16.03 ksamples/s). First compare
normal, 8 kHz-reinterpreted, and half-tempo listening derivatives; if neither
correction is natural, capture the raw 205-byte notifications and decode them
under both 10 ms and 20 ms LC3 hypotheses. Do that before changing Moonshine
timing or queue policy.

Run with the service stopped, UART free, G2 source active, and the power profile
temporarily set to `performance`, using the same cleanup/restoration trap as the
native no-STT smoke above. The probe itself now enforces performance governors.
Use a unique evidence directory and ask exactly the pinned phrase:

```bash
PY="$HOME/hw1ai/bin/python"
SERVICE_ROOT="$HOME/hw1-ai-service"
MODEL="$HOME/.cache/moonshine_voice/download.moonshine.ai/model/medium-streaming-en/quantized_26_07_30"
OUT="$(mktemp -d "$HOME/g2-prefx/native-live-stt-$(date +%Y%m%d-%H%M%S)-XXXXXXXX")"

PYTHONPATH="$SERVICE_ROOT" "$PY" \
  "$SERVICE_ROOT/tools/live_pcm_shadow_probe.py" \
  -c "$HOME/.config/hw1-ai-service/config.yaml" \
  native-stt \
  --model-dir "$MODEL" \
  --model-arch medium-streaming \
  --update-interval 1.0 \
  --stt-queue-chunks 8 \
  --stt-soft-final-target 0.8 \
  --stt-final-timeout 2.0 \
  --expected-text "what is the capital of france" \
  --output-dir "$OUT" | tee "$OUT/probe.log"
```

After the prompt, say “Hey Even,” ask exactly “what is the capital of France,”
then remain silent. Exit status 0 requires all of the following in
`$OUT/result.json`:

- `mode == "native_live_stt_shadow"`, `ok == true`, and
  `stt_started == true`;
- `llm_started == false`, `ask_sent == false`, and `reply_sent == false`;
- valid live END reason 0 with zero drops and clean inbox/status identity;
- `streaming_stt.valid == true`, `accuracy.word_errors == 0`, and all offered
  PCM bytes enqueued and processed;
- queue capacity 8 / 32768 bytes / 1024 ms with no overflow;
- hard final timeout 2.0 s recorded; the 0.8 s soft target is reported but is
  not itself a kill switch;
- canonical nonempty WAV, native parity explicitly N/A because trim is enabled,
  exact cleanup order, and idle live/native state afterward.

A valid empty streaming result is semantically different from a model timeout,
missing `stop()` result, queue overflow, or transport failure. Do not turn this
gate into an RMS/peak threshold or a blacklist for `Yeah`; the v3 evidence shows
why those shortcuts are unsafe. On any STT failure the recorder/live collector
continues draining and exact WAV cleanup remains authoritative.

## Copy diagnostic results back to the Mac

Keep pulled evidence under the repository's ignored `.scratch` directory, not
in source or `docs2`:

```bash
mkdir -p \
  $FIRMWARE_ROOT/.scratch/cm5-diagnostics

rsync -av \
  $CM5_USER@$CM5_HOST:g2-prefx/ \
  $FIRMWARE_ROOT/.scratch/cm5-diagnostics/g2-prefx/

rsync -av \
  $CM5_USER@$CM5_HOST:stt-results/ \
  $FIRMWARE_ROOT/.scratch/cm5-diagnostics/stt-results/
```

For a separate, timestamped pull of only the G2 speed-matrix artifacts, define
the timestamp **in the same Mac shell that uses it**:

```bash
G2_PULL_STAMP="$(date +%Y%m%d-%H%M%S)"
G2_PULL_DIR="$FIRMWARE_ROOT/.scratch/g2-speed-ab-${G2_PULL_STAMP}"

mkdir -p "$G2_PULL_DIR"

rsync -av \
  --include='evenai-speed-ab-*.log' \
  --include='speed-ab-*.log' \
  --exclude='*' \
  $CM5_USER@$CM5_HOST:g2-prefx/ \
  "$G2_PULL_DIR/"

find "$G2_PULL_DIR" -maxdepth 1 -type f -print
```

Shell variables do not cross an SSH boundary or survive a new terminal unless
they are defined again. A Pi-side `G2_TEST_STAMP` therefore must not be reused
implicitly by a later Mac command. If it is unset, `${G2_TEST_STAMP}` expands
to an empty string and produces a harmless but confusing directory such as
`.scratch/g2-speed-ab-`.

## Benchmark rules

1. Run Python from `$HOME`, never from a source directory.
2. Use `$HOME/hw1ai/bin/python` or the matching console script.
3. Stop the service and confirm no `llama-server` remains before loading a
   second model.
4. Record the governor, current frequency, free memory/swap, temperature, and
   `vcgencmd get_throttled` before and after the run.
5. Explicitly select and verify the intended power profile. A stopped daemon
   leaves whichever governor was last active; standalone benchmark programs do
   not invoke the service's automatic performance promotion.
6. Restore the prior power policy and restart the service afterward.

## Evidence bundles

Copy `$HOME/hw1-ai-service`, not `$HOME/ai-service`. Also capture:

- the installed systemd unit and `systemctl show ... -p ExecStart`;
- the console-script shebang and the import/hash verification above;
- the effective config with credentials excluded;
- the relevant service journal;
- governor/frequency, memory/swap, temperature, and throttle state;
- package versions and the exact Moonshine model directory.

Never place credentials, passwords, or private device captures in tracked
documentation.
