"""Study A2 — what take-profit distance actually compounds?

Study A (scripts/calibrate_geometry.py) showed the live +5.5-sigma target sits far
outside the range price travels in a week. It could not say what target WOULD
work, for two reasons this script fixes:

  1. It locked target = 2.2 x stop (the live construction), so it could only sweep
     one knob. Here stop and target are INDEPENDENT.
  2. It measured "was the level ever touched in H days", not which level was
     touched FIRST. At a far target that was a safe approximation — almost nothing
     got there. At a near target most paths touch BOTH levels, so ordering IS the
     answer. This script walks the path day by day.

It also fixes the population error in Study A: that filtered on R>0 (a floor most
of the universe clears), not on being SELECTED. Here we reproduce the real
cross-sectional momentum score from src/momentum.py and condition on actually
ranking top-N, which is what the book holds.

Outcome model per simulated entry (entry = close on day t, in sigma units):
    stop first        -> -stop_mult
    target first      -> +target_mult
    neither in H days -> the actual close-to-close move (the ROTATION exit, which
                         is how 15 of your 16 real closes actually happened)
Same-day touch of both -> counted as the STOP (conservative; intraday order is
unknowable from daily bars).

Expectancy is therefore a real per-trade average including rotation exits, not a
two-outcome approximation.

Needs true intraday highs/lows, which only the live universe panel has, so this
inherits that panel's survivorship bias — today's names over history. Treat the
LEVELS as optimistic and the COMPARISON between rows as the trustworthy part.

Read-only. Run:
    .venv/bin/python scripts/calibrate_targets.py
    .venv/bin/python scripts/calibrate_targets.py --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

PRICES = REPO / "research_store" / "prices"
LOOKBACK, TREND_MA = 252, 200          # must match src/momentum.py
SAMPLE_EVERY = 5
STOP_MULTS = [1.0, 1.5, 2.0, 2.5, 3.0]
TGT_MULTS = [0.5, 1.0, 1.5, 2.0, 3.0, 5.5]
HORIZONS = [5, 10]
TOP_N = 10                              # [portfolio] book_hold


def momentum_rank(close: pd.DataFrame) -> pd.DataFrame:
    """Reproduce src/momentum.py's cross-sectional score, vectorised over all dates.
    score = mean(pct_rank(R/sigma), pct_rank(close/SMA200 - 1)); ineligible (R<=0)
    get NaN. Returns descending rank per row (1 = strongest)."""
    rets = close.pct_change()
    sigma = rets.rolling(LOOKBACK).std()
    R = close / close.shift(LOOKBACK) - 1.0
    trend = close / close.rolling(TREND_MA).mean() - 1.0
    ret = R / sigma
    score = (ret.rank(axis=1, pct=True) + trend.rank(axis=1, pct=True)) / 2.0
    score = score.where(R > 0)                       # absolute gate
    return score.rank(axis=1, ascending=False, method="first")


def first_touch(close, high, low, sigma, stop_mult, tgt_mult, h, mask):
    """Walk forward day by day; return realised outcome in sigma units for every
    cell in `mask`, plus which level was hit. Stop wins same-day ties."""
    stop_px = close * (1.0 - stop_mult * sigma)
    tgt_px = close * (1.0 + tgt_mult * sigma)
    hit_stop = np.zeros(close.shape, dtype=bool)
    hit_tgt = np.zeros(close.shape, dtype=bool)
    for k in range(1, h + 1):
        lo_k, hi_k = low.shift(-k).values, high.shift(-k).values
        undecided = ~(hit_stop | hit_tgt)
        s_now = undecided & (lo_k <= stop_px.values)
        t_now = undecided & ~s_now & (hi_k >= tgt_px.values)   # stop wins the tie
        hit_stop |= s_now
        hit_tgt |= t_now
    rot = ((close.shift(-h) / close - 1.0) / sigma).values      # rotation exit
    out = np.where(hit_stop, -stop_mult, np.where(hit_tgt, tgt_mult, rot))
    m = mask.values & np.isfinite(out) & np.isfinite(rot)
    return out[m], hit_stop[m], hit_tgt[m]


def run(top_only: bool) -> dict:
    close = pd.read_parquet(PRICES / "closes.parquet")
    high = pd.read_parquet(PRICES / "highs.parquet")
    low = pd.read_parquet(PRICES / "lows.parquet")
    cols = close.columns.intersection(high.columns).intersection(low.columns)
    close, high, low = close[cols], high[cols], low[cols]

    sigma = close.pct_change().rolling(LOOKBACK).std()
    base = sigma.notna() & (sigma > 0) & close.notna()
    if top_only:
        base &= momentum_rank(close) <= TOP_N
    rows = np.zeros(len(close), dtype=bool)
    rows[LOOKBACK::SAMPLE_EVERY] = True
    base &= pd.Series(rows, index=close.index).values[:, None]

    label = f"top-{TOP_N} selected" if top_only else "all eligible"
    res = {}
    for h in HORIZONS:
        print(f"\n=== {label}, {h}-day hold "
              f"({'the real book' if top_only else 'baseline'}) ===")
        print("  stop | target | P(tgt 1st) | P(stop 1st) | P(rotate) | "
              "expectancy | win rate")
        print("  -----+--------+------------+-------------+-----------+"
              "------------+---------")
        for sm in STOP_MULTS:
            for tm in TGT_MULTS:
                vals, hs, ht = first_touch(close, high, low, sigma, sm, tm, h, base)
                if vals.size < 500:
                    continue
                exp_, wr = float(vals.mean()), float((vals > 0).mean())
                key = f"h{h}_s{sm}_t{tm}"
                res[key] = {"n": int(vals.size), "horizon": h, "stop_mult": sm,
                            "target_mult": tm, "p_target_first": float(ht.mean()),
                            "p_stop_first": float(hs.mean()),
                            "p_rotate": float(1 - ht.mean() - hs.mean()),
                            "expectancy_sigma": exp_, "win_rate": wr,
                            "rr": round(tm / sm, 2)}
                live = " <- LIVE" if (sm == 2.5 and tm == 5.5) else ""
                print(f"  {sm:4.1f} | {tm:6.1f} | {ht.mean():9.1%}  | {hs.mean():10.1%}  |"
                      f" {1 - ht.mean() - hs.mean():8.1%}  | {exp_:+10.3f}s | {wr:7.1%}{live}")
    return res


def _selftest() -> None:
    """Pin the first-touch walk: ordering, the same-day tie-break, and the
    rotation fallback. This is the only place that can be silently wrong."""
    idx = pd.date_range("2024-01-01", periods=8, freq="B")

    def frame(vals):
        return pd.DataFrame({"X": vals}, index=idx)

    # sigma 10% -> stop_mult 1 => stop at 90, target_mult 1 => target at 110
    sigma = frame([0.10] * 8)
    close = frame([100.0] * 8)
    mask = pd.DataFrame({"X": [True] + [False] * 7}, index=idx)

    # target touched day 1, stop not touched at all -> +1 sigma
    v, hs, ht = first_touch(close, frame([100, 111, 100, 100, 100, 100, 100, 100.0]),
                            frame([100] * 8), sigma, 1.0, 1.0, 5, mask)
    assert v[0] == 1.0 and ht[0] and not hs[0], (v, hs, ht)

    # stop touched day 1, target day 2 -> stop came first -> -1 sigma
    v, hs, ht = first_touch(close, frame([100, 100, 111, 100, 100, 100, 100, 100.0]),
                            frame([100, 89, 100, 100, 100, 100, 100, 100.0]),
                            sigma, 1.0, 1.0, 5, mask)
    assert v[0] == -1.0 and hs[0] and not ht[0], (v, hs, ht)

    # BOTH on day 1 -> conservative: stop wins
    v, hs, ht = first_touch(close, frame([100, 111, 100, 100, 100, 100, 100, 100.0]),
                            frame([100, 89, 100, 100, 100, 100, 100, 100.0]),
                            sigma, 1.0, 1.0, 5, mask)
    assert v[0] == -1.0 and hs[0] and not ht[0], "same-day tie must resolve to the stop"

    # neither touched -> rotation exit at the day-5 close (105 -> +0.5 sigma)
    v, hs, ht = first_touch(frame([100, 100, 100, 100, 100, 105, 100, 100.0]),
                            frame([100] * 8), frame([100] * 8), sigma, 1.0, 1.0, 5, mask)
    assert abs(v[0] - 0.5) < 1e-9 and not hs[0] and not ht[0], v

    # a touch AFTER the horizon must not count
    v, hs, ht = first_touch(close, frame([100, 100, 100, 100, 100, 100, 111, 100.0]),
                            frame([100] * 8), sigma, 1.0, 1.0, 5, mask)
    assert not ht[0], "touch beyond the horizon leaked in"

    print("selftest OK: first-touch ordering, same-day tie -> stop, rotation "
          "fallback, horizon boundary")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        out = {"baseline": run(top_only=False), "selected": run(top_only=True)}
        p = REPO / "research_store" / "target_calibration.json"
        p.write_text(json.dumps(out, indent=2))
        print(f"\nwrote {p}")
