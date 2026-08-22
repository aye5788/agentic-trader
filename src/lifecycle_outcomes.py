"""Analytical projection over the forward-only POSITION LIFECYCLE stream — pure.

WHAT THIS IS FOR
    Steps 1-3 made three separate facts recordable: a stable `position_id`
    derived from observed position state (src/position_lifecycle.py), lifecycle
    events appended at the authoritative snapshot boundary
    (src/lifecycle_journal.py + agent_env.server._write_broker_snapshot), and an
    `agent_decision` that carries the id of the lifecycle that was open when the
    judgement was made (agent_env.decide.decision_entry).

    Those are three interleaved event kinds in one append-only file. This module
    folds them into ONE RECORD PER REAL POSITION, so a later reader can answer:

        what real position was this          -> position_id, symbol, origin
        what did the agent decide while it existed -> decisions[]
        how did it turn out                  -> the close facts, or null

    It is the substrate a learning layer would later sit on. It IS NOT that
    layer: nothing here scores, ranks, summarises, compares to a benchmark or
    reaches the agent.

⛔ THIS STREAM IS SEPARATE FROM THE LEGACY OUTCOME MACHINERY, DELIBERATELY
    The legacy `outcome` / `partial_outcome` events key off `decision_id`, which
    is `SYMBOL:<book-date>` — an identifier the slow loop remints almost nightly,
    anchored on `target_weight > 0`, which is a SELECTION marker and not a
    holding marker. Measured against the ten real closes of 2026-08-14..20 it
    attributed nothing at all for three of them and, for four more, a book built
    AFTER the sale, yielding a negative holding period. NOTHING legacy is read
    here, and no record produced here is fed back into it. Everything reused —
    `lifecycle_journal.is_lifecycle_event`, `LIFECYCLE_EVENT_NAMES`, and the
    event-name/origin/schema constants in `position_lifecycle` — belongs to this
    NEW stream and was checked for legacy identity assumptions before use:
    `is_lifecycle_event` tests only the event name, the lifecycle schema version
    and the presence of position_id/symbol, and the constants are literals. No
    legacy helper is called, and no legacy record is read.

JOINS ARE EXACT, OR THEY DO NOT HAPPEN
    Events and decisions attach to a lifecycle by EXACT `position_id` string
    equality and by nothing else. Not by symbol, not by date proximity, not by
    the book, not by `decision_id`. A decision written before this existed has no
    `position_id` and therefore appears nowhere in this projection — that is the
    forward-only contract, not a gap to be filled by inference.

MISSING IS BETTER THAN FALSE
    Every promoted field is carried VERBATIM off the event that stated it. Where
    an event left a field null — an adopted position's opening date, a close with
    no single identifiable sell — this projection reports null. It does not
    substitute a book date, a thesis date, the observation timestamp, the nearest
    order, or anything read out of prose. The two derived values it does compute
    (`qty_last_known` and the structural verdict) are folds over observed
    quantities and event structure, never estimates.

PURITY
    No I/O, no journal write, no broker call, no filesystem, no clock. The caller
    supplies the journal events; same input -> same output, including ordering.
"""
from __future__ import annotations

import lifecycle_journal as _journal
import position_lifecycle as _lc

OUTCOME_SCHEMA = 1

DECISION_EVENT = "agent_decision"

STATUS_COMPLETE = "complete"
STATUS_OPEN = "open"
STATUS_INVALID = "invalid"


# --------------------------------------------------------------------------- #
# reading one entry
# --------------------------------------------------------------------------- #
def _event_name(entry):
    return entry.get("event") if isinstance(entry, dict) else None


def _unreadable_reason(entry) -> str:
    """WHY a lifecycle-named entry could not be keyed. Never guesses a key.

    Mirrors `lifecycle_journal.is_lifecycle_event` so the two cannot drift into
    disagreeing about what is readable; that function stays the single test, and
    this one only explains its refusal.
    """
    if not isinstance(entry, dict):
        return f"entry is {type(entry).__name__}, not an object"
    schema = entry.get("lifecycle_schema")
    if schema != _lc.LIFECYCLE_SCHEMA:
        return f"lifecycle_schema {schema!r} is not {_lc.LIFECYCLE_SCHEMA}"
    if not entry.get("position_id"):
        return "no position_id"
    if not entry.get("symbol"):
        return "no symbol"
    return "unreadable"


def _decision_position_id(entry):
    """The exact `position_id` string on an agent_decision, or None.

    ⛔ NO NORMALISATION BEYOND REJECTING BLANKS. `decide.decision_entry` already
    strips before writing and omits the key entirely when empty, so an exact
    comparison is the correct one. Upper-casing or re-deriving it here would
    invent a match the writer never made.
    """
    if not isinstance(entry, dict) or entry.get("event") != DECISION_EVENT:
        return None
    pid = entry.get("position_id")
    if not isinstance(pid, str) or not pid.strip():
        return None
    return pid


# --------------------------------------------------------------------------- #
# grouping — by exact position_id, in first-appearance order
# --------------------------------------------------------------------------- #
def _group_lifecycle_events(journal):
    """({position_id: [(index, event), ...]}, order, unreadable). Pure.

    File order is preserved inside each group and across groups, which is what
    makes the whole projection deterministic without sorting on any timestamp —
    and timestamps are exactly the fields that may be null here.
    """
    groups: dict = {}
    order: list = []
    unreadable: list = []
    for index, entry in enumerate(journal or []):
        if _event_name(entry) not in _journal.LIFECYCLE_EVENT_NAMES:
            continue                      # not lifecycle data; not this stream's business
        if not _journal.is_lifecycle_event(entry):
            unreadable.append({"index": index,
                               "event": str(_event_name(entry)),
                               "reason": _unreadable_reason(entry)})
            continue
        pid = str(entry["position_id"])
        if pid not in groups:
            groups[pid] = []
            order.append(pid)
        groups[pid].append((index, entry))
    return groups, order, unreadable


def _decisions_by_position(journal):
    """{position_id: [agent_decision copy, ...]} in file order. Exact join only."""
    out: dict = {}
    for entry in journal or []:
        pid = _decision_position_id(entry)
        if pid is None:
            continue                      # unstamped: outside this projection, by design
        out.setdefault(pid, []).append(dict(entry))
    return out


# --------------------------------------------------------------------------- #
# structural verdict
# --------------------------------------------------------------------------- #
def _structural_reasons(seq, opens, closes) -> list:
    """Ways the event stream for ONE position_id contradicts itself.

    ⛔ A NULL FACT IS NOT CORRUPTION. An adopted lifecycle has no opening order,
    no opened_at and no opened_on, and a close may have no identifiable sell.
    None of that appears here: every test below is about STRUCTURE (how many
    events of which kind, in which order) or about two events stating DIFFERENT
    values for the SAME fact. Confusing the two would mark most honest adopted
    records invalid and hide the real corruption among them.
    """
    reasons = []
    if not opens:
        reasons.append("no position_opened")
    if len(opens) > 1:
        reasons.append(f"duplicate position_opened ({len(opens)})")
    if len(closes) > 1:
        reasons.append(f"duplicate position_closed ({len(closes)})")
    if opens and _event_name(seq[0][1]) != _lc.EVENT_OPENED:
        reasons.append("event precedes position_opened")
    if closes and _event_name(seq[-1][1]) != _lc.EVENT_CLOSED:
        reasons.append("event follows position_closed")

    symbols = sorted({str(e["symbol"]).strip().upper() for _i, e in seq})
    if len(symbols) > 1:
        reasons.append("conflicting symbols " + "/".join(symbols))

    # Two events stating the same fact differently. The close copies `origin` and
    # `opened_on` off the open via the replay fold, so in a stream written only by
    # this system they agree by construction; a disagreement means one of the two
    # events did not come from that fold, and neither may then be trusted.
    if opens and closes:
        for field in ("origin", "opened_on"):
            if opens[0].get(field) != closes[0].get(field):
                reasons.append(
                    f"{field} disagrees between open ({opens[0].get(field)!r}) "
                    f"and close ({closes[0].get(field)!r})")
    return reasons


# --------------------------------------------------------------------------- #
# one record
# --------------------------------------------------------------------------- #
def _qty_last_known(seq):
    """The last OBSERVED quantity, folded forward. A fold, not an estimate.

    `position_closed` carries no quantity — it says the position is gone — so for
    a complete lifecycle this is the size the position was carrying immediately
    before it closed, and for an open one it is the current size. Null only if
    nothing in the group ever stated a quantity.
    """
    qty = None
    for _index, entry in seq:
        if _event_name(entry) in (_lc.EVENT_OPENED, _lc.EVENT_CHANGED):
            qty = entry.get("qty_after")
    return qty


def _record(position_id, seq, decisions) -> dict:
    opens = [e for _i, e in seq if _event_name(e) == _lc.EVENT_OPENED]
    changes = [dict(e) for _i, e in seq if _event_name(e) == _lc.EVENT_CHANGED]
    closes = [e for _i, e in seq if _event_name(e) == _lc.EVENT_CLOSED]

    reasons = _structural_reasons(seq, opens, closes)
    if reasons:
        status = STATUS_INVALID
    elif closes:
        status = STATUS_COMPLETE
    else:
        status = STATUS_OPEN

    # First in FILE ORDER when a kind is duplicated. The record is already
    # flagged invalid in that case; populating it anyway keeps the contradiction
    # inspectable instead of hiding it behind an empty record.
    opened = opens[0] if opens else {}
    closed = closes[0] if closes else {}

    symbol = opened.get("symbol") or (seq[0][1].get("symbol") if seq else None)

    return {
        "outcome_schema": OUTCOME_SCHEMA,
        "position_id": position_id,
        "symbol": symbol,
        "status": status,
        # [] on a healthy record; every entry names a structural contradiction.
        "invalid_reasons": reasons,

        # ---- identity / provenance -------------------------------------
        # `origin` distinguishes a lifecycle whose OPENING WE WATCHED from one we
        # merely found ourselves holding. It governs how the opening fields below
        # must be read: for "adopted" they are null as a statement of fact, and
        # `adopt_ref` records when TRACKING began — never when the position did.
        "origin": opened.get("origin"),
        "account_number": opened.get("account_number"),
        "adopt_ref": opened.get("adopt_ref"),

        # ---- opening facts, verbatim -----------------------------------
        "opened_at": opened.get("opened_at"),
        "opened_on": opened.get("opened_on"),
        "opening_order_id": opened.get("opening_order_id"),
        "opening_quantity": opened.get("opening_quantity"),
        "opening_price": opened.get("opening_price"),
        # RENAMED, NOT ALIASED. On the event this is `avg_cost_after`, which is
        # unambiguous there and ambiguous at lifecycle scope; the name chosen
        # mirrors the close event's own `avg_cost_at_close`. The event's spelling
        # does not also appear in this record.
        "avg_cost_at_open": opened.get("avg_cost_after"),
        "qty_at_open": opened.get("qty_after"),
        # Renamed for the same reason: there are two `observed_at` in one record.
        # ⚠️ This is when WE SAW the state, an upper bound on when it happened.
        "observed_at_open": opened.get("observed_at"),

        # ---- closing facts, verbatim -----------------------------------
        "closing_order_id": closed.get("closing_order_id"),
        "closed_at": closed.get("closed_at"),
        "closed_on": closed.get("closed_on"),
        "exit_price": closed.get("exit_price"),
        "avg_cost_at_close": closed.get("avg_cost_at_close"),
        # Both computed at close time by position_lifecycle and CARRIED, never
        # recomputed here: realized_pnl_pct only when exit_price and a positive
        # avg_cost_at_close both exist; holding_days only when opened_on and
        # closed_on are both real observed dates (so: null for every adopted
        # lifecycle, and null for a close with no identifiable sell).
        "realized_pnl_pct": closed.get("realized_pnl_pct"),
        "holding_days": closed.get("holding_days"),
        # Never inferred from the existence of a sell; null unless a trustworthy
        # source (the monitor's own exit request) supplied it.
        "exit_reason": closed.get("exit_reason"),
        "observed_at_close": closed.get("observed_at"),

        # ---- life ------------------------------------------------------
        "qty_last_known": _qty_last_known(seq),
        "event_count": len(changes),
        "events": changes,
        "decision_count": len(decisions),
        "decisions": decisions,
    }


# --------------------------------------------------------------------------- #
# the projection
# --------------------------------------------------------------------------- #
def project(journal) -> dict:
    """Fold a journal into {"lifecycles", "unreadable", "orphan_decisions"}. Pure.

    `lifecycles` is one record per distinct `position_id`, in the order each id
    first appears in the file — deterministic without relying on any timestamp,
    which matters because timestamps here are allowed to be null.

    `unreadable` names lifecycle-NAMED entries that could not be keyed at all
    (wrong schema version, no position_id, no symbol). They are reported rather
    than dropped: a projection that silently discards events it does not
    understand reads as complete when it is not.

    `orphan_decisions` are stamped decisions whose `position_id` matches no
    lifecycle in this stream. Also reported rather than force-attached — the
    exact join is the whole guarantee, and a near-match is not a match.
    """
    groups, order, unreadable = _group_lifecycle_events(journal)
    by_position = _decisions_by_position(journal)

    lifecycles = [_record(pid, groups[pid], by_position.get(pid, []))
                  for pid in order]

    orphans = [dict(d) for pid, decisions in by_position.items()
               if pid not in groups for d in decisions]

    return {"outcome_schema": OUTCOME_SCHEMA,
            "lifecycles": lifecycles,
            "unreadable": unreadable,
            "orphan_decisions": orphans}


def lifecycles(journal) -> list:
    """Every lifecycle record, any status. Thin reader over `project()`."""
    return project(journal)["lifecycles"]


def completed(journal) -> list:
    """Only structurally COMPLETE lifecycles — one open, one close, no contradiction.

    ⛔ COMPLETE IS A STATEMENT ABOUT STRUCTURE, NOT ABOUT KNOWLEDGE. A complete
    record may still carry a null exit_price, a null realized_pnl_pct and a null
    holding_days; an adopted position closed without an identifiable sell is
    complete and almost entirely unknown. Any consumer computing statistics must
    filter on the FIELDS IT NEEDS being non-null, and must not read this list as
    "records with outcomes".
    """
    return [r for r in project(journal)["lifecycles"] if r["status"] == STATUS_COMPLETE]


# --------------------------------------------------------------------------- #
# selftest — the JOIN and the STRUCTURAL VERDICT, nothing about production
# --------------------------------------------------------------------------- #
def _selftest() -> None:
    """⛔ THIS PROVES THE PROJECTION, NOT THE RECORDING. It says nothing about
    whether the live stream is being written correctly, and passing it is not
    evidence that any real position was captured. The fixtures below are shaped
    exactly as `position_lifecycle` emits, but they are still fixtures."""
    PID = "pos:AMD:6a85c11a-0ff2-4f37-b525-8ac2820aaaaa"
    PID2 = "pos:AMD:bf3a166f-5337-4e8d-0a7a-3f17375fffff"
    ADOPTED = "pos:MRK:adopted:2026-08-22T13:30:00+00:00"

    def opened(pid=PID, sym="AMD", **kw):
        e = {"event": _lc.EVENT_OPENED, "lifecycle_schema": 1, "position_id": pid,
             "symbol": sym, "origin": _lc.ORIGIN_OBSERVED, "account_number": "A1",
             "observed_at": "2026-08-25T13:30:00+00:00",
             "opening_order_id": "6a85c11a", "opened_at": "2026-08-25T13:29:00Z",
             "opened_on": "2026-08-25", "opening_quantity": 0.006391,
             "opening_price": 469.3847, "qty_after": 0.006391,
             "avg_cost_after": 469.21}
        e.update(kw)
        return e

    def changed(pid=PID, sym="AMD", kind="trim", **kw):
        e = {"event": _lc.EVENT_CHANGED, "lifecycle_schema": 1, "position_id": pid,
             "symbol": sym, "kind": kind, "observed_at": "2026-08-27T13:30:00+00:00",
             "order_id": None, "quantity": None, "price": None,
             "qty_before": 0.006391, "qty_after": 0.005, "avg_cost_after": 469.21}
        e.update(kw)
        return e

    def closed(pid=PID, sym="AMD", **kw):
        e = {"event": _lc.EVENT_CLOSED, "lifecycle_schema": 1, "position_id": pid,
             "symbol": sym, "origin": _lc.ORIGIN_OBSERVED,
             "observed_at": "2026-08-28T13:30:00+00:00",
             "closing_order_id": "9d18f44d", "closed_at": "2026-08-28T13:29:00Z",
             "closed_on": "2026-08-28", "exit_price": 495.3,
             "avg_cost_at_close": 470.1, "realized_pnl_pct": 0.053606,
             "opened_on": "2026-08-25", "holding_days": 3, "exit_reason": None}
        e.update(kw)
        return e

    def decision(pid, action, ts, sym="AMD"):
        d = {"event": DECISION_EVENT, "ts": ts, "symbol": sym, "action": action,
             "reason": "because"}
        if pid:
            d["position_id"] = pid
        return d

    # 1. a complete observed lifecycle carries the close's own numbers, verbatim
    r = project([opened(), changed(kind="add", qty_after=0.011466),
                 changed(), closed()])["lifecycles"][0]
    assert r["status"] == STATUS_COMPLETE and r["invalid_reasons"] == []
    assert r["origin"] == _lc.ORIGIN_OBSERVED
    assert (r["realized_pnl_pct"], r["holding_days"]) == (0.053606, 3)
    assert r["avg_cost_at_open"] == 469.21 and r["avg_cost_at_close"] == 470.1, r
    assert [e["kind"] for e in r["events"]] == ["add", "trim"]
    assert r["qty_last_known"] == 0.005, "the size carried into the close"

    # 2. an ADOPTED lifecycle is COMPLETE while knowing almost nothing. Null
    #    historical facts are not corruption; asserting the status here is the
    #    point, because treating them as corruption would discard every position
    #    held before tracking began.
    r = project([opened(ADOPTED, "MRK", origin=_lc.ORIGIN_ADOPTED,
                        adopt_ref="2026-08-22T13:30:00+00:00",
                        opening_order_id=None, opened_at=None, opened_on=None,
                        opening_quantity=None, opening_price=None),
                 closed(ADOPTED, "MRK", origin=_lc.ORIGIN_ADOPTED,
                        closing_order_id=None, closed_at=None, closed_on=None,
                        exit_price=None, realized_pnl_pct=None, opened_on=None,
                        holding_days=None)])["lifecycles"][0]
    assert r["status"] == STATUS_COMPLETE, "unknown history is not invalidity"
    assert r["adopt_ref"] == "2026-08-22T13:30:00+00:00"
    assert all(r[f] is None for f in
               ("opened_at", "opened_on", "opening_order_id", "opening_quantity",
                "opening_price", "closed_at", "closed_on", "closing_order_id",
                "exit_price", "realized_pnl_pct", "holding_days")), r
    # ⛔ and the adoption reference is NOT smuggled into an opening date
    assert r["opened_at"] is None and r["opened_on"] is None

    # 3. no close = OPEN, not complete and not invalid
    assert project([opened()])["lifecycles"][0]["status"] == STATUS_OPEN

    # 4. THE JOIN IS EXACT. Two lifecycles for one symbol keep their own
    #    decisions; a decision stamped with the other id never crosses over, and
    #    an unstamped one is absent from the projection entirely.
    out = project([opened(), decision(PID, "add", "2026-08-26T00:00:00+00:00"),
                   closed(), opened(PID2, opened_on="2026-09-02"),
                   decision(PID2, "open", "2026-09-02T00:00:00+00:00"),
                   decision(None, "skip", "2026-09-03T00:00:00+00:00"),
                   decision("pos:AMD:nosuch", "hold", "2026-09-03T00:00:00+00:00")])
    first, second = out["lifecycles"]
    assert [d["action"] for d in first["decisions"]] == ["add"], first
    assert [d["action"] for d in second["decisions"]] == ["open"], second
    assert first["position_id"] != second["position_id"]
    assert not any(d["action"] == "skip"
                   for r in out["lifecycles"] for d in r["decisions"]), \
        "an unstamped decision must not be attached by symbol"
    assert len(out["orphan_decisions"]) == 1, "a near-match is reported, not joined"

    # 5. structural contradictions, each named rather than repaired
    for journal, needle in (
            ([opened(), opened(), closed()], "duplicate position_opened"),
            ([opened(), closed(), closed()], "duplicate position_closed"),
            ([changed(pid="pos:AMD:ghost")], "no position_opened"),
            ([opened(), closed(), changed()], "event follows position_closed"),
            ([changed(), opened(), closed()], "event precedes position_opened"),
            ([opened(), changed(sym="MU"), closed()], "conflicting symbols"),
            ([opened(), closed(opened_on="2026-08-01")], "opened_on disagrees"),
            ([opened(), closed(origin=_lc.ORIGIN_ADOPTED)], "origin disagrees")):
        r = project(journal)["lifecycles"][0]
        assert r["status"] == STATUS_INVALID, (needle, r)
        assert any(needle in why for why in r["invalid_reasons"]), (needle, r)
        assert completed(journal) == [], needle
    # ⛔ AND THE OPEN IS THE PRIMARY SOURCE of a fact both events state. On a
    # healthy record the two agree, so nothing else can tell the sources apart;
    # only the contradicting stream can, which is why this sits here.
    r = project([opened(), closed(opened_on="2026-08-01")])["lifecycles"][0]
    assert r["opened_on"] == "2026-08-25", ("promoted from the close", r)
    assert r["origin"] == _lc.ORIGIN_OBSERVED

    # 6. an event we cannot key is REPORTED, never dropped and never guessed at,
    #    and it does not contaminate the lifecycle beside it
    out = project([opened(), closed(),
                   dict(opened(pid="pos:X:1"), lifecycle_schema=2),
                   {"event": _lc.EVENT_CLOSED, "lifecycle_schema": 1, "symbol": "X"},
                   "not an object",
                   {"event": "product", "as_of": "2026-08-28"}])
    assert len(out["lifecycles"]) == 1 and out["lifecycles"][0]["status"] == STATUS_COMPLETE
    assert [u["reason"] for u in out["unreadable"]] == \
        ["lifecycle_schema 2 is not 1", "no position_id"], out["unreadable"]

    # 7. empty / lifecycle-free input is empty output, never an exception
    assert project([])["lifecycles"] == [] and project(None)["lifecycles"] == []

    # 8. DETERMINISM, ordering included
    #     ⚠️ THE LAST ID SORTS FIRST. Ordering is FIRST APPEARANCE, and with an
    #     alphabetically ascending fixture a sort would be indistinguishable.
    j = [opened(), changed(), closed(), opened(PID2), opened("pos:AAA:1", "AAA")]
    assert project(j) == project(j)
    assert [r["position_id"] for r in lifecycles(j)] == [PID, PID2, "pos:AAA:1"]

    print("lifecycle_outcomes: OK — one record per position_id; decisions and "
          "events join by exact id only; unstamped decisions stay out; adopted "
          "lifecycles complete with null history; contradictions are named, not "
          "repaired; unkeyable events are reported, not dropped")


if __name__ == "__main__":
    _selftest()
