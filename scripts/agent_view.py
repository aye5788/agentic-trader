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


_SERVER = None


def _server():
    """Load server.py once per process, then reuse it.

    ⚠️ THIS IMPORT IS THE WHOLE COST. It pulls pandas and numpy (directly, and
    again via agent_env.screen -> momentum) plus the FastMCP/Pydantic stack:
    ~134 MB peak RSS, measured on this box 2026-08-14, for every invocation --
    even for the 13 of 19 tools that touch no dataframe at all.

    That was survivable at one call. On 2026-08-14 a review issued 120 of them
    concurrently and the box went from 1963 MB to 44 MB free during market
    hours, with a software-only stop watcher as the sole protection on a live
    book. The reviewer was killed to save the machine.

    Caching here is what makes --batch worth having: N calls in one process pay
    this once instead of N times.
    """
    global _SERVER
    if _SERVER is None:
        spec = importlib.util.spec_from_file_location(
            "_review_server", REPO / "src" / "agent_env" / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SERVER = mod
    return _SERVER


def _call(name: str, kwargs: dict) -> dict:
    """Run one allowlisted tool. -> {"tool","args","ok",...}. Never raises.

    A failure is returned as data rather than thrown, because in a batch one
    bad symbol must not discard the other thirty answers.
    """
    rec = {"tool": name, "args": kwargs}
    if name not in READ_ONLY:
        return {**rec, "ok": False,
                "error": f"{name!r} is not available to the reviewer",
                "reason": ("either it does not exist or it WRITES state. The "
                           "reviewer reads and judges; it does not act."),
                "available": sorted(READ_ONLY)}
    fn = getattr(_server(), name, None)
    if fn is None:
        return {**rec, "ok": False, "error": f"{name} not found on the server"}
    fn = getattr(fn, "fn", fn)          # FastMCP wraps tools; .fn is the plain one
    try:
        return {**rec, "ok": True, "result": fn(**kwargs)}
    except TypeError as e:
        return {**rec, "ok": False, "error": f"bad arguments for {name}: {e}"}
    except Exception as e:              # noqa: BLE001 — one bad call, not the batch
        return {**rec, "ok": False, "error": f"{type(e).__name__}: {e}"}


def _parse_batch(raw: str) -> list[tuple[str, dict]]:
    """Accept either shape, because both are natural to write:

        [["account", {}], ["terrain", {"symbol": "MU"}]]
        [{"tool": "account"}, {"tool": "terrain", "args": {"symbol": "MU"}}]
    """
    spec = json.loads(raw)
    if not isinstance(spec, list):
        raise ValueError("batch must be a JSON array of calls")
    out = []
    for item in spec:
        if isinstance(item, str):
            out.append((item, {}))
        elif isinstance(item, list) and item:
            out.append((item[0], item[1] if len(item) > 1 and item[1] else {}))
        elif isinstance(item, dict) and item.get("tool"):
            out.append((item["tool"], item.get("args") or {}))
        else:
            raise ValueError(f"unrecognised call in batch: {item!r}")
    return out


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--"]
    if not args or args[0] in ("--list", "-l", "--help", "-h"):
        print("read-only tools:\n  " + "\n  ".join(sorted(READ_ONLY)))
        print("\nusage: agent_view.py <tool> [arg=value ...]")
        print("example: agent_view.py terrain symbol=MU")
        print("\nBATCH (STRONGLY PREFERRED — one process, one pandas import):")
        print("  agent_view.py --batch '[[\"account\",{}],"
              "[\"terrain\",{\"symbol\":\"MU\"}],[\"terrain\",{\"symbol\":\"NVDA\"}]]'")
        print("  ... or pipe the same JSON on stdin: agent_view.py --batch -")
        print("  Returns a JSON array; each entry carries tool/args/ok and "
              "result-or-error.")
        print("  A single call costs ~134 MB (it imports pandas). A batch of 30 "
              "costs the same 134 MB once, not 30 times.")
        return

    # ---- batch: many calls, ONE interpreter, ONE pandas import --------------
    if args[0] == "--batch":
        raw = args[1] if len(args) > 1 and args[1] != "-" else sys.stdin.read()
        try:
            calls = _parse_batch(raw)
        except (ValueError, json.JSONDecodeError) as e:
            print(json.dumps({"error": f"could not parse batch: {e}"}, indent=2))
            raise SystemExit(2)
        results = [_call(name, kwargs) for name, kwargs in calls]
        print(json.dumps(results, indent=2, default=str))
        # A batch is a success if it RAN; individual failures are in the data.
        raise SystemExit(0)

    name = args[0]
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

    # Same path as --batch, so the two modes cannot drift into different answers.
    rec = _call(name, kwargs)
    if rec["ok"]:
        print(rec["result"])
        return
    print(json.dumps({k: v for k, v in rec.items()
                      if k not in ("ok", "tool", "args")}, indent=2))
    raise SystemExit(2)


if __name__ == "__main__":
    main()
