"""Adaptive tuner for stop_atr_mult — the off-box weekly payload.

Builds per-candidate realized-R evidence by replaying PIT-pool entries under each
grid multiple (walk-forward: most recent fold held out for the overfit gap), folds
in live stop-outs from the ledger, runs the Bayesian estimator, and writes a
PROPOSAL. Never promotes, never trades (spec §3, §8).

    python scripts/tune_stop.py [--selftest]

Spec: docs/superpowers/specs/2026-07-23-adaptive-input-layer-design.md §6
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from stop_replay import replay_position          # noqa: E402
import adaptive                                   # noqa: E402

GRID = [1.5, 2.0, 2.5, 3.0, 3.5]
BAND = (1.5, 3.5)
INCUMBENT = 2.5
PROPOSAL = REPO / "research_store" / "adaptive" / "proposals" / "stop_atr_mult.json"


def build_samples(entries, panels, grid, tm):
    """entries: list of dicts {sym, entry_price, sigma, fwd_highs, fwd_lows, fwd_closes}.
    Returns (mean_r[len(grid)], count[len(grid)]) — mean realized-R per candidate
    multiple. target/horizon come from `tm` (trade_management config)."""
    target_mult = float(tm["target_r_mults"][0])
    horizon = int(tm.get("ma_exit_days", 21))
    sums = np.zeros(len(grid)); counts = np.zeros(len(grid))
    for e in entries:
        for gi, m in enumerate(grid):
            stop = e["entry_price"] - m * e["sigma"]
            target = e["entry_price"] + target_mult * (e["entry_price"] - stop)
            r = replay_position(e["entry_price"], stop, target,
                                e["fwd_highs"], e["fwd_lows"], e["fwd_closes"], horizon)
            sums[gi] += r["realized_r"]; counts[gi] += 1
    mean_r = np.divide(sums, counts, out=np.zeros_like(sums), where=counts > 0)
    return mean_r, counts


def generate_entries(panels, pool_tickers, tm, portfolio, fold_days=252):
    """Reconstruct historical entries over the PIT pool: at each weekly rebalance
    date (regime-on dates only — production holds cash otherwise), the book-only
    names momentum.select would have bought, their entry close, momentum's own
    trailing sigma (converted to dollar risk-per-share), and the forward OHLC
    window. Reuses the SAME pure signal + selection the live loop uses
    (src/momentum.py: compute/select/regime_on) so replay entries match
    production exactly, and reproduces production geometry algebraically
    (stop = entry - m*frac_sigma*entry; see build_samples's dollar-sigma stop).

    `pool_tickers` should already be restricted to the single-name candidate
    pool (ETF sleeve + SPY excluded) that exist in the price panel — mirrors
    scripts/backtest_pit.py's candidate construction.

    Returns list[dict] with keys sym, entry_price, sigma, fwd_highs, fwd_lows,
    fwd_closes, entry_date (ISO). See momentum.compute/select for the ranking."""
    import momentum

    closes, highs, lows = panels["close"], panels["high"], panels["low"]
    spy = closes["SPY"]
    candidates = [t for t in pool_tickers if t in closes.columns
                  and t in highs.columns and t in lows.columns]
    panel = closes[candidates]

    horizon = int(tm.get("ma_exit_days", 21))
    book_hold = int(portfolio["book_hold"])
    book_band = int(portfolio["book_band"])

    entries = []
    dates = list(closes.index)
    # weekly cadence: step 5 trading days; leave a forward window for replay
    for di in range(fold_days, len(dates) - 5, 5):
        asof = dates[di]
        if not momentum.regime_on(spy, asof, 50):
            continue          # production holds cash on regime-off: no entries
        scored = momentum.compute(panel, asof)
        if scored.empty:
            continue
        sel = momentum.select(scored, set(), book_hold, book_band)
        for sym in sel:
            entry_price = float(closes.loc[asof, sym])
            frac_sigma = float(scored.loc[sym, "sigma"])     # 252d fractional daily sigma
            sigma = frac_sigma * entry_price                  # dollar risk-per-share
            fwd = slice(di + 1, di + 1 + horizon + 5)
            fwd_highs = list(highs[sym].iloc[fwd].values)
            fwd_lows = list(lows[sym].iloc[fwd].values)
            fwd_closes = list(closes[sym].iloc[fwd].values)
            if not fwd_highs or not fwd_lows or not fwd_closes:
                continue
            entries.append({
                "sym": sym, "entry_price": entry_price, "sigma": sigma,
                "fwd_highs": fwd_highs, "fwd_lows": fwd_lows, "fwd_closes": fwd_closes,
                "entry_date": str(asof.date()),
            })
    return entries


def _load_panels():
    import pandas as pd
    p = REPO / "research_store" / "prices"
    return {"close": pd.read_parquet(p / "closes.parquet"),
            "high": pd.read_parquet(p / "highs.parquet"),
            "low": pd.read_parquet(p / "lows.parquet")}


def _pool_candidates(closes):
    """Single-name candidate pool: pit_pool.csv tickers present in the price
    panel, minus the ETF sleeve and the SPY regime proxy — mirrors
    scripts/backtest_pit.py's candidate construction so the tuner ranks the
    same universe the PIT backtest (and, restricted to today's 150, the live
    slow loop) does."""
    import pandas as pd
    pool = pd.read_csv(REPO / "config" / "pit_pool.csv")["ticker"].tolist()
    etfs = set(pd.read_csv(REPO / "config" / "etf_universe.csv")["ticker"])
    exclude = etfs | {"SPY"}
    return [t for t in pool if t not in exclude and t in closes.columns]


def _live_stop_samples(grid):
    """Fold live stop-outs from the ledger into the grid bucket matching the stop
    multiple in force at the time. Returns (sum_r[len], count[len]). Empty until
    live outcomes exist — the posterior simply leans on replay until then."""
    import ledger  # noqa: F401  (reserved for realized_history join; live_n=0 first cut)
    return np.zeros(len(grid)), np.zeros(len(grid))


def main():
    import datetime as dt
    import strategy
    cfg = strategy.load()
    tm = cfg["trade_management"]
    portfolio = cfg["portfolio"]
    panels = _load_panels()
    pool = _pool_candidates(panels["close"])
    entries = generate_entries(panels, pool, tm, portfolio)

    # walk-forward: hold out the most recent 20% of entries by date.
    entries.sort(key=lambda e: e["entry_date"])
    cut = int(len(entries) * 0.8)
    train, holdout = entries[:cut], entries[cut:]

    tr_mean, tr_cnt = build_samples(train, panels, GRID, tm)
    ho_mean, _ = build_samples(holdout, panels, GRID, tm) if holdout else (np.zeros(len(GRID)), None)

    live_sum, live_cnt = _live_stop_samples(GRID)
    tot_cnt = tr_cnt + live_cnt
    tot_mean = np.divide(tr_mean * tr_cnt + live_sum, tot_cnt,
                         out=np.zeros(len(GRID)), where=tot_cnt > 0)

    pm, pv, pc = adaptive.posterior(
        np.array(GRID), tot_mean, tot_cnt, noise_var=1.0,
        prior_mean=float(np.average(tot_mean, weights=tot_cnt)) if tot_cnt.sum() else 0.0,
        length_scale=0.6, prior_var=1.0)
    inc_idx = GRID.index(INCUMBENT)
    rec = adaptive.recommend(np.array(GRID), pm, pc, inc_idx, confidence=0.9)
    gap = adaptive.oos_gap(tr_mean, ho_mean, rec["recommended_idx"])

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    art = {
        "knob": "trade_management.stop_atr_mult", "generated_at": now,
        "incumbent": INCUMBENT, "recommended": rec["recommended_value"],
        "moved": rec["moved"], "p_better": round(rec["p_better"], 4), "band": list(BAND),
        "grid": GRID, "posterior_mean": [round(float(x), 4) for x in pm],
        "evidence": {"replay_n": int(tr_cnt.sum()), "live_n": int(live_cnt.sum())},
        "oos_gap": round(gap, 4),
        "rationale": (f"moved to {rec['recommended_value']} (p={rec['p_better']:.2f})"
                      if rec["moved"] else
                      f"incumbent {INCUMBENT} retained (best challenger p={rec['p_better']:.2f} < 0.90)"),
        "provenance": (f"adaptive layer {now[:10]} from replay_n={int(tr_cnt.sum())} "
                       f"live_n={int(live_cnt.sum())}; incumbent was {INCUMBENT}"),
    }
    assert BAND[0] <= art["recommended"] <= BAND[1], art     # hard band guard
    PROPOSAL.parent.mkdir(parents=True, exist_ok=True)
    PROPOSAL.write_text(json.dumps(art, indent=2))
    print(f"wrote proposal -> {PROPOSAL}\n  {art['rationale']}")


def _selftest() -> None:
    # Two synthetic entries: a name that runs up (rewards a wide stop) and a
    # chop name that whipsaws (rewards a wider stop too). Wider multiple should
    # score >= tighter here, and build_samples must count every entry per grid pt.
    tm = {"target_r_mults": [2.2], "ma_exit_days": 5}
    entries = [
        {"sym": "RUN", "entry_price": 100.0, "sigma": 2.0,
         "fwd_highs": [103, 106, 110, 115, 120], "fwd_lows": [99, 101, 104, 108, 112],
         "fwd_closes": [102, 105, 109, 114, 119]},
        {"sym": "CHOP", "entry_price": 50.0, "sigma": 1.0,
         "fwd_highs": [51, 50, 52, 51, 53], "fwd_lows": [48.5, 48.0, 49, 48.2, 49],
         "fwd_closes": [49, 49.5, 50, 49.5, 51]},
    ]
    mean_r, counts = build_samples(entries, panels=None, grid=GRID, tm=tm)
    assert list(counts) == [2, 2, 2, 2, 2], counts        # every entry, every grid pt
    assert mean_r[-1] >= mean_r[0] - 1e-9, mean_r          # wider >= tighter here
    print("selftest OK: build_samples counts + wide-stop ordering")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
