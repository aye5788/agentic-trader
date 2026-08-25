"""Append executed fills to the Research Store journal — deterministically.

The fast loop's headless agent places orders via the RH MCP, then calls THIS to
record them, instead of hand-editing journal.jsonl (which risks clobbering the
append-only log) or writing throwaway helper scripts. It reads a fixed fills
file and appends exactly one journal event via store.append_journal().

Agent contract:
  1. write research_store/rh/fills.json = a JSON array of objects, e.g.
       [{"symbol":"EEM","side":"buy","amount":3.00,"quantity":0.0421,
         "order_id":"...","status":"filled","avg_price":71.25}, ...]
     Orders the agent SKIPPED (review rejection, unsettled-cash deferral,
     re-entry veto) go in the same array with status "skipped" and a short
     "reason" — so deferred legs are journaled, not just narrated in the report.
  2. if anything EXECUTED, also write research_store/rh/broker_state.json =
       {"positions": <cursor-linked get_equity_positions pages transcript with
                      exhausted=true>,
        "portfolio": <raw get_portfolio output>,
        "account_number": "<account>", "liquidated": false}
     This script then publishes research_store/rh/positions.json through
     _write_broker_snapshot() — the SAME validated writer the session MCP uses —
     stamped with the SAME timestamp as the journal entry, so the two can never
     disagree. See publish_snapshot() for why that matters. Omit the file only
     when nothing executed; a fill journaled without it reads as stale-after-fill
     to the monitor, the health check and every valuation downstream.
  3. run:  .venv/bin/python scripts/record_fills.py

`quantity` is REQUIRED on anything that actually executed (added 2026-08-09).
It is the EXECUTED SHARE COUNT from the broker's order record — not the dollar
notional, and not `amount / avg_price`. Without it no position lifecycle can be
reconstructed: `amount` is the notional we ASKED for, which after partial fills,
adds and trims does not tell you how many shares actually moved, so
zero-crossings (the boundary of a position's life) are unrecoverable. A ledger
missing it can never answer "how long was this position held" — and that loss is
permanent, because the broker record is not re-derivable from our side later.

A filled order arriving without `quantity` is journaled anyway (never drop real
execution evidence) but is flagged `quantity_missing` on the event and warned
about on stdout, so the gap is visible instead of silent.

Append-only; safe to run once per fast-loop execution. Also pushes a phone
notification (ntfy) summarizing what was placed/skipped — without this, the
only trade alert the human gets is Robinhood's own app notification.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from research_store import store  # noqa: E402
from notify import push           # noqa: E402

FILLS = REPO / "research_store" / "rh" / "fills.json"
REENTRY_DECISIONS = REPO / "research_store" / "rh" / "reentry_decisions.json"
BROKER_STATE = REPO / "research_store" / "rh" / "broker_state.json"


def publish_snapshot(state: dict, ts: str) -> dict:
    """Publish positions.json through the SAME validated writer the MCP uses.

    ⛔ ONE TIMESTAMP, ONE WRITER. This exists because freshness was inferred by
    comparing two clocks. `src/snapshot_freshness.py` flags the snapshot stale
    iff `snapshot_ts < newest journalled execution ts`, and the exit path used to
    satisfy those two writes from two different programs: this script stamped its
    own now() for the journal, while the agent hand-wrote positions.json with a
    now() of its own. Whichever landed second decided the verdict.

    On 2026-08-25 the snapshot landed 8 seconds BEFORE the journal entry it
    already reflected (MRK 0.020019 = the post-trim quantity), so a correct
    snapshot was branded "not authoritative" until the next session — which
    disables the monitor's ownership filter (market_monitor.py), fires a phone
    push, and makes session.py record a successful trading session as FAILED.
    A tolerance window would only have hidden it; the timestamps were never the
    question. The question is whether one writer published both facts together.

    Passing `ts` in means the caller's journal entry and this snapshot cannot
    disagree, which is what `record_fills()` in src/agent_env/server.py has
    always done and what this path could not do, because the agentic-trader MCP
    is deliberately NOT mounted for the exit executor (deploy/exit_mcp.json).
    Validation is NOT reimplemented here: a partial or unreadable broker read
    must reject the whole write, and only _write_broker_snapshot knows that.
    """
    from agent_env.server import _write_broker_snapshot   # noqa: PLC0415 (heavy; only when used)
    return _write_broker_snapshot(
        state.get("positions"), state.get("portfolio"), ts,
        liquidated=bool(state.get("liquidated", False)),
        declared_account=str(state.get("account_number") or ""),
        orders=state.get("orders") or (),
    )


def main() -> None:
    if not FILLS.exists():
        sys.exit(f"no fills file at {FILLS} — write it first (see module docstring)")
    fills = json.loads(FILLS.read_text())
    if not isinstance(fills, list):
        sys.exit("fills.json must be a JSON array of fill objects")
    # ONE timestamp for the snapshot AND the journal entry below.
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Reconcile broker state FIRST, under that same ts. Absent file = journal
    # only, exactly as before (other callers pass no broker state), but say so:
    # an execution recorded without a matching snapshot is the stale-after-fill
    # condition the monitor and health check are built to catch.
    snapshot = None
    if BROKER_STATE.exists():
        try:
            snapshot = publish_snapshot(json.loads(BROKER_STATE.read_text()), ts)
        except Exception as e:                                   # noqa: BLE001
            snapshot = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if snapshot.get("ok"):
            # Consumed so a later run cannot republish a stale broker read.
            BROKER_STATE.unlink(missing_ok=True)
            print(f"published positions.json @ {ts} "
                  f"({snapshot.get('positions')} positions)")
        else:
            # REFUSED. Left on disk deliberately: the read is still there to be
            # corrected and retried, and the previous snapshot is untouched.
            print(f"⚠ positions.json NOT refreshed — {snapshot.get('error')}\n"
                  f"  {BROKER_STATE} kept for retry; the fills below are still "
                  f"journaled (execution evidence is never dropped).")
    else:
        print("⚠ no broker_state.json — fills will be journaled but positions.json "
              "is NOT refreshed, which reads as stale-after-fill downstream.")

    entry = {
        "event": "execution",
        "ts": ts,
        "n": len(fills),
        "fills": fills,
    }
    # Executed share count is what makes a position lifecycle reconstructable.
    # Never DROP an execution for lacking it — that would lose real evidence to
    # protect a schema — but never let the gap be silent either: flag it on the
    # event so the ledger itself records which rows can't support a lifecycle.
    missing = missing_quantity(fills)
    if missing:
        entry["quantity_missing"] = missing
        print(f"⚠ {len(missing)} executed fill(s) have no `quantity` "
              f"({', '.join(missing)}) — journaled, but these rows cannot support "
              f"position-lifecycle reconstruction. See the module docstring.")
    # post-take-profit re-entry judgments, if the agent made any this run
    # (prompts/fast_loop.md step 7b) — journaled alongside the fills, then
    # consumed so a later run can't re-journal stale decisions
    if REENTRY_DECISIONS.exists():
        try:
            entry["reentry_decisions"] = json.loads(REENTRY_DECISIONS.read_text())
            REENTRY_DECISIONS.unlink()
        except Exception as e:
            print(f"reentry_decisions.json unreadable ({e}) — journaling fills without it")
    store.append_journal(entry)
    print(f"journaled {len(fills)} fills"
          + (f" + {len(entry.get('reentry_decisions', []))} re-entry decisions" if entry.get("reentry_decisions") else "")
          + f" -> {store.JOURNAL}")
    _push_summary(fills, entry.get("reentry_decisions"))
    # Journaling succeeded; reconciliation may not have. Exit non-zero so a
    # refused snapshot is a FAILED step the caller can see, never a silent one.
    if snapshot is not None and not snapshot.get("ok"):
        sys.exit(f"fills journaled, but positions.json was NOT refreshed: "
                 f"{snapshot.get('error')}")


# Skip reasons the system handles by itself and re-plans next run. These are
# working-as-intended deferrals, not incidents: keep them journaled, but off the
# phone — a text about a self-healing condition trains a human to ignore the
# channel.
#
# ⚠️ NARROWED 2026-08-18. "settle" and "pending" were removed when the account
# moved from cash to LIMITED MARGIN. They belonged here while sale proceeds took
# T+1 to return and a buy following a sell was routinely deferred a day. Proceeds
# now settle same-session, so that deferral should not happen at all — which
# makes a settlement skip an ANOMALY, and the first evidence that something about
# the account changed. Suppressing it would hide exactly the signal worth having.
# An underfunded order is still ordinary, so buying-power skips stay quiet.
_EXPECTED_SKIP = ("buying_power", "insufficient")


def _expected_skip(reason) -> bool:
    r = (reason or "").lower()
    return any(k in r for k in _EXPECTED_SKIP)


def missing_quantity(fills: list) -> list:
    """Symbols of orders that EXECUTED but carry no usable share quantity. Pure.
    A skipped order legitimately has none — nothing moved."""
    out = []
    for f in fills:
        if f.get("status") == "skipped":
            continue
        q = f.get("quantity")
        if q is None or not isinstance(q, (int, float)) or q <= 0:
            out.append(str(f.get("symbol", "?")))
    return out


def _selftest() -> None:
    ok = {"symbol": "AAA", "side": "buy", "amount": 5.0, "quantity": 0.07,
          "status": "filled"}
    assert missing_quantity([ok]) == []
    # skipped orders never moved shares — not a gap
    assert missing_quantity([{"symbol": "BBB", "status": "skipped",
                              "reason": "unsettled cash"}]) == []
    # executed-but-unquantified is a gap, in every shape it can arrive in
    assert missing_quantity([{"symbol": "CCC", "status": "filled"}]) == ["CCC"]
    assert missing_quantity([{"symbol": "DDD", "status": "filled",
                              "quantity": 0}]) == ["DDD"]
    assert missing_quantity([{"symbol": "EEE", "status": "filled",
                              "quantity": None}]) == ["EEE"]
    assert missing_quantity([{"symbol": "FFF", "status": "unconfirmed"}]) == ["FFF"]
    # the real 2026-08-07 fills.json shape, which predates the field
    legacy = [{"symbol": "XLE", "side": "buy", "amount": 5.22, "status": "filled",
               "avg_price": 57.3479}]
    assert missing_quantity(legacy) == ["XLE"], "must flag the legacy shape"
    assert missing_quantity([ok, *legacy]) == ["XLE"], "must flag only the gap"
    # --- _expected_skip: what stays off the phone, and what must NOT ----------
    # This had no coverage at all until 2026-08-18, which is how the settlement
    # entries survived the account change unexamined. An underfunded order is
    # ordinary and stays quiet; a settlement deferral is now an anomaly on a
    # limited-margin account and MUST reach the phone.
    assert _expected_skip("insufficient buying_power") is True
    assert _expected_skip("insufficient funds") is True
    assert _expected_skip("pending_settlement") is False, \
        "a settlement skip is an anomaly under limited margin — must NOT be muted"
    assert _expected_skip("unsettled cash") is False, \
        "unsettled cash should not exist on this account — must NOT be muted"
    assert _expected_skip("review rejected") is False
    assert _expected_skip(None) is False
    # ...and the suppression must actually govern the push, not just the helper
    _quiet = [{"symbol": "AAA", "status": "skipped", "reason": "insufficient buying_power"}]
    _loud = [{"symbol": "BBB", "status": "skipped", "reason": "pending_settlement"}]
    assert [f for f in _quiet if not _expected_skip(f["reason"])] == []
    assert [f["symbol"] for f in _loud if not _expected_skip(f["reason"])] == ["BBB"]

    print("selftest OK: missing_quantity flags executed-without-shares, "
          "ignores skips, catches the legacy shape; _expected_skip mutes "
          "buying-power deferrals and surfaces settlement ones")


def _push_summary(fills: list, reentry: list | None) -> None:
    """Phone push: one line per order. push() never raises, so neither do we.
    Routine buying-power deferrals are suppressed (see _EXPECTED_SKIP) — a run
    whose only activity is one of those sends no text at all. A SETTLEMENT skip
    is no longer routine and is no longer suppressed."""
    placed = [f for f in fills if f.get("status") != "skipped"]
    skipped = [f for f in fills if f.get("status") == "skipped"
               and not _expected_skip(f.get("reason"))]
    lines = [f"{f.get('side', '?').upper()} {f.get('symbol', '?')} ${f.get('amount', '?')}"
             + (f" @ ${f['avg_price']}" if f.get("avg_price") else f" ({f.get('status', '?')})")
             for f in placed]
    lines += [f"{f.get('side', '?').upper()} {f.get('symbol', '?')} ${f.get('amount', '?')}"
              + f" — SKIPPED: {f.get('reason', 'no reason recorded')}"
              for f in skipped]
    lines += [f"re-entry {d.get('symbol', '?')}: {d.get('decision', '?')} — {d.get('reason', '')}"
              for d in (reentry or [])]
    if not lines:
        return
    title = f"Agentic: {len(placed)} order{'s' if len(placed) != 1 else ''} placed"
    if skipped:
        title += f", {len(skipped)} skipped"
    push(title, "\n".join(lines), tags="money_with_wings")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
