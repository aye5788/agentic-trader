"""Residual-momentum comparison — sweep residual_tilt over the survivorship-free
PIT pool and print CAGR / Sharpe / MaxDD / 2022-unwind DD per tilt. One-time
research validation (not a live path, not a cron). Reuses backtest_pit's engine.

    python scripts/backtest_residual.py

Spec: docs/superpowers/specs/2026-07-23-residual-momentum-design.md §3.3
"""
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import backtest as bt              # noqa: E402
import backtest_pit as pit         # noqa: E402

GRID = [0.0, 0.25, 0.5, 0.75, 1.0]


def _dd_2022(equity: pd.Series) -> float:
    """Max drawdown restricted to calendar 2022 (the momentum unwind)."""
    sl = equity[(equity.index >= "2022-01-01") & (equity.index <= "2022-12-31")]
    return bt.max_drawdown(sl) if len(sl) > 1 else float("nan")


def main() -> None:
    data = pit._load_pit_data()   # (closes, dvol, candidates, etfs, spy, etf_panel, rebals, P)
    print(f"\n{'residual_tilt':>13}{'CAGR':>9}{'Sharpe':>8}{'MaxDD':>9}{'2022DD':>9}{'vs w=0':>10}")
    print("-" * 58)
    base_cagr = None
    for w in GRID:
        r = pit.run_backtest(*data, residual_tilt=w)
        eq = r["res"]["equity"]
        dd22 = _dd_2022(eq)
        if base_cagr is None:
            base_cagr = r["cagr"]
        delta = "" if w == 0.0 else f"{(r['cagr'] - base_cagr):+.1%} CAGR"
        print(f"{w:>13.2f}{r['cagr']:>8.1%}{r['sharpe']:>8.2f}{r['maxdd']:>9.1%}{dd22:>9.1%}{delta:>10}")
    print("\nRead all three lenses (return / Sharpe / drawdown). Adjudicate — no auto-adopt.")


if __name__ == "__main__":
    main()
