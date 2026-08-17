#!/usr/bin/env bash
# Download a pinned up-to-3.4B GGUF ladder and benchmark the active model.
#
# This is intentionally a throughput gate, not a model switch or quality test:
# it never edits config.yaml, never moves the active model, and restores the
# user service and concrete power profile on every exit path.

set -Eeuo pipefail
umask 077
export LC_ALL=C

BENCH_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
BENCH_ROOT="$(cd -- "$BENCH_SCRIPT_DIR/.." && pwd -P)"
BENCH_ACCOUNT_HOME="${HOME:?HOME must name the CM5 service account home}"
BENCH_CONFIG="$BENCH_ACCOUNT_HOME/.config/hw1-ai-service/config.yaml"
BENCH_PYTHON="$BENCH_ACCOUNT_HOME/hw1ai/bin/python"
BENCH_MANIFEST="$BENCH_SCRIPT_DIR/llm_benchmark_models.tsv"
BENCH_MODELS_DIR="$BENCH_ACCOUNT_HOME/models/hw1-llm-bench"
BENCH_RESULTS_ROOT="$BENCH_ACCOUNT_HOME/llm-bench-results"
BENCH_BIN_OVERRIDE="${LLAMA_BENCH:-}"
BENCH_ACTIVE_MODEL_OVERRIDE=""
BENCH_SERVICE="hw1-ai-service.service"
BENCH_POWER_HELPER="/usr/local/libexec/hw1-power-helper"
BENCH_MODE="all"
BENCH_DOWNLOAD_RESERVE_BYTES=$((512 * 1024 * 1024))
BENCH_MAX_TEMP_MILLIC=80000
BENCH_MIN_ARM_HZ=2300000000
BENCH_LOCK_PATH="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/hw1-llm-benchmark.lock"

BENCH_RESULT_DIR=""
BENCH_LATEST_TMP=""
BENCH_TELEMETRY_PID=""
BENCH_RUN_PID=""
BENCH_SERVICE_WAS_ACTIVE=0
BENCH_SERVICE_STOPPED=0
BENCH_POWER_CHANGED=0
BENCH_PREVIOUS_PROFILE=""
BENCH_CLEANUP_RAN=0
BENCH_CHALLENGER_SUCCESSES=0
BENCH_BASELINE_OK=0

declare -a BENCH_IDS=()
declare -a BENCH_REPOS=()
declare -a BENCH_REVISIONS=()
declare -a BENCH_FILENAMES=()
declare -a BENCH_BYTES=()
declare -a BENCH_SHA256=()

usage() {
  cat <<'EOF'
Usage: benchmark_llm_models.sh [options]

Downloads a pinned Q4_0 ladder, stops hw1-ai-service, runs the same
llama-bench test used by the historical Pi 5 record, and restores the service.
The active YAML model is measured first and last but is never changed.

Options:
  --download-only          Download and verify candidates; do not stop service.
  --benchmark-only         Require already-downloaded candidates; run tests.
  --models-dir DIR         Candidate directory (default: ~/models/hw1-llm-bench).
  --results-dir DIR        Result root (default: ~/llm-bench-results).
  --manifest FILE          Pinned TSV manifest to use.
  --config FILE            Active service YAML.
  --python FILE            Python with hw1_ai_service installed.
  --bench-bin FILE         llama-bench executable (otherwise derived from YAML).
  --active-model FILE      Baseline GGUF (otherwise read from YAML).
  -h, --help               Show this help.

The benchmark is fixed at: llama-bench -p 128 -n 64 -t 4
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --download-only)
      BENCH_MODE="download"
      shift
      ;;
    --benchmark-only)
      BENCH_MODE="benchmark"
      shift
      ;;
    --models-dir|--results-dir|--manifest|--config|--python|--bench-bin|--active-model)
      (($# >= 2)) || die "$1 requires a value"
      case "$1" in
        --models-dir) BENCH_MODELS_DIR="$2" ;;
        --results-dir) BENCH_RESULTS_ROOT="$2" ;;
        --manifest) BENCH_MANIFEST="$2" ;;
        --config) BENCH_CONFIG="$2" ;;
        --python) BENCH_PYTHON="$2" ;;
        --bench-bin) BENCH_BIN_OVERRIDE="$2" ;;
        --active-model) BENCH_ACTIVE_MODEL_OVERRIDE="$2" ;;
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

[[ -r "$BENCH_MANIFEST" ]] || die "manifest is not readable: $BENCH_MANIFEST"

command -v flock >/dev/null 2>&1 || die "flock is required"
[[ -d "$(dirname -- "$BENCH_LOCK_PATH")" ]] ||
  die "runtime lock directory is unavailable: $(dirname -- "$BENCH_LOCK_PATH")"
[[ ! -L "$BENCH_LOCK_PATH" ]] || die "refusing a symlink at the lock path: $BENCH_LOCK_PATH"
exec 9>"$BENCH_LOCK_PATH"
flock -n 9 || die "another LLM download/benchmark run is active"

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
    ((${#bytes} <= 11)) || die "manifest byte count is too large for safe arithmetic: $id"
    ((bytes <= 10000000000)) || die "manifest model exceeds the 10 GB safety ceiling: $id"
    [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || die "invalid SHA-256 for $id"
    for prior in "${BENCH_IDS[@]}"; do
      [[ "$prior" != "$id" ]] || die "duplicate manifest id: $id"
    done
    for prior in "${BENCH_FILENAMES[@]}"; do
      [[ "$prior" != "$filename" ]] || die "duplicate manifest filename: $filename"
    done
    BENCH_IDS+=("$id")
    BENCH_REPOS+=("$repo")
    BENCH_REVISIONS+=("$revision")
    BENCH_FILENAMES+=("$filename")
    BENCH_BYTES+=("$bytes")
    BENCH_SHA256+=("$sha")
  done < "$BENCH_MANIFEST"
  ((${#BENCH_IDS[@]} > 0)) || die "manifest has no model rows"
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

preflight_download_space() {
  local needed=0 i final partial partial_bytes remaining available filesystem_size reserve
  for ((i = 0; i < ${#BENCH_IDS[@]}; ++i)); do
    final="$BENCH_MODELS_DIR/${BENCH_FILENAMES[i]}"
    partial="$final.part"
    [[ ! -L "$final" && ! -L "$partial" ]] ||
      die "refusing a symlink at a model download path: $final"
    if [[ -e "$final" ]]; then
      verified_model_file "$final" "${BENCH_BYTES[i]}" "${BENCH_SHA256[i]}" ||
        die "existing candidate failed size/checksum validation: $final"
      continue
    fi
    partial_bytes=0
    if [[ -e "$partial" ]]; then
      [[ -f "$partial" ]] || die "partial download is not a regular file: $partial"
      partial_bytes="$(stat -c '%s' -- "$partial")"
      ((partial_bytes <= BENCH_BYTES[i])) ||
        die "partial download is larger than its pinned size: $partial"
    fi
    remaining=$((BENCH_BYTES[i] - partial_bytes))
    needed=$((needed + remaining))
  done
  read -r filesystem_size available < <(
    df -kP "$BENCH_MODELS_DIR" |
      awk 'NR == 2 {printf "%.0f %.0f\n", $2 * 1024, $4 * 1024}'
  )
  [[ "$filesystem_size" =~ ^[0-9]+$ ]] || die "could not determine filesystem size"
  [[ "$available" =~ ^[0-9]+$ ]] || die "could not determine free disk space"
  reserve=$((filesystem_size / 10))
  ((reserve >= BENCH_DOWNLOAD_RESERVE_BYTES)) || reserve="$BENCH_DOWNLOAD_RESERVE_BYTES"
  ((available >= needed + reserve)) ||
    die "not enough disk: need $((needed + reserve)) bytes free including reserve, have $available"
  printf 'Download preflight: %s bytes remaining, %s bytes available.\n' "$needed" "$available"
}

download_one() {
  local index="$1" id repo revision filename expected_bytes expected_sha final partial url
  local actual_bytes actual_sha partial_bytes block_limit http_code curl_rc
  id="${BENCH_IDS[index]}"
  repo="${BENCH_REPOS[index]}"
  revision="${BENCH_REVISIONS[index]}"
  filename="${BENCH_FILENAMES[index]}"
  expected_bytes="${BENCH_BYTES[index]}"
  expected_sha="${BENCH_SHA256[index]}"
  final="$BENCH_MODELS_DIR/$filename"
  partial="$final.part"
  url="https://huggingface.co/$repo/resolve/$revision/$filename?download=true"

  [[ ! -L "$final" && ! -L "$partial" ]] ||
    die "refusing a symlink at a model download path: $final"
  if verified_model_file "$final" "$expected_bytes" "$expected_sha"; then
    printf 'Verified existing %s: %s\n' "$id" "$final"
    return 0
  fi
  [[ ! -e "$final" ]] || die "refusing to overwrite invalid existing file: $final"

  if [[ -f "$partial" ]] && [[ "$(stat -c '%s' -- "$partial")" == "$expected_bytes" ]]; then
    actual_sha="$(file_sha256 "$partial")"
    [[ "$actual_sha" == "$expected_sha" ]] ||
      die "$id complete partial has the wrong checksum: $actual_sha"
    mv -- "$partial" "$final"
    chmod 0644 "$final"
    printf 'Promoted complete verified partial for %s: %s\n' "$id" "$final"
    return 0
  fi

  command -v curl >/dev/null 2>&1 || die "curl is required to download models safely"
  partial_bytes=0
  [[ ! -e "$partial" ]] || partial_bytes="$(stat -c '%s' -- "$partial")"
  ((partial_bytes < expected_bytes)) ||
    die "$id partial is not smaller than its pinned size: $partial"
  # RLIMIT_FSIZE is a second, kernel-enforced guard against a response that
  # streams beyond the pinned artifact. Bash expresses it in 1024-byte blocks.
  block_limit=$(((expected_bytes + 1023) / 1024))

  printf 'Downloading %s (%s of %s bytes remaining)...\n' \
    "$id" "$((expected_bytes - partial_bytes))" "$expected_bytes"
  set +e
  http_code="$({
      ulimit -S -f "$block_limit"
      curl --fail --location --proto '=https' --proto-redir '=https' \
        --retry 5 --retry-delay 3 --continue-at - \
        --max-filesize "$expected_bytes" --output "$partial" \
        --write-out '%{http_code}' "$url"
  })"
  curl_rc=$?
  set -e
  ((curl_rc == 0)) ||
    die "$id download failed with curl rc=$curl_rc (verified prefix retained)"
  if ((partial_bytes > 0)); then
    [[ "$http_code" == 206 ]] ||
      die "$id server did not honor the safe resume range (HTTP $http_code)"
  else
    [[ "$http_code" == 200 || "$http_code" == 206 ]] ||
      die "$id download returned unexpected HTTP $http_code"
  fi

  actual_bytes="$(stat -c '%s' -- "$partial")"
  [[ "$actual_bytes" == "$expected_bytes" ]] ||
    die "$id download has $actual_bytes bytes; expected $expected_bytes (partial retained)"
  actual_sha="$(file_sha256 "$partial")"
  [[ "$actual_sha" == "$expected_sha" ]] ||
    die "$id checksum mismatch (partial retained): got $actual_sha"
  mv -- "$partial" "$final"
  chmod 0644 "$final"
  printf 'Downloaded and verified %s: %s\n' "$id" "$final"
}

load_active_paths() {
  local config_output server_bin value
  local -a config_values=()
  if [[ -n "$BENCH_ACTIVE_MODEL_OVERRIDE" && -n "$BENCH_BIN_OVERRIDE" ]]; then
    BENCH_ACTIVE_MODEL="$BENCH_ACTIVE_MODEL_OVERRIDE"
    BENCH_BIN="$BENCH_BIN_OVERRIDE"
    return 0
  fi

  [[ -x "$BENCH_PYTHON" ]] || die "service Python is not executable: $BENCH_PYTHON"
  [[ -r "$BENCH_CONFIG" ]] || die "service config is not readable: $BENCH_CONFIG"
  config_output="$(
    cd -- "$BENCH_ACCOUNT_HOME"
    PYTHONPATH="$BENCH_ROOT" "$BENCH_PYTHON" - "$BENCH_CONFIG" <<'PY'
import os
import sys

from hw1_ai_service.config import load

cfg = load(sys.argv[1])
print(os.path.expanduser(cfg.llm.server_bin))
print(os.path.expanduser(cfg.llm.model))
PY
  )"
  while IFS= read -r value; do
    config_values+=("$value")
  done <<< "$config_output"
  ((${#config_values[@]} == 2)) || die "could not read server/model paths from config"
  server_bin="${config_values[0]}"
  BENCH_ACTIVE_MODEL="${BENCH_ACTIVE_MODEL_OVERRIDE:-${config_values[1]}}"
  if [[ -n "$BENCH_BIN_OVERRIDE" ]]; then
    BENCH_BIN="$BENCH_BIN_OVERRIDE"
  else
    [[ -n "$server_bin" ]] || die "llm.server_bin is empty; pass --bench-bin"
    BENCH_BIN="$(dirname -- "$server_bin")/llama-bench"
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

cleanup_benchmark() {
  local shell_rc=$? cleanup_rc=0 main_pid state invocation current_invocation
  local invocation_log ready restore_output restore_rc service_start_output service_start_rc
  ((BENCH_CLEANUP_RAN == 0)) || return
  BENCH_CLEANUP_RAN=1
  trap - EXIT
  # Restoration is more important than reacting to a second Ctrl-C/HUP.
  trap '' INT TERM HUP
  set +e

  terminate_active_benchmark

  if [[ -n "$BENCH_LATEST_TMP" ]]; then
    rm -f -- "$BENCH_LATEST_TMP"
    BENCH_LATEST_TMP=""
  fi

  if [[ -n "$BENCH_TELEMETRY_PID" ]]; then
    kill "$BENCH_TELEMETRY_PID" >/dev/null 2>&1
    wait "$BENCH_TELEMETRY_PID" >/dev/null 2>&1
    BENCH_TELEMETRY_PID=""
  fi

  if ((BENCH_POWER_CHANGED == 1)); then
    restore_output="$(sudo -n "$BENCH_POWER_HELPER" \
      profile "$BENCH_PREVIOUS_PROFILE" 2>&1)"
    restore_rc=$?
    printf '%s\n' "$restore_output" >"$BENCH_RESULT_DIR/power-restore.log" 2>/dev/null ||
      printf 'power restore output: %s\n' "$restore_output" >&2
    ((restore_rc == 0)) || cleanup_rc=1
  fi

  if ((BENCH_SERVICE_WAS_ACTIVE == 1 && BENCH_SERVICE_STOPPED == 1)); then
    service_start_output="$(systemctl --user start "$BENCH_SERVICE" 2>&1)"
    service_start_rc=$?
    printf '%s\n' "$service_start_output" >"$BENCH_RESULT_DIR/service-start.log" 2>/dev/null ||
      printf 'service start output: %s\n' "$service_start_output" >&2
    ((service_start_rc == 0)) || cleanup_rc=1
    for _ in $(seq 1 30); do
      state="$(systemctl --user show "$BENCH_SERVICE" -p ActiveState --value 2>/dev/null)"
      main_pid="$(systemctl --user show "$BENCH_SERVICE" -p MainPID --value 2>/dev/null)"
      if [[ "$state" == active ]] && uart_matches_mainpid "${main_pid:-0}"; then
        break
      fi
      sleep 1
    done
    [[ "${state:-}" == active ]] || cleanup_rc=1
    uart_matches_mainpid "${main_pid:-0}" || cleanup_rc=1
    if ((cleanup_rc == 0)); then
      invocation="$(systemctl --user show "$BENCH_SERVICE" \
        -p InvocationID --value 2>/dev/null)"
      ready=0
      for _ in $(seq 1 120); do
        state="$(systemctl --user show "$BENCH_SERVICE" \
          -p ActiveState --value 2>/dev/null)"
        current_invocation="$(systemctl --user show "$BENCH_SERVICE" \
          -p InvocationID --value 2>/dev/null)"
        if [[ "$state" != active || "$current_invocation" != "$invocation" ]]; then
          break
        fi
        invocation_log="$(journalctl --quiet --user -u "$BENCH_SERVICE" \
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

  if [[ -n "$BENCH_RESULT_DIR" ]]; then
    systemctl --user --no-pager -l status "$BENCH_SERVICE" \
      >"$BENCH_RESULT_DIR/service-status-after.log" 2>&1 || true
    fuser -v /dev/ttyAMA2 >"$BENCH_RESULT_DIR/uart-holder-after.log" 2>&1 || true
  fi

  if ((shell_rc == 0 && cleanup_rc != 0)); then
    shell_rc=$cleanup_rc
  fi
  if [[ -n "$BENCH_RESULT_DIR" ]]; then
    printf 'exit=%s cleanup=%s results=%s\n' "$shell_rc" "$cleanup_rc" "$BENCH_RESULT_DIR"
  fi
  exit "$shell_rc"
}

trap cleanup_benchmark EXIT

terminate_active_benchmark() {
  local session="${BENCH_RUN_PID:-}"
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
  BENCH_RUN_PID=""
}

handle_benchmark_signal() {
  local exit_code="$1"
  trap - INT TERM HUP
  terminate_active_benchmark
  exit "$exit_code"
}

trap 'handle_benchmark_signal 130' INT
trap 'handle_benchmark_signal 143' TERM
trap 'handle_benchmark_signal 129' HUP

start_telemetry() {
  local output="$BENCH_RESULT_DIR/telemetry.tsv"
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
      if [[ "$temp_millic" =~ ^[0-9]+$ ]] && ((temp_millic > BENCH_MAX_TEMP_MILLIC)); then
        printf '%s\n' "$temp_millic" > "$BENCH_RESULT_DIR/over-temperature-millic.txt"
      fi
      sleep 2
    done
  ) &
  BENCH_TELEMETRY_PID=$!
}

record_host_metadata() {
  {
    printf 'captured=%s\n' "$(date --iso-8601=seconds)"
    printf 'root=%s\nconfig=%s\nactive_model=%s\nllama_bench=%s\nmanifest=%s\n' \
      "$BENCH_ROOT" "$BENCH_CONFIG" "$BENCH_ACTIVE_MODEL" "$BENCH_BIN" "$BENCH_MANIFEST"
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
    printf '\n--- CPU frequency limits ---\n'
    for frequency in /sys/devices/system/cpu/cpufreq/policy*/{scaling_min_freq,scaling_max_freq,cpuinfo_max_freq}; do
      [[ -r "$frequency" ]] && printf '%s=%s\n' "$frequency" "$(cat -- "$frequency")"
    done
    printf '\n--- llama-bench version ---\n'
    "$BENCH_BIN" --version 2>&1 || true
    printf '\n--- llama-bench libraries ---\n'
    ldd "$BENCH_BIN" 2>&1 || true
    printf '\n--- hashes ---\n'
    sha256sum -- "$BENCH_BIN" "$BENCH_ACTIVE_MODEL" "$BENCH_MANIFEST"
  } > "$BENCH_RESULT_DIR/host-metadata.txt" 2>&1
}

check_model_memory() {
  local model="$1" bytes available needed
  bytes="$(stat -c '%s' -- "$model")"
  available="$(current_mem_available_bytes)"
  # Same conservative shape as the service preflight: 1.2x file size plus
  # 200 MiB runtime allowance and 300 MiB host headroom.
  needed=$(((bytes * 12) / 10 + 500 * 1024 * 1024))
  ((available > needed)) ||
    die "not enough available RAM for $model: need >$needed bytes, have $available"
}

verify_run_health() {
  local label="$1" throttled temp swap_used arm_clock arm_hz
  throttled="$(vcgencmd get_throttled)"
  printf '%s\n' "$throttled" > "$BENCH_RESULT_DIR/$label.throttled-after.txt"
  [[ "$throttled" == throttled=0x0 ]] ||
    die "power/throttle event during $label: $throttled; this ranking is invalid"
  temp="$(current_temp_millic)"
  [[ "$temp" =~ ^[0-9]+$ ]] || die "could not read CM5 temperature"
  printf '%s\n' "$temp" > "$BENCH_RESULT_DIR/$label.temp-millic-after.txt"
  [[ ! -e "$BENCH_RESULT_DIR/over-temperature-millic.txt" ]] ||
    die "temperature exceeded $((BENCH_MAX_TEMP_MILLIC / 1000)) C during the sweep"
  ((temp <= BENCH_MAX_TEMP_MILLIC)) ||
    die "temperature exceeded $((BENCH_MAX_TEMP_MILLIC / 1000)) C during $label"
  swap_used="$(current_swap_used_kib)"
  printf '%s\n' "$swap_used" > "$BENCH_RESULT_DIR/$label.swap-used-kib-after.txt"
  ((swap_used <= BENCH_SWAP_USED_KIB_BEFORE)) ||
    die "swap use grew during $label; this ranking is invalid"
  arm_clock="$(vcgencmd measure_clock arm)"
  printf '%s\n' "$arm_clock" > "$BENCH_RESULT_DIR/$label.arm-clock-after.txt"
  arm_hz="${arm_clock#frequency(0)=}"
  [[ "$arm_hz" =~ ^[0-9]+$ ]] || die "could not read the ARM clock after $label"
  ((arm_hz >= BENCH_MIN_ARM_HZ)) ||
    die "ARM clock was below 2.3 GHz after $label ($arm_hz Hz); comparison is invalid"
}

run_one_model() {
  local label="$1" model="$2" sha bytes log rc session
  [[ -r "$model" && -f "$model" ]] || die "model is not a readable regular file: $model"
  check_model_memory "$model"
  sha="$(file_sha256 "$model")"
  bytes="$(stat -c '%s' -- "$model")"
  log="$BENCH_RESULT_DIR/$label.log"
  printf '\n===== %s: %s =====\n' "$label" "$model"

  set +e
  if [[ -x /usr/bin/time ]]; then
    setsid timeout --signal=TERM --kill-after=10s 10m \
      /usr/bin/time -v "$BENCH_BIN" -m "$model" -p 128 -n 64 -t 4 >"$log" 2>&1 &
  else
    setsid timeout --signal=TERM --kill-after=10s 10m \
      "$BENCH_BIN" -m "$model" -p 128 -n 64 -t 4 >"$log" 2>&1 &
  fi
  BENCH_RUN_PID=$!
  session="$BENCH_RUN_PID"
  wait "$session"
  rc=$?
  if pgrep -s "$session" >/dev/null 2>&1; then
    terminate_active_benchmark
    ((rc != 0)) || rc=125
  else
    BENCH_RUN_PID=""
  fi
  set -e
  cat -- "$log"
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$label" "$model" "$bytes" "$sha" "$rc" "$log" >> "$BENCH_RESULT_DIR/runs.tsv"
  verify_run_health "$label"
  BENCH_LAST_RC="$rc"
}

write_summary() {
  local summary_rc
  set +e
  "$BENCH_PYTHON" - "$BENCH_RESULT_DIR/runs.tsv" \
    "$BENCH_RESULT_DIR/summary.csv" "$BENCH_RESULT_DIR/summary.md" <<'PY'
import csv
import pathlib
import re
import sys

runs_path, csv_path, md_path = map(pathlib.Path, sys.argv[1:])
rows = []
with runs_path.open(newline="") as handle:
    reader = csv.DictReader(handle, delimiter="\t")
    for row in reader:
        rates = {}
        model_name = ""
        max_rss_kib = None
        table_columns = None
        log_path = pathlib.Path(row["log"])
        if log_path.exists():
            for line in log_path.read_text(errors="replace").splitlines():
                if line.lstrip().startswith("Maximum resident set size (kbytes):"):
                    try:
                        max_rss_kib = int(line.rsplit(":", 1)[1].strip())
                    except ValueError:
                        pass
                if not line.lstrip().startswith("|"):
                    continue
                cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
                lowered = [cell.lower() for cell in cells]
                if "test" in lowered and "t/s" in lowered:
                    table_columns = {name: index for index, name in enumerate(lowered)}
                    continue
                if not table_columns or len(cells) <= max(table_columns.values()):
                    continue
                test = cells[table_columns["test"]]
                if test not in {"pp128", "tg64"}:
                    continue
                match = re.search(
                    r"([0-9]+(?:\.[0-9]+)?)\s*(?:±\s*([0-9]+(?:\.[0-9]+)?))?",
                    cells[table_columns["t/s"]],
                )
                if not match:
                    continue
                model_name = cells[table_columns.get("model", 0)]
                rates[test] = (float(match.group(1)), float(match.group(2) or 0.0))
        row["model_name"] = model_name
        row["pp128"] = rates.get("pp128", (None, None))[0]
        row["pp128_sd"] = rates.get("pp128", (None, None))[1]
        row["tg64"] = rates.get("tg64", (None, None))[0]
        row["tg64_sd"] = rates.get("tg64", (None, None))[1]
        row["max_rss_kib"] = max_rss_kib
        row["status"] = "ok" if row["rc"] == "0" and row["pp128"] and row["tg64"] else "failed"
        rows.append(row)

baseline = next((row for row in rows if row["label"] == "active-baseline-start" and row["status"] == "ok"), None)
for row in rows:
    if baseline and row["status"] == "ok":
        row["pp_vs_baseline_pct"] = 100.0 * row["pp128"] / baseline["pp128"]
        row["tg_vs_baseline_pct"] = 100.0 * row["tg64"] / baseline["tg64"]
    else:
        row["pp_vs_baseline_pct"] = None
        row["tg_vs_baseline_pct"] = None

fields = [
    "label", "model_name", "path", "bytes", "sha256", "rc", "status",
    "pp128", "pp128_sd", "pp_vs_baseline_pct",
    "tg64", "tg64_sd", "tg_vs_baseline_pct", "max_rss_kib", "log",
]
with pathlib.Path(csv_path).open("w", newline="") as handle:
    writer = csv.DictWriter(handle, fields)
    writer.writeheader()
    writer.writerows({key: row.get(key) for key in fields} for row in rows)

def number(value, suffix=""):
    return "—" if value is None else f"{value:.2f}{suffix}"

lines = [
    "# CM5 LLM throughput benchmark",
    "",
    "Fixed command: `llama-bench -p 128 -n 64 -t 4`.",
    "The pp128 and tg64 rows are independent tests; tg64 starts at effectively empty context, not after pp128.",
    "This compares raw throughput; it does not grade answer quality, production-depth/cache TTFT, or STT.",
    "",
    "| run | llama model | status | pp128 tok/s | vs baseline | tg64 tok/s | vs baseline | max RSS MiB |",
    "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
]
for row in rows:
    lines.append(
        f"| {row['label']} | {row['model_name'] or pathlib.Path(row['path']).name} | {row['status']} | "
        f"{number(row['pp128'])} | {number(row['pp_vs_baseline_pct'], '%')} | "
        f"{number(row['tg64'])} | {number(row['tg_vs_baseline_pct'], '%')} | "
        f"{number(None if row['max_rss_kib'] is None else row['max_rss_kib'] / 1024)} |"
    )

start = baseline
end = next((row for row in rows if row["label"] == "active-baseline-end" and row["status"] == "ok"), None)
if start and end:
    pp_drift = 100.0 * (end["pp128"] - start["pp128"]) / start["pp128"]
    tg_drift = 100.0 * (end["tg64"] - start["tg64"]) / start["tg64"]
    lines.extend(["", f"Baseline repeat drift: pp128 {pp_drift:+.2f}%, tg64 {tg_drift:+.2f}%."])
    if abs(pp_drift) > 7 or abs(tg_drift) > 7:
        lines.append("WARNING: baseline drift exceeded 7%; rerun before ranking close results.")

pathlib.Path(md_path).write_text("\n".join(lines) + "\n")

challengers = [row for row in rows if not row["label"].startswith("active-baseline-")]
valid = bool(start and end and challengers and all(row["status"] == "ok" for row in challengers))
if start and end:
    valid = valid and abs(pp_drift) <= 7 and abs(tg_drift) <= 7
raise SystemExit(0 if valid else 1)
PY
  summary_rc=$?
  set -e
  cat -- "$BENCH_RESULT_DIR/summary.md"
  ((summary_rc == 0)) ||
    die "benchmark metrics are incomplete or baseline drift exceeded 7%; ranking is invalid"
}

load_manifest
mkdir -p -- "$BENCH_MODELS_DIR"

# For a combined run, reject a missing/misconfigured benchmark installation
# before spending time or disk on the multi-GB candidate downloads. A pure
# --download-only run deliberately does not require llama.cpp to be installed.
if [[ "$BENCH_MODE" != download ]]; then
  load_active_paths
  [[ -x "$BENCH_BIN" ]] || die "llama-bench is not executable: $BENCH_BIN"
  [[ -r "$BENCH_ACTIVE_MODEL" && -f "$BENCH_ACTIVE_MODEL" ]] ||
    die "active baseline is not a readable regular file: $BENCH_ACTIVE_MODEL"
  command -v timeout >/dev/null 2>&1 || die "GNU timeout is required"
  command -v setsid >/dev/null 2>&1 || die "setsid is required"
  command -v pgrep >/dev/null 2>&1 || die "pgrep is required"
  command -v pkill >/dev/null 2>&1 || die "pkill is required"
  command -v vcgencmd >/dev/null 2>&1 || die "vcgencmd is required on the CM5"
  command -v fuser >/dev/null 2>&1 || die "fuser is required"
fi

if [[ "$BENCH_MODE" != benchmark ]]; then
  preflight_download_space
  for ((i = 0; i < ${#BENCH_IDS[@]}; ++i)); do
    download_one "$i"
  done
fi

if [[ "$BENCH_MODE" == download ]]; then
  printf 'Candidates are downloaded and verified in %s. The service was not interrupted.\n' "$BENCH_MODELS_DIR"
  exit 0
fi

for ((i = 0; i < ${#BENCH_IDS[@]}; ++i)); do
  candidate="$BENCH_MODELS_DIR/${BENCH_FILENAMES[i]}"
  verified_model_file "$candidate" "${BENCH_BYTES[i]}" "${BENCH_SHA256[i]}" ||
    die "candidate is missing or invalid; run without --benchmark-only first: $candidate"
done

mkdir -p -- "$BENCH_RESULTS_ROOT"
BENCH_RESULT_DIR="$(mktemp -d "$BENCH_RESULTS_ROOT/run-$(date +%Y%m%d-%H%M%S)-XXXXXXXX")"
BENCH_LATEST_TMP="$(mktemp "$BENCH_RESULTS_ROOT/.latest-XXXXXXXX")"
printf '%s\n' "$BENCH_RESULT_DIR" > "$BENCH_LATEST_TMP"
mv -T -- "$BENCH_LATEST_TMP" "$BENCH_RESULTS_ROOT/latest.txt"
BENCH_LATEST_TMP=""
printf 'label\tpath\tbytes\tsha256\trc\tlog\n' > "$BENCH_RESULT_DIR/runs.tsv"

vcgencmd get_throttled | tee "$BENCH_RESULT_DIR/throttled-preflight.txt"
grep -Fxq throttled=0x0 "$BENCH_RESULT_DIR/throttled-preflight.txt" ||
  die "sticky throttle/power flags are set; fix power and reboot before benchmarking"

if systemctl --user is-active --quiet "$BENCH_SERVICE"; then
  BENCH_SERVICE_WAS_ACTIVE=1
  systemctl --user --no-pager -l status "$BENCH_SERVICE" \
    > "$BENCH_RESULT_DIR/service-status-before.log" 2>&1 || true
  # From this point onward cleanup must try to restore the originally-active
  # unit even if systemctl reports an error after partly stopping its cgroup.
  BENCH_SERVICE_STOPPED=1
  systemctl --user stop "$BENCH_SERVICE"
  for _ in $(seq 1 20); do
    state="$(systemctl --user show "$BENCH_SERVICE" -p ActiveState --value)"
    [[ "$state" == inactive ]] && break
    sleep 1
  done
  [[ "$state" == inactive ]] || die "service did not stop cleanly"
fi

if pgrep -af '[/]hw1ai/bin/hw1-ai-service|[/]llama-server([[:space:]]|$)' \
    > "$BENCH_RESULT_DIR/competing-processes.log"; then
  die "a competing AI service or llama-server is still running"
fi
if pgrep -af '[/]llama-bench([[:space:]]|$)|[/]llama-cli([[:space:]]|$)' \
    >> "$BENCH_RESULT_DIR/competing-processes.log"; then
  die "another llama benchmark or CLI process is running"
fi

if [[ -x "$BENCH_POWER_HELPER" ]] && \
    sudo -n "$BENCH_POWER_HELPER" status > "$BENCH_RESULT_DIR/power-before.json" 2>&1; then
  BENCH_PREVIOUS_PROFILE="$("$BENCH_PYTHON" - "$BENCH_RESULT_DIR/power-before.json" <<'PY'
import json
import pathlib
import sys

profile = json.loads(pathlib.Path(sys.argv[1]).read_text())["profile"]
if profile not in {"eco", "balanced", "performance"}:
    raise SystemExit(f"unsafe prior profile: {profile!r}")
print(profile)
PY
  )"
  if [[ "$BENCH_PREVIOUS_PROFILE" != performance ]]; then
    BENCH_POWER_CHANGED=1
    sudo -n "$BENCH_POWER_HELPER" profile performance \
      > "$BENCH_RESULT_DIR/power-performance.json"
  fi
elif ! governors_are_performance; then
  die "performance governor unavailable; install systemd/install-power-helper.sh and rerun"
fi

governors_are_performance || die "not every CPU policy entered the performance governor"
vcgencmd get_throttled | tee "$BENCH_RESULT_DIR/throttled-before.txt"
grep -Fxq throttled=0x0 "$BENCH_RESULT_DIR/throttled-before.txt" ||
  die "power/throttle flags changed during preflight"

BENCH_SWAP_USED_KIB_BEFORE="$(current_swap_used_kib)"
printf '%s\n' "$BENCH_SWAP_USED_KIB_BEFORE" > "$BENCH_RESULT_DIR/swap-used-kib-before.txt"
record_host_metadata
cp -- "$0" "$BENCH_RESULT_DIR/benchmark_llm_models.sh"
cp -- "$BENCH_MANIFEST" "$BENCH_RESULT_DIR/llm_benchmark_models.tsv"
sha256sum -- "$BENCH_RESULT_DIR/benchmark_llm_models.sh" \
  "$BENCH_RESULT_DIR/llm_benchmark_models.tsv" > "$BENCH_RESULT_DIR/evidence-sha256.txt"
start_telemetry

vcgencmd measure_clock arm | tee "$BENCH_RESULT_DIR/arm-clock-before.txt"
BENCH_ARM_HZ_BEFORE="$(sed -n 's/^frequency(0)=//p' "$BENCH_RESULT_DIR/arm-clock-before.txt")"
[[ "$BENCH_ARM_HZ_BEFORE" =~ ^[0-9]+$ ]] || die "could not read the pre-run ARM clock"
((BENCH_ARM_HZ_BEFORE >= BENCH_MIN_ARM_HZ)) ||
  die "ARM clock is below 2.3 GHz before the sweep ($BENCH_ARM_HZ_BEFORE Hz)"

run_one_model active-baseline-start "$BENCH_ACTIVE_MODEL"
((BENCH_LAST_RC == 0)) || die "active baseline failed; see active-baseline-start.log"
BENCH_BASELINE_OK=1

for ((i = 0; i < ${#BENCH_IDS[@]}; ++i)); do
  run_one_model "${BENCH_IDS[i]}" "$BENCH_MODELS_DIR/${BENCH_FILENAMES[i]}"
  if ((BENCH_LAST_RC == 0)); then
    BENCH_CHALLENGER_SUCCESSES=$((BENCH_CHALLENGER_SUCCESSES + 1))
  else
    printf 'Candidate %s failed to load or benchmark; continuing to the next model.\n' \
      "${BENCH_IDS[i]}" >&2
  fi
done

run_one_model active-baseline-end "$BENCH_ACTIVE_MODEL"
((BENCH_LAST_RC == 0)) || printf 'WARNING: ending baseline repeat failed.\n' >&2

write_summary
((BENCH_BASELINE_OK == 1)) || die "baseline was not measured"
((BENCH_CHALLENGER_SUCCESSES > 0)) || die "no challenger completed successfully"
printf 'Benchmark complete. Raw evidence and summary: %s\n' "$BENCH_RESULT_DIR"
