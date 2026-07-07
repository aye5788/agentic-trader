"""Merge earnings Observations from >=1 providers into canonical EarningsEvents.

Confirmed/estimated policy: status = "confirmed" ONLY when a provider explicitly
verifies the date (Robinhood's report.verified). Agreement across providers is
corroboration, not confirmation — and DISagreement forces "estimated" (a conflict
is uncertainty). This mirrors the reality that ~39% of forward earnings dates are
estimates, not company-confirmed.

EarningsEvent shape:
    {symbol, report_date, session, status(confirmed|estimated),
     agreement(agree|disagree|single), fiscal_period, eps_estimate,
     sources{provider: {date, verified}}, as_of, prior_date, revised,
     revision_direction}
"""
from datetime import datetime, timezone


def _period(fy, fq) -> str:
    return f"Q{fq} {fy}" if fy and fq else "unknown"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def compile_events(observations: list, *, as_of: str | None = None) -> list:
    """Group observations by (symbol, fiscal period) and merge each group.

    Returns EarningsEvent dicts sorted by (symbol, report_date). Distinct fiscal
    periods for the same symbol stay separate events (this quarter vs. next).
    """
    as_of = as_of or _now_iso()
    groups: dict = {}
    for o in observations:
        key = (o["symbol"], o.get("fiscal_year"), o.get("fiscal_quarter"))
        groups.setdefault(key, []).append(o)

    events = [_merge_group(sym, fy, fq, obs, as_of) for (sym, fy, fq), obs in groups.items()]
    events.sort(key=lambda e: (e["symbol"], e["report_date"]))
    return events


def _merge_group(symbol, fy, fq, obs, as_of) -> dict:
    verified = [o for o in obs if o["verified"] is True]
    distinct_dates = {o["date"] for o in obs}
    distinct_providers = {o["provider"] for o in obs}

    # Canonical date: prefer the earliest VERIFIED observation; else earliest overall
    # (conservative — the defensive gate would rather be early than surprised).
    canonical = min(verified or obs, key=lambda o: o["date"])

    if len(distinct_dates) > 1:
        agreement, status = "disagree", "estimated"   # conflict => not trustworthy
    elif len(distinct_providers) > 1:
        agreement = "agree"
        status = "confirmed" if verified else "estimated"
    else:
        agreement = "single"
        status = "confirmed" if verified else "estimated"

    eps = canonical["eps_estimate"]
    if eps is None:
        eps = next((o["eps_estimate"] for o in obs if o["eps_estimate"] is not None), None)

    return {
        "symbol": symbol,
        "report_date": canonical["date"],
        "session": canonical["session"],
        "status": status,
        "agreement": agreement,
        "fiscal_period": _period(fy, fq),
        "eps_estimate": eps,
        "sources": {o["provider"]: {"date": o["date"], "verified": o["verified"]} for o in obs},
        "as_of": as_of,
        "prior_date": None,
        "revised": False,
        "revision_direction": None,
    }
