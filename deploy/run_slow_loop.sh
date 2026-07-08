#!/usr/bin/env bash
# Slow loop — pure Python, no Claude, no tokens. Fetch prices, rank, write the
# target book to the Research Store. Safe to run unattended.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
"$PY" scripts/fetch_prices.py --force
"$PY" scripts/slow_loop.py
