"""Journal-backed reconciliation for the forward-only position lifecycle.

WHAT THIS IS
    `src/position_lifecycle.py` is a PURE state machine over a prior -> new
    position pair. It needs a caller to answer two questions it deliberately
    refuses to answer itself: *what did we hold last time* and *which lifecycle
    is currently open for each symbol*. This module answers both by REPLAYING
    THE APPEND-ONLY JOURNAL, and nothing else.

⛔ WHY PRIOR STATE COMES FROM THE EVENT STREAM AND NOT FROM positions.json
    The obvious prior is the snapshot file about to be overwritten. It is the
    wrong one, for two independent reasons.

    1. THE SNAPSHOT HAS A SECOND, HAND-WRITING AUTHOR. `prompts/exit.md` step 7
       instructs the monitor's exit executor to rewrite
       research_store/rh/positions.json itself, without going through
       agent_env.server._write_broker_snapshot. So after a stop fires, the file
       ALREADY shows the position gone by the time the next session publishes.
       Diffing file-against-broker would compare "absent" with "absent", emit
       nothing, and lose the CLOSE — the one terminal event this whole stream
       exists to capture — permanently and silently, leaving an open lifecycle
       for a position that no longer exists. Replaying the stream, the close is
       still pending and is recorded at the next reconciliation.

    2. IDEMPOTENCY BECOMES STRUCTURAL RATHER THAN INCIDENTAL. The prior is
       computed from the very events we appended last time, so re-running
       against unchanged broker state necessarily produces an empty diff. There
       is no window — not even a crash between the append and the file replace —
       in which the same transition can be emitted twice.

    This introduces NO second source of truth: the journal is append-only and is
    already the system's factual record. Nothing here is stored anywhere else,
    and nothing here is mutable.

    The cost, stated plainly: if an append is ever lost, two successive moves
    collapse into one aggregate event (qty went from X to Z, with the
    intermediate step unrecorded). That is a coarser TRUE statement about
    observed state, never a false one, and it is the direction that self-heals.

PURITY
    No I/O, no clock, no broker, no filesystem. The caller supplies the journal
    events, the new positions and the timestamp. Same inputs -> same outputs.
"""
from __future__ import annotations

import position_lifecycle as _lc

LIFECYCLE_EVENT_NAMES = (_lc.EVENT_OPENED, _lc.EVENT_CHANGED, _lc.EVENT_CLOSED)


def is_lifecycle_event(entry) -> bool:
    """True for a well-formed lifecycle event of the schema we understand.

    The schema check is not decoration. A future schema 2 event replayed by
    schema-1 logic would produce a prior state we cannot vouch for, and this
    module's whole job is to be the thing that can be vouched for. An event we
    do not understand is not read at all.
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("event") not in LIFECYCLE_EVENT_NAMES:
        return False
    if entry.get("lifecycle_schema") != _lc.LIFECYCLE_SCHEMA:
        return False
    return bool(entry.get("position_id")) and bool(entry.get("symbol"))


def lifecycle_events(journal) -> list:
    """Only the lifecycle events, in file order. Everything else is ignored.

    The journal carries thirteen other event kinds (agent_decision, execution,
    exit_signal, outcome, ...). None of them describes position lifecycle and
    none is read here — this stream is separate by design.
    """
    return [e for e in (journal or []) if is_lifecycle_event(e)]


def _qty_after(entry, symbol: str) -> float:
    """`qty_after` off a lifecycle event, or raise.

    ⛔ AN UNREADABLE QUANTITY MUST NEVER READ AS ZERO here either. A zero prior
    turns the next observation into an OPEN, which would mint a SECOND active
    lifecycle for a symbol that already has one — two identities for one
    position, which is the exact condition this module exists to make
    impossible. Refuse the whole replay instead.
    """
    raw = entry.get("qty_after")
    if raw is None:
        raise ValueError(f"lifecycle event for {symbol} has no qty_after")
    try:
        qty = float(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"lifecycle event for {symbol} has non-numeric qty_after {raw!r}") from None
    if qty != qty or qty in (float("inf"), float("-inf")):
        raise ValueError(f"lifecycle event for {symbol} has a non-finite qty_after")
    if qty < 0:
        raise ValueError(f"lifecycle event for {symbol} has a negative qty_after ({qty})")
    return qty


def _avg_cost_after(entry):
    """`avg_cost_after` if usable, else None. Optional everywhere, so it degrades."""
    raw = entry.get("avg_cost_after")
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return None if val != val or val in (float("inf"), float("-inf")) else val


def replay(journal) -> dict:
    """Fold the lifecycle stream into {"active": {...}, "ignored": [...]}.

    `active` is {SYMBOL: {position_id, origin, opened_on, qty, avg_cost}} — at
    most ONE entry per symbol, always. An open sets it, an add/trim updates the
    quantity it carries, a close removes it. A later open after a close is a new
    entry with a new id; the closed one is never resurrected.

    `ignored` names events that could not be attributed — an add/trim/close
    quoting a position_id that is not the symbol's active lifecycle. That can
    only happen if the stream itself is inconsistent. Skipping such an event
    leaves the prior quantity slightly behind (self-healing: the next diff emits
    the aggregate move), whereas raising would wedge lifecycle recording forever
    on one bad historical line. It is REPORTED rather than swallowed.
    """
    active: dict = {}
    ignored: list = []
    for entry in lifecycle_events(journal):
        symbol = str(entry["symbol"]).strip().upper()
        position_id = str(entry["position_id"])
        kind = entry["event"]

        if kind == _lc.EVENT_OPENED:
            active[symbol] = {
                "position_id": position_id,
                "origin": entry.get("origin"),
                "opened_on": entry.get("opened_on"),
                "qty": _qty_after(entry, symbol),
                "avg_cost": _avg_cost_after(entry),
            }
            continue

        current = active.get(symbol)
        if current is None or current["position_id"] != position_id:
            ignored.append(f"{kind}:{symbol}:{position_id}")
            continue

        if kind == _lc.EVENT_CHANGED:
            current["qty"] = _qty_after(entry, symbol)
            current["avg_cost"] = _avg_cost_after(entry)
        else:                                    # EVENT_CLOSED
            del active[symbol]
    return {"active": active, "ignored": ignored}


def reconcile(journal, positions, *, observed_at: str, orders=(),
              account_number=None) -> dict:
    """The events implied by `positions` given everything the stream has recorded.

    `positions` is the VALIDATED holdings mapping the snapshot publisher is about
    to write: {SYMBOL: {"qty": float, "avg_cost": float}}.

    Returns {"mode": "adopt"|"diff", "events": [...], "ignored": [...]}. It
    appends nothing and writes nothing — persistence is the caller's job, and
    keeping it there is what lets the caller decide the failure boundary.

    ⛔ ADOPTION FIRES EXACTLY ONCE, AND ITS TRIGGER IS THE STREAM'S OWN EMPTINESS.
    An empty lifecycle stream means tracking has never begun, so every currently
    held position is a position whose opening we did not observe. Routing that
    first observation through `diff()` instead would read all of them as OPENs
    and, for any name with a single matching filled buy in the orders payload,
    mint `pos:SYM:<that order id>` — asserting that this order opened a position
    we have been holding for weeks. `adopt()` records the opening facts as
    unknown, which is the truth. After it runs the stream is no longer empty, so
    the branch can never be taken again; no flag, no marker, no state file.

    ⚠️ ONE ACCEPTED DEGRADATION. If the very first reconciliation happens to run
    on a publish that ALSO contains a brand-new buy, that new position is adopted
    alongside the rest and its observable opening order is not recorded. Adopted
    facts are null, never wrong — and this is a first-deployment-only window.
    """
    events = lifecycle_events(journal)
    if not events:
        return {"mode": "adopt", "ignored": [],
                "events": _lc.adopt(positions, adopt_ref=observed_at,
                                    observed_at=observed_at,
                                    account_number=account_number)}

    state = replay(events)
    active = state["active"]
    # Prior state and open-lifecycle identity come from the SAME fold, so they
    # can never disagree: every symbol with a positive prior quantity has, by
    # construction, an active position_id. `position_lifecycle.unattributed()`
    # is therefore empty for this caller — not by luck, but because the two
    # inputs are two views of one replay.
    prior = {sym: {"qty": st["qty"], "avg_cost": st["avg_cost"]}
             for sym, st in active.items()}
    open_lifecycles = {sym: {"position_id": st["position_id"],
                             "origin": st["origin"],
                             "opened_on": st["opened_on"]}
                       for sym, st in active.items()}
    return {"mode": "diff", "ignored": state["ignored"],
            "events": _lc.diff(prior=prior, new=positions,
                               open_lifecycles=open_lifecycles,
                               observed_at=observed_at, orders=orders,
                               account_number=account_number)}
