#!/usr/bin/env bash
# Run every in-repo --selftest under the project venv.
#
# Why this exists: the scripts need Python 3.11+ (tomllib) and the venv's deps.
# Invoking them with a bare `python3` (system 3.10) fails on `import tomllib` and
# looks like a broken test when it's just the wrong interpreter. This pins the
# venv Python so a pass means the logic is sound, not that the env happened to fit.
#
# Usage:  deploy/run_selftests.sh        (exit 0 = all passed)
set -euo pipefail

cd "$(dirname "$0")/.."
PY="./.venv/bin/python"

if [[ ! -x "$PY" ]]; then
    echo "FAIL: $PY not found — create the venv first (python3.12 -m venv .venv)" >&2
    exit 2
fi

# Scripts that expose a --selftest entrypoint. Add new ones here.
SELFTESTS=(
    "scripts/market_monitor.py"
    "scripts/fast_loop.py"
    "scripts/universe_refresh.py"
    "src/concentration.py"
    "src/ti_signals.py"
)

fail=0
for s in "${SELFTESTS[@]}"; do
    echo "=== $s --selftest ==="
    if "$PY" "$s" --selftest; then
        echo "  PASS"
    else
        echo "  FAIL ($s)" >&2
        fail=1
    fi
    echo
done

if [[ "$fail" -ne 0 ]]; then
    echo "SELFTESTS FAILED" >&2
    exit 1
fi
echo "ALL SELFTESTS PASSED"
