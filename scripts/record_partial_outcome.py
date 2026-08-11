#!/usr/bin/env python3
"""Journal PARTIAL closes (trims) — the de-risking record — from a fixed input file.

WHY THIS EXISTS AS A SCRIPT. Same reason as scripts/record_exit_outcome.py and
scripts/record_rotation_outcome.py: a prompt step that says "run a short
`.venv/bin/python` snippet" is only executable if the agent holds a grant of the
shape `Bash(.venv/bin/python *)` — "run any python program you compose" — which
is unrestricted code execution and defeats every deny rule in
deploy/loop_settings.json. This script is the same work behind an EXACT,
argument-free command, so the grant can be exact too:

    .venv/bin/python scripts/record_partial_outcome.py

THE FAILURE THIS PREVENTS. A trim used to write NOTHING to the ledger. On
2026-08-10 the risk overlay sold ~1/3 of AMAT at +6.53% ahead of earnings — a
correct, deliberate de-risk — and `performance()` still read "17 closed trades,
0 targets hit, avg trade −6.1%", because a partial sale left no outcome event of
any kind. The agent judges its own approach from that tool, so the record
understated exactly the behaviour it should be reinforcing. This makes a trim
visible WITHOUT pretending it is a round trip: the event is `partial_outcome`,
the thesis stays open, and win_rate never sees it.

Agent contract (mirrors scripts/record_exit_outcome.py deliberately):
  1. write research_store/rh/partial_closes.json = a JSON array, one object per
     symbol sold PARTIALLY (position still open afterwards):
       [{"symbol":"AMAT",
         "fraction":     0.33,        # portion of the position SOLD, 0 < f < 1
         "entry_price":  501.32,      # avg cost from the PRE-sell positions read
         "exit_price":   534.0401,    # the sell's average_price from get_equity_orders
         "exit_date":    "2026-08-10",
         "exit_reason":  "trim"},     # "trim" (risk review), "target1" (monitor),
        ...]                          # or "rebalance" (fast loop rebalance-down)
  2. run:  .venv/bin/python scripts/record_partial_outcome.py

`entry_price` MUST be the average cost read BEFORE selling, for the same reason
as the full-close scripts: the reconcile step overwrites positions.json with
post-sell state.

`fraction` >= 1.0 is REFUSED. That is a full close and belongs to
record_exit_outcome.py (stop/take-profit) or record_rotation_outcome.py
(rotation) — those close and archive the thesis, which a trim must not do.

WHAT THIS DOES NOT COVER. It records the label; it does not verify the sale
happened (record_fills.py journals the execution). It cannot detect a wrong
price or a wrong fraction, only that they are numbers in range. It computes no
hit_stop/hit_target — a trim is a sizing decision, not a level being reached.
A symbol with no thesis anywhere is reported UNANCHORED and recorded nowhere,
rather than being given an invented parent.

Idempotent on the COMPOSITE key (decision_id, exit_date, exit_price): a name can
legitimately be trimmed more than once under one decision, so a second, genuinely
different trim records, while a re-run of the same one is a no-op.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

CLOSES = REPO / "research_store" / "rh" / "partial_closes.json"

_REQUIRED = ("symbol", "fraction", "entry_price", "exit_price", "exit_date", "exit_reason")
# research_store.record_partial_outcome takes any string, but a partial sale in
# this system is one of exactly three things and a typo silently mislabels the
# training data: "trim" is the risk review's kind (prompts/risk_review.md step
# 6b), "target1" is the monitor's scale-out reason in exit_request.json
# (prompts/exit.md step 7d), "rebalance" is the fast loop's reduce-but-don't-close
# order reason (prompts/fast_loop.md step 9c).
_ALLOWED_REASONS = ("trim", "target1", "rebalance")


def validate(rows: object) -> list[str]:
    """-> list of problems with the submitted rows. Pure; no I/O, no store."""
    if not isinstance(rows, list):
        return ["partial_closes.json must be a JSON array of partial-close objects"]
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
        frac = row.get("fraction")
        if frac is not None and frac != "":
            if isinstance(frac, bool) or not isinstance(frac, (int, float)):
                problems.append(f"{who}: 'fraction' must be a number in (0, 1)")
            elif frac >= 1:
                problems.append(
                    f"{who}: 'fraction' {frac} is a FULL close — record it with "
                    f"record_exit_outcome.py (stop/take-profit) or "
                    f"record_rotation_outcome.py (rotation), not here")
            elif frac <= 0:
                problems.append(f"{who}: 'fraction' must be > 0 and < 1")
        date = row.get("exit_date")
        if date is not None and not (
            isinstance(date, str) and len(date) == 10
            and date[4] == "-" and date[7] == "-"
        ):
            problems.append(f"{who}: 'exit_date' must be YYYY-MM-DD")
        reason = row.get("exit_reason")
        if reason is not None and reason != "" and reason not in _ALLOWED_REASONS:
            problems.append(f"{who}: 'exit_reason' must be one of {_ALLOWED_REASONS}")
    return problems


def record(rows: list[dict], now_iso: str) -> tuple[list[str], list[str]]:
    """Journal a `partial_outcome` event per row.

    -> (recorded symbols, unanchored symbols). A symbol is "unanchored" when no
    thesis exists for it in the current book or the archive: the sale is real and
    already journaled by record_fills.py, but there is no decision to attach a
    label to, so the ledger records nothing rather than inventing a parent.
    """
    from research_store import record_partial_outcome  # noqa: PLC0415 (needs sys.path)

    recorded: list[str] = []
    unanchored: list[str] = []
    for row in rows:
        symbol = row["symbol"]
        ev = record_partial_outcome(
            symbol,
            fraction=float(row["fraction"]),
            entry_price=float(row["entry_price"]),
            exit_price=float(row["exit_price"]),
            exit_date=row["exit_date"],
            now_iso=now_iso,
            exit_reason=row["exit_reason"],
        )
        if ev is None:
            unanchored.append(symbol)
            print(f"UNANCHORED {symbol}: no thesis in the book or the archive — "
                  f"no partial outcome recorded")
        else:
            recorded.append(symbol)
            print(f"recorded {symbol}: partial_outcome fraction={ev['fraction']} "
                  f"pnl_pct={ev['pnl_pct']:.4f} reason={ev['exit_reason']}")
    return recorded, unanchored


def main() -> None:
    if not CLOSES.exists():
        print(f"no partial-close file at {CLOSES} — nothing to record")
        return
    rows = json.loads(CLOSES.read_text())
    problems = validate(rows)
    if problems:
        for p in problems:
            print(f"ERROR: {p}", file=sys.stderr)
        sys.exit(f"{len(problems)} problem(s) in {CLOSES} — nothing recorded")
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    recorded, unanchored = record(rows, now_iso)
    print(f"partial outcomes: {len(recorded)} recorded, "
          f"{len(unanchored)} unanchored, {len(rows)} submitted")


def _selftest() -> None:
    # ---------------- validate(): pure input gate ----------------
    good = [{"symbol": "AMAT", "fraction": 0.33, "entry_price": 501.32,
             "exit_price": 534.0401, "exit_date": "2026-08-10",
             "exit_reason": "trim"}]
    assert validate(good) == [], validate(good)
    assert validate([dict(good[0], exit_reason="target1", fraction=0.5)]) == []
    assert validate([dict(good[0], exit_reason="rebalance", fraction=0.2)]) == []
    assert validate({}) == [
        "partial_closes.json must be a JSON array of partial-close objects"]
    assert "not a JSON object" in validate(["AMAT"])[0]

    for key in _REQUIRED:
        rows = [{k: v for k, v in good[0].items() if k != key}]
        assert any(f"{key!r}" in p and "missing" in p for p in validate(rows)), \
            (key, validate(rows))

    # a FULL close must be refused HERE and pointed at the scripts that close a
    # thesis — recording it as a partial would leave the position open forever.
    for full in (1.0, 1, 1.5):
        problems = validate([dict(good[0], fraction=full)])
        assert any("FULL close" in p for p in problems), (full, problems)
    for bad_frac in (0, -0.2, "0.33", True):
        assert validate([dict(good[0], fraction=bad_frac)]), \
            f"must reject fraction={bad_frac!r}"

    for bad_price in (0, -1, "501.32", True):
        assert validate([dict(good[0], entry_price=bad_price)]), \
            f"must reject entry_price={bad_price!r}"
    assert any("positive number" in p for p in validate([dict(good[0], exit_price=0)]))
    assert any("YYYY-MM-DD" in p for p in validate([dict(good[0], exit_date="8/10/26")]))
    # a FULL-close reason must not slip in wearing a partial's clothes
    for wrong in ("stop", "target2", "rebalanced"):
        assert any("exit_reason" in p for p in validate([dict(good[0], exit_reason=wrong)]))

    # ---------------- record(): end-to-end against a TEMP store ----------------
    import tempfile
    import research_store.store as store

    tmp = Path(tempfile.mkdtemp())
    saved = (store.STORE_DIR, store.CURRENT, store.ARCHIVE, store.JOURNAL)
    store.STORE_DIR, store.CURRENT = tmp, tmp / "current.json"
    store.ARCHIVE, store.JOURNAL = tmp / "archive", tmp / "journal.jsonl"
    try:
        # AMAT is still in the CURRENT book (the normal trim case — a trim never
        # removes the name); XLE was rotated out but has a HELD archived thesis.
        store.save_archived("2026-08-06", {"as_of": "2026-08-06", "theses": [
            {"symbol": "XLE", "as_of": "2026-08-06", "target_weight": 0.07,
             "stop": 55.0, "targets": [62.0, 66.0]},
        ]})
        store.save_current({"as_of": "2026-08-09", "theses": [
            {"symbol": "AMAT", "as_of": "2026-08-09", "target_weight": 0.09,
             "stop": 489.0, "targets": [560.0, 590.0]},
        ]}, archive=False)

        rows = [good[0],
                {"symbol": "XLE", "fraction": 0.5, "entry_price": 57.35,
                 "exit_price": 59.18, "exit_date": "2026-08-10",
                 "exit_reason": "target1"},
                {"symbol": "NOPE", "fraction": 0.25, "entry_price": 10.0,
                 "exit_price": 11.0, "exit_date": "2026-08-10",
                 "exit_reason": "trim"}]
        recorded, unanchored = record(rows, "2026-08-10T16:10:00+00:00")
        assert recorded == ["AMAT", "XLE"], recorded
        assert unanchored == ["NOPE"], unanchored  # no thesis -> label, not invention

        partials = [e for e in store.read_journal()
                    if e.get("event") == "partial_outcome"]
        assert len(partials) == 2, partials
        by_id = {e["decision_id"]: e for e in partials}
        assert "AMAT:2026-08-09" in by_id, by_id       # current-book thesis
        assert "XLE:2026-08-06" in by_id, by_id        # archived-held fallback
        amat = by_id["AMAT:2026-08-09"]
        assert amat["fraction"] == 0.33 and amat["pnl_pct"] == 0.0653, amat
        assert amat["exit_reason"] == "trim", amat

        # THE POINT: a trim must NOT close the thesis. No `outcome` event, and
        # the belief carries no outcome — the position is still open.
        assert not [e for e in store.read_journal() if e.get("event") == "outcome"]
        assert not (store.load_current()["theses"][0].get("outcome")), \
            "a trim must not attach a closing outcome to a live thesis"

        # idempotent on a re-run (the loop may retry this step)...
        record(rows, "2026-08-10T17:00:00+00:00")
        again = [e for e in store.read_journal() if e.get("event") == "partial_outcome"]
        assert len(again) == 2, ("re-run must not duplicate", again)

        # ...but a SECOND, genuinely different trim of the same name records:
        # same decision, different day and price.
        record([dict(good[0], exit_price=545.0, exit_date="2026-08-12")],
               "2026-08-12T16:00:00+00:00")
        third = [e for e in store.read_journal() if e.get("event") == "partial_outcome"]
        assert len(third) == 3, ("a second real trim must record", third)

        # the store-level guard refuses a full close outright, not just the file gate
        from research_store import record_partial_outcome  # noqa: PLC0415
        try:
            record_partial_outcome("AMAT", fraction=1.0, entry_price=501.32,
                                   exit_price=534.04, exit_date="2026-08-10",
                                   now_iso="2026-08-10T16:10:00+00:00",
                                   exit_reason="trim")
        except ValueError as e:
            assert "FULL close" in str(e), e
        else:
            raise AssertionError("fraction=1.0 must raise, not record a partial")
    finally:
        store.STORE_DIR, store.CURRENT, store.ARCHIVE, store.JOURNAL = saved

    print("selftest OK: record_partial_outcome -- input gate bites, a full close "
          "is refused at both the file gate and the store, a trim anchors to its "
          "thesis WITHOUT closing it, unanchored symbol is reported not invented, "
          "re-run is idempotent while a second real trim still records")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
