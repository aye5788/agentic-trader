"""Load the codified strategy mandate (config/strategy.toml).

Single source of truth for WHAT the system does: risk gates (consumed by the
Research Store), the tradeable universe, PEAD signal thresholds, trade-management
rules, and the regime floor. TOML → human-editable with comments, read via stdlib
`tomllib` (no dependency). The slow/fast loops load this instead of hard-coding
parameters, so tuning the strategy is a config edit, not a code change.
"""
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = REPO_ROOT / "config" / "strategy.toml"


def load(path: Path = DEFAULT_PATH) -> dict:
    """Parse and return the full strategy config as a nested dict."""
    with open(path, "rb") as f:
        return tomllib.load(f)


def risk_mandate(cfg: dict | None = None) -> dict:
    """Return the [risk] table — the dict the Research Store validates against.

    Usage in the slow loop:
        import strategy, research_store as rs
        rs.write_product(product, mandate=strategy.risk_mandate())
    """
    return (cfg or load()).get("risk", {})
