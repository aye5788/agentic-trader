"""What the agent holds, and what it is worth. Pure assembly over marks.load()
and the current product — no new valuation logic, so the agent, the dashboard and
the equity log can never disagree about what a position is worth."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))


def holdings(valued: dict, theses: list) -> dict:
    """Merge broker positions with the levels the agent set for them.

    `valued` is marks.load(). `theses` is product.theses (may be empty).
    A held symbol with no thesis is reported with stop/target None and
    `watched=False` — that is the unprotected case and must be visible, not
    silently dropped.
    """
    by_sym = {t.symbol: t for t in (theses or [])}
    av = float(valued.get("account_value") or 0.0)
    out = {}
    for sym, p in (valued.get("positions") or {}).items():
        t = by_sym.get(sym)
        out[sym] = {
            "qty": p.get("qty"),
            "avg_cost": p.get("avg_cost"),
            "mark": p.get("mark"),
            "value": p.get("value"),
            "pnl": p.get("pnl"),
            "share_of_equity": (float(p["value"]) / av) if av > 0 and p.get("value") is not None else None,
            "stop": getattr(t, "stop", None) if t else None,
            "targets": list(getattr(t, "targets", []) or []) if t else [],
            "watched": bool(t is not None and getattr(t, "stop", None) is not None),
        }
    return out


def _selftest() -> None:
    import types
    T = lambda s, stop, tgts: types.SimpleNamespace(
        symbol=s, stop=stop, targets=tgts, target_weight=0.07)
    valued = {"account_value": 100.0, "cash": 10.0, "invested": 90.0,
              "positions": {"AAA": {"qty": 1.0, "avg_cost": 50.0, "mark": 60.0,
                                    "value": 60.0, "pnl": 0.2},
                            "BBB": {"qty": 1.0, "avg_cost": 30.0, "mark": 30.0,
                                    "value": 30.0, "pnl": 0.0}}}
    h = holdings(valued, [T("AAA", 55.0, [70.0, 80.0])])
    assert h["AAA"]["stop"] == 55.0 and h["AAA"]["watched"] is True, h
    assert h["AAA"]["value"] == 60.0 and h["AAA"]["share_of_equity"] == 0.6, h
    # held with NO thesis -> visible, flagged unwatched, never dropped
    assert "BBB" in h and h["BBB"]["stop"] is None and h["BBB"]["watched"] is False, h
    # a thesis for something NOT held must not appear as a holding
    h2 = holdings(valued, [T("ZZZ", 1.0, [2.0])])
    assert "ZZZ" not in h2, h2
    assert holdings({"account_value": 100.0, "positions": {}}, []) == {}
    print("selftest OK: holdings merges marks with agent levels, unwatched visible")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
