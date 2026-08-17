"""Daily bars and the levels derived from them -- where a name has actually traded.

The agent sets its own stops and targets but had only *endpoints* and a
*distribution*: `quote()` gives today's OHLC plus the 52-week extremes,
`terrain()` gives a volatility distribution of forward excursions. Neither
shows the *shape* between those endpoints -- a moving average, a prior swing
low, a consolidation base, a gap. This module supplies that shape so a stop
can be placed under a prior low or a base rather than at an arbitrary
distance.

Pure arithmetic only: no file reads, no network. All I/O (loading the price
panels) lives in src/agent_env/server.py.
"""
from __future__ import annotations


def series(opens, highs, lows, closes, symbol: str, days: int) -> dict:
    """Daily bars (oldest -> newest) and the levels derived from them for `symbol`.

    `opens`/`highs`/`lows`/`closes` are date-indexed DataFrames, one column per
    symbol -- pass the panels exactly as loaded from disk. Pure: no I/O.

    Returns up to `days` most recent bars (never padded -- fewer than `days`
    rows on record means fewer bars, not fabricated ones) plus a `levels`
    dict of moving averages and rolling highs/lows. Any level whose window is
    longer than the available history is `None` rather than a partial
    average presented as a full one. `pct_from_X` values are signed fractions
    of the LAST CLOSE relative to that level (`last/X - 1`); negative means
    the last close sits below it.

    Returns `{"error": ...}` -- never raises, never an empty success -- when
    `symbol` is absent from any of the four panels or has no rows.
    """
    missing_from = [name for name, panel in
                    (("open", opens), ("high", highs), ("low", lows), ("close", closes))
                    if symbol not in panel.columns]
    if missing_from:
        return {"error": f"{symbol} is missing from panel(s): {', '.join(missing_from)}"}

    c = closes[symbol].dropna()
    if c.empty:
        return {"error": f"{symbol} has no rows in the close panel"}

    o = opens[symbol].reindex(c.index)
    h = highs[symbol].reindex(c.index)
    l = lows[symbol].reindex(c.index)

    n_bars = min(int(days), len(c))
    tail_idx = c.index[-n_bars:]
    bars = [{
        "d": ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10],
        "o": round(float(o.loc[ts]), 4),
        "h": round(float(h.loc[ts]), 4),
        "l": round(float(l.loc[ts]), 4),
        "c": round(float(c.loc[ts]), 4),
    } for ts in tail_idx]

    def _ma(n: int):
        if len(c) < n:
            return None
        return round(float(c.tail(n).mean()), 4)

    def _hi(n: int):
        if len(h) < n:
            return None
        return round(float(h.tail(n).max()), 4)

    def _lo(n: int):
        if len(l) < n:
            return None
        return round(float(l.tail(n).min()), 4)

    ma20, ma50, ma200 = _ma(20), _ma(50), _ma(200)
    high_20, low_20 = _hi(20), _lo(20)
    high_50, low_50 = _hi(50), _lo(50)
    high_252, low_252 = _hi(252), _lo(252)

    last = float(c.iloc[-1])

    def _pct(level):
        if level is None or level == 0:
            return None
        return round(last / level - 1.0, 6)

    levels = {
        "ma20": ma20, "ma50": ma50, "ma200": ma200,
        "high_20": high_20, "low_20": low_20,
        "high_50": high_50, "low_50": low_50,
        "high_252": high_252, "low_252": low_252,
        "pct_from_ma50": _pct(ma50),
        "pct_from_high_20": _pct(high_20),
        "pct_from_low_20": _pct(low_20),
        "pct_from_high_252": _pct(high_252),
    }

    asof = c.index[-1]
    return {
        "symbol": symbol,
        "asof": asof.date().isoformat() if hasattr(asof, "date") else str(asof)[:10],
        "bars": bars,
        "levels": levels,
    }


def _selftest() -> None:
    import pandas as pd
    idx = pd.date_range("2026-01-01", periods=60, freq="D")
    # a clean ramp 100..159 so every derived level is hand-checkable
    c = pd.DataFrame({"AAA": [100.0 + i for i in range(60)]}, index=idx)
    o = c.copy(); h = c + 1.0; l = c - 1.0

    r = series(o, h, l, c, "AAA", days=10)
    assert r["symbol"] == "AAA", r
    assert r["asof"] == "2026-03-01", r["asof"]          # 60th day
    assert len(r["bars"]) == 10, len(r["bars"])
    assert r["bars"][0]["d"] < r["bars"][-1]["d"], "bars must run oldest -> newest"
    assert r["bars"][-1]["c"] == 159.0, r["bars"][-1]
    assert r["bars"][-1]["h"] == 160.0 and r["bars"][-1]["l"] == 158.0, r["bars"][-1]

    lv = r["levels"]
    assert lv["high_20"] == 160.0, lv          # highs = close+1, last 20 rows
    assert lv["low_20"] == 139.0, lv           # lows  = close-1, 20 rows back
    assert round(lv["ma20"], 4) == round(sum(range(140, 160)) / 20, 4), lv
    assert lv["ma200"] is None, "not enough history for a 200d average"
    # last close 159 vs 20d high 160 -> just below it
    assert lv["pct_from_high_20"] < 0, lv
    assert abs(lv["pct_from_high_20"] - (159.0 / 160.0 - 1)) < 1e-9, lv

    # asking for more days than exist returns what exists, never pads
    assert len(series(o, h, l, c, "AAA", days=999)["bars"]) == 60

    # unknown symbol is an error, never an empty success
    assert "error" in series(o, h, l, c, "ZZZ", days=10), "unknown symbol must error"

    print("selftest OK: history -- bars oldest->newest, derived levels, "
          "short history yields None rather than a fabricated average")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
