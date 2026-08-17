"""SHARED TRUTH TABLE for what an override actually does to a thesis.

scripts/market_monitor.py:apply_overrides() is the ONLY authority on enforcement.
src/agent_env/decide.py:evaluate_enforcement() only REPORTS what it will do --
and cannot import it, because market_monitor loads the moomoo SDK at module
scope and runs under a different interpreter (system 3.10 vs .venv 3.12).

That duplication silently diverged: apply_overrides was changed to let targets
move in EITHER direction, and decide.py kept reporting the old lower-only rule,
so the agent was told a legal move was illegal. This table is the pin. Both
sides assert against it, so the next divergence is a failing test instead of a
lie told to the agent.

⛔ STDLIB ONLY. This module is imported by BOTH interpreters.
"""

# Each case: a thesis (stop + targets), an override, and what apply_overrides
# leaves behind. `stop_enforced` / `target_enforced` mean "the level actually
# changed", which is exactly what decide.py claims to predict.
CASES = [
    {"name": "stop raised is applied",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"stop": 108.0, "reason": "tighter"},
     "stop_after": 108.0, "targets_after": [130.0],
     "stop_enforced": True, "target_enforced": False},

    {"name": "stop lowered is ignored without widen",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"stop": 90.0, "reason": "looser"},
     "stop_after": 100.0, "targets_after": [130.0],
     "stop_enforced": False, "target_enforced": False},

    {"name": "stop lowered IS applied with widen + reason",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"stop": 90.0, "widen": True, "reason": "inside the noise"},
     "stop_after": 90.0, "targets_after": [130.0],
     "stop_enforced": True, "target_enforced": False},

    {"name": "widen without a reason is ignored",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"stop": 90.0, "widen": True, "reason": ""},
     "stop_after": 100.0, "targets_after": [130.0],
     "stop_enforced": False, "target_enforced": False},

    # THE DIVERGENCE. apply_overrides permits this; decide.py reported it as
    # ignored, which is why no take-profit has ever been reached.
    {"name": "target RAISED is applied",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"targets": [160.0], "reason": "let it run"},
     "stop_after": 100.0, "targets_after": [160.0],
     "stop_enforced": False, "target_enforced": True},

    {"name": "target lowered is applied",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"targets": [118.0], "reason": "pull it in"},
     "stop_after": 100.0, "targets_after": [118.0],
     "stop_enforced": False, "target_enforced": True},

    # THE BLOCKER. Every live thesis has two targets; set_levels sent one.
    {"name": "target count mismatch is ignored",
     "thesis_stop": 100.0, "thesis_targets": [130.0, 150.0],
     "ov": {"targets": [140.0], "reason": "one of two"},
     "stop_after": 100.0, "targets_after": [130.0, 150.0],
     "stop_enforced": False, "target_enforced": False},

    {"name": "full target list on a two-target thesis is applied",
     "thesis_stop": 100.0, "thesis_targets": [130.0, 150.0],
     "ov": {"targets": [140.0, 170.0], "reason": "both"},
     "stop_after": 100.0, "targets_after": [140.0, 170.0],
     "stop_enforced": False, "target_enforced": True},

    {"name": "malformed override changes nothing",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"stop": "not-a-number", "targets": "nope", "reason": "junk"},
     "stop_after": 100.0, "targets_after": [130.0],
     "stop_enforced": False, "target_enforced": False},
]


def _selftest() -> None:
    assert CASES, "the table must not be empty"
    keys = {"name", "thesis_stop", "thesis_targets", "ov", "stop_after",
            "targets_after", "stop_enforced", "target_enforced"}
    for c in CASES:
        assert keys <= set(c), (c.get("name"), keys - set(c))
    names = [c["name"] for c in CASES]
    assert len(names) == len(set(names)), "case names must be unique"
    print("selftest OK: level_rules -- shared enforcement table is well-formed")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
