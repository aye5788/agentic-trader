"""Position valuation — the single place snapshot positions become dollars.

The RH snapshot (research_store/rh/positions.json) stores SHARES and COST, and
this module marks them to the freshest price available. Every consumer
(dashboard, log_equity, fast_loop diff) values positions through here so the
numbers can never drift apart.

Snapshot schema (written by the agent at every execution event — see
prompts/fast_loop.md step 4 and prompts/exit.md):

    {"account_number": "…", "account_value": <total $>, "cash": <$>,
     "as_of": "YYYY-MM-DD", "ts": "<iso utc>",
     "positions": {"SYM": {"qty": <shares>, "avg_cost": <$>, "last": <$>}, …}}

Mark priority per symbol: monitor quote (15 s during RTH, written by
market_monitor to research_store/monitor/quotes.json) when newer than the
snapshot, else the snapshot's `last`, else `avg_cost`.

Legacy schema ({"SYM": <cost dollars>}) is still valued (at cost, qty unknown)
so an old snapshot degrades gracefully instead of crashing the dashboard.
"""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RS = REPO / "research_store"
SNAPSHOT = RS / "rh" / "positions.json"
QUOTES = RS / "monitor" / "quotes.json"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def load(snapshot_path: Path = SNAPSHOT) -> dict | None:
    """Read the snapshot, overlay the freshest marks, return valued state:

    {"account_number", "as_of", "ts", "marked_at", "cash",
     "positions": {sym: {"qty","avg_cost","mark","value","pnl"}},
     "invested", "account_value"}

    account_value = cash + marked invested when the snapshot carries cash
    (new schema); otherwise the snapshot's own (possibly stale) total.
    Returns None when no snapshot exists yet.
    """
    snap = _read_json(snapshot_path, None)
    if not snap:
        return None
    snap_ts = snap.get("ts") or ""
    mq = _read_json(QUOTES, {})
    monitor_fresh = bool(mq.get("ts")) and mq["ts"] > snap_ts
    marks = mq.get("prices", {}) if monitor_fresh else {}
    marked_at = mq.get("ts") if monitor_fresh else (snap_ts or snap.get("as_of"))

    positions, invested = {}, 0.0
    for sym, p in (snap.get("positions") or {}).items():
        if not isinstance(p, dict):                    # legacy: dollars at cost
            value = float(p or 0)
            positions[sym] = {"qty": None, "avg_cost": None, "mark": None,
                              "value": round(value, 2), "pnl": None}
            invested += value
            continue
        qty = float(p.get("qty") or 0)
        avg = float(p.get("avg_cost") or 0)
        mark = marks.get(sym) or p.get("last") or avg
        mark = float(mark) if mark else 0.0
        value = qty * mark
        cost = qty * avg
        positions[sym] = {"qty": qty, "avg_cost": avg, "mark": round(mark, 4),
                          "value": round(value, 2),
                          "pnl": round(value / cost - 1.0, 4) if cost > 0 else None}
        invested += value

    cash = snap.get("cash")
    if cash is not None:
        account_value = float(cash) + invested
    else:                                              # legacy snapshot: trust its total
        account_value = float(snap.get("account_value", 0) or 0)
        cash = account_value - invested
    return {"account_number": snap.get("account_number"),
            "as_of": snap.get("as_of"), "ts": snap_ts or None,
            "marked_at": marked_at, "cash": round(float(cash), 2),
            "positions": positions, "invested": round(invested, 2),
            "account_value": round(account_value, 2)}
