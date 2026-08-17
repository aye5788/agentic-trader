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

    # ---- APPLY-TIME PRICE GUARD (added 2026-08-17, Part 3 of the levels-
    # persistence plan) -- levels no longer expire, so a stop override can
    # outlive its position and wake up on RE-ENTRY at a price it was never
    # written for. `prices` is a NEW, OPTIONAL key: cases above omit it, which
    # means "prices not supplied to apply_overrides for this case" (the
    # pre-guard arithmetic, exercised with the guard's legacy no-op default).
    # Cases below supply it to exercise the guard itself.
    {"name": "override stop at or above the current price is refused",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"stop": 160.0, "reason": "stale from a previous holding"},
     "prices": {"AAA": 150.0},
     "stop_after": 100.0, "targets_after": [130.0],
     "stop_enforced": False, "target_enforced": False},

    {"name": "override stop below the current price still applies",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"stop": 120.0, "reason": "genuine tightening"},
     "prices": {"AAA": 150.0},
     "stop_after": 120.0, "targets_after": [130.0],
     "stop_enforced": True, "target_enforced": False},

    {"name": "unknown price refuses the stop override (fail closed)",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"stop": 120.0, "reason": "tightening with no quote available"},
     "prices": {},
     "stop_after": 100.0, "targets_after": [130.0],
     "stop_enforced": False, "target_enforced": False},

    {"name": "unknown price does NOT block a target change",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"targets": [160.0], "reason": "target with no quote available"},
     "prices": {},
     "stop_after": 100.0, "targets_after": [160.0],
     "stop_enforced": False, "target_enforced": True},

    # ⛔ NaN IS NOT A USABLE PRICE (found in final review, 2026-08-17). A bare
    # `px <= 0` / `stop >= px` comparison is FALSE for a NaN price in both
    # directions -- Python defines every comparison against NaN as False --
    # so the old guard (`not isinstance(px, (int, float)) or px <= 0 or
    # stop >= px`) let a NaN price sail through untouched and the override
    # was APPLIED. src/marks.py already treats a NaN/inf mark as "a corrupt
    # monitor quote" and refuses to use it (FIX B, 2026-08-10) -- this is the
    # same failure mode reaching the stop guard instead of the valuation
    # path. Demonstrated by the reviewer: price NaN -> stop 120.0 applied
    # over a thesis stop of 100.0.
    {"name": "non-finite price refuses the stop override (fail closed)",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"stop": 120.0, "reason": "tightening against a corrupt NaN quote"},
     "prices": {"AAA": float("nan")},
     "stop_after": 100.0, "targets_after": [130.0],
     "stop_enforced": False, "target_enforced": False},
]


def _selftest() -> None:
    assert CASES, "the table must not be empty"
    # `prices` is deliberately NOT in this required set -- it is optional.
    # A case that omits it is not testing the apply-time price guard at all
    # (both interpreters treat that as "prices not supplied": the guard's
    # own no-op default, i.e. the pre-guard arithmetic). Only cases that
    # want to exercise the guard carry a `prices` dict.
    keys = {"name", "thesis_stop", "thesis_targets", "ov", "stop_after",
            "targets_after", "stop_enforced", "target_enforced"}
    for c in CASES:
        assert keys <= set(c), (c.get("name"), keys - set(c))
        if "prices" in c:
            assert isinstance(c["prices"], dict), (c["name"], "prices must be a dict")
    names = [c["name"] for c in CASES]
    assert len(names) == len(set(names)), "case names must be unique"
    print("selftest OK: level_rules -- shared enforcement table is well-formed")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
