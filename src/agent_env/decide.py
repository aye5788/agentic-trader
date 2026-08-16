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

# How long an agent-set level keeps arming the live monitor. A level is a
# judgment about a situation, and situations end -- see _expiry.
LEVEL_TTL_DAYS = 5
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
        # ⚠️ EXPIRY IS MANDATORY. risk_review.read_overrides prunes on
        # `o.get("expires", "9999") >= today`, so an entry written WITHOUT this
        # key never expires -- a stop the agent set once kept arming the live
        # monitor indefinitely, outliving the position, the thesis and any
        # reason it was set for. A level is a judgment about a situation, and
        # situations end; the agent re-states it if it still holds.
        "expires": _expiry(ts),
    }
    return out


def _expiry(ts: str, days: int = LEVEL_TTL_DAYS) -> str:
    """The date this level stops arming the monitor (YYYY-MM-DD)."""
    from datetime import date, datetime, timedelta      # noqa: PLC0415
    try:
        base = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()
    except Exception:                                   # noqa: BLE001
        base = date.today()
    return (base + timedelta(days=days)).isoformat()


def evaluate_enforcement(stop: float, target, has_thesis: bool, target_weight,
                         owned, current_stop, current_targets,
                         verdict: str = "") -> dict:
    """Report what scripts/market_monitor.py's apply_overrides() will ACTUALLY do
    with this stop/target at the next poll. This does not enforce anything
    itself -- it mirrors the stop/target arithmetic of apply_overrides() AND
    the enclosing `held`-set construction that gates whether apply_overrides()
    ever looks at this symbol at all, so the report can never drift from the
    real behaviour:

        held = {t.symbol: t for t in prod.theses if in_book(t) and t.stop}
        # ...where in_book(t) is `target_weight > 0 or verdict == "hold"`
        owned = owned_symbols(_load(RH_POSITIONS, None))
        if owned is not None:
            held = {s: t for s, t in held.items() if s in owned}
        held = apply_overrides(held, overrides)

    So a level can be enforced only if ALL of these hold:
      - the symbol has a thesis, that thesis's `target_weight` is positive, AND
        it carries a (truthy) stop -- the monitor's BOOK filter. Any one of
        these missing means the symbol is never placed in `held` at all, so
        overrides for it are never evaluated, regardless of what was just
        written.
      - the position is currently OWNED per the broker snapshot
        (research_store/rh/positions.json, the same file and interpretation
        `owned_symbols()` uses) -- the monitor's OWNERSHIP filter. If ownership
        is confirmed False, the symbol is dropped from `held` even though the
        book filter passed, and neither level is enforced. If ownership could
        not be determined at all (torn/absent snapshot), the monitor FAILS
        OPEN and does not apply this filter -- watching proceeds as if owned,
        and the note says so explicitly rather than silently claiming a clean
        `enforced: true`.
      - stop: the new stop RAISES the thesis's current stop. A looser stop is
        silently ignored.
      - target: `target` is not None and the resulting one-element list
        matches the LENGTH of the thesis's existing targets list (only ever
        true for a thesis with exactly one target) AND lowers it. Any count
        mismatch is silently ignored.

    `current_stop`/`current_targets` are the thesis's own stop/targets (None /
    [] when `has_thesis` is False). `owned` is a tri-state: True = confirmed
    held, False = confirmed not held, None = ownership could not be determined
    (mirrors `owned_symbols()` returning None on a torn/absent snapshot).
    """
    if not has_thesis:
        note = "no thesis for this symbol -- the monitor is not watching it at all"
        return {
            "stop": {"enforced": False, "note": note},
            "target": {"enforced": False, "note": note} if target is not None else
                      {"enforced": False, "note": "no target was set"},
        }

    # ⛔ MIRRORS market_monitor.in_book() — `target_weight > 0 OR verdict ==
    # "hold"`. It gated on the weight ALONE until 2026-08-16, which was correct
    # only until protective theses existed. Those carry weight 0.0 and verdict
    # "hold" (slow_loop.protective_theses) for names the AGENT holds that the
    # ranking did not select, and the monitor DOES watch them. So this reported
    # `enforced: false` for four live, genuinely-protected positions.
    # prompts/charter.md tells the agent that `enforcement.stop.enforced: true`
    # is the ONLY evidence the monitor will act — so a false negative here does
    # not merely under-report, it invites the agent to exit or re-set a position
    # that was already protected. Found by the independent audit, 2026-08-16.
    watched = (target_weight is not None and target_weight > 0) or verdict == "hold"
    if not watched:
        note = ("this thesis is neither weighted nor a held-protective one "
                "(verdict='hold'), so the monitor's book filter excludes the "
                "symbol from the watch-list entirely and overrides for it are "
                "never evaluated")
        return {
            "stop": {"enforced": False, "note": note},
            "target": {"enforced": False, "note": note} if target is not None else
                      {"enforced": False, "note": "no target was set"},
        }

    if not current_stop:
        note = ("thesis has no stop set -- the monitor's book filter "
                "(target_weight > 0 and stop) excludes this symbol from the watch-list "
                "entirely, so overrides for it are never evaluated")
        return {
            "stop": {"enforced": False, "note": note},
            "target": {"enforced": False, "note": note} if target is not None else
                      {"enforced": False, "note": "no target was set"},
        }

    if owned is False:
        note = ("position is not currently held per the broker snapshot -- the "
                "monitor's ownership filter drops this symbol from the watch-list even "
                "though it has a valid thesis, so overrides for it are never evaluated")
        return {
            "stop": {"enforced": False, "note": note},
            "target": {"enforced": False, "note": note} if target is not None else
                      {"enforced": False, "note": "no target was set"},
        }

    unverified = ("" if owned is True else
                 " -- ownership could not be verified this call (broker snapshot "
                 "unreadable); the monitor FAILS OPEN in that state and watches this "
                 "thesis anyway, but confirm the position is really held")

    if isinstance(stop, (int, float)) and stop > current_stop:
        stop_result = {"enforced": True,
                       "note": f"raises the thesis's current stop ({current_stop}) -- "
                               "will be applied at the next monitor poll" + unverified}
    else:
        stop_result = {"enforced": False,
                       "note": f"not stricter than the thesis's current stop ({current_stop}) "
                               "-- the monitor only tightens stops, so this is ignored" + unverified}

    if target is None:
        target_result = {"enforced": False, "note": "no target was set"}
    else:
        cur = list(current_targets or [])
        if len(cur) != 1:
            target_result = {"enforced": False,
                             "note": f"thesis has {len(cur)} target(s); a single target here "
                                     "only matches a thesis with exactly 1 target -- ignored" + unverified}
        elif target < cur[0]:
            target_result = {"enforced": True,
                             "note": f"lowers the thesis's current target ({cur[0]}) -- "
                                     "will be applied at the next monitor poll" + unverified}
        else:
            target_result = {"enforced": False,
                             "note": f"not lower than the thesis's current target ({cur[0]}) "
                                     "-- the monitor only lowers targets, so this is ignored" + unverified}

    return {"stop": stop_result, "target": target_result}


RH_POSITIONS = REPO / "research_store" / "rh" / "positions.json"


def owned_symbols(snap) -> set | None:
    """Symbols actually held (qty>0) in an RH positions snapshot dict.

    Exact functional duplicate of scripts/market_monitor.py's owned_symbols() --
    NOT imported, because that module imports the moomoo SDK at load time and
    only runs under system /usr/bin/python3 (3.10); this process runs under
    .venv (3.12) and cannot import it (see CLAUDE.md, "Two Python runtimes").
    Reads the SAME file (research_store/rh/positions.json) with the SAME
    interpretation (qty>0) so the tool's claim can never drift from what the
    monitor actually does -- if that function's logic ever changes, this one
    must change with it.

    Returns None when the snapshot can't be interpreted (absent / torn / wrong
    shape) -- the caller then treats ownership as indeterminate, mirroring the
    monitor's fail-open state.
    """
    if not isinstance(snap, dict) or not isinstance(snap.get("positions"), dict):
        return None
    out = set()
    for sym, p in snap["positions"].items():
        qty = p.get("qty") if isinstance(p, dict) else p   # {qty,...} or legacy dollars
        try:
            if float(qty or 0) > 0:
                out.add(sym)
        except (TypeError, ValueError):
            continue
    return out


def load_owned(path: Path | None = None) -> set | None:
    """Load + interpret the broker-ownership snapshot the monitor itself reads.

    `path` defaults to the live module-level RH_POSITIONS, looked up at CALL
    time (not bound as a default argument) so tests can redirect reads by
    patching `decide.RH_POSITIONS` -- same pattern as `write_levels`'s `path`.
    A missing or unparseable file degrades to None (indeterminate), never an
    exception -- this is a read path the tool must never crash on.
    """
    path = path or RH_POSITIONS
    try:
        snap = json.loads(path.read_text()) if path.exists() else None
    except Exception:
        snap = None
    return owned_symbols(snap)


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


def decision_entry(symbol: str, action: str, reason: str, ts: str) -> dict:
    """Build the journal event for one agent decision. Pure.

    Every field is required. An action with no reason is exactly the thing that
    makes a later review impossible.
    """
    sym = str(symbol or "").strip().upper()
    act = str(action or "").strip().lower()
    why = str(reason or "").strip()
    if not sym:
        raise ValueError("symbol is required")
    if not act:
        raise ValueError("action is required")
    if not why:
        raise ValueError("reason is required: an action with no stated why cannot "
                         "be reviewed")
    return {"event": "agent_decision", "ts": ts, "symbol": sym,
            "action": act, "reason": why}


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
    # mirroring its stricter-only-stop / count-matched-lower-only-target logic
    # AND the enclosing held-set construction (book filter + ownership filter).
    # Every scenario below that should reach the stop/target arithmetic is
    # given a positive target_weight and owned=True so ownership is not what
    # gates the result -- isolating exactly the property each test names.
    # 1) no thesis at all -> neither level enforced, and it says why.
    r = evaluate_enforcement(stop=108.0, target=118.0, has_thesis=False,
                             target_weight=None, owned=None,
                             current_stop=None, current_targets=None)
    assert r["stop"]["enforced"] is False and r["target"]["enforced"] is False, r
    assert "no thesis" in r["stop"]["note"] and "no thesis" in r["target"]["note"], r

    # 2) stop LOOSER than the thesis's current stop -> not enforced, reported.
    # ⛔ REGRESSION GUARD (2026-08-16). A protective thesis — weight 0.0,
    # verdict "hold", written by slow_loop for a name the AGENT holds that the
    # ranking did not select — IS watched by market_monitor.in_book(). This
    # reported enforced=False for four live positions until the verdict clause
    # was added, and the charter tells the agent that flag is the only proof a
    # stop is real.
    r = evaluate_enforcement(stop=108.0, target=118.0, has_thesis=True,
                             target_weight=0.0, verdict="hold", owned=True,
                             current_stop=100.0, current_targets=[120.0, 140.0])
    assert r["stop"]["enforced"] is True, r
    # ...and an R:R-dropped thesis (weight 0, verdict "avoid") is NOT watched
    r = evaluate_enforcement(stop=108.0, target=118.0, has_thesis=True,
                             target_weight=0.0, verdict="avoid", owned=True,
                             current_stop=100.0, current_targets=[120.0, 140.0])
    assert r["stop"]["enforced"] is False, r

    r = evaluate_enforcement(stop=90.0, target=None, has_thesis=True,
                             target_weight=0.1, owned=True,
                             current_stop=100.0, current_targets=[120.0])
    assert r["stop"]["enforced"] is False, r
    assert "100.0" in r["stop"]["note"], r

    # 3) stop STRICTER (higher) than the thesis's current stop -> enforced.
    r = evaluate_enforcement(stop=108.0, target=None, has_thesis=True,
                             target_weight=0.1, owned=True,
                             current_stop=100.0, current_targets=[120.0])
    assert r["stop"]["enforced"] is True, r

    # 4) target count MISMATCHES the thesis's existing targets -> not enforced.
    r = evaluate_enforcement(stop=None, target=110.0, has_thesis=True,
                             target_weight=0.1, owned=True,
                             current_stop=100.0, current_targets=[120.0, 140.0])
    assert r["target"]["enforced"] is False, r
    assert "2 target" in r["target"]["note"], r

    # 5) matching count AND lowers -> enforced.
    r = evaluate_enforcement(stop=None, target=110.0, has_thesis=True,
                             target_weight=0.1, owned=True,
                             current_stop=100.0, current_targets=[120.0])
    assert r["target"]["enforced"] is True, r

    # matching count but does NOT lower -> not enforced.
    r = evaluate_enforcement(stop=None, target=130.0, has_thesis=True,
                             target_weight=0.1, owned=True,
                             current_stop=100.0, current_targets=[120.0])
    assert r["target"]["enforced"] is False, r
    print("selftest OK: evaluate_enforcement mirrors apply_overrides() exactly "
          "(no-thesis, looser-stop, stricter-stop, target-count-mismatch, "
          "target-lowers)")

    # --- coverage for the finding this fix closes: the held-set construction,
    # not just the arithmetic, gates enforcement ---

    # 6) thesis passes the book filter (weight>0, has a stop) but the broker
    #    snapshot confirms the symbol is NOT held -> neither level enforced,
    #    and the note is distinct from "no thesis" (different operator action).
    r = evaluate_enforcement(stop=108.0, target=90.0, has_thesis=True,
                             target_weight=0.1, owned=False,
                             current_stop=100.0, current_targets=[120.0])
    assert r["stop"]["enforced"] is False and r["target"]["enforced"] is False, r
    assert "not currently held" in r["stop"]["note"], r
    assert "no thesis" not in r["stop"]["note"], r

    # 7) held (owned=True) with a thesis but target_weight == 0 -> the book
    #    filter (`target_weight > 0 and stop`) excludes it -> not enforced.
    r = evaluate_enforcement(stop=108.0, target=90.0, has_thesis=True,
                             target_weight=0.0, owned=True,
                             current_stop=100.0, current_targets=[120.0])
    assert r["stop"]["enforced"] is False and r["target"]["enforced"] is False, r
    assert "target_weight" in r["stop"]["note"], r

    # 8) held (owned=True) with a thesis but NO stop -> the book filter
    #    excludes it too -> not enforced, distinct note.
    r = evaluate_enforcement(stop=108.0, target=90.0, has_thesis=True,
                             target_weight=0.1, owned=True,
                             current_stop=None, current_targets=[120.0])
    assert r["stop"]["enforced"] is False and r["target"]["enforced"] is False, r
    assert "no stop" in r["stop"]["note"], r

    # 9) ownership INDETERMINATE (torn/absent broker snapshot) -> the monitor
    #    fails open and still watches a book-filtered thesis, so enforcement
    #    follows the normal arithmetic -- but the note must say plainly that
    #    ownership was never confirmed, not silently claim a clean result.
    r = evaluate_enforcement(stop=108.0, target=None, has_thesis=True,
                             target_weight=0.1, owned=None,
                             current_stop=100.0, current_targets=[120.0])
    assert r["stop"]["enforced"] is True, r
    assert "could not be verified" in r["stop"]["note"], r
    print("selftest OK: evaluate_enforcement also mirrors the held-set "
          "construction (book filter: target_weight>0 and stop; ownership "
          "filter: not-held vs indeterminate-fails-open) -- not just the "
          "stop/target arithmetic")

    # decision_entry: every decision must carry a reason (pure builder for
    # journal events)
    e = decision_entry("aaa", "OPEN", "strongest score, terrain supports a 3s target", "t1")
    assert e["event"] == "agent_decision" and e["symbol"] == "AAA", e
    assert e["action"] == "open" and e["ts"] == "t1", e
    assert e["reason"].startswith("strongest score"), e
    for bad in [("AAA", "open", ""), ("AAA", "", "r"), ("", "open", "r")]:
        try:
            decision_entry(*bad, "t")
            raise AssertionError(f"should have rejected {bad}")
        except ValueError:
            pass
    print("selftest OK: merge_levels is pure, reason mandatory, levels sane")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
