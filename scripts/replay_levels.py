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
    if not any_monitor:
        # Loud, not silent: the monitor is the reference implementation, and a
        # run that only checked the resolver has verified half of the point.
        print("⚠ the monitor was NOT cross-checked (market_monitor could not be "
              "imported — it needs system python3 for the moomoo SDK). Run this "
              "under /usr/bin/python3 to compare against the real enforcer.")
    return 1 if total_f else 0


if __name__ == "__main__":
    sys.exit(main())
