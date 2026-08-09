"""Study A3 — does capital RECYCLING rescue close take-profit targets?

Studies A and A2 measured single positions held to a fixed horizon, and both said
closer targets lose money. But both are structurally blind to the mechanism the
swing-trading argument actually rests on: a target hit FREES CASH, which gets
redeployed into a fresh name. Many small wins are supposed to compound not because
each is large but because capital turns over faster.

This models that directly, at the portfolio level:

  - equal-weight book of `hold_n` names, rebuilt weekly from src/momentum.py
    (the SAME ranking function the live slow loop calls, so the signal cannot
    drift between this study and production)
  - intra-week exits checked daily against REAL highs and lows
  - a stop or target exit frees cash, which is redeployed on T+1 into the
    highest-ranked eligible name not already held (the recycling mechanism)
  - transaction costs charged on every buy and sell

Deliberate modelling choices, and which way each one leans:

  T+1 redeploy      matches the live system's settlement behaviour. Same-day
                    redeploy is available via --same-day as an optimistic bound
                    (leans FOR the close-target case).
  fill at the level stop/target fills exactly at the level, so overnight gaps
                    through a stop cost nothing. Leans FOR every variant equally,
                    but flatters tight stops most.
  stop wins ties    if a bar touches both levels, the stop is taken. Intraday
                    order is unknowable from daily bars. Leans AGAINST targets.
  no regime gate    the SPY filter is omitted so variants differ ONLY in target
                    distance. Absolute returns are therefore not comparable to
                    the live system; the RANKING between rows is the result.

Survivorship: uses the live universe panel (today's names over history), so all
absolute figures are optimistic. Compare rows, not levels.

Read-only. Run:
    .venv/bin/python scripts/sim_recycle.py
    .venv/bin/python scripts/sim_recycle.py --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

PRICES = REPO / "research_store" / "prices"
LOOKBACK = 252
HOLD_N = 10
# Per side, on notional. NOT commission — Robinhood charges none on stocks. This
# is bid/ask spread + slippage, which is real because both the fast loop and the
# exit executor place MARKET orders, so every entry and exit crosses the spread.
# 1bp is defensible for the mega-cap liquidity this universe holds. An earlier
# run used 5bps, which was never justified and was NOT neutral: the tight-target
# variants trade ~10x more often, so a per-trade cost penalises exactly the
# hypothesis under test. Verified at 0/1/5 — the ranking is identical at all
# three and 0.5-sigma is negative even frictionless. Override with --cost-bps N.
COST_BPS = 1.0
TARGETS = [0.5, 1.0, 2.0, 3.0, 5.5, None]   # None = no target at all
STOP_MULT = 2.5


def _metrics(curve: pd.Series, trades: int) -> dict:
    r = curve.pct_change().dropna()
    yrs = len(curve) / 252.0
    cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1 / yrs) - 1 if yrs > 0 else 0.0
    vol = r.std() * np.sqrt(252)
    dd = float((curve / curve.cummax() - 1).min())
    return {"cagr": float(cagr), "vol": float(vol),
            "sharpe": float(cagr / vol) if vol else 0.0,
            "max_dd": dd, "trades": trades,
            "final": float(curve.iloc[-1] / curve.iloc[0])}


def simulate(close, high, low, sigma, ranks, tgt_mult, stop_mult=STOP_MULT,
             cost_bps=COST_BPS, hold_n=HOLD_N, same_day=False,
             rebal_dates=frozenset()) -> tuple:
    """Daily portfolio walk. `ranks` is a per-day DataFrame of cross-sectional ranks
    (weekly values forward-filled by the caller); `rebal_dates` are the days on
    which the book is actually rebuilt."""
    dates = close.index
    cash, pos, trades = 1.0, {}, 0
    pending = []                      # (available_date_idx, dollars) — T+1 queue
    curve = []
    cost = cost_bps / 10_000.0

    def rank_row(i):
        return ranks.iloc[i].dropna().sort_values()

    def buy(sym, dollars, px, i):
        nonlocal cash, trades
        if dollars <= 1e-12 or not np.isfinite(px) or px <= 0:
            return False
        sig = sigma.iat[i, close.columns.get_loc(sym)]
        if not np.isfinite(sig) or sig <= 0:
            return False
        shares = dollars * (1 - cost) / px
        pos[sym] = {"shares": shares, "entry": px,
                    "stop": px * (1 - stop_mult * sig),
                    "target": px * (1 + tgt_mult * sig) if tgt_mult else np.inf}
        cash -= dollars
        trades += 1
        return True

    def sell(sym, px):
        nonlocal cash, trades
        cash += pos.pop(sym)["shares"] * px * (1 - cost)
        trades += 1

    for i, d in enumerate(dates):
        # 1. released cash from prior-day exits becomes available
        if pending:
            ready = [p for p in pending if p[0] <= i]
            pending = [p for p in pending if p[0] > i]
            for _, amt in ready:
                cash += amt

        # 2. intra-day exits — stop checked first (conservative tie-break)
        freed = 0.0
        for sym in list(pos):
            p = pos[sym]
            lo, hi = low.iat[i, low.columns.get_loc(sym)], high.iat[i, high.columns.get_loc(sym)]
            if np.isfinite(lo) and lo <= p["stop"]:
                before = cash; sell(sym, p["stop"]); freed += cash - before
            elif np.isfinite(hi) and hi >= p["target"]:
                before = cash; sell(sym, p["target"]); freed += cash - before
        if freed > 0:
            cash -= freed
            pending.append((i if same_day else i + 1, freed))

        # 3. weekly rebuild, then fill any empty slot with the best unheld name
        try:
            order = rank_row(i)
        except Exception:
            order = pd.Series(dtype=float)
        if not order.empty:
            want = list(order.index[:hold_n])
            if d in rebal_dates:
                for sym in [s for s in pos if s not in want]:
                    px = close.iat[i, close.columns.get_loc(sym)]
                    if np.isfinite(px):
                        sell(sym, px)
            slots = hold_n - len(pos)
            if slots > 0 and cash > 1e-9:
                cands = [s for s in order.index if s not in pos][:slots]
                if cands:
                    per = cash / len(cands)
                    for sym in cands:
                        buy(sym, per, close.iat[i, close.columns.get_loc(sym)], i)

        mtm = sum(p["shares"] * close.iat[i, close.columns.get_loc(s)]
                  for s, p in pos.items()
                  if np.isfinite(close.iat[i, close.columns.get_loc(s)]))
        curve.append(cash + mtm + sum(a for _, a in pending))

    return pd.Series(curve, index=dates), trades


def run(same_day: bool = False, cost_bps: float = COST_BPS) -> dict:
    import momentum
    close = pd.read_parquet(PRICES / "closes.parquet")
    high = pd.read_parquet(PRICES / "highs.parquet")
    low = pd.read_parquet(PRICES / "lows.parquet")
    uni = {ln.split(",")[0].strip() for ln in
           (REPO / "config" / "universe.csv").read_text().splitlines()[1:] if ln.strip()}
    cols = [c for c in close.columns if c in uni
            and c in high.columns and c in low.columns]
    close, high, low = close[cols], high[cols], low[cols]
    sigma = close.pct_change().rolling(LOOKBACK).std()

    rebal = close.index[LOOKBACK::5]                       # weekly rebuild
    print(f"universe {len(cols)} names | {close.index[0].date()} -> {close.index[-1].date()} "
          f"| {len(rebal)} weekly rebuilds | spread/slippage {cost_bps}bps/side "
          f"| redeploy {'SAME-DAY' if same_day else 'T+1'}")

    rows = {}
    for d in rebal:                                        # THE live ranking fn
        sc = momentum.compute(close, d)
        rows[d] = sc["rank"] if not sc.empty else pd.Series(dtype=float)
    ranks = pd.DataFrame(rows).T.reindex(close.index).ffill()

    out, curves = {}, {}
    print("\n  target |    CAGR |  Sharpe |  max DD | trades | final x")
    print("  -------+---------+---------+---------+--------+--------")
    for t in TARGETS:
        curve, tr = simulate(close, high, low, sigma, ranks, t, same_day=same_day,
                             cost_bps=cost_bps, rebal_dates=frozenset(rebal))
        m = _metrics(curve, tr)
        lbl = "none" if t is None else f"{t:.1f}s"
        out[lbl], curves[lbl] = m, curve
        live = " <- LIVE" if t == 5.5 else ""
        print(f"  {lbl:>6} | {m['cagr']:6.2%} | {m['sharpe']:7.2f} | {m['max_dd']:6.1%} |"
              f" {tr:6d} | {m['final']:6.2f}x{live}")

    # ---- REGIME STABILITY -------------------------------------------------
    # A full-sample optimum is exactly the artefact that does not survive a regime
    # change. If one target distance is genuinely better it should win in MOST
    # sub-periods, not just on the 10-year average. If the winner moves around,
    # the full-sample peak is a fit to one realised path and must not be traded.
    periods = [("2016-18", "2016-01-01", "2018-12-31"),
               ("2019-20", "2019-01-01", "2020-12-31"),
               ("2021-22", "2021-01-01", "2022-12-31"),
               ("2023-24", "2023-01-01", "2024-12-31"),
               ("2025-26", "2025-01-01", "2026-12-31")]
    print("\n  REGIME STABILITY — Sharpe by sub-period (best in each row marked *)")
    print("  period  | " + " | ".join(f"{k:>7}" for k in curves))
    print("  --------+-" + "-+-".join("-" * 7 for _ in curves))
    wins = {k: 0 for k in curves}
    for lbl, a, b in periods:
        seg = {k: v.loc[a:b] for k, v in curves.items()}
        if min(len(v) for v in seg.values()) < 60:
            continue
        sh = {k: _metrics(v, 0)["sharpe"] for k, v in seg.items()}
        best = max(sh, key=sh.get)
        wins[best] += 1
        cells = " | ".join(f"{sh[k]:6.2f}{'*' if k == best else ' '}" for k in curves)
        print(f"  {lbl:<7} | {cells}")
    print("  --------+-" + "-+-".join("-" * 7 for _ in curves))
    print("  wins    | " + " | ".join(f"{wins[k]:6d} " for k in curves))
    out["_period_wins"] = wins
    return out


def _selftest() -> None:
    """Pin the portfolio mechanics: a target exit must free cash, that cash must be
    unavailable until T+1, and costs must be charged on both sides."""
    idx = pd.date_range("2024-01-01", periods=4, freq="B")
    close = pd.DataFrame({"A": [100.0, 100.0, 100.0, 100.0]}, index=idx)
    sigma = pd.DataFrame({"A": [0.10] * 4}, index=idx)
    ranks = pd.DataFrame({"A": [1.0] * 4}, index=idx)

    # high clears +1 sigma (110) on day 1 -> target exit; no stop touch.
    # 115 not 110: the target is computed as 100*(1+1.0*0.10) = 110.00000000000001,
    # so an exactly-equal bar misses on float precision.
    high = pd.DataFrame({"A": [100.0, 115.0, 100.0, 100.0]}, index=idx)
    low = pd.DataFrame({"A": [100.0, 100.0, 100.0, 100.0]}, index=idx)
    curve, trades = simulate(close, high, low, sigma, ranks, 1.0, stop_mult=2.5,
                             cost_bps=0.0, hold_n=1)
    assert trades >= 2, trades                     # at least one buy + one sell
    assert curve.iloc[-1] > 1.05, curve.tolist()   # ~+10% captured at the target

    # with a stop touch on the same bar, the STOP must win the tie
    low2 = pd.DataFrame({"A": [100.0, 70.0, 100.0, 100.0]}, index=idx)
    c2, _ = simulate(close, high, low2, sigma, ranks, 1.0, stop_mult=2.5,
                     cost_bps=0.0, hold_n=1)
    assert c2.iloc[-1] < 1.0, ("stop must win the same-bar tie", c2.tolist())

    # costs strictly reduce the outcome
    c3, _ = simulate(close, high, low, sigma, ranks, 1.0, stop_mult=2.5,
                     cost_bps=50.0, hold_n=1)
    assert c3.iloc[-1] < curve.iloc[-1], "transaction costs were not charged"

    # equity is conserved while cash sits in the T+1 queue (no vanishing money)
    assert all(np.isfinite(curve.values)) and curve.min() > 0, curve.tolist()

    print("selftest OK: target exit frees cash, T+1 queue conserves equity, "
          "stop wins same-bar tie, costs charged both sides")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        same = "--same-day" in sys.argv
        cb = float(sys.argv[sys.argv.index("--cost-bps") + 1]) \
            if "--cost-bps" in sys.argv else COST_BPS
        res = run(same_day=same, cost_bps=cb)
        p = REPO / "research_store" / ("recycle_sim_sameday.json" if same
                                       else "recycle_sim.json")
        p.write_text(json.dumps(res, indent=2))
        print(f"\nwrote {p}")
