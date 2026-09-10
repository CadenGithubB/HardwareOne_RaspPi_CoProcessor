#!/bin/bash
# Prove that the production llama listener belongs to the supervised model unit.

set -Eeuo pipefail
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

[[ $# -eq 6 ]] || { printf 'usage: wait_model_ready UNIT SERVER MODEL PORT CONTEXT TIMEOUT\n' >&2; exit 2; }
unit=$1
server=$2
model=$3
port=$4
context=$5
timeout=$6
[[ $unit == hw1-openclaw-model.service ]] || { printf 'unexpected unit\n' >&2; exit 2; }
[[ $server == /* && $model == /* ]] || { printf 'server/model paths must be absolute\n' >&2; exit 2; }
[[ $port =~ ^[1-9][0-9]{0,4}$ ]] && ((10#$port <= 65535)) || { printf 'invalid port\n' >&2; exit 2; }
[[ $context =~ ^[1-9][0-9]{3,5}$ ]] || { printf 'invalid context\n' >&2; exit 2; }
[[ $timeout =~ ^[1-9][0-9]{0,3}$ ]] || { printf 'invalid timeout\n' >&2; exit 2; }
[[ ${LLAMA_API_KEY:-} =~ ^[0-9a-f]{64}$ ]] || { printf 'missing or malformed LLAMA_API_KEY\n' >&2; exit 2; }

deadline=$((SECONDS + 10#$timeout))
while ((SECONDS < deadline)); do
  pid="$(/usr/bin/systemctl show --property MainPID --value "$unit" 2>/dev/null || true)"
  if [[ $pid =~ ^[1-9][0-9]*$ && -r /proc/$pid/cmdline && -r /proc/$pid/cgroup ]]; then
    if /usr/bin/python3 -I - "$pid" "$server" "$model" "$port" "$context" <<'PY'
import os
import pathlib
import sys

pid, server, model, port, context = sys.argv[1:]
proc = pathlib.Path("/proc") / pid
if pathlib.Path(os.readlink(proc / "exe")).resolve(strict=True) != pathlib.Path(server).resolve(strict=True):
    raise SystemExit(1)
expected = [
    server, "--model", model, "--alias", "hw1-openclaw-local", "--host", "127.0.0.1",
    "--port", port, "-t", "4", "-c", context, "--parallel", "1", "--cache-reuse", "256", "--jinja",
]
actual = (proc / "cmdline").read_bytes().rstrip(b"\0").decode("utf-8").split("\0")
if actual != expected:
    raise SystemExit(1)
if "/hw1-openclaw-model.service" not in (proc / "cgroup").read_text(encoding="ascii"):
    raise SystemExit(1)
expected_uid = int(__import__("pwd").getpwnam("openclaw-model").pw_uid)
status = (proc / "status").read_text(encoding="ascii")
uid_line = next((line for line in status.splitlines() if line.startswith("Uid:")), "")
if not uid_line or {int(value) for value in uid_line.split()[1:]} != {expected_uid}:
    raise SystemExit(1)
PY
    then
      listeners="$(/usr/bin/ss -H -ltnp "sport = :$port" 2>/dev/null || true)"
      if [[ -n $listeners ]] && ! grep -Evq '127\.0\.0\.1:' <<<"$listeners" &&
        grep -Fq "pid=$pid," <<<"$listeners" &&
        /usr/bin/curl --disable --fail --silent --show-error --max-time 2 --max-filesize 4096 \
          --output /dev/null "http://127.0.0.1:$port/health" &&
        /usr/bin/python3 -I - "$port" <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

url = f"http://127.0.0.1:{int(sys.argv[1])}/tokenize"
payload = json.dumps({"content": "auth-probe"}).encode("utf-8")
plain = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(plain, timeout=2)
except urllib.error.HTTPError as exc:
    if exc.code != 401:
        raise SystemExit(1)
else:
    raise SystemExit(1)
key = os.environ["LLAMA_API_KEY"]
authorized = urllib.request.Request(
    url,
    data=payload,
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
)
with urllib.request.urlopen(authorized, timeout=2) as response:
    if not 200 <= response.status < 300:
        raise SystemExit(1)
    body = response.read(65537)
if len(body) > 65536:
    raise SystemExit(1)
value = json.loads(body)
if not isinstance(value, dict) or not isinstance(value.get("tokens"), list):
    raise SystemExit(1)
PY
      then
        exit 0
      fi
    fi
  fi
  sleep 1
done

printf 'model readiness/identity proof timed out after %ss\n' "$timeout" >&2
exit 1
