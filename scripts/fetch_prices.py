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
from adapters.schwab import research  # noqa: E402

OUT_DIR = REPO / "research_store" / "prices"
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
    series = {}
    meta = []
    errors = []
    for i, sym in enumerate(tickers, 1):
        ph, err = _try_pull(sym, args.years)
        if not ph:
            meta.append((sym, 0, "FAILED"))
            if err:
                errors.append(err)
            print(f"  [{i:3}/{len(tickers)}] {sym:6} FAILED"
                  + (f"  ({err[:90]})" if err else ""))
            continue
        s = pd.Series(
            {pd.Timestamp(c["datetime"], unit="ms").normalize(): c["close"] for c in ph}
        )
        series[sym] = s
        meta.append((sym, len(s), "ok"))
        if i % 25 == 0:
            print(f"  [{i:3}/{len(tickers)}] ... {sym} ({len(s)} candles)")
        # Schwab limit ~120/min = 1 call / 0.5s. Hold 0.6s to stay safely under.
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

    panel = pd.DataFrame(series).sort_index()
    try:
        panel.to_parquet(CLOSES)
    except Exception as e:  # never lose a full pull to a missing parquet engine
        fallback = CLOSES.with_suffix(".csv")
        panel.to_csv(fallback)
        print(f"WARN parquet write failed ({e}); wrote CSV fallback -> {fallback}")
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


if __name__ == "__main__":
    main()
