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

    Missing/unusable data discipline: a missing or non-numeric MOST RECENT point
    means current equity is unknown, so this reports INSUFFICIENT_DATA rather than
    silently promoting an older value to "current" (which would report a stale
    drawdown as if it were live). Missing points elsewhere in the series are
    dropped from the computation as before, but the count is surfaced via
    `nulls_dropped` and folded into `reason` so a gapped series can never read as
    a clean one — a dropped point may also have been the true high-water peak,
    understating the drawdown.
    """
    out = {"criterion": "drawdown", "state": INSUFFICIENT, "value": None,
           "limit": max_pct, "room": None, "reason": "", "nulls_dropped": 0}

    if not equity:
        out["reason"] = "need 2+ daily closes, have 0"
        return out

    last_raw = equity[-1]
    if last_raw is None:
        out["reason"] = "most recent equity value is missing; current equity is unknown"
        return out
    try:
        float(last_raw)
    except (TypeError, ValueError):
        out["reason"] = (f"most recent equity value is non-numeric ({last_raw!r}); "
                          f"current equity is unknown")
        return out

    vals: list[float] = []
    nulls_dropped = 0
    for v in equity:
        if v is None:
            nulls_dropped += 1
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            out["reason"] = f"non-numeric equity value ({v!r}) in series"
            return out

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
    out["nulls_dropped"] = nulls_dropped
    reason = (f"{dd:.2%} from peak {peak:.2f} "
              f"(limit {abs(max_pct):.0%})")
    if nulls_dropped:
        reason += f"; {nulls_dropped} missing value(s) dropped from series"
    out["reason"] = reason
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
    # a clean series (no gaps) reports nulls_dropped == 0
    assert r["nulls_dropped"] == 0, r

    # --- missing/unusable-data discipline (review finding) --------------------
    # trailing None: the MOST RECENT point is missing -> current equity is
    # unknown, must NOT silently fall back to an older value as if it were live
    r_trail = drawdown([100.0, 95.0, None], md)
    assert r_trail["state"] == INSUFFICIENT, r_trail
    assert "current equity is unknown" in r_trail["reason"], r_trail
    # leading + interior None: still computable from the remaining points, but
    # the drop count must be surfaced so a gapped series can't read as clean
    r_gap = drawdown([None, 100.0, None, 95.0], md)
    assert r_gap["state"] == PASS, r_gap
    assert r_gap["nulls_dropped"] == 2, r_gap
    assert "2 missing value(s) dropped" in r_gap["reason"], r_gap
    assert abs(r_gap["value"] + 0.05) < 1e-9, r_gap
    # the two-day-outage example from the finding: [100.0, None, None, 95.0]
    r_outage = drawdown([100.0, None, None, 95.0], md)
    assert r_outage["state"] == PASS, r_outage
    assert r_outage["nulls_dropped"] == 2, r_outage
    assert "missing value(s) dropped" in r_outage["reason"], r_outage
    # non-numeric value anywhere -> INSUFFICIENT_DATA, never an uncaught exception
    r_bad_last = drawdown([100.0, 95.0, "oops"], md)
    assert r_bad_last["state"] == INSUFFICIENT, r_bad_last
    assert "non-numeric" in r_bad_last["reason"], r_bad_last
    r_bad_interior = drawdown([100.0, "oops", 95.0], md)
    assert r_bad_interior["state"] == INSUFFICIENT, r_bad_interior
    assert "non-numeric" in r_bad_interior["reason"], r_bad_interior

    print("selftest OK: mandate loads, terms match")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
