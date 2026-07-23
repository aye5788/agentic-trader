"""Pure distillation of raw moomoo records into the locked signal-panel scalars.

No I/O, no OpenD, no clock — dataframes are converted to list-of-dicts by the
caller (scripts/collect_signals.py) and passed in. Each distiller is null-safe:
missing/degenerate inputs return None rather than raising, because the collector
is non-fatal by contract.

Locked fields (see docs/superpowers/specs/2026-07-23-moomoo-signal-panel-design.md):
  capflow_bignet_20d, short_pct, days_to_cover, short_pct_chg,
  pc_vol_ratio, pc_oi_ratio, iv_rank, pct_52w_high, volume_ratio
"""
from __future__ import annotations


def _num(x):
    try:
        v = float(x)
        return v if v == v else None  # NaN -> None
    except (TypeError, ValueError):
        return None


def distill_capflow(rows, market_val):
    """Σ over the last 20 daily bars of (super+big net inflow) ÷ market cap.
    rows: daily capital-flow records (any order). None if no rows or no mktcap."""
    mv = _num(market_val)
    if not rows or not mv:
        return None
    ordered = sorted(rows, key=lambda r: r.get("capital_flow_item_time", ""))
    last20 = ordered[-20:]
    total = 0.0
    for r in last20:
        s = _num(r.get("super_in_flow")) or 0.0
        b = _num(r.get("big_in_flow")) or 0.0
        total += s + b
    return round(total / mv, 6)


def distill_short(rows):
    """Latest short_percent + days_to_cover, and the change vs the prior reading.
    rows sorted newest-first by timestamp; short_pct_chg is None with <2 rows."""
    out = {"short_pct": None, "days_to_cover": None, "short_pct_chg": None}
    if not rows:
        return out
    ordered = sorted(rows, key=lambda r: r.get("timestamp_str", ""), reverse=True)
    latest = ordered[0]
    out["short_pct"] = _num(latest.get("short_percent"))
    out["days_to_cover"] = _num(latest.get("days_to_cover"))
    if len(ordered) >= 2:
        prev = _num(ordered[1].get("short_percent"))
        if out["short_pct"] is not None and prev is not None:
            out["short_pct_chg"] = round(out["short_pct"] - prev, 4)
    return out


def _ratio(num, den):
    n, d = _num(num), _num(den)
    if n is None or not d:      # den None or 0 -> None
        return None
    return round(n / d, 4)


def distill_options(row):
    """put/call volume + OI ratios and iv_rank from an option-overview row."""
    row = row or {}
    return {
        "pc_vol_ratio": _ratio(row.get("put_volume"), row.get("call_volume")),
        "pc_oi_ratio": _ratio(row.get("put_open_interest"), row.get("call_open_interest")),
        "iv_rank": _num(row.get("iv_rank")),
    }


def distill_snapshot(row):
    """52-week-high proximity + abnormal-volume ratio from a market snapshot row."""
    row = row or {}
    return {
        "pct_52w_high": _ratio(row.get("last_price"), row.get("highest52weeks_price")),
        "volume_ratio": _num(row.get("volume_ratio")),
    }


def _selftest() -> None:
    # capflow: (10+20)+(5+5)+(-3+8) = 45 ; /1000 = 0.045
    cf = [
        {"capital_flow_item_time": "2026-07-01", "super_in_flow": 10, "big_in_flow": 20},
        {"capital_flow_item_time": "2026-07-02", "super_in_flow": 5, "big_in_flow": 5},
        {"capital_flow_item_time": "2026-07-03", "super_in_flow": -3, "big_in_flow": 8},
    ]
    assert distill_capflow(cf, 1000.0) == 0.045, distill_capflow(cf, 1000.0)
    assert distill_capflow(cf, 0) is None          # no mktcap -> None
    assert distill_capflow([], 1000.0) is None      # no rows -> None

    # short: newest 0.7/1.4 ; chg = 0.7 - 0.9 = -0.2
    si = [
        {"timestamp_str": "2026-06-30", "short_percent": 0.7, "days_to_cover": 1.4},
        {"timestamp_str": "2026-06-15", "short_percent": 0.9, "days_to_cover": 1.6},
    ]
    s = distill_short(si)
    assert s == {"short_pct": 0.7, "days_to_cover": 1.4, "short_pct_chg": -0.2}, s
    one = distill_short([si[0]])
    assert one["short_pct"] == 0.7 and one["short_pct_chg"] is None, one
    assert distill_short([]) == {"short_pct": None, "days_to_cover": None, "short_pct_chg": None}

    # options: 320/400=0.8 ; 2000/2700=0.7407 ; iv_rank passthrough ; 0-guard
    o = distill_options({"call_volume": 400, "put_volume": 320,
                         "call_open_interest": 2700, "put_open_interest": 2000,
                         "iv_rank": 77.7})
    assert o == {"pc_vol_ratio": 0.8, "pc_oi_ratio": 0.7407, "iv_rank": 77.7}, o
    assert distill_options({"call_volume": 0, "put_volume": 5})["pc_vol_ratio"] is None

    # snapshot: 320/335 = 0.9552
    sn = distill_snapshot({"last_price": 320, "highest52weeks_price": 335, "volume_ratio": 0.54})
    assert sn == {"pct_52w_high": 0.9552, "volume_ratio": 0.54}, sn
    assert distill_snapshot({"last_price": 320, "highest52weeks_price": 0})["pct_52w_high"] is None

    print("selftest OK: distill_capflow/short/options/snapshot")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
