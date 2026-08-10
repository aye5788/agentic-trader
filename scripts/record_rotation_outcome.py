#!/usr/bin/env python3
"""Journal rotation-close outcomes (the learning label) — from a fixed input file.

WHY THIS EXISTS AS A SCRIPT. prompts/fast_loop.md step 9b used to tell the agent
to "run one short `.venv/bin/python` snippet" that imported research_store and
called record_rotation_outcome(...) directly. That instruction is only executable
if the agent holds a permission grant of the shape `Bash(.venv/bin/python *)` —
i.e. "run any python program you compose", which is unrestricted code execution
and defeats every other guardrail in .claude/settings.json. This script is the
same work behind an EXACT, argument-free command, so the grant can be exact too:

    .venv/bin/python scripts/record_rotation_outcome.py

Agent contract (mirrors scripts/record_fills.py deliberately):
  1. write research_store/rh/rotation_closes.json = a JSON array, one object per
     symbol sold to a FULL close (position now zero) on a ROTATION out of the
     book — not a stop/take-profit exit, which prompts/exit.md handles:
       [{"symbol":"XLI",
         "entry_price": 179.5774,   # avg cost from the PRE-sell positions snapshot
         "exit_price":  182.7601,   # the sell's average_price from get_equity_orders
         "exit_date":   "2026-07-23",
         "exit_reason": "rebalanced"},   # or "regime_off"; optional, defaults to "rebalanced"
        ...]
  2. run:  .venv/bin/python scripts/record_rotation_outcome.py

`entry_price` MUST be the average cost read BEFORE selling. Step 8 of the fast
loop overwrites positions.json with post-sell state and drops fully-closed names,
so a re-read cannot supply it.

Partial (trim) sells do not belong here — only full closes get an outcome (the
same first-cut rule as prompts/exit.md step 7c).

Idempotent: research_store.record_rotation_outcome keys on the archived thesis's
decision_id, so re-running never double-attaches an outcome.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

CLOSES = REPO / "research_store" / "rh" / "rotation_closes.json"

_REQUIRED = ("symbol", "entry_price", "exit_price", "exit_date")
# research_store._outcome_from_exit takes any string, but a rotation close is one
# of exactly two things and a typo here silently mislabels the training data.
_ALLOWED_REASONS = ("rebalanced", "regime_off")


def validate(rows: object) -> list[str]:
    """-> list of problems with the submitted rows. Pure; no I/O, no store."""
    if not isinstance(rows, list):
        return ["rotation_closes.json must be a JSON array of close objects"]
    problems: list[str] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            problems.append(f"entry {i}: not a JSON object")
            continue
        who = f"entry {i} ({row.get('symbol', '?')})"
        for key in _REQUIRED:
            if row.get(key) in (None, ""):
                problems.append(f"{who}: missing required field {key!r}")
        for key in ("entry_price", "exit_price"):
            val = row.get(key)
            if val is None:
                continue
            if isinstance(val, bool) or not isinstance(val, (int, float)) or val <= 0:
                problems.append(f"{who}: {key!r} must be a positive number")
        date = row.get("exit_date")
        if date is not None and not (
            isinstance(date, str) and len(date) == 10
            and date[4] == "-" and date[7] == "-"
        ):
            problems.append(f"{who}: 'exit_date' must be YYYY-MM-DD")
        reason = row.get("exit_reason", "rebalanced")
        if reason not in _ALLOWED_REASONS:
            problems.append(
                f"{who}: 'exit_reason' must be one of {_ALLOWED_REASONS}"
            )
    return problems


def record(rows: list[dict], now_iso: str) -> tuple[list[str], list[str]]:
    """Attach an outcome to each row's archived thesis.

    -> (recorded symbols, unanchored symbols). A symbol is "unanchored" when no
    HELD archived thesis exists for it: the sale is real and already journaled by
    record_fills.py, but there is no decision to attach a label to, so the ledger
    records nothing rather than inventing a parent.
    """
    from research_store import record_rotation_outcome  # noqa: PLC0415 (needs sys.path)

    recorded: list[str] = []
    unanchored: list[str] = []
    for row in rows:
        symbol = row["symbol"]
        outcome = record_rotation_outcome(
            symbol,
            entry_price=float(row["entry_price"]),
            exit_price=float(row["exit_price"]),
            exit_date=row["exit_date"],
            now_iso=now_iso,
            exit_reason=row.get("exit_reason", "rebalanced"),
        )
        if outcome is None:
            unanchored.append(symbol)
            print(f"UNANCHORED {symbol}: no held archived thesis — no outcome recorded")
        else:
            recorded.append(symbol)
            pnl = outcome.get("pnl_pct")
            shown = f"{pnl:.4f}" if isinstance(pnl, (int, float)) else "n/a"
            print(f"recorded {symbol}: status={outcome.get('status')} pnl_pct={shown}")
    return recorded, unanchored


def main() -> None:
    if not CLOSES.exists():
        print(f"no rotation-close file at {CLOSES} — nothing to record")
        return
    rows = json.loads(CLOSES.read_text())
    problems = validate(rows)
    if problems:
        for p in problems:
            print(f"ERROR: {p}", file=sys.stderr)
        sys.exit(f"{len(problems)} problem(s) in {CLOSES} — nothing recorded")
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    recorded, unanchored = record(rows, now_iso)
    print(f"rotation outcomes: {len(recorded)} recorded, "
          f"{len(unanchored)} unanchored, {len(rows)} submitted")


def _selftest() -> None:
    # ---------------- validate(): pure input gate ----------------
    good = [{"symbol": "XLI", "entry_price": 179.5774, "exit_price": 182.7601,
             "exit_date": "2026-07-23", "exit_reason": "rebalanced"}]
    assert validate(good) == [], validate(good)
    # exit_reason is optional and defaults to "rebalanced"
    assert validate([{k: v for k, v in good[0].items() if k != "exit_reason"}]) == []
    assert validate({}) == ["rotation_closes.json must be a JSON array of close objects"]
    assert "not a JSON object" in validate(["XLI"])[0]

    missing = validate([{"symbol": "XLI", "exit_price": 1.0, "exit_date": "2026-07-23"}])
    assert any("'entry_price'" in p and "missing" in p for p in missing), missing

    for bad_price in (0, -1, "179.58", True, None):
        rows = [dict(good[0], entry_price=bad_price)]
        assert validate(rows), f"must reject entry_price={bad_price!r}"
    assert any("positive number" in p for p in validate([dict(good[0], exit_price=0)]))
    assert any("YYYY-MM-DD" in p for p in validate([dict(good[0], exit_date="7/23/26")]))
    assert any("exit_reason" in p for p in validate([dict(good[0], exit_reason="stop")]))
    # a real exit_reason that is NOT a rotation must not slip through as one
    assert validate([dict(good[0], exit_reason="regime_off")]) == []

    # ---------------- record(): end-to-end against a TEMP store ----------------
    import tempfile
    import research_store.store as store

    tmp = Path(tempfile.mkdtemp())
    saved = (store.STORE_DIR, store.CURRENT, store.ARCHIVE, store.JOURNAL)
    store.STORE_DIR, store.CURRENT = tmp, tmp / "current.json"
    store.ARCHIVE, store.JOURNAL = tmp / "archive", tmp / "journal.jsonl"
    try:
        store.save_archived("2026-07-20", {"as_of": "2026-07-20", "theses": [
            {"symbol": "XLI", "as_of": "2026-07-20", "target_weight": 0.075,
             "stop": 173.46, "targets": [188.37, 196.76]},
        ]})
        store.save_current({"as_of": "2026-07-22", "theses": []}, archive=False)

        rows = [good[0], {"symbol": "NOPE", "entry_price": 10.0, "exit_price": 11.0,
                          "exit_date": "2026-07-23"}]
        recorded, unanchored = record(rows, "2026-07-23T14:06:00+00:00")
        assert recorded == ["XLI"], recorded
        assert unanchored == ["NOPE"], unanchored   # no thesis -> label, not invention

        outcomes = [e for e in store.read_journal() if e.get("event") == "outcome"]
        assert len(outcomes) == 1, outcomes
        assert outcomes[0]["decision_id"] == "XLI:2026-07-20", outcomes[0]

        # idempotent: the whole point of running this from a prompt that may retry
        record(rows, "2026-07-23T15:00:00+00:00")
        again = [e for e in store.read_journal() if e.get("event") == "outcome"]
        assert len(again) == 1, ("re-run must not double-attach", again)
    finally:
        store.STORE_DIR, store.CURRENT, store.ARCHIVE, store.JOURNAL = saved

    print("selftest OK: record_rotation_outcome -- input gate bites, rotation "
          "close attaches to its archived thesis, unanchored symbol is reported "
          "not invented, re-run is idempotent")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
