"""READ-ONLY behavioural MEASUREMENT over the lifecycle/decision corpus — pure.

WHAT THIS IS
    The first measurement layer over the forward-only corpus built by
    src/position_lifecycle.py (identity), src/lifecycle_journal.py (recording),
    agent_env.decide.decision_entry (stamping) and src/lifecycle_outcomes.py
    (projection). It answers three OBSERVATIONAL questions for a HUMAN:

        what discretionary actions did the agent take while holding real positions
        what did the market do afterwards
        is there anything here worth studying

⛔ WHAT THIS IS NOT, AND MUST NOT BECOME
    It does not score, rank, label, reward, or advise. There is no notion here of
    a decision being right, wrong, good, bad, successful or optimal. A trim
    followed by a rise is not an error and a hold followed by a rise is not a
    skill: this module has no model of the risk the agent was managing, of the
    path between two closes, or of the alternative it declined. It reports what
    happened and stops.

    It is also not wired to anything. Nothing imports it; it appears in no MCP
    tool, no brief, no research_log, no prompt, no charter, no service, no timer.
    The only consumer is scripts/behavior_report.py, which a human runs. If a
    later step wants any of this in front of the agent, that is a separate,
    deliberate decision — not a side effect of this file existing.

WHY NOT REUSE score_reviews.py
    That script exists to SETTLE a disagreement between the agent and its
    reviewer, so by design it collapses every action into a direction
    (`REDUCE`/`INCREASE`), picks ONE horizon, and emits RIGHT/WRONG. All three
    are the assumptions this corpus was built to stop inheriting: the direction
    map invents intent for ambiguous strings, one horizon silently encodes a
    reward function, and RIGHT/WRONG is the verdict we are explicitly not issuing
    yet. It also keys off nothing lifecycle-related. Nothing from it is imported
    and it is left untouched.

WHAT COUNTS AS A MEASURABLE DECISION
    Exactly one thing: an `agent_decision` carrying a `position_id` that matches
    a lifecycle in the same stream, by exact string equality — the join
    `lifecycle_outcomes` already performs and the only one anyone here is allowed
    to perform. An unstamped decision (every historical one) is not in the corpus
    and is not inferred into it by symbol or by date. Its absence is COUNTED, in
    coverage, so an empty corpus can never read as a clean one.

NULL IS A MEASUREMENT, ZERO IS NOT
    Every horizon cell is one of three states and says which:
        measured     — a real number
        pending      — the sessions have not happened yet; ask again later
        unavailable  — the panel cannot price it; asking again will not help
    A missing return is never 0.0, and never omitted from the row.

PURITY
    No I/O, no clock, no journal write, no broker call. The caller supplies the
    journal, the panel and (optionally) an `as_of` cutoff. Same input -> same
    output. `panel_from_frame` is the one pandas-touching function and it only
    reshapes a DataFrame the caller already loaded.
"""
from __future__ import annotations

import bisect
import statistics
import math
from datetime import datetime, time, timezone

import lifecycle_outcomes as _outcomes

MEASUREMENT_SCHEMA = 1

#: Horizons measured, in PANEL SESSIONS (trading days), not calendar days.
#: Several on purpose: one horizon is a reward function with the argument left
#: out. Reporting the path is the point.
HORIZONS = (1, 5, 10, 20)

MEASURED = "measured"
PENDING = "pending"
UNAVAILABLE = "unavailable"

#: Regular-session close in market local time. A decision recorded at or after
#: it could already see that day's close, so that close cannot be its baseline.
MARKET_CLOSE = time(16, 0)
MARKET_TZ = "America/New_York"


# --------------------------------------------------------------------------- #
# analytical grouping — ANALYTICS-LOCAL, EXPOSURE DIRECTION ONLY
# --------------------------------------------------------------------------- #
# ⛔ THIS IS NOT AN ACTION ONTOLOGY AND MUST NOT LEAK.
#   * The writer is untouched: `decide.decision_entry` still records whatever
#     string the agent used, and this table never travels back to it.
#   * `raw_action` is preserved on every row and every aggregate beside the
#     group, so any reader can ignore the grouping entirely and regroup.
#   * Membership is EXACT-MATCH on the lowercased string. No prefix rule, no
#     stemming, no fuzzy match: "skip_trim" is not read as a trim, and
#     "hold_theme" is not read as a hold, because neither is unambiguous about
#     what happened to the position.
#   * The ONLY question the table answers is which way the position's exposure
#     moved. It says nothing about intent, conviction or correctness — a `trim`
#     and an `exit` are both REDUCE and are otherwise unlike each other.
#   * Anything not listed is `unclassified`, which is reported and never dropped.
GROUP_INCREASE = "increase_exposure"
GROUP_REDUCE = "reduce_exposure"
GROUP_HOLD = "hold_exposure"
GROUP_RISK_LEVEL = "risk_level"
GROUP_UNCLASSIFIED = "unclassified"

_EXPOSURE_GROUP = {
    "open": GROUP_INCREASE,
    "add": GROUP_INCREASE,
    "buy": GROUP_INCREASE,
    "trim": GROUP_REDUCE,
    "exit": GROUP_REDUCE,
    "sell": GROUP_REDUCE,
    "hold": GROUP_HOLD,
    "set_level": GROUP_RISK_LEVEL,
    "clear_level": GROUP_RISK_LEVEL,
    "tighten_stop": GROUP_RISK_LEVEL,
    "tighten_stops": GROUP_RISK_LEVEL,
}


def analytical_group(raw_action) -> str:
    """Exposure direction of one raw action string, or `unclassified`. Pure."""
    if not isinstance(raw_action, str):
        return GROUP_UNCLASSIFIED
    return _EXPOSURE_GROUP.get(raw_action.strip().lower(), GROUP_UNCLASSIFIED)


# --------------------------------------------------------------------------- #
# the price panel, as this module wants it
# --------------------------------------------------------------------------- #
def panel_from_frame(frame) -> dict:
    """DataFrame (sessions x tickers of closes) -> the panel this module reads.

        {"sessions": ["YYYY-MM-DD", ...ascending], "series": {SYM: [close|None]}}

    Reshape only: no fill, no interpolation, no resample, no drop. A NaN in the
    frame becomes None here and is reported later as `unavailable`, never as a
    carried-forward price — a made-up close would silently invent a return.
    """
    if frame is None or len(getattr(frame, "columns", [])) == 0:
        return {"sessions": [], "series": {}}

    index = list(frame.index)
    sessions = [str(getattr(d, "date", lambda: d)())[:10] for d in index]
    order = sorted(range(len(sessions)), key=lambda i: sessions[i])
    sessions_sorted = [sessions[i] for i in order]

    series = {}
    for column in frame.columns:
        values = list(frame[column].values)
        out = []
        for i in order:
            v = values[i]
            try:
                f = float(v)
            except (TypeError, ValueError):
                out.append(None)
                continue
            out.append(None if math.isnan(f) or math.isinf(f) else f)
        series[str(column).strip().upper()] = out
    return {"sessions": sessions_sorted, "series": series}


# --------------------------------------------------------------------------- #
# when a decision happened, in sessions
# --------------------------------------------------------------------------- #
def _market_datetime(ts):
    """ISO timestamp -> market-local datetime, or None. Pure; reads no clock.

    A naive stamp is read as UTC, which is what the writer produces; the
    conversion matters because a decision at 00:30Z belongs to the PREVIOUS
    trading day in New York, and pricing it a day late would be a real error.
    """
    if not isinstance(ts, str) or not ts.strip():
        return None
    text = ts.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return parsed.astimezone(ZoneInfo(MARKET_TZ))
    except Exception:
        # No tz database: fall back to a fixed -4/-5h offset would be a guess.
        # UTC is what we actually know, and the caller sees the resulting
        # session date, so state it rather than pretend to a local calendar.
        return parsed


def baseline_session_index(sessions, ts):
    """(index, reason, status) of the first close that is AFTER the decision. Pure.

    THE BASELINE IS THE FIRST CLOSE THE DECISION COULD NOT HAVE SEEN.
      * decided before 16:00 market time -> that same date's close, if the panel
        has it (the live sessions run 10:35 and 15:15, both mid-session);
      * decided at or after 16:00, or on a non-session day -> the next session's
        close.
    Measuring forward from an earlier close would fold in a move that happened
    BEFORE the decision and attribute it to the decision. Measuring from the
    decision's own intraday price is not possible from a daily panel and is not
    faked; the cost is that the remainder of the decision session is excluded,
    which is stated rather than hidden.

    The third element describes an ABSENT baseline and is None when one was
    found. It exists so the caller never has to infer the kind of absence from
    the wording of the reason: PENDING when the session simply has not happened
    yet (the panel will supply it later), UNAVAILABLE when waiting cannot help.
    """
    when = _market_datetime(ts)
    if when is None:
        return None, "decision ts is not a parseable timestamp", UNAVAILABLE
    if not sessions:
        return None, "panel has no sessions", UNAVAILABLE

    day = when.date().isoformat()
    if when.timetz().replace(tzinfo=None) >= MARKET_CLOSE:
        # strictly after this date
        i = bisect.bisect_right(sessions, day)
    else:
        i = bisect.bisect_left(sessions, day)
    if i >= len(sessions):
        return (None,
                f"no panel session after the decision ({day}); panel ends {sessions[-1]}",
                PENDING)
    return i, None, None


# --------------------------------------------------------------------------- #
# one forward return
# --------------------------------------------------------------------------- #
def _cell(status, value=None, reason=None) -> dict:
    return {"status": status, "value": value, "reason": reason}


def forward_return(panel, symbol, base_index, horizon, last_index) -> dict:
    """One horizon cell: measured / pending / unavailable. Pure.

        value = close[base_index + horizon] / close[base_index] - 1

    `last_index` bounds what has elapsed. Beyond it the answer is PENDING: the
    sessions have not happened (or the panel has not caught up), and both are
    "ask later", never 0.
    """
    series = panel["series"].get(str(symbol or "").strip().upper())
    if series is None:
        return _cell(UNAVAILABLE, reason=f"{symbol} is not a column in the panel")

    target = base_index + horizon
    if target > last_index:
        return _cell(PENDING, reason=f"needs session #{target}; measurable through #{last_index}")

    base = series[base_index] if base_index < len(series) else None
    end = series[target] if target < len(series) else None
    if base is None:
        return _cell(UNAVAILABLE, reason=f"no close for {symbol} on {panel['sessions'][base_index]}")
    if base <= 0:
        return _cell(UNAVAILABLE, reason=f"non-positive baseline close for {symbol}")
    if end is None:
        return _cell(UNAVAILABLE, reason=f"no close for {symbol} on {panel['sessions'][target]}")
    return _cell(MEASURED, value=end / base - 1.0)


# --------------------------------------------------------------------------- #
# one decision row
# --------------------------------------------------------------------------- #
def _row(decision, lifecycle, panel, last_index, horizons) -> dict:
    raw_action = decision.get("action")
    symbol = decision.get("symbol") or lifecycle.get("symbol")
    base_index, base_reason, base_status = baseline_session_index(
        panel["sessions"], decision.get("ts"))

    cells = {}
    if base_index is None:
        # No baseline: an unparseable ts can never be priced, a not-yet-existing
        # session can. `baseline_session_index` already made that distinction; it
        # is carried verbatim into every cell rather than re-derived here.
        for h in horizons:
            cells[h] = _cell(base_status, reason=base_reason)
    else:
        for h in horizons:
            cells[h] = forward_return(panel, symbol, base_index, h, last_index)

    statuses = {c["status"] for c in cells.values()}
    if MEASURED in statuses:
        measurement_status = MEASURED
    elif PENDING in statuses:
        measurement_status = PENDING
    else:
        measurement_status = UNAVAILABLE

    row = {
        "ts": decision.get("ts"),
        "symbol": symbol,
        "raw_action": raw_action,
        # Derived HERE, never written back to the journal or shown to the agent.
        "analytical_group": analytical_group(raw_action),
        "position_id": lifecycle.get("position_id"),

        "lifecycle_status": lifecycle.get("status"),
        "lifecycle_origin": lifecycle.get("origin"),

        # ⛔ CONTEXT, NOT ATTRIBUTION. These describe the POSITION the decision
        # was made inside — a lifecycle carrying several decisions repeats the
        # same pair on each of its rows. Nothing here claims this decision caused
        # that P&L, and summing these across rows would double-count a position.
        "lifecycle_realized_pnl_pct": lifecycle.get("realized_pnl_pct"),
        "lifecycle_holding_days": lifecycle.get("holding_days"),

        "baseline_session": panel["sessions"][base_index] if base_index is not None else None,
        "measurement_status": measurement_status,
    }
    for h in horizons:
        row[f"return_{h}d"] = cells[h]["value"]
    row["horizons"] = {f"{h}d": cells[h] for h in horizons}
    row["missing_reasons"] = sorted({c["reason"] for c in cells.values() if c["reason"]})
    return row


# --------------------------------------------------------------------------- #
# aggregates — descriptive only
# --------------------------------------------------------------------------- #
def _summarise(values) -> dict:
    """N with mean and median. N FIRST AND ALWAYS, including N=0 and N=1.

    No confidence interval, no significance, no adjective. A reader who wants to
    know whether four observations mean anything can see that there are four.
    """
    vals = [v for v in values if isinstance(v, (int, float))]
    if not vals:
        return {"n": 0, "mean": None, "median": None}
    return {"n": len(vals),
            "mean": statistics.fmean(vals),
            "median": statistics.median(vals)}


def _distribution(values) -> dict:
    vals = [v for v in values if isinstance(v, (int, float))]
    base = _summarise(vals)
    base["min"] = min(vals) if vals else None
    base["max"] = max(vals) if vals else None
    return base


def _counts(items) -> dict:
    out: dict = {}
    for item in items:
        key = item if isinstance(item, str) else repr(item)
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def _by_key_returns(rows, key, horizons) -> list:
    groups: dict = {}
    for row in rows:
        groups.setdefault(row.get(key), []).append(row)
    out = []
    for value, members in sorted(groups.items(), key=lambda kv: (-len(kv[1]), str(kv[0]))):
        entry = {key: value, "n_decisions": len(members)}
        if key == "raw_action":
            entry["analytical_group"] = analytical_group(value)
        for h in horizons:
            entry[f"{h}d"] = _summarise(r[f"return_{h}d"] for r in members)
        out.append(entry)
    return out


# --------------------------------------------------------------------------- #
# the measurement
# --------------------------------------------------------------------------- #
def measure(journal, panel, *, as_of=None, horizons=HORIZONS) -> dict:
    """Journal + panel -> the measurement object. Pure; no clock, no I/O.

    `as_of` (an ISO date) only ever SHRINKS what counts as elapsed; it does not
    reach into the panel. Absent, the panel's own last session is the frontier —
    which is why this function needs no clock: what has elapsed is a fact about
    the data, not about now.
    """
    horizons = tuple(horizons)
    projection = _outcomes.project(journal)
    sessions = panel["sessions"]

    if not sessions:
        last_index = -1
    elif as_of:
        last_index = bisect.bisect_right(sessions, str(as_of)) - 1
    else:
        last_index = len(sessions) - 1

    rows = []
    for lifecycle in projection["lifecycles"]:
        for decision in lifecycle["decisions"]:
            # ONE ROW PER DECISION. Several decisions inside one lifecycle stay
            # several rows: collapsing them to a per-position verdict would erase
            # the sequence, which is the behaviour we are trying to observe.
            rows.append(_row(decision, lifecycle, panel, last_index, horizons))

    all_decisions = [e for e in (journal or [])
                     if isinstance(e, dict) and e.get("event") == _outcomes.DECISION_EVENT]
    stamped = [e for e in all_decisions if _outcomes._decision_position_id(e) is not None]

    measured_rows = [r for r in rows if r["measurement_status"] == MEASURED]

    coverage = {
        # Everything the agent recorded, whether or not this corpus can see it.
        "total_agent_decisions": len(all_decisions),
        "stamped_with_position_id": len(stamped),
        # Not a gap to be filled: the stamp is forward-only and every decision
        # written before Step 3 is permanently outside this corpus, by contract.
        "unstamped_decisions": len(all_decisions) - len(stamped),
        "lifecycle_linked_decisions": len(rows),
        # Stamped, but naming a lifecycle this stream does not contain.
        "orphan_stamped_decisions": len(projection["orphan_decisions"]),
        "unreadable_lifecycle_events": len(projection["unreadable"]),
        "priceable_decisions": len(measured_rows),
        "pending_decisions": sum(1 for r in rows if r["measurement_status"] == PENDING),
        "unpriceable_decisions": sum(1 for r in rows if r["measurement_status"] == UNAVAILABLE),
        "panel_sessions": len(sessions),
        "panel_last_session": sessions[-1] if sessions else None,
        "measured_through_session": sessions[last_index] if 0 <= last_index < len(sessions) else None,
        "by_horizon": {
            f"{h}d": {
                "measured": sum(1 for r in rows if r["horizons"][f"{h}d"]["status"] == MEASURED),
                "pending": sum(1 for r in rows if r["horizons"][f"{h}d"]["status"] == PENDING),
                "unavailable": sum(1 for r in rows if r["horizons"][f"{h}d"]["status"] == UNAVAILABLE),
            } for h in horizons},
    }

    lifecycles = projection["lifecycles"]
    complete = [lc for lc in lifecycles if lc["status"] == _outcomes.STATUS_COMPLETE]
    lifecycle_context = {
        "lifecycles": len(lifecycles),
        "by_status": _counts(lc["status"] for lc in lifecycles),
        "by_origin": _counts(str(lc.get("origin")) for lc in lifecycles),
        "completed": len(complete),
        # ⛔ Over COMPLETED lifecycles only, and only those that stated a number.
        # `completed` is a structural verdict; an adopted position closed without
        # an identifiable sell is complete and has no P&L at all.
        "realized_pnl_pct": _distribution(lc.get("realized_pnl_pct") for lc in complete),
        "realized_pnl_pct_null": sum(1 for lc in complete
                                     if not isinstance(lc.get("realized_pnl_pct"), (int, float))),
        "holding_days": _distribution(lc.get("holding_days") for lc in complete),
        "holding_days_null": sum(1 for lc in complete
                                 if not isinstance(lc.get("holding_days"), (int, float))),
        "decisions_per_lifecycle": _counts(str(lc["decision_count"]) for lc in lifecycles),
    }

    return {
        "measurement_schema": MEASUREMENT_SCHEMA,
        "outcome_schema": projection["outcome_schema"],
        "horizons": list(horizons),
        "as_of": as_of,
        "coverage": coverage,
        "raw_action_distribution": _counts(r["raw_action"] for r in rows),
        # Context for what the corpus CANNOT see, so an empty corpus is legible.
        "unlinked_raw_action_distribution": _counts(
            e.get("action") for e in all_decisions
            if _outcomes._decision_position_id(e) is None),
        "analytical_group_distribution": _counts(r["analytical_group"] for r in rows),
        "forward_returns_by_raw_action": _by_key_returns(rows, "raw_action", horizons),
        "forward_returns_by_analytical_group": _by_key_returns(rows, "analytical_group", horizons),
        "lifecycle_context": lifecycle_context,
        # The detailed layer. Aggregates above are recomputable from it alone.
        "decisions": rows,
    }


# --------------------------------------------------------------------------- #
# rendering — plain text, for a human
# --------------------------------------------------------------------------- #
def _pct(value):
    return "     —" if not isinstance(value, (int, float)) else f"{value * 100:+6.2f}%"


def render(measurement, *, limit=None) -> str:
    """The measurement as operator-readable text. Pure; no verdicts, no advice."""
    m = measurement
    cov = m["coverage"]
    horizons = m["horizons"]
    out = []
    add = out.append

    add("BEHAVIOURAL MEASUREMENT — lifecycle-linked agent decisions")
    add("READ-ONLY. Descriptive. No decision here is labelled right or wrong,")
    add("and nothing in this report is shown to the trading agent.")
    add("")
    add("A. COVERAGE")
    add(f"  agent decisions in journal      {cov['total_agent_decisions']}")
    add(f"    stamped with position_id      {cov['stamped_with_position_id']}")
    add(f"    unstamped (pre-Step-3)        {cov['unstamped_decisions']}   [permanently outside this corpus]")
    add(f"  lifecycle-linked (the corpus)   {cov['lifecycle_linked_decisions']}")
    add(f"    orphan stamped decisions      {cov['orphan_stamped_decisions']}")
    add(f"    unreadable lifecycle events   {cov['unreadable_lifecycle_events']}")
    add(f"  of the corpus: priceable        {cov['priceable_decisions']}")
    add(f"                 pending          {cov['pending_decisions']}")
    add(f"                 unpriceable      {cov['unpriceable_decisions']}")
    add(f"  panel: {cov['panel_sessions']} sessions, last {cov['panel_last_session']}, "
        f"measured through {cov['measured_through_session']}")
    add("  per horizon (measured / pending / unavailable):")
    for h in horizons:
        cell = cov["by_horizon"][f"{h}d"]
        add(f"    {h:>3}d   {cell['measured']:>4} / {cell['pending']:>4} / {cell['unavailable']:>4}")
    add("")

    add("B. RAW ACTION DISTRIBUTION (corpus)")
    if not m["raw_action_distribution"]:
        add("  (no lifecycle-linked decisions)")
    for action, n in m["raw_action_distribution"].items():
        add(f"  {action:<24} {n:>4}   [{analytical_group(action)}]")
    add("")
    add("   raw actions NOT in the corpus (unstamped decisions, for context):")
    unlinked = m["unlinked_raw_action_distribution"]
    if not unlinked:
        add("     (none)")
    for action, n in list(unlinked.items()):
        add(f"     {action:<22} {n:>4}")
    add("")

    add("C. FORWARD RETURNS BY RAW ACTION  (N shown for every cell; N is the number")
    add("   of decisions whose horizon has ELAPSED, not the number of decisions)")
    header = f"  {'raw_action':<24}{'dec':>5}"
    for h in horizons:
        header += f"   {str(h) + 'd  N/mean/median':>26}"
    add(header)
    if not m["forward_returns_by_raw_action"]:
        add("  (nothing to measure)")
    for entry in m["forward_returns_by_raw_action"]:
        line = f"  {str(entry['raw_action']):<24}{entry['n_decisions']:>5}"
        for h in horizons:
            s = entry[f"{h}d"]
            line += f"   {s['n']:>3} {_pct(s['mean'])} {_pct(s['median'])}"
        add(line)
    add("")
    add("   by analytical group (exposure direction only; derived in this module,")
    add("   never written back to the journal or shown to the agent):")
    for entry in m["forward_returns_by_analytical_group"]:
        line = f"  {str(entry['analytical_group']):<24}{entry['n_decisions']:>5}"
        for h in horizons:
            s = entry[f"{h}d"]
            line += f"   {s['n']:>3} {_pct(s['mean'])} {_pct(s['median'])}"
        add(line)
    add("")

    ctx = m["lifecycle_context"]
    add("D. LIFECYCLE CONTEXT  (about the POSITIONS the decisions sat inside —")
    add("   NOT the result of any individual decision)")
    add(f"  lifecycles              {ctx['lifecycles']}   by status {ctx['by_status'] or '{}'}")
    add(f"  by origin               {ctx['by_origin'] or '{}'}")
    add(f"  completed               {ctx['completed']}")
    pnl = ctx["realized_pnl_pct"]
    add(f"  realized_pnl_pct        N={pnl['n']} (null {ctx['realized_pnl_pct_null']})  "
        f"mean {_pct(pnl['mean'])}  median {_pct(pnl['median'])}  "
        f"min {_pct(pnl['min'])}  max {_pct(pnl['max'])}")
    hold = ctx["holding_days"]
    add(f"  holding_days            N={hold['n']} (null {ctx['holding_days_null']})  "
        f"mean {hold['mean']}  median {hold['median']}  min {hold['min']}  max {hold['max']}")
    add(f"  decisions per lifecycle {ctx['decisions_per_lifecycle'] or '{}'}")
    add("")

    rows = m["decisions"]
    shown = rows if limit is None else rows[:limit]
    add(f"E. PER-DECISION DETAIL ({len(shown)} of {len(rows)})")
    if shown:
        add(f"  {'ts':<20}{'sym':<7}{'raw_action':<18}{'life':<9}"
            + "".join(f"{str(h) + 'd':>9}" for h in horizons) + "  status")
    for row in shown:
        line = (f"  {str(row['ts'])[:19]:<20}{str(row['symbol']):<7}"
                f"{str(row['raw_action']):<18}{str(row['lifecycle_status']):<9}")
        for h in horizons:
            line += f"{_pct(row[f'return_{h}d']):>9}"
        line += f"  {row['measurement_status']}"
        add(line)
        # Per horizon, so a reason is attached to the cell it explains rather
        # than pooled into one line the reader has to match up by hand.
        for h in horizons:
            cell = row["horizons"][f"{h}d"]
            if cell["status"] != MEASURED:
                add(f"      {h:>3}d {cell['status']}: {cell['reason']}")
    if not shown:
        add("  (no lifecycle-linked decisions yet)")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# selftest — PRODUCTION-SHAPED, and says so
# --------------------------------------------------------------------------- #
def _selftest() -> None:
    """Exercise the measurement on events built by THE REAL WRITERS.

    ⛔ THIS IS NOT LIVE DATA AND PROVES NOTHING ABOUT THE LIVE STREAM. Every
    event below comes from `position_lifecycle.adopt/diff` and every decision
    from `agent_env.decide.decision_entry`, so the SHAPES are production's and a
    field rename upstream breaks this test — but the values are invented, they
    live only in memory, and nothing is written anywhere. What it checks is the
    join, the three horizon states, the exposure-direction grouping and the
    refusal to invent a number.
    """
    import position_lifecycle as lc
    from agent_env.decide import decision_entry

    account = "TESTACCT"
    # --- an OBSERVED lifecycle: opened on a sole filled buy, later closed -----
    orders = [{"id": "ord-1", "symbol": "AAA", "side": "buy", "state": "filled",
               "quantity": "2", "average_price": "100",
               "last_transaction_at": "2026-08-03T14:00:00+00:00"}]
    opened = lc.diff(prior={}, new={"AAA": {"qty": "2", "avg_cost": "100"}},
                     open_lifecycles={}, observed_at="2026-08-03T20:05:00+00:00",
                     orders=orders, account_number=account)
    assert len(opened) == 1 and opened[0]["event"] == lc.EVENT_OPENED, opened
    pid = opened[0]["position_id"]

    active = {"AAA": {"position_id": pid, "origin": opened[0]["origin"],
                      "opened_on": opened[0]["opened_on"]}}
    trimmed = lc.diff(prior={"AAA": {"qty": "2", "avg_cost": "100"}},
                      new={"AAA": {"qty": "1", "avg_cost": "100"}},
                      open_lifecycles=active, observed_at="2026-08-06T20:05:00+00:00",
                      account_number=account)
    closed = lc.diff(prior={"AAA": {"qty": "1", "avg_cost": "100"}},
                     new={}, open_lifecycles=active,
                     observed_at="2026-08-12T20:05:00+00:00",
                     orders=[{"id": "ord-2", "symbol": "AAA", "side": "sell", "state": "filled",
                              "quantity": "1", "average_price": "110",
                              "last_transaction_at": "2026-08-12T18:00:00+00:00"}],
                     account_number=account)
    assert closed and closed[0]["event"] == lc.EVENT_CLOSED

    # --- an ADOPTED lifecycle, still open, with null opening facts -----------
    adopted = lc.adopt({"BBB": {"qty": "3", "avg_cost": "50"}},
                       adopt_ref="2026-08-03", observed_at="2026-08-03T20:05:00+00:00",
                       account_number=account)
    bpid = adopted[0]["position_id"]
    assert adopted[0]["opened_on"] is None and adopted[0]["origin"] == lc.ORIGIN_ADOPTED

    journal = (
        opened + adopted
        + [decision_entry("AAA", "hold", "still leading its sector",
                          "2026-08-04T14:40:00+00:00", position_id=pid)]
        + trimmed
        + [decision_entry("AAA", "trim", "extended vs the 20d",
                          "2026-08-06T19:20:00+00:00", position_id=pid),
           # after the close: 16:05 ET, so the baseline is the NEXT session
           decision_entry("AAA", "exit", "thesis done",
                          "2026-08-12T20:05:00+00:00", position_id=pid),
           # an action with no unambiguous exposure meaning
           decision_entry("BBB", "skip_trim", "left it alone",
                          "2026-08-04T14:41:00+00:00", position_id=bpid),
           # a symbol the panel cannot price
           decision_entry("BBB", "hold", "unpriceable name",
                          "2026-08-05T14:41:00+00:00", position_id=bpid),
           # stamped at a lifecycle this stream does not contain -> orphan
           decision_entry("CCC", "hold", "orphan", "2026-08-05T14:41:00+00:00",
                          position_id="observed:CCC:nope"),
           # unstamped -> outside the corpus entirely
           decision_entry("DDD", "hold", "never stamped", "2026-08-05T14:41:00+00:00")]
        + closed)

    sessions = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07",
                "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"]
    #                    03    04    05    06    07    10    11    12    13
    closes_aaa = [100.0, 101.0, 102.0, 103.0, None, 105.0, 106.0, 107.0, 108.0]
    panel = {"sessions": sessions, "series": {"AAA": closes_aaa}}

    m = measure(journal, panel)
    cov = m["coverage"]
    assert cov["total_agent_decisions"] == 7, cov
    assert cov["stamped_with_position_id"] == 6, cov
    assert cov["unstamped_decisions"] == 1, cov
    assert cov["lifecycle_linked_decisions"] == 5, cov
    assert cov["orphan_stamped_decisions"] == 1, cov

    rows = {(r["symbol"], r["raw_action"]): r for r in m["decisions"]}
    assert len(m["decisions"]) == 5

    # ONE ROW PER DECISION, three of them inside the SAME lifecycle
    aaa_rows = [r for r in m["decisions"] if r["position_id"] == pid]
    assert len(aaa_rows) == 3, aaa_rows
    # ...each repeating the lifecycle's own P&L as CONTEXT, not as its own result
    assert {r["lifecycle_realized_pnl_pct"] for r in aaa_rows} == {closed[0]["realized_pnl_pct"]}

    hold = rows[("AAA", "hold")]
    assert hold["baseline_session"] == "2026-08-04"          # decided 10:40 ET
    assert abs(hold["return_1d"] - (102.0 / 101.0 - 1)) < 1e-12
    assert hold["horizons"]["5d"]["status"] == MEASURED       # 04 -> 11
    assert abs(hold["return_5d"] - (106.0 / 101.0 - 1)) < 1e-12
    assert hold["horizons"]["10d"]["status"] == PENDING       # past the panel
    assert hold["return_10d"] is None, "a pending horizon must be null, never 0"
    assert hold["analytical_group"] == GROUP_HOLD

    exit_row = rows[("AAA", "exit")]
    # 2026-08-12T20:05Z = 16:05 ET, at/after the close -> next session's close
    assert exit_row["baseline_session"] == "2026-08-13", exit_row["baseline_session"]
    assert exit_row["measurement_status"] == PENDING
    assert exit_row["analytical_group"] == GROUP_REDUCE

    trim_row = rows[("AAA", "trim")]
    assert trim_row["baseline_session"] == "2026-08-06"
    # 07 has no close -> that CELL is unavailable, and the others still measure
    assert trim_row["horizons"]["1d"]["status"] == UNAVAILABLE, trim_row["horizons"]["1d"]
    assert trim_row["return_1d"] is None
    assert trim_row["horizons"]["5d"]["status"] == MEASURED
    assert trim_row["missing_reasons"], "an unavailable cell must say why"

    bbb = rows[("BBB", "hold")]
    assert bbb["measurement_status"] == UNAVAILABLE
    assert all(bbb[f"return_{h}d"] is None for h in HORIZONS)
    assert bbb["lifecycle_status"] == "open" and bbb["lifecycle_origin"] == lc.ORIGIN_ADOPTED
    assert bbb["lifecycle_realized_pnl_pct"] is None
    # an OPEN lifecycle's decisions are still in the corpus
    assert bbb["position_id"] == bpid

    unclassified = rows[("BBB", "skip_trim")]
    assert unclassified["raw_action"] == "skip_trim", "raw action must survive"
    assert unclassified["analytical_group"] == GROUP_UNCLASSIFIED, "no prefix inference"

    # aggregates carry N, and raw action beside any grouping
    by_action = {e["raw_action"]: e for e in m["forward_returns_by_raw_action"]}
    assert by_action["hold"]["n_decisions"] == 2
    assert by_action["hold"]["1d"]["n"] == 1, "only the priceable hold is counted"
    assert by_action["skip_trim"]["analytical_group"] == GROUP_UNCLASSIFIED

    ctx = m["lifecycle_context"]
    assert ctx["lifecycles"] == 2 and ctx["completed"] == 1
    assert ctx["by_origin"] == {"adopted": 1, "observed": 1}, ctx["by_origin"]
    assert ctx["realized_pnl_pct"]["n"] == 1
    assert ctx["decisions_per_lifecycle"] == {"2": 1, "3": 1}, ctx["decisions_per_lifecycle"]

    # as_of only shrinks what has elapsed; it never reaches into the panel
    earlier = measure(journal, panel, as_of="2026-08-06")
    assert earlier["coverage"]["measured_through_session"] == "2026-08-06"
    assert earlier["decisions"][0]["horizons"]["5d"]["status"] == PENDING

    # determinism, and no mutation of the caller's journal
    before = [dict(e) for e in journal]
    assert measure(journal, panel) == m
    assert journal == before, "measure() mutated its input"

    # an empty corpus renders, and reads as empty rather than as clean
    empty = measure([], {"sessions": [], "series": {}})
    assert empty["coverage"]["lifecycle_linked_decisions"] == 0
    assert render(empty)

    text = render(m)
    for needle in ("A. COVERAGE", "B. RAW ACTION", "skip_trim", "E. PER-DECISION"):
        assert needle in text, needle
    # No verdict language anywhere BELOW the disclaimer (which necessarily says
    # the words "right or wrong" in order to disclaim them).
    body = "\n".join(text.splitlines()[3:]).lower()
    for banned in ("right", "wrong", "good", "bad", "poor", "successful",
                   "favorable", "unfavorable", "strong evidence", "optimal"):
        assert banned not in body, f"render() must issue no verdict: {banned}"

    print("behavior_measurement selftest OK — PRODUCTION-SHAPED, not live data")


if __name__ == "__main__":
    _selftest()
