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
  - whitelist   : only ever trade names in the configured universe
  - live switch : [proof] live_approved gates PLACEMENT entirely (proof gate §9)

Pure functions + a tiny JSON state file for the drawdown peak. No broker I/O —
the fast loop feeds in account value + plan and acts on the verdict. Params live
in [governance] / [proof] in config/strategy.toml (the source of truth).

Run the tests:  .venv/bin/python src/governance.py --selftest
"""
from __future__ import annotations

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
    """
    if not math.isfinite(account_value):
        return True, float("nan")
    peak = update_peak(account_value)["peak_value"] or float(account_value)
    dd = 0.0 if peak <= 0 else (float(account_value) / peak - 1.0)
    return dd < -abs(cfg["governance"]["max_drawdown"]), dd


def whitelist(cfg) -> set[str]:
    def col(path):
        return {ln.split(",")[0].strip()
                for ln in (REPO / path).read_text().splitlines()[1:] if ln.strip()}
    return col(cfg["universe"]["source"]) | col(cfg["etf_sleeve"]["source"])


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


def vet_plan(plan: list[dict], account_value: float, cfg) -> tuple[list[dict], list[dict]]:
    """Split a plan into (approved, blocked). Each blocked order carries a reason.
    Only BUYS are capped by max_order_pct — capping a sell would strand a position
    the system is trying to exit. For the same reason, a corrupted/missing
    `amount` or a non-finite `account_value` (which makes the cap itself
    meaningless — max_order would be NaN) ONLY ever blocks a BUY here; a SELL
    is never refused for this reason, so a bad mark can never trap an open
    position."""
    g = cfg["governance"]
    wl = whitelist(cfg) if g.get("require_whitelist") else None
    account_value_bad = not math.isfinite(account_value)
    max_order = None if account_value_bad else g["max_order_pct"] * float(account_value)
    approved, blocked = [], []
    for o in plan:
        why = None
        if wl is not None and o["symbol"] not in wl:
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
            assert whitelist(cfg) == {"AAPL", "MSFT", "XLK"}
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

        print("selftest OK: governance two-tier gates "
              "(only the kill switch blocks a sell), peak, whitelist, order cap, "
              "NaN/inf account_value and order-amount fail-closed handling")
    finally:
        REPO, STATE = _repo, _state


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
