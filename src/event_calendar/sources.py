"""Source fetchers/normalizers for the earnings calendar.

Each source is normalized to a common shape so the compiler can merge them
without caring where a date came from:

    {"date": "YYYY-MM-DD", "session": "bmo|amc|unknown",
     "confirmed": True|False|None, "eps_estimate": float|None,
     "fiscal_period": "Q3 2026" | None}

Two kinds of source, by necessity:
- **Finnhub** is a plain REST API → fetched deterministically from Python (cron-
  safe, zero agent). This is the spine.
- **Robinhood** earnings data is an MCP tool, reachable by the Claude agent but
  NOT by deterministic Python. So RH enters as a *snapshot* the slow-loop agent
  writes to disk; we only read/normalize it here. Absent snapshot → RH simply
  doesn't contribute (Finnhub-only), which is a graceful degrade, not an error.
"""
import json
import os
import tempfile
from pathlib import Path

from adapters.finnhub.client import get as finnhub_get

REPO_ROOT = Path(__file__).resolve().parents[2]
# Where the slow-loop agent drops its RH earnings-calendar snapshot (git-ignored
# runtime state). One JSON object: {"as_of": ..., "events": {SYMBOL: {...}, ...}}.
RH_SNAPSHOT = REPO_ROOT / "research_store" / "calendar" / "rh_snapshot.json"

_SESSION_MAP = {"bmo": "bmo", "amc": "amc", "dmh": "unknown",
                "am": "bmo", "pm": "amc"}


def _norm_session(raw) -> str:
    if not raw:
        return "unknown"
    return _SESSION_MAP.get(str(raw).strip().lower(), "unknown")


def fetch_finnhub(symbol: str, *, from_date: str, to_date: str) -> list:
    """Return normalized upcoming earnings records for one symbol from Finnhub.

    Finnhub's /calendar/earnings gives scheduled dates + EPS estimate but carries
    NO company-verification flag, so `confirmed` is always None here — confidence
    has to come from cross-check + revision-tracking, not Finnhub alone.
    """
    data = finnhub_get("calendar/earnings", symbol=symbol,
                       **{"from": from_date, "to": to_date})
    out = []
    for row in data.get("earningsCalendar", []):
        q, y = row.get("quarter"), row.get("year")
        out.append({
            "date": row.get("date"),
            "session": _norm_session(row.get("hour")),
            "confirmed": None,  # Finnhub does not expose a verification flag
            "eps_estimate": row.get("epsEstimate"),
            "fiscal_period": f"Q{q} {y}" if q and y else None,
        })
    # Nearest upcoming first.
    return sorted((r for r in out if r["date"]), key=lambda r: r["date"])


def load_rh_snapshot(path: Path = RH_SNAPSHOT) -> dict:
    """Return the agent-supplied RH calendar snapshot as {symbol: normalized}.

    The snapshot is produced out-of-band by the slow-loop agent (which can call
    the RH MCP earnings-calendar tool). Missing file → {} (RH just won't
    contribute to the cross-check this run).
    """
    if not path.exists():
        return {}
    with path.open() as f:
        payload = json.load(f)
    events = payload.get("events", {})
    normalized = {}
    for symbol, rec in events.items():
        normalized[symbol.upper()] = {
            "date": rec.get("date"),
            "session": _norm_session(rec.get("session") or rec.get("hour")),
            "confirmed": rec.get("confirmed"),  # RH carries a real verified flag
            "eps_estimate": rec.get("eps_estimate") or rec.get("epsEstimate"),
            "fiscal_period": rec.get("fiscal_period"),
        }
    return normalized


def normalize_rh_entry(entry: dict) -> dict:
    """Map one raw RH earnings-calendar entry to our normalized source shape.

    RH raw shape (from the get_earnings_calendar MCP tool), verified live:
      {"symbol","year","quarter","eps":{"estimate":"1.23","actual":None},
       "report":{"date":"YYYY-MM-DD","timing":"am"|"pm"|None,"verified":bool}}
    """
    report = entry.get("report") or {}
    eps = entry.get("eps") or {}
    est = eps.get("estimate")
    try:
        est = float(est) if est is not None else None
    except (TypeError, ValueError):
        est = None
    q, y = entry.get("quarter"), entry.get("year")
    return {
        "date": report.get("date"),
        "session": _norm_session(report.get("timing")),
        "confirmed": report.get("verified"),
        "eps_estimate": est,
        "fiscal_period": f"Q{q} {y}" if q and y else None,
    }


def write_rh_snapshot(raw_entries, *, as_of: str, path: Path = RH_SNAPSHOT) -> Path:
    """Normalize raw RH earnings entries and atomically write the snapshot.

    This is the ONE step the slow-loop agent performs for the calendar: it calls
    the RH get_earnings_calendar MCP tool (agent-only), then hands the raw entry
    list here. We map each entry, keep the nearest upcoming report per symbol, and
    write the file the deterministic compiler reads. Pure Python → unit-testable
    with a captured RH payload, and the compiler stays agent-free.
    """
    events: dict = {}
    for entry in raw_entries:
        symbol = (entry.get("symbol") or "").upper()
        norm = normalize_rh_entry(entry)
        if not symbol or not norm["date"]:
            continue
        cur = events.get(symbol)
        if cur is None or norm["date"] < cur["date"]:  # keep nearest upcoming
            events[symbol] = norm

    payload = {"as_of": as_of, "count": len(events), "events": events}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path
