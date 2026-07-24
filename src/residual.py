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
    mvar = mkt.var(ddof=0)
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


def residual_mom_multifactor(rets, factor_rets):
    """Multivariate residual momentum: regress each name's returns on the factor
    returns (intercept + factors) via one lstsq, residual_mom = α / std(resid).
    Solves only names with a complete non-NaN window; others -> NaN. NaN-safe."""
    fr = factor_rets.dropna()
    if len(fr) < len(factor_rets.columns) + 2:            # need obs > params
        return pd.Series(index=rets.columns, dtype=float)
    rets = rets.reindex(fr.index)
    X = np.column_stack([np.ones(len(fr)), fr.values])   # T x (1+K)
    clean = ~rets.isna().any()
    out = pd.Series(np.nan, index=rets.columns)
    if not clean.any():
        return out
    Yc = rets.loc[:, clean].values                       # T x Nc
    B, *_ = np.linalg.lstsq(X, Yc, rcond=None)           # (1+K) x Nc
    resid = Yc - X @ B
    alpha = B[0]
    std = resid.std(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        rm = np.where(std > 0, alpha / std, np.nan)
    out.loc[clean] = rm
    return out.replace([np.inf, -np.inf], np.nan)


def residual_momentum_sector(panel, asof, factor_panel, lookback: int = 252):
    """Multi-factor residual momentum per ticker as of `asof`, from a close-price
    panel and a factor close-price panel (the sector ETFs). Empty if window short."""
    hist = panel.loc[:asof]
    if len(hist) < lookback + 1:
        return pd.Series(dtype=float)
    win = hist.iloc[-(lookback + 1):]
    rets = win.pct_change().iloc[1:]
    frets = factor_panel.loc[:asof].reindex(win.index).pct_change().iloc[1:]
    return residual_mom_multifactor(rets, frets)


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

    # --- multi-factor (sector) residual ---
    nn = 60
    tt = np.arange(nn)
    S = pd.DataFrame({
        "S1": 0.001 + 0.012 * np.sin(2 * np.pi * tt / 15),
        "S2": 0.0005 + 0.010 * np.sin(2 * np.pi * tt / 11 + 0.7),
        "S3": -0.0003 + 0.008 * np.sin(2 * np.pi * tt / 9 + 1.3),
    }, index=pd.RangeIndex(nn))
    dp = pd.Series(0.0018 + 0.004 * np.sin(2 * np.pi * tt / 7), index=S.index)   # +mean idio
    dz = pd.Series(0.004 * np.sin(2 * np.pi * tt / 8 + 0.4), index=S.index)      # 0-mean idio
    mf = pd.DataFrame({
        "PURE": 0.8 * S["S1"] + 0.5 * S["S2"] + dz,     # spanned by factors -> ~0 residual mom
        "POS":  0.8 * S["S1"] + 0.3 * S["S2"] + dp,     # + positive idiosyncratic
        "NEG":  0.8 * S["S1"] + 0.3 * S["S2"] - dp,     # + negative idiosyncratic
    })
    rmf = residual_mom_multifactor(mf, S)
    assert rmf["POS"] > 0 and rmf["NEG"] < 0, rmf.round(3).to_dict()
    assert rmf["POS"] > rmf["PURE"] > rmf["NEG"], rmf.round(3).to_dict()
    assert abs(rmf["PURE"]) < 1.0, rmf["PURE"]
    mf_nan = mf.copy(); mf_nan.iloc[5, 0] = np.nan
    assert pd.isna(residual_mom_multifactor(mf_nan, S)["PURE"]), "NaN-window name -> NaN"

    print("selftest OK: residual (single + multi-factor)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
