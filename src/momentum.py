"""The momentum signal — the single source of truth for the ranking math.

This is the deterministic engine described in docs/STRATEGY.md §3. The backtest
and the (future) live slow loop MUST both call this, so the numbers can never
drift apart. Pure functions over a close-price panel; no I/O, no clock.

Signal (per ticker, as of a given date, using only data up to that date):
  R      = close_t / close_[t-252] - 1          # 12-month return, 12-0 (no skip)
  sigma  = std(daily returns over the 252d)     # trailing volatility
  ret    = R / sigma                            # risk-adjusted momentum
  trend  = close_t / SMA200 - 1                 # distance above the 200-day mean
  score  = mean( pct_rank(ret), pct_rank(trend) )   # equal-weight rank-average
  eligible = R > 0                              # absolute gate
Only eligible names are ranked/held; score orders them descending.
"""
from __future__ import annotations

import pandas as pd

LOOKBACK = 252   # 12-month formation window (trading days)
TREND_MA = 200   # trend moving-average window


def _asof_slice(panel: pd.DataFrame, asof: pd.Timestamp) -> pd.DataFrame:
    """Rows up to and including `asof` — enforces no look-ahead."""
    return panel.loc[:asof]


def compute(panel: pd.DataFrame, asof: pd.Timestamp,
            lookback: int = LOOKBACK, residual_tilt: float = 0.0,
            market: "pd.Series | None" = None) -> pd.DataFrame:
    """Return a per-ticker DataFrame [R, sigma, ret, trend, score, eligible,
    rank], indexed by ticker, for the given as-of date. Tickers without enough
    history (need lookback+1 and TREND_MA closes) are dropped. `lookback`
    defaults to the production 252d; the backtest sweep varies it."""
    hist = _asof_slice(panel, asof)
    if len(hist) < lookback + 1:
        return pd.DataFrame()

    window = hist.iloc[-(lookback + 1):]          # (lookback+1) rows -> lookback returns
    close_t = window.iloc[-1]
    close_0 = window.iloc[0]
    daily_ret = window.pct_change().iloc[1:]      # 252 daily returns

    R = close_t / close_0 - 1
    sigma = daily_ret.std()
    sma200 = hist.iloc[-TREND_MA:].mean()
    trend = close_t / sma200 - 1

    # need a full, finite history for every field, else drop the ticker
    valid = close_t.notna() & close_0.notna() & (sigma > 0) & sma200.notna() & (
        hist.iloc[-TREND_MA:].notna().sum() >= TREND_MA
    )
    df = pd.DataFrame({"R": R, "sigma": sigma, "trend": trend})[valid]
    if df.empty:
        return df
    df["ret"] = df["R"] / df["sigma"]

    p_ret = df["ret"].rank(pct=True)
    p_trend = df["trend"].rank(pct=True)
    if residual_tilt and residual_tilt > 0.0 and market is not None:
        import residual
        rm = residual.residual_momentum(panel, asof, market, lookback).reindex(df.index)
        p_resid = rm.rank(pct=True).fillna(p_ret)     # missing residual -> fall back to R/σ rank
        p_mom = (1.0 - residual_tilt) * p_ret + residual_tilt * p_resid
    else:
        p_mom = p_ret
    df["score"] = (p_mom + p_trend) / 2.0

    df["eligible"] = df["R"] > 0                  # absolute gate
    # rank eligible names by score desc; ineligible get NaN rank
    df["rank"] = df["score"].where(df["eligible"]).rank(ascending=False, method="first")
    return df.sort_values("score", ascending=False)


def select(scored: pd.DataFrame, held: set[str], hold_n: int, band_n: int) -> list[str]:
    """Apply banded selection: keep a currently-held name until it falls below
    rank `band_n`; add a new name only if it's in the top `hold_n` and a slot is
    open. Returns the target holding list (only eligible names)."""
    if scored.empty:
        return []
    elig = scored[scored["eligible"]].copy()
    ranked = elig.sort_values("score", ascending=False)
    order = ranked.index.tolist()
    rank_of = {t: i + 1 for i, t in enumerate(order)}

    # 1. retain held names still inside the band
    keep = [t for t in order if t in held and rank_of[t] <= band_n]
    # 2. fill remaining slots from the top down with fresh names
    for t in order:
        if len(keep) >= hold_n:
            break
        if t not in keep and rank_of[t] <= max(hold_n, band_n):
            keep.append(t)
    return keep[:hold_n]


def regime_on(spy: pd.Series, asof: pd.Timestamp, ma_days: int = 50) -> bool:
    """Mechanical floor: SPY (proxy for $SPX) above its 50-day MA. New entries
    are blocked when this is False (existing holds still managed by exits)."""
    s = spy.loc[:asof].dropna()
    if len(s) < ma_days:
        return False
    return bool(s.iloc[-1] > s.iloc[-ma_days:].mean())


def _selftest() -> None:
    import numpy as np
    # 260 business days so lookback=252 has a full window.
    idx = pd.date_range("2024-01-01", periods=260, freq="B")
    rng = np.random.default_rng(0)
    mkt = pd.Series(100 * (1 + pd.Series(rng.normal(0.0005, 0.01, 260), index=idx)).cumprod().values, index=idx)
    panel = pd.DataFrame({"SPY": mkt.values}, index=idx)
    for i in range(6):
        panel[f"T{i}"] = 100 * (1 + pd.Series(rng.normal(0.0008, 0.02, 260), index=idx)).cumprod().values
    # T0 as a pure leveraged market clone (large beta, ~zero idiosyncratic alpha):
    # guarantees residual momentum has genuine market exposure to strip out, so the
    # tilt=1 re-rank below is deterministic rather than a coin-flip on the noise draw
    # (the 6 purely-idiosyncratic T-series above have near-zero true beta to the
    # market, so residual momentum ~ raw momentum for them regardless of seed).
    panel["T0"] = mkt.values * 3.0
    asof = idx[-1]

    # residual_tilt=0.0 reproduces the classic score exactly.
    base = compute(panel, asof)                                  # default tilt 0
    d = compute(panel, asof, residual_tilt=0.0, market=panel["SPY"])
    assert base["score"].equals(d["score"]), "tilt=0 must equal the classic score"
    # classic formula check: score == mean(rank(ret), rank(trend))
    expect = (base["ret"].rank(pct=True) + base["trend"].rank(pct=True)) / 2.0
    assert np.allclose(base["score"].values, expect.reindex(base.index).values), "score formula drifted"

    # residual_tilt=1.0 changes the ranking (uses residual instead of R/σ).
    full = compute(panel, asof, residual_tilt=1.0, market=panel["SPY"])
    assert not full["score"].equals(base["score"]), "tilt=1 should re-rank"

    print("selftest OK: compute residual_tilt (0=identity, 1=re-ranks)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
