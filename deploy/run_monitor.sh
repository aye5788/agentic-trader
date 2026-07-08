#!/usr/bin/env bash
# Intraday exit monitor — long-running; self-gates to 09:30–16:00 ET. Watches
# live prices vs. stored stops/targets and fires the exit executor on a breach.
set -euo pipefail
cd "$(dirname "$0")/.."

# Footgun guard: a stray ANTHROPIC_API_KEY would bill the breach-time claude
# calls per-token instead of the subscription.
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "REFUSING: ANTHROPIC_API_KEY is set — would bill per-token. Unset it." >&2
  exit 1
fi

mkdir -p logs
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
exec "$PY" scripts/market_monitor.py
