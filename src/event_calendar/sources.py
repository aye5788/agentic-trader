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
