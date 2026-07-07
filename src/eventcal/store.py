"""Persist the compiled calendar (belief) + append revisions (journal).

Layout under research_store/calendar/  (git-ignored runtime state):
  current.json    - the current compiled calendar: {as_of, events:[EarningsEvent]}
  revisions.jsonl - append-only log of date changes (the revision signal + hygiene)

This is the calendar's slice of the two-store model (see docs/DESIGN.md -> Research
Store design): current.json is the always-loaded belief; revisions.jsonl is the
never-fully-loaded journal.

Revision detection: on save, any event whose report_date moved vs. the prior
current.json is stamped (revised / prior_date / revision_direction) and appended to
revisions.jsonl. Later = delay = skews negative; earlier = the opposite.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CAL_DIR = REPO_ROOT / "research_store" / "calendar"
CURRENT = CAL_DIR / "current.json"
REVISIONS = CAL_DIR / "revisions.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_current() -> dict:
    """Load the current calendar, or an empty skeleton if none exists yet."""
    if not CURRENT.exists():
        return {"as_of": None, "events": []}
    return json.loads(CURRENT.read_text())


def _index(events) -> dict:
    return {(e["symbol"], e["fiscal_period"]): e for e in events}


def save(events: list, *, as_of: str | None = None) -> list:
    """Diff vs. current for revisions, stamp events in place, persist, return revisions."""
    as_of = as_of or _now_iso()
    prior = _index(load_current().get("events", []))

    revisions = []
    for e in events:
        p = prior.get((e["symbol"], e["fiscal_period"]))
        if p and p["report_date"] != e["report_date"]:
            direction = "later" if e["report_date"] > p["report_date"] else "earlier"
            e["revised"] = True
            e["prior_date"] = p["report_date"]
            e["revision_direction"] = direction
            revisions.append({
                "symbol": e["symbol"],
                "fiscal_period": e["fiscal_period"],
                "old": p["report_date"],
                "new": e["report_date"],
                "direction": direction,
                "detected_at": as_of,
            })

    CAL_DIR.mkdir(parents=True, exist_ok=True)
    CURRENT.write_text(json.dumps({"as_of": as_of, "events": events}, indent=2))
    if revisions:
        with REVISIONS.open("a") as f:
            for rev in revisions:
                f.write(json.dumps(rev) + "\n")
    return revisions
