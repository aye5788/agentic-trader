"""Persistence for the compiled event calendar.

Two artifacts, consistent with the Research Store's belief/journal split:
  research_store/calendar/current.json   the BELIEF  — the current compiled calendar
  research_store/calendar/revisions.jsonl the JOURNAL — append-only date-change log

The current calendar is rewritten atomically each nightly compile; the revision
log only ever grows. Both are plain JSON so any future agent (or a human) can read
them cold. This directory is runtime state — git-ignored, regenerated on the box.
"""
import json
import os
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STORE_DIR = REPO_ROOT / "research_store" / "calendar"
CURRENT = STORE_DIR / "current.json"
REVISIONS = STORE_DIR / "revisions.jsonl"


def _ensure_dir() -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)


def load_current() -> dict:
    """Return the last compiled calendar as {symbol: record}, or {} if none yet."""
    if not CURRENT.exists():
        return {}
    with CURRENT.open() as f:
        payload = json.load(f)
    return payload.get("events", {})


def save_current(events: dict, *, as_of: str) -> Path:
    """Atomically write the current calendar (temp file + os.replace).

    events: {symbol: record}. as_of: ISO timestamp stamped by the caller (the
    slow loop) — this module never calls the clock itself.
    """
    _ensure_dir()
    payload = {"as_of": as_of, "count": len(events), "events": events}
    fd, tmp = tempfile.mkstemp(dir=STORE_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(tmp, CURRENT)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return CURRENT


def append_revisions(revisions: list) -> None:
    """Append date-change events to the journal (one JSON object per line).

    Each: {symbol, old, new, direction, detected_at}. No-op on empty input.
    """
    if not revisions:
        return
    _ensure_dir()
    with REVISIONS.open("a") as f:
        for rev in revisions:
            f.write(json.dumps(rev, sort_keys=True) + "\n")


def read_revisions() -> list:
    """Return the full revision journal (list of change records; [] if none)."""
    if not REVISIONS.exists():
        return []
    out = []
    with REVISIONS.open() as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
