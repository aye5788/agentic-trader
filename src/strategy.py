"""Load the codified strategy mandate (config/strategy.toml).

Single source of truth for WHAT the system does: risk gates (consumed by the
Research Store), the tradeable universe, PEAD signal thresholds, trade-management
rules, and the regime floor. TOML → human-editable with comments, read via stdlib
`tomllib` (no dependency). The slow/fast loops load this instead of hard-coding
parameters, so tuning the strategy is a config edit, not a code change.

Box-local overrides: config/strategy.local.toml (git-ignored) is deep-merged
over the base config when present. The committed strategy.toml ships SAFE
(live_approved=false); arming a box for live trading is a local-override act,
like installing credentials — it never travels through git.
"""
try:
    import tomllib  # stdlib, Python 3.11+
except ModuleNotFoundError:  # Python 3.10 (e.g. system python3 for the moomoo SDK)
    import tomli as tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = REPO_ROOT / "config" / "strategy.toml"
LOCAL_PATH = REPO_ROOT / "config" / "strategy.local.toml"


def _merge(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(path: Path = DEFAULT_PATH) -> dict:
    """Parse the strategy config, deep-merging the git-ignored local override
    (config/strategy.local.toml) over it when one exists."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    if path == DEFAULT_PATH and LOCAL_PATH.exists():
        with open(LOCAL_PATH, "rb") as f:
            _merge(cfg, tomllib.load(f))
    return cfg


def risk_mandate(cfg: dict | None = None) -> dict:
    """Return the [risk] table — the dict the Research Store validates against.

    Usage in the slow loop:
        import strategy, research_store as rs
        rs.write_product(product, mandate=strategy.risk_mandate())
    """
    return (cfg or load()).get("risk", {})
