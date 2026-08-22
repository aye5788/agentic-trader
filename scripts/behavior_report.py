#!/usr/bin/env python3
"""OPERATOR REPORT — what the agent decided inside real positions, and what the
market did next.

    .venv/bin/python scripts/behavior_report.py [--as-of YYYY-MM-DD]
                                                [--limit N] [--stdout-only]
                                                [--json PATH]

WHAT THIS IS
    The thin I/O wrapper around src/behavior_measurement.py: it loads the
    journal and the close panel, calls the pure measurement, prints it and
    writes the JSON artifact. Every number is computed in that module; this file
    holds no analysis and must not grow any.

WHO IT IS FOR
    A HUMAN, at a terminal. Nothing here reaches the trading agent: the output
    goes to stdout and to research_store/reviews/behavior_measurement.json, and
    neither is read by brief(), research_log(), the charter, any MCP tool or any
    service. It is not scheduled and does not need to be.

WHAT IT DOES NOT DO
    It does not judge. There is no right/wrong column, no score, no ranking, no
    parameter suggestion and no "lesson". It reports counts, forward returns and
    what could not be measured — and where the sample is small it simply shows N
    and leaves the reading to the operator.

WHAT IT WRITES
    Exactly one file, the JSON artifact, under an existing analytics directory
    (research_store/reviews/, alongside scorecard.json). It appends NOTHING to
    the journal, touches no belief, places no order and calls no broker.
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
OUT = REPO / "research_store" / "reviews" / "behavior_measurement.json"


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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--as-of", default=None,
                    help="treat horizons after this session as not yet elapsed "
                         "(default: the panel's last session)")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap the per-decision detail printed (the JSON keeps all rows)")
    ap.add_argument("--json", default=str(OUT), help=f"artifact path (default {OUT})")
    ap.add_argument("--stdout-only", action="store_true", help="print, write nothing")
    args = ap.parse_args(argv)

    import behavior_measurement as bm
    measurement = bm.measure(_read_journal(JOURNAL), _read_panel(PANEL),
                             as_of=args.as_of)
    print(bm.render(measurement, limit=args.limit))

    if not args.stdout_only:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(measurement, indent=2, sort_keys=True) + "\n")
        print(f"\nwrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
