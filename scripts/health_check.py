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
    python scripts/health_check.py --open-issue  # also file/comment a deduped
                                               # GitHub issue for newly-alerting
                                               # conditions (via the box's
                                               # already-authenticated `gh`;
                                               # never breaks the push if `gh`
                                               # fails). Combine with --dry to
                                               # preview without filing.
    python scripts/health_check.py --selftest # logic tests, no I/O
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import health                                    # noqa: E402
from notify import push, ops_topic               # noqa: E402

STATE = REPO / "research_store" / "health_state.json"

# The issue title is the dedupe key: an open issue with this exact title gets
# a comment instead of a new issue, so a still-broken condition doesn't spam a
# fresh issue every day. Keep it stable.
ISSUE_TITLE = "\U0001F534 Scheduled job unhealthy"  # "🔴 Scheduled job unhealthy"

# Created idempotently before filing — a fresh repo (or a fresh mirror) may not
# have these labels yet, and `gh issue create --label` needs them to exist.
ISSUE_LABELS = [
    ("bug", "d73a4a", "Something isn't working"),
    ("auto-fix", "0e8a16",
     "Filed by the automated oversight loop for an agent to propose a fix"),
]

# Printed verbatim in every filed issue. The issue body is PUBLIC (this repo's
# issue tracker), so this line is what tells a reader — human or agent — that
# the fix belongs in ops/plumbing (scripts/, deploy/, config), never in a live
# trading decision. Pair with compose()'s "never positions/prices/P&L" rule:
# the same "ops, not the book" boundary applies to both channels.
OPS_VS_CODE_NOTE = (
    "This is an ops/scheduling alert (a job stopped leaving evidence it ran) — "
    "not a trading decision. A fix belongs in the code/config that runs the "
    "job (scripts/, deploy/, config/), never in a live position, order, or "
    "the book itself."
)

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
    """Atomic write: a kill mid-write must never leave a truncated JSON file,
    because `load_state`'s `except ValueError` treats corrupt JSON as "no
    state" and silently resets EVERY flag, re-alerting everything at once."""
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name(f"{STATE.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(st, indent=2, sort_keys=True))
    os.replace(tmp, STATE)


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


def issue_body(to_alert) -> str:
    """Build the GitHub issue body. Pure string building — no `gh` call here,
    so this is fully testable without invoking the CLI.

    PUBLIC-SAFETY CONTRACT: this is filed verbatim into a public GitHub issue.
    Same rule as compose(): job names, statuses, ages, remedy strings — and
    NOTHING else. No dollar figures, no positions, no P&L, no secrets, no file
    contents beyond what health.Check already exposes.
    """
    lines = ["Daily health check found the following unhealthy scheduled job(s):", ""]
    for c in to_alert:
        lines.append(f"- **{c.label}** (`{c.key}`) — status: `{c.status}` — {c.detail}")
        remedy = NEVER_REMEDY if c.status == "never" else REMEDY.get(c.key)
        if remedy:
            lines.append(f"  - Remedy: {remedy}")
    lines.append("")
    lines.append(OPS_VS_CODE_NOTE)
    return "\n".join(lines)


def _gh(args: list[str]) -> tuple[bool, str, str]:
    """Run a `gh` subcommand under REPO with a timeout. NEVER raises: any
    failure (gh missing, not authenticated, network down, rate limited,
    timeout) degrades to (False, "", diagnostic) so the issue-filing bonus can
    never take down the check or the phone push, which is the primary channel.

    encoding="utf-8", errors="replace": the box's cron locale may not be UTF-8
    (PEP 538 coercion can be off), and issue bodies/titles carry emoji. Without
    this, decoding `gh issue list`'s JSON can raise, dedupe silently returns
    None, and every run files a FRESH duplicate issue instead of commenting on
    the existing one. stdin=DEVNULL: an interactive `gh` prompt (e.g. first-run
    config) must never block waiting on input for the full timeout.
    """
    try:
        out = subprocess.run(["gh"] + args, capture_output=True, text=True,
                              timeout=30, cwd=REPO, encoding="utf-8",
                              errors="replace", stdin=subprocess.DEVNULL)
        return out.returncode == 0, out.stdout, out.stderr
    except Exception as e:  # gh not installed, timeout, etc.
        return False, "", f"{type(e).__name__}: {e}"


def _ensure_labels() -> None:
    """Create `bug` / `auto-fix` idempotently. Best-effort: a label that
    already exists returns a 422 from the API, which we swallow; any other
    failure just prints a diagnostic (the labels may already exist from a
    prior run, or `gh issue create --label` may fail below — either way we
    continue rather than blocking the alert).

    `gh api` writes the JSON error body (which contains `already_exists`) to
    STDOUT, not stderr — stderr only carries the generic "Validation Failed
    (HTTP 422)" line. Checking `err` alone means this prints a spurious
    "could not ensure label" diagnostic on every single run in steady state,
    once the labels already exist.
    """
    for name, color, desc in ISSUE_LABELS:
        ok, out, err = _gh(["api", "repos/{owner}/{repo}/labels", "-X", "POST",
                             "-f", f"name={name}", "-f", f"color={color}",
                             "-f", f"description={desc}"])
        already_exists = "already_exists" in out or "already exists" in out \
            or "already_exists" in err or "already exists" in err
        if not ok and not already_exists:
            print(f"gh: could not ensure label {name!r} (continuing): "
                  f"{(out + err).strip()[:200]}")


def _match_issue(items: list[dict], title: str) -> int | None:
    """Pure: given a parsed `gh issue list --json number,title` result and a
    target title, return the matching OPEN issue's number, or None. Exact
    match only — a near-miss title must NOT match, since dedupe existing to
    avoid a different issue silently soaking up our comments."""
    for item in items:
        if item.get("title") == title:
            return item.get("number")
    return None


def _find_open_issue(title: str) -> int | None:
    """Number of an OPEN issue with this exact title, or None. Lists rather
    than server-side searches on title so we don't depend on GitHub search
    query syntax behaving a particular way across `gh` versions.

    Filtered to the `auto-fix` label (present on this box's gh v2.4.0, and on
    any modern `gh`): cheap, shrinks the `--limit 100` pagination surface, and
    rules out an unrelated same-titled issue colliding with our dedupe."""
    ok, out, err = _gh(["issue", "list", "--state", "open", "--label", "auto-fix",
                         "--json", "number,title", "--limit", "100"])
    if not ok:
        print(f"gh: could not list issues for dedupe (continuing): {err.strip()[:200]}")
        return None
    try:
        items = json.loads(out or "[]")
    except ValueError:
        return None
    return _match_issue(items, title)


def file_issue(to_alert, *, dry: bool) -> bool:
    """File-or-comment the deduped issue for newly-alerting (or previously
    filing-failed) conditions. Returns True iff the issue was actually created
    or commented — callers persist that as `"filed": true` so a failed run
    gets retried later WITHOUT re-pushing (the phone push already fired at
    fire-once time and must never repeat).

    Wrapped so any `gh` failure prints a diagnostic and returns — filing is a
    bonus channel, never allowed to break the check or the phone push (the
    primary channel), which have already run by the time this is called.
    """
    if not to_alert:
        return False
    body = issue_body(to_alert)
    if dry:
        print(f"--- would file/comment issue (dry) ---\n{ISSUE_TITLE}\n{body}\n"
              f"{'-' * 30}")
        return False
    try:
        number = _find_open_issue(ISSUE_TITLE)
        if number is not None:
            ok, _, err = _gh(["issue", "comment", str(number), "--body", body])
            if ok:
                print(f"gh: commented on existing issue #{number}")
                return True
            print(f"gh: could not comment on issue #{number} (continuing): "
                  f"{err.strip()[:200]}")
            return False
        # Labels are only needed on the create path (`gh issue create --label`
        # requires them to exist); skip the two extra API calls on the far
        # more common comment path above.
        _ensure_labels()
        ok, out, err = _gh(["issue", "create", "--title", ISSUE_TITLE,
                             "--body", body, "--label", "bug",
                             "--label", "auto-fix"])
        if ok:
            print(f"gh: filed issue {out.strip()}")
            return True
        print(f"gh: could not create issue (continuing): {err.strip()[:200]}")
        return False
    except Exception as e:  # belt-and-braces: filing must never break the run
        print(f"gh: issue filing crashed unexpectedly (continuing): {e}")
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="print, don't push")
    ap.add_argument("--offline", action="store_true", help="skip the GitHub Actions probe")
    ap.add_argument("--open-issue", action="store_true",
                     help="also file/comment a deduped GitHub issue for newly-alerting "
                          "conditions (via `gh`; never breaks the push on failure)")
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
    new_keys = set()
    for c in to_alert:
        flagged[c.key] = {"at": now, "status": c.status, "detail": c.detail,
                           "filed": False}
        new_keys.add(c.key)

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

    # Persist BEFORE filing (the bonus channel), not after: filing can burn
    # ~120s of subprocess time (up to 4 `gh` calls x 30s timeout), and this box
    # is memory-tight and swaps. If the process dies mid-filing, the fire-once
    # flag must already be on disk — otherwise the next run both re-pushes AND
    # re-files the same condition. The phone push above is untouched by this
    # reordering: it still fires first and is never blocked by filing.
    if not args.dry:
        st["flagged"] = flagged
        st["last_run"] = now
        save_state(st)

    if args.open_issue:
        # File newly-alerting conditions, plus any previously-flagged
        # condition whose filing failed last time out (retried here WITHOUT
        # re-pushing — the phone push already fired for it and fire-once must
        # not repeat it). Conditions from an older state file with no "filed"
        # key are assumed already filed (no retroactive retry storm).
        rows_by_key = {c.key: c for c in rows}
        retry = [rows_by_key[k] for k, e in flagged.items()
                 if k not in new_keys and not e.get("filed", True) and k in rows_by_key]
        to_file = to_alert + retry
        if to_file:
            filed_ok = file_issue(to_file, dry=args.dry)
            if not args.dry and filed_ok:
                for c in to_file:
                    flagged[c.key]["filed"] = True
                st["flagged"] = flagged
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

    # --- issue body composition (pure — no gh invoked) ---
    body = issue_body([bad, due])
    assert "Panel" in body and "signal_panel" in body, "must name the bad condition"
    assert "Schwab token" in body and "schwab_token" in body, "must name the due condition"
    assert "crontab -l" in body, "must carry the never-ran remedy"
    assert "schwab_auth.py" in body, "must carry the schwab remedy"
    assert OPS_VS_CODE_NOTE in body, "must carry the ops-vs-code note verbatim"
    assert "$" not in body, "issue body (PUBLIC) must never carry a dollar figure"

    empty_body = issue_body([])
    assert "$" not in empty_body

    # --- _match_issue (pure dedupe-lookup logic, no subprocess) ---
    items = [{"number": 7, "title": ISSUE_TITLE}, {"number": 9, "title": "unrelated"}]
    assert _match_issue(items, ISSUE_TITLE) == 7, "exact match must be found"
    assert _match_issue(items, "no such title") is None, "no match must return None"
    assert _match_issue([], ISSUE_TITLE) is None, "empty list must return None"
    near_miss = [{"number": 3, "title": ISSUE_TITLE + " "}]  # trailing-space drift
    assert _match_issue(near_miss, ISSUE_TITLE) is None, \
        "a near-miss title must NOT match (avoids soaking up a different issue)"

    print("health_check selftest: PASS")


if __name__ == "__main__":
    main()
