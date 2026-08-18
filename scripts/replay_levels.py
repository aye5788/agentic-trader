#!/usr/bin/env python3
"""Replay frozen level-resolution cases against BOTH implementations.

⛔ WHY THIS EXISTS. This repo's recurring failure is not a wrong calculation. It
is two components answering the same question differently, with nothing that
compares them -- so the disagreement is discovered in production, one incident at
a time. On 2026-08-18 the display said the agent's stop was in force while the
monitor was enforcing the thesis stop, and three freshly-opened positions carried
levels nobody had chosen.

So this asserts three things at once, per frozen case:

  1. src/levels.resolve() produces the expected result. The expectation is
     written by hand in the fixture, INDEPENDENTLY of either implementation --
     a fixture regenerated from the code it tests proves only that the code
     equals itself.
  2. scripts/market_monitor.apply_overrides() -- the thing that actually places
     the sell -- produces the same result. The monitor is the reference; if
     these diverge, the resolver is wrong, not the monitor.
  3. The refusal is reported per field. A stop can be refused while its targets
     apply (TER, 2026-08-18), and reporting that as a blanket rejection would
     tell the agent something false in the other direction.

Run: .venv/bin/python scripts/replay_levels.py  (or --selftest, same thing)
Exit 1 on any mismatch. Wire into the pre-merge checks, not into a session.
"""
from __future__ import annotations

import json
import math
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import levels                                            # noqa: E402

FIXTURES = REPO / "tests" / "fixtures"


def _num(x):
    """Fixtures are JSON, which has no NaN literal; "nan" means the corrupt-quote case."""
    if isinstance(x, str) and x.lower() == "nan":
        return float("nan")
    return x


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) < 1e-9


def _same_list(a, b) -> bool:
    a = [float(x) for x in (a or [])]
    b = [float(x) for x in (b or [])]
    return len(a) == len(b) and all(abs(x - y) < 1e-9 for x, y in zip(a, b))


def _monitor_result(case):
    """What market_monitor.apply_overrides() would enforce for this case.

    Built through the monitor's own function rather than a copy of its rule, so
    a change to the monitor breaks this replay instead of silently diverging.
    Returns None when the monitor cannot be imported (it needs the moomoo SDK,
    which lives only under system python3) -- the resolver check still runs, and
    the caller reports the skip loudly rather than passing quietly.
    """
    try:
        import market_monitor as mm                       # noqa: PLC0415
    except Exception:
        return None

    class _T:                                             # minimal thesis stand-in
        def __init__(self, sym, stop, targets):
            self.symbol, self.stop, self.targets = sym, stop, list(targets or [])
            self.target_weight, self.verdict = 1.0, "buy"

    sym = "ZZZ"
    held = {sym: _T(sym, case["thesis_stop"], case["thesis_targets"])}
    ov = {sym: case["override"]} if case["override"] is not None else {}
    price = _num(case.get("price"))
    # A null price in the fixture means "the caller did not look up a price",
    # which is apply_overrides(prices=None) -- NOT an empty dict. An empty dict
    # means it looked and found nothing, and the monitor then fails closed.
    # Conflating the two was a bug in this harness, not in the resolver.
    if price is None and not case.get("price_guard"):
        out = mm.apply_overrides(held, ov)
    else:
        out = mm.apply_overrides(held, ov, {} if price is None else {sym: price})
    t = out[sym]
    return t.stop, list(t.targets or [])


def run(path: pathlib.Path) -> tuple[int, int, bool]:
    doc = json.loads(path.read_text())
    cases = doc["cases"]
    passed = failed = 0
    monitor_checked = False
    for c in cases:
        r = levels.resolve(c["thesis_stop"], c["thesis_targets"],
                           c["override"], price=_num(c.get("price")),
                           price_guard=bool(c.get("price_guard")))
        errs = []
        if not _same(r["effective_stop"], c["expect_stop"]):
            errs.append(f"stop {r['effective_stop']} != expected {c['expect_stop']}")
        if not _same_list(r["effective_targets"], c["expect_targets"]):
            errs.append(f"targets {r['effective_targets']} != expected {c['expect_targets']}")
        if ("REJECTED" in r["effective_stop_status"]) != bool(c["expect_stop_rejected"]):
            errs.append(f"stop rejection reported as {'REJECTED' in r['stop_status']}, "
                        f"expected {c['expect_stop_rejected']} ({r['stop_status']})")
        if ("REJECTED" in r["effective_targets_status"]) != bool(c["expect_targets_rejected"]):
            errs.append(f"target rejection reported as {'REJECTED' in r['targets_status']}, "
                        f"expected {c['expect_targets_rejected']} ({r['targets_status']})")

        m = _monitor_result(c)
        if m is not None:
            monitor_checked = True
            m_stop, m_targets = m
            if not _same(m_stop, r["effective_stop"]):
                errs.append(f"MONITOR DIVERGES: monitor stop {m_stop} vs resolver "
                            f"{r['effective_stop']}")
            if not _same_list(m_targets, r["effective_targets"]):
                errs.append(f"MONITOR DIVERGES: monitor targets {m_targets} vs resolver "
                            f"{r['effective_targets']}")
        if errs:
            failed += 1
            print(f"  FAIL  {c['name']}")
            for e in errs:
                print(f"          {e}")
        else:
            passed += 1
            print(f"  ok    {c['name']}")
    return passed, failed, monitor_checked


def read_model_check() -> int:
    """positions()' actual read model, end to end, for each frozen case.

    The fixtures replayed resolve() and apply_overrides() but never
    state.holdings(), which is why a review found the display still resolving
    against `mark` (which can be COST BASIS) instead of the monitor's quote
    file. Testing the two halves and not the thing built on them is how that
    survived. This asserts the field the agent actually reads.
    """
    import types as _types                                      # noqa: PLC0415
    sys.path.insert(0, str(REPO / "src" / "agent_env"))
    import state                                                # noqa: PLC0415
    bad = 0
    for f in sorted(FIXTURES.glob("levels_*.json")):
        for c in json.loads(f.read_text())["cases"]:
            th = _types.SimpleNamespace(symbol="ZZZ", stop=c["thesis_stop"],
                                        targets=list(c["thesis_targets"]),
                                        target_weight=1.0, verdict="buy",
                                        as_of="2026-08-17", signals={})
            price = _num(c.get("price"))
            valued = {"account_value": 100.0,
                      "positions": {"ZZZ": {"qty": 1.0, "avg_cost": 50.0,
                                            "mark": 999.0,      # deliberately WRONG
                                            "value": 999.0}}}
            mp = {} if price is None else {"ZZZ": price}
            h = state.holdings(valued, [th], {"ZZZ": c["override"]} if c["override"] else {}, mp)
            got = h["ZZZ"]["stop"]
            # Where the read model legitimately differs from the
            # apply_overrides(prices=None) path, the fixture states the
            # expectation by hand rather than the checker deriving it.
            want = c.get("read_model_expect_stop", c["expect_stop"])
            if not _same(got, want):
                bad += 1
                print(f"  READ-MODEL FAIL {c['name']}: positions() stop {got} "
                      f"!= expected {want}")
    print(f"  read-model check: {'all cases agree with the enforced stop' if not bad else f'{bad} FAILED'}")
    return bad


def differential_sweep() -> int:
    """Compare resolver vs enforcer across a GENERATED input space.

    ⛔ THE FIXTURES ARE NOT ENOUGH. Twelve hand-written cases proved equivalence
    for twelve shapes; an independent review generated a wider space and found
    73 divergent combinations the fixtures never touched -- malformed overrides
    that raised instead of being ignored, and non-finite stops/targets the
    enforcer accepted and the resolver refused. Hand-picked cases prove only
    that the cases were picked. This enumerates the space instead.
    """
    try:
        import market_monitor as mm                             # noqa: PLC0415
    except Exception as e:                                      # noqa: BLE001
        print(f"  differential sweep SKIPPED — market_monitor unimportable ({e})")
        return -1
    import copy
    import types as _types

    NAN, INF = float("nan"), float("inf")
    stops = [None, 90.0, 100.0, 110.0, NAN, INF, -INF, 0.0, -5.0, "x", True]
    targets = [None, [], [120.0], [130.0, 150.0], [NAN], [120.0, NAN],
               ["x"], "nope", [130.0]]
    widens = [None, True, False]
    reasons = [None, "", "   ", "a reason"]
    prices = [None, {}, {"A": 150.0}, {"A": 95.0}, {"A": NAN}, {"A": 0.0}]
    th_stop, th_targets = 100.0, [120.0, 140.0]

    checked = diverged = 0
    for st in stops:
        for tg in targets:
            for wd in widens:
                for rs in reasons:
                    for px in prices:
                        ov = {}
                        if st is not None: ov["stop"] = st
                        if tg is not None: ov["targets"] = tg
                        if wd is not None: ov["widen"] = wd
                        if rs is not None: ov["reason"] = rs
                        th = _types.SimpleNamespace(symbol="A", stop=th_stop,
                                                    targets=list(th_targets),
                                                    target_weight=1.0, verdict="buy")
                        try:
                            m = (mm.apply_overrides({"A": copy.copy(th)}, {"A": ov})
                                 if px is None else
                                 mm.apply_overrides({"A": copy.copy(th)}, {"A": ov}, px))["A"]
                            m_stop, m_tg = m.stop, [float(x) for x in (m.targets or [])]
                        except Exception as e:                  # noqa: BLE001
                            m_stop, m_tg = f"RAISED {type(e).__name__}", []
                        try:
                            r = levels.resolve(th_stop, list(th_targets), ov,
                                               price=(None if px is None else px.get("A")),
                                               price_guard=px is not None)
                            r_stop = r["effective_stop"]
                            r_tg = [float(x) for x in r["effective_targets"]]
                        except Exception as e:                  # noqa: BLE001
                            r_stop, r_tg = f"RAISED {type(e).__name__}", []
                        checked += 1
                        same = (_same(m_stop, r_stop) if not isinstance(m_stop, str)
                                and not isinstance(r_stop, str) else m_stop == r_stop)
                        if not same or m_tg != r_tg:
                            diverged += 1
                            if diverged <= 8:
                                print(f"  DIVERGE ov={ov} px={px}\n"
                                      f"          monitor: stop={m_stop} targets={m_tg}\n"
                                      f"          resolver: stop={r_stop} targets={r_tg}")
    print(f"\n  differential sweep: {checked} combinations, {diverged} divergent")
    return diverged


def main() -> int:
    files = sorted(FIXTURES.glob("levels_*.json"))
    if not files:
        print("no level fixtures found — nothing replayed", file=sys.stderr)
        return 1
    total_p = total_f = 0
    any_monitor = False
    for f in files:
        print(f"\n{f.name}")
        p, fl, mc = run(f)
        total_p += p
        total_f += fl
        any_monitor = any_monitor or mc
    print(f"\nreplay_levels: {total_p} passed, {total_f} failed")
    total_f += read_model_check()
    div = differential_sweep()
    if div > 0:
        total_f += div
    if not any_monitor:
        # ⛔ EXIT NON-ZERO. This printed a warning and still exited 0, so a run
        # that verified only half the point looked like a pass -- the same
        # "reads as coverage" defect the suite exists to catch. Found by review.
        print("⚠ FAIL: the monitor was NOT cross-checked (market_monitor could "
              "not be imported — it needs system python3 for the moomoo SDK). "
              "Run under /usr/bin/python3; a resolver-only run proves nothing "
              "about what is enforced.")
        return 1
    return 1 if total_f else 0


if __name__ == "__main__":
    sys.exit(main())
