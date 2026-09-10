#!/usr/bin/env bash
# First-time Raspberry Pi setup guide for the HardwareOne CM5 companion.
#
# This is the one user-facing entry point. It deliberately runs as the
# logged-in administrator so the core service can use a systemd --user unit;
# the OpenClaw installer is invoked through sudo and runs its own locked
# service identities. No ESP32 credentials are accepted on the command line.
set -Eeuo pipefail
umask 077
export LC_ALL=C

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
CORE_INSTALLER="$SCRIPT_DIR/bootstrap.sh"
OPENCLAW_INSTALLER="$SCRIPT_DIR/openclaw/bootstrap_openclaw.sh"

MODE=
DRY_RUN=0
ASSUME_YES=0
LLM_MODEL=auto
WITH_HOST_HARDENING=0
WITH_FIREWALL=0
ALLOW_OPENCLAW_NETWORK=0
VAULT_OPERATOR=
PROFILE_INTERACTIVE=0

die() { printf '\n\033[31mabort:\033[0m %s\n' "$*" >&2; exit 1; }
note() { printf '  %s\n' "$*"; }
phase() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

usage() {
  cat <<'EOF'
Usage: ./setup.sh [options]

This is the interactive first-time setup guide for a Raspberry Pi 5/CM5.
Run it as the normal SSH/console administrator, not as root. It installs:

  core       STT + local LLM + HardwareOne ESP32 daemon
  openclaw   core plus the isolated, notes-only OpenClaw agent
  openclaw-only
             the isolated RPi OpenClaw agent without UART/ESP32 setup

Options:
  --mode core|openclaw|openclaw-only
                             non-interactive profile selection
  --core-only                same as --mode core
  --with-openclaw            same as --mode openclaw
  --openclaw-only            same as --mode openclaw-only
  --llm-model auto|lfm2-8b-a1b|qwen3.5-2b
  --with-host-hardening      unattended updates + fail2ban/sshd setup
  --with-firewall            host hardening plus UFW (SSH ports are allowed)
  --allow-openclaw-network   opt out of the default OpenClaw egress block
  --vault-operator USER      explicitly grant a human account vault access
  --no-vault-operator        do not grant a human account vault access
  --dry-run                  print both installer plans without changing files
  --yes, -y                  accept package/download confirmations
  -h, --help                 show this help

The OpenClaw profile provisions its pinned model and llama.cpp server. The
default Gateway policy is loopback-only with outbound networking denied.
EOF
}

need_value() { (($# >= 2)) || die "$1 requires a value"; }

# Root is intentionally not the normal execution mode: bootstrap.sh needs the
# caller's systemd --user session. If invoked with sudo, return to that user
# while retaining the original arguments and tty; the OpenClaw phase elevates
# only when it reaches its root-owned installer.
if [ "$(id -u)" -eq 0 ]; then
  case "${1:-}" in
    -h|--help) usage; exit 0 ;;
  esac
  if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    exec sudo -u "$SUDO_USER" -H -- "$0" "$@"
  fi
  die "run ./setup.sh as your normal Pi administrator; it calls sudo only for root-owned steps"
fi

while (($#)); do
  case "$1" in
    --mode) need_value "$@"; MODE=$2; shift 2 ;;
    --core-only) MODE=core; shift ;;
    --with-openclaw) MODE=openclaw; shift ;;
    --openclaw-only) MODE=openclaw-only; shift ;;
    --llm-model) need_value "$@"; LLM_MODEL=$2; shift 2 ;;
    --with-host-hardening) WITH_HOST_HARDENING=1; shift ;;
    --with-firewall) WITH_FIREWALL=1; WITH_HOST_HARDENING=1; shift ;;
    --allow-openclaw-network) ALLOW_OPENCLAW_NETWORK=1; shift ;;
    --vault-operator) need_value "$@"; VAULT_OPERATOR=$2; shift 2 ;;
    --no-vault-operator) VAULT_OPERATOR=none; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

case "$MODE" in
  core|openclaw|openclaw-only) ;;
  '')
    [ -t 0 ] || die "non-interactive setup requires --mode core, --mode openclaw, or --mode openclaw-only"
    phase "HardwareOne Raspberry Pi first-time setup"
    note "This changes only the RPi side. The ESP32 must be connected for the final link probe."
    note "Core mode installs STT, the local LLM, and the ESP32 daemon."
    note "OpenClaw mode adds a notes-only agent, a pinned local model, and a hardened Gateway."
    note "OpenClaw-only skips UART, STT, and the ESP32 daemon entirely."
    printf '\n  1) Core only\n  2) Core + OpenClaw\n  3) OpenClaw only (RPi)\n\n'
    read -r -p '  Select an install profile [1-3]: ' profile
    case "${profile:-}" in
      1) MODE=core ;;
      2) MODE=openclaw ;;
      3) MODE=openclaw-only ;;
      *) die 'choose 1, 2, or 3' ;;
    esac
    PROFILE_INTERACTIVE=1
    ;;
  *) die "--mode must be core or openclaw" ;;
esac

if [ "$PROFILE_INTERACTIVE" -eq 1 ] && [[ "$MODE" == openclaw* ]]; then
  printf '\n'
  read -r -p '  Enable unattended security updates + fail2ban? [y/N] ' hardening
  case "${hardening:-n}" in
    y|Y) WITH_HOST_HARDENING=1 ;;
  esac
  read -r -p '  Enable UFW with SSH allowed and deny other inbound traffic? [y/N] ' firewall
  case "${firewall:-n}" in
    y|Y) WITH_FIREWALL=1; WITH_HOST_HARDENING=1 ;;
  esac
  read -r -p "  Give this login direct read/write access to the notes vault? [Y/n] " vault_operator
  case "${vault_operator:-y}" in
    n|N) VAULT_OPERATOR=none ;;
    *) VAULT_OPERATOR=$USER ;;
  esac
fi

# Non-interactive installs keep the strongest default: the agent can use its
# vault, but the login account is not added to the vault group unless the
# operator explicitly supplies --vault-operator.
if [ "$MODE" = openclaw ] && [ "$PROFILE_INTERACTIVE" -eq 0 ] && [ -z "$VAULT_OPERATOR" ]; then
  VAULT_OPERATOR=none
fi

if [ ! -x "$CORE_INSTALLER" ]; then die "missing core installer: $CORE_INSTALLER"; fi
if [[ "$MODE" == openclaw* ]] && [ ! -x "$OPENCLAW_INSTALLER" ]; then
  die "missing OpenClaw installer: $OPENCLAW_INSTALLER"
fi

phase "selected profile"
note "profile: $MODE"
note "login/service account: $USER"
note "LLM profile: $LLM_MODEL"
if [[ "$MODE" == openclaw* ]]; then
  note "OpenClaw egress: $([ "$ALLOW_OPENCLAW_NETWORK" -eq 1 ] && printf allowed || printf blocked)"
  note "direct vault access: $([ "$VAULT_OPERATOR" = none ] && printf no || printf '%s' "${VAULT_OPERATOR:-none}")"
  note "resource note: core and OpenClaw each keep a supervised local model server;"
  note "              do not run the benchmark concurrently with normal traffic"
fi
if [ "$DRY_RUN" -eq 1 ]; then note "dry run: no changes will be made"; fi

core_args=(--llm-model "$LLM_MODEL")
((DRY_RUN)) && core_args+=(--dry-run)
((ASSUME_YES)) && core_args+=(--yes)

if [ "$MODE" = openclaw-only ]; then
  note "skipping HardwareOne core (no UART, STT, LLM daemon, or ESP32 setup)"
else
  phase "HardwareOne core"
  note "running the existing core installer (STT + LLM + ESP32 daemon)"
  "$CORE_INSTALLER" "${core_args[@]}"
fi

if [ "$MODE" = core ]; then
  phase "complete"
  note "Core profile finished. Re-run with --with-openclaw later to add the agent."
  exit 0
fi

oc_args=()
((DRY_RUN)) && oc_args+=(--dry-run)
((ASSUME_YES)) && oc_args+=(--yes)
((ALLOW_OPENCLAW_NETWORK)) && oc_args+=(--allow-network)
((WITH_HOST_HARDENING)) && oc_args+=(--with-host-hardening)
((WITH_FIREWALL)) && oc_args+=(--with-firewall)

# The common Raspberry Pi SSH image may use the human login name "openclaw".
# The agent cannot safely run as that login, so request a distinct locked
# identity. Other login names keep the historical OpenClaw service identity.
if [ "$USER" = openclaw ]; then
  oc_args+=(--agent-user hw1-openclaw-agent)
fi
if [ -n "$VAULT_OPERATOR" ] && [ "$VAULT_OPERATOR" != none ]; then
  oc_args+=(--operator "$VAULT_OPERATOR")
fi

phase "OpenClaw"
note "elevating only for the root-owned OpenClaw installer"
sudo "$OPENCLAW_INSTALLER" "${oc_args[@]}"

phase "complete"
note "The selected RPi profile is installed and validated."
if [[ "$MODE" == openclaw* ]]; then
  note "Gateway remains loopback-only and internet egress is blocked by default."
  note "Use 'sudo hw1-openclaw-benchmark' for the isolated TTFT/decode benchmark."
fi
