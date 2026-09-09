"""Maintain the daily OHLC panel for the universe (names + ETF sleeve + SPY) in
research_store/prices/*.parquet (git-ignored). Source: **moomoo**, via OpenD.

Was Schwab. Schwab's refresh token expired every 7 days and had to be renewed by
hand at a browser — a standing chore whose only job was keeping a price feed alive.
moomoo authenticates through the already-running OpenD gateway, so there is no
recurring credential work at all. Verified byte-identical on the switch: 166/168
tickers matched the cached Schwab close exactly, the other two by <0.15%.

DEFAULT MODE IS APPEND, not re-pull. The panel already holds ~10y from the Schwab
era; each run adds the current session's row. That is one `get_market_snapshot`
call for the whole universe (~0.2s, no quota) instead of 168 sequential history
pulls. moomoo meters `request_history_kline` against a hard **100 distinct stocks
account-wide**, so a full re-pull of a 168-name universe is IMPOSSIBLE — do not
reintroduce one.

PLUS A TARGETED REPAIR (2026-09-06, `_repair_incomplete`). That is not a re-pull
and does not weaken the rule above: it asks the history API ONLY for universe
members whose panel column cannot satisfy `momentum.compute()`'s window, and
never for a name too young to have that history at all. On a healthy panel it
costs zero calls. It exists because `universe_refresh.py` became UNATTENDED
while the manual `--backfill` that used to follow it did not — so auto-admitted
mature names (ARM, ABNB, SLB, SNPS, HPE) sat accumulating one bar a day and the
signal silently dropped them. See src/history_repair.py for the invariant.

    /usr/bin/python3 scripts/fetch_prices.py              # append + repair gaps
    /usr/bin/python3 scripts/fetch_prices.py --no-repair  # append only
    /usr/bin/python3 scripts/fetch_prices.py --backfill 30  # gap-fill, <=100 names
    /usr/bin/python3 scripts/fetch_prices.py --selftest

⚠️ RUNTIME: must run under system /usr/bin/python3 (the moomoo SDK is not in .venv).
"""
import argparse
import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import cohort as _cohort      # noqa: E402 — stdlib-only, safe under 3.10
import quota_planner as qp    # noqa: E402 — pure, no moomoo import

MARKET_TZ = ZoneInfo("America/New_York")

# moomoo meters request_history_kline against this many DISTINCT stocks,
# account-wide and cumulative — hit live on 2026-07-29 at exactly "stock: 100/100"
# with 98 of 168 universe names still unfetched. The snapshot path is unmetered,
# which is why the daily append uses it instead.
MOOMOO_HISTORY_QUOTA = 100

OUT_DIR = REPO / "research_store" / "prices"
OPENS = OUT_DIR / "opens.parquet"
HIGHS = OUT_DIR / "highs.parquet"
LOWS = OUT_DIR / "lows.parquet"
CLOSES = OUT_DIR / "closes.parquet"
# Consolidated-tape $-volume, appended from the SAME unmetered snapshot call as
# the OHLC above -- it was already in the response and discarded. This is what
# [governance] min_dollar_volume_20d is calibrated against; the old source
# (Alpaca IEX, pool_dvol.parquet) is a single venue and a fraction of the tape,
# so it reads genuinely liquid names as illiquid. Shallow by construction: it
# starts accumulating the day this ships, so consumers MUST handle a short or
# absent history rather than assume 20 rows exist.
TURNOVER = OUT_DIR / "turnover.parquet"
META = OUT_DIR / "fetch_meta.csv"


def universe_tickers(cfg=None) -> list[str]:
    """Every series the panel must carry: the tradeable universe + the price
    series the signal and the regime observation READ but never trade.

    ⛔ CHANGED 2026-08-20. This used to append config/etf_universe.csv — all 18
    ETFs — because the retired sleeve was ranked from the same panel. The sleeve
    and that file are gone. What remains is deliberately narrow and is NOT a
    universe:

      - residual.SECTOR_FACTORS (11 sector series): the residual-momentum tilt
        regresses every name on these to separate a stock's own move from its
        sector's. Drop them and the tilt silently degrades to plain momentum.
      - SPY: the regime observation (price vs its 50-day mean), and the "market"
        variant of the same tilt.

    None of these is tradeable: they are absent from the buy eligibility set
    (governance.buy_eligibility), so a buy naming one is refused.

    ⛔ COHORT MODE WIDENS IT, AND THE WIDENING IS FREE. Under `[universe] mode
    = "cohort"` the names come from the persisted eligibility artifact instead
    of the CSV, plus every HELD symbol. The daily append is ONE unmetered
    `get_market_snapshot` call and moomoo takes up to 400 codes per call, so
    carrying ~200 eligible names + factors costs the same one call and zero
    history quota. What is NOT free is giving those names a 252-session window;
    that is metered, rationed by src/quota_planner.py, and is exactly why an
    eligible name is not a candidate until the panel can score it.

    ⛔ HELD NAMES ARE ADDED UNCONDITIONALLY IN COHORT MODE. A position whose
    price column stops updating loses its mark, and the stop watcher falls back
    to coarser marks (src/marks.py). Membership of a discovery cohort must never
    decide whether an OPEN position is priced — that is the same asymmetry as
    "a sell is refused by nothing". `universe_refresh._validate_before_write`
    already warns when a held name leaves the CSV; this removes the consequence.
    """
    import residual                                       # noqa: PLC0415
    factors = list(residual.SECTOR_FACTORS) + ["SPY"]     # read-only series
    if cfg is not None and _cohort.mode(cfg) == "cohort":
        names, note = _cohort_tickers(cfg)
        if names is not None:
            print(note)
            return list(dict.fromkeys(names + factors))
        # No usable cohort -> the CSV, ANNOUNCED. This is the one place a
        # fallback to the legacy list is correct: the panel is DATA, not
        # permission, and a narrower panel cannot authorise anything. The order
        # gate has the opposite polarity and never falls back (governance.
        # buy_eligibility), so a degraded cohort refuses buys even while prices
        # keep flowing from the CSV.
        print(note)
    names = pd.read_csv(REPO / "config" / "universe.csv")["ticker"].tolist()
    return list(dict.fromkeys(names + factors))


def _cohort_tickers(cfg):
    """-> (tickers | None, note). Eligible cohort + held names, or why not."""
    try:
        view = _cohort.active_view(cfg, REPO, dt.datetime.now(MARKET_TZ).date())
    except _cohort.CohortInvalid as e:
        return None, (f"  cohort unusable ({e}) — panel falls back to "
                      f"config/universe.csv. Prices are DATA: a fallback here "
                      f"cannot authorise a buy, and the order gate does not "
                      f"fall back.")
    names = list(view["by_ticker"])
    held = sorted(_held_symbols())
    extra = [h for h in held if h not in set(names)]
    return names + extra, (
        f"  cohort mode: {len(names)} eligible name(s) as of {view['as_of']}"
        + (f" + {len(extra)} held-but-outside ({' '.join(extra)})" if extra else ""))


def _held_symbols() -> set:
    """Symbols in the broker snapshot. Unreadable -> empty set (fail quiet).

    Used only to WIDEN the panel, so an empty answer degrades to the previous
    behaviour rather than to something unsafe.
    """
    try:
        d = json.loads((REPO / "research_store" / "rh" / "positions.json").read_text())
        return {str(s).strip().upper() for s in (d.get("positions") or {})}
    except Exception:                                     # noqa: BLE001
        return set()


def _quota_settings(cfg) -> dict:
    """The `[history_acquisition]` knobs, resolved. -> dict.

    `rolling_window_days` is UNSET by default and means UNKNOWN — no capacity is
    ever reclaimed by the passage of time. It is named for what it is: a broker
    window duration an operator has DOCUMENTED. It replaced `repeat_credit_days`,
    which was an arbitrary 7-day timer doubling as a capacity model and silently
    handed back 100 symbols of spend on day 8.
    """
    h = (cfg or {}).get("history_acquisition") or {}
    try:
        quota = int(h.get("distinct_symbol_quota", MOOMOO_HISTORY_QUOTA))
    except (TypeError, ValueError):
        quota = MOOMOO_HISTORY_QUOTA
    win = h.get("rolling_window_days", qp.DEFAULT_ROLLING_WINDOW_DAYS)
    try:
        win = None if win in (None, 0, "") else int(win)
    except (TypeError, ValueError):
        win = None                      # unparseable -> UNKNOWN, never a guess
    return {"quota": quota, "rolling_window_days": win,
            "ledger": REPO / str(h.get("ledger_file") or qp.LEDGER_FILE),
            "reset": REPO / str(h.get("reset_file") or qp.RESET_FILE)}


def resolve_capacity(cfg, ctx, mmp, today) -> dict:
    """Remaining distinct-symbol capacity, from the best authority available.

    Asks the BROKER first (`history_quota()` — unmetered, read-only, and the
    only account-wide-correct answer), then falls back to the local ledger with
    an operator-confirmed reset or a documented window, then to UNKNOWN.

    ⛔ A TELEMETRY FAILURE IS NOT ZERO USED. `history_quota()` returns
    `ok=False` on any problem and this hands that straight to
    `capacity_state()`, which then resolves from local evidence — never from an
    assumption that the account is idle.
    """
    q = _quota_settings(cfg)
    tel = None
    if ctx is not None and mmp is not None:
        try:
            tel = mmp.history_quota(ctx=ctx)
        except Exception as e:                            # noqa: BLE001
            tel = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if tel and not tel.get("ok"):
            print(f"  history quota telemetry unavailable ({tel.get('error')}) "
                  f"— falling back to local evidence")
    cap = qp.capacity_state(
        telemetry=tel, ledger_state=qp.load_ledger_state(q["ledger"]),
        reset_state=qp.load_reset_state(q["reset"]), today=today,
        quota=q["quota"], rolling_window_days=q["rolling_window_days"])
    cap["settings"] = q
    return cap


def authorized_history_fetch(panels, wanted, *, cfg, ctx, mmp, today,
                             start, end, held=(), missing_rows=None,
                             cohort_ranks=None, dry=False, label="repair"):
    """⛔ THE ONE AND ONLY PATH TO `request_history_kline` IN THIS REPO.

    Both the automatic repair and the operator's `--backfill` come through here.
    Neither has its own quota accounting; they differ only in how they ORDER
    their candidates, which is passed in.

    It does, in this order and always:
      1. resolves remaining capacity from the source of truth
         (`resolve_capacity`);
      2. plans against it (`quota_planner.plan`) — authorised symbols only;
      3. issues `mmp.daily_panel()` for exactly the authorised symbols;
      4. MERGES the returned bars into `panels` before anything assesses
         completeness or persists;
      5. records every ATTEMPTED symbol — including failures, because the meter
         charges a symbol rather than a successful response — and records
         nothing when nothing was attempted.

    -> (panels, result) where result carries request/deferred/reasons/budget/
    errors/raw_counts.

    ⛔ `--backfill` USED TO BYPASS ALL OF THIS. It sliced its candidate list at
    100 and called `daily_panel()` directly: no awareness of what the window had
    already spent, and no ledger entry afterwards, so its spend was invisible to
    the next automatic repair. An operator action may be deliberate about WHICH
    names it wants; it cannot be exempt from the broker's meter.
    """
    cap = resolve_capacity(cfg, ctx, mmp, today)
    print(f"  history quota [{label}]: {cap['note']}  (source={cap['source']})")
    budget = qp.plan(
        wanted, held=held, missing_rows=missing_rows or {},
        cohort_ranks=cohort_ranks or {}, quota=cap["settings"]["quota"],
        charged=cap["charged"], remaining_new=cap["remaining_new"],
        capacity_unknown=cap["capacity_unknown"])
    b = budget["budget"]
    print(f"  history budget [{label}]: {b['requested']} authorized "
          f"({b['new_symbols']} new + {b['repeat_symbols']} repeat), "
          f"{b['remaining_new_distinct']} distinct-symbol slot(s) available; "
          f"{b['deferred']} deferred")

    result = {"request": budget["request"], "deferred": budget["deferred"],
              "reasons": budget["reasons"], "budget": b, "errors": {},
              "raw": {}, "capacity": cap}
    if not budget["request"]:
        # ⛔ NOTHING ATTEMPTED -> NOTHING RECORDED. A ledger entry for a request
        # that was never sent would spend capacity we still have.
        return panels, result
    if dry:
        print(f"  [dry] would request {len(budget['request'])} name(s); "
              f"nothing requested, nothing recorded")
        return panels, result

    raw, errs = mmp.daily_panel(budget["request"], start, end, ctx=ctx)

    # ⛔ MERGE BEFORE ANY ASSESSMENT OR PERSISTENCE. `fetched.combine_first(base)`
    # lets the fetch win where it has a value and the stored panel fill every gap
    # it does not cover, so a partial response adds observations and can never
    # erase valid stored data. Dropping this step is what made every repaired
    # name report STILL INCOMPLETE for ever while spending a quota unit a run.
    if raw:
        fetched = _field_panels(raw)
        for f in _FIELDS:
            base = panels.get(f)
            panels[f] = (fetched[f] if base is None or base.empty
                         else fetched[f].combine_first(base).sort_index())

    try:
        # `cap["retention"]` is the permission to forget, from whichever
        # authority set the capacity above — `{}` (keep everything) whenever
        # capacity was inferred from local evidence or could not be established.
        # Passing it through is what stops a write from reclaiming a
        # distinct-symbol slot that no telemetry, reset or documented window
        # ever released.
        qp.record(cap["settings"]["ledger"], budget["request"], today,
                  **(cap.get("retention") or {}))
    except Exception as e:                                # noqa: BLE001
        print(f"  ⚠️ history-quota ledger not updated ({type(e).__name__}: {e}) "
              f"— the next run cannot see this spend; fix before the next "
              f"metered run, or capacity will read higher than it is")
    result["errors"] = {t: str(e)[:200] for t, e in (errs or {}).items()}
    result["raw"] = {t: len(c) for t, c in (raw or {}).items()}
    return panels, result


def _missing_rows(close, tickers) -> dict:
    """{ticker: sessions short of the momentum window}. Smaller = cheaper to fix.

    This is what makes "closest to becoming scoreable" a MEASUREMENT rather than
    a guess: it counts the non-null closes actually present inside the window
    `momentum.compute()` reads, so a name needing 6 more sessions outranks one
    needing 250. A ticker with no column at all carries no entry — it is not
    "nearly there", and priority_key falls through to its cohort rank.
    """
    import history_repair as hr                            # noqa: PLC0415
    out = {}
    if close is None or getattr(close, "empty", True):
        return out
    for t in tickers:
        if t not in close.columns:
            continue
        col = close[t].iloc[-hr.REQUIRED_ROWS:]
        out[t] = max(0, hr.REQUIRED_ROWS - int(col.notna().sum()))
    return out


def _cohort_ranks(cfg) -> dict:
    """{ticker: eligibility rank} from the cohort artifact, or {} in fixed_list
    mode. Used only to ORDER a rationed queue — never to decide membership."""
    if cfg is None or _cohort.mode(cfg) != "cohort":
        return {}
    try:
        view = _cohort.active_view(cfg, REPO, dt.datetime.now(MARKET_TZ).date())
    except _cohort.CohortInvalid:
        return {}
    return {t: r for t, r in view["ranks"].items() if isinstance(r, int)}


def write_history_state(panels, tickers, plan, path, today) -> dict:
    """Persist WHY each name is or is not scoreable. Returns the states written.

    ⛔ FIVE STATES, AND THE DISTINCTIONS ARE THE POINT (src/cohort.py holds the
    vocabulary):

      complete         the window is satisfied — the signal has a number, so the
                       name is a candidate and is buyable
      seasoning        listed too recently to have the window. Not a gap, not a
                       fault, and nothing to fix: it resolves by the passage of
                       time and must never burn a quota unit meanwhile
      deferred_quota   mature and repairable, but past this run's budget. It is
                       retried next run in the same priority order
      repairing        history was requested and the window is STILL short —
                       a partial result, which must never be treated as a valid
                       signal input
      failed_provider  the request came back with an error. Distinguished from
                       `repairing` because one is a data-shape problem and the
                       other is an outage, and an operator reading the dashboard
                       needs to know which

    Everything except `complete` composes to a research lead: visible, not
    buyable. A state string this file does not know composes to pending too —
    src/cohort.compose has no branch that reaches `scoreable` except an exact
    `complete`, so a typo can never make a name buyable.
    """
    import history_repair as hr                            # noqa: PLC0415
    close = panels.get("close")
    plan = plan or {}
    seasoning = set(plan.get("seasoning") or ())
    deferred = set(plan.get("deferred") or ())
    attempted = set(plan.get("repair") or ())
    errors = plan.get("errors") or {}
    states, reasons = {}, {}
    for t in tickers:
        ok = (close is not None and not getattr(close, "empty", True)
              and t in close.columns and hr.window_complete(close[t]))
        if ok:
            states[t] = _cohort.HIST_COMPLETE
        elif t in seasoning:
            states[t] = _cohort.HIST_SEASONING
        elif t in deferred:
            states[t] = _cohort.HIST_DEFERRED
            reasons[t] = (plan.get("reasons") or {}).get(t, "past this run's quota")
        elif t in attempted and t in errors:
            states[t] = _cohort.HIST_FAILED
            reasons[t] = errors[t]
        elif t in attempted:
            states[t] = _cohort.HIST_REPAIRING
            reasons[t] = "history requested; the momentum window is still short"
        else:
            states[t] = _cohort.HIST_REPAIRING
            reasons[t] = "incomplete window, not yet requested"
    doc = {"as_of": str(today), "quota": (plan.get("budget") or {}),
           "counts": {s: sum(1 for v in states.values() if v == s)
                      for s in sorted(set(states.values()))},
           "reasons": reasons, "states": states}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(doc, indent=2, sort_keys=True))
    return states


_FIELDS = ("open", "high", "low", "close", "turnover")


def _read_panels() -> dict:
    """Load the cached OHLC panels. Missing files -> empty frames (first-ever run)."""
    paths = {"open": OPENS, "high": HIGHS, "low": LOWS, "close": CLOSES,
             "turnover": TURNOVER}
    return {f: (pd.read_parquet(p) if p.exists() else pd.DataFrame())
            for f, p in paths.items()}


def _merge_bars(panels: dict, bars: dict) -> dict:
    """Fold {ticker: candle} into the cached panels as one dated row. Pure.

    An existing row for the same date is UPDATED, not duplicated — so re-running
    after the close correctly upgrades a partial bar to the settled one, and running
    twice in a day is a no-op rather than a corruption. New tickers widen the panel;
    tickers absent from `bars` keep their history and get NaN for the new date.
    """
    if not bars:
        return panels
    out = {}
    for f in _FIELDS:
        col = {t: c.get(f) for t, c in bars.items()}   # .get: turnover is optional
        dates = {pd.Timestamp(c["datetime"], unit="ms").normalize() for c in bars.values()}
        if len(dates) != 1:
            raise ValueError(f"snapshot spans {len(dates)} dates, expected 1: {sorted(dates)}")
        row = pd.DataFrame(col, index=[dates.pop()])
        base = panels.get(f)
        if base is None or base.empty:
            out[f] = row.sort_index()
            continue
        base = base.drop(index=row.index, errors="ignore")   # replace same-date row
        out[f] = pd.concat([base, row]).sort_index()
    return out


# MARKET_TZ is defined with the other module constants at the top — the cohort
# helpers above read it, so it cannot live down here any more.
# RTH closes 16:00 ET; give the feed a buffer to stamp the settled daily bar.
SETTLE_AFTER = dt.time(16, 15)


def _drop_unsettled_session(panels: dict, now_et: dt.datetime,
                            trading_day: bool | None = None) -> tuple[dict, str | None]:
    """Drop the current session's bar unless it is a REAL, settled trading day.

    Why this exists: we now pass `endDate` to Schwab (see `_try_pull`), which is
    the ONLY way to get today's bar at all — without it Schwab silently defaults
    `endDate` to the PREVIOUS trading day, so an 18:00 ET run ranked on
    yesterday's close (the 2026-07-23 regime-gate lag). But `endDate=now` during
    RTH returns a LIVE, partial bar whose `close` is just the last trade. Feeding
    that to momentum/the regime gate would rank the book on an intraday snapshot.

    So: before 16:15 ET, drop today's row (falls back to the old, correct-if-late
    behaviour); after it, keep it — that is the whole point of the fix.

    NON-TRADING DAYS (added 2026-08-10). The time-of-day test alone is not enough.
    On a Sunday the weekly 20:15 ET job passes "after 16:15" and the row was kept
    — but `get_market_snapshot` on a closed market returns the PREVIOUS session's
    close, so the row is a byte-identical duplicate of Friday stamped Sunday.
    Measured: 2026-08-02 and 2026-08-09 matched the prior Friday for 100% of 168
    names. Each one injects a 0% return, deflating sigma — and sigma sets stop
    distance, so stops end up tighter than the name's real volatility. Two rows
    cost only 0.05–0.23% today, but they accrue ~52/yr and never self-heal. Ten
    years of Schwab-era history contain none; this began with the moomoo feed.

    `trading_day`: True / False / None (calendar unavailable). The WEEKEND test is
    applied unconditionally and needs no network — Saturday and Sunday are never
    US trading days, so that half can never be wrong and works with OpenD down.
    `trading_day=False` additionally catches weekday holidays. Passing None
    degrades to weekend-only rather than dropping a real session: this panel is
    NON-REGENERABLE (history is capped at 100 distinct stocks account-wide), so
    wrongly discarding a genuine close is the expensive error.

    Returns (panels, dropped_date_iso | None). Pure: no network, no I/O, no clock.
    """
    close = panels.get("close")
    if close is None or close.empty:
        return panels, None
    last = close.index.max()
    today = now_et.date()
    if last.date() != today:
        return panels, None                      # nothing from today — nothing to drop
    if today.weekday() >= 5 or trading_day is False:
        # Market shut: whatever the feed returned is the prior session restamped.
        return ({f: p.drop(index=last, errors="ignore") for f, p in panels.items()},
                str(last.date()))
    if now_et.time() >= SETTLE_AFTER:
        return panels, None                      # settled close — keep it
    return {f: p.drop(index=last, errors="ignore") for f, p in panels.items()}, str(last.date())


def _field_panels(raw: dict) -> dict:
    """Turn {sym: [candle,...]} into one dates×tickers DataFrame per OHLC field.
    Pure: no network, no I/O. `close` panel is byte-for-byte what we cached before."""
    out = {f: {} for f in _FIELDS}
    for sym, candles in raw.items():
        for f in _FIELDS:
            out[f][sym] = pd.Series(
                # .get, NOT [f]: the BACKFILL path (the metered history API)
                # returns candles with no `turnover` key at all, and a KeyError
                # here would take down the whole fetch -- every panel, every
                # ticker -- over one optional field. Absent becomes NaN, which
                # is the truth: unknown, not zero.
                {pd.Timestamp(c["datetime"], unit="ms").normalize(): c.get(f)
                 for c in candles}
            )
    return {f: pd.DataFrame(series).sort_index() for f, series in out.items()}


def _repair_incomplete(panels: dict, tickers, ctx, mmp, mmr, today, dry=False,
                       cfg=None):
    """Give every universe member the history `momentum.compute()` needs — or
    establish that it is too young to have it. Returns (panels, plan).

    ⛔ WHY THIS RUNS ON THE DAILY PATH AND NOT IN universe_refresh. The refresh
    fires once a week (Fri 17:00); this runs before every ranking, so a name is
    repaired before the FIRST `slow_loop.py` that would have to score it, and a
    repair that fails on Friday is retried Monday instead of waiting a week. It
    also keeps the refresh doing one job — deciding MEMBERSHIP — rather than
    owning the price panel as well.

    Quota discipline, all of it load-bearing (see src/history_repair.py):
      - the window test runs FIRST, so a complete panel costs zero calls;
      - listing ages are fetched only when something is actually missing, and
        `get_stock_basicinfo` is UNMETERED — no history unit is ever spent to
        discover that a name is young;
      - names positively known to be too young are never requested, so a fresh
        listing cannot burn a unit a day fetching bars that do not exist;
      - re-requesting a symbol already inside moomoo's rolling window costs no
        additional quota (verified 2026-09-06: a second NVDA pull left
        used_quota unchanged), so a name whose repair fails is retried without
        compounding cost;
      - the request is capped by the SAME distinct-symbol quota the --backfill
        path uses, and the overflow is NAMED rather than silently dropped.

    ⛔ THE OUTCOME OF EVERY NAME IS PERSISTED, not merely printed. Returning the
    plan and logging it left the SCOREABILITY of the pool knowable only by
    re-deriving it from a parquet, so no other process — the order gate, the
    brief, the dashboard — could tell a name that is seasoning from one that is
    deferred from one whose provider request failed. `history_state.json` is
    that record, and src/cohort.py composes it with the eligibility artifact
    into the tri-state view every surface reads.
    """
    import history_repair as hr                        # noqa: PLC0415

    close = panels.get("close")
    # ⛔ REPAIR FIXES NAMES, IT DOES NOT REBUILD PANELS. If the panel itself is
    # shorter than one momentum window, EVERY name reads incomplete and this
    # would quietly spend the entire 100-symbol quota trying to rebuild from a
    # missing or truncated parquet. A rebuild is a deliberate `--backfill`, run
    # by a human who has looked at research_store/prices/backup/.
    if close is None or close.empty or len(close) < hr.REQUIRED_ROWS:
        rows = 0 if close is None or close.empty else len(close)
        print(f"  repair SKIPPED: panel has {rows} rows, fewer than the "
              f"{hr.REQUIRED_ROWS}-session window — that is a panel rebuild, "
              f"not a per-name gap. Use --backfill deliberately.")
        return panels, None

    need = hr.incomplete(close, tickers)
    if not need:
        return panels, None

    ages = mmr.listing_dates(need, ctx=ctx)
    if not ages:
        print("  ⚠️ listing dates unavailable — treating every age as UNKNOWN, "
              "which attempts a real backfill rather than assuming youth")

    # ⛔ THE ORDER IS A DECISION, NOT AN ACCIDENT (2026-09-09). `plan_repair`
    # used to take `repair[:quota]` off an alphabetical `sorted(set(...))`, which
    # is deterministic but expresses no priority at all — a HELD position could
    # be deferred behind three names beginning with 'A'. That was survivable
    # while the pool was 150 curated names and rarely more than a handful needed
    # repair; with a ~200-name eligibility cohort the deferral set is routine and
    # who gets deferred matters every week. src/quota_planner.py supplies the
    # order: held first, then whichever names are closest to scoreable, then
    # cohort rank. `missing_rows` is what makes "closest" measurable rather than
    # a guess — it is the actual shortfall against momentum.compute()'s window.
    #
    # ⛔ THE SEASONING SPLIT HAPPENS FIRST, THEN THE BUDGET. `plan_repair` is
    # asked for NO quota, so it separates "too young to have the window" from
    # "should have it and does not" using listing age alone; only the second
    # group is then rationed. Applying the budget first would let a young name —
    # which is never requested at all — consume a slot a mature name needed.
    #
    # ⛔ THE SEASONING SPLIT RUNS FIRST WITH NO QUOTA, THEN THE ONE AUTHORIZED
    # PATH RATIONS WHAT IS LEFT. A name too young to have bars must never
    # consume a slot a mature name needed, and quota accounting lives in exactly
    # one place for every metered call in this repo.
    plan = hr.plan_repair(close, need, ages, today, quota=None)
    start_d, end_d = hr.backfill_window(today)
    panels, res = authorized_history_fetch(
        panels, plan["repair"], cfg=cfg, ctx=ctx, mmp=mmp, today=today,
        start=start_d, end=end_d, held=_held_symbols(),
        missing_rows=_missing_rows(close, plan["repair"]),
        cohort_ranks=_cohort_ranks(cfg), dry=dry, label="repair")
    plan["repair"] = res["request"]
    plan["deferred"] = res["deferred"]
    plan["budget"] = res["budget"]
    plan["reasons"] = res["reasons"]
    plan["errors"] = res["errors"]

    if plan["seasoning"]:
        print(f"  seasoning ({len(plan['seasoning'])}): too young for a "
              f"{hr.REQUIRED_ROWS}-session window — intentionally unscoreable, "
              f"not a gap: " + " ".join(plan["seasoning"]))
    if plan["deferred"]:
        print(f"  DEFERRED ({len(plan['deferred'])}): " + " ".join(plan["deferred"])
              + " — NOT retried as new requests until distinct-symbol capacity "
                "is re-established by broker telemetry or an operator-confirmed "
                "reset; then in the same priority order")
    if not plan["repair"]:
        return panels, plan
    print(f"  repaired {len(plan['repair'])} name(s) via the history API "
          f"({start_d} .. {end_d})")
    for t in plan["repair"]:
        n = res["raw"].get(t, 0)
        ok = hr.window_complete(panels["close"][t]) if t in panels["close"] else False
        print(f"    {t:7s} {n:4d} bars -> {'scoreable' if ok else 'STILL INCOMPLETE'}"
              + (f"  ({str(plan['errors'][t])[:70]})" if t in plan["errors"] else ""))
    return panels, plan


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, metavar="DAYS",
                    help="gap-fill DAYS of history via request_history_kline. "
                         "Capped by moomoo at 100 DISTINCT stocks account-wide.")
    ap.add_argument("--dry", action="store_true", help="show the change, write nothing")
    ap.add_argument("--no-repair", action="store_true",
                    help="skip the automatic history repair of universe members "
                         "the signal cannot score (see _repair_incomplete)")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(REPO / "src"))
    from adapters.moomoo import prices as mmp          # noqa: PLC0415
    from adapters.moomoo import research as mmr        # noqa: PLC0415
    from adapters.moomoo.client import OpenDUnavailable, quote_ctx  # noqa: PLC0415

    import strategy                                    # noqa: PLC0415
    cfg = strategy.load()
    tickers = universe_tickers(cfg)
    panels = _read_panels()
    before = panels["close"].shape if not panels["close"].empty else (0, 0)
    print(f"cached panel: {before[0]} dates x {before[1]} tickers"
          + (f", latest {panels['close'].index.max().date()}" if before[0] else " (empty)"))

    try:
        ctx = quote_ctx()
    except OpenDUnavailable as e:
        print(f"ABORT: {e}")
        print(f"  cache LEFT INTACT: {CLOSES}")
        raise SystemExit(2)

    meta, errs, repair_plan = [], {}, None
    try:
        if args.backfill:
            # ⛔ A DELIBERATE OPERATOR ACTION IS NOT EXEMPT FROM THE BROKER'S
            # METER. This branch used to slice its candidate list at 100 and
            # call `daily_panel()` directly — no awareness of what the rolling
            # window had already spent, and NO LEDGER ENTRY afterwards, so its
            # spend was invisible to the next automatic repair, which then
            # planned as if the quota were untouched. `--backfill` keeps its own
            # CANDIDATE ORDERING (the operator chose these names and this
            # date range); it does not keep its own quota accounting.
            end_d = dt.datetime.now(MARKET_TZ).date()
            start_d = end_d - dt.timedelta(days=args.backfill)
            have = panels["close"]
            need = [t for t in tickers
                    if have.empty or t not in have.columns
                    or have[t].loc[str(start_d):].isna().any()]
            print(f"backfill: {len(need)} ticker(s) need bars, "
                  f"{start_d} .. {end_d} (history API)")
            # The operator's own ordering is preserved by handing it in as the
            # `cohort_ranks` tiebreak; held names still come first, because a
            # position the book cannot rank is a risk before it is an
            # inconvenience.
            panels, res = authorized_history_fetch(
                panels, need, cfg=cfg, ctx=ctx, mmp=mmp, today=end_d,
                start=str(start_d), end=str(end_d), held=_held_symbols(),
                cohort_ranks={t: i + 1 for i, t in enumerate(need)},
                dry=args.dry, label="backfill")
            errs = res["errors"]
            meta = [(t, n, "ok") for t, n in res["raw"].items()]
            got = len(res["raw"])
            wanted = len(res["request"])
            if res["deferred"]:
                print(f"  backfill DEFERRED {len(res['deferred'])} name(s) for "
                      f"want of distinct-symbol capacity: "
                      + " ".join(res["deferred"][:20])
                      + ("…" if len(res["deferred"]) > 20 else ""))
        else:
            bars, errs = mmp.snapshot_ohlc(tickers, ctx=ctx)
            got, wanted = len(bars), len(tickers)
            print(f"snapshot: {got}/{wanted} bars (1 call, no history quota)")
            # SAFETY GUARD (2026-07-15 incident, preserved): a systemic failure must
            # never be written over a good cache. A few dead names failing is normal;
            # a majority failing means OpenD/network is broken.
            if got < max(1, wanted // 2):
                common = Counter(errs.values()).most_common(1)
                print(f"\nABORT: only {got}/{wanted} tickers returned a bar — systemic "
                      f"failure, not a few dead names.\n  most common: "
                      f"{common[0][0] if common else 'unknown'}")
                print(f"  cache LEFT INTACT: {CLOSES}")
                raise SystemExit(2)
            panels = _merge_bars(panels, bars)
            meta = [(t, 1, "ok") for t in bars]
            # Repair AFTER the append, so today's bar is already in place and a
            # name that needs only today is not counted as needing history.
            if not args.no_repair:
                panels, repair_plan = _repair_incomplete(
                    panels, tickers, ctx, mmp, mmr,
                    dt.datetime.now(MARKET_TZ).date(), dry=args.dry, cfg=cfg)
        # Ask the calendar while the context is still open, so this costs no
        # extra connection. None = could not tell -> the weekend test still
        # applies; only weekday HOLIDAYS go unrecognised.
        now_et = dt.datetime.now(MARKET_TZ)
        trading_day = mmp.is_trading_day(now_et.date(), ctx=ctx)
    finally:
        ctx.close()

    panels, dropped = _drop_unsettled_session(panels, now_et, trading_day)
    if dropped:
        why = ("market CLOSED that day — the feed returns the prior session's "
               "close, so this row is a duplicate"
               if (now_et.date().weekday() >= 5 or trading_day is False)
               else f"before {SETTLE_AFTER.strftime('%H:%M')} ET — partial, not a close")
        print(f"  dropped session bar {dropped} ({why})")

    panel = panels["close"]
    print(f"panel now: {panel.shape[0]} dates x {panel.shape[1]} tickers, "
          f"{panel.index.min().date()} .. {panel.index.max().date()}")
    if errs:
        print(f"no bar ({len(errs)}): " + " ".join(sorted(errs)[:20])
              + (" ..." if len(errs) > 20 else ""))
        for t, e in list(errs.items())[:3]:
            print(f"    {t}: {str(e)[:100]}")

    if args.dry:
        print("[dry] nothing written")
        return

    field_to_path = {"open": OPENS, "high": HIGHS, "low": LOWS, "close": CLOSES,
                     "turnover": TURNOVER}
    for field, path in field_to_path.items():
        try:
            panels[field].to_parquet(path)
        except Exception as e:  # never lose a full pull to a missing parquet engine
            fallback = path.with_suffix(".csv")
            panels[field].to_csv(fallback)
            print(f"WARN parquet write failed for {field} ({e}); wrote CSV -> {fallback}")
    pd.DataFrame(meta, columns=["ticker", "candles", "status"]).to_csv(META, index=False)
    print(f"wrote {CLOSES} (+ opens/highs/lows/turnover)")

    # ⛔ THE SCOREABILITY RECORD, WRITTEN EVERY RUN. Without it, "can the signal
    # score this name" is answerable only by re-deriving the window test from a
    # parquet, which the order gate (stdlib-only, latency-budgeted) and the
    # dashboard cannot do. `research_store/universe/history_state.json` is what
    # src/cohort.py composes with the weekly eligibility artifact to produce the
    # scoreable / pending / unscoreable view every surface reads.
    #
    # It covers the TRADEABLE names only -- the sector factors and SPY are
    # regression inputs and a regime observation, never candidates, so recording
    # a scoreability verdict for them would invite a reader to treat them as
    # ones. Same reasoning as their absence from the ranked candidate list.
    try:
        import residual                                    # noqa: PLC0415
        factors = set(residual.SECTOR_FACTORS) | {"SPY"}
        _cohort_path, hstate_path = _cohort.paths(cfg, REPO)
        states = write_history_state(
            panels, [t for t in tickers if t not in factors], repair_plan,
            hstate_path, dt.datetime.now(MARKET_TZ).date())
        counts = {}
        for v in states.values():
            counts[v] = counts.get(v, 0) + 1
        print(f"history state -> {hstate_path.name}: "
              + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    except Exception as e:                                 # noqa: BLE001
        # Never lose a good price write to a bookkeeping failure. An absent
        # history state composes to "nothing is scoreable", which REFUSES buys
        # rather than authorising them -- see cohort.load_history_state.
        print(f"WARN history state not written ({type(e).__name__}: {e}) — "
              f"nothing will read as scoreable until it is")


def _selftest() -> None:
    raw = {
        "AAA": [
            {"datetime": 1609459200000, "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5},
            {"datetime": 1609545600000, "open": 10.5, "high": 12.0, "low": 10.0, "close": 11.8},
        ],
        "BBB": [
            {"datetime": 1609459200000, "open": 20.0, "high": 21.0, "low": 19.0, "close": 20.5},
        ],
    }
    panels = _field_panels(raw)
    assert set(panels) == {"open", "high", "low", "close", "turnover"}, panels.keys()
    # close panel preserves the pre-refactor content/shape
    assert panels["close"].loc[panels["close"].index[1], "AAA"] == 11.8
    assert panels["high"].loc[panels["high"].index[0], "AAA"] == 11.0
    assert panels["low"].loc[panels["low"].index[0], "BBB"] == 19.0
    # sorted by date, aligned index across tickers
    assert list(panels["close"].index) == sorted(panels["close"].index)
    print("selftest OK: _field_panels open/high/low/close")

    # --- unsettled-session guard (2026-07-27 regime-lag fix) -------------------
    def _panels_ending(day: str) -> dict:
        idx = pd.to_datetime([pd.Timestamp(day) - pd.Timedelta(days=1), pd.Timestamp(day)])
        return {f: pd.DataFrame({"AAA": [1.0, 2.0]}, index=idx) for f in _FIELDS}

    def _et(day: str, h: int, m: int = 0) -> dt.datetime:
        return dt.datetime.combine(dt.date.fromisoformat(day), dt.time(h, m), tzinfo=MARKET_TZ)

    D = "2026-07-27"
    # mid-session: today's bar is a LIVE partial -> must be dropped
    p, dropped = _drop_unsettled_session(_panels_ending(D), _et(D, 10, 24))
    assert dropped == D, f"expected partial {D} bar dropped, got {dropped}"
    assert p["close"].index.max().date() == dt.date(2026, 7, 26), p["close"].index
    for f in _FIELDS:                                    # every field, not just close
        assert len(p[f]) == 1, (f, p[f])
    # after settle: today's bar is the real close -> must be KEPT (the whole fix)
    p, dropped = _drop_unsettled_session(_panels_ending(D), _et(D, 18, 3))
    assert dropped is None and p["close"].index.max().date() == dt.date(2026, 7, 27)
    # boundary is inclusive at 16:15
    _, dropped = _drop_unsettled_session(_panels_ending(D), _et(D, 16, 15))
    assert dropped is None, "16:15 ET must count as settled"
    _, dropped = _drop_unsettled_session(_panels_ending(D), _et(D, 16, 14))
    assert dropped == D, "16:14 ET is still unsettled"
    # panel that doesn't reach today (weekend/holiday run) -> untouched
    _, dropped = _drop_unsettled_session(_panels_ending("2026-07-24"), _et(D, 10, 0))
    assert dropped is None, "no bar for today -> nothing to drop"
    # empty panel must not explode
    _, dropped = _drop_unsettled_session({f: pd.DataFrame() for f in _FIELDS}, _et(D, 10, 0))
    assert dropped is None

    # --- non-trading days (2026-08-10) --------------------------------------
    # A Sunday 20:15 ET run passes the settle test but the market was SHUT, so
    # the feed returned Friday's close restamped. Must drop, calendar or not.
    SUN, SAT = "2026-08-09", "2026-08-08"
    for day in (SUN, SAT):
        _, dropped = _drop_unsettled_session(_panels_ending(day), _et(day, 20, 15))
        assert dropped == day, f"{day} is a weekend — must drop even after settle"
        # ...and the weekend test must not need the calendar to be right
        _, dropped = _drop_unsettled_session(_panels_ending(day), _et(day, 20, 15),
                                             trading_day=None)
        assert dropped == day, f"{day} must drop with no calendar available"
        # ...nor may a wrong calendar answer override it
        _, dropped = _drop_unsettled_session(_panels_ending(day), _et(day, 20, 15),
                                             trading_day=True)
        assert dropped == day, f"{day} must drop even if the calendar says otherwise"

    # A weekday HOLIDAY is only caught when the calendar says so.
    HOL = "2026-12-25"                                   # Friday, market closed
    assert dt.date.fromisoformat(HOL).weekday() < 5, "fixture must be a weekday"
    _, dropped = _drop_unsettled_session(_panels_ending(HOL), _et(HOL, 20, 15),
                                         trading_day=False)
    assert dropped == HOL, "a weekday holiday must drop when the calendar says closed"
    _, dropped = _drop_unsettled_session(_panels_ending(HOL), _et(HOL, 20, 15),
                                         trading_day=None)
    assert dropped is None, ("unknown calendar must NOT discard a weekday bar — "
                            "this panel is non-regenerable")

    # A normal settled weekday is still KEPT (the original fix must survive).
    _, dropped = _drop_unsettled_session(_panels_ending(D), _et(D, 18, 3),
                                         trading_day=True)
    assert dropped is None, "a real settled session must still be kept"
    print("selftest OK: _drop_unsettled_session (partial dropped, settled kept, "
          "weekend/holiday duplicates dropped)")

    # --- _merge_bars: the append must never corrupt the 10y panel ---------------
    idx = pd.to_datetime(["2026-07-27", "2026-07-28"])
    cached = {f: pd.DataFrame({"AAA": [1.0, 2.0], "BBB": [10.0, 20.0]}, index=idx)
              for f in _FIELDS}
    ms = int(pd.Timestamp("2026-07-29").timestamp() * 1000)
    bars = {"AAA": {"datetime": ms, "open": 3.0, "high": 3.5, "low": 2.5, "close": 3.2},
            "CCC": {"datetime": ms, "open": 9.0, "high": 9.5, "low": 8.5, "close": 9.2}}
    m = _merge_bars(cached, bars)
    c = m["close"]
    assert len(c) == 3, f"one new dated row, got {len(c)}"
    assert c.loc["2026-07-29", "AAA"] == 3.2, c
    assert c.loc["2026-07-28", "AAA"] == 2.0, "history must be preserved verbatim"
    assert pd.isna(c.loc["2026-07-29", "BBB"]), "ticker with no bar -> NaN, not stale carry"
    assert pd.isna(c.loc["2026-07-27", "CCC"]), "new ticker gets NaN history, not backfill"
    assert c.loc["2026-07-28", "BBB"] == 20.0, "existing ticker history intact"
    assert list(c.index) == sorted(c.index), "index must stay sorted"
    for f in _FIELDS:                                   # all four panels, not just close
        assert m[f].shape == (3, 3), (f, m[f].shape)

    # re-running the same day UPDATES the row (partial -> settled), never duplicates
    bars2 = dict(bars); bars2["AAA"] = {**bars["AAA"], "close": 3.9}
    m2 = _merge_bars(m, bars2)
    assert len(m2["close"]) == 3, f"same-date re-run must not duplicate, got {len(m2['close'])}"
    assert m2["close"].loc["2026-07-29", "AAA"] == 3.9, "settled close must overwrite partial"

    # empty bars (OpenD returned nothing) must be a no-op, never a wipe
    assert _merge_bars(cached, {})["close"].equals(cached["close"]), "empty -> untouched"

    # a mixed-date snapshot means something is wrong upstream; refuse it
    try:
        _merge_bars(cached, {"AAA": {**bars["AAA"]},
                             "CCC": {**bars["CCC"],
                                     "datetime": int(pd.Timestamp("2026-07-30").timestamp() * 1000)}})
        raise AssertionError("mixed-date snapshot must raise")
    except ValueError as e:
        assert "spans 2 dates" in str(e), e

    # first-ever run: empty cache + bars -> a valid one-row panel
    fresh = _merge_bars({f: pd.DataFrame() for f in _FIELDS}, bars)
    assert fresh["close"].shape == (1, 2), fresh["close"].shape
    print("selftest OK: _merge_bars (append, same-date update, no-op, mixed-date guard)")
    # ---- turnover rides along, and its ABSENCE must not break the fetch ----
    # turnover comes free in the daily snapshot response (it was discarded), but
    # the BACKFILL path uses the metered history API, whose candles have no such
    # key. A `c[f]` lookup there raised KeyError and took down every panel for
    # every ticker over one optional field.
    hist_only = {"AAA": [{"datetime": 1_700_000_000_000, "open": 1.0, "high": 2.0,
                          "low": 0.5, "close": 1.5}]}          # NO turnover key
    pt = _field_panels(hist_only)
    assert set(pt) == {"open", "high", "low", "close", "turnover"}, pt.keys()
    assert pt["close"].iloc[0, 0] == 1.5
    assert pd.isna(pt["turnover"].iloc[0, 0]), "absent turnover must be NaN, not 0"

    # ...and when the snapshot DOES carry it, it lands in the panel
    with_tv = {"BBB": [{"datetime": 1_700_000_000_000, "open": 1.0, "high": 2.0,
                        "low": 0.5, "close": 1.5, "turnover": 9_000_000.0}]}
    assert _field_panels(with_tv)["turnover"].iloc[0, 0] == 9_000_000.0

    # the APPEND path (_merge_bars) must tolerate it too -- same optional field,
    # a different consumer, and it had the same `c[f]` lookup
    merged = _merge_bars({}, {"AAA": {"datetime": 1_700_000_000_000, "open": 1.0,
                                      "high": 2.0, "low": 0.5, "close": 1.5}})
    assert pd.isna(merged["turnover"].iloc[0, 0]), "append path must tolerate no turnover"
    merged2 = _merge_bars({}, {"AAA": {"datetime": 1_700_000_000_000, "open": 1.0,
                                       "high": 2.0, "low": 0.5, "close": 1.5,
                                       "turnover": 5.0}})
    assert merged2["turnover"].iloc[0, 0] == 5.0, merged2["turnover"]
    print("selftest OK: turnover panel rides along free, and a candle without it "
          "yields NaN instead of killing the whole fetch")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
