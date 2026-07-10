"""Append executed fills to the Research Store journal — deterministically.

The fast loop's headless agent places orders via the RH MCP, then calls THIS to
record them, instead of hand-editing journal.jsonl (which risks clobbering the
append-only log) or writing throwaway helper scripts. It reads a fixed fills
file and appends exactly one journal event via store.append_journal().

Agent contract:
  1. write research_store/rh/fills.json = a JSON array of objects, e.g.
       [{"symbol":"EEM","side":"buy","amount":3.00,"order_id":"...","status":"unconfirmed"}, ...]
     Orders the agent SKIPPED (review rejection, unsettled-cash deferral,
     re-entry veto) go in the same array with status "skipped" and a short
     "reason" — so deferred legs are journaled, not just narrated in the report.
  2. run:  .venv/bin/python scripts/record_fills.py

Append-only; safe to run once per fast-loop execution. Also pushes a phone
notification (ntfy) summarizing what was placed/skipped — without this, the
only trade alert the human gets is Robinhood's own app notification.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from research_store import store  # noqa: E402
from notify import push           # noqa: E402

FILLS = REPO / "research_store" / "rh" / "fills.json"
REENTRY_DECISIONS = REPO / "research_store" / "rh" / "reentry_decisions.json"


def main() -> None:
    if not FILLS.exists():
        sys.exit(f"no fills file at {FILLS} — write it first (see module docstring)")
    fills = json.loads(FILLS.read_text())
    if not isinstance(fills, list):
        sys.exit("fills.json must be a JSON array of fill objects")
    entry = {
        "event": "execution",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n": len(fills),
        "fills": fills,
    }
    # post-take-profit re-entry judgments, if the agent made any this run
    # (prompts/fast_loop.md step 7b) — journaled alongside the fills, then
    # consumed so a later run can't re-journal stale decisions
    if REENTRY_DECISIONS.exists():
        try:
            entry["reentry_decisions"] = json.loads(REENTRY_DECISIONS.read_text())
            REENTRY_DECISIONS.unlink()
        except Exception as e:
            print(f"reentry_decisions.json unreadable ({e}) — journaling fills without it")
    store.append_journal(entry)
    print(f"journaled {len(fills)} fills"
          + (f" + {len(entry.get('reentry_decisions', []))} re-entry decisions" if entry.get("reentry_decisions") else "")
          + f" -> {store.JOURNAL}")
    _push_summary(fills, entry.get("reentry_decisions"))


def _push_summary(fills: list, reentry: list | None) -> None:
    """Phone push: one line per order. push() never raises, so neither do we."""
    placed = [f for f in fills if f.get("status") != "skipped"]
    skipped = [f for f in fills if f.get("status") == "skipped"]
    lines = [f"{f.get('side', '?').upper()} {f.get('symbol', '?')} ${f.get('amount', '?')}"
             + (f" @ ${f['avg_price']}" if f.get("avg_price") else f" ({f.get('status', '?')})")
             for f in placed]
    lines += [f"{f.get('side', '?').upper()} {f.get('symbol', '?')} ${f.get('amount', '?')}"
              + f" — SKIPPED: {f.get('reason', 'no reason recorded')}"
              for f in skipped]
    lines += [f"re-entry {d.get('symbol', '?')}: {d.get('decision', '?')} — {d.get('reason', '')}"
              for d in (reentry or [])]
    if not lines:
        return
    title = f"Agentic: {len(placed)} order{'s' if len(placed) != 1 else ''} placed"
    if skipped:
        title += f", {len(skipped)} skipped"
    push(title, "\n".join(lines), tags="money_with_wings")


if __name__ == "__main__":
    main()
