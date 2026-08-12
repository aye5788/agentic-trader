#!/usr/bin/env bash
# Agent SESSION runner — the inversion's cron entry point.
#
# The legacy loops (run_fast_loop.sh etc.) hand a fixed PROCEDURE to a headless
# Claude. This runs a SESSION: the agent gets the charter and decides. All of
# the supervision — the lock, the brief, the process group, the integrity
# tripwire, the verdict — lives in scripts/session.py, which is testable. This
# file is the thin cron shim around it and should stay thin.
#
#   deploy/run_session.sh <premarket|open|close|wake>
#
# ⚠️ DO NOT source deploy/session_tools.sh here. That file is consumed by
# session.py, which builds the argv itself. Sourcing it in a shell that also
# runs python would export nothing useful and invites the two definitions to
# drift.
set -euo pipefail
cd "$(dirname "$0")/.."

MODE="${1:-}"
case "$MODE" in
  premarket|open|close|wake) ;;
  *) echo "usage: $0 <premarket|open|close|wake>" >&2; exit 2 ;;
esac

# ERR trap -> phone push. Note session.py exits NON-ZERO on a failed session, so
# a session that dies (529, auth, timeout) pages rather than passing silently —
# which is the whole point of classify().
source deploy/alert.sh "session:$MODE" "logs/session.log"

# Footgun guard (docs/DESIGN.md): a stray ANTHROPIC_API_KEY silently switches
# billing from the subscription to per-token. Refuse to run if it's set.
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "REFUSING: ANTHROPIC_API_KEY is set — would bill per-token. Unset it." >&2
  exit 1
fi

mkdir -p logs

# ⚠️ .venv python, NOT /usr/bin/python3. session.py imports charter/strategy/
# mandate (tomllib + the repo's own modules) and never imports moomoo — quotes
# reach it through the MCP server, which runs its own interpreter.
.venv/bin/python scripts/session.py "$MODE"
rc=$?

# mirror the non-regenerable ledger off-box (best-effort; never fails the run)
deploy/backup_ledger.sh || true

exit "$rc"
