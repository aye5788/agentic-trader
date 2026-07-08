"""Walk-forward backtest of the dual-momentum strategy (docs/STRATEGY.md).

Reads the cached close panel (scripts/fetch_prices.py), rebalances WEEKLY, and
simulates the two-engine book (70% single-names / 30% ETF sleeve) with the
absolute gate, banded selection, and the SPY>50DMA regime floor. Benchmarks vs
SPY buy-and-hold.

  python scripts/backtest.py

⚠️ SURVIVORSHIP BIAS: the universe is TODAY's 150 liquid names. Backtesting a
present-day list over history is optimistic — the losers that would have been
dropped aren't here. Read results as an UPPER BOUND / mechanics check, not truth.

v1 simplification: intra-week stop / 21-day-MA exits are NOT modeled — only the
weekly rebalance, absolute gate, band, and regime floor. Omitting stops if
anything *understates* drawdown control, so this is a conservative first pass.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import momentum as mom  # noqa: E402
import strategy as strat  # noqa: E402

PANEL = REPO / "research_store" / "prices" / "closes.parquet"


def weekly_rebalance_dates(idx: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """Last trading day of each ISO week in the index."""
    s = pd.Series(idx, index=idx)
    return [g.iloc[-1] for _, g in s.groupby([idx.isocalendar().year, idx.isocalendar().week])]


def max_drawdown(equity: pd.Series) -> float:
    return float((equity / equity.cummax() - 1).min())


def annualized(returns: pd.Series, periods_per_year: float):
    if returns.std() == 0 or returns.empty:
        return 0.0, 0.0, 0.0
    cagr = (1 + returns).prod() ** (periods_per_year / len(returns)) - 1
    vol = returns.std() * np.sqrt(periods_per_year)
    sharpe = (returns.mean() / returns.std()) * np.sqrt(periods_per_year)  # rf=0
    return float(cagr), float(vol), float(sharpe)


def load_panel() -> pd.DataFrame:
    """Read the cached close panel — parquet, or the CSV fallback if the pull
    landed there (missing parquet engine)."""
    if PANEL.exists():
        return pd.read_parquet(PANEL)
    csv = PANEL.with_suffix(".csv")
    if csv.exists():
        return pd.read_csv(csv, index_col=0, parse_dates=True)
    sys.exit("no price cache — run scripts/fetch_prices.py first")


def simulate(panel, names, etfs, spy, *, lookback=mom.LOOKBACK,
             book_hold=10, book_band=15, sleeve_hold=4,
             book_w=0.70, sleeve_w=0.30, use_regime=True, use_sleeve=True):
    """Run one weekly walk-forward pass and return (metrics, res_df).

    All strategy knobs are parameters so the sweep can vary one at a time.
    `use_sleeve=False` routes 100% to the single-name book; `use_regime=False`
    removes the SPY>50DMA entry floor. `metrics` = dict of headline stats."""
    if not use_sleeve:
        book_w, sleeve_w = 1.0, 0.0
    name_panel = panel[names]
    etf_panel = panel[etfs]

    rebals = weekly_rebalance_dates(panel.index)
    rebals = [d for d in rebals if len(panel.loc[:d]) >= lookback + 5]

    per_slot_book = book_w / book_hold
    per_slot_etf = (sleeve_w / sleeve_hold) if sleeve_hold else 0.0

    held_book, held_etf = set(), set()
    equity = 1.0
    curve, turnover = [], []
    for i in range(len(rebals) - 1):
        t0, t1 = rebals[i], rebals[i + 1]

        regime = mom.regime_on(spy, t0, 50) if use_regime else True
        book_scored = mom.compute(name_panel, t0, lookback)
        new_book = mom.select(book_scored, held_book, book_hold, book_band)
        if not regime:
            new_book = [t for t in new_book if t in held_book]

        if use_sleeve:
            etf_scored = mom.compute(etf_panel, t0, lookback)
            new_etf = mom.select(etf_scored, held_etf, sleeve_hold, sleeve_hold)
        else:
            new_etf = []

        turnover.append(len(set(new_book) ^ held_book) + len(set(new_etf) ^ held_etf))
        held_book, held_etf = set(new_book), set(new_etf)

        def leg_ret(holds, pnl_panel, per_slot):
            r = 0.0
            for t in holds:
                p0, p1 = pnl_panel.loc[t0, t], pnl_panel.loc[t1, t]
                if pd.notna(p0) and pd.notna(p1) and p0 > 0:
                    r += per_slot * (p1 / p0 - 1)
            return r

        port_ret = leg_ret(new_book, name_panel, per_slot_book) + \
            leg_ret(new_etf, etf_panel, per_slot_etf)
        equity *= (1 + port_ret)
        curve.append((t1, equity, port_ret, len(new_book), len(new_etf), regime))

    res = pd.DataFrame(curve, columns=["date", "equity", "ret", "n_book", "n_etf", "regime"]
                       ).set_index("date")
    cagr, vol, sharpe = annualized(res["ret"], 52.0)
    metrics = {
        "total": float(res["equity"].iloc[-1] - 1), "cagr": cagr, "vol": vol,
        "sharpe": sharpe, "maxdd": max_drawdown(res["equity"]),
        "turnover": float(np.mean(turnover)), "reg_on": float(res["regime"].mean()),
        "weeks": len(res),
    }
    return metrics, res


def main() -> None:
    cfg = strat.load()
    P = cfg["portfolio"]

    panel = load_panel().sort_index()
    names = [t for t in pd.read_csv(REPO / "config" / "universe.csv")["ticker"] if t in panel]
    etfs = [t for t in pd.read_csv(REPO / "config" / "etf_universe.csv")["ticker"] if t in panel]
    spy = panel["SPY"]

    m, res = simulate(
        panel, names, etfs, spy,
        book_hold=P["book_hold"], book_band=P["book_band"], sleeve_hold=P["sleeve_hold"],
        book_w=P["book_weight"], sleeve_w=P["sleeve_weight"],
    )
    print(f"universe: {len(names)} names + {len(etfs)} ETFs | "
          f"{len(res)} weekly rebalances {res.index[0].date()}..{res.index[-1].date()}")

    bench = spy.loc[res.index[0]:res.index[-1]].reindex(res.index).ffill()
    bench_eq = bench / bench.iloc[0]
    bcagr, bvol, bsharpe = annualized(bench_eq.pct_change().dropna(), 52.0)
    scagr, svol, ssharpe = m["cagr"], m["vol"], m["sharpe"]

    print("\n" + "=" * 60)
    print("DUAL-MOMENTUM BACKTEST  (weekly, 70/30 book/sleeve)")
    print("=" * 60)
    print(f"period          : {res.index[0].date()} .. {res.index[-1].date()}  "
          f"({len(res)} weeks)")
    print(f"{'':16}{'STRATEGY':>12}{'SPY B&H':>12}")
    print(f"{'total return':16}{res['equity'].iloc[-1]-1:>11.1%}{bench_eq.iloc[-1]-1:>12.1%}")
    print(f"{'CAGR':16}{scagr:>11.1%}{bcagr:>12.1%}")
    print(f"{'volatility':16}{svol:>11.1%}{bvol:>12.1%}")
    print(f"{'Sharpe (rf=0)':16}{ssharpe:>11.2f}{bsharpe:>12.2f}")
    print(f"{'max drawdown':16}{max_drawdown(res['equity']):>11.1%}{max_drawdown(bench_eq):>12.1%}")
    print(f"{'avg turnover/wk':16}{m['turnover']:>11.1f}{'—':>12}")
    print(f"{'pct weeks reg-on':16}{m['reg_on']:>11.0%}{'—':>12}")
    print("=" * 60)
    print("⚠️ survivorship-biased (today's names, run over history) — upper bound.")
    print("   intra-week stops/MA-exits not modeled (conservative on drawdown).")

    out = REPO / "research_store" / "prices" / "backtest_equity.csv"
    res.join(bench_eq.rename("spy_equity")).to_csv(out)
    print(f"\nequity curve -> {out}")


if __name__ == "__main__":
    main()
