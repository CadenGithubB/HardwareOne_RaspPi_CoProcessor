#!/usr/bin/env bash
# Run the full local-model -> OpenClaw -> note-plugin -> Obsidian-vault memory gate.

set -Eeuo pipefail
umask 077
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ASSET_ROOT=/usr/local/libexec/hw1-openclaw
[[ -f $SCRIPT_DIR/openclaw_memory_probe.py ]] && ASSET_ROOT=$SCRIPT_DIR

PROBE="$ASSET_ROOT/openclaw_memory_probe.py"
CASES="$ASSET_ROOT/openclaw_memory_cases.json"
CONFIG=/etc/hw1-openclaw-bench/openclaw.json
NODE=/opt/hw1-openclaw/node/bin/node
OPENCLAW=/opt/hw1-openclaw/current/openclaw.mjs
PROD_CLI=/usr/local/sbin/hw1-openclaw
PRODUCTION_VAULT="@@VAULT_PATH@@"
PRODUCTION_MODEL_HEALTH="http://127.0.0.1:@@LLAMA_PORT@@/health"
RESULTS_ROOT=/var/lib/hw1-openclaw-bench/results
LOCK_PATH=/run/lock/hw1-openclaw-benchmark.lock
OPENCLAW_VERSION=2026.9.2
NODE_VERSION=24.20.0
MAX_TEMP_MILLIC=80000
TEMP_PATH=/sys/class/thermal/thermal_zone0/temp

REPEATS=1
CASE_TIMEOUT=600
MODEL_START_TIMEOUT=300
MODEL_PROBES=1
MODEL_MAX_TOKENS=64
TOTAL_TIMEOUT=0
ALLOW_CONTENTION=0

die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
note() { printf '%s\n' "$*"; }

bench_runtime() {
  runuser -u openclaw-bench -- env -i \
    HOME=/var/lib/hw1-openclaw-bench USER=openclaw-bench LOGNAME=openclaw-bench \
    LC_ALL=C PATH=/usr/bin:/bin \
    OPENCLAW_STATE_DIR=/var/lib/hw1-openclaw-bench/state \
    OPENCLAW_CONFIG_PATH="$CONFIG" \
    OPENCLAW_DISABLE_PLUGIN_REGISTRY_MIGRATION=1 \
    "$@"
}

verify_benchmark_identity() {
  local entry name uid gid home shell primary_entry primary_name primary_gid explicit
  local matching_users matching_groups actual_members
  entry="$(getent passwd openclaw-bench)"
  [[ -n $entry && $entry != *$'\n'* ]] || die "openclaw-bench must resolve to exactly one passwd entry"
  IFS=: read -r name _ uid gid _ home shell <<<"$entry"
  [[ $name == openclaw-bench && $uid =~ ^[0-9]+$ && $uid -gt 0 ]] ||
    die "openclaw-bench has an unsafe numeric identity"
  [[ $gid =~ ^[0-9]+$ && $gid -gt 0 ]] || die "openclaw-bench has an unsafe primary GID"
  [[ $home == /var/lib/hw1-openclaw-bench ]] || die "openclaw-bench home changed: $home"
  [[ $shell == /usr/sbin/nologin || $shell == /sbin/nologin ]] || die "openclaw-bench has a login shell"
  matching_users="$(getent passwd | awk -F: -v uid="$uid" '$3 == uid {print $1}')"
  [[ $matching_users == openclaw-bench ]] || die "openclaw-bench UID is shared with another account"
  primary_entry="$(getent group openclaw-bench)"
  [[ -n $primary_entry && $primary_entry != *$'\n'* ]] || die "openclaw-bench primary group is ambiguous"
  IFS=: read -r primary_name _ primary_gid explicit <<<"$primary_entry"
  [[ $primary_name == openclaw-bench && $primary_gid == "$gid" ]] ||
    die "openclaw-bench must use its same-named primary group"
  matching_groups="$(getent group | awk -F: -v gid="$gid" '$3 == gid {print $1}')"
  [[ $matching_groups == openclaw-bench ]] || die "openclaw-bench GID is shared with another group"
  actual_members="$({
    getent passwd | awk -F: -v gid="$gid" '$4 == gid {print $1}'
    [[ -z $explicit ]] || tr ',' '\n' <<<"$explicit"
  } | sed '/^$/d' | sort -u)"
  [[ $actual_members == openclaw-bench ]] || die "openclaw-bench group has unexpected members"
  [[ $(id -nG openclaw-bench) == openclaw-bench ]] ||
    die "openclaw-bench has unexpected supplementary groups"
}

require_openclaw_version() {
  local output
  if ! output="$(bench_runtime "$NODE" "$OPENCLAW" --version 2>&1)"; then
    die "OpenClaw version command failed: $output"
  fi
  if ! /usr/bin/python3 -I - "$OPENCLAW_VERSION" "$output" <<'PY'
import re
import sys

expected, output = sys.argv[1:]
match = re.fullmatch(r"OpenClaw ([^ ]+)(?: \([0-9a-f]{7}\))?", output)
if match is None or match.group(1) != expected:
    raise SystemExit(1)
PY
  then
    die "unexpected OpenClaw version string: $output"
  fi
}

usage() {
  cat <<'EOF'
Usage: sudo hw1-openclaw-benchmark [options]

Options:
  --repeats N              Repeat the three-session lifecycle 1..20 times.
  --case-timeout SECONDS   Per-agent-turn limit, 30..1800 (default 600).
  --model-start-timeout N  llama-server readiness limit, 30..900 (default 300).
  --model-probes N          Direct streaming LLM speed probes, 0..10 (default 1).
  --model-max-tokens N      Tokens per direct speed probe, 8..512 (default 64).
  --total-timeout N        Whole-probe limit; computed conservatively by default.
  --allow-contention       Deliberately run while hw1-ai-service/another
                           llama-server may be active; evidence is marked tainted.
  --config PATH            Benchmark config installed by bootstrap.
  --production-vault PATH  Vault whose inaccessibility to the benchmark UID is gated.
  -h, --help               Show this help.

The production OpenClaw unit is stopped for the run and restored on exit. The
script never deletes a result or disposable vault; each run remains as evidence.
EOF
}

need_value() { (($# >= 2)) || die "$1 requires a value"; }
while (($#)); do
  case "$1" in
    --repeats) need_value "$@"; REPEATS=$2; shift 2 ;;
    --case-timeout) need_value "$@"; CASE_TIMEOUT=$2; shift 2 ;;
    --model-start-timeout) need_value "$@"; MODEL_START_TIMEOUT=$2; shift 2 ;;
    --model-probes) need_value "$@"; MODEL_PROBES=$2; shift 2 ;;
    --model-max-tokens) need_value "$@"; MODEL_MAX_TOKENS=$2; shift 2 ;;
    --total-timeout) need_value "$@"; TOTAL_TIMEOUT=$2; shift 2 ;;
    --allow-contention) ALLOW_CONTENTION=1; shift ;;
    --config) need_value "$@"; CONFIG=$2; shift 2 ;;
    --production-vault) need_value "$@"; PRODUCTION_VAULT=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "run with sudo"
[[ $REPEATS =~ ^[0-9]+$ ]] && ((REPEATS >= 1 && REPEATS <= 20)) || die "--repeats must be 1..20"
[[ $CASE_TIMEOUT =~ ^[0-9]+$ ]] && ((CASE_TIMEOUT >= 30 && CASE_TIMEOUT <= 1800)) ||
  die "--case-timeout must be 30..1800"
[[ $MODEL_START_TIMEOUT =~ ^[0-9]+$ ]] && ((MODEL_START_TIMEOUT >= 30 && MODEL_START_TIMEOUT <= 900)) ||
  die "--model-start-timeout must be 30..900"
[[ $MODEL_PROBES =~ ^[0-9]+$ ]] && ((MODEL_PROBES >= 0 && MODEL_PROBES <= 10)) ||
  die "--model-probes must be 0..10"
[[ $MODEL_MAX_TOKENS =~ ^[0-9]+$ ]] && ((MODEL_MAX_TOKENS >= 8 && MODEL_MAX_TOKENS <= 512)) ||
  die "--model-max-tokens must be 8..512"
[[ $TOTAL_TIMEOUT =~ ^[0-9]+$ ]] || die "--total-timeout must be an integer"
if ((TOTAL_TIMEOUT == 0)); then
  TOTAL_TIMEOUT=$((MODEL_START_TIMEOUT + REPEATS * 3 * (CASE_TIMEOUT + 30) + 120))
fi
((TOTAL_TIMEOUT >= 120)) || die "--total-timeout must be >= 120"
[[ $CONFIG == /* && $PRODUCTION_VAULT == /* ]] || die "config and vault paths must be absolute"

for command in curl flock free getent install jq pgrep ps python3 realpath runuser sha256sum stat swapon systemctl timeout vcgencmd; do
  command -v "$command" >/dev/null 2>&1 || die "missing required command: $command"
done
getent passwd openclaw-bench >/dev/null || die "openclaw-bench account is not installed"
verify_benchmark_identity
[[ -r $TEMP_PATH ]] || die "CM5 thermal telemetry is unavailable: $TEMP_PATH"
for path in "$PROBE" "$CASES" "$CONFIG" "$NODE" "$OPENCLAW" "$PROD_CLI"; do
  [[ -f $path && ! -L $path ]] || die "required regular file is missing or symlinked: $path"
done
for path in "$PROBE" "$CASES" "$CONFIG"; do
  [[ $(realpath -e -- "$path") == "$path" ]] || die "required input has a symlinked/noncanonical path: $path"
done
[[ -d $PRODUCTION_VAULT && ! -L $PRODUCTION_VAULT ]] || die "managed production vault is missing or symlinked"
[[ $(realpath -e -- "$PRODUCTION_VAULT") == "$PRODUCTION_VAULT" ]] ||
  die "production vault path is symlinked or noncanonical"
[[ $(stat -c '%G %a' "$PRODUCTION_VAULT") == 'openclaw-notes 2770' ]] ||
  die "production vault group/mode changed"
[[ -f $PRODUCTION_VAULT/.hw1-openclaw-managed && ! -L $PRODUCTION_VAULT/.hw1-openclaw-managed ]] ||
  die "production vault has no valid managed marker"
[[ $(stat -c '%U:%G %a' "$PRODUCTION_VAULT/.hw1-openclaw-managed") == 'root:openclaw-notes 640' ]] ||
  die "production vault marker ownership/mode changed"
grep -Fxq 'hw1-openclaw-managed-v1' "$PRODUCTION_VAULT/.hw1-openclaw-managed" ||
  die "production vault marker content changed"
[[ $(bench_runtime "$NODE" --version) == v$NODE_VERSION ]] || die "unexpected Node version"
require_openclaw_version
runuser -u openclaw-bench -- test -r "$CONFIG" || die "benchmark UID cannot read its config"
runuser -u openclaw-bench -- test -r "$PROBE" || die "benchmark UID cannot read the probe"
runuser -u openclaw-bench -- test -r "$CASES" || die "benchmark UID cannot read the cases"
if [[ -e $PRODUCTION_VAULT ]] && {
  runuser -u openclaw-bench -- test -r "$PRODUCTION_VAULT" ||
    runuser -u openclaw-bench -- test -w "$PRODUCTION_VAULT" ||
    runuser -u openclaw-bench -- test -x "$PRODUCTION_VAULT"
}; then
  die "benchmark UID has read, write, or traversal access to the production vault"
fi

if [[ -e $LOCK_PATH || -L $LOCK_PATH ]]; then
  [[ -f $LOCK_PATH && ! -L $LOCK_PATH ]] || die "benchmark lock is not a regular file"
  [[ $(stat -c '%U:%G %a' "$LOCK_PATH") == 'root:root 600' ]] || die "benchmark lock ownership/mode changed"
else
  install -o root -g root -m 0600 /dev/null "$LOCK_PATH"
fi
exec 9<>"$LOCK_PATH"
flock -n 9 || die "another OpenClaw benchmark is running"

PROD_WAS_ACTIVE=0
PROBE_PID=
TELEMETRY_PID=
RUN_DIR=
PROBE_RUN_DIR=
cleanup() {
  local status=$? bench_pid residual=0 restored=0
  trap - EXIT INT TERM HUP
  if [[ -n ${PROBE_PID:-} ]] && kill -0 "$PROBE_PID" 2>/dev/null; then
    kill -TERM "$PROBE_PID" 2>/dev/null || true
    wait "$PROBE_PID" 2>/dev/null || true
  fi
  while read -r bench_pid; do
    [[ $bench_pid =~ ^[0-9]+$ ]] || continue
    residual=1
    kill -TERM "$bench_pid" 2>/dev/null || true
  done < <(pgrep -u openclaw-bench -x llama-server 2>/dev/null || true)
  while read -r bench_pid; do
    [[ $bench_pid =~ ^[0-9]+$ ]] || continue
    residual=1
    kill -TERM "$bench_pid" 2>/dev/null || true
  done < <(pgrep -u openclaw-bench -f '[/]opt/hw1-openclaw/current/openclaw\.mjs agent' 2>/dev/null || true)
  if ((residual)); then
    for _attempt in $(seq 1 10); do
      if ! pgrep -u openclaw-bench -x llama-server >/dev/null 2>&1 &&
        ! pgrep -u openclaw-bench -f '[/]opt/hw1-openclaw/current/openclaw\.mjs agent' >/dev/null 2>&1; then
        break
      fi
      sleep 1
    done
    while read -r bench_pid; do
      [[ $bench_pid =~ ^[0-9]+$ ]] || continue
      kill -KILL "$bench_pid" 2>/dev/null || true
    done < <(pgrep -u openclaw-bench -x llama-server 2>/dev/null || true)
    while read -r bench_pid; do
      [[ $bench_pid =~ ^[0-9]+$ ]] || continue
      kill -KILL "$bench_pid" 2>/dev/null || true
    done < <(pgrep -u openclaw-bench -f '[/]opt/hw1-openclaw/current/openclaw\.mjs agent' 2>/dev/null || true)
    note "WARNING: reaped a residual benchmark model/agent process"
    ((status == 0)) && status=3
  fi
  if [[ -n ${TELEMETRY_PID:-} ]] && kill -0 "$TELEMETRY_PID" 2>/dev/null; then
    kill -TERM "$TELEMETRY_PID" 2>/dev/null || true
    wait "$TELEMETRY_PID" 2>/dev/null || true
  fi
  if ((PROD_WAS_ACTIVE)) && ! systemctl is-active --quiet hw1-openclaw.service; then
    if systemctl start hw1-openclaw.service; then
      for _attempt in $(seq 1 30); do
        if systemctl is-active --quiet hw1-openclaw-model.service &&
          /usr/bin/curl --disable --fail --silent --max-time 2 --output /dev/null "$PRODUCTION_MODEL_HEALTH" &&
          systemctl is-active --quiet hw1-openclaw.service &&
          timeout 5s "$PROD_CLI" gateway status --require-rpc --json >/dev/null 2>&1; then
          restored=1
          break
        fi
        sleep 1
      done
    fi
    if ((!restored)); then
      note "WARNING: failed to restore a healthy hw1-openclaw.service Gateway"
      status=3
    fi
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

assert_no_hw1_ai_process() {
  if pgrep -f '[/]hw1ai/bin/hw1-ai-service' >/dev/null 2>&1; then
    die "hw1-ai-service is active; stop its user unit first or use --allow-contention deliberately"
  fi
}
if ((!ALLOW_CONTENTION)); then
  assert_no_hw1_ai_process
  sleep 6
  assert_no_hw1_ai_process
fi
if systemctl is-active --quiet hw1-openclaw.service; then
  PROD_WAS_ACTIVE=1
  systemctl stop hw1-openclaw.service hw1-openclaw-model.service
fi
systemctl is-active --quiet hw1-openclaw-model.service &&
  die "production model unit is active without a production Gateway"
if ((!ALLOW_CONTENTION)) && pgrep -x llama-server >/dev/null 2>&1; then
  die "another llama-server is running after the production Gateway stopped"
fi
if ((!ALLOW_CONTENTION)) && pgrep -af '[/]openclaw\.mjs.*gateway' >/dev/null 2>&1; then
  die "another OpenClaw Gateway process is running"
fi

[[ -d $RESULTS_ROOT && ! -L $RESULTS_ROOT ]] || die "managed results root is missing or symlinked"
[[ $(stat -c '%U:%G %a' "$RESULTS_ROOT") == 'root:openclaw-bench 710' ]] ||
  die "managed results root ownership/mode changed"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="$(mktemp -d "$RESULTS_ROOT/${stamp}-XXXXXX")"
chown root:openclaw-bench "$RUN_DIR"
chmod 0710 "$RUN_DIR"
PROBE_RUN_DIR="$RUN_DIR/probe"
install -d -o openclaw-bench -g openclaw-bench -m 0700 "$PROBE_RUN_DIR"

readarray -t model_facts < <(/usr/bin/python3 -I - "$CONFIG" <<'PY'
import json
import pathlib
import sys

config = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
local = config["models"]["providers"]["hw1local"]["localService"]
args = local["args"]
index = args.index("--model")
print(local["command"])
print(args[index + 1])
print(local["healthUrl"])
PY
)
SERVER_BIN=${model_facts[0]:-}
MODEL_PATH=${model_facts[1]:-}
MODEL_HEALTH=${model_facts[2]:-}
[[ -f $SERVER_BIN && -x $SERVER_BIN && ! -L $SERVER_BIN ]] || die "invalid benchmark llama-server: $SERVER_BIN"
[[ -f $MODEL_PATH && -r $MODEL_PATH && ! -L $MODEL_PATH ]] || die "invalid benchmark model: $MODEL_PATH"
[[ $(realpath -e -- "$SERVER_BIN") == "$SERVER_BIN" ]] || die "llama-server path contains a symlink"
[[ $(realpath -e -- "$MODEL_PATH") == "$MODEL_PATH" ]] || die "model path contains a symlink"
runuser -u openclaw-bench -- test -x "$SERVER_BIN" || die "benchmark UID cannot execute llama-server"
runuser -u openclaw-bench -- test -r "$MODEL_PATH" || die "benchmark UID cannot read the model"

throttled_before="$(vcgencmd get_throttled)"
printf '%s\n' "$throttled_before" >"$RUN_DIR/throttled-before.txt"
[[ $throttled_before == throttled=0x0 ]] || die "sticky power/throttle flags are set: $throttled_before"
temp_before_millic="$(tr -d '[:space:]' <"$TEMP_PATH")"
[[ $temp_before_millic =~ ^[0-9]+$ ]] || die "could not parse CM5 temperature"
((temp_before_millic <= MAX_TEMP_MILLIC)) || die "CM5 is already above $((MAX_TEMP_MILLIC / 1000)) C"
swap_used_before_kib="$(free -k | awk '$1 == "Swap:" {print $3}')"
[[ $swap_used_before_kib =~ ^[0-9]+$ ]] || die "could not parse starting swap use"

{
  printf 'benchmark=hw1-openclaw-obsidian-memory\n'
  printf 'started_utc=%s\n' "$stamp"
  printf 'repeats=%s\ncase_timeout_seconds=%s\nmodel_start_timeout_seconds=%s\n' \
    "$REPEATS" "$CASE_TIMEOUT" "$MODEL_START_TIMEOUT"
  printf 'model_probes=%s\nmodel_max_tokens=%s\n' "$MODEL_PROBES" "$MODEL_MAX_TOKENS"
  printf 'total_timeout_seconds=%s\nallow_contention=%s\n' "$TOTAL_TIMEOUT" "$ALLOW_CONTENTION"
  printf 'max_temp_millic=%s\n' "$MAX_TEMP_MILLIC"
  printf 'openclaw_version=%s\nnode_version=%s\n' "$OPENCLAW_VERSION" "$NODE_VERSION"
  printf 'model_health=%s\nproduction_vault=%s\n' "$MODEL_HEALTH" "$PRODUCTION_VAULT"
} >"$RUN_DIR/run-metadata.env"
uname -a >"$RUN_DIR/uname.txt"
cp -- /proc/meminfo "$RUN_DIR/meminfo-before.txt"
free -b >"$RUN_DIR/free-before.txt"
swapon --show --bytes >"$RUN_DIR/swap-before.txt" || true
{
  if pgrep -f '[/]hw1ai/bin/hw1-ai-service' >/dev/null 2>&1; then
    printf 'active process(es)\n'
    pgrep -af '[/]hw1ai/bin/hw1-ai-service' || true
  else
    printf 'no matching process\n'
  fi
} >"$RUN_DIR/hw1-ai-service-state.txt"
systemctl is-active hw1-openclaw.service >"$RUN_DIR/hw1-openclaw-state-during.txt" || true
{
  sha256sum -- "$PROBE" "$CASES" "$CONFIG" "$NODE" "$OPENCLAW" "$SERVER_BIN" "$MODEL_PATH"
} >"$RUN_DIR/input-sha256.txt"
cp -- "$PROBE" "$RUN_DIR/openclaw_memory_probe.py"
cp -- "$CASES" "$RUN_DIR/openclaw_memory_cases.json"
cp -- "$CONFIG" "$RUN_DIR/benchmark-config.json"

telemetry() {
  local timestamp temp temp_millic throttled volts clock available swap_used llama_rss node_rss load
  printf 'timestamp_utc\ttemp\ttemp_millic\tthrottled\tcore_volts\tarm_clock\tmem_available_kib\tswap_used_kib\tllama_rss_kib\tnode_rss_kib\tloadavg\n'
  while :; do
    timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    temp="$(vcgencmd measure_temp 2>/dev/null || printf unavailable)"
    temp_millic="$(tr -d '[:space:]' <"$TEMP_PATH")"
    throttled="$(vcgencmd get_throttled 2>/dev/null || printf unavailable)"
    volts="$(vcgencmd measure_volts core 2>/dev/null || printf unavailable)"
    clock="$(vcgencmd measure_clock arm 2>/dev/null || printf unavailable)"
    available="$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)"
    swap_used="$(free -k | awk '$1 == "Swap:" {print $3}')"
    llama_rss="$(ps -C llama-server -o rss= 2>/dev/null | awk '{sum += $1} END {print sum + 0}' || true)"
    node_rss="$(ps -C node -o rss= 2>/dev/null | awk '{sum += $1} END {print sum + 0}' || true)"
    load="$(cut -d' ' -f1-3 /proc/loadavg)"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$timestamp" "$temp" "$temp_millic" "$throttled" "$volts" "$clock" "$available" \
      "$swap_used" "$llama_rss" "$node_rss" "$load"
    sleep 2
  done
}
telemetry >"$RUN_DIR/telemetry.tsv" &
TELEMETRY_PID=$!

cd -- "$RUN_DIR"
set +e
timeout --signal=TERM --kill-after=30s "${TOTAL_TIMEOUT}s" \
  runuser -u openclaw-bench -- \
  env -i HOME=/var/lib/hw1-openclaw-bench USER=openclaw-bench LOGNAME=openclaw-bench \
    LC_ALL=C PATH=/usr/bin:/bin \
  python3 "$PROBE" \
    --node "$NODE" \
    --openclaw "$OPENCLAW" \
    --config "$CONFIG" \
    --cases "$CASES" \
    --run-dir "$PROBE_RUN_DIR" \
    --production-vault "$PRODUCTION_VAULT" \
    --repeats "$REPEATS" \
    --case-timeout "$CASE_TIMEOUT" \
    --model-start-timeout "$MODEL_START_TIMEOUT" \
    --model-probes "$MODEL_PROBES" \
    --model-max-tokens "$MODEL_MAX_TOKENS" \
  >"$RUN_DIR/probe-stdout.json" 2>"$RUN_DIR/probe-stderr.log" &
PROBE_PID=$!
wait "$PROBE_PID"
probe_status=$?
PROBE_PID=
set -e

kill -TERM "$TELEMETRY_PID" 2>/dev/null || true
wait "$TELEMETRY_PID" 2>/dev/null || true
TELEMETRY_PID=
vcgencmd get_throttled >"$RUN_DIR/throttled-after.txt"
cp -- /proc/meminfo "$RUN_DIR/meminfo-after.txt"
free -b >"$RUN_DIR/free-after.txt"
swapon --show --bytes >"$RUN_DIR/swap-after.txt" || true

throttled_after="$(cat "$RUN_DIR/throttled-after.txt")"
valid_power=1
[[ $throttled_after == throttled=0x0 ]] || valid_power=0
max_temp_millic="$(awk -F '\t' 'NR > 1 && $3 ~ /^[0-9]+$/ {if ($3 > max) max=$3; seen=1} END {print seen ? max : 999999}' "$RUN_DIR/telemetry.tsv")"
valid_thermal=1
((max_temp_millic <= MAX_TEMP_MILLIC)) || valid_thermal=0
swap_used_after_kib="$(free -k | awk '$1 == "Swap:" {print $3}')"
max_swap_used_kib="$(awk -F '\t' 'NR > 1 && $8 ~ /^[0-9]+$/ {if ($8 > max) max=$8; seen=1} END {print seen ? max : "invalid"}' "$RUN_DIR/telemetry.tsv")"
valid_swap=1
[[ $swap_used_after_kib =~ ^[0-9]+$ ]] || valid_swap=0
[[ $max_swap_used_kib =~ ^[0-9]+$ ]] || valid_swap=0
((valid_swap && max_swap_used_kib <= swap_used_before_kib && swap_used_after_kib <= swap_used_before_kib)) || valid_swap=0

/usr/bin/python3 -I - "$RUN_DIR" "$PROBE_RUN_DIR" "$probe_status" "$valid_power" "$valid_thermal" "$valid_swap" \
  "$ALLOW_CONTENTION" "$max_temp_millic" "$swap_used_before_kib" "$max_swap_used_kib" \
  "$swap_used_after_kib" <<'PY'
import json
import os
import pathlib
import pwd
import stat
import sys

run_dir = pathlib.Path(sys.argv[1])
probe_dir = pathlib.Path(sys.argv[2])
probe_exit = int(sys.argv[3])
valid_power = sys.argv[4] == "1"
valid_thermal = sys.argv[5] == "1"
valid_swap = sys.argv[6] == "1"
contention = sys.argv[7] == "1"
max_temp_millic = sys.argv[8]
swap_before, swap_peak, swap_after = sys.argv[9:12]
try:
    run_path = probe_dir / "run.json"
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(run_path, flags)
    try:
        info = os.fstat(fd)
        expected_uid = pwd.getpwnam("openclaw-bench").pw_uid
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != expected_uid
            or info.st_size > 16 * 1024 * 1024
        ):
            raise ValueError("unsafe probe run.json")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as handle:
            run = json.load(handle)
    finally:
        os.close(fd)
except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
    run = {"status": "MISSING", "repeats": []}
rows = []
for repeat in run.get("repeats", []):
    for case in repeat.get("cases", []):
        rows.append(
            (
                repeat.get("repeat"),
                case.get("id"),
                case.get("status"),
                case.get("wall_ms", ""),
                ", ".join(call.get("name", "?") for call in case.get("calls", [])),
            )
        )
valid = (
    probe_exit == 0
    and run.get("status") == "PASS"
    and valid_power
    and valid_thermal
    and valid_swap
    and not contention
)
lines = [
    "# OpenClaw agent + Obsidian memory benchmark",
    "",
    f"- Result: **{'PASS' if valid else 'FAIL/TAINTED'}**",
    f"- Probe status: `{run.get('status', 'MISSING')}` (exit `{probe_exit}`)",
    f"- Power validity: `{'valid' if valid_power else 'invalid'}`",
    f"- Thermal validity: `{'valid' if valid_thermal else 'invalid'}` (peak `{max_temp_millic}` millicelsius)",
    f"- Swap validity: `{'valid' if valid_swap else 'invalid'}` (baseline `{swap_before}`, peak `{swap_peak}`, final `{swap_after}` KiB)",
    f"- Contention allowed: `{str(contention).lower()}`",
    f"- Model startup: `{run.get('model_startup_ms', 'unknown')} ms`",
]
speed = run.get("model_speed") or {}
if speed.get("status") == "PASS":
    samples = speed.get("samples") or []
    lines.extend(
        [
            f"- Direct model speed probes: `{len(samples)}`",
            "",
            "| Probe | Prompt tokens | Completion tokens | TTFT ms | E2E ms | Decode tok/s |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for sample in samples:
        lines.append(
            f"| {sample.get('sample', '')} | {sample.get('prompt_tokens', '')} | "
            f"{sample.get('completion_tokens', '')} | {sample.get('ttft_ms', '')} | "
            f"{sample.get('e2e_ms', '')} | {sample.get('tokens_per_second', '')} |"
        )
elif speed.get("status") == "SKIPPED":
    lines.append("- Direct model speed probes: `skipped`")
else:
    errors = speed.get("errors") or ["unknown error"]
    lines.append(f"- Direct model speed probes: `error` ({errors[0]})")
lines.extend(
    [
        "",
        "| Repeat | Case | Status | Wall ms | Proven tool calls |",
        "|---:|---|---|---:|---|",
    ]
)
for repeat, case, status, wall_ms, calls in rows:
    lines.append(f"| {repeat} | {case} | {status} | {wall_ms} | {calls} |")
lines.extend(["", "The retained `run.json`, session JSONL, vault, hashes, logs, and telemetry are canonical evidence.", ""])
(run_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
PY

latest_tmp="$RESULTS_ROOT/.latest.$$"
ln -s -- "$(basename -- "$RUN_DIR")" "$latest_tmp"
mv -Tf -- "$latest_tmp" "$RESULTS_ROOT/latest"
note "Evidence: $RUN_DIR"
note "Summary:  $RUN_DIR/summary.md"
if ((probe_status != 0)); then
  [[ $probe_status -ne 124 ]] || note "The whole probe was right-censored at ${TOTAL_TIMEOUT}s."
  exit "$probe_status"
fi
((valid_power)) || die "power/throttle flags changed during the run: $throttled_after"
((valid_thermal)) || die "temperature exceeded $((MAX_TEMP_MILLIC / 1000)) C (peak ${max_temp_millic} millicelsius)"
((valid_swap)) || die "swap use grew during the run (baseline ${swap_used_before_kib}, peak ${max_swap_used_kib}, final ${swap_used_after_kib} KiB)"
if ((ALLOW_CONTENTION)); then
  die "probe completed, but --allow-contention makes the performance result non-canonical"
fi
note "PASS: write/read, cross-session recall, and archive/search were proven from transcripts and vault state."
