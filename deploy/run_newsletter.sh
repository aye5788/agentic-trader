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
# ⚠️ --settings deploy/loop_settings.json is LOAD-BEARING. Without it this loop
# runs under .claude/settings.json, which is deliberately permissive so a human
# can develop in this repo. The lockdown -- no edits to src/, scripts/, config/,
# deploy/, prompts/ or the ledger -- lives ONLY in the loop settings file.
#
# ⚠️ Those rules are spelled Edit(...), never Write(...). Claude Code does not
# match a path-scoped Write() rule against file tools and says so on every run;
# a settings file full of Write() denies enforces nothing while looking locked.
# Verified by live probe 2026-08-11.
claude -p --model claude-opus-5 --settings deploy/loop_settings.json "$(cat prompts/newsletter.md)"
# 3. delivery (no-op with a notice until NEWSLETTER_* creds exist in .env)
.venv/bin/python scripts/send_newsletter.py || true
