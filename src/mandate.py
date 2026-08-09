"""THE MANDATE — continuous, falsifiable evaluation of the terms the agent works
under (spec: docs/superpowers/specs/2026-08-09-agent-authority-inversion-design.md §5).

Pure functions over data that already exists on disk. No network, no broker, no
clock of its own — callers pass `asof`. Three-state by design: a criterion that
cannot be computed reports INSUFFICIENT_DATA and MUST NOT read as a pass.

Run the tests:  .venv/bin/python src/mandate.py --selftest
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

PASS = "PASS"
FAIL = "FAIL"
INSUFFICIENT = "INSUFFICIENT_DATA"


def load(path: str = "config/mandate.toml") -> dict:
    """Load the mandate terms. Raises if absent — an unstated mandate is not a
    permissive one, and must never silently default."""
    p = REPO / path
    if not p.exists():
        raise FileNotFoundError(f"mandate not found at {p}; the terms must be explicit")
    with p.open("rb") as fh:
        return tomllib.load(fh)


def drawdown(equity: list[float], max_pct: float) -> dict:
    """Criterion 1 (BLOCKING). Close-to-close drawdown from the all-time high-water
    mark. `equity` is the ordered daily close series, oldest first.

    Never measured intraday: an intraday measure fires the flatten on noise.
    """
    out = {"criterion": "drawdown", "state": INSUFFICIENT, "value": None,
           "limit": max_pct, "room": None, "reason": ""}
    vals = [float(v) for v in equity if v is not None]
    if len(vals) < 2:
        out["reason"] = f"need 2+ daily closes, have {len(vals)}"
        return out
    peak = max(vals)
    if peak <= 0:
        out["reason"] = "non-positive peak equity; drawdown undefined"
        return out
    dd = vals[-1] / peak - 1.0
    out["value"] = dd
    out["room"] = abs(max_pct) + dd          # how much further it may fall
    out["state"] = FAIL if dd < (-abs(max_pct) - 1e-9) else PASS
    out["reason"] = (f"{dd:.2%} from peak {peak:.2f} "
                     f"(limit {abs(max_pct):.0%})")
    return out


def _selftest() -> None:
    m = load()
    assert m["drawdown"]["max_pct"] == 0.15, m["drawdown"]
    assert m["concentration"]["max_position_pct"] == 0.15, m["concentration"]
    assert m["pnl_concentration"]["window_days"] == 90
    assert m["pnl_concentration"]["max_single_share"] == 0.40
    assert m["pnl_concentration"]["min_distinct_names"] == 4
    assert m["relative_return"]["window_days"] == 60
    assert m["relative_return"]["benchmark"] == "SPY"
    # Pairwise, not chained: `PASS != FAIL != INSUFFICIENT` reads as a three-way
    # check but only asserts the two adjacent pairs, never PASS vs INSUFFICIENT.
    # These three being distinguishable IS the module's central safety property.
    assert PASS != FAIL and FAIL != INSUFFICIENT and PASS != INSUFFICIENT

    # --- criterion 1: drawdown ------------------------------------------------
    md = m["drawdown"]["max_pct"]
    # peak 100 -> 95 is a 5% drawdown against a 15% limit: PASS, 10% of room left
    r = drawdown([80.0, 100.0, 95.0], md)
    assert r["state"] == PASS, r
    assert abs(r["value"] + 0.05) < 1e-9, r
    assert abs(r["room"] - 0.10) < 1e-9, r
    # peak 100 -> 84 breaches 15%
    assert drawdown([100.0, 84.0], md)["state"] == FAIL
    # exactly at the limit is NOT a breach (breach is strictly worse than the limit)
    assert drawdown([100.0, 85.0], md)["state"] == PASS
    # the peak is all-time and does not follow the book down
    assert abs(drawdown([100.0, 90.0, 92.0], md)["value"] + 0.08) < 1e-9
    # fewer than two points cannot express a drawdown
    assert drawdown([100.0], md)["state"] == INSUFFICIENT
    assert drawdown([], md)["state"] == INSUFFICIENT
    # a non-positive peak is undefined, not a pass
    assert drawdown([0.0, 0.0], md)["state"] == INSUFFICIENT
    print("selftest OK: mandate loads, terms match")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
