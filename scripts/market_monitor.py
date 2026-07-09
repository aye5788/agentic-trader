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
import strategy as strat            # noqa: E402
import governance as gov            # noqa: E402
from research_store import read_current, store   # noqa: E402
from adapters.schwab import research             # noqa: E402

MON = REPO / "research_store" / "monitor"
STATE = MON / "state.json"
QUOTES = MON / "quotes.json"
COOLDOWN = MON / "cooldown.json"
EXIT_REQ = MON / "exit_request.json"
EXIT_RES = MON / "exit_result.json"
ET = ZoneInfo("America/New_York")


def _now_et():
    return datetime.now(ET)


def market_open(now=None) -> bool:
    now = now or _now_et()
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= mins < 16 * 60          # 09:30–16:00 ET


def _last_price(block: dict):
    q = (block or {}).get("quote", {}) or block or {}
    for k in ("lastPrice", "mark", "closePrice", "bidPrice"):
        v = q.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


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


def run_executor() -> dict:
    """Fire the headless exit executor and return its result file ([] on failure)."""
    try:
        subprocess.run(["claude", "-p", (REPO / "prompts" / "exit.md").read_text()],
                       cwd=str(REPO), timeout=180, check=False)
    except Exception as e:                            # never let execution crash the monitor
        print(f"  executor error: {e}")
    return _load(EXIT_RES, {"sold": []})


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

    if not triggers:
        _save(STATE, st)
        return 0

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for t in triggers:
        print(f"  ⚠ {t['reason'].upper()} {t['symbol']} @ {t['price']} "
              f"(level {t['level']}) — {'EXECUTE' if armed else 'ALERT-ONLY'}")
    store.append_journal({"event": "exit_signal", "ts": ts, "armed": armed,
                          "triggers": triggers})

    if armed:
        _save(EXIT_REQ, {"ts": ts, "account": "948184924", "exits": triggers})
        if EXIT_RES.exists():
            EXIT_RES.unlink()
        result = run_executor()
        sold = {s["symbol"] for s in result.get("sold", [])}
    else:
        sold = {t["symbol"] for t in triggers}       # alert-only: mark seen, don't sell

    # mark fired + cooldown the stops we acted on
    fired_key = {"stop": "stop", "target1": "t1", "target2": "t2"}
    for t in triggers:
        if t["symbol"] in sold:
            st["fired"].setdefault(t["symbol"], []).append(fired_key[t["reason"]])
            if t["reason"] == "stop" and armed:
                add_cooldown(t["symbol"], m.get("cooldown_days", 5))
    _save(STATE, st)
    return len(triggers)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single pass then exit")
    ap.add_argument("--force", action="store_true", help="ignore the market-hours gate")
    args = ap.parse_args()
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
