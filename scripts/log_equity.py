"""Append one equity point to research_store/history/equity.jsonl — the data
behind the dashboard's performance curve.

Reads the RH snapshot the fast loop already wrote (account_value + positions), so
it needs no extra broker call. Run it right after the fast loop each day; over
time the jsonl becomes the account's track record. Append-only, idempotent per
day (a second run the same date overwrites that day's point).

    .venv/bin/python scripts/log_equity.py
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SNAP = REPO / "research_store" / "rh" / "positions.json"
HIST = REPO / "research_store" / "history" / "equity.jsonl"


def main() -> None:
    if not SNAP.exists():
        sys.exit("no positions snapshot yet — run the fast loop first")
    d = json.loads(SNAP.read_text())
    value = float(d["account_value"])
    invested = sum(float(v) for v in d.get("positions", {}).values())
    date = d.get("as_of") or datetime.now(timezone.utc).date().isoformat()
    point = {"date": date,
             "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "value": round(value, 2), "invested": round(invested, 2),
             "cash": round(value - invested, 2)}

    HIST.parent.mkdir(parents=True, exist_ok=True)
    prior = [json.loads(l) for l in HIST.read_text().splitlines() if l.strip()] if HIST.exists() else []
    prior = [p for p in prior if p.get("date") != date]     # replace same-day point
    prior.append(point)
    HIST.write_text("".join(json.dumps(p) + "\n" for p in prior))
    print(f"logged equity ${value:.2f} for {date} -> {HIST} ({len(prior)} points)")


if __name__ == "__main__":
    main()
