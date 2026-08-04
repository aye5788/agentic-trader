"""Adaptive tuner for stop_atr_mult — the off-box weekly payload.

Builds per-candidate realized-R evidence by replaying entries under each grid
multiple (walk-forward: most recent fold held out for the overfit gap), folds
in live stop-outs from the ledger, runs the Bayesian estimator, and writes a
PROPOSAL. Never promotes, never trades (spec §3, §8).

⚠️ Survivorship bias, by design, not oversight: entries are replayed over
research_store/prices/{closes,highs,lows}.parquet, which fetch_prices.py
populates from config/universe.csv — TODAY'S ~150-name LIVE universe, not the
816-name survivorship-free PIT pool (config/pit_pool.csv, used by
scripts/backtest_pit.py). The true PIT pool has close + dollar-volume only
(Alpaca IEX) — no intraday high/low — so a stop-touch replay cannot run on it.
This replay is therefore a PRIOR built on clean-but-survivorship-biased data;
the live stop-outs folded in via `ledger` (once populated) are the
survivorship-free correction the posterior leans on over time. See
`evidence`/`survivorship` fields in the written artifact for a machine-visible
disclosure of this on every run.

    python scripts/tune_stop.py [--selftest]

Spec: docs/superpowers/specs/2026-07-23-adaptive-input-layer-design.md §6
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from stop_replay import replay_position          # noqa: E402
import adaptive                                   # noqa: E402

GRID = [1.5, 2.0, 2.5, 3.0, 3.5]
BAND = (1.5, 3.5)
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
            if not math.isnan(r["realized_r"]):
                sums[gi] += r["realized_r"]; counts[gi] += 1
    mean_r = np.divide(sums, counts, out=np.zeros_like(sums), where=counts > 0)
    return mean_r, counts


def generate_entries(panels, pool_tickers, tm, portfolio, fold_days=252):
    """Reconstruct historical entries over the CURRENT ~150-name live universe
    (survivorship-biased — see module docstring): at each weekly rebalance date
    (regime-on dates only — production holds cash otherwise), the book-only
    names momentum.select would have bought, their entry close, momentum's own
    trailing sigma (converted to dollar risk-per-share), and the forward OHLC
    window. Reuses the SAME pure signal + selection the live loop uses
    (src/momentum.py: compute/select/regime_on) so replay entries match
    production exactly, and reproduces production geometry algebraically
    (stop = entry - m*frac_sigma*entry; see build_samples's dollar-sigma stop).

    `pool_tickers` should already be restricted to the single-name candidate
    pool (ETF sleeve + SPY excluded) that exist in the price panel. NOTE: this
    is NOT the same universe as scripts/backtest_pit.py, which ranks the
    816-name survivorship-free pool (pool_closes/pool_dvol, no highs/lows);
    this replay is confined to whatever of pit_pool.csv survives into today's
    closes/highs/lows panels — i.e. effectively today's live ~150 names.

    Caveat — stateless selection: each rebalance calls momentum.select with an
    empty `held` set, so this does not model production's stateful band
    hysteresis. A name that stays durably top-ranked across several weekly
    dates yields multiple overlapping (correlated) entries here rather than
    one independent holding period — a known evidence-correlation
    simplification, not a production behavior.

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
    # weekly cadence: step 5 trading days; leave a full horizon+5 forward
    # window for replay so every generated entry has enough forward data.
    for di in range(fold_days, len(dates) - (horizon + 5), 5):
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
    panel, minus the ETF sleeve and the SPY regime proxy. NOTE on fidelity:
    `closes` here is research_store/prices/closes.parquet, built from today's
    live config/universe.csv (~150 names), NOT the 816-name PIT pool that
    scripts/backtest_pit.py ranks — so despite starting from pit_pool.csv,
    the `t in closes.columns` filter collapses this to (a subset of) today's
    live universe. Ticker-exclusion logic (ETF sleeve + SPY) is unaffected
    and correct; only the "mirrors the PIT backtest's universe" framing was
    wrong and has been removed."""
    import pandas as pd
    pool = pd.read_csv(REPO / "config" / "pit_pool.csv")["ticker"].tolist()
    etfs = set(pd.read_csv(REPO / "config" / "etf_universe.csv")["ticker"])
    exclude = etfs | {"SPY"}
    return [t for t in pool if t not in exclude and t in closes.columns]


def live_stop_samples(theses, outcomes, grid):
    """Fold live stop-outs into the grid bucket matching the stop multiple that
    was IN FORCE for that decision. Pure — callers supply the data.

    theses:   {decision_id: {"entry_zone": [lo, hi], "stop": float, "sigma": float}}
    outcomes: [{"decision_id", "hit_stop", "pnl_pct", "entry_price"}, ...]
    Returns (sum_r[len(grid)], count[len(grid)]).

    Only `hit_stop` outcomes contribute: a name that exited on a rotation or a
    regime flip says nothing about where the stop should sit. The multiple is
    recovered per-decision as (entry - stop) / (entry * sigma) rather than read
    from today's config, so samples stay correctly bucketed across a promotion
    that changes the dial. realized_R = pnl_pct / risk_pct, the same
    mean-realized-R objective the replay half optimises (spec §4).

    A live sample lands ONLY in its own bucket — unlike replay, it carries no
    counterfactual about what a different multiple would have returned.
    """
    g = np.asarray(grid, dtype=float)
    sums, counts = np.zeros(len(grid)), np.zeros(len(grid))
    for o in outcomes:
        if not o.get("hit_stop"):
            continue
        th = theses.get(o.get("decision_id"))
        if not th:
            continue                      # outcome with no archived thesis — skip, don't guess
        stop, sigma = th.get("stop"), th.get("sigma")
        zone = th.get("entry_zone") or []
        pnl, entry_price = o.get("pnl_pct"), o.get("entry_price")
        if not (stop and sigma and zone) or pnl is None or not entry_price:
            continue
        entry_mid = sum(zone) / len(zone)
        if entry_mid <= 0 or sigma <= 0:
            continue
        risk_pct = (float(entry_price) - float(stop)) / float(entry_price)
        if risk_pct <= 0:
            continue                      # stop at/above entry — degenerate, no R to compute
        mult = (entry_mid - float(stop)) / (entry_mid * float(sigma))
        idx = int(np.argmin(np.abs(g - mult)))
        sums[idx] += float(pnl) / risk_pct
        counts[idx] += 1
    return sums, counts


def count_stopouts(outcomes):
    """How many stop-outs the ledger CONTAINS — independent of how many the join
    managed to bucket. The artifact reports both; `seen > joined` is the signature
    of a missing/mismatched archive quietly starving the live half."""
    return sum(1 for o in outcomes if o.get("hit_stop"))


def _load_live_samples(grid):
    """Thin I/O around live_stop_samples: archived theses + journal outcomes.

    Returns zeros when the ledger/archive is absent (a fresh clone, or a CI run
    whose mirror copy skipped them) — the posterior then leans purely on replay,
    which is the correct degradation, not an error.
    """
    import ledger
    journal_path = REPO / "research_store" / "journal.jsonl"
    archive_dir = REPO / "research_store" / "archive"
    if not journal_path.exists():
        return np.zeros(len(grid)), np.zeros(len(grid)), 0

    journal = []
    for line in journal_path.read_text().splitlines():
        if line.strip():
            try:
                journal.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    theses = {}
    for f in sorted(archive_dir.glob("*.json")) if archive_dir.exists() else []:
        try:
            book = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for t in book.get("theses") or []:
            did = t.get("decision_id")
            if did and did not in theses:      # first archive wins = the entry-time geometry
                theses[did] = {"entry_zone": t.get("entry_zone"),
                               "stop": t.get("stop"),
                               "sigma": (t.get("signals") or {}).get("sigma")}

    outcomes = [e.get("outcome") or {} for e in journal if e.get("event") == "outcome"]
    # realized_history is the ledger's own view of the same events — used here to
    # assert the two agree on how many closes exist, so a schema drift is loud.
    assert len(outcomes) == len(ledger.realized_history(journal)), "outcome/ledger view drift"
    sums, counts = live_stop_samples(theses, outcomes, grid)
    return sums, counts, count_stopouts(outcomes)


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

    live_sum, live_cnt, stopouts_seen = _load_live_samples(GRID)
    tot_cnt = tr_cnt + live_cnt
    tot_mean = np.divide(tr_mean * tr_cnt + live_sum, tot_cnt,
                         out=np.zeros(len(GRID)), where=tot_cnt > 0)

    pm, pv, pc = adaptive.posterior(
        np.array(GRID), tot_mean, tot_cnt, noise_var=1.0,
        prior_mean=float(np.average(tot_mean, weights=tot_cnt)) if tot_cnt.sum() else 0.0,
        length_scale=0.6, prior_var=1.0)
    incumbent = float(tm["stop_atr_mult"])
    inc_idx = int(np.argmin(np.abs(np.array(GRID) - incumbent)))
    rec = adaptive.recommend(np.array(GRID), pm, pc, inc_idx, confidence=0.9)
    gap = adaptive.oos_gap(tr_mean, ho_mean, rec["recommended_idx"])
    best_idx = int(np.argmax(pm))   # the posterior's favourite (may be the incumbent itself)

    if rec["moved"]:
        rationale = f"moved to {rec['recommended_value']} (p_better={rec['p_better']:.2f} >= 0.90)"
    elif best_idx == inc_idx:
        rationale = f"incumbent {incumbent} is the posterior peak — retained"
    else:
        rationale = (f"incumbent {incumbent} retained — challenger {GRID[best_idx]} leads but "
                     f"only p_better={rec['p_better']:.2f} < 0.90")

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    art = {
        "knob": "trade_management.stop_atr_mult", "generated_at": now,
        "incumbent": incumbent, "recommended": rec["recommended_value"],
        "moved": rec["moved"], "p_better": round(rec["p_better"], 4), "band": list(BAND),
        "grid": GRID, "posterior_mean": [round(float(x), 4) for x in pm],
        "evidence": {
            "replay_n": int(tr_cnt.sum()), "live_n": int(live_cnt.sum()),
            # stop-outs the ledger holds vs. how many the join actually used.
            # Equal = healthy. seen > live_n = the archive is missing/mismatched
            # and the live half is being starved silently — investigate, don't promote.
            "live_stopouts_seen": int(stopouts_seen),
            "live_join_healthy": int(stopouts_seen) == int(live_cnt.sum()),
            "candidate_universe": "live-150 (config/universe.csv, survivorship-biased)",
            "effective_candidates": len({e["sym"] for e in entries}),
            "survivorship": "biased",
        },
        "oos_gap": round(gap, 4),
        "rationale": rationale,
        "provenance": (f"adaptive layer {now[:10]} from replay_n={int(tr_cnt.sum())} "
                       f"live_n={int(live_cnt.sum())}; incumbent was {incumbent}"),
    }
    if not (BAND[0] <= art["recommended"] <= BAND[1]):
        raise ValueError(f"recommended {art['recommended']} outside band {BAND}: {art}")
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

    _selftest_live()


def _selftest_live() -> None:
    # Hand-computed vectors. mult in force = (entry-stop)/(entry*sigma); the
    # sample lands in the nearest grid bucket. realized_R = pnl_pct / risk_pct,
    # risk_pct = (entry_price - stop)/entry_price.
    #   A: entry 100 stop 90 sigma .04 -> mult 2.5 (idx 2); pnl -.10 / risk .10 = -1.0
    #   B: entry  50 stop 46 sigma .04 -> mult 2.0 (idx 1); pnl -.08 / risk .08 = -1.0
    #   C: entry 100 stop 90 sigma .04 -> mult 2.5 (idx 2); pnl -.05 / risk .10 = -0.5
    #   D: hit_stop False -> contributes nothing
    #   E: no matching thesis -> skipped, must not raise
    theses = {
        "A:2026-01-01": {"entry_zone": [100.0, 100.0], "stop": 90.0, "sigma": 0.04},
        "B:2026-01-01": {"entry_zone": [50.0, 50.0], "stop": 46.0, "sigma": 0.04},
        "C:2026-01-01": {"entry_zone": [100.0, 100.0], "stop": 90.0, "sigma": 0.04},
        "D:2026-01-01": {"entry_zone": [100.0, 100.0], "stop": 90.0, "sigma": 0.04},
    }
    outcomes = [
        {"decision_id": "A:2026-01-01", "hit_stop": True, "pnl_pct": -0.10, "entry_price": 100.0},
        {"decision_id": "B:2026-01-01", "hit_stop": True, "pnl_pct": -0.08, "entry_price": 50.0},
        {"decision_id": "C:2026-01-01", "hit_stop": True, "pnl_pct": -0.05, "entry_price": 100.0},
        {"decision_id": "D:2026-01-01", "hit_stop": False, "pnl_pct": 0.04, "entry_price": 100.0},
        {"decision_id": "E:2026-01-01", "hit_stop": True, "pnl_pct": -0.10, "entry_price": 100.0},
    ]
    sums, counts = live_stop_samples(theses, outcomes, GRID)
    assert list(counts) == [0, 1, 2, 0, 0], counts
    assert np.allclose(sums, [0.0, -1.0, -1.5, 0.0, 0.0]), sums

    # An empty ledger must still return correctly-shaped zeros (the current state).
    z_sums, z_counts = live_stop_samples({}, [], GRID)
    assert list(z_counts) == [0] * len(GRID), z_counts
    assert np.allclose(z_sums, np.zeros(len(GRID))), z_sums

    # count_stopouts is the drift detector: it counts stop-outs PRESENT in the
    # ledger, independent of how many the join actually bucketed. seen > joined
    # means the archive is missing/mismatched and the live half is silently
    # contributing nothing — the exact failure this dial already shipped once.
    assert count_stopouts(outcomes) == 4, count_stopouts(outcomes)   # A,B,C,E (not D)
    assert count_stopouts([]) == 0
    print("selftest OK: live_stop_samples bucketing + realized-R + empty-ledger + drift count")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
