#!/usr/bin/env bash
# Gated deploy: the full local check suite must pass before rsync can run.
# User-invoked only — never wire this into anything automatic.
#
#   ./deploy.sh           # checks -> itemized dry-run -> confirm -> rsync
#   ./deploy.sh --verify  # additionally run the Pi-side post-deploy check
#                             # over ssh (will prompt for the password)
#   ./deploy.sh --verify-only  # verify an already-synced installation
#   CM5_HOST=x.x.x.x ./deploy.sh --verify  # temporary DHCP override
#   CM5_USER=name ./deploy.sh --verify      # alternate SSH account
#
# Canonical paths per docs/CM5_DEPLOYMENT_PATHS.md: trailing slashes are load-
# bearing (contents of the one Mac source dir into the one Pi source dir),
# and --delete is deliberately absent.
set -euo pipefail
cd "$(dirname "$0")"

usage() {
    echo "usage: $0 [--verify|--verify-only]" >&2
}

RUN_DEPLOY=1
VERIFY=0
case "$#" in
    0) ;;
    1)
        if [ "$1" = "--verify" ]; then
            VERIFY=1
        elif [ "$1" = "--verify-only" ]; then
            RUN_DEPLOY=0
            VERIFY=1
        else
            usage
            exit 2
        fi
        ;;
    *)
        usage
        exit 2
        ;;
esac

SRC="$(pwd)/ai-service/"
CM5_HOST="${CM5_HOST:-xiaocm5}"
CM5_USER="${CM5_USER:-cm5}"
CM5_SSH="${CM5_USER}@${CM5_HOST}"
# A relative rsync destination is resolved from the authenticated account's
# home directory.  This keeps deployment correct for the selected account's
# $HOME without rebuilding an account name into the path.
DEST="${CM5_SSH}:hw1-ai-service/"
EXCLUDES=(--exclude '.pytest_cache/' --exclude '__pycache__/' --exclude '*.pyc'
          --exclude '.venv*/' --exclude '*.egg-info/' --exclude '.ruff_cache/'
          --exclude '.corpus/')

if [ "$RUN_DEPLOY" -eq 1 ]; then
    echo "==== 1/3 local checks (deploy refuses on red) ===="
    ./ai-service/run_checks.sh

    echo
    echo "==== 2/3 dry run ===="
    rsync -avni --itemize-changes "${EXCLUDES[@]}" "$SRC" "$DEST"

    echo
    if ! read -r -p "Apply this sync to the Pi? [y/N] " answer; then
        echo "aborted — confirmation input unavailable; nothing synced" >&2
        exit 1
    fi
    if [ "${answer:-n}" != "y" ]; then
        echo "aborted — nothing synced"
        exit 1
    fi

    echo "==== 3/3 sync ===="
    rsync -avi --itemize-changes "${EXCLUDES[@]}" "$SRC" "$DEST"

    echo
    echo "Synced. On the Pi:"
    echo "  ./hw1-ai-service/bootstrap.sh    # first install, or after adding a helper"
    echo "  systemctl --user restart hw1-ai-service.service"
    echo "  systemctl --user show hw1-ai-service.service -p ActiveState -p SubState -p NRestarts -p WatchdogTimestampMonotonic"
    echo "  # If journald is available: journalctl --user -u hw1-ai-service.service -n 40 --no-pager"
fi

if [ "$VERIFY" -eq 1 ]; then
    echo
    echo "==== post-deploy verification (ssh) ===="
    ssh "$CM5_SSH" '
        set -e
        test -x "$HOME/hw1ai/bin/python" || {
          echo "ERROR: missing $HOME/hw1ai/bin/python; run ~/hw1-ai-service/bootstrap.sh" >&2
          exit 1
        }
        test -x "$HOME/hw1ai/bin/hw1-ai-service" || {
          echo "ERROR: missing $HOME/hw1ai/bin/hw1-ai-service; run ~/hw1-ai-service/bootstrap.sh" >&2
          exit 1
        }
        test -r "$HOME/.config/hw1-ai-service/config.yaml" || {
          echo "ERROR: missing live config under $HOME/.config/hw1-ai-service" >&2
          exit 1
        }
        test -f "$HOME/.config/systemd/user/hw1-ai-service.service" || {
          echo "ERROR: missing user unit; run ~/hw1-ai-service/bootstrap.sh" >&2
          exit 1
        }
        package_path=$("$HOME/hw1ai/bin/python" -c \
          "import hw1_ai_service; print(hw1_ai_service.__file__)")
        case "$package_path" in
          "$HOME"/hw1-ai-service/hw1_ai_service/*) ;;
          *) echo "ERROR: service imports unexpected tree: $package_path" >&2; exit 1 ;;
        esac
        echo "PackagePath=$package_path"
        "$HOME/hw1ai/bin/python" -c \
          "from hw1_ai_service import config; import sys; cfg = config.load(sys.argv[1]); config.read_credentials(cfg.link.credentials_file)" \
          "$HOME/.config/hw1-ai-service/config.yaml"

        fragment=$(systemctl --user show hw1-ai-service.service \
          -p FragmentPath --value)
        [ "$fragment" = "$HOME/.config/systemd/user/hw1-ai-service.service" ] || {
          echo "ERROR: systemd loaded unexpected unit: $fragment" >&2
          exit 1
        }
        cmp -s "$HOME/hw1-ai-service/systemd/hw1-ai-service.service" \
          "$HOME/.config/systemd/user/hw1-ai-service.service" || {
          echo "ERROR: installed user unit differs from the synced tracked unit; run ~/hw1-ai-service/bootstrap.sh" >&2
          exit 1
        }
        [ "$(systemctl --user show hw1-ai-service.service -p NeedDaemonReload --value)" = no ] || {
          echo "ERROR: systemd requires daemon-reload; run ~/hw1-ai-service/bootstrap.sh" >&2
          exit 1
        }
        systemctl --user restart hw1-ai-service.service
        invocation=$(systemctl --user show hw1-ai-service.service \
          -p InvocationID --value)
        main_pid=$(systemctl --user show hw1-ai-service.service \
          -p MainPID --value)
        restart_count=$(systemctl --user show hw1-ai-service.service \
          -p NRestarts --value)
        [ -n "$invocation" ]
        [ -n "$main_pid" ] && [ "$main_pid" -gt 1 ]
        [ -n "$restart_count" ]
        sleep 3
        state=$(systemctl --user show hw1-ai-service.service -p ActiveState --value)
        echo "ActiveState=$state"; [ "$state" = active ]
        [ "$(systemctl --user show hw1-ai-service.service -p InvocationID --value)" = "$invocation" ]
        [ "$(systemctl --user show hw1-ai-service.service -p MainPID --value)" = "$main_pid" ]
        [ "$(systemctl --user show hw1-ai-service.service -p NRestarts --value)" = "$restart_count" ]
        systemctl --user show hw1-ai-service.service \
          -p FragmentPath -p ExecStart -p ActiveState -p SubState \
          -p NRestarts --no-pager
        w1=$(systemctl --user show hw1-ai-service.service -p WatchdogTimestampMonotonic --value)
        [ -n "$w1" ] && [ "$w1" != 0 ]
        sleep 35
        [ "$(systemctl --user show hw1-ai-service.service -p ActiveState --value)" = active ]
        [ "$(systemctl --user show hw1-ai-service.service -p InvocationID --value)" = "$invocation" ]
        [ "$(systemctl --user show hw1-ai-service.service -p MainPID --value)" = "$main_pid" ]
        [ "$(systemctl --user show hw1-ai-service.service -p NRestarts --value)" = "$restart_count" ]
        w2=$(systemctl --user show hw1-ai-service.service -p WatchdogTimestampMonotonic --value)
        echo "watchdog $w1 -> $w2"
        [ -n "$w2" ] && [ "$w2" != 0 ] && [ "$w2" != "$w1" ]
        if invocation_log=$(journalctl --quiet --user \
          -u hw1-ai-service.service _SYSTEMD_INVOCATION_ID="$invocation" \
          --no-pager 2>/dev/null) \
          && [ -n "$invocation_log" ]; then
          printf "%s\n" "$invocation_log" | grep -q "daemon ready" || {
            echo "ERROR: current service invocation has no daemon-ready marker" >&2
            exit 1
          }
          if printf "%s\n" "$invocation_log" | grep -iE " ERROR "; then
            echo "ERROR: current service invocation logged an error" >&2
            exit 1
          fi
          echo "JournalReadiness=daemon-ready"
        else
          socket_path=$("$HOME/hw1ai/bin/python" - \
            "$HOME/.config/hw1-ai-service/config.yaml" <<'PY'
import os
import sys
from pathlib import Path
from hw1_ai_service.config import load

path = Path(os.path.expanduser(load(sys.argv[1]).service.socket_path))
if not path.is_absolute():
    raise SystemExit(f"control socket path is not absolute: {path}")
print(path)
PY
          )
          test -S "$socket_path"
          "$HOME/hw1ai/bin/python" - "$socket_path" <<'PY'
import socket
import sys

path = sys.argv[1]
expected = b"evenai requires a firmware-issued exchange ID\n"
client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.settimeout(3.0)
try:
    client.connect(path)
    client.sendall(b"evenai\n")
    response = b""
    while not response.endswith(b"\n") and len(response) < 512:
        chunk = client.recv(512 - len(response))
        if not chunk:
            break
        response += chunk
finally:
    client.close()
if response != expected:
    raise SystemExit(f"unexpected control-socket response: {response!r}")
print("ControlSocket=responsive")
PY
          echo "JournalReadiness=unavailable"
          echo "HOST CONTROL PLANE GREEN"
          echo "APPLICATION READINESS REQUIRES: run 'cm5 status' twice from another authenticated XIAO interface"
        fi
        echo "POST-DEPLOY HOST CHECKS GREEN"
    '
fi
