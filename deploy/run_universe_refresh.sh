#!/usr/bin/env bash
# Quarterly universe liquidity refresh (Piece 1). Runs under /usr/bin/python3
# because the moomoo SDK lives there. Auto-applies routine changes; HOLDs + alerts
# on seed-drops/anomalies. Never touches the live trading path.
set -euo pipefail
cd /opt/agentic-trader
exec /usr/bin/python3 scripts/universe_refresh.py --run
