#!/usr/bin/env bash
# Fast loop — headless Claude (RH is MCP-only). Reads the target book, fetches
# live account state, and places the APPROVED plan (only if live_approved=true).
set -euo pipefail
cd "$(dirname "$0")/.."

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
claude -p "$(cat prompts/fast_loop.md)"
