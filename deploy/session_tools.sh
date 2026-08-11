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
  --tools ""
  --permission-mode dontAsk
  --allowedTools "${_AGENTIC_TOOLS[@]}" "${_RH_TOOLS[@]}"
)
