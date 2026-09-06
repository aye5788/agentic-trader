#!/usr/bin/env bash
# WEEKLY universe liquidity refresh (Piece 1). Runs under /usr/bin/python3 because
# the moomoo SDK lives there. APPLIES the rescreen unattended; the only outcome
# that changes nothing is a data-integrity failure (incomplete feed / short
# pond / unverifiable add), which self-clears on the next healthy run. There
# is NO human approval step any more — see universe_maint.classify. Never
# touches the live trading path: it rescreens the CANDIDATE POOL the agent
# selects from, it does not trade.
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
#
# ⚠️ RUNTIME DEPENDENCY (2026-09-06): with `screen_backend = "v2_turnover"` the
# refresh shells out to scripts/v2_screen.py under ./v2env, because moomoo's V2
# decoder needs protobuf < 5 while the rest of the box runs 7.35.1. v2env/ is
# GIT-IGNORED, so a rebuilt droplet has none and the screen would report
# NO_CHANGE every week. Rebuild it with deploy/setup_v2env.sh. The pre-flight
# below states that plainly rather than leaving a future operator to infer it
# from a weekly "changed nothing" push.
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/alert.sh "universe refresh" "logs/universe.log"   # phone alert if this run dies
mkdir -p logs
if ! grep -q 'screen_backend *= *"legacy_v1"' config/strategy*.toml 2>/dev/null; then
  if [ ! -x v2env/bin/python ]; then
    echo "MISSING V2 RUNTIME: ./v2env/bin/python not found."
    echo "  The weekly liquidity screen needs it (protobuf < 5; see"
    echo "  deploy/v2env-requirements.txt). Rebuild:  deploy/setup_v2env.sh"
    echo "  The universe is UNCHANGED until then — no rotation has been applied."
    exit 1
  fi
fi
/usr/bin/python3 scripts/universe_refresh.py --run
