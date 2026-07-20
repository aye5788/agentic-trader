"""Point-in-time (survivorship-corrected) walk-forward backtest.

The difference from scripts/backtest.py: the single-name universe is NOT today's
150 survivors held fixed. At EACH weekly rebalance we rank the whole pool
(config/pit_pool.csv — includes names that have since delisted) by trailing
dollar-volume and take the top 150 AS OF THAT DATE. A name that was liquid in
2021 and died in 2023 is therefore eligible in 2021-2022 and the book can hold
it straight into its collapse — exactly what the biased backtest could never do.

Everything else (the momentum signal, the 70/30 book/sleeve, the band, the
regime floor) is identical and reuses scripts/backtest.py + src/momentum.py.

    python scripts/backtest_pit.py

Caveats that remain (honestly): (1) window is 2020-07+ (Alpaca IEX free-tier
floor), ~5y post-warmup, shorter/less powerful than the biased 9y run; (2) the
liquidity rank uses IEX-slice volume — a consistent but noisy proxy for
consolidated dollar-volume (no dead-name bias, just noise, smoothed 63d);
(3) pre-2020 delistings are absent (irrelevant — window starts 2020); (4) the
dead-name backbone is S&P-500 PIT membership + a hand-list, so a liquid non-index
name that died and we didn't list could still be missed. This is a strong
correction, not a perfect one.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))
import momentum as mom       # noqa: E402
import strategy as strat     # noqa: E402
import backtest as bt        # noqa: E402  (weekly_rebalance_dates, annualized, max_drawdown, simulate helpers)
import concentration as conc  # noqa: E402

PRICES = REPO / "research_store" / "prices"
DVOL_WINDOW = 63             # trailing days to smooth the noisy IEX volume
UNIV_SIZE = 150              # top-N by dollar-volume, matching config/universe size


def pit_universe(dvol: pd.DataFrame, candidates: list[str], asof, closes: pd.DataFrame,
                 size=UNIV_SIZE) -> list[str]:
    """Top-`size` single names by trailing-DVOL_WINDOW mean dollar-volume as of
    `asof`, restricted to names with enough price history for the momentum
    lookback. Pure point-in-time: only data <= asof is used."""
    hist_dv = dvol.loc[:asof, candidates].tail(DVOL_WINDOW)
    if hist_dv.empty:
        return []
    liq = hist_dv.mean().dropna()
    # require a full momentum lookback of prices as of asof
    price_hist = closes.loc[:asof, candidates]
    enough = price_hist.notna().sum()
    liq = liq[[t for t in liq.index if enough.get(t, 0) >= mom.LOOKBACK + 1]]
    return liq.sort_values(ascending=False).head(size).index.tolist()


def _load_pit_data():
    """Load the survivorship-free pool + config once, shared by main() and
    run_sweep(). Returns (closes, dvol, candidates, etfs, spy, etf_panel, rebals, P)."""
    if not (PRICES / "pool_closes.parquet").exists():
        sys.exit("no pool cache — run scripts/fetch_pool.py first")
    cfg = strat.load()
    P = cfg["portfolio"]

    closes = pd.read_parquet(PRICES / "pool_closes.parquet").sort_index()
    dvol = pd.read_parquet(PRICES / "pool_dvol.parquet").sort_index()
    pool = pd.read_csv(REPO / "config" / "pit_pool.csv")
    etfs = [t for t in pd.read_csv(REPO / "config" / "etf_universe.csv")["ticker"] if t in closes]
    # single-name candidates = pool minus the ETF sleeve (and minus SPY proxy)
    etfset = set(etfs) | {"SPY"}
    candidates = [t for t in pool["ticker"] if t in closes and t not in etfset]
    spy = closes["SPY"]
    etf_panel = closes[etfs]

    rebals = bt.weekly_rebalance_dates(closes.index)
    rebals = [d for d in rebals if len(closes.loc[:d]) >= mom.LOOKBACK + 5]
    return closes, dvol, candidates, etfs, spy, etf_panel, rebals, P


def run_backtest(closes, dvol, candidates, etfs, spy, etf_panel, rebals, P, cap_params=None):
    """One PIT backtest run. cap_params=None -> baseline (equal slots, no cap);
    a params dict -> concentration.cap_weights applied to each week's weights."""
    book_w, sleeve_w = P["book_weight"], P["sleeve_weight"]
    book_hold, book_band, sleeve_hold = P["book_hold"], P["book_band"], P["sleeve_hold"]
    per_slot_book = book_w / book_hold
    per_slot_etf = sleeve_w / sleeve_hold

    held_book, held_etf = set(), set()
    equity = 1.0
    curve, turnover, univ_sizes, dead_held = [], [], [], []
    for i in range(len(rebals) - 1):
        t0, t1 = rebals[i], rebals[i + 1]
        univ = pit_universe(dvol, candidates, t0, closes)
        univ_sizes.append(len(univ))
        regime = mom.regime_on(spy, t0, 50)

        book_scored = mom.compute(closes[univ], t0)
        new_book = mom.select(book_scored, held_book, book_hold, book_band)
        if not regime:
            new_book = [t for t in new_book if t in held_book]
        etf_scored = mom.compute(etf_panel, t0)
        new_etf = mom.select(etf_scored, held_etf, sleeve_hold, sleeve_hold)

        turnover.append(len(set(new_book) ^ held_book) + len(set(new_etf) ^ held_etf))
        held_book, held_etf = set(new_book), set(new_etf)

        # equal slots -> optional concentration cap -> per-name weights
        weights = {t: per_slot_book for t in new_book}
        weights.update({t: per_slot_etf for t in new_etf})
        if cap_params is not None:
            weights = conc.cap_weights(weights, closes, t0, cap_params)

        # realize t0 -> t1; a name that delists mid-week has NaN at t1 -> we mark
        # it a total loss of that slot (held into the void), not a free exit.
        def name_ret(t):
            p0 = closes.loc[t0, t] if t in closes.columns else np.nan
            p1 = closes.loc[t1, t] if t in closes.columns else np.nan
            if pd.isna(p0) or p0 <= 0:
                return 0.0
            return -1.0 if pd.isna(p1) else (p1 / p0 - 1)

        # count names we held whose price vanished by t1 (survivorship in action)
        dead_held.append(sum(1 for t in new_book
                             if pd.notna(closes.loc[t0, t]) and pd.isna(closes.loc[t1, t])))
        port_ret = sum(wt * name_ret(t) for t, wt in weights.items())
        equity *= (1 + port_ret)
        curve.append((t1, equity, port_ret, len(new_book), len(new_etf), regime))

    res = pd.DataFrame(curve, columns=["date", "equity", "ret", "n_book", "n_etf", "regime"]
                       ).set_index("date")
    cagr, vol, sharpe = bt.annualized(res["ret"], 52.0)
    return {"res": res, "cagr": cagr, "vol": vol, "sharpe": sharpe,
            "maxdd": bt.max_drawdown(res["equity"]),
            "total_return": res["equity"].iloc[-1] - 1,
            "avg_turnover": float(np.mean(turnover)),
            "univ_sizes": univ_sizes, "dead_held": dead_held,
            "regime_frac": res["regime"].mean()}


def main() -> None:
    (closes, dvol, candidates, etfs, spy, etf_panel, rebals, P) = _load_pit_data()
    print(f"pool: {len(candidates)} single-name candidates + {len(etfs)} ETFs | "
          f"{len(rebals)} weekly rebalances {rebals[0].date()}..{rebals[-1].date()}")

    r = run_backtest(closes, dvol, candidates, etfs, spy, etf_panel, rebals, P, None)
    res = r["res"]
    bench = spy.loc[res.index[0]:res.index[-1]].reindex(res.index).ffill()
    bench_eq = bench / bench.iloc[0]

    scagr, svol, ssharpe = r["cagr"], r["vol"], r["sharpe"]
    bcagr, bvol, bsharpe = bt.annualized(bench_eq.pct_change().dropna(), 52.0)

    print("\n" + "=" * 62)
    print("POINT-IN-TIME BACKTEST  (survivorship-corrected, weekly, 70/30)")
    print("=" * 62)
    print(f"period          : {res.index[0].date()} .. {res.index[-1].date()}  "
          f"({len(res)} weeks)")
    print(f"{'':16}{'PIT STRAT':>12}{'SPY B&H':>12}")
    print(f"{'total return':16}{res['equity'].iloc[-1]-1:>11.1%}{bench_eq.iloc[-1]-1:>12.1%}")
    print(f"{'CAGR':16}{scagr:>11.1%}{bcagr:>12.1%}")
    print(f"{'volatility':16}{svol:>11.1%}{bvol:>12.1%}")
    print(f"{'Sharpe (rf=0)':16}{ssharpe:>11.2f}{bsharpe:>12.2f}")
    print(f"{'max drawdown':16}{bt.max_drawdown(res['equity']):>11.1%}{bt.max_drawdown(bench_eq):>12.1%}")
    print(f"{'avg turnover/wk':16}{r['avg_turnover']:>11.1f}{'—':>12}")
    print(f"{'pct weeks reg-on':16}{r['regime_frac']:>11.0%}{'—':>12}")
    print(f"{'avg universe':16}{np.mean(r['univ_sizes']):>11.0f}{'—':>12}")
    print(f"{'held-into-death':16}{sum(r['dead_held']):>11}{'—':>12}  (slots that delisted while held)")
    print("=" * 62)
    print("survivorship-CORRECTED: pool includes since-delisted names, ranked in")
    print("by point-in-time liquidity. Window 2020-07+ (Alpaca free-tier floor);")
    print("liquidity uses noisy IEX-slice volume (consistent, so unbiased).")

    out = PRICES / "backtest_pit_equity.csv"
    res.join(bench_eq.rename("spy_equity")).to_csv(out)
    print(f"\nequity curve -> {out}")


if __name__ == "__main__":
    main()
