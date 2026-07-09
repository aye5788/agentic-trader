#!/usr/bin/env bash
# Monday nag: Schwab's refresh token dies every 7 days and can't be renewed
# unattended — without the paste flow the price feed (slow loop + monitor)
# goes dark mid-week. Logs AND pushes to the phone (ntfy).
cd "$(dirname "$0")/.."
echo "$(date) ACTION NEEDED: run scripts/schwab_auth.py (7-day token)" >> logs/reauth.log
topic=$(grep -E '^NTFY_TOPIC=' .env 2>/dev/null | head -1 | cut -d= -f2)
[ -n "$topic" ] || exit 0
server=$(grep -E '^NTFY_SERVER=' .env 2>/dev/null | head -1 | cut -d= -f2)
curl -fsS -m 10 \
  -H "Title: Schwab re-auth due (7-day token)" \
  -H "Priority: high" -H "Tags: key" \
  -d "Run scripts/schwab_auth.py this week or the price feed (quotes, stops, ranking) dies." \
  "${server:-https://ntfy.sh}/${topic}" >/dev/null 2>&1 || true
