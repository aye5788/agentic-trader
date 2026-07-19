"""MARKET MONITOR — the always-on stop-loss / take-profit watcher (Part 1 of 2).

The daily loops only STORE each name's stop and take-profit prices; nothing
enforced them. This does. During market hours it polls live Schwab quotes for
every held name every `poll_secs` (all names in one call) and checks each price
against its stored stop / targets. On a breach it journals the event and — Part 2
— invokes the headless executor (prompts/exit.md) to place the market sell via RH.

Watching is pure Python + Schwab (cheap, no Claude). Claude fires only on an
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
from adapters.schwab import research             # noqa: E402
from notify import push as notify               # noqa: E402  shared ntfy helper

MON = REPO / "research_store" / "monitor"
STATE = MON / "state.json"
QUOTES = MON / "quotes.json"
COOLDOWN = MON / "cooldown.json"
REENTRY = MON / "reentry_review.json"
EXIT_REQ = MON / "exit_request.json"
EXIT_RES = MON / "exit_result.json"
RH_POSITIONS = REPO / "research_store" / "rh" / "positions.json"
ET = ZoneInfo("America/New_York")

# Remembers the last-logged "not held" set so we log it once per change, not every
# 15s poll (the set is static all day — logging it each tick just floods journald).
_LAST_DROPPED: frozenset | None = None


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
    # looser override is IGNORED (stop can't move down, target can't move up)
    out = apply_overrides(held, {"NVDA": {"stop": 90.0, "targets": [130.0, 150.0]}})
    assert out["NVDA"].stop == 100.0 and out["NVDA"].targets == [120.0, 140.0], out["NVDA"]
    # malformed per-symbol override (not a dict) is IGNORED, not raised
    out = apply_overrides(held, {"NVDA": "garbage"})
    assert out["NVDA"].stop == 100.0 and out["NVDA"].targets == [120.0, 140.0], out["NVDA"]
    # malformed whole-overrides object (not a dict) is IGNORED, not raised
    out = apply_overrides(held, ["nope"])
    assert out["NVDA"].stop == 100.0 and out["NVDA"].targets == [120.0, 140.0], out["NVDA"]
    print("monitor selftest OK: stricter-only override overlay")

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
    assert owned_symbols({"positions": "torn"}) is None
    print("monitor selftest OK: holdings filter (phantom-holding guard)")


def _last_price(block: dict):
    q = (block or {}).get("quote", {}) or block or {}
    for k in ("lastPrice", "mark", "closePrice", "bidPrice"):
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
            if isinstance(ov.get("stop"), (int, float)) and t.stop is not None and ov["stop"] > t.stop:
                t.stop = float(ov["stop"])
            ot = ov.get("targets")
            if (isinstance(ot, list) and t.targets and len(ot) == len(t.targets)
                    and all(isinstance(o, (int, float)) for o in ot)):
                t.targets = [min(cur, float(o)) for cur, o in zip(t.targets, ot)]
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


def _load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _save(path, obj):
    MON.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2))


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


def run_executor() -> dict:
    """Fire the headless exit executor and return its result file ([] on failure)."""
    try:
        # model pinned (see run_fast_loop.sh) — the exit path must never break
        # because a default model was retired
        subprocess.run(["claude", "-p", "--model", "claude-opus-4-8",
                        (REPO / "prompts" / "exit.md").read_text()],
                       cwd=str(REPO), timeout=180, check=False)
    except Exception as e:                            # never let execution crash the monitor
        print(f"  executor error: {e}")
    return _load(EXIT_RES, {"sold": []})


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


def check_once(cfg, client) -> int:
    """One pass: poll, detect breaches, act. Returns count of triggers acted on."""
    m = cfg["monitor"]
    prod = read_current()
    if not prod:
        return 0
    armed = gov.live_approved(cfg) and not m.get("alert_only", False)
    if gov.kill_switch_active(cfg):
        print("  kill-switch active — idle")
        return 0

    held = {t.symbol: t for t in prod.theses if t.target_weight > 0 and t.stop}
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
    try:
        _ov = json.loads((MON / "overrides.json").read_text())   # json already imported at module top
    except Exception:
        _ov = {}   # absent OR a torn read → ignore ALL overrides this tick (self-heals next tick;
                   # risk_review.py writes atomically via os.replace, so torn reads are rare)
    if _ov:
        held = apply_overrides(held, _ov)
    if not held:
        return 0

    st = _load(STATE, {})
    if st.get("book_asof") != prod.as_of:            # new book -> reset fired flags
        st = {"book_asof": prod.as_of, "fired": {}}

    try:
        quotes = research.get_quotes(list(held), client=client)
    except Exception as e:
        print(f"  quote error (will retry next tick): {e}")
        return 0

    # persist the marks we just paid for — the dashboard + equity logger value
    # positions from this file (via src/marks.py) instead of stale snapshots
    prices = {sym: px for sym in held
              if (px := _last_price(quotes.get(sym))) is not None}
    _save(QUOTES, {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "prices": prices})

    triggers = []
    for sym, th in held.items():
        px = prices.get(sym)
        if px is None:
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

    for t in act:
        print(f"  ⚠ {t['reason'].upper()} {t['symbol']} @ {t['price']} "
              f"(level {t['level']}) — {'EXECUTE' if armed else 'ALERT-ONLY'}")
    store.append_journal({"event": "exit_signal", "ts": ts, "armed": armed,
                          "triggers": act})
    if fresh:                                        # routine alert: fresh breaches only
        notify(f"{'Executing' if armed else 'Alert'}: "
               + ", ".join(f"{t['reason'].upper()} {t['symbol']}" for t in fresh),
               "\n".join(f"{t['symbol']} {t['reason']} @ {t['price']} (level {t['level']}) "
                         f"— selling {int(t['fraction'] * 100)}%" for t in fresh)
               + ("" if armed else "\nALERT-ONLY (not armed): no order will be placed"),
               tags="chart_with_downwards_trend" if any(t["reason"] == "stop" for t in fresh)
               else "moneybag")

    if armed:
        _save(EXIT_REQ, {"ts": ts, "account": "948184924", "exits": act})
        if EXIT_RES.exists():
            EXIT_RES.unlink()
        result = run_executor()
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
    client = research.build_client(interactive_auth=False)

    if args.once:
        if not args.force and not market_open():
            print("market closed — nothing to do (use --force to test)")
            return
        check_once(cfg, client)
        return

    print(f"monitor up — polling every {poll}s during 09:30–16:00 ET")
    while True:
        if market_open():
            try:
                check_once(cfg, client)
            except Exception as e:
                print(f"loop error (continuing): {e}")
            time.sleep(poll)
        else:
            time.sleep(60)                            # closed — check again in a minute


if __name__ == "__main__":
    main()
