#!/usr/bin/env bash
# Default-off physical Gate 3 smoke: real G2 PCM -> bounded Moonshine stream.
# No LLM, ASK, REPLY, or production-daemon live path is enabled.

set -Eeuo pipefail

CM5_HOME="${HOME:?HOME must name the service account home}"
PY="$CM5_HOME/hw1ai/bin/python"
ROOT="$CM5_HOME/hw1-ai-service"
PROBE="$ROOT/tools/link/live_pcm_shadow_probe.py"
G2_PROBE="$ROOT/tools/link/g2_evenai_probe.py"
CFG="$CM5_HOME/.config/hw1-ai-service/config.yaml"
MODEL="$CM5_HOME/.cache/moonshine_voice/download.moonshine.ai/model/medium-streaming-en/quantized_26_07_30"
POWER_HELPER=/usr/local/libexec/hw1-power-helper
UART=/dev/ttyAMA2
SERVICE=hw1-ai-service.service
RESULT_ROOT="$CM5_HOME/g2-prefx"
EXPECTED_TEXT='what is the capital of france'

mkdir -p "$RESULT_ROOT"
OUT="$(mktemp -d \
  "$RESULT_ROOT/native-live-stt-$(date +%Y%m%d-%H%M%S)-XXXXXXXX")"
printf '%s\n' "$OUT" > "$RESULT_ROOT/native-live-stt-latest.txt"

PREVIOUS_PROFILE=
POWER_CHANGED=0
SERVICE_WAS_ACTIVE=0
PROBE_RC=99

uart_matches_mainpid() {
  local expected="$1" holders
  local -a pids=()
  holders="$(fuser "$UART" 2>/dev/null || true)"
  read -r -a pids <<< "$holders" || true
  [[ "$expected" =~ ^[1-9][0-9]*$ ]] \
    && (( ${#pids[@]} == 1 )) \
    && [[ "${pids[0]}" == "$expected" ]]
}

cleanup_native_live_stt() {
  shell_rc=$?
  trap - EXIT
  set +e
  cleanup_rc=0

  if ! fuser "$UART" >/dev/null 2>&1; then
    "$PY" "$G2_PROBE" --config "$CFG" cmd \
      'g2evenai status' 'g2micstats' 'closemic' \
      'micsource g2' 'openmic' 'micsource' 'micread json' \
      >"$OUT/final-g2-cleanup.log" 2>&1 || cleanup_rc=1
  else
    fuser -v "$UART" >"$OUT/uart-holder-before-cleanup.log" 2>&1
    cleanup_rc=1
  fi

  if [ "$POWER_CHANGED" -eq 1 ]; then
    sudo -n "$POWER_HELPER" profile "$PREVIOUS_PROFILE" \
      >"$OUT/power-restore.log" 2>&1 || cleanup_rc=1
  fi

  service_restored=0
  uart_restored=0
  if [ "$SERVICE_WAS_ACTIVE" -eq 1 ]; then
    systemctl --user start "$SERVICE" \
      >"$OUT/service-start.log" 2>&1 || cleanup_rc=1
    for _ in $(seq 1 30); do
      if systemctl --user is-active --quiet "$SERVICE"; then
        main_pid="$(systemctl --user show "$SERVICE" \
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
    test "$service_restored" -eq 1 || cleanup_rc=1
    test "$uart_restored" -eq 1 || cleanup_rc=1
  fi

  systemctl --user is-active "$SERVICE" \
    >"$OUT/service-active-after.log" 2>&1 || true
  systemctl --user --no-pager -l status "$SERVICE" \
    >"$OUT/service-status-after.log" 2>&1 || true
  fuser -v "$UART" >"$OUT/uart-holder-after.log" 2>&1 || true

  if [ "$shell_rc" -eq 0 ] && [ "$cleanup_rc" -ne 0 ]; then
    shell_rc=$cleanup_rc
  fi
  printf 'probe_rc=%s shell_rc=%s cleanup_rc=%s evidence=%s\n' \
    "$PROBE_RC" "$shell_rc" "$cleanup_rc" "$OUT"
  exit "$shell_rc"
}

trap cleanup_native_live_stt EXIT

test -x "$PY"
test -f "$PROBE"
test -f "$G2_PROBE"
test -f "$CFG"
test -d "$MODEL"
test -x "$POWER_HELPER"

systemctl --user is-active "$SERVICE" | tee "$OUT/service-active-before.log"
grep -Fxq active "$OUT/service-active-before.log"
SERVICE_WAS_ACTIVE=1
main_pid_before="$(systemctl --user show "$SERVICE" -p MainPID --value)"
fuser -v "$UART" >"$OUT/uart-holder-before-stop.log" 2>&1 || true
uart_matches_mainpid "$main_pid_before" || {
  echo "STOP: UART holder is not exactly service MainPID $main_pid_before" >&2
  exit 1
}

systemctl --user stop "$SERVICE"
for _ in $(seq 1 20); do
  state="$(systemctl --user show "$SERVICE" -p ActiveState --value)"
  [ "$state" = inactive ] && break
  sleep 1
done
printf '%s\n' "$state" > "$OUT/service-state-after-stop.log"
test "$state" = inactive
if fuser -v "$UART" >"$OUT/uart-holder-before.log" 2>&1; then
  echo "STOP: $UART still has an owner" >&2
  exit 1
fi
# Do not grep the broad source-tree name: this runner itself lives under
# The runner itself lives below hw1-ai-service and would match a broad source
# tree search. Match only an installed daemon entry point (under any account's
# home) or an actual llama-server executable.
if pgrep -af '[/]hw1ai/bin/hw1-ai-service|[/]llama-server([[:space:]]|$)' \
    >"$OUT/processes-after-stop.log"; then
  echo 'STOP: competing AI process is still running' >&2
  exit 1
fi

sudo -n "$POWER_HELPER" status | tee "$OUT/power-before.json"
PREVIOUS_PROFILE="$("$PY" - "$OUT/power-before.json" <<'PY'
import json
import pathlib
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_text())["profile"]
if value not in {"eco", "balanced", "performance"}:
    raise SystemExit(f"unsafe prior profile: {value!r}")
print(value)
PY
)"
POWER_CHANGED=1
sudo -n "$POWER_HELPER" profile performance | tee "$OUT/power-profile.log"
for governor in /sys/devices/system/cpu/cpufreq/policy*/scaling_governor; do
  grep -Fxq performance "$governor"
done
vcgencmd get_throttled | tee "$OUT/throttled-before.txt"
if ! grep -Fxq 'throttled=0x0' "$OUT/throttled-before.txt"; then
  echo "PREFLIGHT ABORT: Pi power/throttle flags are sticky:" \
       "$(cat "$OUT/throttled-before.txt")" >&2
  echo "These latch since boot (0x50000 = undervoltage+throttling occurred)." >&2
  echo "Reboot the Pi to clear them, then rerun:  sudo reboot" >&2
  exit 1
fi

sha256sum \
  "$ROOT/hw1_ai_service/stt/live.py" \
  "$PROBE" "$G2_PROBE" "$0" > "$OUT/tool-sha256.txt"

"$PY" "$G2_PROBE" --config "$CFG" cmd \
  'g2status' \
  'g2evenai status' \
  'g2show "LIVE STT SHADOW - WAIT FOR TERMINAL PROMPT"' \
  'g2micreset' 'micsource g2' 'openmic' 'micsource' 'micread json' \
  2>&1 | tee "$OUT/preflight.log"

grep -Fq 'state=connected L=up R=up' "$OUT/preflight.log"
grep -Fq 'EvenAI session: idle id=-' "$OUT/preflight.log"
grep -Fq 'preference=g2, active=g2' "$OUT/preflight.log"
grep -Fq '"recording":false' "$OUT/preflight.log"
grep -Fq '"source":"g2"' "$OUT/preflight.log"
grep -Fq '"sampleRate":16000' "$OUT/preflight.log"
grep -Fq '"bitDepth":16' "$OUT/preflight.log"
grep -Fq '"channels":1' "$OUT/preflight.log"

sleep 3
"$PY" "$G2_PROBE" --config "$CFG" cmd 'g2micstats' \
  >"$OUT/g2micstats-before.log" 2>&1

set +e
# HW1_GATE_VERBOSE=1 turns on the probe's DEBUG logging (per-frame link and
# worker traces). Diagnosis aid only — leave off for timing-sensitive runs.
VERBOSE_FLAG=""
[ "${HW1_GATE_VERBOSE:-0}" = "1" ] && VERBOSE_FLAG="--verbose"
PYTHONPATH="$ROOT" "$PY" "$PROBE" \
  -c "$CFG" \
  $VERBOSE_FLAG \
  native-stt \
  --model-dir "$MODEL" \
  --model-arch medium-streaming \
  --update-interval 1.0 \
  --stt-queue-chunks 16 \
  --stt-soft-final-target 0.8 \
  --stt-final-timeout 2.0 \
  --expected-text "$EXPECTED_TEXT" \
  --output-dir "$OUT" \
  2>&1 | tee "$OUT/probe.log"
pipe_rc=("${PIPESTATUS[@]}")
PROBE_RC="${pipe_rc[0]}"
if (( pipe_rc[1] != 0 && PROBE_RC == 0 )); then
  PROBE_RC="${pipe_rc[1]}"
fi
set -e
printf '%s\n' "$PROBE_RC" > "$OUT/exit-code.txt"

vcgencmd get_throttled | tee "$OUT/throttled-after.txt"
if ! grep -Fxq 'throttled=0x0' "$OUT/throttled-after.txt"; then
  echo "POWER EVENT DURING THE RUN: $(cat "$OUT/throttled-after.txt") —" \
       "this run's timing is suspect; reboot the Pi and rerun." >&2
  exit 1
fi

if [ -s "$OUT/result.json" ]; then
"$PY" - "$OUT/result.json" <<'PY'
import json
import pathlib
import re
import sys

r = json.loads(pathlib.Path(sys.argv[1]).read_text())
eid = r["exchange_id"]
assert re.fullmatch(r"[0-9a-f]{16}", eid)
assert r["schema"] == 1
assert r["mode"] == "native_live_stt_shadow"
assert r["ok"] is True
assert r["stt_started"] is True
assert r["llm_started"] is False
assert r["ask_sent"] is False
assert r["reply_sent"] is False
assert r["expected_source"] == "g2"
assert r["begin"]["exchange_id"] == eid
assert r["begin"]["synthetic"] is False
assert r["begin"]["source"] == 2
assert r["begin"]["sample_rate"] == 16000
terminal = r["live"]["terminal"]
assert terminal["kind"] == "end"
assert terminal["valid"] is True
assert terminal["reason"] == 0
assert terminal["dropped_samples"] == 0
assert r["live"]["status_matches_terminal"] is True
assert r["live"]["inbox"]["fault_count"] == 0
assert r["live"]["inbox"]["late_frame_count"] == 0
assert r["live"]["inbox"]["last_fault"] is None
stt = r["streaming_stt"]
assert stt["valid"] is True
assert stt["done"] is True
assert stt["failure_reasons"] == []
acc = stt["accuracy"]
# Same acceptance the probe's stt_gate_ok uses: exact, or exact once the
# leading wake-fragment line is structurally discounted.
assert acc["exact_words"] or acc.get("exact_words_ignoring_leading_line"), acc
if acc["exact_words"]:
    assert acc["word_errors"] == 0
assert stt["audio"]["offered_bytes"] == r["live"]["bytes"]
assert stt["audio"]["enqueued_bytes"] == r["live"]["bytes"]
assert stt["audio"]["processed_bytes"] == r["live"]["bytes"]
q = stt["queue"]
assert q["logical_chunk_bytes"] == 4096
# Capacity is configurable (--stt-queue-chunks); assert self-consistency,
# not a hardcoded size.
assert q["capacity_chunks"] >= 1
assert q["capacity_bytes"] == q["capacity_chunks"] * 4096
assert q["capacity_ms"] == q["capacity_bytes"] * 1000 // 32000
assert q["overflowed"] is False
assert stt["final_policy"]["soft_target_seconds"] == 0.8
assert stt["final_policy"]["hard_timeout_seconds"] == 2.0
assert r["wav"]["canonical"] is True
assert r["wav"]["samples"] > 0
assert r["parity"]["applicable"] is False
assert r["parity"]["reason"] == "native_capture_trim_enabled"
assert r["lease_errors"] == []
assert r["control_errors"] == []
assert r["cleanup_order"] == [
    "shadow_off", "lease_release", "voicefetch",
    "micdeleteid", "g2evenai_exitid",
]
assert all(r["cleanup"].values())
for name in ("live_pcm", "wav", "result"):
    assert pathlib.Path(r["local_paths"][name]).is_file()
print(json.dumps({
    "ok": r["ok"],
    "exchange_id": eid,
    "text": stt["text"],
    "end_to_final_seconds": stt["stream"]["end_to_final_seconds"],
    "soft_target_met": stt["final_policy"]["soft_target_met"],
    "queue": stt["queue"],
}, sort_keys=True))
PY
fi

test "$PROBE_RC" -eq 0
