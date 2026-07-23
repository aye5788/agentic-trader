"""Weekly moomoo signal-panel collector — forward-log decision context to the ledger.

Runs under SYSTEM /usr/bin/python3 (moomoo needs 3.10), after the Sunday rebalance.
Reads the held book, pulls four moomoo signals per name, distills them, and appends
ONE `signal_panel` journal event. Non-fatal: any failure -> null field + a `gaps`
entry; OpenD down / zero names -> phone push so a silent miss surfaces. Never trades.

    /usr/bin/python3 scripts/collect_signals.py [--dry]

Spec: docs/superpowers/specs/2026-07-23-moomoo-signal-panel-design.md
"""
import datetime as dt
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import signal_panel as sp                       # noqa: E402
from adapters.moomoo import research as mm       # noqa: E402
from adapters.moomoo.client import quote_ctx     # noqa: E402


def _held_book():
    """(list[(symbol, decision_id)], as_of) for names with target_weight>0."""
    import research_store as rs
    prod = rs.read_current()
    if prod is None:
        return [], None
    held = [(t.symbol, t.decision_id or f"{t.symbol}:{prod.as_of}")
            for t in prod.theses if (t.target_weight or 0) > 0]
    return held, prod.as_of


def _panel_for(ctx, sym, overview, snaps):
    """Assemble one name's panel dict; missing pieces -> null + a reason string."""
    gaps = []
    snap = snaps.get(sym, {})
    cf_rows = mm.capital_flow_daily(ctx, sym)
    if not cf_rows:
        gaps.append(f"{sym}: capital_flow")
    si_rows = mm.short_interest(ctx, sym)
    if not si_rows:
        gaps.append(f"{sym}: short_interest")
    if sym not in overview:
        gaps.append(f"{sym}: option_overview")
    if not snap:
        gaps.append(f"{sym}: snapshot")

    panel = {"capflow_bignet_20d": sp.distill_capflow(cf_rows, snap.get("total_market_val"))}
    panel.update(sp.distill_short(si_rows))
    panel.update(sp.distill_options(overview.get(sym, {})))
    panel.update(sp.distill_snapshot(snap))
    return panel, gaps


def main():
    dry = "--dry" in sys.argv
    held, as_of = _held_book()
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    event = {"event": "signal_panel", "as_of": as_of, "at": now,
             "source": "moomoo", "names": {}, "opend_ok": True, "gaps": []}

    if not held:
        print("no held book — nothing to collect")
        return

    syms = [s for s, _ in held]
    ctx = None
    try:
        ctx = quote_ctx()
        overview = mm.option_overview(ctx, syms)
        snaps = mm.snapshot_fields(ctx, syms)
        # OpenD down / systemic failure = both batched pulls empty for a real book
        if not overview and not snaps:
            event["opend_ok"] = False
            event["gaps"].append("OpenD unreachable or returned nothing")
        else:
            for sym, did in held:
                panel, gaps = _panel_for(ctx, sym, overview, snaps)
                panel["decision_id"] = did
                event["names"][sym] = panel
                event["gaps"].extend(gaps)
    except Exception as e:  # never crash the cycle
        event["opend_ok"] = False
        event["gaps"].append(f"collector error: {type(e).__name__}: {e}")
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass

    if dry:
        print(json.dumps(event, indent=2, default=str))
        return

    from research_store.store import append_journal
    append_journal(event)
    print(f"signal_panel: {len(event['names'])} names, {len(event['gaps'])} gaps, "
          f"opend_ok={event['opend_ok']}")

    if not event["opend_ok"] or not event["names"]:
        try:
            import notify
            notify.push("Agentic: signal panel gap",
                        f"opend_ok={event['opend_ok']} names={len(event['names'])} "
                        f"gaps={len(event['gaps'])}", tags="floppy_disk")
        except Exception:
            pass


if __name__ == "__main__":
    main()
