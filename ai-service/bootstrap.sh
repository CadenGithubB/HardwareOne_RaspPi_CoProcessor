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
#   ~/hw1-ai-service/bootstrap.sh --llm-model auto|lfm2-8b-a1b|qwen3.5-2b
#
# Re-runnable by design: every step checks before it acts, existing credentials
# and custom configuration are never overwritten, and large/toolchain downloads
# require an explicit confirmation. Missing UART credentials are collected with
# a no-echo prompt. Model artifacts are revision-pinned, resumable, and verified
# by byte count plus SHA-256 before they become live.
#
# Board differences are detected, not configured: Pi 5 and CM5 take the same
# UART overlay and the same device node, and differ only in whether a kernel
# `pwmfan` topology is present for the fan controller to own.
set -euo pipefail

DRY_RUN=0
WITH_OC=0
NO_HELPERS=0
ASSUME_YES=0
LLM_MODEL_CHOICE=auto

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
        --llm-model)
            [ "$#" -ge 2 ] || { echo "--llm-model requires a value" >&2; usage 2; }
            LLM_MODEL_CHOICE=$2
            shift
            ;;
        --yes|-y)         ASSUME_YES=1 ;;
        -h|--help)        usage 0 ;;
        *) echo "unknown option: $1" >&2; usage 2 ;;
    esac
    shift
done

case "$LLM_MODEL_CHOICE" in
    auto|lfm2-8b-a1b|qwen3.5-2b) ;;
    *) die_early="unknown --llm-model profile: $LLM_MODEL_CHOICE" ;;
esac
[ -z "${die_early:-}" ] || { echo "$die_early" >&2; exit 2; }

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

# Production artifacts. Keep these synchronized with
# tools/llm/llm_serve_models.tsv. The 8GB profile is the measured shipping
# winner; the 4GB profile is the measured board-safe fallback.
LLAMA_REPO=https://github.com/ggml-org/llama.cpp.git
LLAMA_REF=b10516
LLAMA_COMMIT=b95502b
LLAMA_DIR=/opt/llama.cpp
DEFAULT_SERVER_BIN=/opt/llama.cpp/build/bin/llama-server

LFM_ID=lfm2-8b-a1b
LFM_REPO=unsloth/LFM2-8B-A1B-GGUF
LFM_REV=01c1c9ba807324289806d69f5895e11aff6de784
LFM_FILE=LFM2-8B-A1B-UD-Q3_K_XL.gguf
LFM_BYTES=3676339264
LFM_SHA=d10253b60d9699c4936a024fded42cba4581dc3640182146cba95fe57c143ac6
LFM_PATH=/opt/models/$LFM_FILE

QWEN_ID=qwen3.5-2b
QWEN_REPO=bartowski/Qwen_Qwen3.5-2B-GGUF
QWEN_REV=7d26695454df6de5fbcce2e58681e62dae06ce43
QWEN_FILE=Qwen_Qwen3.5-2B-Q4_0.gguf
QWEN_BYTES=1296764000
QWEN_SHA=91c102fc9a86de80e427057ee938e1e34fcaf3bba956b7296e252406e05f36f6
QWEN_PATH=/opt/models/$QWEN_FILE

LEGACY_DEFAULT_MODEL=/opt/models/Qwen3-1.7B-Q4_0.gguf
RAM_8GB_THRESHOLD_KIB=$((6 * 1024 * 1024))
RAM_4GB_THRESHOLD_KIB=$((3 * 1024 * 1024))

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

format_gib() {
    awk -v bytes="$1" 'BEGIN {printf "%.2f", bytes / 1073741824}'
}

select_model_profile() {
    local ram_kib=$1
    if [ "$LLM_MODEL_CHOICE" != auto ]; then
        printf '%s\n' "$LLM_MODEL_CHOICE"
    elif [ "$ram_kib" -ge "$RAM_8GB_THRESHOLD_KIB" ]; then
        printf '%s\n' "$LFM_ID"
    elif [ "$ram_kib" -ge "$RAM_4GB_THRESHOLD_KIB" ]; then
        printf '%s\n' "$QWEN_ID"
    else
        printf '%s\n' none
    fi
}

load_model_profile() {
    case "$1" in
        "$LFM_ID")
            MODEL_REPO=$LFM_REPO MODEL_REV=$LFM_REV MODEL_FILE=$LFM_FILE
            MODEL_BYTES=$LFM_BYTES MODEL_SHA=$LFM_SHA MODEL_PATH=$LFM_PATH
            ;;
        "$QWEN_ID")
            MODEL_REPO=$QWEN_REPO MODEL_REV=$QWEN_REV MODEL_FILE=$QWEN_FILE
            MODEL_BYTES=$QWEN_BYTES MODEL_SHA=$QWEN_SHA MODEL_PATH=$QWEN_PATH
            ;;
        none)
            MODEL_REPO= MODEL_REV= MODEL_FILE= MODEL_BYTES=0 MODEL_SHA= MODEL_PATH=
            ;;
        *) die "internal model profile error: $1" ;;
    esac
}

set_config_scalar() {
    local section=$1 key=$2 value=$3
    run "$VENV/bin/python" - "$CFG" "$section" "$key" "$value" <<'PY'
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
section, key, value = sys.argv[2:]
text = path.read_text(encoding="utf-8")
lines = text.splitlines(keepends=True)
inside = False
changed = 0
for index, line in enumerate(lines):
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*:\s*(?:#.*)?$", line.rstrip("\n")):
        inside = line.startswith(section + ":")
        continue
    if inside and re.match(rf"^  {re.escape(key)}:\s*", line):
        comment = ""
        if "#" in line:
            comment = "  #" + line.split("#", 1)[1].rstrip("\n")
        lines[index] = f"  {key}: {value}{comment}\n"
        changed += 1
if changed != 1:
    raise SystemExit(f"expected exactly one {section}.{key}, found {changed}")
path.write_text("".join(lines), encoding="utf-8")
PY
}

credentials_valid() {
    "$VENV/bin/python" - "$CFG" <<'PY' >/dev/null 2>&1
import sys
from hw1_ai_service import config
cfg = config.load(sys.argv[1])
config.read_credentials(cfg.link.credentials_file)
PY
}

prompt_credentials() {
    if [ "$DRY_RUN" -eq 1 ]; then
        note "would securely prompt for the existing ESP32 UART account"
        todo "create $CREDS via the guided no-echo credential prompt"
        return
    fi
    if [ ! -t 0 ]; then
        todo "create $CREDS from an interactive terminal (password input is never accepted via argv)"
        return
    fi
    note "The Linux account '$USER' already exists; this is the separate ESP32 UART account."
    note "Create that account on the ESP32 first if it does not already exist."
    if ! confirm "configure ESP32 UART credentials now?"; then
        todo "create $CREDS with the guided credential prompt"
        return
    fi
    local uart_user uart_pass uart_pass_again tmp
    read -r -p "  ESP32 UART username [cm5]: " uart_user
    uart_user=${uart_user:-cm5}
    case "$uart_user" in
        *[!A-Za-z0-9_.-]*|'') todo "UART username must use only A-Z, a-z, 0-9, _, . or -"; return ;;
    esac
    read -r -s -p "  ESP32 UART password: " uart_pass; printf '\n'
    read -r -s -p "  Repeat ESP32 UART password: " uart_pass_again; printf '\n'
    if [ -z "$uart_pass" ] || [ "$uart_pass" != "$uart_pass_again" ]; then
        unset uart_pass uart_pass_again
        todo "UART passwords were empty or did not match"
        return
    fi
    case "$uart_pass" in
        *[[:space:]]*) unset uart_pass uart_pass_again; todo "UART password may not contain whitespace"; return ;;
    esac
    tmp=$(mktemp "$CFG_DIR/.credentials.XXXXXX")
    chmod 0600 "$tmp"
    printf '%s %s\n' "$uart_user" "$uart_pass" > "$tmp"
    unset uart_pass uart_pass_again
    mv "$tmp" "$CREDS"
    ok "UART credentials created at $CREDS (0600)"
}

read_config_values() {
    eval "$("$VENV/bin/python" - "$CFG" <<'PY'
import shlex
import sys
from hw1_ai_service.config import load

cfg = load(sys.argv[1])
for name, value in (
    ("LLM_ENGINE", cfg.llm.engine),
    ("LLM_SERVER_BIN", cfg.llm.server_bin),
    ("LLM_MODEL", cfg.llm.model),
    ("LLM_MODEL_DIR", cfg.llm.model_dir),
    ("STT_ENGINE", cfg.stt.engine),
    ("STT_MODEL", cfg.stt.model),
):
    print(f"{name}={shlex.quote(str(value))}")
PY
)"
}

maybe_select_managed_model() {
    read_config_values
    [ "$LLM_ENGINE" = server ] || return 0
    if [ "$MODEL_PROFILE" = none ]; then
        case "$LLM_MODEL" in
            "$LFM_PATH"|"$QWEN_PATH"|"$LEGACY_DEFAULT_MODEL")
                note "RAM policy does not automatically admit a local LLM below 4GB-class RAM."
                if [ "$DRY_RUN" -eq 1 ]; then
                    note "would offer to set llm.engine to none"
                elif confirm "disable the managed local LLM for this board?"; then
                    cp -n "$CFG" "$CFG.hw1.bak"
                    set_config_scalar llm engine none
                    ok "disabled llm.engine (original preserved at $CFG.hw1.bak)"
                else
                    todo "disable llm.engine or explicitly select a board-safe custom model"
                fi
                ;;
        esac
        return 0
    fi
    [ "$LLM_MODEL" != "$MODEL_PATH" ] || return 0

    case "$LLM_MODEL" in
        "$LFM_PATH"|"$QWEN_PATH"|"$LEGACY_DEFAULT_MODEL")
            note "config currently selects: $LLM_MODEL"
            note "RAM policy selects:       $MODEL_PATH"
            if [ "$DRY_RUN" -eq 1 ]; then
                note "would offer to switch this managed setting to $MODEL_PROFILE"
                return
            fi
            if confirm "switch this managed model setting to $MODEL_PROFILE?"; then
                cp -n "$CFG" "$CFG.hw1.bak"
                set_config_scalar llm model "$MODEL_PATH"
                ok "updated llm.model (original preserved at $CFG.hw1.bak)"
            else
                todo "review llm.model: managed RAM policy selected $MODEL_PATH"
            fi
            ;;
        *)
            if [ "$LLM_MODEL_CHOICE" = auto ]; then
                note "custom llm.model preserved: $LLM_MODEL"
            else
                note "explicit profile $LLM_MODEL_CHOICE would replace custom model: $LLM_MODEL"
                if [ "$DRY_RUN" -eq 1 ]; then
                    note "would ask before replacing the custom setting"
                elif confirm "replace the custom llm.model with $MODEL_PROFILE?"; then
                    cp -n "$CFG" "$CFG.hw1.bak"
                    set_config_scalar llm model "$MODEL_PATH"
                    ok "updated llm.model (original preserved at $CFG.hw1.bak)"
                else
                    todo "apply explicit --llm-model $LLM_MODEL_CHOICE or keep auto"
                fi
            fi
            ;;
    esac
}

moonshine_ready() {
    "$VENV/bin/python" - "$STT_MODEL" <<'PY' >/dev/null 2>&1
import sys
from hw1_ai_service.stt.moonshine import MoonshineSTT
MoonshineSTT(sys.argv[1])
PY
}

ensure_moonshine() {
    [ "$STT_ENGINE" = moonshine ] || { skip "Moonshine model (stt.engine=$STT_ENGINE)"; return; }
    if moonshine_ready; then
        ok "Moonshine package and model are loadable ($STT_MODEL)"
        return
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        note "would offer to download and validate the Moonshine STT model: $STT_MODEL"
        return
    fi
    case "$STT_MODEL" in
        ??|??-??) ;;
        *) todo "Moonshine model path is not loadable: $STT_MODEL"; return ;;
    esac
    if ! confirm "download the Moonshine STT model for language '$STT_MODEL'?"; then
        todo "download Moonshine STT model '$STT_MODEL'"
        return
    fi
    if [ ! -x "$VENV/bin/moonshine-voice" ]; then
        todo "moonshine-voice CLI missing after installing $TREE[$EXTRA]"
        return
    fi
    run "$VENV/bin/moonshine-voice" download --stt --language "$STT_MODEL"
    moonshine_ready \
        && ok "Moonshine package and model are loadable ($STT_MODEL)" \
        || todo "Moonshine download completed but model '$STT_MODEL' still cannot load"
}

ensure_llama_server() {
    [ "$LLM_ENGINE" = server ] || { skip "llama-server (llm.engine=$LLM_ENGINE)"; return; }
    if [ -z "$LLM_SERVER_BIN" ]; then
        skip "llama-server build (configured external server)"
        return
    fi
    if [ -x "$LLM_SERVER_BIN" ]; then
        ok "llama-server present: $LLM_SERVER_BIN"
        return
    fi
    if [ "$LLM_SERVER_BIN" != "$DEFAULT_SERVER_BIN" ]; then
        todo "configured llama-server is missing: $LLM_SERVER_BIN"
        return
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        note "would offer to build pinned llama.cpp $LLAMA_REF ($LLAMA_COMMIT) at $LLAMA_DIR"
        return
    fi
    if ! confirm "install build tools and build pinned llama.cpp $LLAMA_REF?"; then
        todo "build $LLM_SERVER_BIN from pinned llama.cpp $LLAMA_REF"
        return
    fi
    run sudo apt-get update
    run sudo apt-get install -y build-essential cmake git curl ca-certificates
    if [ -d "$LLAMA_DIR/.git" ]; then
        actual_ref=$(git -C "$LLAMA_DIR" describe --tags --exact-match 2>/dev/null || true)
        if [ "$actual_ref" != "$LLAMA_REF" ]; then
            todo "$LLAMA_DIR exists at ${actual_ref:-an untagged revision}; refusing to replace it"
            return
        fi
        actual_commit=$(git -C "$LLAMA_DIR" rev-parse --short=7 HEAD)
        if [ "$actual_commit" != "$LLAMA_COMMIT" ]; then
            todo "$LLAMA_DIR is at commit ${actual_commit:-unknown}; expected $LLAMA_COMMIT"
            return
        fi
    elif [ -e "$LLAMA_DIR" ] && [ -n "$(find "$LLAMA_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
        todo "$LLAMA_DIR exists and is not an empty pinned llama.cpp checkout"
        return
    else
        run sudo install -d -o "$USER" -g "$USER" -m 0755 "$LLAMA_DIR"
        run git clone --depth 1 --branch "$LLAMA_REF" "$LLAMA_REPO" "$LLAMA_DIR"
    fi
    run cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" \
        -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON -DLLAMA_CURL=OFF
    run cmake --build "$LLAMA_DIR/build" --target llama-server -j4
    if [ ! -x "$LLM_SERVER_BIN" ]; then
        todo "llama.cpp build finished without producing $LLM_SERVER_BIN"
        return
    fi
    "$LLM_SERVER_BIN" --help 2>&1 | grep -q -- '--cache-reuse' \
        || { todo "$LLM_SERVER_BIN lacks required --cache-reuse support"; return; }
    ok "llama-server built from pinned $LLAMA_REF: $LLM_SERVER_BIN"
}

load_profile_for_path() {
    case "$1" in
        "$LFM_PATH") load_model_profile "$LFM_ID" ;;
        "$QWEN_PATH") load_model_profile "$QWEN_ID" ;;
        *) return 1 ;;
    esac
}

verify_model_file() {
    local path=$1 bytes=$2 sha=$3 actual
    [ -r "$path" ] || return 1
    [ "$(stat -c '%s' "$path" 2>/dev/null || echo 0)" = "$bytes" ] || return 1
    actual=$(sha256sum "$path" | awk '{print $1}')
    [ "$actual" = "$sha" ]
}

ensure_llm_model() {
    [ "$LLM_ENGINE" = server ] || { skip "GGUF model (llm.engine=$LLM_ENGINE)"; return; }
    if ! load_profile_for_path "$LLM_MODEL"; then
        [ -r "$LLM_MODEL" ] \
            && ok "custom llm.model present (not managed by bootstrap): $LLM_MODEL" \
            || todo "custom llm.model is missing: $LLM_MODEL"
        return
    fi
    if verify_model_file "$MODEL_PATH" "$MODEL_BYTES" "$MODEL_SHA"; then
        ok "$MODEL_FILE verified ($(format_gib "$MODEL_BYTES") GiB, SHA-256)"
        return
    fi
    if [ -e "$MODEL_PATH" ]; then
        todo "$MODEL_PATH exists but fails its pinned byte-count/SHA-256 check"
        return
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        note "would offer a resumable $(format_gib "$MODEL_BYTES") GiB download: $MODEL_FILE"
        note "revision $MODEL_REV; SHA-256 $MODEL_SHA"
        return
    fi
    if ! confirm "download pinned $MODEL_FILE ($(format_gib "$MODEL_BYTES") GiB)?"; then
        todo "download pinned model $MODEL_FILE"
        return
    fi
    command -v curl >/dev/null || {
        run sudo apt-get update
        run sudo apt-get install -y curl ca-certificates
    }
    model_dir=$(dirname "$MODEL_PATH")
    run sudo install -d -o "$USER" -g "$USER" -m 0755 "$model_dir"
    part="$MODEL_PATH.part"
    have=$(stat -c '%s' "$part" 2>/dev/null || echo 0)
    free=$(df -Pk "$model_dir" | awk 'NR == 2 {printf "%.0f", $4 * 1024}')
    need=$((MODEL_BYTES - have + 268435456))
    if [ "$free" -lt "$need" ]; then
        todo "insufficient free disk for $MODEL_FILE (need download remainder plus 256MiB margin)"
        return
    fi
    url="https://huggingface.co/$MODEL_REPO/resolve/$MODEL_REV/$MODEL_FILE?download=true"
    run curl -fL --retry 5 --retry-all-errors -C - -o "$part" "$url"
    if ! verify_model_file "$part" "$MODEL_BYTES" "$MODEL_SHA"; then
        todo "downloaded .part failed byte-count/SHA-256; it was retained for inspection"
        return
    fi
    run mv "$part" "$MODEL_PATH"
    ok "$MODEL_FILE downloaded and verified"
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

RAM_KIB=$(awk '$1 == "MemTotal:" {print $2; exit}' /proc/meminfo 2>/dev/null || true)
case "$RAM_KIB" in
    ''|*[!0-9]*) die "cannot determine physical RAM from /proc/meminfo" ;;
esac
MODEL_PROFILE=$(select_model_profile "$RAM_KIB")
load_model_profile "$MODEL_PROFILE"
note "RAM:     $(awk -v kib="$RAM_KIB" 'BEGIN {printf "%.1f GiB", kib / 1048576}')"
if [ "$MODEL_PROFILE" = none ]; then
    note "LLM:     none automatically selected (below 4GB-class RAM)"
else
    note "LLM:     $MODEL_PROFILE ($MODEL_FILE, $(format_gib "$MODEL_BYTES") GiB)"
fi

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
        run sudo sh -c "printf '\n# hw1-ai-service: UART link to the ESP32 on GPIO4/5\n%s\n' '$OVERLAY' >> '$BOOT_CFG'"
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

CFG_CREATED=0
if [ -e "$CFG" ]; then
    ok "config exists (left untouched): $CFG"
else
    run install -m 0600 "$TREE/config.example.yaml" "$CFG"
    CFG_CREATED=1
    if [ "$DRY_RUN" -eq 1 ]; then
        note "would configure the fresh file for model profile: $MODEL_PROFILE"
    elif [ "$MODEL_PROFILE" = "$QWEN_ID" ]; then
        set_config_scalar llm model "$QWEN_PATH"
    elif [ "$MODEL_PROFILE" = none ]; then
        set_config_scalar llm engine none
    fi
    ok "created fresh config for profile $MODEL_PROFILE: $CFG"
fi

if [ -e "$CREDS" ]; then
    perm="$(stat -c '%a' "$CREDS" 2>/dev/null || echo '?')"
    if [ "$perm" = 600 ]; then
        ok "UART credentials present"
    else
        run chmod 600 "$CREDS"
        ok "UART credentials present (tightened to 0600)"
    fi
    if [ "$DRY_RUN" -eq 0 ] && ! credentials_valid; then
        todo "$CREDS is not a valid '<user> <password>' credential file"
    fi
else
    prompt_credentials
fi

# ------------------------------------------------------------------- models --
phase "models"

if [ -r "$CFG" ] && [ -x "$VENV/bin/python" ]; then
    maybe_select_managed_model
    read_config_values
    if [ -n "${LLM_MODEL_DIR:-}" ]; then
        if [ -d "$LLM_MODEL_DIR" ]; then
            ok "model_dir exists: $LLM_MODEL_DIR"
        elif [ "$DRY_RUN" -eq 1 ]; then
            note "would create model catalog: $LLM_MODEL_DIR"
        elif sudo install -d -o "$USER" -g "$USER" -m 0755 "$LLM_MODEL_DIR" 2>/dev/null; then
            ok "created model_dir: $LLM_MODEL_DIR"
        else
            todo "create the catalog directory $LLM_MODEL_DIR (readable by $USER)"
        fi
    fi
    ensure_moonshine
    ensure_llama_server
    ensure_llm_model
else
    note "fresh dry-run plan: install Moonshine package/model and pinned llama.cpp $LLAMA_REF ($LLAMA_COMMIT)"
    if [ "$MODEL_PROFILE" != none ]; then
        note "fresh dry-run plan: download $MODEL_FILE ($(format_gib "$MODEL_BYTES") GiB)"
        note "revision $MODEL_REV; SHA-256 $MODEL_SHA"
    fi
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
else
    die "missing $UNIT_SRC"
fi

SERVICE_ENABLED=$(systemctl --user is-enabled hw1-ai-service.service 2>/dev/null || true)
SERVICE_ACTIVE=$(systemctl --user is-active hw1-ai-service.service 2>/dev/null || true)
note "activation unchanged for now (enabled=${SERVICE_ENABLED:-unknown}, active=${SERVICE_ACTIVE:-unknown})"

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
    run systemctl --user enable hw1-ai-service.service
    if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" = yes ]; then
        ok "lingering enabled (survives logout, starts at boot)"
    else
        run sudo loginctl enable-linger "$USER"
        ok "enabled lingering for $USER"
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
    printf '\n  %s outstanding item(s); service activation was left unchanged:\n\n' "${#TODO[@]}"
    for t in "${TODO[@]}"; do printf '    - %s\n' "$t"; done
    printf '\n  Re-run this script when they are done; it continues from here.\n'
    exit 1
fi
