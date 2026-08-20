"""Assemble the deterministic facts for the weekly investor letter.

Division of labor (same as everywhere in this repo): numbers are computed HERE,
in plain Python from the Research Store; the headless letter-writer
(prompts/newsletter.md) turns them into prose and may not invent or recompute
any figure. Output: research_store/newsletters/facts.json.

    .venv/bin/python scripts/letter_facts.py
"""
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import marks                                    # noqa: E402
from research_store import read_current         # noqa: E402

RS = REPO / "research_store"
LETTERS = RS / "newsletters"


def _read_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _jsonl(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def _in_window(event, since) -> bool:
    """Is this journal event inside the letter's [since, now] window?

    Pure — the letter's trade table is only as honest as this predicate.

    An event is timed by its `ts` when it has one, and ONLY otherwise by its
    coarser `as_of` date. The two clocks must never be OR-ed: the early
    first-deployment `execution` events carry `as_of` but no `ts`, and the
    previous form — `(e.get("ts") or "9999") >= since or as_of >= since[:10]`
    — substituted "9999" for the missing `ts`, which is >= every real
    timestamp. Those events therefore passed the window test forever and
    republished themselves in EVERY subsequent issue (see docs/OPSLOG.md
    2026-07-27).
    """
    ts = event.get("ts")
    if ts:
        return ts >= since
    return event.get("as_of", "") >= since[:10]


def _selftest() -> None:
    since = "2026-07-20T01:04:33+00:00"

    # a ts inside / outside the window
    assert _in_window({"ts": "2026-07-24T14:02:13+00:00"}, since)
    assert not _in_window({"ts": "2026-07-17T14:02:13+00:00"}, since)

    # THE BUG: an event with NO ts must be judged by as_of, never waved
    # through. These two are the real 2026-07-08 first-deployment events.
    assert not _in_window({"as_of": "2026-07-08"}, since), \
        "a ts-less event from before the window must NOT be included"
    assert _in_window({"as_of": "2026-07-22"}, since), \
        "a ts-less event from inside the window must still be included"

    # an event with neither clock cannot be dated -> excluded, not waved through
    assert not _in_window({}, since), "an undatable event must not be included"

    # a real ts inside the window is not overridden by a stale as_of
    assert _in_window({"ts": "2026-07-24T14:02:13+00:00", "as_of": "2026-07-08"}, since)

    print("letter_facts selftest: PASS")


def _flows_since(start_date, end_date):
    """Confirmed external cash flows in (start_date, end_date] — deposits count
    positive, withdrawals negative. Reads research_store/flows.jsonl. Returns
    (net_amount, entries). Used to strip contributions out of week P&L so a
    deposit is never narrated as performance."""
    entries = [f for f in _jsonl(RS / "flows.jsonl")
               if f.get("status") == "confirmed"
               and start_date < f.get("date", "") <= end_date]
    net = sum((-1.0 if f.get("direction") == "withdrawal" else 1.0)
              * float(f.get("amount") or 0) for f in entries)
    return round(net, 2), entries


def _profit_but_loss(cost, mark, effective_stop):
    """True iff the mark is above average cost AND the currently watched
    effective stop is below it. None when any input is missing or non-finite,
    or when no stop is in force -- claiming what a position "would close at"
    with nothing watching implies protection that does not exist.

    Same definition as src/agent_env/state.py; replaced the unstable
    gain_protected_pct ratio on 2026-08-19.
    """
    import math                                           # noqa: PLC0415

    def _f(x):
        try:
            return isinstance(x, (int, float)) and math.isfinite(float(x))
        except (TypeError, ValueError):
            return False
    if not (_f(cost) and _f(mark) and _f(effective_stop)):
        return None
    return bool(float(mark) > float(cost) and float(effective_stop) < float(cost))


def main() -> None:
    LETTERS.mkdir(parents=True, exist_ok=True)
    today = date.today()
    monday = today - timedelta(days=today.weekday())

    issues = sorted(LETTERS.glob("issue_*.html"))
    issue_number = len(issues) + 1
    # events "this week" = since the previous issue was generated (or 7 days)
    if issues:
        since = datetime.fromtimestamp(issues[-1].stat().st_mtime,
                                       tz=timezone.utc).isoformat(timespec="seconds")
    else:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")

    valued = marks.load()
    if valued is None:
        sys.exit("no RH snapshot — cannot write a letter without account state")

    # --- week P&L from the equity curve (needs >= 2 points; else null) ---
    # week_pnl is NET OF external cash flows: a deposit/withdrawal changes
    # account value but is not performance, so it is stripped out here. The
    # raw (flow-inclusive) ratio is kept as week_pnl_gross for transparency.
    points = _jsonl(RS / "history" / "equity.jsonl")
    week_pnl = None
    week_pnl_gross = None
    baseline = None
    net_flows = 0.0
    flows_week = []
    if len(points) >= 2:
        cutoff = (today - timedelta(days=7)).isoformat()
        prior = [p for p in points[:-1] if p.get("date", "") >= cutoff] or points[:-1]
        baseline = prior[0]
        net_flows, flows_week = _flows_since(baseline.get("date", ""), today.isoformat())
        if baseline.get("value"):
            bv = float(baseline["value"])
            week_pnl_gross = round(valued["account_value"] / bv - 1.0, 4)
            week_pnl = round((valued["account_value"] - net_flows) / bv - 1.0, 4)

    # --- unrealized P&L on cost (always available; the honest fallback) ---
    cost = sum(p["qty"] * p["avg_cost"] for p in valued["positions"].values()
               if p.get("qty") and p.get("avg_cost"))
    unrealized = round(valued["invested"] / cost - 1.0, 4) if cost > 0 else None

    # --- actual broker positions, enriched with a thesis where one exists ---
    # The target book is a proposal, not account state.  Since the procedural
    # executor was retired it may diverge from the broker, so ownership must
    # come exclusively from marks.load()'s broker-backed position snapshot.
    prod = read_current()
    theses = {t.symbol: t for t in prod.theses} if prod else {}

    # ⛔ THE `sleeve` FIELD IS GONE (2026-08-20). It classified a position as
    # ETF-sleeve or not. The sleeve was retired 2026-08-16, its positions sold
    # 08-17, and the whole mechanism deleted 08-20 -- so every value it could
    # report is now False, and a field that is always False in an investor
    # letter is worse than absent: it implies the distinction still exists.
    #
    # (Kept as a note because the bug it fixed is instructive: the field was
    # once inferred from `rank >= 100`, which had been the sleeve's rank band.
    # protective_theses() later started assigning rank 200 to agent-bought
    # names, so on 2026-08-18 MRK -- a pharma stock -- was reported as an ETF
    # sleeve position. A sentinel that encodes two meanings encodes neither.)

    # --- D4: how far each position RAN, and how much of it is protected ------
    # The single most important risk fact in the book, and the letter could not
    # see it: issue 008 said nothing about AMD and TER showing a profit while
    # their stops sat below cost -- both would have closed RED. The arithmetic
    # is pure in src/excursion.py (shared with positions()); only the I/O is
    # here. Best-effort: a missing panel must never stop a letter being written.

    # ⛔ THE EFFECTIVE STOP, resolved by src/levels.py -- the same rule the
    # monitor enforces and positions() displays. This file used the THESIS stop
    # only, and read the profit-but-loss flag out of the excursion dict, which
    # stopped producing it when the field moved on 2026-08-19; the letter would
    # then have silently reported nothing for what its own prompt calls the most
    # important risk fact in the book. Degrades to the thesis stop, never crashes.
    _eff_stop = {}
    try:
        sys.path.insert(0, str(REPO / "src"))
        import levels as _levels                          # noqa: PLC0415
        try:
            _ov_raw = json.loads((RS / "monitor" / "overrides.json").read_text())
        except Exception:                                 # noqa: BLE001
            _ov_raw = {}
        try:
            _px_raw = (json.loads((RS / "monitor" / "quotes.json").read_text())
                       or {}).get("prices", {}) or {}
        except Exception:                                 # noqa: BLE001
            _px_raw = {}
        for _sym in valued["positions"]:
            _th = theses.get(_sym)
            _eff_stop[_sym] = _levels.resolve(
                getattr(_th, "stop", None) if _th else None,
                list(getattr(_th, "targets", []) or []) if _th else [],
                _ov_raw.get(_sym), price=_px_raw.get(_sym),
                price_guard=True)["effective_stop"]
    except Exception as _e:                               # noqa: BLE001
        print(f"  effective stops unavailable ({_e.__class__.__name__}) — "
              f"profit-but-loss flag will be null", file=sys.stderr)

    _exc = {}
    try:
        import pandas as _pd                                    # noqa: PLC0415
        import excursion            # REPO/src already on sys.path  # noqa: PLC0415
        _hi = _pd.read_parquet(RS / "prices" / "highs.parquet")
        _events = [json.loads(l) for l in
                   (RS / "journal.jsonl").read_text().splitlines() if l.strip()]
        for _sym, _p in valued["positions"].items():
            if _p.get("avg_cost") is None:
                continue
            _ent = excursion.entry_date(_events, _sym)
            _series = []
            if _ent is not None and _sym in _hi.columns:
                _series = _hi.loc[_hi.index >= _ent, _sym].dropna().tolist()
            _th = theses.get(_sym)
            _exc[_sym] = excursion.facts(_p.get("avg_cost"), _p.get("mark"),
                                         _th.stop if _th else None, _series)
    except Exception as _e:                      # noqa: BLE001
        _exc = {}
        print(f"  excursion facts unavailable ({_e.__class__.__name__}) — "
              f"positions carry nulls", file=sys.stderr)

    positions = []
    for symbol, p in valued["positions"].items():
        t = theses.get(symbol)
        e = _exc.get(symbol) or {}
        positions.append({
            # profitable_now_but_loss_at_stop = shows a profit, would close at
            # a LOSS because the stop never followed the price up. Was the ratio
            # gain_protected_pct until 2026-08-19; that exploded near entry and
            # ranked two positions backwards, so it is a boolean now and
            # magnitude comes from the P&L fields.
            "peak_pct": (round(e["peak_pct"], 4)
                         if e.get("peak_pct") is not None else None),
            "giveback_pct": (round(e["giveback_pct"], 4)
                             if e.get("giveback_pct") is not None else None),
            "profitable_now_but_loss_at_stop": _profit_but_loss(
                p.get("avg_cost"), p.get("mark"), _eff_stop.get(symbol)),
            "symbol": symbol,
            # ⛔ signals["rank"] is the MOMENTUM rank; t.rank is a portfolio-slot
            # sentinel (200+ for a held-but-unselected name). Reporting the
            # sentinel told the letter a rank-24 name ranked 200th — the same
            # confusion that sold BAC on 2026-08-20.
            "rank": (t.signals or {}).get("rank") if t else None,
            "book_slot": t.rank if t else None,
            # A thesis carrying rank>=100 AND zero target weight is not a
            # recommendation: the loop did not select this name, the AGENT
            # bought it, and the geometry exists only so the monitor can watch
            # it. Say that rather than showing a held position at 0% target
            # weight with no explanation.
            "protective_only": (bool(t.rank is not None and t.rank >= 100
                                     and not t.target_weight) if t else None),
            "thesis": t.thesis if t else None,
            "weight": round(t.target_weight, 4) if t else None,
            "stop": round(t.stop, 2) if t and t.stop else None,
            "targets": [round(x, 2) for x in (t.targets or [])] if t else [],
            "score": t.signals.get("score") if t else None,
            "value": p.get("value"),
            "pnl": p.get("pnl"),
            "review_by": t.review_by if t else None,
        })

    # Keep unowned targets available as proposals, but never mix them into the
    # holdings list the letter renders as the portfolio.
    held_symbols = set(valued["positions"])
    proposed_positions = []
    if prod:
        for t in sorted(prod.theses, key=lambda x: x.rank):
            if t.target_weight > 0 and t.symbol not in held_symbols:
                proposed_positions.append({
                    "symbol": t.symbol,
                    "rank": (t.signals or {}).get("rank"),
                    "book_slot": t.rank,
                    "target_weight": round(t.target_weight, 4),
                    "thesis": t.thesis,
                })

    # --- this week's journal events ---
    events = [e for e in _jsonl(RS / "journal.jsonl") if _in_window(e, since)]
    fills, exits, notes, reentries = [], [], [], []
    risk_actions = []
    # ⚠️ THE AGENT'S OWN REASONS. Since 2026-08-12 the book is decided by an
    # agent that records WHY, not only what. Without this the letter can say a
    # position was sold and never why — and "we exited AMAT ahead of earnings
    # because the stop is software that only runs during RTH" is the most
    # investor-relevant sentence in the whole file. Reasons are truncated here
    # rather than in the prompt so the narrator cannot pad them out.
    agent_decisions = []
    for e in events:
        if e.get("event") == "execution":
            for f in e.get("fills", e.get("placed", [])):
                if f.get("status") == "skipped":
                    continue    # deferred/rejected legs (e.g. insufficient buying
                                # power, re-planned next run) are plumbing —
                                # journaled, never narrated;
                                # they must not eat letter space. Meaningful PM
                                # judgment comes through reentry_decisions instead.
                # ⚠️ `note` IS THE SYSTEM'S OWN REASON AND IT WAS BEING DROPPED.
                # Every order this system places carries why it was placed
                # ("exit: dropped out of target book", "rebalance trim to 7%
                # target weight"). This whitelist omitted it, so facts.json
                # handed the narrator a motive-free trade list -- and the
                # letter, obeying "never invent a why", apologised four times
                # in issue 007 for rationales that were in the journal all
                # along. Costly, because it reads as evasion to the owner.
                fills.append({k: f.get(k) for k in
                              ("symbol", "side", "amount", "status", "avg_price",
                               "note") if k in f})
                if f.get("reason") == "risk_review":
                    risk_actions.append({"symbol": f.get("symbol"), "kind": f.get("side")})
            reentries.extend(e.get("reentry_decisions", []))
            if e.get("halt_reason"):
                notes.append(e["halt_reason"])
        elif e.get("event") == "agent_decision":
            agent_decisions.append({
                "symbol": e.get("symbol"), "action": e.get("action"),
                "reason": str(e.get("reason") or "")[:600]})
        elif e.get("event") == "exit_signal":
            exits.extend(e.get("triggers", []))
        elif e.get("event") == "risk_review":
            # Only geometry tightenings that were actually persisted. An unarmed
            # (alert-only) pass decides on the same actions but never writes them
            # to overrides.json — narrating those would claim a de-risk that never
            # took effect. trim/exit are NOT taken from here — they are intents;
            # their CONFIRMED fills arrive as `execution` events (handled above),
            # tagged reason="risk_review".
            if e.get("armed"):
                for a in e.get("applied", []):
                    if a.get("kind") in ("tighten_stop", "lower_tp"):
                        risk_actions.append({"symbol": a.get("symbol"), "kind": a.get("kind")})

    # ---- JOIN THE WHY TO THE WHAT --------------------------------------
    # fills and agent_decisions arrived as two unrelated lists with no key
    # between them, so the narrator could not tell that an AMAT decision and an
    # AMAT fill were ONE story. Issue 007 therefore told AMAT three times --
    # once from the decisions in the body, once from the fills in the cards,
    # once more for the churn -- and read as disjointed and repetitive because
    # it structurally was. Attach each symbol's decisions to its fills; keep
    # session-level judgement (symbol "PORTFOLIO") separate, since it explains
    # the week rather than any one trade.
    by_symbol: dict[str, list] = {}
    portfolio_decisions = []
    for d in agent_decisions:
        raw = str(d.get("symbol") or "").strip()
        if not raw or raw.upper() == "PORTFOLIO":
            portfolio_decisions.append(d)
            continue
        for sym in raw.split(","):          # one decision can cover a basket,
            sym = sym.strip()               # e.g. "IWM,XLK,XLE,XLV"
            if sym:
                by_symbol.setdefault(sym, []).append(d)
    for f in fills:
        matched = by_symbol.get(f.get("symbol"))
        if matched:
            f["agent_reasons"] = [{"action": d.get("action"), "reason": d.get("reason")}
                                  for d in matched]

    # Reconcile raw exit_signals into real exits. The intraday monitor emits a
    # fresh signal EVERY tick a name is below its stop and only stops once the
    # sell fills, so a single stop shows up as many identical triggers; a book
    # name that was never actually held (a phantom, e.g. AMAT on 2026-07-17)
    # re-fires forever and never fills at all. Collapse to one row per
    # (symbol, reason) and keep only names that actually sold this week — an
    # un-filled signal is a monitor artifact, not an exit worth narrating.
    sold = {f["symbol"] for f in fills if f.get("side") == "sell" and f.get("symbol")}
    seen, real_exits = set(), []
    for t in exits:
        key = (t.get("symbol"), t.get("reason"))
        if key in seen or t.get("symbol") not in sold:
            continue
        seen.add(key)
        real_exits.append(t)
    exits = real_exits

    days_ahead = (6 - today.weekday()) % 7 or 7           # next Sunday
    facts = {
        "issue_number": f"{issue_number:03d}",
        "issue_date": f"Week of {monday.strftime('%B %-d, %Y')}",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "account": {"value": valued["account_value"], "cash": valued["cash"],
                    "invested": valued["invested"],
                    "cash_pct": round(100 * valued["cash"] / valued["account_value"])
                    if valued["account_value"] else None,
                    # ⛔ CASH IS NOT THE SAME AS DEPLOYABLE. Issue 008 reported
                    # "41% cash" and framed it as "a real decision in front of
                    # me" when only $8.36 of $31.13 could actually be spent --
                    # 11% of the account, not 41%. The letter described a
                    # constraint as a choice. These three fields are what make
                    # the difference statable; the narrator judges, no
                    # threshold lives here.
                    #
                    # ⚠️ The CAUSE changed 2026-08-18: the account moved from
                    # cash to limited margin, so proceeds settle same-session
                    # and buying_power normally equals cash now. That is
                    # exactly why the reason is NOT hardcoded anywhere here --
                    # a gap can still open (a pending deposit, a broker hold),
                    # and the letter must read the numbers rather than recite
                    # a settlement story that has stopped being true.
                    "buying_power": valued.get("buying_power"),
                    "unsettled_funds": valued.get("unsettled_funds"),
                    "deployable_pct": (
                        round(100 * float(valued["buying_power"]) / valued["account_value"])
                        if valued.get("buying_power") is not None
                        and valued.get("account_value") else None),
                    "marked_at": valued["marked_at"],
                    # ⛔ IS THIS A MARKET VALUE OR A COST FIGURE? The letter is
                    # written on a Sunday, when the book used to fall back to
                    # cost basis silently (src/marks.py, fixed 2026-08-17) --
                    # so issue 007 reported a -3.4% week that was really ~+7.9%
                    # and called it "the market marked down what we still
                    # hold". Never let that be inferred again: if anything here
                    # is cost-marked, the letter must say so instead of
                    # reporting the number as performance.
                    "priced_at_cost": valued.get("priced_at_cost", []),
                    "valuation_basis": ("market" if not valued.get("priced_at_cost")
                                        else "PARTLY COST BASIS — some positions "
                                             "carry no market price; unrealized "
                                             "P&L on those is 0.0 by construction, "
                                             "not by fact. Say so; do not report "
                                             "these as performance.")},
        "week_pnl": week_pnl,                              # NET of deposits/withdrawals
        "week_pnl_gross": week_pnl_gross,                  # raw value ratio (flow-inclusive)
        "week_pnl_baseline": baseline,                     # null on early issues
        # ⛔ THE MEASURED WINDOW, WHICH IS NOT THE HEADER WINDOW. `issue_date`
        # comes from today's Monday; `week_pnl` comes from the newest equity
        # point at least ~7 days old. They are computed independently and can
        # describe different spans -- always, not just when it goes wrong.
        # Issue 008 headed itself "Week of August 17" and reported +9.6% for a
        # span starting 08-10 -- the same days issue 007 covered and called
        # -3.4%. Two letters, overlapping windows, opposite signs, no
        # reconciliation. A reader adding them together gets a fiction.
        "week_pnl_from": (baseline or {}).get("date"),
        "week_pnl_to": valued.get("as_of") or today.isoformat(),
        "week_pnl_window_matches_header": (
            (baseline or {}).get("date") == monday.isoformat()
            if baseline else None),
        "net_deposits_this_week": net_flows,               # + = money added, - = withdrawn
        "flows_this_week": [{k: f.get(k) for k in
                             ("date", "amount", "direction", "status", "note")}
                            for f in flows_week],
        "unrealized_pnl_on_cost": unrealized,
        "regime": (prod.regime if prod and prod.regime else {"status": "unknown"}),
        "positions": positions,
        "proposed_positions": proposed_positions,
        "fills_this_week": fills,
        "exit_signals_this_week": exits,
        # Narrate FROM these, never invent a motive for a trade. If a fill has
        # no decision here, say what was done and not why.
        "agent_decisions_this_week": agent_decisions,
        # Session-level judgement (symbol "PORTFOLIO"): explains the WEEK, not
        # any single trade. Kept apart from the per-fill reasons so the letter
        # stops narrating one story in two places.
        "portfolio_decisions_this_week": portfolio_decisions,
        "reentry_decisions_this_week": reentries,
        "risk_actions_this_week": risk_actions,
        "realized": _read_json(RS / "rh" / "realized.json", None),
        "notes": notes,
        "cooldown": list(_read_json(RS / "monitor" / "cooldown.json", {})),
        "next_rebalance": (today + timedelta(days=days_ahead)).isoformat(),
        "kill_switch": (RS / "HALT").exists(),
    }
    out = LETTERS / "facts.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    flow_note = f", net_flows={net_flows:+.2f}" if net_flows else ""
    print(f"facts -> {out}  (issue {facts['issue_number']}, "
          f"{len(fills)} fills, {len(positions)} positions, "
          f"week_pnl={'n/a' if week_pnl is None else f'{week_pnl:+.2%}'}"
          f"{flow_note})")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
