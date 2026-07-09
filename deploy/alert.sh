#!/usr/bin/env bash
# Sourced by the run_*.sh wrappers (after cd to repo root): arms an ERR trap
# that pushes a phone alert — same ntfy channel as the monitor's stop/target
# alerts — when a cron run dies. Without this, cron failures rot silently in
# log files. No NTFY_TOPIC in .env -> no-op.
#
#   source "$(dirname "$0")/alert.sh" "<loop name>" "<log path>"
_alert_name="${1:-run}"
_alert_log="${2:-logs/}"

notify_fail() {
  set +e   # we run under the caller's `set -e`; a failure HANDLER must not abort
  topic=$(grep -E '^NTFY_TOPIC=' .env 2>/dev/null | head -1 | cut -d= -f2)
  [ -n "$topic" ] || return 0
  server=$(grep -E '^NTFY_SERVER=' .env 2>/dev/null | head -1 | cut -d= -f2)
  curl -fsS -m 10 \
    -H "Title: agentic-trader: ${_alert_name} FAILED" \
    -H "Priority: high" -H "Tags: x" \
    -d "$(date '+%F %T') — check ${_alert_log} on the droplet" \
    "${server:-https://ntfy.sh}/${topic}" >/dev/null 2>&1 || true
}
trap notify_fail ERR
