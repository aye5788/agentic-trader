"""Pull ~10y daily closes for the whole universe (names + ETF sleeve + SPY) from
Schwab and cache to research_store/prices/closes.parquet (git-ignored).

Idempotent-ish: skips the network pull if the cache exists and --force isn't set.
Run once; the backtest reads the cache. Records tickers that failed / are too
short (fresh IPOs) so the backtest can drop them honestly.

    python scripts/fetch_prices.py [--force] [--years 10]
"""
import argparse
import sys
import time
from collections import Counter
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

OUT_DIR = REPO / "research_store" / "prices"
OPENS = OUT_DIR / "opens.parquet"
HIGHS = OUT_DIR / "highs.parquet"
LOWS = OUT_DIR / "lows.parquet"
CLOSES = OUT_DIR / "closes.parquet"
META = OUT_DIR / "fetch_meta.csv"


def universe_tickers() -> list[str]:
    names = pd.read_csv(REPO / "config" / "universe.csv")["ticker"].tolist()
    etfs = pd.read_csv(REPO / "config" / "etf_universe.csv")["ticker"].tolist()
    # SPY = benchmark + regime proxy for $SPX. Dedupe, keep order.
    tickers = list(dict.fromkeys(names + etfs + ["SPY"]))
    return tickers


def _try_pull(sym: str, years: int):
    """Return (list[candle] | None, err_msg | None). Try a couple symbol spellings
    Schwab wants. On failure return the last error so the caller can tell a dead
    ticker apart from a systemic outage (expired token / locked DB / network)."""
    from adapters.schwab import research  # noqa: E402
    last_err = None
    for cand in (sym, sym.replace(".", "/")):  # BRK.B -> BRK/B fallback
        try:
            ph = research.get_price_history(
                cand, period_type="year", period=years, frequency_type="daily"
            )
            if ph:
                return ph, None
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
    return None, last_err


_FIELDS = ("open", "high", "low", "close")


def _field_panels(raw: dict) -> dict:
    """Turn {sym: [candle,...]} into one dates×tickers DataFrame per OHLC field.
    Pure: no network, no I/O. `close` panel is byte-for-byte what we cached before."""
    out = {f: {} for f in _FIELDS}
    for sym, candles in raw.items():
        for f in _FIELDS:
            out[f][sym] = pd.Series(
                {pd.Timestamp(c["datetime"], unit="ms").normalize(): c[f] for c in candles}
            )
    return {f: pd.DataFrame(series).sort_index() for f, series in out.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--years", type=int, default=10)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if CLOSES.exists() and not args.force:
        df = pd.read_parquet(CLOSES)
        print(f"cache exists: {CLOSES} ({df.shape[0]} rows x {df.shape[1]} tickers). "
              f"--force to re-pull.")
        return

    tickers = universe_tickers()
    print(f"pulling {len(tickers)} tickers, {args.years}y daily from Schwab...")
    raw = {}
    meta = []
    errors = []
    for i, sym in enumerate(tickers, 1):
        ph, err = _try_pull(sym, args.years)
        if not ph:
            meta.append((sym, 0, "FAILED"))
            if err:
                errors.append(err)
            print(f"  [{i:3}/{len(tickers)}] {sym:6} FAILED" + (f"  ({err[:90]})" if err else ""))
            continue
        raw[sym] = ph
        meta.append((sym, len(ph), "ok"))
        if i % 25 == 0:
            print(f"  [{i:3}/{len(tickers)}] ... {sym} ({len(ph)} candles)")
        time.sleep(0.6)

    ok = sum(1 for _, _, st in meta if st == "ok")
    # SAFETY GUARD (2026-07-15 incident): never overwrite a good cache with a
    # systemic-failure result. A handful of dead/illiquid names failing is normal;
    # a majority failing means auth/lock/network is broken. Writing that
    # (near-)empty panel would DESTROY the existing 10y cache — which is exactly
    # what turned a token hiccup into "everything's gone". Abort loudly instead
    # (non-zero exit -> cron ERR-trap phone alert) and leave the cache untouched.
    if ok < max(1, len(tickers) // 2):
        reason = Counter(errors).most_common(1)
        reason = reason[0][0] if reason else "unknown (no error captured)"
        print(f"\nABORT: only {ok}/{len(tickers)} tickers fetched — systemic "
              f"failure, not a few dead names.\n  most common error: {reason}")
        if CLOSES.exists():
            print(f"  existing cache LEFT INTACT (not overwritten): {CLOSES}")
        raise SystemExit(2)

    panels = _field_panels(raw)
    field_to_path = {"open": OPENS, "high": HIGHS, "low": LOWS, "close": CLOSES}
    for field, path in field_to_path.items():
        try:
            panels[field].to_parquet(path)
        except Exception as e:  # never lose a full pull to a missing parquet engine
            fallback = path.with_suffix(".csv")
            panels[field].to_csv(fallback)
            print(f"WARN parquet write failed for {field} ({e}); wrote CSV -> {fallback}")
    panel = panels["close"]   # keep existing summary prints working
    pd.DataFrame(meta, columns=["ticker", "candles", "status"]).to_csv(META, index=False)

    failed = [t for t, _, st in meta if st != "ok"]
    short = [t for t, n, st in meta if st == "ok" and n < 300]
    print(f"\ndone: {ok}/{len(tickers)} ok -> {CLOSES}")
    if not panel.empty:
        print(f"panel: {panel.shape[0]} dates x {panel.shape[1]} tickers, "
              f"{panel.index.min().date()} .. {panel.index.max().date()}")
    if failed:
        print(f"FAILED ({len(failed)}):", " ".join(failed))
    if short:
        print(f"SHORT <300 candles ({len(short)}):", " ".join(short))


def _selftest() -> None:
    raw = {
        "AAA": [
            {"datetime": 1609459200000, "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5},
            {"datetime": 1609545600000, "open": 10.5, "high": 12.0, "low": 10.0, "close": 11.8},
        ],
        "BBB": [
            {"datetime": 1609459200000, "open": 20.0, "high": 21.0, "low": 19.0, "close": 20.5},
        ],
    }
    panels = _field_panels(raw)
    assert set(panels) == {"open", "high", "low", "close"}, panels.keys()
    # close panel preserves the pre-refactor content/shape
    assert panels["close"].loc[panels["close"].index[1], "AAA"] == 11.8
    assert panels["high"].loc[panels["high"].index[0], "AAA"] == 11.0
    assert panels["low"].loc[panels["low"].index[0], "BBB"] == 19.0
    # sorted by date, aligned index across tickers
    assert list(panels["close"].index) == sorted(panels["close"].index)
    print("selftest OK: _field_panels open/high/low/close")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
