"""Short-term momentum early-warning indicators for the risk review (Piece 3).
PURE: daily close Series in, per-name established-TI reads out — no I/O.
Two dimensions, established indicators, confirmed in conjunction (no TI in isolation):
  absolute leg (name's own momentum decelerating): MACD(12,26,9) histogram, RSI(14) vs 50
  relative leg (losing edge vs the market): relative-strength line = close/SPY vs its MA
The stateless risk review is fed the VALUES + a light 'weakening' tag; it judges. Only
established indicators, so the model reads them cold (no bespoke-score manual to re-feed).
"""
import pandas as pd


def macd(closes, fast=12, slow=26, signal=9):
    """Standard MACD. Returns (macd_line, signal_line, hist) as Series."""
    ema_f = closes.ewm(span=fast, adjust=False).mean()
    ema_s = closes.ewm(span=slow, adjust=False).mean()
    line = ema_f - ema_s
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig


def rsi(closes, period=14):
    """Wilder's RSI as a Series (0..100)."""
    d = closes.diff()
    gain = d.clip(lower=0.0)
    loss = (-d).clip(lower=0.0)
    ag = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    al = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    # When al=0 (no losses), rs is infinite, giving RSI=100. When ag=0 (no gains), rs=0, giving RSI=0.
    rs = ag / al.replace(0.0, pd.NA)
    rsi_vals = 100.0 - 100.0 / (1.0 + rs)
    # Handle edge cases: when al is 0 (no losses), RSI should be 100; when ag is 0 (no gains), RSI should be 0
    rsi_vals = rsi_vals.fillna(0.0)  # When ag=0 and al=0, both are 0, so NaN -> 0 is arbitrary but safe
    rsi_vals = rsi_vals.mask(al == 0.0, 100.0)  # Override: when al=0, RSI=100
    return rsi_vals


def compute(closes, spy, asof, params=None):
    """Per-name early-warning read as of `asof`. closes/spy: daily close Series
    (index=dates) with >= slow+signal rows through asof. Returns values + soft flags +
    two 'weakening' tags (MACD-based and RSI-based pairings; both require the relative
    leg ALSO soft — no indicator acts alone)."""
    p = {"macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
         "rsi_period": 14, "rsi_line": 50.0, "rs_ma": 20, **(params or {})}
    c = closes.loc[:asof].dropna()
    s = spy.loc[:asof].reindex(c.index).ffill()
    if len(c) < p["macd_slow"] + p["macd_signal"] + 1:
        return {"tag_macd": "n/a", "tag_rsi": "n/a", "reason": "insufficient history"}

    line, sig, hist = macd(c, p["macd_fast"], p["macd_slow"], p["macd_signal"])
    h0, h1 = float(hist.iloc[-1]), float(hist.iloc[-2])
    macd_soft = h0 < 0.0 and h0 <= h1                       # histogram negative AND not improving

    r = float(rsi(c, p["rsi_period"]).iloc[-1])
    rsi_soft = r < p["rsi_line"]                            # below the 50 regime line

    rs_line = c / s
    rs_ma = rs_line.rolling(p["rs_ma"]).mean()
    rs0, rsm = float(rs_line.iloc[-1]), float(rs_ma.iloc[-1])
    rel_soft = rs0 < rsm                                    # relative-strength below its MA

    def tag(abs_soft):
        if abs_soft and rel_soft:
            return "weakening"
        return "watch" if (abs_soft or rel_soft) else "ok"

    return {"macd_hist": round(h0, 4), "macd_hist_prev": round(h1, 4), "macd_soft": macd_soft,
            "rsi": round(r, 1), "rsi_soft": rsi_soft,
            "rs": round(rs0, 5), "rs_ma": round(rsm, 5), "rel_soft": rel_soft,
            "tag_macd": tag(macd_soft), "tag_rsi": tag(rsi_soft)}


def _selftest():
    import numpy as np

    idx = pd.date_range("2025-01-01", periods=80, freq="B")
    # a clean uptrend that ROLLS OVER over the last ~25 days, while SPY keeps rising:
    # the name is both decelerating (absolute) AND lagging the market (relative).
    # Construct with acceleration: slow decline then sharp crash to ensure MACD worsens.
    uptrend = np.linspace(100, 145, 55)
    # Slow decline then sharp crash (acceleration)
    downtrend = np.concatenate([
        np.linspace(145, 125, 15),  # moderate decline
        np.linspace(125, 75, 10)    # sharp crash
    ])
    px = pd.Series(np.concatenate([uptrend, downtrend]), index=idx)
    spy = pd.Series(np.linspace(400, 480, 80), index=idx)
    asof = idx[-1]
    out = compute(px, spy, asof)
    assert out["macd_soft"] and out["rel_soft"], out
    assert out["tag_macd"] == "weakening", out
    assert out["rsi"] < 50, out
    print("ti_signals selftest OK: rollover vs rising market -> weakening")

    # a healthy leader in a flat market: nothing soft -> ok
    strong = pd.Series(np.linspace(100, 185, 80), index=idx)
    flat = pd.Series(np.full(80, 400.0), index=idx)
    o2 = compute(strong, flat, asof)
    assert not o2["macd_soft"] and not o2["rel_soft"], o2
    assert o2["tag_macd"] == "ok" and o2["rsi"] >= 50, o2
    print("ti_signals selftest OK: strong leader -> ok")

    # insufficient history -> n/a (no crash)
    short = pd.Series(np.linspace(100, 110, 10), index=idx[:10])
    assert compute(short, spy, idx[9])["tag_macd"] == "n/a"
    print("ti_signals selftest OK: insufficient history -> n/a")


if __name__ == "__main__":
    _selftest()
