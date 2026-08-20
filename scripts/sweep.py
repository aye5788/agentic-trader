"""Parameter sensitivity sweep for the dual-momentum strategy.

Robustness check, NOT optimization. We vary ONE knob at a time around the
production config and ask: does the edge survive, or is it knife-edge / an
artifact of one lucky setting? A real momentum edge should hold its *shape*
(Sharpe in a tight band) across nearby configs; a fragile fit will swing wildly.

Reuses the cached price panel — no network calls. Run after scripts/backtest.py.

    python scripts/sweep.py

Same caveats as the backtest apply to EVERY row: survivorship-biased (today's
150 names run over history) and no intra-week stops. Read the SPREAD across
rows, not any single absolute number.
"""
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))
import backtest as bt  # noqa: E402  (simulate + load_panel live here)

# Production config = the baseline every axis is perturbed around.
# ⛔ SINGLE-ENGINE since 2026-08-20 (the ETF sleeve was deleted). The
# BOOK/SLEEVE SPLIT axis went with it — there is no split to sweep.
BASE = dict(lookback=252, book_hold=14, book_band=20, book_w=1.0, use_regime=True)


def sweep_axis(label, panel, names, spy, variants):
    """variants: list of (name, override_dict). Prints one block."""
    rows = []
    for vname, override in variants:
        params = {**BASE, **override}
        m, _ = bt.simulate(panel, names, spy, **params)
        rows.append((vname, m))
    print(f"\n{label}")
    print(f"  {'variant':<16}{'CAGR':>8}{'vol':>8}{'Sharpe':>8}"
          f"{'maxDD':>9}{'turn/wk':>9}")
    for vname, m in rows:
        star = "  *" if _is_base(vname) else ""
        print(f"  {vname:<16}{m['cagr']:>7.1%}{m['vol']:>7.1%}{m['sharpe']:>8.2f}"
              f"{m['maxdd']:>8.1%}{m['turnover']:>9.1f}{star}")
    return rows


# Baseline markers must match BASE above; "70/30" and "top-10" were left here
# after the sleeve was deleted and the book moved to 14 names (2026-08-20), so
# the "*" marked a row that was no longer the baseline.
_BASE_MARK = {"252d", "top-14", "regime ON", "band 20"}


def _is_base(vname):
    return vname in _BASE_MARK


def main():
    panel = bt.load_panel().sort_index()
    names = [t for t in pd.read_csv(REPO / "config" / "universe.csv")["ticker"] if t in panel]
    spy = panel["SPY"]

    print("=" * 62)
    print("SENSITIVITY SWEEP — one knob at a time around production config")
    print("  (* = baseline value.  SPY B&H over this span: CAGR 13.1% / Sharpe 0.78)")
    print("=" * 62)

    sweep_axis("LOOKBACK (formation window)", panel, names, spy, [
        ("126d", {"lookback": 126}),
        ("189d", {"lookback": 189}),
        ("252d", {"lookback": 252}),
        ("315d", {"lookback": 315}),
    ])

    sweep_axis("BOOK HOLD COUNT (concentration)", panel, names, spy, [
        ("top-8", {"book_hold": 8, "book_band": 12}),
        ("top-14", {"book_hold": 14, "book_band": 20}),
        ("top-20", {"book_hold": 20, "book_band": 28}),
    ])

    sweep_axis("BAND WIDTH (vs hard top-N)", panel, names, spy, [
        ("band 14", {"book_band": 14}),   # == hold_n: no hysteresis
        ("band 20", {"book_band": 20}),
        ("band 26", {"book_band": 26}),
    ])

    sweep_axis("REGIME FILTER (SPY>50DMA)", panel, names, spy, [
        ("regime ON", {"use_regime": True}),
        ("regime OFF", {"use_regime": False}),
    ])

    print("\n" + "=" * 62)
    print("READ THE SPREAD, NOT THE LEVEL. Every row is survivorship-biased.")
    print("Tight Sharpe band across an axis = robust; wild swings = fragile fit.")
    print("=" * 62)


if __name__ == "__main__":
    main()
