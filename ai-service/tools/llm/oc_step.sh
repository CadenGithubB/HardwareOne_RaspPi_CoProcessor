#!/usr/bin/env bash
# oc_step.sh — walk the CM5 up an overclock ladder one rung at a time.
#
#   ./tools/llm/oc_step.sh status                 # what is configured vs measured
#   sudo -n /usr/local/libexec/hw1-oc-helper stage 2600 0
#   sudo -n /usr/local/libexec/hw1-oc-helper reboot-try
#   ./tools/llm/oc_step.sh soak --minutes 15 --expected-mhz 2600 --expected-tryboot 1
#   ./tools/llm/oc_step.sh ladder                 # every rung tried, and its verdict
#
# Scope note, so this tool is not mistaken for a guaranteed speed win: on Pi 5
# / CM5 the SDRAM clock is not configurable. Decode may therefore gain much
# less than compute-heavy prefill, but this host has no valid stock-versus-OC
# A/B yet. Measure both phases at every rung.
#
# Trials use Raspberry Pi's one-shot tryboot path. A reset or power-cycle after
# a failed trial returns to untouched stock config.txt. The root-owned finite
# helper is the only supported boot-config writer; never run this user-writable
# script with sudo.

set -Eeuo pipefail
umask 022
export LC_ALL=C

OC_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
OC_ROOT="$(cd -- "$OC_SCRIPT_DIR/.." && pwd -P)"
OC_HOME="${HOME:?HOME must be set}"
OC_CONFIG_TXT="${OC_CONFIG_TXT:-/boot/firmware/config.txt}"
OC_RESULTS_DIR="${OC_RESULTS_DIR:-$OC_HOME/oc-results}"
OC_LADDER="$OC_RESULTS_DIR/ladder-v2.tsv"
OC_SERVICE="hw1-ai-service.service"
OC_PYTHON="${OC_PYTHON:-$OC_HOME/hw1ai/bin/python}"
OC_SERVICE_CONFIG="${OC_SERVICE_CONFIG:-$OC_HOME/.config/hw1-ai-service/config.yaml}"
# A port the daemon does not use, so a stray service instance cannot answer our
# determinism probe and make an unstable clock look clean.
OC_PROBE_PORT="${OC_PROBE_PORT:-8099}"
OC_POWER_HELPER="/usr/local/libexec/hw1-power-helper"
OC_FAN_SOCKET="/run/hw1-fan-controller/control.sock"
OC_LOCK_PATH="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/hw1-oc-soak.lock"

OC_BLOCK_BEGIN='# >>> hw1-oc (managed by tools/llm/oc_step.sh) >>>'
OC_BLOCK_END='# <<< hw1-oc <<<'

# These are guard rails, not recommendations. Start each rung with no manual
# voltage. Only after clean input power and a compute-correctness failure should
# over_voltage_delta be tried, in 10000 uV increments.
OC_MAX_MHZ=3000
OC_MIN_MHZ=1500
OC_MAX_DELTA_UV=50000

OC_SOAK_MINUTES=30
OC_SOAK_REPEATS=6
OC_MAX_TEMP_MILLIC=80000
OC_MIN_EXT5V_UV=4750000
OC_CLOCK_FLOOR_PERCENT=98
OC_CLOCK_HIT_PERCENT=95

OC_SERVICE_WAS_ACTIVE=0
OC_SERVER_PID=""
OC_MONITOR_PID=""
OC_SOAK_DIR=""
OC_PHASE_FILE=""
OC_CLEANUP_RAN=0
OC_POWER_CHANGED=0
OC_PREVIOUS_PROFILE=""
OC_FAN_CHANGED=0
OC_PREVIOUS_FAN_MODE=""
OC_DETERMINISM_HASH=""

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
  exit 1
}

need_root() {
  ((EUID == 0)) || die "$1 edits $OC_CONFIG_TXT — rerun with sudo"
}

# --- reading current state ---------------------------------------------------

configured_value() {
  local key="$1" value
  [[ -r "$OC_CONFIG_TXT" ]] || return 1
  # [0-9][0-9]* rather than [0-9]\+ : the latter is a GNU sed extension and
  # fails silently (empty match, no error) on BSD sed.
  value="$(sed -n "s/^[[:space:]]*${key}=\([0-9][0-9]*\).*/\1/p" "$OC_CONFIG_TXT" | tail -1)"
  [[ -n "$value" ]] || return 1
  printf '%s' "$value"
}

measured_arm_hz() {
  local clock
  clock="$(vcgencmd measure_clock arm 2>/dev/null)" || return 1
  [[ "$clock" =~ ^frequency\([0-9]+\)=([0-9]+)$ ]] || return 1
  printf '%s' "${BASH_REMATCH[1]}"
}

cpuinfo_max_hz() {
  local khz
  khz="$(cat /sys/devices/system/cpu/cpufreq/policy0/cpuinfo_max_freq 2>/dev/null)" || return 1
  [[ "$khz" =~ ^[0-9]+$ ]] || return 1
  printf '%s' "$((khz * 1000))"
}

current_temp_millic() {
  local raw whole fraction
  raw="$(vcgencmd measure_temp 2>/dev/null)" || return 1
  [[ "$raw" =~ ^temp=([0-9]+)\.([0-9]+)\'C$ ]] || return 1
  whole="${BASH_REMATCH[1]}"
  fraction="${BASH_REMATCH[2]}000"
  printf '%s' "$((10#$whole * 1000 + 10#${fraction:0:3}))"
}

# Bits 0-3 are live conditions; bits 16-19 are "has occurred since boot". An
# overclock soak cares about both, but they mean different things: live bits say
# the rung is failing right now, sticky bits say it failed at some point.
throttled_int() {
  local raw hex
  raw="$(vcgencmd get_throttled 2>/dev/null)" || return 1
  hex="${raw#throttled=}"
  [[ "$hex" =~ ^0x[0-9a-fA-F]+$ ]] || return 1
  printf '%d' "$((hex))"
}

throttled_raw() {
  local raw
  raw="$(vcgencmd get_throttled 2>/dev/null)" || return 1
  [[ "$raw" =~ ^throttled=0x[0-9a-fA-F]+$ ]] || return 1
  printf '%s' "$raw"
}

firmware_arm_mhz() {
  local raw
  raw="$(vcgencmd get_config arm_freq 2>/dev/null)" || return 1
  [[ "$raw" =~ ^arm_freq=([0-9]+)$ ]] || return 1
  printf '%s' "${BASH_REMATCH[1]}"
}

firmware_delta_uv() {
  local raw
  raw="$(vcgencmd get_config over_voltage_delta 2>/dev/null)" || return 1
  if [[ -z "$raw" ]]; then
    printf 0
  elif [[ "$raw" =~ ^over_voltage_delta=([0-9]+)$ ]]; then
    printf '%s' "${BASH_REMATCH[1]}"
  else
    return 1
  fi
}

describe_throttled() {
  local value="$1"
  local -a labels=()
  ((value & 0x1)) && labels+=("under-voltage now")
  ((value & 0x2)) && labels+=("frequency capped now")
  ((value & 0x4)) && labels+=("throttled now")
  ((value & 0x8)) && labels+=("soft temperature limit now")
  ((value & 0x10000)) && labels+=("under-voltage occurred")
  ((value & 0x20000)) && labels+=("frequency capping occurred")
  ((value & 0x40000)) && labels+=("throttling occurred")
  ((value & 0x80000)) && labels+=("soft temperature limit occurred")
  if ((${#labels[@]} == 0)); then
    printf clean
  else
    local IFS=', '
    printf '%s' "${labels[*]}"
  fi
}

ext5v_volts() {
  local raw value
  raw="$(vcgencmd pmic_read_adc EXT5V_V 2>/dev/null)" ||
    { printf unavailable; return 1; }
  value="$(awk '
    $1 == "EXT5V_V" {
      value = $0
      sub(/^.*=/, "", value)
      sub(/[[:space:]]*V[[:space:]]*$/, "", value)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      if (value !~ /^[0-9]+([.][0-9]+)?$/) exit 1
      print value
      found = 1
      exit
    }
    END { if (!found) exit 1 }
  ' <<< "$raw")" || { printf unavailable; return 1; }
  [[ "$value" =~ ^[0-9]+([.][0-9]+)?$ ]] || { printf unavailable; return 1; }
  printf '%s' "$value"
}

display_tsv() {
  local file="$1"
  if command -v column >/dev/null 2>&1; then
    column -t -s $'\t' < "$file"
  else
    cat -- "$file"
  fi
}

governors() {
  local path value found=0
  for path in /sys/devices/system/cpu/cpufreq/policy*/scaling_governor; do
    [[ -r "$path" ]] || continue
    value="$(cat -- "$path")"
    ((found > 0)) && printf ','
    printf '%s:%s' "$(basename -- "$(dirname -- "$path")")" "$value"
    found=$((found + 1))
  done
  ((found > 0)) || printf unavailable
}

cmd_status() {
  local conf_mhz conf_delta measured max_hz temp effective
  conf_mhz="$(configured_value arm_freq || true)"
  conf_delta="$(configured_value over_voltage_delta || true)"
  measured="$(measured_arm_hz || printf 0)"
  max_hz="$(cpuinfo_max_hz || printf 0)"
  temp="$(current_temp_millic || printf 0)"
  effective="$(firmware_arm_mhz || true)"
  printf 'config file      : %s\n' "$OC_CONFIG_TXT"
  printf 'arm_freq         : %s\n' "${conf_mhz:-<unset, firmware default 2400>}"
  printf 'firmware effective: %s MHz\n' "${effective:-<not reported>}"
  printf 'over_voltage_delta: %s uV\n' "${conf_delta:-0}"
  printf 'kernel max       : %s MHz\n' "$((max_hz / 1000000))"
  printf 'measured now     : %s MHz  (idle governors clock down; read it under load)\n' \
    "$((measured / 1000000))"
  printf 'temperature      : %s.%s C\n' "$((temp / 1000))" "$(((temp % 1000) / 100))"
  printf 'EXT5V now        : %s V\n' "$(ext5v_volts || true)"
  printf 'governors        : %s\n' "$(governors)"
  printf 'throttled        : %s\n' "$(vcgencmd get_throttled 2>/dev/null || printf unknown)"
  if grep -qs '^[[:space:]]*force_turbo=1' "$OC_CONFIG_TXT"; then
    printf '\nWARNING: force_turbo=1 is set. It raises idle power and heat and is not\n'
    printf 'needed for a sustained all-core benchmark. Remove it.\n'
  fi
  if [[ -r "$OC_LADDER" ]]; then
    printf '\n--- ladder so far ---\n'
    display_tsv "$OC_LADDER"
  fi
}

# --- writing a rung ----------------------------------------------------------

cmd_set() {
  local mhz="${1:-}" delta=0 backup tmp
  shift || true
  while (($#)); do
    case "$1" in
      --delta-uv)
        (($# >= 2)) || die "--delta-uv requires a value"
        delta="$2"
        shift 2
        ;;
      *) die "unknown option for set: $1" ;;
    esac
  done

  # Validate before the privilege check, so a bad value is rejected without
  # making you re-run under sudo to find out.
  [[ "$mhz" =~ ^[0-9]+$ ]] || die "set requires a frequency in MHz, e.g. 'set 2600'"
  ((mhz >= OC_MIN_MHZ && mhz <= OC_MAX_MHZ)) ||
    die "refusing arm_freq=$mhz; allowed range is $OC_MIN_MHZ..$OC_MAX_MHZ MHz"
  [[ "$delta" =~ ^[0-9]+$ ]] || die "--delta-uv must be a non-negative integer of microvolts"
  ((delta <= OC_MAX_DELTA_UV)) ||
    die "refusing over_voltage_delta=$delta; ceiling is $OC_MAX_DELTA_UV uV (+0.05 V)"
  need_root set
  [[ -w "$OC_CONFIG_TXT" ]] || die "cannot write $OC_CONFIG_TXT"

  backup="$OC_CONFIG_TXT.hw1-oc-bak-$(date +%Y%m%d-%H%M%S)"
  cp -a -- "$OC_CONFIG_TXT" "$backup"

  tmp="$(mktemp "$OC_CONFIG_TXT.hw1-oc-XXXXXX")"
  # Strip any previous managed block, then append the new one. Settings outside
  # the markers are never touched.
  awk -v b="$OC_BLOCK_BEGIN" -v e="$OC_BLOCK_END" '
    $0 == b { skip = 1; next }
    $0 == e { skip = 0; next }
    !skip   { print }
  ' "$OC_CONFIG_TXT" > "$tmp"
  {
    printf '%s\n' "$OC_BLOCK_BEGIN"
    printf '[cm5]\n'
    printf 'arm_freq=%s\n' "$mhz"
    # over_voltage_delta, NOT over_voltage: the delta form is added to whatever
    # DVFS computes and preserves the curve. Plain over_voltage disables the
    # firmware's automatic overclock-voltage selection.
    ((delta > 0)) && printf 'over_voltage_delta=%s\n' "$delta"
    printf '[all]\n'
    printf '%s\n' "$OC_BLOCK_END"
  } >> "$tmp"
  chmod --reference="$OC_CONFIG_TXT" "$tmp"
  mv -- "$tmp" "$OC_CONFIG_TXT"
  sync

  printf 'Wrote arm_freq=%s' "$mhz"
  ((delta > 0)) && printf ' over_voltage_delta=%s' "$delta"
  printf ' to %s\n' "$OC_CONFIG_TXT"
  printf 'Backup: %s\n' "$backup"
  printf '\n--- managed block ---\n'
  sed -n "/^${OC_BLOCK_BEGIN//\//\\/}$/,/^${OC_BLOCK_END//\//\\/}$/p" "$OC_CONFIG_TXT"
  printf '\nReboot, then: ./tools/llm/oc_step.sh soak\n'
  printf 'If it will not boot: power off, put the SD card in another machine, and\n'
  printf 'delete the block between the two hw1-oc markers (or restore the backup).\n'
}

# Removes the managed block outright rather than restoring a backup.
#
# Restoring the newest backup was WRONG and shipped a real failure: `set` backs
# up before writing, so after two `set` calls the newest backup already contains
# the first call's overclock. Reverting to it restored 2800 MHz while reporting
# success. Stripping the block is idempotent and correct no matter how many
# times `set` ran. Backups are still written by `set`; they are for disaster
# recovery, not for this.
cmd_revert() {
  local tmp backup had_block
  need_root revert
  [[ -w "$OC_CONFIG_TXT" ]] || die "cannot write $OC_CONFIG_TXT"
  had_block="$(grep -c -F -- "$OC_BLOCK_BEGIN" "$OC_CONFIG_TXT" || true)"
  if [[ "$had_block" == 0 ]]; then
    printf 'No hw1-oc block in %s — already at firmware defaults.\n' "$OC_CONFIG_TXT"
    grep -n 'arm_freq\|over_voltage' "$OC_CONFIG_TXT" >&2 &&
      printf 'NOTE: the settings above are outside the managed block; this tool did not\n'\
'write them and will not remove them.\n' >&2
    return 0
  fi
  backup="$OC_CONFIG_TXT.hw1-oc-bak-$(date +%Y%m%d-%H%M%S)"
  cp -a -- "$OC_CONFIG_TXT" "$backup"
  tmp="$(mktemp "$OC_CONFIG_TXT.hw1-oc-XXXXXX")"
  awk -v b="$OC_BLOCK_BEGIN" -v e="$OC_BLOCK_END" '
    $0 == b { skip = 1; next }
    $0 == e { skip = 0; next }
    !skip   { print }
  ' "$OC_CONFIG_TXT" > "$tmp"
  chmod --reference="$OC_CONFIG_TXT" "$tmp"
  mv -- "$tmp" "$OC_CONFIG_TXT"
  sync
  printf 'Removed the hw1-oc block from %s (backup: %s).\n' "$OC_CONFIG_TXT" "$backup"
  printf 'Remaining arm_freq/over_voltage lines, if any:\n'
  grep -n 'arm_freq\|over_voltage' "$OC_CONFIG_TXT" || printf '  (none)\n'
  printf 'Reboot to apply.\n'
}

# --- soak --------------------------------------------------------------------

cleanup_soak() {
  local rc=$? restore_failed=0
  ((OC_CLEANUP_RAN == 0)) || return
  OC_CLEANUP_RAN=1
  trap - EXIT
  trap '' INT TERM HUP
  set +e
  [[ -n "$OC_MONITOR_PID" ]] && terminate_pid "$OC_MONITOR_PID"
  [[ -n "$OC_SERVER_PID" ]] && terminate_pid "$OC_SERVER_PID"
  if ((OC_FAN_CHANGED == 1)); then
    if ! fan_request "mode $OC_PREVIOUS_FAN_MODE" >/dev/null; then
      printf 'ERROR: failed to restore fan mode %s\n' "$OC_PREVIOUS_FAN_MODE" >&2
      restore_failed=1
    fi
  fi
  if ((OC_POWER_CHANGED == 1)); then
    if ! sudo -n "$OC_POWER_HELPER" profile "$OC_PREVIOUS_PROFILE" >/dev/null; then
      printf 'ERROR: failed to restore power profile %s\n' "$OC_PREVIOUS_PROFILE" >&2
      restore_failed=1
    fi
  fi
  if ((OC_SERVICE_WAS_ACTIVE == 1)); then
    if ! systemctl --user start "$OC_SERVICE" >/dev/null 2>&1 ||
       ! systemctl --user is-active --quiet "$OC_SERVICE"; then
      printf 'ERROR: failed to restore %s\n' "$OC_SERVICE" >&2
      restore_failed=1
    fi
  fi
  if ((rc == 0 && restore_failed != 0)); then
    rc=1
  fi
  exit "$rc"
}

terminate_pid() {
  local pid="$1" i
  kill "$pid" 2>/dev/null || true
  for i in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.25
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
  fi
  wait "$pid" 2>/dev/null || true
}

json_field() {
  local field="$1"
  "$OC_PYTHON" -c \
    'import json,sys; value=json.load(sys.stdin).get(sys.argv[1]); print("" if value is None else value)' \
    "$field"
}

fan_request() {
  local command="$1"
  "$OC_PYTHON" - "$OC_FAN_SOCKET" "$command" <<'PY'
import socket
import sys

path, command = sys.argv[1:]
client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.settimeout(3.0)
try:
    client.connect(path)
    client.sendall(command.encode("ascii") + b"\n")
    response = b""
    while not response.endswith(b"\n") and len(response) < 4096:
        chunk = client.recv(4096 - len(response))
        if not chunk:
            break
        response += chunk
finally:
    client.close()
if not response.endswith(b"\n"):
    raise SystemExit("incomplete fan-controller response")
sys.stdout.buffer.write(response)
PY
}

prepare_power_and_fan() {
  local power_json fan_json fan_health fan_pwm
  [[ -x "$OC_POWER_HELPER" ]] ||
    die "root-owned power helper is not installed: $OC_POWER_HELPER"
  power_json="$(sudo -n "$OC_POWER_HELPER" status)" ||
    die "cannot query the root-owned power helper non-interactively"
  OC_PREVIOUS_PROFILE="$(json_field profile <<< "$power_json")"
  case "$OC_PREVIOUS_PROFILE" in
    eco|balanced|performance) ;;
    *) die "power helper returned an unsafe/unknown profile: $OC_PREVIOUS_PROFILE" ;;
  esac
  if [[ "$OC_PREVIOUS_PROFILE" != performance ]]; then
    sudo -n "$OC_POWER_HELPER" profile performance > "$OC_SOAK_DIR/power-performance.json" ||
      die "could not enter the performance governor"
    OC_POWER_CHANGED=1
  fi
  [[ "$(json_field profile <<< "$(sudo -n "$OC_POWER_HELPER" status)")" == performance ]] ||
    die "not every CPU policy entered the performance governor"

  if [[ -S "$OC_FAN_SOCKET" ]]; then
    fan_json="$(fan_request status)" || die "fan controller exists but is not queryable"
    OC_PREVIOUS_FAN_MODE="$(json_field requested_mode <<< "$fan_json")"
    case "$OC_PREVIOUS_FAN_MODE" in auto|quiet|max) ;; *) die "invalid fan mode: $OC_PREVIOUS_FAN_MODE" ;; esac
    fan_request 'mode max' > "$OC_SOAK_DIR/fan-max.json" || die "could not set fan to max"
    OC_FAN_CHANGED=1
    sleep 2
    fan_json="$(fan_request status)" || die "could not verify max fan mode"
    [[ "$(json_field effective_mode <<< "$fan_json")" == max ]] || die "fan did not enter max mode"
    fan_health="$(json_field health <<< "$fan_json")"
    fan_pwm="$(json_field pwm <<< "$fan_json")"
    [[ "$fan_pwm" == 255 ]] || die "fan controller reported pwm=$fan_pwm instead of 255"
    case "$fan_health" in ok|boosting|tach_unavailable) ;; *) die "fan health is $fan_health" ;; esac
    printf '%s\n' "$fan_json" > "$OC_SOAK_DIR/fan-status.json"
  else
    printf 'unavailable\n' > "$OC_SOAK_DIR/fan-status.txt"
  fi
}

start_monitor() {
  local out="$OC_SOAK_DIR/telemetry.tsv"
  printf 'epoch_s\ttimestamp\tphase\ttemp_millic\tthrottled\tcore_volts\tarm_hz\text5v_v\n' > "$out"
  (
    local phase temp throttle core_volts arm_hz ext5v
    while :; do
      phase="$(cat -- "$OC_PHASE_FILE" 2>/dev/null || printf unknown)"
      temp="$(current_temp_millic 2>/dev/null || printf unavailable)"
      throttle="$(throttled_raw 2>/dev/null || printf unavailable)"
      core_volts="$(vcgencmd measure_volts core 2>/dev/null || printf unavailable)"
      arm_hz="$(measured_arm_hz 2>/dev/null || printf unavailable)"
      ext5v="$(ext5v_volts 2>/dev/null || printf unavailable)"
      printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$(date +%s)" \
        "$(date --iso-8601=seconds)" \
        "$phase" \
        "$temp" \
        "$throttle" \
        "$core_volts" \
        "$arm_hz" \
        "$ext5v" >> "$out"
      sleep 1
    done
  ) &
  OC_MONITOR_PID=$!
}

set_monitor_phase() {
  printf '%s\n' "$1" > "$OC_PHASE_FILE"
}

# Phase 2 of the soak, and the one that matters most here. stress-ng proves the
# chip can run hot without faulting; it does NOT prove the NEON/i8mm GEMM
# kernels llama.cpp actually uses produce correct results. Greedy decoding with
# a fixed seed and a fixed thread count is deterministic on CPU, so identical
# prompts must yield byte-identical completions. A rung that returns a different
# answer on repeat 4 is computing wrong numbers — the failure mode that never
# shows up as a crash and quietly corrupts every benchmark you run afterwards.
determinism_probe() {
  local server_bin="$1" model="$2" repeats="$3" expected_hash="$4"
  local i hash byte_count result response first="" mismatches=0 ready=0

  if command -v ss >/dev/null 2>&1 &&
     [[ -n "$(ss -H -ltn "sport = :$OC_PROBE_PORT" 2>/dev/null)" ]]; then
    printf 'determinism probe: TCP port %s already has a listener\n' "$OC_PROBE_PORT" >&2
    return 1
  fi
  if curl -fsS --connect-timeout 1 --max-time 2 \
      "http://127.0.0.1:$OC_PROBE_PORT/health" >/dev/null 2>&1; then
    printf 'determinism probe: an existing server answered on port %s\n' "$OC_PROBE_PORT" >&2
    return 1
  fi

  "$server_bin" --model "$model" --host 127.0.0.1 --port "$OC_PROBE_PORT" \
    -t 4 -c 2048 --parallel 1 > "$OC_SOAK_DIR/llama-server.log" 2>&1 &
  OC_SERVER_PID=$!

  for _ in $(seq 1 120); do
    if curl -fsS --connect-timeout 1 --max-time 2 \
        "http://127.0.0.1:$OC_PROBE_PORT/health" >/dev/null 2>&1; then
      kill -0 "$OC_SERVER_PID" 2>/dev/null || break
      ready=1
      break
    fi
    if ! kill -0 "$OC_SERVER_PID" 2>/dev/null; then
      printf 'determinism probe: llama-server died during startup (see llama-server.log)\n' >&2
      OC_SERVER_PID=""
      return 1
    fi
    sleep 1
  done
  if ((ready == 0)); then
    printf 'determinism probe: llama-server did not become ready (see llama-server.log)\n' >&2
    terminate_pid "$OC_SERVER_PID"
    OC_SERVER_PID=""
    return 1
  fi

  for ((i = 1; i <= repeats; ++i)); do
    response="$OC_SOAK_DIR/determinism-$i.json"
    if ! curl -fsS --connect-timeout 2 --max-time 300 \
      -H 'Content-Type: application/json' \
      -d '{"prompt":"Explain in exactly three sentences how a lithium-ion battery stores and releases energy.","n_predict":160,"temperature":0,"top_k":1,"seed":42,"cache_prompt":false}' \
      "http://127.0.0.1:$OC_PROBE_PORT/completion" \
      > "$response"; then
      result="REQUEST-FAILED"
    else
      result="$("$OC_PYTHON" - "$response" <<'PY'
import hashlib
import json
import pathlib
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_text())
content = value.get("content")
if not isinstance(content, str):
    raise SystemExit("completion content is not a string")
encoded = content.encode("utf-8")
if len(encoded) < 64:
    raise SystemExit("completion content is implausibly short")
print(hashlib.sha256(encoded).hexdigest(), len(encoded), sep="\t")
PY
      )" || result="REQUEST-FAILED"
    fi
    if [[ "$result" == REQUEST-FAILED ]]; then
      hash="REQUEST-FAILED"
      byte_count=0
    else
      IFS=$'\t' read -r hash byte_count <<< "$result"
      [[ "$hash" =~ ^[0-9a-f]{64}$ && "$byte_count" =~ ^[0-9]+$ ]] || {
        hash="REQUEST-FAILED"
        byte_count=0
      }
    fi
    printf '%d\t%s\t%s\n' "$i" "$hash" "$byte_count" >> "$OC_SOAK_DIR/determinism.tsv"
    if [[ -z "$first" ]]; then
      first="$hash"
    elif [[ "$hash" != "$first" ]]; then
      mismatches=$((mismatches + 1))
    fi
    printf '  determinism %d/%d: %s%s\n' "$i" "$repeats" "${hash:0:16}" \
      "$([[ -n "$first" && "$hash" != "$first" ]] && printf '  <-- MISMATCH' || true)"
  done

  terminate_pid "$OC_SERVER_PID"
  OC_SERVER_PID=""
  [[ "$first" != "REQUEST-FAILED" ]] || return 1
  ((mismatches == 0)) || return 1
  if [[ -n "$expected_hash" && "$first" != "$expected_hash" ]]; then
    printf 'determinism probe: completion differs from clean-stock golden hash\n' >&2
    return 1
  fi
  OC_DETERMINISM_HASH="$first"
  printf '%s\n' "$first" > "$OC_SOAK_DIR/completion.sha256"
}

cmd_soak() {
  local minutes="$OC_SOAK_MINUTES" repeats="$OC_SOAK_REPEATS"
  local server_bin model before_throttle after_throttle peak_temp verdict reasons
  local stress_rc=0 determinism_rc=0 max_hz expected_hz active_mhz active_delta
  local expected_mhz="" expected_delta=0 expected_hash="" expected_tryboot="" run_label=""
  local min_ext5v_uv stress_samples clock_hits clock_hit_percent monitor_alive=0
  local valid_temp valid_throttle valid_clock valid_ext throttle_bad minimum_samples
  local temp_coverage throttle_coverage clock_coverage ext_coverage last_epoch sample_age
  local service_state_rc boot_id tryboot_value

  while (($#)); do
    case "$1" in
      --minutes) (($# >= 2)) || die "--minutes requires a value"; minutes="$2"; shift 2 ;;
      --repeats) (($# >= 2)) || die "--repeats requires a value"; repeats="$2"; shift 2 ;;
      --expected-mhz) (($# >= 2)) || die "--expected-mhz requires a value"; expected_mhz="$2"; shift 2 ;;
      --expected-delta-uv) (($# >= 2)) || die "--expected-delta-uv requires a value"; expected_delta="$2"; shift 2 ;;
      --expected-hash) (($# >= 2)) || die "--expected-hash requires a value"; expected_hash="$2"; shift 2 ;;
      --expected-tryboot) (($# >= 2)) || die "--expected-tryboot requires a value"; expected_tryboot="$2"; shift 2 ;;
      --label) (($# >= 2)) || die "--label requires a value"; run_label="$2"; shift 2 ;;
      *) die "unknown option for soak: $1" ;;
    esac
  done
  [[ "$minutes" =~ ^[0-9]+$ ]] && ((minutes >= 1)) || die "--minutes must be >= 1"
  [[ "$repeats" =~ ^[0-9]+$ ]] && ((repeats >= 2)) || die "--repeats must be >= 2"
  [[ -z "$expected_mhz" || "$expected_mhz" =~ ^[0-9]+$ ]] || die "--expected-mhz must be an integer"
  [[ "$expected_delta" =~ ^[0-9]+$ ]] || die "--expected-delta-uv must be an integer"
  [[ -z "$expected_hash" || "$expected_hash" =~ ^[0-9a-f]{64}$ ]] || die "--expected-hash must be a lowercase SHA-256"
  [[ -z "$expected_tryboot" || "$expected_tryboot" == 0 || "$expected_tryboot" == 1 ]] || die "--expected-tryboot must be 0 or 1"
  [[ -z "$run_label" || "$run_label" =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]] || die "--label contains unsafe characters"
  [[ "$OC_PROBE_PORT" =~ ^[0-9]+$ ]] || die "OC_PROBE_PORT must be an integer"
  OC_PROBE_PORT=$((10#$OC_PROBE_PORT))
  ((OC_PROBE_PORT >= 1 && OC_PROBE_PORT <= 65535)) || die "OC_PROBE_PORT must be 1..65535"
  minutes=$((10#$minutes))
  repeats=$((10#$repeats))
  [[ -z "$expected_mhz" ]] || expected_mhz=$((10#$expected_mhz))
  expected_delta=$((10#$expected_delta))
  [[ -z "$expected_tryboot" ]] || expected_tryboot=$((10#$expected_tryboot))

  command -v vcgencmd >/dev/null 2>&1 || die "vcgencmd is required"
  command -v curl >/dev/null 2>&1 || die "curl is required"
  command -v flock >/dev/null 2>&1 || die "flock is required"
  command -v stress-ng >/dev/null 2>&1 || die "stress-ng is required"
  [[ -x "$OC_PYTHON" ]] || die "service Python not found: $OC_PYTHON"
  [[ -r "$OC_SERVICE_CONFIG" ]] || die "service config not readable: $OC_SERVICE_CONFIG"

  read -r server_bin model < <(
    PYTHONPATH="$OC_ROOT" "$OC_PYTHON" - "$OC_SERVICE_CONFIG" <<'PY'
import os, sys
from hw1_ai_service.config import load
cfg = load(sys.argv[1])
print(os.path.expanduser(cfg.llm.server_bin), os.path.expanduser(cfg.llm.model))
PY
  )
  [[ -x "$server_bin" ]] || die "llama-server not executable: $server_bin
Build it with: cmake --build $(dirname -- "$(dirname -- "$server_bin")") --target llama-server -j4"
  [[ -r "$model" ]] || die "active model not readable: $model"

  mkdir -p -- "$OC_RESULTS_DIR"
  [[ -d "$(dirname -- "$OC_LOCK_PATH")" ]] || die "runtime lock directory is unavailable"
  [[ ! -L "$OC_LOCK_PATH" ]] || die "refusing a symlink at the lock path"
  exec 9>"$OC_LOCK_PATH"
  flock -n 9 || die "another overclock soak is active"
  if [[ -n "$run_label" ]]; then
    OC_SOAK_DIR="$OC_RESULTS_DIR/$run_label"
    mkdir -- "$OC_SOAK_DIR" || die "result label already exists: $run_label"
  else
    OC_SOAK_DIR="$(mktemp -d "$OC_RESULTS_DIR/soak-$(date +%Y%m%d-%H%M%S)-XXXXXX")"
  fi
  OC_PHASE_FILE="$OC_SOAK_DIR/phase"
  set_monitor_phase preflight
  trap cleanup_soak EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP

  before_throttle="$(throttled_int)" || die "could not parse vcgencmd get_throttled"
  if ((before_throttle != 0)); then
    die "get_throttled is already 0x$(printf '%x' "$before_throttle") ($(describe_throttled "$before_throttle")); reboot and require 0x0 before a certification soak"
  fi

  # CPU contention from the daemon would make a marginal rung look stable.
  if systemctl --user is-active --quiet "$OC_SERVICE"; then
    OC_SERVICE_WAS_ACTIVE=1
    systemctl --user stop "$OC_SERVICE"
    systemctl --user is-active --quiet "$OC_SERVICE" &&
      die "$OC_SERVICE remained active after stop"
    sleep 2
  else
    service_state_rc=$?
    ((service_state_rc == 3)) || die "could not determine $OC_SERVICE state (rc=$service_state_rc)"
  fi

  prepare_power_and_fan

  active_mhz="$(firmware_arm_mhz)" || die "could not parse firmware-effective arm_freq"
  active_delta="$(firmware_delta_uv)" || die "could not parse firmware-effective over_voltage_delta"
  [[ -n "$expected_mhz" ]] || expected_mhz="$active_mhz"
  ((active_mhz == expected_mhz)) ||
    die "firmware-effective arm_freq=$active_mhz does not match expected $expected_mhz MHz"
  ((active_delta == expected_delta)) ||
    die "firmware-effective over_voltage_delta=$active_delta does not match expected $expected_delta uV"
  max_hz="$(cpuinfo_max_hz)" || die "kernel CPU-frequency ceiling is unavailable"
  expected_hz="$((expected_mhz * 1000000))"
  boot_id="$(cat /proc/sys/kernel/random/boot_id)" || die "boot ID is unavailable"
  [[ "$boot_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] ||
    die "boot ID has an unexpected format: $boot_id"
  tryboot_value="$(od -An -tu4 --endian=big /proc/device-tree/chosen/bootloader/tryboot 2>/dev/null | tr -d '[:space:]' || true)"
  [[ -n "$tryboot_value" ]] || tryboot_value=0
  [[ "$tryboot_value" =~ ^[0-9]+$ ]] || die "tryboot state has an unexpected value: $tryboot_value"
  if [[ -n "$expected_tryboot" ]] && ((tryboot_value != expected_tryboot)); then
    die "boot tryboot=$tryboot_value does not match expected $expected_tryboot"
  fi

  {
    printf 'soak started    : %s\n' "$(date --iso-8601=seconds)"
    printf 'boot id         : %s\n' "$boot_id"
    printf 'tryboot         : %s\n' "$tryboot_value"
    printf 'arm_freq active : %s MHz\n' "$active_mhz"
    printf 'kernel max      : %s MHz\n' "$((max_hz / 1000000))"
    printf 'over_voltage_delta: %s uV\n' "$active_delta"
    printf 'minutes         : %s\n' "$minutes"
    printf 'determinism runs: %s\n' "$repeats"
    printf 'model           : %s\n' "$model"
  } | tee "$OC_SOAK_DIR/soak-info.txt"

  # The firmware silently ignores an arm_freq it will not honour, so a rung can
  # look "applied" in config.txt while the chip never runs it.
  if ((max_hz < expected_hz * OC_CLOCK_FLOOR_PERCENT / 100 ||
       max_hz > expected_hz * 102 / 100)); then
    die "kernel ceiling $((max_hz / 1000000)) MHz does not match the active $expected_mhz MHz rung"
  fi

  start_monitor

  printf '\n--- phase 1: %s min all-core soak ---\n' "$minutes"
  set_monitor_phase stress
  # --verify makes stress-ng check its own results, which is what turns this
  # from a heater into a correctness test.
  stress-ng --cpu 4 --cpu-method all --verify \
    --timeout "${minutes}m" --metrics-brief \
    > "$OC_SOAK_DIR/stress-ng.log" 2>&1 || stress_rc=$?
  tail -n 15 -- "$OC_SOAK_DIR/stress-ng.log"

  printf '\n--- phase 2: %s identical greedy completions ---\n' "$repeats"
  set_monitor_phase determinism
  determinism_probe "$server_bin" "$model" "$repeats" "$expected_hash" || determinism_rc=$?
  set_monitor_phase complete

  kill -0 "$OC_MONITOR_PID" 2>/dev/null && monitor_alive=1
  terminate_pid "$OC_MONITOR_PID"
  OC_MONITOR_PID=""

  after_throttle="$(throttled_int)" || die "could not parse final vcgencmd get_throttled"
  peak_temp="$(awk -F'\t' 'NR>1 && $4 ~ /^[0-9]+$/ && $4+0 > m {m = $4+0; found=1} END {if (found) print m; else print 0}' "$OC_SOAK_DIR/telemetry.tsv")"
  min_ext5v_uv="$(awk -F'\t' '
    NR>1 && $8 ~ /^[0-9]+([.][0-9]+)?$/ {
      uv=int($8 * 1000000 + 0.5)
      if (!found || uv < min) min=uv
      found=1
    }
    END {if (found) print min; else print 0}
  ' "$OC_SOAK_DIR/telemetry.tsv")"
  read -r stress_samples valid_temp valid_throttle valid_clock valid_ext clock_hits throttle_bad last_epoch < <(awk -F'\t' \
    -v floor="$((expected_hz * OC_CLOCK_FLOOR_PERCENT / 100))" \
    -v ceiling="$((expected_hz * 102 / 100))" '
    NR>1 {
      if ($1 ~ /^[0-9]+$/) last=$1
      if ($3 != "stress") next
      samples++
      if ($4 ~ /^[0-9]+$/ && $4+0 > 0) valid_temp++
      if ($5 ~ /^throttled=0x[0-9a-fA-F]+$/) {
        valid_throttle++
        if ($5 != "throttled=0x0") throttle_bad++
      }
      if ($7 ~ /^[0-9]+$/) {
        valid_clock++
        if ($7+0 >= floor && $7+0 <= ceiling) hits++
      }
      if ($8 ~ /^[0-9]+([.][0-9]+)?$/) valid_ext++
    }
    END {print samples+0, valid_temp+0, valid_throttle+0, valid_clock+0, valid_ext+0, hits+0, throttle_bad+0, last+0}
  ' "$OC_SOAK_DIR/telemetry.tsv")
  if ((stress_samples > 0)); then
    clock_hit_percent=$((clock_hits * 100 / stress_samples))
    temp_coverage=$((valid_temp * 100 / stress_samples))
    throttle_coverage=$((valid_throttle * 100 / stress_samples))
    clock_coverage=$((valid_clock * 100 / stress_samples))
    ext_coverage=$((valid_ext * 100 / stress_samples))
  else
    clock_hit_percent=0
    temp_coverage=0
    throttle_coverage=0
    clock_coverage=0
    ext_coverage=0
  fi
  minimum_samples=$((minutes * 30))
  sample_age=$(($(date +%s) - last_epoch))

  reasons=""
  verdict="PASS"
  if ((determinism_rc != 0)); then
    verdict="FAIL"
    reasons="${reasons}nondeterministic or failed completions; "
  fi
  if ((stress_rc != 0)); then
    verdict="FAIL"
    reasons="${reasons}stress-ng rc=$stress_rc; "
  fi
  if ((after_throttle != 0)); then
    verdict="FAIL"
    reasons="${reasons}final throttle word 0x$(printf '%x' "$after_throttle") ($(describe_throttled "$after_throttle")); "
  fi
  if ((peak_temp > OC_MAX_TEMP_MILLIC)); then
    verdict="FAIL"
    reasons="${reasons}peak ${peak_temp} millic over $((OC_MAX_TEMP_MILLIC / 1000)) C; "
  fi
  if ((peak_temp == 0)); then
    verdict="FAIL"
    reasons="${reasons}temperature telemetry unavailable; "
  fi
  if ((min_ext5v_uv == 0)); then
    verdict="FAIL"
    reasons="${reasons}EXT5V telemetry unavailable; "
  elif ((min_ext5v_uv < OC_MIN_EXT5V_UV)); then
    verdict="FAIL"
    reasons="${reasons}sampled EXT5V minimum $((min_ext5v_uv / 1000)) mV below 4750 mV floor; "
  fi
  if ((monitor_alive == 0)); then
    verdict="FAIL"
    reasons="${reasons}telemetry monitor died before collection ended; "
  fi
  if ((stress_samples < minimum_samples)); then
    verdict="FAIL"
    reasons="${reasons}only $stress_samples stress telemetry rows (need $minimum_samples); "
  fi
  if ((temp_coverage < 95 || throttle_coverage < 95 || clock_coverage < 95 || ext_coverage < 95)); then
    verdict="FAIL"
    reasons="${reasons}telemetry coverage temp/throttle/clock/EXT5V=${temp_coverage}/${throttle_coverage}/${clock_coverage}/${ext_coverage}% (need 95% each); "
  fi
  if ((sample_age < 0 || sample_age > 5)); then
    verdict="FAIL"
    reasons="${reasons}telemetry tail is ${sample_age}s old; "
  fi
  if ((throttle_bad != 0)); then
    verdict="FAIL"
    reasons="${reasons}$throttle_bad nonzero throttle samples during stress; "
  fi
  if ((stress_samples == 0 || clock_hit_percent < OC_CLOCK_HIT_PERCENT)); then
    verdict="FAIL"
    reasons="${reasons}ARM clock reached at least $OC_CLOCK_FLOOR_PERCENT% of target for ${clock_hit_percent}% of stress samples (need ${OC_CLOCK_HIT_PERCENT}%); "
  fi
  [[ -n "$reasons" ]] || reasons="clean"

  [[ -r "$OC_LADDER" ]] || printf 'when\tarm_mhz\tdelta_uv\tverdict\tpeak_temp_C\tmin_ext5v_V\tclock_hit_pct\tthrottled_after\tcompletion_sha256\tnotes\tdir\n' > "$OC_LADDER"
  printf '%s\t%s\t%s\t%s\t%s.%s\t%s.%06d\t%s\t0x%x\t%s\t%s\t%s\n' \
    "$(date --iso-8601=seconds)" \
    "$((expected_hz / 1000000))" \
    "$active_delta" \
    "$verdict" \
    "$((peak_temp / 1000))" "$(((peak_temp % 1000) / 100))" \
    "$((min_ext5v_uv / 1000000))" "$((min_ext5v_uv % 1000000))" \
    "$clock_hit_percent" \
    "$after_throttle" \
    "${OC_DETERMINISM_HASH:-unavailable}" \
    "$reasons" \
    "$OC_SOAK_DIR" >> "$OC_LADDER"

  printf '\n===== %s @ %s MHz =====\n' "$verdict" "$((expected_hz / 1000000))"
  printf 'peak temp   : %s.%s C\n' "$((peak_temp / 1000))" "$(((peak_temp % 1000) / 100))"
  printf 'min EXT5V   : %s.%06d V (1 Hz samples; faster dips remain possible)\n' \
    "$((min_ext5v_uv / 1000000))" "$((min_ext5v_uv % 1000000))"
  printf 'clock target: %s%% of stress samples at >= %s%% of requested clock\n' \
    "$clock_hit_percent" "$OC_CLOCK_FLOOR_PERCENT"
  printf 'throttled   : 0x%x\n' "$after_throttle"
  printf 'notes       : %s\n' "$reasons"
  printf 'evidence    : %s\n' "$OC_SOAK_DIR"
  "$OC_PYTHON" - "$OC_SOAK_DIR/result.json" "$verdict" "$expected_mhz" "$active_delta" \
    "$peak_temp" "$min_ext5v_uv" "$clock_hit_percent" "$after_throttle" \
    "${OC_DETERMINISM_HASH:-}" "$boot_id" "$tryboot_value" "$reasons" <<'PY'
import json
import pathlib
import sys

(path, verdict, mhz, delta, peak, ext5v, clock_pct, throttled,
 completion_hash, boot_id, tryboot, reasons) = sys.argv[1:]
value = {
    "schema": 1,
    "verdict": verdict,
    "arm_mhz": int(mhz),
    "delta_uv": int(delta),
    "peak_temp_millic": int(peak),
    "min_ext5v_uv": int(ext5v),
    "clock_hit_percent": int(clock_pct),
    "throttled_after": int(throttled),
    "completion_sha256": completion_hash or None,
    "boot_id": boot_id,
    "tryboot": int(tryboot),
    "notes": reasons,
}
pathlib.Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
PY
  printf 'RESULT_JSON=%s\n' "$OC_SOAK_DIR/result.json"
  printf '\nLadder:\n'
  display_tsv "$OC_LADDER"

  [[ "$verdict" == PASS ]]
}

cmd_ladder() {
  [[ -r "$OC_LADDER" ]] || die "no ladder yet: $OC_LADDER"
  display_tsv "$OC_LADDER"
}

case "${1:-}" in
  status) shift; cmd_status "$@" ;;
  set|revert) die "direct config editing is disabled; use the installed root-owned hw1-oc-helper tryboot workflow" ;;
  soak) shift; cmd_soak "$@" ;;
  ladder) shift; cmd_ladder "$@" ;;
  *) usage ;;
esac
