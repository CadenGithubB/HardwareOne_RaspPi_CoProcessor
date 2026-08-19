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

## 1. The hardware profile

Every runbook is written against these variables. Set them once per shell (or
keep them in an untracked `~/.config/hw1-investigation.env` and source it) so
that no host, account, device path or model choice is baked into a procedure.

```bash
# --- host co-processor (the SBC running the service) ---
export HW_HOST=              # ip or hostname
export HW_USER=              # ssh user
export HW_SERVICE=           # service unit, e.g. hw1-ai-service.service
export HW_PY=                # interpreter in the service venv
export HW_CFG=               # service config file
export HW_EVID_ROOT=         # directory for evidence bundles

# --- the link under test ---
export HW_LINK_KIND=         # uart | usb-cdc | spi | tcp
export HW_LINK_DEV=          # host-side device node, e.g. /dev/ttyAMA2
export HW_LINK_BAUD=         # if serial

# --- the microcontroller / peripheral side ---
export HW_MCU=               # board identifier
export HW_PROBE=             # host-side probe/CLI entry point for MCU commands

# --- what is being measured ---
export HW_AUDIO_SRC=         # pdm | i2s | ble | usb  (where the PCM comes from)
export HW_STT_MODEL=         # model dir or id
export HW_LLM_MODEL=         # model file or id
export HW_RENDER_SINK=       # display target, or none
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
