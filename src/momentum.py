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
            market: "pd.Series | None" = None,
            factors: "pd.DataFrame | None" = None) -> pd.DataFrame:
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

    # equal-weight rank-average of the (optionally residual-blended) momentum view and the trend view
    p_ret = df["ret"].rank(pct=True)
    p_trend = df["trend"].rank(pct=True)
    if residual_tilt and residual_tilt > 0.0 and (factors is not None or market is not None):
        import residual
        if factors is not None:
            rm = residual.residual_momentum_sector(panel, asof, factors, lookback).reindex(df.index)
        else:
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
    import residual
    n = 260
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    t = np.arange(n)
    m = 0.0008 + 0.012 * np.sin(2 * np.pi * t / 15)            # market daily returns (+drift, real variance)

    def _px(rets):
        return pd.Series(100 * np.cumprod(1 + rets), index=idx)

    # Build names from RETURNS (beta*market + idiosyncratic) then compound to prices,
    # so beta is genuine (NOT a price-level scalar, which pct_change would cancel).
    panel = pd.DataFrame({
        "SPY": _px(m),
        "BETA_ONLY": _px(1.8 * m + 0.004 * np.sin(2 * np.pi * t / 9 + 1.1)),   # high beta, ZERO-mean idio -> ~0 residual mom
        "IDIO_WIN":  _px(0.4 * m + (0.0018 + 0.004 * np.sin(2 * np.pi * t / 11))),  # low beta, POSITIVE idio -> strong residual mom
        "F1": _px(1.0 * m + (0.0006 + 0.003 * np.sin(2 * np.pi * t / 7))),
        "F2": _px(1.2 * m - (0.0006 + 0.003 * np.sin(2 * np.pi * t / 13))),
        "F3": _px(0.7 * m + 0.003 * np.sin(2 * np.pi * t / 8 + 0.5)),
    }, index=idx)
    asof = idx[-1]

    # 1. Backward-compat (LOAD-BEARING): residual_tilt=0.0 reproduces the classic score exactly.
    base = compute(panel, asof)                                   # default tilt 0
    d0 = compute(panel, asof, residual_tilt=0.0, market=panel["SPY"])
    assert base["score"].equals(d0["score"]), "tilt=0 must equal the classic score"
    expect = (base["ret"].rank(pct=True) + base["trend"].rank(pct=True)) / 2.0
    assert np.allclose(base["score"].values, expect.reindex(base.index).values), "score formula drifted"

    # 2. Residual momentum is well-conditioned (real signal, not 0/0 float noise).
    rm = residual.residual_momentum(panel, asof, panel["SPY"])
    assert rm["IDIO_WIN"] > 0.3 and rm["BETA_ONLY"] < 0.1, rm.round(3).to_dict()

    # 3. Tilting fully to residual meaningfully re-ranks: BETA_ONLY (high beta, ~0
    #    residual momentum) is demoted by a wide margin when the residual view takes over.
    full = compute(panel, asof, residual_tilt=1.0, market=panel["SPY"])
    assert not base["score"].equals(full["score"]), "residual_tilt=1 should re-rank"
    assert full.loc["BETA_ONLY", "score"] < base.loc["BETA_ONLY", "score"] - 0.05, \
        (base.loc["BETA_ONLY", "score"], full.loc["BETA_ONLY", "score"])

    # --- sector (multi-factor) path ---
    import residual as _res
    ns = 260
    sidx = pd.date_range("2024-01-01", periods=ns, freq="B")
    st = np.arange(ns)
    def _p(r): return pd.Series(100 * np.cumprod(1 + r), index=sidx)
    S1 = 0.0009 + 0.012 * np.sin(2 * np.pi * st / 15)
    S2 = 0.0006 + 0.010 * np.sin(2 * np.pi * st / 11 + 0.7)
    factors = pd.DataFrame({"XLK": _p(S1), "XLF": _p(S2)}, index=sidx)
    dps = 0.0018 + 0.004 * np.sin(2 * np.pi * st / 7)
    spanel = pd.DataFrame({
        "SPANNED": _p(0.8 * S1 + 0.5 * S2 + 0.004 * np.sin(2 * np.pi * st / 8 + 0.4)),  # ~0 sector-residual
        "IDIO":    _p(0.4 * S1 + dps),                                                  # strong sector-residual
        "G1": _p(1.0 * S1 + 0.002 * np.sin(2 * np.pi * st / 6)),
        "G2": _p(0.7 * S2 - 0.0015 * np.sin(2 * np.pi * st / 9)),
        "G3": _p(0.5 * S1 + 0.5 * S2 + 0.002 * np.sin(2 * np.pi * st / 13)),  # zero-mean idio (NOT a constant — a constant gives ~0 residual var -> blowup)
    }, index=sidx)
    sasof = sidx[-1]
    # tilt=0 is still identity on this panel
    assert compute(spanel, sasof)["score"].equals(
        compute(spanel, sasof, residual_tilt=0.0, factors=factors)["score"]), "tilt0 identity (sector)"
    # the sector residual is well-conditioned (ties to Task 1 math): IDIO >> SPANNED
    rms = _res.residual_momentum_sector(spanel, sasof, factors)
    assert rms["IDIO"] > rms["SPANNED"], rms.round(3).to_dict()
    # the factors path produces finite scores and re-ranks vs baseline
    fs = compute(spanel, sasof, residual_tilt=1.0, factors=factors)
    assert fs["score"].notna().all() and not compute(spanel, sasof)["score"].equals(fs["score"]), "sector path re-ranks"

    print("selftest OK: compute residual_tilt (0=identity, 1=meaningfully re-ranks)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
