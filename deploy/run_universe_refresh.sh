#!/usr/bin/env bash
# WEEKLY universe liquidity refresh (Piece 1). Runs under /usr/bin/python3 because
# the moomoo SDK lives there. Auto-applies routine changes; HOLDs + alerts on
# seed-drops/anomalies. Never touches the live trading path — it rescreens the
# CANDIDATE POOL the agent selects from, it does not trade.
#
# ⚠️ CADENCE CHANGED 2026-08-20: quarterly -> WEEKLY, Fridays.
# The old schedule was `0 19 1-7 1,4,7,10 *` here in cron PLUS a
# `[ "$(date +%u)" -eq 7 ]` guard in this file — the real cadence (first Sunday
# of Jan/Apr/Jul/Oct) was spelled in two languages and written plainly nowhere.
# It had never once fired since being armed 2026-07-20.
#
# THE DAY GUARD IS GONE FROM THIS FILE ON PURPOSE. It now lives in
# `[universe_maintenance] screen_day` and is enforced by the Python
# (universe_maint.screen_due), so the cadence has ONE definition that a reader
# and a test can both reach. Cron fires this every Friday; if the cron line and
# the config ever disagree, the config wins and the script says so and exits 0.
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/alert.sh "universe refresh" "logs/universe.log"   # phone alert if this run dies
mkdir -p logs
/usr/bin/python3 scripts/universe_refresh.py --run
