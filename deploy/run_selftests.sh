#!/usr/bin/env bash
# Run every in-repo --selftest under the interpreter it actually needs.
#
# TWO runtimes, and they are NOT interchangeable:
#   - .venv/bin/python (3.12) has tomllib (stdlib 3.11+) and every repo dep.
#     Most modules need this.
#   - system /usr/bin/python3 (3.10) is the ONLY place the moomoo SDK is
#     installed. A module that `import moomoo`s at module scope (currently
#     only scripts/market_monitor.py) CANNOT run under .venv — it fails with
#     ModuleNotFoundError before a single assertion runs, which used to look
#     like a broken test when it was just the wrong interpreter. Conversely,
#     system python3 has no tomllib, so anything reaching strategy.py (via
#     src/mandate.py etc.) must NOT be run there.
#   Some modules (scripts/universe_refresh.py) import
#   moomoo lazily inside a function body, not at module scope, so their
#   --selftest never touches it and they run fine under .venv.
#
# Usage:  deploy/run_selftests.sh        (exit 0 = all passed)
set -euo pipefail

cd "$(dirname "$0")/.."
PY_VENV="./.venv/bin/python"
PY_SYS="/usr/bin/python3"

if [[ ! -x "$PY_VENV" ]]; then
    echo "FAIL: $PY_VENV not found — create the venv first (python3.12 -m venv .venv)" >&2
    exit 2
fi
if [[ ! -x "$PY_SYS" ]]; then
    echo "FAIL: $PY_SYS not found — moomoo-importing selftests cannot run" >&2
    exit 2
fi

# Scripts that expose a --selftest entrypoint and run under the project venv.
VENV_SELFTESTS=(
    "scripts/universe_refresh.py"
    "src/concentration.py"
    "src/ti_signals.py"
    "src/deployed.py"
    "src/health.py"
    "scripts/health_check.py"
    "src/repo_checks.py"
    "src/mandate.py"
    "src/governance.py"
    "src/marks.py"
    "scripts/record_fills.py"
    "scripts/record_exit_outcome.py"
    "scripts/record_rotation_outcome.py"
    "scripts/record_partial_outcome.py"
    "src/charter.py"
    "src/announce.py"
    "src/notify.py"
    "src/integrity.py"
    "src/session_lock.py"
    "src/agent_env/live.py"
    "src/agent_env/memory.py"
    "src/agent_env/wakes.py"
    "src/agent_env/state.py"
    "src/agent_env/screen.py"
    "src/agent_env/terrain.py"
    "src/agent_env/decide.py"
    "src/agent_env/server.py"
    "scripts/session_tools.py"
    "scripts/session.py"
    "scripts/review_session.py"
    "scripts/score_reviews.py"
    # ⚠️ slow_loop has HAD a _selftest() since it was written and was never in
    # this list, so it ran only when someone invoked it by hand -- which is to
    # say, effectively never. Its regime-off case pins the line that liquidated
    # eleven positions on 2026-07-27; a pinned regression that nothing runs is
    # not pinned. Both are cheap: --selftest returns before any I/O.
    "scripts/slow_loop.py"
    "scripts/hooks/pretooluse_order_gate.py"
    # The remedy paired to src/deployed.py's detector. Its selftest pins the one
    # rule that matters: the stop watcher is refused mid-sale and ONLY mid-sale.
    "scripts/reload_stale.py"
    # ⚠️ Same omission as slow_loop above: letter_facts has had a _selftest()
    # and was never listed, so it ran only by hand. It is the sole producer of
    # every number in the investor letter — the one artifact that reaches the
    # account owner as fact.
    "scripts/letter_facts.py"
    "src/level_rules.py"
    "src/excursion.py"
    "src/history.py"
    # The nightly re-rank's durable output + its pure diff (2026-08-20). Listed
    # from the start, deliberately: the two omissions flagged above (slow_loop,
    # letter_facts) both shipped selftests that then ran only by hand.
    "src/rank_history.py"
    "src/residual.py"
)

# Scripts that `import moomoo` at module scope — MUST run under system python3.
SYS_SELFTESTS=(
    "scripts/market_monitor.py"
    "scripts/adjust_splits.py"
    "src/adapters/moomoo/prices.py"
)

fail=0

for s in "${VENV_SELFTESTS[@]}"; do
    echo "=== $s --selftest (.venv) ==="
    if "$PY_VENV" "$s" --selftest; then
        echo "  PASS"
    else
        echo "  FAIL ($s)" >&2
        fail=1
    fi
    echo
done

for s in "${SYS_SELFTESTS[@]}"; do
    echo "=== $s --selftest (system python3) ==="
    if "$PY_SYS" "$s" --selftest; then
        echo "  PASS"
    else
        echo "  FAIL ($s)" >&2
        fail=1
    fi
    echo
done

# src/research_store/validate.py uses a relative import (`from .models import
# ...`), so it cannot be invoked as a standalone script — it must be imported
# as a package member.
echo "=== src/research_store/validate.py _selftest() (.venv) ==="
if "$PY_VENV" -c "import sys; sys.path.insert(0,'src'); from research_store import validate; validate._selftest()"; then
    echo "  PASS"
else
    echo "  FAIL (src/research_store/validate.py)" >&2
    fail=1
fi
echo

if [[ "$fail" -ne 0 ]]; then
    echo "SELFTESTS FAILED" >&2
    exit 1
fi
echo "ALL SELFTESTS PASSED"
