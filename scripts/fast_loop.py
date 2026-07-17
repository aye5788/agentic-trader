"""FAST LOOP — turn the stored target book into a concrete order plan, then
(only with explicit go-ahead) place it on the Robinhood Agentic account.

Division of labor (docs/STRATEGY.md §8): the RH calls are MCP tool calls made by
the deployed agent; the *diff* — targets vs. actual holdings -> buy/sell list — is
deterministic and lives here so it's testable and identical every run.

Procedure the agent runs:
  1. get_accounts        -> the ONE account with agentic_allowed=true ("Agentic").
                            Zero or >1 match -> ABORT, trade nothing.
  2. get_equity_positions-> actual holdings (qty + avg cost); get_portfolio ->
     account value + cash; get_equity_quotes -> last prices. Write the snapshot
     research_store/rh/positions.json (schema in src/marks.py — this reads it
     through marks.load(), qty × freshest mark).
  3. plan_orders(...)    -> fractional dollar-notional buy/sell list.
  4. review_equity_order -> (human approval) -> place_equity_order, per order.
     Equities only, options OFF. Never touch any non-Agentic account.

    python scripts/fast_loop.py --selftest      # prove the diff logic
    python scripts/fast_loop.py                 # plan from the live snapshot, print only

This script NEVER places orders — placement is the agent's MCP step, gated by the
proof gate (§9: no live orders until backtested AND human-approved).
"""
import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

SNAPSHOT = REPO / "research_store" / "rh" / "positions.json"
REENTRY = REPO / "research_store" / "monitor" / "reentry_review.json"
COOLDOWN = REPO / "research_store" / "monitor" / "cooldown.json"
MIN_ORDER = 1.00          # RH fractional min ~ $1; skip dust
REBALANCE_BAND = 0.005    # skip trims/adds smaller than 0.5% of the account (churn guard)


def plan_orders(targets: dict, positions: dict, account_value: float,
                *, min_order: float = MIN_ORDER, band: float = REBALANCE_BAND) -> list[dict]:
    """Deterministic diff. `targets`={sym: weight}, `positions`={sym: market_value $},
    `account_value`=total $ to allocate. Returns dollar-notional buy/sell orders.

    - a target with no/short position -> BUY the shortfall
    - a held name below/above target  -> trim/add to target (unless within `band`)
    - a held name NOT in targets       -> SELL to zero (full exit)
    """
    orders = []
    for sym in sorted(set(targets) | set(positions)):
        tgt_val = targets.get(sym, 0.0) * account_value
        cur_val = positions.get(sym, 0.0)
        delta = tgt_val - cur_val
        if abs(delta) < min_order:
            continue
        if account_value > 0 and abs(delta) / account_value < band and tgt_val > 0:
            continue    # small rebalance of an existing hold -> leave it (churn guard)
        orders.append({
            "symbol": sym, "side": "buy" if delta > 0 else "sell",
            "amount": round(abs(delta), 2), "target_weight": round(targets.get(sym, 0.0), 4),
            "current_value": round(cur_val, 2), "target_value": round(tgt_val, 2),
            "reason": "exit (not in book)" if tgt_val == 0 else
                      ("open" if cur_val == 0 else "rebalance"),
        })
    # sells first (free up cash before buys) — matters for a cash account
    return sorted(orders, key=lambda o: (o["side"] != "sell", -o["amount"]))


def apply_reentry(orders: list[dict], reviews: dict, prices: dict,
                  guard: float) -> tuple[list[dict], list[dict], list[dict]]:
    """Route buys for names under post-take-profit review ([reentry]) out of
    the auto-place list. Returns (approved, review, blocked):
    - price fell below exit_price*(1-guard) -> BLOCKED (hard knife-guard, no
      discretion — this is code, not judgment)
    - otherwise (or price unknown here)     -> REVIEW: the agent judges
      full / half / skip (veto-downsize only; prompts/fast_loop.md)
    Sells and unreviewed buys pass straight through. Pure; covered by --selftest."""
    approved, review, blocked = [], [], []
    for o in orders:
        r = reviews.get(o["symbol"]) if o["side"] == "buy" else None
        if not r:
            approved.append(o)
            continue
        floor = round(float(r["exit_price"]) * (1.0 - guard), 4)
        px = prices.get(o["symbol"])
        if px is not None and px < floor:
            blocked.append({**o, "blocked":
                            f"reentry knife-guard: {px} < {floor} (fell >{guard:.0%} "
                            f"below the {r['tier']} exit at {r['exit_price']})"})
        else:
            review.append({**o, "reentry": {"tier": r["tier"],
                                            "exit_price": r["exit_price"],
                                            "knife_floor": floor,
                                            "price_checked": px}})
    return approved, review, blocked


def load_cooldown(path: Path = COOLDOWN, today: str | None = None) -> set:
    """Symbols still inside their stop-out cooldown window (until-date >= today).

    Same file the slow loop honors (scripts/slow_loop.py) — the monitor writes it
    when a stop fires. Absent/torn file -> empty set (fail open: never block a buy
    just because the cooldown file is missing)."""
    today = today or date.today().isoformat()
    try:
        cd = json.loads(path.read_text())
    except Exception:
        return set()
    return {s for s, until in cd.items() if str(until) >= today}


def apply_cooldown(orders: list[dict], cooled: set) -> tuple[list[dict], list[dict]]:
    """Block BUY orders for names on active stop-out cooldown — the fast-loop half
    of the stop-vs-momentum churn guard, so a name the monitor just stopped out is
    not rebought against a book that hasn't rebuilt yet. Sells/exits and non-cooled
    buys pass straight through (cooldown never blocks getting OUT). Returns
    (kept, blocked). Pure; covered by --selftest."""
    kept, blocked = [], []
    for o in orders:
        if o["side"] == "buy" and o["symbol"] in cooled:
            blocked.append({**o, "blocked": "cooldown: stopped out recently — "
                                            "no rebuy until the cooldown window clears"})
        else:
            kept.append(o)
    return kept, blocked


def load_reviews(path: Path = REENTRY) -> dict:
    """Active re-entry flags, pruning expired ones on the way through."""
    try:
        rv = json.loads(path.read_text())
    except Exception:
        return {}
    today = date.today().isoformat()
    live = {s: r for s, r in rv.items() if r.get("expires", "9999") >= today}
    if live != rv:
        path.write_text(json.dumps(live, indent=2))
    return live


def _selftest() -> None:
    targets = {"AAPL": 0.10, "MSFT": 0.10, "NVDA": 0.10}
    positions = {"AAPL": 5.0, "NVDA": 30.0, "TSLA": 12.0}   # TSLA not in book, NVDA overweight
    acct = 300.0
    plan = plan_orders(targets, positions, acct)
    for o in plan:
        print(f"  {o['side'].upper():4} ${o['amount']:>7.2f} {o['symbol']:5} "
              f"[{o['reason']}] {o['current_value']}->{o['target_value']}")
    # expected: SELL TSLA $12 (exit), SELL NVDA $0 (30->30 within band=skip),
    #           BUY MSFT $30 (open), BUY AAPL $25 (5->30)
    syms = {o["symbol"]: o for o in plan}
    assert syms["TSLA"]["side"] == "sell" and syms["TSLA"]["amount"] == 12.0
    assert syms["MSFT"]["side"] == "buy" and syms["MSFT"]["amount"] == 30.0
    assert syms["AAPL"]["side"] == "buy" and syms["AAPL"]["amount"] == 25.0
    assert "NVDA" not in syms   # 30->30 exactly, within band -> no order

    # --- re-entry routing: review when price holds, hard-block on the knife ---
    reviews = {"MSFT": {"tier": "target1", "exit_price": 100.0}}
    orders = [{"symbol": "MSFT", "side": "buy", "amount": 30.0},
              {"symbol": "AAPL", "side": "buy", "amount": 25.0},
              {"symbol": "TSLA", "side": "sell", "amount": 12.0}]
    ok, rev, blk = apply_reentry(orders, reviews, {"MSFT": 99.0}, 0.04)
    assert [o["symbol"] for o in ok] == ["AAPL", "TSLA"] and not blk
    assert rev[0]["symbol"] == "MSFT" and rev[0]["reentry"]["knife_floor"] == 96.0
    ok, rev, blk = apply_reentry(orders, reviews, {"MSFT": 95.9}, 0.04)
    assert not rev and blk[0]["symbol"] == "MSFT" and "knife-guard" in blk[0]["blocked"]
    ok, rev, blk = apply_reentry(orders, reviews, {}, 0.04)   # price unknown -> review
    assert rev and rev[0]["reentry"]["price_checked"] is None

    # --- cooldown: block rebuys of stopped-out names, never block sells/exits ---
    orders = [{"symbol": "XLK", "side": "buy", "amount": 5.0},    # cooled -> blocked
              {"symbol": "AAPL", "side": "buy", "amount": 25.0},  # not cooled -> kept
              {"symbol": "XLK", "side": "sell", "amount": 3.0}]   # exit of cooled name -> kept
    kept, blk = apply_cooldown(orders, {"XLK"})
    assert [(o["symbol"], o["side"]) for o in kept] == [("AAPL", "buy"), ("XLK", "sell")], kept
    assert len(blk) == 1 and blk[0]["symbol"] == "XLK" and blk[0]["side"] == "buy"
    assert "cooldown" in blk[0]["blocked"]
    kept, blk = apply_cooldown(orders, set())                    # nothing cooled -> all pass
    assert not blk and len(kept) == 3
    # load_cooldown honors the until-date; absent file fails open (empty set)
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "cooldown.json"
        p.write_text(json.dumps({"XLK": "2026-07-22", "OLD": "2020-01-01"}))
        assert load_cooldown(p, today="2026-07-17") == {"XLK"}   # OLD expired -> excluded
        assert load_cooldown(p, today="2026-07-25") == set()     # all expired
        assert load_cooldown(Path(d) / "missing.json") == set()  # absent -> fail open
    print("selftest OK: exits, opens, rebalances, band-skip, reentry routing, cooldown block")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--snapshot", default=str(SNAPSHOT),
                    help="RH positions snapshot json (schema: src/marks.py)")
    args = ap.parse_args()

    if args.selftest:
        _selftest()
        return

    import strategy as strat                                   # noqa: E402
    import governance as gov                                    # noqa: E402
    from research_store import read_current, get_targets        # noqa: E402
    cfg = strat.load()
    prod = read_current()
    if prod is None:
        sys.exit("no research product — run scripts/slow_loop.py first")
    targets = get_targets()

    import marks                                                 # noqa: E402
    valued = marks.load(Path(args.snapshot))
    if valued is None:
        sys.exit(f"no RH snapshot at {args.snapshot}\n  (the agent writes it from "
                 f"get_equity_positions + get_portfolio before running the fast loop)")
    positions = {s: p["value"] for s, p in valued["positions"].items()}
    acct = valued["account_value"]

    plan = plan_orders(targets, positions, acct)
    print(f"target book as_of {prod.as_of} | regime {prod.regime['status']} | "
          f"account ${acct:,.2f} | {len(targets)} targets")

    # ALWAYS overwrite the plan file — a stale plan from a prior run must never
    # survive an empty/halted run, or the placement agent could re-execute it.
    out = REPO / "research_store" / "rh" / "order_plan.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    def write_plan(approved, blocked, live, halted=None, review=None):
        out.write_text(json.dumps({"as_of": prod.as_of, "account_value": acct,
                                   "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                   "live_approved": live, "halted": halted or [],
                                   "approved": approved, "review": review or [],
                                   "blocked": blocked}, indent=2))

    # ---- governance: global halts, then per-order vetting ----
    halts = gov.preflight(acct, cfg)
    if halts:
        print("\n⛔ TRADING HALTED — place NOTHING:")
        for h in halts:
            print(f"   - {h}")
        write_plan([], [], False, halted=halts)
        print(f"empty plan -> {out}")
        return
    approved, blocked = gov.vet_plan(plan, acct, cfg)

    # ---- stop-out cooldown: never rebuy a name the monitor just stopped ----
    # The book won't drop a stopped name until the weekly rebuild, so without this
    # the daily fast loop rebuys it the same session (churn). Mirrors slow_loop.py.
    cooled = load_cooldown()
    if cooled:
        approved, cblocked = apply_cooldown(approved, cooled)
        if cblocked:
            print(f"cooldown: blocking rebuy of "
                  f"{', '.join(sorted(o['symbol'] for o in cblocked))}")
        blocked += cblocked

    # ---- post-take-profit re-entry: judgment, not rubber-stamp ([reentry]) ----
    review = []
    rcfg = cfg.get("reentry", {})
    reviews = load_reviews() if rcfg.get("enabled") else {}
    if reviews:
        prices = {s: p["mark"] for s, p in valued["positions"].items() if p.get("mark")}
        approved, review, rblocked = apply_reentry(
            approved, reviews, prices, float(rcfg.get("knife_guard_pct", 0.04)))
        blocked += rblocked

    if not plan:
        print("\nno orders — portfolio already matches the target book.")
        write_plan([], [], gov.live_approved(cfg))
        print(f"empty plan -> {out}")
        return
    print(f"\nORDER PLAN ({len(approved)} approved, {len(review)} for re-entry review, "
          f"{len(blocked)} blocked):")
    for o in approved:
        print(f"  {o['side'].upper():4} ${o['amount']:>8.2f} {o['symbol']:6} "
              f"[{o['reason']:16}] cur ${o['current_value']:.2f} -> tgt ${o['target_value']:.2f}")
    for o in review:
        r = o["reentry"]
        print(f"  REVIEW ${o['amount']:>7.2f} {o['symbol']:6} — hit {r['tier']} @ "
              f"{r['exit_price']}; agent judges full/half/skip (knife floor {r['knife_floor']})")
    for o in blocked:
        print(f"  BLOCK     {o['symbol']:6} — {o['blocked']}")
    buys = sum(o["amount"] for o in approved if o["side"] == "buy")
    sells = sum(o["amount"] for o in approved if o["side"] == "sell")
    print(f"\ntotal (approved): ${sells:.2f} sells, ${buys:.2f} buys | net ${buys-sells:+.2f}")

    live = gov.live_approved(cfg)
    print(f"\nlive_approved = {live} — "
          + ("agent MAY place the approved orders (review_equity_order->place)."
             if live else
             "PLAN ONLY. Set [proof] live_approved=true to authorize placement (proof gate §9)."))
    # emit machine-readable plan for the agent's placement step
    write_plan(approved, blocked, live, review=review)
    print(f"plan -> {out}")


if __name__ == "__main__":
    main()
