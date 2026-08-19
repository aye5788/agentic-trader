#!/usr/bin/env bash
# Controlled recovery after the 2026-08-19 unexpected-order incident.
# This is invoked by a one-shot systemd timer, not by a trading schedule.
set -euo pipefail
cd "$(dirname "$0")/.."

HALT_FILE="research_store/HALT"

# Never resume if the operator has changed the file or removed/replaced it.
if [[ ! -e "$HALT_FILE" ]]; then
  echo "resume: HALT is already cleared; refusing duplicate resume"
  exit 0
fi

# The Codex review must remain disabled during this recovery.
if grep -Eq '^[[:space:]]*systemctl start --wait agentic-review\.service' deploy/run_session.sh; then
  echo "resume: refusing — Codex review is enabled in run_session.sh" >&2
  exit 1
fi

echo "resume: enabling monitor and session timers"
systemctl enable agentic-monitor.service agentic-session@open.timer agentic-session@close.timer
systemctl start agentic-monitor.service agentic-session@open.timer agentic-session@close.timer

# Remove the kill switch only after the services are enabled and started.
rm -f "$HALT_FILE"
echo "resume: trading services online; Codex review remains disabled"
