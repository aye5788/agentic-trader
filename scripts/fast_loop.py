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
                *, min_order: float = MIN_ORDER, band: float = REBALANCE_BAND,
                shares: dict | None = None) -> list[dict]:
    """Deterministic diff. `targets`={sym: weight}, `positions`={sym: market_value $},
    `account_value`=total $ to allocate. Returns dollar-notional buy/sell orders.

    - a target with no/short position -> BUY the shortfall
    - a held name below/above target  -> trim/add to target (unless within `band`)
    - a held name NOT in targets       -> SELL to zero (full exit)

    A FULL EXIT also carries `quantity` (exact shares held, from `shares`), and the
    placement step must use it instead of the dollar amount. A dollar-denominated
    sell-all cannot reliably close a position: the amount is computed from a stored
    mark, so by the time it reaches the broker it is either a hair OVER the live
    position value -- Robinhood rejects it with EQUITY_DOLLAR_BASED_SELL_ALL_ERROR
    and fast_loop.md tells the agent to skip the order -- or a hair UNDER, which
    fills but leaves a dust position behind. Dust is the worse outcome of the two:
    it stays held, stays OUT of the book, and is therefore watched by neither the
    monitor nor the risk review. Verified against the live broker on 2026-07-30 with
    XLI: $4.96 (99.7% of the position) alerted, 0.027904 shares reviewed clean.
    """
    shares = shares or {}
    orders = []
    for sym in sorted(set(targets) | set(positions)):
        tgt_val = targets.get(sym, 0.0) * account_value
        cur_val = positions.get(sym, 0.0)
        delta = tgt_val - cur_val
        if abs(delta) < min_order:
            continue
        if account_value > 0 and abs(delta) / account_value < band and tgt_val > 0:
            continue    # small rebalance of an existing hold -> leave it (churn guard)
        order = {
            "symbol": sym, "side": "buy" if delta > 0 else "sell",
            "amount": round(abs(delta), 2), "target_weight": round(targets.get(sym, 0.0), 4),
            "current_value": round(cur_val, 2), "target_value": round(tgt_val, 2),
            "reason": "exit (not in book)" if tgt_val == 0 else
                      ("open" if cur_val == 0 else "rebalance"),
        }
        # Full exit -> close it by SHARES, not dollars (see docstring). Guarded on a
        # positive qty: the legacy snapshot schema reports qty=None, and there the
        # dollar amount remains the only thing we know.
        if tgt_val == 0 and order["side"] == "sell":
            qty = shares.get(sym)
            if qty and qty > 0:
                order["quantity"] = qty
        orders.append(order)
    # sells first (free up cash before buys) — matters for a cash account
    return sorted(orders, key=lambda o: (o["side"] != "sell", -o["amount"]))


def apply_chase_guard(orders: list[dict], zones: dict, prices: dict,
                      *, tol_sigma: float) -> tuple[list[dict], list[dict]]:
    """Enforce `[trade_management] no_chase` — don't buy a name that has already
    run away from its thesis entry zone. Returns (kept, blocked). Pure.

    This implements a rule that was documented in docs/STRATEGY.md §6,
    docs/DESIGN.md, the Thesis dataclass, and set as `no_chase = true` in
    strategy.toml — and enforced by NOTHING. Every buy went in at market at
    whatever the price was. On 2026-07-23 LITE filled 5.0% above its zone and
    exited -18.1%.

    ASYMMETRIC ON PURPOSE. Only an over-price blocks. The documented wording
    ("only buy inside the entry_zone") would also skip a name trading BELOW the
    zone — refusing a better fill, which is nonsense. Cheap never blocks.

    Ceiling is vol-scaled (`tol_sigma` * the name's daily sigma) because the zone
    itself is a flat +/-0.5% band while the rest of the geometry is vol-scaled: a
    fixed band is noise for a 7%/day mover and a straitjacket for a 1.5%/day ETF.
    At 0.5 sigma this blocks ~8% of entries (2y, 168 names).

    FAILS OPEN by design: a missing zone, sigma, or quote passes the order
    through. A guard that halts all buying on a quote hiccup is a new outage;
    passing through is exactly today's behaviour, never worse.
    """
    kept, blocked = [], []
    for o in orders:
        z = zones.get(o["symbol"]) or {}
        hi, sig, px = z.get("high"), z.get("sigma"), prices.get(o["symbol"])
        if o["side"] != "buy" or not hi or not sig or not px:
            kept.append(o)
            continue
        ceiling = hi * (1.0 + tol_sigma * sig)
        if px > ceiling:
            blocked.append({**o, "blocked": (
                f"no_chase: {px:.4g} > zone high {hi:.4g} +{tol_sigma:g}s "
                f"(ceiling {ceiling:.4g}, sigma {sig*100:.1f}%/d)")})
        else:
            kept.append(o)
    return kept, blocked


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

    # A full exit must carry `quantity` so the agent closes it by SHARES. A
    # dollar-denominated sell-all is computed from a stored mark and so reaches the
    # broker either just over the live position value (rejected:
    # EQUITY_DOLLAR_BASED_SELL_ALL_ERROR -> fast_loop.md skips the order) or just
    # under (fills, leaves dust that is held but off-book, hence watched by neither
    # the monitor nor the risk review). Confirmed live on 2026-07-30 with XLI.
    ex = {o["symbol"]: o for o in plan_orders(targets, positions, acct,
                                              shares={"TSLA": 0.0279, "NVDA": 1.5})}
    assert ex["TSLA"]["quantity"] == 0.0279, ex["TSLA"]
    assert ex["TSLA"]["side"] == "sell" and ex["TSLA"]["target_value"] == 0
    # a partial trim is NOT a full exit -> stays dollar-denominated
    trim = {o["symbol"]: o for o in plan_orders({"NVDA": 0.02}, {"NVDA": 30.0}, acct,
                                                shares={"NVDA": 1.5})}
    assert "quantity" not in trim["NVDA"], trim["NVDA"]
    # buys are never quantity-denominated
    assert all("quantity" not in o for o in plan_orders(targets, positions, acct,
                                                        shares={"MSFT": 9.9})
               if o["side"] == "buy")
    # legacy snapshot (qty unknown) must fall back to dollars, not emit qty=None
    legacy = {o["symbol"]: o for o in plan_orders(targets, positions, acct,
                                                  shares={"TSLA": None})}
    assert "quantity" not in legacy["TSLA"], legacy["TSLA"]
    assert legacy["TSLA"]["amount"] == 12.0
    # and with no shares mapping at all, behaviour is exactly as before
    assert "quantity" not in {o["symbol"]: o for o in plan_orders(
        targets, positions, acct)}["TSLA"]
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
    # --- no-chase guard (the rule that was documented but never wired) --------
    Z = {"LITE": {"high": 833.85, "sigma": 0.0569},   # 0.5s ceiling ~ 857.6
         "MU":   {"high": 964.28, "sigma": 0.0487},   # 0.5s ceiling ~ 987.8
         "EEM":  {"high": 65.31,  "sigma": 0.0151}}
    b = lambda s: {"symbol": s, "side": "buy", "amount": 5.0}
    kept, blk = apply_chase_guard([b("LITE")], Z, {"LITE": 875.34}, tol_sigma=0.5)
    assert not kept and len(blk) == 1, (kept, blk)          # the real 07-23 fill
    assert "no_chase" in blk[0]["blocked"]
    kept, blk = apply_chase_guard([b("MU")], Z, {"MU": 983.84}, tol_sigma=0.5)
    assert len(kept) == 1 and not blk, (kept, blk)          # inside tolerance
    # ASYMMETRY: below the zone is a better fill, never blocked
    kept, blk = apply_chase_guard([b("EEM")], Z, {"EEM": 40.0}, tol_sigma=0.5)
    assert len(kept) == 1 and not blk, "cheap must never block"
    # sells are never chase-guarded
    kept, blk = apply_chase_guard([{"symbol": "LITE", "side": "sell", "amount": 5.0}],
                                  Z, {"LITE": 99999.0}, tol_sigma=0.5)
    assert len(kept) == 1 and not blk, "sells must pass"
    # fail OPEN on missing quote / sigma / zone
    for zz, pp in ((Z, {}), ({"LITE": {"high": 833.85}}, {"LITE": 875.34}), ({}, {"LITE": 875.34})):
        kept, blk = apply_chase_guard([b("LITE")], zz, pp, tol_sigma=0.5)
        assert len(kept) == 1 and not blk, ("must fail open", zz, pp)
    # tolerance actually widens
    _, blk = apply_chase_guard([b("LITE")], Z, {"LITE": 875.34}, tol_sigma=1.0)
    assert not blk, "1.0s must admit the 07-23 LITE fill"
    print("selftest OK: exits, opens, rebalances, band-skip, reentry routing, cooldown block, "
          "no-chase (asymmetric, fail-open)")


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
    # exact share counts, so a full exit can be closed by quantity rather than $
    held_shares = {s: p.get("qty") for s, p in valued["positions"].items()}
    acct = valued["account_value"]

    plan = plan_orders(targets, positions, acct, shares=held_shares)
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

    # ---- governance: two-tier halts, then per-order vetting ----
    # block_all (kill switch)          -> place NOTHING, buy or sell.
    # block_entries (HALT_ENTRIES, dd) -> refuse buys, let exits through.
    # The split exists because stops here are SOFTWARE-ONLY: blocking a sell does
    # not pause risk, it removes an open position's only protection.
    gv = gov.gates(acct, cfg)
    if gv["block_all"]:
        print("\n⛔ TRADING HALTED — place NOTHING:")
        for h in gv["block_all"]:
            print(f"   - {h}")
        write_plan([], [], False, halted=gv["block_all"])
        print(f"empty plan -> {out}")
        return
    approved, blocked = gov.vet_plan(plan, acct, cfg)
    if gv["block_entries"]:
        print("\n⛔ NEW ENTRIES HALTED — exits still allowed:")
        for h in gv["block_entries"]:
            print(f"   - {h}")
        approved, eblocked = gov.apply_entry_halts(approved, gv["block_entries"])
        blocked += eblocked

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

    # ---- no-chase: never open a name that has run past its entry zone --------
    tm = cfg.get("trade_management", {})
    if tm.get("no_chase"):
        want = sorted({o["symbol"] for o in approved if o["side"] == "buy"})
        quotes = {}
        if want:
            try:                       # moomoo = quote source (via OpenD)
                from adapters.moomoo import prices as _mmp    # noqa: E402
                for sym, q in (_mmp.live_quotes(want) or {}).items():
                    px = (q or {}).get("last")
                    if px:
                        quotes[sym] = float(px)
            except Exception as e:     # fail OPEN, but never silently
                print(f"no_chase: quote fetch failed ({type(e).__name__}: {e}) — "
                      f"guard SKIPPED this run, orders pass through")
            missing = [s for s in want if s not in quotes]
            if missing:
                print(f"no_chase: no quote for {', '.join(missing)} — passing through")
        zones = {t.symbol: {"high": (t.entry_zone or [None, None])[1],
                            "sigma": (t.signals or {}).get("sigma")}
                 for t in prod.theses}
        approved, chased = apply_chase_guard(
            approved, zones, quotes, tol_sigma=float(tm.get("chase_tol_sigma", 0.5)))
        if chased:
            print("no_chase: blocking "
                  + ", ".join(sorted(o["symbol"] for o in chased)))
        blocked += chased

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
