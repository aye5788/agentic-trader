"""Human-in-the-loop promotion helper for adaptive proposals.

Reads a proposal and prints the exact config/strategy.local.toml stanza to paste
to ACCEPT it (propose-not-apply, spec §8). Never edits config itself — promotion
is a deliberate human act. Flags a proposal that recommends a move but hasn't been
promoted.

    python scripts/promote_proposal.py [--selftest]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROPOSAL = REPO / "research_store" / "adaptive" / "proposals" / "stop_atr_mult.json"


def pending_line(proposal: dict) -> str:
    p = proposal
    return (f"# {p['provenance']}\n"
            f"[trade_management]\n"
            f"stop_atr_mult = {p['recommended']}")


def main():
    if not PROPOSAL.exists():
        print("no proposal found:", PROPOSAL)
        return
    prop = json.loads(PROPOSAL.read_text())
    print(f"proposal for {prop['knob']} generated {prop['generated_at']}")
    print(f"  incumbent={prop['incumbent']}  recommended={prop['recommended']}  "
          f"moved={prop['moved']}  p_better={prop['p_better']}  oos_gap={prop['oos_gap']}")
    print(f"  evidence: {prop['evidence']}")
    print(f"  rationale: {prop['rationale']}")
    if prop["moved"]:
        print("\nTo ACCEPT, append to config/strategy.local.toml:\n")
        print(pending_line(prop))
    else:
        print("\nNo change recommended — nothing to promote.")


def _selftest() -> None:
    prop = {"recommended": 3.0, "moved": True,
            "provenance": "adaptive layer 2026-07-26 from replay_n=4120 live_n=7; incumbent was 2.5"}
    line = pending_line(prop)
    assert "stop_atr_mult = 3.0" in line, line
    assert "[trade_management]" in line, line
    assert line.startswith("# adaptive layer"), line
    print("selftest OK: pending_line")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
