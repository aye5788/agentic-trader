#!/usr/bin/env bash
# Weekly investor letter ("The Claude Ledger") — deterministic facts, headless
# Claude narrative, deterministic email. Sundays after the slow loop.
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/alert.sh "weekly letter" "logs/newsletter.log"   # phone alert if this run dies

# Footgun guard (docs/DESIGN.md): a stray ANTHROPIC_API_KEY silently switches
# billing from the subscription to per-token. Refuse to run if it's set.
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "REFUSING: ANTHROPIC_API_KEY is set — would bill per-token. Unset it." >&2
  exit 1
fi

mkdir -p logs
# 1. numbers (pure Python — the letter may not invent figures)
.venv/bin/python scripts/letter_facts.py
# 2. narrative (headless Claude fills newsletter/template.html; model pinned —
#    see run_fast_loop.sh)
claude -p --model claude-opus-4-8 "$(cat prompts/newsletter.md)"
# 3. delivery (no-op with a notice until NEWSLETTER_* creds exist in .env)
.venv/bin/python scripts/send_newsletter.py || true
