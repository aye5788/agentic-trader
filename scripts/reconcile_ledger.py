"""Reconcile the journal against Robinhood ground truth — auto-heal, then alarm.

Robinhood is the source of truth for what executed. Only the AGENT can reach RH,
so it writes an order dump to research_store/rh/orders_dump.json; this script is
the deterministic half: any FILLED order missing from journal.jsonl is appended
(source="reconcile"), then we re-verify and — if any RH fill is still unjournaled
— phone-alarm and exit non-zero. A silently-incomplete ledger becomes impossible.

Idempotent: keyed on order_id, so re-running never double-appends.

Spec: docs/superpowers/specs/2026-07-22-decision-outcome-ledger-design.md
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from research_store import store  # noqa: E402
from notify import push           # noqa: E402

ORDERS_DUMP = REPO / "research_store" / "rh" / "orders_dump.json"


def unwrap_orders(dump):
    """The order list from either accepted shape. Pure.

    prompts/exit.md step 7e says "the raw get_equity_orders response shape",
    which is {"data": {"orders": [...]}}; this script demanded a bare array.
    The two agreed only while the EXECUTOR ran this script and happened to
    write arrays. On 2026-09-04, the first day the MONITOR ran it, the
    executor wrote the raw shape as told and the reconcile refused it — a
    contract contradiction, not a broker problem. Both shapes are the same
    evidence; both are accepted.
    """
    if isinstance(dump, list):
        return [_normalize_order(o) for o in dump]
    if isinstance(dump, dict):
        inner = dump.get("data") if isinstance(dump.get("data"), dict) else dump
        orders = inner.get("orders")
        if isinstance(orders, list):
            return [_normalize_order(o) for o in orders]
    return dump


def _normalize_order(o):
    """A raw broker order in this script's field names. Pure.

    The broker calls the id `id` and the EXECUTED size `cumulative_quantity`
    (`quantity` is what was asked for). The executor's old jq reshaping did
    exactly this mapping before running the script; now the script does it,
    so a raw dump and a reshaped one are the same evidence.
    """
    if not isinstance(o, dict):
        return o
    n = dict(o)
    if not n.get("order_id") and n.get("id"):
        n["order_id"] = str(n["id"])
    if n.get("cumulative_quantity") not in (None, ""):
        n["quantity"] = n["cumulative_quantity"]
    return n


def journaled_order_ids(journal: list) -> set:
    """Every order_id already recorded in an execution event."""
    ids = set()
    for e in journal:
        if e.get("event") != "execution":
            continue
        for f in e.get("fills") or []:
            oid = f.get("order_id")
            if oid:
                ids.add(oid)
    return ids


def missing_orders(journaled: set, rh_orders: list) -> list:
    """FILLED RH orders whose order_id is not yet in the journal (deduped)."""
    out, seen = [], set()
    for o in rh_orders:
        if o.get("state") != "filled":
            continue
        oid = o.get("order_id")
        if oid and oid not in journaled and oid not in seen:
            seen.add(oid)
            out.append(o)
    return out


def unidentified_fills(rh_orders: list) -> list:
    """FILLED RH orders with no usable order_id — they cannot be idempotently
    tracked, so they must be surfaced (alarmed), never silently dropped."""
    return [o for o in rh_orders
            if o.get("state") == "filled" and not o.get("order_id")]


def heal_event(missing: list, ts: str) -> dict | None:
    """Build one execution event (source='reconcile') for the missing orders,
    or None if nothing is missing."""
    if not missing:
        return None
    fills = [{
        "symbol": o.get("symbol"),
        "side": o.get("side"),
        "order_id": o.get("order_id"),
        "avg_price": o.get("average_price"),
        "quantity": o.get("quantity"),
        "status": "filled",
    } for o in missing]
    return {"event": "execution", "source": "reconcile", "ts": ts,
            "n": len(fills), "fills": fills}


def main() -> None:
    if not ORDERS_DUMP.exists():
        # No dump written this run — nothing to reconcile against. Not an error.
        print(f"no orders dump at {ORDERS_DUMP} — skipping reconcile")
        return
    try:
        rh_orders = json.loads(ORDERS_DUMP.read_text())
    except Exception as e:
        push("Agentic: ledger reconcile FAILED",
             f"orders_dump.json unreadable: {e}", tags="rotating_light")
        sys.exit(f"malformed orders_dump.json: {e}")
    rh_orders = unwrap_orders(rh_orders)
    if not isinstance(rh_orders, list):
        push("Agentic: ledger reconcile FAILED",
             "orders_dump.json is neither an array nor a get_equity_orders response",
             tags="rotating_light")
        sys.exit("orders_dump.json must be a JSON array of order objects, or the "
                 "raw get_equity_orders response ({\"data\": {\"orders\": [...]}})")
    journal = store.read_journal()
    journaled = journaled_order_ids(journal)

    missing = missing_orders(journaled, rh_orders)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ev = heal_event(missing, ts)
    if ev:
        store.append_journal(ev)
        print(f"reconcile: healed {ev['n']} unjournaled fill(s): "
              + ", ".join(f["order_id"] for f in ev["fills"]))

    # Verify: after healing, NO filled RH order may be absent from the journal,
    # and no filled RH order may lack a usable order_id (those can never heal).
    journal = store.read_journal()
    still = missing_orders(journaled_order_ids(journal), rh_orders)
    unidentified = unidentified_fills(rh_orders)
    if still or unidentified:
        push("Agentic: LEDGER DIVERGENCE",
             f"{len(still)} filled RH order(s) unjournaled after reconcile; "
             f"{len(unidentified)} filled order(s) with no order_id",
             tags="rotating_light")
        sys.exit(f"ledger divergence: {len(still)} unjournaled + "
                 f"{len(unidentified)} unidentified filled orders")
    print("reconcile: journal complete vs RH dump")


def _selftest() -> None:
    # both accepted shapes yield the same list; anything else passes through untouched
    raw = {"data": {"orders": [{"id": "a", "state": "filled", "quantity": "1", "cumulative_quantity": "0.5"}]}}
    u = unwrap_orders(raw)
    assert u[0]["order_id"] == "a" and u[0]["quantity"] == "0.5", u   # id -> order_id, executed size
    assert unwrap_orders({"orders": [{"order_id": "b"}]})[0]["order_id"] == "b"
    assert unwrap_orders([{"order_id": "c"}]) == [{"order_id": "c"}]
    assert unwrap_orders({"x": 1}) == {"x": 1}
    assert unidentified_fills(u) == []
    jrnl = [
        {"event": "execution", "fills": [
            {"symbol": "XLE", "side": "buy", "order_id": "o1", "avg_price": 100.0}]},
        {"event": "product", "as_of": "2026-07-06"},
    ]
    assert journaled_order_ids(jrnl) == {"o1"}

    rh = [
        {"order_id": "o1", "symbol": "XLE", "side": "buy", "state": "filled",
         "quantity": 0.05, "average_price": 100.0},
        {"order_id": "o2", "symbol": "MU", "side": "sell", "state": "filled",
         "quantity": 0.01, "average_price": 190.0},
        {"order_id": "o3", "symbol": "AAPL", "side": "buy", "state": "cancelled",
         "quantity": 0.0, "average_price": None},
    ]
    miss = missing_orders({"o1"}, rh)
    assert [m["order_id"] for m in miss] == ["o2"], miss   # o1 known, o3 not filled

    ev = heal_event(miss, ts="2026-07-20T15:00:00+00:00")
    assert ev["event"] == "execution" and ev["source"] == "reconcile"
    assert [f["order_id"] for f in ev["fills"]] == ["o2"]
    assert ev["fills"][0]["side"] == "sell" and ev["fills"][0]["avg_price"] == 190.0

    # nothing missing -> no event
    assert heal_event([], ts="2026-07-20T15:00:00+00:00") is None
    # re-run idempotency: once o2 is journaled, it is no longer missing
    assert missing_orders({"o1", "o2"}, rh) == []

    # unidentified_fills: filled + no order_id (None or "") must be surfaced;
    # filled-with-id and non-filled must be ignored
    unid_cases = [
        {"state": "filled", "order_id": None, "symbol": "ZZ", "side": "buy"},
        {"state": "filled", "order_id": "ok1", "symbol": "YY", "side": "buy"},
        {"state": "cancelled", "order_id": None, "symbol": "XX", "side": "buy"},
        {"state": "filled", "order_id": "", "symbol": "WW", "side": "sell"},
    ]
    unid = unidentified_fills(unid_cases)
    assert unid == [unid_cases[0], unid_cases[3]], unid

    # missing_orders dedupes a duplicated filled order_id within one dump
    dup_dump = [
        {"order_id": "dup1", "symbol": "AA", "side": "buy", "state": "filled",
         "quantity": 0.01, "average_price": 50.0},
        {"order_id": "dup1", "symbol": "AA", "side": "buy", "state": "filled",
         "quantity": 0.01, "average_price": 50.0},
    ]
    dup_miss = missing_orders(set(), dup_dump)
    assert [m["order_id"] for m in dup_miss] == ["dup1"], dup_miss

    print("selftest OK: journaled_order_ids, missing_orders (dedup), heal_event "
          "(idempotent), unidentified_fills")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
