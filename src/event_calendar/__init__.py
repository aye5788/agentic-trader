"""Earnings/event calendar — the deterministic timing + risk spine.

For a PEAD strategy the calendar is load-bearing on both sides:
- **offense** — `fresh_reports()` surfaces names that just reported (drift entries),
- **defense** — `reports_within()` stops a swing being held into an unscheduled
  print (the opposite of edge),
plus `recent_revisions()` exposes date-change signal (a pushed-back report skews
negative).

Build/refresh the calendar with `compiler.compile_calendar(...)` (slow loop, once
nightly). These query helpers read the last compiled calendar. They take `today`
explicitly (YYYY-MM-DD) — the package never reads the clock itself, so behavior is
reproducible and testable.
"""
from datetime import date

from . import compiler, sources, store

__all__ = ["compiler", "sources", "store", "next_earnings", "days_until_earnings",
           "reports_within", "fresh_reports", "recent_revisions"]


def _cal(calendar):
    return store.load_current() if calendar is None else calendar


def next_earnings(symbol: str, calendar=None) -> dict | None:
    """Return the compiled next-earnings record for a symbol, or None."""
    return _cal(calendar).get(symbol.upper())


def days_until_earnings(symbol: str, today: str, calendar=None) -> int | None:
    """Whole days from `today` to the symbol's next report (negative if past)."""
    rec = _cal(calendar).get(symbol.upper())
    if not rec or not rec.get("report_date"):
        return None
    return (date.fromisoformat(rec["report_date"]) - date.fromisoformat(today)).days


def reports_within(symbol: str, days: int, today: str, calendar=None) -> bool:
    """Defensive gate: does this name report within the next `days` (inclusive)?

    True means "do not open a swing that would straddle this print." Uses the
    compiled `report_date`, which the compiler already skews conservative
    (earliest) when sources disagree.
    """
    d = days_until_earnings(symbol, today, calendar)
    return d is not None and 0 <= d <= days


def fresh_reports(today: str, since_days: int = 1, calendar=None) -> list:
    """Offense: symbols whose report_date falls in [today - since_days, today].

    These are PEAD entry *candidates*. NOTE: a calendar date is only the heads-up —
    confirm the report actually landed via Finnhub earnings-surprise data
    (adapters.finnhub.research.get_earnings_surprises) before treating it as a
    trigger, since forward dates can be estimated/wrong.
    """
    t = date.fromisoformat(today)
    out = []
    for symbol, rec in _cal(calendar).items():
        rd = rec.get("report_date")
        if not rd:
            continue
        delta = (t - date.fromisoformat(rd)).days
        if 0 <= delta <= since_days:
            out.append(symbol)
    return sorted(out)


def recent_revisions(days: int, today: str) -> list:
    """Date-change events detected within the last `days` (the revision signal)."""
    t = date.fromisoformat(today)
    out = []
    for rev in store.read_revisions():
        stamp = (rev.get("detected_at") or "")[:10]
        if not stamp:
            continue
        if 0 <= (t - date.fromisoformat(stamp)).days <= days:
            out.append(rev)
    return out
