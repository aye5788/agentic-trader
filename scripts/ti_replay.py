"""Piece-3 Phase-1 replay: did the short-term-momentum 'weakening' tag fire BEFORE
real stop-outs (giving the risk review an earlier exit), and does it discriminate
(low false-fire base rate)? Offline, no live path. Reads config/stopout_events.csv +
the cached daily closes; prints the go/no-go table.

    .venv/bin/python scripts/ti_replay.py [--window 5]
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import ti_signals as ti           # noqa: E402

CLOSES = REPO / "research_store" / "prices" / "closes.parquet"
EVENTS = REPO / "config" / "stopout_events.csv"


def first_fire(closes, spy, ticker, stop_date, window, key):
    """Earliest of the `window` trading days before stop_date on which compute()[key]
    == 'weakening'. Returns lead in trading days (>=1) or None."""
    if ticker not in closes.columns:
        return "no-data"
    idx = closes.index
    if stop_date not in idx:                      # snap to the last trading day <= stop
        prior = idx[idx <= stop_date]
        if len(prior) == 0:
            return None
        stop_date = prior[-1]
    si = idx.get_loc(stop_date)
    for lead in range(window, 0, -1):             # oldest day in the window first
        if si - lead < 0:
            continue
        d = idx[si - lead]
        tag = ti.compute(closes[ticker], spy, d).get(key)
        if tag == "weakening":
            return lead
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=5, help="trading days before stop to scan")
    args = ap.parse_args()

    if not CLOSES.exists():
        sys.exit("no price cache — run scripts/fetch_prices.py first")
    closes = pd.read_parquet(CLOSES).sort_index()
    spy = closes["SPY"]
    events = pd.read_csv(EVENTS)
    events["stop_date"] = pd.to_datetime(events["stop_date"])

    print(f"replay: {len(events)} events, window {args.window}d before stop\n")
    print(f"{'ticker':8}{'stop':12}{'MACD lead':>11}{'RSI lead':>10}")
    print("-" * 41)
    hits = {"tag_macd": [], "tag_rsi": []}
    for _, e in events.iterrows():
        lm = first_fire(closes, spy, e.ticker, e.stop_date, args.window, "tag_macd")
        lr = first_fire(closes, spy, e.ticker, e.stop_date, args.window, "tag_rsi")
        for key, v in (("tag_macd", lm), ("tag_rsi", lr)):
            if isinstance(v, int):
                hits[key].append(v)
        fmt = lambda v: (f"{v}d ahead" if isinstance(v, int) else str(v or "—"))
        print(f"{e.ticker:8}{str(e.stop_date.date()):12}{fmt(lm):>11}{fmt(lr):>10}")

    # false-fire base rate: fraction of ALL universe names flagged 'weakening' on the
    # event dates (if the tag fires on everything, a 'hit' means nothing).
    uni = [c for c in closes.columns if c != "SPY"]
    dates = sorted(events["stop_date"].map(
        lambda d: closes.index[closes.index <= d][-1]).unique())
    base = {"tag_macd": 0, "tag_rsi": 0, "n": 0}
    for d in dates:
        for t in uni:
            o = ti.compute(closes[t], spy, d)
            if o.get("tag_macd") == "n/a":
                continue
            base["n"] += 1
            base["tag_macd"] += o["tag_macd"] == "weakening"
            base["tag_rsi"] += o["tag_rsi"] == "weakening"

    print("\n--- summary ---")
    n_ev = len(events)
    for key, label in (("tag_macd", "MACD+RS"), ("tag_rsi", "RSI50+RS")):
        h = hits[key]
        hit_rate = len(h) / n_ev if n_ev else 0
        avg_lead = sum(h) / len(h) if h else 0
        ff = base[key] / base["n"] if base["n"] else 0
        print(f"{label:10} hit {len(h)}/{n_ev} ({hit_rate:.0%})  avg lead {avg_lead:.1f}d"
              f"  |  false-fire base rate {ff:.0%}")
    print("\nPASS if a pairing leads the stop by >=1-2d on a majority of events with a")
    print("false-fire base rate well below its hit rate. Else drop it (stops already work).")


if __name__ == "__main__":
    main()
