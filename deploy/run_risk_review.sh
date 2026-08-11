#!/usr/bin/env bash
# Intraday risk review — headless Claude (RH is MCP-only). Reads holdings +
# live quotes/regime signals, applies the one-way de-risk invariant, and (if
# a stricter-only override is warranted) writes it to the Research Store.
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/alert.sh "risk review" "logs/risk_review.log"   # phone alert if this run dies

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
# ever retired/renamed, cron must keep reviewing on a known-available model.
# ⚠️ --settings deploy/loop_settings.json is LOAD-BEARING. Without it this loop
# runs under .claude/settings.json, which is deliberately permissive so a human
# can develop in this repo. The lockdown -- no edits to src/, scripts/, config/,
# deploy/, prompts/ or the ledger -- lives ONLY in the loop settings file.
#
# ⚠️ Those rules are spelled Edit(...), never Write(...). Claude Code does not
# match a path-scoped Write() rule against file tools and says so on every run;
# a settings file full of Write() denies enforces nothing while looking locked.
# Verified by live probe 2026-08-11.
claude -p --model claude-opus-5 --settings deploy/loop_settings.json "$(cat prompts/risk_review.md)"
