"""Typed models for the Research Store — the slow→fast loop handoff.

A ResearchProduct is one nightly research output: a ranked set of Thesis records
(the current *book*) plus the regime read. Plain dataclasses with to_dict/from_dict
so the store is model-independent JSON on disk — any future agent, or a human, can
read it cold (the "repo = single source of truth" principle).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields

# Verdicts that imply we hold size (and thus need full trade geometry + weight).
TRADEABLE_VERDICTS = {"buy", "accumulate", "hold", "trim"}
VERDICTS = TRADEABLE_VERDICTS | {"exit", "avoid"}


def _known(cls, d: dict) -> dict:
    """Drop unknown keys so from_dict tolerates schema drift / extra fields."""
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in (d or {}).items() if k in names}


@dataclass
class Thesis:
    """One name's current view + the trade plan that expresses it."""
    symbol: str
    rank: int
    verdict: str
    thesis: str = ""                                  # the written view (prose)
    entry_zone: list | None = None                    # [low, high]; never chase above
    stop: float | None = None                         # enforced exit floor
    targets: list = field(default_factory=list)       # tiered, ascending
    target_weight: float = 0.0                        # fraction of the sleeve (0..cap)
    confidence: float = 0.0                           # 0..1
    signals: dict = field(default_factory=dict)       # evidence + provenance
    as_of: str | None = None
    review_by: str | None = None                      # e.g. "2026-07-30 (weekly rebalance)"
    earnings_date: str | None = None                  # "YYYY-MM-DD" next confirmed/est.
                                                      #   report, stamped by slow_loop from
                                                      #   event_calendar. None = unknown, and
                                                      #   unknown NEVER fabricates a flag.
    outcome: dict | None = None                       # set on close: {status, pnl_pct, ...}
    decision_id: str | None = None                    # "<SYMBOL>:<as_of>" join key

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Thesis":
        return cls(**_known(cls, d))


@dataclass
class ResearchProduct:
    """One nightly research output: the ranked book + regime context."""
    as_of: str
    theses: list = field(default_factory=list)        # list[Thesis], rank order
    regime: dict | None = None                        # {status, floor, notes}
    notes: str = ""
    schema_version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)                            # recurses into Thesis records

    @classmethod
    def from_dict(cls, d: dict) -> "ResearchProduct":
        d = _known(cls, d)
        d["theses"] = [Thesis.from_dict(t) for t in (d.get("theses") or [])]
        return cls(**d)

    def by_symbol(self) -> dict:
        return {t.symbol: t for t in self.theses}
