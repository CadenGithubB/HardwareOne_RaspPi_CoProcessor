#!/usr/bin/env bash
# One-command local quality gate for the cm5 AI service.
#
#   ./run_checks.sh          # bootstrap/reuse .venv-dev, then: compile, lint, test
#   ./run_checks.sh --fast   # skip the pip dependency sync (venv already good)
#
# Deliberately deps-only (no editable install): tests/conftest.py puts the
# source dir on sys.path, so installing the package would only add an
# *.egg-info to the tree that rsync could then leak to the Pi.
set -euo pipefail
cd "$(dirname "$0")"

VENV=.venv-dev
PY="$VENV/bin/python"
DEPS=(pyserial httpx PyYAML pytest pytest-xdist ruff)

if [ ! -x "$PY" ]; then
    echo "== bootstrap: creating $VENV"
    python3 -m venv "$VENV"
    "$PY" -m pip install --quiet --upgrade pip
fi
if [ "${1:-}" != "--fast" ]; then
    echo "== deps: ${DEPS[*]}"
    "$PY" -m pip install --quiet "${DEPS[@]}"
fi

echo "== compile check"
PYTHONDONTWRITEBYTECODE=1 "$PY" -m compileall -q hw1_ai_service tools tests

# Bug-class rules only (syntax errors, undefined names, misused f-strings):
# a red gate here is always a real defect, never a style opinion. Tighten
# deliberately later if wanted.
echo "== ruff (E9,F63,F7,F82)"
"$PY" -m ruff check --select E9,F63,F7,F82 hw1_ai_service tools tests

echo "== pytest (parallel)"
PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest -q -p no:cacheprovider -n auto

echo "== ALL CHECKS GREEN"
