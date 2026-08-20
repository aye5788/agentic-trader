"""Assemble the survivorship-free candidate pool for the point-in-time backtest.

The single-name backtest universe (config/universe.csv) is TODAY's 150 liquid
names — survivorship-biased when run over history. This builds a broader pool
that INCLUDES names that were liquid in 2016-2026 and have since delisted, so a
point-in-time backtest can rank them in at each date and hold them into their
collapse (or acquisition) instead of pretending they never existed.

  pool = S&P-500 point-in-time members (2015-2026)     <- the dead-name backbone
       ∪ config/universe.csv  (our 150 survivors, incl. non-index liquid names)
       (equities only — funds were removed 2026-08-20; see main())
       ∪ HAND_DELISTED (liquid non-index blow-ups the S&P set misses)

S&P PIT membership comes from the fja05680/sp500 reconstruction (Wikipedia
change-log based) — the academic-standard free survivorship source. We only use
it to decide WHICH tickers to pull data for; actual universe selection at each
date is by trailing dollar-volume (scripts/backtest_pit.py).

    python scripts/build_pool.py   ->  config/pit_pool.csv

Output columns: ticker, source (sp500/seed/etf/hand), in_sp500_ever.
"""
import csv
import io
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "config" / "pit_pool.csv"

# fja05680/sp500 — "S&P 500 Historical Components & Changes (Updated).csv"
SP500_URL = ("https://raw.githubusercontent.com/fja05680/sp500/master/"
             "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv")
WINDOW_START = "2015-06-01"   # a little before the 2017 backtest start (252d warmup)

# Liquid non-index names that blew up / delisted and the S&P set won't contain.
# Momentum-relevant: each had large dollar-volume at its peak then went to ~0 or
# was taken out. The trailing-$-vol filter in the backtest decides if/when they
# actually qualify — this list just makes them REACHABLE.
HAND_DELISTED = ["BBBY", "FTCH", "WE", "WISH", "RIDE", "NKLA", "GME", "AMC",
                 "PTON", "HOOD", "COIN", "PLTR", "LCID", "CVNA"]


def load_sp500_pit() -> set[str]:
    print(f"downloading S&P PIT membership ...\n  {SP500_URL}")
    raw = urllib.request.urlopen(SP500_URL, timeout=60).read().decode("utf-8")
    rows = list(csv.reader(io.StringIO(raw)))[1:]   # skip header (date,tickers)
    ever: set[str] = set()
    snaps = 0
    for date, tickers in rows:
        if date >= WINDOW_START:
            ever |= set(tickers.split(","))
            snaps += 1
    print(f"  {snaps} change-snapshots since {WINDOW_START} -> {len(ever)} unique members")
    return ever


def read_col(path: Path) -> list[str]:
    return [line.split(",")[0].strip()
            for line in path.read_text().splitlines()[1:] if line.strip()]


def main() -> None:
    try:
        sp = load_sp500_pit()
    except Exception as e:
        sys.exit(f"failed to fetch S&P PIT list: {e}")

    seed = read_col(REPO / "config" / "universe.csv")

    # merge with provenance; a ticker's first-seen source wins the label, but we
    # record in_sp500_ever separately so the dead-name backbone is auditable.
    source: dict[str, str] = {}
    for t in sorted(sp):
        source.setdefault(t, "sp500")
    for t in seed:
        source.setdefault(t, "seed")
    # ⛔ NO FUNDS IN THE POOL (2026-08-20). This used to fold in all 18 of
    # config/etf_universe.csv. The pool is not just a backtest input: it is the
    # FALLBACK CANDIDATE POND for the weekly universe screen
    # (moomoo.research.candidate_pond), so every fund in it was a name the
    # screen could propose ADDING to config/universe.csv — which is the
    # order-gate whitelist. The sleeve is deleted; the pool is equities.
    for t in HAND_DELISTED:
        source.setdefault(t, "hand")

    rows = [(t, source[t], "yes" if t in sp else "no") for t in sorted(source)]
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "source", "in_sp500_ever"])
        w.writerows(rows)

    by_src: dict[str, int] = {}
    for _, s, _ in rows:
        by_src[s] = by_src.get(s, 0) + 1
    print(f"\npool -> {OUT}  ({len(rows)} names)")
    print("  by source:", ", ".join(f"{k}={v}" for k, v in sorted(by_src.items())))


if __name__ == "__main__":
    main()
