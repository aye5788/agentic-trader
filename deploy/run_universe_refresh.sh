#!/usr/bin/env bash
# Quarterly universe liquidity refresh (Piece 1). Runs under /usr/bin/python3
# because the moomoo SDK lives there. Auto-applies routine changes; HOLDs + alerts
# on seed-drops/anomalies. Never touches the live trading path.
#
# Cron fires this at 19:00 on days 1-7 of Jan/Apr/Jul/Oct with day-of-week left as
# * (to avoid cron's day-of-month OR day-of-week gotcha); the date guard below
# limits it to the FIRST SUNDAY of those months.
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/alert.sh "universe refresh" "logs/universe.log"   # phone alert if this run dies
mkdir -p logs
[ "$(date +%u)" -eq 7 ] || exit 0   # first Sunday only (cron already limits to days 1-7; %u: Mon=1..Sun=7)
/usr/bin/python3 scripts/universe_refresh.py --run
