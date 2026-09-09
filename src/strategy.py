"""Load the codified strategy mandate (config/strategy.toml).

Single source of truth for WHAT the system does: risk gates (consumed by the
Research Store), the tradeable universe, PEAD signal thresholds, trade-management
rules, and the regime floor. TOML → human-editable with comments, read via stdlib
`tomllib` (no dependency). The slow/fast loops load this instead of hard-coding
parameters, so tuning the strategy is a config edit, not a code change.

Override layers (deep-merged, low → high precedence):
  1. config/strategy.toml       — committed base (ships SAFE, live_approved=false)
  2. config/strategy.local.toml — box-local / human override; git-ignored.
Arming a box for live trading, or pinning a knob by hand, is a local-override act
that never travels through git.

⛔ THERE IS NO MACHINE-WRITTEN OVERRIDE LAYER (removed 2026-09-09). A third file,
config/strategy.adaptive.toml, used to sit between these two and was written by an
off-box learner that tuned stop_atr_mult. The whole adaptive layer is gone — see
docs/OPSLOG.md 2026-09-09. Every knob in this config is now set by a human and
only by a human. Do not reintroduce a layer that edits strategy from code.
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
    """Parse the strategy config, deep-merging the local override when present.
    Precedence low → high: base (strategy.toml) < local (strategy.local.toml,
    human). There is no machine-written layer — see the module docstring."""
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
