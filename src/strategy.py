"""Load the codified strategy mandate (config/strategy.toml).

Single source of truth for WHAT the system does: risk gates (consumed by the
Research Store), the tradeable universe, PEAD signal thresholds, trade-management
rules, and the regime floor. TOML → human-editable with comments, read via stdlib
`tomllib` (no dependency). The slow/fast loops load this instead of hard-coding
parameters, so tuning the strategy is a config edit, not a code change.

Override layers (deep-merged, low → high precedence):
  1. config/strategy.toml          — committed base (ships SAFE, live_approved=false)
  2. config/strategy.adaptive.toml — machine-written by the adaptive layer's
                                     promote step (scripts/promote_proposal.py
                                     --apply/--set); git-ignored, box-local.
  3. config/strategy.local.toml    — box-local / human override; git-ignored.
The human's local override is highest, so it always wins over the learner —
arming a box for live trading, or pinning a knob by hand, is a local-override act
that never travels through git.
"""
try:
    import tomllib  # stdlib, Python 3.11+
except ModuleNotFoundError:  # Python 3.10 (e.g. system python3 for the moomoo SDK)
    import tomli as tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = REPO_ROOT / "config" / "strategy.toml"
ADAPTIVE_PATH = REPO_ROOT / "config" / "strategy.adaptive.toml"
LOCAL_PATH = REPO_ROOT / "config" / "strategy.local.toml"


def _merge(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(path: Path = DEFAULT_PATH) -> dict:
    """Parse the strategy config, deep-merging overrides when present. Precedence
    low → high: base (strategy.toml) < adaptive (strategy.adaptive.toml, learner-
    written) < local (strategy.local.toml, human). The human local override is
    applied last so it always wins over the learner."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    if path == DEFAULT_PATH:
        for override in (ADAPTIVE_PATH, LOCAL_PATH):   # adaptive first (lower), then local (wins)
            if override.exists():
                with open(override, "rb") as f:
                    _merge(cfg, tomllib.load(f))
    return cfg


def risk_mandate(cfg: dict | None = None) -> dict:
    """Return the [risk] table — the dict the Research Store validates against.

    Usage in the slow loop:
        import strategy, research_store as rs
        rs.write_product(product, mandate=strategy.risk_mandate())
    """
    return (cfg or load()).get("risk", {})
