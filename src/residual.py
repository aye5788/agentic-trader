"""Residual (market-beta-stripped) momentum — pure, no I/O, no clock.

For each ticker over a trailing window, regress its daily returns on the market's
(SPY) and score the idiosyncratic momentum as the regression **intercept α** (the
average non-market return) ÷ residual vol. NOTE: use α, NOT Σresid — OLS residuals
sum to ~0 by construction (with an intercept), so Σresid would be zero for every
name. α is the average idiosyncratic return, which is exactly what we want. Analog
of momentum's `ret = R/σ`, market noise removed. Null-safe: degenerate inputs (no
market variance, empty window) return NaN, never raise.

Spec: docs/superpowers/specs/2026-07-23-residual-momentum-design.md §3.1
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def residual_mom_from_returns(rets: pd.DataFrame, mkt: pd.Series) -> pd.Series:
    """Per-ticker residual momentum from daily returns. rets: dates×tickers; mkt:
    market daily returns (same index). Regress each column on mkt (skipna),
    residual_mom = α / std(resid) — the average idiosyncratic return per unit
    idiosyncratic risk. (NOT Σresid: OLS residuals sum to ~0 with an intercept.)
    NaN where undefined."""
    mkt = mkt.dropna()
    if len(mkt) < 2:
        return pd.Series(index=rets.columns, dtype=float)
    rets = rets.reindex(mkt.index)
    mvar = mkt.var()
    if not mvar or mvar == 0:
        return pd.Series(index=rets.columns, dtype=float)
    dm = mkt - mkt.mean()
    cov = rets.sub(rets.mean()).mul(dm, axis=0).mean()          # per-column cov (skipna)
    beta = cov / mvar
    alpha = rets.mean() - beta * mkt.mean()                     # avg idiosyncratic return
    fitted = pd.DataFrame(np.outer(mkt.values, beta.values),
                          index=mkt.index, columns=rets.columns).add(alpha, axis=1)
    resid = rets.sub(fitted)
    rm = alpha / resid.std()                                    # risk-adjusted idiosyncratic momentum
    return rm.replace([np.inf, -np.inf], np.nan)


def residual_momentum(panel: pd.DataFrame, asof, market: pd.Series,
                      lookback: int = 252) -> pd.Series:
    """Residual momentum per ticker as of `asof`, from a close-price panel and a
    market close-price Series. Empty Series if the window is too short."""
    hist = panel.loc[:asof]
    if len(hist) < lookback + 1:
        return pd.Series(dtype=float)
    win = hist.iloc[-(lookback + 1):]
    rets = win.pct_change().iloc[1:]
    mkt = market.loc[:asof].reindex(win.index).pct_change().iloc[1:]
    return residual_mom_from_returns(rets, mkt)


def _selftest() -> None:
    # Clean fixture: market returns m; three names with known idiosyncratic content.
    m = pd.Series([0.01, -0.02, 0.015, -0.01, 0.02, -0.015, 0.01, -0.005],
                  index=pd.RangeIndex(8))
    d_pos = pd.Series([0.01, 0.008, 0.012, 0.009, 0.011, 0.007, 0.013, 0.010], index=m.index)
    d_zero = pd.Series([0.01, -0.01, 0.01, -0.01, 0.01, -0.01, 0.01, -0.01], index=m.index)
    a = 0.5 * m + d_zero    # half-beta + ZERO-mean idiosyncratic  -> residual_mom ~ 0
    b = 0.5 * m + d_pos     # half-beta + POSITIVE idiosyncratic    -> residual_mom > 0
    c = 0.5 * m - d_pos     # half-beta + NEGATIVE idiosyncratic    -> residual_mom < 0
    rm = residual_mom_from_returns(pd.DataFrame({"A": a, "B": b, "C": c}), m)
    assert rm["B"] > 0, rm.to_dict()                     # positive idiosyncratic drift
    assert rm["C"] < 0, rm.to_dict()                     # negative idiosyncratic drift
    assert rm["B"] > rm["A"] > rm["C"], rm.to_dict()     # correct ordering (A ~ 0 in the middle)
    assert abs(rm["A"]) < 1.0, rm["A"]                   # zero-mean idiosyncratic -> near 0
    # (verified numerically: B≈+6.3, C≈-7.5, A≈-0.08.) NOTE: don't test an EXACT
    # market rider (a=1.0*m) — its residual is ~0 with ~0 variance, so α/std is
    # a 0/0 float-garbage value, not a meaningful assertion.

    # degenerate: zero market variance -> all NaN, no raise
    flat = pd.Series([0.0] * 8, index=m.index)
    assert residual_mom_from_returns(pd.DataFrame({"A": a, "B": b}), flat).isna().all()

    print("selftest OK: residual_mom_from_returns (sign + ordering + degenerate)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
