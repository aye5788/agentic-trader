#!/usr/bin/env python3
"""RELOAD THE SERVICES RUNNING STALE CODE — the remedy, paired to the detector.

WHY THIS EXISTS
---------------
`src/deployed.py` already works out exactly which long-running service is
running code that no longer matches disk, which files changed, and — in its own
alert text — the precise command that fixes it: `restart <unit>`. It then did
nothing with that, and paged a human at 08:00 the next morning instead. So the
most frequent alert this system produced was one whose entire remedy it had
already computed. Detection without a paired remedy is just a chore with extra
steps, and it made "stale code" the single most common thing the principal was
asked to fix by hand.

This is that remedy. It asks deployed.py what is stale and restarts exactly
those units — never a blanket `restart everything`, which would bounce the stop
watcher on every unrelated edit.

THE ONE THING THAT IS NOT SAFE TO RESTART BLINDLY
-------------------------------------------------
`agentic-monitor.service` IS the stop. Robinhood has no native stop for
fractional shares, so a position's protection is this process watching prices
and firing an exit. Restarting it is nonetheless SMALL — the process re-reads
its state on start, and the gap is under one 15s poll interval — but it is not
zero, and there is one moment where it is genuinely unsafe: while an exit is in
flight. The monitor signals that by writing exit_request.json and DELETING
exit_result.json (market_monitor.py:1058-1060), so "requested but not yet
resulted" is an exact, unambiguous test rather than a guess. In that window we
refuse and say so; the next run picks it up.

`agentic-dashboard.service` is a read-only Flask page on localhost. It has no
such hazard and should never have generated a human alert at all.

USAGE
    .venv/bin/python scripts/reload_stale.py            # restart what is stale
    .venv/bin/python scripts/reload_stale.py --dry-run  # say what it would do
    .venv/bin/python scripts/reload_stale.py --selftest

Exit status: 0 = nothing stale, or everything stale was restarted.
             1 = something stale was NOT restarted (deferred or failed) — the
                 caller still has a reason to look.
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import deployed                                        # noqa: E402

MON = REPO / "research_store" / "monitor"
EXIT_REQ = MON / "exit_request.json"
EXIT_RES = MON / "exit_result.json"


def exit_in_flight(req: pathlib.Path | None = None,
                   res: pathlib.Path | None = None) -> bool:
    """Is the monitor mid-sale? market_monitor writes the request and unlinks
    the result, so request-without-result is exactly the in-flight window.

    ⚠️ The paths are resolved from the module globals at CALL time, not bound as
    default arguments — Python evaluates defaults once at def time, which
    silently pinned this to the live paths and made the in-flight guard
    untestable (and, worse, quietly unpatchable). The selftest caught it.
    """
    req = req or EXIT_REQ
    res = res or EXIT_RES
    return req.exists() and not res.exists()


def blocked_reason(unit: str) -> str | None:
    """Why this unit must NOT be restarted right now, or None if it may be.

    Pure policy, kept apart from the doing so it can be tested without systemd.
    """
    if unit == "agentic-monitor.service" and exit_in_flight():
        return ("an exit is in flight (exit_request.json with no "
                "exit_result.json) — restarting the stop watcher mid-sale "
                "could orphan the sell; deferred to the next run")
    return None


def stale_units(now: dt.datetime | None = None) -> list[dict]:
    """[{unit, key, detail}] for every WATCHED service whose code != disk."""
    now = now or dt.datetime.now(dt.timezone.utc)
    units = [w["unit"] for w in deployed.WATCHED]
    svcs = deployed.gather(REPO, deployed.systemd_start_times(units))
    by_key = {"deployed_" + s["unit"].replace(".service", "").replace("-", "_"): s
              for s in svcs}
    out = []
    for d in deployed.evaluate(now, svcs):
        if d["status"] == "stale":
            svc = by_key.get(d["key"], {})
            out.append({"unit": svc.get("unit", "?"), "key": d["key"],
                        "detail": d["detail"]})
    return out


def restart(unit: str) -> tuple[bool, str]:
    try:
        p = subprocess.run(["systemctl", "restart", unit],
                           capture_output=True, text=True, timeout=120)
    except Exception as e:                              # noqa: BLE001
        return False, str(e)
    if p.returncode != 0:
        return False, (p.stderr or p.stdout or f"exit {p.returncode}").strip()
    return True, "restarted"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be restarted, change nothing")
    args = ap.parse_args()

    stale = stale_units()
    if not stale:
        print("reload_stale: nothing stale — every watched service matches disk")
        return 0

    rc = 0
    for s in stale:
        why = blocked_reason(s["unit"])
        if why:
            print(f"reload_stale: DEFERRED {s['unit']} — {why}")
            rc = 1
            continue
        if args.dry_run:
            print(f"reload_stale: WOULD restart {s['unit']} — {s['detail']}")
            continue
        ok, msg = restart(s["unit"])
        if ok:
            print(f"reload_stale: restarted {s['unit']} — {s['detail']}")
        else:
            print(f"reload_stale: FAILED to restart {s['unit']}: {msg}")
            rc = 1
    return rc


def _selftest() -> None:
    """The policy is what matters here: the stop watcher must be refused while
    an exit is in flight, and nothing else may be refused for that reason."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        req = pathlib.Path(d) / "exit_request.json"
        res = pathlib.Path(d) / "exit_result.json"

        assert exit_in_flight(req, res) is False, "no request = not in flight"
        req.write_text("{}")
        assert exit_in_flight(req, res) is True, "request without result = in flight"
        res.write_text("{}")
        assert exit_in_flight(req, res) is False, "result present = sale concluded"
        req.unlink(); res.unlink()
        assert exit_in_flight(req, res) is False

    # the dashboard is never blocked — it is a read-only page and blocking it
    # would recreate the very alert this script exists to retire
    assert blocked_reason("agentic-dashboard.service") is None

    # ...and the monitor is blocked ONLY by the live in-flight test. Point the
    # module's paths at a clean temp dir to assert the not-in-flight case
    # without depending on whatever the live box happens to be doing.
    global EXIT_REQ, EXIT_RES
    _req, _res = EXIT_REQ, EXIT_RES
    try:
        with tempfile.TemporaryDirectory() as d:
            EXIT_REQ = pathlib.Path(d) / "exit_request.json"
            EXIT_RES = pathlib.Path(d) / "exit_result.json"
            assert blocked_reason("agentic-monitor.service") is None
            EXIT_REQ.write_text("{}")
            r = blocked_reason("agentic-monitor.service")
            assert r and "exit is in flight" in r, r
    finally:
        EXIT_REQ, EXIT_RES = _req, _res

    # every WATCHED unit must have a decidable policy (no KeyError on a new one)
    for w in deployed.WATCHED:
        blocked_reason(w["unit"])

    print("selftest OK: reload_stale -- the stop watcher is refused mid-sale "
          "and only mid-sale; the dashboard is never refused")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        sys.exit(main())
