#!/usr/bin/env bash
# Reproducibly install a notes-only OpenClaw gateway on a 16 GB CM5/Pi 5.
#
# The gateway and its llama-server child run as a locked, dedicated agent
# account.  The default is `openclaw`; a human login with that name is detected
# and can be paired with a distinct `--agent-user` identity.
# A second locked account, `openclaw-bench`, owns disposable benchmark state and
# cannot traverse the production Obsidian vault. Runtime, plugin and config
# inputs are root-owned; the model gets only the ten note_* tools.

set -Eeuo pipefail
umask 077
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

OPENCLAW_VERSION=2026.9.2
OPENCLAW_URL="https://registry.npmjs.org/openclaw/-/openclaw-${OPENCLAW_VERSION}.tgz"
OPENCLAW_BYTES=0
OPENCLAW_INTEGRITY="sha512-M6C7UsnX815nv26qBJFYGe6aGzv+ftZLRzV6S9oRXUtXg2Yn67eVntpssT94kgkquKVSeUxerUg0j1ONp4WYQg=="
NODE_VERSION=24.20.0
NODE_URL="https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-arm64.tar.xz"
NODE_BYTES=0
NODE_SHA256=5f4ddab610c1ab2016b3c227cebdbf6d9495161487e4739c7b90090595f465f7
NOTES_BUNDLE_VERSION=1.4.0-hw1.5

OPENCLAW_USER=openclaw
MODEL_USER=openclaw-model
BENCH_USER=openclaw-bench
BUILD_USER=_openclaw-build
NOTES_GROUP=openclaw-notes
MODEL_ACCESS_GROUP=openclaw-model-access
OPENCLAW_HOME=/var/lib/hw1-openclaw
MODEL_HOME=/var/lib/hw1-openclaw-model
BENCH_HOME=/var/lib/hw1-openclaw-bench
BUILD_HOME=/var/lib/hw1-openclaw-build
OPT_ROOT=/opt/hw1-openclaw
CONFIG_ROOT=/etc/hw1-openclaw
MODEL_CONFIG_ROOT=/etc/hw1-openclaw-model
BENCH_CONFIG_ROOT=/etc/hw1-openclaw-bench
AUDIT_ROOT=/etc/hw1-openclaw/verification
CACHE_ROOT=/var/cache/hw1-openclaw-install
BENCH_ASSET_ROOT=/usr/local/libexec/hw1-openclaw

DEFAULT_MODEL_PATH=/opt/models/LFM2-8B-A1B-UD-Q3_K_XL.gguf
DEFAULT_MODEL_BYTES=3676339264
DEFAULT_MODEL_SHA256=d10253b60d9699c4936a024fded42cba4581dc3640182146cba95fe57c143ac6
DEFAULT_MODEL_REPO=unsloth/LFM2-8B-A1B-GGUF
DEFAULT_MODEL_REV=01c1c9ba807324289806d69f5895e11aff6de784
DEFAULT_MODEL_FILE=LFM2-8B-A1B-UD-Q3_K_XL.gguf
DEFAULT_SERVER_BIN=/opt/llama.cpp/build/bin/llama-server
DEFAULT_LLAMA_ROOT=/opt/llama.cpp
DEFAULT_LLAMA_REPO=https://github.com/ggml-org/llama.cpp.git
DEFAULT_LLAMA_REF=b10516
DEFAULT_LLAMA_COMMIT=b95502b
MODEL_PATH=$DEFAULT_MODEL_PATH
SERVER_BIN=$DEFAULT_SERVER_BIN
VAULT_PATH=/srv/hw1-openclaw-vaults/main
CONTEXT_TOKENS=16384
GATEWAY_PORT=18789
LLAMA_PORT=18080
BENCH_LLAMA_PORT=18081
OPERATOR_USER=
AGENT_USER_EXPLICIT=0

DRY_RUN=0
ASSUME_YES=0
NO_START=0
ALLOW_NON_PI=0
ALLOW_NETWORK=0
PROVISION_MODEL=1
PROVISION_LLAMA=1
WITH_HOST_HARDENING=0
WITH_FIREWALL=0
ADOPT_VAULT=0
LIVE_VALIDATION_PENDING=0
TEMP_DIRS=()
TEMP_FILES=()

note() { printf '  %s\n' "$*"; }
ok() { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*" >&2; }
phase() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mabort:\033[0m %s\n' "$*" >&2; exit 1; }

track_temp_dir() {
  case "$1" in
    /var/tmp/hw1-openclaw-node.*|/var/tmp/hw1-openclaw-release.*|\
    /var/tmp/hw1-openclaw-build-home.*|/var/tmp/hw1-agent-notes.*) ;;
    *) die "refusing to track unexpected temporary directory: $1" ;;
  esac
  TEMP_DIRS+=("$1")
}

track_temp_file() {
  case "$1" in
    "$CACHE_ROOT"/.download.*) ;;
    *) die "refusing to track unexpected temporary file: $1" ;;
  esac
  TEMP_FILES+=("$1")
}

on_exit() {
  local status=$? temp_dir temp_file
  trap - EXIT
  for temp_file in "${TEMP_FILES[@]-}"; do
    [[ -n $temp_file ]] || continue
    if [[ -L $temp_file ]]; then
      warn "left suspicious symlink at tracked temporary path: $temp_file"
    elif [[ -f $temp_file ]]; then
      rm -f -- "$temp_file"
    elif [[ -e $temp_file ]]; then
      warn "left unexpected object at tracked temporary path: $temp_file"
    fi
  done
  for temp_dir in "${TEMP_DIRS[@]-}"; do
    [[ -n $temp_dir ]] || continue
    if [[ -L $temp_dir ]]; then
      warn "left suspicious symlink at tracked temporary path: $temp_dir"
    elif [[ -d $temp_dir ]]; then
      rm -rf -- "$temp_dir"
    fi
  done
  if ((LIVE_VALIDATION_PENDING)); then
    systemctl disable --now hw1-openclaw.service >/dev/null 2>&1 || true
    systemctl stop hw1-openclaw-model.service >/dev/null 2>&1 || true
    warn "disabled and stopped the OpenClaw Gateway/model because live validation did not complete"
  fi
  exit "$status"
}
trap on_exit EXIT

usage() {
  cat <<'EOF'
Usage: sudo ./bootstrap_openclaw.sh [options]

Options:
  --model PATH             GGUF used by the dedicated 16k-context server.
  --server-bin PATH        Existing llama-server executable.
  --vault PATH             Dedicated direct child of /srv/hw1-openclaw-vaults/
                           (default /srv/hw1-openclaw-vaults/main).
  --adopt-vault            Adopt an existing vault root at --vault. This changes
                           only that root's group/mode, never recursive ownership.
  --operator USER          Opt in a human account to the vault-only group.
  --agent-user USER        Dedicated locked Gateway account (default openclaw;
                           use this when the human SSH account is also named
                           openclaw).
  --context N              Context tokens, 8192..32768 (default 16384).
  --gateway-port N         Loopback Gateway port (default 18789).
  --llama-port N           Production local-model port (default 18080).
  --bench-llama-port N     Isolated benchmark model port (default 18081).
  --allow-network          Permit outbound networking from the gateway unit.
                           The Gateway still binds only to loopback.
  --no-model-provision     Do not download the pinned default GGUF when absent.
  --no-llama-provision     Do not clone/build the pinned default llama.cpp server.
  --with-host-hardening    Enable unattended security updates and fail2ban/sshd.
  --with-firewall          Also enable UFW after allowing every detected SSH port.
  --no-start               Install and validate, but do not enable/start the unit.
  --allow-non-pi           Permit a Debian arm64 host that is not identified as Pi 5/CM5.
  --dry-run                Print mutations without making them.
  --yes, -y                Accept the download/package-install confirmation.
  -h, --help               Show this help.

The script never changes sshd authentication policy. Switch to keys-only SSH
separately, after verifying a second key-authenticated login, to avoid lockout.
EOF
}

need_value() { (($# >= 2)) || die "$1 requires a value"; }
while (($#)); do
  case "$1" in
    --model) need_value "$@"; MODEL_PATH=$2; shift 2 ;;
    --server-bin) need_value "$@"; SERVER_BIN=$2; shift 2 ;;
    --vault) need_value "$@"; VAULT_PATH=$2; shift 2 ;;
    --operator) need_value "$@"; OPERATOR_USER=$2; shift 2 ;;
    --agent-user) need_value "$@"; OPENCLAW_USER=$2; AGENT_USER_EXPLICIT=1; shift 2 ;;
    --adopt-vault) ADOPT_VAULT=1; shift ;;
    --context) need_value "$@"; CONTEXT_TOKENS=$2; shift 2 ;;
    --gateway-port) need_value "$@"; GATEWAY_PORT=$2; shift 2 ;;
    --llama-port) need_value "$@"; LLAMA_PORT=$2; shift 2 ;;
    --bench-llama-port) need_value "$@"; BENCH_LLAMA_PORT=$2; shift 2 ;;
    --allow-network) ALLOW_NETWORK=1; shift ;;
    --no-model-provision) PROVISION_MODEL=0; shift ;;
    --no-llama-provision) PROVISION_LLAMA=0; shift ;;
    --with-host-hardening) WITH_HOST_HARDENING=1; shift ;;
    --with-firewall) WITH_FIREWALL=1; WITH_HOST_HARDENING=1; shift ;;
    --no-start) NO_START=1; shift ;;
    --allow-non-pi) ALLOW_NON_PI=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

validate_account_name() {
  local label=$1 value=$2
  [[ $value =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] ||
    die "$label must be a Linux account name (lowercase letters, digits, _ or -; max 32 chars)"
}

select_agent_user() {
  if ((AGENT_USER_EXPLICIT)); then
    validate_account_name --agent-user "$OPENCLAW_USER"
  elif command -v getent >/dev/null 2>&1 && getent passwd openclaw >/dev/null 2>&1; then
    local shell
    shell="$(getent passwd openclaw | cut -d: -f7)"
    case "$shell" in
      /usr/sbin/nologin|/sbin/nologin) ;;
      *) OPENCLAW_USER=hw1-openclaw-agent; note "human login openclaw detected; using locked agent account $OPENCLAW_USER" ;;
    esac
  fi
  validate_account_name agent-user "$OPENCLAW_USER"
}

select_agent_user

run() {
  if ((DRY_RUN)); then
    printf '  would run:'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

confirm_plan() {
  ((ASSUME_YES || DRY_RUN)) && return 0
  [[ -t 0 ]] || die "non-interactive setup requires --yes"
  local reply
  read -r -p "  Install pinned Node/OpenClaw and required packages? [y/N] " reply
  [[ ${reply:-n} == y ]] || die "cancelled before changes"
}

quiesce_service_for_reconcile() {
  phase "quiesce installed Gateway"
  if ((DRY_RUN)); then
    note "would stop and disable any installed hw1-openclaw.service before replacing managed inputs"
    return 0
  fi
  LIVE_VALIDATION_PENDING=1
  systemctl disable --now hw1-openclaw.service >/dev/null 2>&1 || true
  systemctl stop hw1-openclaw-model.service >/dev/null 2>&1 || true
  ! systemctl is-active --quiet hw1-openclaw.service ||
    die "could not stop hw1-openclaw.service before reconciliation"
  ! systemctl is-active --quiet hw1-openclaw-model.service ||
    die "could not stop hw1-openclaw-model.service before reconciliation"
  ! systemctl is-enabled --quiet hw1-openclaw.service ||
    die "could not disable hw1-openclaw.service before reconciliation"
  if ((NO_START)); then
    LIVE_VALIDATION_PENDING=0
    ok "installed Gateway is stopped and disabled for --no-start staging"
  else
    ok "old Gateway is stopped and disabled until the new live gates pass"
  fi
}

validate_port() {
  [[ $2 =~ ^[1-9][0-9]{0,4}$ ]] || die "$1 must be an integer port"
  ((10#$2 <= 65535)) || die "$1 must be <= 65535"
}

validate_absolute_path() {
  [[ $2 == /* && $2 != / ]] || die "$1 must be an absolute, non-root path"
  [[ $2 =~ ^/[A-Za-z0-9._/-]+$ ]] || die "$1 contains a character unsafe for systemd or shell configuration"
  [[ $2 != */./* && $2 != */../* && $2 != */. && $2 != */.. && $2 != *//* ]] ||
    die "$1 must be lexically normalized (no '.', '..', or repeated '/')"
  local normalized
  normalized="$(/usr/bin/python3 -I -c 'import os,sys; print(os.path.normpath(sys.argv[1]))' "$2")"
  [[ $normalized == "$2" ]] || die "$1 must be in canonical lexical form: $normalized"
}

reject_service_hidden_path() {
  local label=$1 path=$2
  case "$path/" in
    /home/*|/root/*|/run/user/*|/tmp/*|/var/tmp/*|/dev/*)
      die "$label is hidden by ProtectHome, PrivateTmp, or PrivateDevices; place it under /opt or another durable system path"
      ;;
  esac
}

assert_no_symlink_components() {
  /usr/bin/python3 -I - "$1" <<'PY'
import os
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
if not target.is_absolute() or target == pathlib.Path("/"):
    raise SystemExit("path must be absolute and narrower than /")
cur = pathlib.Path(target.anchor)
for part in target.parts[1:]:
    cur /= part
    try:
        if cur.is_symlink():
            raise SystemExit(f"symlinked path component is not allowed: {cur}")
        cur.lstat()
    except FileNotFoundError:
        break
PY
}

verify_existing_managed_unit_for_quiesce() {
  local unit=$1 destination="/etc/systemd/system/$1" marker expected actual fragment
  marker="${destination}.hw1-managed-sha256"
  if [[ -e $destination || -L $destination ]]; then
    [[ -f $destination && ! -L $destination ]] || die "existing $unit is not a regular unit file"
    [[ -f $marker && ! -L $marker ]] || die "refusing to stop untracked existing unit: $unit"
    [[ $(stat -c '%U:%G %a' "$marker") == 'root:root 644' ]] ||
      die "managed marker ownership/mode changed for $unit"
    expected="$(tr -d '[:space:]' <"$marker")"
    actual="$(sha256sum "$destination" | awk '{print $1}')"
    [[ $expected =~ ^[0-9a-f]{64}$ && $actual == "$expected" ]] ||
      die "refusing to stop locally modified existing unit: $unit"
  elif [[ -e $marker || -L $marker ]]; then
    [[ -f $marker && ! -L $marker ]] || die "orphan unit marker is not regular: $marker"
    [[ $(stat -c '%U:%G %a' "$marker") == 'root:root 644' ]] ||
      die "orphan unit marker ownership/mode changed: $marker"
    expected="$(tr -d '[:space:]' <"$marker")"
    [[ $expected =~ ^[0-9a-f]{64}$ ]] || die "orphan unit marker is malformed: $marker"
  fi
  fragment="$(systemctl show --property FragmentPath --value "$unit" 2>/dev/null || true)"
  [[ -z $fragment || $fragment == "$destination" ]] ||
    die "refusing to stop unrelated $unit loaded from $fragment"
  if [[ ! -e $destination && ! -e $marker ]]; then
    ! systemctl is-active --quiet "$unit" || die "refusing to stop untracked active unit: $unit"
    ! systemctl is-enabled --quiet "$unit" || die "refusing to disable untracked enabled unit: $unit"
  fi
}

preflight() {
  phase "preflight"
  ((DRY_RUN || EUID == 0)) || die "run as root (sudo)"
  [[ -r $SCRIPT_DIR/openclaw.json.in ]] || die "missing openclaw.json.in"
  [[ -r $SCRIPT_DIR/benchmark.json.in ]] || die "missing benchmark.json.in"
  [[ -r $SCRIPT_DIR/hw1-openclaw.service ]] || die "missing service template"
  [[ -r $SCRIPT_DIR/hw1-openclaw-model.service ]] || die "missing model service template"
  [[ -x $SCRIPT_DIR/wait_model_ready.sh ]] || die "missing executable model-readiness helper"
  [[ -r $SCRIPT_DIR/hw1-openclaw ]] || die "missing CLI wrapper"
  [[ -r $SCRIPT_DIR/../tools/openclaw/benchmark_openclaw.sh ]] || die "missing benchmark wrapper"
  [[ -r $SCRIPT_DIR/../tools/openclaw/openclaw_memory_probe.py ]] || die "missing benchmark probe"
  [[ -r $SCRIPT_DIR/../tools/openclaw/openclaw_memory_cases.json ]] || die "missing benchmark cases"
  [[ -r $SCRIPT_DIR/vendor/agent-notes/SHA256SUMS ]] || die "missing notes SHA256SUMS"
  command -v python3 >/dev/null 2>&1 || die "python3 is required for safe rendering"
  command -v systemctl >/dev/null 2>&1 || die "systemd is required"
  command -v apt-get >/dev/null 2>&1 || die "this installer requires Debian-family apt"
  [[ -r /etc/os-release ]] || die "cannot identify the operating system"
  verify_existing_managed_unit_for_quiesce hw1-openclaw.service
  verify_existing_managed_unit_for_quiesce hw1-openclaw-model.service
  local managed_path
  for managed_path in \
    "$OPENCLAW_HOME" "$OPENCLAW_HOME/state" "$OPENCLAW_HOME/workspace" \
    "$MODEL_HOME" "$MODEL_HOME/workspace" \
    "$BENCH_HOME" "$BENCH_HOME/state" "$BENCH_HOME/workspace" "$BENCH_HOME/results" \
    "$BUILD_HOME" "$OPT_ROOT" "$CONFIG_ROOT" "$MODEL_CONFIG_ROOT" "$BENCH_CONFIG_ROOT" "$CACHE_ROOT" "$CACHE_ROOT/npm" \
    "$AUDIT_ROOT" "$BENCH_ASSET_ROOT"; do
    assert_no_symlink_components "$managed_path"
  done

  validate_absolute_path --model "$MODEL_PATH"
  validate_absolute_path --server-bin "$SERVER_BIN"
  validate_absolute_path --vault "$VAULT_PATH"
  reject_service_hidden_path --model "$MODEL_PATH"
  reject_service_hidden_path --server-bin "$SERVER_BIN"
  assert_no_symlink_components "$MODEL_PATH"
  assert_no_symlink_components "$SERVER_BIN"
  validate_port --gateway-port "$GATEWAY_PORT"
  validate_port --llama-port "$LLAMA_PORT"
  validate_port --bench-llama-port "$BENCH_LLAMA_PORT"
  [[ $GATEWAY_PORT != "$LLAMA_PORT" && $GATEWAY_PORT != "$BENCH_LLAMA_PORT" && $LLAMA_PORT != "$BENCH_LLAMA_PORT" ]] ||
    die "gateway and model ports must be distinct"
  [[ $CONTEXT_TOKENS =~ ^[0-9]+$ ]] || die "--context must be an integer"
  ((CONTEXT_TOKENS >= 8192 && CONTEXT_TOKENS <= 32768)) || die "--context must be 8192..32768"

  [[ $VAULT_PATH == /srv/hw1-openclaw-vaults/* ]] ||
    die "--vault must be strictly below /srv/hw1-openclaw-vaults/"
  [[ $VAULT_PATH != /srv/hw1-openclaw-vaults ]] || die "--vault must name one dedicated vault"
  [[ ${VAULT_PATH#/srv/hw1-openclaw-vaults/} != */* ]] ||
    die "--vault must be a direct child of /srv/hw1-openclaw-vaults/"
  assert_no_symlink_components "$VAULT_PATH"

  local arch model ram_kib
  arch="$(uname -m)"
  [[ $arch == aarch64 || $arch == arm64 ]] || die "pinned Node artifact requires arm64; got $arch"
  model=""
  [[ ! -r /proc/device-tree/model ]] || model="$(tr -d '\0' </proc/device-tree/model)"
  if [[ $model != *"Raspberry Pi 5"* && $model != *"Compute Module 5"* ]]; then
    ((ALLOW_NON_PI)) || die "expected Pi 5/CM5; detected '${model:-unknown}' (use --allow-non-pi deliberately)"
    warn "non-Pi platform check bypassed: ${model:-unknown}"
  else
    ok "platform: $model"
  fi
  if [[ -r /proc/meminfo ]]; then
    ram_kib="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
    ((ram_kib >= 12 * 1024 * 1024)) || warn "less than 12 GiB detected; 16k local-agent results may swap or OOM"
  fi
  if [[ ! -f $MODEL_PATH || ! -r $MODEL_PATH ]]; then
    if [[ $MODEL_PATH == "$DEFAULT_MODEL_PATH" && $PROVISION_MODEL -eq 1 ]]; then
      warn "default GGUF is absent; the installer will download and verify it"
    elif ((NO_START)); then
      warn "model is not readable yet: $MODEL_PATH"
    else
      die "model is not readable: $MODEL_PATH"
    fi
  fi
  if [[ ! -f $SERVER_BIN || ! -x $SERVER_BIN ]]; then
    if [[ $SERVER_BIN == "$DEFAULT_SERVER_BIN" && $PROVISION_LLAMA -eq 1 ]]; then
      warn "default llama-server is absent; the installer will build it from $DEFAULT_LLAMA_REF"
    elif ((NO_START)); then
      warn "llama-server is not executable yet: $SERVER_BIN"
    else
      die "llama-server is not executable: $SERVER_BIN"
    fi
  fi
  case "$OPERATOR_USER" in
    "$OPENCLAW_USER"|"$MODEL_USER"|"$BENCH_USER"|"$BUILD_USER")
      die "--operator must not name a managed service identity: $OPERATOR_USER"
      ;;
  esac
  [[ -z $OPERATOR_USER || $OPERATOR_USER == root ]] || getent passwd "$OPERATOR_USER" >/dev/null || die "operator user does not exist: $OPERATOR_USER"
  if [[ -e $VAULT_PATH && ! -d $VAULT_PATH ]]; then
    die "vault path exists but is not a directory: $VAULT_PATH"
  fi
  if [[ -e $VAULT_PATH/.hw1-openclaw-managed || -L $VAULT_PATH/.hw1-openclaw-managed ]]; then
    [[ -f $VAULT_PATH/.hw1-openclaw-managed && ! -L $VAULT_PATH/.hw1-openclaw-managed ]] ||
      die "managed-vault marker must be a regular non-symlink file"
  fi
  if [[ -d $VAULT_PATH && ! -f $VAULT_PATH/.hw1-openclaw-managed ]]; then
    ((ADOPT_VAULT)) || die "existing vault is unmanaged; re-run with --adopt-vault after reviewing its path and backup"
  fi
  ok "pins: OpenClaw $OPENCLAW_VERSION, Node $NODE_VERSION, llama.cpp $DEFAULT_LLAMA_REF, notes $NOTES_BUNDLE_VERSION"
}

install_packages() {
  phase "packages"
  local -a packages=(ca-certificates curl xz-utils python3 jq git build-essential cmake pkg-config iproute2 util-linux procps)
  ((WITH_HOST_HARDENING)) && packages+=(fail2ban unattended-upgrades)
  ((WITH_FIREWALL)) && packages+=(ufw)
  ((WITH_HOST_HARDENING || WITH_FIREWALL)) && packages+=(openssh-server)
  run apt-get update
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${packages[@]}"
  if ((!DRY_RUN)); then
    local required_command
    for required_command in curl find getent git install jq passwd pgrep pkill python3 runuser sha256sum ss stat systemctl systemd-analyze tar useradd usermod; do
      command -v "$required_command" >/dev/null 2>&1 ||
        die "required command is unavailable after package install: $required_command"
    done
    [[ -x /usr/bin/python3 ]] || die "required isolated Python runtime is missing: /usr/bin/python3"
  fi
}

verify_model_and_server_identity() {
  phase "model and llama.cpp identity"
  if [[ -f $MODEL_PATH ]]; then
    if [[ $MODEL_PATH == "$DEFAULT_MODEL_PATH" ]]; then
      local model_bytes model_sha
      model_bytes="$(stat -c '%s' "$MODEL_PATH")"
      [[ $model_bytes == "$DEFAULT_MODEL_BYTES" ]] ||
        die "default GGUF byte count changed: $model_bytes (expected $DEFAULT_MODEL_BYTES)"
      model_sha="$(sha256sum "$MODEL_PATH" | awk '{print $1}')"
      [[ $model_sha == "$DEFAULT_MODEL_SHA256" ]] || die "default GGUF SHA-256 changed: $model_sha"
      ok "verified pinned default GGUF: $model_sha"
    else
      warn "custom GGUF is not pinned by this installer; benchmark evidence will record its SHA-256"
    fi
  fi

  if [[ -x $SERVER_BIN ]]; then
    if [[ $SERVER_BIN == "$DEFAULT_SERVER_BIN" ]]; then
      local actual_ref
      [[ -d $DEFAULT_LLAMA_ROOT/.git ]] || die "default llama-server has no pinned source checkout at $DEFAULT_LLAMA_ROOT"
      actual_ref="$(git -c safe.directory="$DEFAULT_LLAMA_ROOT" -C "$DEFAULT_LLAMA_ROOT" \
        describe --tags --exact-match 2>/dev/null || true)"
      [[ $actual_ref == "$DEFAULT_LLAMA_REF" ]] ||
        die "default llama.cpp checkout is ${actual_ref:-not at an exact tag}; expected $DEFAULT_LLAMA_REF"
      local actual_commit
      actual_commit="$(git -c safe.directory="$DEFAULT_LLAMA_ROOT" -C "$DEFAULT_LLAMA_ROOT" rev-parse --short=7 HEAD)"
      [[ $actual_commit == "$DEFAULT_LLAMA_COMMIT" ]] ||
        die "default llama.cpp checkout is at commit $actual_commit; expected $DEFAULT_LLAMA_COMMIT"
      git -c safe.directory="$DEFAULT_LLAMA_ROOT" -C "$DEFAULT_LLAMA_ROOT" \
        diff --quiet --ignore-submodules HEAD -- ||
        die "default llama.cpp tracked source differs from tag $DEFAULT_LLAMA_REF"
      ok "verified default llama.cpp source ref: $DEFAULT_LLAMA_REF ($DEFAULT_LLAMA_COMMIT)"
    else
      warn "custom llama-server is not revision-pinned by this installer; benchmark evidence will hash it"
    fi
  fi
}

ensure_account() {
  local name=$1 home=$2 entry actual_name uid gid actual_home actual_shell
  local primary_entry primary_name primary_gid matching_users matching_groups
  if getent passwd "$name" >/dev/null; then
    actual_home="$(getent passwd "$name" | cut -d: -f6)"
    actual_shell="$(getent passwd "$name" | cut -d: -f7)"
    [[ $actual_home == "$home" ]] || die "$name has unexpected home $actual_home"
    [[ $actual_shell == /usr/sbin/nologin || $actual_shell == /sbin/nologin ]] || die "$name has a login shell: $actual_shell"
  else
    run useradd --system --user-group --create-home --home-dir "$home" --shell /usr/sbin/nologin "$name"
  fi
  ((DRY_RUN)) && return 0
  entry="$(getent passwd "$name")"
  [[ -n $entry && $entry != *$'\n'* ]] || die "$name does not resolve to exactly one passwd entry"
  IFS=: read -r actual_name _ uid gid _ actual_home actual_shell <<<"$entry"
  [[ $actual_name == "$name" ]] || die "$name resolves to unexpected account $actual_name"
  [[ $uid =~ ^[0-9]+$ && $uid -gt 0 ]] || die "$name must have a non-root numeric UID"
  [[ $gid =~ ^[0-9]+$ && $gid -gt 0 ]] || die "$name must have a non-root numeric primary GID"
  [[ $actual_home == "$home" ]] || die "$name has unexpected home $actual_home"
  [[ $actual_shell == /usr/sbin/nologin || $actual_shell == /sbin/nologin ]] || die "$name has a login shell: $actual_shell"
  matching_users="$(getent passwd | awk -F: -v uid="$uid" '$3 == uid {print $1}')"
  [[ $matching_users == "$name" ]] || die "$name UID $uid is shared with another account"
  primary_entry="$(getent group "$name")"
  [[ -n $primary_entry && $primary_entry != *$'\n'* ]] || die "$name does not have exactly one same-named primary group"
  IFS=: read -r primary_name _ primary_gid _ <<<"$primary_entry"
  [[ $primary_name == "$name" && $primary_gid == "$gid" ]] ||
    die "$name must use its same-named primary group"
  matching_groups="$(getent group | awk -F: -v gid="$gid" '$3 == gid {print $1}')"
  [[ $matching_groups == "$name" ]] || die "$name primary GID $gid is shared with another group"
  run passwd --lock "$name"
}

verify_group_identity() {
  local group=$1 entry gid matching_groups
  ((DRY_RUN)) && return 0
  entry="$(getent group "$group")"
  [[ -n $entry && $entry != *$'\n'* ]] || die "$group does not resolve to exactly one group entry"
  IFS=: read -r _ _ gid _ <<<"$entry"
  [[ $gid =~ ^[0-9]+$ && $gid -gt 0 ]] || die "$group must have a non-root numeric GID"
  matching_groups="$(getent group | awk -F: -v gid="$gid" '$3 == gid {print $1}')"
  [[ $matching_groups == "$group" ]] || die "$group GID $gid is shared with another group"
}

verify_group_members_exact() {
  local group=$1 allowed=$2 entry gid explicit actual expected member
  ((DRY_RUN)) && return 0
  verify_group_identity "$group"
  entry="$(getent group "$group")"
  IFS=: read -r _ _ gid explicit <<<"$entry"
  actual="$({
    getent passwd | awk -F: -v gid="$gid" '$4 == gid {print $1}'
    [[ -z $explicit ]] || tr ',' '\n' <<<"$explicit"
  } | sed '/^$/d' | sort -u)"
  expected="$(for member in $allowed; do printf '%s\n' "$member"; done | sort -u)"
  [[ $actual == "$expected" ]] ||
    die "$group membership is '$actual'; expected exactly '$expected'"
}

verify_groups() {
  local name=$1 allowed=$2 group
  ((DRY_RUN)) && return 0
  for group in $(id -nG "$name"); do
    case " $allowed " in
      *" $group "*) ;;
      *) die "$name has unexpected supplementary/admin-capable group: $group" ;;
    esac
  done
}

verify_no_sudo_grant() {
  local name=$1
  ((DRY_RUN)) && return 0
  if command -v sudo >/dev/null 2>&1 && sudo -n -l -U "$name" >/dev/null 2>&1; then
    die "$name has a direct sudoers grant"
  fi
}

assert_user_cannot_mutate_artifact() {
  local user=$1 artifact=$2 label=$3 current
  if runuser -u "$user" -- test -w "$artifact"; then
    die "$user can modify $label: $artifact"
  fi
  current="$(dirname -- "$artifact")"
  while [[ $current != / ]]; do
    if runuser -u "$user" -- test -w "$current"; then
      die "$user can replace $label through writable parent: $current"
    fi
    current="$(dirname -- "$current")"
  done
}

verify_model_server_boundary() {
  [[ -f $MODEL_PATH && -x $SERVER_BIN ]] || return 0
  local model_mode server_mode
  model_mode="$(stat -c '%a' "$MODEL_PATH")"
  server_mode="$(stat -c '%a' "$SERVER_BIN")"
  (((8#$model_mode & 0022) == 0)) || die "model must not be group/world-writable: $MODEL_PATH"
  (((8#$server_mode & 06022) == 0)) ||
    die "llama-server must not be set-id or group/world-writable: $SERVER_BIN"
  assert_user_cannot_mutate_artifact "$OPENCLAW_USER" "$MODEL_PATH" model
  assert_user_cannot_mutate_artifact "$OPENCLAW_USER" "$SERVER_BIN" llama-server
  assert_user_cannot_mutate_artifact "$MODEL_USER" "$MODEL_PATH" model
  assert_user_cannot_mutate_artifact "$MODEL_USER" "$SERVER_BIN" llama-server
  assert_user_cannot_mutate_artifact "$BENCH_USER" "$MODEL_PATH" model
  assert_user_cannot_mutate_artifact "$BENCH_USER" "$SERVER_BIN" llama-server
}

create_accounts_and_dirs() {
  phase "accounts and permission boundaries"
  getent group "$NOTES_GROUP" >/dev/null || run groupadd --system "$NOTES_GROUP"
  getent group "$MODEL_ACCESS_GROUP" >/dev/null || run groupadd --system "$MODEL_ACCESS_GROUP"
  verify_group_identity "$NOTES_GROUP"
  verify_group_identity "$MODEL_ACCESS_GROUP"
  ensure_account "$OPENCLAW_USER" "$OPENCLAW_HOME"
  ensure_account "$MODEL_USER" "$MODEL_HOME"
  ensure_account "$BENCH_USER" "$BENCH_HOME"
  ensure_account "$BUILD_USER" "$BUILD_HOME"
  run usermod --append --groups "$NOTES_GROUP" "$OPENCLAW_USER"
  run usermod --append --groups "$MODEL_ACCESS_GROUP" "$OPENCLAW_USER"
  run usermod --append --groups "$MODEL_ACCESS_GROUP" "$MODEL_USER"
  if [[ -n $OPERATOR_USER && $OPERATOR_USER != root ]]; then
    run usermod --append --groups "$NOTES_GROUP" "$OPERATOR_USER"
    note "$OPERATOR_USER must log out/in before new vault-group access is visible"
  fi
  local notes_members=$OPENCLAW_USER
  [[ -z $OPERATOR_USER || $OPERATOR_USER == root ]] || notes_members+=" $OPERATOR_USER"
  verify_group_members_exact "$OPENCLAW_USER" "$OPENCLAW_USER"
  verify_group_members_exact "$MODEL_USER" "$MODEL_USER"
  verify_group_members_exact "$BENCH_USER" "$BENCH_USER"
  verify_group_members_exact "$BUILD_USER" "$BUILD_USER"
  verify_group_members_exact "$NOTES_GROUP" "$notes_members"
  verify_group_members_exact "$MODEL_ACCESS_GROUP" "$OPENCLAW_USER $MODEL_USER"
  verify_groups "$OPENCLAW_USER" "$OPENCLAW_USER $NOTES_GROUP $MODEL_ACCESS_GROUP"
  verify_groups "$MODEL_USER" "$MODEL_USER $MODEL_ACCESS_GROUP"
  verify_groups "$BENCH_USER" "$BENCH_USER"
  verify_groups "$BUILD_USER" "$BUILD_USER"
  verify_no_sudo_grant "$OPENCLAW_USER"
  verify_no_sudo_grant "$MODEL_USER"
  verify_no_sudo_grant "$BENCH_USER"
  verify_no_sudo_grant "$BUILD_USER"

  run install -d -o root -g root -m 0755 "$OPT_ROOT"
  run install -d -o root -g "$OPENCLAW_USER" -m 0750 "$CONFIG_ROOT"
  run install -d -o root -g "$MODEL_ACCESS_GROUP" -m 0750 "$MODEL_CONFIG_ROOT"
  run install -d -o root -g "$BENCH_USER" -m 0750 "$BENCH_CONFIG_ROOT"
  run install -d -o root -g root -m 0700 "$AUDIT_ROOT"
  run install -d -o "$OPENCLAW_USER" -g "$OPENCLAW_USER" -m 0700 \
    "$OPENCLAW_HOME" "$OPENCLAW_HOME/state" "$OPENCLAW_HOME/workspace"
  run install -d -o "$MODEL_USER" -g "$MODEL_USER" -m 0700 \
    "$MODEL_HOME" "$MODEL_HOME/workspace"
  run install -d -o root -g "$BENCH_USER" -m 0710 "$BENCH_HOME"
  run install -d -o "$BENCH_USER" -g "$BENCH_USER" -m 0700 \
    "$BENCH_HOME/state" "$BENCH_HOME/workspace"
  run install -d -o root -g "$BENCH_USER" -m 0710 "$BENCH_HOME/results"
  run install -d -o "$BUILD_USER" -g "$BUILD_USER" -m 0700 "$BUILD_HOME"
  run install -d -o root -g root -m 0755 "$CACHE_ROOT"
  run install -d -o "$BUILD_USER" -g "$BUILD_USER" -m 0700 "$CACHE_ROOT/npm"

  if ((DRY_RUN)); then
    note "would create/verify the managed vault root without recursively changing an existing vault"
  else
    local vault_dir
    install -d -o root -g root -m 0755 /srv/hw1-openclaw-vaults
    if [[ ! -d $VAULT_PATH ]]; then
      install -d -o root -g "$NOTES_GROUP" -m 2770 "$VAULT_PATH"
    elif [[ ! -f $VAULT_PATH/.hw1-openclaw-managed ]]; then
      ((ADOPT_VAULT)) || die "refusing to adopt unmanaged vault without --adopt-vault"
      for vault_dir in reference reference/openclaw projects log archive; do
        [[ ! -e $VAULT_PATH/$vault_dir ]] ||
          [[ -d $VAULT_PATH/$vault_dir && ! -L $VAULT_PATH/$vault_dir &&
             $(stat -c '%G %a' "$VAULT_PATH/$vault_dir") == "$NOTES_GROUP 2770" ]] ||
          die "adoption is non-recursive; prepare existing directory as group $NOTES_GROUP mode 2770 first: $VAULT_PATH/$vault_dir"
      done
      chgrp "$NOTES_GROUP" "$VAULT_PATH"
      chmod 2770 "$VAULT_PATH"
    fi
    [[ ! -L $VAULT_PATH ]] || die "vault root became a symlink"
    [[ $(stat -c '%G %a' "$VAULT_PATH") == "$NOTES_GROUP 2770" ]] ||
      die "managed vault root must be group $NOTES_GROUP mode 2770"

    local marker="$VAULT_PATH/.hw1-openclaw-managed"
    if [[ -e $marker ]]; then
      [[ -f $marker && ! -L $marker ]] || die "invalid managed-vault marker"
      [[ $(stat -c '%U:%G %a' "$marker") == "root:$NOTES_GROUP 640" ]] ||
        die "managed-vault marker ownership/mode changed"
      grep -Fxq 'hw1-openclaw-managed-v1' "$marker" || die "managed-vault marker content changed"
    else
      local marker_tmp
      marker_tmp="$(mktemp)"
      printf 'hw1-openclaw-managed-v1\n' >"$marker_tmp"
      install -o root -g "$NOTES_GROUP" -m 0640 "$marker_tmp" "$marker"
      rm -f -- "$marker_tmp"
    fi

    for vault_dir in reference reference/openclaw projects log archive; do
      if [[ -e $VAULT_PATH/$vault_dir ]]; then
        [[ -d $VAULT_PATH/$vault_dir && ! -L $VAULT_PATH/$vault_dir ]] ||
          die "managed vault child is not a real directory: $VAULT_PATH/$vault_dir"
        [[ $(stat -c '%G %a' "$VAULT_PATH/$vault_dir") == "$NOTES_GROUP 2770" ]] ||
          die "existing vault directory must be group $NOTES_GROUP mode 2770: $VAULT_PATH/$vault_dir"
      else
        install -d -o "$OPENCLAW_USER" -g "$NOTES_GROUP" -m 2770 "$VAULT_PATH/$vault_dir"
      fi
    done
    if [[ -f $MODEL_PATH && -x $SERVER_BIN ]]; then
      runuser -u "$OPENCLAW_USER" -- test -r "$MODEL_PATH" || die "$OPENCLAW_USER cannot read the model"
      runuser -u "$OPENCLAW_USER" -- test -x "$SERVER_BIN" || die "$OPENCLAW_USER cannot execute llama-server"
      runuser -u "$MODEL_USER" -- test -r "$MODEL_PATH" || die "$MODEL_USER cannot read the model"
      runuser -u "$MODEL_USER" -- test -x "$SERVER_BIN" || die "$MODEL_USER cannot execute llama-server"
      runuser -u "$BENCH_USER" -- test -r "$MODEL_PATH" || die "$BENCH_USER cannot read the model"
      runuser -u "$BENCH_USER" -- test -x "$SERVER_BIN" || die "$BENCH_USER cannot execute llama-server"
      verify_model_server_boundary
    elif ((!NO_START)) && ! {
      [[ $MODEL_PATH == "$DEFAULT_MODEL_PATH" && $PROVISION_MODEL -eq 1 ]] ||
        [[ $SERVER_BIN == "$DEFAULT_SERVER_BIN" && $PROVISION_LLAMA -eq 1 ]]
    }; then
      die "model/server disappeared after preflight"
    fi
  fi
  ok "service, benchmark, and package-build accounts are locked and have disjoint write scopes"
}

verify_download_hash() {
  local path=$1 expected=$2 mode=$3 actual
  case "$mode" in
    sha256)
      actual="$(sha256sum -- "$path" | awk '{print $1}')"
      [[ $actual == "$expected" ]] || { warn "SHA-256 mismatch: $actual"; return 1; }
      ;;
    sha512-sri)
      actual="$(/usr/bin/python3 -I - "$path" <<'PY'
import base64
import hashlib
import pathlib
import sys

digest = hashlib.sha512(pathlib.Path(sys.argv[1]).read_bytes()).digest()
print("sha512-" + base64.b64encode(digest).decode("ascii"))
PY
)"
      [[ $actual == "$expected" ]] || { warn "SHA-512 integrity mismatch: $actual"; return 1; }
      ;;
    *) die "unknown download hash mode: $mode" ;;
  esac
}

download_checked() {
  local url=$1 path=$2 expected_bytes=$3 expected_hash=$4 hash_mode=$5 tmp actual_bytes
  [[ ! -L $path ]] || die "download cache entry is a symlink: $path"
  if [[ -f $path ]]; then
    actual_bytes="$(stat -c '%s' -- "$path")"
    if { [[ $expected_bytes == 0 ]] || [[ $actual_bytes == "$expected_bytes" ]]; } &&
      verify_download_hash "$path" "$expected_hash" "$hash_mode"; then
      ok "verified cached $(basename -- "$path")"
      return 0
    fi
    warn "cached artifact failed verification and will be atomically replaced: $path"
  fi
  tmp="$(mktemp "$CACHE_ROOT/.download.XXXXXX")"
  track_temp_file "$tmp"
  curl --disable --fail --location --proto '=https' --tlsv1.2 --retry 4 --retry-all-errors --output "$tmp" "$url"
  actual_bytes="$(stat -c '%s' -- "$tmp")"
  [[ $expected_bytes == 0 || $actual_bytes == "$expected_bytes" ]] || die "byte count mismatch for $url: $actual_bytes"
  verify_download_hash "$tmp" "$expected_hash" "$hash_mode" || die "integrity check failed for $url"
  install -o root -g root -m 0644 "$tmp" "$path"
  rm -f -- "$tmp"
}

verify_model_file() {
  local path=$1 expected_bytes=$2 expected_sha=$3 actual_bytes actual_sha
  [[ -f $path && ! -L $path && -r $path ]] || return 1
  actual_bytes="$(stat -c '%s' -- "$path")"
  [[ $actual_bytes == "$expected_bytes" ]] || return 1
  actual_sha="$(sha256sum -- "$path" | awk '{print $1}')"
  [[ $actual_sha == "$expected_sha" ]]
}

provision_default_model() {
  [[ $MODEL_PATH == "$DEFAULT_MODEL_PATH" ]] || return 0
  if verify_model_file "$MODEL_PATH" "$DEFAULT_MODEL_BYTES" "$DEFAULT_MODEL_SHA256"; then
    ok "verified pinned default GGUF: $MODEL_PATH"
    return 0
  fi
  if [[ -e $MODEL_PATH ]]; then
    die "default GGUF exists but fails its pinned byte-count/SHA-256 check: $MODEL_PATH"
  fi
  ((PROVISION_MODEL)) || { ((NO_START)) && return 0; die "default GGUF is missing and --no-model-provision was set"; }
  if ((DRY_RUN)); then
    note "would download and verify $DEFAULT_MODEL_FILE ($DEFAULT_MODEL_BYTES bytes, SHA-256 $DEFAULT_MODEL_SHA256)"
    return 0
  fi
  phase "pinned default GGUF"
  local model_dir part free need url
  model_dir="$(dirname -- "$MODEL_PATH")"
  install -d -o root -g root -m 0755 "$model_dir"
  part="$MODEL_PATH.part"
  [[ ! -L $part ]] || die "model partial download is a symlink: $part"
  local have=0
  [[ -f $part ]] && have="$(stat -c '%s' -- "$part")"
  free="$(df -Pk "$model_dir" | awk 'NR == 2 {printf "%.0f", $4 * 1024}')"
  need=$((DEFAULT_MODEL_BYTES - have + 268435456))
  ((free >= need)) || die "insufficient free disk for $DEFAULT_MODEL_FILE (need remainder plus 256 MiB margin)"
  url="https://huggingface.co/$DEFAULT_MODEL_REPO/resolve/$DEFAULT_MODEL_REV/$DEFAULT_MODEL_FILE?download=true"
  curl --disable --fail --location --proto '=https' --tlsv1.2 --retry 5 --retry-all-errors -C - -o "$part" "$url"
  verify_model_file "$part" "$DEFAULT_MODEL_BYTES" "$DEFAULT_MODEL_SHA256" ||
    die "downloaded GGUF failed its pinned byte-count/SHA-256 check; retained at $part"
  chown root:root "$part"
  chmod 0644 "$part"
  mv -f -- "$part" "$MODEL_PATH"
  ok "downloaded and verified pinned default GGUF"
}

finalize_llama_tree() {
  local root=$1 build_uid bad
  [[ $root == "$DEFAULT_LLAMA_ROOT" && -d $root && ! -L $root ]] || die "unexpected llama.cpp staging root: $root"
  build_uid="$(id -u "$BUILD_USER")"
  pkill -KILL -u "$build_uid" >/dev/null 2>&1 || true
  ! pgrep -u "$build_uid" >/dev/null 2>&1 || die "$BUILD_USER left a process behind after llama.cpp build"
  chown -hR root:root "$root"
  find "$root" -xdev -type d -exec chmod 0755 {} +
  find "$root" -xdev -type f -exec chmod a+r,go-w,u-s,g-s {} +
  find "$root" -xdev -type f -perm /111 -exec chmod a+x {} +
  bad="$(find "$root" -xdev ! \( -type d -o -type f -o -type l \) -print -quit)"
  [[ -z $bad ]] || die "llama.cpp tree contains a special file: $bad"
}

provision_default_llama() {
  [[ $SERVER_BIN == "$DEFAULT_SERVER_BIN" ]] || return 0
  if [[ -x $SERVER_BIN ]]; then
    return 0
  fi
  ((PROVISION_LLAMA)) || { ((NO_START)) && return 0; die "default llama-server is missing and --no-llama-provision was set"; }
  if ((DRY_RUN)); then
    note "would clone, verify, and build llama.cpp $DEFAULT_LLAMA_REF ($DEFAULT_LLAMA_COMMIT)"
    return 0
  fi
  phase "pinned llama.cpp server"
  local parent="$DEFAULT_LLAMA_ROOT" actual_ref actual_commit
  assert_no_symlink_components "$parent"
  if [[ -e $parent && ! -d $parent ]]; then
    die "llama.cpp path exists but is not a directory: $parent"
  fi
  if [[ -d $parent/.git ]]; then
    actual_ref="$(git -c safe.directory="$parent" -C "$parent" describe --tags --exact-match 2>/dev/null || true)"
    actual_commit="$(git -c safe.directory="$parent" -C "$parent" rev-parse --short=7 HEAD 2>/dev/null || true)"
    [[ $actual_ref == "$DEFAULT_LLAMA_REF" && $actual_commit == "$DEFAULT_LLAMA_COMMIT" ]] ||
      die "existing llama.cpp checkout is ${actual_ref:-unknown}/${actual_commit:-unknown}; expected $DEFAULT_LLAMA_REF/$DEFAULT_LLAMA_COMMIT"
    git -c safe.directory="$parent" -C "$parent" diff --quiet --ignore-submodules HEAD ||
      die "existing llama.cpp checkout has tracked changes; refusing to rebuild it"
  else
    [[ ! -e $parent || -z "$(find "$parent" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]] ||
      die "llama.cpp path is non-empty but not a managed checkout: $parent"
    install -d -o "$BUILD_USER" -g "$BUILD_USER" -m 0755 "$parent"
    runuser -u "$BUILD_USER" -- env -i HOME="$BUILD_HOME" USER="$BUILD_USER" LOGNAME="$BUILD_USER" LC_ALL=C \
      PATH=/usr/bin:/bin git clone --depth 1 --branch "$DEFAULT_LLAMA_REF" --recurse-submodules \
      "$DEFAULT_LLAMA_REPO" "$parent"
    actual_commit="$(git -c safe.directory="$parent" -C "$parent" rev-parse --short=7 HEAD)"
    [[ $actual_commit == "$DEFAULT_LLAMA_COMMIT" ]] || die "cloned llama.cpp commit is $actual_commit; expected $DEFAULT_LLAMA_COMMIT"
  fi
  runuser -u "$BUILD_USER" -- env -i HOME="$BUILD_HOME" USER="$BUILD_USER" LOGNAME="$BUILD_USER" LC_ALL=C \
    PATH=/usr/bin:/bin cmake -S "$parent" -B "$parent/build" -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON -DLLAMA_CURL=OFF
  runuser -u "$BUILD_USER" -- env -i HOME="$BUILD_HOME" USER="$BUILD_USER" LOGNAME="$BUILD_USER" LC_ALL=C \
    PATH=/usr/bin:/bin cmake --build "$parent/build" --target llama-server -j"$(nproc)"
  local server_built="$parent/build/bin/llama-server"
  [[ -x $server_built ]] || die "llama.cpp build finished without producing $server_built"
  "$server_built" --help 2>&1 | grep -q -- '--cache-reuse' || die "$server_built lacks required --cache-reuse support"
  finalize_llama_tree "$parent"
  ok "built llama-server from pinned $DEFAULT_LLAMA_REF ($DEFAULT_LLAMA_COMMIT)"
}

atomic_symlink() {
  local target=$1 link=$2 tmp="${link}.new.$$"
  ln -sfn -- "$target" "$tmp"
  mv -Tf -- "$tmp" "$link"
}

require_openclaw_version() {
  local label=$1
  shift
  local output
  if ! output="$("$@" 2>&1)"; then
    die "$label version command failed: $output"
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
    die "$label returned an unexpected version string: $output"
  fi
}

build_openclaw_cli() {
  runuser -u "$BUILD_USER" -- env -i \
    HOME="$BUILD_HOME" USER="$BUILD_USER" LOGNAME="$BUILD_USER" LC_ALL=C \
    OPENCLAW_STATE_DIR="$BUILD_HOME/state" \
    OPENCLAW_DISABLE_PLUGIN_REGISTRY_MIGRATION=1 \
    PATH="$OPT_ROOT/node/bin:/usr/bin:/bin" \
    "$@"
}

normalize_immutable_tree() {
  local root=$1
  chown -hR root:root "$root"
  find "$root" -xdev -type d -exec chmod 0755 {} +
  find "$root" -xdev -type f -exec chmod a+r,go-w,u-s,g-s {} +
  find "$root" -xdev -type f -perm /111 -exec chmod a+x {} +
}

finalize_hostile_build_tree() {
  local root=$1 marker_value=$2 build_uid bad attempt
  case "$root" in
    /var/tmp/hw1-openclaw-release.*) ;;
    *) die "refusing to finalize unexpected build tree: $root" ;;
  esac
  # npm lifecycle code and the launcher ran as the dedicated build UID. Reap
  # anything it left behind, then reclaim the tree without following
  # attacker-controlled symlinks before any root write inside.
  build_uid="$(id -u "$BUILD_USER")"
  pkill -KILL -u "$build_uid" >/dev/null 2>&1 || true
  for attempt in $(seq 1 10); do
    pgrep -u "$build_uid" >/dev/null 2>&1 || break
    sleep 1
  done
  ! pgrep -u "$build_uid" >/dev/null 2>&1 || die "$BUILD_USER left a process behind after package build"
  [[ -d $root && ! -L $root ]] || die "build output is not a real directory: $root"
  chown -h root:root "$root"
  [[ -d $root && ! -L $root && $(stat -c '%U:%G' "$root") == root:root ]] ||
    die "could not revoke the build UID's staging root"
  chmod 0700 "$root"
  chown -hR root:root "$root"
  bad="$(find "$root" -xdev ! \( -type d -o -type f -o -type l \) -print -quit)"
  [[ -z $bad ]] || die "build output contains a special file: $bad"

  /usr/bin/python3 -I - "$root/.hw1-source-sha256" "$marker_value" <<'PY'
import os
import sys

path, value = sys.argv[1:]
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
fd = os.open(path, flags, 0o444)
try:
    os.write(fd, (value + "\n").encode("ascii"))
    os.fsync(fd)
finally:
    os.close(fd)
PY
  normalize_immutable_tree "$root"
}

verify_immutable_tree_access() {
  local root=$1 bad
  bad="$(find "$root" -xdev -type d ! -perm -0555 -print -quit)"
  [[ -z $bad ]] || die "immutable runtime directory is not traversable/readable: $bad"
  bad="$(find "$root" -xdev -type f ! -perm -0444 -print -quit)"
  [[ -z $bad ]] || die "immutable runtime file is not readable: $bad"
}

verify_root_tree() {
  local root=$1 expected_marker=$2 bad
  [[ -d $root && ! -L $root ]] || die "runtime tree is missing or symlinked: $root"
  [[ -f $root/.hw1-source-sha256 && ! -L $root/.hw1-source-sha256 ]] ||
    die "runtime tree has no source-integrity marker: $root"
  grep -Fxq "$expected_marker" "$root/.hw1-source-sha256" ||
    die "runtime source-integrity marker mismatch: $root"
  bad="$(find "$root" -xdev ! -user root -print -quit)"
  [[ -z $bad ]] || die "runtime tree contains a non-root-owned entry: $bad"
  bad="$(find "$root" -xdev ! -type l \( -perm /022 -o -perm /6000 \) -print -quit)"
  [[ -z $bad ]] || die "runtime tree contains a writable or set-id entry: $bad"
  /usr/bin/python3 -I - "$root" <<'PY'
import os
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve(strict=True)
for current, dirs, files in os.walk(root, followlinks=False):
    for name in dirs + files:
        item = pathlib.Path(current) / name
        if item.is_symlink():
            try:
                item.resolve(strict=True).relative_to(root)
            except (FileNotFoundError, ValueError):
                raise SystemExit(f"runtime symlink escapes or is broken: {item}")
PY
}

install_runtime() {
  phase "pinned runtime"
  if ((DRY_RUN)); then
    note "would download and verify Node $NODE_VERSION (sha256 $NODE_SHA256)"
    note "would download and verify OpenClaw $OPENCLAW_VERSION ($OPENCLAW_INTEGRITY)"
    return 0
  fi
  local node_archive="$CACHE_ROOT/node-v${NODE_VERSION}-linux-arm64.tar.xz"
  local claw_archive="$CACHE_ROOT/openclaw-${OPENCLAW_VERSION}.tgz"
  download_checked "$NODE_URL" "$node_archive" "$NODE_BYTES" "$NODE_SHA256" sha256
  download_checked "$OPENCLAW_URL" "$claw_archive" "$OPENCLAW_BYTES" "$OPENCLAW_INTEGRITY" sha512-sri

  local node_root="$OPT_ROOT/node-v${NODE_VERSION}"
  if [[ -d $node_root ]]; then
    verify_root_tree "$node_root" "$NODE_SHA256"
    normalize_immutable_tree "$node_root"
    verify_immutable_tree_access "$node_root"
    [[ $($node_root/bin/node --version) == v$NODE_VERSION ]] || die "existing Node directory has wrong version: $node_root"
  else
    local node_stage
    node_stage="$(mktemp -d /var/tmp/hw1-openclaw-node.XXXXXX)"
    track_temp_dir "$node_stage"
    tar --no-same-owner -xJf "$node_archive" -C "$node_stage" --strip-components=1
    [[ $($node_stage/bin/node --version) == v$NODE_VERSION ]] || die "extracted Node version mismatch"
    printf '%s\n' "$NODE_SHA256" >"$node_stage/.hw1-source-sha256"
    normalize_immutable_tree "$node_stage"
    mv -- "$node_stage" "$node_root"
    verify_root_tree "$node_root" "$NODE_SHA256"
    verify_immutable_tree_access "$node_root"
  fi
  atomic_symlink "node-v${NODE_VERSION}" "$OPT_ROOT/node"

  local release_root="$OPT_ROOT/releases/$OPENCLAW_VERSION"
  install -d -o root -g root -m 0755 "$OPT_ROOT/releases"
  if [[ -d $release_root ]]; then
    verify_root_tree "$release_root" "$OPENCLAW_INTEGRITY"
    normalize_immutable_tree "$release_root"
    verify_immutable_tree_access "$release_root"
    require_openclaw_version "existing OpenClaw release" \
      build_openclaw_cli "$node_root/bin/node" "$release_root/openclaw.mjs" --version
  else
    local claw_stage build_home
    claw_stage="$(mktemp -d /var/tmp/hw1-openclaw-release.XXXXXX)"
    track_temp_dir "$claw_stage"
    build_home="$(mktemp -d /var/tmp/hw1-openclaw-build-home.XXXXXX)"
    track_temp_dir "$build_home"
    tar --no-same-owner -xzf "$claw_archive" -C "$claw_stage" --strip-components=1
    [[ $(/usr/bin/python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$claw_stage/package.json") == "$OPENCLAW_VERSION" ]] ||
      die "OpenClaw package.json version mismatch"
    [[ -f $claw_stage/npm-shrinkwrap.json ]] || die "packaged OpenClaw release has no npm-shrinkwrap.json"
    chown -R "$BUILD_USER:$BUILD_USER" "$claw_stage" "$build_home"
    runuser -u "$BUILD_USER" -- env -i \
      HOME="$build_home" USER="$BUILD_USER" LOGNAME="$BUILD_USER" LC_ALL=C CI=1 \
      OPENCLAW_STATE_DIR="$build_home/state" \
      OPENCLAW_DISABLE_PLUGIN_REGISTRY_MIGRATION=1 \
      PATH="$node_root/bin:/usr/bin:/bin" npm_config_cache="$CACHE_ROOT/npm" \
      "$node_root/bin/node" "$node_root/lib/node_modules/npm/bin/npm-cli.js" \
      ci --omit=dev --no-audit --no-fund --prefix "$claw_stage"
    require_openclaw_version "installed OpenClaw CLI" \
      build_openclaw_cli "$node_root/bin/node" "$claw_stage/openclaw.mjs" --version
    finalize_hostile_build_tree "$claw_stage" "$OPENCLAW_INTEGRITY"
    mv -- "$claw_stage" "$release_root"
    rm -rf -- "$build_home"
    verify_root_tree "$release_root" "$OPENCLAW_INTEGRITY"
    verify_immutable_tree_access "$release_root"
  fi
  atomic_symlink "releases/$OPENCLAW_VERSION" "$OPT_ROOT/current"
  runuser -u "$OPENCLAW_USER" -- test -x "$OPT_ROOT/node/bin/node" ||
    die "$OPENCLAW_USER cannot execute the pinned Node runtime"
  runuser -u "$OPENCLAW_USER" -- test -r "$OPT_ROOT/current/openclaw.mjs" ||
    die "$OPENCLAW_USER cannot read the pinned OpenClaw launcher"
  runuser -u "$BENCH_USER" -- test -x "$OPT_ROOT/node/bin/node" ||
    die "$BENCH_USER cannot execute the pinned Node runtime"
  runuser -u "$BENCH_USER" -- test -r "$OPT_ROOT/current/openclaw.mjs" ||
    die "$BENCH_USER cannot read the pinned OpenClaw launcher"
  ok "root-owned OpenClaw runtime installed; npm lifecycle never ran as root"
}

install_notes_bundle() {
  phase "pinned Obsidian notes plugin and skill"
  local source="$SCRIPT_DIR/vendor/agent-notes" source_manifest_sha
  if ((DRY_RUN)); then
    note "would verify $source/SHA256SUMS and install root-owned notes bundle $NOTES_BUNDLE_VERSION"
    return 0
  fi
  (cd -- "$source" && sha256sum --check SHA256SUMS)
  source_manifest_sha="$(sha256sum "$source/SHA256SUMS" | awk '{print $1}')"
  local final="$OPT_ROOT/agent-notes-$NOTES_BUNDLE_VERSION"
  if [[ -d $final ]]; then
    [[ ! -L $final ]] || die "existing notes bundle is a symlink"
    [[ -f $final/.hw1-source-manifest-sha256 ]] || die "existing notes bundle has no digest marker"
    grep -Fxq "$source_manifest_sha" "$final/.hw1-source-manifest-sha256" ||
      die "existing notes bundle digest differs from the vendored source"
    (cd -- "$final" && sha256sum --check BUNDLE.SHA256)
    local bad
    bad="$(find "$final" -xdev ! -user root -print -quit)"
    [[ -z $bad ]] || die "notes bundle contains a non-root-owned entry: $bad"
    bad="$(find "$final" -xdev ! -type l -perm /022 -print -quit)"
    [[ -z $bad ]] || die "notes bundle contains a group/world-writable entry: $bad"
    normalize_immutable_tree "$final"
    verify_immutable_tree_access "$final"
  else
    local stage
    stage="$(mktemp -d /var/tmp/hw1-agent-notes.XXXXXX)"
    track_temp_dir "$stage"
    install -d -o root -g root -m 0755 "$stage/plugin" "$stage/skills/notes"
    install -o root -g root -m 0644 "$source/plugin/index.js" "$source/plugin/openclaw.plugin.json" \
      "$source/plugin/package.json" "$source/plugin/README.md" "$stage/plugin/"
    install -o root -g root -m 0644 "$source/SKILL.md" "$stage/skills/notes/SKILL.md"
    install -o root -g root -m 0644 "$source/LICENSE" "$source/SOURCE.md" "$source/SHA256SUMS" "$stage/"
    printf '%s\n' "$source_manifest_sha" >"$stage/.hw1-source-manifest-sha256"
    chmod 0644 "$stage/.hw1-source-manifest-sha256"
    (
      cd -- "$stage"
      sha256sum -- \
        plugin/index.js plugin/openclaw.plugin.json plugin/package.json plugin/README.md \
        skills/notes/SKILL.md LICENSE SOURCE.md SHA256SUMS .hw1-source-manifest-sha256 \
        >BUNDLE.SHA256
    )
    chmod 0644 "$stage/BUNDLE.SHA256"
    normalize_immutable_tree "$stage"
    mv -- "$stage" "$final"
    (cd -- "$final" && sha256sum --check BUNDLE.SHA256)
    verify_immutable_tree_access "$final"
  fi
  atomic_symlink "agent-notes-$NOTES_BUNDLE_VERSION" "$OPT_ROOT/agent-notes"
  runuser -u "$OPENCLAW_USER" -- test -r "$OPT_ROOT/agent-notes/plugin/index.js" ||
    die "$OPENCLAW_USER cannot read the pinned notes plugin"
  runuser -u "$OPENCLAW_USER" -- test -r "$OPT_ROOT/agent-notes/skills/notes/SKILL.md" ||
    die "$OPENCLAW_USER cannot read the pinned notes skill"
  runuser -u "$BENCH_USER" -- test -r "$OPT_ROOT/agent-notes/plugin/index.js" ||
    die "$BENCH_USER cannot read the pinned notes plugin"
  runuser -u "$BENCH_USER" -- test -r "$OPT_ROOT/agent-notes/skills/notes/SKILL.md" ||
    die "$BENCH_USER cannot read the pinned notes skill"
  ok "agent sees note_* tools; only trusted root-owned plugin code sees the vault path"
}

render_json() {
  local template=$1 output=$2 llama_port=$3 workspace_kind=$4
  /usr/bin/python3 -I - "$template" "$output" "$SERVER_BIN" "$MODEL_PATH" "$llama_port" "$CONTEXT_TOKENS" "$GATEWAY_PORT" <<'PY'
import json
import pathlib
import sys

template, output, server, model, llama_port, context, gateway_port = sys.argv[1:]
text = pathlib.Path(template).read_text(encoding="utf-8")
replacements = {
    "@@LLAMA_SERVER@@": json.dumps(server)[1:-1],
    "@@LLAMA_MODEL@@": json.dumps(model)[1:-1],
    "@@LLAMA_PORT@@": str(int(llama_port)),
    "@@CONTEXT_TOKENS@@": str(int(context)),
    "@@GATEWAY_PORT@@": str(int(gateway_port)),
}
for key, value in replacements.items():
    text = text.replace(key, value)
if "@@" in text:
    raise SystemExit("unresolved template placeholder")
json.loads(text)
pathlib.Path(output).write_text(text + ("" if text.endswith("\n") else "\n"), encoding="utf-8")
PY
}

render_text_file() {
  local source=$1 output=$2
  /usr/bin/python3 -I - "$source" "$output" "$VAULT_PATH" "$GATEWAY_PORT" "$SERVER_BIN" \
    "$MODEL_PATH" "$LLAMA_PORT" "$CONTEXT_TOKENS" "$OPENCLAW_USER" <<'PY'
import pathlib
import sys

source, output, vault, gateway_port, server, model, llama_port, context, agent_user = sys.argv[1:]
if any(any(c in value for c in "\r\n") for value in (vault, server, model)):
    raise SystemExit("unsafe text-template path")
text = pathlib.Path(source).read_text(encoding="utf-8")
replacements = {
    "@@VAULT_PATH@@": vault,
    "@@GATEWAY_PORT@@": str(int(gateway_port)),
    "@@LLAMA_SERVER@@": server,
    "@@LLAMA_MODEL@@": model,
    "@@LLAMA_PORT@@": str(int(llama_port)),
    "@@CONTEXT_TOKENS@@": str(int(context)),
    "@@OPENCLAW_USER@@": agent_user,
}
for marker, value in replacements.items():
    text = text.replace(marker, value)
if "@@" in text:
    raise SystemExit("unresolved text-template placeholder")
pathlib.Path(output).write_text(text, encoding="utf-8")
PY
}

install_managed_file() {
  local source=$1 destination=$2 owner=$3 group=$4 mode=$5
  local marker="${destination}.hw1-managed-sha256" new_sha current_sha= expected_sha=
  local destination_tmp marker_tmp
  new_sha="$(sha256sum "$source" | awk '{print $1}')"
  if [[ -e $destination || -L $destination ]]; then
    [[ -f $destination && ! -L $destination ]] || die "managed destination is not a regular file: $destination"
    current_sha="$(sha256sum "$destination" | awk '{print $1}')"
    if [[ -e $marker || -L $marker ]]; then
      [[ -f $marker && ! -L $marker ]] || die "managed-file digest marker is not regular: $marker"
      [[ $(stat -c '%U:%G %a' "$marker") == 'root:root 644' ]] ||
        die "managed-file digest marker ownership/mode changed: $marker"
      expected_sha="$(tr -d '[:space:]' <"$marker")"
      [[ $expected_sha =~ ^[0-9a-f]{64}$ ]] || die "managed-file digest marker is malformed: $marker"
      if [[ $current_sha != "$expected_sha" && $current_sha != "$new_sha" ]]; then
        die "refusing to overwrite locally modified managed file: $destination"
      fi
      [[ $current_sha == "$expected_sha" ]] ||
        warn "recovering interrupted managed-file marker update: $destination"
    elif [[ $current_sha == "$new_sha" ]]; then
      warn "recovering interrupted first managed-file install: $destination"
    else
      die "refusing to overwrite untracked file: $destination (save/reconcile it first)"
    fi
  elif [[ -e $marker || -L $marker ]]; then
    [[ -f $marker && ! -L $marker ]] || die "orphan managed-file marker is not regular: $marker"
    [[ $(stat -c '%U:%G %a' "$marker") == 'root:root 644' ]] ||
      die "orphan managed-file marker ownership/mode changed: $marker"
    expected_sha="$(tr -d '[:space:]' <"$marker")"
    [[ $expected_sha =~ ^[0-9a-f]{64}$ ]] || die "orphan managed-file marker is malformed: $marker"
    warn "recovering managed destination missing after an interrupted install: $destination"
  fi
  destination_tmp="$(mktemp "$(dirname -- "$destination")/.$(basename -- "$destination").XXXXXX")"
  install -o "$owner" -g "$group" -m "$mode" "$source" "$destination_tmp"
  mv -Tf -- "$destination_tmp" "$destination"
  marker_tmp="$(mktemp "$(dirname -- "$marker")/.$(basename -- "$marker").XXXXXX")"
  printf '%s\n' "$new_sha" >"$marker_tmp"
  chown root:root "$marker_tmp"
  chmod 0644 "$marker_tmp"
  mv -Tf -- "$marker_tmp" "$marker"
}

install_config_and_unit() {
  phase "config, secret and systemd confinement"
  if ((DRY_RUN)); then
    note "would render strict JSON configs, preserve the gateway token, and install the hardened system unit"
    return 0
  fi
  local tmp_prod tmp_bench tmp_unit tmp_model_unit tmp_wrapper tmp_benchmark_wrapper
  tmp_prod="$(mktemp)"; tmp_bench="$(mktemp)"; tmp_unit="$(mktemp)"; tmp_model_unit="$(mktemp)"; tmp_wrapper="$(mktemp)"
  tmp_benchmark_wrapper="$(mktemp)"
  render_json "$SCRIPT_DIR/openclaw.json.in" "$tmp_prod" "$LLAMA_PORT" production
  render_json "$SCRIPT_DIR/benchmark.json.in" "$tmp_bench" "$BENCH_LLAMA_PORT" benchmark
  render_text_file "$SCRIPT_DIR/hw1-openclaw.service" "$tmp_unit"
  render_text_file "$SCRIPT_DIR/hw1-openclaw-model.service" "$tmp_model_unit"
  render_text_file "$SCRIPT_DIR/hw1-openclaw" "$tmp_wrapper"
  render_text_file "$SCRIPT_DIR/../tools/openclaw/benchmark_openclaw.sh" "$tmp_benchmark_wrapper"
  install_managed_file "$tmp_prod" "$CONFIG_ROOT/openclaw.json" root "$OPENCLAW_USER" 0640
  install_managed_file "$tmp_bench" "$BENCH_CONFIG_ROOT/openclaw.json" root "$BENCH_USER" 0640
  install_managed_file "$tmp_unit" /etc/systemd/system/hw1-openclaw.service root root 0644
  install_managed_file "$tmp_model_unit" /etc/systemd/system/hw1-openclaw-model.service root root 0644
  install_managed_file "$tmp_wrapper" /usr/local/sbin/hw1-openclaw root root 0755
  rm -f -- "$tmp_prod" "$tmp_bench" "$tmp_unit" "$tmp_model_unit" "$tmp_wrapper"

  install -d -o root -g root -m 0755 "$BENCH_ASSET_ROOT"
  install_managed_file "$SCRIPT_DIR/wait_model_ready.sh" \
    "$BENCH_ASSET_ROOT/wait-model-ready" root root 0755
  install_managed_file "$SCRIPT_DIR/../tools/openclaw/openclaw_memory_probe.py" \
    "$BENCH_ASSET_ROOT/openclaw_memory_probe.py" root root 0755
  install_managed_file "$SCRIPT_DIR/../tools/openclaw/openclaw_memory_cases.json" \
    "$BENCH_ASSET_ROOT/openclaw_memory_cases.json" root root 0644
  install_managed_file "$tmp_benchmark_wrapper" \
    /usr/local/sbin/hw1-openclaw-benchmark root root 0755
  rm -f -- "$tmp_benchmark_wrapper"

  if [[ ! -e $CONFIG_ROOT/secrets.env && ! -L $CONFIG_ROOT/secrets.env ]]; then
    local secret_tmp token
    secret_tmp="$(mktemp)"
    token="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
    [[ $token =~ ^[0-9a-f]{64}$ ]] || die "could not generate gateway token"
    printf 'OPENCLAW_GATEWAY_TOKEN=%s\n' "$token" >"$secret_tmp"
    install -o root -g "$OPENCLAW_USER" -m 0640 "$secret_tmp" "$CONFIG_ROOT/secrets.env"
    rm -f -- "$secret_tmp"
  else
    [[ -f $CONFIG_ROOT/secrets.env && ! -L $CONFIG_ROOT/secrets.env ]] ||
      die "existing secrets.env is not a regular non-symlink file"
    [[ $(stat -c '%U:%G %a' "$CONFIG_ROOT/secrets.env") == "root:$OPENCLAW_USER 640" ]] ||
      die "existing secrets.env must be root:$OPENCLAW_USER mode 0640"
    if ! /usr/bin/python3 -I - "$CONFIG_ROOT/secrets.env" <<'PY'
import pathlib
import re
import sys

text = pathlib.Path(sys.argv[1]).read_text(encoding="ascii")
if re.fullmatch(r"OPENCLAW_GATEWAY_TOKEN=[0-9a-f]{64}\n", text) is None:
    raise SystemExit(1)
PY
    then
      die "existing gateway token file has an unexpected format"
    fi
  fi

  if [[ ! -e $MODEL_CONFIG_ROOT/model.env && ! -L $MODEL_CONFIG_ROOT/model.env ]]; then
    local model_secret_tmp model_token
    model_secret_tmp="$(mktemp)"
    model_token="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
    [[ $model_token =~ ^[0-9a-f]{64}$ ]] || die "could not generate model API token"
    printf 'HW1_MODEL_API_KEY=%s\nLLAMA_API_KEY=%s\n' "$model_token" "$model_token" >"$model_secret_tmp"
    install -o root -g "$MODEL_ACCESS_GROUP" -m 0640 \
      "$model_secret_tmp" "$MODEL_CONFIG_ROOT/model.env"
    rm -f -- "$model_secret_tmp"
  else
    [[ -f $MODEL_CONFIG_ROOT/model.env && ! -L $MODEL_CONFIG_ROOT/model.env ]] ||
      die "existing model.env is not a regular non-symlink file"
    [[ $(stat -c '%U:%G %a' "$MODEL_CONFIG_ROOT/model.env") == "root:$MODEL_ACCESS_GROUP 640" ]] ||
      die "existing model.env must be root:$MODEL_ACCESS_GROUP mode 0640"
    if ! /usr/bin/python3 -I - "$MODEL_CONFIG_ROOT/model.env" <<'PY'
import pathlib
import re
import sys

text = pathlib.Path(sys.argv[1]).read_text(encoding="ascii")
match = re.fullmatch(
    r"HW1_MODEL_API_KEY=([0-9a-f]{64})\nLLAMA_API_KEY=([0-9a-f]{64})\n",
    text,
)
if match is None or match.group(1) != match.group(2):
    raise SystemExit(1)
PY
    then
      die "existing model API token file has an unexpected format"
    fi
  fi

  local dropin=/etc/systemd/system/hw1-openclaw.service.d/20-outbound-network.conf
  if ((ALLOW_NETWORK)); then
    install -d -o root -g root -m 0755 "$(dirname -- "$dropin")"
    local net_tmp
    net_tmp="$(mktemp)"
    printf '[Service]\nIPAddressDeny=\nIPAddressAllow=\n' >"$net_tmp"
    install_managed_file "$net_tmp" "$dropin" root root 0644
    rm -f -- "$net_tmp"
    warn "gateway outbound networking enabled; note text remains untrusted input"
  elif [[ -e $dropin || -L $dropin ]]; then
    local dropin_marker="${dropin}.hw1-managed-sha256" dropin_expected dropin_actual
    [[ -f $dropin && ! -L $dropin && -f $dropin_marker && ! -L $dropin_marker ]] ||
      die "refusing to remove an untracked outbound-network drop-in"
    [[ $(stat -c '%U:%G %a' "$dropin_marker") == 'root:root 644' ]] ||
      die "outbound-network drop-in marker ownership/mode changed"
    dropin_expected="$(tr -d '[:space:]' <"$dropin_marker")"
    dropin_actual="$(sha256sum "$dropin" | awk '{print $1}')"
    [[ $dropin_expected =~ ^[0-9a-f]{64}$ && $dropin_expected == "$dropin_actual" ]] ||
      die "refusing to remove a locally modified outbound-network drop-in"
    rm -f -- "$dropin" "$dropin_marker"
  fi
  systemctl daemon-reload
}

prod_cli() {
  runuser -u "$OPENCLAW_USER" -- env -i \
    HOME="$OPENCLAW_HOME" USER="$OPENCLAW_USER" LOGNAME="$OPENCLAW_USER" LC_ALL=C \
    PATH="$OPT_ROOT/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    OPENCLAW_STATE_DIR="$OPENCLAW_HOME/state" \
    OPENCLAW_CONFIG_PATH="$CONFIG_ROOT/openclaw.json" \
    OPENCLAW_SERVICE_REPAIR_POLICY=external \
    OPENCLAW_NO_RESPAWN=1 OPENCLAW_DISABLE_BONJOUR=1 \
    OPENCLAW_DISABLE_PLUGIN_REGISTRY_MIGRATION=1 \
    AGENT_NOTES_VAULT="$VAULT_PATH" AGENT_NOTES_MAX_VAULT_BYTES=536870912 \
    AGENT_NOTES_DESTRUCTIVE_APPROVALS=1 AGENT_NOTES_SHARED_GROUP=1 \
    bash -c 'umask 0007; cd -- /var/lib/hw1-openclaw/workspace; set -a; source /etc/hw1-openclaw/secrets.env; source /etc/hw1-openclaw-model/model.env; set +a; exec /opt/hw1-openclaw/node/bin/node /opt/hw1-openclaw/current/openclaw.mjs "$@"' \
    bash "$@"
}

bench_cli() {
  runuser -u "$BENCH_USER" -- env -i \
    HOME="$BENCH_HOME" USER="$BENCH_USER" LOGNAME="$BENCH_USER" LC_ALL=C \
    PATH="$OPT_ROOT/node/bin:/usr/bin:/bin" \
    OPENCLAW_STATE_DIR="$BENCH_HOME/state" OPENCLAW_CONFIG_PATH="$BENCH_CONFIG_ROOT/openclaw.json" \
    AGENT_NOTES_VAULT="$BENCH_HOME/vault-preflight" AGENT_NOTES_MAX_VAULT_BYTES=67108864 \
    AGENT_NOTES_DESTRUCTIVE_APPROVALS=1 AGENT_NOTES_SHARED_GROUP=0 \
    OPENCLAW_DISABLE_PLUGIN_REGISTRY_MIGRATION=1 \
    bash -c 'umask 0077; cd -- /var/lib/hw1-openclaw-bench/workspace; exec /opt/hw1-openclaw/node/bin/node /opt/hw1-openclaw/current/openclaw.mjs "$@"' \
    bash "$@"
}

record_verification() {
  local source=$1 name=$2 destination staged
  [[ $name =~ ^[A-Za-z0-9._-]+$ ]] || die "unsafe verification evidence name: $name"
  destination="$AUDIT_ROOT/$name"
  [[ (! -e $destination && ! -L $destination) || (-f $destination && ! -L $destination) ]] ||
    die "verification evidence target is not a regular non-symlink file: $destination"
  staged="$(mktemp "$AUDIT_ROOT/.${name}.XXXXXX")"
  install -o root -g root -m 0600 "$source" "$staged"
  mv -Tf -- "$staged" "$destination"
}

verify_plugin_report() {
  local report=$1
  /usr/bin/python3 -I - "$report" <<'PY'
import json
import pathlib
import sys

expected_tools = {
    "note_append",
    "note_archive",
    "note_backlinks",
    "note_folders",
    "note_list",
    "note_move",
    "note_read",
    "note_search",
    "note_tag",
    "note_write",
}
value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
plugin = value.get("plugin")
if not isinstance(plugin, dict):
    raise SystemExit("plugin inspect report has no plugin object")
if plugin.get("id") != "agent-notes" or plugin.get("status") != "loaded":
    raise SystemExit(f"agent-notes is not loaded: {plugin.get('id')!r} {plugin.get('status')!r}")
tool_groups = value.get("tools")
if not isinstance(tool_groups, list):
    raise SystemExit("plugin inspect report has no tool inventory")
tools = [
    name
    for group in tool_groups
    if isinstance(group, dict)
    for name in group.get("names", [])
    if isinstance(name, str)
]
if len(tools) != len(expected_tools) or set(tools) != expected_tools:
    raise SystemExit(f"unexpected agent-notes tool inventory: {sorted(tools)!r}")
typed_hooks = value.get("typedHooks")
if not isinstance(typed_hooks, list) or "before_tool_call" not in {
    item.get("name") for item in typed_hooks if isinstance(item, dict)
}:
    raise SystemExit("agent-notes before_tool_call approval hook is not loaded")
policy = value.get("policy")
if not isinstance(policy, dict) or policy.get("allowPromptInjection") is not False:
    raise SystemExit("agent-notes prompt-injection hook policy is not fail-closed")
if policy.get("allowConversationAccess") is not False:
    raise SystemExit("agent-notes conversation-access hook policy is not fail-closed")
diagnostics = value.get("diagnostics")
if not isinstance(diagnostics, list) or any(
    isinstance(item, dict) and item.get("level") == "error" for item in diagnostics
):
    raise SystemExit("agent-notes has an error diagnostic")
compatibility = value.get("compatibility")
if not isinstance(compatibility, list) or any(
    isinstance(item, dict) and item.get("severity") == "warn" for item in compatibility
):
    raise SystemExit("agent-notes has a runtime compatibility warning")
PY
}

verify_skill_report() {
  local report=$1 expected_file="$OPT_ROOT/agent-notes-$NOTES_BUNDLE_VERSION/skills/notes/SKILL.md"
  /usr/bin/python3 -I - "$report" "$expected_file" <<'PY'
import json
import pathlib
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
expected_file = pathlib.Path(sys.argv[2]).resolve(strict=True)
required = {
    "name": "notes",
    "eligible": True,
    "modelVisible": True,
    "disabled": False,
    "blockedByAllowlist": False,
    "blockedByAgentFilter": False,
}
for key, expected in required.items():
    if value.get(key) != expected:
        raise SystemExit(f"notes skill has unexpected {key}: {value.get(key)!r}")
file_path = value.get("filePath")
if not isinstance(file_path, str) or pathlib.Path(file_path).resolve(strict=True) != expected_file:
    raise SystemExit(f"notes skill did not resolve to pinned bundle: {file_path!r}")
PY
}

verify_security_report() {
  local report=$1 require_live_gateway=$2
  /usr/bin/python3 -I - "$report" "$require_live_gateway" <<'PY'
import json
import pathlib
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
summary = value.get("summary")
if not isinstance(summary, dict) or type(summary.get("critical")) is not int:
    raise SystemExit("security audit has no numeric critical count")
if summary["critical"] != 0:
    raise SystemExit(f"security audit reported {summary['critical']} critical finding(s)")
if sys.argv[2] == "1":
    deep = value.get("deep")
    gateway = deep.get("gateway") if isinstance(deep, dict) else None
    if not isinstance(gateway, dict) or gateway.get("attempted") is not True or gateway.get("ok") is not True:
        raise SystemExit("deep security audit did not prove a live Gateway RPC connection")
PY
}

capture_security_audit() {
  local kind=$1 report
  report="$(mktemp)"
  if [[ $kind == live ]]; then
    if ! prod_cli security audit --deep --json >"$report"; then
      record_verification "$report" live-security-audit.json
      rm -f -- "$report"
      die "OpenClaw live deep security-audit command failed; inspect $AUDIT_ROOT/live-security-audit.json"
    fi
    if ! verify_security_report "$report" 1; then
      record_verification "$report" live-security-audit.json
      rm -f -- "$report"
      die "OpenClaw live deep security audit did not pass; inspect $AUDIT_ROOT/live-security-audit.json"
    fi
    record_verification "$report" live-security-audit.json
  else
    if ! prod_cli security audit --json >"$report"; then
      record_verification "$report" cold-security-audit.json
      rm -f -- "$report"
      die "OpenClaw cold security-audit command failed; inspect $AUDIT_ROOT/cold-security-audit.json"
    fi
    if ! verify_security_report "$report" 0; then
      record_verification "$report" cold-security-audit.json
      rm -f -- "$report"
      die "OpenClaw cold security audit did not pass; inspect $AUDIT_ROOT/cold-security-audit.json"
    fi
    record_verification "$report" cold-security-audit.json
  fi
  rm -f -- "$report"
}

validate_install() {
  phase "fail-closed validation"
  ((DRY_RUN)) && { note "would validate both configs, plugin/skill inventory, unit hardening and security audit"; return 0; }
  require_openclaw_version "production CLI" prod_cli --version
  require_openclaw_version "benchmark CLI" bench_cli --version
  prod_cli config validate --json >/dev/null
  bench_cli config validate --json >/dev/null
  local plugin_report skill_report
  plugin_report="$(mktemp)"
  skill_report="$(mktemp)"
  if ! prod_cli plugins inspect agent-notes --runtime --json >"$plugin_report" ||
    ! verify_plugin_report "$plugin_report"; then
    record_verification "$plugin_report" plugin-inspect.json
    rm -f -- "$plugin_report" "$skill_report"
    die "agent-notes runtime contract failed; inspect $AUDIT_ROOT/plugin-inspect.json"
  fi
  record_verification "$plugin_report" plugin-inspect.json
  prod_cli plugins doctor >/dev/null
  prod_cli skills check >/dev/null
  if ! prod_cli skills info notes --json --agent main >"$skill_report" ||
    ! verify_skill_report "$skill_report"; then
    record_verification "$skill_report" skill-info.json
    rm -f -- "$plugin_report" "$skill_report"
    die "notes skill runtime contract failed; inspect $AUDIT_ROOT/skill-info.json"
  fi
  record_verification "$skill_report" skill-info.json
  rm -f -- "$plugin_report" "$skill_report"
  if ((NO_START)) && { [[ ! -x $SERVER_BIN ]] || [[ ! -f $MODEL_PATH ]]; }; then
    warn "skipping systemd path verification because --no-start model/server is not installed"
  else
    systemd-analyze verify \
      /etc/systemd/system/hw1-openclaw-model.service \
      /etc/systemd/system/hw1-openclaw.service
  fi
  capture_security_audit cold
  ok "config, exact plugin/skill contracts, unit and cold security-audit gates passed"
}

configure_host_hardening() {
  ((WITH_HOST_HARDENING || WITH_FIREWALL)) || return 0
  phase "optional host hardening"
  local ssh_port ports_csv jail_tmp
  local -a ssh_ports=()
  command -v sshd >/dev/null 2>&1 || die "sshd is required to resolve the SSH ports safely"
  mapfile -t ssh_ports < <(sshd -T 2>/dev/null | awk '$1 == "port" {print $2}' | sort -nu)
  ((${#ssh_ports[@]} > 0)) || die "could not resolve effective SSH ports"
  for ssh_port in "${ssh_ports[@]}"; do
    [[ $ssh_port =~ ^[1-9][0-9]{0,4}$ ]] && ((10#$ssh_port <= 65535)) ||
      die "invalid effective SSH port: $ssh_port"
  done
  ports_csv="$(IFS=,; printf '%s' "${ssh_ports[*]}")"

  if ((WITH_HOST_HARDENING)); then
    if ((DRY_RUN)); then
      note "would enable unattended upgrades and a fail2ban sshd jail on port(s) $ports_csv"
    else
    jail_tmp="$(mktemp)"
    printf '[sshd]\nenabled = true\nbackend = systemd\nport = %s\nmaxretry = 5\nfindtime = 10m\nbantime = 1h\n' \
      "$ports_csv" >"$jail_tmp"
    install_managed_file "$jail_tmp" /etc/fail2ban/jail.d/hw1-openclaw-sshd.local root root 0644
    rm -f -- "$jail_tmp"
    systemctl enable --now fail2ban.service
    dpkg-reconfigure -f noninteractive unattended-upgrades
    fi
  fi

  ((WITH_FIREWALL)) || return 0
  for ssh_port in "${ssh_ports[@]}"; do
    run ufw allow "$ssh_port/tcp" comment 'SSH allowed before HW1 firewall enable'
  done
  if ((DRY_RUN)) || ! ufw status 2>/dev/null | grep -Fq 'Status: active'; then
    run ufw default deny incoming
    run ufw default allow outgoing
    run ufw --force enable
    ok "UFW enabled; OpenClaw has no inbound rule because it is loopback-only"
  else
    ok "UFW was already active; existing defaults were preserved and SSH ports were allowed"
  fi
}

start_and_check() {
  if ((NO_START)); then
    ((DRY_RUN)) && { note "would leave hw1-openclaw.service stopped and disabled"; return 0; }
    systemctl disable --now hw1-openclaw.service >/dev/null 2>&1 || true
    systemctl stop hw1-openclaw-model.service >/dev/null 2>&1 || true
    ! systemctl is-active --quiet hw1-openclaw.service || die "--no-start unit is unexpectedly active"
    ! systemctl is-active --quiet hw1-openclaw-model.service || die "--no-start model unit is unexpectedly active"
    ! systemctl is-enabled --quiet hw1-openclaw.service || die "--no-start unit is unexpectedly enabled"
    warn "unit installed but left stopped and disabled (--no-start)"
    return 0
  fi
  phase "start and live health"
  ((DRY_RUN)) && { note "would enable/start hw1-openclaw.service and require RPC health on loopback"; return 0; }
  LIVE_VALIDATION_PENDING=1
  systemctl enable hw1-openclaw.service
  systemctl restart hw1-openclaw-model.service
  systemctl is-active --quiet hw1-openclaw-model.service || die "authenticated model readiness gate did not pass"
  systemctl start hw1-openclaw.service
  local attempt
  for attempt in $(seq 1 30); do
    if prod_cli gateway status --require-rpc --json >/dev/null 2>&1; then break; fi
    sleep 1
  done
  prod_cli gateway status --require-rpc --json >/dev/null || {
    systemctl status --no-pager hw1-openclaw.service >&2 || true
    die "gateway did not reach RPC health"
  }
  local listeners
  listeners="$(ss -H -ltn "sport = :$GATEWAY_PORT")"
  [[ -n $listeners ]] || die "gateway port is not listening"
  if grep -Evq '127\.0\.0\.1:|\[::1\]:' <<<"$listeners"; then
    die "gateway listener escaped loopback: $listeners"
  fi
  capture_security_audit live

  phase "live agent + durable-memory activation"
  local title_nonce secret_nonce title note_path write_json read_json write_session read_session
  title_nonce="$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
  secret_nonce="$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
  [[ $title_nonce =~ ^[0-9a-f]{12}$ && $secret_nonce =~ ^[0-9a-f]{24}$ ]] ||
    die "could not generate activation nonces"
  title="log/openclaw-install-smoke-$title_nonce"
  note_path="$VAULT_PATH/$title.md"
  write_session="install-smoke-write-$title_nonce"
  read_session="install-smoke-read-$title_nonce"
  write_json="$(mktemp)"
  read_json="$(mktemp)"
  if ! prod_cli agent --agent main --session-key "$write_session" --thinking off --timeout 600 --json \
    --message "Activation gate. Use exactly three note tools in order: (1) note_search with query='$title_nonce', folder='log', exclude=['archive']; it must be empty. (2) note_folders with prefix='log'. (3) note_append with title='$title', content exactly 'activation-code: $secret_nonce', and tags=['openclaw-smoke']. Then reply exactly WRITE_OK." \
    >"$write_json"; then
    record_verification "$write_json" "activation-$title_nonce-write.json"
    rm -f -- "$write_json" "$read_json"
    die "live note-write activation failed; evidence saved under $AUDIT_ROOT"
  fi
  record_verification "$write_json" "activation-$title_nonce-write.json"
  [[ -f $note_path && ! -L $note_path ]] || die "agent reported success without creating the smoke note"
  grep -Fxq "activation-code: $secret_nonce" "$note_path" || die "smoke note content differs from the requested durable value"
  [[ $(stat -c '%U:%G %a' "$note_path") == "$OPENCLAW_USER:$NOTES_GROUP 660" ]] ||
    die "smoke note ownership/mode escaped the vault-only boundary"
  if ! prod_cli agent --agent main --session-key "$read_session" --thinking off --timeout 600 --json \
    --message "Fresh-session memory gate. Use exactly one tool: note_read with title='$title'. Reply with only the activation-code value from that note." \
    >"$read_json"; then
    record_verification "$read_json" "activation-$title_nonce-read.json"
    rm -f -- "$write_json" "$read_json"
    die "fresh-session note-read activation failed; evidence saved under $AUDIT_ROOT"
  fi
  record_verification "$read_json" "activation-$title_nonce-read.json"
  /usr/bin/python3 -I - "$write_json" "$read_json" "$secret_nonce" <<'PY'
import json
import pathlib
import sys

def gateway_result(path, expected_tools):
    value = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    result = value.get("result")
    if value.get("status") != "ok" or not isinstance(result, dict):
        raise SystemExit(f"activation did not use a successful Gateway result: {path}")
    for item in (value, value.get("meta"), result, result.get("meta")):
        if isinstance(item, dict) and item.get("fallbackFrom") is not None:
            raise SystemExit(f"activation silently fell back from Gateway transport: {path}")
    meta = result.get("meta")
    summary = meta.get("toolSummary") if isinstance(meta, dict) else None
    if not isinstance(summary, dict):
        raise SystemExit(f"activation result has no trusted tool summary: {path}")
    if summary.get("calls") != len(expected_tools) or summary.get("tools") != expected_tools:
        raise SystemExit(f"activation used unexpected tools: {path}: {summary!r}")
    if summary.get("failures") != 0:
        raise SystemExit(f"activation tool summary reports a failure: {path}: {summary!r}")
    return result

def texts(path, expected_tools):
    result = gateway_result(path, expected_tools)
    return [
        item["text"].strip()
        for item in (result.get("payloads") or [])
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]

write_path, read_path, secret = sys.argv[1:]
if "WRITE_OK" not in texts(write_path, ["note_search", "note_folders", "note_append"]):
    raise SystemExit("write turn did not return the exact success sentinel")
if secret not in texts(read_path, ["note_read"]):
    raise SystemExit("fresh read turn did not return the stored activation code")
PY
  rm -f -- "$write_json" "$read_json"
  LIVE_VALIDATION_PENDING=0
  ok "Gateway RPC passed deep audit; a fresh session recovered a value through note_read"
}

preflight
confirm_plan
install_packages
verify_model_and_server_identity
quiesce_service_for_reconcile
create_accounts_and_dirs
provision_default_model
provision_default_llama
install_runtime
install_notes_bundle
install_config_and_unit
validate_install
configure_host_hardening
start_and_check

phase "done"
note "Production vault: $VAULT_PATH (group $NOTES_GROUP; model has no direct filesystem tool)"
note "Run the isolated agent benchmark: sudo hw1-openclaw-benchmark"
note "Do not run hw1-ai-service and the OpenClaw benchmark together unless measuring contention."
