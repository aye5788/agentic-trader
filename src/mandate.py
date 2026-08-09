"""THE MANDATE — continuous, falsifiable evaluation of the terms the agent works
under (spec: docs/superpowers/specs/2026-08-09-agent-authority-inversion-design.md §5).

Pure functions over data that already exists on disk. No network, no broker, no
clock of its own — callers pass `asof`. Three-state by design: a criterion that
cannot be computed reports INSUFFICIENT_DATA and MUST NOT read as a pass.

Run the tests:  .venv/bin/python src/mandate.py --selftest
"""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

PASS = "PASS"
FAIL = "FAIL"
INSUFFICIENT = "INSUFFICIENT_DATA"


def load(path: str = "config/mandate.toml") -> dict:
    """Load the mandate terms. Raises if absent — an unstated mandate is not a
    permissive one, and must never silently default."""
    p = REPO / path
    if not p.exists():
        raise FileNotFoundError(f"mandate not found at {p}; the terms must be explicit")
    with p.open("rb") as fh:
        return tomllib.load(fh)


def _selftest() -> None:
    m = load()
    assert m["drawdown"]["max_pct"] == 0.15, m["drawdown"]
    assert m["concentration"]["max_position_pct"] == 0.15, m["concentration"]
    assert m["pnl_concentration"]["window_days"] == 90
    assert m["pnl_concentration"]["max_single_share"] == 0.40
    assert m["pnl_concentration"]["min_distinct_names"] == 4
    assert m["relative_return"]["window_days"] == 60
    assert m["relative_return"]["benchmark"] == "SPY"
    assert PASS != FAIL != INSUFFICIENT
    print("selftest OK: mandate loads, terms match")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
