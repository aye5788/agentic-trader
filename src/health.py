"""Scheduled-job liveness — "did each moving part actually run?"

Exists because of a failure class that is invisible by construction: a cron job
that never runs cannot alert you. On 2026-07-24 the moomoo signal panel was found
to have never fired once — it had been built, documented as running weekly, and
added to `deploy/crontab.template`, but never appended to the live crontab. Its
own OpenD-gap phone alert could not help: that alert only fires *inside* a run.
Nothing was broken, so nothing complained. Silence looked exactly like health.

The fix is to stop inferring health from the absence of complaints and instead
demand positive evidence: every scheduled job leaves an artifact behind (a book,
a log line, a journal event, a git commit), so a job that stopped running shows
up as an artifact that stopped moving.

Split in two on purpose:
  evaluate()  pure — timestamps in, verdicts out. All the logic, fully selftested.
  gather()    thin I/O — reads mtimes, the journal, tokens.db, the Actions API.
Keeping the judgement pure is what lets `--selftest` cover the weekend/holiday
edges without waiting a week to see them.

    python src/health.py              # human-readable status table
    python src/health.py --selftest   # logic tests, no I/O

Thresholds are deliberately LOOSE (see SPECS). A precise "did it miss a scheduled
run" test has to model weekends, market holidays and DST; getting that subtly
wrong produces false alarms, and a monitor that cries wolf gets muted, which is
strictly worse than one that notices a day late. Late-but-trusted beats
prompt-but-noisy.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess
import sys
from dataclasses import dataclass

REPO = pathlib.Path(__file__).resolve().parents[1]

# Schwab refresh tokens live 7 days and cannot be renewed unattended.
SCHWAB_TTL_DAYS = 7
# Remind this many days before expiry — enough runway to hit a busy day and still
# have a couple of chances left.
SCHWAB_WARN_DAYS = 3


# Sentinel for a probe we chose NOT to run (e.g. the dashboard skips the network
# call to GitHub so a page render never blocks). "We didn't look" and "it has
# never run" are different claims, and conflating them produces a confident false
# alarm — the dashboard would read NEVER RAN for a workflow that ran this morning.
SKIPPED = "__skipped__"


@dataclass(frozen=True)
class Check:
    key: str
    label: str
    last_seen: dt.datetime | None
    status: str            # "ok" | "stale" | "never" | "due" | "expired" | "unknown"
    detail: str

    @property
    def healthy(self) -> bool:
        # "unknown" is not healthy, but it must never ALERT — you cannot act on a
        # check that was not performed. health_check.py filters on alertable.
        return self.status == "ok"

    @property
    def alertable(self) -> bool:
        return self.status not in ("ok", "unknown")


# key -> (human label, stale after N days, what proves it ran)
SPECS = {
    "slow_loop":     ("Slow loop (rebalance)",   3,  "research_store/current.json"),
    "fast_loop":     ("Fast loop (execution)",   4,  "logs/fast.log"),
    "risk_review":   ("Risk review",             4,  "logs/risk_review.log"),
    "monitor":       ("Intraday monitor",        2,  "research_store/monitor/state.json"),
    "ledger_backup": ("Ledger backup",           3,  "logs/backup.log"),
    "signal_panel":  ("moomoo signal panel",    10,  "signal_panel journal event"),
    "newsletter":    ("Investor letter",        10,  "research_store/newsletters/"),
    "adaptive_tune": ("Adaptive tuner (CI)",    10,  "GitHub Actions run"),
}


def evaluate(now: dt.datetime, probes: dict) -> list[Check]:
    """Pure: {key: timestamp|None} + schwab_issued -> verdicts. No I/O."""
    out: list[Check] = []

    for key, (label, max_days, source) in SPECS.items():
        ts = probes.get(key)
        if ts == SKIPPED:
            out.append(Check(key, label, None, "unknown", "not checked here"))
            continue
        if ts is None:
            out.append(Check(key, label, None, "never",
                             f"no {source} has ever appeared"))
            continue
        age = now - ts
        age_d = age.total_seconds() / 86400
        if age_d > max_days:
            out.append(Check(key, label, ts, "stale",
                             f"last ran {_ago(age)} ago (expected within {max_days}d)"))
        else:
            out.append(Check(key, label, ts, "ok", f"last ran {_ago(age)} ago"))

    # Schwab is the odd one out: judged on time REMAINING, not time since.
    issued = probes.get("schwab_issued")
    if issued is None:
        out.append(Check("schwab_token", "Schwab token", None, "never",
                         "could not read tokens.db"))
    else:
        expires = issued + dt.timedelta(days=SCHWAB_TTL_DAYS)
        left = (expires - now).total_seconds() / 86400
        if left <= 0:
            out.append(Check("schwab_token", "Schwab token", issued, "expired",
                             "EXPIRED — price feed is dead until you re-auth"))
        elif left <= SCHWAB_WARN_DAYS:
            out.append(Check("schwab_token", "Schwab token", issued, "due",
                             f"{left:.1f} days left — re-auth this week"))
        else:
            out.append(Check("schwab_token", "Schwab token", issued, "ok",
                             f"{left:.1f} days left"))
    return out


def _ago(delta: dt.timedelta) -> str:
    s = delta.total_seconds()
    if s < 3600:
        return f"{int(s // 60)}m"
    if s < 86400:
        return f"{int(s // 3600)}h"
    return f"{s / 86400:.1f}d"


# ---------------------------------------------------------------- I/O side

def _mtime(path: pathlib.Path) -> dt.datetime | None:
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
    except OSError:
        return None


def _newest_journal_event(root: pathlib.Path, event: str) -> dt.datetime | None:
    """Newest `at`/`as_of` of a given journal event kind. The journal is small
    (append-only, one line per decision) so a full scan is cheap and exact."""
    jp = root / "research_store" / "journal.jsonl"
    newest = None
    try:
        for line in jp.read_text().splitlines():
            line = line.strip()
            if not line or f'"{event}"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("event") != event:
                continue
            raw = d.get("at") or d.get("as_of")
            if not raw:
                continue
            try:
                ts = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            if newest is None or ts > newest:
                newest = ts
    except OSError:
        return None
    return newest


def _newest_in_dir(d: pathlib.Path, pattern: str = "*") -> dt.datetime | None:
    try:
        times = [_mtime(p) for p in d.glob(pattern)]
    except OSError:
        return None
    times = [t for t in times if t]
    return max(times) if times else None


def _last_actions_run(workflow: str = "adaptive-tune") -> dt.datetime | None:
    """Newest run of a workflow, via the already-authenticated `gh` CLI.

    Uses gh rather than a fresh PAT precisely so this adds no new credential:
    the box is already logged in for ordinary repo work. Any failure (gh missing,
    logged out, network) returns None -> reported as "never", which is honest:
    we genuinely do not know that it ran.
    """
    try:
        out = subprocess.run(
            ["gh", "run", "list", "--workflow", f"{workflow}.yml",
             "--limit", "1", "--json", "createdAt"],
            capture_output=True, text=True, timeout=30, cwd=REPO)
        if out.returncode != 0:
            return None
        runs = json.loads(out.stdout or "[]")
        if not runs:
            return None
        return dt.datetime.fromisoformat(runs[0]["createdAt"].replace("Z", "+00:00"))
    except Exception:
        return None


def _schwab_issued() -> dt.datetime | None:
    try:
        sys.path.insert(0, str(REPO / "scripts"))
        from schwab_status import _token_issued  # noqa: PLC0415
        ts = _token_issued()
    except Exception:
        return None
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)


def gather(root: pathlib.Path | None = None, *, use_network: bool = True) -> dict:
    """Collect the timestamps evaluate() judges. All failures degrade to None."""
    root = root or REPO
    return {
        "slow_loop":     _mtime(root / "research_store" / "current.json"),
        "fast_loop":     _mtime(root / "logs" / "fast.log"),
        "risk_review":   _mtime(root / "logs" / "risk_review.log"),
        "monitor":       _mtime(root / "research_store" / "monitor" / "state.json"),
        "ledger_backup": _mtime(root / "logs" / "backup.log"),
        "signal_panel":  _newest_journal_event(root, "signal_panel"),
        "newsletter":    _newest_in_dir(root / "research_store" / "newsletters", "*.sent"),
        "adaptive_tune": _last_actions_run() if use_network else SKIPPED,
        "schwab_issued": _schwab_issued(),
    }


def checks(now: dt.datetime | None = None, root: pathlib.Path | None = None,
           *, use_network: bool = True) -> list[Check]:
    now = now or dt.datetime.now(dt.timezone.utc)
    return evaluate(now, gather(root, use_network=use_network))


# ---------------------------------------------------------------- selftest

def _selftest() -> None:
    now = dt.datetime(2026, 7, 24, 12, 0, tzinfo=dt.timezone.utc)
    day = dt.timedelta(days=1)

    # fresh artifact -> ok
    r = {c.key: c for c in evaluate(now, {"slow_loop": now - day})}
    assert r["slow_loop"].status == "ok", r["slow_loop"]

    # past its window -> stale
    r = {c.key: c for c in evaluate(now, {"slow_loop": now - 5 * day})}
    assert r["slow_loop"].status == "stale", r["slow_loop"]

    # never seen at all -> "never" (the signal-panel case that motivated this)
    r = {c.key: c for c in evaluate(now, {})}
    assert r["signal_panel"].status == "never", r["signal_panel"]
    assert all(c.status in ("never",) for c in evaluate(now, {})), "empty probes"

    # a probe we chose not to run is "unknown", NOT "never" — and must not alert
    r = {c.key: c for c in evaluate(now, {"adaptive_tune": SKIPPED})}
    assert r["adaptive_tune"].status == "unknown", r["adaptive_tune"]
    assert not r["adaptive_tune"].alertable, "an unperformed check must never alert"
    assert not r["adaptive_tune"].healthy, "unknown is not a clean bill of health"
    assert r["signal_panel"].alertable, "a genuinely-never-run job must alert"

    # boundary: exactly at the limit is still ok, a hair past is stale
    r = {c.key: c for c in evaluate(now, {"monitor": now - 2 * day})}
    assert r["monitor"].status == "ok", "exactly at threshold must not alarm"
    r = {c.key: c for c in evaluate(now, {"monitor": now - 2 * day - dt.timedelta(hours=1)})}
    assert r["monitor"].status == "stale"

    # schwab: fresh / due / expired
    def schwab(issued_days_ago):
        return {c.key: c for c in evaluate(now, {"schwab_issued": now - issued_days_ago * day})}["schwab_token"]
    assert schwab(1).status == "ok", schwab(1)
    assert schwab(5).status == "due", schwab(5)          # 2 days left
    assert schwab(8).status == "expired", schwab(8)
    assert schwab(4).status == "due", "3 days left is the warn boundary"
    assert schwab(3.5).status == "ok", "3.5 days left is still fine"

    # unreadable token db -> never, not a crash
    assert {c.key: c for c in evaluate(now, {})}["schwab_token"].status == "never"

    # healthy property tracks status
    assert Check("k", "l", now, "ok", "").healthy
    assert not Check("k", "l", now, "stale", "").healthy

    # _ago formatting stays human
    assert _ago(dt.timedelta(minutes=30)) == "30m"
    assert _ago(dt.timedelta(hours=5)) == "5h"
    assert _ago(dt.timedelta(days=2, hours=12)) == "2.5d"

    print("health selftest: PASS")


def main() -> None:
    if "--selftest" in sys.argv:
        _selftest()
        return
    offline = "--offline" in sys.argv
    rows = checks(use_network=not offline)
    width = max(len(c.label) for c in rows)
    bad = 0
    for c in rows:
        mark = "OK  " if c.healthy else "-->"
        if not c.healthy:
            bad += 1
        print(f"{mark} {c.label:<{width}}  {c.detail}")
    print(f"\n{len(rows) - bad}/{len(rows)} healthy")


if __name__ == "__main__":
    main()
