"""The momentum screen, exposed as a CANDIDATE GENERATOR.

Spec §3: a screen is not a decision. It ranks; the agent chooses — including
choosing nothing, or something outside the top N, with a stated reason. Nothing
here restricts what may be traded.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

import momentum   # noqa: E402


def read_universe(path: Path) -> list:
    """First column of a header-carrying CSV, blank lines skipped."""
    return [ln.split(",")[0].strip()
            for ln in path.read_text().splitlines()[1:] if ln.strip()]


def rank(panel, asof, tickers: list):
    """momentum.compute restricted to `tickers`, sorted best-first.

    Returns the full scored frame — the caller decides how many to show.
    """
    cols = [c for c in tickers if c in panel.columns]
    scored = momentum.compute(panel[cols], asof)
    if scored.empty:
        return scored
    return scored.sort_values("score", ascending=False)


def _selftest() -> None:
    import tempfile, numpy as np, pandas as pd
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "u.csv"
        p.write_text("ticker,sector\nAAA,tech\nBBB,fin\n\n")
        assert read_universe(p) == ["AAA", "BBB"], read_universe(p)

    n = 300
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    t = np.arange(n)
    panel = pd.DataFrame({
        "AAA": 100 * np.cumprod(1 + (0.002 + 0.005 * np.sin(2 * np.pi * t / 11))),
        "BBB": 100 * np.cumprod(1 + (0.0005 + 0.005 * np.sin(2 * np.pi * t / 13))),
        "CCC": 100 * np.cumprod(1 + (-0.001 + 0.005 * np.sin(2 * np.pi * t / 7))),
    }, index=idx)
    r = rank(panel, idx[-1], ["AAA", "BBB", "CCC"])
    assert list(r.columns) >= ["R", "sigma", "score"], list(r.columns)
    assert r.index[0] == "AAA", r.index.tolist()          # strongest first
    # restricting the universe must not change the remaining names' own numbers
    r2 = rank(panel, idx[-1], ["AAA", "BBB"])
    assert "CCC" not in r2.index, r2.index.tolist()
    assert abs(r2.loc["AAA", "R"] - r.loc["AAA", "R"]) < 1e-12
    # a ticker absent from the panel is simply not ranked, never an error
    r3 = rank(panel, idx[-1], ["AAA", "NOPE"])
    assert "NOPE" not in r3.index and "AAA" in r3.index
    print("selftest OK: screen ranks, restricts cleanly, tolerates unknown tickers")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
