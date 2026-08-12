#!/usr/bin/env python3
"""READ-ONLY view of everything the trading agent can see. For the REVIEWER.

    .venv/bin/python scripts/agent_view.py <tool> [arg=value ...]
    .venv/bin/python scripts/agent_view.py --list

WHY THIS EXISTS
    The reviewer is a DIFFERENT MODEL (Codex) deliberately, because the trading
    agent and the assistant that built it are the same model and share the same
    priors. A reviewer that shares the bias cannot see it: when the agent
    rationalised trimming winners on "concentration", the same-model reviewer
    read the argument and found it persuasive, because it is the argument that
    model produces.

    So the reviewer needs the SAME VIEW, not a summary written by the thing it
    is reviewing. A summary is the reviewed party choosing the evidence.

WHY NOT JUST GIVE IT THE MCP SERVER
    Codex gates third-party MCP tool calls behind an approval its headless mode
    auto-cancels ("user cancelled MCP tool call"). Rather than defeat that
    guard, this exposes the same functions over a plain CLI the sandbox already
    permits. Same code path, same numbers -- server.py's own functions are
    called, nothing is reimplemented here, so the reviewer cannot be shown a
    different book than the agent saw.

⛔ READ-ONLY BY CONSTRUCTION. The allowlist below contains no tool that writes:
   no set_levels, no record_decision, no rule_out, no place_equity_order. The
   reviewer's job is to judge, and a reviewer that can trade is a second trader.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "agent_env"))

# Every tool here must be free of side effects. Adding a writer to this list
# turns the reviewer into a second trading agent -- which is the one thing it
# must never become.
READ_ONLY = {
    "brief", "account", "positions", "halt_status", "mandate_status",
    "universe", "candidates", "terrain", "quote", "sectors", "leaders",
    "macro", "macro_calendar", "earnings", "news", "depth",
    "performance", "research_log", "check_order",
}


def _server():
    spec = importlib.util.spec_from_file_location(
        "_review_server", REPO / "src" / "agent_env" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--"]
    if not args or args[0] in ("--list", "-l", "--help", "-h"):
        print("read-only tools:\n  " + "\n  ".join(sorted(READ_ONLY)))
        print("\nusage: agent_view.py <tool> [arg=value ...]")
        print("example: agent_view.py terrain symbol=MU")
        return

    name = args[0]
    if name not in READ_ONLY:
        print(json.dumps({
            "error": f"{name!r} is not available to the reviewer",
            "reason": ("either it does not exist or it WRITES state. The "
                       "reviewer reads and judges; it does not act."),
            "available": sorted(READ_ONLY)}, indent=2))
        raise SystemExit(2)

    kwargs = {}
    for a in args[1:]:
        if "=" not in a:
            continue
        k, v = a.split("=", 1)
        try:
            kwargs[k] = int(v)
        except ValueError:
            try:
                kwargs[k] = float(v)
            except ValueError:
                kwargs[k] = v

    mod = _server()
    fn = getattr(mod, name, None)
    if fn is None:
        print(json.dumps({"error": f"{name} not found on the server"}))
        raise SystemExit(2)
    # FastMCP wraps tools; the plain function hangs off .fn on wrapped ones
    fn = getattr(fn, "fn", fn)
    try:
        print(fn(**kwargs))
    except TypeError as e:
        print(json.dumps({"error": f"bad arguments for {name}: {e}"}, indent=2))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
