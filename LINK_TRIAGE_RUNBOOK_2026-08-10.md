# Link-triage runbook — 2026-08-10

Goal: decide between **H-late** (complete audio, BLE-throttled delivery → link
fix) and **H-loss** (packets dropped → damaged audio), and capture the link
state that correlates with the half-rate anomaly. Every block is copy-paste.
Blocks are labeled **[PI]** (paste into your `$CM5_USER@xiaopi` SSH shell) or
**[MAC]** (paste into a Mac terminal).

Prerequisites: XIAO powered with G2 connected (glasses on, not in the case).
No speech is needed for any Pi step — the idle mic stream carries frames even
in silence. Total hands-on time ≈ 15 minutes.

What each step answers:

| Step | Question it answers |
|---|---|
| 1 | Is the anomalous audio *complete* (batch STT recovers the question) or *damaged*? Does +14 dB gain fix accuracy? Is the 8 kHz theory really dead? |
| 2 | Human check: are words missing, or is it merely quiet/robotic? |
| 3 | What is the measured BLE conn interval, idle mic cadence, and radio state right now — and does forcing the interval to HIGH change the cadence? Does a reconnect clear a degraded state? |
| 4 | Raw 205-byte packets on disk for trailer-counter analysis (sender-slow vs link-loss). |
| 5 | Bundle everything on the Mac for analysis. |

---

## Step 1 — [PI] Batch-STT matrix on the existing captures

No hardware interaction; the service can keep running. Runs the deployed
medium-streaming model in batch mode over the healthy WAV, the anomalous WAV,
their +14 dB variants, the raw live PCMs, and the anomalous audio
*reinterpreted at 8 kHz* (the decisive test of the dead theory).

```bash
bash <<'BASH'
set -Eeuo pipefail
PY=/home/$CM5_USER/hw1ai/bin/python
ROOT=/home/$CM5_USER/hw1-ai-service
MODEL=/home/$CM5_USER/.cache/moonshine_voice/download.moonshine.ai/model/medium-streaming-en/quantized_26_07_30
HEALTHY=/home/$CM5_USER/g2-prefx/native-shadow-20260810-060415
ANOM=/home/$CM5_USER/g2-prefx/native-live-stt-20260810-095106-sbJW7l5d

EVID="$(mktemp -d /home/$CM5_USER/g2-prefx/link-triage-$(date +%Y%m%d-%H%M%S)-XXXXXXXX)"
printf '%s\n' "$EVID" > /home/$CM5_USER/g2-prefx/link-triage-latest.txt
echo "EVID=$EVID"

for f in "$HEALTHY/recording-6bda87ea00000002.wav" \
         "$HEALTHY/live-6bda87ea00000002.pcm" \
         "$ANOM/recording-ae13f08400000001.wav" \
         "$ANOM/live-ae13f08400000001.pcm"; do
  [[ -f "$f" ]] || { echo "MISSING: $f"; exit 1; }
done

vcgencmd get_throttled | tee "$EVID/throttled-before-stt.txt"

PYTHONPATH="$ROOT" "$PY" - "$MODEL" "$HEALTHY" "$ANOM" "$EVID" <<'PYEOF' 2>&1 | tee "$EVID/batch-stt-matrix.log"
import json, sys, time, wave
import numpy as np
from hw1_ai_service.stt.moonshine import MoonshineSTT

model_dir, healthy, anom, evid = sys.argv[1:5]

def load_wav(p):
    w = wave.open(p)
    assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
    return w.readframes(w.getnframes())

def load_pcm(p):
    return open(p, 'rb').read()

def gain_db(pcm, db):
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) * (10 ** (db / 20.0))
    return np.clip(x, -32768, 32767).astype(np.int16).tobytes()

hw = load_wav(f"{healthy}/recording-6bda87ea00000002.wav")
hp = load_pcm(f"{healthy}/live-6bda87ea00000002.pcm")
aw = load_wav(f"{anom}/recording-ae13f08400000001.wav")
ap = load_pcm(f"{anom}/live-ae13f08400000001.pcm")

cases = [
    # (name, pcm, rate)
    ("healthy_wav_16k",        hw,               16000),
    ("healthy_wav_16k_+14dB",  gain_db(hw, 14),  16000),
    ("healthy_livepcm_16k",    hp,               16000),
    ("anom_wav_16k",           aw,               16000),
    ("anom_wav_16k_+14dB",     gain_db(aw, 14),  16000),
    ("anom_livepcm_16k",       ap,               16000),
    ("anom_livepcm_16k_+14dB", gain_db(ap, 14),  16000),
    ("anom_wav_AS_8kHz",       aw,                8000),   # the dead-theory test
    ("anom_wav_AS_8kHz_+14dB", gain_db(aw, 14),   8000),
]

print("loading model:", model_dir, flush=True)
stt = MoonshineSTT(model_dir)
out = {"model_dir": model_dir,
       "expected_text": "what is the capital of france",
       "results": []}
for name, pcm, rate in cases:
    t0 = time.monotonic()
    try:
        text = stt.transcribe(pcm, rate)
        err = None
    except Exception as exc:
        text, err = None, repr(exc)
    dt = round(time.monotonic() - t0, 2)
    rec = {"case": name, "rate": rate, "seconds_audio": round(len(pcm) / 2 / rate, 3),
           "transcribe_s": dt, "text": text, "error": err}
    out["results"].append(rec)
    print(f"{name:28s} rate={rate:5d}  {dt:6.2f}s  -> {text!r}" + (f"  ERR={err}" if err else ""), flush=True)

with open(f"{evid}/batch-stt-matrix.json", "w") as f:
    json.dump(out, f, indent=1)
print("\nwrote", f"{evid}/batch-stt-matrix.json")
PYEOF

vcgencmd get_throttled | tee "$EVID/throttled-after-stt.txt"
echo "STEP 1 DONE -> $EVID"
BASH
```

How to read it (no need to wait for me):
- `healthy_wav_16k` correct + `anom_wav_16k` correct → **H-late confirmed**:
  nothing was lost, the link was merely slow, and STT itself is fine.
- `healthy_wav_16k` correct + `anom_wav_16k` wrong → content damaged → H-loss.
- `healthy_wav_16k` wrong too → G2 audio quality/quietness is the real
  accuracy problem, and the cadence anomaly is a separate (still real) issue.
  Check whether the `+14dB` variant fixes it.
- `anom_wav_AS_8kHz` correct → the 8 kHz theory revives (not expected —
  pitch evidence says it will be gibberish).

## Step 2 — [MAC] The one listening question

```bash
bash <<'BASH'
set -Eeuo pipefail
DIR=$FIRMWARE_ROOT/.scratch/native-live-stt-GBMcqNF1
HDIR=$FIRMWARE_ROOT/.scratch/native-shadow-LANSCdQ1

# make a +14 dB listen copy of the HEALTHY capture for a fair comparison
python3 - "$HDIR/recording-6bda87ea00000002.wav" "$HDIR/healthy-listen-plus14db.wav" <<'PYEOF'
import sys, wave, array
src, dst = sys.argv[1:3]
w = wave.open(src); pcm = array.array('h'); pcm.frombytes(w.readframes(w.getnframes()))
g = 10 ** (14 / 20)
out = array.array('h', (max(-32768, min(32767, int(v * g))) for v in pcm))
o = wave.open(dst, 'w'); o.setnchannels(1); o.setsampwidth(2); o.setframerate(16000)
o.writeframes(out.tobytes()); o.close()
print("wrote", dst)
PYEOF

echo "=== ANOMALOUS capture (the failed run), normal 16 kHz label ==="
afplay "$DIR/recording-ae13f08400000001-listen-plus14db.wav"
echo "=== HEALTHY capture for comparison ==="
afplay "$HDIR/healthy-listen-plus14db.wav"

cat > "$DIR/listening-notes.txt" <<'EOF'
Q1. In the ANOMALOUS clip, are ALL the words of "what is the capital of
    France" present and in order? (yes / no — if no, which parts are missing?)
A1:

Q2. Tempo of the ANOMALOUS clip vs how you actually spoke: normal / too fast /
    too slow?
A2:

Q3. Pitch: normal voice, or chipmunk-high, or unnaturally deep?
A3:

Q4. Compared to the HEALTHY clip, is the anomalous one more garbled/robotic,
    or about the same character just different content?
A4:
EOF
open -e "$DIR/listening-notes.txt"
echo "Answer the 4 questions in the TextEdit window and save."
BASH
```

Replay either file as many times as needed:
`afplay .scratch/native-live-stt-GBMcqNF1/recording-ae13f08400000001-listen-plus14db.wav`

## Step 3 — [PI] Link-state snapshot, idle cadence, interval experiment, reconnect test

Stops the service (UART needed), restores it on exit via trap. Measures idle
mic cadence in four controlled 12 s windows: (a) as-found, (b) conn interval
forced HIGH (15 ms), (c) interval restored to default, (d) after a BLE
disconnect/reconnect cycle. Each window is bracketed by `g2connpri` reports so
cadence↔interval correlation is explicit.

```bash
bash <<'BASH'
set -Eeuo pipefail
PY=/home/$CM5_USER/hw1ai/bin/python
ROOT=/home/$CM5_USER/hw1-ai-service
G2_PROBE=$ROOT/tools/g2_evenai_probe.py
CFG=/home/$CM5_USER/.config/hw1-ai-service/config.yaml
SERVICE=hw1-ai-service.service
UART=/dev/ttyAMA2
EVID="$(cat /home/$CM5_USER/g2-prefx/link-triage-latest.txt)"
[[ -d "$EVID" ]] || { echo "run Step 1 first (no EVID dir)"; exit 1; }
echo "EVID=$EVID"

SERVICE_WAS_ACTIVE=0
systemctl --user is-active --quiet "$SERVICE" && SERVICE_WAS_ACTIVE=1
restore() {
  set +e
  if [ "$SERVICE_WAS_ACTIVE" -eq 1 ]; then
    systemctl --user start "$SERVICE" >"$EVID/service-restart.log" 2>&1
    for _ in $(seq 1 30); do
      systemctl --user is-active --quiet "$SERVICE" && break
      sleep 1
    done
    systemctl --user is-active "$SERVICE" >>"$EVID/service-restart.log" 2>&1
  fi
}
trap restore EXIT

if [ "$SERVICE_WAS_ACTIVE" -eq 1 ]; then
  systemctl --user stop "$SERVICE"
  sleep 2
fi
if fuser "$UART" >/dev/null 2>&1; then
  fuser -v "$UART" | tee "$EVID/uart-holder.log"
  echo "UART still held — aborting"; exit 1
fi

probe() { # probe <logname> <cmd>...
  local log="$1"; shift
  "$PY" "$G2_PROBE" --config "$CFG" cmd "$@" 2>&1 | tee -a "$EVID/$log"
}

echo "=== P1 snapshot (as found) ==="
probe 03-snapshot.log \
  'g2status' 'g2evenai status' 'g2connpri' 'g2envgap' 'g2micstats' \
  'wifistatus' 'espnowstatus' 'micsource' 'micread json' 'liveaudio status'

echo "=== P2 window A: as-found cadence (12 s) ==="
probe 03-window-A.log 'micsource g2' 'openmic' 'g2connpri' 'g2micreset'
sleep 12
probe 03-window-A.log 'g2micstats' 'g2connpri'

echo "=== P3 window B: conn interval forced HIGH 12/12 (15 ms) ==="
probe 03-window-B.log 'g2connpri 12 12' 'g2micreset'
sleep 12
probe 03-window-B.log 'g2micstats' 'g2connpri'

echo "=== P4 window C: interval restored to default ==="
probe 03-window-C.log 'g2connpri default' 'g2micreset'
sleep 12
probe 03-window-C.log 'g2micstats' 'g2connpri'

echo "=== P5 window D: BLE reconnect cycle ==="
probe 03-window-D.log 'closeg2'
sleep 4
probe 03-window-D.log 'openg2'
sleep 12
probe 03-window-D.log 'g2status'
probe 03-window-D.log 'micsource g2' 'openmic' 'g2connpri' 'g2micreset'
sleep 12
probe 03-window-D.log 'g2micstats' 'g2connpri' 'g2status'

echo "=== P6 leave mic idle-open (matches normal bench state) + final snapshot ==="
probe 03-final.log 'g2micstats' 'micread json' 'g2evenai status'

echo "STEP 3 DONE -> $EVID (service restore follows via trap)"
BASH
```

Expected healthy numbers per 12 s window: ~240 frames, `~20 fps`. The
anomalous morning session idled at ~14 fps and captured at 10 fps. If window A
is degraded and window B (forced 15 ms interval) jumps to ~20 fps → the conn
interval is the mechanism. If window D fixes it → the state is per-connection
and a reconnect clears it. If all windows read ~20 fps → the degraded state is
intermittent; we gate on it at preflight and move on.

## Step 4 — [PI] Raw packet dump with trailer bytes (optional but cheap)

Captures ~10 s of raw 205-byte notifications to SD, then pulls the file to the
Pi evidence dir. The trailer bytes 200–204 of each packet are the only real
on-wire counter; their continuity distinguishes "G2 sending slower" from
"packets lost in flight". Run immediately after Step 3 (service still stopped —
rerun the stop stanza from Step 3 if you did it separately).

```bash
bash <<'BASH'
set -Eeuo pipefail
PY=/home/$CM5_USER/hw1ai/bin/python
ROOT=/home/$CM5_USER/hw1-ai-service
G2_PROBE=$ROOT/tools/g2_evenai_probe.py
CFG=/home/$CM5_USER/.config/hw1-ai-service/config.yaml
SERVICE=hw1-ai-service.service
UART=/dev/ttyAMA2
EVID="$(cat /home/$CM5_USER/g2-prefx/link-triage-latest.txt)"

SERVICE_WAS_ACTIVE=0
systemctl --user is-active --quiet "$SERVICE" && SERVICE_WAS_ACTIVE=1
restore() {
  set +e
  if [ "$SERVICE_WAS_ACTIVE" -eq 1 ]; then
    systemctl --user start "$SERVICE" >>"$EVID/service-restart.log" 2>&1
  fi
}
trap restore EXIT
[ "$SERVICE_WAS_ACTIVE" -eq 1 ] && { systemctl --user stop "$SERVICE"; sleep 2; }
fuser "$UART" >/dev/null 2>&1 && { echo "UART held — aborting"; exit 1; }

probe() { local log="$1"; shift; "$PY" "$G2_PROBE" --config "$CFG" cmd "$@" 2>&1 | tee -a "$EVID/$log"; }

probe 04-rawdump.log 'micsource g2' 'openmic' 'g2micrec start "/sd/g2mic_triage.lc3"'
sleep 10
probe 04-rawdump.log 'g2micrec stop' 'g2micstats'
"$PY" "$G2_PROBE" --config "$CFG" fetch /sd/g2mic_triage.lc3 "$EVID/g2mic_triage.raw" \
  2>&1 | tee -a "$EVID/04-rawdump.log"
ls -la "$EVID/g2mic_triage.raw" | tee -a "$EVID/04-rawdump.log"
echo "STEP 4 DONE"
BASH
```

If `g2micrec` or `fetch` errors, just keep the log — the arg syntax may
differ; do not improvise, we'll adjust from the log.

## Step 5 — [MAC] Collect the bundle

```bash
bash <<'BASH'
set -Eeuo pipefail
REPO=$FIRMWARE_ROOT
TS=$(date +%Y%m%d-%H%M%S)
DEST="$REPO/.scratch/link-triage-$TS"
mkdir -p "$DEST/pi"

PI_EVID="$(ssh $CM5_USER@$CM5_HOST 'cat /home/$CM5_USER/g2-prefx/link-triage-latest.txt')"
rsync -av "$CM5_USER@$CM5_HOST:$PI_EVID/" "$DEST/pi/"
cp "$REPO/.scratch/native-live-stt-GBMcqNF1/listening-notes.txt" "$DEST/" 2>/dev/null || \
  echo "note: listening-notes.txt not found (Step 2 not done?)"

ln -sfn "$DEST" "$REPO/.scratch/link-triage-latest"
find "$DEST" -type f | sort
echo
echo "BUNDLE READY: $DEST"
BASH
```

Then just tell me the bundle is ready (I can read `.scratch/link-triage-latest`
directly). If any step failed, include which one — the logs land in the bundle
either way.

## Step 6 — [PI] Confirmation A/B (REVISED after adversarial review)

The first pass found a candidate mechanism (`by=peer(L2CAP)` slow interval)
but all cadence windows read ZERO frames: no display container was active, and
the G2 silently ignores mic-enable without one (docs/G2_PROTOCOL.md:1219). The
adversarial review also found the lens idle-times-out after **~30 s** with no
input, tearing the container down mid-test — so this revision re-issues
`g2show` at the start of EVERY window, restores the conn interval in the trap,
verifies the active mic source, and re-reads `g2connpri` after every window
(a mid-window peer renegotiation would silently invalidate that window).

Keep WiFi/ESP-NOW idle during this test; 2.4 GHz load can mimic the cap.

```bash
bash <<'BASH'
set -Eeuo pipefail
PY=/home/$CM5_USER/hw1ai/bin/python
ROOT=/home/$CM5_USER/hw1-ai-service
G2_PROBE=$ROOT/tools/g2_evenai_probe.py
CFG=/home/$CM5_USER/.config/hw1-ai-service/config.yaml
SERVICE=hw1-ai-service.service
UART=/dev/ttyAMA2
EVID="$(cat /home/$CM5_USER/g2-prefx/link-triage-latest.txt)"

SERVICE_WAS_ACTIVE=0
systemctl --user is-active --quiet "$SERVICE" && SERVICE_WAS_ACTIVE=1
restore() {
  set +e
  # best-effort: close any dangling raw capture, restore interval, restart service
  "$PY" "$G2_PROBE" --config "$CFG" cmd 'g2micrec stop' 'g2connpri default' \
    >>"$EVID/06-restore.log" 2>&1
  if [ "$SERVICE_WAS_ACTIVE" -eq 1 ]; then
    systemctl --user start "$SERVICE" >>"$EVID/service-restart.log" 2>&1
  fi
}
trap restore EXIT
[ "$SERVICE_WAS_ACTIVE" -eq 1 ] && { systemctl --user stop "$SERVICE"; sleep 2; }
fuser "$UART" >/dev/null 2>&1 && { echo "UART held — aborting"; exit 1; }

probe() { local log="$1"; shift; "$PY" "$G2_PROBE" --config "$CFG" cmd "$@" 2>&1 | tee -a "$EVID/$log"; }

echo "=== radio state + container up + mic on ==="
probe 06-confirm.log 'wifistatus json' 'g2status' \
  'g2show "LINK TRIAGE A"' 'micsource g2' 'openmic' 'micsource'
# micsource line above MUST show active=g2; if it says pdm, stop and report.

echo "=== window A: as-found interval, container fresh ==="
probe 06-confirm.log 'g2show "LINK TRIAGE A2"' 'g2connpri' 'g2micreset'
sleep 12
probe 06-confirm.log 'g2micstats' 'g2connpri'

echo "=== window B: SLOW 84/84 (105 ms — the peer's afternoon value) ==="
probe 06-confirm.log 'g2show "LINK TRIAGE B"' 'g2connpri 84 84' 'g2micreset'
sleep 12
probe 06-confirm.log 'g2micstats' 'g2connpri'
probe 06-confirm.log 'g2show "LINK TRIAGE B2"' 'g2micrec start "/sd/g2mic_slow.lc3"'
sleep 10
probe 06-confirm.log 'g2micrec stop' 'g2connpri'

echo "=== window C: FAST 12/12 (15 ms) ==="
probe 06-confirm.log 'g2show "LINK TRIAGE C"' 'g2connpri 12 12' 'g2micreset'
sleep 12
probe 06-confirm.log 'g2micstats' 'g2connpri'
probe 06-confirm.log 'g2show "LINK TRIAGE C2"' 'g2micrec start "/sd/g2mic_fast.lc3"'
sleep 10
probe 06-confirm.log 'g2micrec stop' 'g2connpri'

echo "=== restore + fetch raw dumps ==="
probe 06-confirm.log 'g2connpri default' 'g2micstats' 'g2connpri'
"$PY" "$G2_PROBE" --config "$CFG" fetch /sd/g2mic_slow.lc3 "$EVID/g2mic_slow.raw" \
  2>&1 | tee -a "$EVID/06-confirm.log"
"$PY" "$G2_PROBE" --config "$CFG" fetch /sd/g2mic_fast.lc3 "$EVID/g2mic_fast.raw" \
  2>&1 | tee -a "$EVID/06-confirm.log"
ls -la "$EVID"/g2mic_*.raw | tee -a "$EVID/06-confirm.log"
echo "STEP 6 DONE -> $EVID"
BASH
```

How to read it (corrected predictions — the interval arithmetic matters):

- Window A frames > 0 at all ⇒ the container explanation for Step 3's zero
  frames is confirmed (positive control).
- Window C (15 ms): ≈ 240 frames / ~20 fps expected.
- Window B (84 ticks = 105 ms): **~114 frames / ~9.5 fps** if the glasses send
  one notification per connection event. Note the morning anomaly measured
  **10.0/s (100 ms spacing)**, which 84 ticks cannot produce exactly — so:
  - ~9.5 fps ⇒ mechanism class confirmed; the morning link was probably
    80 ticks, not 84.
  - ~10.0 fps ⇒ effective cadence is 100 ms-quantized; matches the morning
    even better.
  - **~20 fps ⇒ the interval does NOT cap mic delivery** (two notifications
    per event) — the slow-interval theory is refuted and the morning cause is
    back open. This outcome is important; report it prominently.
- If any post-window `g2connpri` shows `by=peer` again, that window is
  invalid (the peer renegotiated mid-test) — note it and rerun the window.
- The `.raw` files: 205-byte records; bytes 200–204 are the on-wire trailer.
  Counter continuity at the slow interval distinguishes "G2 buffers and sends
  late (nothing lost)" from "packets dropped".

Caveat this test cannot close: it demonstrates the mechanism, not the morning
event itself — no interval was recorded at 09:51 (different BLE connection;
tx counters prove reconnects in between). The morning attribution stays
inference-by-consistency either way.

Afterwards rerun Step 5 on the Mac to pull the bundle.

## Step 7 — Firmware fix-package validation (added after implementation)

Two human moments (flash cable, one spoken phrase); everything else is
automated with PASS/FAIL verdicts.

### 7a — [MAC] Flash the new build

The xiao_s3 build is on the classic partition layout, so the build's own
flash command is correct. Plug the XIAO into the Mac via USB-C, find the
port, flash, then reconnect it to the carrier:

```bash
ls /dev/cu.usbmodem*
```

```bash
cd $FIRMWARE_ROOT && python -m esptool --chip esp32s3 -p $(ls /dev/cu.usbmodem* | head -1) -b 460800 --before default_reset --after hard_reset write_flash --flash_mode dio --flash_size 8MB --flash_freq 80m 0x0 build-xiao_s3/bootloader/bootloader.bin 0x8000 build-xiao_s3/partition_table/partition-table.bin 0x10000 build-xiao_s3/hardwareone-idf.bin
```

(If this bench XIAO was migrated to the OTA layout at some point, STOP and
use your ota0-flash loop instead — the classic-layout write would clobber
the updater. The build printing `HW1_OTA_LAYOUT=0` says it expects classic.)

Reconnect the XIAO to the carrier, power up, glasses on and connected.

### 7b — [PI] Automated smoke suite (~2 min, no speech needed)

Checks, in order (REVISED after the boot "Listening..." regression — the
container + FAST hold are now RECORDING-scoped): (0) new firmware running
(g2micstats fingerprint); (1) idle-open mic stays SILENT and paints no page
— the boot regression check; (2) a `micrecord` capture auto-creates the
container, streams ~20/s, and holds the FAST interval; (3) lossless
`g2micrec` during a recording — ring_drops=0 and a contiguous trailer-seq
file, including across the old ~41 KB stall spot; (4) counters sane
(degraded=0, decode_fail=0).

```bash
bash <<'BASH'
set -Eeuo pipefail
PY=/home/$CM5_USER/hw1ai/bin/python
ROOT=/home/$CM5_USER/hw1-ai-service
G2_PROBE=$ROOT/tools/g2_evenai_probe.py
CFG=/home/$CM5_USER/.config/hw1-ai-service/config.yaml
SERVICE=hw1-ai-service.service
UART=/dev/ttyAMA2
EVID="$(mktemp -d /home/$CM5_USER/g2-prefx/fixpkg-test-$(date +%Y%m%d-%H%M%S)-XXXXXXXX)"
printf '%s\n' "$EVID" > /home/$CM5_USER/g2-prefx/fixpkg-latest.txt
echo "EVID=$EVID"
PASS=(); FAIL=()
verdict() { if [ "$2" = 1 ]; then PASS+=("$1"); echo "PASS: $1"; else FAIL+=("$1"); echo "FAIL: $1"; fi; }

SERVICE_WAS_ACTIVE=0
systemctl --user is-active --quiet "$SERVICE" && SERVICE_WAS_ACTIVE=1
restore() {
  set +e
  "$PY" "$G2_PROBE" --config "$CFG" cmd 'g2micrec stop' 'g2connpri default' \
    >>"$EVID/restore.log" 2>&1
  [ "$SERVICE_WAS_ACTIVE" -eq 1 ] && systemctl --user start "$SERVICE" \
    >>"$EVID/restore.log" 2>&1
  echo; echo "===== VERDICTS ====="
  printf 'PASS: %s\n' "${PASS[@]:-none}"
  printf 'FAIL: %s\n' "${FAIL[@]:-none}"
  echo "Evidence: $EVID"
}
trap restore EXIT
[ "$SERVICE_WAS_ACTIVE" -eq 1 ] && { systemctl --user stop "$SERVICE"; sleep 2; }
fuser "$UART" >/dev/null 2>&1 && { echo "UART held — aborting"; exit 1; }

probe() { local log="$1"; shift; "$PY" "$G2_PROBE" --config "$CFG" cmd "$@" 2>&1 | tee -a "$EVID/$log"; }

echo "=== T0: firmware fingerprint + clean slate ==="
OUT="$(probe t0.log 'g2micstats')"
if echo "$OUT" | grep -q "gap_events="; then verdict "new-firmware-running" 1
else verdict "new-firmware-running (OLD FORMAT — flash didn't take?)" 0; exit 1; fi
probe t0.log 'g2clear' 'closemic' 'g2micreset' >/dev/null
sleep 2

echo "=== T1: idle-open mic stays silent (boot-regression check) ==="
probe t1.log 'micsource g2' 'openmic' >/dev/null
sleep 6
OUT="$(probe t1.log 'g2micstats')"
FRAMES=$(echo "$OUT" | grep -o 'L=[0-9]* frames' | grep -o '[0-9]*' | head -1)
[ "${FRAMES:-1}" -eq 0 ] && verdict "idle mic silent, no auto page (frames=0)" 1 \
  || verdict "idle mic silent (frames=${FRAMES:-?}, expected 0 — page/stream leaked?)" 0
# ALSO verify by eye: the glasses should still show their default screen here.

echo "=== T2: recording auto-creates container + streams + holds FAST ==="
probe t2.log 'g2micreset' 'micrecord start' >/dev/null
sleep 8
OUT="$(probe t2.log 'g2micstats' 'g2connpri')"
FRAMES=$(echo "$OUT" | grep -o 'L=[0-9]* frames' | grep -o '[0-9]*' | head -1)
RATE=$(echo "$OUT" | grep -o 'rate=[0-9]*\.[0-9]*' | head -1 | cut -d= -f2)
[ "${FRAMES:-0}" -ge 120 ] && verdict "recording streams (frames=$FRAMES)" 1 \
  || verdict "recording streams (frames=${FRAMES:-0}, expected >=120)" 0
echo "$OUT" | grep -qE "rate=(1[89]|2[01])\." && verdict "rate ~20/s (rate=$RATE)" 1 \
  || verdict "rate ~20/s (got rate=$RATE)" 0
echo "$OUT" | grep -qE "L=12\(15\...ms\) lat=0 .*by=self" \
  && verdict "FAST held during recording (L=12 by=self)" 1 \
  || verdict "FAST held during recording (see t2.log)" 0
echo "$OUT" | grep -q "degraded=0" && verdict "not degraded" 1 || verdict "not degraded" 0
# Glasses should show "Listening..." DURING this recording, then return to
# the default screen a moment after the stop below.

echo "=== T3: lossless g2micrec during a recording (15 s) ==="
probe t3.log 'g2micrec start "/sd/fixpkg_test.lc3"' >/dev/null
sleep 15
OUT="$(probe t3.log 'g2micrec stop')"
probe t3.log 'micrecord stop' >/dev/null
PKTS=$(echo "$OUT" | grep -o '[0-9]* packets' | grep -o '[0-9]*' | head -1)
echo "$OUT" | grep -q "ring_drops=0" && verdict "ring_drops=0" 1 || verdict "ring_drops=0" 0
echo "$OUT" | grep -qE "mutex_miss=[0-3]\)" && verdict "mutex_miss<=3" 1 || verdict "mutex_miss<=3" 0
[ "${PKTS:-0}" -ge 280 ] && verdict "packet count (pkts=$PKTS >=280)" 1 \
  || verdict "packet count (pkts=${PKTS:-0}, expected >=280)" 0
"$PY" "$G2_PROBE" --config "$CFG" fetch /sd/fixpkg_test.lc3 "$EVID/fixpkg_test.raw" \
  2>&1 | tee -a "$EVID/t3.log"
SEQCHK="$("$PY" - "$EVID/fixpkg_test.raw" <<'PYEOF'
import sys
d = open(sys.argv[1], 'rb').read()
n = len(d) // 205
seq = [d[i*205 + 204] for i in range(n)]
gaps = [(i, seq[i], seq[i+1]) for i in range(n-1) if (seq[i+1]-seq[i]) % 256 != 1]
print(f"packets={n} gaps={len(gaps)} {gaps[:5]}")
PYEOF
)"
echo "$SEQCHK" | tee -a "$EVID/t3.log"
echo "$SEQCHK" | grep -q "gaps=0" && verdict "trailer seq contiguous" 1 \
  || verdict "trailer seq contiguous ($SEQCHK)" 0

echo "=== T4: final counter sanity ==="
OUT="$(probe t4.log 'g2micstats')"
echo "$OUT" | grep -q "decode_fail=0" && verdict "decode_fail=0" 1 || verdict "decode_fail=0" 0
echo "$OUT" | grep -q "degraded=0" && verdict "still not degraded" 1 || verdict "still not degraded" 0
echo "STEP 7b DONE"
BASH
```

### 7c — [PI] The one human test: native gate (say the phrase)

Same runner as before — it validates the whole wake→live→STT chain and its
result now also carries the new `evenai_timing` event:

```bash
/home/$CM5_USER/hw1-ai-service/tools/run_native_live_stt_gate.sh
```

When prompted, say "Hey Even", then exactly **"what is the capital of
France"**, then stay silent. Afterwards check the timing event landed:

```bash
grep -o 'evenai_timing[^"]*' "$(cat /home/$CM5_USER/g2-prefx/native-live-stt-latest.txt)/result.json" || echo "no evenai_timing event captured"
```

### 7e — Repetition run (T11: the accuracy answer, n=5)

First sync the updated ai-service to the Pi (the event parser now records
`evenai_timing` / `evenai_stream_complete` into result.json — add
`--exclude '.venv-dev/'` to keep the Mac dev venv off the Pi):

```bash
rsync -av --itemize-changes --exclude '.pytest_cache/' --exclude '__pycache__/' --exclude '*.pyc' --exclude '.venv-dev/' $REPO_ROOT/ai-service/ $CM5_USER@$CM5_HOST:/home/$CM5_USER/hw1-ai-service/
```

Then on the Pi, five gates back to back — say "Hey Even" + exactly the
pinned phrase each time the prompt appears, ~15 s apart:

```bash
for i in 1 2 3 4 5; do
  echo "===== RUN $i/5 ====="
  /home/$CM5_USER/hw1-ai-service/tools/run_native_live_stt_gate.sh
  sleep 3
done 2>&1 | tee /home/$CM5_USER/g2-prefx/t11-5x-$(date +%H%M%S).log
```

Each run prints its own result JSON; exit 1 with word_errors=1 (the trailing
artifact) still counts as a transport pass — what we're measuring is how
often the artifact appears and the timing spread. Quick per-run summary
afterwards:

```bash
grep -o '"text":"[^"]*"' /home/$CM5_USER/g2-prefx/t11-5x-*.log | tail -5; grep -o '"word_errors":[0-9]*' /home/$CM5_USER/g2-prefx/t11-5x-*.log | tail -5
```

### 7d — [MAC] Pull everything

```bash
rsync -av $CM5_USER@$CM5_HOST:"$(ssh $CM5_USER@$CM5_HOST 'cat /home/$CM5_USER/g2-prefx/fixpkg-latest.txt')/" $FIRMWARE_ROOT/.scratch/fixpkg-test/ && rsync -av $CM5_USER@$CM5_HOST:"$(ssh $CM5_USER@$CM5_HOST 'cat /home/$CM5_USER/g2-prefx/native-live-stt-latest.txt')/" $FIRMWARE_ROOT/.scratch/fixpkg-native/
```

And the daemon's own journal (covers exchanges the SERVICE handled between
probe runs — the probe evidence dirs never contain these).

One-time setup (the Pi has no persistent journal, which is why
`journalctl --user` said "No journal files were found"; this enables it
permanently):

```bash
ssh -t $CM5_USER@$CM5_HOST 'sudo mkdir -p /var/log/journal && sudo systemctl restart systemd-journald'
```

Then the pull — user-unit lines live in the system journal tagged
`_SYSTEMD_USER_UNIT`, which needs sudo to read:

```bash
ssh -t $CM5_USER@$CM5_HOST 'sudo journalctl _SYSTEMD_USER_UNIT=hw1-ai-service.service --since "2 hours ago" --no-pager' > $FIRMWARE_ROOT/.scratch/daemon-journal.log
```

(Only lines logged AFTER the one-time setup are captured — older runs are
gone; that's expected.)

(Password prompts as usual.) Then tell me and I read everything from
`.scratch/`.

Interpretation notes:
- T1 failing with frames=0 → the auto-container didn't take; grab `t1.log`
  — the glasses may have been asleep (wake them, rerun).
- T2 relies on `g2connpri default` having HIGH=12/12; if a previous session
  left the runtime HIGH values changed, run `g2connpri default` first.
- T3's seq check tolerates nothing: even the old deterministic 2-packet
  stall at ~41 KB must be gone (that's the staging ring working).
- (Resolved 2026-08-10: the FAST hold and the "Listening..." container are
  now RECORDING-scoped — boot/idle-open mic paints nothing and pins nothing.
  Expect the page only while a capture is actually running.)

## Notes

- Step 3/4 leave the system as found: service restarted by trap, mic left in
  the normal idle-open state, conn interval restored to default.
- Nothing here writes to the glasses beyond `closeg2`/`openg2` (a reconnect you
  do routinely) and the interval request the firmware already uses.
- If the G2 shows as disconnected at any point (`g2status` not `state=connected
  L=up R=up`), stop and note it — cadence numbers from a partial link are not
  comparable.
- Speech is NOT needed for steps 3/4; silence still produces the full frame
  cadence. Wear or hold the glasses normally so the arms stay powered.
