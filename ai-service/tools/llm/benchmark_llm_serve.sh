#!/usr/bin/env bash
# Measure real llama-server latency for the pinned candidate ladder.
#
# This is the companion to benchmark_llm_models.sh, not a replacement. That
# script answers "how fast does this GGUF decode"; llama-bench drives
# llama_decode directly and never starts a server, so it cannot see speculative
# decoding at all — an MTP arm and a plain arm produce identical llama-bench
# numbers — and its synthetic 128-token prompt says nothing about
# time-to-first-token behind the production system prompt with the KV prefix
# warm. This script answers "how fast does a turn feel", by driving the same
# supervisor and client the daemon uses.
#
# Like its companion it never edits config.yaml, never moves the active model,
# and restores the user service and power profile on every exit path.

set -Eeuo pipefail
umask 077
export LC_ALL=C

SERVE_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SERVE_ROOT="$(cd -- "$SERVE_SCRIPT_DIR/.." && pwd -P)"
SERVE_ACCOUNT_HOME="${HOME:?HOME must name the CM5 service account home}"
SERVE_CONFIG="$SERVE_ACCOUNT_HOME/.config/hw1-ai-service/config.yaml"
SERVE_PYTHON="$SERVE_ACCOUNT_HOME/hw1ai/bin/python"
SERVE_MANIFEST="$SERVE_SCRIPT_DIR/llm_serve_models.tsv"
SERVE_PROBE="$SERVE_SCRIPT_DIR/llm_serve_probe.py"
SERVE_DOWNLOADER="$SERVE_SCRIPT_DIR/benchmark_llm_models.sh"
# Shared with benchmark_llm_models.sh on purpose: a candidate downloaded by
# either tool is reused by the other instead of being fetched twice.
SERVE_MODELS_DIR="$SERVE_ACCOUNT_HOME/models/hw1-llm-bench"
SERVE_RESULTS_ROOT="$SERVE_ACCOUNT_HOME/llm-serve-results"
SERVE_BIN_OVERRIDE=""
SERVE_ACTIVE_MODEL_OVERRIDE=""
SERVE_SERVICE="hw1-ai-service.service"
SERVE_POWER_HELPER="/usr/local/libexec/hw1-power-helper"
SERVE_MODE="all"
SERVE_STRICT_POWER=0
SERVE_THROTTLED_BEFORE=0
SERVE_REPEATS=2
SERVE_ARM_TIMEOUT=25m
SERVE_MAX_TEMP_MILLIC=80000
SERVE_MIN_ARM_HZ=2300000000
# Moonshine medium-streaming-en resident size, per docs/CM5_PI5_PERFORMANCE_RECORD.md
# section 7. Used only to report whether an arm could be co-resident with STT in
# production; the sweep itself runs with the service stopped.
SERVE_STT_RESIDENT_KIB=629145
# Distinct from the downloader's lock: this script shells out to that script for
# the download phase, and a shared path would deadlock against our own hold.
SERVE_LOCK_PATH="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/hw1-llm-serve.lock"
# Draft depth for the MTP arm. MEASURED 2026-08-13: depth 6 made decode WORSE,
# 6.85 -> 3.36 tok/s. On a GPU, verifying N drafted tokens is nearly free
# because the pass is memory-bound and compute sits idle; on this CPU the weight
# read is ~120 ms but seven tokens of batched compute is another ~94 ms, so a
# verification step costs ~1.5x a plain decode step and only pays off at high
# acceptance. Shallower drafts waste less per rejection, hence the knob.
SERVE_MTP_N_MAX=2

SERVE_RESULT_DIR=""
SERVE_LATEST_TMP=""
SERVE_TELEMETRY_PID=""
SERVE_RUN_PID=""
SERVE_SERVICE_WAS_ACTIVE=0
SERVE_SERVICE_STOPPED=0
SERVE_POWER_CHANGED=0
SERVE_PREVIOUS_PROFILE=""
SERVE_CLEANUP_RAN=0
SERVE_ARM_SUCCESSES=0
SERVE_BASELINE_OK=0
SERVE_MTP_SUPPORTED=0
SERVE_LAST_RC=0

# Extra llama-server flags applied to EVERY arm, baseline included, so an A/B
# stays fair. Repeatable: --server-arg --mlock --server-arg --no-mmap
declare -a SERVE_EXTRA_SERVER_ARGS=()

declare -a SERVE_IDS=()
declare -a SERVE_FILENAMES=()
declare -a SERVE_BYTES=()
declare -a SERVE_SHA256=()

usage() {
  cat <<'EOF'
Usage: benchmark_llm_serve.sh [options]

Downloads the pinned llm_serve_models.tsv ladder (delegating to
benchmark_llm_models.sh --download-only), stops hw1-ai-service, and measures
time-to-first-token and decode rate for each candidate by driving the real
llama-server through the service's own supervisor and client. Models whose
manifest id contains "-mtp-" are measured twice: once plain, once with
--spec-type draft-mtp. The active YAML model is measured first and last as the
baseline but is never changed.

Options:
  --download-only          Fetch and verify candidates; do not stop the service.
  --serve-only             Require already-downloaded candidates; run the sweep.
  --server-arg VALUE       Extra llama-server flag applied to every arm,
                           baseline included. Repeatable. Use for A/B tests of
                           a server option, e.g. --server-arg --mlock to force
                           all weights resident instead of paging in lazily.
  --strict-power           Abort on any power excursion, including a transient
                           dip that has already recovered. Default is to record
                           it, mark the run TAINTED in summary.md, and continue;
                           a live under-voltage or thermal condition always
                           aborts either way.
  --models-dir DIR         Candidate directory (default: ~/models/hw1-llm-bench).
  --results-dir DIR        Result root (default: ~/llm-serve-results).
  --manifest FILE          Pinned TSV manifest to use.
  --config FILE            Active service YAML.
  --python FILE            Python with hw1_ai_service installed.
  --server-bin FILE        llama-server executable (otherwise read from YAML).
  --active-model FILE      Baseline GGUF (otherwise read from YAML).
  --repeats N              Passes over the prompt set per arm (default: 2).
  --mtp-n-max N            Draft depth for the MTP arm (default: 2). Depth 6
                           measured 0.49x on this CPU; shallower drafts waste
                           less work per rejected token.
  -h, --help               Show this help.

Each arm runs a fixed six-prompt set with production settings: the config's
system prompt, max_tokens, and history_turns; streaming chat completions with
cache_prompt and enable_thinking=False; llama-server at -t 4 -c 2048
--cache-reuse 256 --parallel 1.
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --download-only)
      SERVE_MODE="download"
      shift
      ;;
    --serve-only)
      SERVE_MODE="serve"
      shift
      ;;
    --strict-power)
      SERVE_STRICT_POWER=1
      shift
      ;;
    --server-arg)
      (($# >= 2)) || die "--server-arg requires a value"
      SERVE_EXTRA_SERVER_ARGS+=("$2")
      shift 2
      ;;
    --models-dir|--results-dir|--manifest|--config|--python|--server-bin|--active-model|--repeats|--mtp-n-max)
      (($# >= 2)) || die "$1 requires a value"
      case "$1" in
        --mtp-n-max) SERVE_MTP_N_MAX="$2" ;;
        --models-dir) SERVE_MODELS_DIR="$2" ;;
        --results-dir) SERVE_RESULTS_ROOT="$2" ;;
        --manifest) SERVE_MANIFEST="$2" ;;
        --config) SERVE_CONFIG="$2" ;;
        --python) SERVE_PYTHON="$2" ;;
        --server-bin) SERVE_BIN_OVERRIDE="$2" ;;
        --active-model) SERVE_ACTIVE_MODEL_OVERRIDE="$2" ;;
        --repeats) SERVE_REPEATS="$2" ;;
      esac
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

[[ "$SERVE_REPEATS" =~ ^[1-9][0-9]?$ ]] || die "--repeats must be 1..99"
[[ "$SERVE_MTP_N_MAX" =~ ^[1-9][0-9]?$ ]] || die "--mtp-n-max must be 1..99"
[[ -r "$SERVE_MANIFEST" ]] || die "manifest is not readable: $SERVE_MANIFEST"
[[ -r "$SERVE_PROBE" ]] || die "probe is not readable: $SERVE_PROBE"

command -v flock >/dev/null 2>&1 || die "flock is required"
[[ -d "$(dirname -- "$SERVE_LOCK_PATH")" ]] ||
  die "runtime lock directory is unavailable: $(dirname -- "$SERVE_LOCK_PATH")"
[[ ! -L "$SERVE_LOCK_PATH" ]] || die "refusing a symlink at the lock path: $SERVE_LOCK_PATH"
exec 9>"$SERVE_LOCK_PATH"
flock -n 9 || die "another LLM serve sweep is active"

load_manifest() {
  local id repo revision filename bytes sha extra prior
  while IFS=$'\t' read -r id repo revision filename bytes sha extra || [[ -n "${id:-}" ]]; do
    [[ -z "${id:-}" || "$id" == \#* ]] && continue
    [[ -z "${extra:-}" ]] || die "too many columns in manifest row: $id"
    [[ "$id" =~ ^[a-z0-9._-]+$ ]] || die "unsafe manifest id: $id"
    [[ "$id" != active-baseline-* ]] || die "reserved manifest id: $id"
    [[ "$repo" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || die "unsafe repository: $repo"
    [[ "$revision" =~ ^[0-9a-f]{40}$ ]] || die "revision must be a 40-character commit: $id"
    [[ "$filename" =~ ^[A-Za-z0-9._-]+\.gguf$ ]] || die "unsafe GGUF filename: $filename"
    [[ "$bytes" =~ ^[1-9][0-9]*$ ]] || die "invalid byte count for $id"
    [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || die "invalid SHA-256 for $id"
    for prior in "${SERVE_IDS[@]}"; do
      [[ "$prior" != "$id" ]] || die "duplicate manifest id: $id"
    done
    SERVE_IDS+=("$id")
    SERVE_FILENAMES+=("$filename")
    SERVE_BYTES+=("$bytes")
    SERVE_SHA256+=("$sha")
  done < "$SERVE_MANIFEST"
  ((${#SERVE_IDS[@]} > 0)) || die "manifest has no model rows"
}

file_sha256() {
  sha256sum -- "$1" | awk '{print $1}'
}

verified_model_file() {
  local path="$1" expected_bytes="$2" expected_sha="$3" actual_bytes actual_sha
  [[ -f "$path" ]] || return 1
  actual_bytes="$(stat -c '%s' -- "$path")"
  [[ "$actual_bytes" == "$expected_bytes" ]] || return 1
  actual_sha="$(file_sha256 "$path")"
  [[ "$actual_sha" == "$expected_sha" ]]
}

load_active_paths() {
  local config_output value
  local -a config_values=()
  [[ -x "$SERVE_PYTHON" ]] || die "service Python is not executable: $SERVE_PYTHON"
  [[ -r "$SERVE_CONFIG" ]] || die "service config is not readable: $SERVE_CONFIG"
  config_output="$(
    cd -- "$SERVE_ACCOUNT_HOME"
    PYTHONPATH="$SERVE_ROOT" "$SERVE_PYTHON" - "$SERVE_CONFIG" <<'PY'
import os
import sys

from hw1_ai_service.config import load

cfg = load(sys.argv[1])
print(os.path.expanduser(cfg.llm.server_bin))
print(os.path.expanduser(cfg.llm.model))
print(cfg.llm.port)
PY
  )"
  while IFS= read -r value; do
    config_values+=("$value")
  done <<< "$config_output"
  ((${#config_values[@]} == 3)) || die "could not read server/model/port from config"
  SERVE_BIN="${SERVE_BIN_OVERRIDE:-${config_values[0]}}"
  SERVE_ACTIVE_MODEL="${SERVE_ACTIVE_MODEL_OVERRIDE:-${config_values[1]}}"
  SERVE_PORT="${config_values[2]}"
  [[ -n "$SERVE_BIN" ]] || die "llm.server_bin is empty; pass --server-bin"
  [[ "$SERVE_PORT" =~ ^[1-9][0-9]*$ ]] || die "invalid llm.port in config: $SERVE_PORT"
}

# llama-server is a cmake target, not an installed binary — building only the
# llama-bench target (as the throughput sweep needs) leaves this missing, and
# the failure then looks like a PATH problem rather than a build one.
require_server_binary() {
  [[ -e "$SERVE_BIN" ]] || die "llama-server is missing: $SERVE_BIN
Build it with:
  cmake --build $(dirname -- "$(dirname -- "$SERVE_BIN")") --target llama-server -j4"
  [[ -x "$SERVE_BIN" ]] || die "llama-server is not executable: $SERVE_BIN"
}

probe_mtp_support() {
  local help_text
  help_text="$("$SERVE_BIN" --help 2>&1 || true)"
  if grep -Fq -- '--spec-type' <<< "$help_text"; then
    SERVE_MTP_SUPPORTED=1
  else
    SERVE_MTP_SUPPORTED=0
  fi
}

current_swap_used_kib() {
  awk '
    /^SwapTotal:/ { total = $2 }
    /^SwapFree:/  { free = $2 }
    END { printf "%.0f\n", total - free }
  ' /proc/meminfo
}

current_mem_available_bytes() {
  awk '/^MemAvailable:/ { printf "%.0f\n", $2 * 1024; exit }' /proc/meminfo
}

current_temp_millic() {
  local path
  for path in /sys/class/thermal/thermal_zone*/temp; do
    [[ -r "$path" ]] || continue
    cat -- "$path"
    return 0
  done
  return 1
}

# vcgencmd get_throttled is two different reports in one word. Bits 0-3 are
# live conditions (under-voltage NOW, arm capped NOW, throttled NOW, soft temp
# limit NOW); bits 16-19 are "has occurred since boot" and stay set until a
# reboot. Treating the whole word as pass/fail conflates "the rail is sagging
# right now, every number after this is garbage" with "something dipped for two
# seconds an hour ago" — and the second one was killing whole sweeps.
SERVE_THROTTLE_NOW_MASK=$((0xF))
SERVE_THROTTLE_STICKY_MASK=$((0xF0000))

throttled_to_int() {
  local raw="$1" hex
  hex="${raw#throttled=}"
  [[ "$hex" =~ ^0x[0-9a-fA-F]+$ ]] || return 1
  printf '%d\n' "$((hex))"
}

describe_throttled() {
  local value="$1"
  local -a notes=()
  ((value & 0x1)) && notes+=("under-voltage NOW")
  ((value & 0x2)) && notes+=("arm capped NOW")
  ((value & 0x4)) && notes+=("throttled NOW")
  ((value & 0x8)) && notes+=("soft temp limit NOW")
  ((value & 0x10000)) && notes+=("under-voltage occurred")
  ((value & 0x20000)) && notes+=("arm capping occurred")
  ((value & 0x40000)) && notes+=("throttling occurred")
  ((value & 0x80000)) && notes+=("soft temp limit occurred")
  ((${#notes[@]} > 0)) || notes=("clean")
  local out="" note
  for note in "${notes[@]}"; do
    if [[ -z "$out" ]]; then out="$note"; else out="$out, $note"; fi
  done
  printf '%s' "$out"
}

# Printed around every arm so a power or thermal excursion is visible in the
# log at the moment it happens, instead of only as a verdict afterwards.
report_power_state() {
  local when="$1" temp_millic temp volts clock arm_mhz throttled
  temp_millic="$(current_temp_millic 2>/dev/null || printf 0)"
  temp="$((temp_millic / 1000)).$(((temp_millic % 1000) / 100))"
  volts="$(vcgencmd measure_volts core 2>/dev/null || printf 'volt=?')"
  clock="$(vcgencmd measure_clock arm 2>/dev/null || printf 'frequency(0)=0')"
  arm_mhz=$(( ${clock#frequency(0)=} / 1000000 ))
  throttled="$(vcgencmd get_throttled 2>/dev/null || printf 'throttled=?')"
  printf '  [power %-6s] temp=%s C  %s  arm=%s MHz  %s (%s)\n' \
    "$when" "$temp" "${volts#volt=}" "$arm_mhz" "$throttled" \
    "$(describe_throttled "$(throttled_to_int "$throttled" 2>/dev/null || printf 0)")"
}

governors_are_performance() {
  local governor found=0
  for governor in /sys/devices/system/cpu/cpufreq/policy*/scaling_governor; do
    [[ -r "$governor" ]] || continue
    found=1
    grep -Fxq performance "$governor" || return 1
  done
  ((found == 1))
}

uart_matches_mainpid() {
  local expected="$1" holders
  local -a pids=()
  holders="$(fuser /dev/ttyAMA2 2>/dev/null || true)"
  read -r -a pids <<< "$holders" || true
  [[ "$expected" =~ ^[1-9][0-9]*$ ]] \
    && ((${#pids[@]} == 1)) \
    && [[ "${pids[0]}" == "$expected" ]]
}

terminate_active_arm() {
  local session="${SERVE_RUN_PID:-}"
  [[ "$session" =~ ^[1-9][0-9]*$ ]] || return 0
  pkill -TERM -s "$session" >/dev/null 2>&1 || true
  for _ in $(seq 1 50); do
    pgrep -s "$session" >/dev/null 2>&1 || break
    sleep 0.1
  done
  if pgrep -s "$session" >/dev/null 2>&1; then
    pkill -KILL -s "$session" >/dev/null 2>&1 || true
  fi
  wait "$session" >/dev/null 2>&1 || true
  SERVE_RUN_PID=""
}

cleanup_serve() {
  local shell_rc=$? cleanup_rc=0 main_pid state invocation current_invocation
  local invocation_log ready restore_output restore_rc service_start_output service_start_rc
  ((SERVE_CLEANUP_RAN == 0)) || return
  SERVE_CLEANUP_RAN=1
  trap - EXIT
  # Restoration matters more than reacting to a second Ctrl-C/HUP.
  trap '' INT TERM HUP
  set +e

  terminate_active_arm
  # The probe kills its own child, but a hard abort mid-arm can outlive it and
  # would then hold both the port and multiple GB of RAM against the service.
  pkill -f "[/]llama-server .*--port ${SERVE_PORT:-8080}" >/dev/null 2>&1

  if [[ -n "$SERVE_LATEST_TMP" ]]; then
    rm -f -- "$SERVE_LATEST_TMP"
    SERVE_LATEST_TMP=""
  fi

  if [[ -n "$SERVE_TELEMETRY_PID" ]]; then
    kill "$SERVE_TELEMETRY_PID" >/dev/null 2>&1
    wait "$SERVE_TELEMETRY_PID" >/dev/null 2>&1
    SERVE_TELEMETRY_PID=""
  fi

  if ((SERVE_POWER_CHANGED == 1)); then
    restore_output="$(sudo -n "$SERVE_POWER_HELPER" \
      profile "$SERVE_PREVIOUS_PROFILE" 2>&1)"
    restore_rc=$?
    printf '%s\n' "$restore_output" >"$SERVE_RESULT_DIR/power-restore.log" 2>/dev/null ||
      printf 'power restore output: %s\n' "$restore_output" >&2
    ((restore_rc == 0)) || cleanup_rc=1
  fi

  if ((SERVE_SERVICE_WAS_ACTIVE == 1 && SERVE_SERVICE_STOPPED == 1)); then
    service_start_output="$(systemctl --user start "$SERVE_SERVICE" 2>&1)"
    service_start_rc=$?
    printf '%s\n' "$service_start_output" >"$SERVE_RESULT_DIR/service-start.log" 2>/dev/null ||
      printf 'service start output: %s\n' "$service_start_output" >&2
    ((service_start_rc == 0)) || cleanup_rc=1
    for _ in $(seq 1 30); do
      state="$(systemctl --user show "$SERVE_SERVICE" -p ActiveState --value 2>/dev/null)"
      main_pid="$(systemctl --user show "$SERVE_SERVICE" -p MainPID --value 2>/dev/null)"
      if [[ "$state" == active ]] && uart_matches_mainpid "${main_pid:-0}"; then
        break
      fi
      sleep 1
    done
    [[ "${state:-}" == active ]] || cleanup_rc=1
    uart_matches_mainpid "${main_pid:-0}" || cleanup_rc=1
    if ((cleanup_rc == 0)); then
      invocation="$(systemctl --user show "$SERVE_SERVICE" \
        -p InvocationID --value 2>/dev/null)"
      ready=0
      for _ in $(seq 1 120); do
        state="$(systemctl --user show "$SERVE_SERVICE" \
          -p ActiveState --value 2>/dev/null)"
        current_invocation="$(systemctl --user show "$SERVE_SERVICE" \
          -p InvocationID --value 2>/dev/null)"
        if [[ "$state" != active || "$current_invocation" != "$invocation" ]]; then
          break
        fi
        invocation_log="$(journalctl --quiet --user -u "$SERVE_SERVICE" \
          _SYSTEMD_INVOCATION_ID="$invocation" --no-pager 2>/dev/null)"
        if grep -Fq 'daemon ready' <<< "$invocation_log"; then
          ready=1
          break
        fi
        sleep 1
      done
      ((ready == 1)) || cleanup_rc=1
      if grep -iEq ' ERROR ' <<< "${invocation_log:-}"; then
        cleanup_rc=1
      fi
    fi
  fi

  if [[ -n "$SERVE_RESULT_DIR" ]]; then
    systemctl --user --no-pager -l status "$SERVE_SERVICE" \
      >"$SERVE_RESULT_DIR/service-status-after.log" 2>&1 || true
    fuser -v /dev/ttyAMA2 >"$SERVE_RESULT_DIR/uart-holder-after.log" 2>&1 || true
  fi

  if ((shell_rc == 0 && cleanup_rc != 0)); then
    shell_rc=$cleanup_rc
  fi
  if [[ -n "$SERVE_RESULT_DIR" ]]; then
    printf 'exit=%s cleanup=%s results=%s\n' "$shell_rc" "$cleanup_rc" "$SERVE_RESULT_DIR"
  fi
  exit "$shell_rc"
}

trap cleanup_serve EXIT

handle_serve_signal() {
  local exit_code="$1"
  trap - INT TERM HUP
  terminate_active_arm
  exit "$exit_code"
}

trap 'handle_serve_signal 130' INT
trap 'handle_serve_signal 143' TERM
trap 'handle_serve_signal 129' HUP

start_telemetry() {
  local output="$SERVE_RESULT_DIR/telemetry.tsv"
  printf 'timestamp\ttemp\ttemp_millic\tthrottled\tvolts\tarm_clock\n' > "$output"
  (
    local temp_millic
    while :; do
      temp_millic="$(current_temp_millic 2>/dev/null || printf unavailable)"
      printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$(date --iso-8601=seconds)" \
        "$(vcgencmd measure_temp 2>/dev/null || printf unavailable)" \
        "$temp_millic" \
        "$(vcgencmd get_throttled 2>/dev/null || printf unavailable)" \
        "$(vcgencmd measure_volts core 2>/dev/null || printf unavailable)" \
        "$(vcgencmd measure_clock arm 2>/dev/null || printf unavailable)" \
        >> "$output"
      if [[ "$temp_millic" =~ ^[0-9]+$ ]] && ((temp_millic > SERVE_MAX_TEMP_MILLIC)); then
        printf '%s\n' "$temp_millic" > "$SERVE_RESULT_DIR/over-temperature-millic.txt"
      fi
      sleep 2
    done
  ) &
  SERVE_TELEMETRY_PID=$!
}

record_host_metadata() {
  {
    printf 'captured=%s\n' "$(date --iso-8601=seconds)"
    printf 'root=%s\nconfig=%s\nactive_model=%s\nllama_server=%s\nmanifest=%s\n' \
      "$SERVE_ROOT" "$SERVE_CONFIG" "$SERVE_ACTIVE_MODEL" "$SERVE_BIN" "$SERVE_MANIFEST"
    printf 'mtp_supported=%s\nmtp_n_max=%s\nrepeats=%s\nextra_server_args=%s\n' "$SERVE_MTP_SUPPORTED" "$SERVE_MTP_N_MAX" "$SERVE_REPEATS" "${SERVE_EXTRA_SERVER_ARGS[*]:-none}"
    uname -a
    printf '\n--- lscpu ---\n'
    lscpu
    printf '\n--- memory ---\n'
    free -h
    printf '\n--- swap ---\n'
    swapon --show || true
    printf '\n--- governors ---\n'
    for governor in /sys/devices/system/cpu/cpufreq/policy*/scaling_governor; do
      [[ -r "$governor" ]] && printf '%s=%s\n' "$governor" "$(cat -- "$governor")"
    done
    printf '\n--- llama-server help (spec-decoding flags) ---\n'
    "$SERVE_BIN" --help 2>&1 | grep -iE 'spec-|draft' || printf '(none reported)\n'
    printf '\n--- hashes ---\n'
    sha256sum -- "$SERVE_BIN" "$SERVE_ACTIVE_MODEL" "$SERVE_MANIFEST" "$SERVE_PROBE"
  } > "$SERVE_RESULT_DIR/host-metadata.txt" 2>&1
}

check_model_memory() {
  local model="$1" bytes available needed
  bytes="$(stat -c '%s' -- "$model")"
  available="$(current_mem_available_bytes)"
  # Same conservative shape as the service preflight: 1.2x file size plus a
  # 500 MiB runtime-and-host allowance.
  needed=$(((bytes * 12) / 10 + 500 * 1024 * 1024))
  ((available > needed)) ||
    die "not enough available RAM for $model: need >$needed bytes, have $available"
}

# Record that one arm's numbers are not trustworthy, without ending the sweep.
# Every condition here is per-arm: it says "this measurement is suspect", not
# "the machine is broken". Aborting the whole run on one of them throws away the
# arms that already succeeded AND the ones still queued, which is how a single
# swap event cost an entire hour.
taint_arm() {
  local label="$1" reason="$2"
  printf '%s\t%s\n' "$label" "$reason" >> "$SERVE_RESULT_DIR/arm-taints.tsv"
  printf '  [health WARN ] %s: %s — arm marked TAINTED\n' "$label" "$reason" >&2
}

verify_arm_health() {
  local label="$1" throttled temp swap_used arm_clock arm_hz value new_bits over
  throttled="$(vcgencmd get_throttled)"
  printf '%s\n' "$throttled" > "$SERVE_RESULT_DIR/$label.throttled-after.txt"
  value="$(throttled_to_int "$throttled")" ||
    die "could not parse get_throttled output during $label: $throttled"
  # The ONE condition that still aborts: the rail or the die is unhealthy right
  # now, so every subsequent arm would be garbage too.
  if ((value & SERVE_THROTTLE_NOW_MASK)); then
    die "live power/thermal condition during $label: $throttled ($(describe_throttled "$value")); this ranking is invalid"
  fi

  new_bits=$((value & SERVE_THROTTLE_STICKY_MASK & ~SERVE_THROTTLED_BEFORE))
  if ((new_bits != 0)); then
    if ((SERVE_STRICT_POWER == 1)); then
      die "power excursion during $label: $throttled ($(describe_throttled "$value")); --strict-power is set"
    fi
    printf '%s\t%s\t%s\n' "$label" "$throttled" "$(describe_throttled "$new_bits")" \
      >> "$SERVE_RESULT_DIR/power-excursions.tsv"
    taint_arm "$label" "power excursion: $throttled ($(describe_throttled "$new_bits"))"
    # Absorb the new bits into the baseline so one dip does not taint every
    # later arm as well.
    SERVE_THROTTLED_BEFORE=$((SERVE_THROTTLED_BEFORE | new_bits))
  fi

  temp="$(current_temp_millic)"
  [[ "$temp" =~ ^[0-9]+$ ]] || die "could not read CM5 temperature"
  printf '%s\n' "$temp" > "$SERVE_RESULT_DIR/$label.temp-millic-after.txt"
  if [[ -e "$SERVE_RESULT_DIR/over-temperature-millic.txt" ]]; then
    over="$(cat "$SERVE_RESULT_DIR/over-temperature-millic.txt")"
    taint_arm "$label" "temperature exceeded $((SERVE_MAX_TEMP_MILLIC / 1000)) C (peak ${over} millic)"
    # Consume the marker; the telemetry sampler re-creates it if it happens again.
    rm -f -- "$SERVE_RESULT_DIR/over-temperature-millic.txt"
  fi

  swap_used="$(current_swap_used_kib)"
  printf '%s\n' "$swap_used" > "$SERVE_RESULT_DIR/$label.swap-used-kib-after.txt"
  if ((swap_used > SERVE_SWAP_USED_KIB_BEFORE)); then
    taint_arm "$label" \
      "swap grew $((swap_used - SERVE_SWAP_USED_KIB_BEFORE)) KiB (model did not fit RAM)"
    # Re-baseline: swapped-out pages stay out, so without this every later arm
    # inherits this arm's growth and reports a false positive.
    SERVE_SWAP_USED_KIB_BEFORE="$swap_used"
  fi

  arm_clock="$(vcgencmd measure_clock arm)"
  arm_hz="${arm_clock#frequency(0)=}"
  [[ "$arm_hz" =~ ^[0-9]+$ ]] || die "could not read the ARM clock after $label"
  ((arm_hz >= SERVE_MIN_ARM_HZ)) ||
    taint_arm "$label" \
      "ARM clock $((arm_hz / 1000000)) MHz is below the $((SERVE_MIN_ARM_HZ / 1000000)) MHz floor"
}

run_one_arm() {
  local label="$1" model="$2"
  shift 2
  local -a extra=("$@" ${SERVE_EXTRA_SERVER_ARGS[@]+"${SERVE_EXTRA_SERVER_ARGS[@]}"})
  local log json rc session
  local -a probe_cmd=()
  [[ -r "$model" && -f "$model" ]] || die "model is not a readable regular file: $model"
  check_model_memory "$model"
  log="$SERVE_RESULT_DIR/$label.log"
  json="$SERVE_RESULT_DIR/arms/$label.json"
  # The previous arm's mmap'd weights stay in page cache after its server exits.
  # They are reclaimable, but with vm.swappiness=60 the kernel will happily swap
  # anonymous pages to keep them — which makes a later arm look like it did not
  # fit RAM when really it was competing with a dead model's cached pages. Best
  # effort: no sudoers rule is required for the sweep, so a failure here is
  # silent and simply leaves the old behaviour.
  sync
  sudo -n sh -c 'echo 3 > /proc/sys/vm/drop_caches' >/dev/null 2>&1 || true

  printf '\n===== %s: %s %s =====\n' "$label" "$model" "${extra[*]:-(no spec flags)}"
  report_power_state before

  probe_cmd=("$SERVE_PYTHON" "$SERVE_PROBE"
    --config "$SERVE_CONFIG"
    --model "$model"
    --server-bin "$SERVE_BIN"
    --label "$label"
    --out "$json"
    --port "$SERVE_PORT"
    --repeats "$SERVE_REPEATS")
  # --extra-arg=VALUE, not --extra-arg VALUE: every value here starts with a
  # dash (--spec-type, --spec-draft-n-max), and argparse refuses to consume a
  # dash-leading token as an option's value in the separated form. The equals
  # form is taken literally, which is what killed the MTP arm.
  local arg
  for arg in "${extra[@]:-}"; do
    [[ -n "$arg" ]] && probe_cmd+=("--extra-arg=$arg")
  done

  # setsid is backgrounded directly, exactly as the companion script does it, so
  # $! is the new session id and terminate_active_arm can sweep the whole group
  # (probe plus the llama-server it spawned). Wrapping this in a subshell would
  # make $! the subshell instead and leak the server on abort.
  set +e
  setsid timeout --signal=TERM --kill-after=20s "$SERVE_ARM_TIMEOUT" \
    env PYTHONPATH="$SERVE_ROOT" "${probe_cmd[@]}" >"$log" 2>&1 &
  SERVE_RUN_PID=$!
  session="$SERVE_RUN_PID"
  wait "$session"
  rc=$?
  if pgrep -s "$session" >/dev/null 2>&1; then
    terminate_active_arm
    ((rc != 0)) || rc=125
  else
    SERVE_RUN_PID=""
  fi
  set -e
  tail -n 20 -- "$log"
  report_power_state after
  printf '%s\t%s\t%s\t%s\t%s\n' \
    "$label" "$model" "${extra[*]:-}" "$rc" "$log" >> "$SERVE_RESULT_DIR/arms.tsv"
  verify_arm_health "$label"
  SERVE_LAST_RC="$rc"
}

write_summary() {
  local summary_rc
  set +e
  "$SERVE_PYTHON" - "$SERVE_RESULT_DIR" "$SERVE_STT_RESIDENT_KIB" <<'PY'
import json
import pathlib
import sys

result_dir = pathlib.Path(sys.argv[1])
stt_kib = int(sys.argv[2])
arms_dir = result_dir / "arms"

order = []
for line in (result_dir / "arms.tsv").read_text().splitlines()[1:]:
    label = line.split("\t", 1)[0]
    if label:
        order.append(label)

arms = []
for label in order:
    path = arms_dir / f"{label}.json"
    if not path.exists():
        arms.append({"label": label, "status": "failed", "error": "no result file"})
        continue
    try:
        arms.append(json.loads(path.read_text()))
    except json.JSONDecodeError as exc:
        arms.append({"label": label, "status": "failed", "error": f"unparsable: {exc}"})

baseline = next(
    (a for a in arms if a["label"] == "active-baseline-start" and a.get("status") == "ok"),
    None,
)


def pct(value, base):
    if value is None or not base:
        return None
    return 100.0 * value / base


def num(value, suffix="", digits=2):
    return "—" if value is None else f"{value:.{digits}f}{suffix}"


taints = {}
taint_path = result_dir / "arm-taints.tsv"
if taint_path.exists():
    for raw in taint_path.read_text().splitlines():
        parts = raw.split("\t")
        if len(parts) >= 2:
            taints.setdefault(parts[0], []).append(parts[1])

lines = [
    "# CM5 llama-server latency sweep",
    "",
]
if taints:
    lines.extend([
        "> **TAINTED — some arms are not directly comparable.**",
        "> A health condition was recorded during the arms listed below. Marked ⚠ in",
        "> the table. Two fingerprints worth knowing: under-voltage throttling drops the",
        "> CPU clock but not the memory clock, so prefill/TTFT is hit hard (~40%) while",
        "> decode barely moves (~4%); swap growth means the model did not fit RAM on this",
        "> host, which is itself a result — that model is not deployable here regardless",
        "> of how fast it looked.",
        ">",
    ])
    for label, reasons in taints.items():
        for reason in reasons:
            lines.append(f"> - `{label}` — {reason}")
    lines.append("")
lines.extend([
    "Production path, not a synthetic one: the service's own LlamaServerSupervisor",
    "(`-t 4 -c 2048 --cache-reuse 256 --parallel 1`) and LlmClient (streaming",
    "`/v1/chat/completions`, `cache_prompt`, `enable_thinking=False`), with the",
    "config's system prompt, `max_tokens`, and `history_turns`.",
    "",
    "All medians are STEADY STATE: the first generation after server start is a",
    "one-off cost (paid at daemon boot, not per turn) and is reported separately in",
    "the cold column rather than averaged in. TTFT is then split because turn 1 pays",
    "for a cold history prefix and later turns run",
    "behind `--cache-reuse`. Decode is a median rate over all turns; sampling is",
    "production's, so answers vary between runs and only the rate is comparable.",
    "",
    "| arm | status | cold 1st gen s | TTFT turn1 s | TTFT later s | decode tok/s | vs baseline | median turn s | peak RSS MiB | +STT fits 8GB |",
    "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: |",
])

for arm in arms:
    shown = arm["label"] + (" ⚠" if arm["label"] in taints else "")
    if arm.get("status") != "ok":
        lines.append(
            f"| {shown} | failed | — | — | — | — | — | — | — | — |"
        )
        continue
    rss = arm.get("peak_rss_kib")
    total = arm.get("mem_total_kib")
    if rss and total:
        fits = "yes" if (rss + stt_kib) < total * 0.90 else "NO"
    else:
        fits = "—"
    lines.append(
        f"| {shown} | ok | "
        f"{num(arm.get('cold_first_generation_s'))} | "
        f"{num(arm.get('ttft_first_turn_median_s'))} | "
        f"{num(arm.get('ttft_later_turn_median_s'))} | "
        f"{num(arm.get('decode_median_per_s'))} | "
        f"{num(pct(arm.get('decode_median_per_s'), baseline and baseline.get('decode_median_per_s')), '%')} | "
        f"{num(arm.get('total_median_s'))} | "
        f"{num(None if rss is None else rss / 1024, digits=0)} | {fits} |"
    )

# The point of the whole exercise: MTP is invisible to llama-bench, so report the
# plain-vs-MTP delta for every model that has both arms.
by_label = {a["label"]: a for a in arms if a.get("status") == "ok"}
deltas = []
for label, arm in by_label.items():
    if not label.endswith(":mtp"):
        continue
    plain = by_label.get(label[: -len(":mtp")] + ":plain")
    if not plain:
        continue
    base_rate = plain.get("decode_median_per_s")
    mtp_rate = arm.get("decode_median_per_s")
    if base_rate and mtp_rate:
        deltas.append(f"- `{label[:-4]}`: MTP decode {mtp_rate / base_rate:.2f}x "
                      f"({base_rate:.2f} -> {mtp_rate:.2f} tok/s)")
if deltas:
    lines.extend(["", "## Speculative-decoding effect", ""] + deltas)

start = baseline
end = next(
    (a for a in arms if a["label"] == "active-baseline-end" and a.get("status") == "ok"),
    None,
)
drift_ok = True
if start and end and start.get("decode_median_per_s") and end.get("decode_median_per_s"):
    drift = 100.0 * (end["decode_median_per_s"] - start["decode_median_per_s"]) / start["decode_median_per_s"]
    lines.extend(["", f"Baseline repeat drift: decode {drift:+.2f}%."])
    if abs(drift) > 10:
        drift_ok = False
        lines.append("WARNING: baseline drift exceeded 10%; rerun before ranking close results.")

empty = [a["label"] for a in arms if a.get("status") == "ok" and a.get("empty_answers")]
if empty:
    lines.extend([
        "",
        "## Empty answers",
        "",
        "These arms returned at least one turn with no answer text at all — the"
        " signature of a reasoning model spending the whole `max_tokens` budget"
        " before emitting anything displayable: " + ", ".join(f"`{x}`" for x in empty),
    ])

lines.extend([
    "",
    "Answers for every arm are in `answers.md`. Speed is only half the question;"
    " read them before switching.",
])

(result_dir / "summary.md").write_text("\n".join(lines) + "\n")

answer_lines = ["# Answers by arm", ""]
for arm in arms:
    answer_lines.append(f"## {arm['label']}")
    answer_lines.append("")
    if arm.get("status") != "ok":
        answer_lines.extend([f"failed: {arm.get('error', 'see log')}", ""])
        continue
    seen = set()
    for turn in arm.get("turns", []):
        if turn["prompt"] in seen:
            continue
        seen.add(turn["prompt"])
        answer_lines.append(f"**{turn['prompt']}**")
        answer_lines.append("")
        answer_lines.append((turn["answer"] or "_(empty)_").strip())
        answer_lines.append("")
(result_dir / "answers.md").write_text("\n".join(answer_lines) + "\n")

challengers = [a for a in arms if not a["label"].startswith("active-baseline-")]
valid = bool(start and end and challengers
             and any(a.get("status") == "ok" for a in challengers) and drift_ok)
raise SystemExit(0 if valid else 1)
PY
  summary_rc=$?
  set -e
  cat -- "$SERVE_RESULT_DIR/summary.md"
  ((summary_rc == 0)) ||
    die "sweep metrics are incomplete or baseline drift exceeded 10%; ranking is invalid"
}

load_manifest
mkdir -p -- "$SERVE_MODELS_DIR"

if [[ "$SERVE_MODE" != download ]]; then
  load_active_paths
  require_server_binary
  probe_mtp_support
  [[ -r "$SERVE_ACTIVE_MODEL" && -f "$SERVE_ACTIVE_MODEL" ]] ||
    die "active baseline is not a readable regular file: $SERVE_ACTIVE_MODEL"
  command -v timeout >/dev/null 2>&1 || die "GNU timeout is required"
  command -v setsid >/dev/null 2>&1 || die "setsid is required"
  command -v pgrep >/dev/null 2>&1 || die "pgrep is required"
  command -v pkill >/dev/null 2>&1 || die "pkill is required"
  command -v vcgencmd >/dev/null 2>&1 || die "vcgencmd is required on the CM5"
  command -v fuser >/dev/null 2>&1 || die "fuser is required"
fi

# Downloads reuse the companion script's audited pinned-fetch path verbatim
# rather than reimplementing resume, size caps, and checksum verification here.
if [[ "$SERVE_MODE" != serve ]]; then
  [[ -x "$SERVE_DOWNLOADER" ]] || die "downloader is not executable: $SERVE_DOWNLOADER"
  "$SERVE_DOWNLOADER" --download-only \
    --manifest "$SERVE_MANIFEST" \
    --models-dir "$SERVE_MODELS_DIR"
fi

if [[ "$SERVE_MODE" == download ]]; then
  printf 'Candidates are downloaded and verified in %s. The service was not interrupted.\n' \
    "$SERVE_MODELS_DIR"
  exit 0
fi

# Re-verifying every candidate means hashing several GB off disk before the
# first arm starts. Announce it: a silent multi-minute pause at startup reads as
# a hung script, which is exactly how it was first reported.
serve_total_bytes=0
for ((i = 0; i < ${#SERVE_IDS[@]}; ++i)); do
  serve_total_bytes=$((serve_total_bytes + SERVE_BYTES[i]))
done
printf 'Verifying %d candidates (%s MiB of SHA-256); this takes a few minutes.\n' \
  "${#SERVE_IDS[@]}" "$((serve_total_bytes / 1048576))"
for ((i = 0; i < ${#SERVE_IDS[@]}; ++i)); do
  candidate="$SERVE_MODELS_DIR/${SERVE_FILENAMES[i]}"
  printf '  [%d/%d] %s (%s MiB)... ' \
    "$((i + 1))" "${#SERVE_IDS[@]}" "${SERVE_IDS[i]}" "$((SERVE_BYTES[i] / 1048576))"
  verified_model_file "$candidate" "${SERVE_BYTES[i]}" "${SERVE_SHA256[i]}" || {
    printf 'FAILED\n'
    die "candidate is missing or invalid; run without --serve-only first: $candidate"
  }
  printf 'ok\n'
done

if ((SERVE_MTP_SUPPORTED == 0)); then
  for id in "${SERVE_IDS[@]}"; do
    [[ "$id" == *-mtp-* ]] || continue
    printf 'WARNING: %s builds llama-server without --spec-type; MTP arms are skipped.\n' \
      "$SERVE_BIN" >&2
    printf 'Rebuild with: cmake --build %s --target llama-server -j4\n' \
      "$(dirname -- "$(dirname -- "$SERVE_BIN")")" >&2
    break
  done
fi

mkdir -p -- "$SERVE_RESULTS_ROOT"
SERVE_RESULT_DIR="$(mktemp -d "$SERVE_RESULTS_ROOT/run-$(date +%Y%m%d-%H%M%S)-XXXXXXXX")"
mkdir -p -- "$SERVE_RESULT_DIR/arms"
SERVE_LATEST_TMP="$(mktemp "$SERVE_RESULTS_ROOT/.latest-XXXXXXXX")"
printf '%s\n' "$SERVE_RESULT_DIR" > "$SERVE_LATEST_TMP"
mv -T -- "$SERVE_LATEST_TMP" "$SERVE_RESULTS_ROOT/latest.txt"
SERVE_LATEST_TMP=""
printf 'label\tmodel\textra_args\trc\tlog\n' > "$SERVE_RESULT_DIR/arms.tsv"

vcgencmd get_throttled | tee "$SERVE_RESULT_DIR/throttled-preflight.txt"
SERVE_THROTTLED_BEFORE="$(throttled_to_int \
  "$(cat "$SERVE_RESULT_DIR/throttled-preflight.txt")")" ||
  die "could not parse the preflight get_throttled output"
# Live conditions still block the run outright. Pre-existing sticky bits from an
# earlier boot are recorded as the baseline instead of demanding a reboot, so
# only bits that appear DURING this sweep count as an excursion.
if ((SERVE_THROTTLED_BEFORE & SERVE_THROTTLE_NOW_MASK)); then
  die "live power/thermal condition before the sweep: $(describe_throttled "$SERVE_THROTTLED_BEFORE"); fix power and retry"
fi
if ((SERVE_THROTTLED_BEFORE != 0)); then
  printf 'NOTE: sticky flags already set at preflight (%s). Baselining against them;\n' \
    "$(describe_throttled "$SERVE_THROTTLED_BEFORE")"
  printf '      reboot first if you want a clean slate.\n'
fi

if systemctl --user is-active --quiet "$SERVE_SERVICE"; then
  SERVE_SERVICE_WAS_ACTIVE=1
  systemctl --user --no-pager -l status "$SERVE_SERVICE" \
    > "$SERVE_RESULT_DIR/service-status-before.log" 2>&1 || true
  SERVE_SERVICE_STOPPED=1
  systemctl --user stop "$SERVE_SERVICE"
  for _ in $(seq 1 20); do
    state="$(systemctl --user show "$SERVE_SERVICE" -p ActiveState --value)"
    [[ "$state" == inactive ]] && break
    sleep 1
  done
  [[ "$state" == inactive ]] || die "service did not stop cleanly"
fi

if pgrep -af '[/]hw1ai/bin/hw1-ai-service|[/]llama-server([[:space:]]|$)' \
    > "$SERVE_RESULT_DIR/competing-processes.log"; then
  die "a competing AI service or llama-server is still running"
fi
if pgrep -af '[/]llama-bench([[:space:]]|$)|[/]llama-cli([[:space:]]|$)' \
    >> "$SERVE_RESULT_DIR/competing-processes.log"; then
  die "another llama benchmark or CLI process is running"
fi

if [[ -x "$SERVE_POWER_HELPER" ]] && \
    sudo -n "$SERVE_POWER_HELPER" status > "$SERVE_RESULT_DIR/power-before.json" 2>&1; then
  SERVE_PREVIOUS_PROFILE="$("$SERVE_PYTHON" - "$SERVE_RESULT_DIR/power-before.json" <<'PY'
import json
import pathlib
import sys

profile = json.loads(pathlib.Path(sys.argv[1]).read_text())["profile"]
if profile not in {"eco", "balanced", "performance"}:
    raise SystemExit(f"unsafe prior profile: {profile!r}")
print(profile)
PY
  )"
  if [[ "$SERVE_PREVIOUS_PROFILE" != performance ]]; then
    SERVE_POWER_CHANGED=1
    sudo -n "$SERVE_POWER_HELPER" profile performance \
      > "$SERVE_RESULT_DIR/power-performance.json"
  fi
elif ! governors_are_performance; then
  die "performance governor unavailable; install systemd/install-power-helper.sh and rerun"
fi

governors_are_performance || die "not every CPU policy entered the performance governor"

SERVE_SWAP_USED_KIB_BEFORE="$(current_swap_used_kib)"
printf '%s\n' "$SERVE_SWAP_USED_KIB_BEFORE" > "$SERVE_RESULT_DIR/swap-used-kib-before.txt"
record_host_metadata
cp -- "$0" "$SERVE_RESULT_DIR/benchmark_llm_serve.sh"
cp -- "$SERVE_PROBE" "$SERVE_RESULT_DIR/llm_serve_probe.py"
cp -- "$SERVE_MANIFEST" "$SERVE_RESULT_DIR/llm_serve_models.tsv"
sha256sum -- "$SERVE_RESULT_DIR/benchmark_llm_serve.sh" \
  "$SERVE_RESULT_DIR/llm_serve_probe.py" \
  "$SERVE_RESULT_DIR/llm_serve_models.tsv" > "$SERVE_RESULT_DIR/evidence-sha256.txt"
start_telemetry

vcgencmd measure_clock arm | tee "$SERVE_RESULT_DIR/arm-clock-before.txt"
SERVE_ARM_HZ_BEFORE="$(sed -n 's/^frequency(0)=//p' "$SERVE_RESULT_DIR/arm-clock-before.txt")"
[[ "$SERVE_ARM_HZ_BEFORE" =~ ^[0-9]+$ ]] || die "could not read the pre-run ARM clock"
((SERVE_ARM_HZ_BEFORE >= SERVE_MIN_ARM_HZ)) ||
  die "ARM clock is below $((SERVE_MIN_ARM_HZ / 1000000)) MHz before the sweep ($SERVE_ARM_HZ_BEFORE Hz)"

# Re-base the per-arm floor on what this machine actually tops out at, instead
# of the stock-2.4 GHz constant. Overclocked to 3.0 GHz, a throttle back to 2.4
# would sail through a fixed 2.3 GHz check while every number after it silently
# came from a slower chip.
SERVE_ARM_MAX_HZ=$(( $(cat /sys/devices/system/cpu/cpufreq/policy0/cpuinfo_max_freq \
  2>/dev/null || printf 0) * 1000 ))
if ((SERVE_ARM_MAX_HZ > SERVE_MIN_ARM_HZ)); then
  SERVE_MIN_ARM_HZ=$((SERVE_ARM_MAX_HZ * 95 / 100))
  printf 'Per-arm ARM clock floor: %s MHz (95%% of this host'\''s %s MHz maximum).\n' \
    "$((SERVE_MIN_ARM_HZ / 1000000))" "$((SERVE_ARM_MAX_HZ / 1000000))"
fi

run_one_arm active-baseline-start "$SERVE_ACTIVE_MODEL"
((SERVE_LAST_RC == 0)) || die "active baseline failed; see active-baseline-start.log"
SERVE_BASELINE_OK=1

for ((i = 0; i < ${#SERVE_IDS[@]}; ++i)); do
  id="${SERVE_IDS[i]}"
  candidate="$SERVE_MODELS_DIR/${SERVE_FILENAMES[i]}"

  run_one_arm "$id:plain" "$candidate"
  if ((SERVE_LAST_RC == 0)); then
    SERVE_ARM_SUCCESSES=$((SERVE_ARM_SUCCESSES + 1))
  else
    printf 'Arm %s:plain failed; continuing to the next arm.\n' "$id" >&2
  fi

  if [[ "$id" == *-mtp-* ]] && ((SERVE_MTP_SUPPORTED == 1)); then
    run_one_arm "$id:mtp" "$candidate" \
      --spec-type draft-mtp --spec-draft-n-max "$SERVE_MTP_N_MAX"
    if ((SERVE_LAST_RC == 0)); then
      SERVE_ARM_SUCCESSES=$((SERVE_ARM_SUCCESSES + 1))
    else
      printf 'Arm %s:mtp failed; continuing to the next arm.\n' "$id" >&2
    fi
  fi
done

run_one_arm active-baseline-end "$SERVE_ACTIVE_MODEL"
((SERVE_LAST_RC == 0)) || printf 'WARNING: ending baseline repeat failed.\n' >&2

write_summary
((SERVE_BASELINE_OK == 1)) || die "baseline was not measured"
((SERVE_ARM_SUCCESSES > 0)) || die "no candidate arm completed successfully"
printf 'Sweep complete. Raw evidence and summary: %s\n' "$SERVE_RESULT_DIR"
