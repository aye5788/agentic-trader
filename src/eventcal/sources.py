"""Raw earnings-date fetchers, normalized to one common Observation shape.

Fetching is deliberately decoupled from merging (see compiler.py): every provider
yields the same Observation dict, so the confirmed/estimated logic works with one
source or several.

Access patterns differ by provider:
  - Finnhub  : per-SYMBOL query over a date range — code-callable (REST + our key).
               No confirmation flag -> verified=None.
  - Robinhood: market-WIDE window sweep (<=31 days) carrying `report.verified` —
               our authoritative confirmed/estimated flag. RH is an agent-facing
               MCP (not a REST key), so its rows are fetched by the SLOW-LOOP AGENT
               and handed to rh_results_to_observations(); this module never calls
               the MCP itself.

Observation shape:
    {provider, symbol, date, session(bmo|amc|unknown), verified(True|False|None),
     fiscal_year, fiscal_quarter, eps_estimate}
"""
from adapters.finnhub.client import get as _finnhub_get

_FINNHUB_SESSION = {"bmo": "bmo", "amc": "amc", "dmh": "unknown", "": "unknown"}
_RH_SESSION = {"am": "bmo", "pm": "amc"}


def _obs(provider, symbol, day, session, verified, fy, fq, eps):
    return {
        "provider": provider,
        "symbol": (symbol or "").upper(),
        "date": day,
        "session": session,
        "verified": verified,
        "fiscal_year": fy,
        "fiscal_quarter": fq,
        "eps_estimate": eps,
    }


def finnhub_earnings(symbol: str, *, from_date: str, to_date: str) -> list:
    """Per-symbol earnings calendar over [from_date, to_date] (YYYY-MM-DD).

    Finnhub gives forward dates but no confirmation flag -> verified=None.
    """
    rows = _finnhub_get(
        "calendar/earnings", symbol=symbol, **{"from": from_date, "to": to_date}
    ).get("earningsCalendar", [])
    out = []
    for r in rows:
        if not r.get("date"):
            continue
        out.append(_obs(
            "finnhub", r.get("symbol", symbol), r["date"],
            _FINNHUB_SESSION.get((r.get("hour") or "").lower(), "unknown"),
            None, r.get("year"), r.get("quarter"), r.get("epsEstimate"),
        ))
    return out


def rh_results_to_observations(rh_payload) -> list:
    """Normalize a Robinhood get_earnings_calendar payload into Observations.

    The slow-loop AGENT calls the RH MCP tool and passes its JSON here.
    `report.verified` is the authoritative confirmed/estimated flag; `report.timing`
    ("am"/"pm") maps to bmo/amc. Accepts the full {"data":{"results":[...]}} envelope
    or a bare results list.
    """
    if isinstance(rh_payload, dict):
        results = rh_payload.get("data", {}).get("results") or rh_payload.get("results") or []
    else:
        results = rh_payload or []
    out = []
    for r in results:
        rep = r.get("report") or {}
        if not rep.get("date"):
            continue
        eps = (r.get("eps") or {}).get("estimate")
        try:
            eps = float(eps) if eps not in (None, "") else None
        except (TypeError, ValueError):
            eps = None
        out.append(_obs(
            "robinhood", r.get("symbol", ""), rep["date"],
            _RH_SESSION.get(rep.get("timing"), "unknown"),
            bool(rep.get("verified")), r.get("year"), r.get("quarter"), eps,
        ))
    return out
