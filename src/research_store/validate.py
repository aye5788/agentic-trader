"""Validation gates for the Research Store — the strategy's hard rules, in code.

These thresholds ARE the codified risk mandate (position cap, reward:risk floor,
concentration). They are externalized to `config/strategy.toml` ([risk] table),
loaded via `strategy.risk_mandate()` and passed to write_product(); DEFAULT_MANDATE
below is the in-code fallback / default. write_product() enforces them so a
bad-geometry or over-concentrated product can NEVER reach the fast loop — quality
can't silently rot.
"""
from .models import Thesis, VERDICTS

# The risk mandate. Sourced from the IBD-derived trade-management spec
# (docs/DESIGN.md → "Trade management & risk rules").
DEFAULT_MANDATE = {
    "max_weight_per_name": 0.10,   # IBD "full position" = 10% of the sleeve
    "max_total_weight": 1.0,       # fully-invested ceiling (sum of weights)
    "max_holdings": 14,            # concentration guard = equity-only book_hold(14)
}


def reward_risk(t: Thesis) -> float | None:
    """Reward:risk for a long: (target1 - entry) / (entry - stop), entry = zone mid.

    None if geometry is incomplete or risk is non-positive (a degenerate setup).
    """
    if not t.entry_zone or t.stop is None or not t.targets:
        return None
    lo, hi = t.entry_zone
    entry = (lo + hi) / 2.0
    risk = entry - t.stop
    if risk <= 0:
        return None
    return (t.targets[0] - entry) / risk


def validate_product(product, mandate: dict = DEFAULT_MANDATE) -> list:
    """Return a list of human-readable mandate violations ([] means valid).

    Collects ALL violations (not just the first) so the slow loop can fix the
    whole product in one pass.
    """
    m = {**DEFAULT_MANDATE, **(mandate or {})}
    errors: list = []
    theses = product.theses

    ranks = [t.rank for t in theses]
    if len(set(ranks)) != len(ranks):
        errors.append("duplicate ranks across theses")

    total_w = 0.0
    held = 0
    for t in theses:
        tag = t.symbol
        if t.verdict not in VERDICTS:
            errors.append(f"{tag}: unknown verdict '{t.verdict}'")
        if not 0.0 <= t.confidence <= 1.0:
            errors.append(f"{tag}: confidence {t.confidence} out of [0,1]")
        if t.target_weight < 0 or t.target_weight > m["max_weight_per_name"]:
            errors.append(
                f"{tag}: weight {t.target_weight} outside [0, {m['max_weight_per_name']}]")
        total_w += max(0.0, t.target_weight)

        if t.target_weight > 0:
            held += 1
            if not t.entry_zone or t.stop is None or not t.targets:
                errors.append(f"{tag}: weighted position missing entry_zone/stop/targets")
                continue
            lo, hi = t.entry_zone
            if not lo < hi:
                errors.append(f"{tag}: entry_zone {t.entry_zone} not low<high")
            if not t.stop < lo:
                errors.append(f"{tag}: stop {t.stop} not below entry-low {lo}")
            if not t.targets[0] > hi:
                errors.append(f"{tag}: first target {t.targets[0]} not above entry-high {hi}")
            # The reward:risk >= 2 gate was deleted 2026-08-09. It was tautological:
            # slow_loop built target[0] AS 2.2x the stop distance, so it could only fail on
            # NaN, rounding, or a degenerate sigma. Zero information, non-zero abort risk
            # (it could reject a whole valid book), and a STRATEGY opinion inside a SAFETY
            # path. It also emitted a PASS, so it read as evidence geometry was validated.
            # Trade geometry belongs to the agent. See the inversion spec, 2026-08-09.
            # Degenerate geometry (risk <= 0, i.e. stop at/above the entry mid-point) is
            # still caught above by "stop below entry-low": risk <= 0 implies
            # stop >= entry_mid > entry_low, so it was always redundant with that check.

    if total_w > m["max_total_weight"] + 1e-9:
        errors.append(f"total weight {total_w:.3f} exceeds {m['max_total_weight']}")
    if held > m["max_holdings"]:
        errors.append(f"{held} weighted holdings exceeds max {m['max_holdings']}")
    return errors


def _selftest() -> None:
    """The structural gates must still bite; the tautological one must be gone."""
    import types
    from .models import Thesis, VERDICTS
    v = sorted(VERDICTS)[0]

    def _p(lo, hi, stop, t1, weight=0.05):
        t = Thesis(symbol="AAA", rank=1, verdict=v, target_weight=weight,
                   entry_zone=[lo, hi], stop=stop, targets=[t1], confidence=0.5)
        return types.SimpleNamespace(theses=[t])

    # entry mid 100, risk 2.00, reward 1.50 -> reward:risk 0.75, well under the old
    # 2.0 floor. It must now VALIDATE. The old gate could only ever fail on NaN or a
    # degenerate sigma, because slow_loop built target[0] AS 2.2x the stop distance --
    # arithmetic checking itself, while reading to every reviewer as a real gate.
    assert validate_product(_p(99.0, 101.0, 98.0, 101.5)) == [], \
        validate_product(_p(99.0, 101.0, 98.0, 101.5))

    # the STRUCTURAL gates are unaffected and must still reject:
    assert any("stop" in e for e in validate_product(_p(99.0, 101.0, 100.0, 105.0)))
    assert any("first target" in e for e in validate_product(_p(99.0, 101.0, 98.0, 100.5)))
    assert any("weight" in e for e in validate_product(_p(99.0, 101.0, 98.0, 105.0, 0.5)))
    assert any("entry_zone" in e for e in validate_product(_p(101.0, 99.0, 98.0, 105.0)))

    # DEGENERATE GEOMETRY (stop at or above the entry mid-point, or at entry_low) must
    # still be rejected even with the reward:risk gate gone -- it is caught by the
    # "stop below entry-low" structural check, since risk <= 0 implies
    # stop >= entry_mid > entry_low (entry_zone low<high is enforced separately).
    lo, hi = 99.0, 101.0
    mid = (lo + hi) / 2.0  # 100.0
    # stop == entry mid-point (risk == 0, degenerate)
    errs = validate_product(_p(lo, hi, mid, 105.0))
    assert any("stop" in e for e in errs), errs
    # stop > entry mid-point (risk < 0, degenerate)
    errs = validate_product(_p(lo, hi, mid + 0.5, 105.0))
    assert any("stop" in e for e in errs), errs
    # stop == entry_low exactly (boundary: not strictly below entry-low)
    errs = validate_product(_p(lo, hi, lo, 105.0))
    assert any("stop" in e for e in errs), errs

    print("selftest OK: validate -- structural gates bite, tautological R:R gate gone, "
          "degenerate geometry still rejected")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
