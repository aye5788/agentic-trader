"""Append executed fills to the Research Store journal — deterministically.

The fast loop's headless agent places orders via the RH MCP, then calls THIS to
record them, instead of hand-editing journal.jsonl (which risks clobbering the
append-only log) or writing throwaway helper scripts. It reads a fixed fills
file and appends exactly one journal event via store.append_journal().

Agent contract:
  1. write research_store/rh/fills.json = a JSON array of objects, e.g.
       [{"symbol":"EEM","side":"buy","amount":3.00,"order_id":"...","status":"unconfirmed"}, ...]
  2. run:  .venv/bin/python scripts/record_fills.py

Append-only; safe to run once per fast-loop execution.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from research_store import store  # noqa: E402

FILLS = REPO / "research_store" / "rh" / "fills.json"


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
    store.append_journal(entry)
    print(f"journaled {len(fills)} fills -> {store.JOURNAL}")


if __name__ == "__main__":
    main()
