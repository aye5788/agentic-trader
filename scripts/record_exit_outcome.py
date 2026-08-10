#!/usr/bin/env python3
"""Journal stop / take-profit exit outcomes (the learning label) — from a fixed input file.

WHY THIS EXISTS AS A SCRIPT. prompts/exit.md step 7c used to tell the agent to
compose "a short python snippet run with `.venv/bin/python`" that imported
ledger + research_store and called record_outcome(...) directly. That instruction
is only executable if the agent holds a permission grant of the shape
`Bash(.venv/bin/python *)` — i.e. "run any python program you compose", which is
unrestricted code execution and defeats every other guardrail in
.claude/settings.json. Worse than the fast loop's equivalent (step 9b, fixed by
scripts/record_rotation_outcome.py): this is the STOP-LOSS path, fired by
scripts/market_monitor.py when a level breaks, so a step that cannot be granted
is a step that hangs on an approval no headless run can give. This script is the
same work behind an EXACT, argument-free command, so the grant can be exact too:

    .venv/bin/python scripts/record_exit_outcome.py

Agent contract (mirrors scripts/record_fills.py and
scripts/record_rotation_outcome.py deliberately):
  1. write research_store/rh/exit_closes.json = a JSON array, one object per
     symbol sold to a FULL close (position now zero) on a STOP or TAKE-PROFIT
     exit — not a weekly rotation, which the fast loop handles:
       [{"symbol":"WDC",
         "entry_price": 61.4200,   # avg cost from the PRE-sell get_equity_positions
         "exit_price":  58.9100,   # the sell's average_price from get_equity_orders
         "exit_date":   "2026-08-06",
         "exit_reason": "stop",    # the exit's `reason` from exit_request.json
         "spy_entry": null, "spy_exit": null},   # optional; omit if unknown
        ...]
  2. run:  .venv/bin/python scripts/record_exit_outcome.py

`entry_price` MUST be the average cost read BEFORE selling. Step 7 of exit.md
overwrites positions.json with post-sell state and drops fully-closed names, so a
re-read cannot supply it — and neither can this script, which is why the number
is an input rather than something it looks up.

stop / targets / as_of are NOT inputs: this script reads them from the symbol's
own thesis (research_store/current.json, falling back to the most recent HELD
archived thesis), so the agent cannot mistype the levels the label is scored
against. hit_stop / hit_target / holding_days are derived from them by
ledger.outcome_from_exit.

Partial (scale-out) sells do not belong here — only full closes get an outcome
(the same first-cut rule as scripts/record_rotation_outcome.py). In practice that
means `stop` and `target2` (fraction 1.0); `target1` sells half and leaves the
position open, so it has no outcome yet.

WHAT THIS DOES NOT COVER. It records the label; it does not verify the sale
happened — record_fills.py journals the execution and reconcile_ledger.py checks
completeness. It cannot detect a wrong `entry_price` or `exit_price`, only that
they are positive numbers. A symbol with no thesis anywhere is reported
UNANCHORED and recorded nowhere, rather than being given an invented parent.

Idempotent: research_store.record_outcome keys on the thesis's decision_id, so
re-running (the monitor may retry a killed executor) never double-attaches.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

CLOSES = REPO / "research_store" / "rh" / "exit_closes.json"

_REQUIRED = ("symbol", "entry_price", "exit_price", "exit_date", "exit_reason")
# ledger.outcome_from_exit takes any string, but a monitor-fired exit is one of
# exactly these and a typo silently mislabels the training data. These are the
# `reason` values scripts/market_monitor.py writes into exit_request.json.
_ALLOWED_REASONS = ("stop", "target1", "target2")


def validate(rows: object) -> list[str]:
    """-> list of problems with the submitted rows. Pure; no I/O, no store."""
    if not isinstance(rows, list):
        return ["exit_closes.json must be a JSON array of close objects"]
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
        reason = row.get("exit_reason")
        if reason is not None and reason != "" and reason not in _ALLOWED_REASONS:
            problems.append(
                f"{who}: 'exit_reason' must be one of {_ALLOWED_REASONS}"
            )
        for key in ("spy_entry", "spy_exit"):
            val = row.get(key)
            if val is None:
                continue
            if isinstance(val, bool) or not isinstance(val, (int, float)) or val <= 0:
                problems.append(f"{who}: {key!r} must be a positive number or null")
    return problems


def find_thesis(symbol: str) -> tuple[str | None, dict | None]:
    """(as_of, thesis) for `symbol` — current book first, archive as fallback.

    The stop/take-profit path normally finds the name in current.json: it was
    held when the level broke and exit.md never rewrites the current product.
    The archive fallback covers a close that lands after the slow loop has
    already rotated the name out of the book (a Sunday-night rebalance between
    the breach and this step), which would otherwise read as unanchored.
    """
    import research_store  # noqa: PLC0415 (needs sys.path)
    from research_store import store  # noqa: PLC0415

    sym = symbol.upper()
    cur = store.load_current() or {}
    for t in cur.get("theses", []):
        if str(t.get("symbol", "")).upper() == sym:
            return t.get("as_of") or cur.get("as_of"), t
    return research_store._last_held_archived_thesis(symbol)


def record(rows: list[dict], now_iso: str) -> tuple[list[str], list[str]]:
    """Attach an outcome to each row's thesis and journal it.

    -> (recorded symbols, unanchored symbols). A symbol is "unanchored" when no
    thesis exists for it in the current book or the archive: the sale is real and
    already journaled by record_fills.py, but there is no decision to attach a
    label to, so the ledger records nothing rather than inventing a parent.
    """
    from ledger import outcome_from_exit  # noqa: PLC0415 (needs sys.path)
    from research_store import record_outcome  # noqa: PLC0415

    recorded: list[str] = []
    unanchored: list[str] = []
    for row in rows:
        symbol = row["symbol"]
        as_of, thesis = find_thesis(symbol)
        if not thesis or not as_of:
            unanchored.append(symbol)
            print(f"UNANCHORED {symbol}: no thesis in the book or the archive — "
                  f"no outcome recorded")
            continue
        outcome = outcome_from_exit(
            symbol=symbol,
            as_of=as_of,
            entry_price=float(row["entry_price"]),
            exit_price=float(row["exit_price"]),
            stop=thesis.get("stop"),
            targets=thesis.get("targets"),
            exit_reason=row["exit_reason"],
            entry_date=as_of,
            exit_date=row["exit_date"],
            spy_entry=row.get("spy_entry"),
            spy_exit=row.get("spy_exit"),
        )
        record_outcome(symbol, outcome, now_iso)
        recorded.append(symbol)
        pnl = outcome.get("pnl_pct")
        shown = f"{pnl:.4f}" if isinstance(pnl, (int, float)) else "n/a"
        print(f"recorded {symbol}: status={outcome.get('status')} "
              f"pnl_pct={shown} hit_stop={outcome.get('hit_stop')}")
    return recorded, unanchored


def main() -> None:
    if not CLOSES.exists():
        print(f"no exit-close file at {CLOSES} — nothing to record")
        return
    rows = json.loads(CLOSES.read_text())
    problems = validate(rows)
    if problems:
        for p in problems:
            print(f"ERROR: {p}", file=sys.stderr)
        sys.exit(f"{len(problems)} problem(s) in {CLOSES} — nothing recorded")
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    recorded, unanchored = record(rows, now_iso)
    print(f"exit outcomes: {len(recorded)} recorded, "
          f"{len(unanchored)} unanchored, {len(rows)} submitted")


def _selftest() -> None:
    # ---------------- validate(): pure input gate ----------------
    good = [{"symbol": "WDC", "entry_price": 61.42, "exit_price": 58.91,
             "exit_date": "2026-08-06", "exit_reason": "stop"}]
    assert validate(good) == [], validate(good)
    assert validate([dict(good[0], exit_reason="target2")]) == []
    assert validate([dict(good[0], spy_entry=None, spy_exit=None)]) == []
    assert validate([dict(good[0], spy_entry=640.0, spy_exit=651.0)]) == []
    assert validate({}) == ["exit_closes.json must be a JSON array of close objects"]
    assert "not a JSON object" in validate(["WDC"])[0]

    # exit_reason is REQUIRED here (unlike a rotation close, which has a sane
    # default): "stop" vs "target2" is the label, and guessing it is a lie.
    no_reason = [{k: v for k, v in good[0].items() if k != "exit_reason"}]
    assert any("'exit_reason'" in p and "missing" in p for p in validate(no_reason)), \
        validate(no_reason)

    missing = validate([{"symbol": "WDC", "exit_price": 1.0,
                         "exit_date": "2026-08-06", "exit_reason": "stop"}])
    assert any("'entry_price'" in p and "missing" in p for p in missing), missing

    for bad_price in (0, -1, "61.42", True):
        rows = [dict(good[0], entry_price=bad_price)]
        assert validate(rows), f"must reject entry_price={bad_price!r}"
    assert any("positive number" in p for p in validate([dict(good[0], exit_price=0)]))
    assert any("YYYY-MM-DD" in p for p in validate([dict(good[0], exit_date="8/6/26")]))
    # a rotation reason must NOT slip in here — it belongs to record_rotation_outcome
    assert any("exit_reason" in p for p in validate([dict(good[0], exit_reason="rebalanced")]))
    assert any("positive number" in p for p in validate([dict(good[0], spy_entry=0)]))

    # ---------------- record(): end-to-end against a TEMP store ----------------
    import tempfile
    import research_store.store as store

    tmp = Path(tempfile.mkdtemp())
    saved = (store.STORE_DIR, store.CURRENT, store.ARCHIVE, store.JOURNAL)
    store.STORE_DIR, store.CURRENT = tmp, tmp / "current.json"
    store.ARCHIVE, store.JOURNAL = tmp / "archive", tmp / "journal.jsonl"
    try:
        # WDC is still in the CURRENT book (the normal stop-exit case); XLE was
        # rotated out but has a HELD archived thesis (the fallback case).
        store.save_archived("2026-08-03", {"as_of": "2026-08-03", "theses": [
            {"symbol": "XLE", "as_of": "2026-08-03", "target_weight": 0.07,
             "stop": 88.0, "targets": [99.0, 104.0]},
        ]})
        store.save_current({"as_of": "2026-08-04", "theses": [
            {"symbol": "WDC", "as_of": "2026-08-04", "target_weight": 0.08,
             "stop": 59.5, "targets": [70.0, 76.0]},
        ]}, archive=False)

        rows = [good[0],
                {"symbol": "XLE", "entry_price": 90.0, "exit_price": 100.0,
                 "exit_date": "2026-08-06", "exit_reason": "target2"},
                {"symbol": "NOPE", "entry_price": 10.0, "exit_price": 11.0,
                 "exit_date": "2026-08-06", "exit_reason": "stop"}]
        recorded, unanchored = record(rows, "2026-08-06T18:04:00+00:00")
        assert recorded == ["WDC", "XLE"], recorded
        assert unanchored == ["NOPE"], unanchored  # no thesis -> label, not invention

        outcomes = [e for e in store.read_journal() if e.get("event") == "outcome"]
        assert len(outcomes) == 2, outcomes
        by_id = {e["decision_id"]: e["outcome"] for e in outcomes}
        assert "WDC:2026-08-04" in by_id, by_id           # current-book thesis
        assert "XLE:2026-08-03" in by_id, by_id           # archived-held fallback

        # the levels come from the THESIS, not from the agent: 58.91 <= stop 59.50
        assert by_id["WDC:2026-08-04"]["hit_stop"] is True, by_id["WDC:2026-08-04"]
        assert by_id["WDC:2026-08-04"]["hit_target"] is False
        assert by_id["WDC:2026-08-04"]["status"] == "stop"
        # ...and 100.0 >= targets[0] 99.0 on the archived thesis
        assert by_id["XLE:2026-08-03"]["hit_target"] is True, by_id["XLE:2026-08-03"]
        assert by_id["XLE:2026-08-03"]["hit_stop"] is False

        # the label is attached to the belief, not just journaled
        cur = store.load_current()
        assert cur["theses"][0].get("outcome"), "current thesis must carry the label"

        # idempotent: the monitor can kill and retry this executor mid-prompt
        record(rows, "2026-08-06T19:00:00+00:00")
        again = [e for e in store.read_journal() if e.get("event") == "outcome"]
        assert len(again) == 2, ("re-run must not double-attach", again)
    finally:
        store.STORE_DIR, store.CURRENT, store.ARCHIVE, store.JOURNAL = saved

    print("selftest OK: record_exit_outcome -- input gate bites, stop/target "
          "levels come from the thesis not the agent, archived fallback anchors "
          "a rotated-out close, unanchored symbol is reported not invented, "
          "re-run is idempotent")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
