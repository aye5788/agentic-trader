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
import math
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
        if not math.isfinite(avg):                      # corrupt cost basis
            avg = 0.0
        mark = marks.get(sym) or p.get("last") or avg
        mark = float(mark) if mark else 0.0
        if not math.isfinite(mark):
            # FIX B (2026-08-10): a NaN/inf mark (e.g. a corrupt monitor quote)
            # must never reach `value`/`invested`/`account_value`. Python's json
            # module writes a bare NaN and reads it back happily, so an
            # unguarded NaN here would flow straight into
            # research_store/history/equity.jsonl via scripts/log_equity.py and
            # permanently corrupt the equity track record mandate.drawdown()
            # reads. src/mandate.py's drawdown() now survives a corrupt
            # interior POINT once one exists, but the real fix is to never
            # WRITE one -- fall back exactly like an absent mark would: cost
            # basis (already guaranteed finite above), or 0.0 if that is
            # unusable too.
            mark = avg
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
            # Pass through the broker's OWN funding figures when the snapshot
            # carries them. `cash` is NOT what can be spent: on this cash account
            # sale proceeds are unsettled for T+1, so on 2026-08-10 cash was
            # $9.20 while buying_power was $2.14 (unsettled_funds $7.06). Anything
            # sizing a BUY against `cash` is sizing against money that is not
            # there. Absent -> None, never a substituted value.
            "buying_power": (round(float(snap["buying_power"]), 2)
                             if snap.get("buying_power") is not None else None),
            "unsettled_funds": (round(float(snap["unsettled_funds"]), 2)
                                if snap.get("unsettled_funds") is not None else None),
            "marked_at": marked_at, "cash": round(float(cash), 2),
            "positions": positions, "invested": round(invested, 2),
            "account_value": round(account_value, 2)}


def _selftest() -> None:
    """Covers the isfinite guard (FIX B, 2026-08-10): a NaN/inf mark or
    avg_cost must never reach `value`/`invested`/`account_value` -- it must
    fall back exactly like an absent mark would (cost basis, or 0.0), and
    ordinary finite marks must be entirely unaffected."""
    import tempfile
    global SNAPSHOT, QUOTES
    _snap, _quotes = SNAPSHOT, QUOTES
    try:
        with tempfile.TemporaryDirectory() as d:
            snap_path = Path(d) / "positions.json"
            QUOTES = Path(d) / "quotes.json"        # absent by default -> no monitor overlay

            def _write_snap(last, avg_cost=10.0, ts="2026-08-10T14:00:00+00:00"):
                snap_path.write_text(json.dumps({
                    "account_number": "1", "cash": 10.0, "as_of": "2026-08-10",
                    "ts": ts,
                    "positions": {"AAA": {"qty": 2.0, "avg_cost": avg_cost, "last": last}}}))

            # 1. ordinary finite mark: unaffected by the guard
            _write_snap(last=12.0)
            out = load(snap_path)
            assert out["positions"]["AAA"]["mark"] == 12.0, out
            assert out["positions"]["AAA"]["value"] == 24.0, out
            assert math.isfinite(out["account_value"]), out

            # 2. NaN `last` falls back to the (finite) avg_cost, never NaN
            _write_snap(last=float("nan"))
            out = load(snap_path)
            assert out["positions"]["AAA"]["mark"] == 10.0, out
            assert out["positions"]["AAA"]["value"] == 20.0, out
            assert math.isfinite(out["positions"]["AAA"]["mark"]), out
            assert math.isfinite(out["account_value"]), out

            # 3. +inf `last` is guarded the same way as NaN
            _write_snap(last=float("inf"))
            out = load(snap_path)
            assert out["positions"]["AAA"]["mark"] == 10.0, out
            assert math.isfinite(out["account_value"]), out

            # 4. -inf `last` is guarded the same way
            _write_snap(last=float("-inf"))
            out = load(snap_path)
            assert out["positions"]["AAA"]["mark"] == 10.0, out
            assert math.isfinite(out["account_value"]), out

            # 5. BOTH last and avg_cost corrupt -> mark falls all the way to
            #    0.0 (not NaN), same discipline as the pre-existing "mark or 0"
            #    fallback for an absent mark, never a corrupted one
            _write_snap(last=float("nan"), avg_cost=float("nan"))
            out = load(snap_path)
            assert out["positions"]["AAA"]["mark"] == 0.0, out
            assert out["positions"]["AAA"]["avg_cost"] == 0.0, out
            assert math.isfinite(out["account_value"]), out

            # 6. a NaN monitor-quote overlay (fresher than the snapshot) is
            #    guarded exactly like a NaN snapshot `last` -- the priority
            #    chain (monitor > last > avg_cost) must never smuggle a NaN
            #    through its highest-priority source
            _write_snap(last=12.0, ts="2026-08-10T14:00:00+00:00")
            QUOTES.write_text(json.dumps({"ts": "2026-08-10T15:00:00+00:00",
                                          "prices": {"AAA": float("nan")}}))
            out = load(snap_path)
            assert out["positions"]["AAA"]["mark"] == 10.0, out   # falls to avg_cost
            assert math.isfinite(out["account_value"]), out

        print("selftest OK: marks -- isfinite guard on mark/avg_cost "
              "(NaN/inf never reaches value/account_value)")
    finally:
        SNAPSHOT, QUOTES = _snap, _quotes


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
