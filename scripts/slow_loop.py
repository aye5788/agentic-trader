"""SLOW LOOP — compute today's target book and write it to the Research Store.

This is the deterministic brain (docs/STRATEGY.md, "run as code, do not eyeball").
It does NOT trade. It produces the research product the fast loop later executes.

  1. Load recent closes for the 150 names + 18 ETFs + SPY.
  2. Regime floor: SPY (proxy $SPX) > 50DMA  AND  VIX <= [regime].vix_ceiling
     (VIX from a live Schwab $VIX quote, FRED VIXCLS prior close as fallback;
     both unavailable -> gate skipped FAIL-OPEN, the trend floor still rules).
  3. Rank both engines with the momentum signal (src/momentum.py).
  4. Select the book (top-10, banded) + sleeve (top-4).
  5. Attach IBD trade geometry per name (entry_zone, vol-adjusted stop, +5/+10%
     targets) and the reward:risk >= 2 gate — a name that can't be given >=2:1
     geometry is NOT held (STRATEGY.md §4); its slot stays cash.
  6. write_product() -> the Research Store validates against the [risk] mandate
     and persists current.json (belief) + journal.

    python scripts/slow_loop.py [--dry]   # --dry: print, don't write the store

Regime-off or nothing-eligible -> an empty book is a valid, intended state (cash).
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import momentum as mom          # noqa: E402
import strategy as strat        # noqa: E402
from research_store import write_product                       # noqa: E402
from research_store.models import Thesis, ResearchProduct      # noqa: E402
from research_store.validate import reward_risk                # noqa: E402

PANEL = REPO / "research_store" / "prices" / "closes.parquet"


def fetch_vix() -> tuple[float | None, str]:
    """Current VIX level for the regime ceiling: live Schwab '$VIX' quote first,
    FRED VIXCLS (prior close) as fallback. Returns (None, reason) when both
    fail — the caller treats that FAIL-OPEN (VIX gate skipped, trend floor
    still gates) because a data outage should not push the book to cash."""
    try:
        from adapters.schwab import research
        blk = research.get_quotes(["$VIX"]).get("$VIX", {})
        q = blk.get("quote", blk) or {}
        px = q.get("lastPrice") or q.get("closePrice")
        if px and float(px) > 0:
            return float(px), "Schwab $VIX live"
    except Exception as e:
        print(f"  VIX via Schwab failed ({type(e).__name__}) — trying FRED")
    try:
        from adapters.fred import indicators
        v = indicators.get_vix()
        if v and v.get("value") is not None:
            stale = " stale" if v.get("stale") else ""
            return float(v["value"]), f"FRED VIXCLS {v.get('date', '')}{stale}"
    except Exception as e:
        print(f"  VIX via FRED failed ({type(e).__name__})")
    return None, "unavailable (Schwab+FRED) — gate skipped fail-open"


def geometry(price: float, sigma: float, stop_mult: float, r_mults: list[float]) -> dict:
    """IBD trade geometry, made self-consistent by scaling BOTH stop and targets
    to the name's own volatility (params from [trade_management]). stop =
    stop_mult * daily-sigma below entry; targets at r_mults x that risk distance
    -> reward:risk >= r_mults[0] by construction. For a ~2%/day stock this is
    near IBD's classic stop/target sizing; a 5%/day semi scales up in step,
    instead of being rejected by a fixed +5% target it can never reach at 2:1."""
    entry = price
    stop_dist = stop_mult * float(sigma)                    # fractional risk
    stop = entry * (1.0 - stop_dist)
    targets = [round(entry * (1.0 + r * stop_dist), 4) for r in r_mults]
    return {"entry_zone": [round(price * 0.995, 4), round(price * 1.005, 4)],
            "stop": round(stop, 4), "targets": targets}


def build_theses(sel, scored, closes, asof, per_slot, tm, start_rank):
    """Turn a selection list into weighted Thesis records with geometry, dropping
    any name that can't clear reward:risk >= 2 (it doesn't get held). `tm` is the
    [trade_management] config table (stop_atr_mult, target_r_mults)."""
    held, dropped = [], []
    rank = start_rank
    for sym in sel:
        row = scored.loc[sym]
        price = float(closes.loc[asof, sym])
        g = geometry(price, row["sigma"], tm["stop_atr_mult"], tm["target_r_mults"])
        t = Thesis(symbol=sym, rank=rank, verdict="buy",
                   thesis=f"momentum rank {int(row['rank'])}: score {row['score']:.2f} "
                          f"(R={row['R']:.1%}, trend={row['trend']:.1%})",
                   entry_zone=g["entry_zone"], stop=g["stop"], targets=g["targets"],
                   target_weight=round(per_slot, 4),
                   confidence=round(float(row["score"]), 3),
                   signals={"score": round(float(row["score"]), 4),
                            "R": round(float(row["R"]), 4),
                            "trend": round(float(row["trend"]), 4),
                            "sigma": round(float(row["sigma"]), 5),
                            "rank": int(row["rank"]), "source": "momentum.compute"},
                   as_of=str(asof.date()),
                   review_by=f"{(asof + pd.Timedelta(days=7)).date()} (weekly rebalance)")
        rr = reward_risk(t)
        if rr is None or rr < strat.load()["risk"]["min_reward_risk"]:
            t.verdict, t.target_weight = "avoid", 0.0
            dropped.append((sym, rr, row["sigma"]))
        else:
            held.append(t)
        rank += 1
    return held, dropped


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="print, don't write the store")
    args = ap.parse_args()

    if not PANEL.exists():
        sys.exit("no price cache — run scripts/fetch_prices.py first")
    cfg = strat.load()
    P, TM = cfg["portfolio"], cfg["trade_management"]

    closes = pd.read_parquet(PANEL).sort_index()
    names = [t for t in pd.read_csv(REPO / "config" / "universe.csv")["ticker"] if t in closes]
    etfs = [t for t in pd.read_csv(REPO / "config" / "etf_universe.csv")["ticker"] if t in closes]
    asof = closes.index[-1]
    spy = closes["SPY"]

    # exclude names the intraday monitor stopped out (cooldown) so we don't rebuy
    # them the next morning — the stop vs. momentum-rank churn guard.
    cd_path = REPO / "research_store" / "monitor" / "cooldown.json"
    if cd_path.exists():
        import json as _json
        cd = _json.loads(cd_path.read_text())
        cooled = {s for s, until in cd.items() if until >= str(asof.date())}
        if cooled:
            names = [t for t in names if t not in cooled]
            etfs = [t for t in etfs if t not in cooled]
            print(f"cooldown: excluding {sorted(cooled)} until their date")

    trend = mom.regime_on(spy, asof, cfg["regime"]["trend_ma_days"])
    vix, vix_src = fetch_vix()
    ceiling = float(cfg["regime"]["vix_ceiling"])
    vix_ok = vix is None or vix <= ceiling          # None = data outage -> fail-open
    regime = trend and vix_ok
    book_scored = mom.compute(closes[names], asof)
    etf_scored = mom.compute(closes[etfs], asof)

    book_sel = mom.select(book_scored, set(), P["book_hold"], P["book_band"])
    etf_sel = mom.select(etf_scored, set(), P["sleeve_hold"], P["sleeve_hold"])
    if not regime:
        book_sel = []   # regime-off: no new single-name entries (first run = none held)

    per_book = P["book_weight"] / P["book_hold"]
    per_etf = P["sleeve_weight"] / P["sleeve_hold"]
    book_held, book_drop = build_theses(book_sel, book_scored, closes, asof, per_book, TM, 1)
    etf_held, etf_drop = build_theses(etf_sel, etf_scored, closes, asof, per_etf, TM, 100)

    theses = book_held + etf_held
    product = ResearchProduct(
        as_of=str(asof.date()), theses=theses,
        regime={"status": "on" if regime else "off",
                "floor": f"SPY>{cfg['regime']['trend_ma_days']}DMA={trend}",
                "vix": (f"{vix:.1f}{'<=' if vix_ok else '>'}{ceiling:g}"
                        f"{'' if vix_ok else ' CEILING BREACHED'} ({vix_src})"
                        if vix is not None else f"n/a — {vix_src}"),
                "notes": ""},
        notes=f"slow_loop dual-momentum book as of {asof.date()}")

    # ---- report ----
    print(f"as_of {asof.date()} | regime {'ON' if regime else 'OFF (cash)'} "
          f"(trend={trend}, vix={f'{vix:.1f}' if vix is not None else 'n/a'}/{ceiling:g}) | "
          f"book {len(book_held)}/{P['book_hold']} held, sleeve {len(etf_held)}/{P['sleeve_hold']} held")
    print(f"\n{'BOOK (top-10, R:R>=2 gated)':40}{'weight':>8}{'entry':>9}{'stop':>9}{'R:R':>6}")
    for t in book_held:
        print(f"  {t.symbol:<8}{t.thesis[:28]:<30}{t.target_weight:>8.2%}"
              f"{(t.entry_zone[0]+t.entry_zone[1])/2:>9.2f}{t.stop:>9.2f}{reward_risk(t):>6.2f}")
    if book_drop:
        print(f"  -- dropped for geometry (stop too wide for 2:1): "
              + ", ".join(f"{s}(σ={sg:.1%})" for s, _, sg in book_drop))
    print(f"\n{'SLEEVE (top-4)':40}{'weight':>8}{'entry':>9}{'stop':>9}{'R:R':>6}")
    for t in etf_held:
        print(f"  {t.symbol:<8}{t.thesis[:28]:<30}{t.target_weight:>8.2%}"
              f"{(t.entry_zone[0]+t.entry_zone[1])/2:>9.2f}{t.stop:>9.2f}{reward_risk(t):>6.2f}")
    if etf_drop:
        print("  -- dropped: " + ", ".join(s for s, _, _ in etf_drop))
    total_w = sum(t.target_weight for t in theses)
    print(f"\ntotal invested weight {total_w:.0%} | cash {1-total_w:.0%}")

    if args.dry:
        print("\n[--dry] not written to store")
        return
    write_product(product, mandate=strat.risk_mandate(cfg))
    print("\nwritten to Research Store (current.json + journal)")


if __name__ == "__main__":
    main()
