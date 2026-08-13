# Tool surface for the NEW session runner only. Sourced, not executed.
#
# ⚠️ NOT FOR THE LEGACY LOOPS. prompts/fast_loop.md, risk_review.md and
# newsletter.md all drive `python3 scripts/*.py` through Bash. Sourcing this in
# those runners removes the tool they depend on and they fail in the direction
# that looks like a quiet day. The new session needs no shell: everything it
# needs is an MCP tool.
#
# `--tools ""` disables EVERY built-in tool (`claude --help`: 'Use "" to disable
# all tools'). Allowlist by construction, not a denylist: no Bash, Read, Write,
# Edit or WebFetch exists, so the surface cannot silently grow when a new
# built-in ships.
#
# `--permission-mode dontAsk` auto-DENIES anything unlisted instead of
# prompting. Headless there is nobody to answer, and `default` would hang until
# the timeout — a silent no-trade day.
#
# No `set -euo pipefail` here. This file is SOURCED, not executed — a `set`
# here changes the CALLING shell's options too, which is a side effect on a
# process we don't own (and unlike run_fast_loop.sh etc., this has no `cd` of
# its own to make that trade-off for). The empty-discovery guard below is an
# explicit `if`, so it does not depend on `-e` anyway — and `-e` would not
# have covered the python failure mode either: with `mapfile -t X < <(cmd)`,
# `-e` sees mapfile's own exit status, not the process-substituted command's,
# so a crashing `session_tools.py --print` was never caught by `-e` here.
#
# Repo root resolved from this file's own path, not `$0` (unreliable when
# sourced) and not `cd` (would move the caller's shell). Absolute paths from
# here on so this is correct regardless of the caller's cwd.
_session_tools_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

# Derived, never hardcoded — see scripts/session_tools.py for the incident that
# rule comes from.
mapfile -t _AGENTIC_TOOLS < <("${_session_tools_root}/.venv/bin/python" "${_session_tools_root}/scripts/session_tools.py" --print)
if [ "${#_AGENTIC_TOOLS[@]}" -eq 0 ]; then
  echo "REFUSING: MCP tool discovery returned nothing" >&2
  exit 1
fi

# Robinhood is listed explicitly: it is the ONLY execution venue, and the list is
# a deliberate 10 of its 53 tools. place_option_order is absent, so options are
# unreachable rather than merely forbidden.
_RH_TOOLS=(
  "mcp__robinhood-trading__get_accounts"
  "mcp__robinhood-trading__get_equity_positions"
  "mcp__robinhood-trading__get_portfolio"
  "mcp__robinhood-trading__get_equity_quotes"
  "mcp__robinhood-trading__get_equity_orders"
  "mcp__robinhood-trading__get_realized_pnl"
  "mcp__robinhood-trading__get_pnl_trade_history"
  "mcp__robinhood-trading__review_equity_order"
  "mcp__robinhood-trading__place_equity_order"
  "mcp__robinhood-trading__cancel_equity_order"
)

SESSION_TOOL_ARGS=(
  # ⚠️ --settings IS LOAD-BEARING AND WAS MISSING. The PreToolUse order gate
  # (scripts/hooks/pretooluse_order_gate.py) is declared ONLY in
  # deploy/loop_settings.json. Without this flag a session runs with the
  # project settings, which carry no `hooks` key at all -- so every order goes
  # STRAIGHT to the broker with no kill-switch, halt-entries, order-cap or
  # shadow-mode check. The lockdown looked complete and the gate did not bind.
  --settings "${_session_tools_root}/deploy/loop_settings.json"
  # ⚠️ THE MCP SURFACE IS DECLARED, NOT INHERITED — AND THIS IS A CONTEXT
  # BUDGET, NOT A GUARDRAIL. A headless `claude -p` otherwise loads the
  # USER-level MCP config (/root/.claude.json) and every claude.ai account
  # connector on top of this repo's .mcp.json. On 2026-08-13 the 10:35 open
  # session died with "Autocompact is thrashing" having loaded claude.ai
  # Airtable, Cryptoquant docs, Hugging Face, Interactive Brokers and Langfuse:
  # ~54KB of tool definitions and instruction blocks it could never call, on top
  # of a 29.6KB brief and a 36KB research_log. Three compacts in three turns and
  # the CLI killed the session. Nothing was mis-traded -- the allowlist below
  # never contained another broker -- but a connected server costs context
  # whether or not its tools are permitted, and that cost grew every time a
  # connector was added to the account.
  #
  # --strict-mcp-config makes --mcp-config the ONLY source. Verified 2026-08-13
  # under strict mode: agentic-trader up, robinhood-trading OAuth still resolves
  # (it lives in the CLI credential store, not the config file), the moomoo-fed
  # `quote` path still returns live snapshot data, and zero connector
  # instructions reach the transcript.
  --strict-mcp-config
  --mcp-config "${_session_tools_root}/deploy/session_mcp.json"
  --tools ""
  --permission-mode dontAsk
  # ⛔ --allowedTools IS VARIADIC AND MUST STAY LAST. It swallows every
  # following argument as a tool name -- that is how the brief once became a
  # 47th "tool" and no session could start at all.
  --allowedTools "${_AGENTIC_TOOLS[@]}" "${_RH_TOOLS[@]}"
)
