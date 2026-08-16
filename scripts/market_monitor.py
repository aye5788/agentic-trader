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
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

try:                                     # NTFY_TOPIC for phone push alerts
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
except Exception:
    pass
import strategy as strat            # noqa: E402
import governance as gov            # noqa: E402
from research_store import read_current, store   # noqa: E402
from adapters.moomoo import prices as mmp        # noqa: E402
from adapters.moomoo.client import quote_ctx     # noqa: E402
from notify import push as notify               # noqa: E402  shared ntfy helper

MON = REPO / "research_store" / "monitor"
STATE = MON / "state.json"
QUOTES = MON / "quotes.json"
COOLDOWN = MON / "cooldown.json"
REENTRY = MON / "reentry_review.json"
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
_LAST_HALTED: bool | None = None      # log kill-switch mode on transition, not every tick

# Unprotected-position invariant (spec 2026-08-09 §8): dedupe the phone push to
# CHANGES in the finding, same fire-on-transition pattern as _LAST_DROPPED /
# _LAST_HALTED above — not a re-push every 15s poll while the condition holds.
_LAST_UNPROTECTED: frozenset = frozenset()
_LAST_SUSPECT_EMPTY: bool = False
_LAST_NO_TARGET: frozenset = frozenset()


def _now_et():
    return datetime.now(ET)


def market_open(now=None) -> bool:
    now = now or _now_et()
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= mins < 16 * 60          # 09:30–16:00 ET


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
    print("monitor selftest OK: refire backoff + escalation gate")

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
    print("monitor selftest OK: wakes fire on unheld symbols, respect budget, "
          "and alert-only when not armed")


def _last_price(block: dict):
    """Extract a usable mark from one quote block, or None.

    `last` is the moomoo shape (adapters.moomoo.prices.live_quotes). The camelCase
    keys are the old Schwab shape, kept as a fallback so an on-disk quotes.json
    written before the feed switch still reads back. Only a POSITIVE number counts —
    a 0.0 must never become a price, because it would read as a total loss and fire
    a market sell.
    """
    q = (block or {}).get("quote", {}) or block or {}
    for k in ("last", "lastPrice", "mark", "closePrice", "bidPrice"):
        v = q.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


import copy


def apply_overrides(held: dict, overrides: dict) -> dict:
    """Overlay stricter-only risk-review geometry onto held theses (copies).
    Stop may only be raised; each target may only be lowered. Looser or malformed
    overrides are ignored — a bad file can never loosen a live stop, and can never
    abort the whole tick (a malformed entry degrades to "no override" for that
    symbol, or for the whole book if `overrides` itself isn't a dict)."""
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
            if isinstance(ov_stop, (int, float)) and t.stop is not None:
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
            ot = ov.get("targets")
            if (isinstance(ot, list) and t.targets and len(ot) == len(t.targets)
                    and all(isinstance(o, (int, float)) for o in ot)):
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


def unprotected_positions(theses, owned) -> dict:
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
    watched = {t.symbol for t in theses if in_book(t) and t.stop}
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
                if in_book(t) and t.stop and (t.targets or [])}
    return {"unprotected": sorted(owned - watched),
            "no_target": sorted((owned & watched) - targeted),
            "suspect_empty_snapshot": False}


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
    MON.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2))


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


def add_reentry_review(symbol: str, tier: str, exit_price: float, days: int):
    """A take-profit fired and sold — flag the name so the fast loop routes its
    otherwise-automatic rebuy through the agent's re-entry judgment ([reentry])."""
    rv = _load(REENTRY, {})
    rv[symbol] = {"tier": tier, "exit_price": exit_price,
                  "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "expires": (_now_et() + timedelta(days=days)).date().isoformat()}
    _save(REENTRY, rv)


def run_executor(timeout_secs: int = 300) -> dict:
    """Fire the headless exit executor and return its result file ([] on failure).

    Deliberately reads the RESULT FILE rather than trusting the subprocess exit
    code: the sell is step 4 of an 8-step prompt and the tail is bookkeeping, so a
    run that placed its orders and then died still reports correctly. exit.md is
    instructed to rewrite that file after EACH placement so the lossy window is a
    single MCP call wide.

    Raised 180 -> 300 on 2026-08-09: the 2026-08-06 WDC stop consumed the entire
    180s on post-sale reconciliation. That is survivable now, but a timeout that
    routinely fires is a timeout carrying no information."""
    try:
        # model pinned (see run_fast_loop.sh) — the exit path must never break
        # because a default model was retired
        # ⚠️ --settings IS LOAD-BEARING AND WAS MISSING SINCE THIS PATH WAS
        # WRITTEN. Without it the exit executor runs under .claude/settings.json,
        # which is deliberately permissive for humans and carries NO `hooks` key
        # -- so the PreToolUse order gate has never bound to the exit path. Every
        # stop-triggered market sell this system has ever placed went out with no
        # harness gate: no live_approved check, no per-order cap, no SHADOW, no
        # whitelist. (The monitor checks the kill switch itself, so HALT worked;
        # nothing else did.) This is the same defect documented as caught for
        # sessions in deploy/session_tools.sh -- it was still live here.
        subprocess.run(["claude", "-p", "--model", "claude-opus-4-8",
                        "--settings", str(REPO / "deploy" / "loop_settings.json"),
                        (REPO / "prompts" / "exit.md").read_text()],
                       cwd=str(REPO), timeout=timeout_secs, check=False)
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
                _sp.Popen([str(REPO / "deploy" / "run_session.sh"), "wake"],
                          cwd=str(REPO), start_new_session=True,
                          stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
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
    try:                                     # watch only names we ACTUALLY hold
        owned = owned_symbols(_load(RH_POSITIONS, None))
    except Exception:
        owned = None                         # torn read → fail open (watch all)
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
    unprot = unprotected_positions(prod.theses, owned)
    _save(MON / "unprotected.json", {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "unprotected": unprot["unprotected"],
        "no_target": unprot.get("no_target", []),
        "suspect_empty_snapshot": unprot["suspect_empty_snapshot"],
    })
    global _LAST_UNPROTECTED, _LAST_SUSPECT_EMPTY
    cur_unprot = frozenset(unprot["unprotected"])
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

    try:
        _ov = json.loads((MON / "overrides.json").read_text())   # json already imported at module top
    except Exception:
        _ov = {}   # absent OR a torn read → ignore ALL overrides this tick (self-heals next tick;
                   # risk_review.py writes atomically via os.replace, so torn reads are rare)
    if _ov:
        held = apply_overrides(held, _ov)
    # ⚠️ NOT a bare `return 0`. An all-cash book still has wakes to watch, and
    # that is exactly when a wake matters most: "tell me if NVDA reaches X so I
    # can re-enter" is registered precisely when the name is NOT held. Returning
    # here on an empty book would leave those wakes silently unevaluated for as
    # long as the book stayed flat.
    if not _should_poll(held, _wake_symbols()):
        return 0

    st = _load(STATE, {})
    if st.get("book_asof") != prod.as_of:            # new book -> reset fired flags
        st = {"book_asof": prod.as_of, "fired": {}}

    # A wake can name a symbol we do NOT hold -- that is most of the point ("tell
    # me if NVDA reaches X"). Quoting only holdings would leave those wakes
    # permanently unevaluated, which is how wakes.due() came to have no caller at
    # all: it was written, selftested, and nothing ever fed it a price.
    wake_syms = _wake_symbols()
    try:
        quotes = mmp.live_quotes(sorted(set(held) | wake_syms), ctx=client)
    except Exception as e:
        # Do NOT swallow: signal the main loop so it rebuilds the client and, if
        # the feed stays wedged, exits for a clean systemd restart + phone alert.
        raise QuoteFeedError(str(e)) from e

    # persist the marks we just paid for — the dashboard + equity logger value
    # positions from this file (via src/marks.py) instead of stale snapshots
    prices = {sym: px for sym in (set(held) | wake_syms)
              if (px := _last_price(quotes.get(sym))) is not None}
    _save(QUOTES, {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "prices": prices})

    # Wakes fire off the SAME prices we just paid for, and BEFORE the stop
    # scan -- a wake is non-blocking (it spawns and does not wait), so putting
    # it first costs the stop watcher nothing and means a wake is not skipped
    # by an early return further down.
    _fire_wakes(prices, armed)

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
        fired = set(st["fired"].get(sym, []))
        if m.get("enable_stops", True) and px <= th.stop and "stop" not in fired:
            triggers.append({"symbol": sym, "reason": "stop", "fraction": 1.0,
                             "price": px, "level": th.stop})
        elif m.get("enable_targets") and th.targets:
            if px >= th.targets[-1] and "t2" not in fired:
                triggers.append({"symbol": sym, "reason": "target2", "fraction": 1.0,
                                 "price": px, "level": th.targets[-1]})
            elif px >= th.targets[0] and "t1" not in fired:
                triggers.append({"symbol": sym, "reason": "target1", "fraction": 0.5,
                                 "price": px, "level": th.targets[0]})

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
    store.append_journal({"event": "exit_signal", "ts": ts, "armed": armed,
                          "halted": halted, "triggers": act})
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
        _save(EXIT_REQ, {"ts": ts, "account": "948184924", "exits": act})
        if EXIT_RES.exists():
            EXIT_RES.unlink()
        result = run_executor(int(m.get("executor_timeout_secs", 300)))
        sold = {s["symbol"] for s in result.get("sold", [])}
        failed = {t["symbol"] for t in act} - sold
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
            if sym in escalate:
                u["escalated"] = True
        for sym in sold:
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
        sold = set()
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

    # mark fired + cooldown the stops we acted on
    fired_key = {"stop": "stop", "target1": "t1", "target2": "t2"}
    reentry = cfg.get("reentry", {})
    for t in act:
        if t["symbol"] in sold:
            st["fired"].setdefault(t["symbol"], []).append(fired_key[t["reason"]])
            if t["reason"] == "stop" and armed:
                add_cooldown(t["symbol"], m.get("cooldown_days", 5))
            elif t["reason"].startswith("target") and armed and reentry.get("enabled"):
                add_reentry_review(t["symbol"], t["reason"], t["price"],
                                   int(reentry.get("review_days", 5)))
    _save(STATE, st)
    return len(act)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single pass then exit")
    ap.add_argument("--force", action="store_true", help="ignore the market-hours gate")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        return
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
            if not args.force and not market_open():
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
        if market_open():
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
                print(f"loop error (continuing): {e}")
            time.sleep(poll)
        else:
            consec_fail = 0                           # closed — don't carry failures over
            time.sleep(60)                            # check again in a minute


if __name__ == "__main__":
    main()
