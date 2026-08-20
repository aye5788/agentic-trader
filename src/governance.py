"""Governance guardrails (docs/DESIGN.md Layer 5) — the last gate before a live
order. The Research Store [risk] mandate validates the research PRODUCT on write;
this validates EXECUTION at trade time — the checks that must hold even when the
book itself is fine.

TWO TIERS, and the distinction is load-bearing (2026-08-09):

  block_all      no order of ANY kind — buy or sell. The machine stops touching
                 the account and the human takes over. Only the kill switch
                 (research_store/HALT) does this.
  block_entries  no RISK-INCREASING order. Buys/adds are refused; sells and
                 exits stay available. HALT_ENTRIES and the drawdown halt.

Why the split: stops in this system are SOFTWARE-ONLY (scripts/market_monitor.py
places the sell; the broker holds no native stop order, because RH has none for
fractional shares). So a gate that blocks sells does not merely pause trading —
it removes the only protection an open position has. "Halted" must never quietly
mean "unprotected".

The kill switch therefore keeps its documented meaning — the machine places
NOTHING — but it is no longer *blind*: the monitor keeps polling and PHONE-ALERTS
every breach so the human can sell by hand. Previously it returned early and
watched nothing at all.

The drawdown halt moved from block_all to block_entries to match what
config/strategy.toml has always documented it as ("halt new buys if account down
>25%"). Being deep in a drawdown and unable to exit is the worst reachable state.

  - kill-switch : research_store/HALT          -> block_all
  - halt-entries: research_store/HALT_ENTRIES  -> block_entries
  - drawdown    : down > max_drawdown from peak -> block_entries
  - order caps  : reject any single BUY over max_order_pct of account value
  - whitelist   : only ever BUY names in the configured universe (a SELL is
                  never blocked by it — exiting a name you already hold must
                  never be refused by a universe list)
  - live switch : [proof] live_approved gates PLACEMENT entirely (proof gate §9)

Pure functions + a tiny JSON state file for the drawdown peak. No broker I/O —
the fast loop feeds in account value + plan and acts on the verdict. Params live
in [governance] / [proof] in config/strategy.toml (the source of truth).

Run the tests:  .venv/bin/python src/governance.py --selftest
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATE = REPO / "research_store" / "governance" / "state.json"

# Fallback only — config/strategy.toml [governance] is the source of truth.
HALT_ENTRIES_DEFAULT = "research_store/HALT_ENTRIES"


def kill_switch_active(cfg) -> bool:
    """research_store/HALT — the machine places NO order, buy or sell."""
    return (REPO / cfg["governance"]["kill_switch_file"]).exists()


def halt_entries_active(cfg) -> bool:
    """research_store/HALT_ENTRIES — no new risk; exits stay armed."""
    return (REPO / cfg["governance"].get("halt_entries_file",
                                         HALT_ENTRIES_DEFAULT)).exists()


def live_approved(cfg) -> bool:
    """The master switch. Fast loop may PLACE orders only when this is true."""
    return bool(cfg.get("proof", {}).get("live_approved", False))


def _load_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {"peak_value": 0.0}


def update_peak(account_value: float) -> dict:
    """Maintains THIS GATE'S OWN peak tracker — research_store/governance/state.json.

    NOT the mandate's high-water mark (2026-08-10, FIX A — see docs/OPSLOG.md and
    the design spec §5, criterion 1). Two candidate sources of "the account's
    all-time peak" exist in this repo and they read differently RIGHT NOW
    (this file's persisted peak_value=82.22 vs max(research_store/history/
    equity.jsonl "value")=81.99): this one is sampled at whatever moment
    scripts/fast_loop.py happens to call gates()/drawdown_halt() (today, once
    per weekday run, but nothing stops a manual/test invocation from seeding it
    with an intraday or off-cycle mark that scripts/log_equity.py's daily
    close-to-close append never records) — so it is NOT reproducible from
    anything on disk and NOT audited against a fixed cadence.

    DECISION: src/mandate.py's drawdown() — which recomputes max() fresh from
    research_store/history/equity.jsonl on every call — is the ONLY authoritative
    high-water mark for anything that judges the mandate or flattens the book.
    This tracker keeps its narrower, pre-existing job: seeding THIS gate's own
    [governance] max_drawdown entries-halt (a different threshold, 0.25 here vs
    0.20 in config/mandate.toml, for a different action — block_entries only,
    never a flatten). The two are allowed to read differently because they now
    answer different questions for different gates; what they must never do is
    both claim to be *the* peak that authorises liquidating the book. Nothing in
    src/mandate.py reads this file, and nothing computing a number for a human
    (dashboard/app.py) reads it either as of this fix — both now source their
    peak from the equity log, same as mandate.drawdown(). Do not wire this
    tracker into the mandate path; if the two gates are ever merged, merge them
    onto the equity-log peak, not this one.
    """
    st = _load_state()
    st["peak_value"] = max(float(st.get("peak_value", 0.0)), float(account_value))
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=2))
    return st


def drawdown_halt(account_value: float, cfg) -> tuple[bool, float]:
    """(halted?, current_drawdown). Updates the peak as a side effect.

    FAILS CLOSED on a non-finite account_value (NaN/+inf/-inf): every direct
    comparison against NaN evaluates False, so `dd < -abs(max_drawdown)` would
    silently read as "not halted" for a corrupted reading — the exact opposite
    of what a corrupted number should produce. A non-finite value is therefore
    treated as an automatic halt (of ENTRIES — see gates()), and it is NEVER
    passed to update_peak(): `max(stored_peak, nan)` happens to return the
    first argument today, but that is luck, not a guarantee, and a poisoned
    peak would corrupt every future drawdown measurement permanently.

    The peak this measures drawdown against is update_peak()'s own tracker, NOT
    the mandate's high-water mark — see update_peak()'s docstring (FIX A,
    2026-08-10) for why the two are intentionally allowed to diverge and which
    one is authoritative for the mandate/flatten path (it is not this one).
    """
    if not math.isfinite(account_value):
        return True, float("nan")
    peak = update_peak(account_value)["peak_value"] or float(account_value)
    dd = 0.0 if peak <= 0 else (float(account_value) / peak - 1.0)
    return dd < -abs(cfg["governance"]["max_drawdown"]), dd


def drawdown_breach(account_value: float, cfg) -> tuple[bool, float]:
    """(breached?, drawdown) against the STORED peak. READS ONLY — never writes.

    ⛔ WHY THIS EXISTS. The automatic drawdown entries-halt was enforced by
    exactly one caller: scripts/fast_loop.py invoking gates() before it placed.
    That script was deleted on 2026-08-14 when the procedural executor was
    retired, and the halt went with it -- gates() survives only in
    check_order(), which is a tool the agent MAY call and can skip. The
    unbypassable PreToolUse hook deliberately does not call gates(), because
    gates() -> drawdown_halt() -> update_peak() WRITES, and a hook that writes
    state on every order would ratchet a live gate as a side effect of being
    consulted.

    So the halt became advisory without anyone deciding it should. Found by the
    independent reviewer reviewing that deletion.

    This is the read-only half: same comparison, same threshold, same
    fail-closed behaviour on a non-finite value -- but it takes the peak as it
    is on disk instead of advancing it. The hook can call this on the critical
    path; the peak keeps being maintained by whoever calls gates() (check_order,
    and the session runner at session start).

    A peak that is stale can only ever UNDERSTATE the drawdown, never overstate
    it, so this cannot invent a halt that gates() would not also raise.

    ⚠️ A MISSING [governance] max_drawdown MEANS "NO LIMIT CONFIGURED", not
    zero. This runs on the critical path of every buy, so a hard KeyError here
    would crash the hook -- and the hook FAILS CLOSED, so an unconfigured limit
    would refuse every order on the box. Absent key -> no automatic halt, which
    is exactly the behaviour before this check existed.
    """
    limit = cfg.get("governance", {}).get("max_drawdown")
    if not math.isfinite(account_value):
        return True, float("nan")          # same fail-closed rule as drawdown_halt
    peak = float(_load_state().get("peak_value", 0.0)) or float(account_value)
    dd = 0.0 if peak <= 0 else (float(account_value) / peak - 1.0)
    if limit is None:
        return False, dd
    return dd < -abs(float(limit)), dd


COOLDOWN_FILE = "research_store/monitor/cooldown.json"


def cooldown_until(symbol: str, today: str | None = None,
                   path: Path | None = None) -> str | None:
    """-> the date this symbol's stop-out cooldown runs to, or None. READS ONLY.

    ⛔ WHY THIS IS HERE. scripts/market_monitor.py writes {SYMBOL: "YYYY-MM-DD"}
    when a stop fires, so a name just stopped out is not immediately rebought
    against a book that has not rebuilt yet. TWO readers honoured it: the slow
    loop (excludes cooled names at rebuild) and scripts/fast_loop.py (refused
    the buy at order time). fast_loop.py was deleted 2026-08-14 with the
    procedural executor, so the ORDER-TIME half vanished -- a session could buy
    a name the monitor had stopped minutes earlier, which is exactly the churn
    the cooldown exists to prevent. The slow loop's half only bites at the next
    rebuild.

    FAILS OPEN. An absent, torn or unreadable file yields None for every symbol:
    a missing cooldown record is not evidence of a cooldown, and a guard that
    blocked buying because it could not read a file would be an outage. Same
    direction the deleted implementation chose.
    """
    today = today or dt.date.today().isoformat()
    p = path or (REPO / COOLDOWN_FILE)
    try:
        cd = json.loads(p.read_text())
        until = cd.get(str(symbol).strip().upper())
    except Exception:                      # noqa: BLE001 — see FAILS OPEN above
        return None
    if until is None:
        return None
    return str(until) if str(until) >= today else None


def whitelist(cfg) -> set[str]:
    """Symbols a BUY may name. Buy-only: a sell is never refused by this.

    ⛔ ETFs ARE NOT IN IT (2026-08-20). This used to be
    `universe ∪ etf_sleeve`, and the config's stated reason for keeping the ETF
    half was "four are HELD right now" — they were sold on 2026-08-17, which
    voided the reason and left the entry. The sleeve is deleted, so an ETF is no
    longer a thing this system buys.

    The 11 SPDR sector series survive as residual-tilt FACTORS
    (src/residual.py:SECTOR_FACTORS) and SPY as the regime observation. Being a
    factor is not being tradeable: they are deliberately absent here, so naming
    one in a buy is refused like any other off-universe symbol.

    A stale `[etf_sleeve]` table in a local override is IGNORED rather than
    honoured — re-adding it must not silently re-open the whitelist.
    """
    def col(path):
        return {ln.split(",")[0].strip()
                for ln in (REPO / path).read_text().splitlines()[1:] if ln.strip()}
    return col(cfg["universe"]["source"])


def assert_agentic_account(accounts, snapshot_account: str | None = None) -> str:
    """Resolve THE one tradeable account, or raise. This is Layer 1.

    The reference system at /opt/trading gets its Layer 1 from the endpoint: a
    paper key that cannot reach real money at all. We have no equivalent — the
    same token reaches every Robinhood account — so this guard is the boundary,
    and it must be code rather than an instruction in a prompt.

    Raises PermissionError on: no authorised account, more than one, or a
    mismatch against the account a snapshot was taken from. Never guesses.

    `agentic_allowed` must be the literal boolean True — deliberately `is True`,
    not truthy. A string "true" or an int 1 must NOT authorise an account: this
    mirrors the fail-closed style used elsewhere in this file (drawdown_halt,
    vet_plan) where a loosely-typed value must never slide through the
    permissive branch of the ONE guard standing between the agent and every
    other account the user owns.

    `accounts` must be a list of dicts, or this raises PermissionError rather
    than crashing: a malformed element is NEVER skipped and scanning continued,
    because a skipped element could have been the one authorised account, and
    silently dropping it would turn "I cannot parse this" into "I found exactly
    one account" -- the exact failure this function exists to prevent.
    """
    if accounts is not None and not isinstance(accounts, list):
        raise PermissionError(
            f"accounts must be a list, got {type(accounts).__name__}; "
            "cannot know what is in it, placing nothing")
    for a in (accounts or []):
        if not isinstance(a, dict):
            raise PermissionError(
                f"account list could not be parsed: element {a!r} is not an "
                f"account record ({type(a).__name__}, not dict); cannot know "
                "what is in it, placing nothing")
    allowed = [a for a in (accounts or [])
               if a.get("agentic_allowed") is True
               and str(a.get("account_number") or "").strip()]
    if not allowed:
        raise PermissionError(
            "no account with agentic_allowed=true; placing nothing")
    if len(allowed) > 1:
        raise PermissionError(
            f"expected exactly one agentic_allowed account, found {len(allowed)}: "
            f"{sorted(str(a['account_number']) for a in allowed)}; placing nothing")
    acct = str(allowed[0]["account_number"]).strip()
    if snapshot_account is not None and str(snapshot_account) != acct:
        raise PermissionError(
            f"account mismatch: snapshot is from {snapshot_account!r} but the "
            f"authorised account is {acct!r}; placing nothing")
    return acct


def gates(account_value: float, cfg) -> dict:
    """THE governance verdict, evaluated before any order.

    Returns {"block_all": [reasons], "block_entries": [reasons], "drawdown": f}.
    Empty lists = clear. `block_all` non-empty means place nothing at all;
    `block_entries` non-empty means buys are refused but exits remain available.

    Always updates the drawdown peak, including on a halted day, so the peak
    keeps tracking while the system is paused.
    """
    halted, dd = drawdown_halt(account_value, cfg)
    g = cfg["governance"]
    block_all: list[str] = []
    block_entries: list[str] = []

    if kill_switch_active(cfg):
        block_all.append(
            f"KILL-SWITCH active ({g['kill_switch_file']} present) — no orders of "
            "any kind. The monitor still watches and will ALERT on a breach; any "
            "exit must be placed BY HAND.")
    if halt_entries_active(cfg):
        block_entries.append(
            f"HALT-ENTRIES active ({g.get('halt_entries_file', HALT_ENTRIES_DEFAULT)}"
            " present) — no new buys; stop/target exits stay armed.")
    if not math.isfinite(account_value):
        # A corrupted (NaN/inf) account_value cannot be compared against the
        # drawdown limit at all, so it must surface as its OWN blocking
        # condition rather than silently falling through drawdown_halt's
        # "not halted" branch. Entries only — see drawdown_halt() docstring
        # for why an exit must never be blocked by this.
        block_entries.append(
            f"ACCOUNT VALUE INVALID ({account_value!r}) — a corrupted/non-finite "
            "account value cannot be used to measure drawdown or size an order; "
            "refusing new buys until a trustworthy value is available. "
            "Stop/target exits stay armed.")
    elif halted:
        block_entries.append(
            f"DRAWDOWN halt: {dd:.1%} from peak (limit {g['max_drawdown']:.0%}) — "
            "no new buys; stop/target exits stay armed.")

    return {"block_all": block_all, "block_entries": block_entries, "drawdown": dd}


def apply_entry_halts(plan: list[dict], reasons: list[str]) -> tuple[list[dict], list[dict]]:
    """Split a plan when risk-INCREASING orders are halted: buys are blocked,
    every sell passes through untouched. Pure. No reasons -> plan unchanged.

    Safe against a malformed order: this only ever branches on `o["side"]`
    (a string equality check), never on `o["amount"]`, so a NaN/inf/missing
    amount cannot flip a comparison here the way it can in drawdown_halt/
    vet_plan. Amount validity is vet_plan's job and runs before this in the
    fast loop (gates -> vet_plan -> apply_entry_halts), so a malformed BUY
    should already be gone by the time it would reach here.
    """
    if not reasons:
        return list(plan), []
    why = "; ".join(reasons)
    approved = [o for o in plan if o["side"] != "buy"]
    blocked = [{**o, "blocked": why} for o in plan if o["side"] == "buy"]
    return approved, blocked


def _amount_invalid(amount) -> bool:
    """True if `amount` cannot be trusted to size an order or compare against a
    cap: missing, not a number at all, or non-finite (NaN/+inf/-inf). Every
    direct comparison against NaN is False, so `amount > max_order` silently
    APPROVES a NaN order unless this is checked first."""
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return True
    return not math.isfinite(amount)


def liquidity_ok(symbol: str, dollar_volume: float | None,
                 min_dollar_volume: float) -> tuple[bool, str]:
    """Tradeability floor. Returns (ok, reason_if_not).

    This is the property config/universe.csv was standing in for. Stating it
    directly means the gate constrains SAFETY without constraining SELECTION —
    the agent may trade anything that clears the floor.

    FAILS CLOSED on unknown liquidity: a name we cannot measure is not a name we
    have shown to be tradeable. "Unknown" covers None, a non-numeric value
    (including a non-numeric string), and any non-finite float (NaN/+inf/-inf)
    — every direct comparison against NaN is False, so `dollar_volume < min`
    would silently treat a NaN reading as "not below the floor" unless this is
    checked first. A `bool` is deliberately treated as unknown too: Python
    makes `isinstance(True, int)` True and `True == 1`, but a bool can never be
    a genuine dollar-volume reading, so letting it through as $1/$0 of
    liquidity would be a type-confusion bug wearing a pass. A negative
    dollar_volume is impossible for a real reading — that is corrupt data, not
    a merely-illiquid name — so it fails the same way. Zero is a legitimate
    (if bad) reading and is left to the floor comparison below, not treated as
    unknown.

    The threshold itself is validated too: a non-finite or non-positive
    min_dollar_volume is a malformed config, and a malformed config must never
    silently authorise everything.
    """
    if (not isinstance(min_dollar_volume, (int, float))
            or isinstance(min_dollar_volume, bool)
            or not math.isfinite(min_dollar_volume)
            or min_dollar_volume <= 0):
        return False, (f"{symbol}: liquidity floor is misconfigured "
                       f"({min_dollar_volume!r}); refusing to treat this as tradeable")
    if (dollar_volume is None
            or isinstance(dollar_volume, bool)
            or not isinstance(dollar_volume, (int, float))
            or not math.isfinite(dollar_volume)
            or dollar_volume < 0):
        return False, f"{symbol}: liquidity unknown; cannot show it clears the floor"
    if float(dollar_volume) < float(min_dollar_volume):
        return False, (f"{symbol}: 20d $-volume {float(dollar_volume):,.0f} is below "
                       f"the {float(min_dollar_volume):,.0f} floor")
    return True, ""


def mandate_action(status: dict) -> dict:
    """Translate a mandate status (mandate.status()) into machine action. Decides;
    does not execute -- a later wiring calls this and hands the verdict to the
    monitor, which is the one place that actually places the sell.

    ONLY a drawdown breach flattens -- that is the mandate's own terminal term,
    so enforcing it is enforcing the terms the human agreed to, not forming a
    view about the market. A concentration breach deliberately does NOT flatten:
    choosing WHICH name to sell to fix concentration is a trading decision, and
    trading decisions belong to the agent, never the machine. A blocking
    criterion that is merely UNMEASURABLE (blocking_unmeasurable) must not
    flatten either -- not knowing is not the same as knowing something is
    wrong, and liquidating a book because a data feed broke would be a
    catastrophe caused by the safety system itself. Either way it blocks new
    entries: manage what is open, open nothing new.

    Informational criteria (informational_fail) never appear here -- they judge
    whether autonomy is working, never whether the machine should act, and
    mandate.status() keeps them out of blocking_fail/blocking_unmeasurable, so
    this function never even looks at that key.

    CONTRACT ON MALFORMED INPUT -- READ BEFORE WRAPPING THIS IN try/except:
    `status` must have `blocking_fail` and `blocking_unmeasurable` as lists and
    `criteria` as a dict; any missing key or wrong type (including `None` in
    place of a list) raises ValueError naming exactly which key was wrong. This
    function refuses to guess. It deliberately does NOT follow the old
    `.get(key, [])` pattern of treating a missing/malformed key as "nothing
    blocking reported" -- on a function whose job is to decide whether to
    liquidate, an empty verdict manufactured from garbage input is
    indistinguishable from a genuinely clean pass, and that is the one failure
    this function must never produce. A caller that catches this ValueError
    MUST treat it as UNKNOWN STATE, never as all-clear: degrade exactly like
    the unmeasurable-criterion case above -- manage what is open, open nothing
    new -- and never assume the book is safe just because a parse failed.

    Every criterion's `reason` string is read defensively: if `blocking_fail`
    or `blocking_unmeasurable` name a criterion absent from `criteria` (or
    present without a `reason`), the output says so explicitly ("no detail
    recorded") rather than silently repeating the bare key -- a phone alert
    that just says "MANDATE BREACH [drawdown]: drawdown" tells a human nothing
    they didn't already know from the key itself.
    """
    for key, expected_type in (("blocking_fail", list),
                                ("blocking_unmeasurable", list),
                                ("criteria", dict)):
        if key not in status:
            raise ValueError(
                f"mandate status missing required key {key!r}; "
                "cannot know what is blocking, treating this as unknown state")
        if not isinstance(status[key], expected_type):
            raise ValueError(
                f"mandate status key {key!r} must be a {expected_type.__name__}, "
                f"got {type(status[key]).__name__}; cannot know what is "
                "blocking, treating this as unknown state")

    criteria = status["criteria"]
    reasons, flatten = [], False
    for k in status["blocking_fail"]:
        why = (criteria.get(k) or {}).get("reason") or "no detail recorded"
        reasons.append(f"MANDATE BREACH [{k}]: {why}")
        if k == "drawdown":
            flatten = True
    for k in status["blocking_unmeasurable"]:
        why = (criteria.get(k) or {}).get("reason") or "no detail recorded"
        reasons.append(f"UNMEASURABLE [{k}]: {why}; degraded mode -- "
                       "manage what is open, open nothing new")
    return {"flatten": flatten,
            "block_entries": bool(reasons),
            "reasons": reasons}


def vet_plan(plan: list[dict], account_value: float, cfg) -> tuple[list[dict], list[dict]]:
    """Split a plan into (approved, blocked). Each blocked order carries a reason.
    Only BUYS are capped by max_order_pct — capping a sell would strand a position
    the system is trying to exit. For the same reason, a corrupted/missing
    `amount` or a non-finite `account_value` (which makes the cap itself
    meaningless — max_order would be NaN) ONLY ever blocks a BUY here; a SELL
    is never refused for this reason, so a bad mark can never trap an open
    position.

    The whitelist is likewise BUY-ONLY (2026-08-10): a SELL is never checked
    against it. `scripts/universe_refresh.py` has never run — its first window
    is Oct 1-7 — so the day it drops a held name, that name must still be
    exitable. Stops in this system are software-only (market_monitor.py places
    the sell); a gate that can block an exit removes an open position's only
    protection, exactly the failure mode this file's module docstring already
    documents for the kill switch and the drawdown halt. A universe list may
    say what the agent is allowed to newly BUY; it must never say what it is
    forbidden to sell."""
    g = cfg["governance"]
    wl = whitelist(cfg) if g.get("require_whitelist") else None
    account_value_bad = not math.isfinite(account_value)
    max_order = None if account_value_bad else g["max_order_pct"] * float(account_value)
    approved, blocked = [], []
    for o in plan:
        why = None
        if wl is not None and o["side"] == "buy" and o["symbol"] not in wl:
            why = "not in whitelist universe"
        elif o["side"] == "buy" and _amount_invalid(o.get("amount")):
            why = (f"order amount is invalid ({o.get('amount')!r}) — must be a "
                   "finite number; refusing to size this buy")
        elif o["side"] == "buy" and account_value_bad:
            why = (f"account value is invalid ({account_value!r}) — the order "
                   "cap cannot be computed; refusing this buy")
        elif o["side"] == "buy" and o["amount"] > max_order + 1e-9:
            why = f"${o['amount']:.2f} exceeds max order ${max_order:.2f} ({g['max_order_pct']:.0%})"
        (blocked if why else approved).append({**o, "blocked": why} if why else o)
    return approved, blocked


def _selftest() -> None:
    """Covers the two-tier split, the peak tracker, whitelist parsing and the
    order cap. The load-bearing assertions are the ones proving that NO gate
    except the kill switch can ever block a sell."""
    import tempfile
    global REPO, STATE
    _repo, _state = REPO, STATE
    try:
        with tempfile.TemporaryDirectory() as d:
            REPO = Path(d)
            STATE = REPO / "research_store" / "governance" / "state.json"
            (REPO / "research_store").mkdir(parents=True)
            (REPO / "config").mkdir()
            (REPO / "config" / "universe.csv").write_text("ticker,flag\nAAPL,\nMSFT,\n")
            (REPO / "config" / "etf_universe.csv").write_text("ticker,flag\nXLK,\n")
            cfg = {
                "governance": {"kill_switch_file": "research_store/HALT",
                               "halt_entries_file": "research_store/HALT_ENTRIES",
                               "max_drawdown": 0.25, "max_order_pct": 0.15,
                               "require_whitelist": True},
                "universe": {"source": "config/universe.csv"},
                "etf_sleeve": {"source": "config/etf_universe.csv"},
            }
            buy = {"symbol": "AAPL", "side": "buy", "amount": 10.0}
            sell = {"symbol": "AAPL", "side": "sell", "amount": 10.0}

            # 1. clean box: nothing blocked, peak seeded
            g = gates(100.0, cfg)
            assert g["block_all"] == [] and g["block_entries"] == [], g
            assert _load_state()["peak_value"] == 100.0

            # 2. drawdown blocks ENTRIES ONLY — it must never strand an exit.
            #    (Pre-2026-08-09 this sat in preflight() and blocked sells too,
            #    contradicting strategy.toml's own "halt new buys" comment.)
            g = gates(70.0, cfg)                       # -30% vs peak 100, limit 25%
            assert g["block_all"] == [], "drawdown must NOT block sells"
            assert len(g["block_entries"]) == 1 and "DRAWDOWN" in g["block_entries"][0]
            assert round(g["drawdown"], 4) == -0.30, g["drawdown"]
            appr, blkd = apply_entry_halts([buy, sell], g["block_entries"])
            assert appr == [sell] and len(blkd) == 1 and blkd[0]["symbol"] == "AAPL"
            assert "DRAWDOWN" in blkd[0]["blocked"]

            # 3. peak is a high-water mark: it does not follow the account down
            assert _load_state()["peak_value"] == 100.0

            # 4. HALT_ENTRIES blocks entries only; sells still flow
            (REPO / "research_store" / "HALT_ENTRIES").touch()
            g = gates(100.0, cfg)
            assert g["block_all"] == [], "HALT_ENTRIES must NOT block sells"
            assert any("HALT-ENTRIES" in r for r in g["block_entries"]), g
            assert apply_entry_halts([buy, sell], g["block_entries"])[0] == [sell]
            (REPO / "research_store" / "HALT_ENTRIES").unlink()

            # 5. the kill switch is the ONLY gate that stops everything
            (REPO / "research_store" / "HALT").touch()
            g = gates(100.0, cfg)
            assert len(g["block_all"]) == 1 and "KILL-SWITCH" in g["block_all"][0]
            assert "BY HAND" in g["block_all"][0], "must say exits are now manual"
            (REPO / "research_store" / "HALT").unlink()
            assert gates(100.0, cfg)["block_all"] == [], "removing HALT resumes"

            # 6. no halts -> plan passes through untouched
            assert apply_entry_halts([buy, sell], []) == ([buy, sell], [])

            # 7. whitelist + order cap; the cap applies to buys only
            #
            # ⛔ ETFs ARE NOT WHITELISTED (2026-08-20). This asserted
            # {"AAPL","MSFT","XLK"} while the whitelist was universe ∪
            # etf_sleeve. The sleeve is deleted, so XLK must NOT appear even
            # though config/etf_universe.csv is still present in this fixture
            # and even though the fixture cfg still carries an [etf_sleeve]
            # table — a stale local override must not silently re-open it.
            assert whitelist(cfg) == {"AAPL", "MSFT"}, whitelist(cfg)
            assert "XLK" not in whitelist(cfg), "a retired sleeve must not be buyable"

            # ...and that has to BITE at the gate, buys only: naming an ETF in a
            # BUY is refused like any other off-universe symbol, while a SELL is
            # untouched. Blocking a sell would strip a position of its only
            # protection (the stop here is software).
            etf_appr, etf_blkd = vet_plan(
                [{"symbol": "XLK", "side": "buy", "amount": 1.0},
                 {"symbol": "XLK", "side": "sell", "amount": 1.0}], 100.0, cfg)
            assert [o["symbol"] for o in etf_appr] == ["XLK"], etf_appr
            assert etf_appr[0]["side"] == "sell", "an ETF SELL must never be refused"
            assert len(etf_blkd) == 1 and etf_blkd[0]["side"] == "buy", etf_blkd
            assert "not in whitelist" in etf_blkd[0]["blocked"], etf_blkd
            appr, blkd = vet_plan(
                [{"symbol": "NVDA", "side": "buy", "amount": 1.0},      # off-universe
                 {"symbol": "AAPL", "side": "buy", "amount": 20.0},     # > 15% of 100
                 {"symbol": "AAPL", "side": "buy", "amount": 15.0},     # exactly at cap
                 {"symbol": "MSFT", "side": "sell", "amount": 90.0}],   # sell: uncapped
                100.0, cfg)
            assert [o["symbol"] for o in appr] == ["AAPL", "MSFT"], appr
            assert [o["amount"] for o in appr] == [15.0, 90.0], appr
            assert "not in whitelist" in blkd[0]["blocked"]
            assert "exceeds max order" in blkd[1]["blocked"]

            # 7b. WHITELIST-SELL REGRESSION (load-bearing): the whitelist must be
            # BUY-ONLY. A held name that has fallen off config/universe.csv (e.g.
            # scripts/universe_refresh.py hasn't run since a rebalance dropped it)
            # must still be exitable — a gate that blocks a sell removes an open
            # position's only protection, since stops here are software-only. This
            # is the assertion that makes the module/_selftest docstrings' claim
            # ("NO gate except the kill switch can ever block a sell") actually
            # true for the whitelist, rather than merely asserted in prose.
            off_universe_sell = {"symbol": "NFLX", "side": "sell", "amount": 10.0}
            off_universe_buy = {"symbol": "NFLX", "side": "buy", "amount": 10.0}
            appr, blkd = vet_plan([off_universe_sell, off_universe_buy], 100.0, cfg)
            assert appr == [off_universe_sell], (
                "an off-universe SELL must be approved — the whitelist must "
                "never strand an exit", appr, blkd)
            assert len(blkd) == 1 and blkd[0]["symbol"] == "NFLX" and blkd[0]["side"] == "buy"
            assert "not in whitelist" in blkd[0]["blocked"], (
                "an off-universe BUY must still be blocked", blkd)

            # 8. NaN/CORRUPTED-VALUE REGRESSION: a non-finite account_value must
            #    fail CLOSED through drawdown_halt (not silently "not halted",
            #    which is what every direct NaN comparison produces), and must
            #    NEVER poison the persisted peak.
            seed_peak = _load_state()["peak_value"]
            assert seed_peak == 100.0, seed_peak
            for bad in (float("nan"), float("inf"), float("-inf")):
                halted, dd = drawdown_halt(bad, cfg)
                assert halted is True, (bad, halted, "must fail CLOSED, not open")
                assert math.isnan(dd), (bad, dd)
                assert _load_state()["peak_value"] == seed_peak, (
                    "non-finite account_value must NEVER reach update_peak()")

            # 9. gates() must surface a non-finite account_value as its own
            #    blocking condition, and it must be entries-only (never a sell).
            g = gates(float("nan"), cfg)
            assert g["block_all"] == [], "invalid account value must NOT block a sell/all"
            assert len(g["block_entries"]) == 1, g
            assert "ACCOUNT VALUE INVALID" in g["block_entries"][0], g
            appr, blkd = apply_entry_halts([buy, sell], g["block_entries"])
            assert appr == [sell], "sell must still pass with a corrupted account value"
            assert len(blkd) == 1 and blkd[0]["symbol"] == "AAPL"
            assert _load_state()["peak_value"] == seed_peak, "gates() must not poison the peak either"

            for bad in (float("inf"), float("-inf")):
                g = gates(bad, cfg)
                assert g["block_all"] == [], bad
                assert "ACCOUNT VALUE INVALID" in g["block_entries"][0], (bad, g)

            # 10. vet_plan: a malformed order amount must BLOCK a buy — never
            #     approve it — and must NEVER block a sell (same "don't trap an
            #     open position" rule as the halt tier).
            bad_amounts = [float("nan"), float("inf"), float("-inf"), None, "not-a-number"]
            buy_plan = [{"symbol": "AAPL", "side": "buy", "amount": a} for a in bad_amounts]
            buy_plan.append({"symbol": "AAPL", "side": "buy"})            # amount missing entirely
            appr, blkd = vet_plan(buy_plan, 100.0, cfg)
            assert appr == [], "no buy with a malformed amount may ever be approved"
            assert len(blkd) == len(buy_plan), blkd
            assert all("invalid" in b["blocked"] for b in blkd), blkd

            sell_plan = [{"symbol": "AAPL", "side": "sell", "amount": a} for a in bad_amounts]
            appr, blkd = vet_plan(sell_plan, 100.0, cfg)
            assert blkd == [], "a malformed amount must NEVER block a sell"
            assert len(appr) == len(sell_plan)

            # 11. vet_plan: a non-finite account_value makes the cap itself
            #     meaningless -> blocks buys, never sells.
            appr, blkd = vet_plan([buy, sell], float("nan"), cfg)
            assert appr == [sell], "invalid account value must never block a SELL"
            assert len(blkd) == 1 and "account value is invalid" in blkd[0]["blocked"], blkd
            for bad in (float("inf"), float("-inf")):
                appr, blkd = vet_plan([buy, sell], bad, cfg)
                assert appr == [sell], bad
                assert len(blkd) == 1 and "account value is invalid" in blkd[0]["blocked"]

            # --- liquidity: the property the whitelist was standing in for ---
            ok, why = liquidity_ok("AAA", 100_000_000.0, 50_000_000.0)
            assert ok and why == "", (ok, why)
            ok, why = liquidity_ok("BBB", 10_000_000.0, 50_000_000.0)
            assert not ok and "below the" in why, (ok, why)
            # exactly at the floor is acceptable
            assert liquidity_ok("CCC", 50_000_000.0, 50_000_000.0)[0]
            # UNKNOWN liquidity fails CLOSED -- never assume a name is tradeable
            ok, why = liquidity_ok("DDD", None, 50_000_000.0)
            assert not ok and "unknown" in why.lower(), (ok, why)

            # NaN/+inf/-inf dollar_volume: every direct comparison against NaN
            # is False, so `dollar_volume < min` would silently pass a NaN
            # through as "not below the floor" unless isfinite is checked
            # first. +inf/-inf are equally untrustworthy readings, not
            # legitimate liquidity.
            for bad in (float("nan"), float("inf"), float("-inf")):
                ok, why = liquidity_ok("EEE", bad, 50_000_000.0)
                assert not ok and "unknown" in why.lower(), (bad, ok, why)

            # a negative dollar volume is impossible -- corrupt data, not a
            # low-liquidity name -- and must fail closed the same way.
            ok, why = liquidity_ok("FFF", -1_000_000.0, 50_000_000.0)
            assert not ok and "unknown" in why.lower(), (ok, why)

            # a non-numeric string must not slide through Python's loose
            # comparison operators
            ok, why = liquidity_ok("GGG", "not-a-number", 50_000_000.0)
            assert not ok and "unknown" in why.lower(), (ok, why)

            # bool IS numeric in Python (True == 1, isinstance(True, int) is
            # True) -- but a bool can never be a real dollar-volume reading,
            # so it must be rejected as unknown rather than silently treated
            # as $1 or $0 of liquidity.
            for bad in (True, False):
                ok, why = liquidity_ok("HHH", bad, 50_000_000.0)
                assert not ok and "unknown" in why.lower(), (bad, ok, why)

            # zero dollar volume is a legitimate (if bad) reading, not
            # "unknown" -- it must fail on the floor comparison, not the
            # unknown-liquidity branch.
            ok, why = liquidity_ok("III", 0.0, 50_000_000.0)
            assert not ok and "below the" in why, (ok, why)

            # a malformed threshold (non-finite or non-positive) must not
            # silently authorise everything -- a config bug should never
            # look like "this name is liquid enough".
            for bad_floor in (float("nan"), float("inf"), float("-inf"), 0.0, -1.0):
                ok, why = liquidity_ok("JJJ", 100_000_000.0, bad_floor)
                assert not ok and "floor" in why.lower(), (bad_floor, ok, why)

            # --- account scoping: OUR layer 1, since no paper endpoint exists ---
            accts = [{"account_number": "111", "agentic_allowed": False},
                     {"account_number": "222", "agentic_allowed": True}]
            assert assert_agentic_account(accts) == "222"
            # zero authorised accounts must raise, never fall through
            try:
                assert_agentic_account([{"account_number": "111",
                                         "agentic_allowed": False}])
                raise AssertionError("must raise when no account is authorised")
            except PermissionError as e:
                assert "no account" in str(e).lower(), e
            # MORE than one is ambiguous and must raise rather than pick
            try:
                assert_agentic_account([{"account_number": "1", "agentic_allowed": True},
                                        {"account_number": "2", "agentic_allowed": True}])
                raise AssertionError("must raise on ambiguity")
            except PermissionError as e:
                assert "exactly one" in str(e).lower(), e
            # a snapshot naming a DIFFERENT account must raise
            try:
                assert_agentic_account(accts, snapshot_account="111")
                raise AssertionError("must raise on account mismatch")
            except PermissionError as e:
                assert "mismatch" in str(e).lower(), e
            # matching snapshot is fine
            assert assert_agentic_account(accts, snapshot_account="222") == "222"
            # empty/garbage input raises rather than returning something falsy
            for bad in ([], None):
                try:
                    assert_agentic_account(bad)
                    raise AssertionError("must raise on empty account list")
                except PermissionError:
                    pass

            # a truthy STRING "true" is not authorisation -- only the literal
            # boolean True unlocks an account (mirrors the NaN-fail-closed style
            # elsewhere in this file: don't let a loosely-typed value slide
            # through a permissive branch).
            try:
                assert_agentic_account([{"account_number": "111", "agentic_allowed": "true"},
                                         {"account_number": "222", "agentic_allowed": False}])
                raise AssertionError("string 'true' must not authorise an account")
            except PermissionError as e:
                assert "no account" in str(e).lower(), e
            # int 1 is truthy and == True, but is not `is True` -- must not authorise
            try:
                assert_agentic_account([{"account_number": "111", "agentic_allowed": 1}])
                raise AssertionError("int 1 must not authorise an account")
            except PermissionError as e:
                assert "no account" in str(e).lower(), e
            # agentic_allowed key absent entirely -- absence is not authorisation
            try:
                assert_agentic_account([{"account_number": "111"}])
                raise AssertionError("missing agentic_allowed key must not authorise")
            except PermissionError as e:
                assert "no account" in str(e).lower(), e
            # account_number empty or None on an otherwise-allowed account: an
            # account we cannot name is one we must not trade
            for bad_num in ("", None):
                try:
                    assert_agentic_account([{"account_number": bad_num, "agentic_allowed": True}])
                    raise AssertionError(f"empty/None account_number ({bad_num!r}) must not authorise")
                except PermissionError as e:
                    assert "no account" in str(e).lower(), e
            # whitespace-only account_number is truthy but nameless -- must be
            # treated exactly like "" or None, not accepted as an identifier
            for bad_num in ("   ", "\t\n", "\t \n "):
                try:
                    assert_agentic_account([{"account_number": bad_num, "agentic_allowed": True}])
                    raise AssertionError(f"whitespace-only account_number ({bad_num!r}) must not authorise")
                except PermissionError as e:
                    assert "no account" in str(e).lower(), e
            # a valid but padded account_number must resolve to the STRIPPED
            # value -- padding is cosmetic, not part of the account's identity
            assert assert_agentic_account(
                [{"account_number": "  111  ", "agentic_allowed": True}]) == "111"
            # snapshot_account as an int must match an account_number stored as a
            # string, and vice versa. DECISION: this SHOULD match. Account
            # numbers cross several boundaries in this repo (RH MCP JSON, the
            # snapshot writer, hand-typed config) that are not disciplined about
            # int vs str for what is the same real-world account number: the
            # comparison must be about IDENTITY of the account, not the JSON
            # type that happened to carry it. The implementation already
            # normalises both sides through str() for exactly this reason, so a
            # type-only mismatch must never cause a false "mismatch" refusal
            # here -- that would be a spurious block on the ONLY account this
            # system is allowed to trade, which is worse than being lenient on
            # type.
            assert assert_agentic_account(
                [{"account_number": "222", "agentic_allowed": True}],
                snapshot_account=222) == "222"
            assert assert_agentic_account(
                [{"account_number": 222, "agentic_allowed": True}],
                snapshot_account="222") == "222"

            # a malformed `accounts` container must raise PermissionError with a
            # message naming the real problem, never crash with AttributeError --
            # every other refusal path in this function fails closed this way.
            try:
                assert_agentic_account({"account_number": "1", "agentic_allowed": True})
                raise AssertionError("a dict (not a list) must raise, not silently authorise")
            except PermissionError as e:
                assert "list" in str(e).lower(), e
            except AttributeError:
                raise AssertionError("must raise PermissionError, not AttributeError, on a dict container")
            try:
                assert_agentic_account([None])
                raise AssertionError("a list containing None must raise")
            except PermissionError as e:
                assert "parsed" in str(e).lower(), e
            except AttributeError:
                raise AssertionError("must raise PermissionError, not AttributeError, on a None element")
            try:
                assert_agentic_account(["not a dict"])
                raise AssertionError("a list containing a string must raise")
            except PermissionError as e:
                assert "parsed" in str(e).lower(), e
            except AttributeError:
                raise AssertionError("must raise PermissionError, not AttributeError, on a string element")
            # a valid authorised account ALONGSIDE a malformed element must
            # still raise -- the malformed element must never be silently
            # skipped, because it could have been the real authorised account
            # and skipping it would turn "cannot parse this" into "found
            # exactly one", the exact failure this function exists to prevent.
            try:
                assert_agentic_account([{"account_number": "222", "agentic_allowed": True}, None])
                raise AssertionError("a malformed element alongside a valid account must still raise")
            except PermissionError as e:
                assert "parsed" in str(e).lower(), e

            # --- mandate action: the ONLY mechanical close in the system ------
            clean = {"blocking_fail": [], "blocking_unmeasurable": [],
                     "criteria": {}, "tradeable": True, "degraded": False}
            a = mandate_action(clean)
            assert a == {"flatten": False, "block_entries": False, "reasons": []}, a
            # a drawdown breach flattens
            dd = {"blocking_fail": ["drawdown"], "blocking_unmeasurable": [],
                  "criteria": {"drawdown": {"reason": "-18% from peak"}},
                  "tradeable": False, "degraded": False}
            a = mandate_action(dd)
            assert a["flatten"] is True and a["block_entries"] is True, a
            assert any("-18%" in r for r in a["reasons"]), a
            # a CONCENTRATION breach blocks entries but must NOT flatten the book --
            # forced selling to fix concentration is a trading decision, not enforcement
            conc = {"blocking_fail": ["concentration"], "blocking_unmeasurable": [],
                    "criteria": {"concentration": {"reason": "AAA at 22%"}},
                    "tradeable": False, "degraded": False}
            a = mandate_action(conc)
            assert a["flatten"] is False and a["block_entries"] is True, a
            # unmeasurable blocking criteria: degraded mode, no new risk, no flatten
            dark = {"blocking_fail": [], "blocking_unmeasurable": ["concentration"],
                    "criteria": {"concentration": {"reason": "no usable mark"}},
                    "tradeable": False, "degraded": True}
            a = mandate_action(dark)
            assert a["flatten"] is False and a["block_entries"] is True, a
            # drawdown breach AND concentration breach together -- flatten must
            # win (drawdown's terminal term is absolute) and BOTH reasons must
            # surface, because the concentration breach still needs a human to
            # act on once the flatten is over.
            both = {"blocking_fail": ["drawdown", "concentration"],
                    "blocking_unmeasurable": [],
                    "criteria": {"drawdown": {"reason": "-20% from peak"},
                                 "concentration": {"reason": "BBB at 30%"}},
                    "tradeable": False, "degraded": False}
            a = mandate_action(both)
            assert a["flatten"] is True and a["block_entries"] is True, a
            assert any("-20%" in r for r in a["reasons"]), a
            assert any("BBB at 30%" in r for r in a["reasons"]), a
            assert len(a["reasons"]) == 2, a
            # an informational criterion FAILing while everything blocking
            # passes must produce a completely empty verdict -- informational
            # criteria judge whether autonomy is working, never whether the
            # machine should act, and must never leak into this output.
            info_only = {"blocking_fail": [], "blocking_unmeasurable": [],
                         "informational_fail": ["relative_return"],
                         "criteria": {"relative_return": {"reason": "trailing SPY by 9%"}},
                         "tradeable": True, "degraded": False}
            a = mandate_action(info_only)
            assert a == {"flatten": False, "block_entries": False, "reasons": []}, a
            # reasons must be human-readable enough to carry the SPECIFIC
            # number from the criterion's own reason string, not just repeat
            # the criterion name -- a phone alert saying "drawdown" tells a
            # human nothing actionable; "-18% from peak" does.
            a = mandate_action(dd)
            assert "drawdown" in a["reasons"][0] and "-18% from peak" in a["reasons"][0], a
            a = mandate_action(dark)
            assert "concentration" in a["reasons"][0] and "no usable mark" in a["reasons"][0], a
            # the input dict must never be mutated -- a shared/reused status
            # object mutated here would corrupt whatever the caller does with
            # it next.
            before = {"blocking_fail": ["drawdown"], "blocking_unmeasurable": [],
                      "criteria": {"drawdown": {"reason": "-18% from peak"}}}
            import copy as _copy
            snapshot = _copy.deepcopy(before)
            mandate_action(before)
            assert before == snapshot, "mandate_action must not mutate its input"
            # flatten=True is reachable ONLY from the literal "drawdown" key --
            # a lookalike name (case variant, near-miss) must block but never
            # flatten.
            lookalike = {"blocking_fail": ["DRAWDOWN", "Drawdown"],
                         "blocking_unmeasurable": [],
                         "criteria": {"DRAWDOWN": {"reason": "x"},
                                      "Drawdown": {"reason": "y"}}}
            a = mandate_action(lookalike)
            assert a["flatten"] is False and a["block_entries"] is True, a
            # an unrecognised criterion name (not drawdown, not concentration,
            # not anything the mandate currently defines) still blocks entries
            # -- a blocking_fail entry is blocking by construction -- but must
            # never flatten, since only "drawdown" carries the terminal term.
            unknown = {"blocking_fail": ["some_future_criterion"],
                       "blocking_unmeasurable": [],
                       "criteria": {"some_future_criterion": {"reason": "new rule tripped"}}}
            a = mandate_action(unknown)
            assert a["flatten"] is False and a["block_entries"] is True, a
            assert "new rule tripped" in a["reasons"][0], a

            # --- mandate action: malformed input must RAISE, never launder
            # into an all-clear verdict. On the function that decides whether
            # to liquidate, an empty verdict manufactured from garbage input
            # is indistinguishable from a genuinely clean pass -- exactly the
            # finding this closes. Every case below must raise ValueError
            # naming the offending key, not return a dict.
            def _assert_raises_naming(bad_status, expected_key):
                try:
                    mandate_action(bad_status)
                    raise AssertionError(
                        f"mandate_action({bad_status!r}) must raise ValueError, "
                        "not return a verdict")
                except ValueError as e:
                    assert expected_key in str(e), (
                        f"ValueError message must name {expected_key!r}: {e}")
                except AssertionError:
                    raise
                except Exception as e:
                    raise AssertionError(
                        f"must raise ValueError, not {type(e).__name__}: {e}")

            # completely empty dict: missing blocking_fail (checked first)
            _assert_raises_naming({}, "blocking_fail")
            # a dict missing only `criteria`
            _assert_raises_naming(
                {"blocking_fail": [], "blocking_unmeasurable": []}, "criteria")
            # blocking_fail explicitly None -- the exact input that used to
            # raise an uncaught TypeError instead of a clean ValueError
            _assert_raises_naming(
                {"blocking_fail": None, "blocking_unmeasurable": [], "criteria": {}},
                "blocking_fail")
            # blocking_fail as a bare string -- iterable, so the old code
            # would have silently iterated its characters as criterion names
            _assert_raises_naming(
                {"blocking_fail": "drawdown", "blocking_unmeasurable": [], "criteria": {}},
                "blocking_fail")
            # criteria as a list instead of a dict
            _assert_raises_naming(
                {"blocking_fail": [], "blocking_unmeasurable": [], "criteria": []},
                "criteria")
            # the dangerous one the finding called out by name: `{"tradeable":
            # True}` used to be indistinguishable from a genuinely clean pass
            _assert_raises_naming({"tradeable": True}, "blocking_fail")

        print("selftest OK: governance two-tier gates "
              "(only the kill switch blocks a sell), peak, whitelist, order cap, "
              "NaN/inf account_value and order-amount fail-closed handling, "
              "account-scoping layer 1 (assert_agentic_account), "
              "mandate_action (the only mechanical close)")
    finally:
        REPO, STATE = _repo, _state


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
