"""Where the agent records the decisions it makes: the levels it sets, and why.

`reason` is mandatory on every write. A level with no stated reason cannot be
reviewed later, and a later session cannot tell whether the thesis behind it
still holds.
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))


def merge_levels(existing: dict, symbol: str, stop, target, reason: str, ts: str) -> dict:
    """Return a NEW overrides dict with `symbol` set. Never mutates `existing`.

    Raises ValueError on: a blank reason, a non-finite or non-positive level, or
    a target at or below the stop (which would be immediately self-triggering).
    """
    if not reason or not str(reason).strip():
        raise ValueError("reason is required: a level nobody can review is a level "
                         "a later session cannot judge")
    try:
        s = float(stop)
    except (TypeError, ValueError):
        raise ValueError(f"stop {stop!r} is not a number")
    if not math.isfinite(s) or s <= 0:
        raise ValueError(f"stop {stop!r} must be a finite positive price")
    t = None
    if target is not None:
        try:
            t = float(target)
        except (TypeError, ValueError):
            raise ValueError(f"target {target!r} is not a number")
        if not math.isfinite(t) or t <= 0:
            raise ValueError(f"target {target!r} must be a finite positive price")
        if t <= s:
            raise ValueError(f"target {t} is at or below stop {s}; it would trigger "
                             "immediately")
    out = {k: dict(v) for k, v in (existing or {}).items()}
    out[str(symbol).strip().upper()] = {
        "stop": s,
        "target": t,                          # kept for future use; the monitor
                                               # does NOT read this singular key
        "targets": [t] if t is not None else [],  # the shape apply_overrides()
                                                   # in scripts/market_monitor.py
                                                   # actually reads
        "reason": str(reason).strip(), "ts": ts,
    }
    return out


def evaluate_enforcement(stop: float, target, has_thesis: bool,
                         current_stop, current_targets) -> dict:
    """Report what scripts/market_monitor.py's apply_overrides() will ACTUALLY do
    with this stop/target at the next poll, given the symbol's thesis (or lack of
    one). This does not enforce anything itself -- it mirrors that function's
    logic exactly so the report can never drift from the real behaviour:

      - stop: applied only if the symbol has a thesis, that thesis has a stop
        set, and the new stop RAISES it. A looser stop is silently ignored.
      - target: applied only if the symbol has a thesis, `target` is not None,
        and the resulting one-element list matches the LENGTH of the thesis's
        existing targets list (only ever true for a thesis with exactly one
        target) AND lowers it. Any count mismatch is silently ignored.
      - no thesis at all -> the monitor is not watching this symbol -> NEITHER
        level is enforced, regardless of what was just written.

    `current_stop`/`current_targets` are the thesis's own stop/targets (None /
    [] when `has_thesis` is False).
    """
    if not has_thesis:
        note = "no thesis for this symbol -- the monitor is not watching it at all"
        return {
            "stop": {"enforced": False, "note": note},
            "target": {"enforced": False, "note": note} if target is not None else
                      {"enforced": False, "note": "no target was set"},
        }

    if current_stop is not None and isinstance(stop, (int, float)) and stop > current_stop:
        stop_result = {"enforced": True,
                       "note": f"raises the thesis's current stop ({current_stop}) -- "
                               "will be applied at the next monitor poll"}
    elif current_stop is None:
        stop_result = {"enforced": False,
                       "note": "thesis has no stop set to compare against -- "
                               "the monitor cannot verify a tightening, so this is ignored"}
    else:
        stop_result = {"enforced": False,
                       "note": f"not stricter than the thesis's current stop ({current_stop}) "
                               "-- the monitor only tightens stops, so this is ignored"}

    if target is None:
        target_result = {"enforced": False, "note": "no target was set"}
    else:
        cur = list(current_targets or [])
        if len(cur) != 1:
            target_result = {"enforced": False,
                             "note": f"thesis has {len(cur)} target(s); a single target here "
                                     "only matches a thesis with exactly 1 target -- ignored"}
        elif target < cur[0]:
            target_result = {"enforced": True,
                             "note": f"lowers the thesis's current target ({cur[0]}) -- "
                                     "will be applied at the next monitor poll"}
        else:
            target_result = {"enforced": False,
                             "note": f"not lower than the thesis's current target ({cur[0]}) "
                                     "-- the monitor only lowers targets, so this is ignored"}

    return {"stop": stop_result, "target": target_result}


OVERRIDES = REPO / "research_store" / "monitor" / "overrides.json"


def write_levels(symbol: str, stop, target, reason: str, ts: str,
                 path: Path | None = None) -> dict:
    """Merge one symbol's levels into overrides.json ATOMICALLY.

    os.replace mirrors scripts/risk_review.py: the monitor reads this file every
    poll and a torn read makes it drop ALL overrides for that tick.

    `path` defaults to the live module-level OVERRIDES, looked up at CALL time
    (not bound as a default argument) so tests can redirect writes by patching
    `decide.OVERRIDES` -- including calls made indirectly through server.py's
    set_levels(), which never passes `path` itself.
    """
    path = path or OVERRIDES
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except Exception:
            existing = {}
    merged = merge_levels(existing, symbol, stop, target, reason, ts)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, indent=2))
    os.replace(tmp, path)
    return merged


def _selftest() -> None:
    base = {"AAA": {"stop": 10.0, "target": 20.0, "reason": "old", "ts": "t0"}}
    out = merge_levels(base, "BBB", 5.0, 9.0, "broke out on volume", "t1")
    assert out["BBB"]["stop"] == 5.0 and out["BBB"]["target"] == 9.0
    assert out["BBB"]["reason"] == "broke out on volume" and out["BBB"]["ts"] == "t1"
    assert base == {"AAA": {"stop": 10.0, "target": 20.0, "reason": "old", "ts": "t0"}}, "mutated input"
    assert out["AAA"] == base["AAA"], "clobbered an unrelated symbol"
    # overwriting the same symbol replaces it
    out2 = merge_levels(out, "AAA", 11.0, 21.0, "tightened", "t2")
    assert out2["AAA"]["stop"] == 11.0 and out2["AAA"]["reason"] == "tightened"
    # a stop with no target is allowed (target None)
    assert merge_levels({}, "CCC", 5.0, None, "stop only", "t")["CCC"]["target"] is None

    for bad, why in [
        (("DDD", 5.0, 9.0, "", "t"), "blank reason"),
        (("DDD", 5.0, 9.0, "   ", "t"), "whitespace reason"),
        (("DDD", float("nan"), 9.0, "r", "t"), "non-finite stop"),
        (("DDD", 0.0, 9.0, "r", "t"), "non-positive stop"),
        (("DDD", -1.0, 9.0, "r", "t"), "negative stop"),
        (("DDD", 5.0, 5.0, "r", "t"), "target equal to stop"),
        (("DDD", 5.0, 4.0, "r", "t"), "target below stop"),
        (("DDD", 5.0, float("inf"), "r", "t"), "non-finite target"),
    ]:
        try:
            merge_levels({}, *bad)
            raise AssertionError(f"should have rejected: {why}")
        except ValueError:
            pass
    print("selftest OK: merge_levels is pure, reason mandatory, levels sane")

    # merge_levels must write the LIST shape apply_overrides() in
    # scripts/market_monitor.py actually reads, not just the dead singular
    # "target" key -- this is the finding this change fixes.
    out3 = merge_levels({}, "EEE", 5.0, 9.0, "r", "t")
    assert out3["EEE"]["targets"] == [9.0], out3["EEE"]
    out4 = merge_levels({}, "FFF", 5.0, None, "stop only", "t")
    assert out4["FFF"]["targets"] == [], out4["FFF"]
    print("selftest OK: merge_levels writes targets as a list (monitor-consumable shape)")

    # evaluate_enforcement: report what apply_overrides() will ACTUALLY do,
    # mirroring its stricter-only-stop / count-matched-lower-only-target logic.
    # 1) no thesis at all -> neither level enforced, and it says why.
    r = evaluate_enforcement(stop=108.0, target=118.0, has_thesis=False,
                             current_stop=None, current_targets=None)
    assert r["stop"]["enforced"] is False and r["target"]["enforced"] is False, r
    assert "no thesis" in r["stop"]["note"] and "no thesis" in r["target"]["note"], r

    # 2) stop LOOSER than the thesis's current stop -> not enforced, reported.
    r = evaluate_enforcement(stop=90.0, target=None, has_thesis=True,
                             current_stop=100.0, current_targets=[120.0])
    assert r["stop"]["enforced"] is False, r
    assert "100.0" in r["stop"]["note"], r

    # 3) stop STRICTER (higher) than the thesis's current stop -> enforced.
    r = evaluate_enforcement(stop=108.0, target=None, has_thesis=True,
                             current_stop=100.0, current_targets=[120.0])
    assert r["stop"]["enforced"] is True, r

    # 4) target count MISMATCHES the thesis's existing targets -> not enforced.
    r = evaluate_enforcement(stop=None, target=110.0, has_thesis=True,
                             current_stop=100.0, current_targets=[120.0, 140.0])
    assert r["target"]["enforced"] is False, r
    assert "2 target" in r["target"]["note"], r

    # 5) matching count AND lowers -> enforced.
    r = evaluate_enforcement(stop=None, target=110.0, has_thesis=True,
                             current_stop=100.0, current_targets=[120.0])
    assert r["target"]["enforced"] is True, r

    # matching count but does NOT lower -> not enforced.
    r = evaluate_enforcement(stop=None, target=130.0, has_thesis=True,
                             current_stop=100.0, current_targets=[120.0])
    assert r["target"]["enforced"] is False, r
    print("selftest OK: evaluate_enforcement mirrors apply_overrides() exactly "
          "(no-thesis, looser-stop, stricter-stop, target-count-mismatch, "
          "target-lowers)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
