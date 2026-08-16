"""Freshness checks for the broker-ownership snapshot.

Quotes can re-mark prices, but they cannot make the set of broker holdings
fresh.  Ownership freshness is therefore always measured from positions.json's
own ``ts`` against the newest journalled execution.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path


def parse_ts(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        out = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return out.replace(tzinfo=dt.timezone.utc) if out.tzinfo is None else out
    except (TypeError, ValueError):
        return None


def latest_fill_ts(journal_path: Path) -> dt.datetime | None:
    newest = None
    try:
        lines = journal_path.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        if row.get("event") != "execution" or not row.get("fills"):
            continue
        stamp = parse_ts(row.get("ts"))
        if stamp is not None and (newest is None or stamp > newest):
            newest = stamp
    return newest


def status(snapshot_path: Path, journal_path: Path) -> dict:
    """Return authoritative snapshot/fill timestamps and whether they conflict."""
    try:
        snap = json.loads(snapshot_path.read_text())
    except (OSError, ValueError, TypeError):
        snap = None
    snapshot_ts = parse_ts(snap.get("ts")) if isinstance(snap, dict) else None
    fill_ts = latest_fill_ts(journal_path)
    reason = None
    if fill_ts is not None and snapshot_ts is None:
        reason = "snapshot is missing or has no parseable ts after a recorded fill"
    elif fill_ts is not None and snapshot_ts < fill_ts:
        reason = "snapshot predates the newest recorded fill"
    return {"stale": reason is not None, "reason": reason,
            "snapshot_ts": snapshot_ts, "last_fill_ts": fill_ts}


def _selftest() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        snap, journal = root / "positions.json", root / "journal.jsonl"
        journal.write_text(json.dumps({"event": "execution",
            "ts": "2026-08-14T14:41:00Z", "fills": [{"symbol": "AMAT"}]}) + "\n")
        snap.write_text(json.dumps({"ts": "2026-08-14T14:02:05Z"}))
        assert status(snap, journal)["stale"] is True
        snap.write_text(json.dumps({"ts": "2026-08-14T14:42:00Z"}))
        assert status(snap, journal)["stale"] is False
        # A later quote/mark is intentionally irrelevant to ownership freshness.
        snap.write_text(json.dumps({"ts": "2026-08-14T14:02:05Z",
                                    "marked_at": "2026-08-14T19:59:50Z"}))
        assert status(snap, journal)["stale"] is True
    print("selftest OK: broker snapshot freshness follows fills, not price marks")


if __name__ == "__main__":
    _selftest()
