"""CONTROL BINDING — did each guard ever actually FIRE?

`src/health.py` answers "did this job RUN?" That is liveness, and it is blind to
the failure that has actually bitten this repo repeatedly: a control that is
configured, documented, shipped, selftested GREEN — and inert.

The roll-call as of 2026-08-05:

  no_chase          set + documented 3x, read by zero code    (dead from inception
                                                               -> fixed 2026-07-28)
  earnings_soon     read Thesis.review_by, which slow_loop
                    always writes as "(weekly rebalance)"     (0 fires, ever)
  ma_break_days     passed into build_facts, never read       (structurally impossible)
  ma_exit_days      "exit if close < 21-day MA"               (no live consumer)
  take-profit t1/t2 armed, ~1.4% touch probability per hold   (0 of 14)

Every one was found by a human noticing an anomaly and someone going digging.
That is not an architecture, that is luck with an attentive owner.

A control that has never bound is indistinguishable from a control that does not
exist. This module makes that distinction measurable: each guard declares itself,
its fires are counted from the machine-written record (never from narration), and
a control that has never fired is a DEFECT until proven otherwise — it is either
miscalibrated or disconnected, and both need a human.

`evaluate()` is pure (observations in, verdicts out) so the calendar/threshold
logic is fully selftested; `gather()` is the thin I/O that counts fires on disk.

    python3 src/controls.py --selftest    # logic tests, no I/O
    python3 src/controls.py               # live roll-call
"""
from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import dataclass

# status values
BINDING = "binding"     # fired within its expected window — provably alive
SILENT = "silent"       # has fired before, but not lately (miscalibration risk)
NEVER = "never"         # never fired since it shipped — DEFECT until proven otherwise
YOUNG = "young"         # shipped too recently to expect a fire yet
UNKNOWN = "unknown"     # not measured this pass — never alerts


@dataclass(frozen=True)
class Control:
    """A guard that is supposed to bind, and how often we expect it to."""
    key: str
    label: str
    expect_days: int        # a fire is expected at least this often
    evidence: str           # where its fires are recorded (for the human)


@dataclass(frozen=True)
class Binding:
    key: str
    label: str
    fires: int
    last_fire: dt.datetime | None
    status: str
    detail: str

    @property
    def healthy(self) -> bool:
        return self.status in (BINDING, YOUNG)

    @property
    def alertable(self) -> bool:
        return self.status not in (BINDING, YOUNG, UNKNOWN)


# The registry. Adding a guard to the system means adding it HERE — that is the
# point: an unregistered control is invisible, and invisible is how they die.
#
# ⚠️ 2026-08-13 — FIVE OF THESE NO LONGER HAVE A LIVE PRODUCER, and saying so
# here is the whole purpose of this file. The intraday risk-review overlay was
# retired into the agent sessions (deploy/crontab.template explains why: 0 orders
# in the 3 runs after sessions went live, 93 `watch` no-ops in 40 runs). It was
# the ONLY thing computing `risk_review facts flags`, so:
#
#   earnings_soon, near_stop, giveback, vol_expansion, ma_break
#       -> their producer, `risk_review.py --facts`, is no longer scheduled.
#   risk_review_order
#       -> obsolete outright; there is no risk-review agent to place an order.
#
# These duties did NOT vanish — the open/close sessions assess events, stops and
# giveback with judgment, and market_monitor watches every stop CONTINUOUSLY
# through RTH. What vanished is the flag-shaped EVIDENCE that they were assessed.
# That is a measurement gap, not a coverage gap, and it is left declared rather
# than deleted so the next person to wire this file sees it.
#
# ⛔ Nothing here fires today anyway: main() raises "live roll-call not wired
# yet". This registry is a declaration, not a live detector — which is why
# retiring the overlay broke no runtime behaviour. If you wire gather(), decide
# THEN whether these five are re-pointed at session evidence, re-fed by putting
# `risk_review.py --facts` back on cron (pure Python, 3s, no model), or retired.
REGISTRY = {
    "stop_fired":     Control("stop_fired", "Stop-loss exits", 30,
                              "journal exit_signal (reason=stop)"),
    "earnings_soon":  Control("earnings_soon", "Earnings-proximity flag", 30,
                              "risk_review facts flags"),
    "near_stop":      Control("near_stop", "Near-stop flag", 21,
                              "risk_review facts flags"),
    "giveback":       Control("giveback", "Giveback-from-high flag", 30,
                              "risk_review facts flags"),
    "vol_expansion":  Control("vol_expansion", "Vol-expansion flag", 30,
                              "risk_review facts flags"),
    "ma_break":       Control("ma_break", "21-day MA break flag", 30,
                              "risk_review facts flags"),
    "no_chase":       Control("no_chase", "Chase guard (skipped buys)", 30,
                              "journal execution (reason=chase)"),
    "take_profit":    Control("take_profit", "Take-profit scale-outs", 60,
                              "journal exit_signal (reason=target1|target2)"),
    "risk_review_order": Control("risk_review_order", "Risk-review orders placed", 30,
                                 "journal risk_review orders_intended"),
}


def evaluate(now: dt.datetime, observations: dict,
             registry: dict | None = None) -> list[Binding]:
    """Pure: {key: (fires, last_fire|None, shipped_days_ago)} -> verdicts.

    `shipped_days_ago` lets a freshly-added control be YOUNG rather than a false
    DEFECT. A control absent from `observations` is UNKNOWN and never alerts —
    you cannot act on a measurement that was not taken.
    """
    reg = REGISTRY if registry is None else registry
    out: list[Binding] = []
    for key, c in reg.items():
        obs = observations.get(key)
        if obs is None:
            out.append(Binding(key, c.label, 0, None, UNKNOWN, "not measured this pass"))
            continue
        fires, last, shipped = obs
        if fires <= 0:
            if shipped is not None and shipped < c.expect_days:
                out.append(Binding(key, c.label, 0, None, YOUNG,
                                   f"shipped {shipped}d ago; a fire is not yet expected "
                                   f"(within {c.expect_days}d)"))
            else:
                age = f"{shipped}d" if shipped is not None else "its whole life"
                out.append(Binding(key, c.label, 0, None, NEVER,
                                   f"NEVER fired in {age} — miscalibrated or "
                                   f"disconnected. Evidence: {c.evidence}"))
            continue
        age_d = (now - last).total_seconds() / 86400 if last else None
        if age_d is not None and age_d > c.expect_days:
            out.append(Binding(key, c.label, fires, last, SILENT,
                               f"{fires} fires but none in {int(age_d)}d "
                               f"(expected within {c.expect_days}d)"))
        else:
            when = f"{int(age_d)}d ago" if age_d is not None else "recently"
            out.append(Binding(key, c.label, fires, last, BINDING,
                               f"{fires} fires, last {when}"))
    return out


# ------------------------------------------------------------------ selftest

def _selftest() -> None:
    now = dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.timezone.utc)
    reg = {"x": Control("x", "X guard", 30, "journal")}

    # a control that has never fired, and is old enough to have, is a DEFECT
    r = evaluate(now, {"x": (0, None, 60)}, reg)[0]
    assert r.status == NEVER, r
    assert r.alertable and not r.healthy, r
    assert "NEVER fired" in r.detail, r.detail

    # ...but one that shipped yesterday is merely YOUNG — no false alarm
    r = evaluate(now, {"x": (0, None, 1)}, reg)[0]
    assert r.status == YOUNG and r.healthy and not r.alertable, r

    # fired recently -> binding
    r = evaluate(now, {"x": (7, now - dt.timedelta(days=2), 90)}, reg)[0]
    assert r.status == BINDING and r.healthy, r
    assert r.fires == 7, r

    # fired, but long ago -> silent (miscalibration, not disconnection)
    r = evaluate(now, {"x": (3, now - dt.timedelta(days=45), 90)}, reg)[0]
    assert r.status == SILENT and r.alertable, r
    assert "none in 45d" in r.detail, r.detail

    # unmeasured -> UNKNOWN, and UNKNOWN must never alert
    r = evaluate(now, {}, reg)[0]
    assert r.status == UNKNOWN and not r.alertable, r

    # the real registry must be evaluable and every key must round-trip
    verdicts = evaluate(now, {}, None)
    assert {v.key for v in verdicts} == set(REGISTRY), "registry/evaluate key drift"
    assert all(v.status == UNKNOWN for v in verdicts), "unmeasured must be UNKNOWN"

    print(f"selftest OK: control-binding verdicts ({len(REGISTRY)} registered)")


def main() -> None:
    ap = argparse.ArgumentParser(description="control-binding roll-call")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        return
    raise SystemExit("live roll-call not wired yet — run --selftest")


if __name__ == "__main__":
    main()
