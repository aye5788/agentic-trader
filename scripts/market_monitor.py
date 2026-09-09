"""MARKET MONITOR — the always-on stop-loss / take-profit watcher (Part 1 of 2).

The daily loops only STORE each name's stop and take-profit prices; nothing
enforced them. This does. During market hours it polls live moomoo quotes for
every held name every `poll_secs` (all names in one call) and checks each price
against its stored stop / targets. On a breach it journals the event and — Part 2
— invokes the headless executor (prompts/exit.md) to place the market sell via RH.

Watching is pure Python + moomoo via OpenD (cheap, no Claude, and no weekly
credential chore — the Schwab feed this replaced needed a browser re-auth every
7 days to keep the stop watcher alive). Claude fires only on an
actual breach. Governance: idles on the kill-switch; runs ALERT-ONLY (journal,
no sell) whenever [proof] live_approved is false or [monitor] alert_only is true.
A stopped-out name goes on a cooldown list so the slow loop won't rebuy it next
morning (stop-vs-momentum churn guard).

    .venv/bin/python scripts/market_monitor.py          # run; self-gates to RTH
    .venv/bin/python scripts/market_monitor.py --once    # single pass (testing)
    .venv/bin/python scripts/market_monitor.py --once --force   # ignore market-hours gate
"""
import argparse
import json
import math
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "hooks"))

try:                                     # NTFY_TOPIC for phone push alerts
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
except Exception:
    pass
import strategy as strat            # noqa: E402
import governance as gov            # noqa: E402
import snapshot_freshness            # noqa: E402
import trailing                      # noqa: E402  pure arithmetic, no I/O
import lifecycle_journal             # noqa: E402  pure fold, no I/O of its own
from research_store import read_current, store   # noqa: E402
from adapters.moomoo import prices as mmp        # noqa: E402
from adapters.moomoo.client import quote_ctx     # noqa: E402
from notify import push as notify               # noqa: E402  shared ntfy helper
import exit_bookkeeping                          # noqa: E402  monitor-owned exit recording (§4, 2026-09-03)
import models                                    # noqa: E402  the exit executor's model + chain (config/models.toml)
import fallback                                  # noqa: E402  the chain walk (clean failure only)
# ⛔ ONE DEFINITION OF "THE SAME REQUEST", shared with the gate that enforces it.
# The producer of the identity and its verifier must never drift into two
# implementations, so the stamp the executor is launched with and the check the
# hook performs come from the same function. That module is stdlib-only for
# exactly this reason: the monitor runs under system /usr/bin/python3 (3.10) and
# the hook under the repo .venv (3.12).
from pretooluse_exit_scope import request_id as _exit_request_id   # noqa: E402

MON = REPO / "research_store" / "monitor"
STATE = MON / "state.json"
QUOTES = MON / "quotes.json"
COOLDOWN = MON / "cooldown.json"
EXIT_REQ = MON / "exit_request.json"
EXIT_RES = MON / "exit_result.json"
WAKES = MON / "wakes.json"
FEED_ALERT = MON / "feed_alert.json"    # cooldown clock for the "quotes down" push
RH_POSITIONS = REPO / "research_store" / "rh" / "positions.json"
ET = ZoneInfo("America/New_York")

# Remembers the last-logged "not held" set so we log it once per change, not every
# 15s poll (the set is static all day — logging it each tick just floods journald).
_LAST_DROPPED: frozenset | None = None
_LAST_SUSPECT_ACTION: frozenset = frozenset()
# Shadow-mode trail breaches already journalled — fire-on-CHANGE, so a breach
# that persists across every 15s poll is recorded once, not 240 times an hour.
_LAST_TRAIL_SHADOW: frozenset = frozenset()
_LAST_HALTED: bool | None = None      # log kill-switch mode on transition, not every tick

# Unprotected-position invariant (spec 2026-08-09 §8): dedupe the phone push to
# CHANGES in the finding, same fire-on-transition pattern as _LAST_DROPPED /
# _LAST_HALTED above — not a re-push every 15s poll while the condition holds.
_LAST_UNPROTECTED: frozenset = frozenset()
_LAST_SUSPECT_EMPTY: bool = False
_LAST_NO_TARGET: frozenset = frozenset()
# Positions watched on an agent-set stop with no thesis, and the ones
# refused because their stop could not be proven below spot. Fire-on-change,
# matching _LAST_DROPPED/_LAST_UNPROTECTED rather than re-pushing every 15s.
_LAST_SOLO: frozenset = frozenset()
_LAST_SOLO_REFUSED: frozenset = frozenset()
_LAST_STALE_OWNERSHIP: bool = False
# ⛔ AN UNREADABLE overrides.json IS NOT AN EMPTY ONE. Same fire-on-transition
# pattern as the flags above. See the read below for why this exists.
_LAST_OV_UNREADABLE: bool = False
_LAST_TARGET_SUPPRESSED: frozenset = frozenset()   # journal on change, not every 15s tick


def _now_et():
    return datetime.now(ET)


def market_open(now=None) -> bool:
    """Weekday + 09:30-16:00 ET. PURE — no I/O, no calendar, deliberately.

    ⛔ DO NOT PUT THE HOLIDAY LOOKUP IN HERE. This stays pure because that is
    what makes it testable at any instant without a network; the calendar is a
    separate, cached gate applied in main() alongside it — see trading_day().
    """
    now = now or _now_et()
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= mins < 16 * 60          # 09:30–16:00 ET


# Definitive calendar answers only, keyed by ET date; None is never cached here.
_TRADING_DAY: dict = {}
_TRADING_DAY_TRIED: dict = {}
_TRADING_DAY_RETRY_SECS = 900


def trading_day(ctx=None, now=None) -> bool:
    """Is TODAY a US trading session? -> bool. Asked ONCE per ET date.

    market_open() tests weekday and clock alone, so on a market HOLIDAY the
    monitor polled all day. `get_market_snapshot` on a shut market returns the
    PREVIOUS session's close, so it compared Friday's prices to every stop and
    every target: on 2026-09-07 (Labor Day) that fired MU's target1 against a
    stale 1016.59 and placed a real order the broker queued, which the 10:35
    session had to cancel by hand. Recorded in OPSLOG 2026-09-07 as a hole left
    open on the judgement that a shut market "can breach no stop" — true of the
    stop, but not of the ORDER it placed, nor of the take-profit latch that
    order then held spent.

    ⛔ UNKNOWN MEANS RUN, NEVER HALT. `is_trading_day` returns None for "could
    not tell" — and a calendar lookup that fails must never be what stops the
    stop watcher from running on a real trading day. None therefore falls back
    to exactly today's behaviour (weekday + clock), which needs no network and
    is never wrong about a weekend; this call only adds weekday HOLIDAYS on top.

    ASKED ONCE PER DAY, not once per poll: the loop ticks every 15s, so asking
    per tick would be ~1,500 calls a day against a paced API. Only a definitive
    True/False is cached; an inconclusive answer is retried, but no more often
    than every 15 minutes, so a transient failure cannot silently disable the
    gate for the rest of the day. Reuses the monitor's open context the way
    fetch_prices does, so it costs no extra connection.
    """
    d = (now or _now_et()).date().isoformat()
    if d in _TRADING_DAY:
        return _TRADING_DAY[d]
    last = _TRADING_DAY_TRIED.get(d)
    if last is not None and (time.monotonic() - last) < _TRADING_DAY_RETRY_SECS:
        return True                      # unknown and asked recently — behave as before
    ans = mmp.is_trading_day(d, ctx=ctx)
    if ans is None:
        _TRADING_DAY_TRIED[d] = time.monotonic()
        return True                      # unknown -> run, never halt
    _TRADING_DAY[d] = bool(ans)
    if not _TRADING_DAY[d]:
        print(f"  market CLOSED today ({d}) — holiday per the exchange calendar; "
              f"not polling, so no stop or target is evaluated against a stale feed")
    return _TRADING_DAY[d]


# ⛔ BROKER STATES IN WHICH NOTHING HAS EXECUTED. Anything NOT listed here —
# including a missing or unrecognised status — counts as a sale, deliberately:
# the expensive error is the other way round. An unrecorded fill leaves the
# ledger and positions.json stale and silently unprotected (the DELL trim,
# 2026-09-03), whereas a spurious "sold" only runs idempotent recorders.
UNEXECUTED_STATES = frozenset({
    "queued", "new", "unconfirmed", "pending", "confirmed",
    "cancelled", "canceled", "rejected", "failed", "voided",
})


def _lvl(x):
    """A price level as a stable comparison key. Unparseable/non-finite -> None."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, 6) if math.isfinite(v) else None


def carry_fired(prev) -> dict:
    """Normalise the `fired` latch, which is now CARRIED, not wiped. Pure.

    ⛔ IT USED TO BE KEYED TO THE BOOK DATE, AND THAT SOLD THE SAME POSITION
    TWICE (2026-09-07). The old line was `if st["book_asof"] != prod.as_of: st =
    {..., "fired": {}}`. The slow loop rolls the book at 18:00 EVERY weekday, so
    every spent take-profit came back ARMED at 09:30 the next morning while its
    LEVEL sat there unchanged — and the monitor trimmed an already-trimmed name
    again. Measured: MU target1 1010 fired 2026-09-04 15:46 ET (half sold at
    1009.55); the book rolled at 18:00; at 09-07 09:30 the latch was empty, 1010
    was still the level, and it placed a second 50% sale of the remainder.
    `[monitor] enable_targets` in strategy.toml is "t1 half, t2 rest" — two
    rungs, each firing ONCE. A third trim is not a strategy variant, it is this
    bug. (It had been masked because the agent was raising spent targets BY HAND
    each session — it did exactly that for SNDK and INTC on 09-04. MU slipped
    through because it hit 1010 half an hour AFTER that session ended.)

    ⛔ SPENT IS PER LEVEL, NOT PER SYMBOL — that is what makes carrying safe:
      - agent moves t1 1010 -> 1105   -> different level, arms again (this is
        the repair the close session was making by hand)
      - position exits and is re-entered -> clear_levels() on exit then new
        levels on entry, so the stored level cannot match -> arms again
      - same position, same level, price wobbles back over it -> STAYS SPENT

    Because re-entry is handled by the level itself, this deliberately does NOT
    prune on "symbol absent from the book". Absence is inferred, and a book that
    flickered for a single tick would re-arm a spent target — reintroducing the
    exact defect. A full exit clears its own symbol at the call site instead.
    """
    out = {}
    for sym, tiers in (prev or {}).items():
        if isinstance(tiers, dict):
            out[str(sym)] = {str(k): _lvl(v) for k, v in tiers.items()}
        elif isinstance(tiers, list):
            # Legacy shape {sym: ["t1"]} — tier known, level NOT. A None level
            # matches no live level, so it arms once and re-latches with a real
            # one. That is no worse than the shape it replaces, which re-armed
            # every single day by construction.
            out[str(sym)] = {str(k): None for k in tiers}
    return out


def tier_spent(entry, tier, level) -> bool:
    """True if `tier` was already taken AT THIS LEVEL. Pure. See carry_fired()."""
    if not isinstance(entry, dict) or tier not in entry:
        return False
    stored = entry[tier]
    return stored is not None and stored == _lvl(level)


def _selftest() -> None:
    from research_store.models import Thesis
    held = {"NVDA": Thesis(symbol="NVDA", rank=1, verdict="buy", stop=100.0,
                           targets=[120.0, 140.0], target_weight=0.07)}
    # stricter override: stop up, first target pulled in
    out = apply_overrides(held, {"NVDA": {"stop": 108.0, "targets": [118.0, 140.0]}})
    assert out["NVDA"].stop == 108.0 and out["NVDA"].targets[0] == 118.0, out["NVDA"]
    # a looser STOP is ignored without the explicit widen flag; the TARGET moves
    # freely (raising it adds no loss risk -- see the overlay's own comment).
    out = apply_overrides(held, {"NVDA": {"stop": 90.0, "targets": [130.0, 150.0]}})
    assert out["NVDA"].stop == 100.0, out["NVDA"]
    assert out["NVDA"].targets == [130.0, 150.0], out["NVDA"]
    # malformed per-symbol override (not a dict) is IGNORED, not raised
    out = apply_overrides(held, {"NVDA": "garbage"})
    assert out["NVDA"].stop == 100.0 and out["NVDA"].targets == [120.0, 140.0], out["NVDA"]
    # malformed whole-overrides object (not a dict) is IGNORED, not raised
    out = apply_overrides(held, ["nope"])
    assert out["NVDA"].stop == 100.0 and out["NVDA"].targets == [120.0, 140.0], out["NVDA"]
    # ⚠️ WIDENING A STOP IS OPT-IN, NOT IMPOSSIBLE. Refusing it left exactly one
    # compliant move when an inherited stop sits inside the noise -- close the
    # position -- turning a routine adjustment into a forced exit.
    out = apply_overrides(held, {"NVDA": {"stop": 90.0, "widen": True,
                                          "reason": "2.5 sigma sits inside the noise"}})
    assert out["NVDA"].stop == 90.0, out["NVDA"]
    # the flag alone is not enough: an unexplained loosening is refused
    assert apply_overrides(held, {"NVDA": {"stop": 90.0, "widen": True}})["NVDA"].stop == 100.0
    assert apply_overrides(held, {"NVDA": {"stop": 90.0, "widen": True,
                                           "reason": "  "}})["NVDA"].stop == 100.0
    # ...and a reason WITHOUT the flag is refused too -- the de-risk pass writes a
    # reason on every override, so a reason alone must never loosen a stop.
    assert apply_overrides(held, {"NVDA": {"stop": 90.0,
                                           "reason": "de-risk"}})["NVDA"].stop == 100.0
    # tightening never needs the flag
    assert apply_overrides(held, {"NVDA": {"stop": 108.0}})["NVDA"].stop == 108.0

    # ⚠️ TARGETS MOVE BOTH WAYS. Raising one adds no loss risk -- the stop is
    # unchanged -- so letting a winner run must not require permission. Pulling
    # one in only reduces exposure and never did.
    out = apply_overrides(held, {"NVDA": {"targets": [130.0, 160.0]}})
    assert out["NVDA"].targets == [130.0, 160.0], out["NVDA"]     # raised
    out = apply_overrides(held, {"NVDA": {"targets": [118.0, 130.0]}})
    assert out["NVDA"].targets == [118.0, 130.0], out["NVDA"]     # pulled in
    # a count mismatch is still ignored -- that is a malformed override, not a move
    assert apply_overrides(held, {"NVDA": {"targets": [130.0]}})["NVDA"].targets \
        == [120.0, 140.0]
    print("monitor selftest OK: stop widens only on an explicit reasoned flag; "
          "targets move both ways (letting a winner run needs no permission)")

    print("monitor selftest OK: stricter-only override overlay")

    # ---- SHARED TRUTH TABLE ------------------------------------------------
    # src/agent_env/decide.py mirrors this function's arithmetic and cannot
    # import it. Both sides assert against src/level_rules.py so a divergence
    # fails a test instead of quietly misinforming the agent.
    import level_rules                                        # noqa: PLC0415
    for c in level_rules.CASES:
        th = Thesis(symbol="AAA", rank=1, verdict="buy",
                    stop=c["thesis_stop"], targets=list(c["thesis_targets"]),
                    target_weight=0.07)
        got = apply_overrides({"AAA": th}, {"AAA": c["ov"]}, c.get("prices"))["AAA"]
        assert got.stop == c["stop_after"], (c["name"], got.stop)
        assert list(got.targets) == c["targets_after"], (c["name"], got.targets)
        assert (got.stop != c["thesis_stop"]) == c["stop_enforced"], c["name"]
        assert (list(got.targets) != c["thesis_targets"]) == c["target_enforced"], \
            c["name"]
    # ---- stops and targets must not fail the same way -----------------------
    _trig = [{"symbol": "MRK", "reason": "target1", "price": 153.1, "level": 148.07},
             {"symbol": "AMD", "reason": "stop", "price": 400.0, "level": 429.7},
             {"symbol": "MU", "reason": "target2", "price": 1500.0, "level": 1422.3}]
    _fire, _held = suppress_unowned_targets(_trig, stale=False)
    assert len(_fire) == 3 and _held == [], "a fresh snapshot suppresses nothing"
    _fire, _held = suppress_unowned_targets(_trig, stale=True)
    assert [t["symbol"] for t in _fire] == ["AMD"], _fire
    assert {t["symbol"] for t in _held} == {"MRK", "MU"}, _held
    assert all(t["reason"] == "stop" for t in _fire), \
        "a stale snapshot must NEVER suppress a stop -- that is the protection"
    assert suppress_unowned_targets([], True) == ([], [])
    print("monitor selftest OK: apply_overrides pinned against src/level_rules.CASES")
    print("monitor selftest OK: stale ownership holds targets, never stops")

    # ---- the fired latch: spent PER LEVEL, carried across books -----------
    # Regression for 2026-09-07: MU t1 1010 fired 09-04, the book rolled that
    # evening, and the old book_asof reset re-armed it for a second 50% trim.
    _mu = carry_fired({"MU": {"t1": 1010.0}})
    assert tier_spent(_mu["MU"], "t1", 1010.0) is True, "a spent level must stay spent"
    assert tier_spent(_mu["MU"], "t1", 1010.0000001) is True, "float noise must not re-arm"
    # the repair the close session made BY HAND for SNDK/INTC: move the level
    assert tier_spent(_mu["MU"], "t1", 1105.0) is False, "a MOVED target must re-arm"
    # the other rung is untouched, and an unknown symbol is never spent
    assert tier_spent(_mu["MU"], "t2", 1105.0) is False, "t2 is independent of t1"
    assert tier_spent(carry_fired({}).get("ZZZ") or {}, "t1", 5.0) is False
    # legacy {sym: ["t1"]} carries the tier with NO level -> arms once, safely
    _legacy = carry_fired({"MU": ["t1"]})
    assert _legacy == {"MU": {"t1": None}}, _legacy
    assert tier_spent(_legacy["MU"], "t1", 1010.0) is False, "unknown level must not suppress"
    # junk in state must never raise or silently suppress a stop
    assert carry_fired({"MU": "garbage"}) == {}, "unparseable entry is dropped"
    assert tier_spent({"stop": None}, "stop", 910.0) is False
    assert tier_spent({"stop": float("nan")}, "stop", 910.0) is False
    assert tier_spent({"stop": 910.0}, "stop", float("nan")) is False, "NaN never matches"
    print("monitor selftest OK: fired latch is per-level and survives a book roll")

    # ---- an ACCEPTED order is not a SALE ---------------------------------
    # Regression for 2026-09-07: the executor placed MU into a shut market and
    # reported status "queued"; reading it status-blind paged "MU SOLD".
    def _split(rows):
        p = {r["symbol"] for r in rows
             if str(r.get("status", "")).strip().lower() in UNEXECUTED_STATES}
        return {r["symbol"] for r in rows} - p, p

    _s, _p = _split([{"symbol": "MU", "status": "queued"}])
    assert (_s, _p) == (set(), {"MU"}), (_s, _p)          # THE finding
    _s, _p = _split([{"symbol": "MU", "status": "filled"}])
    assert (_s, _p) == ({"MU"}, set()), (_s, _p)
    _s, _p = _split([{"symbol": "MU", "status": "partially_filled"}])
    assert (_s, _p) == ({"MU"}, set()), "a partial fill IS a sale — it must be recorded"
    # ⛔ FAIL TOWARDS RECORDING. An absent/unknown status counts as sold: an
    # unrecorded fill leaves the ledger stale and the position unprotected,
    # which is far worse than running an idempotent recorder for nothing.
    _s, _p = _split([{"symbol": "MU"}])
    assert (_s, _p) == ({"MU"}, set()), "unknown status must fail towards recording"
    _s, _p = _split([{"symbol": "MU", "status": "CANCELLED"}])
    assert (_s, _p) == (set(), {"MU"}), "status match is case-insensitive"
    print("monitor selftest OK: a queued order is placed, not sold")

    # ⛔ WEIGHT 0 MEANS TWO DIFFERENT THINGS. A protective thesis (verdict
    # "hold") is a name the AGENT holds that the ranking did not select --
    # geometry supplied precisely so it CAN be watched. An "avoid" thesis
    # failed the reward:risk gate and is not held. Watching keyed on
    # target_weight>0 alone excluded both, so the protective geometry added
    # 2026-08-14 had a stop that nothing enforced.
    import types as _t
    assert in_book(_t.SimpleNamespace(target_weight=0.07, verdict="buy"))
    assert in_book(_t.SimpleNamespace(target_weight=0.0, verdict="hold")), \
        "a HELD protective thesis must be watched — that is why it exists"
    assert not in_book(_t.SimpleNamespace(target_weight=0.0, verdict="avoid")), \
        "an R:R-dropped name is not held and must not be watched"
    assert not in_book(_t.SimpleNamespace(target_weight=0.0, verdict=""))
    # and the unprotected report must agree: held + hold-thesis + stop = watched
    prot = Thesis(symbol="ZZZ", rank=200, verdict="hold", stop=9.0,
                  targets=[11.0], target_weight=0.0)
    rep = unprotected_positions([prot], {"ZZZ"})
    assert rep["unprotected"] == [], rep
    drop = Thesis(symbol="YYY", rank=99, verdict="avoid", stop=9.0,
                  targets=[11.0], target_weight=0.0)
    assert unprotected_positions([drop], {"YYY"})["unprotected"] == ["YYY"], \
        "an avoid thesis must still read as unprotected when held"
    print("monitor selftest OK: protective (weight-0, verdict hold) theses ARE "
          "watched; R:R-dropped (verdict avoid) are not")


    # holdings filter: only names actually held in RH are stop-watched. A book
    # name that was never bought (phantom) must be excluded so it can't fire an
    # un-fillable exit every tick (the AMAT infinite-loop bug, 2026-07-17).
    snap = {"positions": {"IWM": {"qty": 0.02}, "SPY": {"qty": 0.008},
                          "GONE": {"qty": 0.0}}}
    assert owned_symbols(snap) == {"IWM", "SPY"}, owned_symbols(snap)
    assert owned_symbols({"positions": {"X": 1.5}}) == {"X"}   # legacy dollars-at-cost
    # unreadable / absent snapshot → None → caller fails OPEN (keeps watching all)
    assert owned_symbols(None) is None
    assert owned_symbols({}) is None

    # refire gate: a failing exit backs off between retries and escalates once.
    from datetime import timezone as _tz
    t0 = datetime(2026, 1, 1, 15, 0, 0, tzinfo=_tz.utc)
    trg = [{"symbol": "MU", "reason": "stop", "fraction": 1.0, "price": 1.0, "level": 2.0}]
    # fresh breach → act, no escalation
    act, esc = refire_gate(trg, {}, t0, retry_secs=120, escalate_n=3)
    assert [t["symbol"] for t in act] == ["MU"] and esc == [], (act, esc)
    # failed once, only 30s later → suppressed (still in backoff)
    unr = {"MU": {"fails": 1, "last_try_ts": t0.isoformat(), "escalated": False}}
    act, esc = refire_gate(trg, unr, t0 + timedelta(seconds=30), 120, 3)
    assert act == [] and esc == [], (act, esc)
    # backoff elapsed → retry, still below escalate threshold
    act, esc = refire_gate(trg, unr, t0 + timedelta(seconds=180), 120, 3)
    assert [t["symbol"] for t in act] == ["MU"] and esc == [], (act, esc)
    # 3 prior fails + backoff elapsed → retry AND escalate once
    unr = {"MU": {"fails": 3, "last_try_ts": t0.isoformat(), "escalated": False}}
    act, esc = refire_gate(trg, unr, t0 + timedelta(seconds=200), 120, 3)
    assert esc == ["MU"], (act, esc)
    # already escalated → retry but no repeat escalation
    unr = {"MU": {"fails": 5, "last_try_ts": t0.isoformat(), "escalated": True}}
    act, esc = refire_gate(trg, unr, t0 + timedelta(seconds=200), 120, 3)
    assert [t["symbol"] for t in act] == ["MU"] and esc == [], (act, esc)
    # unknown result is terminal until broker state is reconciled: retrying a
    # fractional target can sell the same position repeatedly
    unr = {"MU": {"fails": 1, "last_try_ts": t0.isoformat(), "paused": True}}
    act, esc = refire_gate(trg, unr, t0 + timedelta(hours=1), 120, 3)
    assert act == [] and esc == [], (act, esc)
    print("monitor selftest OK: refire backoff + escalation gate")

    # ---- LAUNCH CEILING: the guard that was missing on 2026-08-19 ------------
    # The monitor spawns a MODEL to sell. Nothing counted those spawns, so one
    # trigger that would not resolve launched 27 Opus sessions in 68 minutes and
    # exhausted the account budget -- which then guaranteed every retry failed.
    # These exercise the FAILURE paths, because assuming the failure path is
    # what produced every defect this system has had.
    L0 = datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc)
    W, N = 3600, 6

    # room in an empty window
    allow, pruned = launch_gate([], L0, N, W)
    assert allow and pruned == []

    # exactly at the ceiling -> refused
    at_cap = [(L0 + timedelta(minutes=i)).isoformat() for i in range(N)]
    allow, pruned = launch_gate(at_cap, L0 + timedelta(minutes=N), N, W)
    assert not allow and len(pruned) == N, (allow, pruned)

    # a CEILING, not a latch: as launches age out the monitor resumes itself, so
    # a real cascade of stops in a falling market is delayed, never abandoned
    # (the LAST entry is at L0+5min, so the whole window clears at W+6min)
    allow, pruned = launch_gate(at_cap, L0 + timedelta(seconds=W, minutes=N + 1), N, W)
    assert allow and pruned == [], (allow, pruned)

    # partial ageing: only the entries still inside the window count
    mixed = [(L0 - timedelta(seconds=W + 10)).isoformat()] * 5 + at_cap[:2]
    allow, pruned = launch_gate(mixed, L0 + timedelta(minutes=2), N, W)
    assert allow and len(pruned) == 2, (allow, pruned)

    # ⛔ THE MRK LOOP, REPLAYED, WITH THE CALLER'S ALERT-ONCE RULE. 26 identical
    # re-arms 2.5 min apart -- what actually happened on 2026-08-19. The ceiling
    # must bound spawns for the whole hour AND alert exactly once. A first
    # implementation put the transition flag in the pure function, where it read
    # True on every poll while the window stayed full: 19 alerts, i.e. the
    # breaker reproducing the storm it exists to stop.
    st_sim, launches, spawned, alerts = {}, [], 0, 0
    for i in range(26):
        now = L0 + timedelta(minutes=2.5 * i)
        allow, launches = launch_gate(launches, now, N, W)
        if not allow:
            if "executor_breaker" not in st_sim:      # the caller's rule
                alerts += 1
                st_sim["executor_breaker"] = {"since": now.isoformat()}
        else:
            st_sim.pop("executor_breaker", None)
            spawned += 1
            launches.append(now.isoformat())
    assert spawned <= N + 2, f"ceiling leaked: {spawned} spawns in the hour"
    assert alerts == 1, f"alerted {alerts} times, must be exactly once per incident"
    # ...and 26 unbounded launches is what the absence of this guard cost
    assert spawned < 26 // 2, f"{spawned} spawns is not a meaningful bound"

    # a corrupt persisted entry is DROPPED, never counted as room to spawn
    allow, pruned = launch_gate(["not-a-timestamp", None, 42] + at_cap,
                                L0 + timedelta(minutes=N), N, W)
    assert not allow and len(pruned) == N, (allow, pruned)

    # a ceiling of 0 refuses everything -- an operator kill switch for the path
    allow, _ = launch_gate([], L0, 0, W)
    assert not allow

    print("monitor selftest OK: executor launch ceiling bounds model spend, "
          "alerts exactly once per incident, releases as launches age out, and "
          "holds against a replay of the 2026-08-19 MRK loop")

    # feed-failure escalation: recover on transients, alert at threshold, exit at cap
    assert _feed_action(0, 4, 12) == "recover"
    assert _feed_action(3, 4, 12) == "recover"        # below alert band
    assert _feed_action(4, 4, 12) == "alert"          # crosses alert threshold
    assert _feed_action(11, 4, 12) == "alert"         # still in alert band
    assert _feed_action(12, 4, 12) == "exit"          # hits exit cap
    assert _feed_action(99, 4, 12) == "exit"
    # feed-alert cooldown: fires first time, suppressed within window, re-fires after
    from datetime import timezone as _tzf
    nf = datetime(2026, 1, 1, 15, 0, 0, tzinfo=_tzf.utc)
    assert _feed_alert_due(nf, None, 1800) is True                       # never fired
    assert _feed_alert_due(nf, nf.isoformat(), 1800) is False            # just fired
    assert _feed_alert_due(nf + timedelta(seconds=1799), nf.isoformat(), 1800) is False
    assert _feed_alert_due(nf + timedelta(seconds=1801), nf.isoformat(), 1800) is True
    assert _feed_alert_due(nf, "garbage", 1800) is True                  # unparseable → fire
    print("monitor selftest OK: feed-failure recover/alert/exit + cooldown")
    assert owned_symbols({"positions": "torn"}) is None
    print("monitor selftest OK: holdings filter (phantom-holding guard)")

    # unprotected-position invariant (design spec 2026-08-09 §8): every held
    # name has an agent-set stop, or it is loudly flagged. Pure classification.
    protected = Thesis(symbol="NVDA", rank=1, verdict="buy", stop=100.0,
                       targets=[120.0], target_weight=0.07)
    no_stop = Thesis(symbol="TSLA", rank=2, verdict="buy", stop=None,
                     targets=[], target_weight=0.05)
    book = [protected, no_stop]
    # held symbol with NO thesis at all (AAPL was never in the book)
    out = unprotected_positions(book, {"NVDA", "AAPL"})
    assert out["unprotected"] == ["AAPL"] and out["suspect_empty_snapshot"] is False, out
    # held symbol whose thesis carries no stop
    out = unprotected_positions(book, {"NVDA", "TSLA"})
    assert out["unprotected"] == ["TSLA"] and out["suspect_empty_snapshot"] is False, out
    # normal all-protected case -> nothing to flag
    out = unprotected_positions(book, {"NVDA"})
    assert out["unprotected"] == [] and out["suspect_empty_snapshot"] is False, out
    # well-formed EMPTY snapshot while the book expects holdings -> suspect the
    # snapshot, don't panic about stops (usually a stale/partial write)
    out = unprotected_positions(book, set())
    assert out["unprotected"] == [] and out["suspect_empty_snapshot"] is True, out
    # well-formed empty snapshot with an EMPTY book too -> genuinely nothing wrong
    # verdict "avoid", not "hold": since 2026-08-14 `hold` means HELD BY THE
    # AGENT (a protective thesis the ranking did not select) and in_book() counts
    # it, so a fixture standing for "the book expects no holdings" must use the
    # verdict that actually means not-held. This assertion caught the collision.
    flat_book = [Thesis(symbol="X", rank=1, verdict="avoid", stop=None,
                        targets=[], target_weight=0.0)]
    out = unprotected_positions(flat_book, set())
    assert out["unprotected"] == [] and out["suspect_empty_snapshot"] is False, out
    # torn/unreadable snapshot (owned=None) -> caller already fails open and
    # watches every thesis, so there is nothing NEW to flag here
    out = unprotected_positions(book, None)
    assert out["unprotected"] == [] and out["suspect_empty_snapshot"] is False, out
    # --- corporate-action guard (2026-08-10) --------------------------------
    # An ordinary breach must STILL fire — the guard must not blunt the stop.
    assert corporate_action_suspected(99.0, 100.0, 130.0) is None
    assert corporate_action_suspected(90.0, 100.0, 130.0) is None
    assert corporate_action_suspected(60.0, 100.0, 130.0) is None, \
        "a 40% gap is a crash, not a split — must still sell"
    # A 2:1 split lands at ~50% of the stop -> suspected, not actioned.
    assert corporate_action_suspected(50.0, 100.0, 130.0)
    assert "split" in corporate_action_suspected(25.0, 100.0, 130.0)   # 4:1
    # Reverse split multiplies -> must not read as a target hit.
    assert corporate_action_suspected(1300.0, 100.0, 130.0)            # 1:10
    assert corporate_action_suspected(260.0, 100.0, 130.0)             # 1:2
    assert corporate_action_suspected(140.0, 100.0, 130.0) is None, \
        "a normal target overshoot must still take profit"
    # Degenerate inputs must not raise or suppress.
    assert corporate_action_suspected(None, 100.0, 130.0) is None
    assert corporate_action_suspected(0.0, 100.0, 130.0) is None
    assert corporate_action_suspected(50.0, None, None) is None
    assert corporate_action_suspected(50.0, 0.0, 0.0) is None
    print("monitor selftest OK: corporate-action guard (split suppressed, "
          "real crash and real target still act)")

    # ⚠️ EVERY POSITION CARRIES BOTH LEVELS. A stop bounds the loss; a target is
    # the decision about when the trade is finished, made in advance instead of
    # in the moment. A stop-only position is NOT fully protected and must be
    # announced -- separately, because the urgency differs.
    b_ok = Thesis(symbol="AAA", rank=1, verdict="buy", stop=10.0,
                  targets=[20.0], target_weight=0.1)
    b_notgt = Thesis(symbol="BBB", rank=2, verdict="buy", stop=10.0,
                     targets=[], target_weight=0.1)
    b_nostop = Thesis(symbol="CCC", rank=3, verdict="buy", stop=None,
                      targets=[20.0], target_weight=0.1)
    out = unprotected_positions([b_ok, b_notgt, b_nostop], {"AAA", "BBB", "CCC"})
    assert out["unprotected"] == ["CCC"], out          # no stop = unbounded risk
    assert out["no_target"] == ["BBB"], out            # stop but no finish line
    # a fully-levelled book raises neither
    out = unprotected_positions([b_ok], {"AAA"})
    assert out["unprotected"] == [] and out["no_target"] == [], out
    # a name with NO stop is reported once, as unprotected -- never twice
    assert "CCC" not in unprotected_positions(
        [b_nostop], {"CCC"})["no_target"]
    # torn snapshot still fails open on BOTH findings
    out = unprotected_positions([b_notgt], None)
    assert out["unprotected"] == [] and out["no_target"] == [], out
    print("monitor selftest OK: every position needs BOTH a stop and a target "
          "(missing target announced separately, no double-report, fail-open)")

    # ---- AGENT-SET STOP WITH NO THESIS IS WATCHED --------------------------
    # The defect this closes: theses come from the NIGHTLY slow loop, so a
    # position opened intraday was unwatched for the rest of the session no
    # matter what stop the agent wrote. Twice in two days (BAC/FTNT 08-19,
    # JNJ 08-20).
    ov_solo = {"JNJ": {"stop": 261.0, "targets": [279.5, 288.5]},
               "NOSTOP": {"targets": [10.0]},
               "JUNK": {"stop": "not-a-number"},
               "NEG": {"stop": -5.0}}
    cands = standalone_candidates({"JNJ", "NOSTOP", "JUNK", "NEG", "HASTH"},
                                  ov_solo, {"HASTH"})
    assert set(cands) == {"JNJ"}, cands          # only a real, positive stop counts
    # ⛔ UNKNOWN OWNERSHIP FAILS OPEN, like the thesis watch set. Returning {}
    # here left an override-only position neither armed NOR reported
    # unprotected -- invisible on both sides exactly when ownership is
    # uncertain. Watching a name we may not own costs nothing: the exit
    # executor re-reads the broker and skips a zero position.
    assert set(standalone_candidates(None, ov_solo, set())) == {"JNJ"}, \
        "stale/unreadable ownership must fail OPEN, not drop the watch"
    assert standalone_candidates(None, ov_solo, {"JNJ"}) == {}, \
        "...but a name that already has a thesis is still not duplicated"
    assert standalone_candidates(set(), ov_solo, set()) == {}, \
        "a KNOWN-empty book holds nothing to watch"
    # inf/NaN/bool/numeric-string handling on the stop
    assert standalone_candidates({"A"}, {"A": {"stop": float("inf")}}, set()) == {}
    assert standalone_candidates({"A"}, {"A": {"stop": float("nan")}}, set()) == {}
    assert standalone_candidates({"A"}, {"A": {"stop": True}}, set()) == {}
    assert set(standalone_candidates({"A"}, {"A": {"stop": "250"}}, set())) == {"A"}
    assert standalone_candidates({"JNJ"}, "garbage", set()) == {}, "torn file -> nothing"
    # a symbol that ALREADY has a thesis is never duplicated here
    assert standalone_candidates({"HASTH"}, {"HASTH": {"stop": 1.0}}, {"HASTH"}) == {}

    armed_solo, refused = arm_standalone(cands, {"JNJ": 270.02})
    assert set(armed_solo) == {"JNJ"} and not refused, (armed_solo, refused)
    j = armed_solo["JNJ"]
    assert j.stop == 261.0 and j.targets == [279.5, 288.5], j
    assert j.verdict == "hold" and j.target_weight == 0.0, j
    assert in_book(j) and j.stop, "an armed standalone must pass the watch predicate"

    # ⛔ THE LIQUIDATION GUARD. A stop at or above spot must NEVER arm: the
    # monitor would read it as an instant breach and market-sell the whole
    # position without any order having been placed (2026-08-12, AMD 474.00
    # against a 473.65 mark). Refuse, alarm, stay unprotected.
    _, ref_hi = arm_standalone(cands, {"JNJ": 260.0})
    assert set(ref_hi) == {"JNJ"} and "at or above spot" in ref_hi["JNJ"], ref_hi
    _, ref_eq = arm_standalone(cands, {"JNJ": 261.0})
    assert set(ref_eq) == {"JNJ"}, "stop EQUAL to spot must also refuse"
    # ...and an unknown price is refused rather than guessed
    _, ref_np = arm_standalone(cands, {})
    assert set(ref_np) == {"JNJ"} and "no live price" in ref_np["JNJ"], ref_np
    _, ref_bad = arm_standalone(cands, {"JNJ": "x"})
    assert set(ref_bad) == {"JNJ"}, "unreadable price must refuse"

    # a target-less agent stop still arms -- a stop with no target is legal
    solo_nt = standalone_candidates({"Z"}, {"Z": {"stop": 5.0}}, set())
    a_nt, _ = arm_standalone(solo_nt, {"Z": 6.0})
    assert a_nt["Z"].targets == [], a_nt["Z"].targets

    # ...and the invariant report must count it PROTECTED, not raise a false alarm
    u = unprotected_positions([], {"JNJ"}, extra_watched={"JNJ"},
                              extra_targets={"JNJ": [279.5]})
    assert u["unprotected"] == [] and u["no_target"] == [], u
    u2 = unprotected_positions([], {"JNJ"}, extra_watched={"JNJ"},
                               extra_targets={"JNJ": []})
    assert u2["unprotected"] == [] and u2["no_target"] == ["JNJ"], u2
    # with no extra_watched it is still reported unprotected (the old behaviour)
    assert unprotected_positions([], {"JNJ"})["unprotected"] == ["JNJ"]
    print("monitor selftest OK: an agent-set stop with NO thesis is watched, and a "
          "stop at/above spot is REFUSED rather than fired")

    print("monitor selftest OK: unprotected-position invariant (held-no-thesis, "
          "held-no-stop, empty-snapshot suspicion, torn-snapshot fail-open, "
          "all-protected no-alert)")

    # ---- wakes: registered by the agent, fired by nothing until now --------
    # wakes.due() was written, selftested and had NO CALLER anywhere. A session
    # could register a wake, get an ok, and be woken by nothing.
    import tempfile as _tf, json as _js
    _real_wakes, _real_notify = WAKES, notify
    _pushes, _spawns, _journalled = [], [], []
    with _tf.TemporaryDirectory() as _d:
        wf = Path(_d) / "wakes.json"
        try:
            globals()["WAKES"] = wf
            globals()["notify"] = lambda *a, **k: _pushes.append(a)
            # ⛔ AND THE JOURNAL. Stubbing the wakes FILE and the notifier is not
            # enough: _fire_wakes also calls store.append_journal, which writes
            # the REAL research_store/journal.jsonl. Every run of this suite
            # appended a fabricated `wake_fired` event for NVDA at 505.0 to the
            # live append-only ledger -- 15 of them on 2026-08-12 before it was
            # noticed, indistinguishable from real ones at a glance. A test that
            # writes production state is not a test, it is a mutation.
            _real_append = store.append_journal
            store.append_journal = lambda ev: _journalled.append(ev)

            # no file at all -> no symbols, no crash, no fire
            assert _wake_symbols() == set()
            assert _fire_wakes({"NVDA": 100.0}, False) == []

            wf.write_text(_js.dumps({
                "NVDA|above|500": {"symbol": "NVDA", "direction": "above",
                                   "level": 500.0, "budget": 1, "fired": 0,
                                   "reason": "re-entry level"},
                "MU|below|50":    {"symbol": "MU", "direction": "below",
                                   "level": 50.0, "budget": 1, "fired": 0,
                                   "reason": "add level"}}))
            # a wake symbol we do NOT hold is still watched -- the whole point
            assert _wake_symbols() == {"NVDA", "MU"}, _wake_symbols()

            # unmet condition fires nothing
            assert _fire_wakes({"NVDA": 400.0, "MU": 60.0}, False) == []
            assert _pushes == []

            # met condition fires, pushes, and DOES NOT spawn when not armed
            got = _fire_wakes({"NVDA": 505.0, "MU": 60.0}, False)
            assert len(got) == 1 and got[0]["symbol"] == "NVDA", got
            assert len(_pushes) == 1, _pushes
            assert "ALERT ONLY" in _pushes[0][1], _pushes[0]

            # BUDGET IS SPENT: the same price must not re-fire every 15s poll
            assert _fire_wakes({"NVDA": 505.0}, False) == [], "wake re-fired"
            assert len(_pushes) == 1, "a spent wake pushed again"

            # the POLL GUARD itself, not a paraphrase of it: an empty book with
            # a live wake must still poll. A mutation back to `if not held`
            # passed the entire suite before this was extracted.
            assert _should_poll({}, {"NVDA"}) is True, \
                "an all-cash book with a live wake must still be polled"
            assert _should_poll({"MU": 1}, set()) is True
            assert _should_poll({"MU": 1}, {"NVDA"}) is True
            assert _should_poll({}, set()) is False, "nothing to watch -> skip"
            # the journal entry is part of the contract -- assert it, having
            # made sure it lands in a list rather than the live ledger
            assert len(_journalled) == 1, _journalled
            assert _journalled[0]["event"] == "wake_fired", _journalled[0]
            assert _journalled[0]["symbol"] == "NVDA", _journalled[0]
        finally:
            globals()["WAKES"] = _real_wakes
            globals()["notify"] = _real_notify
            store.append_journal = _real_append
    # ---- a wake must not outlive the position it guards --------------------
    # RTX was sold at 11:53 on 2026-08-20 and its wake fired at 13:54, twice,
    # fifteen seconds apart -- two model sessions spent waking an agent about a
    # position that no longer existed.
    import tempfile as _tfw
    with _tfw.TemporaryDirectory() as _dw:
        _orig_wakes = WAKES
        try:
            globals()["WAKES"] = Path(_dw) / "wakes.json"
            _save(WAKES, {
                "RTX:below:214.5": {"symbol": "RTX", "level": 214.5,
                                    "direction": "below", "budget": 2, "fired": 0},
                "rtx:above:230": {"symbol": "rtx", "level": 230.0,
                                  "direction": "above", "budget": 1, "fired": 0},
                "NVDA:above:900": {"symbol": "NVDA", "level": 900.0,
                                   "direction": "above", "budget": 1, "fired": 0}})
            gone = drop_wakes_for("RTX")
            assert len(gone) == 2, gone          # case-insensitive on the symbol
            left = _load(WAKES, {})
            assert set(left) == {"NVDA:above:900"}, left
            # a re-entry wake on a name we do NOT hold is legitimate and stays
            assert drop_wakes_for("RTX") == [], "second call must be a no-op"
            # an unreadable path must not take down the tick that just sold
            globals()["WAKES"] = Path(_dw) / "nope" / "wakes.json"
            assert drop_wakes_for("RTX") == []
        finally:
            globals()["WAKES"] = _orig_wakes
    print("monitor selftest OK: a full exit retires that symbol's wakes (a wake "
          "must not spend a model session on a position that is gone)")

    print("monitor selftest OK: wakes fire on unheld symbols, respect budget, "
          "and alert-only when not armed")


def newest_feed_ts(quotes: dict):
    """The newest `update_time` across the whole book, or None. Pure.

    A string compare is a time compare here: moomoo stamps
    `YYYY-MM-DD HH:MM:SS[.ffffff]`, which sorts lexicographically in
    chronological order. That matters, because it means this never has to
    PARSE the value — and therefore never has to know its timezone, which is
    undocumented and unresolved (`20:02:19` on a shut market is equally
    consistent with 16:02 ET stamped UTC and 20:02 ET).
    """
    best = None
    for blk in (quotes or {}).values():
        ts = (blk or {}).get("ts")
        if not ts:
            continue
        ts = str(ts)
        if best is None or ts > best:
            best = ts
    return best


def feed_stalled(st: dict, newest, limit: int) -> bool:
    """Has the feed's OWN clock stopped advancing? Mutates st['feed']. -> bool.

    ⛔ WHY THIS COMPARES THE FEED AGAINST ITSELF, NOT AGAINST OUR CLOCK.
    The failure being caught is a successful response carrying stale prices —
    `QuoteFeedError` and the feed-down ladder only fire when the CALL fails,
    so a wedged-but-answering OpenD reads as perfectly healthy. Checking
    freshness against wall-clock time needs `update_time`'s timezone, which is
    not documented anywhere and which an earlier attempt at this treated as a
    blocker. It is not one: a wedged feed is a timestamp that stops moving, and
    you do not need to know what timezone a clock is in to see it is not
    ticking. This is therefore immune to that ambiguity, to DST, and to
    half-day closures alike.

    BOOK-WIDE, NEVER PER-SYMBOL. A single thin name can legitimately go minutes
    without a print, so per-symbol this would cry wolf constantly. If NOTHING
    in the entire book has printed in `limit` consecutive polls of a supposedly
    open market, that is the feed, not the market.

    One mechanism, both real cases: a market holiday (frozen at the previous
    session's close) and OpenD wedging mid-session (frozen at the minute it
    hung). `limit` is counted in POLLS, so it scales with poll_secs.
    """
    f = st.setdefault("feed", {})
    if newest is None:
        # No symbol carried a timestamp. Do NOT call that a stall: a feed that
        # never reports one would then look permanently wedged and the ladder
        # would restart forever. Absent is unknown; unknown is not evidence.
        f["stalls"] = 0
        return False
    if f.get("newest") == newest:
        f["stalls"] = int(f.get("stalls", 0)) + 1
    else:
        f["newest"] = newest
        f["stalls"] = 0
    return f["stalls"] >= limit


def _last_price(block: dict):
    """Extract a usable mark from one quote block, or None.

    `last` is the moomoo shape (adapters.moomoo.prices.live_quotes). The camelCase
    keys are the old Schwab shape, kept as a fallback so an on-disk quotes.json
    written before the feed switch still reads back. Only a POSITIVE number counts —
    a 0.0 must never become a price, because it would read as a total loss and fire
    a market sell.

    ⛔ AND ONLY A FINITE NUMBER. `v > 0` is true for `inf`, so a corrupt quote
    could return infinity as a price -- which compares above EVERY stop and
    would therefore arm any standalone watch and satisfy any take-profit.
    Found by the independent reviewer, 2026-08-20. `bool` is excluded too:
    it is a subclass of int, so `True` would otherwise read as a price of 1.0.
    """
    q = (block or {}).get("quote", {}) or block or {}
    for k in ("last", "lastPrice", "mark", "closePrice", "bidPrice"):
        v = q.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and math.isfinite(v) and v > 0:
            return float(v)
    return None


import copy


def suppress_unowned_targets(triggers: list, stale: bool) -> tuple:
    """Split triggers into (fire_now, suppressed) when ownership is unverifiable.

    ⛔ A STOP AND A TARGET MUST NOT FAIL THE SAME WAY.
    When positions.json predates the newest journalled execution, check_once
    FAILS OPEN: it disables the ownership filter and watches every eligible
    thesis. That is right for a stop -- an old ownership set must never exclude
    a newly-bought real position from the only protection it has, and a missed
    stop is an unbounded loss.

    It is WRONG for a target. A target is profit-taking, not protection: missing
    one costs foregone upside and nothing else. Firing one against an ownership
    set known to be out of date is how the same position gets sold twice --
    which is exactly the 2026-08-19 incident, where "a fractional TARGET was
    retried against the reduced current quantity", and exactly what happened to
    MRK at 19:19:46 that day, 47 seconds after the session had already closed
    the whole position. The trade is asymmetric, so the failure mode must be.

    Pure, so the asymmetry is asserted in the selftest rather than trusted.
    """
    if not stale:
        return list(triggers), []
    fire = [t for t in triggers if t.get("reason") == "stop"]
    held = [t for t in triggers if t.get("reason") != "stop"]
    return fire, held


def apply_overrides(held: dict, overrides: dict, prices: dict | None = None) -> dict:
    """Overlay stricter-only risk-review geometry onto held theses (copies).
    Stop may only be raised; each target may only be lowered. Looser or malformed
    overrides are ignored — a bad file can never loosen a live stop, and can never
    abort the whole tick (a malformed entry degrades to "no override" for that
    symbol, or for the whole book if `overrides` itself isn't a dict).

    `prices` is `{symbol: last_known_price}` -- see the guard inline below. It
    defaults to None for backward compatibility with callers that predate the
    apply-time price guard (Part 3, 2026-08-17) and are not exercising it; the
    ONE live production call site (this module's poll loop) always passes an
    actual dict, possibly empty on a torn read, which is exactly what makes the
    guard fail closed there."""
    if not isinstance(overrides, dict):
        return dict(held)
    out = {}
    for sym, th in held.items():
        ov = overrides.get(sym)
        if not ov or not isinstance(ov, dict):
            out[sym] = th
            continue
        try:
            t = copy.copy(th)
            # STRICTER BY DEFAULT: a stop may only be raised. That guard exists
            # so a de-risk pass -- which never sets `widen` -- cannot loosen a
            # live stop by accident, and so a malformed file can never weaken
            # protection.
            #
            # WIDENING IS OPT-IN, NOT IMPOSSIBLE. An inherited stop can sit
            # inside the name's own noise, where it is not protection but a
            # guarantee of being taken out by nothing. Refusing to widen it left
            # exactly one compliant move -- close the position -- turning a
            # routine adjustment into a forced exit. So a loosening is honoured
            # ONLY when the override says so explicitly AND carries a reason.
            # Deliberate, attributable, and announced by the caller; never a
            # silent side effect of a stray number.
            ov_stop = ov.get("stop")
            # ⛔ A STOP AT OR ABOVE THE CURRENT PRICE IS NOT A STOP -- it is
            # already breached, and this process sells the whole position at
            # market within one poll. set_levels refuses exactly this when a
            # level is SET; an override that outlives its position and wakes on
            # RE-ENTRY never passes through that check, so the same invariant
            # has to hold here. Levels no longer expire (principal, 2026-08-17),
            # which makes a stale override a permanent possibility rather than a
            # transient one.
            #
            # FAIL CLOSED: with no price we refuse the override and keep the
            # thesis stop -- the position stays protected, just less tightly.
            # Applying a stale stop instead would liquidate it. Refusing can
            # never unprotect; applying wrongly can.
            #
            # `prices is None` (the default) means the CALLER passed no price
            # information at all -- backward compatibility for callers that
            # predate this guard. The live call site (below, in the poll loop)
            # always passes a real dict, possibly empty on a torn read, so THIS
            # tick's guard is always active there: a symbol absent from that
            # dict is exactly "no usable price" and is refused just like an
            # unfavourable one.
            if isinstance(ov_stop, (int, float)) and prices is not None:
                px = prices.get(sym) if isinstance(prices, dict) else None
                # ⛔ NaN IS NOT A USABLE PRICE. `px <= 0` and `stop >= px` are
                # BOTH False for a NaN px -- every comparison against NaN is
                # False in Python -- so a bare isinstance+<=0 guard let a
                # corrupt quote sail straight through and the stale override
                # was APPLIED. src/marks.py already treats a NaN/inf mark as
                # "a corrupt monitor quote" and refuses to use it (FIX B,
                # 2026-08-10); this mirrors that reasoning for the same
                # failure mode reaching the stop guard instead of the
                # valuation path. Found by the reviewer: price NaN -> stop
                # 120.0 applied over a thesis stop of 100.0.
                if (not isinstance(px, (int, float)) or not math.isfinite(px)
                        or px <= 0 or float(ov_stop) >= float(px)):
                    ov_stop = None
            # math.isfinite, not just isinstance: NaN and +/-inf ARE floats, so
            # an isinstance check alone let a NaN stop through -- and because
            # every comparison against NaN is False, `ov_stop > t.stop` was
            # False while an explicit widen still applied it, arming the stop
            # watcher on a level no price can ever satisfy. Found by review,
            # 2026-08-18; src/levels.py rejected it and the enforcer did not.
            if (isinstance(ov_stop, (int, float)) and math.isfinite(ov_stop)
                    and t.stop is not None):
                widen = bool(ov.get("widen")) and str(ov.get("reason") or "").strip()
                if ov_stop > t.stop or widen:
                    t.stop = float(ov_stop)
            # TARGETS MOVE IN EITHER DIRECTION, FREELY.
            #
            # This was min() -- a target could only ever be pulled IN. Combined
            # with a stop that could only be raised, BOTH permitted directions
            # shortened the trade and both blocked directions let it run: a
            # one-way ratchet toward cutting early, which is this model's
            # documented bias encoded in code rather than chosen.
            #
            # Raising a target increases NO risk of loss. The stop is unchanged,
            # the downside is identical; it costs only unrealised gain. There is
            # no safety argument for blocking it, and blocking it is why nothing
            # in this book has ever reached a take-profit. Pulling a target in
            # needs no permission either -- it only ever reduces exposure.
            # ⛔ `t.targets` USED TO BE REQUIRED TRUTHY HERE, which meant the
            # agent could only ever REPLACE targets the loop had already
            # generated -- never supply them where the loop had none. Once the
            # loop stopped generating take-profits (2026-08-25, so that profit
            # levels are the agent's judgement rather than a 5.5-sigma formula
            # nothing ever reached), that condition would have made an
            # agent-set target permanently unenforceable: silently dropped
            # here, while positions() showed it as set. Exactly the
            # display/enforcer divergence src/levels.py exists to prevent.
            #
            # So: with NO thesis targets, the agent's list is taken verbatim --
            # it is the only take-profit that exists. With thesis targets
            # present, the count must still match, because a partial list on a
            # two-tier thesis is ambiguous about which tier was meant.
            ot = ov.get("targets")
            if (isinstance(ot, list) and ot
                    and (not t.targets or len(ot) == len(t.targets))
                    and all(isinstance(o, (int, float)) and math.isfinite(o)
                            for o in ot)):
                t.targets = [float(o) for o in ot]
            out[sym] = t
        except Exception:
            out[sym] = th
    return out


def owned_symbols(snap) -> set | None:
    """Symbols actually held (qty>0) in an RH positions snapshot dict.

    Returns None when the snapshot can't be interpreted (absent / torn / wrong
    shape) — the caller then FAILS OPEN and keeps watching every thesis, because
    silently dropping stop protection is worse than a rare re-fire. When it IS
    readable, this is the guard that keeps a book name that was never actually
    bought (a phantom holding) out of the stop-watch list, so it can't fire an
    un-fillable exit every tick."""
    if not isinstance(snap, dict) or not isinstance(snap.get("positions"), dict):
        return None
    out = set()
    for sym, p in snap["positions"].items():
        qty = p.get("qty") if isinstance(p, dict) else p   # {qty,...} or legacy dollars
        try:
            if float(qty or 0) > 0:
                out.add(sym)
        except (TypeError, ValueError):
            continue
    return out


def _publish_enforcement(held, pre, prices, book_asof) -> None:
    """Publish what apply_overrides ACTUALLY applied, for readers to trust.

    ⛔ NOTHING HERE MAY PREVENT A SELL. This is a reporting side-effect sitting
    on the poll path that ends in a stop scan, so every failure is swallowed. An
    earlier version called _save() unguarded: _save does a direct write_text
    that can raise, the outer loop only prints "loop error (continuing)", and a
    path-specific write failure would therefore have skipped the entire stop
    scan every tick while looking like a transient. A report that can stop the
    stop is worse than no report. Found by review, 2026-08-18.
    """
    try:
        _save(MON / "enforcement.json", {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "poll_id": f"{book_asof}:{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            "book_asof": book_asof,
            "quotes_seen": sorted(prices.keys()) if isinstance(prices, dict) else [],
            "levels": {
                s: {
                    "effective_stop": getattr(th, "stop", None),
                    "effective_targets": list(getattr(th, "targets", []) or []),
                    "thesis_stop": pre.get(s, (None, []))[0],
                    "thesis_targets": pre.get(s, (None, []))[1],
                    "override_applied": (
                        getattr(th, "stop", None) != pre.get(s, (None, []))[0]
                        or list(getattr(th, "targets", []) or []) != pre.get(s, (None, []))[1]),
                }
                for s, th in held.items()
            },
        })
    except Exception:                                           # noqa: BLE001
        pass        # never let a report block the scan that places the sell


def unprotected_positions(theses, owned, extra_watched=None,
                          extra_targets=None) -> dict:
    """Classify the invariant in docs/superpowers/specs/2026-08-09-agent-authority
    -inversion-design.md §8: "every position has an agent-set stop, or it is
    loudly flagged unprotected." Pure — no I/O.

    `theses` = the current book's FULL thesis list (`prod.theses`, unfiltered —
    not the `held` dict, which has already been narrowed to what we watch).
    `owned` = `owned_symbols()`'s result: the set of symbols actually held per
    the broker snapshot, or None when the snapshot could not be read (torn /
    absent / wrong-shape). On None the caller already fails open and watches
    every thesis, so there is nothing NEW to flag — this returns quiet.

    Two distinct findings on purpose, because the operator's response differs:

      "unprotected" — owned symbols with no thesis at all, or a thesis that
        carries no stop, or a thesis whose weight has gone to zero. These are
        genuinely unwatched right now: set a stop or exit by hand.

      "suspect_empty_snapshot" — the snapshot is well-formed but reports ZERO
        holdings while the book expects some (>=1 thesis with target_weight>0).
        That combination is far more often a stale/partial/failed snapshot
        write (prompts/exit.md step 7 rewrites this file after selling) than a
        genuinely flat account — the right response is to check the snapshot,
        not to panic about stops.
    """
    if owned is None:
        return {"unprotected": [], "no_target": [], "suspect_empty_snapshot": False}
    # `extra_watched` = positions watched on an AGENT-SET stop with no thesis
    # (see standalone_candidates). They ARE protected, so reporting them
    # unprotected would be a false alarm -- and a false alarm here is not
    # harmless: this is the row the operator scans for the real ones.
    watched = {t.symbol for t in theses if in_book(t) and t.stop} | set(extra_watched or ())
    targets_extra = {sym for sym, tg in (extra_targets or {}).items() if tg}
    if not owned:
        expects_holdings = any(in_book(t) for t in theses)
        return {"unprotected": [], "no_target": [],
                "suspect_empty_snapshot": expects_holdings}
    # A position needs BOTH levels. A stop bounds the loss; a target is the
    # decision about when the trade is finished, made in advance rather than in
    # the moment. Reported SEPARATELY from `unprotected` because the urgency
    # differs -- a missing stop is unbounded risk right now, a missing target is
    # an unfinished decision -- but both are announced, because the standing
    # instruction is that every position carries both and neither is optional.
    targeted = {t.symbol for t in theses
                if in_book(t) and t.stop and (t.targets or [])} | targets_extra
    return {"unprotected": sorted(owned - watched),
            "no_target": sorted((owned & watched) - targeted),
            "suspect_empty_snapshot": False}


def _finite_pos(v):
    """-> float if `v` is a finite number strictly above zero, else None.

    ⛔ `v > 0` IS NOT ENOUGH. `float("inf") > 0` is True, so infinity passed the
    old stop validation; as a stop it sits above every price (nothing can breach
    it) and as a price it sits above every stop (everything arms). `bool` is
    excluded because it is an int subclass and `True` would read as 1.0.
    """
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def standalone_candidates(owned, overrides, thesis_syms) -> dict:
    """Owned positions carrying an AGENT-SET stop but no thesis. -> {sym: ov}.

    ⛔ WHY THIS EXISTS. The monitor's watch set was theses ∩ owned, and theses
    are written by the NIGHTLY slow loop -- so a position opened intraday was
    unwatched for the rest of that session no matter what stop the agent wrote.
    `set_levels` said so out loud ("no thesis for this symbol -- the monitor is
    not watching it at all") and the position stayed unprotected anyway. It
    surfaced on 2026-08-19 and again on 2026-08-20 (JNJ, bought 10:40, stop
    261.00 written and enforced by nothing).

    An agent-set stop IS a stop. The stricter-only override machinery needs a
    base thesis to be stricter THAN, but that is an argument about how to merge
    two stops, not a reason to enforce neither.

    ⛔ FAILS OPEN ON UNKNOWN OWNERSHIP, like the thesis path. `owned is None`
    means the positions snapshot was stale or unreadable, and the thesis watch
    set deliberately fails OPEN there ("an old ownership set must never exclude
    a newly bought real position from software stop coverage"). This returned
    {} in that case, so an override-only position was neither armed NOR
    reported unprotected -- invisible on both sides at exactly the moment
    ownership is uncertain. Found by the independent reviewer, 2026-08-20.
    Watching a name we may not own costs nothing: the exit executor re-reads
    the broker and skips a position that is already zero.

    Pure: no clock, no I/O. Expiry is handled by the caller's override reader.
    """
    if not isinstance(overrides, dict):
        return {}
    if owned is None:                    # ownership unknown -> fail OPEN
        cands = sorted(set(overrides) - set(thesis_syms))
    elif not owned:
        return {}
    else:
        cands = sorted(set(owned) - set(thesis_syms))
    out = {}
    for sym in cands:
        ov = overrides.get(sym)
        if not isinstance(ov, dict):
            continue
        if _finite_pos(ov.get("stop")) is None:
            continue                     # NaN, inf, <=0, non-numeric: not a stop
        out[sym] = ov
    return out


def arm_standalone(candidates: dict, prices: dict, start_rank: int = 900):
    """Turn override-only candidates into watchable theses. -> (armed, refused).

    ⛔ THE PRICE GUARD IS THE WHOLE SAFETY ARGUMENT, and it is not optional.
    On 2026-08-12 `set_levels` accepted a stop ABOVE the live price and reported
    it enforced; the armed monitor reads that as an instant breach and market-
    sells the entire position -- a full liquidation reached without any order
    being placed. Creating watches out of overrides re-opens exactly that door,
    so a candidate arms ONLY when a live price is known AND sits strictly above
    the stop.

    Anything else is REFUSED, with a reason, and stays in `unprotected` where it
    is already alarmed. Refusing costs a position the protection it did not have
    a moment ago; arming wrongly sells it at market. Those are not symmetric.
    """
    from research_store.models import Thesis                   # noqa: PLC0415
    armed, refused = {}, {}
    rank = start_rank
    for sym, ov in sorted(candidates.items()):
        stop = _finite_pos(ov["stop"])
        if stop is None:                        # re-checked here, not assumed
            refused[sym] = "stop is not a finite positive number"
            continue
        raw_px = prices.get(sym)
        if raw_px is None:
            refused[sym] = "no live price yet — cannot verify the stop is below spot"
            continue
        px = _finite_pos(raw_px)
        if px is None:
            # inf/NaN/<=0 must never arm: inf compares above every stop, so a
            # corrupt quote would arm everything (reviewer, 2026-08-20).
            refused[sym] = "price is not a finite positive number"
            continue
        if not (px > stop):
            refused[sym] = (f"stop {stop:g} is at or above spot {px:g} — arming it "
                            f"would fire an immediate market sell")
            continue
        # ⛔ TARGETS ARE VALIDATED, NOT JUST CAST. A negative or zero target
        # reaches corporate_action_suspected() and the target scan: a negative
        # last target can make the corporate-action guard suppress ALL action
        # including the stop, and a zero target is satisfied by any price. An
        # unusable target is dropped; the stop still stands on its own, which
        # is the level that bounds the loss. Found by the reviewer, 2026-08-20.
        tg = ov.get("targets")
        if not isinstance(tg, list):
            tg = [ov["target"]] if ov.get("target") is not None else []
        targets = [t for t in (_finite_pos(x) for x in tg) if t is not None]
        if len(targets) != len(tg):
            refused.setdefault(sym, "")   # noted below, never silent
            print(f"  ⚠ {sym}: dropped {len(tg) - len(targets)} unusable "
                  f"target(s); the stop is unaffected")
            refused.pop(sym, None)
        armed[sym] = Thesis(
            symbol=sym, rank=rank, verdict="hold",
            thesis=("HELD with an agent-set stop and no thesis (opened intraday). "
                    "Watched on the agent's own levels; the loop is not "
                    "prescribing this position."),
            entry_zone=[], stop=stop, targets=targets, target_weight=0.0,
            signals={"source": "agent_override"})
        rank += 1
    return armed, refused


def in_book(t) -> bool:
    """Is this thesis one the monitor should WATCH? (before the stop check)

    ⛔ NOT just `target_weight > 0`. That was the whole condition until
    2026-08-14, and it silently excluded the theses added the same day to
    protect names the AGENT holds but the ranking did not select
    (slow_loop.protective_theses). Those carry full geometry at weight 0.0 --
    deliberately, because the loop is not prescribing the position -- so under
    the old test they had a stop that nothing enforced. The geometry existed and
    the protection did not, which is the exact defect class this repo keeps
    finding.

    A weight of zero means two different things and they must not be conflated:
      verdict "avoid"  -> failed the reward:risk gate, NOT held, do not watch.
      verdict "hold"   -> HELD by the agent, outside the ranked selection,
                          geometry supplied so it can be watched. Watch it.

    src/agent_env/state.py mirrors this exactly; the two must not drift, because
    reporting `watched=True` for something unwatched is a false "protected"
    signal and worse than reporting nothing.
    """
    return bool(getattr(t, "target_weight", 0) > 0
                or getattr(t, "verdict", "") == "hold")


def _load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _save(path, obj):
    """Write JSON state ATOMICALLY — temp file in the same dir, then rename.

    ⛔ IT USED TO BE A BARE write_text (2026-09-08). state.json is the `fired`
    latch: the ONE record of which take-profit has already been taken. A plain
    write truncates first, so a crash or an OOM kill mid-write leaves a
    half-written file, every reader's json.loads raises, the caller substitutes
    an empty default — and an empty latch re-arms every spent target. That is
    the same defect the latch exists to prevent, arriving through the file
    instead of through the code. os.replace is atomic on POSIX: readers see
    either the old file or the new one, never a partial one.
    """
    MON.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")   # per-process: never collide
    with open(tmp, "w") as fh:
        fh.write(json.dumps(obj, indent=2))
        fh.flush()
        os.fsync(fh.fileno())        # durable BEFORE the rename, not just atomic
    os.replace(tmp, path)


FIRED_KEY = {"stop": "stop", "target1": "t1", "target2": "t2"}

# ⛔ TWO QUESTIONS, TWO ANSWERS (2026-09-08). `sold`/`placed` above answers
# "should the recorders run?" and deliberately FAILS TOWARDS RECORDING — an
# unknown status counts as sold, because an unrecorded fill is worse than an
# idempotent recorder running for nothing (selftest, "unknown status must fail
# towards recording"). The LATCH asks a different question — "is this trigger
# spent?" — where failing towards yes means NEVER SELLING THIS AGAIN. One
# variable cannot carry both failure directions, and reusing it for both is why
# a REJECTED order marked its level spent and dropped out of the retry path.
LIVE_AT_BROKER = frozenset({"queued", "new", "unconfirmed", "pending", "confirmed"})
TERMINAL_NO_FILL = frozenset({"cancelled", "canceled", "rejected", "failed", "voided"})


def _cum_qty(row) -> float:
    """Executed quantity on an order row; unreadable/absent -> 0.0."""
    for k in ("cumulative_quantity", "filled_quantity", "quantity_filled"):
        v = row.get(k)
        if v not in (None, ""):
            try:
                q = float(v)
            except (TypeError, ValueError):
                return 0.0
            return q if math.isfinite(q) and q > 0 else 0.0
    return 0.0


def classify_outcome(row) -> str:
    """One order row -> 'filled' | 'live' | 'terminal_no_fill' | 'ambiguous'. Pure.

    ⛔ AMBIGUOUS IS A REAL OUTCOME, NOT A DEFAULT. A partial fill, an unknown
    status, or a terminal status carrying a NONZERO executed quantity all mean
    "something happened and we do not know how much". None of those may latch
    the trigger (that strands the residual) and none may be blindly retried
    (that places a second order). They pause the symbol and page a human.
    """
    status = str(row.get("status", "")).strip().lower()
    qty = _cum_qty(row)
    if status in TERMINAL_NO_FILL:
        # A cancel/reject that nonetheless executed part of the order is NOT a
        # clean failure — retrying it would sell the remainder twice.
        return "ambiguous" if qty > 0 else "terminal_no_fill"
    if status == "filled":
        return "filled"
    if status in LIVE_AT_BROKER:
        # A live order that has already partially executed is ambiguous for the
        # same reason; a clean live order latches so we do not duplicate it.
        return "ambiguous" if qty > 0 else "live"
    return "ambiguous"          # partially_filled, absent, or anything unknown


def outcome_sets(rows, act_symbols) -> dict:
    """Split the executor's rows into the four sets the monitor acts on. Pure.

    `retry` also carries every triggered symbol the executor said NOTHING about
    — silence is a failure to act, not a success.
    """
    by = {"filled": set(), "live": set(), "terminal_no_fill": set(), "ambiguous": set()}
    seen = set()
    for r in rows:
        sym = r.get("symbol")
        if not sym:
            continue
        seen.add(sym)
        by[classify_outcome(r)].add(sym)
    # a symbol reported twice with conflicting outcomes takes the WORST one
    for sym in list(by["filled"] | by["live"]):
        if sym in by["ambiguous"] or sym in by["terminal_no_fill"]:
            by["filled"].discard(sym)
            by["live"].discard(sym)
    # ⛔ `no_fill` IS NOT `retry`. Both get retried, but they are different
    # FACTS: `no_fill` means the broker answered and executed nothing, which is
    # a settled outcome worth recording; the rest of `retry` is the executor
    # saying NOTHING about a symbol, which is an unknown and must never be
    # written down as "did not execute" (that would suppress the one alarm that
    # catches an unrecorded fill — see health.unrecorded_fills).
    return {"latch": by["filled"] | by["live"],
            "retry": by["terminal_no_fill"] | (set(act_symbols) - seen),
            "no_fill": by["terminal_no_fill"] - by["ambiguous"],
            "paused": by["ambiguous"],
            "live": by["live"], "filled": by["filled"]}


def sweep_pending(st, unresolved, now_dt, window_s: int) -> list:
    """Expire latches held for orders that were LIVE and never became fills.

    ⛔ WHY THIS EXISTS. A `queued` order latches its trigger so the next poll
    cannot place a second one against it — correct. But nothing re-reads that
    order afterwards. If the broker CANCELS it minutes later (exactly what
    happened to MU into the Labor Day close, 2026-09-07), the trigger stays
    marked spent and the position is never sold again. A latch taken on the
    promise of a fill must expire when the fill does not arrive.

    Expiry does NOT retry: the order may still be live, and a blind retry is a
    duplicate sale. It un-latches, PAUSES the symbol and pages a human. Pure
    apart from the caller's dicts; returns the symbols it expired.
    """
    out = []
    for sym, p in list((st.get("pending") or {}).items()):
        try:
            age = (now_dt - datetime.fromisoformat(str(p.get("ts")))).total_seconds()
        except (TypeError, ValueError):
            age = window_s + 1        # unparseable stamp -> treat as expired, never stuck
        if age < window_s:
            continue
        tier = p.get("tier")
        tiers = st.get("fired", {}).get(sym) or {}
        if tier and tiers.get(tier) == p.get("level"):
            tiers.pop(tier, None)                 # the promised fill never came
            if not tiers:
                st.get("fired", {}).pop(sym, None)
        st["pending"].pop(sym, None)
        u = unresolved.setdefault(sym, {"fails": 0, "escalated": False})
        u["paused"] = True
        u["reason"] = "a live exit order never became a fill within the window"
        out.append(sym)
    return out


def latch_fired(st, act, latch_syms, _unused=None):
    """Mark every acted-on trigger SPENT AT ITS LEVEL and persist it NOW.

    ⛔ THIS MUST RUN THE INSTANT `sold`/`placed` ARE KNOWN — BEFORE the
    recorders and before any phone push (2026-09-08). It used to run only at
    the very END of check_once, downstream of exit_bookkeeping and four
    notify() calls, so ANY exception in between left a COMPLETED SALE
    UNLATCHED and the next 15s poll re-fired the same take-profit at the same
    level. That is not hypothetical: on 2026-09-08 a local `_lvl` shadowing
    the module helper raised on this very line, and STX target1 866 sold FOUR
    times in one morning (0.011069 -> 0.001384 sh, ~87% of the position) while
    state.json's latch stayed empty. Recording what was DONE must never sit
    downstream of reporting that it was done.

    ⛔ `placed` LATCHES TOO, not just `sold`: an accepted-but-unfilled order is
    LIVE at the broker, so re-triggering would place a SECOND order against it
    (the MRK retry, 2026-08-19). THE LEVEL IS THE LATCH, not the tier name —
    see carry_fired(): moving a spent target re-arms it, the same level does
    not.

    Idempotent — it writes the same level for the same trigger — so the tail of
    check_once calls it again after the wake/cooldown work without harm.
    """
    for t in act:
        if t["symbol"] not in latch_syms:
            continue
        tier = FIRED_KEY.get(t["reason"])
        if tier is None:
            # ⛔ NEVER SILENTLY. An unrecognised reason means this trigger can
            # never be marked spent, so it re-fires every poll — the STX defect
            # by another route. It must not crash the tick either. Say so.
            print(f"  🚨 unlatchable trigger: {t['symbol']} reason={t['reason']!r} "
                  f"is not in FIRED_KEY — it CANNOT be marked spent and will "
                  f"re-fire every poll")
            notify("Unlatchable exit trigger",
                   f"{t['symbol']} fired with reason {t['reason']!r}, which has no "
                   f"latch tier. The level cannot be marked spent, so it will "
                   f"re-trigger on every poll. Investigate before the next exit.",
                   tags="rotating_light")
            continue
        st["fired"].setdefault(t["symbol"], {})[tier] = _lvl(t["level"])
    _save(STATE, st)
    return st


def _active_lifecycle_state():
    """Lifecycle identity for this pass, or None when it is unusable.

    ⛔ WHY THE TRAIL NEEDS THIS AT ALL. Trail state used to be keyed by SYMBOL,
    so a name that closed and was re-entered inherited the CLOSED position's
    entry and peak. Measured 2026-08-30: FTNT was sold at target1/target2 on
    08-27 and re-entered on 08-28 at 163.95, while trails.json still carried
    entry 152.71 / peak 172.80 — a trail stop of 165.77 derived from a giveback
    the live position never had. Every shadow poll that day said it would exit.
    A position_id cannot be inherited that way: a trim keeps it, a close and
    re-open mint a new one.

    Read once per pass, never cached: the journal is the authority and a cache
    would reintroduce exactly the staleness this exists to remove.
    """
    try:
        return lifecycle_journal.replay(store.read_journal())
    except Exception as e:                                      # noqa: BLE001
        print(f"  ⚠ lifecycle replay failed ({type(e).__name__}: {e}) — "
              f"trailing state will not be updated")
        return None


class QuoteFeedError(Exception):
    """The moomoo quote poll failed. Raised (not swallowed) so the main loop can
    rebuild the OpenD context and, if the feed stays down, exit for a clean restart.

    Root cause this guards against (2026-07-15 lock-starve + 2026-07-22 stale
    token): a long-lived process holding long-lived client state that rots with no
    self-recovery. That class of bug is not Schwab-specific — an OpenD context can
    be dropped by a gateway restart just as easily (and OpenD is shared with
    moomoo-vol-desk, which restarts it), so the recover/alert/exit ladder below is
    kept verbatim. We treat a wedged feed as recoverable, not silent."""


def _feed_action(consec_fail: int, alert_after: int, exit_after: int) -> str:
    """Classify what to do after N consecutive quote-feed failures.

    'recover' — rebuild the client, keep polling (handles the common transient).
    'alert'   — feed down long enough to warrant one phone push.
    'exit'    — give up and let systemd restart us with a clean client."""
    if consec_fail >= exit_after:
        return "exit"
    if consec_fail >= alert_after:
        return "alert"
    return "recover"


def _feed_alert_due(now, last_ts, cooldown_secs: int) -> bool:
    """True if a feed-down push may fire: never fired, unparseable clock, or the
    cooldown has elapsed. Keeps a genuine multi-day outage to a periodic reminder
    (survives restarts via FEED_ALERT) instead of a per-tick storm."""
    if not last_ts:
        return True
    try:
        last = datetime.fromisoformat(last_ts)
    except Exception:
        return True
    return (now - last).total_seconds() >= cooldown_secs


def _maybe_feed_alert(consec_fail: int, cooldown_secs: int) -> None:
    """Fire ONE cooldown-gated phone push that the quote feed is down and stops
    are therefore unwatched. Cooldown clock persists in FEED_ALERT."""
    now = datetime.now(timezone.utc)
    st = _load(FEED_ALERT, {})
    if not _feed_alert_due(now, st.get("last_ts"), cooldown_secs):
        return
    notify("🚨 Monitor feed DOWN — stops unwatched",
           f"moomoo quotes failing {consec_fail}× in a row; the stop-loss watcher "
           f"is blind. Auto-recovering (client rebuilt each tick; restarts if it "
           f"persists). If it keeps failing, re-auth Schwab (weekly token).")
    _save(FEED_ALERT, {"last_ts": now.isoformat(timespec="seconds"),
                       "consec_fail": consec_fail})


def add_cooldown(symbol: str, days: int):
    cd = _load(COOLDOWN, {})
    until = (_now_et() + timedelta(days=days)).date().isoformat()
    cd[symbol] = until
    _save(COOLDOWN, cd)


def drop_wakes_for(symbol: str) -> list:
    """Retire every wake registered on `symbol`. -> the keys removed.

    ⛔ A WAKE MUST NOT OUTLIVE THE POSITION IT GUARDS. Every firing spawns a
    full model session. On 2026-08-20 RTX was stopped out and sold at 11:53,
    and a wake registered on it fired at 13:54 -- twice, fifteen seconds apart
    -- spending two sessions of the operator's usage to wake an agent about a
    position that no longer existed. The session's own conclusion was that the
    level was orphaned and it cleared it by hand.

    ⛔ ONLY WAKES THAT PREDATE THE EXIT. This deleted EVERY wake on the symbol,
    including a legitimate re-entry wake ("tell me if RTX gets back to X"),
    which is the opposite of the intent and destroys a judgement the agent made
    (reviewer, 2026-08-20). A wake registered BEFORE the exit was guarding the
    position that just closed; one registered AFTER is about re-entering, and is
    left alone. `registered_at` already carries what is needed, so no new field
    and no reclassification of existing wakes is required.

    Never raises: a torn or absent wakes file leaves the wakes alone rather than
    taking down the tick that just sold something.
    """
    try:
        data = _load(WAKES, {})
        cutoff = datetime.now(timezone.utc)
        gone = []
        for k, w in data.items():
            if str(w.get("symbol", "")).upper() != str(symbol).upper():
                continue
            reg = w.get("registered_at")
            if reg:
                try:
                    t = datetime.fromisoformat(str(reg))
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=timezone.utc)
                    if t > cutoff:            # registered AFTER this exit
                        continue              # a re-entry wake — leave it
                except (TypeError, ValueError):
                    pass                      # undatable -> treat as pre-existing
            gone.append(k)
        if not gone:
            return []
        for k in gone:
            data.pop(k, None)
        _save(WAKES, data)
        print(f"  retired {len(gone)} wake(s) on {symbol} — position closed")
        return gone
    except Exception as e:                                    # noqa: BLE001
        print(f"  could not retire wakes on {symbol}: {type(e).__name__}: {e}")
        return []


#: The exit executor's MCP tool surface, stated here rather than inherited.
#: place_equity_order appears because there is no sell-only variant of it; SELL-
#: ONLY is enforced mechanically by scripts/hooks/pretooluse_exit_scope.py.
#:
#: ⛔ THE BUILT-INS ARE DELIBERATELY ABSENT FROM THIS LIST, AND MUST STAY ABSENT.
#: prompts/exit.md needs Read and Write — it writes its own result file and the
#: staging files the monitor's recorders consume (src/exit_bookkeeping.py). It
#: no longer needs Bash: until 2026-09-03 it ran the four recorder scripts
#: itself under exact-match grants, and one un-retried refusal left a filled
#: sale unrecorded. This is
#: NOT the sessions' MCP architecture (do not quietly convert it into one). But
#: a BARE tool name in --allowedTools is a BLANKET grant that overrides the
#: narrow `Bash(...)` / `Write(...)` rules in the settings file: probed on this
#: box, an executor granted bare `Bash` ran `ls /opt` happily while
#: deploy/exit_executor_settings.json at the time permitted only four specific
#: `.venv/bin/python scripts/record_*.py` commands (none today). Naming the
#: built-in here silently converts "may run nothing" into "may run anything".
#: Their authority therefore comes from the per-command and per-path rules in
#: that settings file, which is the only place it can be read and reviewed.
EXECUTOR_TOOLS = [
    "mcp__robinhood-trading__get_accounts",
    "mcp__robinhood-trading__get_equity_positions",
    "mcp__robinhood-trading__get_portfolio",
    "mcp__robinhood-trading__get_equity_quotes",
    "mcp__robinhood-trading__get_equity_orders",
    "mcp__robinhood-trading__get_realized_pnl",
    "mcp__robinhood-trading__review_equity_order",
    "mcp__robinhood-trading__place_equity_order",
]


def executor_argv(model: str) -> list:
    """The exact argv for one exit-executor spawn of `model`. PURE (no clock, no I/O).

    `model` is a parameter, never a literal: config/models.toml names the exit
    role's primary and the chain behind it (src/models.py), read at spawn time.

    Split out from run_executor() so the invocation can be READ and probed
    without spawning a model against a live broker.

    ⛔ EVERY FLAG HERE CLOSES AN INHERITANCE. Until now this process was started
    as `claude -p --model ... --settings loop_settings.json <prompt>`, and
    everything not named was whatever the box happened to have: the repo
    CLAUDE.md, auto-memory, the user's plugins, the user's settings, and the
    user-level MCP config with every claude.ai connector on it. The trading
    sessions were isolated from exactly that on 2026-08-22 (commit 2f72df7);
    this path was not, and it is the one that runs unattended on a price breach.

    --setting-sources ""  drops the User, Project and Local settings sources. It
        does NOT drop --settings below, and cannot drop enterprise policy: the
        CLI computes enabled sources as {what this flag allows} ∪ {flagSettings,
        policySettings}, so both order gates survive by construction.
        ⛔ THE EMPTY STRING IS THE ARGUMENT AND MUST SURVIVE. It fails CLOSED if
        lost — the flag takes exactly one value, so a dropped "" makes it eat
        the next flag and the CLI refuses to start. A monitor that cannot spawn
        an executor is loud; one that quietly re-inherits the box is not.
    --settings            the executor's OWN contract, not the shared loop file:
        the general order gate AND the exit-scope gate, plus a permission
        allowlist scoped to this job.
    --strict-mcp-config   makes --mcp-config the only MCP source. This is what
        removes the OTHER BROKERS (Interactive Brokers, webull, Public.com) that
        a user-level connector list had mounted on a live-money exit process.
    --permission-mode dontAsk  auto-DENIES anything unlisted instead of
        prompting: headless there is nobody to answer and `default` would hang
        until the wall-clock timeout, which on this path means an unplaced stop.
    --allowedTools        MUST STAY LAST — it is variadic and swallows every
        following argument as a tool name.

    ⚠️ NOT `--tools ""`. The sessions disable every built-in because they need
    no shell. This executor still needs Read and Write: it writes its own
    result file and the staging files (fills.json, broker_state.json,
    exit_closes.json, partial_closes.json, orders_dump.json) that the monitor
    hands to the recorder scripts after this process returns. It does NOT run
    those scripts any more — see exit_bookkeeping.record_exits — so it holds
    no Bash grant at all.
    """
    return ["claude", "-p",
            # stream-json to logs/exit_stream.jsonl: the chain's clean-failure
            # test counts tool_use blocks in it, and until 2026-09-04 the
            # executor's tool calls were recoverable only from ~/.claude/.
            "--output-format", "stream-json", "--verbose",
            # the model is PINNED (never the CLI default, which can be retired
            # under us) but pinned in config, not here
            "--model", model,
            "--setting-sources", "",
            "--settings", str(REPO / "deploy" / "exit_executor_settings.json"),
            "--strict-mcp-config",
            "--mcp-config", str(REPO / "deploy" / "exit_mcp.json"),
            "--permission-mode", "dontAsk",
            "--allowedTools", *EXECUTOR_TOOLS]


def executor_env(req_id: str) -> dict:
    """The environment one exit-executor spawn runs under. PURE.

    ⛔ AGENTIC_EXIT_REQUEST_ID IS THE AUTHORIZATION. It is the content hash of
    the request just written, and scripts/hooks/pretooluse_exit_scope.py refuses
    every order unless the file on disk still hashes to it. That is what binds
    THIS PROCESS to THE REQUEST IT WAS LAUNCHED FOR: an executor started by hand
    carries no stamp and can sell nothing, and a request rewritten underneath a
    running executor stops authorizing it mid-flight. Verified on this box that
    the value reaches the hook: hooks inherit the CLI's environment.

    The two CLAUDE_CODE_DISABLE_* variables suppress instruction FILES and
    auto-memory — they are NOT what closes plugin/settings inheritance (that is
    `--setting-sources ""` in executor_argv), and the pair only look redundant.
    """
    env = dict(os.environ)
    env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    env["AGENTIC_EXIT_REQUEST_ID"] = req_id
    return env


EXIT_STREAM = REPO / "logs" / "exit_stream.jsonl"
EXIT_CHAIN_BUDGET_X = 2.0            # the whole chain fits two executor timeouts
DRILL_MCP = REPO / "deploy" / "drill_mcp.json"


def _spawn_executor(argv: list, prompt: str, env: dict, timeout_secs: int,
                    stream_path: Path) -> tuple:
    """ONE executor spawn. -> (rc, stream text, stderr). Never raises."""
    stream_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(stream_path, "w") as sfh:
            cp = subprocess.run(argv, input=prompt, text=True, cwd=str(REPO), env=env,
                                timeout=timeout_secs, stdout=sfh, stderr=subprocess.PIPE,
                                check=False)
        rc, err = cp.returncode, cp.stderr or ""
    except subprocess.TimeoutExpired:
        rc, err = -1, f"timed out after {timeout_secs}s"
    except Exception as e:                            # noqa: BLE001
        rc, err = -1, f"spawn failed: {type(e).__name__}: {e}"
    try:
        out = stream_path.read_text()
    except OSError:
        out = ""
    return rc, out, err


def _executor_judge(rc: int, out: str, err: str) -> tuple:
    """The RESULT FILE is the verdict, never the exit code (see run_executor)."""
    if EXIT_RES.exists():
        return True, None
    tail = (err or "").strip() or fallback.result_text(out).strip()
    return False, f"exit {rc}: {tail[-300:] or 'no result file'}"


def _journal_fallback(role: str, frm: str, to, reason, attempt: int,
                      drill: bool = False) -> None:
    """One journal event + one phone push per chain transition. Never raises."""
    rec = {"event": "model_fallback",
           "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "role": role, "mode": "exit", "from": frm, "to": to,
           "reason": reason, "attempt": attempt}
    if drill:
        rec["drill"] = True
    try:
        store.append_journal(rec)
    except Exception as e:                            # noqa: BLE001
        print(f"  model_fallback journal write failed: {e}")
    body = f"{frm} → {to or 'EXHAUSTED'} ({reason}) attempt {attempt}"
    print(f"  model fallback: {body}")
    notify(f"FALLBACK {role}" + (" (DRILL)" if drill else ""), body,
           tags="warning" if to else "rotating_light")


def run_executor(req_id: str, timeout_secs: int = 300) -> dict:
    """Fire the headless exit executor and return its result file ([] on failure).

    ⛔ SINCE 2026-09-04 THIS WALKS THE MODEL CHAIN (config/models.toml,
    src/fallback.py): one spawn per model, the next ONLY when the previous
    spawn failed with ZERO tool calls in its transcript. A failure after any
    tool call is ambiguous -- it may have placed the sell -- and stops here,
    exactly as before the chain existed. The verdict per spawn is the result
    file below; each transition is journalled and pushed.

    Deliberately reads the RESULT FILE rather than trusting the subprocess exit
    code: the sell is step 4 of an 8-step prompt and the tail is bookkeeping, so a
    run that placed its orders and then died still reports correctly. exit.md is
    instructed to rewrite that file after EACH placement so the lossy window is a
    single MCP call wide.

    Raised 180 -> 300 on 2026-08-09: the 2026-08-06 WDC stop consumed the entire
    180s on post-sale reconciliation. That is survivable now, but a timeout that
    routinely fires is a timeout carrying no information."""
    try:
        # ⚠️ --settings IS LOAD-BEARING AND WAS MISSING SINCE THIS PATH WAS
        # WRITTEN. Without it the exit executor runs under .claude/settings.json,
        # which is deliberately permissive for humans and carries NO `hooks` key
        # -- so the PreToolUse order gate has never bound to the exit path. Every
        # stop-triggered market sell this system has ever placed went out with no
        # harness gate: no live_approved check, no per-order cap, no SHADOW, no
        # whitelist. (The monitor checks the kill switch itself, so HALT worked;
        # nothing else did.) This is the same defect documented as caught for
        # sessions in deploy/session_tools.sh -- it was still live here.
        #
        # ⛔ AND --settings ALONE WAS STILL NOT SCOPE. The general order gate
        # ALLOWS every sell by design (a gate that refuses one strips a
        # fractional position of its only stop), so it bound this process to
        # global safety policy and to nothing about THIS request: the executor
        # could have sold a holding the monitor never asked about and passed.
        # The second hook in exit_executor_settings.json is what closes that;
        # the request id below is what it checks against.
        #
        # ⛔ THE PROMPT GOES ON STDIN, NEVER IN ARGV. `--allowedTools` is
        # VARIADIC, so a trailing prompt argument is consumed as one more tool
        # name and the CLI then exits 1 with "Input must be provided either
        # through stdin or as a prompt argument" — i.e. a breached stop that
        # places nothing, and the monitor reads the absent result file as a
        # failed exit. scripts/session.py:784 records the same failure for the
        # trading sessions; this path inherits the lesson rather than the bug.
        # (It also sidesteps MAX_ARG_STRLEN, 128KiB per argument.)
        prompt = (REPO / "prompts" / "exit.md").read_text()
        env = executor_env(req_id)
        seller = models.terminal("exit") == "code_seller"
        held_back: list = []          # the walk's final "→ EXHAUSTED" event, if any

        def _on_fallback(frm, to, reason, n):
            # When the code seller is configured, the chain's end is not the
            # end: hold the EXHAUSTED event and let the seller transition below
            # be the one event/push, so the phone never reads "EXHAUSTED" and
            # then "→ code_seller" seconds apart. An AMBIGUOUS stop still pages.
            if to is None and seller:
                held_back.append((frm, to, reason, n))
                return
            _journal_fallback("exit", frm, to, reason, n)

        walk = fallback.run_chain(
            models.chain("exit"),
            lambda m: _spawn_executor(executor_argv(m), prompt, env, timeout_secs, EXIT_STREAM),
            _executor_judge,
            per_attempt_s=timeout_secs, budget_s=timeout_secs * EXIT_CHAIN_BUDGET_X,
            cli_retry_after_s=models.cli_retry_after_s(),
            on_fallback=_on_fallback)
        last = walk["attempts"][-1]
        if held_back and not (walk["stopped"] in ("exhausted", "budget")):
            _journal_fallback("exit", *held_back[-1])   # ambiguous: page as before
        # the executor's final text used to reach journald via inherited stdout;
        # with stream-json on disk, print it here so the log stays readable
        print(f"  executor [{last.model}] rc={last.rc} tool_calls={last.tool_calls} "
              f"chain={walk['stopped']}:\n{fallback.result_text(last.out)[:3000]}")
        # ⛔ THE TERMINAL STEP: the model-free code seller (scripts/code_seller.py,
        # spec §3). Reached ONLY when every model failed CLEANLY -- an ambiguous
        # failure stops above, because the model may already have sold. The
        # seller carries the same request id, so the request on disk must still
        # hash to it; it refuses on HALT/SHADOW/live_approved and bounds every
        # order with the exit-scope hook's own verdict. Its verdict is the same
        # result file. Drilled 2026-09-04 08:33 ET (review only) before wiring.
        if (not walk["ok"] and walk["stopped"] in ("exhausted", "budget")
                and models.terminal("exit") == "code_seller"):
            _journal_fallback("exit", last.model, "code_seller", last.reason_class,
                              len(walk["attempts"]) + 1)
            try:
                cp = subprocess.run([str(REPO / ".venv" / "bin" / "python"),
                                     str(REPO / "scripts" / "code_seller.py")],
                                    cwd=str(REPO), env=env, capture_output=True, text=True,
                                    timeout=timeout_secs, check=False)
                print(f"  code_seller rc={cp.returncode}:\n{(cp.stdout or '')[-2000:]}"
                      f"{(cp.stderr or '')[-500:]}")
            except Exception as e:                        # noqa: BLE001
                print(f"  code_seller error: {e}")
    except Exception as e:                            # never let execution crash the monitor
        print(f"  executor error: {e}")
    return _load(EXIT_RES, {"sold": []})


# A price this far from its own level is not a market move. Stops sit BELOW the
# entry already, so reaching 55% of the stop means the name roughly halved in a
# single 15s tick — LULD halts trading long before that prints. A 2:1 split lands
# at exactly 50%, a 4:1 at 25%. Reverse splits multiply, which is why the upside
# is guarded too: a 1:10 reverse makes px 10x and would "hit" every target.
SPLIT_LOW_RATIO = 0.55
SPLIT_HIGH_RATIO = 1.90


def corporate_action_suspected(px, stop, target) -> str | None:
    """Is this 'breach' a corporate action or bad tick rather than a real move?

    Pure — no I/O, no clock. Returns a reason string, or None if the move is
    plausible and should be acted on normally.

    Why this exists: the panel and every stored level are UNADJUSTED
    (`AuType.NONE` in the moomoo adapter, load-bearing so the series splices with
    Schwab-era history). Nothing in this repo processes corporate actions. So on
    a 4:1 split the quote drops ~75% while the stored stop stays pre-split, and
    `px <= stop` fires a 100% market sell on a move that never happened. That is
    a real, mechanical path to an erroneous live sale.

    The trade this makes: on a genuine >45% single-tick collapse it alerts a human
    instead of selling. That is the right side to err on — a phantom sale is
    immediate and irreversible, while a real crash still gets a loud phone push
    and a person who is watching. It refuses to ACT; it never hides the event.
    """
    if px is None or px <= 0:
        return None
    if stop and px <= stop * SPLIT_LOW_RATIO:
        return (f"price {px:.4f} is {px / stop:.0%} of its stop {stop:.4f} — "
                "implausible in one tick; a forward split or a bad print")
    if target and px >= target * SPLIT_HIGH_RATIO:
        return (f"price {px:.4f} is {px / target:.1f}x its target {target:.4f} — "
                "implausible in one tick; a reverse split or a bad print")
    return None


def launch_gate(launches, now_dt, max_per_window, window_secs):
    """May the executor be launched right now? -> (allow, pruned_window). PURE.

    ⛔ WHY THIS EXISTS -- 2026-08-19, and it is the guard that should have been
    here from the first line of the exit path.

    The monitor does not sell. It SPAWNS A MODEL to sell: run_executor() starts
    a full headless Opus session running prompts/exit.md. Nothing counted those
    spawns. One trigger that would not resolve -- MRK's inherited target sitting
    BELOW the mark, so it re-armed on every poll -- launched 27 sessions in 68
    minutes and exhausted the account's model budget.

    That is a self-reinforcing failure: an exhausted budget makes the executor
    fail, a failed executor writes no result, a missing result reads as a failed
    exit, and the retry spends more of the budget that is already gone. The
    thing consumed by the failure is the thing needed to recover from it.

    The `paused` flag added the same day closes ONE door: an unknown broker
    outcome. This closes the corridor. No repeating condition -- known, unknown,
    or not yet invented -- can spawn more than `max_per_window` executors in
    `window_secs`. It is a CEILING, not a latch: as launches age out the monitor
    resumes on its own, so a genuine burst of real stops in a falling market is
    delayed rather than abandoned, while an infinite loop is bounded forever.

    `launches` is the persisted list of ISO timestamps. Returns the pruned list
    so the caller writes back a window that cannot grow without bound.
    """
    cutoff = now_dt - timedelta(seconds=int(window_secs))
    pruned = []
    for s in (launches or []):
        try:
            if datetime.fromisoformat(s) >= cutoff:
                pruned.append(s)
        except (TypeError, ValueError):
            continue            # a corrupt entry is dropped, never counted as room
    # ⛔ NO TRANSITION FLAG HERE. A first attempt returned one, computed as
    # `len(pruned) == max_per_window` -- which stays true on EVERY later poll
    # while the window is full, so the breaker would have alerted 19 times in a
    # replay of the incident it exists to stop. Whether this is the FIRST
    # refusal is a fact about persisted state, not about the window, so the
    # caller decides it from `executor_breaker` in state.json.
    return len(pruned) < int(max_per_window), pruned


def refire_gate(triggers, unresolved, now_dt, retry_secs, escalate_n):
    """Throttle re-firing breaches whose exit keeps FAILING. Pure — no I/O.

    A breach whose sell fails is left un-`fired` so it retries — but re-running the
    (Claude-subprocess) executor and re-pushing the alert every 15s poll spams the
    phone AND blocks the watch loop up to 180s per spawn (the AMAT-style loop, but
    for a genuinely-held name a stop-out can't fill). So: a still-`unresolved` name
    is suppressed until `retry_secs` elapse, then retried once; after `escalate_n`
    failures it raises ONE manual-intervention escalation.

    `unresolved`: {sym: {"fails": int, "last_try_ts": iso, "escalated": bool}}.
    Returns (act, escalate): `act` = triggers to alert/journal/execute this poll;
    `escalate` = symbols that just crossed the manual-intervention line.
    """
    act, escalate = [], []
    for t in triggers:
        u = unresolved.get(t["symbol"])
        if u is None:                                    # fresh breach — always act
            act.append(t)
            continue
        # A missing executor result is an UNKNOWN outcome, not a retryable
        # failure. Retrying a fractional target against current quantity can
        # sell the same position repeatedly (the MRK incident on 2026-08-19).
        if u.get("paused"):
            continue
        elapsed = (now_dt - datetime.fromisoformat(u["last_try_ts"])).total_seconds()
        if elapsed < retry_secs:                         # still in backoff — suppress
            continue
        act.append(t)                                    # backoff elapsed — retry
        if u["fails"] >= escalate_n and not u.get("escalated"):
            escalate.append(t["symbol"])
    return act, escalate


# --------------------------------------------------------------------------- #
# wakes — the agent asking to be called back
# --------------------------------------------------------------------------- #
def _should_poll(held, wake_syms) -> bool:
    """Is there anything to poll this tick? -> bool.

    Extracted so it can be TESTED. The condition previously sat inline in
    check_once as a bare `if not held: return 0`, which no selftest reaches --
    and a mutation reverting it to that form passed the whole suite green. A
    guard nothing can test is a guard nothing protects.

    An empty book is NOT a reason to stop: a wake on an unheld symbol is the
    case wakes exist for ("tell me if NVDA reaches X so I can re-enter"), and it
    is registered precisely when the name is not held.
    """
    return bool(held) or bool(wake_syms)


def _wake_symbols() -> set:
    """Symbols any live wake is watching. Never raises; {} on any problem.

    Read straight off disk rather than through src/agent_env/wakes.py: this
    module runs under system /usr/bin/python3 (3.10) for moomoo, and importing
    the agent_env package pulls the mcp dependency chain, which lives only in
    the .venv. The file is a flat JSON dict -- reading two keys out of it does
    not justify a second interpreter.
    """
    try:
        return {str(w["symbol"]).upper() for w in json.loads(WAKES.read_text()).values()
                if w.get("symbol")}
    except Exception:                                 # noqa: BLE001
        return set()


def _fire_wakes(prices: dict, armed: bool) -> list:
    """Fire any wake the current prices satisfy. -> the wakes fired.

    A wake is the AGENT asking to be called back when something it cannot sit
    and watch for happens. Until now `wakes.due()` had no caller anywhere: a
    session could register one, get an ok, and be woken by nothing -- a control
    that reads as present and does nothing, which is the defect class this
    system keeps producing.

    ⚠️ NON-BLOCKING, DELIBERATELY. The session is spawned and NOT waited on.
    This runs inside the 15s stop-watching poll; a session takes minutes, and
    blocking here would stop watching every stop in the book while one wake is
    serviced. The stop watcher must never be the thing a feature pauses.

    Concurrency is the session lock's job, not a flag here: session.py acquires
    it and a `wake` that cannot get it inside 120s exits saying so. Ordinary
    order safety is the gate's job. Nothing new is invented at this layer.
    """
    if not prices:
        return []
    try:
        import subprocess as _sp                      # noqa: PLC0415
        sys.path.insert(0, str(REPO / "src"))
        from agent_env import wakes as _wk            # noqa: PLC0415
    except Exception as e:                            # noqa: BLE001
        print(f"  wakes unavailable: {e}")
        return []

    try:
        hits = _wk.due(WAKES, prices)
    except Exception as e:                            # noqa: BLE001
        print(f"  wakes.due failed: {e}")
        return []

    fired = []
    for w in hits:
        try:
            _wk.mark_fired(WAKES, w["key"])           # budget FIRST, so a crash
            fired.append(w)                           # below cannot re-fire it
            store.append_journal({
                "event": "wake_fired",
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "symbol": w.get("symbol"), "direction": w.get("direction"),
                "level": w.get("level"), "price": w.get("price"),
                "reason": w.get("reason"), "armed": bool(armed)})
            notify(f"Wake: {w.get('symbol')} {w.get('direction')} "
                   f"{w.get('level')}",
                   f"{w.get('symbol')} at {w.get('price')} "
                   f"({w.get('reason') or 'no reason given'}). "
                   f"{'Waking a session.' if armed else 'ALERT ONLY - not armed.'}",
                   tags="alarm_clock")
            if armed:
                try:
                    _sp.Popen([str(REPO / "deploy" / "run_session.sh"), "wake"],
                              cwd=str(REPO), start_new_session=True,
                              stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                except Exception as e:                # noqa: BLE001
                    # ⛔ THE FIRING IS REFUNDED. The budget is marked BEFORE the
                    # spawn so a crash cannot leave the wake re-firing in a
                    # loop -- but that also meant a failed spawn silently spent
                    # a firing and no session ever ran (reviewer, 2026-08-20).
                    print(f"  wake {w.get('key')}: session spawn FAILED "
                          f"({type(e).__name__}) — refunding the firing")
                    _wk.refund_firing(WAKES, w["key"])
                    fired.pop() if fired and fired[-1] is w else None
                    notify("⚠️ Wake fired but its session did not start",
                           f"{w.get('symbol')} {w.get('direction')} "
                           f"{w.get('level')}: the wake condition was met and "
                           f"the session could not be launched. The firing has "
                           f"been refunded so it can retry.", tags="warning")
        except Exception as e:                        # noqa: BLE001
            print(f"  wake {w.get('key')} failed: {e}")
    return fired


def check_once(cfg, client) -> int:
    """One pass: poll, detect breaches, act. Returns count of triggers acted on."""
    m = cfg["monitor"]
    prod = read_current()
    if not prod:
        return 0
    armed = gov.live_approved(cfg) and not m.get("alert_only", False)
    # The kill switch means the machine places no ORDER — NOT that it stops
    # LOOKING. Until 2026-08-09 this returned 0 here, so touching HALT left every
    # open position unprotected AND unwatched at the exact moment the operator
    # believed they had made things safe (stops are software-only — this process
    # IS the stop). Now it degrades to the alert-only path: keep polling, keep
    # journalling, keep phoning; the human places the exit by hand.
    halted = gov.kill_switch_active(cfg)
    if halted:
        armed = False
    global _LAST_HALTED
    if halted != _LAST_HALTED:
        print("  kill-switch active — WATCHING ONLY, exits must be placed BY HAND"
              if halted else "  kill-switch cleared — resuming normal mode")
        _LAST_HALTED = halted

    held = {t.symbol: t for t in prod.theses if in_book(t) and t.stop}
    freshness = snapshot_freshness.status(RH_POSITIONS,
                                           REPO / "research_store" / "journal.jsonl")
    global _LAST_STALE_OWNERSHIP
    if freshness["stale"]:
        # Fail open: an old ownership set must never exclude a newly bought
        # real position from software stop coverage. Stop arithmetic below is
        # unchanged; only this ownership filter is bypassed.
        owned = None
        if not _LAST_STALE_OWNERSHIP:
            print("  🚨 positions snapshot predates the last fill — ownership "
                  "filter disabled; watching every eligible thesis")
            notify("🚨 Broker positions snapshot stale after a fill",
                   "positions.json predates the newest journalled execution. "
                   "The monitor is failing open and watching every eligible "
                   "thesis until broker state is reconciled.",
                   tags="rotating_light")
        _LAST_STALE_OWNERSHIP = True
    else:
        _LAST_STALE_OWNERSHIP = False
        try:                                  # watch only names we ACTUALLY hold
            owned = owned_symbols(_load(RH_POSITIONS, None))
        except Exception:
            owned = None                     # torn read → fail open (watch all)
    if owned is not None:
        dropped = [s for s in held if s not in owned]
        held = {s: t for s, t in held.items() if s in owned}
        global _LAST_DROPPED                  # log only when the set changes, not every tick
        if frozenset(dropped) != _LAST_DROPPED:
            if dropped:                       # phantom book names never bought
                print(f"  not held — skipping stop-watch: {', '.join(sorted(dropped))}")
            _LAST_DROPPED = frozenset(dropped)

    # Invariant (design spec §8): every position has an agent-set stop, or it
    # is loudly flagged unprotected — checked every cycle, not just logged.
    # `held` above is what we ARE watching; this is the converse — what we
    # actually hold (per the broker snapshot) that isn't in that set, either
    # because it has no current thesis, or a thesis with no stop / zero
    # weight. Persisted every tick regardless of outcome so src/health.py can
    # read it for the daily 08:00 check; the phone push itself only fires on
    # a CHANGE (see _LAST_UNPROTECTED/_LAST_SUSPECT_EMPTY above), matching the
    # _LAST_DROPPED/_LAST_HALTED fire-on-transition pattern rather than
    # re-pushing every 15s poll while the condition persists.
    # ⛔ ABSENT AND UNREADABLE ARE NOT THE SAME THING, AND THIS TREATED THEM AS
    # ONE. `except Exception: {}` made a TORN overrides.json indistinguishable
    # from a book with no agent levels set — and {} means "apply no overrides",
    # so every position silently reverts to the loop's looser thesis stop.
    #
    # MEASURED 2026-08-31 by making the file unreadable: all 12 held positions
    # dropped to the book stop (MU 843.00 -> 813.259, LITE 810.85 -> 758.60),
    # CRWD lost its stop entirely, every `set_by_agent` flag cleared, and NOTHING
    # anywhere reported it — not the monitor, not positions(), not the dashboard.
    # All three then agreed, and all three were correct: the protection really
    # had reverted. That is the same outcome as the Sunday wipe (see
    # slow_loop.py), reachable by one corrupt file, in silence.
    #
    # The fallback is UNCHANGED and stays fail-safe: with no usable overrides we
    # keep the thesis stops, because refusing to watch would unprotect the book.
    # What changes is that it is no longer SILENT.
    _ov_path = MON / "overrides.json"
    _ov_unreadable = False
    if not _ov_path.exists():
        _ov_early = {}                      # a real, ordinary state: none set
    else:
        try:
            _ov_early = json.loads(_ov_path.read_text())
        except Exception:                                    # noqa: BLE001
            _ov_early = {}
            _ov_unreadable = True
    global _LAST_OV_UNREADABLE
    if _ov_unreadable != _LAST_OV_UNREADABLE:
        if _ov_unreadable:
            print("  ⚠ overrides.json is PRESENT but UNREADABLE — every agent-set "
                  "level is being ignored this tick; positions are on their "
                  "thesis stops, which are looser")
            notify("🚨 Agent-set stop levels cannot be read",
                   "research_store/monitor/overrides.json exists but will not "
                   "parse. Every position has reverted to the slow loop's thesis "
                   "stop, which is LOOSER than the level the agent set. Nothing "
                   "was lost and exits still fire, but the book is less protected "
                   "than intended until the file is repaired or rewritten with "
                   "set_levels.", tags="rotating_light")
        _LAST_OV_UNREADABLE = _ov_unreadable
    # Candidates about to be armed on their own agent-set stop. Subtracted from
    # the ALERT below (not from the file) so a snapshot rewrite does not push a
    # spurious "unprotected" for a position the very next line protects. If
    # arming then fails, arm_standalone's own refusal alert fires -- nothing is
    # dropped silently, it is just not announced twice per snapshot write.
    _pending_early = standalone_candidates(owned, _ov_early, set(held))
    unprot = unprotected_positions(prod.theses, owned)
    _save(MON / "unprotected.json", {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "unprotected": unprot["unprotected"],
        "no_target": unprot.get("no_target", []),
        "suspect_empty_snapshot": unprot["suspect_empty_snapshot"],
    })
    global _LAST_UNPROTECTED, _LAST_SUSPECT_EMPTY
    cur_unprot = frozenset(unprot["unprotected"]) - set(_pending_early)
    if cur_unprot != _LAST_UNPROTECTED:
        if cur_unprot:
            names = ", ".join(sorted(cur_unprot))
            print(f"  ⚠ UNPROTECTED — no stop being watched: {names}")
            notify("🚨 Unprotected position(s) — no stop being watched",
                   f"{names}: held per the broker snapshot but has no current thesis "
                   f"or no stop, so the monitor is watching {'it' if len(cur_unprot) == 1 else 'them'} "
                   f"for nothing. Set a stop or exit by hand.",
                   tags="rotating_light")
        _LAST_UNPROTECTED = cur_unprot
    # A held position with a stop but NO TARGET. Separate push from the
    # unprotected one: the risk is bounded, but the trade has no pre-decided
    # finish, which is the standing instruction it violates. Fire-on-CHANGE so a
    # standing condition does not re-push every 15s poll.
    global _LAST_NO_TARGET
    cur_nt = frozenset(unprot.get("no_target", []))
    if cur_nt != _LAST_NO_TARGET:
        if cur_nt:
            names = ", ".join(sorted(cur_nt))
            print(f"  ⚠ NO TARGET — stop set, no take-profit: {names}")
            notify("⚠️ Position(s) with no take-profit target",
                   f"{names}: stopped but with no target, so nothing decides when "
                   f"the trade is finished. Every position is meant to carry both. "
                   f"Set one, or say why this position ends only at its stop.",
                   tags="warning")
        _LAST_NO_TARGET = cur_nt

    if unprot["suspect_empty_snapshot"] != _LAST_SUSPECT_EMPTY:
        if unprot["suspect_empty_snapshot"]:
            print("  ⚠ snapshot reports ZERO positions but the book expects holdings"
                  " — check the snapshot, not the stops")
            notify("⚠️ Snapshot reports zero positions but the book expects holdings",
                   "research_store/rh/positions.json shows no holdings while the book "
                   "has weighted theses. This usually means a stale, partial, or failed "
                   "snapshot write (prompts/exit.md step 7 rewrites it after selling), "
                   "not a genuinely flat account — check the snapshot before assuming "
                   "stops are the problem.",
                   tags="warning")
        _LAST_SUSPECT_EMPTY = unprot["suspect_empty_snapshot"]

    _ov = _ov_early   # read once per tick, above (absent OR torn -> {} , which
                      # ignores ALL overrides this tick and self-heals next tick)
    # apply_overrides is called BEFORE this tick's own quotes are fetched
    # (below), so it has no live price yet -- read the monitor's OWN last
    # write of quotes.json (its own poll, ~15s old) instead. A missing or
    # torn read degrades to {}, which the guard inside apply_overrides
    # treats as "no usable price for any symbol" -- every stop override is
    # refused (fail closed), never applied on a guess.
    try:
        _px = (json.loads(QUOTES.read_text()) or {}).get("prices", {})
    except Exception:                                           # noqa: BLE001
        _px = {}            # no quotes -> every stop override is refused (fail closed)
    _pre = {s: (getattr(th, "stop", None), list(getattr(th, "targets", []) or []))
            for s, th in held.items()}
    if _ov:
        held = apply_overrides(held, _ov, _px)
    # EVERY poll, not only the ones with overrides: a reader must be able to
    # tell "no override applied" from "this file is stale". Writing it only
    # under `if _ov` left the previous poll's snapshot standing, which is the
    # same class of lie the file exists to remove. Found by review 2026-08-18.
    _publish_enforcement(held, _pre, _px, prod.as_of)

    # ⛔ POSITIONS WITH AN AGENT-SET STOP AND NO THESIS. Theses come from the
    # NIGHTLY slow loop, so before 2026-08-20 anything opened intraday was
    # unwatched for the rest of that session however carefully the agent set its
    # levels -- set_levels even said so and the position stayed unprotected
    # anyway (JNJ, 2026-08-20; BAC/FTNT the day before). An agent-set stop is a
    # stop. These are quoted with everything else below and ARMED only once a
    # live price proves the stop sits under spot.
    _pending = _pending_early

    # ⚠️ NOT a bare `return 0`. An all-cash book still has wakes to watch, and
    # that is exactly when a wake matters most: "tell me if NVDA reaches X so I
    # can re-enter" is registered precisely when the name is NOT held. Returning
    # here on an empty book would leave those wakes silently unevaluated for as
    # long as the book stayed flat.
    if not _should_poll(dict(held, **{k: None for k in _pending}), _wake_symbols()):
        return 0

    st = _load(STATE, {})
    # ⛔ book_asof is RECORDED, never used as a latch epoch — see carry_fired().
    # Resetting `fired` here is what sold MU twice on 2026-09-07.
    st["book_asof"] = prod.as_of
    st["fired"] = carry_fired(st.get("fired"))
    # ⛔ EXPIRE LATCHES TAKEN ON A PROMISE. Runs EVERY tick, before any trigger
    # evaluation, so a live order the broker later cancelled cannot hold its
    # trigger spent forever with the position unsold. See sweep_pending().
    _unresolved_now = st.setdefault("unresolved", {})
    _expired = sweep_pending(st, _unresolved_now, datetime.now(timezone.utc),
                             int((cfg.get("monitor") or {}).get("pending_resolve_secs", 600)))
    if _expired:
        _save(STATE, st)
        print(f"  🚨 latch expired (live order never filled): {', '.join(sorted(_expired))}"
              f" — un-latched and PAUSED, not retried")
        store.append_journal({"event": "exit_latch_expired",
                              "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "symbols": sorted(_expired),
                              "reason": "an exit order was accepted but never became a "
                                        "fill inside the window; the latch it was holding "
                                        "is released and the symbol paused for a human"})
        notify("🚨 Exit order never filled — latch released",
               f"{', '.join(sorted(_expired))}: the broker accepted an exit order but it "
               f"never filled. The take-profit/stop is no longer marked spent, and the "
               f"symbol is PAUSED rather than retried — the order may still be live. "
               f"Check Robinhood (948184924) before reissuing.",
               tags="rotating_light")

    # A wake can name a symbol we do NOT hold -- that is most of the point ("tell
    # me if NVDA reaches X"). Quoting only holdings would leave those wakes
    # permanently unevaluated, which is how wakes.due() came to have no caller at
    # all: it was written, selftested, and nothing ever fed it a price.
    wake_syms = _wake_symbols()
    try:
        quotes = mmp.live_quotes(sorted(set(held) | wake_syms | set(_pending)),
                                 ctx=client)
    except Exception as e:
        # Do NOT swallow: signal the main loop so it rebuilds the client and, if
        # the feed stays wedged, exits for a clean systemd restart + phone alert.
        raise QuoteFeedError(str(e)) from e

    # ⛔ A SUCCESSFUL CALL IS NOT A FRESH ONE. Raised BEFORE any trigger is
    # evaluated, so a stale price can never satisfy a stop or a target — that
    # is the whole point. It goes through QuoteFeedError deliberately: the
    # existing ladder already rebuilds the context, alerts, and finally exits
    # for a clean systemd restart, which is exactly the right response to a
    # wedged feed and needs no second alerting path. See feed_stalled().
    if feed_stalled(st, newest_feed_ts(quotes),
                    int((cfg.get("monitor") or {}).get("feed_stall_polls", 4))):
        _save(STATE, st)
        raise QuoteFeedError(
            f"feed timestamp has not advanced across "
            f"{st['feed']['stalls']} polls (newest {st['feed'].get('newest')!r}) "
            f"— the call succeeded but the prices are stale, so no stop or "
            f"target may be evaluated against them")

    # persist the marks we just paid for — the dashboard + equity logger value
    # positions from this file (via src/marks.py) instead of stale snapshots
    prices = {sym: px for sym in (set(held) | wake_syms | set(_pending))
              if (px := _last_price(quotes.get(sym))) is not None}
    _save(QUOTES, {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "prices": prices})

    # Wakes fire off the SAME prices we just paid for, and BEFORE the stop
    # scan -- a wake is non-blocking (it spawns and does not wait), so putting
    # it first costs the stop watcher nothing and means a wake is not skipped
    # by an early return further down.
    _fire_wakes(prices, armed)

    # Arm the override-only watches now that this tick's prices are in hand.
    # arm_standalone REFUSES anything it cannot prove is below spot -- a stop at
    # or above the live price would fire an immediate market sell, which is the
    # 2026-08-12 whole-position-liquidation path.
    if _pending:
        _armed_solo, _refused_solo = arm_standalone(_pending, prices)
        held.update(_armed_solo)
        global _LAST_SOLO, _LAST_SOLO_REFUSED
        cur_solo = frozenset(_armed_solo)
        if cur_solo != _LAST_SOLO:
            if cur_solo:
                print(f"  watching on agent-set stops (no thesis): "
                      f"{', '.join(sorted(cur_solo))}")
            _LAST_SOLO = cur_solo
        cur_ref = frozenset(_refused_solo)
        if cur_ref != _LAST_SOLO_REFUSED:
            for sym, why in sorted(_refused_solo.items()):
                print(f"  ⚠ NOT arming {sym} on its agent-set stop: {why}")
            if cur_ref:
                names = ", ".join(f"{s} ({w})" for s, w in sorted(_refused_solo.items()))
                notify("⚠️ Agent-set stop NOT enforceable",
                       f"{names}. The position stays UNPROTECTED — set a stop "
                       f"below the live price, or exit by hand.", tags="warning")
            _LAST_SOLO_REFUSED = cur_ref
        # ⛔ RE-PUBLISH ENFORCEMENT TOO. _publish_enforcement() runs before
        # arming, so an active standalone watch was absent from
        # enforcement.json -- the file a reader consults to see what the
        # monitor is actually holding in force (reviewer, 2026-08-20).
        if _armed_solo:
            _publish_enforcement(held, _pre, prices, prod.as_of)
        # Re-publish the invariant now that the armed set is known, so a
        # genuinely protected position is not reported unprotected.
        if _armed_solo:
            unprot2 = unprotected_positions(
                prod.theses, owned, extra_watched=set(_armed_solo),
                extra_targets={s: t.targets for s, t in _armed_solo.items()})
            _save(MON / "unprotected.json", {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "unprotected": unprot2["unprotected"],
                "no_target": unprot2.get("no_target", []),
                "suspect_empty_snapshot": unprot2["suspect_empty_snapshot"],
            })

    if not held:
        return 0        # wakes-only tick: nothing to stop-watch

    triggers = []
    suspect = {}
    for sym, th in held.items():
        px = prices.get(sym)
        if px is None:
            continue
        # Corporate action / bad print: refuse to ACT, but never go quiet.
        why = corporate_action_suspected(px, th.stop,
                                         th.targets[-1] if th.targets else None)
        if why:
            suspect[sym] = why
            continue
        fired = st["fired"].get(sym) or {}
        if (m.get("enable_stops", True) and px <= th.stop
                and not tier_spent(fired, "stop", th.stop)):
            triggers.append({"symbol": sym, "reason": "stop", "fraction": 1.0,
                             "price": px, "level": th.stop})
        # ⛔ ONLY THE AGENT'S TAKE-PROFITS FIRE (2026-08-25, principal's call).
        # The loop still WRITES targets -- the store's [risk] mandate requires a
        # weighted position to carry them (research_store/validate.py) -- but
        # they are generated geometry, not a view: 2.2R and 4.0R off the stop,
        # i.e. ~5.5 and ~10 sigma. The repo's own calibration puts first-hit at
        # 10.7% within ten days, and `target2` has fired ZERO times in the
        # system's entire history. Selling real money at a level nothing
        # decided, which is unreachable anyway, is code dictating profit-taking.
        #
        # So the loop's numbers are now a DEFAULT THE MONITOR WILL NOT ACT ON,
        # and a take-profit fires only where the agent set one -- the same way
        # a stop is the agent's, reasoned from the chart. A position with no
        # agent target simply has no take-profit; that is a legal, visible
        # state (`positions().targets_status`), not a silent one.
        elif (m.get("enable_targets") and th.targets
              and isinstance((_ov.get(sym) or {}).get("targets"), list)
              and (_ov.get(sym) or {}).get("targets")):
            if px >= th.targets[-1] and not tier_spent(fired, "t2", th.targets[-1]):
                triggers.append({"symbol": sym, "reason": "target2", "fraction": 1.0,
                                 "price": px, "level": th.targets[-1]})
            elif px >= th.targets[0] and not tier_spent(fired, "t1", th.targets[0]):
                triggers.append({"symbol": sym, "reason": "target1", "fraction": 0.5,
                                 "price": px, "level": th.targets[0]})

    # ---- TRAILING STOP, SHADOW MODE (2026-08-25) -------------------------
    # ⛔ THIS SELLS NOTHING. It advances each position's high-water mark and
    # journals the exit a trail WOULD have taken, so ten sessions of evidence
    # exist before anything is armed. `[monitor.trail].enabled` gates arming and
    # ships false; until it is true this block appends NOTHING to `triggers`.
    #
    # ⛔ WRAPPED WHOLE, DELIBERATELY. This process IS the stop -- Robinhood has
    # no native stop for fractional shares. A defect in a shadow feature must
    # never be able to stop the real one from firing, so every exception here is
    # caught and reported and the stop path above is already complete by this
    # point. Same reasoning as the ownership filter failing OPEN.
    try:
        _tcfg = (m.get("trail") or {})
        _trails = _load(MON / "trails.json", {}) or {}
        _snap_pos = (_load(RH_POSITIONS, {}) or {}).get("positions") or {}
        _shadow = []

        # ⛔ KEYED BY LIFECYCLE position_id, NEVER BY SYMBOL. See
        # _active_lifecycle_state(): a symbol key lets a re-entry inherit the
        # CLOSED position's entry and peak, which is exactly what happened to
        # FTNT on 2026-08-28. A trim keeps its id; a close and re-open mint a
        # new one, so the new position starts from its own cost.
        #
        # ⛔ AND IT FAILS CLOSED, NOT OPEN. positions.json is a CACHE and has
        # gone two days stale before (2026-08-14). Pruning on a stale or empty
        # read would delete live trail state; reusing rows against it would
        # measure a giveback from the wrong position. So when ownership or
        # identity is not trustworthy this pass does NOTHING -- it does not
        # prune, does not update, and leaves the file byte-identical. The
        # ordinary stop and targets are already resolved above and are
        # unaffected either way.
        _snapshot_syms = set()
        _snapshot_valid = (owned is not None and isinstance(_snap_pos, dict)
                           and bool(_snap_pos))
        if _snapshot_valid:
            for _raw_sym, _row in _snap_pos.items():
                if not isinstance(_row, dict):
                    _snapshot_valid = False
                    break
                try:
                    _qty = float(_row.get("qty"))
                except (TypeError, ValueError):
                    _snapshot_valid = False
                    break
                if not math.isfinite(_qty) or _qty <= 0:
                    _snapshot_valid = False
                    break
                _snapshot_syms.add(str(_raw_sym).strip().upper())

        _life = _active_lifecycle_state() if _snapshot_valid else None

        if not _snapshot_valid:
            print("  ⚠ trailing pass skipped — ownership snapshot is stale, "
                  "empty, or malformed")
        elif _life is None:
            print("  ⚠ trailing pass skipped — lifecycle identity unavailable")
        else:
            _ignored_syms = {str(ref).split(":", 2)[1]
                             for ref in (_life.get("ignored") or [])
                             if len(str(ref).split(":", 2)) >= 2}
            _ids = {}
            for _sym in _snapshot_syms:
                _current = (_life.get("active") or {}).get(_sym)
                if _current and _sym not in _ignored_syms:
                    _pid = _current.get("position_id")
                    if _pid:
                        _ids[_sym] = str(_pid)

            if set(_ids) != _snapshot_syms:
                print("  ⚠ trailing pass skipped — incomplete lifecycle identity")
            else:
                # Retire rows whose lifecycle is no longer open. Safe ONLY here,
                # under a snapshot and a replay that both verified above.
                _live_ids = set(_ids.values())
                _trails = {pid: row for pid, row in _trails.items()
                           if pid in _live_ids and isinstance(row, dict)}

                for sym, th in held.items():
                    _pid = _ids.get(sym)
                    if not _pid:
                        continue
                    px = prices.get(sym)
                    if px is None or sym in suspect:
                        continue
                    _entry = (_snap_pos.get(sym) or {}).get("avg_cost")
                    if not _entry:
                        continue              # no cost basis -> no peak to measure from
                    _st = _trails.get(_pid) or {"entry_price": float(_entry),
                                                "peak_price": float(_entry)}
                    # max(session high, last): a high printed BETWEEN two 15s polls
                    # is invisible to `last`, and a peak that misses it measures a
                    # giveback the position never had. live_quotes returns high.
                    _q = quotes.get(sym) or {}
                    _hi = _q.get("high") if isinstance(_q, dict) else None
                    _obs = max([v for v in (_hi, px) if isinstance(v, (int, float))]
                               or [px])
                    _st = trailing.update_peak(_st, _obs)
                    _sig = (getattr(th, "signals", None) or {}).get("sigma")
                    _hit = trailing.trail_trigger(sym, px, _st, _sig, _tcfg,
                                                  base_stop=th.stop,
                                                  agent_stop=None)
                    _trail_lvl, _act = trailing.compute_trail_stop(
                        _st, _sig, float(_tcfg.get("activation_sigma", 2.5)),
                        float(_tcfg.get("giveback_fraction", 0.35)))
                    if _trail_lvl is not None:
                        _st["trail_stop"] = _trail_lvl        # persist the ratchet
                    _st["activated"] = bool(_act)
                    _trails[_pid] = _st
                    if _hit and _hit.get("reason") == "trail":
                        _shadow.append(_hit)
                    elif _hit and _hit.get("reason") == "trail_state_invalid":
                        print(f"  ⚠ trail state unusable for {sym}: "
                              f"{_hit.get('detail')} — ordinary stop still enforced")
                _save(MON / "trails.json", _trails)
        # Fire-on-CHANGE, matching _LAST_DROPPED/_LAST_SUSPECT_ACTION: a shadow
        # breach persists across every 15s poll, and journalling it each time
        # would bury the record it exists to create.
        global _LAST_TRAIL_SHADOW
        _cur = frozenset(h["symbol"] for h in _shadow)
        if _shadow and _cur != _LAST_TRAIL_SHADOW:
            for h in _shadow:
                print(f"  [trail-shadow] {h['symbol']} would exit @ {h['price']} "
                      f"(level {h['level']:.4f}, peak +{h['peak_gain_pct']}%, "
                      f"locking +{h['locked_gain_pct']}%)")
            store.append_journal({"event": "trail_shadow", "armed": False,
                     "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "would_exit": _shadow})
        _LAST_TRAIL_SHADOW = _cur
        if _tcfg.get("enabled"):
            # Not reachable until an operator arms it. Deliberately the LAST
            # thing added, so an armed trail is one config flag and no new path.
            triggers.extend(_shadow)
    except Exception as e:                                       # noqa: BLE001
        print(f"  ⚠ trailing shadow pass failed ({type(e).__name__}: {e}) — "
              f"stops and targets above are UNAFFECTED")

    # Suspected corporate action: loud, distinct, and fire-on-CHANGE (matching
    # _LAST_DROPPED/_LAST_HALTED) so a split does not re-push every 15s all day.
    global _LAST_SUSPECT_ACTION
    cur_suspect = frozenset(suspect)
    if cur_suspect != _LAST_SUSPECT_ACTION:
        if cur_suspect:
            for s in sorted(cur_suspect):
                print(f"  ⚠ NOT ACTING on {s}: {suspect[s]}")
            notify("🚨 Suspected split/bad print — stop NOT actioned",
                   f"{', '.join(sorted(cur_suspect))}: the quote is implausibly far "
                   f"from its stored level, so no order was placed. Levels are "
                   f"UNADJUSTED — if this is a split, re-base the stop before the "
                   f"position is watched again. Verify by hand.",
                   tags="rotating_light")
        _LAST_SUSPECT_ACTION = cur_suspect

    # ⛔ Ownership is unverifiable while the snapshot is stale (see the fail-open
    # above). Stops still fire; profit-taking does NOT -- see
    # suppress_unowned_targets for why the asymmetry is deliberate.
    global _LAST_TARGET_SUPPRESSED
    triggers, _suppressed = suppress_unowned_targets(triggers, bool(freshness["stale"]))
    _supp_key = frozenset(f"{t['symbol']}:{t['reason']}" for t in _suppressed)
    if _supp_key != _LAST_TARGET_SUPPRESSED:
        if _suppressed:
            for t in _suppressed:
                print(f"  ⏸ HOLDING {t['reason']} on {t['symbol']}: positions "
                      f"snapshot predates the last fill, so ownership is "
                      f"unverified — profit-taking waits, stops do not")
            store.append_journal({
                "event": "target_suppressed_stale_snapshot",
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "suppressed": [{"symbol": t["symbol"], "reason": t["reason"],
                                "price": t.get("price"), "level": t.get("level")}
                               for t in _suppressed],
                "why": "positions.json predates the newest journalled execution; "
                       "firing a fractional target against an unverified quantity "
                       "is the 2026-08-19 double-sell mechanism",
            })
        _LAST_TARGET_SUPPRESSED = _supp_key

    # drop unresolved entries whose breach has cleared (price recovered above stop)
    unresolved = st.setdefault("unresolved", {})
    live = {t["symbol"] for t in triggers}
    for sym in [s for s in unresolved if s not in live]:
        del unresolved[sym]

    if not triggers:
        _save(STATE, st)
        return 0

    now_dt = datetime.now(timezone.utc)
    ts = now_dt.isoformat(timespec="seconds")
    retry_secs = int(m.get("refire_retry_secs", 120))
    escalate_n = int(m.get("refire_escalate_n", 3))
    # `act` = fresh breaches + failing ones whose backoff elapsed; `escalate` = names
    # that just crossed the manual-intervention line. Suppressed re-fires (still in
    # backoff) neither re-alert nor re-spawn the executor.
    act, escalate = refire_gate(triggers, unresolved, now_dt, retry_secs, escalate_n)
    if not act:
        _save(STATE, st)
        return 0
    fresh = [t for t in act if t["symbol"] not in unresolved]   # first alert only

    mode = "EXECUTE" if armed else ("HALTED" if halted else "ALERT-ONLY")
    verb = "selling" if armed else "NEEDS MANUAL SELL OF"
    unarmed_note = ("\n⛔ KILL-SWITCH (HALT) active: no order will be placed and this "
                    "position is UNPROTECTED. Sell BY HAND, or `rm research_store/HALT` "
                    "to hand the exit back to the monitor."
                    if halted else
                    "\nALERT-ONLY (not armed): no order will be placed")
    for t in act:
        print(f"  ⚠ {t['reason'].upper()} {t['symbol']} @ {t['price']} "
              f"(level {t['level']}) — {mode}")
    # ⛔ `launched` IS PART OF THE RECORD. The signal is journalled here, BEFORE
    # the launch ceiling below and before the executor runs, so a breach that
    # placed nothing looked identical to one that did -- and
    # health.unrecorded_fills then expected a fill that was never meant to
    # exist (reviewer, 2026-08-20). Computed here rather than inferred later:
    # the monitor is the only party that knows.
    _will_launch = bool(armed) and launch_gate(
        st.get("executor_launches", []), datetime.now(timezone.utc),
        int(m.get("executor_max_per_window", 6)),
        int(m.get("executor_window_secs", 3600)))[0]
    store.append_journal({"event": "exit_signal", "ts": ts, "armed": armed,
                          "halted": halted, "launched": _will_launch,
                          "triggers": act})
    if fresh:                                        # routine alert: fresh breaches only
        notify(f"{'Executing' if armed else 'MANUAL EXIT NEEDED' if halted else 'Alert'}: "
               + ", ".join(f"{t['reason'].upper()} {t['symbol']}" for t in fresh),
               "\n".join(f"{t['symbol']} {t['reason']} @ {t['price']} (level {t['level']}) "
                         f"— {verb} {int(t['fraction'] * 100)}%" for t in fresh)
               + ("" if armed else unarmed_note),
               tags="rotating_light" if halted
               else "chart_with_downwards_trend" if any(t["reason"] == "stop" for t in fresh)
               else "moneybag")

    if armed:
        # ⛔ THE LAUNCH CEILING. Checked BEFORE the request file is written and
        # before any spawn, because the spawn is what costs. Refusing here
        # leaves the breach detected, journalled and alerted -- the position is
        # not silently forgotten, it is escalated to a human instead of to
        # another model.
        _now = datetime.now(timezone.utc)
        _allow, _pruned = launch_gate(
            st.get("executor_launches", []), _now,
            int(m.get("executor_max_per_window", 6)),
            int(m.get("executor_window_secs", 3600)))
        # FIRST refusal of this incident? Persisted state is the only thing that
        # knows, and it is what keeps the breaker from becoming a second storm.
        _tripped = (not _allow) and "executor_breaker" not in st
        st["executor_launches"] = _pruned
        if not _allow:
            st["executor_breaker"] = {
                "since": st.get("executor_breaker", {}).get("since") or _now.isoformat(timespec="seconds"),
                "window_secs": int(m.get("executor_window_secs", 3600)),
                "max_per_window": int(m.get("executor_max_per_window", 6)),
                "blocked": sorted({x["symbol"] for x in act}),
            }
            _save(STATE, st)
            store.append_journal({
                "event": "executor_breaker", "ts": ts,
                "blocked": sorted({x["symbol"] for x in act}),
                "launches_in_window": len(_pruned),
                "reason": "executor launch ceiling reached — refusing to spawn "
                          "another exit agent until the window clears",
            })
            if _tripped:                     # ONE push per incident, not per poll
                notify("EXIT EXECUTOR HALTED — launch ceiling",
                       f"{len(_pruned)} executor launches in the last "
                       f"{int(m.get('executor_window_secs', 3600)) // 60} min. "
                       f"Refusing to spawn more.\nUNSOLD: "
                       f"{', '.join(sorted({x['symbol'] for x in act}))}\n"
                       f"Place these exits BY HAND and reconcile Robinhood.",
                       tags="rotating_light")
            print(f"  ⛔ executor launch ceiling ({len(_pruned)}) — refusing to spawn; "
                  f"unsold: {sorted({x['symbol'] for x in act})}")
            return 0
        st.pop("executor_breaker", None)
        _pruned.append(_now.isoformat(timespec="seconds"))
        st["executor_launches"] = _pruned
        _save(STATE, st)                     # persist BEFORE spawning: a crash
                                             # mid-run must still count the launch
        # ⛔ THE REQUEST AND ITS IDENTITY COME FROM THE SAME OBJECT. The hash is
        # taken from the dict that is written, not re-read from disk, so the
        # stamp the executor is launched with describes exactly the bytes it
        # will be checked against. Anything that rewrites exit_request.json
        # afterwards — a later trigger, or the executor itself — stops matching,
        # and scripts/hooks/pretooluse_exit_scope.py then refuses every order.
        _request = {"ts": ts, "account": "948184924", "exits": act}
        _save(EXIT_REQ, _request)
        if EXIT_RES.exists():
            EXIT_RES.unlink()
        result = run_executor(_exit_request_id(_request),
                              int(m.get("executor_timeout_secs", 300)))
        result_present = EXIT_RES.exists()
        # ⛔ AN ACCEPTED ORDER IS NOT A SALE (2026-09-07). `result["sold"]` is
        # every order the executor PLACED; reading it status-blind told the
        # phone "MU SOLD but the executor staged nothing" when MU had sold
        # nothing at all — the order was `queued` (placed into a shut market on
        # Labor Day) and was later cancelled with cumulative_quantity 0.
        # THREE outcomes, not two, because they need different handling:
        #   sold   — it executed: run the recorders, clear unresolved
        #   placed — accepted, nothing executed: the order is LIVE at the
        #            broker, so do NOT record it (there is no fill), do NOT
        #            retry it (that places a second order), and do NOT call it
        #            a sale on the phone
        #   failed — nothing was placed: back off and retry, as before
        _placed_rows = result.get("sold", []) or []
        placed = {s["symbol"] for s in _placed_rows
                  if str(s.get("status", "")).strip().lower() in UNEXECUTED_STATES}
        sold = {s["symbol"] for s in _placed_rows} - placed
        # ⛔ THE LATCH IS COMPUTED SEPARATELY FROM THE RECORDER SETS (2026-09-08).
        # See classify_outcome(): `sold`/`placed` fails TOWARDS recording, the
        # latch must fail AWAY from "never sell this again".
        _out = outcome_sets(_placed_rows, {t["symbol"] for t in act})
        # ⛔ LATCH FIRST — BEFORE THE RECORDERS AND BEFORE EVERY PUSH. Nothing
        # below this line may run while a completed sale is still unrecorded:
        # the whole block that follows is bookkeeping and reporting, and on
        # 2026-09-08 an exception inside it re-armed a spent take-profit and
        # sold STX four times. See latch_fired().
        latch_fired(st, act, _out["latch"])
        # ⛔ A LATCHED LIVE ORDER MUST EXPIRE. A `queued` order latches so we do
        # not place a second against it — but if the broker cancels it later,
        # nothing re-reads that, and the trigger would stay spent forever with
        # the position unsold. Record it; _sweep_pending() below un-latches and
        # pauses it if it has not become a fill within the window.
        _pend = st.setdefault("pending", {})
        for t in act:
            if t["symbol"] in _out["live"]:
                _pend[t["symbol"]] = {"tier": FIRED_KEY.get(t["reason"]),
                                      "level": _lvl(t["level"]), "ts": ts}
            else:
                _pend.pop(t["symbol"], None)      # resolved one way or the other
        _save(STATE, st)
        # ⛔ THE MONITOR RECORDS THE EXIT, NOT THE EXECUTOR (2026-09-03). The
        # executor writes staging files; the four recorder scripts run HERE,
        # whichever path sold, so a filled sale with a silent ledger/snapshot
        # is impossible — including when the executor dies after placing.
        bk = exit_bookkeeping.record_exits(sold, ts=ts)
        for r in bk["ran"]:
            print(f"  bookkeeping ok: {r['script']} -> archived {r['archived'].name if r['archived'] else '-'}")
        for r in bk["failed"]:
            print(f"  bookkeeping FAILED: {r['script']} rc={r['rc']}: {r['tail']}")
            notify("Exit bookkeeping FAILED",
                   f"{r['script']} rc={r['rc']}\n{r['tail']}\nStaging file kept for retry.",
                   tags="warning")
        for w in bk["warnings"]:
            print(f"  bookkeeping warning: {w}")
        if bk["gap"]:
            store.append_journal({"event": "exit_bookkeeping_gap", "ts": ts,
                                  "sold": sorted(sold),
                                  "reason": "executor reported a sale but wrote no staging "
                                            "file — ledger and positions.json NOT updated"})
            notify("EXIT BOOKKEEPING INCOMPLETE",
                   f"{', '.join(sorted(sold))} SOLD but the executor staged nothing: "
                   f"ledger + positions.json are stale until reconciled. The 08:00 "
                   f"health check will flag unrecorded fills.",
                   tags="rotating_light")
        if placed:
            notify("Exit order placed — NOT yet filled",
                   f"{', '.join(sorted(placed))}: the broker accepted the order "
                   f"but nothing has executed. Nothing is recorded and no retry "
                   f"will be made while it is live. If it fills, the ledger and "
                   f"positions.json catch up at the next reconcile; if it is "
                   f"cancelled, the position keeps its current size.",
                   tags="hourglass")
        # ⛔ AMBIGUOUS OUTCOMES PAUSE, THEY DO NOT RETRY (2026-09-08). A partial
        # fill, an unknown status, or a cancel/reject carrying a nonzero executed
        # quantity all mean "something executed and we do not know how much".
        # Retrying sells the remainder twice; latching strands it unsold. The
        # only correct move is to stop touching the symbol and page a human, who
        # reconciles against the broker and reissues for the remainder only.
        for sym in sorted(_out["paused"]):
            u = unresolved.setdefault(sym, {"fails": 0, "last_try_ts": ts,
                                            "escalated": False})
            u["paused"] = True
            u["last_try_ts"] = ts
            u["reason"] = "ambiguous executor outcome — partial/unknown fill"
        if _out["paused"]:
            store.append_journal({"event": "exit_outcome_ambiguous", "ts": ts,
                                  "symbols": sorted(_out["paused"]),
                                  "reason": "partial fill, unknown status, or a terminal "
                                            "status with nonzero executed quantity — the "
                                            "symbol is PAUSED, not retried and not latched"})
            notify("🚨 EXIT OUTCOME AMBIGUOUS — position PAUSED",
                   f"{', '.join(sorted(_out['paused']))}: the order partly executed or "
                   f"returned an unknown status. It is NOT latched and will NOT be "
                   f"retried automatically — retrying could sell the remainder twice. "
                   f"Reconcile against Robinhood (948184924) and reissue for the "
                   f"remainder only.",
                   tags="rotating_light")
        # ⛔ `retry` COMES FROM classify_outcome, NOT from set arithmetic on
        # `sold`/`placed`. A REJECTED order used to land in `placed`, which both
        # latched it AND cleared its backoff, so a breach the broker refused was
        # never tried again (INTC hit exactly this twice on 2026-09-08, on the $1
        # fractional minimum). A terminal no-fill means nothing executed and
        # nothing is live: it is the ordinary retry path.
        failed = _out["retry"] - _out["paused"]
        # ⛔ RECORD THAT NOTHING EXECUTED (2026-09-09). Until now the exit path
        # journalled an `exit_signal` when an order was ATTEMPTED and an
        # `execution` when one FILLED, and NOTHING AT ALL when the broker took
        # the order and executed zero — cancelled, rejected, failed, voided.
        # So the journal could not distinguish "sold and nobody wrote it down"
        # from "never sold at all", and health.unrecorded_fills — whose whole
        # job is the first — reported the second as a missing fill. That is
        # GitHub #16: MU's Labor Day target1 order 6a9ebc9b was cancelled at
        # the broker with cumulative_quantity 0.000000, and the check called it
        # an unrecorded trade. The fact was never wrong; it was never written.
        # Only a symbol the broker ANSWERED about lands here (`no_fill`);
        # executor silence stays unknown and keeps alarming.
        if _out["no_fill"]:
            store.append_journal({"event": "exit_no_fill", "ts": ts,
                                  "symbols": sorted(_out["no_fill"]),
                                  "reason": "the broker accepted and terminated these exit "
                                            "orders with zero executed quantity — nothing "
                                            "was sold, so there is no fill to record"})
        if failed:
            notify("Exit executor result",
                   f"FAILED/skipped (backing off, will retry): {', '.join(sorted(failed))}"
                   + (f"\nSOLD: {', '.join(sorted(sold))}" if sold else ""),
                   tags="warning")
        # track failures for backoff/escalation; a clean sell clears the name
        for sym in failed:
            u = unresolved.setdefault(sym, {"fails": 0, "last_try_ts": ts, "escalated": False})
            u["fails"] += 1
            u["last_try_ts"] = ts
            if not result_present:
                # The subprocess may have traded and then exhausted Claude or
                # timed out during bookkeeping. Do not place another order
                # until broker state is reconciled by a human.
                u["paused"] = True
            if sym in escalate:
                u["escalated"] = True
        # ⛔ ONLY A LATCHED OUTCOME CLEARS THE RETRY STATE. It used to be
        # `sold | placed`, and `placed` included rejected/cancelled/failed —
        # so a refused order had its fail count, backoff and escalation wiped
        # as though the breach had been handled. Ambiguous symbols keep their
        # `paused` flag; terminal no-fills keep their backoff.
        for sym in _out["latch"]:
            unresolved.pop(sym, None)
        if escalate:                                 # one loud manual-intervention push
            notify("🚨 MANUAL INTERVENTION — stop-sell failing",
                   f"{', '.join(sorted(escalate))} breached its stop but the exit "
                   f"executor has failed {escalate_n}+ times — position(s) UNPROTECTED. "
                   f"Sell manually in the Agentic account (948184924).",
                   tags="rotating_light")
    elif halted:
        # HALT: nothing was sold and nothing is "seen". Do NOT mark these fired —
        # that would suppress every later alert and leave an unprotected position
        # silent. A halted breach is the same situation as an exit that keeps
        # failing, so it goes through the SAME unresolved/backoff/escalation path:
        # re-alert on the retry cadence, then one loud manual-intervention push.
        sold, placed = set(), set()
        for t in act:
            u = unresolved.setdefault(t["symbol"],
                                      {"fails": 0, "last_try_ts": ts, "escalated": False})
            u["fails"] += 1
            u["last_try_ts"] = ts
            if t["symbol"] in escalate:
                u["escalated"] = True
        if escalate:
            notify("🚨 MANUAL INTERVENTION — HALT active, position unprotected",
                   f"{', '.join(sorted(escalate))} breached its stop while the "
                   f"kill-switch (research_store/HALT) is active, so NO exit was "
                   f"placed. Sell manually, or `rm research_store/HALT` to let the "
                   f"monitor handle it.",
                   tags="rotating_light")
    else:
        sold = {t["symbol"] for t in act}            # alert-only: mark seen, don't sell
        placed = set()

    # mark fired + cooldown the stops we acted on.
    # ⛔ THE LATCH ITSELF IS SET BY latch_fired(), which the armed path already
    # ran the moment `sold`/`placed` were known. Calling it again here is
    # deliberate and idempotent: it is what latches the HALTED and ALERT-ONLY
    # paths, which set `sold` in their own branches below the executor.
    latch_fired(st, act, sold, placed)
    for t in act:
        if t["symbol"] in sold or t["symbol"] in placed:
            # A full exit ends the position, so any wake guarding it is spent —
            # leaving it armed spends a model session on a name we no longer
            # hold (RTX, 2026-08-20). A partial (target-1 trim) keeps its wakes:
            # the position, and the reason to watch it, both survive.
            if float(t.get("fraction", 1.0) or 1.0) >= 1.0:
                drop_wakes_for(t["symbol"])
                # ...and so is the whole ladder — but ONLY once it actually
                # executed. The position is gone; a later re-entry writes its
                # own levels, and carrying a dead symbol's tiers could suppress
                # the first target of that new position if the agent reused a
                # number. Clear on the event, not by inferring absence.
                # ⛔ `sold`, NOT `placed`: a QUEUED full exit has closed
                # nothing yet. Clearing there would re-arm the breach on the
                # next tick and place a SECOND order against a live one.
                if t["symbol"] in sold:
                    st["fired"].pop(t["symbol"], None)
            if t["reason"] == "stop" and armed:
                add_cooldown(t["symbol"], m.get("cooldown_days", 5))
    _save(STATE, st)
    return len(act)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single pass then exit")
    ap.add_argument("--force", action="store_true", help="ignore the market-hours gate")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--executor-drill", action="store_true",
                    help="walk the exit executor's model chain with a no-tool prompt: "
                         "no request, no broker, no MCP. Proves the chain, not the exit.")
    ap.add_argument("--drill-chain", default="",
                    help="comma-separated model ids for --executor-drill (default: the "
                         "configured exit chain); a bogus first id is the test")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        return
    if args.executor_drill:
        chain = [m.strip() for m in args.drill_chain.split(",") if m.strip()] or models.chain("exit")
        walk = fallback.run_chain(
            chain,
            lambda m: _spawn_executor(fallback.drill_argv(m, str(DRILL_MCP)), fallback.DRILL_PROMPT,
                                      executor_env(""), 120, EXIT_STREAM),
            lambda rc, out, err: (rc == 0, None if rc == 0 else f"exit {rc}: {(err or '').strip()[-300:]}"),
            per_attempt_s=120, budget_s=120 * len(chain) + 60,
            cli_retry_after_s=models.cli_retry_after_s(),
            on_fallback=lambda frm, to, reason, n: _journal_fallback("exit", frm, to, reason, n, drill=True))
        print(json.dumps({"ok": walk["ok"], "drill": True, "chain": chain, "stopped": walk["stopped"],
                          "attempts": [{"model": a.model, "rc": a.rc, "reason_class": a.reason_class,
                                        "tool_calls": a.tool_calls,
                                        "answer": fallback.result_text(a.out)[:60]}
                                       for a in walk["attempts"]]}, indent=2))
        raise SystemExit(0 if walk["ok"] else 1)
    cfg = strat.load()
    poll = cfg["monitor"]["poll_secs"]
    client = quote_ctx()

    if args.once:
        # ⚠️ The context MUST be closed here. moomoo's OpenQuoteContext spawns
        # NON-DAEMON threads (network_manager poll + callback_executor), so without
        # close() the interpreter blocks forever in threading._shutdown AFTER the
        # work is done — the run looks like a hang, exits only on SIGKILL, and trips
        # the cron ERR trap on a pass that actually succeeded. The Schwab client this
        # replaced needed no teardown, which is why the try/finally is new here.
        try:
            if not args.force and not (market_open() and trading_day(ctx=client)):
                print("market closed — nothing to do (use --force to test)")
                return
            try:
                check_once(cfg, client)
            except QuoteFeedError as e:
                print(f"  quote error: {e}")
        finally:
            client.close()
        return

    print(f"monitor up — polling every {poll}s during 09:30–16:00 ET")
    m = cfg["monitor"]
    alert_after = int(m.get("feed_fail_alert", 4))          # ~1 min @ 15s poll
    exit_after = int(m.get("feed_fail_exit", 12))           # ~3 min @ 15s poll
    cooldown = int(m.get("feed_alert_cooldown_mins", 30)) * 60
    consec_fail = 0
    while True:
        if market_open() and trading_day(ctx=client):
            try:
                check_once(cfg, client)
                consec_fail = 0                       # healthy tick — reset the counter
            except QuoteFeedError as e:
                consec_fail += 1
                print(f"  quote feed error #{consec_fail} (recovering): {e}")
                # LAYER 1 (root cause): discard the wedged context so a fresh one
                # reconnects to OpenD next tick. CLOSE the old one first — each
                # OpenQuoteContext owns two non-daemon threads and a socket, so
                # rebuilding without closing leaks both on every failure tick. On a
                # ~2GB box that turns a transient feed blip into an OOM.
                try:
                    client.close()
                except Exception:
                    pass                              # already dead; nothing to salvage
                try:
                    client = quote_ctx()
                except Exception as be:
                    print(f"  client rebuild failed (will retry): {be}")
                action = _feed_action(consec_fail, alert_after, exit_after)
                if action == "alert":                 # LAYER 3: cooldown-gated push
                    _maybe_feed_alert(consec_fail, cooldown)
                elif action == "exit":                # LAYER 2: hand off to systemd
                    _maybe_feed_alert(consec_fail, cooldown)
                    print(f"  feed down {consec_fail}× — exiting nonzero for a clean "
                          f"systemd restart (fresh context)")
                    # Same non-daemon-thread trap as the --once path: without this
                    # close, sys.exit() blocks in threading._shutdown and systemd
                    # never gets the exit it is waiting on to restart us.
                    try:
                        client.close()
                    except Exception:
                        pass
                    sys.exit(1)
            except Exception as e:
                # ⛔ THE MESSAGE ALONE IS NOT A DIAGNOSIS (2026-09-08). This
                # printed "'NoneType' object is not callable" three times while
                # STX was being sold four times against one take-profit, and
                # the bare text named no file, no line and no frame — the cause
                # (a local `_lvl` shadowing the module helper) took an
                # independent review to find. A traceback would have named it
                # in seconds. stdout, like every other line here, so it lands
                # in the same journalctl stream.
                print(f"loop error (continuing): {e}")
                traceback.print_exc(file=sys.stdout)
            time.sleep(poll)
        else:
            consec_fail = 0                           # closed — don't carry failures over
            time.sleep(60)                            # check again in a minute


if __name__ == "__main__":
    main()
