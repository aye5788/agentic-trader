"""FastMCP server exposing the agent's tool surface.

Run manually:   .venv/bin/python src/agent_env/server.py
Run the tests:  .venv/bin/python src/agent_env/server.py --selftest

Transport is stdio: Claude Code launches this as a subprocess and speaks MCP over
its stdin/stdout, so NOTHING may be printed to stdout except protocol traffic.
Diagnostics go to stderr.
"""
from __future__ import annotations

import json
import math
import os
from urllib.parse import parse_qs, urlparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from mcp.server.fastmcp import FastMCP   # noqa: E402

import marks                                    # noqa: E402
from research_store import read_current         # noqa: E402
from research_store import store                # noqa: E402
from agent_env import state                           # noqa: E402  sibling module
from agent_env import screen                          # noqa: E402  sibling module
from agent_env import terrain as terrain_mod          # noqa: E402  sibling module
import history as history_mod                   # noqa: E402
from agent_env import decide                          # noqa: E402  sibling module
from agent_env import live as live_mod                # noqa: E402  sibling module
from agent_env import memory                          # noqa: E402  sibling module
from agent_env import wakes                           # noqa: E402  sibling module
import mandate                                  # noqa: E402
import announce as announce_mod                 # noqa: E402  (module name != tool name)
import notify                                   # noqa: E402
import governance as gov                        # noqa: E402
import strategy as strat                        # noqa: E402
import momentum                                 # noqa: E402
import snapshot_freshness                       # noqa: E402
import pandas as pd                             # noqa: E402

mcp = FastMCP("agentic-trader")

EQUITY = REPO / "research_store" / "history" / "equity.jsonl"
JOURNAL = REPO / "research_store" / "journal.jsonl"
RH_POSITIONS = REPO / "research_store" / "rh" / "positions.json"
CLOSES = REPO / "research_store" / "prices" / "closes.parquet"
HIGHS = REPO / "research_store" / "prices" / "highs.parquet"
LOWS = REPO / "research_store" / "prices" / "lows.parquet"
OPENS = REPO / "research_store" / "prices" / "opens.parquet"
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


# How stale a broker snapshot may be before the agent must be told, measured in
# TRADING DAYS, not wall-clock hours.
#
# ⚠️ An hours threshold measures the wrong thing. The writers (the 10:00 fast
# loop, the monitor during RTH) stop at the close by design, so a PREMARKET
# session at 09:00 legitimately sees a 17h-old snapshot and a Monday premarket
# sees a 65h-old one -- both are the freshest data that exists, and an 8h rule
# would have fired the alarm on every premarket run from the day it shipped. An
# alert that fires on correct behaviour is one the agent learns to scroll past,
# which costs the alert its meaning at the moment it is finally right.
#
# One trading day of gap is normal (yesterday's close is today's premarket
# truth). Two means a writer has actually failed.
SNAPSHOT_STALE_BUSDAYS = 1


def _staleness(v: dict) -> dict | None:
    """How old is this snapshot, and is that a problem? -> dict, or None if fine.

    ⚠️ NOTHING USED TO SAY THIS. marks.py carries a comment acknowledging the
    total may be "possibly stale" and surfaced it nowhere, so a session whose
    snapshot writer had failed would plan against YESTERDAY'S holdings -- with
    placement allowed and no signal anything was wrong. That is the worst shape
    of failure available here: not zero orders, but confident wrong ones, sized
    and targeted against positions that may already have been sold.

    The writer is now agent_env.refresh_broker_snapshot() (every session, trading
    or not) plus record_fills() after a trade. The 10:00 fast loop used to write
    it too; when that was deleted on 2026-08-14 nothing took over, and the
    snapshot went two days stale on 08-16 while the monitor stop-watched a
    position that had already been sold. That is why this is loud.
    """
    from datetime import datetime, timezone           # noqa: PLC0415
    causal = snapshot_freshness.status(
        REPO / "research_store" / "rh" / "positions.json", JOURNAL)
    # Only attach the on-disk journal comparison when `v` represents that same
    # on-disk snapshot.  Unit tests and callers may pass an isolated valued dict.
    value_ts = snapshot_freshness.parse_ts(v.get("ts"))
    if causal["stale"] and value_ts == causal["snapshot_ts"]:
        return {"age": "OLDER THAN LAST FILL", "stale": True,
                "as_of": v.get("ts") or v.get("as_of"),
                "warning": ("⚠️ THIS BROKER SNAPSHOT PREDATES THE NEWEST "
                            "JOURNALLED FILL. Its holdings are not authoritative. "
                            "Call get_equity_positions and get_portfolio, then "
                            "record_fills with both broker outputs before acting.")}
    # Price marks do not refresh broker ownership.  Using marked_at here let a
    # fresh monitor quote disguise an old positions.json indefinitely.
    raw = v.get("ts") or v.get("as_of")
    if not raw:
        return {"age": "UNKNOWN", "stale": True,
                "warning": ("This snapshot carries NO timestamp, so its age "
                            "cannot be established. Treat every holding and "
                            "dollar figure in it as unverified: read positions "
                            "from Robinhood directly before acting on them.")}
    try:
        txt = str(raw)
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        if dt.tzinfo is None:                 # a bare date is midnight, not now
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:                         # noqa: BLE001
        return {"age": f"UNPARSEABLE ({raw})", "stale": True,
                "warning": ("This snapshot's timestamp could not be parsed, so "
                            "its age is unknown. Read positions from Robinhood "
                            "directly before acting on them.")}
    import numpy as _np                              # noqa: PLC0415
    hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    gap = int(_np.busday_count(dt.date(), datetime.now(timezone.utc).date()))
    if gap <= SNAPSHOT_STALE_BUSDAYS:
        return None
    return {"age": f"{hours:.1f}h ({gap} trading days)", "as_of": str(raw),
            "stale": True,
            "warning": (f"⚠️ THIS SNAPSHOT IS {gap} TRADING DAYS OLD (limit "
                        f"{SNAPSHOT_STALE_BUSDAYS}). Whoever refreshes it has "
                        f"probably failed. The positions and dollar figures below "
                        f"may describe a book you no longer hold. Call "
                        f"get_equity_positions and get_portfolio for the real "
                        f"state before sizing, stopping or selling anything.")}


@mcp.tool()
def positions() -> str:
    """Every position actually held, with the stop and targets ACTUALLY ENFORCED.

    `stop` and `targets` are what the monitor will use, resolved by
    src/levels.py -- not what you asked for. When an override you set was
    refused, `LEVELS_NOT_IN_FORCE` says so and names which part, and
    `your_stop` / `your_targets` show what you had asked for.

    `watched: false` means the position has NO stop being enforced — it is
    unprotected. That is deliberately reported rather than hidden.

    These columns are measurements, not recommendations, and row order has no
    policy meaning. `trade_pnl_at_stop` is P&L relative to entry and is not
    prospective risk. `mark_to_stop` assumes execution exactly at the stop;
    gaps and slippage can produce a larger loss. `stop_distance_sigma` is
    standardized distance under the stated volatility estimator, not
    stop-hit probability. No single column identifies the correct trim.
    Portfolio exposure, cluster concentration, strategy state, and the
    proposed order's before/after effects remain separate considerations.
    """
    prod = read_current()
    v = marks.load()
    ov = _overrides()
    out = state.holdings(v, prod.theses if prod else [], ov)
    # Excursion facts: what the path looked like, not just where it stands.
    # I/O lives here; the arithmetic is pure in src/excursion.py.
    try:
        import pandas as _pd                                   # noqa: PLC0415
        import excursion                                       # noqa: PLC0415
        _hi = _pd.read_parquet(REPO / "research_store" / "prices" / "highs.parquet")
        _events = [json.loads(l) for l in
                   (REPO / "research_store" / "journal.jsonl").read_text().splitlines()
                   if l.strip()]
    except Exception:
        _hi, _events = None, None
    # Correlation-cluster membership (Codex step 6, correlation half only). I/O
    # here, arithmetic pure in src/concentration.py. The whole model contract
    # travels with it -- lookback, threshold, panel as-of, minimum-observation
    # rule, version -- because a field called "cluster" with none of that reads
    # as a sector classification, which it is not.
    # Only when there ARE positions: with no snapshot this tool must return {}
    # and invent nothing, and its selftest asserts exactly that.
    try:
        import pandas as _pd2                                  # noqa: PLC0415
        import concentration as _conc                          # noqa: PLC0415
        _cl = _pd2.read_parquet(REPO / "research_store" / "prices" / "closes.parquet")
        _rep = _conc.clusters_report(list(out.keys()), _cl, _cl.index[-1]) if out else None
    except Exception:                                          # noqa: BLE001
        _rep = None
    if _rep:
        _av = float((marks.load() or {}).get("account_value") or 0.0)
        for _sym, _p in out.items():
            if not isinstance(_p, dict):
                continue
            _mem = _rep["members"].get(_sym, [_sym])
            _p["correlation_cluster_id"] = _rep["cluster_id"].get(_sym, _sym)
            _p["correlation_cluster_members"] = _mem
            _wsum = sum(float(out[m].get("market_value") or 0.0)
                        for m in _mem if isinstance(out.get(m), dict))
            _p["correlation_cluster_weight_pct_nav"] = (
                round(_wsum / _av, 6) if _av > 0 else None)
        out["_correlation_cluster_model"] = _rep["model"]

    if _hi is not None:
        for _sym, _p in out.items():
            if not isinstance(_p, dict) or _p.get("avg_cost") is None:
                continue
            _ent = excursion.entry_date(_events, _sym)
            _series = []
            if _ent is not None and _sym in _hi.columns:
                _series = [x for x in _hi.loc[_hi.index >= _ent, _sym].dropna().tolist()]
            _p.update(excursion.facts(_p.get("avg_cost"), _p.get("mark"),
                                      _p.get("stop"), _series))
            if _ent is None:
                # I5 (final review): since abb4338, entry_date() returns the
                # LATER buy on a genuine re-entry -- a re-entry is no longer a
                # reason this can be None. What's actually left: the symbol
                # was never bought, was bought and fully closed with no
                # re-buy since, or a fill's quantity could not be derived at
                # all (see src/excursion.py:entry_date's docstring).
                _p["excursion_note"] = (
                    "no current holding period could be derived for this "
                    "symbol (never bought, fully closed with no re-buy "
                    "since, or a fill whose quantity could not be "
                    "determined) — peak not computed")

    # I4 (final review): the displayed `stop` may be the agent's OWN override
    # (state.holdings() reports it whenever one exists) even when the
    # monitor's own apply-time price guard (apply_overrides(), mirrored here
    # by decide.evaluate_enforcement) will REFUSE to apply it -- e.g. an
    # override that raises the thesis's stop but has no live price the
    # monitor knows about. gain_protected_pct above is computed straight from
    # this `stop`, so an unenforceable override otherwise produces a
    # confident, wrong protection figure. Flag the record rather than
    # restructure state.holdings() -- the arithmetic already exists in
    # evaluate_enforcement, this just asks it the same question set_levels()
    # does at write time, now at read time.
    _by_sym = {t.symbol: t for t in (prod.theses if prod else [])}
    _owned_set = decide.load_owned()
    for _sym, _p in out.items():
        if not isinstance(_p, dict) or not _p.get("set_by_agent") or _p.get("stop") is None:
            continue
        _thesis = _by_sym.get(_sym)
        _owned = None if _owned_set is None else (_sym in _owned_set)
        _enf = decide.evaluate_enforcement(
            stop=_p["stop"], target=None,
            has_thesis=_thesis is not None,
            target_weight=getattr(_thesis, "target_weight", None),
            verdict=getattr(_thesis, "verdict", "") or "",
            owned=_owned,
            current_stop=_p.get("book_stop"),
            current_targets=list(getattr(_thesis, "targets", []) or []) if _thesis else None,
            price=decide.load_price(_sym),
            # ⛔ THE WIDEN FLAG WAS NEVER PASSED. evaluate_enforcement takes it
            # and defaults it False, so a DELIBERATELY widened stop -- the one
            # case where a looser override IS applied by the monitor -- was
            # reported as rejected. Live on 2026-08-18: MRK carried
            # widen=true and was the only override actually in force, and this
            # report said otherwise. The monitor requires widen AND a reason,
            # so both are read here from the same override record.
            widen=bool((_ovr := (ov.get(_sym) or {})).get("widen"))
                  and bool(str(_ovr.get("reason") or "").strip()),
        )
        _p["stop_enforced"] = _enf["stop"]["enforced"]
        if not _enf["stop"]["enforced"]:
            _p["stop_enforcement_note"] = _enf["stop"]["note"]

    # A level does not expire (2026-08-17) and can outlive the position it was
    # written for -- clear_levels() is the remedy, but a session that forgets
    # to call it needs to be able to SEE what it left behind, or a cleared
    # level is not reliably known to be gone (see docs/superpowers/plans/
    # 2026-08-17-levels-persistence.md, "the hazard part 3/4 close"). Any
    # override symbol not in `out` (the held set state.holdings() just built
    # from the SAME `ov`) is surfaced here, under its own key rather than
    # folded into a symbol's own record -- there is no held record for it.
    # Present ONLY when at least one exists, so a clean book (every override
    # matches a held name) never carries an empty, meaningless key.
    orphaned = {sym: {"reason": o.get("reason")} for sym, o in (ov or {}).items()
                if sym not in out}
    if orphaned:
        out["levels_without_positions"] = orphaned
    # The staleness banner rides with the HOLDINGS too, not only account(). An
    # agent that reads positions() and never calls account() would otherwise act
    # on a stale book with nothing telling it so.
    # NO snapshot is a different state from a STALE one, and already handled:
    # the agent gets {} here and nulls from account(). Blurring them would put a
    # scary banner on a fresh deploy that simply has not marked yet. The banner
    # is for a snapshot that EXISTS and is old -- data that looks usable and
    # is not.
    stale = _staleness(v) if v else None
    if stale:
        out = {"STALE": stale, "holdings": out}
    return json.dumps(out, indent=2, default=str)


@mcp.tool()
def account() -> str:
    """Account value, cash, invested capital, and when it was last marked.

    Degrades to an object of nulls (never raises) when no snapshot exists yet
    — a fresh deploy, or a deleted/corrupt snapshot, is a foreseeable state,
    not an error; a tool that raises tells the agent nothing about what it holds.
    """
    v = marks.load() or {}
    out = {k: v.get(k) for k in
           ("account_number", "account_value", "cash", "invested",
            "as_of", "marked_at")}
    stale = _staleness(v)
    if stale:
        out["STALE"] = stale
    bp = v.get("buying_power")
    out["buying_power"] = bp
    out["unsettled_funds"] = v.get("unsettled_funds")
    if bp is None:
        out["spendable"] = ("UNKNOWN from this snapshot. Call Robinhood's "
                            "get_portfolio for the authoritative buying_power "
                            "before sizing any buy — that is the figure an order "
                            "is checked against, and this snapshot lacks it.")
    else:
        out["spendable"] = (f"{bp} — size buys against THIS. Limited-margin "
                            f"account: sale proceeds are spendable the same "
                            f"session, so a sale can fund a purchase today.")
    return json.dumps(out, indent=2, default=str)


@mcp.tool()
def compare_trims(notional: float, symbols: str = "") -> str:
    """What would an equal-sized trim in each holding actually change?

    Answers one counterfactual and nothing else. It does not approve, reject,
    rank or select an action, and it returns no composite score. `symbols` is a
    comma-separated list, or empty for every holding.

    Returned per name: trim quantity and proceeds, post-trim value and weight,
    the change in position weight, the change in gross exposure, the change in
    stop-defined downside as a share of NAV, and the trimmed name's strategy
    rank.

    DELIBERATELY ABSENT: an estimated portfolio-volatility change, an estimated
    slippage, and a cluster-weight change. Each needs a model this system has
    not specified and validated, and an unvalidated estimate sitting beside
    measured facts is indistinguishable from one.
    """
    v = marks.load() or {}
    prod = read_current()
    held = state.holdings(v, prod.theses if prod else [], _overrides())
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()] or None
    return json.dumps(
        state.compare_trims(held, v.get("account_value") or 0.0, float(notional), syms),
        indent=2, default=str)


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


@mcp.tool()
def terrain(symbol: str) -> str:
    """How far this name actually travels, in units of its own daily volatility.

    Use this to set stops and targets against measured behaviour rather than a
    formula. `mfe_median` at horizon 5 is the typical BEST move over five sessions
    in sigma; `mae_median` the typical worst. A target beyond `mfe_p90` is reached
    less than one time in ten.

    Context: the retired formula placed the first target 5.5 sigma out, which
    price reached about 2.6% of the time in five days, while its 2.5-sigma stop
    was hit about 20% of the time (scripts/calibrate_geometry.py, 2026-08-09).
    """
    return json.dumps(terrain_mod.excursions(
        pd.read_parquet(CLOSES), pd.read_parquet(HIGHS), pd.read_parquet(LOWS),
        symbol.strip().upper()), indent=2, default=str)


# `days` is clamped to a sane request range, never trusted as-is: a caller
# asking for 5000 would otherwise load and serialize the whole panel on every
# call, and 0 or a negative number would silently return no bars at all. This
# is a payload-shape guard, not a trading rule -- it bounds what history()
# hands back, not what the agent may do with it.
HISTORY_DAYS_MIN = 5
HISTORY_DAYS_MAX = 252


@mcp.tool()
def history(symbol: str, days: int = 60) -> str:
    """Daily bars and the levels derived from them — where this name has actually traded.

    Use this to decide WHERE a level belongs. `terrain()` tells you how far this
    name travels in its own volatility; this tells you where it has been, so a
    stop can sit under a prior low or a base rather than at an arbitrary distance.

    ⚠️ Ends at the last COMPLETED session, not today — today's bar is still
    forming. Use `quote()` for the live price and combine the two.

    `days` is capped; ask for what you need rather than the maximum.
    """
    sym = symbol.strip().upper()
    try:
        clamped = max(HISTORY_DAYS_MIN, min(HISTORY_DAYS_MAX, int(days)))
        result = history_mod.series(
            pd.read_parquet(OPENS), pd.read_parquet(HIGHS), pd.read_parquet(LOWS),
            pd.read_parquet(CLOSES), sym, clamped)
    except Exception as e:      # noqa: BLE001 — never raise, never an empty success
        return json.dumps({"error": f"could not load price history for {sym}: {e}"},
                          indent=2)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def set_levels(symbol: str, stop: float,
               targets: float | list[float] | None = 0.0,
               reason: str = "", widen: bool = False) -> str:
    """Set YOUR stop and take-profit(s) for a position. `reason` is required.

    This WRITES to the override file the monitor merges every poll -- it does
    NOT force the monitor to act on it. scripts/market_monitor.py only ever
    looks at a symbol's overrides if it is in the monitor's `held` set, which
    requires: a thesis with target_weight > 0 and a stop (the BOOK filter),
    AND that the position is actually owned per the broker snapshot (the
    OWNERSHIP filter) -- a name in tonight's book whose buy has not filled yet
    is invisible to the monitor no matter what levels you set here. Within
    that set, your stop is applied only if it RAISES the thesis's current
    stop, and your targets are applied only if the COUNT matches the thesis's
    existing targets -- apply_overrides then moves them in EITHER direction,
    so a raise is applied exactly like a lower.

    `targets` accepts a single number or a list -- pass ALL of a multi-target
    thesis's targets together. `positions()` shows you the current list; a
    thesis with two targets requires two here, or the whole set is ignored.

    `widen=True` is how you LOOSEN a stop deliberately -- the one exception to
    "stricter only". Use it when an inherited stop sits inside the name's own
    noise, where it is not protection but a guarantee of being taken out by
    nothing: `terrain()` and `history()` tell you where that is. It is
    deliberate, attributable, and recorded -- the monitor honours a widen ONLY
    together with a `reason` (already required on every call here), so a
    widen with no reason is not merely undocumented, it is refused and
    therefore pointless. Widening does NOT relax the price guard below: a stop
    at or above the current price is still refused regardless of widen --
    that guard is about arming a breached stop, which widening a level below
    spot is never about.

    Read the `enforcement` object in the response before assuming a position is
    protected -- `ok: true` only means the write succeeded, not that either
    level will actually be acted on. Pass targets=0 to set a stop with no
    take-profit. Use `terrain()` first so the levels sit where price actually
    goes.
    """
    sym = symbol.strip().upper()
    # Accept 0/None (no target), one number, or the full list. A thesis with
    # two targets REQUIRES two here -- the monitor applies a target list only
    # when the count matches, so a single value on a two-target thesis is
    # silently ignored. positions() shows you the current list.
    #
    # ⚠️ EVERY SPELLING OF "NO TARGET" MUST BE ACCEPTED, NOT JUST THE
    # NUMERIC ONES. `targets` used to carry no type annotation at all, which
    # left FastMCP no type to build a real MCP schema from -- it fell back to
    # advertising `targets` as a plain STRING. A well-behaved client follows
    # this docstring's own instruction ("Pass targets=0 to set a stop with no
    # take-profit") and, under a string schema, sends the STRING "0". That
    # matched none of `(0, 0.0, None, "")` below, fell through to `[targets]`
    # = `["0"]`, and merge_levels() raised ValueError on a non-positive
    # target price BEFORE it ever wrote anything -- discarding the STOP too,
    # which is the real damage: a single malformed target silently vetoed a
    # protective stop the agent had every intention of setting. The type
    # annotation above fixes the schema (FastMCP/pydantic now advertise and
    # coerce number/array/null, so a real MCP client won't send a string at
    # all); this widened check is defense-in-depth for any caller that still
    # does -- every stringy/zero/blank spelling degrades to "no target",
    # never to a malformed one that aborts the whole write.
    if (targets in (0, 0.0, None, "")
            or (isinstance(targets, str) and targets.strip() in ("", "0", "0.0"))):
        _targets = None
    elif isinstance(targets, (list, tuple)):
        _targets = list(targets)
    else:
        _targets = [targets]
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # ⚠️ A STOP THAT CANNOT TRIGGER IS NOT A STOP. Without this, set_levels(sym,
    # stop=0.01) satisfies the `watched: true` flag and clears the
    # unprotected-position check while protecting nothing — defeating the safety
    # check by satisfying its letter. That is the same shape of defect as a test
    # that asserts nothing: it reads as coverage and is hollow. Rejected here,
    # loudly, rather than accepted and reported as protected.
    # Reference price comes from the SNAPSHOT already in memory, never a live
    # quote: set_levels sits on the decision path, and a network round trip here
    # would add latency to every level set and make this untestable offline.
    _v = marks.load() or {}
    _pos = (_v.get("positions") or {}).get(sym) or {}
    px = _pos.get("mark") or _pos.get("avg_cost")
    if px and stop and float(stop) > 0:
        # ⛔ CEILING FIRST. The guard below only rejected stops that are too FAR
        # from spot; there was no upper bound at all, so a stop ABOVE the live
        # price was accepted and returned "enforced": true. That is not a stop --
        # it is already breached the instant it is written, and the monitor is a
        # live armed service that polls every 15s and sells the WHOLE position at
        # market on a breach. One accepted level was therefore a full liquidation
        # with no order ever placed by the agent, which is how it slipped past
        # the order gate entirely (demonstrated on AMD: stop 474.00 against a
        # 473.65 mark -> TRIGGERS fraction 1.0).
        #
        # A long stop must sit BELOW the price it protects. Refused rather than
        # clamped: silently moving a level the agent chose would teach it that
        # the number it set is not the number in force.
        if float(stop) >= float(px):
            return json.dumps({
                "ok": False,
                "error": (f"stop {float(stop):.4f} is at or ABOVE the last price "
                          f"{float(px):.4f} — it is breached the moment it is "
                          f"set, and the monitor would sell this whole position "
                          f"at market within seconds. A stop sits below the "
                          f"price it protects. To exit now, sell deliberately "
                          f"rather than by arming a tripwire under your feet."),
                "last_price": px,
            }, indent=2)

        floor_px = float(px) * MIN_STOP_FRACTION
        if float(stop) < floor_px:
            return json.dumps({
                "ok": False,
                "error": (f"stop {float(stop):.4f} is {float(stop)/float(px):.1%} "
                          f"of the last price {float(px):.4f} — that far away it "
                          f"can never trigger, so it is not protection. It would "
                          f"still mark this position `watched`, which is worse "
                          f"than leaving it visibly unprotected. Set a stop you "
                          f"would actually act on (see terrain(symbol) for what "
                          f"this name measurably does), or close the position."),
                "last_price": px,
                "nearest_allowed_stop": round(floor_px, 4),
            }, indent=2)

    try:
        # An empty list must become None (a populated one passes through
        # intact) -- merge_levels/write_levels treat "no targets supplied"
        # and "an explicit empty list" identically, but keeping the `or None`
        # normalisation here matches the prior singular-value contract.
        merged = decide.write_levels(symbol, stop, _targets or None, reason, ts,
                                     widen=bool(widen))
    except ValueError as e:
        return json.dumps({"ok": False, "error": str(e)}, indent=2)

    # M-b (final review): clear_levels() journals its decision; set_levels()
    # did not -- so the ledger recorded a level's REMOVAL and reason but never
    # its CREATION, and a level's original reason lived only inside
    # overrides.json, meaning clearing it erased the only record of why it was
    # ever set. Mirror clear_levels()'s exact journalling path
    # (decide.decision_entry + store.append_journal) -- reason is already
    # guaranteed non-blank here, since write_levels()/merge_levels() would
    # have raised ValueError above otherwise.
    store.append_journal(decide.decision_entry(sym, "set_level", reason.strip(), ts))

    prod = read_current()
    thesis = None
    if prod:
        for t in prod.theses:
            if str(t.symbol).strip().upper() == sym:
                thesis = t
                break

    owned_set = decide.load_owned()
    owned = None if owned_set is None else (sym in owned_set)

    # ⛔ THE REPORT MUST NOT CLAIM ENFORCEMENT THE MONITOR WILL REFUSE.
    # scripts/market_monitor.py:apply_overrides() (Part 3, 2026-08-17) fails
    # CLOSED on a stop it has no live price for -- this call used to omit
    # `price` entirely, which falls back to evaluate_enforcement()'s sentinel
    # (pre-guard) behaviour and reports `enforced: true` regardless of price.
    # That is the exact defect this whole project exists to remove: the
    # advisory layer telling the agent something the enforcement layer does
    # not do.
    #
    # SOURCE: decide.load_price(sym), NOT `px`. `px` (above) is only good for
    # the ceiling/floor REFUSAL guard, where it is correct either way --
    # refusing a stop at or above the last known price cannot be wrong,
    # whichever source that price came from. But `px` FALLS BACK to
    # marks.load()'s avg_cost whenever no live monitor quote exists
    # (src/marks.py's mark-priority chain), so a position bought long ago and
    # never quoted by the monitor since -- or never quoted at all -- still
    # produces a perfectly usable `px` from cost basis alone. apply_overrides()'s
    # real call site never reads avg_cost; it reads ONLY
    # research_store/monitor/quotes.json and fails closed when this symbol has
    # no entry there. Reporting enforcement against `px` therefore could -- and
    # did -- disagree with what the monitor will actually do: a symbol the
    # monitor has never quoted (cost basis still usable) read `enforced: true`
    # here and would be refused there. THE DISAGREEMENT IS THE INFORMATION --
    # decide.load_price() reads the SAME file with the SAME "no entry -> None"
    # fail-closed behaviour apply_overrides()'s call site has, so this report
    # can never claim more than the monitor actually knows.
    _enf_price = decide.load_price(sym)

    enforcement = decide.evaluate_enforcement(
        stop=merged[sym]["stop"], target=merged[sym].get("targets") or None,
        has_thesis=thesis is not None,
        target_weight=getattr(thesis, "target_weight", None),
        verdict=getattr(thesis, "verdict", "") or "",
        owned=owned,
        current_stop=getattr(thesis, "stop", None),
        current_targets=list(getattr(thesis, "targets", []) or []) if thesis else None,
        price=_enf_price,
        # Read back from `merged[sym]`, not the raw `widen` argument -- that is
        # exactly what got WRITTEN (merge_levels only sets the key when True),
        # so the enforcement report can never claim a widen that was not
        # actually recorded.
        widen=bool(merged[sym].get("widen")),
    )
    return json.dumps({"ok": True, "symbol": sym, "written": merged[sym],
                       "enforcement": enforcement}, indent=2)


@mcp.tool()
def clear_levels(symbol: str, reason: str = "") -> str:
    """Remove YOUR stop/target for a symbol. Do this when you close a position.

    Levels do not expire. An override you leave behind is inert while the name
    is not held, but it WAKES UP if the name re-enters the book later -- at a
    price it was never written for. Clearing is part of closing a position, not
    bookkeeping after it.

    `reason` is required, and is journalled like any other decision. Clearing a
    symbol with no level on file is a NO-OP, not an error -- you should not need
    to know whether one was ever set before calling this after an exit.
    """
    sym = symbol.strip().upper()
    if not reason or not reason.strip():
        return json.dumps({"ok": False,
                           "error": "reason is required: a level cleared for no "
                                    "recorded reason cannot be judged later"}, indent=2)
    remaining = decide.clear_levels_file(sym)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry = decide.decision_entry(sym, "clear_level", reason.strip(), ts)
    store.append_journal(entry)
    return json.dumps({"ok": True, "symbol": sym, "cleared": True,
                       "remaining_symbols": sorted(remaining)}, indent=2)


@mcp.tool()
def record_fills(orders: str, broker_positions: str = "", portfolio: str = "") -> str:
    """Journal the orders you PLACED, so the fill is on the permanent record.

    ⛔ CALL THIS AFTER EVERY SESSION IN WHICH YOU PLACED AN ORDER, before you
    finish. Pass the raw JSON you got back from `get_equity_orders` — the whole
    thing; this reads it, keeps only the FILLED ones, and skips any order_id
    already recorded, so calling it twice is safe and calling it with extra
    orders in the payload is safe.

    WHY IT MATTERS AND WHAT BREAKS WITHOUT IT
        `record_decision` records what you DECIDED. This records what actually
        EXECUTED, and they are different things: a decision can be right and the
        order rejected, or filled at a price you did not expect. Everything
        downstream keys on the execution, not the decision — the weekly letter
        counts trades from it, reconcile_ledger.py checks it against the broker,
        the outcome ledger attaches realized P&L to it, and `performance` reads
        it back to you next session.

        Measured 2026-08-12: two real sells filled at the broker and NOTHING
        journalled them, because the legacy loop's record_fills.py is a shell
        script you have no shell to run. The letter would have reported a week
        in which you did nothing.

    Also requires a completeness-proven transcript of the post-trade
    get_equity_positions pages and the raw get_portfolio output. The same call
    atomically rewrites positions.json, so an execution cannot be recorded as
    complete while account state stays old. See refresh_broker_snapshot for the
    required page-transcript shape.
    """
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        raw = json.loads(orders) if isinstance(orders, str) else orders
    except Exception as e:      # noqa: BLE001
        return json.dumps({"ok": False, "error": f"could not parse orders JSON: {e}"},
                          indent=2)

    # accept the tool's own envelope, a bare list, or {"orders": [...]}
    if isinstance(raw, dict):
        raw = (raw.get("data") or raw).get("orders", raw.get("orders", []))
    if not isinstance(raw, list):
        return json.dumps({"ok": False, "error": "no orders[] found in the payload"},
                          indent=2)

    try:
        seen = set()
        for e in store.read_journal():
            if e.get("event") == "execution":
                for f in (e.get("fills") or []):
                    if f.get("order_id"):
                        seen.add(str(f["order_id"]))
    except Exception:           # noqa: BLE001
        seen = set()

    fills = []
    filled_orders = []
    for o in raw:
        if not isinstance(o, dict) or str(o.get("state")) != "filled":
            continue
        filled_orders.append(o)
        oid = str(o.get("id") or "")
        if not oid or oid in seen:
            continue           # idempotent: never double-append an order_id
        qty = o.get("cumulative_quantity") or o.get("quantity")
        px = o.get("average_price") or o.get("price")
        amt = ((o.get("dollar_based_amount") or {}).get("amount")
               if isinstance(o.get("dollar_based_amount"), dict) else None)
        try:
            amount = float(amt) if amt is not None else (
                float(qty) * float(px) if qty and px else None)
        except (TypeError, ValueError):
            amount = None
        fills.append({"symbol": o.get("symbol"), "side": o.get("side"),
                      "quantity": qty, "avg_price": px, "amount": amount,
                      "order_id": oid, "status": "filled",
                      "placed_at": o.get("created_at")})

    snapshot_result = None
    if filled_orders:
        snapshot_result = _write_broker_snapshot(broker_positions, portfolio, ts)

    if not fills:
        reconciled = not filled_orders or bool(snapshot_result and snapshot_result.get("ok"))
        return json.dumps({"ok": reconciled, "recorded": 0,
                           "snapshot": snapshot_result,
                           "note": ("nothing new to record — every filled order "
                                    "in this payload was already journalled, or "
                                    "none were filled")}, indent=2)

    store.append_journal({"event": "execution", "ts": ts,
                          "source": "session", "fills": fills})
    return json.dumps({"ok": bool(snapshot_result and snapshot_result.get("ok")),
                       "recorded": len(fills), "fills": fills,
                       "snapshot": snapshot_result,
                       "error": (None if snapshot_result and snapshot_result.get("ok")
                                 else "fills were journalled but positions.json was NOT refreshed")},
                      indent=2)


def _payload(value):
    return json.loads(value) if isinstance(value, str) else value


def _num(value, field: str) -> float:
    """Parse a broker number. REJECTS non-finite — never coerces silently.

    `float("nan")` succeeds and `json.dumps` will happily emit a bare NaN, which
    is not valid JSON and poisons every consumer that reads it back. A number we
    cannot trust must stop the write, not enter the snapshot.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field}: {value!r} is not a number")
    if not math.isfinite(f):
        raise ValueError(f"{field}: {value!r} is not finite")
    return f


def _position_pages(value) -> tuple[list, str]:
    """Verify a caller-supplied, cursor-linked transcript through exhaustion.

    The broker read is paginated.  A bare response can never prove it is the
    whole book, even when it happens to contain every current holding.  The
    accepted shape is::

        {"pages": [
          {"cursor": null, "response": <raw first response>},
          {"cursor": "...", "response": <raw next response>}
        ], "exhausted": true}

    Every non-final response must point at the cursor recorded for the next
    request, and the final response must have no next URL/cursor.  Thus dropping
    either a middle page or the tail makes the transcript fail closed.
    """
    proof = _payload(value)
    if not isinstance(proof, dict) or proof.get("exhausted") is not True:
        raise ValueError(
            "positions completeness proof required: pass a cursor-linked pages[] "
            "transcript with exhausted=true; a bare positions response cannot "
            "prove that every pagination page was read")
    pages = proof.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ValueError("positions completeness proof has no pages[]")

    rows = []
    account_number = ""
    for i, item in enumerate(pages):
        if not isinstance(item, dict) or "response" not in item or "cursor" not in item:
            raise ValueError(f"positions proof page[{i}] needs cursor and response")
        supplied_cursor = item["cursor"]
        if i == 0 and supplied_cursor not in (None, ""):
            raise ValueError("positions proof must begin with the cursorless first page")

        envelope = _payload(item["response"])
        raw = envelope["data"] if isinstance(envelope, dict) and "data" in envelope else envelope
        page_rows = raw.get("positions") if isinstance(raw, dict) else None
        if not isinstance(page_rows, list):
            raise ValueError(f"positions proof page[{i}] is not a raw positions response")
        rows.extend(page_rows)
        page_account = str(raw.get("account_number") or "")
        if page_account and account_number and page_account != account_number:
            raise ValueError("positions proof pages belong to different accounts")
        account_number = account_number or page_account

        # Connectors differ on whether pagination metadata sits beside `data`
        # or inside it. Accept both, but never infer exhaustion from row count.
        next_value = (envelope.get("next") or envelope.get("next_url")
                      if isinstance(envelope, dict) else None)
        if not next_value:
            next_value = raw.get("next") or raw.get("next_url")
        next_cursor = None
        if next_value:
            if not isinstance(next_value, str):
                raise ValueError(f"positions proof page[{i}] has a non-string next link")
            parsed = parse_qs(urlparse(next_value).query).get("cursor", [])
            if len(parsed) != 1 or not parsed[0]:
                raise ValueError(f"positions proof page[{i}] next link has no unique cursor")
            next_cursor = parsed[0]

        if i + 1 < len(pages):
            if next_cursor is None:
                raise ValueError(f"positions proof continues after exhausted page[{i}]")
            if str(pages[i + 1].get("cursor")) != next_cursor:
                raise ValueError(f"positions proof page[{i + 1}] cursor does not follow page[{i}]")
        elif next_cursor is not None:
            raise ValueError(
                f"positions proof stops before exhaustion; page[{i}] has another cursor")
    return rows, account_number


def _write_broker_snapshot(broker_positions, portfolio, ts: str,
                           *, liquidated: bool = False) -> dict:
    """VALIDATE broker reads, then atomically publish the shares/cost snapshot.

    ⛔ THIS USED TO COERCE, NOT VALIDATE, and the difference is the whole point.
    The previous version silently skipped any row it could not read, so a
    TRUNCATED response — three of twelve positions — was published as the whole
    book and the other nine were deleted from local truth. An EMPTY list wrote
    `positions: {}` and returned ok, erasing every holding. A missing average
    cost became 0.0. NaN passed straight through into the file. None of it
    raised; all of it looked like a successful reconciliation.

    Its docstring already said "Validate broker reads". The word was there and
    the behaviour was not, which is the defect class this repo keeps paying for.

    The rule now: NO WRITE WITHOUT PAGINATION EXHAUSTION EVIDENCE, and ANY row
    we cannot fully parse REJECTS THE WHOLE SNAPSHOT. A
    partial book is more dangerous than a stale one — stale is detectable and
    was detected; partial looks current, carries a fresh timestamp, and silently
    unprotects whatever it dropped.

    `liquidated=True` is the ONLY way to publish an empty holdings list. It must
    be asserted deliberately by a caller that has confirmed the account really
    is flat, because "no positions" is indistinguishable from "the read failed"
    and is the single most destructive thing this function can write.
    """
    try:
        rows, account_number = _position_pages(broker_positions)
        port_raw = _payload(portfolio)
        if isinstance(port_raw, dict) and "data" in port_raw:
            port_raw = port_raw["data"]
        if not isinstance(rows, list) or not isinstance(port_raw, dict):
            raise ValueError("pass proven get_equity_positions pages and raw get_portfolio output")

        positions = {}
        for i, row in enumerate(rows):
            # REJECT, never skip. A row we cannot read means the response is not
            # trustworthy, and publishing the rest of it deletes real positions.
            if not isinstance(row, dict):
                raise ValueError(f"positions[{i}] is {type(row).__name__}, not an object")
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                raise ValueError(f"positions[{i}] has no symbol")
            if symbol in positions:
                raise ValueError(f"positions[{i}]: duplicate symbol {symbol}")
            raw_qty = row.get("quantity", row.get("qty"))
            if raw_qty is None:
                raise ValueError(f"{symbol}: no quantity in the broker row")
            qty = _num(raw_qty, f"{symbol}.quantity")
            if qty < 0:
                raise ValueError(f"{symbol}: negative quantity {qty}")
            if qty == 0:
                continue          # a genuinely closed position; not an error
            raw_avg = row.get("average_buy_price", row.get("avg_cost"))
            if raw_avg is None:
                raise ValueError(f"{symbol}: no average cost in the broker row")
            positions[symbol] = {"qty": qty, "avg_cost": _num(raw_avg, f"{symbol}.avg_cost")}
            account_number = account_number or row.get("account_number")

        # ⛔ AN EMPTY BOOK MUST BE ASSERTED, NEVER INFERRED. This is the write
        # that erases everything, and "the read came back empty" and "the
        # account is flat" are the same bytes.
        if not positions and not liquidated:
            raise ValueError(
                "broker read produced NO positions. Refusing to publish an empty "
                "book — this is indistinguishable from a failed read and would "
                "erase every holding. If the account really is flat, the caller "
                "must assert liquidated=True.")

        total = port_raw.get("total_value", port_raw.get("account_value"))
        cash = port_raw.get("cash")
        if total is None or cash is None:
            raise ValueError("portfolio output lacks total_value/account_value or cash")
        acct = str(port_raw.get("account_number") or account_number or "")

        # IDENTITY. A snapshot from the wrong account is worse than none: it is
        # a confident, fresh-looking description of a book that is not yours.
        # CLAUDE.md hard rule 1 — one account is tradeable, every other is
        # read-only — makes writing another account's state here a mandate breach.
        expected = _expected_account()
        # ⛔ A KNOWN IDENTITY MAY NEVER BE REPLACED BY AN UNKNOWN ONE.
        # _expected_account() reads the identity back OUT of this very file, so
        # publishing a blank one does not merely lose a field — it makes
        # `expected` blank on every future write and disarms BOTH branches of
        # this guard permanently. Nothing repairs it, because each subsequent
        # write re-reads the blank it wrote. This happened on 2026-08-17 and had
        # to be restored by hand (as it had been once before, in e49c82b);
        # restoring the value never fixed the class, because the publisher kept
        # accepting the blank. Refuse instead — the caller supplies identity,
        # exactly as it must supply completeness.
        if expected and not acct:
            raise ValueError(
                f"no account number in the broker payload, but this system "
                f"trades {expected}. Refusing to publish a snapshot whose "
                f"identity cannot be checked: a blank identity is read back as "
                f"'no expected account' and would silently disarm this guard "
                f"for every future write.")
        if expected and acct and acct != expected:
            raise ValueError(
                f"account mismatch: broker payload is for {acct}, this system "
                f"trades {expected}. Refusing to publish another account's state.")

        snap = {"account_number": acct,
                "account_value": _num(total, "portfolio.total_value"),
                "cash": _num(cash, "portfolio.cash"),
                "as_of": date.today().isoformat(), "ts": ts,
                "positions": positions}
        for key in ("buying_power", "unsettled_funds"):
            v = port_raw.get(key)
            if isinstance(v, dict):                 # RH nests buying_power
                v = v.get(key)
            if v is not None:
                snap[key] = _num(v, f"portfolio.{key}")

        path = RH_POSITIONS
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snap, indent=2, allow_nan=False) + "\n")
        os.replace(tmp, path)
        return {"ok": True, "ts": ts, "positions": len(positions)}
    except Exception as e:  # noqa: BLE001 - return loudly through the MCP tool
        return {"ok": False, "error": str(e)}


def _expected_account() -> str:
    """The one tradeable account number, from the existing snapshot. '' if unknown.

    Deliberately read from the snapshot rather than config: the account number is
    not in any config file, and the previous snapshot is the system's own record
    of which account it trades. On a first-ever write there is nothing to compare
    against and the check stands down rather than blocking setup.
    """
    try:
        return str(json.loads(RH_POSITIONS.read_text()).get("account_number") or "")
    except Exception:                                # noqa: BLE001
        return ""


@mcp.tool()
def refresh_broker_snapshot(broker_positions: str, portfolio: str,
                            liquidated: bool = False) -> str:
    """Publish the broker's CURRENT account state. Call this before you finish.

    ⛔ EVERY SESSION, WHETHER OR NOT YOU TRADED. Read get_equity_positions from
    the cursorless first page through the page with no `next`, then pass this
    JSON as broker_positions (response is each tool's raw output)::

      {"pages":[{"cursor":null,"response":FIRST},
                  {"cursor":"CURSOR_FROM_FIRST_NEXT","response":SECOND}],
       "exhausted":true}

    Each cursor must match the prior response's next URL. Also pass the raw
    get_portfolio output. This is what keeps the system's picture of your book
    from drifting away from the broker's.

    WHY IT EXISTS. Refreshing state used to be possible only through
    `record_fills()`, and only when that call contained a filled order — so a
    session that traded nothing could not correct the file even when it could
    see the truth. On 2026-08-14 a session sold AMAT, journalled the fill, and
    the snapshot was never rewritten; it sat two days stale, still listing a
    position that had been sold, while the stop watcher tracked a phantom and
    every downstream number was wrong. Nothing could repair it, because repair
    required a trade.

    Journalling a fill and publishing account state are different jobs with
    different prerequisites: a fill needs an execution, state needs only a good
    read. Coupling them made the second impossible without the first.

    ⚠️ IT CAN REFUSE, AND A REFUSAL IS NOT A FORMALITY. The write is rejected
    outright — leaving the previous snapshot intact — if pagination exhaustion
    is not proven, a page is omitted or mis-linked, any row is unreadable,
    a quantity or cost is missing or non-finite, a symbol repeats, or the
    payload belongs to a different account. A partial book is more dangerous
    than a stale one: stale is detectable, partial looks current.

    `liquidated=True` is required to publish an EMPTY book, and only assert it
    when you have confirmed the account is genuinely flat. "No positions
    returned" and "the read failed" are the same bytes.

    `ok:false` means the snapshot was NOT updated. Say so; do not report the
    session reconciled.
    """
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return json.dumps(_write_broker_snapshot(broker_positions, portfolio, ts,
                                             liquidated=bool(liquidated)), indent=2)


@mcp.tool()
def record_decision(symbol: str, action: str, reason: str) -> str:
    """Record a decision and WHY, to the append-only journal.

    Call this for anything you decide, including deciding NOT to act — a
    considered pass is a decision, and a later session cannot tell the difference
    between 'ruled this out' and 'never looked' unless you say so.

    action is free text: open, add, trim, exit, hold, skip, tighten_stop, …
    """
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        entry = decide.decision_entry(symbol, action, reason, ts)
    except ValueError as e:
        return json.dumps({"ok": False, "error": str(e)}, indent=2)
    store.append_journal(entry)
    return json.dumps({"ok": True, "recorded": entry}, indent=2)


@mcp.tool()
def announce(headline: str, detail: str = "") -> str:
    """Push a message to the operator's phone. THIS IS A NOTIFICATION, NOT A GATE.

    ⚠️ NOBODY IS WAITING TO ANSWER. This session is headless, started by cron.
    Calling this sends a message and returns immediately; it does not and cannot
    block for a reply, and there is no reply channel. So announce and then
    PROCEED — do not wait, do not treat silence as approval, and do not treat
    silence as refusal. The operator's actual intervention is the kill switch,
    exercised after the fact on a position that is already open.

    Use it for the unusual, before you act on it:
      - abandoning the house view wholesale, not a single-name deviation. This
        is the one class nothing else can detect — it is a property of YOUR
        reasoning, invisible in any single order — so it is yours to announce.
      - anything a person reading the journal tomorrow would wish they had known
        today.
    Off-universe entries and positions crossing the announce line are pushed
    automatically by the order gate as the order goes out; you do not need to
    duplicate those, and duplicating them trains the operator to ignore the
    channel.

    Do NOT announce the routine: every fill is already pushed with its reason,
    and a buying-power deferral self-heals and is deliberately silent.

    Delivery is best-effort and unconfirmed. The message is handed to a detached
    sender so a slow or unreachable ntfy server cannot stall you; if no push
    topic is configured on this box it is a silent no-op. `sent: true` means the
    sender was started, never that a phone lit up. An unsent announcement is not
    an error to retry — say it in `record_decision` instead, which is durable.
    """
    text = str(headline or "").strip()
    if not text:
        return json.dumps({"ok": False, "error": "headline is empty — an "
                                                 "announcement with no content is noise"},
                          indent=2)
    body = str(detail or "").strip() or text

    # JOURNAL FIRST, PUSH SECOND. A push can fail -- a wedged ntfy server, an
    # unset topic, a header that will not encode (which silently killed SEVEN of
    # eight alert titles in this repo until 2026-08-12). If the announcement
    # exists only as a push, a failed one leaves NO trace and nobody can tell
    # afterwards whether the agent announced and the channel dropped it, or the
    # agent never announced at all. The journal entry is the durable record; the
    # push is best-effort delivery of it.
    try:
        store.append_journal({
            "event": "announcement",
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "headline": text, "detail": body, "source": "agent",
        })
        journalled = True
    except Exception:       # noqa: BLE001
        journalled = False

    # Plain blocking push, NOT push_detached. That helper forks, and this server
    # is threaded -- forking from a threaded process risks a child deadlocking on
    # a lock held by a thread that does not exist after the fork. It exists for
    # the ORDER-GATE hook, which is on a ~0.1s critical path and cannot afford a
    # 10s timeout. A tool call is not on that path: waiting is fine here, and the
    # fork hazard is not worth buying latency nobody needs.
    try:
        notify.push(f"AGENT: {text}", body, tags="loudspeaker")
        spawned = True
    except Exception:       # noqa: BLE001 — a failed alert must never end a session
        spawned = False
    return json.dumps({"ok": True, "sent": spawned,
                       "announced": {"headline": text, "detail": body},
                       "journalled": journalled,
                       "note": "NOTIFICATION ONLY — nothing waits for a reply; "
                               "proceed with your decision. The kill switch is "
                               "the operator's intervention, after the fact."},
                      indent=2)


def _dollar_volume(symbol: str) -> tuple[float | None, str | None]:
    """Trailing 20-day average dollar volume for `symbol`, or None if unmeasurable.

    Source: research_store/prices/pool_dvol.parquet (scripts/fetch_pool.py, built
    from Alpaca IEX volume -- a single venue, a fraction of the consolidated
    tape). Returns (value, as_of) where as_of is the ISO date of the file's last
    row, so a caller can report how stale the reading is. Returns (None, None)
    if the file is missing/unreadable; (None, as_of) if the file exists but the
    symbol isn't in it or has no data in the trailing window. Never raises --
    governance.liquidity_ok() fails CLOSED on None, which is the correct
    direction: a name we cannot measure has not been shown to be tradeable.
    """
    # MOOMOO TURNOVER FIRST -- it is the consolidated tape, which is what
    # min_dollar_volume_20d is actually calibrated against. The IEX fallback is
    # ONE venue and a fraction of the tape, so it reads genuinely liquid names as
    # illiquid: measured across the live universe it puts SOC, HTZ, ACHR and KEEL
    # at 0.1-0.2x a floor they comfortably clear on the real tape.
    #
    # The turnover panel is SHALLOW BY CONSTRUCTION -- it starts accumulating the
    # day it ships (fetch_prices appends it from the same unmetered snapshot),
    # so it will be short of 20 rows for weeks. A short window is used and
    # LABELLED, not silently padded or silently rejected.
    for path, source in ((REPO / "research_store" / "prices" / "turnover.parquet",
                          "moomoo consolidated tape"),
                         (REPO / "research_store" / "prices" / "pool_dvol.parquet",
                          "Alpaca IEX (ONE venue -- undercounts the tape)")):
        got = _dvol_from(path, symbol, source)
        if got is not None:
            return got
    return None, None


def _dvol_from(path, symbol: str, source: str):
    """One panel's trailing-20d mean. -> (value, as_of_note) or None to fall through.

    Returns None (fall through to the next source) when the file is absent or the
    symbol is not in it. Returns (None, note) when the symbol IS present but has
    no usable readings -- that is a measured absence, not a missing source, and
    liquidity_ok fails CLOSED on it, which is correct.
    """
    if not path.exists():
        return None
    try:
        d = pd.read_parquet(path)
        if symbol not in d.columns:
            return None
        as_of = d.index[-1].date().isoformat() if len(d.index) else None
        col = d[symbol].dropna().tail(20)
        if not len(col):
            return (None, as_of)
        # ⚠️ THE SOURCE AND THE DEPTH RIDE WITH THE NUMBER. The old form returned
        # a bare float, so a reading off ONE venue was indistinguishable from the
        # consolidated tape, and a 3-day mean from a 20-day one. The caller
        # reports this to a live agent sizing real orders.
        note = f"{as_of} | {source} | {len(col)}d mean"
        if len(col) < 20:
            note += " (SHORT WINDOW -- fewer than 20 sessions on record yet)"
        return (float(col.mean()), note)
    except Exception:
        return None


@mcp.tool()
def check_order(symbol: str, side: str, amount: float) -> str:
    """Ask whether an order is permitted BEFORE you place it.

    Returns JSON: {"allowed": bool, "reasons": [...], "liquidity_advisory": {...}}.
    Call this first, then place through the Robinhood MCP tools if allowed. It
    does not place anything itself. A refusal is a finding worth reporting,
    never something to work around.

    What can make `allowed` false (buys only -- see below): the kill switch
    (blocks everything, buy or sell), HALT-ENTRIES and the drawdown halt (block
    new buys only), the per-order size cap, and the universe whitelist.
    A SELL is NEVER refused by the drawdown halt, HALT-ENTRIES, the whitelist,
    or the order cap -- stops in this system are software-only, so refusing an
    exit would strand a position's only protection (only the kill switch, which
    stops ALL placement by design, can still show up in a sell's reasons).

    `liquidity_advisory` is reported SEPARATELY and NEVER makes `allowed` false.
    It is ADVISORY ONLY, pending task #30: research_store/prices/pool_dvol.parquet
    is built from Alpaca IEX volume (a single venue -- a fraction of consolidated
    dollar volume) and is currently weeks stale. Checked against the configured
    $50M floor as-is, roughly 30% of the curated 168-name universe reads below
    it -- almost certainly a data problem, not an illiquid universe, so this
    tool reports the finding (with the data's age) rather than hard-refusing on
    it. Read `liquidity_advisory.ok` and use judgment.

    This tool does NOT restrict which name you pick beyond the checks above --
    symbol selection is yours.
    """
    cfg = strat.load()
    v = marks.load()
    acct = float(v["account_value"]) if v and v.get("account_value") is not None else float("nan")
    sym = str(symbol).strip().upper()
    sd = str(side).strip().lower()
    reasons: list[str] = []

    g = gov.gates(acct, cfg)
    reasons += g["block_all"]
    if sd == "buy":
        reasons += g["block_entries"]

    approved, blocked = gov.vet_plan(
        [{"symbol": sym, "side": sd, "amount": float(amount)}], acct, cfg)
    reasons += [b["blocked"] for b in blocked]

    floor = float(cfg.get("governance", {}).get("min_dollar_volume_20d", 0.0))
    dvol, as_of = _dollar_volume(sym)
    liq_ok, liq_why = gov.liquidity_ok(sym, dvol, floor)
    # as_of is now a NOTE ("2026-08-12 | moomoo consolidated tape | 20d mean"),
    # not a bare date -- the source and window depth have to reach the agent with
    # the number, or a one-venue 3-day mean reads identically to the consolidated
    # 20-day one. Parse the date PREFIX for the age; a bare fromisoformat on the
    # whole string throws, and the existing except would have silently reported
    # age_days=None on every call.
    age_days = None
    if as_of:
        try:
            age_days = (datetime.now(timezone.utc).date()
                        - date.fromisoformat(str(as_of).split(" | ")[0])).days
        except Exception:
            age_days = None

    # Funding: ADVISORY, never a block. An underfunded buy is rejected by the
    # broker, so it wastes a placement rather than endangering anything — and
    # blocking on a possibly-stale snapshot could freeze trading on bad data.
    # Size against `buying_power`, never `cash`: it is what Robinhood checks an
    # order against. Under the old CASH account those two could differ by most of
    # the balance (2026-08-10: cash $9.20, buying_power $2.14) because proceeds
    # sat unsettled for T+1. The account moved to LIMITED MARGIN on 2026-08-18,
    # so proceeds are spendable the same session and the gap is normally zero --
    # buying_power remains the authority either way, which is why this reads it
    # rather than inferring spendability from `cash`.
    funding = None
    if sd == "buy":
        bp = v.get("buying_power") if v else None
        if bp is None:
            funding = {"known": False,
                       "note": "this snapshot carries no buying_power. `cash` is "
                               "NOT spendable — call Robinhood get_portfolio for "
                               "the live figure before sizing this buy."}
        else:
            bp = float(bp)
            funding = {"known": True, "buying_power": bp,
                       "amount": float(amount),
                       "affordable": float(amount) <= bp,
                       "unsettled_funds": v.get("unsettled_funds"),
                       "note": ("fits within buying power" if float(amount) <= bp else
                                f"${float(amount):.2f} exceeds buying power ${bp:.2f} "
                                f"— Robinhood will reject this.")}

    liquidity_advisory = {
        "ok": liq_ok,
        "dollar_volume_20d": dvol,
        "floor": floor,
        "as_of": as_of,
        "age_days": age_days,
        "reason": liq_why or None,
        "advisory_only": True,
        "why_advisory": (
            "pool_dvol.parquet is single-venue (Alpaca IEX) volume and is "
            "currently weeks stale -- ~30% of the curated universe reads below "
            "the floor on it. Tracked as task #30. This never blocks the order."
        ),
    }

    out = {"allowed": not reasons, "symbol": sym, "side": sd,
           "amount": float(amount), "reasons": reasons,
           "liquidity_advisory": liquidity_advisory}
    if funding is not None:
        out["funding"] = funding
    return json.dumps(out, indent=2)


def _symlist(symbols) -> list:
    """Accept 'NVDA,MU', 'NVDA MU', ['NVDA','MU'] -- agents pass all three."""
    if symbols is None:
        return []
    if isinstance(symbols, str):
        return [s for s in symbols.replace(",", " ").split() if s]
    return [str(s).strip() for s in symbols if str(s).strip()]


@mcp.tool()
def quote(symbols: str) -> str:
    """The LIVE price of any US symbol, right now, straight from moomoo.

    Accepts one symbol or many ('NVDA' or 'NVDA,MU,SPY') -- up to 400 in a single
    unmetered call, so asking for the whole universe costs the same as asking for
    one. Works for names you do NOT hold and names outside the universe: this is
    how you price a candidate before deciding anything.

    Returns last, open, prev_close, day high/low, change_pct, volume, turnover,
    52-week high/low and pct_below_52w_high, plus `update_time` so you can see how
    fresh the tick is. Outside market hours the last trade is the previous
    session's close.

    If the feed is down you get an `error` -- never a stale price dressed up as a
    live one. A symbol moomoo does not recognise lands in `unavailable` with a
    reason and does NOT prevent the others from returning.
    """
    return json.dumps(live_mod.quotes(_symlist(symbols)), indent=2, default=str)


@mcp.tool()
def earnings(symbols: str = "", weeks: int = 6) -> str:
    """When these names next report, from moomoo's live US earnings calendar.

    Pass symbols ('NVDA,MU') or leave blank to cover what you currently hold.
    Returns the date, `days_until`, and whether the report lands BEFORE the open
    or AFTER the close -- an AFTER report means the move happens overnight, while
    the stop watcher is not running.

    `none_scheduled` means nothing falls inside the weeks scanned, NOT that no
    report is coming. ETFs never appear.

    These are dates, not instructions. Nothing here blocks a trade or closes a
    position; what to do about a report is your call.
    """
    syms = _symlist(symbols)
    if not syms:
        v = marks.load() or {}
        syms = sorted(state.holdings(v, [], _overrides()).keys())
        if not syms:
            return json.dumps({"earnings": {},
                               "note": "no symbols given and no positions held"},
                              indent=2)
    return json.dumps(live_mod.earnings(syms, weeks=weeks), indent=2, default=str)


@mcp.tool()
def depth(symbol: str, levels: int = 5) -> str:
    """The live bid/ask ladder for one name — can you actually get filled here?

    `quote` tells you how much trades over a day; this tells you what is resting
    at the touch right now and what crossing the spread costs. Use it before
    committing size to a thinner name, and to sanity-check a fill you did not
    like.

    Outside regular hours the ladder is usually empty — that is the session, not
    illiquidity, and the reply says so.
    """
    return json.dumps(live_mod.depth(symbol, levels), indent=2, default=str)


@mcp.tool()
def leaders(period: str = "20d", n: int = 25, worst: bool = False) -> str:
    """What is actually moving in the whole US market — beyond our 168 names.

    Periods: 5d, 10d, 20d, 60d, 120d, 250d, ytd. `worst=True` gives the weakest.

    This exists so the configured universe is a starting point, not a cage: it
    looks at ~6,900 names, most of which we hold no history for. A name found
    here is a candidate to investigate, not a signal — we have no deep panel for
    it, so `terrain` and `candidates` cannot score it. Say so if you act on one.
    """
    return json.dumps(live_mod.leaders(period=period, n=n, worst=worst),
                      indent=2, default=str)


@mcp.tool()
def sectors(symbols: str) -> str:
    """Real industry classification per name, from moomoo's plate data.

    Useful for seeing concentration the position count hides — five names in one
    industry is one bet wearing five hats. ETFs have no industry plate and come
    back under `unsupported`; that is expected, not a failure.
    """
    return json.dumps(live_mod.sectors(_symlist(symbols)), indent=2, default=str)


@mcp.tool()
def macro_calendar(days: int = 7, importance: str = "HIGH") -> str:
    """Scheduled US macro events — FOMC, CPI, payrolls — in the next `days`.

    The macro sibling of `earnings`: these hit the whole book at once rather than
    one name. `importance` is HIGH / MEDIUM / LOW / ALL.

    Dates only. Nothing here blocks a trade or sizes one; what to do about a CPI
    print two days out is your call.
    """
    return json.dumps(live_mod.macro_calendar(days=days, importance=importance),
                      indent=2, default=str)


# A stop below this fraction of the live price cannot plausibly be reached, so it
# is decoration rather than protection. 0.50 is deliberately permissive -- it
# rejects only the degenerate case (a stop at a cent, or half the price away),
# never a legitimately wide stop on a volatile name. The point is to stop the
# `watched` flag being satisfiable by a level nobody would ever act on.
MIN_STOP_FRACTION = 0.50

OVERRIDES = REPO / "research_store" / "monitor" / "overrides.json"
WAKES = REPO / "research_store" / "monitor" / "wakes.json"


def _overrides() -> dict:
    try:
        return json.loads(OVERRIDES.read_text())
    except Exception:                       # noqa: BLE001 -- absent or torn
        return {}


@mcp.tool()
def performance(limit: int = 30) -> str:
    """Your own track record — the equity curve and every closed round trip.

    Answers the question none of the other tools do: is what I am doing working?
    `positions` shows unrealized P&L on what is open; `mandate_status` judges
    against the terms. This is the history: where equity started, where it is,
    its high-water mark, the drawdown from it, and each completed trade with its
    return, holding period and how it ended (stop / target / rebalanced).

    `win_rate` is reported ONLY alongside average win and average loss. A win
    rate on its own is the most misleading number in trading — 78% winners lost
    money in this system's own backtest, because the losers were larger.

    `win_rate` and every figure in `closed_trades_summary` cover FULL closes
    only; trims live in `partial_closes` and are never blended in — a 1/3 sale at
    +6.5% is a sizing decision, not a round trip, and averaging the two produces
    a number that means nothing.
    """
    pts = []
    if EQUITY.exists():
        for line in EQUITY.read_text().splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            v = r.get("value")
            if isinstance(v, (int, float)) and v == v:
                pts.append({"date": r.get("date"), "value": float(v)})

    curve = {}
    if pts:
        vals = [p["value"] for p in pts]
        peak = max(vals)
        curve = {
            "points": len(pts),
            "from": pts[0]["date"], "to": pts[-1]["date"],
            "start": vals[0], "current": vals[-1],
            "high_water": peak,
            "return_pct": round((vals[-1] / vals[0] - 1) * 100, 2) if vals[0] else None,
            "drawdown_from_peak_pct": round((vals[-1] / peak - 1) * 100, 2) if peak else None,
        }

    closed = []
    for rec in _outcomes():
        if rec.get("event") != "outcome":
            continue
        o = rec.get("outcome") or {}
        closed.append({
            "symbol": rec.get("symbol"), "at": rec.get("at"),
            "pnl_pct": o.get("pnl_pct"), "holding_days": o.get("holding_days"),
            "exit_reason": o.get("exit_reason"),
            "hit_stop": o.get("hit_stop"), "hit_target": o.get("hit_target"),
        })
    closed.sort(key=lambda r: str(r.get("at") or ""), reverse=True)

    rs = [c["pnl_pct"] for c in closed
          if isinstance(c.get("pnl_pct"), (int, float))]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    summary = {"closed_trades": len(rs)}
    if rs:
        summary.update({
            "win_rate_pct": round(len(wins) / len(rs) * 100, 1),
            "avg_win_pct": round(sum(wins) / len(wins) * 100, 2) if wins else None,
            "avg_loss_pct": round(sum(losses) / len(losses) * 100, 2) if losses else None,
            "avg_trade_pct": round(sum(rs) / len(rs) * 100, 2),
            "stopped_out": sum(1 for c in closed if c.get("hit_stop")),
            "hit_target": sum(1 for c in closed if c.get("hit_target")),
        })

    # PARTIAL closes (trims) — reported SEPARATELY and never folded into the
    # figures above. A trim de-risks a position that is still open, so it has no
    # holding period, no stop/target verdict, and no claim on win_rate; blending
    # it in would let a 1/3 sale rewrite the record of a full round trip.
    parts = []
    for rec in _outcomes():
        if rec.get("event") != "partial_outcome":
            continue
        pnl, frac = rec.get("pnl_pct"), rec.get("fraction")
        if not isinstance(pnl, (int, float)) or not isinstance(frac, (int, float)):
            continue
        parts.append({"symbol": rec.get("symbol"), "pnl_pct": float(pnl),
                      "fraction": float(frac)})
    partials = {"count": len(parts)}
    if parts:
        prs = [p["pnl_pct"] for p in parts]
        wsum = sum(p["fraction"] for p in parts)
        partials.update({
            "avg_pnl_pct": round(sum(prs) / len(prs) * 100, 2),
            "best_pnl_pct": round(max(prs) * 100, 2),
            "worst_pnl_pct": round(min(prs) * 100, 2),
            # each trim weighted by how much of the position it sold: three 10%
            # nibbles are not the same evidence as one 50% cut.
            "capital_weighted_pnl_pct": round(
                sum(p["pnl_pct"] * p["fraction"] for p in parts) / wsum * 100, 2
            ) if wsum else None,
        })

    return json.dumps({
        "equity_curve": curve or {"note": "no equity history recorded yet"},
        "closed_trades_summary": summary,
        "partial_closes": partials,
        "recent_closed_trades": closed[:int(limit)],
        "note": "Unrealized P&L on open positions is in positions(); this is the "
                "realized record. Read win_rate only next to avg_win/avg_loss. "
                "closed_trades_summary is FULL closes only — trims are counted "
                "separately in partial_closes and are not in win_rate.",
    }, indent=2, default=str)


@mcp.tool()
def halt_status() -> str:
    """Are the switches on? Read this BEFORE planning orders.

    Three independent controls, and they are not the same thing:
      - kill switch (`research_store/HALT`): nothing is placed, not even a sell.
        Exits must be done by hand. The monitor keeps WATCHING and alerting.
      - halt-entries (`research_store/HALT_ENTRIES`): new buys blocked, exits
        still go through.
      - live_approved: the master switch in config. Off means alert-only.

    An order that violates these is refused at placement, so without this tool
    you would plan, be denied, and have no way to learn why.
    """
    cfg = strat.load()
    kill = gov.kill_switch_active(cfg)
    entries = gov.halt_entries_active(cfg)
    live = gov.live_approved(cfg)
    return json.dumps({
        "kill_switch": kill,
        "halt_entries": entries,
        "live_approved": live,
        "can_buy": bool(live and not kill and not entries),
        "can_sell": bool(live and not kill),
        "meaning": ("kill switch: nothing places, exits by hand. halt_entries: "
                    "buys blocked, exits still place. live_approved off: "
                    "alert-only, nothing places."),
    }, indent=2)


@mcp.tool()
def macro() -> str:
    """Macro regime facts: VIX, the 10y-2y curve, and high-yield spreads (FRED).

    Deep history, updated daily — VIX here is the INDEX close, not an intraday
    print. moomoo does not carry it (checked: no volatility index among its 24 US
    macro indicators, and VIXY/UVXY are decaying futures ETFs, not the index).

    These are observations about conditions, not switches. Nothing here turns
    trading on or off.
    """
    try:
        from adapters.fred import indicators as fred   # noqa: PLC0415
        snap = fred.snapshot()
    except Exception as e:                  # noqa: BLE001
        return json.dumps({"error": f"FRED unavailable ({type(e).__name__}: {e})",
                           "note": "no cached value is being substituted"}, indent=2)
    return json.dumps({"source": "FRED (daily close, deep history)",
                       "indicators": snap,
                       "note": "facts about conditions, not a trading switch"},
                      indent=2, default=str)


@mcp.tool()
def news(symbols: str = "", limit: int = 20) -> str:
    """Recent symbol-tagged headlines (Alpaca). Blank symbols = what you hold.

    Context for judgment, not a signal. A headline explains a move you already
    see; it does not by itself justify an entry.
    """
    syms = _symlist(symbols)
    if not syms:
        v = marks.load() or {}
        syms = sorted(state.holdings(v, [], _overrides()).keys())
    if not syms:
        return json.dumps({"news": [], "note": "no symbols and nothing held"},
                          indent=2)
    try:
        from adapters.alpaca import news as anews      # noqa: PLC0415
        items = anews.get_news(syms, limit=int(limit))
    except Exception as e:                  # noqa: BLE001
        return json.dumps({"error": f"Alpaca news unavailable "
                                    f"({type(e).__name__}: {e})"}, indent=2)
    return json.dumps({"symbols": syms, "news": items}, indent=2, default=str)


# ---- memory: what past sessions concluded --------------------------------

@mcp.tool()
def research_log(limit: int = 25) -> str:
    """What past sessions decided and why, plus current rule-outs and questions.

    Read this early. Sessions are separate processes with separate contexts, so
    without it you re-derive the same conclusions and re-litigate the same
    rejections. It is YOUR past reasoning, not market fact — supersede it freely
    with a stated reason.
    """
    return json.dumps(memory.research_log(REPO / "research_store", JOURNAL, limit),
                      indent=2, default=str)


@mcp.tool()
def rule_out(symbol: str, reason: str, until: str = "") -> str:
    """Record that you decided against this name, and why. ⛔ THIS BLOCKS BUYS.

    Not a fact about the name — your own decision. But it BINDS: the 10:00 fast
    loop drops the name from its order plan and the order gate refuses the buy,
    until `revisit` clears it. That is the point. The loop is deterministic and
    knows nothing about why you sold; unrecorded, it rebuys the name the next
    morning — which is exactly what happened to AMAT three days running.

    It never blocks a sell, a trim or an exit. Buys only.

    If you sell a name for a reason that should still hold tomorrow, call this.

    `until`: ISO date (YYYY-MM-DD) for a reason with a known expiry — "flat into
    earnings on the 13th" should stop binding afterwards. Leave it empty and the
    rule-out holds until revisited.
    """
    return json.dumps(memory.rule_out(REPO / "research_store", symbol, reason,
                                      until=(until.strip() or None)),
                      indent=2, default=str)


@mcp.tool()
def revisit(symbol: str, reason: str) -> str:
    """Supersede an earlier rule-out. Appends — the original is never deleted."""
    return json.dumps(memory.revisit(REPO / "research_store", symbol, reason),
                      indent=2, default=str)


@mcp.tool()
def open_question(question: str) -> str:
    """Record something worth resolving that this session could not settle."""
    return json.dumps(memory.open_question(REPO / "research_store", question),
                      indent=2, default=str)


@mcp.tool()
def close_question(question: str, answer: str) -> str:
    """Answer an open question. The original entry stays in the record."""
    return json.dumps(memory.close_question(REPO / "research_store", question,
                                            answer), indent=2, default=str)


# ---- wakes: asking to be woken -------------------------------------------

@mcp.tool()
def wake_register(symbol: str, direction: str, level: float, reason: str,
                  budget: int = 3, ttl_days: int = 5) -> str:
    """Ask to be woken with a full session when `symbol` trades above/below
    `level`. `direction` is "above" or "below".

    ⚠️ A WAKE IS NOT A STOP. It starts a session so you can decide; it places no
    order and protects nothing. If what you want is "get me out at X", that is
    `set_levels` — the monitor enforces those without waking anything.

    Bounded on purpose: `budget` caps how many times it may ever fire so a
    flapping price cannot spawn endless sessions, and it expires after
    `ttl_days`.
    """
    return json.dumps(wakes.register(WAKES, symbol, direction, level, reason,
                                     budget=budget, ttl_days=ttl_days),
                      indent=2, default=str)


@mcp.tool()
def wake_status() -> str:
    """Your active wakes, with expired ones listed rather than silently dropped."""
    return json.dumps(wakes.status(WAKES), indent=2, default=str)


@mcp.tool()
def wake_deregister(key: str) -> str:
    """Cancel a wake by its key (from wake_status)."""
    return json.dumps(wakes.deregister(WAKES, key), indent=2, default=str)


@mcp.tool()
def brief() -> str:
    """Everything you need to decide, assembled fresh: mandate status, what you
    hold and whether it is protected, the top candidates, and the market backdrop.

    These are FACTS, not instructions. The regime line is an observation, not a
    switch — nothing here decides for you. Pull `terrain(symbol)` before setting
    levels, and `universe()` if the top candidates do not suit.
    """
    prod = read_current()
    v = marks.load() or {}
    panel = _panel()
    asof = panel.index[-1]

    # THE REGIME IS REPORTED, NEVER ENFORCED. It used to gate the slow loop's
    # selection -- first by emptying it (which sold eleven positions in one
    # minute on 2026-07-27) and then by refusing new entries. Both are gone as
    # of 2026-08-14, which is only correct if the FACT still reaches you.
    #
    # Two halves, and both are here on purpose: the live SPY reading computed
    # from the panel right now, and the compound call the slow loop recorded --
    # which includes VIX, the half that reporting only `spy_above_50dma` left
    # you to reconstruct from macro() yourself.
    regime = None
    if "SPY" in panel.columns:
        try:
            regime = {
                "spy_above_50dma": bool(momentum.regime_on(panel["SPY"], asof)),
                "note": "an observation about the market, not a rule that acts",
            }
        except Exception:
            regime = None
    recorded = (prod.regime if prod else None) or {}
    if recorded:
        regime = {**(regime or {}),
                  "recorded": {**recorded, "as_of": prod.as_of},
                  "note": "an observation about the market, not a rule that acts. "
                          "`recorded` is the compound call (trend AND vix) as of "
                          "the last book build; the spy_above_50dma line above is "
                          "computed live. Nothing acts on either — what a regime "
                          "call means for this book is your judgement."}

    held = state.holdings(v, prod.theses if prod else [], _overrides())
    top = screen.rank(panel, asof, _all_tickers()).head(10).round(4)

    # Degrade mandate_status gracefully when no snapshot exists yet
    try:
        mandate_obj = json.loads(mandate_status())
    except (TypeError, KeyError):
        # marks.load() returned None/empty -> snapshot unavailable
        mandate_obj = {}

    return json.dumps({
        "as_of": str(asof.date()),
        "book_as_of": prod.as_of if prod else None,
        "account": {k: v.get(k) for k in ("account_value", "cash", "invested")},
        "mandate": mandate_obj,
        "positions": held,
        "snapshot_freshness": _staleness(v),
        "unprotected": [s for s, h in held.items() if not h["watched"]],
        "candidates": json.loads(top.to_json(orient="index")),
        "regime": regime,
        "available": "candidates() shows 10 of ~168; universe() shows all. "
                     "terrain(symbol) gives measured excursions for any name.",
    }, indent=2, default=str)


def _selftest() -> None:
    assert ping() == "pong"
    assert mcp is not None
    # FIX 2: no snapshot on disk (marks.load() -> None) must not crash the tools.
    # ⛔ Redirect `OVERRIDES` (this module's own module-level path, distinct
    # from decide.OVERRIDES -- positions()'s `_overrides()` reads THIS one
    # directly) to an empty scratch file for this block, same discipline
    # every other selftest block in this file already applies to
    # decide.OVERRIDES. Without it this assertion depends on the LIVE
    # research_store/monitor/overrides.json being empty -- true in a quiet
    # repo, false the moment a real session has set a real level, which is
    # exactly the state a live trading system is in most of the time.
    orig_load = marks.load
    marks.load = lambda: None
    import tempfile as _tf0                                       # noqa: PLC0415
    orig_overrides_path = globals()["OVERRIDES"]
    _td0 = _tf0.TemporaryDirectory()
    globals()["OVERRIDES"] = Path(_td0.name) / "overrides.json"
    try:
        p = json.loads(positions())
        assert p == {}, p
        a = json.loads(account())
        # Every FIGURE is null with no snapshot -- nothing is invented.
        assert all(a[k] is None for k in
                   ("account_number", "account_value", "cash", "invested",
                    "as_of", "marked_at", "buying_power", "unsettled_funds")), a
        # ...and `spendable` must still WARN rather than go quiet: silence here
        # would read as "size against whatever `cash` says", when the figure an
        # order is actually checked against is missing from this snapshot.
        assert "UNKNOWN" in a["spendable"] and "get_portfolio" in a["spendable"], a
        # brief() must also degrade gracefully without a snapshot: return valid JSON
        # with account block carrying nulls, empty positions, degraded mandate,
        # and still-valid candidates/regime (those don't depend on the snapshot).
        b = json.loads(brief())
        assert isinstance(b, dict), b
        assert "account" in b, b
        assert b["account"]["account_value"] is None, b
        assert b["account"]["cash"] is None, b
        assert b["account"]["invested"] is None, b
        assert b["positions"] == {}, b
        assert isinstance(b["candidates"], dict), b
        assert "regime" in b, b  # still present, not skipped
        assert b["mandate"] == {}, b  # empty when no snapshot
    finally:
        marks.load = orig_load
        globals()["OVERRIDES"] = orig_overrides_path
        _td0.cleanup()
    print("selftest OK: mcp server boots, ping responds, degrades to JSON without a snapshot")

    # ---- a STALE snapshot must announce itself ----------------------------
    # Nothing used to say this: marks.py acknowledged "possibly stale" in a
    # comment and surfaced it nowhere, so a session whose snapshot writer had
    # failed planned against YESTERDAY'S holdings with placement allowed. Not
    # zero orders -- confident wrong ones, against positions possibly sold.
    #
    # Redirects `OVERRIDES` (this module's own path -- see the FIX 2 block
    # above) for the whole section: it calls positions() several times below,
    # including one asserting a bare {} result, which depends on the LIVE
    # overrides.json being empty unless redirected.
    import tempfile as _tf1                                       # noqa: PLC0415
    orig_overrides_path1 = globals()["OVERRIDES"]
    _td1_holder = _tf1.TemporaryDirectory()
    globals()["OVERRIDES"] = Path(_td1_holder.name) / "overrides.json"
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    import numpy as _np2
    fresh = (_dt.now(_tz.utc) - _td(hours=2)).isoformat()
    old_ts = (_dt.now(_tz.utc) - _td(days=9)).isoformat()      # >1 trading day, always

    assert _staleness({"ts": fresh}) is None, "a fresh snapshot must be silent"
    st = _staleness({"ts": old_ts})
    assert st and st["stale"] is True and "trading days" in st["age"], st

    # ⚠️ MEASURED IN TRADING DAYS, NOT HOURS. The writers stop at the close by
    # design, so a 09:00 premarket session legitimately sees a 17h-old snapshot
    # and a MONDAY premarket a 65h-old one -- both the freshest data that
    # exists. An hours threshold fired on every premarket run.
    def _ago(days):        # a timestamp exactly N calendar days back
        return (_dt.now(_tz.utc) - _td(days=days)).isoformat()
    for back in range(1, 5):
        gap = int(_np2.busday_count((_dt.now(_tz.utc) - _td(days=back)).date(),
                                    _dt.now(_tz.utc).date()))
        got = _staleness({"ts": _ago(back)})
        if gap <= 1:
            assert got is None, f"{back}d back = {gap} trading days, must be silent"
        else:
            assert got and got["stale"] is True, (back, gap, got)
    assert "TRADING DAYS OLD" in st["warning"] and "get_equity_positions" in st["warning"], st
    # no timestamp at all, and an unparseable one, are BOTH stale -- never "fine"
    assert _staleness({})["stale"] is True          # a snapshot with no ts IS stale
    # ...but NO snapshot at all is a different, already-handled state: the
    # holdings tool must still degrade to a bare {} rather than a scary banner
    _no_snap = marks.load
    try:
        marks.load = lambda *a, **k: None
        assert json.loads(positions()) == {}, "no snapshot must stay a bare {}"
    finally:
        marks.load = _no_snap
    assert _staleness({"ts": "not-a-date"})["stale"] is True
    # A bare date parses as midnight, and under the TRADING-DAY rule yesterday's
    # date is normal, not stale -- that is the premarket case. (It read as stale
    # under the old hours rule, which is exactly the false alarm being removed.)
    y = (_dt.now(_tz.utc) - _td(days=1)).date().isoformat()
    if int(_np2.busday_count(_dt.fromisoformat(y).date(), _dt.now(_tz.utc).date())) <= 1:
        assert _staleness({"as_of": y}) is None, y
    week = (_dt.now(_tz.utc) - _td(days=8)).date().isoformat()
    assert _staleness({"as_of": week})["stale"] is True, week
    # the banner reaches BOTH tools, not just account()
    _real_load = marks.load
    try:
        marks.load = lambda *a, **k: {"ts": old_ts, "marked_at": fresh, "positions": {},
                                      "account_value": 1.0, "cash": 1.0}
        assert "STALE" in json.loads(account()), "account() must carry the banner"
        assert "STALE" in json.loads(positions()), "positions() must carry it too"
        marks.load = lambda *a, **k: {"ts": fresh, "positions": {},
                                      "account_value": 1.0, "cash": 1.0}
        assert "STALE" not in json.loads(account()), "fresh must not cry wolf"
        assert "STALE" not in json.loads(positions()), "fresh must not cry wolf"
    finally:
        marks.load = _real_load
        globals()["OVERRIDES"] = orig_overrides_path1
        _td1_holder.cleanup()
    print("selftest OK: a stale/undateable snapshot announces itself in BOTH "
          "account() and positions(); a fresh one stays silent")

    # ---- Part 4: a level can outlive its position --------------------------
    # Levels no longer expire (2026-08-17): an override the agent forgot to
    # clear stays on file after the position closes, and WAKES UP if the name
    # re-enters the book later, at a price it was never written for. A
    # cleared level is only reliable if the agent can see what it left
    # behind, so positions() must surface any override whose symbol is not
    # currently held -- under its own key, distinct from a held position's
    # own record (there is no held record to fold an orphaned one into).
    # Redirect via `_overrides` itself (never OVERRIDES/the live file) --
    # same pattern as read_current elsewhere in this selftest.
    orig_overrides_fn = globals()["_overrides"]
    _real_load3 = marks.load
    _fresh_ts = _dt.now(_tz.utc).isoformat()
    try:
        marks.load = lambda *a, **k: {
            "account_value": 1000.0, "ts": _fresh_ts,
            "positions": {"NVDA": {"qty": 10, "avg_cost": 100.0, "mark": 110.0}}}
        # NVDA is held; ZOMBIE is not -- an override left behind after an exit.
        globals()["_overrides"] = lambda: {
            "NVDA": {"stop": 105.0, "reason": "held, tightened"},
            "ZOMBIE": {"stop": 50.0, "reason": "stale from a previous holding"}}
        p = json.loads(positions())
        assert "levels_without_positions" in p, p
        assert set(p["levels_without_positions"]) == {"ZOMBIE"}, p
        assert p["levels_without_positions"]["ZOMBIE"]["reason"] == \
            "stale from a previous holding", p
        assert "NVDA" not in p["levels_without_positions"], \
            "a held symbol's override belongs in its own record, not here"

        # every override corresponds to a held name -> the key is ABSENT, not
        # present-and-empty: an agent scanning for it should not have to
        # distinguish "nothing to see" from "the field always exists".
        globals()["_overrides"] = lambda: {"NVDA": {"stop": 105.0, "reason": "held"}}
        p2 = json.loads(positions())
        assert "levels_without_positions" not in p2, p2
    finally:
        globals()["_overrides"] = orig_overrides_fn
        marks.load = _real_load3
    print("selftest OK: positions() surfaces a level held for a name that is not "
          "(levels_without_positions: symbol -> stored reason), and omits the "
          "key entirely when every override matches a held name")

    # ---- I4 (final review): a displayed stop the monitor will REFUSE must not
    # read as protection. state.holdings() reports `stop` as the agent's own
    # override whenever one exists, with no regard for whether apply_overrides()
    # will actually apply it -- and positions() feeds that same value straight
    # into excursion.facts()'s gain_protected_pct. An override that RAISES the
    # thesis stop but has no known live price (the monitor's own quotes.json
    # carries no entry for this symbol) is exactly what apply_overrides()
    # refuses fail-closed -- so the displayed stop, and any gain_protected_pct
    # computed from it, would be confidently wrong. positions() must flag it.
    import types
    import tempfile
    orig_overrides_fn2 = globals()["_overrides"]
    orig_read_current2 = globals()["read_current"]
    _real_load4 = marks.load
    orig_rh_positions2 = decide.RH_POSITIONS
    orig_quotes2 = decide.QUOTES
    with tempfile.TemporaryDirectory() as td:
        decide.RH_POSITIONS = Path(td) / "positions.json"
        decide.QUOTES = Path(td) / "quotes.json"          # absent -> load_price() is None
        try:
            decide.RH_POSITIONS.write_text(json.dumps({"positions": {"NVDA": {"qty": 10}}}))
            marks.load = lambda *a, **k: {
                "account_value": 1000.0, "ts": _fresh_ts,
                "positions": {"NVDA": {"qty": 10, "avg_cost": 100.0, "mark": 110.0}}}
            th_i4 = types.SimpleNamespace(symbol="NVDA", stop=100.0, targets=[130.0],
                                          target_weight=0.1, verdict="buy")
            globals()["read_current"] = lambda: types.SimpleNamespace(
                as_of="t", theses=[th_i4])
            # the agent's own override RAISES the thesis stop (100 -> 108) but
            # the monitor has no live quote for NVDA at all.
            globals()["_overrides"] = lambda: {
                "NVDA": {"stop": 108.0, "targets": [], "reason": "tightened"}}
            p = json.loads(positions())
            assert p["NVDA"]["stop"] == 108.0, p            # still the displayed value
            assert p["NVDA"]["stop_enforced"] is False, p
            assert "no live price is currently known" in p["NVDA"]["stop_enforcement_note"], p

            # the counter-case: same override, but the monitor DOES have a
            # live quote below the new stop -- must read as enforced, not
            # flagged, or the note would cry wolf on every protected position.
            decide.QUOTES.write_text(json.dumps({"prices": {"NVDA": 150.0}}))
            p2 = json.loads(positions())
            assert p2["NVDA"]["stop_enforced"] is True, p2
            assert "stop_enforcement_note" not in p2["NVDA"], p2
        finally:
            globals()["_overrides"] = orig_overrides_fn2
            globals()["read_current"] = orig_read_current2
            marks.load = _real_load4
            decide.RH_POSITIONS = orig_rh_positions2
            decide.QUOTES = orig_quotes2
    print("selftest OK: positions() flags a displayed stop the monitor's own "
          "price guard will refuse (stop_enforced: false), and stays silent "
          "when the monitor would actually apply it")

    # ---- liquidity: the SOURCE must ride with the number -------------------
    # The floor is $50M of CONSOLIDATED volume, but the only reading was Alpaca
    # IEX -- one venue, a fraction of the tape. Measured live, it put SOC, HTZ,
    # ACHR and KEEL at 0.1-0.2x a floor they clear on the real tape, and the
    # agent was told a bare number with no way to know which tape it came from.
    import tempfile as _tf2
    with _tf2.TemporaryDirectory() as _d2:
        good = Path(_d2) / "t.parquet"
        idx = pd.date_range("2026-08-01", periods=25, freq="D")
        pd.DataFrame({"MU": [100.0] * 25}, index=idx).to_parquet(good)

        val, note = _dvol_from(good, "MU", "moomoo consolidated tape")
        assert val == 100.0, val
        assert "moomoo consolidated tape" in note and "20d mean" in note, note
        assert "SHORT WINDOW" not in note, note

        # a SHORT history is used and LABELLED -- not silently padded, and not
        # silently rejected. The panel starts empty the day it ships.
        short = Path(_d2) / "s.parquet"
        pd.DataFrame({"MU": [7.0, 9.0]}, index=idx[:2]).to_parquet(short)
        val, note = _dvol_from(short, "MU", "moomoo consolidated tape")
        assert val == 8.0, val
        assert "2d mean" in note and "SHORT WINDOW" in note, note

        # absent file / absent symbol FALL THROUGH (None) so the next source is
        # tried; a symbol that is present but empty is a measured absence and
        # returns (None, note) so liquidity_ok can fail closed on it
        assert _dvol_from(Path(_d2) / "nope.parquet", "MU", "x") is None
        assert _dvol_from(good, "NOTLISTED", "x") is None
        empty = Path(_d2) / "e.parquet"
        pd.DataFrame({"MU": [float("nan")] * 3}, index=idx[:3]).to_parquet(empty)
        got = _dvol_from(empty, "MU", "x")
        assert got is not None and got[0] is None, got

    # the note must remain age-parseable by check_order -- it splits on " | ",
    # and a bare fromisoformat on the whole string silently reported no age
    from datetime import date as _dd
    assert _dd.fromisoformat("2026-08-12 | src | 20d mean".split(" | ")[0]) == _dd(2026, 8, 12)
    print("selftest OK: dollar volume carries its SOURCE and window depth; short "
          "histories are labelled, absent sources fall through")

    # ---- THE REVIEWER'S IDENTITY MUST NEVER REACH THE AGENT ---------------
    # The journal records `reviewer: codex` for the OPERATOR's benefit. The
    # agent must not see it: told the source, a model reasons about the source
    # -- discounting a critic it decides misunderstands momentum, or deferring
    # to one it decides is objective. That failure is NOT symmetric. It hands
    # the agent an excuse to dismiss criticism, so the leak systematically
    # favours the party under review.
    #
    # Today no agent-facing tool surfaces codex_review events. That is
    # INCIDENTAL, not designed, and would silently stop being true the moment
    # someone widens research_log's event filter. This test fails if that
    # happens.
    import tempfile as _tf3
    _real_rj = store.read_journal
    try:
        store.read_journal = lambda: [
            {"event": "codex_review", "ts": "2026-08-12T15:29:54+00:00",
             "reviewer": "codex", "stance": "SPLIT",
             "headline": "Idle cash was not justified."},
            {"event": "agent_decision", "ts": "2026-08-12T14:38:56+00:00",
             "symbol": "MU", "action": "trim", "reason": "sector concentration"},
        ]
        for tool in (research_log, performance):
            try:
                out = str(tool(limit=50)).lower()
            except Exception:                       # noqa: BLE001
                continue
            # ⛔ IDENTITY WORDS ONLY. The hazard is the agent learning WHO
            # reviewed it -- told the source, it reasons about the source rather
            # than the argument. That the agent knows a review EXISTS is not the
            # hazard, and cannot be prevented anyway: on 2026-08-13 this tripped
            # on the agent's OWN prose ("the reviewer argued for halving on
            # 08-12"), written into its decision record while verdicts were
            # still being shown to it. research_log replays the agent's past
            # decisions, so its own words come back. Stripping them would mean
            # censoring the agent's record to keep a test green -- which buys
            # nothing, and costs the agent the reasoning it wrote down.
            #
            # A generic word left in this list makes the check fire on something
            # unpreventable, and a check that is permanently red catches nothing.
            for leak in ("codex", "openai", "gpt-"):
                assert leak not in out, (
                    f"{tool.__name__} leaks the reviewer's identity to the agent "
                    f"({leak!r}) -- see the comment above; strip the field or "
                    f"exclude codex_review from this tool")
    finally:
        store.read_journal = _real_rj
    print("selftest OK: no agent-facing tool leaks who the reviewer is")

    # ---- record_fills: what EXECUTED, not what was decided ----------------
    # On 2026-08-12 two real sells filled and nothing journalled them: the
    # legacy loop records fills with a shell script, and a session has no shell.
    # The weekly letter, reconcile_ledger and the outcome ledger all key on the
    # `execution` event, so the letter would have reported a week of no trading.
    _payload = json.dumps({"data": {"orders": [
        {"id": "abc", "symbol": "AMAT", "side": "sell", "state": "filled",
         "cumulative_quantity": "0.004646", "average_price": "547.0328",
         "created_at": "2026-08-12T14:38:14Z"},
        {"id": "def", "symbol": "TER", "side": "sell", "state": "cancelled",
         "cumulative_quantity": "0", "average_price": None},
    ]}})
    _journalled2 = []
    _real_ap, _real_rj2 = store.append_journal, store.read_journal
    global RH_POSITIONS
    _real_rhp = RH_POSITIONS
    try:
        import tempfile as _tf_fill
        _fill_dir = _tf_fill.TemporaryDirectory()
        RH_POSITIONS = Path(_fill_dir.name) / "positions.json"
        store.append_journal = lambda ev: _journalled2.append(ev)
        store.read_journal = lambda: []
        def _proof(response, *, cursor=None, exhausted=True):
            return json.dumps({"pages": [{"cursor": cursor, "response": response}],
                               "exhausted": exhausted})

        _bp = _proof({"positions": [{"symbol": "DELL", "quantity": "2",
                                      "average_buy_price": "100"}]})
        _port = json.dumps({"total_value": "250", "cash": "50",
                            "buying_power": "40"})
        r = json.loads(record_fills.fn(_payload, _bp, _port) if hasattr(record_fills, "fn")
                       else record_fills(_payload, _bp, _port))
        assert r["ok"] and r["recorded"] == 1, r          # cancelled is NOT a fill
        snap = json.loads(RH_POSITIONS.read_text())
        assert snap["positions"]["DELL"] == {"qty": 2.0, "avg_cost": 100.0}, snap
        assert snap["ts"], snap
        f = _journalled2[0]["fills"][0]
        assert f["symbol"] == "AMAT" and f["order_id"] == "abc", f
        assert abs(f["amount"] - 0.004646 * 547.0328) < 1e-6, f
        assert _journalled2[0]["event"] == "execution", _journalled2[0]

        # IDEMPOTENT: an order already on the record must never double-append,
        # or the letter counts the same trade twice and P&L is fabricated
        _journalled2.clear()
        store.read_journal = lambda: [{"event": "execution",
                                       "fills": [{"order_id": "abc"}]}]
        r2 = json.loads(record_fills.fn(_payload, _bp, _port) if hasattr(record_fills, "fn")
                        else record_fills(_payload, _bp, _port))
        assert r2["recorded"] == 0 and _journalled2 == [], r2

        # A fill is still journalled if reconciliation input is absent, but the
        # tool must return a loud failure rather than declaring completion.
        store.read_journal = lambda: []
        r3 = json.loads(record_fills.fn(_payload) if hasattr(record_fills, "fn")
                        else record_fills(_payload))
        assert r3["recorded"] == 1 and not r3["ok"] and r3["snapshot"]["ok"] is False, r3
        _journalled2.clear()

        # garbage in must not raise, and must not write
        for bad in ("not json", "[]", '{"nope": 1}'):
            rb = json.loads(record_fills.fn(bad) if hasattr(record_fills, "fn")
                            else record_fills(bad))
            assert rb.get("recorded", 0) == 0, (bad, rb)
        assert _journalled2 == []

        # ================================================================
        # ⛔ ADVERSARIAL: A BAD READ MUST NEVER REPLACE A GOOD SNAPSHOT.
        # The old writer COERCED instead of validating — it silently skipped
        # any row it could not parse, so a truncated response published a
        # partial book and deleted the rest, an empty list erased everything,
        # a missing cost became 0.0, and NaN went straight into the file. All
        # of it returned ok. Every case below is one that used to succeed.
        #
        # The invariant each asserts is the same: REJECT, and leave the
        # previous file BYTE-IDENTICAL. A partial book is worse than a stale
        # one — stale is detectable and was detected; partial looks current,
        # carries a fresh timestamp, and silently unprotects what it dropped.
        # ================================================================
        _refresh = (refresh_broker_snapshot.fn if hasattr(refresh_broker_snapshot, "fn")
                    else refresh_broker_snapshot)
        good_response = {"data": {"positions": [
            {"symbol": "DELL", "quantity": "2", "average_buy_price": "100"},
            {"symbol": "MU", "quantity": "3", "average_buy_price": "200"}]}}
        good_pos = _proof(good_response)
        good_port = json.dumps({"data": {"total_value": "500", "cash": "5",
                                         "account_number": "948184924"}})
        # a valid read with NO fills refreshes — the whole point of the new tool
        assert json.loads(_refresh(good_pos, good_port))["ok"] is True
        before = RH_POSITIONS.read_bytes()
        assert len(json.loads(before.decode())["positions"]) == 2

        def _must_reject(pos, port, why, **kw):
            decoded = json.loads(pos)
            proof = (pos if kw.pop("as_proof", False) or
                     (isinstance(decoded, dict) and "pages" in decoded)
                     else _proof(decoded))
            r = json.loads(_refresh(proof, port, **kw))
            assert r["ok"] is False, (why, r)
            assert RH_POSITIONS.read_bytes() == before, \
                f"{why}: the previous snapshot was modified"
            return r

        # EMPTY BOOK — the most destructive write there is. "No positions
        # returned" and "the read failed" are the same bytes, so it must be
        # asserted, never inferred.
        # A legacy bare response has well-formed rows but no evidence it is the
        # final page. It must never publish, regardless of whether it grew,
        # shrank, or happens to equal the previous book.
        r = _must_reject(json.dumps(good_response), good_port,
                         "bare page without completeness proof", as_proof=True)
        assert "completeness proof required" in r["error"], r
        # A well-formed partial page is still partial. `exhausted:true` cannot
        # overrule the page's own next link.
        partial = {"positions": good_response["data"]["positions"][:1],
                   "next": "https://broker.test/positions?cursor=page-2"}
        r = _must_reject(_proof(partial), good_port, "tail omitted", as_proof=True)
        assert "stops before exhaustion" in r["error"], r
        # Supplying another page is not enough: it must be the page named by
        # the prior next URL, and the transcript must start cursorless.
        bad_chain = json.dumps({"pages": [
            {"cursor": None, "response": partial},
            {"cursor": "wrong", "response": {"positions": [
                good_response["data"]["positions"][1]]}}], "exhausted": True})
        _must_reject(bad_chain, good_port, "broken cursor chain", as_proof=True)
        wrong_first = json.dumps({"pages": [
            {"cursor": "page-2", "response": {"positions":
                good_response["data"]["positions"]}}], "exhausted": True})
        _must_reject(wrong_first, good_port, "first page omitted", as_proof=True)
        # Nor does a fully linked transcript count until the terminal page says
        # there is no next page.
        not_asserted = json.dumps({"pages": [
            {"cursor": None, "response": {"positions":
                good_response["data"]["positions"]}}], "exhausted": False})
        _must_reject(not_asserted, good_port, "exhaustion not asserted", as_proof=True)
        # Positive multi-page control: cursor linkage plus a terminal page is
        # sufficient evidence, independent of how many holdings are present.
        complete = json.dumps({"pages": [
            {"cursor": None, "response": partial},
            {"cursor": "page-2", "response": {"positions": [
                good_response["data"]["positions"][1]]}}], "exhausted": True})
        assert json.loads(_refresh(complete, good_port))["ok"] is True
        assert len(json.loads(RH_POSITIONS.read_text())["positions"]) == 2
        RH_POSITIONS.write_bytes(before)
        r = _must_reject(json.dumps({"data": {"positions": []}}), good_port, "empty")
        assert "erase every holding" in r["error"], r
        # ...and IS allowed when the caller explicitly asserts a flat account
        assert json.loads(_refresh(_proof({"data": {"positions": []}}),
                                   good_port, liquidated=True))["ok"] is True
        RH_POSITIONS.write_bytes(before)          # restore for the rest

        # ONE malformed row rejects the WHOLE snapshot — it must not publish
        # the readable rows and drop the rest
        _must_reject(json.dumps({"data": {"positions": [
            {"symbol": "DELL", "quantity": "2", "average_buy_price": "100"},
            "not-a-row"]}}), good_port, "malformed row")
        for bad_row, why in (
                ({"symbol": "", "quantity": "1", "average_buy_price": "1"}, "no symbol"),
                ({"symbol": "X", "average_buy_price": "1"}, "no quantity"),
                ({"symbol": "X", "quantity": "1"}, "no avg cost"),
                ({"symbol": "X", "quantity": "-1", "average_buy_price": "1"}, "negative qty"),
                ({"symbol": "X", "quantity": "NaN", "average_buy_price": "1"}, "NaN qty"),
                ({"symbol": "X", "quantity": "Infinity", "average_buy_price": "1"}, "inf qty"),
                ({"symbol": "X", "quantity": "1", "average_buy_price": "NaN"}, "NaN cost")):
            _must_reject(json.dumps({"data": {"positions": [bad_row]}}), good_port, why)
        # duplicate symbols: the old code let the last row silently win
        _must_reject(json.dumps({"data": {"positions": [
            {"symbol": "DELL", "quantity": "2", "average_buy_price": "100"},
            {"symbol": "DELL", "quantity": "9", "average_buy_price": "1"}]}}),
            good_port, "duplicate symbol")
        # WRONG ACCOUNT — a confident, fresh-looking description of a book that
        # is not ours. CLAUDE.md hard rule 1: every other account is read-only.
        r = _must_reject(good_pos, json.dumps({"data": {
            "total_value": "500", "cash": "5", "account_number": "999999999"}}),
            "wrong account")
        assert "account mismatch" in r["error"], r
        # BLANK ACCOUNT — the guard's own undoing, and it actually happened on
        # 2026-08-17. _expected_account() reads the identity back OUT of the
        # snapshot, so a blank one published here makes `expected` blank on
        # every subsequent write and this check false FOREVER. Nothing repairs
        # it, because each write re-reads the blank it wrote. A known identity
        # may never be replaced by an unknown one.
        r = _must_reject(good_pos, json.dumps({"data": {
            "total_value": "500", "cash": "5"}}), "blank account")
        assert "no account number" in r["error"], r
        # ...and the guard must still be armed afterwards: the rejected write
        # must not have leaked a blank identity into the file it protects.
        assert _expected_account() == "948184924", _expected_account()
        # non-finite portfolio numbers
        for bad_port, why in (
                ({"total_value": "NaN", "cash": "5", "account_number": "948184924"}, "NaN total"),
                ({"total_value": "500", "cash": "Infinity", "account_number": "948184924"}, "inf cash")):
            _must_reject(good_pos, json.dumps({"data": bad_port}), why)
        # a genuinely CLOSED position (qty 0) is dropped without rejecting
        r = json.loads(_refresh(_proof({"data": {"positions": [
            {"symbol": "DELL", "quantity": "2", "average_buy_price": "100"},
            {"symbol": "GONE", "quantity": "0", "average_buy_price": "5"}]}}), good_port))
        assert r["ok"] is True and r["positions"] == 1, r
        assert "GONE" not in json.loads(RH_POSITIONS.read_text())["positions"]
        print("selftest OK: a bad broker read is REFUSED and the previous snapshot "
              "survives byte-identical (unproven/truncated pagination, empty, "
              "truncated-row, missing qty/cost, "
              "negative, NaN/inf, duplicate, wrong AND blank account); a "
              "no-fill refresh works")
    finally:
        store.append_journal, store.read_journal = _real_ap, _real_rj2
        RH_POSITIONS = _real_rhp
        _fill_dir.cleanup()
    print("selftest OK: record_fills journals FILLED orders, atomically refreshes "
          "positions, is idempotent, and fails loudly without broker state")

    # --- announce(): a NOTIFICATION, and it must never send during a test ----
    real_push = notify.push
    real_jrnl = store.append_journal
    sent, journalled = [], []
    notify.push = lambda *a, **k: bool(sent.append((a, k))) or True
    store.append_journal = lambda ev: journalled.append(ev)
    try:
        # the tool must NOT fork: this server is threaded, and push_detached
        # forks. A regression back to it would reintroduce that hazard silently,
        # so assert the fork helper is never reached.
        announce_mod.push_detached = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("announce() must not fork from the threaded server"))
        r = json.loads(announce("leaving the momentum book", "regime call, not a rotation"))
        assert r["ok"] is True and r["sent"] is True, r
        assert len(sent) == 1, sent
        # DURABLE FIRST: the announcement is in the journal even though the push
        # happened to succeed here -- that is what makes a FAILED push traceable.
        assert r["journalled"] is True, r
        assert len(journalled) == 1 and journalled[0]["event"] == "announcement", journalled
        assert journalled[0]["headline"] == "leaving the momentum book", journalled
        assert journalled[0]["detail"] == "regime call, not a rotation", journalled
        # the RETURN VALUE must not read as approval — the agent proceeds
        assert "NOTIFICATION ONLY" in r["note"] and "nothing waits for a reply" in r["note"], r
        # an empty announcement is refused rather than pushed as noise
        assert json.loads(announce("   "))["ok"] is False
        assert len(sent) == 1, "an empty headline must not reach the phone"
        assert len(journalled) == 1, "an empty headline must not reach the journal"
        # detail defaults to the headline rather than sending a blank body
        sent.clear()
        json.loads(announce("halted for the day"))
        assert sent[0][0][1] == "halted for the day", sent

        # a transport that BLOWS UP must not end the session — the tool reports
        # sent:false and the agent carries on with its decision.
        def boom(*a, **k):
            raise RuntimeError("no network")
        notify.push = boom
        journalled.clear()
        r = json.loads(announce("still speaking"))
        assert r["ok"] is True and r["sent"] is False, r
        # THE POINT OF JOURNALLING FIRST: the push died, the record survived.
        assert r["journalled"] is True and len(journalled) == 1, (r, journalled)

        # and the mirror case -- a dead JOURNAL must not stop the push either.
        notify.push = lambda *a, **k: bool(sent.append((a, k))) or True
        store.append_journal = boom
        sent.clear()
        r = json.loads(announce("journal is wedged"))
        assert r["ok"] is True and r["sent"] is True and r["journalled"] is False, r
        assert len(sent) == 1, sent
    finally:
        notify.push = real_push
        store.append_journal = real_jrnl
        announce_mod.push_detached = announce_mod.push_detached
    print("selftest OK: announce() journals first, pushes without forking, "
          "refuses empty, survives a dead transport AND a dead journal")

    # set_levels(): the response must report what the monitor will ACTUALLY
    # enforce (per docs/OPSLOG / this fix), never a bare ok:true. Redirect
    # decide.OVERRIDES to a scratch file for the whole block so this NEVER
    # touches the live research_store/monitor/overrides.json.
    import tempfile
    from research_store.models import Thesis, ResearchProduct

    orig_overrides = decide.OVERRIDES
    orig_rh_positions = decide.RH_POSITIONS
    orig_quotes = decide.QUOTES
    orig_read_current = globals()["read_current"]
    # M-b: set_levels() now journals every write (mirrors clear_levels()) --
    # redirect store.append_journal for this whole block too, or every
    # set_levels() call below spams the LIVE research_store/journal.jsonl.
    orig_append_journal = store.append_journal
    _journalled_setlevels: list = []
    store.append_journal = lambda ev: _journalled_setlevels.append(ev)
    with tempfile.TemporaryDirectory() as td:
        decide.OVERRIDES = Path(td) / "overrides.json"
        decide.RH_POSITIONS = Path(td) / "positions.json"
        decide.QUOTES = Path(td) / "quotes.json"
        try:
            # 1) symbol with no thesis at all -> neither level enforced, and the
            #    response says so (the unprotected-position case).
            globals()["read_current"] = lambda: ResearchProduct(as_of="t", theses=[])
            r = json.loads(set_levels("ZZZ", 108.0, 118.0, "test: no thesis"))
            assert r["ok"] is True, r
            assert r["enforcement"]["stop"]["enforced"] is False, r
            assert r["enforcement"]["target"]["enforced"] is False, r
            assert "no thesis" in r["enforcement"]["stop"]["note"], r

            # I6 (final review): `targets` was unannotated, so the generated
            # MCP schema advertised it as a STRING (checked directly against
            # list_tools() when this was found) -- under that schema a
            # well-behaved MCP client sends targets="0" for "no take-profit"
            # (exactly what the docstring tells it to do), and the string "0"
            # discarded the STOP write too: merge_levels() raises ValueError
            # on a non-positive target price before it ever writes anything,
            # and set_levels()'s except-ValueError branch returns ok:false
            # with NOTHING written. Every stringy/zero/blank spelling of "no
            # target" must degrade to None, never reach merge_levels() as a
            # target at all.
            for _no_target in ("0", "0.0", 0, 0.0, "", None):
                r = json.loads(set_levels("ZZZ", 108.0, _no_target,
                                          f"test: no-target spelling {_no_target!r}"))
                assert r["ok"] is True, (_no_target, r)
                assert r["written"]["targets"] == [], (_no_target, r)
            print("selftest OK: set_levels() treats every stringy/zero/blank "
                  "spelling of 'no target' identically -- none of them can "
                  "discard the stop write")

            # ...and the ROOT CAUSE, not just its symptom: the generated MCP
            # schema itself must not advertise `targets` as a string. An
            # unannotated parameter left FastMCP no type to build a real
            # schema from and it fell back to "string" -- this is what
            # induced a well-behaved client into sending "0" in the first
            # place.
            import asyncio                                      # noqa: PLC0415
            _tools = asyncio.run(mcp.list_tools())
            _sl_schema = next(t for t in _tools if t.name == "set_levels").inputSchema
            _tgt_schema = _sl_schema["properties"]["targets"]
            _tgt_types = ({_tgt_schema["type"]} if "type" in _tgt_schema else
                          {opt.get("type") for opt in _tgt_schema.get("anyOf", [])})
            assert "string" not in _tgt_types, _tgt_schema
            assert {"number", "array", "null"} <= _tgt_types, _tgt_schema
            print("selftest OK: set_levels()'s MCP schema advertises `targets` as "
                  "number/array/null, never string")

            # broker snapshot: NVDA and MU actually held (qty > 0). Every
            # scenario below that should reach the enforced/not-enforced
            # arithmetic uses a symbol that IS in this set, so ownership is
            # not what's under test there.
            decide.RH_POSITIONS.write_text(json.dumps(
                {"positions": {"NVDA": {"qty": 10}, "MU": {"qty": 5}}}))

            # Stub the MARK snapshot too. set_levels rejects a stop implausibly
            # far from price, and these fixtures use invented prices (NVDA at
            # 108) that would read as degenerate against the REAL snapshot. What
            # is under test here is the enforcement arithmetic, not plausibility.
            _real_marks_load = marks.load
            marks.load = lambda *a, **k: {
                "account_value": 1000.0,
                "positions": {"NVDA": {"qty": 10, "mark": 110.0},
                              "MU": {"qty": 5, "mark": 50.0}}}
            # I2: the enforcement report now reads decide.load_price() (the
            # monitor's OWN quotes.json), not marks.load()'s mark/avg_cost --
            # so a monitor quote matching the mark above must exist here too,
            # or every "stricter stop -> enforced: True" case below would
            # regress to enforced: False (no known price) rather than testing
            # what it names.
            decide.QUOTES.write_text(json.dumps(
                {"prices": {"NVDA": 110.0, "MU": 50.0}}))

            # thesis on file for NVDA: stop=100, one target=120, held (weight>0)
            th = Thesis(symbol="NVDA", rank=1, verdict="buy", stop=100.0,
                       targets=[120.0], target_weight=0.1)
            globals()["read_current"] = lambda: ResearchProduct(as_of="t", theses=[th])

            # 2) stop LOOSER than the thesis's current stop -> not enforced.
            r = json.loads(set_levels("NVDA", 90.0, 0.0, "test: looser stop"))
            assert r["enforcement"]["stop"]["enforced"] is False, r

            # 3) stop STRICTER (higher) -> enforced. NVDA IS held per the
            #    snapshot above, so this is the genuinely-watched case.
            r = json.loads(set_levels("NVDA", 108.0, 0.0, "test: stricter stop"))
            assert r["enforcement"]["stop"]["enforced"] is True, r

            # 4) target count MISMATCHES the thesis's existing targets -> not enforced.
            th2 = Thesis(symbol="MU", rank=2, verdict="buy", stop=50.0,
                        targets=[60.0, 70.0], target_weight=0.1)
            globals()["read_current"] = lambda: ResearchProduct(as_of="t", theses=[th2])
            r = json.loads(set_levels("MU", 45.0, 55.0, "test: target count mismatch"))
            assert r["enforcement"]["target"]["enforced"] is False, r
            assert "2 target" in r["enforcement"]["target"]["note"], r

            # 4b) THE BLOCKER, FIXED: supply BOTH of the thesis's targets as a
            # list -> count matches -> enforced. Every live thesis carries two
            # targets; before this fix a single value was refused every time.
            r = json.loads(set_levels("MU", 45.0, [65.0, 75.0], "test: full target list"))
            assert r["ok"] is True, r
            assert r["enforcement"]["target"]["enforced"] is True, r
            assert r["written"]["targets"] == [65.0, 75.0], r

            # 4c) targets=[] (an explicit empty list) must normalise to "no
            # target supplied", exactly like targets=0 -- the `_targets or
            # None` guard in set_levels() covers a populated list passing
            # through intact (4b, above) as well as an empty one becoming None.
            r = json.loads(set_levels("MU", 46.0, [], "test: explicit empty list"))
            assert r["ok"] is True, r
            assert r["enforcement"]["target"]["note"] == "no target was set", r
            assert r["written"]["targets"] == [], r

            # 5) ⚠️ A STOP THAT CANNOT TRIGGER IS NOT A STOP. Without this,
            # set_levels(sym, stop=0.01) satisfies `watched: true` and clears the
            # unprotected-position check while protecting nothing -- defeating a
            # safety check by satisfying its letter. It must be REFUSED, and the
            # refusal must say what would be acceptable.
            r = json.loads(set_levels("NVDA", 0.01, 0.0, "test: degenerate stop"))
            assert r["ok"] is False, r
            assert "can never trigger" in r["error"], r
            assert r["nearest_allowed_stop"] == 55.0, r     # 110.0 * 0.50
            # ...and a legitimately WIDE stop on a volatile name still passes
            r = json.loads(set_levels("NVDA", 108.0, 0.0, "test: wide but real"))
            assert "enforcement" in r, r

            # ⛔ A STOP AT OR ABOVE SPOT IS A LIQUIDATION, NOT A LEVEL. There was
            # no upper bound at all: a stop above the live price was accepted and
            # reported "enforced": true, and the live armed monitor -- which
            # polls every 15s and sells the WHOLE position at market on a breach
            # -- fired on it. That is a full exit reached WITHOUT the agent ever
            # placing an order, so the order gate never saw it. Demonstrated on
            # AMD: stop 474.00 against a 473.65 mark -> TRIGGERS fraction 1.0.
            for bad in (110.0, 110.01, 250.0):          # marks.load mark = 110.0
                r = json.loads(set_levels("NVDA", bad, 0.0, "test: stop above spot"))
                assert r["ok"] is False, (bad, r)
                assert "at or ABOVE the last price" in r["error"], (bad, r)
            # just below spot is still a legitimate (tight) stop
            r = json.loads(set_levels("NVDA", 109.99, 0.0, "test: tight but valid"))
            assert "enforcement" in r, r

            marks.load = _real_marks_load

            # 5) matching count AND lowers -> enforced.
            globals()["read_current"] = lambda: ResearchProduct(as_of="t", theses=[th])
            r = json.loads(set_levels("NVDA", 95.0, 110.0, "test: lowers target"))
            assert r["enforcement"]["target"]["enforced"] is True, r

            written = json.loads(Path(decide.OVERRIDES).read_text())
            assert "NVDA" in written, written
        finally:
            decide.OVERRIDES = orig_overrides
            decide.RH_POSITIONS = orig_rh_positions
            decide.QUOTES = orig_quotes
            globals()["read_current"] = orig_read_current
            store.append_journal = orig_append_journal
    print("selftest OK: set_levels() reports actual enforcement (no-thesis, looser-stop, "
          "stricter-stop, target-count-mismatch, target-lowers) -- never a bare ok:true, "
          "and never touched the live overrides.json or journal.jsonl")

    # --- Part 4A2: `widen` -- the agent's route to a DELIBERATE stop
    # loosening. scripts/market_monitor.py:apply_overrides() already honours
    # this (widen=True AND a reason); this proves set_levels() can actually
    # reach that branch, and that the price guard still applies to a widen
    # exactly as it does to any other stop (below spot, never at/above it).
    orig_overrides = decide.OVERRIDES
    orig_rh_positions = decide.RH_POSITIONS
    orig_quotes = decide.QUOTES
    orig_read_current = globals()["read_current"]
    orig_append_journal = store.append_journal
    store.append_journal = lambda ev: None
    with tempfile.TemporaryDirectory() as td:
        decide.OVERRIDES = Path(td) / "overrides.json"
        decide.RH_POSITIONS = Path(td) / "positions.json"
        decide.QUOTES = Path(td) / "quotes.json"
        try:
            decide.RH_POSITIONS.write_text(json.dumps(
                {"positions": {"NVDA": {"qty": 10}}}))
            th_w = Thesis(symbol="NVDA", rank=1, verdict="buy", stop=100.0,
                         targets=[130.0], target_weight=0.1)
            globals()["read_current"] = lambda: ResearchProduct(as_of="t", theses=[th_w])
            _real_marks_load_w = marks.load
            marks.load = lambda *a, **k: {
                "account_value": 1000.0,
                "positions": {"NVDA": {"qty": 10, "mark": 110.0}}}
            decide.QUOTES.write_text(json.dumps({"prices": {"NVDA": 110.0}}))
            try:
                # a) a stop BELOW the thesis stop, no widen -> refused by the
                #    monitor (not stricter), same as before this change.
                r = json.loads(set_levels("NVDA", 90.0, 0.0, "test: no widen"))
                assert r["ok"] is True, r
                assert r["enforcement"]["stop"]["enforced"] is False, r
                assert r["written"].get("widen") is None, r["written"]

                # b) the SAME loosening, now marked widen=True with a reason,
                #    and below the live price -> honoured.
                r = json.loads(set_levels("NVDA", 90.0, 0.0,
                                          "test: inside the noise", widen=True))
                assert r["ok"] is True, r
                assert r["written"]["widen"] is True, r
                assert r["enforcement"]["stop"]["enforced"] is True, r

                # c) a widen still cannot arm a breached stop -- the ceiling
                #    guard (at/above the live price) applies regardless of widen.
                r = json.loads(set_levels("NVDA", 110.0, 0.0,
                                          "test: widen at spot", widen=True))
                assert r["ok"] is False, r
                assert "at or ABOVE the last price" in r["error"], r
            finally:
                marks.load = _real_marks_load_w
        finally:
            decide.OVERRIDES = orig_overrides
            decide.RH_POSITIONS = orig_rh_positions
            decide.QUOTES = orig_quotes
            globals()["read_current"] = orig_read_current
            store.append_journal = orig_append_journal
    print("selftest OK: set_levels(widen=True) reaches the monitor's real "
          "loosening branch (honoured only with a reason, and never past the "
          "at-or-above-spot ceiling)")

    # --- Part 4B: set_levels() must not claim enforcement the monitor will
    # refuse. scripts/market_monitor.py:apply_overrides() (Part 3, 2026-08-17)
    # fails CLOSED on a stop it has no live price for -- but set_levels()
    # never passed a price to evaluate_enforcement() at all (the sentinel
    # default), so it kept answering `enforced: true` regardless. Whenever
    # marks.load() DOES have a usable price for this symbol, the set-time
    # ceiling guard above already refuses (ok: false) any stop at or above it
    # before evaluate_enforcement is ever called -- the two checks share the
    # same in-memory value by construction and can never disagree there. The
    # reachable gap is the OTHER case: no usable price at all. marks.py's own
    # legacy-schema path (a bare {"SYM": <dollars>} row) produces exactly
    # that -- avg_cost=None, mark=None -- a real, supported degraded state,
    # not a contrived one. That is what this fixture reproduces.
    orig_overrides = decide.OVERRIDES
    orig_rh_positions = decide.RH_POSITIONS
    orig_read_current = globals()["read_current"]
    orig_append_journal = store.append_journal   # M-b: set_levels() now journals
    store.append_journal = lambda ev: None
    with tempfile.TemporaryDirectory() as td:
        decide.OVERRIDES = Path(td) / "overrides.json"
        decide.RH_POSITIONS = Path(td) / "positions.json"
        try:
            decide.RH_POSITIONS.write_text(json.dumps(
                {"positions": {"NVDA": {"qty": 10}}}))
            th_px = Thesis(symbol="NVDA", rank=1, verdict="buy", stop=100.0,
                           targets=[130.0], target_weight=0.1)
            globals()["read_current"] = lambda: ResearchProduct(as_of="t", theses=[th_px])

            _real_marks_load2 = marks.load
            # No usable mark AND no avg_cost -- the set-time ceiling guard
            # (`if px and stop ...`) is skipped entirely since px is falsy, so
            # nothing else in set_levels refuses this write; the price guard
            # inside evaluate_enforcement is the only thing left that can
            # catch it.
            marks.load = lambda *a, **k: {
                "account_value": 1000.0,
                "positions": {"NVDA": {"qty": 10, "avg_cost": None, "mark": None}}}
            try:
                r = json.loads(set_levels("NVDA", 108.0, 0.0,
                                          "test: raises stop, no live price known"))
                assert r["ok"] is True, r
                assert r["enforcement"]["stop"]["enforced"] is False, r
                assert "no live price is currently known" in r["enforcement"]["stop"]["note"], r
            finally:
                marks.load = _real_marks_load2
        finally:
            decide.OVERRIDES = orig_overrides
            decide.RH_POSITIONS = orig_rh_positions
            globals()["read_current"] = orig_read_current
            store.append_journal = orig_append_journal
    print("selftest OK: set_levels() passes its own live-price read through to "
          "evaluate_enforcement(), so a stop the monitor cannot verify against a "
          "known price is reported enforced: false -- consistent with "
          "apply_overrides()'s fail-closed guard, not the pre-guard sentinel "
          "behaviour")

    # --- Part 4C (final review, I2): the enforcement report must be built from
    # the SAME price source apply_overrides()'s call site reads
    # (research_store/monitor/quotes.json, via decide.load_price()), never
    # from marks.load()'s `mark or avg_cost` -- which FALLS BACK to cost basis
    # whenever no live monitor quote exists. A position bought long ago and
    # never quoted by the monitor since (or never quoted at all) still
    # produces a perfectly usable `px` from avg_cost alone, so the OLD code
    # reported `enforced: true` for a stop the monitor's own fail-closed price
    # guard would refuse outright -- the exact reachable case the review
    # flagged. This is the REPORT-ONLY price; the ceiling/floor refusal guard
    # above still uses `px` (marks-based) on purpose, since refusing a stop at
    # or above the last known price is correct either way.
    orig_overrides = decide.OVERRIDES
    orig_rh_positions = decide.RH_POSITIONS
    orig_quotes = decide.QUOTES
    orig_read_current = globals()["read_current"]
    orig_append_journal = store.append_journal   # M-b: set_levels() now journals
    store.append_journal = lambda ev: None
    with tempfile.TemporaryDirectory() as td:
        decide.OVERRIDES = Path(td) / "overrides.json"
        decide.RH_POSITIONS = Path(td) / "positions.json"
        decide.QUOTES = Path(td) / "quotes.json"     # absent -> load_price() is None
        try:
            decide.RH_POSITIONS.write_text(json.dumps(
                {"positions": {"NVDA": {"qty": 10}}}))
            th_avgcost = Thesis(symbol="NVDA", rank=1, verdict="buy", stop=100.0,
                                targets=[130.0], target_weight=0.1)
            globals()["read_current"] = lambda: ResearchProduct(as_of="t", theses=[th_avgcost])

            # NO monitor quote at all -- only a cost basis. The set-time
            # ceiling/floor guard still has a usable `px` (150.0, from
            # avg_cost) and happily passes a stop of 120 (raises 100, sits
            # below 150). The monitor, though, has NEVER quoted this symbol
            # -- decide.QUOTES is empty -- so apply_overrides() would refuse
            # this override for lack of a known live price.
            _real_marks_load4 = marks.load
            marks.load = lambda *a, **k: {
                "account_value": 1000.0,
                "positions": {"NVDA": {"qty": 10, "avg_cost": 150.0, "mark": None}}}
            try:
                r = json.loads(set_levels("NVDA", 120.0, 0.0,
                                          "test: avg_cost usable, no monitor quote"))
                assert r["ok"] is True, r
                assert r["enforcement"]["stop"]["enforced"] is False, r
                assert "no live price is currently known" in r["enforcement"]["stop"]["note"], r
            finally:
                marks.load = _real_marks_load4
        finally:
            decide.OVERRIDES = orig_overrides
            decide.RH_POSITIONS = orig_rh_positions
            decide.QUOTES = orig_quotes
            globals()["read_current"] = orig_read_current
            store.append_journal = orig_append_journal
    print("selftest OK: set_levels()'s enforcement report reads the monitor's OWN "
          "quotes.json (decide.load_price()), not marks.load()'s avg_cost fallback "
          "-- a symbol with a cost basis but no live monitor quote reports "
          "enforced: false, matching what apply_overrides() will actually refuse")

    # --- coverage for the finding this fix closes: set_levels() must consult
    # broker ownership too, not just the thesis, or it falsely claims
    # enforcement for a symbol the monitor never looks at ---
    orig_overrides = decide.OVERRIDES
    orig_rh_positions = decide.RH_POSITIONS
    orig_quotes = decide.QUOTES
    orig_read_current = globals()["read_current"]
    orig_append_journal = store.append_journal   # M-b: set_levels() now journals
    store.append_journal = lambda ev: None
    with tempfile.TemporaryDirectory() as td:
        decide.OVERRIDES = Path(td) / "overrides.json"
        decide.RH_POSITIONS = Path(td) / "positions.json"
        decide.QUOTES = Path(td) / "quotes.json"
        # Stub the MARK snapshot for the whole block (Part 4B wires this
        # price through to evaluate_enforcement, so an unmocked marks.load()
        # would read the REAL live positions.json here -- flaky and wrong for
        # a unit test). Only NVDA needs a real number: it is the only symbol
        # below whose stop RAISES the thesis's current stop AND reaches the
        # price-guard branch (cases 6/7/8 all return earlier, on ownership /
        # weight / missing-stop, before price is ever consulted). I2: the
        # enforcement report reads decide.load_price(), not marks.load(), so
        # the SAME number needs a matching entry in the monitor's own
        # quotes.json too.
        _real_marks_load3 = marks.load
        marks.load = lambda *a, **k: {
            "account_value": 1000.0,
            "positions": {"NVDA": {"qty": 10, "mark": 110.0}}}
        decide.QUOTES.write_text(json.dumps({"prices": {"NVDA": 110.0}}))
        try:
            # snapshot only lists NVDA as held -- TSLA is confirmed NOT held.
            decide.RH_POSITIONS.write_text(json.dumps(
                {"positions": {"NVDA": {"qty": 10}}}))

            # 6) THE REVIEWER'S CASE: a thesis for a symbol the broker
            #    snapshot does not list. Book filter passes (weight>0, has a
            #    stop) but the position isn't held -> neither level enforced,
            #    with a note distinct from "no thesis".
            th_tsla = Thesis(symbol="TSLA", rank=3, verdict="buy", stop=200.0,
                             targets=[250.0], target_weight=0.1)
            globals()["read_current"] = lambda: ResearchProduct(as_of="t", theses=[th_tsla])
            r = json.loads(set_levels("TSLA", 210.0, 240.0, "test: not held"))
            assert r["enforcement"]["stop"]["enforced"] is False, r
            assert r["enforcement"]["target"]["enforced"] is False, r
            assert "not currently held" in r["enforcement"]["stop"]["note"], r
            assert "no thesis" not in r["enforcement"]["stop"]["note"], r

            # A genuinely held, watched symbol in the SAME snapshot still
            # reports enforced: true where the arithmetic says so.
            th_nvda = Thesis(symbol="NVDA", rank=1, verdict="buy", stop=100.0,
                             targets=[120.0], target_weight=0.1)
            globals()["read_current"] = lambda: ResearchProduct(as_of="t", theses=[th_nvda])
            r = json.loads(set_levels("NVDA", 108.0, 0.0, "test: held and watched"))
            assert r["enforcement"]["stop"]["enforced"] is True, r

            # 7) held (owned=True) but target_weight == 0 -> book filter
            #    excludes it -> not enforced.
            decide.RH_POSITIONS.write_text(json.dumps(
                {"positions": {"AAPL": {"qty": 3}}}))
            th_zero_w = Thesis(symbol="AAPL", rank=4, verdict="buy", stop=150.0,
                               targets=[180.0], target_weight=0.0)
            globals()["read_current"] = lambda: ResearchProduct(as_of="t", theses=[th_zero_w])
            r = json.loads(set_levels("AAPL", 160.0, 170.0, "test: zero weight"))
            assert r["enforcement"]["stop"]["enforced"] is False, r
            assert "target_weight" in r["enforcement"]["stop"]["note"], r

            # 8) held (owned=True) but thesis carries NO stop -> book filter
            #    excludes it -> not enforced.
            decide.RH_POSITIONS.write_text(json.dumps(
                {"positions": {"MSFT": {"qty": 2}}}))
            th_no_stop = Thesis(symbol="MSFT", rank=5, verdict="buy", stop=None,
                                targets=[300.0], target_weight=0.1)
            globals()["read_current"] = lambda: ResearchProduct(as_of="t", theses=[th_no_stop])
            r = json.loads(set_levels("MSFT", 250.0, 260.0, "test: thesis has no stop"))
            assert r["enforcement"]["stop"]["enforced"] is False, r
            assert "no stop" in r["enforcement"]["stop"]["note"], r

            # 9) ownership INDETERMINATE (no positions.json at all, torn read)
            #    -> the monitor fails open and still watches a book-filtered
            #    thesis -> enforcement follows the normal arithmetic, but the
            #    note must flag that ownership was never confirmed.
            decide.RH_POSITIONS.unlink()
            th_indet = Thesis(symbol="NVDA", rank=1, verdict="buy", stop=100.0,
                              targets=[120.0], target_weight=0.1)
            globals()["read_current"] = lambda: ResearchProduct(as_of="t", theses=[th_indet])
            r = json.loads(set_levels("NVDA", 108.0, 0.0, "test: ownership indeterminate"))
            assert r["enforcement"]["stop"]["enforced"] is True, r
            assert "could not be verified" in r["enforcement"]["stop"]["note"], r
        finally:
            marks.load = _real_marks_load3
            decide.OVERRIDES = orig_overrides
            decide.RH_POSITIONS = orig_rh_positions
            decide.QUOTES = orig_quotes
            globals()["read_current"] = orig_read_current
            store.append_journal = orig_append_journal
    print("selftest OK: set_levels() also consults broker ownership, not just the "
          "thesis (not-held, zero-weight, no-stop, ownership-indeterminate, "
          "genuinely-held-and-watched) -- and never touched the live "
          "overrides.json or positions.json")

    # --- clear_levels(): the mechanism to remove a level, not only set one.
    # With no expiry, an override outlives its position and wakes up on
    # re-entry at a price it was never written for -- this is what closes
    # that hazard. Redirect decide.OVERRIDES to a scratch file so this NEVER
    # touches the live overrides.json, and mock store.append_journal so it
    # NEVER touches the live journal.jsonl either.
    orig_overrides = decide.OVERRIDES
    real_append_journal = store.append_journal
    journalled_clears: list = []
    store.append_journal = lambda ev: journalled_clears.append(ev)
    with tempfile.TemporaryDirectory() as td:
        decide.OVERRIDES = Path(td) / "overrides.json"
        try:
            r1 = json.loads(set_levels("AAA", 1.0, None, "test: level one"))
            assert r1["ok"] is True, r1
            r2 = json.loads(set_levels("BBB", 2.0, None, "test: level two"))
            assert r2["ok"] is True, r2

            # M-b: set_levels() now journals its own decision too, the same
            # way clear_levels() always has -- otherwise the ledger recorded a
            # level's removal and reason but never its creation, and clearing
            # a level erased the only record of why it was ever set.
            assert len(journalled_clears) == 2, journalled_clears
            assert journalled_clears[0]["action"] == "set_level", journalled_clears[0]
            assert journalled_clears[0]["symbol"] == "AAA", journalled_clears[0]
            assert journalled_clears[0]["reason"] == "test: level one", journalled_clears[0]
            assert journalled_clears[1]["symbol"] == "BBB", journalled_clears[1]

            # refuses an empty reason -- and must not touch the file or journal
            before = json.loads(decide.OVERRIDES.read_text())
            _journalled_before_refusal = len(journalled_clears)
            r = json.loads(clear_levels("AAA", ""))
            assert r["ok"] is False, r
            assert "reason is required" in r["error"], r
            after = json.loads(decide.OVERRIDES.read_text())
            assert after == before, "a refused clear must not touch the file"
            assert len(journalled_clears) == _journalled_before_refusal, \
                "a refused clear must not journal"

            r = json.loads(clear_levels("AAA", "closed the position"))
            assert r["ok"] is True, r
            assert r["symbol"] == "AAA" and r["cleared"] is True, r
            assert r["remaining_symbols"] == ["BBB"], r

            # the OTHER symbol survives intact -- this is the whole point
            written = json.loads(decide.OVERRIDES.read_text())
            assert set(written) == {"BBB"}, written
            assert written["BBB"]["stop"] == 2.0, written

            # journalled through the same decide.decision_entry ->
            # store.append_journal path record_decision uses, so a clear
            # appears in research_log's recent_decisions beside every other
            # decision (memory._reasoned_events reads "agent_decision" events).
            assert len(journalled_clears) == 3, journalled_clears
            entry = journalled_clears[-1]
            assert entry["event"] == "agent_decision", entry
            assert entry["symbol"] == "AAA", entry
            assert entry["action"] == "clear_level", entry
            assert entry["reason"] == "closed the position", entry

            # clearing an absent symbol is a no-op, not an error -- the agent
            # calling this after an exit should not need to know whether a
            # level was ever set for it.
            journalled_clears.clear()
            r = json.loads(clear_levels("ZZZ", "no-op check"))
            assert r["ok"] is True, r
            assert r["remaining_symbols"] == ["BBB"], r
            still = json.loads(decide.OVERRIDES.read_text())
            assert set(still) == {"BBB"}, still
        finally:
            decide.OVERRIDES = orig_overrides
            store.append_journal = real_append_journal
    print("selftest OK: clear_levels() removes one symbol atomically, leaves an "
          "unrelated symbol intact, refuses an empty reason without touching "
          "the file or journal, is a no-op on an absent symbol, journals "
          "exactly the way record_decision does, and never touched the live "
          "overrides.json or journal.jsonl -- and set_levels() now journals "
          "its own creation the same way")

    # --- check_order(): a SELL must never be refused for a buy-only reason.
    # Proven BY CONSTRUCTION: build a scratch config/state where EVERY buy-
    # blocking condition fires simultaneously (HALT-ENTRIES touched, drawdown
    # blown via a seeded peak, an order cap of ~0, and a whitelist that
    # excludes the test symbol), then show a SELL of that same symbol/amount
    # sails through clean while a BUY is blocked on all four. gov.REPO/STATE
    # and strategy.load/marks.load are monkeypatched to a tmpdir for the
    # whole block so this never touches the live repo state.
    orig_gov_repo, orig_gov_state = gov.REPO, gov.STATE
    orig_strat_load, orig_marks_load = strat.load, marks.load
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        gov.REPO = tdp
        gov.STATE = tdp / "research_store" / "governance" / "state.json"
        (tdp / "research_store").mkdir(parents=True)
        (tdp / "config").mkdir()
        (tdp / "config" / "universe.csv").write_text("ticker,flag\nAAPL,\n")
        (tdp / "config" / "etf_universe.csv").write_text("ticker,flag\nXLK,\n")
        (tdp / "research_store" / "HALT_ENTRIES").touch()   # blocks entries
        gov.STATE.parent.mkdir(parents=True, exist_ok=True)
        gov.STATE.write_text(json.dumps({"peak_value": 1000.0}))  # so $100 -> -90% dd
        test_cfg = {
            "governance": {"kill_switch_file": "research_store/HALT",
                           "halt_entries_file": "research_store/HALT_ENTRIES",
                           "max_drawdown": 0.01, "max_order_pct": 0.0001,
                           "require_whitelist": True,
                           "min_dollar_volume_20d": 50_000_000.0},
            "universe": {"source": "config/universe.csv"},
            "etf_sleeve": {"source": "config/etf_universe.csv"},
        }
        strat.load = lambda: test_cfg
        marks.load = lambda: {"account_value": 100.0}
        try:
            # off-universe symbol: sell clean, buy blocked on HALT-ENTRIES +
            # DRAWDOWN + whitelist (vet_plan's whitelist check short-circuits
            # before it ever reaches the order-cap check, so a second symbol
            # that IS whitelisted is used below to reach the cap).
            r_sell = json.loads(check_order("ZZZZ_NOT_IN_UNIVERSE", "sell", 5.0))
            assert r_sell["allowed"] is True, r_sell
            assert r_sell["reasons"] == [], r_sell

            r_buy = json.loads(check_order("ZZZZ_NOT_IN_UNIVERSE", "buy", 5.0))
            assert r_buy["allowed"] is False, r_buy
            assert any("HALT-ENTRIES" in x for x in r_buy["reasons"]), r_buy
            assert any("DRAWDOWN" in x for x in r_buy["reasons"]), r_buy
            assert any("whitelist" in x for x in r_buy["reasons"]), r_buy

            # in-universe symbol, oversized: sell still clean; buy now reaches
            # (and is blocked by) the per-order cap instead of the whitelist.
            r_sell2 = json.loads(check_order("AAPL", "sell", 5.0))
            assert r_sell2["allowed"] is True, r_sell2
            assert r_sell2["reasons"] == [], r_sell2

            r_buy2 = json.loads(check_order("AAPL", "buy", 5.0))
            assert r_buy2["allowed"] is False, r_buy2
            assert any("HALT-ENTRIES" in x for x in r_buy2["reasons"]), r_buy2
            assert any("DRAWDOWN" in x for x in r_buy2["reasons"]), r_buy2
            assert any("max order" in x for x in r_buy2["reasons"]), r_buy2
        finally:
            gov.REPO, gov.STATE = orig_gov_repo, orig_gov_state
            strat.load, marks.load = orig_strat_load, orig_marks_load
    print("selftest OK: check_order() -- a SELL is never refused by HALT-ENTRIES, "
          "the drawdown halt, the order cap, or the whitelist (all forced on "
          "simultaneously by construction); the matching BUY is blocked on each "
          "-- and none of this touched the live repo state")

    # liquidity is advisory only: an unmeasurable symbol must not appear in
    # `reasons` (which would make `allowed` false), only in `liquidity_advisory`.
    r = json.loads(check_order("ZZZZ_TOTALLY_UNKNOWN_SYMBOL", "buy", 1.0))
    assert not any("liquidity" in x.lower() for x in r["reasons"]), r
    assert r["liquidity_advisory"]["advisory_only"] is True, r
    assert r["liquidity_advisory"]["ok"] is False, r  # unmeasurable -> fails closed, advisory
    print("selftest OK: check_order() liquidity is advisory-only -- never appears "
          "in the blocking `reasons`, even when unmeasurable")

    # --- performance(): a TRIM must show up, and must NOT touch win_rate.
    # THE FAILURE THIS COVERS: before partial_outcome existed, a de-risking trim
    # wrote nothing at all, so the tool the agent uses to judge its own approach
    # omitted exactly the behaviour worth reinforcing. The opposite error --
    # folding a 1/3 sale into the round-trip stats -- would be just as wrong, so
    # both directions are asserted here against a SCRATCH journal (the live
    # journal.jsonl is never read in this block).
    orig_journal = globals()["JOURNAL"]
    with tempfile.TemporaryDirectory() as td:
        jp = Path(td) / "journal.jsonl"
        jp.write_text("\n".join(json.dumps(e) for e in [
            {"event": "outcome", "symbol": "WDC", "at": "2026-08-06T14:00:00Z",
             "decision_id": "WDC:2026-08-04",
             "outcome": {"pnl_pct": -0.041, "holding_days": 2,
                         "exit_reason": "stop", "hit_stop": True,
                         "hit_target": False}},
            {"event": "partial_outcome", "symbol": "AMAT",
             "decision_id": "AMAT:2026-08-09", "fraction": 0.33,
             "entry_price": 501.32, "exit_price": 534.0401, "pnl_pct": 0.0653,
             "exit_reason": "trim", "exit_date": "2026-08-10",
             "at": "2026-08-10T16:10:00Z"},
            {"event": "partial_outcome", "symbol": "XLE",
             "decision_id": "XLE:2026-08-06", "fraction": 0.5,
             "entry_price": 57.35, "exit_price": 56.2, "pnl_pct": -0.02,
             "exit_reason": "target1", "exit_date": "2026-08-10",
             "at": "2026-08-10T16:12:00Z"},
        ]))
        globals()["JOURNAL"] = jp
        try:
            perf = json.loads(performance())
            pc = perf["partial_closes"]
            assert pc["count"] == 2, pc
            assert pc["best_pnl_pct"] == 6.53 and pc["worst_pnl_pct"] == -2.0, pc
            assert pc["avg_pnl_pct"] == 2.26, pc          # (6.53 + -2.00) / 2
            # capital-weighted: (0.0653*0.33 + -0.02*0.5) / 0.83 = +1.39%, i.e.
            # BELOW the flat average — the winner was the smaller sale, which is
            # the whole reason this figure is reported next to it.
            assert pc["capital_weighted_pnl_pct"] == 1.39, pc
            # ...and the round-trip stats saw ONLY the one full close.
            cts = perf["closed_trades_summary"]
            assert cts["closed_trades"] == 1, cts
            assert cts["win_rate_pct"] == 0.0, cts   # the trims did NOT dilute it
            assert len(perf["recent_closed_trades"]) == 1, perf["recent_closed_trades"]
        finally:
            globals()["JOURNAL"] = orig_journal
    print("selftest OK: performance() surfaces trims in partial_closes (count, "
          "avg/best/worst, capital-weighted) and keeps them OUT of win_rate and "
          "closed_trades -- read from a scratch journal, never the live one")


# --------------------------------------------------------------------------- #
# READ-ONLY MODE — the reviewer's surface
#
# The independent reviewer (Codex) must see exactly what the agent sees and be
# unable to act on it: "a reviewer that can trade is a second trader". Until now
# that was enforced by scripts/agent_view.py, a CLI shim that re-executes THIS
# MODULE once per call -- ~40 MB of pandas per number checked. On 2026-08-14 a
# review ran 120 of them at once and took the box to 44 MB free during RTH with
# a live book and software-only stops.
#
# The agent gets one long-lived server and makes its calls in-process for free.
# The reviewer was paying per call for the same numbers. This closes that gap:
# the reviewer gets the SAME server, started with the write tools removed.
#
# ⛔ FAILS CLOSED. If the registry cannot be read or a write tool survives the
# strip, this REFUSES TO START rather than serving a surface it cannot vouch
# for. A silently-full surface handed to the reviewer is strictly worse than no
# reviewer: it would be a second actor with write access to a live book.
READ_ONLY_TOOLS = {
    "ping", "brief", "account", "positions", "halt_status", "mandate_status",
    "universe", "candidates", "terrain", "history", "quote", "sectors", "leaders",
    "macro", "macro_calendar", "earnings", "news", "depth",
    "performance", "research_log", "check_order",
}


def _strip_to_read_only(server) -> list[str]:
    """Remove every non-read-only tool. -> the surviving names, sorted.

    Raises if the registry shape is unrecognised or anything outside the
    allowlist survives. Both are start-time failures on purpose.
    """
    mgr = getattr(server, "_tool_manager", None)
    reg = getattr(mgr, "_tools", None)
    if not isinstance(reg, dict) or not reg:
        raise RuntimeError(
            "read-only mode cannot find the FastMCP tool registry "
            f"(_tool_manager._tools = {type(reg).__name__}). The mcp library "
            "shape changed; refusing to start rather than serving an "
            "unverified surface to the reviewer.")
    for name in [n for n in reg if n not in READ_ONLY_TOOLS]:
        del reg[name]
    survivors = sorted(reg)
    leaked = [n for n in survivors if n not in READ_ONLY_TOOLS]
    if leaked:
        raise RuntimeError(f"read-only strip failed; still exposed: {leaked}")
    if not survivors:
        raise RuntimeError("read-only strip removed everything — no surface left")
    return survivors


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    elif os.environ.get("AGENT_ENV_READONLY") == "1" or "--read-only" in sys.argv:
        kept = _strip_to_read_only(mcp)
        print(f"agentic-trader MCP: READ-ONLY surface, {len(kept)} tools: "
              f"{', '.join(kept)}", file=sys.stderr)
        mcp.run()
    else:
        mcp.run()
