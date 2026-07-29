#!/usr/bin/env bash
# Slow loop — pure Python, no Claude, no tokens. Fetch prices, rank, write the
# target book to the Research Store. Safe to run unattended.
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/alert.sh "slow loop" "logs/slow.log"   # phone alert if this run dies
mkdir -p logs
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
# fetch_prices now sources bars from moomoo, whose SDK exists ONLY under system
# python3 (3.10) — never the .venv (3.12). Same split as run_universe_refresh.sh.
# `--force` is gone: the panel is appended to, not re-pulled (moomoo caps history
# at 100 distinct stocks, so a 168-name re-pull cannot succeed).
/usr/bin/python3 scripts/fetch_prices.py
"$PY" scripts/slow_loop.py
# mirror the non-regenerable ledger off-box (best-effort; never fails the run)
deploy/backup_ledger.sh || true
