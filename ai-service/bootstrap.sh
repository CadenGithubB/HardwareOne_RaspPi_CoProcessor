#!/usr/bin/env bash
# Take a bare Raspberry Pi 5 or Compute Module 5 to a running hw1-ai-service.
#
# Run this ON THE DEVICE, as the unprivileged service account, after the source
# tree is present (./deploy.sh from the Mac, or a git clone here):
#
#   ~/hw1-ai-service/bootstrap.sh              # do everything it safely can
#   ~/hw1-ai-service/bootstrap.sh --dry-run    # print the plan, change nothing
#   ~/hw1-ai-service/bootstrap.sh --with-oc-helper
#   ~/hw1-ai-service/bootstrap.sh --no-helpers
#
# Re-runnable by design: every step checks before it acts, and nothing is
# overwritten. Steps this script must not perform for you — creating UART
# credentials, downloading model weights — are reported as TODO and the service
# is left stopped until they are done. Run it again afterwards; it continues.
#
# Board differences are detected, not configured: Pi 5 and CM5 take the same
# UART overlay and the same device node, and differ only in whether a kernel
# `pwmfan` topology is present for the fan controller to own.
set -euo pipefail

DRY_RUN=0
WITH_OC=0
NO_HELPERS=0
ASSUME_YES=0

usage() {
    # the whole leading comment block, minus the shebang
    awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
    exit "${1:-0}"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run)        DRY_RUN=1 ;;
        --with-oc-helper) WITH_OC=1 ;;
        --no-helpers)     NO_HELPERS=1 ;;
        --yes|-y)         ASSUME_YES=1 ;;
        -h|--help)        usage 0 ;;
        *) echo "unknown option: $1" >&2; usage 2 ;;
    esac
    shift
done

TREE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
VENV="$HOME/hw1ai"
CFG_DIR="$HOME/.config/hw1-ai-service"
CFG="$CFG_DIR/config.yaml"
CREDS="$CFG_DIR/credentials"
UNIT_SRC="$TREE/systemd/hw1-ai-service.service"
UNIT_DST="$HOME/.config/systemd/user/hw1-ai-service.service"
BOOT_CFG=/boot/firmware/config.txt
OVERLAY=dtoverlay=uart2-pi5
EXTRA="${HW1_EXTRA:-moonshine}"

TODO=()
note()  { printf '  %s\n' "$*"; }
ok()    { printf '  \033[32mok\033[0m    %s\n' "$*"; }
skip()  { printf '  \033[33mskip\033[0m  %s\n' "$*"; }
todo()  { printf '  \033[31mTODO\033[0m  %s\n' "$*"; TODO+=("$*"); }
phase() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die()   { printf '\n\033[31mabort:\033[0m %s\n' "$*" >&2; exit 1; }

run() {
    if [ "$DRY_RUN" -eq 1 ]; then printf '  would run: %s\n' "$*"; return 0; fi
    "$@"
}

confirm() {
    [ "$ASSUME_YES" -eq 1 ] && return 0
    [ "$DRY_RUN" -eq 1 ] && return 1
    local answer
    read -r -p "  $1 [y/N] " answer || return 1
    [ "${answer:-n}" = y ]
}

# ---------------------------------------------------------------- preflight --
phase "preflight"

[ "$(id -u)" -ne 0 ] || die "run as the unprivileged service account, not root (it uses sudo where needed)"
[ -d "$TREE/hw1_ai_service" ] || die "no hw1_ai_service/ beside this script — is the source tree complete?"
[ -r "$TREE/config.example.yaml" ] || die "no config.example.yaml in $TREE"
command -v systemctl >/dev/null || die "systemd is required"
systemctl --user show-environment >/dev/null 2>&1 || die "no systemd --user session; log in over ssh as $USER rather than su'ing"

BOARD="unknown"
if [ -r /proc/device-tree/model ]; then
    BOARD="$(tr -d '\0' < /proc/device-tree/model)"
fi
note "board:   $BOARD"
note "tree:    $TREE"
note "venv:    $VENV"
note "account: $USER"
case "$BOARD" in
    *"Compute Module 5"*|*"Raspberry Pi 5"*) ok "supported board" ;;
    *) skip "unrecognized board — continuing, but the UART overlay and fan topology below are Pi 5 family assumptions" ;;
esac
[ "$DRY_RUN" -eq 1 ] && note "(dry run — nothing will be changed)"

# --------------------------------------------------------------------- uart --
phase "serial link"

PORT=/dev/ttyAMA2   # replaced by the live config value once one exists
if [ -r "$CFG" ] && [ -x "$VENV/bin/python" ]; then
    PORT="$("$VENV/bin/python" - "$CFG" <<'PY' 2>/dev/null || echo /dev/ttyAMA2
import sys
from hw1_ai_service.config import load
print(load(sys.argv[1]).link.port)
PY
)"
fi
note "port: $PORT"

if [ -e "$PORT" ]; then
    ok "$PORT present"
elif [ ! -r "$BOOT_CFG" ]; then
    todo "$PORT missing and $BOOT_CFG unreadable — enable the UART for your platform by hand"
elif grep -qE "^[[:space:]]*$OVERLAY([[:space:]]|$)" "$BOOT_CFG"; then
    todo "$OVERLAY is configured but $PORT does not exist — reboot, then re-run this script"
else
    note "$OVERLAY is absent from $BOOT_CFG"
    note "(this is the Pi 5 family overlay; plain 'uart2' is the Pi 4 one and does nothing here)"
    if confirm "append $OVERLAY to $BOOT_CFG?"; then
        run sudo cp -n "$BOOT_CFG" "$BOOT_CFG.hw1.bak"
        run sudo sh -c "printf '\n# hw1-ai-service: UART link to the XIAO on GPIO4/5\n%s\n' '$OVERLAY' >> '$BOOT_CFG'"
        todo "reboot to create $PORT, then re-run this script"
    else
        todo "add '$OVERLAY' to $BOOT_CFG and reboot, then re-run this script"
    fi
fi

if id -nG "$USER" | tr ' ' '\n' | grep -qx dialout; then
    ok "$USER is in dialout"
else
    run sudo usermod -aG dialout "$USER"
    todo "added $USER to dialout — log out and back in for it to take effect"
fi

# ------------------------------------------------------------------- python --
phase "python environment"

if [ -x "$VENV/bin/python" ]; then
    ok "venv exists at $VENV"
else
    note "creating venv at $VENV"
    run python3 -m venv "$VENV"
fi

note "installing $TREE[$EXTRA] (editable)"
run "$VENV/bin/python" -m pip install --quiet --upgrade pip
run "$VENV/bin/python" -m pip install --quiet -e "$TREE[$EXTRA]"

if [ "$DRY_RUN" -eq 0 ]; then
    pkg="$("$VENV/bin/python" -c 'import hw1_ai_service; print(hw1_ai_service.__file__)')"
    case "$pkg" in
        "$TREE"/hw1_ai_service/*) ok "imports from the deployed tree" ;;
        *) die "service imports an unexpected tree: $pkg" ;;
    esac
fi

# ------------------------------------------------------- config + creds --
phase "configuration"

run install -d -m 0700 "$CFG_DIR"

if [ -e "$CFG" ]; then
    ok "config exists (left untouched): $CFG"
else
    run install -m 0600 "$TREE/config.example.yaml" "$CFG"
    todo "review $CFG — at minimum llm.model, llm.model_dir and link.port"
fi

if [ -e "$CREDS" ]; then
    perm="$(stat -c '%a' "$CREDS" 2>/dev/null || echo '?')"
    if [ "$perm" = 600 ]; then
        ok "UART credentials present"
    else
        run chmod 600 "$CREDS"
        ok "UART credentials present (tightened to 0600)"
    fi
else
    todo "create $CREDS (chmod 600), one line: '<user> <password>'"
    note "     the account is made on the XIAO first, e.g.  useradd cm5svc <pass> 0 admin"
    note "     this script will not invent a credential for you"
fi

# ------------------------------------------------------------------- models --
phase "models"

if [ -r "$CFG" ] && [ -x "$VENV/bin/python" ] && [ "$DRY_RUN" -eq 0 ]; then
    eval "$("$VENV/bin/python" - "$CFG" <<'PY'
import shlex, sys
from hw1_ai_service.config import load
cfg = load(sys.argv[1])
print(f"LLM_MODEL={shlex.quote(cfg.llm.model)}")
print(f"LLM_MODEL_DIR={shlex.quote(cfg.llm.model_dir)}")
PY
)"
    if [ -n "${LLM_MODEL_DIR:-}" ]; then
        if [ -d "$LLM_MODEL_DIR" ]; then
            ok "model_dir exists: $LLM_MODEL_DIR"
        elif sudo install -d -o "$USER" -g "$USER" -m 0755 "$LLM_MODEL_DIR" 2>/dev/null; then
            ok "created model_dir: $LLM_MODEL_DIR"
        else
            todo "create the catalog directory $LLM_MODEL_DIR (readable by $USER)"
        fi
    fi
    if [ -z "${LLM_MODEL:-}" ]; then
        todo "set llm.model in $CFG to a GGUF path"
    elif [ -r "$LLM_MODEL" ]; then
        ok "llm.model present: $LLM_MODEL"
    else
        todo "download the GGUF named by llm.model: $LLM_MODEL"
    fi
    command -v llama-server >/dev/null \
        && ok "llama-server on PATH" \
        || todo "build/install llama-server and put it on PATH"
else
    skip "model checks need a readable config and the venv"
fi

# ------------------------------------------------------------------ service --
phase "user service"

if [ -r "$UNIT_SRC" ]; then
    if [ -e "$UNIT_DST" ] && cmp -s "$UNIT_SRC" "$UNIT_DST"; then
        ok "installed unit matches the tracked one"
    else
        run install -Dm0644 "$UNIT_SRC" "$UNIT_DST"
        ok "installed $UNIT_DST"
    fi
    run systemctl --user daemon-reload
    run systemd-analyze --user verify "$UNIT_DST"
    run systemctl --user enable hw1-ai-service.service
else
    die "missing $UNIT_SRC"
fi

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" = yes ]; then
    ok "lingering enabled (survives logout, starts at boot)"
else
    run sudo loginctl enable-linger "$USER"
    ok "enabled lingering for $USER"
fi

# ------------------------------------------------------- privileged helpers --
phase "privileged helpers"

if [ "$NO_HELPERS" -eq 1 ]; then
    skip "all helpers (--no-helpers)"
else
    if [ -x "$TREE/systemd/install-power-helper.sh" ]; then
        run sudo "$TREE/systemd/install-power-helper.sh" "$USER"
        ok "host power plane"
    else
        skip "power helper installer not found"
    fi

    # Fan: the controller requires exactly one hwmon named 'pwmfan' exposing
    # pwm1. Present on a CM5 IO board and on a Pi 5 with the official cooler;
    # absent with no fan, and ambiguous if something else registers one too.
    fans=0
    for n in /sys/class/hwmon/hwmon*/name; do
        [ -r "$n" ] || continue
        [ "$(cat "$n")" = pwmfan ] || continue
        [ -f "$(dirname "$n")/pwm1" ] || continue
        fans=$((fans + 1))
    done
    if [ "$fans" -eq 1 ]; then
        run sudo "$TREE/systemd/install-fan-controller.sh" "$USER"
        ok "fan controller (one pwmfan topology found)"
    else
        skip "fan controller — expected one pwmfan hwmon exposing pwm1, found $fans"
        note "     the daemon's own discovery would fail the same way; fix the"
        note "     cooling topology first, then re-run to install it"
    fi

    if [ "$WITH_OC" -eq 1 ]; then
        run sudo "$TREE/systemd/install-oc-helper.sh" "$USER"
        ok "overclock helper"
    else
        skip "overclock helper (pass --with-oc-helper; it grants writes to $BOOT_CFG)"
    fi
fi

# ------------------------------------------------------------------ summary --
phase "summary"

if [ "${#TODO[@]}" -eq 0 ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        note "dry run complete — nothing was changed"
        exit 0
    fi
    run systemctl --user restart hw1-ai-service.service
    sleep 3
    state="$(systemctl --user show hw1-ai-service.service -p ActiveState --value)"
    note "ActiveState=$state"
    if [ "$state" = active ]; then
        ok "hw1-ai-service is running"
        note ""
        note "next:  $VENV/bin/hw1-ai-service -c $CFG probe"
        note "       journalctl --user -u hw1-ai-service.service -n 40 --no-pager"
    else
        die "service did not come up — journalctl --user -u hw1-ai-service.service -n 60"
    fi
else
    printf '\n  %s outstanding item(s); service left stopped:\n\n' "${#TODO[@]}"
    for t in "${TODO[@]}"; do printf '    - %s\n' "$t"; done
    printf '\n  Re-run this script when they are done; it continues from here.\n'
    exit 1
fi
