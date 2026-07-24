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

import sys as _sys
from pathlib import Path as _Path
_SRC = str(_Path(__file__).resolve().parents[1])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)
from ledger import decision_id as _decision_id, outcome_from_exit as _outcome_from_exit  # noqa: E402

from . import store
from .models import ResearchProduct, Thesis, TRADEABLE_VERDICTS, VERDICTS
from .validate import DEFAULT_MANDATE, reward_risk, validate_product

__all__ = [
    "ResearchProduct", "Thesis", "TRADEABLE_VERDICTS", "VERDICTS",
    "DEFAULT_MANDATE", "reward_risk", "validate_product", "MandateViolation",
    "write_product", "read_current", "get_targets", "top", "by_symbol",
    "is_stale", "record_outcome", "record_rotation_outcome", "recent_journal", "store",
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
    for t in product.theses:
        if t.as_of:
            t.decision_id = _decision_id(t.symbol, t.as_of)
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


def _outcome_recorded(did: str) -> bool:
    """True if an `outcome` event for this decision_id is already journaled.

    Idempotency guard: recording the same close twice (re-run, retry) is a no-op."""
    if not did:
        return False
    return any(e.get("event") == "outcome" and e.get("decision_id") == did
               for e in store.read_journal())


def record_outcome(symbol: str, outcome: dict, now_iso: str) -> None:
    """Attach a thesis outcome and journal it (the calibration loop). Idempotent.

    Two paths, both supported:
      * current-book close (stop/target, prompts/exit.md) — the name is still in
        `current.json`; attach there.
      * rotation close (fast loop) — the rotated-out name is NO LONGER in the
        current book, so attach to the ARCHIVED thesis identified by
        `outcome['decision_id']` (= symbol:as_of).
    The `outcome` journal event is the learning-corpus record and is ALWAYS
    written; the belief attach is best-effort completeness. Re-recording the same
    decision_id is a safe no-op (idempotent)."""
    did = outcome.get("decision_id")
    if did and _outcome_recorded(did):
        return  # already recorded — safe to re-run

    attached = False
    # 1) current book (stop/take-profit exit path) — name still held
    d = store.load_current()
    if d:
        for t in d.get("theses", []):
            if str(t.get("symbol", "")).upper() == symbol.upper():
                t["outcome"] = outcome
                if not did:
                    did = t.get("decision_id") or (
                        _decision_id(symbol, t["as_of"]) if t.get("as_of") else None)
                store.save_current(d, archive=False)
                attached = True
                break

    # 2) rotated-out name -> ARCHIVED thesis by decision_id (symbol:as_of)
    if not attached:
        if not did:
            raise KeyError(
                f"{symbol} not in current product and outcome carries no "
                f"decision_id to locate an archived thesis")
        as_of = did.split(":", 1)[1] if ":" in did else None
        arch = store.load_archived(as_of) if as_of else None
        if arch:
            for t in arch.get("theses", []):
                if str(t.get("symbol", "")).upper() == symbol.upper():
                    t["outcome"] = outcome
                    store.save_archived(as_of, arch)
                    attached = True
                    break

    # 3) journal the label regardless — it is the corpus record
    ev = {"event": "outcome", "symbol": symbol.upper(), "at": now_iso, "outcome": outcome}
    if did:
        ev["decision_id"] = did
    if not attached:
        ev["note"] = "label recorded; no matching thesis found to attach"
    store.append_journal(ev)


def _last_held_archived_thesis(symbol: str):
    """(as_of, thesis) for the most-recent archived belief where `symbol` was
    HELD (target_weight>0), else (None, None). Anchors a rotation-close outcome to
    the decision that was standing when the position exited."""
    sym = symbol.upper()
    for stamp in reversed(store.archive_stamps()):
        d = store.load_archived(stamp)
        if not d:
            continue
        for t in d.get("theses", []):
            if str(t.get("symbol", "")).upper() == sym and (t.get("target_weight") or 0) > 0:
                return stamp, t
    return None, None


def record_rotation_outcome(symbol: str, entry_price: float, exit_price: float,
                            exit_date: str, now_iso: str, *,
                            exit_reason: str = "rebalanced",
                            spy_entry=None, spy_exit=None) -> dict | None:
    """Record the outcome for a rotated-out FULL close (name left the book).

    Finds the symbol's most-recent HELD archived thesis for stop/targets/as_of,
    computes the outcome (entry_price = the position's avg cost, which the agent
    supplies from get_equity_positions read BEFORE selling), and records it via
    record_outcome (archived-thesis path). Idempotent. Returns the outcome dict,
    or None if no held thesis exists to anchor to (logged by the caller)."""
    as_of, thesis = _last_held_archived_thesis(symbol)
    if not thesis:
        return None
    outcome = _outcome_from_exit(
        symbol=symbol, as_of=as_of, entry_price=entry_price, exit_price=exit_price,
        stop=thesis.get("stop"), targets=thesis.get("targets"),
        exit_reason=exit_reason, entry_date=as_of, exit_date=exit_date,
        spy_entry=spy_entry, spy_exit=spy_exit,
    )
    record_outcome(symbol, outcome, now_iso)
    return outcome


def recent_journal(n: int = 20) -> list:
    return store.read_journal()[-n:]
