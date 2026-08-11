#!/usr/bin/env bash
# Fast loop — headless Claude (RH is MCP-only). Reads the target book, fetches
# live account state, and places the APPROVED plan (only if live_approved=true).
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/alert.sh "fast loop" "logs/fast.log"   # phone alert if this run dies

# Footgun guard (docs/DESIGN.md): a stray ANTHROPIC_API_KEY silently switches
# billing from the subscription to per-token. Refuse to run if it's set.
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "REFUSING: ANTHROPIC_API_KEY is set — would bill per-token. Unset it." >&2
  exit 1
fi

# Claude auth: either an interactive login stored in ~/.claude.json (run `claude`
# + `/mcp` once) OR a CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`. Both work
# headless; we don't require the token so an existing login is fine.

mkdir -p logs
# Model pinned explicitly: don't ride the box default — if the default model is
# ever retired/renamed, cron must keep trading on a known-available model.
# ⚠️ --settings deploy/loop_settings.json is LOAD-BEARING. Without it this loop
# runs under .claude/settings.json, which is deliberately permissive so a human
# can develop in this repo. The lockdown -- no edits to src/, scripts/, config/,
# deploy/, prompts/ or the ledger -- lives ONLY in the loop settings file.
#
# ⚠️ Those rules are spelled Edit(...), never Write(...). Claude Code does not
# match a path-scoped Write() rule against file tools and says so on every run;
# a settings file full of Write() denies enforces nothing while looking locked.
# Verified by live probe 2026-08-11.
claude -p --model claude-opus-5 --settings deploy/loop_settings.json "$(cat prompts/fast_loop.md)"
# Equity logging runs on its OWN post-close 16:15 ET cron entry (scripts/log_equity.py),
# not here — this loop fires ~10:02 ET, 32 minutes after the open, the noisiest
# stretch of the session; mandate.drawdown() must see close-to-close marks only.
# mirror the non-regenerable ledger off-box (best-effort; never fails the run)
deploy/backup_ledger.sh || true
