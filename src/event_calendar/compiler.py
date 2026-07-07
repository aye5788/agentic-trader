"""Compile a cross-checked earnings calendar from the normalized sources.

Per symbol it merges every source's next-upcoming report into one canonical
record, tags it confirmed-vs-estimated, notes source agreement, and detects
date revisions against the previously compiled calendar.

Design notes:
- **Confirmed only if a source explicitly says so.** We never infer "confirmed"
  from agreement — agreement raises *confidence*, not *status*. Finnhub has no
  verified flag, so a Finnhub-only symbol is always "estimated" until an RH
  snapshot (which carries the flag) corroborates it.
- **Disagreement is uncertainty.** If two sources give different dates, status is
  forced to "estimated" and the canonical date is the *earliest* — conservative,
  because the defensive "don't hold into a print" gate would rather be early.
- **Revisions are logged**, both for hygiene (stale-date protection) and as a
  signal (a pushed-back report skews negative — see docs/DESIGN.md regime notes).

The caller (slow loop) passes `as_of` — this module never reads the clock itself,
so runs are reproducible.
"""
from . import sources, store


def _merge_symbol(finnhub_next: dict | None, rh: dict | None) -> dict | None:
    """Merge one symbol's per-source records into a canonical calendar record."""
    src = {}
    if finnhub_next and finnhub_next.get("date"):
        src["finnhub"] = finnhub_next
    if rh and rh.get("date"):
        src["robinhood"] = rh
    if not src:
        return None

    dates = {name: r["date"] for name, r in src.items()}
    confirmed_names = [n for n, r in src.items() if r.get("confirmed") is True]
    distinct_dates = set(dates.values())

    if len(distinct_dates) > 1:
        agreement = "disagree"
        status = "estimated"                      # disagreement = uncertainty
        report_date = min(distinct_dates)         # conservative: earliest
    else:
        agreement = "agree" if len(src) > 1 else "single"
        report_date = next(iter(distinct_dates))
        status = "confirmed" if confirmed_names else "estimated"

    # Field preference: confirmed source wins for date/session; Finnhub is the
    # reliable EPS-estimate + fiscal-period source.
    lead = src.get("robinhood") if "robinhood" in src else src.get("finnhub")
    session = lead.get("session", "unknown")
    fin = src.get("finnhub", {})
    return {
        "report_date": report_date,
        "session": session,
        "status": status,
        "agreement": agreement,
        "sources": {n: {"date": r["date"], "confirmed": r.get("confirmed")}
                    for n, r in src.items()},
        "fiscal_period": fin.get("fiscal_period") or lead.get("fiscal_period"),
        "eps_estimate": fin.get("eps_estimate") if fin.get("eps_estimate") is not None
                        else lead.get("eps_estimate"),
    }


def compile_calendar(symbols, *, as_of: str, from_date: str, to_date: str,
                     rh_snapshot_path=sources.RH_SNAPSHOT, persist: bool = True) -> dict:
    """Compile, revision-check, and (optionally) persist the calendar.

    symbols: universe/watchlist to cover. from_date/to_date: Finnhub lookahead
    window ("YYYY-MM-DD"). as_of: caller-stamped ISO timestamp. Returns
    {symbol: record}.
    """
    prior = store.load_current()
    rh_snap = sources.load_rh_snapshot(rh_snapshot_path)

    events: dict = {}
    revisions: list = []
    for raw in symbols:
        symbol = raw.upper()
        fin_events = sources.fetch_finnhub(symbol, from_date=from_date, to_date=to_date)
        finnhub_next = fin_events[0] if fin_events else None
        rec = _merge_symbol(finnhub_next, rh_snap.get(symbol))
        if rec is None:
            continue

        old_date = (prior.get(symbol) or {}).get("report_date")
        new_date = rec["report_date"]
        if old_date and old_date != new_date:
            direction = "later" if new_date > old_date else "earlier"
            rec["prior_date"] = old_date
            rec["revised"] = True
            rec["revision_direction"] = direction
            revisions.append({
                "symbol": symbol, "old": old_date, "new": new_date,
                "direction": direction, "detected_at": as_of,
            })
        else:
            rec["revised"] = False
            rec["revision_direction"] = None

        rec["as_of"] = as_of
        events[symbol] = rec

    if persist:
        store.save_current(events, as_of=as_of)
        store.append_revisions(revisions)
    return events
