#!/usr/bin/env python3
"""Daily upkeep reminder — pushes when a scheduled job stops leaving evidence.

Runs `src/health.py` and phones you about anything unhealthy. Deliberately
boring: it places no trades, changes no config, and touches nothing but its own
state file.

ALERTING CONTRACT (chosen by Aaron 2026-07-24): **one alert per condition.**
A condition fires once when it first goes bad and then stays quiet, however long
it stays bad. Fixing it clears the flag silently — no "resolved" ping — so the
next time it breaks you hear about it again.

The obvious objection is that a missed buzz means a missed problem forever. That
is handled by pairing, not by nagging: the dashboard renders this same check list
continuously, so an unresolved condition stays visible long after its one
notification is gone. Push tells you something changed; the dashboard tells you
what is true now. Repeating the push would only add noise to a channel whose
value depends on staying rare.

Messages carry job names and ages ONLY — never positions, prices or P&L — which
is what makes them safe on the shared ops topic (see src/notify.py:ops_topic).

    python scripts/health_check.py            # check + push if needed
    python scripts/health_check.py --dry      # print what it WOULD push
    python scripts/health_check.py --selftest # logic tests, no I/O
"""
import argparse
import datetime as dt
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import health                                    # noqa: E402
from notify import push, ops_topic               # noqa: E402

STATE = REPO / "research_store" / "health_state.json"

# "never ran" and "stopped running" have different causes and different fixes.
# A job that has NEVER left an artifact is usually not scheduled at all — that was
# the 2026-07-24 signal-panel bug, where the cron line existed only in the template
# and never in `crontab -l`. Telling you to go re-login to OpenD in that situation
# sends you to the wrong place entirely, so "never" gets its own advice.
NEVER_REMEDY = "Has this EVER been scheduled? Check `crontab -l` (editing crontab.template arms nothing)"

# What to actually DO about each condition — the whole point of the alert.
REMEDY = {
    "schwab_token":  "Run: .venv/bin/python scripts/schwab_auth.py (OPERATOR_MANUAL §1)",
    "signal_panel":  "OpenD likely logged out — see OPERATOR_MANUAL §4",
    "ledger_backup": "Check the box still has push access to agentic-trader-ledger",
    "adaptive_tune": "Check GitHub Actions — the weekly tuner has not run",
    "slow_loop":     "Check logs/slow.log",
    "fast_loop":     "Check logs/fast.log",
    "risk_review":   "Check logs/risk_review.log",
    "monitor":       "systemctl status agentic-monitor",
    "newsletter":    "Check logs/newsletter.log",
}


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {"flagged": {}}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=2, sort_keys=True))


def diff(rows, flagged: dict) -> tuple[list, list]:
    """Pure: (checks, previously-flagged keys) -> (to_alert, healed_keys).

    to_alert = unhealthy and not already flagged  (fire-once)
    healed   = previously flagged, now healthy    (clears silently)
    """
    to_alert = [c for c in rows if c.alertable and c.key not in flagged]
    healed = [c.key for c in rows if c.healthy and c.key in flagged]
    return to_alert, healed


def compose(to_alert) -> tuple[str, str]:
    """Build the push. Job names + ages only — nothing about the book."""
    n = len(to_alert)
    urgent = any(c.status in ("expired", "due") for c in to_alert)
    if n == 1:
        title = f"Agentic upkeep: {to_alert[0].label}"
    else:
        title = f"Agentic upkeep: {n} items need you"
    lines = []
    for c in to_alert:
        lines.append(f"• {c.label}: {c.detail}")
        remedy = NEVER_REMEDY if c.status == "never" else REMEDY.get(c.key)
        if remedy:
            lines.append(f"  {remedy}")
    if urgent:
        lines.append("")
        lines.append("Schwab expiry kills the price feed — stops stop being watched.")
    return title, "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="print, don't push")
    ap.add_argument("--offline", action="store_true", help="skip the GitHub Actions probe")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        _selftest()
        return

    rows = health.checks(use_network=not args.offline)
    st = load_state()
    flagged = st.get("flagged", {})

    to_alert, healed = diff(rows, flagged)

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for key in healed:
        flagged.pop(key, None)
        print(f"healed: {key}")
    for c in to_alert:
        flagged[c.key] = {"at": now, "status": c.status, "detail": c.detail}

    if to_alert:
        title, body = compose(to_alert)
        header = "would push (dry)" if args.dry else "pushed"
        print(f"--- {header} ---\n{title}\n{body}\n{'-' * (len(header) + 8)}")
        if not args.dry:
            push(title, body, tags="wrench", topic=ops_topic())
    elif flagged:
        # Silent by design (fire-once) — but don't let the log claim all is well.
        print(f"no NEW conditions; {len(flagged)} still unresolved and already "
              f"notified: {', '.join(sorted(flagged))}")
    else:
        print(f"all clear ({sum(1 for c in rows if c.healthy)}/{len(rows)} healthy)")

    if not args.dry:
        st["flagged"] = flagged
        st["last_run"] = now
        save_state(st)


# ---------------------------------------------------------------- selftest

def _selftest() -> None:
    C = health.Check
    now = dt.datetime(2026, 7, 24, tzinfo=dt.timezone.utc)
    ok = C("slow_loop", "Slow loop", now, "ok", "fine")
    bad = C("signal_panel", "Panel", None, "never", "never ran")
    due = C("schwab_token", "Schwab token", now, "due", "1.0 days left")

    # fires once
    to_alert, healed = diff([ok, bad], {})
    assert [c.key for c in to_alert] == ["signal_panel"], to_alert
    assert healed == []

    # ...and not again while still bad
    to_alert, healed = diff([ok, bad], {"signal_panel": {}})
    assert to_alert == [], "must not re-alert a still-broken condition"

    # healing clears the flag silently (no alert generated for the heal)
    fixed = C("signal_panel", "Panel", now, "ok", "ran")
    to_alert, healed = diff([fixed], {"signal_panel": {}})
    assert to_alert == [] and healed == ["signal_panel"], (to_alert, healed)

    # a healed-then-rebroken condition alerts again
    to_alert, _ = diff([bad], {})
    assert len(to_alert) == 1, "re-break must be audible"

    # a check that was never PERFORMED must not alert (dashboard skips the network
    # probe; that must never be mistaken for "the tuner has not run")
    unknown = C("adaptive_tune", "Adaptive tuner", None, "unknown", "not checked here")
    to_alert, healed = diff([unknown], {})
    assert to_alert == [], "unknown must not alert"
    assert healed == [], "unknown must not clear an existing flag either"
    to_alert, healed = diff([unknown], {"adaptive_tune": {}})
    assert to_alert == [] and healed == [], "unknown leaves a prior flag untouched"

    # message carries the remedy and no numbers from the book
    title, body = compose([due])
    assert "schwab_auth.py" in body, body
    assert "price feed" in body, "urgent context missing"
    assert "$" not in body, "alerts must never carry dollar figures"

    title, body = compose([bad, due])
    assert "2 items" in title, title
    assert body.count("•") == 2

    # "never ran" must point at scheduling, not at a runtime cause
    _, body = compose([bad])
    assert "crontab -l" in body, body
    assert "OpenD" not in body, "never-ran must not blame a runtime cause"
    stale_panel = C("signal_panel", "Panel", now, "stale", "last ran 20d ago")
    _, body = compose([stale_panel])
    assert "OpenD" in body, "a job that ran before and stopped IS a runtime cause"

    # single-item title names the job
    title, _ = compose([bad])
    assert title.endswith("Panel"), title

    print("health_check selftest: PASS")


if __name__ == "__main__":
    main()
