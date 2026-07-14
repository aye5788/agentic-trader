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
claude -p --model claude-opus-4-8 "$(cat prompts/risk_review.md)"
