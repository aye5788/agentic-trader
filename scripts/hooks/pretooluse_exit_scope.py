#!/usr/bin/env python3
"""THE EXIT-SCOPE GATE — a PreToolUse hook for the EXIT EXECUTOR ONLY.

⛔ THIS IS NOT THE GENERAL ORDER GATE. It answers a different question, and the
two must never be merged:

    scripts/hooks/pretooluse_order_gate.py  — "is this CLASS of live order
                                              allowed by global safety policy?"
                                              (kill switch, SHADOW, live_approved,
                                              drawdown, cooldown, rule-outs, cap)

    THIS FILE                               — "is this order part of the EXACT
                                              exit this executor process was
                                              authorized to perform?"

Both are declared on `place_equity_order` in deploy/exit_executor_settings.json
and both run. Composition was VERIFIED against the installed CLI (2.1.241, this
box): every matching PreToolUse hook executes, and a single `deny` blocks the
call regardless of which hook emitted it or what order the hooks are listed in
— whether they sit in one matcher block or in two. So this gate is purely
ADDITIVE: it can only ever subtract authority, never restore any the general
gate withheld.

⛔ WHY IT EXISTS. The general gate deliberately ALLOWS every SELL after the
kill-switch/SHADOW checks (see its `decide()`, step 5), because
scripts/market_monitor.py IS the stop for fractional shares and a gate that
refuses a sell does not pause trading — it strips an open position of its only
protection. That is correct for the trading sessions.

But the exit executor is not a trading session. Its entire authority is "place
the exits the monitor already decided on, in exit_request.json". prompts/exit.md
says so in prose — "Only sell symbols in exit_request.json. Never exceed the
position you actually hold" — and NOTHING ENFORCED IT. An exit-executor process
that sold some other holding, or sold 100% where a target-1 trim authorized 50%,
passed the generic sell gate untouched. Prompt scope was not mechanical scope.
This file makes them the same thing.

WHAT AUTHORIZES AN ORDER HERE (all of it, or the order is refused):

  identity  The monitor exports AGENTIC_EXIT_REQUEST_ID when it spawns the
            executor, computed by request_id() over the exact request dict it
            just wrote. This hook recomputes it from the file on disk and
            requires a match. That binds THE PROCESS to THE REQUEST IT WAS
            LAUNCHED FOR: a stale exit_request.json left over from an earlier
            trigger authorizes nothing, an executor started by hand with no env
            var authorizes nothing, and a request rewritten underneath a running
            executor stops authorizing it mid-flight.
  account   order.account_number must equal request["account"].
  side      SELL only. The exit executor never buys.
  symbol    must appear in request["exits"].
  once      ONE DISPATCH PER (REQUEST, SYMBOL). The first order that reaches
            Robinhood consumes the request's authorization for that symbol; a
            second is refused however small it is. Bounding each order alone
            does not bound the exit — two "sell 50%" orders are individually
            legal and together a liquidation nobody decided on. See _spent().
  scope     the order must state SHARES, and must sit inside `held` (full exit)
            or `fraction * held` (trim), where `held` is what THIS invocation
            read back from the broker. No broker read -> no authorization. See
            _scope_verdict().

FAIL CLOSED, WITHOUT EXCEPTION. An earlier revision of this gate allowed an
unbounded FULL exit when the position read was missing, reasoning that refusing
a stop is worse than over-selling one. That trade was not this gate's to make:
"sell the whole position" is a claim about the CURRENT position and needs the
current position to be known, and the monitor already owns the recovery path
(refire, backoff, then a loud manual-intervention push to a human). An
unreadable position now produces a refusal and an escalation, not a guess with
real money behind it.

Everything denies: missing/unreadable/malformed request, absent identity,
mismatched identity, unparseable order, wrong account, wrong side, wrong symbol,
spent authorization, unreadable position, unbounded size, or ANY unexpected
exception. driver() wraps the lot, because a hook that crashes prints nothing
and a hook that prints nothing is read by the harness as "no opinion", which
lets the order through. There is no branch that allows on missing information.

Pure/impure split matches the general gate: decide() takes every input as an
argument and touches no clock, no disk and no network; driver() does the I/O.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

ORDER_TOOL = "mcp__robinhood-trading__place_equity_order"
POSITIONS_TOOL = "mcp__robinhood-trading__get_equity_positions"

EXIT_REQUEST = REPO / "research_store" / "monitor" / "exit_request.json"

#: Env var the monitor sets when it spawns the executor. Absent -> no authority.
REQUEST_ID_ENV = "AGENTIC_EXIT_REQUEST_ID"

#: Robinhood accepts fractional quantities to 6 decimal places, so a truthful
#: `fraction * quantity` may legitimately round UP by half a unit in the last
#: place. Anything beyond one full unit is not rounding, it is over-selling.
QTY_TOLERANCE = 1e-6

#: Prefixed to EVERY deny this gate emits. The harness copies a hook's
#: permissionDecisionReason into the tool_result the transcript records, so this
#: token is how a LATER invocation of this same gate can prove that an earlier
#: place_equity_order was stopped by us BEFORE the broker ever saw it. Without a
#: marker we control, "refused by the gate" and "rejected by Robinhood after it
#: had the order" are both just is_error=true, and telling them apart by
#: guesswork is exactly the ambiguity that must never resolve toward "sell
#: again". Do not remove it and do not reword it.
DENY_TOKEN = "[exit-scope]"

#: Substrings that PROVE a prior place_equity_order never reached the broker.
#: Deliberately short and closed: anything NOT matched here is treated as
#: DISPATCHED, so a marker we fail to recognise costs a refused retry (the
#: monitor refires with a fresh request) rather than a double sale.
#:
#: For a SELL the general gate can only refuse via its kill-switch, SHADOW,
#: unreadable-side, unreadable-tool_input or fail-closed branches — every one of
#: those strings is covered below. Its remaining refusals are BUY-only and this
#: gate denies buys outright, so they are unreachable here.
BLOCKED_MARKERS = (
    DENY_TOKEN,                 # this gate
    "order gate",               # general gate: side/tool_input/FAILED CLOSED
    "KILL-SWITCH active",       # general gate: kill switch
    "SHADOW MODE",              # general gate: shadow rollout switch
    "Permission to use",        # harness: tool not permission-approved
    "blocked by a hook",        # harness: hook denial phrasing
)


# --------------------------------------------------------------------------- #
# request identity
# --------------------------------------------------------------------------- #
def request_id(request: dict) -> str:
    """Stable content id for one exit request. PURE.

    ⛔ SHARED WITH THE MONITOR ON PURPOSE. scripts/market_monitor.py imports
    THIS function to stamp the environment it spawns the executor with, so the
    producer and the verifier can never drift into two definitions of "the same
    request". That is why this module imports nothing from `src/` and nothing
    outside the stdlib: the monitor runs under system /usr/bin/python3 (3.10)
    and this hook under the repo .venv (3.12), and the one thing they share has
    to import cleanly in both.

    Content-addressed, not a counter or a timestamp: it changes if ANY field
    changes — the symbol, the fraction, the account, the ts. So a request that
    is rewritten under a running executor stops matching, which is exactly the
    "stale authorization" case this is here to refuse.
    """
    blob = json.dumps(request, sort_keys=True, separators=(",", ":"),
                      default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _allow(reason: str) -> dict:
    return {"permissionDecision": "allow", "permissionDecisionReason": reason}


def _deny(reason: str) -> dict:
    """Every refusal is stamped — see DENY_TOKEN. A later run of this gate reads
    that stamp back out of the transcript to prove the order never dispatched."""
    return {"permissionDecision": "deny",
            "permissionDecisionReason": f"{DENY_TOKEN} {reason}"}


def _to_float(v):
    """Parse an order field to a finite float, else None.

    Same contract as the general gate's `_to_float`, and for the same reason:
    the RH MCP passes `quantity` / `dollar_amount` as STRINGS. Bools are
    rejected despite being `int` to Python, and non-finite values become None,
    so an unusable number can only ever REFUSE — never slide through a `>`
    comparison the way NaN silently does.
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


# --------------------------------------------------------------------------- #
# broker truth, read out of THIS session's own transcript
# --------------------------------------------------------------------------- #
def scan_transcript(lines, current_tool_use_id=None) -> tuple:
    """-> (positions {SYMBOL: qty}, spent {SYMBOL: reason}) for THIS invocation.

    ONE pass, two answers, because both come from the same records and reading
    the file twice on the critical path of an order buys nothing.

    `positions` is the broker truth this session read back — see the notes on
    authority below. `spent` names every symbol whose single permitted sell has
    already been used up under this request, mapping to a short human reason.

    ⛔ WHY `spent` EXISTS: ONE DISPATCH PER (REQUEST, SYMBOL). Bounding each
    order at `fraction × held` bounds each order and nothing else — two
    successive "sell 50%" orders under one authorization are individually legal
    and collectively a full liquidation the monitor never asked for. The
    authorization is for AN EXIT, not for a rate of selling. So the first order
    that reaches Robinhood consumes it, and any later order for that symbol
    under the same request id is refused. Anything genuinely left over is
    reconciled and re-requested by the monitor, which is the recovery path that
    already exists (refire/backoff/escalate) — no residual ledger, no new
    mutable state, and never two exit orders outstanding on one position racing
    each other.

    ⛔ AMBIGUITY RESOLVES TO CONSUMED. A prior call counts as dispatched unless
    its result carries positive proof it was stopped first (BLOCKED_MARKERS). A
    call with NO result at all is the dangerous case — it is what a kill mid-
    flight looks like, and it is indistinguishable from an order the broker
    accepted — so it counts as CONSUMED. That is the same reasoning that made
    scripts/market_monitor.py pause a symbol when exit_result.json is absent
    after the 2026-08-19 incident: an unknown outcome is never retried.

    ⚠️ THE ORDER BEING GATED RIGHT NOW MUST NOT COUNT AGAINST ITSELF. Its own
    tool_use block is already in the transcript when this hook runs (verified on
    this box), so `current_tool_use_id` is excluded by id — not by position,
    which would break the moment the model emits two orders in one turn. When it
    does emit two at once, the second correctly sees the first as outstanding.
    """
    records = list(lines)               # may arrive as an open file handle
    return _positions(records), _spent(records, current_tool_use_id)


def _spent(lines, current_tool_use_id) -> dict:
    """-> {SYMBOL: reason} for symbols whose one permitted sell is used up."""
    calls, results = {}, {}
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:                                   # noqa: BLE001
            continue
        if not isinstance(rec, dict):
            continue
        msg = rec.get("message")
        blocks = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(blocks, list):
            continue
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("name") == ORDER_TOOL:
                ti = b.get("input")
                sym = ""
                if isinstance(ti, dict):
                    sym = str(ti.get("symbol") or "").strip().upper()
                if isinstance(b.get("id"), str):
                    calls[b["id"]] = sym
            elif b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                if isinstance(tid, str):
                    results[tid] = (bool(b.get("is_error")),
                                    json.dumps(b.get("content"), default=str))

    out = {}
    for cid, sym in calls.items():
        if not sym or cid == current_tool_use_id:
            continue
        res = results.get(cid)
        if res is None:
            reason = ("an earlier order for it is still outstanding with no "
                      "recorded result — an unknown outcome is never retried")
        elif not res[0]:
            reason = "an earlier order for it was accepted by Robinhood"
        elif any(mk in res[1] for mk in BLOCKED_MARKERS):
            continue                    # proven stopped before dispatch
        else:
            reason = ("an earlier order for it failed AFTER reaching Robinhood, "
                      "so it cannot be proven undelivered")
        out[sym] = reason               # any consumed call consumes the symbol
    return out


def _positions(lines) -> dict:
    """-> {SYMBOL: qty} from the get_equity_positions results in this session.

    ⛔ WHY THE TRANSCRIPT AND NOT research_store/rh/positions.json. That file is
    a CACHE and CLAUDE.md says so in bold: it is not authoritative, it is
    written by other processes, and it has been two days stale while the monitor
    stop-watched a position that had already been sold. Sizing a live sell
    against it would be exactly the "guess from stale local holdings" this gate
    exists to prevent.

    What this reads instead is the response the BROKER returned to THIS
    executor, in THIS process, for THIS request — step 3 of prompts/exit.md,
    the same read the executor is required to size its own order from. It is
    the authoritative number, and it is the one derived for this request.

    Verified against the installed CLI (2.1.241): the PreToolUse payload carries
    `transcript_path`, and a prior turn's `tool_result` is already flushed to
    that file by the time a later tool's hook runs.

    Tolerant by construction — it walks the parsed payload for anything shaped
    like a position row rather than asserting one envelope. The broker's
    documented shape is {"data": {"positions": [{"symbol", "quantity",
    "average_buy_price", ...}]}}, but the connector has been observed to nest
    and paginate differently, and the caller treats "not found" as a normal
    outcome. This function therefore NEVER raises: an unreadable line is a line
    it skips, and the verdict for a missing symbol is made by the caller, not
    by an exception here.

    LATEST WINS. If the session read positions more than once, the last read is
    used: prompts/exit.md sizes a scale-out against the CURRENT quantity, so
    that is the number the authorization is denominated in.
    """
    ids = set()
    out = {}

    def harvest(node):
        """Depth-first walk collecting any {symbol, quantity} pair."""
        if isinstance(node, dict):
            sym = node.get("symbol")
            qty = node.get("quantity", node.get("qty"))
            if isinstance(sym, str) and sym.strip() and qty is not None:
                q = _to_float(qty)
                if q is not None and q >= 0:
                    out[sym.strip().upper()] = q
            for v in node.values():
                harvest(v)
        elif isinstance(node, list):
            for v in node:
                harvest(v)

    def text_of(content):
        """Flatten an MCP tool_result content into candidate JSON strings."""
        if isinstance(content, str):
            return [content]
        if isinstance(content, list):
            chunks = []
            for b in content:
                if isinstance(b, str):
                    chunks.append(b)
                elif isinstance(b, dict):
                    t = b.get("text")
                    if isinstance(t, str):
                        chunks.append(t)
                    elif b.get("type") == "json" and "json" in b:
                        chunks.append(json.dumps(b["json"]))
            return chunks
        if isinstance(content, dict):
            return [json.dumps(content)]
        return []

    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:                                   # noqa: BLE001
            continue
        if not isinstance(rec, dict):
            continue
        blocks = ((rec.get("message") or {}) if isinstance(rec.get("message"), dict)
                  else {}).get("content")
        if not isinstance(blocks, list):
            continue
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("name") == POSITIONS_TOOL:
                if isinstance(b.get("id"), str):
                    ids.add(b["id"])
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in ids:
                for chunk in text_of(b.get("content")):
                    try:
                        harvest(json.loads(chunk))
                    except Exception:                       # noqa: BLE001
                        continue
    return out


# --------------------------------------------------------------------------- #
# decision core (pure — every input is passed in)
# --------------------------------------------------------------------------- #
def _authorized_fraction(request: dict, symbol: str):
    """-> the largest fraction `symbol` is authorized for, or None if absent.

    `None` and `0.0` are different answers and the caller treats them so: absent
    from the request means NO authority at all, while a present-but-unusable
    fraction is a malformed request. The monitor emits at most one exit per
    symbol per request (its trigger loop is an elif chain keyed on the symbol),
    but taking the max is what makes that an observation rather than an
    assumption this gate would break on.
    """
    exits = request.get("exits")
    if not isinstance(exits, list):
        return None
    best = None
    for e in exits:
        if not isinstance(e, dict):
            continue
        if str(e.get("symbol") or "").strip().upper() != symbol:
            continue
        f = _to_float(e.get("fraction"))
        if f is None or f <= 0:
            return "malformed"
        best = f if best is None else max(best, f)
    return best


def _scope_verdict(symbol: str, fraction: float, ti: dict, held) -> dict:
    """Is the ORDER SIZE inside what this request authorized?

    ONE RULE FOR BOTH SHAPES, and the symmetry is the point:

        fraction >= 1.0  ->  ceiling = held
        fraction <  1.0  ->  ceiling = fraction * held

    where `held` is what THIS invocation read back from Robinhood. Both require
    the order to state a share `quantity`, and both require that broker read.
    A full exit is not a weaker claim than a trim — it is a claim about the
    CURRENT position, and it needs the current position to be known.

    WHY EVERY EXIT MUST STATE SHARES. A `dollar_amount` names no share count, so
    there is nothing to compare against the position; bounding one would mean
    converting through a price this hook must not invent and cannot fetch (the
    general gate documents the same refusal on the buy side). This costs nothing
    real — every sell this system has actually placed went out as a fractional
    share quantity (RTX 0.026963, SNDK 0.00072, from the broker's own order
    records), and Robinhood rejects a dollar-notional sell that consumes a whole
    position anyway (EQUITY_DOLLAR_BASED_SELL_ALL_ERROR).

    WHY THE TOLERANCE. `fraction * held` is a real number and the broker takes 6
    decimal places, so a truthful trim may round UP by half a unit in the last
    place. QTY_TOLERANCE covers exactly that and nothing wider.

    ⛔ AND THE CEILING IS PER ORDER, WHICH IS NOT ENOUGH ON ITS OWN. Two
    successive "sell 50%" orders each pass this function and together liquidate
    the position. The cumulative half of the rule lives in `_spent()` and is
    applied by decide() BEFORE this runs: one dispatch per (request, symbol).
    """
    qty = _to_float(ti.get("quantity"))
    dollars = _to_float(ti.get("dollar_amount"))
    kind = "FULL exit" if fraction >= 1.0 else f"{fraction:g} scale-out"

    # ---- the order must state SHARES ---------------------------------------
    if qty is None:
        if dollars is not None:
            return _deny(
                f"SELL {symbol} REFUSED: the order is priced as "
                f"dollar_amount={ti.get('dollar_amount')!r}, which states no "
                "share count, so there is nothing to bound against the position "
                "you hold. Converting it would mean inventing a price this gate "
                "must not fetch. Re-place as a fractional share `quantity` — "
                "which is how every exit this system has actually placed went "
                "out, and what Robinhood requires for a full position anyway "
                "(it rejects a dollar-notional sell-all).")
        return _deny(
            f"SELL {symbol} REFUSED: no usable share quantity "
            f"({ti.get('quantity')!r}). Place the exit as a fractional share "
            "`quantity`.")
    if qty <= 0:
        return _deny(
            f"SELL {symbol} refused: quantity {ti.get('quantity')!r} is not a "
            "positive number, so this gate cannot tell what is being sold.")

    # ---- and it must be bounded by what is ACTUALLY HELD, right now ---------
    # ⛔ NO BROKER QUANTITY MEANS NO QUANTITY AUTHORIZATION — for a full exit
    # exactly as much as for a trim. A `fraction: 1.0` request authorizes
    # LIQUIDATING THE CURRENT POSITION; it does not authorize an arbitrary share
    # count merely because current ownership could not be read. An earlier
    # revision of this gate allowed an unbounded full exit whenever the position
    # read was missing, on the argument that refusing a stop is worse than
    # over-selling one. That trade was not the gate's to make: the monitor
    # already owns the recovery path (refire, backoff, then a loud manual-
    # intervention push), so an unreadable position produces a REFUSED order and
    # an escalation to a human, not a guess with real money behind it.
    if held is None:
        return _deny(
            f"SELL {symbol} {qty:g} sh REFUSED ({kind}): this invocation has no "
            f"authoritative get_equity_positions result for {symbol}, so the "
            "quantity it authorizes cannot be established. Call "
            "get_equity_positions for the account (step 3) and re-place. This "
            "gate will not size a live sell from the cached snapshot, from a "
            "dollar value, or from the monitor's trigger price.")

    # ⛔ THE TOLERANCE APPLIES ONLY WHERE ROUNDING ACTUALLY HAPPENS. A trim is
    # `fraction * held`, a real number the executor must round to the broker's 6
    # decimal places, so it may legitimately land half a unit high —
    # QTY_TOLERANCE covers that. A FULL exit is not computed: the ceiling IS the
    # held quantity, already a 6dp number straight from the broker, so the only
    # slack it needs is float-representation noise. Granting it the 6dp
    # tolerance instead let an order for one whole extra unit past the position
    # through (caught by case K2) — small in shares, but it is the gate
    # answering "how much may be sold" with a number nobody authorized.
    if fraction >= 1.0:
        ceiling, slack = held, abs(held) * 1e-9
    else:
        ceiling, slack = fraction * held, QTY_TOLERANCE
    if qty > ceiling + slack:
        if fraction >= 1.0:
            return _deny(
                f"SELL {symbol} {qty:g} sh REFUSED: exceeds the {held:g} sh this "
                "account actually holds per this invocation's own "
                "get_equity_positions read. A full exit is the whole position, "
                "not more than it.")
        return _deny(
            f"SELL {symbol} {qty:g} sh REFUSED: the monitor authorized a "
            f"{fraction:g} scale-out of the {held:g} sh held = {ceiling:.6f} sh. "
            "This order is larger. A target trim is not permission to close the "
            "position; if the whole position should go, that is a separate "
            "decision the monitor has not made.")
    return _allow(
        f"SELL {symbol} {qty:g} sh: {kind} authorized, within the {ceiling:.6f} "
        f"sh ceiling ({held:g} sh held per this invocation's broker read). This "
        f"consumes this request's authorization for {symbol} — any further "
        f"{symbol} order needs a new request from the monitor.")


def decide(payload: dict, request, bound_id, held_by_symbol: dict,
           spent: dict | None = None) -> dict:
    """The exit-scope verdict for one hook payload. PURE.

    `request` is the parsed exit_request.json, or None if it is missing or
    unreadable. `bound_id` is the value of AGENTIC_EXIT_REQUEST_ID as the
    executor process received it. `held_by_symbol` is what this session read
    back from the broker.

    Order of the gates, and why:
      1. not the order tool   -> allow, untouched. The matcher should keep us
                                 out; a mis-fire must not block unrelated work.
      2. request identity     -> the process must be running the request it was
                                 launched with. Checked FIRST because every
                                 later check reads that request, and checking
                                 scope against an unauthenticated document is
                                 not a check.
      3. order is readable    -> deny an unparseable order record.
      4. account              -> the request names the account; the order must
                                 match it (CLAUDE.md hard rule 1).
      5. side                 -> SELL only. The executor never buys.
      6. symbol               -> must be in this request.
      7. already spent        -> ONE DISPATCH PER (REQUEST, SYMBOL). Checked
                                 BEFORE size, because a second order for a
                                 symbol is refused on authority grounds however
                                 small it is: the request authorized an exit,
                                 not a sequence of them.
      8. scope                -> _scope_verdict(), see its docstring.
    """
    tool = (payload or {}).get("tool_name")
    if tool != ORDER_TOOL:
        return _allow(f"{tool!r} is not {ORDER_TOOL} — exit-scope gate does not apply")

    # ---- 2. request identity ------------------------------------------------
    if not bound_id:
        return _deny(
            f"EXIT SCOPE: this process carries no {REQUEST_ID_ENV}, so it cannot "
            "prove which exit request it was launched to perform. Only the market "
            "monitor may authorize an exit executor, and it stamps that variable "
            "when it spawns one. No order may be placed. (If you are a human "
            "debugging: place the exit by hand in the broker, do not re-run the "
            "executor to get around this.)")
    if request is None:
        return _deny(
            f"EXIT SCOPE: {EXIT_REQUEST} is missing or unreadable, so there is no "
            "authorization to check this order against. Refusing to sell.")
    if not isinstance(request, dict) or not isinstance(request.get("exits"), list):
        return _deny(
            "EXIT SCOPE: exit_request.json is malformed — no `exits` array. "
            "Refusing to sell against a request that cannot be read.")
    actual = request_id(request)
    if actual != bound_id:
        return _deny(
            f"EXIT SCOPE: STALE OR REWRITTEN REQUEST. This executor was launched "
            f"for request {bound_id}, but exit_request.json on disk now hashes to "
            f"{actual}. An authorization from a different trigger does not carry "
            "over to this process. Nothing is sold; the monitor will re-request "
            "the exit if it still stands.")

    # ---- 3. the order record ------------------------------------------------
    ti = (payload or {}).get("tool_input")
    if not isinstance(ti, dict):
        return _deny(
            f"EXIT SCOPE: tool_input is {type(ti).__name__}, not an order record; "
            "refusing an order it cannot read.")

    # ---- 4. account ---------------------------------------------------------
    want_acct = str(request.get("account") or "").strip()
    got_acct = str(ti.get("account_number") or "").strip()
    if not want_acct:
        return _deny(
            "EXIT SCOPE: the exit request names no account, so this gate cannot "
            "confirm the order is going to the Agentic account. Refusing.")
    if got_acct != want_acct:
        return _deny(
            f"EXIT SCOPE: order targets account {got_acct or '(none)'}, but this "
            f"exit request authorizes account {want_acct}. Every other account is "
            "read-only to this system.")

    # ---- 5. side ------------------------------------------------------------
    side = str(ti.get("side") or "").strip().lower()
    sym = str(ti.get("symbol") or "").strip().upper()
    if side != "sell":
        return _deny(
            f"EXIT SCOPE: side is {ti.get('side')!r}. The exit executor places "
            f"SELLS ONLY — it is the hands for an exit the monitor already "
            f"decided on, and has no authority to open or add to a position. "
            f"Nothing was placed for {sym or '?'}.")

    # ---- 6. symbol ----------------------------------------------------------
    if not sym:
        return _deny("EXIT SCOPE: the order names no symbol; refusing to sell.")
    fraction = _authorized_fraction(request, sym)
    if fraction is None:
        authorized = sorted({str(e.get("symbol") or "").strip().upper()
                             for e in request["exits"] if isinstance(e, dict)} - {""})
        return _deny(
            f"EXIT SCOPE: {sym} is NOT in this exit request. This executor is "
            f"authorized to sell {', '.join(authorized) or '(nothing)'} and "
            "nothing else. Selling another holding is a portfolio decision, and "
            "this process has no portfolio authority — a trading session makes "
            "those.")
    if fraction == "malformed":
        return _deny(
            f"EXIT SCOPE: the exit entry for {sym} has an unusable `fraction`, so "
            "the size it authorizes cannot be established. Refusing to sell.")

    # ---- 7. one dispatch per (request, symbol) ------------------------------
    used = (spent or {}).get(sym)
    if used:
        return _deny(
            f"SELL {sym} REFUSED: this request's authorization for {sym} is "
            f"ALREADY SPENT — {used}. One exit request permits ONE order per "
            "symbol: it authorized an exit, not a series of them, and two "
            "sells under one authorization add up to more than was asked for "
            "(two 50% trims are a full liquidation nobody decided on). If a "
            "remainder is genuinely still open, let this run end and report it "
            "— the monitor reconciles and issues a NEW request, which is "
            "evaluated fresh against a new broker position read.")

    # ---- 8. scope -----------------------------------------------------------
    return _scope_verdict(sym, float(fraction), ti, held_by_symbol.get(sym))


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def _emit(d: dict) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": d["permissionDecision"],
        "permissionDecisionReason": d["permissionDecisionReason"]}}))


def _read_request():
    """-> the parsed request, or None if absent/unreadable.

    None is NOT "no restriction" here — decide() turns it into a deny. That is
    the opposite of the general gate's rule-out loader, and deliberately so:
    there, an absent file means nothing has been ruled out, which is a true
    statement about the world. Here, an absent file means nobody authorized this
    order.
    """
    try:
        return json.loads(EXIT_REQUEST.read_text())
    except Exception:                                       # noqa: BLE001
        return None


def _read_transcript(payload: dict) -> tuple:
    """-> (positions, spent) from this invocation's transcript. Never raises.

    An unreadable transcript yields ({}, {}), which is the SAFE pair: no
    positions means no quantity can be authorized (every exit is refused), and
    no spend record means nothing is wrongly marked used. Both halves fail in
    the direction of refusing, never of selling.
    """
    try:
        tp = (payload or {}).get("transcript_path")
        if not tp:
            return {}, {}
        p = Path(tp)
        if not p.exists():
            return {}, {}
        with p.open("r", errors="replace") as fh:
            return scan_transcript(fh, (payload or {}).get("tool_use_id"))
    except Exception:                                       # noqa: BLE001
        return {}, {}


def driver(raw: str) -> int:
    """Decide on one raw stdin payload and print the hook decision.

    FAILS CLOSED: every exception path prints a `deny` naming the exception
    type. A hook that crashes prints nothing, and a hook that prints nothing is
    read by the harness as "no opinion", which lets the order through — the one
    outcome this wrapper exists to make impossible.
    """
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError(f"hook payload is {type(payload).__name__}, not an object")
        held, spent = _read_transcript(payload)
        _emit(decide(payload, _read_request(),
                     os.environ.get(REQUEST_ID_ENV, "").strip(),
                     held, spent))
    except Exception as e:                                  # noqa: BLE001
        _emit(_deny(
            f"exit-scope gate FAILED CLOSED: {type(e).__name__}: {e} — the scope "
            "guard could not reach a verdict, so no exit order may be placed. "
            "Report this; do not retry the order."))
    return 0


if __name__ == "__main__":
    if "--hook" in sys.argv:
        sys.exit(driver(sys.stdin.read()))
    print(__doc__)
