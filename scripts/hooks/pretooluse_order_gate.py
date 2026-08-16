#!/usr/bin/env python3
"""PreToolUse hook — THE unbypassable gate in front of every live equity order.

WHAT FAILURE THIS PREVENTS
    Until this existed, every safety check the agent ran before placing an order
    was ADVISORY: `check_order()` in src/agent_env/ is a tool the agent may call,
    and nothing forces it to. An agent that forgot the step, mis-parsed the
    verdict, or simply decided to skip it could reach
    mcp__robinhood-trading__place_equity_order directly, and real money moved
    with no gate consulted at all. A PreToolUse hook runs in the HARNESS, before
    the tool call is dispatched, and its `deny` is not something the model can
    argue with or forget — so the gate stops depending on the agent's compliance.

    It is also the phased-rollout switch: `research_store/SHADOW` (same
    touch/rm pattern as `research_store/HALT`) makes the gate refuse EVERY
    order while the loop keeps running end to end, so a new procedure can be
    exercised for real with placement mechanically impossible.

WHAT IT DOES NOT COVER
    - Only `mcp__robinhood-trading__place_equity_order`. Order CANCELLATION,
      option orders, and anything the agent does outside the RH MCP are not
      seen here (option orders are denied outright in deploy/loop_settings.json).
    - It binds only sessions started with `--settings deploy/loop_settings.json`
      (the cron loops). A human's interactive session under
      `.claude/settings.json` is deliberately NOT gated by this.
    - It cannot judge the MERIT of a trade, only the mandate's mechanical
      limits. It never recomputes the signal and never opens the price panel.
    - It cannot price a market BUY expressed in SHARES (no quote is available
      to a hook that must stay under ~0.1s), so it refuses that shape rather
      than guessing — see `_notional`.
    - It is not a substitute for src/governance.gates(): the drawdown halt is
      NOT evaluated here, because `gates()` WRITES
      research_store/governance/state.json (via `update_peak`) and a hook that
      writes state on every order would ratchet a live gate as a side effect of
      being consulted. Only the write-free governance functions are called.

LATENCY BUDGET
    This runs on the critical path of every order. It may read small JSON and
    config ONLY. It must NEVER read research_store/prices/*.parquet or import
    pandas — a full cold start including imports measures ~0.1s; a parquet read
    would make that seconds, on every single order.

    The announcement (below) obeys the same budget: the phone push is a blocking
    HTTPS POST with a 10-second timeout, so it is handed to a DETACHED process
    (src/announce.push_detached) that the gate does not wait for and whose
    stdout is closed. A dead ntfy server costs this hook a fork, not ten
    seconds. See push_detached's docstring for why a thread would not do.

ANNOUNCEMENT (not a gate)
    An ALLOWED buy that is unusual — off-universe, or one that would push the
    resulting POSITION past the announce line — is pushed to the operator's
    phone as it happens, and then placed. It is NOT held for a reply: these
    sessions are headless and nobody is waiting. The push failing, being slow,
    or being switched off cannot change a verdict; the verdict is already
    printed before the announcement is attempted.

INVOCATION
    hook (deploy/loop_settings.json):  ... pretooluse_order_gate.py --hook
        reads the hook JSON on stdin, prints a PreToolUse decision, exits 0.
    selftest:                          ... pretooluse_order_gate.py [--selftest]
        with no payload on stdin, runs _selftest().
    A payload arriving on stdin is ALWAYS answered with a decision, flag or no
    flag — a misconfigured hook command must not silently print selftest text
    (which the harness would read as "no decision" and let the order through).

FAIL-CLOSED
    Any unexpected exception in the driver prints a `deny` naming the exception
    type. A hook that crashes must never be a hook that approves.
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

import governance as gov  # noqa: E402  (json/math/pathlib only — no network, no moomoo)

ORDER_TOOL = "mcp__robinhood-trading__place_equity_order"
SHADOW_FILE = REPO / "research_store" / "SHADOW"


# --------------------------------------------------------------------------- #
# decision core (pure — every input is passed in)
# --------------------------------------------------------------------------- #
def _allow(reason: str) -> dict:
    return {"permissionDecision": "allow", "permissionDecisionReason": reason}


def _deny(reason: str) -> dict:
    return {"permissionDecision": "deny", "permissionDecisionReason": reason}


def _to_float(v):
    """Parse an order field to a finite float, else None.

    The RH MCP passes `dollar_amount` / `quantity` / `limit_price` as STRINGS,
    while scripts/fast_loop.py's own plan dicts carry floats, so both shapes
    have to parse. `None` is returned for anything not finite and numeric —
    including bools, which are `int` to Python but can never be a real dollar
    amount — so an unusable value can only ever end up REFUSING a buy
    (governance._amount_invalid), never sliding through a `>` comparison the
    way NaN silently does.
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


def _notional(tool_input: dict) -> tuple[float | None, str]:
    """-> (dollar notional, how it was derived). `None` means UNSIZEABLE here.

    `amount` is the fast loop's own key; `dollar_amount` is the RH MCP's. A
    limit order priced in shares is multiplied out. A MARKET order priced in
    shares carries no price at all, and pricing it would need a quote — a
    network call this hook's latency budget forbids — so it stays None and the
    caller refuses the BUY. That shape is real and expected on the SELL side:
    RH rejects a dollar-notional order that consumes an entire position
    (EQUITY_DOLLAR_BASED_SELL_ALL_ERROR), so a full exit legitimately arrives
    as a share quantity. Sells never reach this function's verdict.
    """
    raw = tool_input.get("amount")
    if raw is None:
        raw = tool_input.get("dollar_amount")
    amt = _to_float(raw)
    if amt is not None:
        return amt, "dollar_amount"
    qty = _to_float(tool_input.get("quantity"))
    px = _to_float(tool_input.get("limit_price"))
    if qty is not None and px is not None:
        return qty * px, "quantity x limit_price"
    return None, "unsizeable"


def decide(payload: dict, cfg: dict, valued: dict, shadow: bool,
           ruled: dict | None = None) -> dict:
    """The verdict for one hook payload. Pure: reads no clock, writes nothing.

    Order of the gates, and why:

      1. not an order tool          -> allow, untouched. The matcher should
                                       already have kept us out, but a hook
                                       that mis-fires must not block unrelated
                                       work.
      2. kill switch                -> deny EVERYTHING, buy or sell. The one
                                       gate permitted to refuse an exit; the
                                       reason says so, because the human now
                                       has to sell by hand.
      3. shadow mode                -> deny everything. The rollout switch.
      4. side neither buy nor sell  -> deny. We cannot tell whether the order
                                       increases risk, so we do not guess.
      5. SELL                       -> ALLOW, always. Load-bearing: stops here
                                       are software-only (scripts/market_monitor.py
                                       IS the stop), so a gate that blocks a
                                       sell does not pause trading, it strips an
                                       open position of its only protection.
                                       Everything below this line is BUY-only,
                                       deliberately — including `live_approved`.
      6. live_approved / HALT_ENTRIES -> the ordinary entry halts.
      7. active RULE-OUT            -> deny. A session decided against this name
                                       and recorded it; that decision binds the
                                       buy side until revisit() clears it. Passed
                                       in as `ruled` so decide() stays pure.
      8. max_order_pct / whitelist  -> via write-free src/governance functions.

    `valued` is src/marks.load() (or {}). A missing/garbage account value
    becomes NaN, which governance.vet_plan already fails CLOSED on for buys
    and never applies to sells.
    """
    tool = (payload or {}).get("tool_name")
    if tool != ORDER_TOOL:
        return _allow(f"{tool!r} is not {ORDER_TOOL} — order gate does not apply")

    ti = (payload or {}).get("tool_input") or {}
    if not isinstance(ti, dict):
        return _deny(f"order gate: tool_input is {type(ti).__name__}, not an "
                     "order record; refusing an order it cannot read")
    side = str(ti.get("side") or "").strip().lower()
    sym = str(ti.get("symbol") or "").strip().upper()

    if gov.kill_switch_active(cfg):
        return _deny(
            f"KILL-SWITCH active ({cfg['governance']['kill_switch_file']} present) "
            "— the machine places no order of any kind, buy or sell. The monitor "
            "still watches and ALERTS on a breach; any exit must be placed BY HAND.")

    if shadow:
        return _deny(
            "SHADOW MODE (research_store/SHADOW present) — every order is refused, "
            "including sells, while the loop is exercised end to end. Nothing was "
            f"placed for {sym or '?'}. Journal the intent and continue; remove the "
            "SHADOW file to go live.")

    if side not in ("buy", "sell"):
        return _deny(
            f"order gate: side {ti.get('side')!r} is neither 'buy' nor 'sell' — "
            "cannot tell whether this order increases risk, so it is refused.")

    if side == "sell":
        return _allow(
            f"SELL {sym or '?'} — no gate but the kill switch may block an exit "
            "(stops here are software-only, so refusing a sell would leave the "
            "position unprotected).")

    # ---- BUY only from here down -----------------------------------------
    if not gov.live_approved(cfg):
        return _deny(
            "[proof] live_approved is false — this box is not armed to open "
            f"positions; no BUY of {sym or '?'} may be placed. (Exits stay "
            "available: live_approved is checked for buys only, for the same "
            "reason the drawdown halt is.)")

    if gov.halt_entries_active(cfg):
        return _deny(
            "HALT-ENTRIES active "
            f"({cfg['governance'].get('halt_entries_file', gov.HALT_ENTRIES_DEFAULT)}"
            f" present) — no new risk; BUY of {sym or '?'} refused. Exits stay armed.")

    # ---- automatic drawdown halt (READ-ONLY) ------------------------------
    # ⛔ THIS WAS SILENTLY LOST. The drawdown entries-halt was enforced by one
    # caller -- scripts/fast_loop.py calling gates() before placing -- and that
    # script was deleted 2026-08-14 with the procedural executor. gates() then
    # survived only inside check_order(), a tool the agent may skip, so an
    # automatic halt became advisory without anyone choosing that. Caught by the
    # independent reviewer reviewing the deletion.
    #
    # drawdown_breach() is the WRITE-FREE half of drawdown_halt(): same
    # threshold, same fail-closed behaviour on a non-finite value, but it reads
    # the stored peak instead of advancing it. That is what lets this run here
    # at all -- see this module's docstring on why gates() must not.
    av_probe = _to_float((valued or {}).get("account_value"))
    breached, dd = gov.drawdown_breach(
        av_probe if av_probe is not None else float("nan"), cfg)
    if breached:
        # .get, not [..]: this branch is also reached by a NON-FINITE account
        # value, which fails closed regardless of whether a limit is configured
        # -- and a KeyError here would crash a hook that fails closed, i.e.
        # refuse every order on the box.
        limit = cfg.get("governance", {}).get("max_drawdown")
        cap = f"{float(limit):.0%}" if limit is not None else "configured"
        shown = "unreadable" if not math.isfinite(dd) else f"{dd:.1%}"
        return _deny(
            f"DRAWDOWN HALT — account is {shown} against its peak, past the "
            f"{cap} limit; BUY of {sym or '?'} refused. Exits stay armed "
            "(this is an entries halt, never a reason to strand a position).")

    # ---- stop-out cooldown ------------------------------------------------
    # The monitor writes {SYMBOL: until-date} when a stop fires. TWO readers
    # honoured it: the slow loop (excludes cooled names at the next rebuild) and
    # scripts/fast_loop.py (refused the buy at order time). Deleting fast_loop.py
    # took the ORDER-TIME half with it, so a session could rebuy a name the
    # monitor had stopped minutes earlier and the slow loop's half would not
    # bite until the next rebuild. Fails open on an unreadable file.
    cooled = gov.cooldown_until(sym)
    if cooled:
        return _deny(
            f"COOLDOWN — {sym} was stopped out recently and is cooling until "
            f"{cooled}; BUY refused. The stop that fired is the reason: rebuying "
            "into it churns the position against a book that has not rebuilt "
            "yet. (Exits are unaffected — a cooldown never blocks a sell.)")

    # ---- an active rule-out refuses the rebuy -----------------------------
    # The fast loop also drops these from the plan (apply_rule_outs), but the
    # loop is not the only path to place_equity_order: a session can call the
    # MCP tool directly. This is the chokepoint that does not depend on which
    # code path proposed the order, which is the whole reason the hook exists.
    ro = (ruled or {}).get(sym)
    if ro is not None:
        until = f", until {ro['until']}" if ro.get("until") else ""
        return _deny(
            f"BUY {sym or '?'} refused: a session RULED THIS OUT on "
            f"{str(ro.get('ts', ''))[:10]}{until} — "
            f"{ro.get('reason', '(no reason recorded)')}. That decision binds the "
            "buy side until a session calls revisit() with a stated reason. "
            "(Exits are unaffected: a rule-out never blocks a sell.)")

    amount, how = _notional(ti)
    if amount is None:
        return _deny(
            f"BUY {sym or '?'} cannot be sized: no usable dollar_amount, and no "
            "quantity x limit_price to multiply out "
            f"(quantity={ti.get('quantity')!r}, limit_price={ti.get('limit_price')!r}). "
            "This gate must not fetch a quote, so it refuses rather than guessing. "
            "Place buys as a dollar amount.")

    av = _to_float((valued or {}).get("account_value"))
    if av is None:
        av = float("nan")   # vet_plan fails CLOSED on a non-finite value, buys only

    approved, blocked = gov.vet_plan(
        [{"symbol": sym, "side": "buy", "amount": amount}], av, cfg)
    if blocked:
        return _deny(f"BUY {sym or '?'} refused by governance.vet_plan: "
                     f"{blocked[0]['blocked']}")

    return _allow(f"BUY {sym} ${amount:,.2f} ({how}) clears the kill switch, "
                  f"HALT_ENTRIES, live_approved and vet_plan "
                  f"(cap {cfg['governance']['max_order_pct']:.0%} of "
                  f"${av:,.2f}).")


# --------------------------------------------------------------------------- #
# announcement (AFTER the verdict, never part of it)
# --------------------------------------------------------------------------- #
def _journal_announcement(symbol: str, reason: str, amount) -> None:
    """Append the announcement to the journal. Never raises, never slow.

    Loads research_store.store BY FILE PATH rather than importing the package.
    `from research_store import store` runs the package __init__, which pulls the
    models/validation/ledger chain and costs ~70ms -- most of this gate's ~0.1s
    budget, paid on the critical path of every order. store.py itself is
    stdlib-only (json/os/tempfile/pathlib) and derives its paths from __file__,
    so loading the module alone is both cheap and exact. Reusing it rather than
    re-writing the append keeps ONE journal format: a hand-rolled duplicate here
    would drift the day store.py changes, and drift in the audit trail is the
    failure this whole function exists to prevent.
    """
    try:
        import importlib.util                     # noqa: PLC0415
        spec = importlib.util.spec_from_file_location(
            "_gate_store", REPO / "src" / "research_store" / "store.py")
        store = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(store)
        store.append_journal({
            "event": "gate_announcement",
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symbol": symbol, "side": "buy", "reason": reason,
            "amount": amount, "source": "order_gate",
        })
    except Exception:     # noqa: BLE001 — an audit line must never affect an order
        pass


def announce_if_unusual(payload: dict, decision: dict, cfg: dict, valued: dict) -> str | None:
    """Push an ALLOWED-but-unusual buy to the operator's phone. -> the reason, or None.

    Deliberately NOT part of decide(): decide() is pure and its verdict must not
    depend on whether a phone alert succeeded. This runs after the decision has
    already been printed, and its return value changes nothing — an order that is
    announced is placed exactly as an order that is not.

    ⚠️ ANNOUNCE-FIRST IS NOT APPROVAL-FIRST. The operator sees the message as the
    order goes out; nothing waits for an answer, because these sessions run under
    cron with nobody at the other end. The real intervention is the kill switch,
    used afterwards.

    Only ALLOWED orders are announced. A refusal is not an unusual action taken —
    it is an action prevented, and the agent is already told why in the deny
    reason. Note that with [governance] require_whitelist on, an off-universe BUY
    is DENIED by vet_plan and so never reaches this function; the off-universe
    limb of needs_announcement is live only when that whitelist is off. The
    concentration limb fires under either setting, because the gate caps a single
    ORDER and never the resulting POSITION.

    Never raises, and never delays: the push is handed to a detached process.
    """
    try:
        if (decision or {}).get("permissionDecision") != "allow":
            return None
        if (payload or {}).get("tool_name") != ORDER_TOOL:
            return None
        ti = (payload or {}).get("tool_input") or {}
        if not isinstance(ti, dict):
            return None
        if str(ti.get("side") or "").strip().lower() != "buy":
            return None       # a SELL is never announced; see src/announce.py

        import announce as ann        # noqa: PLC0415 — off the import path of decide()
        import mandate                # noqa: PLC0415 — stdlib-only (tomllib), no pandas

        amount, _how = _notional(ti)
        sym = str(ti.get("symbol") or "").strip().upper()
        try:
            uni = gov.whitelist(cfg)
        except Exception:
            uni = None     # unreadable universe: needs_announcement then SKIPS the
                           # off-list limb rather than firing on every symbol — and,
                           # critically, the concentration limb still runs.
        reason = ann.needs_announcement(
            {"symbol": sym, "side": "buy", "amount": amount},
            valued or {}, uni, mandate.load())
        if not reason:
            return None
        # ⛔ A PROBE MUST NOT PAGE. Exercising this gate -- a latency
        # measurement, a debug run, a payload replayed by hand -- runs this
        # function for real against the live ntfy topic, and the body below says
        # "The order was PLACED". On 2026-08-12 a latency probe sent the operator
        # three texts claiming an AMAT buy that never existed; no order was
        # placed (this hook only DECIDES -- placement is a separate MCP call),
        # but the alert asserted otherwise. An alert channel that cries wolf when
        # someone measures it is worse than no channel, because the one real
        # alert is then read as another test. GATE_PROBE=1 keeps the full
        # decision path, timing included, and severs delivery only.
        if os.environ.get("GATE_PROBE"):
            return reason

        # "APPROVED and being sent", never "PLACED". This runs BEFORE the order
        # reaches Robinhood -- it allows the call, and placement happens after
        # and can still fail there. Claiming the order was placed states as fact
        # something the gate cannot observe, and an alert channel that overstates
        # is one the operator learns to discount.
        body = (f"{reason}.\n\nThe order was APPROVED and is being sent — this "
                f"is a notification, not an approval request. To intervene, use "
                f"the kill switch.")

        # JOURNAL BEFORE PUSHING. The push is fire-and-forget by design (see
        # push_detached) -- which means the gate never learns whether it landed.
        # If the phone is the only record, a dropped alert is indistinguishable
        # from an order that was never unusual, and NOBODY is watching a 09:30
        # cron run in real time. The journal line is the durable trace; the push
        # is best-effort delivery of it. Failure here must not touch the order.
        _journal_announcement(sym, reason, amount)

        ann.push_detached(f"UNUSUAL BUY: {sym}", body, tags="warning")
        return reason
    except Exception:     # noqa: BLE001 — an alert must never affect an order
        return None


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
RULED_OUT_FILE = REPO / "research_store" / "memory" / "ruled_out.jsonl"


def _load_ruled() -> dict:
    """Active rule-outs for the gate. Small JSONL — inside the latency budget.

    ABSENT FILE -> {}. Nothing has ever been ruled out; that is a true statement
    about the world, not a swallowed error, and denying every buy because a file
    the agent has not written yet does not exist would be an outage.

    PRESENT BUT UNREADABLE -> RAISES, and driver() turns that into a deny. This
    asymmetry is deliberate. The failure being fixed here is a guard that existed
    on paper and bound nothing; "the record is there but I could not read it, so
    I bought anyway" is that same failure wearing a different hat.
    """
    if not RULED_OUT_FILE.exists():
        return {}
    from agent_env import memory                  # noqa: PLC0415 — off decide()'s path
    return memory.binding_rule_outs(REPO / "research_store")


def _emit(d: dict) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": d["permissionDecision"],
        "permissionDecisionReason": d["permissionDecisionReason"]}}))


def driver(raw: str) -> int:
    """Decide on one raw stdin payload and print the hook decision.

    FAILS CLOSED: every exception path — unparseable JSON, an unreadable
    config, a bug in decide() — prints a `deny` naming the exception type. A
    hook that crashes prints nothing, and a hook that prints nothing is read by
    the harness as "no opinion", which lets the order through. That is the one
    outcome this wrapper exists to make impossible.
    """
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError(f"hook payload is {type(payload).__name__}, not an object")
        import strategy          # noqa: PLC0415 — kept off the import path of decide()
        import marks             # noqa: PLC0415
        cfg = strategy.load()
        valued = marks.load() or {}
        d = decide(payload, cfg, valued, SHADOW_FILE.exists(), _load_ruled())
        _emit(d)                          # the verdict goes out FIRST, always
        announce_if_unusual(payload, d, cfg, valued)
    except Exception as e:       # noqa: BLE001 — deliberate catch-all, see docstring
        _emit(_deny(
            f"order gate FAILED CLOSED: {type(e).__name__}: {e} — the safety gate "
            "could not reach a verdict, so no order may be placed. Report this; "
            "do not retry the order."))
    return 0


# --------------------------------------------------------------------------- #
def _selftest() -> None:
    """Runs against a TEMPORARY repo root (governance reads REPO at call time),
    so the assertions describe the CODE, not whatever HALT/SHADOW files happen
    to exist on the live box today.

    The load-bearing assertions are the ones proving a SELL is refused by
    nothing but the kill switch, and that a crash denies.
    """
    import tempfile
    _repo = gov.REPO
    try:
        with tempfile.TemporaryDirectory() as d:
            gov.REPO = Path(d)
            (gov.REPO / "research_store").mkdir(parents=True)
            (gov.REPO / "config").mkdir()
            (gov.REPO / "config" / "universe.csv").write_text("ticker,flag\nMU,\nAAPL,\n")
            (gov.REPO / "config" / "etf_universe.csv").write_text("ticker,flag\nXLK,\n")

            CFG = {"governance": {"kill_switch_file": "research_store/HALT",
                                  "halt_entries_file": "research_store/HALT_ENTRIES",
                                  "max_order_pct": 0.15, "require_whitelist": False},
                   "proof": {"live_approved": True}}
            V = {"account_value": 100.0, "buying_power": 50.0}
            buy = {"tool_name": ORDER_TOOL,
                   "tool_input": {"symbol": "MU", "side": "buy", "amount": 10.0}}
            sell = {**buy, "tool_input": {"symbol": "MU", "side": "sell", "amount": 10.0}}

            # shadow mode denies EVERYTHING, including sells
            d_ = decide(buy, CFG, V, shadow=True)
            assert d_["permissionDecision"] == "deny" and "shadow" in d_["permissionDecisionReason"].lower()
            assert decide(sell, CFG, V, shadow=True)["permissionDecision"] == "deny"

            # live mode allows an ordinary buy
            assert decide(buy, CFG, V, shadow=False)["permissionDecision"] == "allow"

            # a non-order tool is never touched by this hook
            other = {"tool_name": "mcp__agentic-trader__quote", "tool_input": {}}
            assert decide(other, CFG, V, shadow=False)["permissionDecision"] == "allow"

            # an order exceeding max_order_pct is denied
            big = {**buy, "tool_input": {"symbol": "MU", "side": "buy", "amount": 90.0}}
            assert decide(big, CFG, V, shadow=False)["permissionDecision"] == "deny"

            # A SELL IS NEVER BLOCKED on anything but the kill switch: the monitor's stop
            # is software-only, so blocking a sell removes a position's only protection.
            huge_sell = {**buy, "tool_input": {"symbol": "MU", "side": "sell", "amount": 90.0}}
            assert decide(huge_sell, CFG, V, shadow=False)["permissionDecision"] == "allow"

            # --- the RH wire format: dollar_amount/quantity arrive as STRINGS ---
            wire_buy = {"tool_name": ORDER_TOOL,
                        "tool_input": {"account_number": "X", "symbol": "MU",
                                       "side": "buy", "type": "market",
                                       "dollar_amount": "10.00"}}
            assert decide(wire_buy, CFG, V, shadow=False)["permissionDecision"] == "allow"
            wire_big = {**wire_buy, "tool_input": {**wire_buy["tool_input"],
                                                   "dollar_amount": "90.00"}}
            assert decide(wire_big, CFG, V, shadow=False)["permissionDecision"] == "deny"

            # a SHARE-quantity BUY cannot be priced without a quote -> refused,
            # never guessed, and never silently allowed.
            qty_buy = {"tool_name": ORDER_TOOL,
                       "tool_input": {"symbol": "MU", "side": "buy",
                                      "type": "market", "quantity": "3"}}
            r = decide(qty_buy, CFG, V, shadow=False)
            assert r["permissionDecision"] == "deny" and "sized" in r["permissionDecisionReason"], r
            # ...but a LIMIT buy in shares is sizeable, and the cap still applies
            lim_ok = {"tool_name": ORDER_TOOL,
                      "tool_input": {"symbol": "MU", "side": "buy", "type": "limit",
                                     "quantity": "1", "limit_price": "10.00"}}
            assert decide(lim_ok, CFG, V, shadow=False)["permissionDecision"] == "allow"
            lim_big = {**lim_ok, "tool_input": {**lim_ok["tool_input"], "quantity": "9"}}
            assert decide(lim_big, CFG, V, shadow=False)["permissionDecision"] == "deny"

            # SELL-ALL arrives as a share quantity (RH rejects a dollar-notional
            # order that consumes a whole position: EQUITY_DOLLAR_BASED_SELL_ALL_ERROR).
            # It must pass, not crash and not be denied for being unsizeable.
            qty_sell = {"tool_name": ORDER_TOOL,
                        "tool_input": {"symbol": "MU", "side": "sell",
                                       "type": "market", "quantity": "3.141592"}}
            assert decide(qty_sell, CFG, V, shadow=False)["permissionDecision"] == "allow"

            # --- the kill switch is the ONE gate that may refuse an exit -------
            (gov.REPO / "research_store" / "HALT").touch()
            for p in (buy, sell, qty_sell):
                r = decide(p, CFG, V, shadow=False)
                assert r["permissionDecision"] == "deny", (p, r)
                assert "KILL-SWITCH" in r["permissionDecisionReason"], r
                assert "BY HAND" in r["permissionDecisionReason"], r
            (gov.REPO / "research_store" / "HALT").unlink()

            # --- HALT_ENTRIES: buys refused, exits still armed ----------------
            (gov.REPO / "research_store" / "HALT_ENTRIES").touch()
            assert decide(buy, CFG, V, shadow=False)["permissionDecision"] == "deny"
            assert decide(sell, CFG, V, shadow=False)["permissionDecision"] == "allow", \
                "HALT_ENTRIES must never block a sell"
            assert decide(qty_sell, CFG, V, shadow=False)["permissionDecision"] == "allow"
            (gov.REPO / "research_store" / "HALT_ENTRIES").unlink()

            # --- live_approved gates BUYS ONLY --------------------------------
            OFF = {**CFG, "proof": {"live_approved": False}}
            assert decide(buy, OFF, V, shadow=False)["permissionDecision"] == "deny"
            assert decide(sell, OFF, V, shadow=False)["permissionDecision"] == "allow", \
                "live_approved must never strand an exit"

            # --- the AUTOMATIC DRAWDOWN HALT, restored to the critical path ---
            # It was enforced only by fast_loop.py calling gates(); that script
            # was deleted with the procedural executor and the halt became
            # advisory (check_order is skippable). Buys only, exits never.
            gov.STATE.parent.mkdir(parents=True, exist_ok=True)
            gov.STATE.write_text(json.dumps({"peak_value": 100.0}))
            DD = {**CFG, "governance": {**CFG["governance"], "max_drawdown": 0.25}}
            deep = {"account_value": 70.0}      # -30% against a peak of 100
            r = decide(buy, DD, deep, shadow=False)
            assert r["permissionDecision"] == "deny", r
            assert "DRAWDOWN HALT" in r["permissionDecisionReason"], r
            assert decide(sell, DD, deep, shadow=False)["permissionDecision"] == "allow", \
                "a drawdown halt must never strand an exit"
            assert decide(qty_sell, DD, deep, shadow=False)["permissionDecision"] == "allow"
            # inside the limit -> ordinary behaviour
            assert decide(buy, DD, {"account_value": 90.0},
                          shadow=False)["permissionDecision"] == "allow"
            # ...and it must not have ADVANCED the peak by being consulted --
            # that ratcheting is precisely why gates() is barred from this hook
            assert json.loads(gov.STATE.read_text())["peak_value"] == 100.0, \
                "the gate wrote state; drawdown_breach must be read-only"
            # a corrupted account value fails CLOSED for buys, open for exits
            r = decide(buy, DD, {"account_value": float("nan")}, shadow=False)
            assert r["permissionDecision"] == "deny", r
            assert decide(sell, DD, {"account_value": float("nan")},
                          shadow=False)["permissionDecision"] == "allow"
            gov.STATE.unlink(missing_ok=True)

            # --- STOP-OUT COOLDOWN, restored to the order path ---------------
            # The monitor writes this when a stop fires; fast_loop.py used to
            # refuse the buy at order time and was deleted, leaving only the
            # slow loop's next-rebuild half.
            cdp = gov.REPO / "research_store" / "monitor"
            cdp.mkdir(parents=True, exist_ok=True)
            (cdp / "cooldown.json").write_text(json.dumps({"MU": "2099-01-01"}))
            r = decide(buy, CFG, V, shadow=False)
            assert r["permissionDecision"] == "deny", r
            assert "COOLDOWN" in r["permissionDecisionReason"], r
            assert decide(sell, CFG, V, shadow=False)["permissionDecision"] == "allow", \
                "a cooldown must never block a sell"
            assert decide(qty_sell, CFG, V, shadow=False)["permissionDecision"] == "allow"
            # an EXPIRED cooldown does not block
            (cdp / "cooldown.json").write_text(json.dumps({"MU": "2000-01-01"}))
            assert decide(buy, CFG, V, shadow=False)["permissionDecision"] == "allow"
            # a different symbol is untouched
            (cdp / "cooldown.json").write_text(json.dumps({"AAPL": "2099-01-01"}))
            assert decide(buy, CFG, V, shadow=False)["permissionDecision"] == "allow"
            # a TORN file fails OPEN — a guard that refuses buys because it
            # cannot read a file is an outage, not a guard
            (cdp / "cooldown.json").write_text("{not json")
            assert decide(buy, CFG, V, shadow=False)["permissionDecision"] == "allow"
            (cdp / "cooldown.json").unlink()
            assert decide(buy, CFG, V, shadow=False)["permissionDecision"] == "allow"

            # --- an active RULE-OUT refuses the rebuy, and never an exit ------
            # The failure: a session exited AMAT to be flat into earnings and
            # recorded it; the loop rebought it the next morning three days
            # running because nothing read the record. The loop now drops it from
            # the plan, and this gate refuses it no matter WHO proposes the order.
            RO = {"AMAT": {"ts": "2026-08-12T20:10:00+00:00",
                           "reason": "flat into earnings", "until": "2026-08-15"}}
            ro_buy = {"tool_name": ORDER_TOOL,
                      "tool_input": {"symbol": "AMAT", "side": "buy", "amount": 5.28}}
            r = decide(ro_buy, CFG, V, shadow=False, ruled=RO)
            assert r["permissionDecision"] == "deny", r
            assert "RULED THIS OUT" in r["permissionDecisionReason"], r
            assert "flat into earnings" in r["permissionDecisionReason"], r
            assert "2026-08-15" in r["permissionDecisionReason"], r
            # ⚠️ A RULE-OUT MUST NEVER STRAND AN EXIT
            ro_sell = {**ro_buy, "tool_input": {"symbol": "AMAT", "side": "sell",
                                                "amount": 5.28}}
            assert decide(ro_sell, CFG, V, shadow=False, ruled=RO)["permissionDecision"] \
                == "allow", "a rule-out must never block a sell"
            ro_qty_sell = {**ro_buy, "tool_input": {"symbol": "AMAT", "side": "sell",
                                                    "type": "market", "quantity": "0.0103"}}
            assert decide(ro_qty_sell, CFG, V, shadow=False, ruled=RO)["permissionDecision"] \
                == "allow", "a full exit of a ruled-out name must pass"
            # an unrelated name is untouched
            assert decide(buy, CFG, V, shadow=False, ruled=RO)["permissionDecision"] == "allow"
            # a degenerate record still blocks — `is not None`, not truthiness
            assert decide(ro_buy, CFG, V, shadow=False,
                          ruled={"AMAT": {}})["permissionDecision"] == "deny"
            # no rule-outs at all -> exactly the old behaviour
            assert decide(ro_buy, CFG, V, shadow=False, ruled={})["permissionDecision"] == "allow"
            assert decide(ro_buy, CFG, V, shadow=False)["permissionDecision"] == "allow"
            # the kill switch still outranks it, and still refuses the exit
            (gov.REPO / "research_store" / "HALT").touch()
            assert "KILL-SWITCH" in decide(ro_buy, CFG, V, shadow=False,
                                           ruled=RO)["permissionDecisionReason"]
            (gov.REPO / "research_store" / "HALT").unlink()

            # --- whitelist is BUY-ONLY (mirrors governance.vet_plan) ----------
            WL = {**CFG, "governance": {**CFG["governance"], "require_whitelist": True},
                  "universe": {"source": "config/universe.csv"},
                  "etf_sleeve": {"source": "config/etf_universe.csv"}}
            off_uni_buy = {"tool_name": ORDER_TOOL,
                           "tool_input": {"symbol": "NFLX", "side": "buy", "amount": 5.0}}
            off_uni_sell = {**off_uni_buy,
                            "tool_input": {"symbol": "NFLX", "side": "sell", "amount": 5.0}}
            assert decide(off_uni_buy, WL, V, shadow=False)["permissionDecision"] == "deny"
            assert decide(off_uni_sell, WL, V, shadow=False)["permissionDecision"] == "allow", \
                "an off-universe SELL must never be refused"
            assert decide(buy, WL, V, shadow=False)["permissionDecision"] == "allow"

            # --- garbage in must never approve a BUY --------------------------
            for bad in (float("nan"), float("inf"), None, "not-a-number", True, {}):
                p = {"tool_name": ORDER_TOOL,
                     "tool_input": {"symbol": "MU", "side": "buy", "amount": bad}}
                assert decide(p, CFG, V, shadow=False)["permissionDecision"] == "deny", bad
                s = {"tool_name": ORDER_TOOL,
                     "tool_input": {"symbol": "MU", "side": "sell", "amount": bad}}
                assert decide(s, CFG, V, shadow=False)["permissionDecision"] == "allow", bad
            # an unknown account value (no snapshot) blocks buys, never sells
            for bad_v in ({}, {"account_value": None}, {"account_value": float("nan")}):
                assert decide(buy, CFG, bad_v, shadow=False)["permissionDecision"] == "deny", bad_v
                assert decide(sell, CFG, bad_v, shadow=False)["permissionDecision"] == "allow", bad_v
            # a side we do not recognise is refused, not guessed at
            for bad_side in ("short", "", None, "BUYY"):
                p = {"tool_name": ORDER_TOOL,
                     "tool_input": {"symbol": "MU", "side": bad_side, "amount": 1.0}}
                assert decide(p, CFG, V, shadow=False)["permissionDecision"] == "deny", bad_side
            # a malformed tool_input is refused rather than crashing
            assert decide({"tool_name": ORDER_TOOL, "tool_input": "buy MU"},
                          CFG, V, shadow=False)["permissionDecision"] == "deny"
            # case/whitespace on a legitimate side must still be understood
            for ok_side in ("BUY", " buy ", "Buy"):
                p = {"tool_name": ORDER_TOOL,
                     "tool_input": {"symbol": "MU", "side": ok_side, "amount": 10.0}}
                assert decide(p, CFG, V, shadow=False)["permissionDecision"] == "allow", ok_side

            # --- the ANNOUNCEMENT: fires on an ALLOWED unusual buy, changes
            #     nothing, and NEVER touches the network in a test -------------
            import announce as ann                       # noqa: PLC0415
            sent = []
            real_push = ann.push_detached
            ann.push_detached = lambda *a, **k: sent.append((a, k)) or True
            # capture the journal line instead of appending to the LIVE journal
            jrnl = []
            real_jrnl = globals()["_journal_announcement"]
            globals()["_journal_announcement"] = \
                lambda sym, reason, amt: jrnl.append((sym, reason, amt))
            # the announcement needs the universe SOURCES even when the blocking
            # whitelist is off — off-list is announceable, not refusable, there
            ACFG = {**CFG, "universe": {"source": "config/universe.csv"},
                    "etf_sleeve": {"source": "config/etf_universe.csv"}}
            try:
                # real mandate: 15% concentration -> announce at 80% of it = 12%
                VP = {"account_value": 100.0,
                      "positions": {"MU": {"qty": 1, "value": 10.0}}}
                small = {"tool_name": ORDER_TOOL,
                         "tool_input": {"symbol": "MU", "side": "buy", "amount": 1.0}}
                d_small = decide(small, CFG, VP, shadow=False)
                assert d_small["permissionDecision"] == "allow"
                assert announce_if_unusual(small, d_small, ACFG, VP) is None, \
                    "an ordinary buy must not page anybody"
                assert sent == []
                assert jrnl == [], "an ordinary buy must not litter the journal"

                # ...and the SAME $10 already held plus a $3 add crosses $12.
                # The order gate passes both (each is far under max_order_pct);
                # only the RESULTING POSITION is unusual.
                add = {"tool_name": ORDER_TOOL,
                       "tool_input": {"symbol": "MU", "side": "buy", "amount": 3.0}}
                d_add = decide(add, CFG, VP, shadow=False)
                assert d_add["permissionDecision"] == "allow", d_add
                r = announce_if_unusual(add, d_add, ACFG, VP)
                assert r and "already held" in r, r
                assert len(sent) == 1, sent
                body = sent[0][0][1]
                assert "APPROVED and is being sent" in body, body
                assert "PLACED" not in body, "the gate cannot know the order landed"
                assert "not an approval request" in body, body
                # the DURABLE half: the same announcement is journalled, and
                # journalled BEFORE the push (which is fire-and-forget and can
                # be silently lost -- the trace must not depend on it landing).
                assert len(jrnl) == 1 and jrnl[0][0] == "MU", jrnl
                assert jrnl[0][1] == r and jrnl[0][2] == 3.0, jrnl
                sent.clear(); jrnl.clear()

                # an off-universe BUY, when the whitelist gate is OFF (with it on,
                # vet_plan denies and there is nothing to announce)
                off = {"tool_name": ORDER_TOOL,
                       "tool_input": {"symbol": "NFLX", "side": "buy", "amount": 1.0}}
                d_off = decide(off, CFG, VP, shadow=False)
                assert d_off["permissionDecision"] == "allow"
                assert "OUTSIDE" in (announce_if_unusual(off, d_off, ACFG, VP) or "")
                assert len(jrnl) == 1 and jrnl[0][0] == "NFLX", jrnl
                sent.clear(); jrnl.clear()

                # ⚠️ a SELL is never announced, at any size
                big_sell = {"tool_name": ORDER_TOOL,
                            "tool_input": {"symbol": "NFLX", "side": "sell", "amount": 90.0}}
                d_sell = decide(big_sell, CFG, VP, shadow=False)
                assert d_sell["permissionDecision"] == "allow"
                assert announce_if_unusual(big_sell, d_sell, ACFG, VP) is None
                assert sent == []

                # a DENIED order is not announced — nothing happened to report
                d_shadow = decide(add, CFG, VP, shadow=True)
                assert d_shadow["permissionDecision"] == "deny"
                assert announce_if_unusual(add, d_shadow, ACFG, VP) is None
                assert sent == []

                # a broken announcement can NEVER change or break a verdict
                def boom(*a, **k):
                    raise RuntimeError("ntfy exploded")
                ann.push_detached = boom
                assert announce_if_unusual(add, d_add, ACFG, VP) is None
                assert decide(add, CFG, VP, shadow=False)["permissionDecision"] == "allow"
                # THE WHOLE POINT: the push died and the record still exists.
                assert len(jrnl) == 1 and jrnl[0][0] == "MU", jrnl
                jrnl.clear()

                # the REAL journal helper swallows its own failures, so a
                # broken store can never reach the caller. Exercise it directly
                # with a payload json.dumps cannot serialise.
                real_jrnl(object(), "unserialisable", object())   # must not raise

                # GATE_PROBE severs delivery while leaving the verdict and the
                # reason intact -- so the gate can be measured without paging.
                ann.push_detached = boom      # would explode if delivery ran
                globals()["_journal_announcement"] = real_jrnl
                os.environ["GATE_PROBE"] = "1"
                try:
                    assert announce_if_unusual(add, d_add, ACFG, VP) == r, \
                        "a probe must still compute the reason"
                finally:
                    os.environ.pop("GATE_PROBE", None)
            finally:
                ann.push_detached = real_push
                globals()["_journal_announcement"] = real_jrnl

            # --- the driver FAILS CLOSED on anything it cannot parse ----------
            import io
            import contextlib
            for raw in ("", "not json", "[]", "null", '{"tool_name": null}'):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = driver(raw)
                assert rc == 0, raw
                out = json.loads(buf.getvalue())["hookSpecificOutput"]
                assert out["hookEventName"] == "PreToolUse"
                if raw == '{"tool_name": null}':      # parses fine, just not our tool
                    assert out["permissionDecision"] == "allow", raw
                else:
                    assert out["permissionDecision"] == "deny", raw
                    assert "FAILED CLOSED" in out["permissionDecisionReason"], raw
    finally:
        gov.REPO = _repo
    print("pretooluse_order_gate: OK")


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--hook" in argv:
        sys.exit(driver(sys.stdin.read()))
    if "--selftest" in argv:      # never touch stdin: deploy/run_selftests.sh
        _selftest()               # inherits whatever stdin the suite was run with
        sys.exit(0)
    # No flag: selftest mode — UNLESS a payload actually arrived on stdin, in
    # which case this is a misconfigured hook command and it must still answer
    # with a decision (printing selftest text would read as "no opinion" and let
    # the order through).
    raw = "" if sys.stdin.isatty() else sys.stdin.read()
    if raw.strip():
        sys.exit(driver(raw))
    _selftest()
