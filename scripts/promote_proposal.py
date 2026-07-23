"""Human-in-the-loop promotion for adaptive proposals — review, then apply.

Propose-not-apply (spec §8): the off-box tuner only ever writes a PROPOSAL. This
tool is where a human turns a proposal into a live config value — but only by
running it. Nothing here auto-applies without you.

    python scripts/promote_proposal.py            # review the local proposal
    python scripts/promote_proposal.py --apply     # apply the proposal's recommended value
                                                   #   (only if it recommends a move)
    python scripts/promote_proposal.py --set 3.0   # apply a specific value you approved
                                                   #   (use when you reviewed the CI run)

--apply / --set write config/strategy.adaptive.toml (machine-owned, git-ignored),
which strategy.load() deep-merges UNDER your strategy.local.toml — so a hand edit
always overrides the learner. Every apply is band-guarded [1.5, 3.5], provenance-
stamped, and journalled to the ledger. Revert with `--set 2.5` or delete the file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROPOSAL = REPO / "research_store" / "adaptive" / "proposals" / "stop_atr_mult.json"
ADAPTIVE_TOML = REPO / "config" / "strategy.adaptive.toml"
BAND = (1.5, 3.5)


def pending_line(proposal: dict) -> str:
    p = proposal
    return (f"# {p['provenance']}\n"
            f"[trade_management]\n"
            f"stop_atr_mult = {p['recommended']}")


def _journal_apply(value: float, provenance: str, now_iso: str) -> None:
    """Best-effort audit trail into the ledger. Never blocks the apply."""
    try:
        sys.path.insert(0, str(REPO / "src"))
        from research_store.store import append_journal
        append_journal({
            "event": "adaptive_apply",
            "at": now_iso,
            "knob": "trade_management.stop_atr_mult",
            "value": value,
            "provenance": provenance,
        })
    except Exception as e:  # noqa: BLE001 — journaling must never fail the apply
        print(f"  (note: journal append skipped — {type(e).__name__}: {e})")


def apply_value(value: float, provenance: str) -> None:
    value = float(value)
    if not (BAND[0] <= value <= BAND[1]):
        raise SystemExit(f"refusing to apply {value}: outside band {BAND}")
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ADAPTIVE_TOML.write_text(
        "# AUTO-WRITTEN by scripts/promote_proposal.py — do not hand-edit (overwritten\n"
        "# on the next promotion). strategy.local.toml still overrides this. Revert:\n"
        "# `python scripts/promote_proposal.py --set 2.5` or delete this file.\n"
        f"# {provenance}\n"
        f"# applied {now}\n"
        "[trade_management]\n"
        f"stop_atr_mult = {value}\n"
    )
    _journal_apply(value, provenance, now)
    print(f"✅ applied stop_atr_mult = {value}  ->  {ADAPTIVE_TOML.relative_to(REPO)}")
    print("   effective on the next slow-loop rebuild. Revert: --set 2.5 or delete the file.")


def _load_proposal() -> dict | None:
    return json.loads(PROPOSAL.read_text()) if PROPOSAL.exists() else None


def _review(prop: dict) -> None:
    """Plain-English review — no jargon. `--raw` dumps the underlying numbers."""
    inc = prop["incumbent"]
    rec = prop["recommended"]
    moved = prop["moved"]
    ev = prop.get("evidence", {})
    replay_n = ev.get("replay_n", 0)
    live_n = ev.get("live_n", 0)
    names = ev.get("effective_candidates", "?")
    overfit_ok = prop.get("oos_gap", 0.0) <= 0.10
    date = str(prop.get("generated_at", ""))[:10]

    print(f"Stop-loss width check — {date}")
    print("(how far below your entry the stop sits, in units of the stock's own daily swing)")
    print()

    if not moved:
        print(f"  VERDICT: Keep your current setting ({inc}). No change needed.")
        print()
        print(f"  The system replayed {replay_n:,} past trades at tighter and wider stops,")
        print(f"  and your current {inc} came out best — so there's nothing to change.")
    else:
        direction = "WIDER" if rec > inc else "TIGHTER"
        conf = round(float(prop.get("p_better", 0)) * 100)
        print(f"  VERDICT: Consider moving your stop  {inc} → {rec}  ({direction}).")
        print()
        print(f"  Across {replay_n:,} replayed past trades, {rec} scored better than your current")
        print(f"  {inc} — clearing the {conf}% confidence the system needs before it suggests a change.")

    print()
    print("  How much to trust this:")
    live_note = ("none from your live trading yet — this is all historical simulation"
                 if not live_n else f"{live_n} from your own real trades so far")
    print(f"    • Evidence base : {replay_n:,} simulated trades ({live_note})")
    print(f"    • Overfitting   : {'looks solid — held up on unseen data' if overfit_ok else 'CAUTION — may be too good to be true on unseen data'}")
    print(f"    • Coverage      : {names} of your names, today's survivors only")
    print("                      (that slightly favours tighter stops, so trust 'wider' more than 'tighter')")
    print()

    if not moved:
        print("  → Nothing to do.")
    else:
        if rec < inc:
            print("  ⚠ Note: this TIGHTENS the stop — the direction the biased data leans anyway,")
            print("    so be extra skeptical unless real live trades back it up.")
            print()
        print("  If you agree:  python scripts/promote_proposal.py --apply")
        print("  If not:        do nothing (it re-checks next week).")


def main() -> None:
    ap = argparse.ArgumentParser(description="Review or apply an adaptive proposal.")
    ap.add_argument("--apply", action="store_true",
                    help="apply the local proposal's recommended value (only if it moved)")
    ap.add_argument("--set", type=float, metavar="VALUE", default=None,
                    help="apply a specific approved stop_atr_mult, e.g. --set 3.0")
    ap.add_argument("--raw", action="store_true", help="print the raw proposal JSON (all numbers)")
    args = ap.parse_args()

    if args.raw:
        prop = _load_proposal()
        print(json.dumps(prop, indent=2) if prop else f"no proposal found: {PROPOSAL}")
        return

    if args.set is not None:
        today = dt.date.today().isoformat()
        apply_value(args.set, provenance=f"manual --set {args.set} on {today}")
        return

    prop = _load_proposal()

    if args.apply:
        if prop is None:
            raise SystemExit(
                "no local proposal file — review the Actions run and apply with: "
                "python scripts/promote_proposal.py --set VALUE")
        if not prop.get("moved"):
            print(f"proposal recommends NO change ({prop.get('rationale', '')}). Nothing to apply.")
            return
        apply_value(prop["recommended"], provenance=prop.get("provenance", "adaptive proposal"))
        return

    if prop is None:
        print("no proposal found:", PROPOSAL)
        return
    _review(prop)


def _selftest() -> None:
    prop = {"recommended": 3.0, "moved": True,
            "provenance": "adaptive layer 2026-07-26 from replay_n=4120 live_n=7; incumbent was 2.5"}
    line = pending_line(prop)
    assert "stop_atr_mult = 3.0" in line, line
    assert "[trade_management]" in line, line
    assert line.startswith("# adaptive layer"), line
    # band guard must refuse an out-of-band apply
    try:
        apply_value(9.9, "selftest")
        raise AssertionError("apply_value should have refused 9.9")
    except SystemExit:
        pass
    print("selftest OK: pending_line + band guard")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
