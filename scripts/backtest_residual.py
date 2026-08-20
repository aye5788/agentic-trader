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


SECTORS = ["XLE", "XLF", "XLK", "XLV", "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE", "XLC"]


def _row(r, base_cagr):
    eq = r["res"]["equity"]
    delta = "" if base_cagr is None else f"{(r['cagr'] - base_cagr):+.1f}"
    return f"{r['cagr']*100:>6.1f}{r['sharpe']:>7.2f}{r['maxdd']*100:>7.1f}{_dd_2022(eq)*100:>7.1f}"


def main() -> None:
    data = pit._load_pit_data()   # (closes, dvol, candidates, spy, rebals, P)
    closes = data[0]
    sect = [s for s in SECTORS if s in closes.columns]
    factor_panel = closes[sect]
    print(f"  using {len(sect)}/11 sector ETFs: {sect}")
    hdr = f"{'tilt':>5} | {'MARKET-ONLY  CAGR  Shrp  MaxDD 2022':>34} | {'SECTOR      CAGR  Shrp  MaxDD 2022':>34}"
    print("\n" + hdr); print("-" * len(hdr))
    base_m = None
    for w in GRID:
        rm_ = pit.run_backtest(*data, residual_tilt=w)                    # market-only
        rs_ = pit.run_backtest(*data, residual_tilt=w, factors=factor_panel)  # sector
        if base_m is None:
            base_m = rm_["cagr"]
        print(f"{w:>5.2f} | {'':>6}{_row(rm_, None)} | {'':>6}{_row(rs_, None)}")
    print("\nRead all three lenses per variant (return / Sharpe / drawdown). Adjudicate — no auto-adopt.")


if __name__ == "__main__":
    main()
