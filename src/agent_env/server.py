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
from agent_env import decide                          # noqa: E402  sibling module
from agent_env import live as live_mod                # noqa: E402  sibling module
import mandate                                  # noqa: E402
import governance as gov                        # noqa: E402
import strategy as strat                        # noqa: E402
import momentum                                 # noqa: E402
import pandas as pd                             # noqa: E402

mcp = FastMCP("agentic-trader")

EQUITY = REPO / "research_store" / "history" / "equity.jsonl"
JOURNAL = REPO / "research_store" / "journal.jsonl"
CLOSES = REPO / "research_store" / "prices" / "closes.parquet"
HIGHS = REPO / "research_store" / "prices" / "highs.parquet"
LOWS = REPO / "research_store" / "prices" / "lows.parquet"
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


@mcp.tool()
def set_levels(symbol: str, stop: float, target: float = 0.0,
               reason: str = "") -> str:
    """Set YOUR stop and take-profit for a position. `reason` is required.

    This WRITES to the override file the monitor merges every poll -- it does
    NOT force the monitor to act on it. scripts/market_monitor.py only ever
    looks at a symbol's overrides if it is in the monitor's `held` set, which
    requires: a thesis with target_weight > 0 and a stop (the BOOK filter),
    AND that the position is actually owned per the broker snapshot (the
    OWNERSHIP filter) -- a name in tonight's book whose buy has not filled yet
    is invisible to the monitor no matter what levels you set here. Within
    that set, apply_overrides() is stricter-only: your stop is applied only if
    it RAISES the thesis's current stop, and your target is applied only if
    its count matches the thesis's existing targets and it LOWERS every one.

    Read the `enforcement` object in the response before assuming a position is
    protected -- `ok: true` only means the write succeeded, not that either
    level will actually be acted on. Pass target=0 to set a stop with no
    take-profit. Use `terrain()` first so the levels sit where price actually
    goes.
    """
    sym = symbol.strip().upper()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        merged = decide.write_levels(symbol, stop, target or None, reason, ts)
    except ValueError as e:
        return json.dumps({"ok": False, "error": str(e)}, indent=2)

    prod = read_current()
    thesis = None
    if prod:
        for t in prod.theses:
            if str(t.symbol).strip().upper() == sym:
                thesis = t
                break

    owned_set = decide.load_owned()
    owned = None if owned_set is None else (sym in owned_set)

    enforcement = decide.evaluate_enforcement(
        stop=merged[sym]["stop"], target=merged[sym].get("target"),
        has_thesis=thesis is not None,
        target_weight=getattr(thesis, "target_weight", None),
        owned=owned,
        current_stop=getattr(thesis, "stop", None),
        current_targets=list(getattr(thesis, "targets", []) or []) if thesis else None,
    )
    return json.dumps({"ok": True, "symbol": sym, "written": merged[sym],
                       "enforcement": enforcement}, indent=2)


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
    dvol = REPO / "research_store" / "prices" / "pool_dvol.parquet"
    if not dvol.exists():
        return None, None
    try:
        d = pd.read_parquet(dvol)
        as_of = d.index[-1].date().isoformat() if len(d.index) else None
        if symbol not in d.columns:
            return None, as_of
        s = d[symbol].dropna().tail(20)
        return (float(s.mean()) if len(s) else None), as_of
    except Exception:
        return None, None


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
    age_days = None
    if as_of:
        try:
            age_days = (datetime.now(timezone.utc).date() - date.fromisoformat(as_of)).days
        except Exception:
            age_days = None

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

    return json.dumps({"allowed": not reasons, "symbol": sym, "side": sd,
                       "amount": float(amount), "reasons": reasons,
                       "liquidity_advisory": liquidity_advisory}, indent=2)


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
        syms = sorted(state.holdings(v, []).keys())
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

    regime = None
    if "SPY" in panel.columns:
        try:
            regime = {
                "spy_above_50dma": bool(momentum.regime_on(panel["SPY"], asof)),
                "note": "an observation about the market, not a rule that acts",
            }
        except Exception:
            regime = None

    held = state.holdings(v, prod.theses if prod else [])
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
    orig_load = marks.load
    marks.load = lambda: None
    try:
        p = json.loads(positions())
        assert p == {}, p
        a = json.loads(account())
        assert a == {k: None for k in
                     ("account_number", "account_value", "cash", "invested",
                      "as_of", "marked_at")}, a
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
    print("selftest OK: mcp server boots, ping responds, degrades to JSON without a snapshot")

    # set_levels(): the response must report what the monitor will ACTUALLY
    # enforce (per docs/OPSLOG / this fix), never a bare ok:true. Redirect
    # decide.OVERRIDES to a scratch file for the whole block so this NEVER
    # touches the live research_store/monitor/overrides.json.
    import tempfile
    from research_store.models import Thesis, ResearchProduct

    orig_overrides = decide.OVERRIDES
    orig_rh_positions = decide.RH_POSITIONS
    orig_read_current = globals()["read_current"]
    with tempfile.TemporaryDirectory() as td:
        decide.OVERRIDES = Path(td) / "overrides.json"
        decide.RH_POSITIONS = Path(td) / "positions.json"
        try:
            # 1) symbol with no thesis at all -> neither level enforced, and the
            #    response says so (the unprotected-position case).
            globals()["read_current"] = lambda: ResearchProduct(as_of="t", theses=[])
            r = json.loads(set_levels("ZZZ", 108.0, 118.0, "test: no thesis"))
            assert r["ok"] is True, r
            assert r["enforcement"]["stop"]["enforced"] is False, r
            assert r["enforcement"]["target"]["enforced"] is False, r
            assert "no thesis" in r["enforcement"]["stop"]["note"], r

            # broker snapshot: NVDA and MU actually held (qty > 0). Every
            # scenario below that should reach the enforced/not-enforced
            # arithmetic uses a symbol that IS in this set, so ownership is
            # not what's under test there.
            decide.RH_POSITIONS.write_text(json.dumps(
                {"positions": {"NVDA": {"qty": 10}, "MU": {"qty": 5}}}))

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

            # 5) matching count AND lowers -> enforced.
            globals()["read_current"] = lambda: ResearchProduct(as_of="t", theses=[th])
            r = json.loads(set_levels("NVDA", 95.0, 110.0, "test: lowers target"))
            assert r["enforcement"]["target"]["enforced"] is True, r
        finally:
            decide.OVERRIDES = orig_overrides
            decide.RH_POSITIONS = orig_rh_positions
            globals()["read_current"] = orig_read_current
    print("selftest OK: set_levels() reports actual enforcement (no-thesis, looser-stop, "
          "stricter-stop, target-count-mismatch, target-lowers) -- never a bare ok:true, "
          "and never touched the live overrides.json")

    # --- coverage for the finding this fix closes: set_levels() must consult
    # broker ownership too, not just the thesis, or it falsely claims
    # enforcement for a symbol the monitor never looks at ---
    orig_overrides = decide.OVERRIDES
    orig_rh_positions = decide.RH_POSITIONS
    orig_read_current = globals()["read_current"]
    with tempfile.TemporaryDirectory() as td:
        decide.OVERRIDES = Path(td) / "overrides.json"
        decide.RH_POSITIONS = Path(td) / "positions.json"
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
            decide.OVERRIDES = orig_overrides
            decide.RH_POSITIONS = orig_rh_positions
            globals()["read_current"] = orig_read_current
    print("selftest OK: set_levels() also consults broker ownership, not just the "
          "thesis (not-held, zero-weight, no-stop, ownership-indeterminate, "
          "genuinely-held-and-watched) -- and never touched the live "
          "overrides.json or positions.json")

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


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        mcp.run()
