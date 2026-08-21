"""Forward-only POSITION LIFECYCLE state machine — pure, wired to nothing.

WHAT THIS IS FOR
    A position needs an identity that survives everything the book does to it.
    The existing `decision_id` is `SYMBOL:<book-date>` and cannot serve: the slow
    loop rewrites `current.json` almost nightly (34 archive stamps between
    2026-07-07 and 2026-08-20), so one held position accumulates a new
    "identity" every night, and the anchoring that reads it selects on
    `target_weight > 0` -- a SELECTION marker, not a holding marker. Measured
    against the ten real closes of 2026-08-14..20, that anchoring returned
    nothing at all for three of them (MRK, RTX, BAC -- all protective theses,
    `target_weight` 0.0) and, for four more, a book built AFTER the sale, which
    yields a NEGATIVE holding period.

    So this module introduces one new explicit identifier, `position_id`, and
    derives it from OBSERVED POSITION STATE only. It depends on no part of the
    book: not `target_weight`, not `rank`, not `as_of`, not `verdict`, not
    `thesis`, not `decision_id`.

    ⛔ THIS IS A SEPARATE STREAM. Legacy `outcome` / `partial_outcome` events and
    `decision_id` are untouched and keep their consumers. Nothing here is read by
    anything yet -- that wiring is a later, separate, controlled step.

THE STATE MACHINE, over an authoritative prior -> new snapshot pair

    absent/0 -> >0            OPEN   (position_opened)
    >0       -> same          --     (nothing)
    >0       -> larger        ADD    (position_event, kind="add")
    >0       -> smaller >0    TRIM   (position_event, kind="trim")
    >0       -> absent/0      CLOSE  (position_closed)

    A reduction that REACHES ZERO is a CLOSE, never a trim. A later 0 -> >0 is a
    NEW lifecycle with a NEW id; a closed lifecycle is never resurrected.

TRUTHFULNESS IS THE POINT, NOT A STYLE CHOICE
    Missing data is preferable to false data. Every order-derived field is left
    None unless exactly ONE filled order in the supplied payload can be
    unambiguously associated with the transition. We do not take "the most
    recent" or "the earliest" buy: `get_equity_orders` carries no completeness
    proof, so ordering within it is not evidence. `holding_days` is emitted ONLY
    when `opened_on` is a real observed date -- for an adopted position it is
    None, permanently, rather than a number derived from when we started
    watching. `exit_reason` is never inferred from the existence of a sell.

PURITY
    No I/O, no journal, no broker, no filesystem, no environment, no clock.
    Every timestamp is supplied by the caller. Same inputs -> same outputs.
"""
from __future__ import annotations

import datetime as _dt

LIFECYCLE_SCHEMA = 1

ORIGIN_OBSERVED = "observed"
ORIGIN_ADOPTED = "adopted"

EVENT_OPENED = "position_opened"
EVENT_CHANGED = "position_event"
EVENT_CLOSED = "position_closed"


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #
def observed_position_id(symbol: str, opening_order_id: str) -> str:
    """`pos:SYM:<opening_order_id>` — three colon-separated parts.

    The broker's order id is the only stable, externally-issued, globally unique
    handle available at the moment a position is first observed. Deriving the id
    from it means a re-run recomputes the SAME id without allocating any state.
    """
    return f"pos:{str(symbol).strip().upper()}:{opening_order_id}"


def adopted_position_id(symbol: str, adopt_ref: str) -> str:
    """`pos:SYM:adopted:<adopt_ref>` — FOUR parts, and a literal `adopted` token.

    ⛔ `adopt_ref` IS NOT AN OPEN TIME. It marks when tracking BEGAN for a
    position whose opening was never observed, which is a fact we do have. It is
    never written into opened_at/opened_on, because those describe the position
    and this describes us.

    The shape cannot collide with an observed id: an order id is a UUID and can
    never be the literal `adopted`.

    ⚠️ DISCRIMINATE BY THE `:adopted:` PREFIX, NEVER BY SPLITTING ON ":".
    `adopt_ref` is normally an ISO timestamp, which contains colons of its own,
    so `id.count(":")` is not a valid test. The ref is stored VERBATIM rather
    than reformatted so the id stays traceable to the observation that made it.
    """
    return f"pos:{str(symbol).strip().upper()}:{ORIGIN_ADOPTED}:{adopt_ref}"


# --------------------------------------------------------------------------- #
# input normalisation — REJECT, never coerce
# --------------------------------------------------------------------------- #
def _quantity(row, symbol: str, side: str) -> float:
    """Quantity for one snapshot row, or raise.

    ⛔ AN UNREADABLE ROW MUST NEVER READ AS ZERO. Zero means CLOSED here, so
    coercing an unparseable row to 0.0 would emit a fabricated close for a
    position that is still held -- the same shape as the truncated-snapshot
    failure `_write_broker_snapshot` was rewritten to refuse in 2026-08-16. The
    legacy `{"SYM": <cost dollars>}` snapshot schema noted in src/marks.py lands
    here too, and must raise rather than be guessed at.
    """
    if not isinstance(row, dict):
        raise ValueError(
            f"{side} positions[{symbol}] is {type(row).__name__}, not an object — "
            f"refusing to infer a quantity (an unreadable row must not read as a close)")
    raw = row.get("qty", row.get("quantity"))
    if raw is None:
        raise ValueError(f"{side} positions[{symbol}] has no qty")
    try:
        qty = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{side} positions[{symbol}] qty {raw!r} is not a number") from None
    if qty != qty or qty in (float("inf"), float("-inf")):
        raise ValueError(f"{side} positions[{symbol}] qty is not finite")
    if qty < 0:
        raise ValueError(f"{side} positions[{symbol}] qty is negative ({qty})")
    return qty


def _avg_cost(row):
    """Average cost if present and finite, else None. Optional, so it degrades."""
    if not isinstance(row, dict):
        return None
    raw = row.get("avg_cost", row.get("average_buy_price"))
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return None if val != val or val in (float("inf"), float("-inf")) else val


def _normalise(positions, side: str) -> dict:
    """{SYMBOL: (qty, avg_cost)} from a snapshot's `positions` mapping.

    An explicit qty of 0 is KEPT here, not dropped: the caller's snapshot writer
    discards zero rows (`if qty == 0: continue`), but the broker does emit them,
    and for this module zero and absent must mean the same thing — closed.
    """
    if positions is None:
        return {}
    if not isinstance(positions, dict):
        raise ValueError(f"{side} positions is {type(positions).__name__}, not an object")
    out = {}
    for sym, row in positions.items():
        symbol = str(sym).strip().upper()
        if not symbol:
            raise ValueError(f"{side} positions has an empty symbol key")
        if symbol in out:
            raise ValueError(f"{side} positions has duplicate symbol {symbol}")
        out[symbol] = (_quantity(row, symbol, side), _avg_cost(row))
    return out


# --------------------------------------------------------------------------- #
# order association — exactly one candidate, or nothing
# --------------------------------------------------------------------------- #
def _sole_filled_order(orders, symbol: str, side: str):
    """The ONE filled `side` order for `symbol`, or None if 0 or >1 exist.

    ⛔ NOT "the most recent" AND NOT "the earliest". The orders payload carries
    no completeness proof, so its ordering is not evidence about which order
    opened or closed a position. Two candidates means we do not know, and the
    truthfulness rule says the field stays None. The EVENT is still emitted —
    it is driven by observed state, never by order availability.
    """
    if not orders:
        return None
    found = []
    for o in orders:
        if not isinstance(o, dict):
            continue
        if str(o.get("state")) != "filled":
            continue
        if str(o.get("symbol") or "").strip().upper() != symbol:
            continue
        if str(o.get("side") or "").strip().lower() != side:
            continue
        found.append(o)
    return found[0] if len(found) == 1 else None


def _order_id(order):
    if not isinstance(order, dict):
        return None
    oid = order.get("id", order.get("order_id"))
    return str(oid) if oid else None


def _order_qty(order):
    if not isinstance(order, dict):
        return None
    raw = order.get("cumulative_quantity", order.get("quantity"))
    try:
        return None if raw is None else float(raw)
    except (TypeError, ValueError):
        return None


def _order_price(order):
    if not isinstance(order, dict):
        return None
    raw = order.get("average_price", order.get("price"))
    try:
        return None if raw is None else float(raw)
    except (TypeError, ValueError):
        return None


def _order_at(order):
    if not isinstance(order, dict):
        return None
    at = order.get("created_at")
    return str(at) if at else None


def _on(timestamp):
    """ISO date part of a timestamp, or None. Never raises: this is enrichment."""
    if not timestamp:
        return None
    text = str(timestamp)[:10]
    try:
        _dt.date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _days_between(opened_on, closed_on):
    """Whole days, or None when either end is not a real observed date."""
    if not opened_on or not closed_on:
        return None
    try:
        return (_dt.date.fromisoformat(closed_on) - _dt.date.fromisoformat(opened_on)).days
    except ValueError:
        return None


def _pnl_pct(entry, exit_price):
    if entry is None or exit_price is None:
        return None
    try:
        entry, exit_price = float(entry), float(exit_price)
    except (TypeError, ValueError):
        return None
    if entry <= 0:
        return None
    return round((exit_price - entry) / entry, 6)


# --------------------------------------------------------------------------- #
# adoption
# --------------------------------------------------------------------------- #
def adopt(positions, *, adopt_ref: str, observed_at: str,
          account_number: str | None = None) -> list:
    """Adopt every currently-held position into a lifecycle. DELIBERATE, one-off.

    A SEPARATE FUNCTION ON PURPOSE. If adoption were a branch inside `diff()`,
    then a first run with an empty `prior` would read every held position as an
    OPEN and mint observed ids from whatever orders happened to be in the
    payload. Making it explicit means adoption can never fire by accident.

    Historical opening facts are recorded as UNKNOWN, not reconstructed:
    opening_order_id / opened_at / opened_on are all None, and stay None, so
    every later close of an adopted position reports holding_days=None rather
    than a number measured from when we started looking.
    """
    if not adopt_ref:
        raise ValueError("adopt_ref is required — an adopted id must be stable")
    if not observed_at:
        raise ValueError("observed_at is required — this module never reads a clock")
    out = []
    for symbol, (qty, avg_cost) in sorted(_normalise(positions, "adopted").items()):
        if qty <= 0:
            continue                     # nothing to adopt; not an error
        out.append({
            "event": EVENT_OPENED,
            "lifecycle_schema": LIFECYCLE_SCHEMA,
            "position_id": adopted_position_id(symbol, adopt_ref),
            "symbol": symbol,
            "origin": ORIGIN_ADOPTED,
            "account_number": account_number,
            "observed_at": observed_at,
            "adopt_ref": adopt_ref,
            "qty_after": qty,
            "avg_cost_after": avg_cost,
            # UNKNOWN, and permanently so. Not reconstructed.
            "opening_order_id": None,
            "opened_at": None,
            "opened_on": None,
            "opening_quantity": None,
            "opening_price": None,
        })
    return out


# --------------------------------------------------------------------------- #
# the diff
# --------------------------------------------------------------------------- #
def unattributed(prior, open_lifecycles) -> list:
    """Held symbols carrying NO open lifecycle — the caller's gap to surface.

    `diff()` emits nothing for these because it cannot say which lifecycle an
    add/trim/close belongs to, and inventing one would be exactly the fabrication
    this module exists to avoid. Silence is the right behaviour for a pure
    function and the WRONG behaviour for the system, so the condition is exposed
    here rather than swallowed.
    """
    lifecycles = open_lifecycles or {}
    return sorted(sym for sym, (qty, _c) in _normalise(prior, "prior").items()
                  if qty > 0 and sym not in lifecycles)


def diff(*, prior, new, open_lifecycles, observed_at: str, orders=(),
         account_number: str | None = None, exit_reasons=None) -> list:
    """Lifecycle events implied by prior -> new. PURE; deterministic; no clock.

    `open_lifecycles` is {SYMBOL: {"position_id", "origin", "opened_on"}} — the
    caller's record of which lifecycle is currently open per symbol. It is what
    makes an id SURVIVE: an add, a trim and a close all reuse the id found here,
    so a nightly book rebuild, a rank change or a protective-thesis transition
    cannot alter a position's identity.

    `exit_reasons` is {SYMBOL: reason} supplied by a trustworthy source (the
    monitor's own exit request). Absent -> None. Never inferred from a sell.
    """
    if not observed_at:
        raise ValueError("observed_at is required — this module never reads a clock")

    before = _normalise(prior, "prior")
    after = _normalise(new, "new")
    lifecycles = open_lifecycles or {}
    reasons = exit_reasons or {}

    events = []
    for symbol in sorted(set(before) | set(after)):
        prior_qty, prior_cost = before.get(symbol, (0.0, None))
        new_qty, new_cost = after.get(symbol, (0.0, None))

        # ---- OPEN: absent/0 -> >0 ---------------------------------------
        if prior_qty <= 0 < new_qty:
            events.append(_opened(symbol, new_qty, new_cost, orders,
                                  observed_at, account_number))
            continue

        # Every remaining branch acts on an EXISTING lifecycle, so it needs the
        # id. Without one we cannot attribute the event; see unattributed().
        active = lifecycles.get(symbol) or {}
        position_id = active.get("position_id")

        # ---- CLOSE: >0 -> absent/0 --------------------------------------
        if prior_qty > 0 >= new_qty:
            if not position_id:
                continue
            events.append(_closed(symbol, position_id, active, prior_cost, orders,
                                  observed_at, reasons.get(symbol)))
            continue

        # ---- ADD / TRIM: >0 -> >0 ---------------------------------------
        if prior_qty > 0 and new_qty > 0 and new_qty != prior_qty:
            if not position_id:
                continue
            kind = "add" if new_qty > prior_qty else "trim"
            events.append(_changed(symbol, position_id, kind, prior_qty, new_qty,
                                   new_cost, orders, observed_at))
            continue

        # ---- NO CHANGE (including 0 -> 0) -> nothing --------------------

    return events


def _opened(symbol, new_qty, new_cost, orders, observed_at, account_number) -> dict:
    """An observed open, or an ADOPTED one when no single order identifies it.

    ⛔ AN OBSERVED ID REQUIRES AN OBSERVED OPENING ORDER. With zero or several
    candidate buys we do not know which order opened the position, so we must not
    mint `pos:SYM:<some order id>` — that id would assert a fact we do not have.
    The position is real either way, so it becomes an ADOPTED lifecycle with its
    opening facts recorded as unknown. This is the manual/external-open case the
    design accepts, arriving through the same door.
    """
    order = _sole_filled_order(orders, symbol, "buy")
    opening_order_id = _order_id(order)

    if opening_order_id:
        opened_at = _order_at(order)
        return {
            "event": EVENT_OPENED,
            "lifecycle_schema": LIFECYCLE_SCHEMA,
            "position_id": observed_position_id(symbol, opening_order_id),
            "symbol": symbol,
            "origin": ORIGIN_OBSERVED,
            "account_number": account_number,
            "observed_at": observed_at,
            "opening_order_id": opening_order_id,
            "opened_at": opened_at,
            "opened_on": _on(opened_at),
            "opening_quantity": _order_qty(order),
            "opening_price": _order_price(order),
            "qty_after": new_qty,
            "avg_cost_after": new_cost,
        }

    return {
        "event": EVENT_OPENED,
        "lifecycle_schema": LIFECYCLE_SCHEMA,
        "position_id": adopted_position_id(symbol, observed_at),
        "symbol": symbol,
        "origin": ORIGIN_ADOPTED,
        "account_number": account_number,
        "observed_at": observed_at,
        "adopt_ref": observed_at,
        "opening_order_id": None,
        "opened_at": None,
        "opened_on": None,
        "opening_quantity": None,
        "opening_price": None,
        "qty_after": new_qty,
        "avg_cost_after": new_cost,
    }


def _changed(symbol, position_id, kind, prior_qty, new_qty, new_cost, orders,
             observed_at) -> dict:
    side = "buy" if kind == "add" else "sell"
    order = _sole_filled_order(orders, symbol, side)
    return {
        "event": EVENT_CHANGED,
        "lifecycle_schema": LIFECYCLE_SCHEMA,
        "position_id": position_id,
        "symbol": symbol,
        "kind": kind,
        "observed_at": observed_at,
        "order_id": _order_id(order),
        "quantity": _order_qty(order),
        "price": _order_price(order),
        "qty_before": prior_qty,
        "qty_after": new_qty,
        "avg_cost_after": new_cost,
    }


def _closed(symbol, position_id, active, prior_cost, orders, observed_at,
            exit_reason) -> dict:
    """A close. Driven by STATE — an unidentifiable order never blocks it."""
    order = _sole_filled_order(orders, symbol, "sell")
    closed_at = _order_at(order)
    # ⛔ NO FALLBACK TO observed_at. An earlier draft used the observation date
    # when no order identified the close, which is an UPPER BOUND on the close
    # date, not the close date — a sell that filled yesterday and is first seen
    # today would be stamped today, and holding_days computed from it would be a
    # confident wrong number. `observed_at` is on this event already, so a
    # consumer can still see "closed by then" without this field claiming to be
    # something it is not.
    closed_on = _on(closed_at)
    exit_price = _order_price(order)
    opened_on = active.get("opened_on")
    return {
        "event": EVENT_CLOSED,
        "lifecycle_schema": LIFECYCLE_SCHEMA,
        "position_id": position_id,
        "symbol": symbol,
        "origin": active.get("origin"),
        "observed_at": observed_at,
        "closing_order_id": _order_id(order),
        "closed_at": closed_at,
        "closed_on": closed_on,
        "exit_price": exit_price,
        "avg_cost_at_close": prior_cost,
        "realized_pnl_pct": _pnl_pct(prior_cost, exit_price),
        # ⛔ ONLY when opened_on is a real observed date. An adopted lifecycle
        # reports None here forever rather than measuring from adoption.
        "opened_on": opened_on,
        "holding_days": _days_between(opened_on, closed_on),
        # Never inferred from the existence of a sell.
        "exit_reason": exit_reason,
    }


# --------------------------------------------------------------------------- #
# selftest — the NEW contract this module creates, nothing about production
# --------------------------------------------------------------------------- #
def _selftest() -> None:
    TS = "2026-08-21T19:19:29+00:00"
    OID_BUY = "6a85c11a-0ff2-4f37-b525-8ac2820aaaaa"
    OID_SELL = "6a8722dd-96d7-4f01-a1f8-f1799e8bbbbb"

    def pos(**kw):
        return {s: {"qty": q, "avg_cost": c} for s, (q, c) in kw.items()}

    buy = {"id": OID_BUY, "state": "filled", "side": "buy", "symbol": "AMD",
           "cumulative_quantity": "0.006391", "average_price": "469.3847",
           "created_at": "2026-08-21T19:36:02Z"}
    sell = {"id": OID_SELL, "state": "filled", "side": "sell", "symbol": "AMD",
            "cumulative_quantity": "0.011466", "average_price": "480.4101",
            "created_at": "2026-08-25T15:06:00Z"}

    # 1. 0 -> positive = OPEN, observed id from the sole buy
    ev = diff(prior={}, new=pos(AMD=(0.006391, 469.21)), open_lifecycles={},
              observed_at=TS, orders=[buy])
    assert len(ev) == 1 and ev[0]["event"] == EVENT_OPENED, ev
    assert ev[0]["position_id"] == f"pos:AMD:{OID_BUY}", ev[0]
    assert ev[0]["origin"] == ORIGIN_OBSERVED
    assert ev[0]["opened_on"] == "2026-08-21", ev[0]
    assert ev[0]["lifecycle_schema"] == 1
    OPEN_ID = ev[0]["position_id"]
    live = {"AMD": {"position_id": OPEN_ID, "origin": ORIGIN_OBSERVED,
                    "opened_on": "2026-08-21"}}

    # 2. unchanged positive = NOTHING (identical qty)
    assert diff(prior=pos(AMD=(0.006391, 469.21)), new=pos(AMD=(0.006391, 469.21)),
                open_lifecycles=live, observed_at=TS, orders=[buy]) == []

    # 3. positive -> larger = ADD, same id
    ev = diff(prior=pos(AMD=(0.006391, 469.21)), new=pos(AMD=(0.011466, 469.21)),
              open_lifecycles=live, observed_at=TS, orders=[buy])
    assert len(ev) == 1 and ev[0]["event"] == EVENT_CHANGED and ev[0]["kind"] == "add", ev
    assert ev[0]["position_id"] == OPEN_ID, "an add must not mint a new identity"

    # 4. positive -> smaller but >0 = TRIM, same id
    ev = diff(prior=pos(AMD=(0.011466, 469.21)), new=pos(AMD=(0.005, 469.21)),
              open_lifecycles=live, observed_at=TS, orders=[sell])
    assert len(ev) == 1 and ev[0]["kind"] == "trim", ev
    assert ev[0]["position_id"] == OPEN_ID
    assert ev[0]["qty_before"] == 0.011466 and ev[0]["qty_after"] == 0.005

    # 5. positive -> EXPLICIT ZERO = CLOSE (not a trim)
    ev = diff(prior=pos(AMD=(0.011466, 469.21)), new=pos(AMD=(0.0, 469.21)),
              open_lifecycles=live, observed_at=TS, orders=[sell])
    assert len(ev) == 1 and ev[0]["event"] == EVENT_CLOSED, ev
    assert "kind" not in ev[0], "a close is not a trim and carries no kind"
    assert ev[0]["position_id"] == OPEN_ID
    assert ev[0]["closing_order_id"] == OID_SELL
    assert ev[0]["avg_cost_at_close"] == 469.21
    assert ev[0]["realized_pnl_pct"] == round((480.4101 - 469.21) / 469.21, 6), ev[0]
    assert ev[0]["holding_days"] == 4, ev[0]          # 08-21 -> 08-25
    # ⛔ AN IDENTIFIED SELL IS NOT A REASON. This close HAS a closing order, and
    # exit_reason must still be None because no trustworthy source supplied one.
    # Asserted here rather than only on the order-less close, where it would pass
    # vacuously — a mutation inferring "stop" from the order's existence survived
    # until this assertion sat next to an actual order.
    assert ev[0]["closing_order_id"] is not None and ev[0]["exit_reason"] is None, ev[0]

    # 6. positive -> ABSENT = CLOSE, identical treatment to explicit zero
    ev_absent = diff(prior=pos(AMD=(0.011466, 469.21)), new={},
                     open_lifecycles=live, observed_at=TS, orders=[sell])
    assert len(ev_absent) == 1 and ev_absent[0]["event"] == EVENT_CLOSED
    assert ev_absent[0]["position_id"] == OPEN_ID
    assert ev_absent[0]["realized_pnl_pct"] == ev[0]["realized_pnl_pct"]

    # 7. close -> later reopen = NEW lifecycle, NEW id, old one not resurrected
    buy2 = dict(buy, id="9c11ffff-0000-4aaa-bbbb-ccccccccdddd",
                created_at="2026-08-26T14:00:00Z")
    ev = diff(prior=pos(AMD=(0.0, 469.21)), new=pos(AMD=(0.004, 500.0)),
              open_lifecycles={}, observed_at=TS, orders=[buy2])
    assert ev[0]["event"] == EVENT_OPENED
    assert ev[0]["position_id"] != OPEN_ID, "a reopen must never reuse the closed id"
    assert ev[0]["position_id"].endswith("9c11ffff-0000-4aaa-bbbb-ccccccccdddd")
    # ⛔ AND IT MUST STILL OPEN IF A STALE ENTRY LINGERS. The caller is supposed
    # to drop a lifecycle once closed, but if it does not, a 0 -> >0 transition
    # must still mint a NEW identity rather than silently emitting nothing or
    # resurrecting the dead one. The open branch never consults open_lifecycles;
    # this proves it, and a mutation that made the branch conditional on their
    # absence survived until this case existed.
    stale = {"AMD": {"position_id": OPEN_ID, "origin": ORIGIN_OBSERVED,
                     "opened_on": "2026-08-21"}}
    ev = diff(prior=pos(AMD=(0.0, 469.21)), new=pos(AMD=(0.004, 500.0)),
              open_lifecycles=stale, observed_at=TS, orders=[buy2])
    assert len(ev) == 1 and ev[0]["event"] == EVENT_OPENED, ev
    assert ev[0]["position_id"] != OPEN_ID, "a stale entry must not be resurrected"

    # 8. adoption of an already-held position: unknown opening facts stay unknown
    ad = adopt(pos(XOM=(0.036598, 163.94), JNJ=(0.022219, 270.04)),
               adopt_ref=TS, observed_at=TS, account_number="948184924")
    assert [a["symbol"] for a in ad] == ["JNJ", "XOM"], ad          # deterministic order
    a = ad[1]
    assert a["origin"] == ORIGIN_ADOPTED
    assert a["position_id"] == f"pos:XOM:adopted:{TS}", a
    assert a["opening_order_id"] is None and a["opened_at"] is None
    assert a["opened_on"] is None, "an adopted open must never claim an open date"
    assert a["qty_after"] == 0.036598 and a["avg_cost_after"] == 163.94
    ADOPTED_ID = a["position_id"]
    # ⛔ DISCRIMINATE BY PREFIX, NEVER BY SPLITTING ON ":". `adopt_ref` is an ISO
    # timestamp and CONTAINS colons, so a part count is not a valid test — this
    # assertion originally counted parts and failed, which is how the property
    # was found. The real guarantee is the `:adopted:` marker after the symbol:
    # an order id is a UUID and can never produce it.
    assert ADOPTED_ID.startswith("pos:XOM:adopted:"), ADOPTED_ID
    assert ":adopted:" in ADOPTED_ID and ":adopted:" not in OPEN_ID
    assert not OPEN_ID.startswith(f"pos:AMD:{ORIGIN_ADOPTED}:")
    adopted_live = {"XOM": {"position_id": ADOPTED_ID, "origin": ORIGIN_ADOPTED,
                            "opened_on": None}}

    # 9. adopted -> trim keeps the SAME adopted id
    ev = diff(prior=pos(XOM=(0.036598, 163.94)), new=pos(XOM=(0.02, 163.94)),
              open_lifecycles=adopted_live, observed_at=TS)
    assert ev[0]["kind"] == "trim" and ev[0]["position_id"] == ADOPTED_ID, ev

    # 10. adopted -> close keeps the id AND reports holding_days as UNKNOWN
    # ⛔ THE SELL ORDER IS SUPPLIED ON PURPOSE, so `closed_on` is KNOWN and
    # `opened_on` is the only missing end. An earlier version of this case passed
    # no orders, which made holding_days None because the CLOSE date was absent —
    # it asserted the right value for the wrong reason and a mutation that
    # invented an opened_on survived it.
    xom_sell = {"id": "aaaa1111-2222-4333-8444-555566667777", "state": "filled",
                "side": "sell", "symbol": "XOM", "cumulative_quantity": "0.036598",
                "average_price": "170.00", "created_at": "2026-08-25T15:00:00Z"}
    ev = diff(prior=pos(XOM=(0.036598, 163.94)), new={},
              open_lifecycles=adopted_live, observed_at=TS, orders=[xom_sell])
    assert ev[0]["event"] == EVENT_CLOSED and ev[0]["position_id"] == ADOPTED_ID
    assert ev[0]["closed_on"] == "2026-08-25", "the close date IS known here"
    assert ev[0]["opened_on"] is None, "the OPEN date is not, and must stay unknown"
    assert ev[0]["holding_days"] is None, "adopted lifecycles must not invent a duration"
    assert ev[0]["realized_pnl_pct"] == round((170.0 - 163.94) / 163.94, 6), ev[0]
    assert ev[0]["origin"] == ORIGIN_ADOPTED

    # 11. AMBIGUOUS ORDERS -> optional fields null, event still produced
    two_buys = [buy, dict(buy, id="another-order-entirely")]
    ev = diff(prior={}, new=pos(AMD=(0.006391, 469.21)), open_lifecycles={},
              observed_at=TS, orders=two_buys)
    assert ev[0]["origin"] == ORIGIN_ADOPTED, "two candidates is not an identification"
    assert ev[0]["opening_order_id"] is None and ev[0]["opened_on"] is None
    # ...and a close with NO usable order still closes, with nulls where unknown
    ev = diff(prior=pos(AMD=(0.011466, 469.21)), new={}, open_lifecycles=live,
              observed_at=TS, orders=[])
    assert ev[0]["event"] == EVENT_CLOSED and ev[0]["position_id"] == OPEN_ID
    assert ev[0]["closing_order_id"] is None and ev[0]["exit_price"] is None
    assert ev[0]["realized_pnl_pct"] is None, "no exit price means no return, not a guess"
    # closed_on is UNKNOWN without an identifying order — observed_at is an upper
    # bound on the close date, not the close date, so it is not borrowed here.
    assert ev[0]["closed_on"] is None, "the observation date must not pose as the close date"
    assert ev[0]["holding_days"] is None
    assert ev[0]["observed_at"] == TS, "…but the observation itself is still recorded"
    # exit_reason is null unless a trustworthy source supplies it
    assert ev[0]["exit_reason"] is None
    ev = diff(prior=pos(AMD=(0.011466, 469.21)), new={}, open_lifecycles=live,
              observed_at=TS, orders=[], exit_reasons={"AMD": "stop"})
    assert ev[0]["exit_reason"] == "stop"

    # 12. multiple symbols transition INDEPENDENTLY in one diff
    multi_live = {"AMD": live["AMD"], "XOM": adopted_live["XOM"]}
    ev = diff(prior=pos(AMD=(0.011466, 469.21), XOM=(0.036598, 163.94)),
              new=pos(AMD=(0.02, 469.21), MU=(0.006, 788.86)),
              open_lifecycles=multi_live, observed_at=TS)
    kinds = {e["symbol"]: e["event"] for e in ev}
    assert kinds == {"AMD": EVENT_CHANGED, "XOM": EVENT_CLOSED, "MU": EVENT_OPENED}, ev
    assert [e["symbol"] for e in ev] == ["AMD", "MU", "XOM"], "deterministic ordering"

    # 13. explicit zero rows are ZERO on BOTH sides, never positive
    assert diff(prior=pos(AMD=(0.0, 469.21)), new=pos(AMD=(0.0, 469.21)),
                open_lifecycles=live, observed_at=TS) == []
    assert adopt(pos(AMD=(0.0, 1.0)), adopt_ref=TS, observed_at=TS) == []

    # 14. MALFORMED INPUT FAILS SAFELY — never manufactures a lifecycle fact
    for bad, why in [
        ({"AMD": 12.34}, "legacy float row (src/marks.py schema) must not read as 0"),
        ({"AMD": {"avg_cost": 1.0}}, "missing qty"),
        ({"AMD": {"qty": "abc"}}, "non-numeric qty"),
        ({"AMD": {"qty": float("nan")}}, "non-finite qty"),
        ({"AMD": {"qty": -1.0}}, "negative qty"),
        ({"": {"qty": 1.0}}, "empty symbol"),
    ]:
        try:
            diff(prior=bad, new={}, open_lifecycles=live, observed_at=TS)
            raise AssertionError(f"should have rejected: {why}")
        except ValueError:
            pass
    try:
        diff(prior={}, new={}, open_lifecycles={}, observed_at="")
        raise AssertionError("should have rejected a missing observed_at")
    except ValueError:
        pass

    # a held symbol with NO lifecycle is reported, not silently attributed
    assert unattributed(pos(AMD=(1.0, 2.0), XOM=(1.0, 2.0)), live) == ["XOM"]
    assert diff(prior=pos(XOM=(1.0, 2.0)), new={}, open_lifecycles={},
                observed_at=TS) == [], "no id means no event, never a guessed one"

    # DETERMINISM: identical inputs, identical outputs
    args = dict(prior=pos(AMD=(0.011466, 469.21)), new={}, open_lifecycles=live,
                observed_at=TS, orders=[sell])
    assert diff(**args) == diff(**args)

    print("position_lifecycle: OK — identity from observed state only; adds/trims/"
          "closes preserve the id; reopen mints a new one; adopted lifecycles never "
          "claim an open date or a duration; ambiguous orders leave fields null; "
          "unreadable rows raise rather than read as a close")


if __name__ == "__main__":
    _selftest()
