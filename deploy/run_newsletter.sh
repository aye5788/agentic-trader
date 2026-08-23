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

# 2. narrative (headless Claude fills newsletter/template.html)
#
# ⛔ THIS PROCESS WRITES PROSE. ITS AUTHORITY OVER LIVE MONEY IS ZERO, AND
# EVERY FLAG BELOW IS WHAT MAKES THAT TRUE MECHANICALLY RATHER THAN BY ASKING
# THE MODEL NOT TO TRADE.
#
# It used to run as `claude -p --model ... --settings deploy/loop_settings.json
# "$(cat prompts/newsletter.md)"`, and everything not named was whatever the box
# happened to have. Two inheritances, both real:
#
#   THE SHARED SETTINGS FILE. deploy/loop_settings.json is the TRADING loop's
#   contract and it ALLOWS mcp__robinhood-trading__place_equity_order and
#   cancel_equity_order. The letter-writer held both. The PreToolUse order gate
#   in that file would not have stopped a sale: it allows every sell by design,
#   because refusing one would strip a fractional position of its only stop.
#
#   THE AMBIENT MCP CONFIG. With no MCP flags a headless `claude -p` loads the
#   user-level config and every claude.ai account connector. Enumerated from
#   this directory on 2026-08-23 with `claude mcp list`: 21 servers, among them
#   robinhood-trading (THE execution venue), agentic-trader, and three other
#   brokers with live trading surfaces — Interactive Brokers, webull,
#   Public.com. A newsletter had them mounted.
#
# The letter now gets its own contract, and the trading file is not passed to
# it at all. Flag by flag:
#
#   CLAUDE_CODE_DISABLE_CLAUDE_MDS / _AUTO_MEMORY  suppress instruction FILES
#       (repo CLAUDE.md and friends) and auto-memory. They are NOT what closes
#       plugin/settings inheritance — that is --setting-sources below — and the
#       pair only look redundant.
#   --setting-sources ""  drops the User, Project and Local settings sources, so
#       the human's ~/.claude/settings.json stops governing this run: its
#       `enabledPlugins` (Superpowers), its permissive Bash allowlist, its
#       effort level. It does NOT drop --settings below and cannot drop
#       enterprise policy — the CLI computes enabled sources as {what this flag
#       allows} ∪ {flagSettings, policySettings}.
#       ⛔ THE EMPTY STRING IS THE ARGUMENT AND MUST SURVIVE. It fails CLOSED if
#       lost: the flag takes exactly one value, so a dropped "" makes it eat the
#       next flag and the CLI refuses to start. A letter that will not launch is
#       loud; one that quietly re-inherits the box is not.
#   --settings            the letter's OWN capability contract — read the facts,
#       read the template, write ONE issue file, amend docs/OPSLOG.md. Nothing
#       else. See deploy/newsletter_settings.json.
#   --strict-mcp-config + --mcp-config   make deploy/newsletter_mcp.json the
#       only MCP source. It declares NO servers, so Robinhood and every other
#       broker are ABSENT from this process rather than merely unpermitted.
#   --tools "Read,Write,Edit,Bash"  restricts the BUILT-IN set to the four the
#       letter actually uses, so WebFetch, WebSearch and the rest do not exist
#       here. Comma-separated on purpose: --tools is variadic, and the four are
#       one argument. This selects which tools exist; it grants nothing — the
#       path and command scoping stays in the settings file where it can be read.
#       ⚠️ NOT `--allowedTools`, which is a PERMISSION grant: a bare built-in
#       name there is a BLANKET grant that overrides the narrow Bash(...) /
#       Write(...) rules (probed on this box; see scripts/market_monitor.py).
#   --permission-mode dontAsk  DECIDES instead of prompting. Headless there is
#       nobody to answer, and `default` would hang until the timeout — a Sunday
#       with no letter and no error.
#       ⚠️ IT IS NOT A PURE AUTO-DENY, AND THE OTHER RUNNERS' COMMENTS SAY IT IS.
#       Measured under this exact configuration on 2026-08-23: every WRITE and
#       every NETWORK command not on the allowlist was refused — `touch`, `curl`,
#       `rm research_store/newsletters/facts.json`, `cat .env`, writes outside
#       research_store/newsletters/ — but the CLI auto-approved some read-only
#       in-repo commands it classifies as safe (`git status --short`, `wc -l`)
#       while refusing others (`ls -l`, `git ls-files`). So the floor this mode
#       provides is 'cannot mutate and cannot reach the network', NOT 'cannot
#       run anything unlisted'. It costs the letter nothing — it has no broker
#       tool to reach and no egress — but do not build a guarantee on it.
#   --model               pinned, unchanged: the letter must not break because a
#       default model was retired.
#
# ⚠️ EFFORT IS NOT SET HERE, AND IT USED TO BE INHERITED. Until this change the
# letter ran at `"effortLevel": "high"` from ~/.claude/settings.json — the
# HUMAN's editor preference, picked up by the weekly letter because nothing
# stopped it. Closing the inheritance drops it to the model default. Nobody ever
# chose "high" for the newsletter, so it is not carried over silently; if it
# should be the letter's setting, say so and it becomes one reviewable line:
# `--effort high`. This isolation is a capability boundary, not a tuning change.
#
# ⛔ THE PROMPT GOES ON STDIN, NEVER IN ARGV. `--tools` and `--mcp-config` are
# both VARIADIC, so a trailing prompt argument can be swallowed as one more tool
# name and the CLI then exits 1 with "Input must be provided either through
# stdin or as a prompt argument" — a Sunday that writes no letter. (It also
# sidesteps MAX_ARG_STRLEN, 128KiB per argument; prompts/newsletter.md is 46KB
# and growing.) scripts/session.py and scripts/market_monitor.py record the same
# failure on their own paths; this one inherits the lesson rather than the bug.
CLAUDE_CODE_DISABLE_CLAUDE_MDS=1 \
CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 \
claude -p \
  --model claude-opus-5 \
  --setting-sources "" \
  --settings deploy/newsletter_settings.json \
  --strict-mcp-config \
  --mcp-config deploy/newsletter_mcp.json \
  --tools "Read,Write,Edit,Bash" \
  --permission-mode dontAsk \
  < prompts/newsletter.md

# 3. delivery (no-op with a notice until NEWSLETTER_* creds exist in .env)
.venv/bin/python scripts/send_newsletter.py || true
