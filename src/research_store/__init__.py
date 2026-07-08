"""Research Store — the keystone handoff between the slow and fast loops.

The slow loop writes a *validated* ResearchProduct (ranked theses + regime read);
the fast loop reads it, sizes to $, and trades. It's a maintained set of current
beliefs (revised each run) plus an append-only journal — designed to stay coherent
as history grows past any single context window. Full rationale in docs/DESIGN.md
→ "Research Store design".

Public API
  write_product(product, mandate)        validate → persist → archive → journal
  read_current()                         → ResearchProduct | None
  get_targets(product=None)              → {symbol: weight}   (fast loop sizing)
  top(n) / by_symbol(symbol)             consumer queries
  is_stale(max_age_hours, now_iso)       guard the fast loop against stale research
  record_outcome(symbol, outcome, now)   close the calibration loop
  recent_journal(n)

Callers pass timestamps (`as_of` on the product, `now_iso` to queries) — the
package never reads the clock itself, so behavior is reproducible/testable.
"""
from datetime import datetime

from . import store
from .models import ResearchProduct, Thesis, TRADEABLE_VERDICTS, VERDICTS
from .validate import DEFAULT_MANDATE, reward_risk, validate_product

__all__ = [
    "ResearchProduct", "Thesis", "TRADEABLE_VERDICTS", "VERDICTS",
    "DEFAULT_MANDATE", "reward_risk", "validate_product", "MandateViolation",
    "write_product", "read_current", "get_targets", "top", "by_symbol",
    "is_stale", "record_outcome", "recent_journal", "store",
]


class MandateViolation(ValueError):
    """A research product failed the mandate's hard gates — refuse to persist."""


def write_product(product, mandate=DEFAULT_MANDATE, *, archive: bool = True):
    """Validate against the mandate, then persist + archive + journal the run.

    Raises MandateViolation (listing every violation) if the product breaks a hard
    rule — bad research must never reach the fast loop.
    """
    errors = validate_product(product, mandate)
    if errors:
        raise MandateViolation("; ".join(errors))
    store.save_current(product.to_dict(), archive=archive)
    held = [t for t in product.theses if t.target_weight > 0]
    store.append_journal({
        "event": "product",
        "as_of": product.as_of,
        "theses": len(product.theses),
        "held": len(held),
        "total_weight": round(sum(t.target_weight for t in held), 4),
        "regime": (product.regime or {}).get("status"),
    })
    return store.CURRENT


def read_current():
    d = store.load_current()
    return ResearchProduct.from_dict(d) if d else None


def get_targets(product=None) -> dict:
    """Fast-loop sizing input: {symbol: target_weight} for weighted positions only."""
    p = product or read_current()
    if not p:
        return {}
    return {t.symbol: t.target_weight for t in p.theses if t.target_weight > 0}


def top(n: int = 5, product=None) -> list:
    p = product or read_current()
    if not p:
        return []
    return sorted(p.theses, key=lambda t: t.rank)[:n]


def by_symbol(symbol: str, product=None):
    p = product or read_current()
    return p.by_symbol().get(symbol.upper()) if p else None


def is_stale(max_age_hours: float, now_iso: str, product=None) -> bool:
    """True if the current product is older than max_age_hours (fast-loop guard).

    Missing product or missing timestamp → stale (fail safe: don't trade blind).
    """
    p = product or read_current()
    if not p or not p.as_of:
        return True
    age = datetime.fromisoformat(now_iso) - datetime.fromisoformat(p.as_of)
    return age.total_seconds() > max_age_hours * 3600.0


def record_outcome(symbol: str, outcome: dict, now_iso: str) -> None:
    """Attach/replace a thesis outcome and journal it (the calibration loop)."""
    d = store.load_current()
    if not d:
        raise MandateViolation("no current product to record an outcome against")
    for t in d.get("theses", []):
        if str(t.get("symbol", "")).upper() == symbol.upper():
            t["outcome"] = outcome
            break
    else:
        raise KeyError(f"{symbol} not in current product")
    store.save_current(d, archive=False)
    store.append_journal({"event": "outcome", "symbol": symbol.upper(),
                          "at": now_iso, "outcome": outcome})


def recent_journal(n: int = 20) -> list:
    return store.read_journal()[-n:]
