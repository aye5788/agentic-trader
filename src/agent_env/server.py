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
from agent_env import memory                          # noqa: E402  sibling module
from agent_env import wakes                           # noqa: E402  sibling module
import mandate                                  # noqa: E402
import announce as announce_mod                 # noqa: E402  (module name != tool name)
import notify                                   # noqa: E402
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


# How old a broker snapshot may be before the agent must be told. The snapshot
# is refreshed by the 10:00 fast loop and by the monitor; a close session at
# 15:15 normally sees data hours old, not days.
SNAPSHOT_STALE_HOURS = 8.0


def _staleness(v: dict) -> dict | None:
    """How old is this snapshot, and is that a problem? -> dict, or None if fine.

    ⚠️ NOTHING USED TO SAY THIS. marks.py carries a comment acknowledging the
    total may be "possibly stale" and surfaced it nowhere, so a session whose
    snapshot writer had failed would plan against YESTERDAY'S holdings -- with
    placement allowed and no signal anything was wrong. That is the worst shape
    of failure available here: not zero orders, but confident wrong ones, sized
    and targeted against positions that may already have been sold.

    The writers are the 10:00 fast loop and the monitor. Retiring the fast loop
    (plan Task 8 step 5) removes one of them, so this must be loud before that
    happens, not after.
    """
    from datetime import datetime, timezone           # noqa: PLC0415
    raw = v.get("marked_at") or v.get("ts") or v.get("as_of")
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
    hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    if hours <= SNAPSHOT_STALE_HOURS:
        return None
    return {"age": f"{hours:.1f}h", "as_of": str(raw), "stale": True,
            "warning": (f"⚠️ THIS SNAPSHOT IS {hours:.1f} HOURS OLD (limit "
                        f"{SNAPSHOT_STALE_HOURS:.0f}h). Whoever refreshes it has "
                        f"probably failed. The positions and dollar figures below "
                        f"may describe a book you no longer hold. Call "
                        f"get_equity_positions and get_portfolio for the real "
                        f"state before sizing, stopping or selling anything.")}


@mcp.tool()
def positions() -> str:
    """Every position actually held, with the stop and target set for it.

    `watched: false` means the position has NO stop being enforced — it is
    unprotected. That is deliberately reported rather than hidden.
    """
    prod = read_current()
    v = marks.load()
    out = state.holdings(v, prod.theses if prod else [], _overrides())
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
        out["spendable"] = ("UNKNOWN from this snapshot. `cash` is NOT what you "
                            "can spend — this is a CASH account, so sale proceeds "
                            "are unsettled until T+1. Call Robinhood's "
                            "get_portfolio for the authoritative buying_power "
                            "before sizing any buy.")
    else:
        out["spendable"] = (f"{bp} — size buys against THIS, not `cash`. Sale "
                            f"proceeds settle T+1 on this cash account.")
    return json.dumps(out, indent=2, default=str)


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
    and settlement/buying-power deferrals self-heal and are deliberately silent.

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

    # Funding: ADVISORY, never a block. An underfunded buy is rejected by the
    # broker, so it wastes a placement rather than endangering anything — and
    # blocking on a possibly-stale snapshot could freeze trading on bad data.
    # But `cash` is not spendable on this CASH account (proceeds settle T+1), so
    # an agent sizing against cash plans orders that cannot fill: on 2026-08-10
    # cash was $9.20 and buying_power $2.14.
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
                                f"— Robinhood will reject this. Unsettled proceeds "
                                f"become available T+1.")}

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
def rule_out(symbol: str, reason: str) -> str:
    """Record that you considered this name and rejected it, and why.

    Not a ban and not a fact about the name — a note to your future self so the
    next session does not repeat the work. Use `revisit` when it changes.
    """
    return json.dumps(memory.rule_out(REPO / "research_store", symbol, reason),
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

    regime = None
    if "SPY" in panel.columns:
        try:
            regime = {
                "spy_above_50dma": bool(momentum.regime_on(panel["SPY"], asof)),
                "note": "an observation about the market, not a rule that acts",
            }
        except Exception:
            regime = None

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
        # Every FIGURE is null with no snapshot -- nothing is invented.
        assert all(a[k] is None for k in
                   ("account_number", "account_value", "cash", "invested",
                    "as_of", "marked_at", "buying_power", "unsettled_funds")), a
        # ...and `spendable` must still WARN rather than go quiet: silence here
        # would read as "cash is spendable", which on a T+1 cash account is the
        # error this field exists to prevent.
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
    print("selftest OK: mcp server boots, ping responds, degrades to JSON without a snapshot")

    # ---- a STALE snapshot must announce itself ----------------------------
    # Nothing used to say this: marks.py acknowledged "possibly stale" in a
    # comment and surfaced it nowhere, so a session whose snapshot writer had
    # failed planned against YESTERDAY'S holdings with placement allowed. Not
    # zero orders -- confident wrong ones, against positions possibly sold.
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    fresh = (_dt.now(_tz.utc) - _td(hours=2)).isoformat()
    old_ts = (_dt.now(_tz.utc) - _td(hours=30)).isoformat()

    assert _staleness({"marked_at": fresh}) is None, "a fresh snapshot must be silent"
    st = _staleness({"marked_at": old_ts})
    assert st and st["stale"] is True and "30." in st["age"], st
    assert "HOURS OLD" in st["warning"] and "get_equity_positions" in st["warning"], st
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
    assert _staleness({"marked_at": "not-a-date"})["stale"] is True
    # a bare date is midnight, not "now" -- yesterday's date must read as stale
    y = (_dt.now(_tz.utc) - _td(days=1)).date().isoformat()
    assert _staleness({"as_of": y})["stale"] is True, y
    # the banner reaches BOTH tools, not just account()
    _real_load = marks.load
    try:
        marks.load = lambda *a, **k: {"marked_at": old_ts, "positions": {},
                                      "account_value": 1.0, "cash": 1.0}
        assert "STALE" in json.loads(account()), "account() must carry the banner"
        assert "STALE" in json.loads(positions()), "positions() must carry it too"
        marks.load = lambda *a, **k: {"marked_at": fresh, "positions": {},
                                      "account_value": 1.0, "cash": 1.0}
        assert "STALE" not in json.loads(account()), "fresh must not cry wolf"
        assert "STALE" not in json.loads(positions()), "fresh must not cry wolf"
    finally:
        marks.load = _real_load
    print("selftest OK: a stale/undateable snapshot announces itself in BOTH "
          "account() and positions(); a fresh one stays silent")

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

            # Stub the MARK snapshot too. set_levels rejects a stop implausibly
            # far from price, and these fixtures use invented prices (NVDA at
            # 108) that would read as degenerate against the REAL snapshot. What
            # is under test here is the enforcement arithmetic, not plausibility.
            _real_marks_load = marks.load
            marks.load = lambda *a, **k: {
                "account_value": 1000.0,
                "positions": {"NVDA": {"qty": 10, "mark": 110.0},
                              "MU": {"qty": 5, "mark": 50.0}}}

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

            # EVERY level the agent writes must carry an expiry. risk_review
            # prunes on `expires >= today` with a "9999" default, so an entry
            # written without the key armed the live monitor FOREVER -- outliving
            # the position, the thesis, and the reason it was set for.
            written = json.loads(Path(decide.OVERRIDES).read_text())
            assert "NVDA" in written, written
            assert written["NVDA"].get("expires"), \
                "an agent-set level with no expiry never stops arming the monitor"
            from datetime import date as _date
            assert written["NVDA"]["expires"] > _date.today().isoformat(), written
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


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        mcp.run()
