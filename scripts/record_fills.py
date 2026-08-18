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
  2. run:  .venv/bin/python scripts/record_fills.py

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


def main() -> None:
    if not FILLS.exists():
        sys.exit(f"no fills file at {FILLS} — write it first (see module docstring)")
    fills = json.loads(FILLS.read_text())
    if not isinstance(fills, list):
        sys.exit("fills.json must be a JSON array of fill objects")
    entry = {
        "event": "execution",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
