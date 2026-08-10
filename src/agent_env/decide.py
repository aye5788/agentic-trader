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
    out[str(symbol).strip().upper()] = {"stop": s, "target": t,
                                        "reason": str(reason).strip(), "ts": ts}
    return out


OVERRIDES = REPO / "research_store" / "monitor" / "overrides.json"


def write_levels(symbol: str, stop, target, reason: str, ts: str,
                 path: Path = OVERRIDES) -> dict:
    """Merge one symbol's levels into overrides.json ATOMICALLY.

    os.replace mirrors scripts/risk_review.py: the monitor reads this file every
    poll and a torn read makes it drop ALL overrides for that tick.
    """
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


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
