"""Pull daily close + dollar-volume for the whole survivorship-free pool
(config/pit_pool.csv) from ONE consistent source — Alpaca's free IEX feed —
and cache to research_store/prices/.

Why Alpaca IEX and not Schwab: Schwab serves consolidated volume and 10y of
history but 404s on delisted names (SIVB, TWTR, FRC ...). The point-in-time
backtest NEEDS the dead names, so we must use a source that has them. Alpaca's
IEX feed serves live AND dead names, but two caveats drive the design:

  1. FREE-TIER HISTORY FLOORS AT ~2020-07  -> backtest window is 2020-2026, not
     2017-2026. Still spans the 2022 bear + 2023 bank collapses (the stress the
     survivorship correction exists to test).
  2. IEX VOLUME IS ONLY ~2-5% OF CONSOLIDATED and the fraction varies. We rank
     the universe by trailing dollar-volume, so absolute level doesn't matter —
     only relative order — and because EVERY name (live and dead) uses the same
     IEX slice, there is no systematic bias against the dead names. Noisy, not
     biased. We smooth with a 63-day trailing mean in the backtest.

    python scripts/fetch_pool.py [--force] [--start 2020-07-01]

Writes research_store/prices/pool_closes.parquet (dates x tickers, adj close)
and pool_dvol.parquet (dates x tickers, close*volume). Both git-ignored.
"""
import argparse
import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "research_store" / "prices"
CLOSES = OUT / "pool_closes.parquet"
DVOL = OUT / "pool_dvol.parquet"
META = OUT / "pool_meta.csv"
BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"


def _headers() -> dict:
    for line in (REPO / ".env").read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    k, s = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not (k and s):
        sys.exit("missing ALPACA_API_KEY / ALPACA_SECRET_KEY in .env")
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}


def fetch(symbols: list[str], start: str, headers: dict) -> dict[str, list]:
    """Multi-symbol paged pull. Returns {sym: [bar, ...]} (bar has t,c,v)."""
    out: dict[str, list] = {s: [] for s in symbols}
    # chunk symbols so each request's symbol list stays sane; page each chunk.
    CH = 100
    for i in range(0, len(symbols), CH):
        chunk = symbols[i:i + CH]
        token = None
        while True:
            params = {"symbols": ",".join(chunk), "timeframe": "1Day",
                      "start": start, "feed": "iex", "adjustment": "all",
                      "limit": 10000}
            if token:
                params["page_token"] = token
            r = requests.get(BARS_URL, headers=headers, params=params, timeout=60)
            if not r.ok:
                print(f"  chunk {i//CH}: HTTP {r.status_code} {r.text[:100]}")
                break
            j = r.json()
            for sym, bars in (j.get("bars") or {}).items():
                out[sym].extend(bars)
            token = j.get("next_page_token")
            if not token:
                break
            time.sleep(0.35)   # free tier 200/min — stay well under
        print(f"  [{min(i+CH,len(symbols)):4}/{len(symbols)}] pulled through {chunk[-1]}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--start", default="2020-07-01")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    if CLOSES.exists() and not args.force:
        df = pd.read_parquet(CLOSES)
        print(f"cache exists: {CLOSES} ({df.shape[0]}x{df.shape[1]}). --force to re-pull.")
        return

    tickers = [t for t in pd.read_csv(REPO / "config" / "pit_pool.csv")["ticker"]]
    print(f"pulling {len(tickers)} pool names from Alpaca IEX, {args.start}+ ...")
    bars = fetch(tickers, args.start, _headers())

    close_series, dvol_series, meta = {}, {}, []
    for sym, bl in bars.items():
        if not bl:
            meta.append((sym, 0, "EMPTY"))
            continue
        idx = [pd.Timestamp(b["t"]).normalize() for b in bl]
        close_series[sym] = pd.Series([b["c"] for b in bl], index=idx)
        dvol_series[sym] = pd.Series([b["c"] * b["v"] for b in bl], index=idx)
        meta.append((sym, len(bl), "ok"))

    closes = pd.DataFrame(close_series).sort_index()
    dvol = pd.DataFrame(dvol_series).sort_index()
    closes.to_parquet(CLOSES)
    dvol.to_parquet(DVOL)
    pd.DataFrame(meta, columns=["ticker", "bars", "status"]).to_csv(META, index=False)

    ok = sum(1 for _, _, st in meta if st == "ok")
    empty = [t for t, _, st in meta if st != "ok"]
    print(f"\ndone: {ok}/{len(tickers)} ok")
    print(f"panel: {closes.shape[0]} dates x {closes.shape[1]} tickers, "
          f"{closes.index.min().date()}..{closes.index.max().date()}")
    if empty:
        print(f"EMPTY/no-data ({len(empty)}):", " ".join(empty))


if __name__ == "__main__":
    main()
