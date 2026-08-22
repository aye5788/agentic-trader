#!/usr/bin/env python3
"""OPERATOR BUILDER — rebuild the institutional evidence artifact from experience.

    .venv/bin/python scripts/build_institutional_evidence.py
                        [--as-of YYYY-MM-DD] [--window-days 180]
                        [--json PATH] [--stdout-only] [--show-agent-block]

WHAT THIS IS
    The operator front end for src/evidence_build.py, which loads the journal
    and the close panel, runs the existing Step-5 measurement, calls the pure
    builder and writes research_store/reviews/institutional_evidence.json
    atomically. Every number is computed in behavior_measurement and
    institutional_evidence; this file holds no analysis and no orchestration —
    it parses flags and prints.

    ⛔ NOT THE ONLY CALLER ANY MORE. scripts/session.py runs the SAME
    evidence_build.rebuild() at session startup, before it renders anything into
    the agent's context, so the loop is self-operating and this command is for
    inspection rather than for keeping the artifact alive.

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
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import evidence_build                                        # noqa: E402

#: Paths and the whole rebuild live in src/evidence_build.py, which the SESSION
#: RUNNER also calls at startup. One implementation on purpose: two copies of
#: "load journal, load panel, measure, build, write" drift, and they drift in
#: the direction where the operator checks one and the session runs the other.
JOURNAL = evidence_build.JOURNAL
PANEL = evidence_build.PANEL
OUT = evidence_build.ARTIFACT


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--as-of", default=None,
                    help="EVIDENCE as-of: the window end and the date staleness "
                         "is measured from (default: the panel's last session). "
                         "It does NOT move the measurement frontier — what can "
                         "be measured is bounded by the panel either way.")
    ap.add_argument("--window-days", type=int, default=None,
                    help="rolling evidence window in calendar days")
    ap.add_argument("--json", default=str(OUT), help=f"artifact path (default {OUT})")
    ap.add_argument("--stdout-only", action="store_true", help="print, write nothing")
    ap.add_argument("--show-agent-block", action="store_true",
                    help="also print the block a session would receive (empty "
                         "when nothing is validated)")
    args = ap.parse_args(argv)

    import institutional_evidence as ie

    # THE SAME CALL THE SESSION RUNNER MAKES. --stdout-only computes the artifact
    # and writes nothing, which is also how the session dry run inspects it.
    try:
        evidence = evidence_build.rebuild(evidence_as_of=args.as_of,
                                          out_path=Path(args.json),
                                          window_days=args.window_days,
                                          write=not args.stdout_only)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    print(ie.render_operator(evidence))

    if args.show_agent_block:
        block = ie.render_agent_block(evidence)
        print("\n--- block delivered to a session "
              f"({len(block)} chars) ---")
        print(block if block else "(nothing validated — no block is rendered)")
        for problem in ie.check_block(block):
            print(f"⚠️ {problem}", file=sys.stderr)

    if not args.stdout_only:
        print(f"\nwrote {Path(args.json)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
