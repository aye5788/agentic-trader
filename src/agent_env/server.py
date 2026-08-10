"""FastMCP server exposing the agent's tool surface.

Run manually:   .venv/bin/python src/agent_env/server.py
Run the tests:  .venv/bin/python src/agent_env/server.py --selftest

Transport is stdio: Claude Code launches this as a subprocess and speaks MCP over
its stdin/stdout, so NOTHING may be printed to stdout except protocol traffic.
Diagnostics go to stderr.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from mcp.server.fastmcp import FastMCP   # noqa: E402

import marks                                    # noqa: E402
from research_store import read_current         # noqa: E402
from agent_env import state                           # noqa: E402  sibling module
from agent_env import screen                          # noqa: E402  sibling module
import mandate                                  # noqa: E402
import pandas as pd                             # noqa: E402

mcp = FastMCP("agentic-trader")

EQUITY = REPO / "research_store" / "history" / "equity.jsonl"
JOURNAL = REPO / "research_store" / "journal.jsonl"
CLOSES = REPO / "research_store" / "prices" / "closes.parquet"
UNIVERSE = REPO / "config" / "universe.csv"
ETF_UNIVERSE = REPO / "config" / "etf_universe.csv"


def _outcomes() -> list:
    if not JOURNAL.exists():
        return []
    rows = []
    for line in JOURNAL.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


@mcp.tool()
def ping() -> str:
    """Liveness check. Returns 'pong' if the environment server is reachable."""
    return "pong"


@mcp.tool()
def positions() -> str:
    """Every position actually held, with the stop and target set for it.

    `watched: false` means the position has NO stop being enforced — it is
    unprotected. That is deliberately reported rather than hidden.
    """
    prod = read_current()
    return json.dumps(state.holdings(marks.load(),
                                     prod.theses if prod else []), indent=2, default=str)


@mcp.tool()
def account() -> str:
    """Account value, cash, invested capital, and when it was last marked.

    Degrades to an object of nulls (never raises) when no snapshot exists yet
    — a fresh deploy, or a deleted/corrupt snapshot, is a foreseeable state,
    not an error; a tool that raises tells the agent nothing about what it holds.
    """
    v = marks.load() or {}
    return json.dumps({k: v.get(k) for k in
                       ("account_number", "account_value", "cash", "invested",
                        "as_of", "marked_at")}, indent=2, default=str)


@mcp.tool()
def mandate_status() -> str:
    """All four mandate criteria with real numbers and the room left on each.

    Blocking criteria (drawdown, concentration) gate trading. Informational ones
    (P&L concentration, relative return) judge whether the approach is working and
    never gate an order. A criterion reporting INSUFFICIENT_DATA is NOT a pass.
    """
    v = marks.load()
    eq_dated = state.equity_series_with_dates(EQUITY)
    # Pair equity and SPY by CALENDAR DATE, never by position. equity.jsonl
    # (scripts/log_equity.py) and closes.parquet (scripts/fetch_prices.py) are
    # written on different schedules by different jobs and do NOT necessarily
    # cover the same trading days — a length match between them is a
    # coincidence, not evidence they line up. An equity date with no matching
    # SPY close is dropped entirely (both sides), never forward-filled.
    bench_by_date = {}
    if CLOSES.exists():
        try:
            spy = pd.read_parquet(CLOSES)["SPY"].dropna()
            bench_by_date = {ts.date().isoformat(): float(val) for ts, val in spy.items()}
        except Exception:
            bench_by_date = {}
    eq, bench = state.pair_with_benchmark(eq_dated, bench_by_date)
    s = mandate.status(eq, bench, v["positions"], v["account_value"],
                       _outcomes(), v["as_of"])
    return json.dumps(s, indent=2, default=str)


def _panel():
    return pd.read_parquet(CLOSES)


def _all_tickers() -> list:
    return screen.read_universe(UNIVERSE) + screen.read_universe(ETF_UNIVERSE)


@mcp.tool()
def candidates(n: int = 10) -> str:
    """The momentum screen's top `n` names, strongest first, with their numbers.

    This is an ATTENTION BUDGET, not a boundary — it exists so you are not
    scanning 168 names every session. The full ranked list is one `universe()`
    call away, and you may trade something outside the top n; say why when you do.

    Columns: R (12-month return), sigma (daily volatility), trend (distance above
    the 200-day mean), score (the rank-average), eligible (12-month return > 0).
    """
    panel = _panel()
    r = screen.rank(panel, panel.index[-1], _all_tickers())
    return r.head(int(n)).round(4).to_json(orient="index", indent=2)


@mcp.tool()
def universe() -> str:
    """Ranked momentum screen with full transparency: scores names with sufficient
    price history and reports those that lack it. Call this when the top candidates
    do not suit and you want to see the full picture — including what's excluded
    and why.

    Returns a JSON object with two parts:
    - "ranked": scored and sorted names with their R, sigma, trend, score, eligible, rank
    - "unscoreable": names from config/universe.csv + ETF sleeve that exist in the price
      panel but lack sufficient history to compute momentum (need 252+ trading days for
      the 12-month return, 200 for the trend moving average)
    """
    panel = _panel()
    all_tickers = _all_tickers()

    # Get the ranked results
    r = screen.rank(panel, panel.index[-1], all_tickers)

    # Find which tickers were requested but not scored:
    # these exist in the panel but were dropped by momentum.compute() due to insufficient history
    tickers_in_panel = [c for c in all_tickers if c in panel.columns]
    unscoreable = sorted([t for t in tickers_in_panel if t not in r.index])

    # Build the result with both ranked names and unscoreable list
    ranked_json = json.loads(r.round(4).to_json(orient="index"))
    result = {
        "ranked": ranked_json,
        "unscoreable": [
            {"ticker": t, "reason": "insufficient price history (need 252+ trading days)"}
            for t in unscoreable
        ]
    }
    return json.dumps(result, indent=2)


def _selftest() -> None:
    assert ping() == "pong"
    assert mcp is not None
    # FIX 2: no snapshot on disk (marks.load() -> None) must not crash the tools.
    orig_load = marks.load
    marks.load = lambda: None
    try:
        p = json.loads(positions())
        assert p == {}, p
        a = json.loads(account())
        assert a == {k: None for k in
                     ("account_number", "account_value", "cash", "invested",
                      "as_of", "marked_at")}, a
    finally:
        marks.load = orig_load
    print("selftest OK: mcp server boots, ping responds, degrades to JSON without a snapshot")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        mcp.run()
