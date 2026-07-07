"""Earnings/event calendar — the deterministic spine for PEAD timing + risk.

Compiles forward earnings dates from >=1 providers into a cross-checked calendar
with confirmed/estimated tagging and revision tracking. Zero LLM, fully
backtestable. This is load-bearing for BOTH the edge (when to enter post-earnings
drift) and risk (never hold a swing into an unplanned print).

Two consumers, OPPOSITE risk postures (the compiler exposes uncertainty; each
consumer applies its own posture):
  - reports_within() : DEFENSIVE — "would a swing entered today hold into a print?"
                       Treats an ESTIMATED upcoming date as risk (conservative).
  - fresh_reports()  : OFFENSE — names that just reported (PEAD candidates). The
                       calendar is the heads-up; the robust "it actually reported"
                       confirmation is a new row in Finnhub earnings surprises
                       (adapters.finnhub.research.get_earnings_surprises), which the
                       slow loop cross-checks before acting.

Sources (see sources.py): Finnhub per-symbol (code-callable now, verified=None) +
Robinhood window sweep (carries `verified`; fetched by the slow-loop agent via MCP
and merged in). See docs/DESIGN.md -> "Strategy foundation" / "Regime gate".
"""
import json
from datetime import date, datetime, timedelta

from . import store
from .compiler import compile_events
from .sources import finnhub_earnings, rh_results_to_observations

__all__ = [
    "compile_events", "finnhub_earnings", "rh_results_to_observations", "store",
    "next_earnings", "reports_within", "upcoming", "fresh_reports", "recent_revisions",
]


def _today() -> str:
    return date.today().isoformat()


def next_earnings(symbol: str, *, calendar=None):
    """The next upcoming EarningsEvent for a symbol (report_date >= today), or None."""
    symbol = symbol.upper()
    events = (calendar or store.load_current()).get("events", [])
    today = _today()
    upcoming_ev = sorted(
        (e for e in events if e["symbol"] == symbol and e["report_date"] >= today),
        key=lambda e: e["report_date"],
    )
    return upcoming_ev[0] if upcoming_ev else None


def reports_within(symbol: str, days: int, *, calendar=None) -> bool:
    """DEFENSIVE gate: True if `symbol` reports within `days` from today.

    Returns True even when the next date is ESTIMATED — uncertainty is treated as
    risk. Call before entering/holding a swing so you never sit through an
    unplanned earnings print (a coin-flip that erases the edge).
    """
    e = next_earnings(symbol, calendar=calendar)
    if not e:
        return False
    horizon = (date.today() + timedelta(days=days)).isoformat()
    return e["report_date"] <= horizon


def upcoming(days: int = 7, *, calendar=None) -> list:
    """All events reporting within `days` from today (market-wide discovery)."""
    events = (calendar or store.load_current()).get("events", [])
    today = _today()
    horizon = (date.today() + timedelta(days=days)).isoformat()
    return sorted(
        (e for e in events if today <= e["report_date"] <= horizon),
        key=lambda e: e["report_date"],
    )


def fresh_reports(days: int = 2, *, calendar=None) -> list:
    """OFFENSE: PEAD candidates — names whose report_date fell in the last `days`.

    This is the calendar HEADS-UP, not final confirmation. The robust trigger is a
    new row in Finnhub earnings surprises (the slow loop cross-checks these
    candidates against get_earnings_surprises before entering a drift trade).
    """
    events = (calendar or store.load_current()).get("events", [])
    today = date.today()
    lo = (today - timedelta(days=days)).isoformat()
    hi = today.isoformat()
    return sorted(
        (e for e in events if lo <= e["report_date"] <= hi),
        key=lambda e: e["report_date"], reverse=True,
    )


def recent_revisions(days: int = 7) -> list:
    """Recent earnings-date changes — the revision signal + staleness hygiene.

    A date pushed LATER skews negative (delay = bad news); pulled EARLIER is the
    opposite. Also flags a calendar that silently drifted under us.
    """
    if not store.REVISIONS.exists():
        return []
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    out = []
    for line in store.REVISIONS.read_text().splitlines():
        if not line.strip():
            continue
        rev = json.loads(line)
        try:
            when = datetime.fromisoformat(rev["detected_at"])
        except (ValueError, KeyError):
            out.append(rev)
            continue
        if when >= cutoff:
            out.append(rev)
    return out
