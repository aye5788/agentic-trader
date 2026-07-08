"""FAST LOOP — turn the stored target book into a concrete order plan, then
(only with explicit go-ahead) place it on the Robinhood Agentic account.

Division of labor (docs/STRATEGY.md §8): the RH calls are MCP tool calls made by
the deployed agent; the *diff* — targets vs. actual holdings -> buy/sell list — is
deterministic and lives here so it's testable and identical every run.

Procedure the agent runs:
  1. get_accounts        -> the ONE account with agentic_allowed=true ("Agentic").
                            Zero or >1 match -> ABORT, trade nothing.
  2. get_equity_positions-> actual holdings; get_portfolio -> account value.
     Write both to research_store/rh/positions.json (the snapshot this reads).
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
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

SNAPSHOT = REPO / "research_store" / "rh" / "positions.json"
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
    print("selftest OK: exits, opens, rebalances, and band-skip all correct")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--snapshot", default=str(SNAPSHOT),
                    help="RH positions snapshot json {account_value, positions:{sym:mktval}}")
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

    snap = Path(args.snapshot)
    if not snap.exists():
        sys.exit(f"no RH snapshot at {snap}\n  (the agent writes it from get_equity_positions "
                 f"+ get_portfolio before running the fast loop)")
    data = json.loads(snap.read_text())
    positions, acct = data.get("positions", {}), float(data["account_value"])

    plan = plan_orders(targets, positions, acct)
    print(f"target book as_of {prod.as_of} | regime {prod.regime['status']} | "
          f"account ${acct:,.2f} | {len(targets)} targets")

    # ---- governance: global halts, then per-order vetting ----
    halts = gov.preflight(acct, cfg)
    if halts:
        print("\n⛔ TRADING HALTED — place NOTHING:")
        for h in halts:
            print(f"   - {h}")
        return
    approved, blocked = gov.vet_plan(plan, acct, cfg)

    if not plan:
        print("\nno orders — portfolio already matches the target book.")
        return
    print(f"\nORDER PLAN ({len(approved)} approved, {len(blocked)} blocked):")
    for o in approved:
        print(f"  {o['side'].upper():4} ${o['amount']:>8.2f} {o['symbol']:6} "
              f"[{o['reason']:16}] cur ${o['current_value']:.2f} -> tgt ${o['target_value']:.2f}")
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
    out = REPO / "research_store" / "rh" / "order_plan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"as_of": prod.as_of, "account_value": acct,
                               "live_approved": live, "approved": approved,
                               "blocked": blocked}, indent=2))
    print(f"plan -> {out}")


if __name__ == "__main__":
    main()
