"""Weekly moomoo signal-panel collector — forward-log decision context to the ledger.

Runs under SYSTEM /usr/bin/python3 (moomoo needs 3.10), after the Sunday rebalance.
Reads the held book, pulls four moomoo signals per name, distills them, and appends
ONE `signal_panel` journal event. Non-fatal: any failure -> null field + a `gaps`
entry; OpenD down / zero names -> phone push so a silent miss surfaces. Never trades.

    /usr/bin/python3 scripts/collect_signals.py [--dry] [--timeout SECONDS]

Hang-safe on both OpenD failure modes: NOT LISTENING is caught by the TCP preflight
in adapters/moomoo/client.py, and LISTENING-BUT-WEDGED by the SIGALRM deadline here.
Both surface as opend_ok=False + a phone alert rather than a stuck process — before
these guards, OpenD being down hung the cron forever and the alert never fired.

Spec: docs/superpowers/specs/2026-07-23-moomoo-signal-panel-design.md
"""
import contextlib
import datetime as dt
import json
import pathlib
import signal
import sys

# Bound the whole moomoo pull. Generous — the healthy run takes ~10-20s for a
# 14-name book; this only has to be shorter than "forever".
COLLECT_TIMEOUT = 300
CLOSE_TIMEOUT = 15

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


class _Timeout(Exception):
    """Collection blew its deadline. Subclasses Exception so main()'s existing
    handler catches it and turns it into opend_ok=False + a phone alert."""


@contextlib.contextmanager
def _deadline(seconds: int, what: str):
    """Bound a block with SIGALRM.

    Complements the TCP preflight in adapters/moomoo/client.py. Preflight catches
    "OpenD is not listening"; this catches "OpenD is listening but WEDGED", where
    the socket connects fine and then a call never comes back. Without a deadline
    that stalls the weekly cron forever, and — because the alert lives in the
    except-branch — stalls it *silently*.

    SIGALRM is main-thread-only, which is fine: this is a single-threaded cron
    script. The alarm is always cleared in the finally, so a fast path never
    leaves a stray timer armed.
    """
    def _fire(signum, frame):
        raise _Timeout(f"{what} exceeded {seconds}s — OpenD wedged?")

    old = signal.signal(signal.SIGALRM, _fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def main():
    dry = "--dry" in sys.argv
    timeout = COLLECT_TIMEOUT
    if "--timeout" in sys.argv:
        timeout = int(sys.argv[sys.argv.index("--timeout") + 1])
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
        with _deadline(timeout, "moomoo collection"):
            ctx = quote_ctx()
            overview = mm.option_overview(ctx, syms)
            snaps = mm.snapshot_fields(ctx, syms)
            # OpenD down / systemic failure = both batched pulls empty for a real book
            if not overview and not snaps:
                event["opend_ok"] = False
                event["gaps"].append("OpenD unreachable or returned nothing")
            else:
                for sym, did in held:
                    try:
                        panel, gaps = _panel_for(ctx, sym, overview, snaps)
                        panel["decision_id"] = did
                        event["names"][sym] = panel
                        event["gaps"].extend(gaps)
                    except Exception as e:  # one bad name must not lose the rest of the book
                        event["gaps"].append(f"{sym}: panel error: {type(e).__name__}: {e}")
    except Exception as e:  # never crash the cycle
        event["opend_ok"] = False
        event["gaps"].append(f"collector error: {type(e).__name__}: {e}")
    finally:
        if ctx is not None:
            try:
                # close() talks to the same wedged gateway, so it needs its own
                # (short) bound — otherwise the timeout we just escaped is undone
                # by hanging on the way out.
                with _deadline(CLOSE_TIMEOUT, "OpenD close"):
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
