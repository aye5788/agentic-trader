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
    "min_reward_risk": 2.0,        # require >= 2:1 reward:risk geometry
    "max_holdings": 14,            # concentration guard = book_hold(10) + sleeve_hold(4)
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
            rr = reward_risk(t)
            if rr is None:
                errors.append(f"{tag}: reward:risk undefined (degenerate geometry)")
            elif rr < m["min_reward_risk"]:
                errors.append(f"{tag}: reward:risk {rr:.2f} < required {m['min_reward_risk']}")

    if total_w > m["max_total_weight"] + 1e-9:
        errors.append(f"total weight {total_w:.3f} exceeds {m['max_total_weight']}")
    if held > m["max_holdings"]:
        errors.append(f"{held} weighted holdings exceeds max {m['max_holdings']}")
    return errors
