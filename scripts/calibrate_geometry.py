"""Study A — is the configured trade geometry REACHABLE at all?

The live ledger says 16 closed positions, zero target hits, one stop. That is too
small a sample to conclude anything on its own, and its holding durations are
contaminated by nightly thesis regeneration. So don't ask the ledger. Ask the
price process directly: over ten years and hundreds of names, how often does a
stock travel +5.5 daily sigma (target 1) or -2.5 daily sigma (the stop) within
1/3/5/10/20 trading days?

This needs no fills, no position lifecycle, no thesis identity and no executed
quantity — so none of the ledger's known defects can contaminate it.

Geometry under test (verified in scripts/slow_loop.py:63-75, config [trade_management]):
    stop_dist = stop_atr_mult * sigma        # 2.5 * daily sigma, fractional
    stop      = entry * (1 - stop_dist)      # -2.5 sigma
    target[i] = entry * (1 + r_i * stop_dist)  # r=[2.2, 4.0] -> +5.5 / +10.0 sigma
`sigma` is the 252-day daily-return stdev, exactly as src/momentum.py:44 defines it.

TWO PANELS, biased in OPPOSITE directions, so the truth is bracketed:

  pool   config/pit_pool.csv, 816 names, survivorship-FREE, but Alpaca IEX gives
         CLOSES only. Close-to-close excursion UNDERSTATES reachability (an
         intraday spike through the target that closes back below is missed).
  universe  the live 168-name panel with real intraday HIGHS/LOWS, so touches are
         exact -- but it is today's survivors over history, which OVERSTATES
         reachability (the names that fell over and died are absent).

If both agree the target is rarely reached, the finding is robust to both biases.

Read-only. Touches no live state. Run:
    .venv/bin/python scripts/calibrate_geometry.py
    .venv/bin/python scripts/calibrate_geometry.py --selftest
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
HORIZONS = [1, 3, 5, 10, 20]
LOOKBACK = 252          # must match src/momentum.py LOOKBACK
SAMPLE_EVERY = 5        # sample every 5th session; adjacent days are near-duplicates


def forward_extreme(panel: pd.DataFrame, h: int, how: str) -> pd.DataFrame:
    """At row t, the max (or min) over rows t+1 .. t+h. Reverse -> backward-rolling
    -> reverse restores [t .. t+h-1]; shift(-1) moves it to [t+1 .. t+h]."""
    rev = panel[::-1]
    agg = rev.rolling(h, min_periods=h).max() if how == "max" else rev.rolling(h, min_periods=h).min()
    return agg[::-1].shift(-1)


def excursions(close: pd.DataFrame, high: pd.DataFrame | None,
               low: pd.DataFrame | None, eligible_only: bool) -> dict:
    """P(touch) for each configured level, by horizon. `high`/`low` None -> use
    closes for both directions (the survivorship-free pool has no intraday data)."""
    up_src = high if high is not None else close
    dn_src = low if low is not None else close

    rets = close.pct_change()
    sigma = rets.rolling(LOOKBACK).std()
    R = close / close.shift(LOOKBACK) - 1.0

    base = sigma.notna() & (sigma > 0) & close.notna()
    if eligible_only:
        base &= R > 0                      # src/momentum.py:75 absolute gate

    # sample rows to decorrelate overlapping windows
    rows = np.zeros(len(close), dtype=bool)
    rows[LOOKBACK::SAMPLE_EVERY] = True
    base &= pd.Series(rows, index=close.index).values[:, None]

    out = {}
    for h in HORIZONS:
        fmax = forward_extreme(up_src, h, "max")
        fmin = forward_extreme(dn_src, h, "min")
        mask = base & fmax.notna() & fmin.notna()
        n = int(mask.values.sum())
        if not n:
            continue
        mfe = ((fmax / close - 1.0) / sigma)[mask]      # max favourable, in sigmas
        mae = ((fmin / close - 1.0) / sigma)[mask]      # max adverse, in sigmas
        v_mfe = mfe.values[np.isfinite(mfe.values)]
        v_mae = mae.values[np.isfinite(mae.values)]
        out[h] = {
            "n": int(v_mfe.size),
            "p_target1": float((v_mfe >= 5.5).mean()),
            "p_target2": float((v_mfe >= 10.0).mean()),
            "p_stop": float((v_mae <= -2.5).mean()),
            "mfe_median": float(np.median(v_mfe)),
            "mfe_p90": float(np.percentile(v_mfe, 90)),
            # stop_atr_mult is the ONLY free knob: stop and target are both scaled
            # by the same sigma, so R:R is fixed by construction (2.2) and tightening
            # the stop is the only way to bring the target within reach — at the cost
            # of stopping out more often. This is that trade-off, measured.
            "sweep": {str(m): {"target_sigma": round(2.2 * m, 2),
                               "p_target": float((v_mfe >= 2.2 * m).mean()),
                               "p_stop": float((v_mae <= -m).mean())}
                      for m in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)},
        }
    return out


def _sweep_table(name: str, res: dict, horizons=(5, 20), r1: float = 2.2) -> str:
    """The target pays r1 x R and the stop costs 1 x R, so the target mechanism only
    breaks even when P(target)/P(stop) > 1/r1. Anything below that line is a
    take-profit rule that loses money relative to its own stop."""
    be = 1.0 / r1
    L = [f"\n{name}  — stop_atr_mult trade-off (target is always {r1}x the stop)",
         f"  breakeven needs ratio > {be:.2f}  (target pays {r1}R, stop costs 1R)"]
    for h in horizons:
        if h not in res:
            continue
        L += [f"  day {h}:  stop_mult | target at | P(touch target) | P(touch stop) | ratio",
              "            ----------+-----------+-----------------+---------------+-----------"]
        for m, r in res[h]["sweep"].items():
            ratio = r["p_target"] / r["p_stop"] if r["p_stop"] else float("nan")
            mark = "  ok" if ratio > be else "  LOSS"
            cur = " <- LIVE" if abs(float(m) - 2.5) < 1e-9 else ""
            L.append(f"            {float(m):9.1f} | {r['target_sigma']:8.2f}s |"
                     f" {r['p_target']:14.2%}  | {r['p_stop']:12.2%}  | {ratio:5.2f}{mark}{cur}")
    return "\n".join(L)


def _table(name: str, res: dict) -> str:
    L = [f"\n{name}",
         "  day |      n | P(+5.5s t1) | P(+10s t2) | P(-2.5s stop) | median MFE | p90 MFE",
         "  ----+--------+-------------+------------+---------------+------------+--------"]
    for h, r in res.items():
        L.append(f"  {h:3d} | {r['n']:6d} | {r['p_target1']:10.2%}  | {r['p_target2']:9.2%}  |"
                 f" {r['p_stop']:12.2%}  | {r['mfe_median']:9.2f}s | {r['mfe_p90']:6.2f}s")
    return "\n".join(L)


def run() -> dict:
    import strategy
    tm = strategy.load()["trade_management"]
    mult, r_mults = tm["stop_atr_mult"], tm["target_r_mults"]
    print(f"geometry under test: stop -{mult}s, targets "
          f"{', '.join(f'+{r * mult}s' for r in r_mults)}  (daily sigma, 252d)")
    assert abs(r_mults[0] * mult - 5.5) < 1e-9, "target1 is not 5.5 sigma — update this study"

    report = {"geometry": {"stop_sigma": -mult,
                           "target_sigma": [r * mult for r in r_mults]}, "panels": {}}

    pool = pd.read_parquet(PRICES / "pool_closes.parquet")
    print(f"\npool (survivorship-FREE, closes only): {pool.shape[1]} names, "
          f"{pool.index[0].date()} -> {pool.index[-1].date()}")
    for cut, elig in (("all names", False), ("eligible (R>0)", True)):
        res = excursions(pool, None, None, elig)
        report["panels"][f"pool / {cut}"] = res
        print(_table(f"POOL, {cut}  [understates: closes only, misses intraday touch]", res))

    close = pd.read_parquet(PRICES / "closes.parquet")
    high = pd.read_parquet(PRICES / "highs.parquet")
    low = pd.read_parquet(PRICES / "lows.parquet")
    cols = close.columns.intersection(high.columns).intersection(low.columns)
    close, high, low = close[cols], high[cols], low[cols]
    print(f"\nuniverse (survivorship-BIASED, true intraday H/L): {len(cols)} names, "
          f"{close.index[0].date()} -> {close.index[-1].date()}")
    for cut, elig in (("all names", False), ("eligible (R>0)", True)):
        res = excursions(close, high, low, elig)
        report["panels"][f"universe / {cut}"] = res
        print(_table(f"UNIVERSE, {cut}  [overstates: today's survivors, intraday highs]", res))
        if elig:
            print(_sweep_table("UNIVERSE, eligible (the most favourable panel we have)", res))

    print("\nNOTE: these are 'touched within H days', NOT first-touch. A path that hits "
          "\nthe stop and then the target counts in both columns, so P(target FIRST) can "
          "\nonly be LOWER than the P(touch target) shown. The conclusion holds a fortiori.")
    return report


def _selftest() -> None:
    """The forward-window arithmetic is the only thing here that can be silently
    wrong, so pin it against a hand-computed series."""
    idx = pd.date_range("2024-01-01", periods=6, freq="B")
    p = pd.DataFrame({"X": [10.0, 11.0, 12.0, 9.0, 13.0, 8.0]}, index=idx)

    f1 = forward_extreme(p, 1, "max")["X"].tolist()
    assert f1[:5] == [11.0, 12.0, 9.0, 13.0, 8.0], f1      # t+1 only
    assert pd.isna(f1[5]), f1                              # nothing after the last row

    f3 = forward_extreme(p, 3, "max")["X"].tolist()
    assert f3[0] == 12.0, f3        # max(11,12,9)
    assert f3[1] == 13.0, f3        # max(12,9,13)
    assert f3[2] == 13.0, f3        # max(9,13,8)
    assert all(pd.isna(v) for v in f3[3:]), f3   # fewer than 3 rows remain

    f2min = forward_extreme(p, 2, "min")["X"].tolist()
    assert f2min[0] == 11.0 and f2min[2] == 9.0, f2min

    # no look-ahead: the value at t never depends on row t itself
    q = p.copy(); q.iloc[0, 0] = 999.0
    assert forward_extreme(q, 3, "max")["X"].iloc[0] == 12.0, "leaked row t into its own window"

    print("selftest OK: forward-window excursion arithmetic (no look-ahead)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        rep = run()
        out = REPO / "research_store" / "geometry_calibration.json"
        out.write_text(json.dumps(rep, indent=2))
        print(f"\nwrote {out}")
