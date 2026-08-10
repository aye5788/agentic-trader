"""How far a name actually travels, in units of its own daily volatility.

The agent sets its own stops and targets, so it needs the distribution rather
than a formula. Measured the same way as scripts/calibrate_geometry.py: trailing
252-day daily sigma (matching src/momentum.py), then the forward distribution of
max-high and min-low over each horizon.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

LOOKBACK = 252


def excursions(close, high, low, symbol: str, horizons=(1, 3, 5, 10, 20)) -> dict:
    """Per-horizon favourable/adverse excursion quantiles in sigma units.

    Returns {"symbol","sigma_pct", horizons:{h:{"mfe_median","mfe_p90","mae_median",
    "mae_p10","n"}}} or {"error": ...} when the name has too little history.
    """
    if symbol not in close.columns:
        return {"error": f"{symbol} is not in the price panel"}
    c = close[symbol].dropna()
    if len(c) < LOOKBACK + max(horizons) + 1:
        return {"error": f"{symbol} has {len(c)} closes; need "
                         f"{LOOKBACK + max(horizons) + 1} for a 252d sigma plus a "
                         f"{max(horizons)}-day forward window"}
    h = high[symbol].reindex(c.index)
    l = low[symbol].reindex(c.index)
    sigma = c.pct_change().rolling(LOOKBACK).std()

    out = {"symbol": symbol, "sigma_pct": float(sigma.iloc[-1] * 100.0), "horizons": {}}
    for n in horizons:
        fwd_max = h[::-1].rolling(n, min_periods=n).max()[::-1].shift(-1)
        fwd_min = l[::-1].rolling(n, min_periods=n).min()[::-1].shift(-1)
        mfe = ((fwd_max / c - 1.0) / sigma).replace([np.inf, -np.inf], np.nan).dropna()
        mae = ((fwd_min / c - 1.0) / sigma).replace([np.inf, -np.inf], np.nan).dropna()
        if mfe.empty or mae.empty:
            continue
        out["horizons"][n] = {
            "n": int(len(mfe)),
            "mfe_median": round(float(mfe.median()), 3),
            "mfe_p90": round(float(mfe.quantile(0.90)), 3),
            "mae_median": round(float(mae.median()), 3),
            "mae_p10": round(float(mae.quantile(0.10)), 3),
        }
    return out


def _selftest() -> None:
    import pandas as pd
    n = 400
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    rng = np.random.default_rng(0)
    px = 100 * np.cumprod(1 + rng.normal(0.0005, 0.02, n))
    close = pd.DataFrame({"AAA": px}, index=idx)
    high = pd.DataFrame({"AAA": px * 1.01}, index=idx)
    low = pd.DataFrame({"AAA": px * 0.99}, index=idx)

    t = excursions(close, high, low, "AAA")
    assert t["symbol"] == "AAA" and t["sigma_pct"] > 0, t
    assert set(t["horizons"]) == {1, 3, 5, 10, 20}, t["horizons"].keys()
    h5 = t["horizons"][5]
    assert h5["n"] > 0 and h5["mfe_median"] > 0 > h5["mae_median"], h5
    # favourable excursion must grow with the horizon
    assert t["horizons"][20]["mfe_median"] > t["horizons"][1]["mfe_median"], t
    # an unknown symbol is an explained error, never a crash or a fake number
    assert "error" in excursions(close, high, low, "NOPE")
    # too little history is also an explained error
    short = close.iloc[:10]
    assert "error" in excursions(short, high.iloc[:10], low.iloc[:10], "AAA")
    print("selftest OK: terrain excursions scale with horizon, unknown symbol explained")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
