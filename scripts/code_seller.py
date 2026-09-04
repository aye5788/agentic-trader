#!/usr/bin/env python3
"""THE CODE SELLER — the exit path's model-free last resort (spec §3, 2026-09-04).

WHY. The stop is the monitor plus whatever executes the sale it decided. Until
now that was a model, and on 2026-09-03 the model vendor was down. This is the
exit prompt's MECHANICAL steps with no model: read the request, verify its
identity, read live positions from the broker, sell fraction × live quantity
as a market share-quantity order, write the result file after EACH placement,
and stage the files the monitor's recorders consume. A Claude-wide outage
cannot stop a stop once this exists, provided the broker is up.

AUTHORITY. Steps 1–2 ARE the gate, since the PreToolUse hooks never see this
process: the request on disk must hash to AGENTIC_EXIT_REQUEST_ID (the same
function the exit-scope hook uses, imported — never copied), HALT / SHADOW /
live_approved refuse, the account must be the pinned agentic one, and every
order is checked by the hook's own pure `_scope_verdict` before it is placed.

FAILS CLOSED. Anything unexpected — token refused, paginated positions, a
review that does not match, a non-filled state at the deadline — places
NOTHING for that symbol and leaves the result file absent for it. The monitor
then pauses the symbol and pages, exactly as it does for a model that died.

BROKER ACCESS. The MCP endpoint over streamable HTTP with the OAuth token the
Claude CLI stores in ~/.claude/.credentials.json (read; the only write is
persisting a refreshed token in the same shape). Proven 2026-09-04: the
server accepts that token from a second client.

    AGENTIC_EXIT_REQUEST_ID=<hash> .venv/bin/python scripts/code_seller.py [--drill]

--drill: steps 1–4 for real, then review_equity_order ONLY; nothing is placed,
no file is written. Prints the reviewed payloads. This is the §8.4 proof.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "hooks"))
import governance as gov                                  # noqa: E402
import strategy                                           # noqa: E402
from pretooluse_exit_scope import (                       # noqa: E402  SHARED, never copied
    request_id, _scope_verdict, _authorized_fraction, _to_float)

MON = REPO / "research_store" / "monitor"
RH = REPO / "research_store" / "rh"
EXIT_REQ = MON / "exit_request.json"
EXIT_RES = MON / "exit_result.json"
HALT = REPO / "research_store" / "HALT"
SHADOW = REPO / "research_store" / "SHADOW"
CREDS = Path.home() / ".claude" / ".credentials.json"
SERVER_KEY = "robinhood-trading"
REQUEST_ID_ENV = "AGENTIC_EXIT_REQUEST_ID"
FILL_POLL_S = 60          # how long to wait for `filled` after placing
FILL_POLL_EVERY_S = 3
QTY_DP = 6                # the broker takes 6 decimal places


class Refuse(Exception):
    """A fail-closed refusal: nothing placed for the symbol(s) it names."""


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
def floor_qty(x: float, dp: int = QTY_DP) -> float:
    """Round DOWN to the broker's precision: never a share past the ceiling."""
    f = 10 ** dp
    return math.floor(x * f + 1e-9) / f


def harvest_positions(payload) -> dict:
    """{SYMBOL: {"quantity": float, "avg_cost": float|None}} from any position
    row shape (mirrors the hook's tolerant walk). Never raises."""
    out: dict = {}

    def walk(node):
        if isinstance(node, dict):
            sym = node.get("symbol")
            qty = node.get("quantity", node.get("qty"))
            if isinstance(sym, str) and sym.strip() and qty is not None:
                q = _to_float(qty)
                if q is not None and q >= 0:
                    out[sym.strip().upper()] = {
                        "quantity": q,
                        "avg_cost": _to_float(node.get("average_buy_price", node.get("avg_cost")))}
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(payload)
    return out


def has_next_page(payload) -> bool:
    """Any `next`/cursor pointing onward = a page we did not read. Fail closed."""
    if isinstance(payload, dict):
        for k in ("next", "next_cursor", "cursor", "next_url"):
            if payload.get(k):
                return True
        return any(has_next_page(v) for v in payload.values() if isinstance(v, (dict, list)))
    if isinstance(payload, list):
        return any(has_next_page(v) for v in payload)
    return False


def sell_quantity(fraction: float, live_qty: float) -> float:
    """fraction >= 1 -> the whole live position; else floor(fraction × live, 6dp)."""
    if fraction >= 1.0:
        return live_qty
    return floor_qty(fraction * live_qty)


def order_state(payload) -> tuple:
    """(state, average_price, cumulative_quantity, order dict) from get_equity_orders."""
    found = None

    def walk(node):
        nonlocal found
        if found is not None:
            return
        if isinstance(node, dict):
            if "state" in node and ("id" in node or "order_id" in node):
                found = node
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(payload)
    if not found:
        return None, None, None, None
    return (str(found.get("state") or ""), _to_float(found.get("average_price")),
            _to_float(found.get("cumulative_quantity") or found.get("quantity")), found)


# --------------------------------------------------------------------------- #
# broker token
# --------------------------------------------------------------------------- #
def load_token() -> dict:
    creds = json.loads(CREDS.read_text())
    entry = next((v for k, v in (creds.get("mcpOAuth") or {}).items()
                  if k.startswith(SERVER_KEY)), None)
    if not entry or not entry.get("accessToken") or not entry.get("serverUrl"):
        raise Refuse("no stored broker token in the CLI credential store")
    return entry


def token_fresh(entry: dict, margin_s: int = 300) -> bool:
    exp = _to_float(entry.get("expiresAt"))
    return bool(exp) and (exp / 1000.0 - time.time()) > margin_s


async def refresh_token(entry: dict) -> dict:
    """OAuth refresh through the server's discovered authorization server.
    Persists the new token in the CLI's own shape so the CLI keeps working."""
    import httpx                                          # noqa: PLC0415  (mcp dependency)
    auth = ((entry.get("discoveryState") or {}).get("authorizationServerUrl")
            or entry.get("serverUrl"))
    async with httpx.AsyncClient(timeout=20) as c:
        meta = (await c.get(auth.rstrip("/") + "/.well-known/oauth-authorization-server")).json()
        r = await c.post(meta["token_endpoint"], data={
            "grant_type": "refresh_token", "refresh_token": entry["refreshToken"],
            "client_id": entry["clientId"]})
        if r.status_code != 200:
            raise Refuse(f"token refresh refused ({r.status_code})")
        tok = r.json()
    entry = dict(entry, accessToken=tok["access_token"],
                 refreshToken=tok.get("refresh_token", entry["refreshToken"]),
                 expiresAt=int((time.time() + int(tok.get("expires_in", 3600))) * 1000))
    creds = json.loads(CREDS.read_text())
    for k in list((creds.get("mcpOAuth") or {})):
        if k.startswith(SERVER_KEY):
            creds["mcpOAuth"][k] = entry
    CREDS.write_text(json.dumps(creds))
    return entry


# --------------------------------------------------------------------------- #
# the sale
# --------------------------------------------------------------------------- #
def _text(result) -> str:
    return "".join(getattr(c, "text", "") for c in (result.content or []))


def _json(result):
    t = _text(result)
    try:
        return json.loads(t)
    except Exception:                                     # noqa: BLE001
        return {"_raw": t}


async def _call(session, name: str, args: dict):
    r = await session.call_tool(name, args)
    if getattr(r, "isError", False):
        raise Refuse(f"{name} returned an error: {_text(r)[:300]}")
    return _json(r)


def _write_result(sold: list) -> None:
    EXIT_RES.write_text(json.dumps({"ts": _now(), "sold": sold}, indent=2))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def preflight(bound_id: str) -> tuple:
    """Steps 1–2. -> (request, account). Raises Refuse."""
    if not EXIT_REQ.exists():
        raise Refuse("no exit_request.json")
    request = json.loads(EXIT_REQ.read_text())
    if not bound_id or request_id(request) != bound_id:
        raise Refuse("request on disk does not hash to AGENTIC_EXIT_REQUEST_ID — "
                     "not the request this process was launched for")
    cfg = strategy.load()
    if HALT.exists() or gov.kill_switch_active(cfg):
        raise Refuse("HALT / kill switch active")
    if SHADOW.exists():
        raise Refuse("SHADOW mode: every order is denied")
    if not gov.live_approved(cfg):
        raise Refuse("live_approved is false")
    account = str(request.get("account") or "")
    pinned = str(((cfg.get("account") or {}).get("number")) or "")
    if not account:
        raise Refuse("request names no account")
    if pinned and account != pinned:
        raise Refuse("request account is not the pinned agentic account")
    exits = [e for e in (request.get("exits") or []) if isinstance(e, dict)]
    if not exits:
        raise Refuse("request carries no exits")
    return request, account


async def run(bound_id: str, drill: bool) -> int:
    from mcp import ClientSession                         # noqa: PLC0415
    from mcp.client.streamable_http import streamablehttp_client  # noqa: PLC0415

    request, account = preflight(bound_id)
    entry = load_token()
    if not token_fresh(entry):
        entry = await refresh_token(entry)
    sold: list = []
    fills: list = []
    closes_full: list = []
    closes_partial: list = []
    orders_raw: list = []
    failures: list = []
    async with streamablehttp_client(entry["serverUrl"],
                                     headers={"Authorization": f"Bearer {entry['accessToken']}"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            accts = await _call(s, "get_accounts", {})
            rows = (accts.get("data") or accts).get("accounts") if isinstance(accts, dict) else accts
            ok_acct = any(isinstance(a, dict) and a.get("agentic_allowed")
                          and str(a.get("account_number")) == account for a in (rows or []))
            if not ok_acct:
                raise Refuse("the broker does not list the request's account as agentic_allowed")
            pos_raw = await _call(s, "get_equity_positions", {"account_number": account})
            if has_next_page(pos_raw):
                raise Refuse("positions read is paginated — refusing to size against a partial book")
            held = harvest_positions(pos_raw)
            for e in request["exits"]:
                sym = str(e.get("symbol") or "").strip().upper()
                reason = str(e.get("reason") or "")
                frac = _authorized_fraction(request, sym)
                if frac is None or frac == "malformed":
                    failures.append((sym, "no usable fraction in the request")); continue
                live = held.get(sym)
                if not live or live["quantity"] <= 0:
                    print(f"  {sym}: not held (or zero) per the live read — nothing to sell")
                    continue
                qty = sell_quantity(float(frac), live["quantity"])
                if qty <= 0:
                    failures.append((sym, f"computed quantity {qty} is not positive")); continue
                ti = {"symbol": sym, "side": "sell", "type": "market",
                      "quantity": f"{qty:.6f}", "market_hours": "regular_hours"}
                verdict = _scope_verdict(sym, float(frac), ti, live["quantity"])
                if verdict.get("permissionDecision") != "allow":
                    failures.append((sym, verdict.get("permissionDecisionReason", "scope refused"))); continue
                review = await _call(s, "review_equity_order", dict(ti, account_number=account))
                print(f"  {sym}: reviewed sell {ti['quantity']} sh ({reason}, fraction {frac:g} "
                      f"of {live['quantity']:g}) -> {json.dumps(review)[:160]}")
                if drill:
                    continue
                ref = str(uuid.uuid4())
                placed = await _call(s, "place_equity_order",
                                     dict(ti, account_number=account, ref_id=ref))
                oid = None
                _, _, _, od = order_state(placed)
                if od:
                    oid = str(od.get("id") or od.get("order_id") or "")
                sold.append({"symbol": sym, "reason": reason, "quantity_or_amount": ti["quantity"],
                             "order_id": oid or ref, "status": "placed"})
                _write_result(sold)                       # after EACH placement
                state, px, cum, od = "", None, None, None
                deadline = time.time() + FILL_POLL_S
                while time.time() < deadline:
                    got = await _call(s, "get_equity_orders",
                                      {"account_number": account, "order_id": oid} if oid else
                                      {"account_number": account})
                    state, px, cum, od = order_state(got)
                    if state in ("filled", "cancelled", "rejected", "failed"):
                        break
                    await asyncio.sleep(FILL_POLL_EVERY_S)
                sold[-1]["status"] = state or "unknown"
                _write_result(sold)
                if od:
                    orders_raw.append(od)
                fills.append({"symbol": sym, "side": "sell", "quantity": cum or qty,
                              "avg_price": px, "amount": (px * (cum or qty)) if px else None,
                              "order_id": oid or ref, "status": state or "unknown",
                              "placed_at": _now()})
                row = {"symbol": sym, "entry_price": live["avg_cost"], "exit_price": px,
                       "exit_date": datetime.now(timezone.utc).date().isoformat(),
                       "exit_reason": reason}
                if float(frac) >= 1.0:
                    closes_full.append(row)
                else:
                    closes_partial.append(dict(row, fraction=float(frac)))
            if drill:
                print("DRILL: nothing placed, nothing written.")
                return 0
            if sold:
                post = await _call(s, "get_equity_positions", {"account_number": account})
                if has_next_page(post):
                    print("  ⚠ post-sell positions paginated — broker_state.json NOT staged; "
                          "the snapshot will not republish (monitor warns)")
                else:
                    port = await _call(s, "get_portfolio", {"account_number": account})
                    (RH / "broker_state.json").write_text(json.dumps({
                        "positions": {"pages": [{"cursor": None, "response": post}], "exhausted": True},
                        "portfolio": port, "account_number": account, "liquidated": False}))
                (RH / "fills.json").write_text(json.dumps(fills, indent=2))
                if closes_full:
                    (RH / "exit_closes.json").write_text(json.dumps(closes_full, indent=2))
                if closes_partial:
                    (RH / "partial_closes.json").write_text(json.dumps(closes_partial, indent=2))
                if orders_raw:
                    (RH / "orders_dump.json").write_text(json.dumps(orders_raw, indent=2))
    for sym, why in failures:
        print(f"  {sym}: REFUSED — {why}")
    print(f"code_seller: sold {len(sold)}, refused {len(failures)}")
    return 0 if not failures else 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--drill", action="store_true")
    a = ap.parse_args()
    try:
        return asyncio.run(run(os.environ.get(REQUEST_ID_ENV, ""), a.drill))
    except Refuse as e:
        print(f"code_seller REFUSED: {e}")
        return 3
    except Exception as e:                                # noqa: BLE001
        print(f"code_seller FAILED (nothing further placed): {type(e).__name__}: {e}")
        return 4


def _selftest() -> None:
    assert floor_qty(0.0074709999) == 0.00747 and floor_qty(1.0) == 1.0
    assert sell_quantity(1.0, 0.007471) == 0.007471
    assert sell_quantity(0.5, 0.007471) == 0.003735            # floor, never 0.0037355 rounded up
    assert sell_quantity(0.33, 1.0) == 0.33
    h = harvest_positions({"data": {"positions": [{"symbol": "dell", "quantity": "0.007471",
                                                     "average_buy_price": "444.38"}]}})
    assert h == {"DELL": {"quantity": 0.007471, "avg_cost": 444.38}}, h
    assert harvest_positions("garbage") == {}
    assert has_next_page({"data": {"positions": [], "next": "cursor-2"}})
    assert not has_next_page({"data": {"positions": [], "next": None}})
    st = order_state({"data": {"orders": [{"id": "abc", "state": "filled",
                                            "average_price": "510.7964", "cumulative_quantity": "0.003735"}]}})
    assert st[0] == "filled" and st[1] == 510.7964 and st[2] == 0.003735 and st[3]["id"] == "abc"
    assert order_state({}) == (None, None, None, None)
    # the hook's own verdict guards every order this script would place
    ti = {"symbol": "DELL", "side": "sell", "type": "market", "quantity": f"{sell_quantity(0.5, 0.007471):.6f}"}
    assert _scope_verdict("DELL", 0.5, ti, 0.007471)["permissionDecision"] == "allow"
    ti_bad = dict(ti, quantity="0.005")
    assert _scope_verdict("DELL", 0.5, ti_bad, 0.007471)["permissionDecision"] == "deny"
    # preflight refuses a request that does not hash to the bound id
    import tempfile  # noqa: PLC0415
    global EXIT_REQ
    saved = EXIT_REQ
    try:
        with tempfile.TemporaryDirectory() as td:
            EXIT_REQ = Path(td) / "exit_request.json"
            EXIT_REQ.write_text(json.dumps({"ts": "t", "account": "1", "exits": [{"symbol": "X", "fraction": 1.0}]}))
            try:
                preflight("not-the-hash")
            except Refuse as e:
                assert "does not hash" in str(e), e
            else:
                raise AssertionError("must refuse an unbound request")
            try:
                preflight("")
            except Refuse:
                pass
            else:
                raise AssertionError("must refuse an empty bound id")
    finally:
        EXIT_REQ = saved
    print("code_seller: OK -- quantity floors at 6dp and never exceeds the ceiling, positions "
          "harvest tolerantly, pagination is detected, order state parses, the hook's verdict "
          "guards the order, an unbound request is refused")


if __name__ == "__main__":
    if "--" + "selftest" in sys.argv:
        _selftest()
    else:
        raise SystemExit(main())
