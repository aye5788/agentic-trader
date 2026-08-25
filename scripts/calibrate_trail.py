"""Does a trailing stop help this book, and at what width? Offline sweep.

⛔ WHY THIS EXISTS RATHER THAN A LIVE SHADOW LOG. The trail shipped disabled
(2026-08-25) with a rollout that called for ~10 sessions of shadow observation
before arming. Ten sessions is TEN DAYS OF TAPE. The question "how much giveback
should a momentum name be allowed" is a distributional one, and the panel on disk
holds 2,532 trading days across 168 names. Waiting collects a worse sample slowly
while the position it protects stays unprotected -- and the cost of not having it
is already measured: INTC round-tripped +24.9% to nothing, STX gave back 92.5%.

Shadow mode still has a job -- proving the monitor's code path executes against a
live feed -- but that is a MECHANISM check, not a calibration, and it does not
need ten sessions to answer.

WHAT THIS MEASURES. Walk forward weekly. At each rebalance rank the universe on
the same src/momentum.py the live loop uses, hold the top `book_hold` with the
same retention band, and simulate every position day by day on the REAL high/low
path -- so a stop, a target and a trail can each be hit intraday, which a
close-only backtest cannot see. Then compare, on identical entries:

    baseline : static stop + the two configured targets
    +trail   : the same, plus the peak-anchored ratchet, for a grid of
               (activation_sigma, giveback_fraction)

and report what actually matters: expectancy per trade, how much giveback the
trail avoided, and -- the number that decides it -- how often the trail exited a
name that then kept going without it (`premature`, measured as the forward return
from the trail exit to the horizon).

⛔ HONEST LIMITS, ALL OF WHICH SURVIVE THIS SCRIPT.
  1. DAILY BARS CANNOT ORDER INTRADAY EVENTS. If a bar's low breaches the stop
     and its high touches the target, which came first is unknowable here. This
     resolves that conflict PESSIMISTICALLY -- the protective level is taken
     first -- so the trail is never flattered by an ordering it did not earn.
  2. SURVIVORSHIP. The 168-name panel is today's universe carried backwards; a
     name that died is absent. That inflates every arm equally, so the RANKING of
     configurations is far more trustworthy than any absolute level here.
     scripts/backtest_pit.py exists precisely because absolute levels from a
     survivor panel are not to be believed.
  3. It models no fees, no slippage, and fills every exit at the level itself.
     A gap through a stop fills worse in life than it does here.

So read the SPREAD across configurations, not the level of any one of them.
That is the same instruction scripts/sweep.py carries, for the same reason.

    .venv/bin/python scripts/calibrate_trail.py
    .venv/bin/python scripts/calibrate_trail.py --start 2019-01-01 --horizon 10
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import momentum                                    # noqa: E402
import strategy                                    # noqa: E402
import trailing                                    # noqa: E402

PRICES = REPO / "research_store" / "prices"


def _panels():
    """closes/highs/lows aligned on one index. Raises loudly if absent."""
    out = {}
    for name in ("closes", "highs", "lows"):
        p = PRICES / f"{name}.parquet"
        if not p.exists():
            raise SystemExit(f"missing {p} — this calibration needs the OHLC panel")
        out[name] = pd.read_parquet(p)
    idx = out["closes"].index
    cols = out["closes"].columns
    for k in ("highs", "lows"):
        out[k] = out[k].reindex(index=idx, columns=cols)
    return out["closes"], out["highs"], out["lows"]


def simulate_one(entry_px, sigma, path_hi, path_lo, path_cl,
                 stop_mult, target_mults, trail_cfg):
    """One position's life on the real bar path -> dict. Pure.

    `trail_cfg` None = baseline (static stop + targets only). Otherwise
    {'activation_sigma', 'giveback_fraction'} and the ratchet is layered on via
    src/trailing.py -- the SAME arithmetic the monitor runs, never a
    reimplementation of it, so a calibration cannot drift from the live rule.

    Exit precedence within a bar is PESSIMISTIC (see module docstring): the
    protective level is tested before the target.
    """
    stop = entry_px * (1.0 - stop_mult * sigma)
    targets = [entry_px * (1.0 + m * stop_mult * sigma) for m in target_mults]
    state = {"entry_price": float(entry_px), "peak_price": float(entry_px)}
    peak_seen = float(entry_px)
    for i in range(len(path_cl)):
        hi, lo, cl = path_hi[i], path_lo[i], path_cl[i]
        if not np.isfinite(cl):
            continue
        if np.isfinite(hi):
            peak_seen = max(peak_seen, float(hi))
            state = trailing.update_peak(state, float(hi))
        trail_lvl = None
        if trail_cfg is not None:
            trail_lvl, _ = trailing.compute_trail_stop(
                state, sigma, trail_cfg["activation_sigma"],
                trail_cfg["giveback_fraction"])
            if trail_lvl is not None:
                state["trail_stop"] = trail_lvl
        eff, src = trailing.effective_stop(stop, None, trail_lvl)
        # PROTECTIVE FIRST. Same-bar ambiguity resolved against the trail.
        if np.isfinite(lo) and eff is not None and lo <= eff:
            # effective_stop() names its sources thesis/agent/trail; the exit mix
            # below counts "stop", so map it rather than silently reporting 0.0%.
            label = "trail" if src == "trail" else "stop"
            return {"exit": label, "ret": eff / entry_px - 1.0, "days": i + 1,
                    "peak_ret": peak_seen / entry_px - 1.0, "fwd": _fwd(path_cl, i)}
        if np.isfinite(hi) and hi >= targets[-1]:
            return {"exit": "target2", "ret": targets[-1] / entry_px - 1.0, "days": i + 1,
                    "peak_ret": peak_seen / entry_px - 1.0, "fwd": _fwd(path_cl, i)}
    last = path_cl[np.isfinite(path_cl)]
    end = float(last[-1]) if len(last) else float(entry_px)
    return {"exit": "rotate", "ret": end / entry_px - 1.0, "days": len(path_cl),
            "peak_ret": peak_seen / entry_px - 1.0, "fwd": 0.0}


def _fwd(path_cl, i):
    """Return from the exit bar's close to the end of the window. Pure.

    This is the ONLY honest way to price a premature exit: what the name did
    NEXT, for the rest of the horizon we would otherwise have held it.
    """
    tail = path_cl[i + 1:]
    tail = tail[np.isfinite(tail)]
    if len(tail) == 0 or not np.isfinite(path_cl[i]) or path_cl[i] == 0:
        return 0.0
    return float(tail[-1]) / float(path_cl[i]) - 1.0


def run(start, horizon, book_hold, band_n, stop_mult, target_mults, grid):
    closes, highs, lows = _panels()
    # ⛔ SLICE ALL THREE TOGETHER. Filtering only `closes` left `highs`/`lows`
    # on the full index, so every simulated path was read 109 rows earlier than
    # its own entry — a stop "breached" by a low from five months before the
    # trade. It did not raise; it produced a plausible-looking table with a
    # -7.5% expectancy, a 1.9% win rate and a stop that never fired. The exit
    # mix summing to 3% instead of 100% is what exposed it.
    keep = closes.index >= pd.Timestamp(start)
    closes, highs, lows = closes.loc[keep], highs.loc[keep], lows.loc[keep]
    dates = closes.index
    rebal = list(range(0, len(dates) - horizon, 5))       # weekly
    arms = {"baseline": None}
    for a, g in grid:
        arms[f"trail a{a}_g{g}"] = {"activation_sigma": a, "giveback_fraction": g}

    rows = {k: [] for k in arms}
    held: set[str] = set()
    for r in rebal:
        asof = dates[r]
        try:
            scored = momentum.compute(closes.loc[:asof], asof)
        except Exception:
            continue
        if scored is None or scored.empty:
            continue
        picks = momentum.select(scored, held, book_hold, band_n)
        held = set(picks)
        for sym in picks:
            if sym not in closes.columns:
                continue
            sig = scored["sigma"].get(sym)
            entry = closes[sym].iloc[r] if r < len(closes) else None
            if not (np.isfinite(sig or np.nan) and sig and np.isfinite(entry or np.nan)):
                continue
            sl = slice(r + 1, r + 1 + horizon)
            ph, pl, pc = (highs[sym].values[sl], lows[sym].values[sl],
                          closes[sym].values[sl])
            if len(pc) < 2:
                continue
            for name, cfg in arms.items():
                rows[name].append(simulate_one(float(entry), float(sig),
                                               ph, pl, pc, stop_mult,
                                               target_mults, cfg))
    return rows


def summarise(rows, sigma_scale=True):
    out = []
    for name, rs in rows.items():
        if not rs:
            continue
        d = pd.DataFrame(rs)
        trail_exits = d[d.exit == "trail"]
        out.append({
            "config": name,
            "n": len(d),
            "expectancy_%": round(d["ret"].mean() * 100, 4),
            "median_%": round(d["ret"].median() * 100, 4),
            "win_%": round((d["ret"] > 0).mean() * 100, 2),
            "giveback_pp": round(((d["peak_ret"] - d["ret"]) * 100).mean(), 3),
            "trail_exits_%": round(len(trail_exits) / len(d) * 100, 2),
            # THE NUMBER THAT DECIDES IT: what a trail-exited name did next.
            # Positive = the trail sold winners that kept running.
            "fwd_after_trail_%": (round(trail_exits["fwd"].mean() * 100, 3)
                                  if len(trail_exits) else None),
            "stop_%": round((d.exit == "stop").mean() * 100, 2),
            "t2_%": round((d.exit == "target2").mean() * 100, 2),
            "rotate_%": round((d.exit == "rotate").mean() * 100, 2),
        })
    return pd.DataFrame(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--horizon", type=int, default=10, help="trading days held")
    args = ap.parse_args()

    tm = strategy.load()
    risk = tm.get("trade_management") or tm.get("trade") or {}
    stop_mult = float(risk.get("stop_atr_mult", 2.5))
    target_mults = list(risk.get("target_r_mults", [2.2, 4.0]))
    book = tm.get("portfolio") or {}
    book_hold = int(book.get("book_hold", 14))
    band_n = int(book.get("band_n", 20))

    grid = [(a, g) for a in (2.0, 2.5, 3.0)
            for g in (0.20, 0.35, 0.50, 0.65)]
    print(f"panel-based trail calibration | start={args.start} horizon={args.horizon}d "
          f"| stop={stop_mult}sigma targets={target_mults} book={book_hold}/{band_n}")
    rows = run(args.start, args.horizon, book_hold, band_n,
               stop_mult, target_mults, grid)
    df = summarise(rows)
    if df.empty:
        raise SystemExit("no trades simulated — check the panel and --start")
    base = df[df.config == "baseline"].iloc[0]
    df = df.sort_values("expectancy_%", ascending=False)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 40)
    print(df.to_string(index=False))
    print(f"\nbaseline expectancy {base['expectancy_%']}%  giveback {base['giveback_pp']}pp")
    print("⚠️ survivorship-biased panel and daily-bar ordering — READ THE SPREAD "
          "BETWEEN CONFIGS, not the level of any one. See module docstring.")


if __name__ == "__main__":
    main()
