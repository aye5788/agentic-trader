#!/usr/bin/env python3
"""OPERATOR BUILDER — rebuild the institutional evidence artifact from experience.

    .venv/bin/python scripts/build_institutional_evidence.py
                        [--as-of YYYY-MM-DD] [--window-days 180]
                        [--json PATH] [--stdout-only] [--show-agent-block]

WHAT THIS IS
    The thin I/O wrapper around src/institutional_evidence.py: it loads the
    journal and the close panel, runs the existing Step-5 measurement, calls the
    pure builder and writes research_store/reviews/institutional_evidence.json.
    Every number is computed in those two modules; this file holds no analysis
    and must not grow any.

WHO WRITES EVIDENCE
    THIS SCRIPT, run by an operator. Not the trading agent, which has no write
    path to the artifact and no MCP tool that reaches this file, and not any
    model — there is no LLM anywhere in this pipeline. The agent's only contact
    with the result is the rendered text block a session hands it.

REBUILT, NEVER MUTATED
    The artifact is recomputed from source experience inside the rolling window
    on every run. The previous artifact is read for exactly ONE fact — whether an
    item ever met the validated thresholds — because "decayed below sufficiency"
    is otherwise indistinguishable from "never reached it". Nothing else is
    carried forward, so a statistic here can never drift from the data.

WHAT IT DOES NOT DO
    It appends nothing to the journal, touches no belief, places no order, calls
    no broker, and is not scheduled. It cannot promote evidence: promotion is a
    threshold on sample size computed in the pure module, not an operator act.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

JOURNAL = REPO / "research_store" / "journal.jsonl"
PANEL = REPO / "research_store" / "prices" / "closes.parquet"
OUT = REPO / "research_store" / "reviews" / "institutional_evidence.json"


def _read_journal(path: Path) -> list:
    """The journal, read-only. A malformed line is REPORTED, never guessed at."""
    if not path.exists():
        return []
    out = []
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            print(f"journal line {n} is not JSON ({exc}); skipped", file=sys.stderr)
    return out


def _read_panel(path: Path) -> dict:
    import behavior_measurement as bm
    if not path.exists():
        print(f"no price panel at {path}; every return will read unavailable",
              file=sys.stderr)
        return {"sessions": [], "series": {}}
    import pandas as pd
    return bm.panel_from_frame(pd.read_parquet(path))


def _read_previous(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"previous artifact at {path} is unreadable ({exc}); treating this "
              f"as the first build — no item will be reported stale on decay",
              file=sys.stderr)
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--as-of", default=None,
                    help="the window end and the date staleness is measured "
                         "from (default: the panel's last session)")
    ap.add_argument("--window-days", type=int, default=None,
                    help="rolling evidence window in calendar days")
    ap.add_argument("--json", default=str(OUT), help=f"artifact path (default {OUT})")
    ap.add_argument("--stdout-only", action="store_true", help="print, write nothing")
    ap.add_argument("--show-agent-block", action="store_true",
                    help="also print the block a session would receive (empty "
                         "when nothing is validated)")
    args = ap.parse_args(argv)

    import behavior_measurement as bm
    import institutional_evidence as ie
    import lifecycle_outcomes as lo

    journal = _read_journal(JOURNAL)
    panel = _read_panel(PANEL)

    # THE CLOCK LIVES HERE, NOT IN THE PURE MODULES. Absent an explicit --as-of
    # the frontier is the panel's own last session: what has elapsed is a fact
    # about the data, and reading `now` would make two runs over identical
    # inputs produce different artifacts.
    as_of = args.as_of or (panel["sessions"][-1] if panel["sessions"] else None)
    if not as_of:
        print("no --as-of and no price panel: there is no date to build an "
              "evidence window against", file=sys.stderr)
        return 2

    measurement = bm.measure(journal, panel, as_of=as_of)
    evidence = ie.build(
        measurement,
        as_of=as_of,
        window_days=args.window_days or ie.WINDOW_DAYS,
        previous=_read_previous(Path(args.json)),
        reasons=ie.reason_index(lo.project(journal)),
    )

    print(ie.render_operator(evidence))

    if args.show_agent_block:
        block = ie.render_agent_block(evidence)
        print("\n--- block delivered to a session "
              f"({len(block)} chars) ---")
        print(block if block else "(nothing validated — no block is rendered)")
        for problem in ie.check_block(block):
            print(f"⚠️ {problem}", file=sys.stderr)

    if not args.stdout_only:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
        print(f"\nwrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
