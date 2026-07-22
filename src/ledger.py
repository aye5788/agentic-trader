"""Decision→Outcome Ledger — pure helpers over research_store/journal.jsonl.

No RH I/O, no network, no clock reads inside the pure functions (dates are
passed in). Builds the outcome LABEL and derived views that make the journal a
complete, learnable record.

Spec: docs/superpowers/specs/2026-07-22-decision-outcome-ledger-design.md
"""
from __future__ import annotations

import datetime as _dt


def decision_id(symbol: str, as_of: str) -> str:
    """Stable join key for one position across its lifecycle."""
    return f"{symbol.upper()}:{as_of}"


def _days_between(start: str, end: str) -> int:
    """Whole calendar days from start to end (ISO YYYY-MM-DD)."""
    a = _dt.date.fromisoformat(start[:10])
    b = _dt.date.fromisoformat(end[:10])
    return (b - a).days


def outcome_from_exit(*, symbol, as_of, entry_price, exit_price, stop, targets,
                      exit_reason, entry_date, exit_date,
                      spy_entry=None, spy_exit=None) -> dict:
    """Compute the outcome LABEL for a fully-closed position — pure arithmetic.

    entry_price = the position's average cost; exit_price = the realized sell
    price. return_vs_spy is None unless BOTH spy_entry and spy_exit are given.
    """
    entry_price = float(entry_price)
    exit_price = float(exit_price)
    pnl_pct = (exit_price - entry_price) / entry_price if entry_price else 0.0

    hit_stop = stop is not None and exit_price <= float(stop)
    hit_target = bool(targets) and exit_price >= float(targets[0])

    rel = None
    if spy_entry and spy_exit:  # Truthiness guard: both must be supplied AND non-zero; spy_entry==0 would divide by zero below
        spy_ret = (float(spy_exit) - float(spy_entry)) / float(spy_entry)
        rel = round(pnl_pct - spy_ret, 4)

    return {
        "decision_id": decision_id(symbol, as_of),
        "status": exit_reason,
        "exit_reason": exit_reason,
        "entry_price": round(entry_price, 4),
        "exit_price": round(exit_price, 4),
        "pnl_pct": round(pnl_pct, 4),
        "holding_days": _days_between(entry_date, exit_date),
        "hit_stop": hit_stop,
        "hit_target": hit_target,
        "return_vs_spy": rel,
    }


def _selftest() -> None:
    # decision_id: upper-cases symbol, joins on as_of
    assert decision_id("xle", "2026-07-20") == "XLE:2026-07-20"

    # a winning target exit vs SPY
    o = outcome_from_exit(
        symbol="XLE", as_of="2026-07-06", entry_price=100.0, exit_price=110.0,
        stop=95.0, targets=[108.0, 115.0], exit_reason="target",
        entry_date="2026-07-06", exit_date="2026-07-20",
        spy_entry=500.0, spy_exit=505.0,
    )
    assert o["decision_id"] == "XLE:2026-07-06"
    assert o["pnl_pct"] == 0.1           # (110-100)/100
    assert o["holding_days"] == 14
    assert o["hit_target"] is True       # 110 >= first target 108
    assert o["hit_stop"] is False
    assert o["return_vs_spy"] == 0.09     # 0.10 - 0.01
    assert o["exit_reason"] == "target"

    # a stop-out, no SPY context supplied
    s = outcome_from_exit(
        symbol="MU", as_of="2026-07-10", entry_price=200.0, exit_price=190.0,
        stop=192.0, targets=[220.0], exit_reason="stopped",
        entry_date="2026-07-10", exit_date="2026-07-13",
    )
    assert s["pnl_pct"] == -0.05
    assert s["hit_stop"] is True         # 190 <= stop 192
    assert s["hit_target"] is False
    assert s["return_vs_spy"] is None    # no spy inputs -> None
    assert s["holding_days"] == 3

    # SPY entry == 0.0: truthiness guard prevents division by zero
    z = outcome_from_exit(
        symbol="XLE", as_of="2026-07-06", entry_price=100.0, exit_price=110.0,
        stop=95.0, targets=[108.0], exit_reason="target",
        entry_date="2026-07-06", exit_date="2026-07-20",
        spy_entry=0.0, spy_exit=505.0,
    )
    assert z["return_vs_spy"] is None    # spy_entry==0 fails truthiness, no div-by-zero
    assert z["pnl_pct"] == 0.1           # position PnL still computed

    print("selftest OK: decision_id, outcome_from_exit (win/target, loss/stop, no-spy, spy_entry=0)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
