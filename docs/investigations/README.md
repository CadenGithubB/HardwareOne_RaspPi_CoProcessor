# Investigation runbooks

Reusable procedures for diagnosing a voice/AI co-processor link, distilled from
a series of real investigations on one particular rig. The findings from those
sessions were specific to that hardware and are not in this repository; what is
kept here is the *method*, parameterized so it runs against whatever
combination of board, transport, engine and display you actually have.

Each runbook states the question it answers, the profile fields it needs, the
procedure, and — importantly — how to read a result that does *not* settle the
question.

| Runbook | Answers |
| --- | --- |
| [link-triage.md](link-triage.md) | Is the audio *late* or *lost*? |
| [audio-parity.md](audio-parity.md) | Did the bytes that reached the host equal the bytes the device captured? |
| [stt-benchmark.md](stt-benchmark.md) | Which engine/cadence, and does it hallucinate on silence? |
| [llm-serving.md](llm-serving.md) | Tokens/s, time-to-first-token, and does it hold under thermal load? |
| [render-timing.md](render-timing.md) | When did the wearer/viewer actually see the text? |
| [`uart-baud-test/`](../../uart-baud-test/README.md) | What is the highest link rate this board pair sustains with zero corruption? |

The last one is different in kind: it is not a procedure to follow but a
harness to **run** — a controller script plus a standalone ESP-IDF test
firmware, living outside this directory because it is code, not prose. It is
worth reading even if you never run it, because it is the worked example of
most of the method below: CRC32-framed sequence-numbered traffic, per-direction
error attribution, a `UNSUPPORTED` grade kept distinct from `FAIL` so a rate the
kernel silently clamped is never confused with a link that failed, self-
describing JSON results for comparing hardware revisions, and a `sim_xiao.py`
software stand-in that validates the tool's own logic without claiming to
validate electrical reality.

## 1. The hardware profile

Every runbook is written against these variables. Set them once per shell (or
keep them in an untracked `~/.config/hw1-investigation.env` and source it) so
that no host, account, device path or model choice is baked into a procedure.

```bash
# --- host co-processor (the SBC running the service) ---
export HW_HOST=              # what you ssh to:  192.168.1.42  |  cm5.local
export HW_USER=              # ssh account name:  cm5
export HW_SERVICE=           # systemd unit name:  hw1-ai-service.service
export HW_PY=                # absolute path to the venv interpreter:
                             #   /home/cm5/hw1ai/bin/python
export HW_CFG=               # absolute path to the service config file:
                             #   /home/cm5/.config/hw1-ai-service/config.yaml
export HW_EVID_ROOT=         # existing dir with GBs free:  /home/cm5/evidence

# --- the link under test ---
export HW_LINK_KIND=         # exactly one of:  uart | usb-cdc | spi | tcp
export HW_LINK_DEV=          # host-side node:  /dev/ttyAMA2
export HW_LINK_BAUD=         # integer, must equal the firmware's:  921600
                             #   (leave empty when HW_LINK_KIND is not serial)

# --- the microcontroller / peripheral side ---
export HW_MCU=               # free-form label that goes in the report:
                             #   xiao_s3 @ fw 0.99.82
export HW_PROBE=             # absolute path to the script that sends one
                             #   command to the MCU and prints the reply:
                             #   /home/cm5/hw1-ai-service/tools/probe.py

# --- what is being measured (set only what the runbook asks for) ---
export HW_AUDIO_SRC=         # where the PCM originates, one of:
                             #   pdm | i2s | ble | usb
export HW_STT_MODEL=         # absolute path to the model directory
export HW_LLM_MODEL=         # absolute path to the weights file
export HW_RENDER_SINK=       # label for where text is displayed, or "none"
```

### Where each value comes from

Run these on the co-processor unless noted. If a lookup returns nothing, that
is itself worth knowing before you start measuring.

| Variable | How to find it |
| --- | --- |
| `HW_HOST` | `hostname -I` for the address, `hostname` for the name. Prefer a stable name over a DHCP address. |
| `HW_USER` | `whoami` |
| `HW_SERVICE` | `systemctl --user list-units --type=service` and pick yours. The `.service` suffix is part of the value. |
| `HW_PY` | `systemctl --user show -p ExecStart "$HW_SERVICE"` — the interpreter is the first path in the command line. |
| `HW_CFG` | Same `ExecStart` line, usually after `--config`. Otherwise check `~/.config/`. |
| `HW_EVID_ROOT` | You choose it. `mkdir -p` it, then `df -h` that path — raw packet dumps and WAVs run to hundreds of MB per session. |
| `HW_LINK_KIND` | How the two boards are physically wired. If you are not sure, you cannot interpret any result below — resolve it first. |
| `HW_LINK_DEV` | `ls -l /dev/serial/by-id/` gives a stable name; `dmesg \| grep -i tty` shows what enumerated. |
| `HW_LINK_BAUD` | Read the *configured* rate from the **firmware** source, not from the host — the host silently accepts a wrong rate and delivers garbage, and `stty -F "$HW_LINK_DEV"` only reports what the host is currently set to. For the *sustainable* rate — the more useful number, and usually the one nobody has measured — run [`uart-baud-test/`](../../uart-baud-test/README.md) against your board pair. |
| `HW_MCU` | Your board name plus the firmware version the run used. Version matters: a report without it cannot be compared to the next one. |
| `HW_PROBE` | Whatever your repo ships for one-shot MCU commands — look in `tools/`. If there is none, every runbook below still works, but you will be driving the link by hand. |
| `HW_AUDIO_SRC` | The firmware's mic-source command reports the active one. Do not assume — several of the past surprises were a source that had silently fallen back. |
| `HW_STT_MODEL` | The `stt` section of `$HW_CFG`, or the model download cache. |
| `HW_LLM_MODEL` | The `llm` section of `$HW_CFG`, or `ls` the weights directory. |
| `HW_RENDER_SINK` | The display you are measuring, e.g. the glasses, an OLED page, or `none` for a headless run. |

**Non-systemd hosts:** `HW_SERVICE` and the `systemctl --user` stanzas in the
runbooks assume systemd. On anything else, put your supervisor's handle for the
daemon in `HW_SERVICE` and substitute its stop/start commands — the stop,
restore-on-exit and ownership checks are what matter, not the tool.

### Check the profile before you trust it

```bash
ssh "$HW_USER@$HW_HOST" true                     || echo "FAIL: ssh"
ssh "$HW_USER@$HW_HOST" "test -e '$HW_LINK_DEV'" || echo "FAIL: link device"
ssh "$HW_USER@$HW_HOST" "test -x '$HW_PY'"       || echo "FAIL: interpreter"
ssh "$HW_USER@$HW_HOST" "test -r '$HW_CFG'"      || echo "FAIL: config"
ssh "$HW_USER@$HW_HOST" "test -d '$HW_EVID_ROOT'"|| echo "FAIL: evidence dir"
ssh "$HW_USER@$HW_HOST" "systemctl --user cat '$HW_SERVICE' >/dev/null" \
                                                 || echo "FAIL: unit name"
```

Paste the filled-in profile at the top of every report you write. A number
without its profile is not evidence — most of the surprises in past
investigations came from a profile field nobody had written down (a governor
left in `powersave`, a second resident model, a display config changed by hand
three days earlier).

## 2. Method

These rules are what actually separated the investigations that concluded from
the ones that produced pages of consistent-but-useless data.

**State a hypothesis *pair*, not a hypothesis.** Write two candidate
explanations that predict *opposite* values of the same two counters. A test
that is merely *consistent* with your favourite theory settles nothing; a test
where the two hypotheses disagree settles it in one run. The canonical example
is in [link-triage.md](link-triage.md): late-but-complete audio and genuinely
lost audio predict opposite values of both the connection interval and the loss
counter, so one simultaneous reading of both decides it.

**Run the decisive test before writing any code.** Diagnose, then fix. The
ranked fix list is worthless until the regime is known, because the two regimes
usually need opposite fixes.

**Instrument before you fix.** When a fix and a telemetry change compete for
first place, ship the telemetry: it makes the failure observable, it protects
the pipeline from silently consuming degraded data, and it tells you whether
the fix worked.

**Never conflate milestones.** At minimum distinguish: (1) the command was
accepted by the near device, (2) the far device acknowledged the message,
(3) the far device reports the work complete, (4) a human observed the result.
Each is a different clock and past reports went wrong by calling (1) an "ack"
for (3). Name the milestone in every timing number you record.

**Prefer device monotonic time to host receive time.** Debug lines buffer;
their host wall-clock timestamps move relative to the event's real device time.
If the device stamps a monotonic millisecond field, derive durations from that.

**Keep negative controls.** For anything with a recognizer in it, pin a set of
inputs with *no* signal (silence, static, no speech) and score them for
fabricated output. A model that is accurate on positives and invents text on
silence is not a working model. Do not treat a quiet-but-real input as a
negative control.

**Hash-pin the corpus and the artifacts.** Record file sizes and hashes in an
evidence manifest so a rerun compares against the same inputs. Corpora drift:
one past run lost a file to an overwrite and spent a session unable to say which
prompt a capture contained.

**Write the not-established table.** Every report ends with a claims table:
claim / status (Established, Strongly supported, Not supported, Rejected,
Unknown) / reason. Right-censored measurements — the ones where the timeout
expired before the event — are recorded as censored, never as a value.

## 3. Evidence discipline

Every procedure below follows the same shape, and the shape matters more than
the individual commands:

```bash
# one directory per run, and a pointer to the newest one
EVID="$(mktemp -d "$HW_EVID_ROOT/<investigation>-$(date +%Y%m%d-%H%M%S)-XXXXXXXX")"
printf '%s\n' "$EVID" > "$HW_EVID_ROOT/<investigation>-latest.txt"

# every command tees into the bundle; nothing is read off the terminal only
... 2>&1 | tee "$EVID/step-01.log"
```

Three rules that repeatedly saved a run:

1. **Restore what you stop.** If the procedure must stop the service to take
   the link, install the restart in a `trap ... EXIT` *before* stopping it.
2. **Prove exclusive ownership before claiming a device.** Check the device is
   free (`fuser "$HW_LINK_DEV"`) and abort if something still holds it, rather
   than fighting for it mid-measurement.
3. **Bracket every measurement window with state reads.** Read link/device
   state immediately before and immediately after each window, so the
   correlation between a state and a rate is explicit rather than inferred
   across a whole session.

Retain the bundle even when the run fails. A failed run's log usually contains
the argument-syntax or state precondition that the next attempt needs — improvising
new commands mid-run is how one session lost the ability to compare against its
own earlier windows.
