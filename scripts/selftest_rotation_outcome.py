#!/usr/bin/env python3
"""Regression test for rotation-close outcome recording (Decision→Outcome Ledger, I2).

A rotated-out name is no longer in the current book, so its outcome must attach to
the ARCHIVED thesis by decision_id (spec §4.3). Covers: the rotation path
(compute → journal → attach → realized_history), idempotency, and the existing
current-book (stop/target) path as a no-regression check. Runs against a temp
store — never touches the live ledger.

    .venv/bin/python scripts/selftest_rotation_outcome.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


def main() -> None:
    import research_store.store as store
    import research_store as rs
    import ledger

    tmp = Path(tempfile.mkdtemp())
    store.STORE_DIR = tmp
    store.CURRENT = tmp / "current.json"
    store.ARCHIVE = tmp / "archive"
    store.JOURNAL = tmp / "journal.jsonl"

    def journal():
        return store.read_journal()

    # --- A. rotation close: name NOT in current, HELD in an archived belief ---
    store.save_archived("2026-07-20", {"as_of": "2026-07-20", "theses": [
        {"symbol": "XLI", "as_of": "2026-07-20", "target_weight": 0.075,
         "stop": 173.46, "targets": [188.37, 196.76]},
        {"symbol": "MU", "as_of": "2026-07-20", "target_weight": 0.07,
         "stop": 800, "targets": [1000]},
    ]})
    store.save_current({"as_of": "2026-07-22", "theses": [   # XLI rotated OUT
        {"symbol": "MU", "as_of": "2026-07-22", "target_weight": 0.07,
         "decision_id": "MU:2026-07-22", "stop": 800, "targets": [1000]},
    ]}, archive=False)

    out = rs.record_rotation_outcome(
        "XLI", entry_price=179.5774, exit_price=182.7601,
        exit_date="2026-07-23", now_iso="2026-07-23T14:06:00+00:00")
    assert out and out["decision_id"] == "XLI:2026-07-20", out
    assert out["status"] == "rebalanced"
    assert out["hit_stop"] is False and out["hit_target"] is False
    assert round(out["pnl_pct"], 4) == round((182.7601 - 179.5774) / 179.5774, 4), out
    assert out["holding_days"] == 3, out
    oe = [e for e in journal()
          if e.get("event") == "outcome" and e.get("decision_id") == "XLI:2026-07-20"]
    assert len(oe) == 1, ("expected exactly 1 outcome event", oe)
    xli = [t for t in store.load_archived("2026-07-20")["theses"] if t["symbol"] == "XLI"][0]
    assert xli.get("outcome", {}).get("decision_id") == "XLI:2026-07-20", "not attached to archive"
    rh = ledger.realized_history(journal())
    assert any(r["decision_id"] == "XLI:2026-07-20" and r["status"] == "rebalanced" for r in rh), rh

    # --- B. idempotent: a re-run does not double-record ---
    rs.record_rotation_outcome("XLI", 179.5774, 182.7601, "2026-07-23", "2026-07-23T14:07:00+00:00")
    oe2 = [e for e in journal()
           if e.get("event") == "outcome" and e.get("decision_id") == "XLI:2026-07-20"]
    assert len(oe2) == 1, ("idempotency broken", len(oe2))

    # --- C. current-book (stop) path still attaches + journals — no regression ---
    o_stop = ledger.outcome_from_exit(
        symbol="MU", as_of="2026-07-22", entry_price=900, exit_price=810,
        stop=800, targets=[1000], exit_reason="stopped",
        entry_date="2026-07-22", exit_date="2026-07-24")
    rs.record_outcome("MU", o_stop, "2026-07-24T14:00:00+00:00")
    mu = [t for t in store.load_current()["theses"] if t["symbol"] == "MU"][0]
    assert mu.get("outcome", {}).get("status") == "stopped", "current-book attach regressed"
    assert any(e.get("decision_id") == "MU:2026-07-22"
               for e in journal() if e.get("event") == "outcome")

    # --- D. no held thesis to anchor to -> returns None, records nothing ---
    assert rs.record_rotation_outcome("ZZZZ", 10, 11, "2026-07-23", "2026-07-23T14:08:00+00:00") is None
    assert not any(e.get("symbol") == "ZZZZ" for e in journal())

    print("selftest OK: rotation-close outcomes (journal+attach+realized_history), "
          "idempotent, current-book path intact, missing-thesis safe")


if __name__ == "__main__":
    main()
