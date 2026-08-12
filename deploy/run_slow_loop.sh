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

# Re-base the panel for any split that landed today, BEFORE the signal reads it.
# The panel stores RAW closes, so a split enters as a genuine one-day return
# (MNST's 2:1 on 2026-08-11 read as -50.2%) and momentum's R, sigma and trend
# are all computed off those returns -- sigma being what sets stop distance and
# target geometry, so it reaches real orders, not just the ranking. Detection is
# free and local; only split-SHAPED moves cost a get_rehab call, and nothing is
# adjusted without positive confirmation. Never fails the loop: an unadjusted
# split is a bad score for one name, a dead slow loop is no book at all.
/usr/bin/python3 scripts/adjust_splits.py || true
"$PY" scripts/slow_loop.py
# mirror the non-regenerable ledger off-box (best-effort; never fails the run)
deploy/backup_ledger.sh || true
