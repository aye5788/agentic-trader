"""THE ONE PLACE that decides which stop and targets are actually in force.

⛔ WHY THIS EXISTS. Two components answered that question differently:

  - the MONITOR (scripts/market_monitor.py apply_overrides) applied an agent
    override stop only when it was STRICTER than the thesis stop, or when the
    override carried an explicit `widen` flag WITH a reason; and it applied
    override targets only when the list length matched the thesis target list.

  - the DISPLAY (src/agent_env/state.py) took the agent override whenever one
    existed, unconditionally.

So `positions()` showed the agent one set of levels while the monitor enforced
another. Measured 2026-08-18: XOM, RTX and BAC were all displayed with the stop
and single target the agent had just reasoned out, while the monitor had
rejected every one of them -- the stops for being looser with no `widen`, the
targets for being one where the thesis carried two. The agent had no way to see
that; the enforcement report that exists to tell it was itself not passing the
`widen` flag, so MRK -- the one position whose override IS in force -- was
reported wrong too.

That is the defect class this repo keeps hitting: a check that reads as coverage
while the thing it guarantees is not true.

This module is PURE and is the single definition. The monitor's behaviour is the
reference: this encodes that rule and nothing else, so making the display honest
cannot change what is enforced.
"""
from __future__ import annotations

import math


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(float(x))


def resolve(thesis_stop, thesis_targets, override, price=None,
            price_guard: bool = False, thesis_as_of=None) -> dict:
    """Which stop/targets are in force, and why. Pure.

    `override` is the agent's overrides.json entry for this symbol (or None).

    `price` / `price_guard` mirror market_monitor.apply_overrides(prices=...):

      price_guard=False  -- the caller did not look up a price at all. Matches
        apply_overrides(prices=None), which skips the arming guard entirely.
      price_guard=True   -- the caller DID look up a price. Matches the live
        monitor, which always passes a real dict: a missing, non-finite or
        non-positive price then DROPS the override rather than applying it.
        Fails closed, because a corrupt quote once let a stale override be
        applied over a thesis stop.

    Getting this pair wrong is how the display and the enforcer diverge while
    both look correct, so the replay fixtures cover both settings.

    Returns every input alongside the resolved answer, so a caller can show the
    agent what it asked for AND what it got, rather than silently one or other.
    """
    # ⛔ A MALFORMED OVERRIDE MUST NOT RAISE. apply_overrides ignores any
    # non-dict entry ("garbage", a list, a number) and degrades that symbol to
    # "no override". This called .get() straight away and raised AttributeError
    # instead -- and holdings() raised even earlier -- so one bad entry in
    # overrides.json would have taken down positions() entirely while the
    # monitor carried on. Found by review, 2026-08-18.
    ov = override if isinstance(override, dict) else {}
    ov_stop = ov.get("stop")
    ov_targets = ov.get("targets")
    # `widen` counts only WITH a reason -- same conjunction the monitor uses.
    widen = bool(ov.get("widen")) and bool(str(ov.get("reason") or "").strip())

    # Field names are Codex's specification verbatim (2026-08-18 review), so a
    # consumer written against that spec needs no translation layer. Provenance
    # is carried because the failure being fixed was a stale value quoted as
    # current: `thesis_as_of` is the product's date, `override_written_at` is
    # the override entry's own `ts`.
    out = {
        "thesis_stop": thesis_stop,
        "override_stop": ov_stop if _finite(ov_stop) else None,
        "override_widen": widen,
        "effective_stop": thesis_stop,
        "effective_stop_source": "thesis" if thesis_stop is not None else "none",
        "effective_stop_status": "no override",
        "thesis_targets": list(thesis_targets or []),
        "override_targets": list(ov_targets) if isinstance(ov_targets, list) else None,
        "effective_targets": list(thesis_targets or []),
        "effective_targets_source": "thesis",
        "effective_targets_status": "no override",
        "thesis_as_of": thesis_as_of,
        "override_written_at": ov.get("ts"),
    }

    # ---- stop -------------------------------------------------------------
    if _finite(ov_stop):
        s = float(ov_stop)
        # A stop at or above the live price would arm already-breached and sell
        # instantly; the monitor drops such an override rather than applying it.
        # price is None    -> the caller did not look. Skip unless price_guard
        #                       says it DID look and found nothing.
        # price is garbage  -> it looked and got NaN/inf/<=0. Always reject:
        #                       a corrupt quote once let a stale override be
        #                       applied over a live thesis stop.
        _looked = price is not None or price_guard
        _usable = _finite(price) and float(price) > 0
        if _looked and not _usable:
            out["effective_stop_status"] = ("override REJECTED: no usable live price to "
                                  "check it against — the guard fails closed")
        elif _usable and s >= float(price):
            out["effective_stop_status"] = "override REJECTED: at or above the live price"
        elif thesis_stop is None:
            out["effective_stop_status"] = "override ignored: no thesis stop to apply it over"
        elif s > float(thesis_stop):
            out.update(effective_stop=s, effective_stop_source="override",
                       effective_stop_status="override applied: tighter than the thesis stop")
        elif widen:
            out.update(effective_stop=s, effective_stop_source="override",
                       effective_stop_status="override applied: explicit widen with a reason")
        else:
            out["effective_stop_status"] = (
                "override REJECTED: looser than the thesis stop and not marked "
                "widen — the thesis stop is what the monitor enforces")
    elif ov_stop is not None:
        out["effective_stop_status"] = "override REJECTED: stop is not a finite number"

    # ---- targets ----------------------------------------------------------
    th_t = list(thesis_targets or [])
    # An EMPTY list is not an override. set_levels accepts "no target" as a
    # legal state, and reporting that as a rejected override would tell the
    # agent something was refused when nothing was asked.
    if isinstance(ov_targets, list) and ov_targets:
        if not th_t:
            out["effective_targets_status"] = "override ignored: the thesis carries no targets"
        elif len(ov_targets) != len(th_t):
            out["effective_targets_status"] = (
                f"override REJECTED: {len(ov_targets)} target(s) supplied but the "
                f"thesis carries {len(th_t)} — supply all of them or none")
        elif not all(_finite(o) for o in ov_targets):
            out["effective_targets_status"] = "override REJECTED: a target is not a finite number"
        else:
            out.update(effective_targets=[float(o) for o in ov_targets],
                       effective_targets_source="override",
                       effective_targets_status="override applied")
    return out


def _selftest() -> None:
    # tighter stop wins
    r = resolve(100.0, [120.0], {"stop": 105.0})
    assert r["effective_stop"] == 105.0 and r["effective_stop_source"] == "override", r

    # ⛔ THE LIVE 2026-08-18 CASE. A looser stop with no widen is REJECTED, and
    # the resolver must say so rather than displaying it as if it were in force.
    r = resolve(155.0225, [175.6225, 187.2099], {"stop": 155.0, "targets": [172.0]})
    assert r["effective_stop"] == 155.0225, r
    assert r["effective_stop_source"] == "thesis" and "REJECTED" in r["effective_stop_status"], r
    # ...and one target against the thesis's two is refused, with the count named
    assert r["effective_targets"] == [175.6225, 187.2099], r
    assert "1 target(s) supplied" in r["effective_targets_status"], r
    assert "thesis carries 2" in r["effective_targets_status"], r

    # widen WITH a reason applies a looser stop (the MRK case)
    r = resolve(130.0668, [], {"stop": 128.5, "widen": True, "reason": "inside its own noise"})
    assert r["effective_stop"] == 128.5 and r["override_widen"] is True, r
    assert "explicit widen" in r["effective_stop_status"], r
    # widen with NO reason does not
    r = resolve(130.0668, [], {"stop": 128.5, "widen": True, "reason": "  "})
    assert r["effective_stop"] == 130.0668 and r["override_widen"] is False, r

    # matching target count applies, in EITHER direction
    r = resolve(100.0, [120.0, 140.0], {"targets": [130.0, 150.0]})
    assert r["effective_targets"] == [130.0, 150.0], r
    r = resolve(100.0, [120.0, 140.0], {"targets": [110.0, 130.0]})
    assert r["effective_targets"] == [110.0, 130.0], r

    # a stop at or above the live price is refused (arming already-breached)
    r = resolve(100.0, [], {"stop": 210.0}, price=200.0)
    assert r["effective_stop"] == 100.0 and "live price" in r["effective_stop_status"], r
    # ...and a corrupt price refuses it too, rather than sailing through
    r = resolve(100.0, [], {"stop": 105.0}, price=float("nan"))
    assert r["effective_stop"] == 100.0, r
    # unknown price with NO guard skips it, as apply_overrides(prices=None) does
    r = resolve(100.0, [], {"stop": 105.0}, price=None)
    assert r["effective_stop"] == 105.0, r
    # ...but a caller that DID look and found nothing must fail closed, which is
    # what the live monitor does (it always passes a prices dict).
    r = resolve(100.0, [], {"stop": 105.0}, price=None, price_guard=True)
    assert r["effective_stop"] == 100.0 and "fails closed" in r["effective_stop_status"], r

    # a malformed override is IGNORED, exactly as apply_overrides ignores it,
    # and must never raise -- positions() would otherwise die on a bad file
    for junk in ("garbage", ["nope"], 7, True):
        r = resolve(100.0, [120.0], junk)
        assert r["effective_stop"] == 100.0 and r["effective_targets"] == [120.0], (junk, r)

    # no override at all is quiet and keeps the thesis
    r = resolve(100.0, [120.0], None)
    assert r["effective_stop"] == 100.0 and r["effective_targets"] == [120.0], r
    assert r["effective_stop_source"] == "thesis" and r["effective_targets_source"] == "thesis", r

    # non-finite override stop is refused, not coerced
    r = resolve(100.0, [], {"stop": float("inf")})
    assert r["effective_stop"] == 100.0 and "finite" in r["effective_stop_status"], r

    print("selftest OK: levels.resolve — one rule for display and enforcement; "
          "a looser un-widened stop and a mismatched target count are both "
          "REJECTED and say so")


if __name__ == "__main__":
    _selftest()
