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


def merge_levels(existing: dict, symbol: str, stop, targets, reason: str, ts: str) -> dict:
    """Return a NEW overrides dict with `symbol` set. Never mutates `existing`.

    `targets` accepts None, a single number, or a list of numbers -- the list
    form is the one that matters: every live thesis carries two, and the
    single form was refused by the monitor every time (apply_overrides only
    applies a target list when its length matches the thesis's existing one).

    Raises ValueError on: a blank reason, a non-finite or non-positive level, or
    any target at or below the stop (which would be immediately self-triggering).
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
    # `targets` accepts None, a single number, or a list matching the thesis's
    # target count. The list form is the one that matters: every live thesis
    # carries two, and the single form was refused by the monitor every time.
    if targets is None:
        ts_list = []
    elif isinstance(targets, (list, tuple)):
        ts_list = list(targets)
    else:
        ts_list = [targets]
    parsed = []
    for raw in ts_list:
        try:
            t = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"target {raw!r} is not a number")
        if not math.isfinite(t) or t <= 0:
            raise ValueError(f"target {raw!r} must be a finite positive price")
        if t <= s:
            raise ValueError(f"target {t} is at or below stop {s}; it would trigger "
                             "immediately")
        parsed.append(t)
    out = {k: dict(v) for k, v in (existing or {}).items()}
    out[str(symbol).strip().upper()] = {
        "stop": s,
        "target": parsed[0] if parsed else None,   # legacy singular; the monitor
                                                    # does NOT read this key
        "targets": parsed,                          # the shape apply_overrides reads
        "reason": str(reason).strip(), "ts": ts,
        # NO EXPIRY. A level is the agent's judgement and lasts until the agent
        # changes or clears it. An earlier design expired levels on a timer,
        # which meant protection could vanish on a schedule nobody chose; the
        # pruner that enforced it died with risk_review (2026-08-13) and went
        # unnoticed. The hazard this creates -- a level outliving its position --
        # is closed by clear_levels(), by the charter making a clear part of an
        # exit, and by apply_overrides refusing a stop at or above the current
        # price. Decision: principal, 2026-08-17.
    }
    return out


_PRICE_UNSET = object()   # sentinel, distinct from None -- see `price` below.


def evaluate_enforcement(stop: float, target, has_thesis: bool, target_weight,
                         owned, current_stop, current_targets,
                         verdict: str = "", price=_PRICE_UNSET) -> dict:
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
      - stop: the new stop RAISES the thesis's current stop, AND is BELOW a
        known live price. `price` is scripts/market_monitor.py:apply_overrides()'s
        own price guard (Part 3, 2026-08-17) mirrored here: a stop at or above
        the current price is not a stop -- it is breached the instant it is
        applied, and the monitor sells the whole position at market within one
        poll. `price=None` (unknown) FAILS CLOSED exactly like the monitor
        does -- this reports `enforced: false`, never `true`, because a stop
        the monitor will refuse must never be reported as applied. A looser
        stop, or a stop with no known price, is reported not-enforced.
      - target: `target` is not None, every supplied value is a number, and the
        resulting list matches the LENGTH of the thesis's existing targets list
        -- apply_overrides moves targets in EITHER direction (raising a target
        adds no risk of loss, since the stop is unchanged), so a raise is
        applied exactly like a lower. Any count mismatch, or a list identical
        to the current targets, is ignored.

    `current_stop`/`current_targets` are the thesis's own stop/targets (None /
    [] when `has_thesis` is False). `owned` is a tri-state: True = confirmed
    held, False = confirmed not held, None = ownership could not be determined
    (mirrors `owned_symbols()` returning None on a torn/absent snapshot).

    `price` is meant to be the last known live price for this symbol -- read
    the SAME research_store/monitor/quotes.json the monitor itself writes
    (see `load_price()` below). It is a THREE-state argument, deliberately
    distinguishing "not asked" from "asked and came up empty":
      - omitted entirely (the `_PRICE_UNSET` sentinel default) -- the caller
        does not participate in the apply-time price guard at all. server.py's
        set_levels() no longer does this: as of commit bffac51 it always
        passes a price explicitly, and since the fix that wired in
        `load_price()` (final review, I2) that price IS read from this same
        quotes.json rather than marks.load()'s mark-or-avg_cost fallback --
        so this branch is reachable only by a caller that predates the guard
        entirely, not by the live production call site. Falls back to the
        pre-guard behaviour: a stricter stop is reported enforced.
      - explicitly `None` -- the caller DID check and no live price is known
        for this symbol right now. Reported `enforced: false`, fail-closed,
        exactly like apply_overrides() would refuse it.
      - a positive number -- the last known live price. Reported enforced iff
        the new stop sits below it.
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
        note = ("target_weight is not positive and this is not a held-protective "
                "thesis (verdict='hold'), so the monitor's book filter "
                "(target_weight > 0 or verdict == 'hold') excludes this symbol "
                "from the watch-list entirely and overrides for it are never "
                "evaluated")
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
        # ⛔ MIRRORS apply_overrides()'s apply-time price guard (Part 3,
        # 2026-08-17). A stop at or above the current price is not a stop --
        # it is breached the instant it is applied, and the monitor sells the
        # whole position at market within one poll. With no known price, the
        # note says so plainly rather than asserting a price relationship it
        # cannot verify -- but `enforced` is still `false`, because that much
        # IS known: the monitor requires a known price to apply ANY stop
        # override and fails closed without one, so this one will not be
        # applied at the next poll either way.
        if price is _PRICE_UNSET:
            stop_result = {"enforced": True,
                           "note": f"raises the thesis's current stop ({current_stop}) -- "
                                   "will be applied at the next monitor poll" + unverified}
        elif price is None:
            stop_result = {"enforced": False,
                           "note": f"raises the thesis's current stop ({current_stop}), but no "
                                   "live price is currently known for this symbol -- the monitor "
                                   "requires a known price to apply ANY stop override and fails "
                                   "closed without one, so this will NOT be applied" + unverified}
        elif (not isinstance(price, (int, float)) or not math.isfinite(price)
              or price <= 0 or float(stop) >= float(price)):
            # ⛔ NaN IS NOT A USABLE PRICE. `price <= 0` and `stop >= price`
            # are BOTH False for a NaN price -- every comparison against NaN
            # is False in Python -- so this must check isfinite explicitly or
            # a corrupt quote reads as "enforced: true" here while
            # apply_overrides() (mirrored above) refuses it. src/marks.py
            # already treats a NaN/inf mark as "a corrupt monitor quote" and
            # refuses to use it (FIX B, 2026-08-10); this is the same
            # reasoning applied to the price guard instead of valuation.
            stop_result = {"enforced": False,
                           "note": f"raises the thesis's current stop ({current_stop}), but is at "
                                   f"or above the last known price ({price}) -- the monitor refuses "
                                   "a stop at or above price (it would liquidate the position at "
                                   "the next poll), so this is refused" + unverified}
        else:
            stop_result = {"enforced": True,
                           "note": f"raises the thesis's current stop ({current_stop}) and is "
                                   f"below the last known price ({price}) -- "
                                   "will be applied at the next monitor poll" + unverified}
    else:
        stop_result = {"enforced": False,
                       "note": f"not stricter than the thesis's current stop ({current_stop}) "
                               "-- the monitor only tightens stops, so this is ignored" + unverified}

    if target is None:
        target_result = {"enforced": False, "note": "no target was set"}
    else:
        cur = list(current_targets or [])
        new = list(target) if isinstance(target, (list, tuple)) else [target]
        if not all(isinstance(o, (int, float)) for o in new):
            target_result = {"enforced": False,
                             "note": "targets must all be numbers -- ignored" + unverified}
        elif len(new) != len(cur):
            # THE refusal an agent on this book actually meets: every live
            # thesis carries two targets. Name the expected count so the agent
            # can retry correctly instead of inferring it.
            target_result = {"enforced": False,
                             "note": f"this thesis has {len(cur)} target(s) and you supplied "
                                     f"{len(new)}; the monitor applies a target list only when "
                                     f"the count matches -- supply all {len(cur)}" + unverified}
        elif [float(o) for o in new] == [float(o) for o in cur]:
            target_result = {"enforced": False,
                             "note": "identical to the thesis's current targets -- "
                                     "nothing to change" + unverified}
        else:
            # EITHER DIRECTION. apply_overrides permits raising as well as
            # lowering: raising adds no risk of loss, since the stop is
            # unchanged. Reporting otherwise is why no take-profit in this book
            # has ever been reached.
            target_result = {"enforced": True,
                             "note": f"replaces the thesis's targets ({cur}) -- "
                                     "will be applied at the next monitor poll" + unverified}

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
QUOTES = REPO / "research_store" / "monitor" / "quotes.json"


def load_price(symbol: str, path: Path | None = None) -> float | None:
    """Last known live price for `symbol`, from the monitor's own quotes.json.

    Reads the SAME file scripts/market_monitor.py writes every ~15s during RTH
    and the SAME file its apply_overrides() call site reads (research_store/
    monitor/quotes.json, shape `{"prices": {SYM: px}}`) -- so a report built
    from this can never claim a price the monitor itself doesn't have.

    `path` defaults to the live module-level QUOTES, looked up at CALL time
    (not bound as a default argument) so tests can redirect reads by patching
    `decide.QUOTES` -- same pattern as `load_owned()`. Degrades to None on any
    missing/torn/malformed file, or when the symbol has no entry -- "unknown"
    is the only safe reading of a price this function cannot vouch for, and
    evaluate_enforcement's price guard fails closed on exactly that.
    """
    path = path or QUOTES
    sym = str(symbol).strip().upper()
    try:
        data = json.loads(path.read_text()) if path.exists() else None
    except Exception:
        data = None
    prices = data.get("prices") if isinstance(data, dict) else None
    px = prices.get(sym) if isinstance(prices, dict) else None
    return float(px) if isinstance(px, (int, float)) and px > 0 else None


def clear_level(existing: dict, symbol: str) -> dict:
    """Return a NEW overrides dict with `symbol` removed. Never mutates.

    Clearing an absent symbol is a NO-OP, deliberately: the agent calling this
    after an exit should not have to know whether a level was ever set, and an
    error there would train it to skip the call.
    """
    sym = str(symbol).strip().upper()
    return {k: v for k, v in (existing or {}).items() if k != sym}


def clear_levels_file(symbol: str, path: Path | None = None) -> dict:
    """Remove one symbol's levels from overrides.json ATOMICALLY.

    Same os.replace discipline as write_levels: the monitor reads this file every
    poll and a torn read makes it drop ALL overrides for that tick.

    `path` defaults to the live module-level OVERRIDES, looked up at CALL time
    (not bound as a default argument) so tests can redirect writes by patching
    `decide.OVERRIDES` -- same pattern as write_levels.
    """
    path = path or OVERRIDES
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except Exception:                                   # noqa: BLE001
            existing = {}
    remaining = clear_level(existing, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(remaining, indent=2))
    os.replace(tmp, path)
    return remaining


def write_levels(symbol: str, stop, targets, reason: str, ts: str,
                 path: Path | None = None) -> dict:
    """Merge one symbol's levels into overrides.json ATOMICALLY.

    `targets` accepts None, a single number, or a list of numbers -- see
    merge_levels().

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
    merged = merge_levels(existing, symbol, stop, targets, reason, ts)
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

    # A two-target thesis must be expressible. This is the blocker: every live
    # thesis has two targets, and set_levels only ever accepted one, so a
    # target change was refused 100% of the time.
    out = merge_levels({}, "AAA", 100.0, [140.0, 170.0], "both", "2026-08-17T00:00:00+00:00")
    assert out["AAA"]["targets"] == [140.0, 170.0], out
    # a bare number still works for a one-target thesis
    out = merge_levels({}, "AAA", 100.0, 140.0, "one", "2026-08-17T00:00:00+00:00")
    assert out["AAA"]["targets"] == [140.0], out
    # no target at all
    out = merge_levels({}, "AAA", 100.0, None, "stop only", "2026-08-17T00:00:00+00:00")
    assert out["AAA"]["targets"] == [], out
    # EVERY target must clear the stop, not just the first
    try:
        merge_levels({}, "AAA", 100.0, [140.0, 90.0], "bad", "2026-08-17T00:00:00+00:00")
        raise AssertionError("a target below the stop must be refused")
    except ValueError as e:
        assert "at or below stop" in str(e), e
    print("selftest OK: merge_levels expresses a two-target thesis (the levels-mechanism blocker)")

    # A level must be removable: with no expiry, an override outlives its
    # position and wakes up on re-entry at a price it was never written for.
    ov = {"AAA": {"stop": 1.0, "reason": "x"}, "BBB": {"stop": 2.0, "reason": "y"}}
    out = clear_level(ov, "AAA")
    assert set(out) == {"BBB"}, out
    assert ov == {"AAA": {"stop": 1.0, "reason": "x"}, "BBB": {"stop": 2.0, "reason": "y"}}, \
        "clear_level must not mutate its input"
    assert clear_level(ov, "ZZZ") == ov, "clearing an absent symbol is a no-op, not an error"
    assert clear_level({}, "AAA") == {}, "clearing from an empty file is a no-op"
    assert clear_level(ov, "aaa") == clear_level(ov, "AAA"), "symbol match is case-insensitive"
    print("selftest OK: clear_level removes one symbol, pure, no-op on absent/empty")

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
    # price=150.0 is a permissive stand-in (above the stop being tested) so
    # this scenario isolates the property it names (the verdict clause) --
    # not the separate price guard, which has its own scenarios below.
    r = evaluate_enforcement(stop=108.0, target=118.0, has_thesis=True,
                             target_weight=0.0, verdict="hold", owned=True,
                             current_stop=100.0, current_targets=[120.0, 140.0],
                             price=150.0)
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

    # 3) stop STRICTER (higher) than the thesis's current stop -> enforced,
    #    given a known price below the new stop (see the price-guard block
    #    below for the property this scenario deliberately does not test).
    r = evaluate_enforcement(stop=108.0, target=None, has_thesis=True,
                             target_weight=0.1, owned=True,
                             current_stop=100.0, current_targets=[120.0],
                             price=150.0)
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

    # matching count and RAISES the target -> enforced. apply_overrides moves
    # targets in either direction; a stale one-way report is why no
    # take-profit in this book has ever been reached.
    r = evaluate_enforcement(stop=None, target=130.0, has_thesis=True,
                             target_weight=0.1, owned=True,
                             current_stop=100.0, current_targets=[120.0])
    assert r["target"]["enforced"] is True, r

    # identical to the current target -> not enforced, nothing to change.
    r = evaluate_enforcement(stop=None, target=120.0, has_thesis=True,
                             target_weight=0.1, owned=True,
                             current_stop=100.0, current_targets=[120.0])
    assert r["target"]["enforced"] is False, r
    assert "nothing to change" in r["target"]["note"], r
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
                             current_stop=100.0, current_targets=[120.0],
                             price=150.0)
    assert r["stop"]["enforced"] is True, r
    assert "could not be verified" in r["stop"]["note"], r
    print("selftest OK: evaluate_enforcement also mirrors the held-set "
          "construction (book filter: target_weight>0 and stop; ownership "
          "filter: not-held vs indeterminate-fails-open) -- not just the "
          "stop/target arithmetic")

    # ---- APPLY-TIME PRICE GUARD (Part 3, 2026-08-17) ----------------------
    # decide.py must never claim `enforced: true` for a stop
    # scripts/market_monitor.py:apply_overrides() will actually refuse -- a
    # stale override that outlives its position and wakes on re-entry can sit
    # ABOVE the new price, and apply_overrides() now refuses exactly that
    # (and refuses ANY stop override with no known price at all, fail closed).
    # This block is the required new decide selftest case for that guard.

    # a) known price BELOW the new stop -> enforced, note names the price.
    r = evaluate_enforcement(stop=120.0, target=None, has_thesis=True,
                             target_weight=0.1, owned=True,
                             current_stop=100.0, current_targets=[130.0],
                             price=150.0)
    assert r["stop"]["enforced"] is True, r
    assert "150.0" in r["stop"]["note"], r

    # b) known price AT OR ABOVE the new stop -> refused, note names the
    #    price it compared against -- this is the exact re-entry hazard
    #    (a stale stop from a previous, higher-priced holding).
    r = evaluate_enforcement(stop=160.0, target=None, has_thesis=True,
                             target_weight=0.1, owned=True,
                             current_stop=100.0, current_targets=[130.0],
                             price=150.0)
    assert r["stop"]["enforced"] is False, r
    assert "150.0" in r["stop"]["note"], r
    assert "at or above" in r["stop"]["note"], r

    # c) price UNKNOWN (no live quote for this symbol) -> refused, fail
    #    closed like apply_overrides() -- and the note says the price is
    #    unknown rather than asserting a comparison it cannot make.
    r = evaluate_enforcement(stop=120.0, target=None, has_thesis=True,
                             target_weight=0.1, owned=True,
                             current_stop=100.0, current_targets=[130.0],
                             price=None)
    assert r["stop"]["enforced"] is False, r
    assert "no live price is currently known" in r["stop"]["note"], r
    print("selftest OK: evaluate_enforcement's stop guard mirrors "
          "apply_overrides()'s apply-time price guard -- known price below "
          "the stop enforces, at/above or unknown refuses (fail closed)")

    # load_price(): reads the SAME quotes.json shape apply_overrides()'s call
    # site does, redirected to a scratch file so this NEVER touches the live
    # research_store/monitor/quotes.json.
    import tempfile as _tf                                      # noqa: PLC0415
    global QUOTES
    _orig_quotes = QUOTES
    with _tf.TemporaryDirectory() as _td:
        QUOTES = Path(_td) / "quotes.json"
        try:
            assert load_price("AAA") is None, "absent file -> unknown"
            QUOTES.write_text(json.dumps({"prices": {"AAA": 150.0, "BBB": 0.0}}))
            assert load_price("AAA") == 150.0
            assert load_price("aaa") == 150.0, "case-insensitive"
            assert load_price("BBB") is None, "a non-positive price is not usable"
            assert load_price("ZZZ") is None, "symbol with no entry -> unknown"
            QUOTES.write_text("{not json")
            assert load_price("AAA") is None, "torn read -> unknown, not a crash"
        finally:
            QUOTES = _orig_quotes
    print("selftest OK: load_price reads quotes.json (case-insensitive, "
          "non-positive/absent/torn -> None, never raises)")

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

    # ---- AGREEMENT WITH REAL ENFORCEMENT ----------------------------------
    # This is the regression that would have caught the target-direction lie.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import level_rules                                        # noqa: PLC0415
    for c in level_rules.CASES:
        ov = c["ov"]
        ov_targets = ov.get("targets")
        # Cases carrying a `prices` key are exercising apply_overrides()'s
        # price guard, and use "AAA" as the held symbol (same as this
        # module's and market_monitor's own truth-table harnesses) -- look
        # up that price directly. Cases WITHOUT `prices` are not testing the
        # guard at all (see level_rules.py's own doc comment on the key), so
        # a permissive stand-in keeps them isolating whatever property they
        # actually name, exactly as market_monitor's apply_overrides() also
        # takes prices=None (its own no-op default) for those same cases.
        # A FINITE stand-in, not float("inf") -- the guard now requires
        # math.isfinite(price) (this fix's own change), and inf is
        # definitionally not finite, so it would fail the very guard it is
        # meant to stand permissively clear of. 1e12 sits far above every
        # stop this table exercises while still passing isfinite().
        price = c["prices"].get("AAA") if "prices" in c else 1e12
        r = evaluate_enforcement(
            stop=ov.get("stop") if isinstance(ov.get("stop"), (int, float))
                 else c["thesis_stop"],
            target=ov_targets,
            has_thesis=True, target_weight=0.07, owned=True,
            current_stop=c["thesis_stop"],
            current_targets=list(c["thesis_targets"]),
            price=price)
        # `widen` is a real apply_overrides() branch (Task 1's table covers it)
        # but evaluate_enforcement has no parameter for it, and never needs one:
        # server.py's set_levels() -- the ONLY real caller -- never writes a
        # "widen" key (merge_levels/write_levels accept no such argument), so
        # an agent-written override can never take that branch. Skip the stop
        # assertion only for the cases that exercise it; every other case
        # (including both target-direction cases below) is still checked.
        if "widen" not in ov:
            assert r["stop"]["enforced"] == c["stop_enforced"], (c["name"], r["stop"])
        if ov_targets is not None:
            assert r["target"]["enforced"] == c["target_enforced"], \
                (c["name"], r["target"])
    print("selftest OK: evaluate_enforcement agrees with level_rules.CASES "
          "(the shared truth table pinned against apply_overrides())")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
