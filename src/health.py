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

2026-07-30 sharpened *which* artifact counts. The fast loop and risk review had
been dark for two sessions — an allowlist stopped their gated commands, so each
agent ran, wrote a log explaining it was blocked, and exited 0. Both were checked
by LOG mtime, and a blocked run still writes its log, so this file reported
"7/8 healthy" straight through the outage. The ERR trap in deploy/alert.sh was
equally blind, for the mirror-image reason: it fires on a non-zero exit, and an
agent politely reporting its own blockage exits 0.

The lesson: a log proves the job STARTED, not that it did its work. Prefer the
artifact the job exists to produce — for these two, `order_plan.json` and
`risk_review_facts.json`, both of which were visibly stale the whole time. That
artifact strictly dominates the log: if cron itself dies, the output stops too.

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

# Remind this many days before expiry — enough runway to hit a busy day and still
# have a couple of chances left.


# Sentinel for a probe we chose NOT to run (e.g. the dashboard skips the network
# call to GitHub so a page render never blocks). "We didn't look" and "it has
# never run" are different claims, and conflating them produces a confident false
# alarm — the dashboard would read NEVER RAN for a workflow that ran this morning.
SKIPPED = "__skipped__"

# Sentinel for a job that is deliberately stopped: the kill-switch is engaged, so
# the fast loop and risk review halt before producing their artifacts. That is the
# operator's intent, not a fault, and nagging daily about a switch someone chose to
# throw is exactly the cry-wolf noise this module is supposed to avoid.
HALTED = "__halted__"

# Kill-switch path, mirroring [governance].kill_switch_file in config/strategy.toml.
# Hardcoded rather than read from the config on purpose: this module imports nothing
# from src/ and must stay importable under system python3.10 (no tomllib), because
# the dashboard and the 08:00 cron check run on different interpreters. If the
# config value is ever changed from the default, change it here too.
KILL_SWITCH = "research_store/HALT"

# A job whose log advanced while its output did not has RUN AND FAILED — the exact
# 2026-07-30 signature. This is the sharpest signal in the module: on a healthy run
# both timestamps move together within seconds, so the gap needs no modelling of
# weekends, holidays or DST, and it fires after ONE bad run instead of waiting out
# a multi-day staleness window. 12h is far above the normal seconds-to-minutes gap
# (the log keeps appending through the post-Claude steps) and far below the ~24h
# that a single missed daily run produces.
BLOCKED_GAP = dt.timedelta(hours=12)


@dataclass(frozen=True)
class Check:
    key: str
    label: str
    last_seen: dt.datetime | None
    status: str      # "ok" | "stale" | "never" | "blocked" | "due" | "expired" | "unknown"
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
    # These two track their OUTPUT, not their log — see the 2026-07-30 note in the
    # module docstring. fast_loop.py rewrites order_plan.json on EVERY run by
    # design (it must never leave a stale plan for the placement agent), and
    # risk_review.py --facts rewrites risk_review_facts.json on every run, so an
    # unchanged mtime means the job did not get through its work.
    "fast_loop":     ("Fast loop (execution)",   4,  "research_store/rh/order_plan.json"),
    "risk_review":   ("Risk review",             4,  "research_store/rh/risk_review_facts.json"),
    # 4d, not 2d: this artifact only advances during RTH, so Friday's close ->
    # Monday's 08:00 check is ~2.7d of legitimate dead air (3.7d when Monday is
    # a market holiday). 2d alarmed every Monday. Matches the other weekday-only
    # jobs above. Cost: a monitor that dies mid-week is caught ~4d later.
    "monitor":       ("Intraday monitor",        4,  "research_store/monitor/state.json"),
    "ledger_backup": ("Ledger backup",           3,  "logs/backup.log"),
    "signal_panel":  ("moomoo signal panel",    10,  "signal_panel journal event"),
    "newsletter":    ("Investor letter",        10,  "research_store/newsletters/"),
    "adaptive_tune": ("Adaptive tuner (CI)",    10,  "GitHub Actions run"),
}


def evaluate(now: dt.datetime, probes: dict) -> list[Check]:
    """Pure: {key: timestamp|None} -> verdicts. No I/O."""
    out: list[Check] = []

    for key, (label, max_days, source) in SPECS.items():
        ts = probes.get(key)
        if ts == SKIPPED:
            out.append(Check(key, label, None, "unknown", "not checked here"))
            continue
        if ts == HALTED:
            out.append(Check(key, label, None, "unknown",
                             "kill-switch engaged — job intentionally stopped"))
            continue

        # A probe may be a bare timestamp, or (output_ts, log_ts) for the jobs
        # where we can tell "started" apart from "finished". Only the pair can
        # distinguish a job that never fired from one that fired and got stuck.
        log_ts = None
        if isinstance(ts, tuple):
            ts, log_ts = ts
        if log_ts is not None and (ts is None or log_ts - ts > BLOCKED_GAP):
            behind = f"output {_ago(now - ts)} old" if ts else f"no {source} at all"
            out.append(Check(key, label, ts, "blocked",
                             f"RAN {_ago(now - log_ts)} ago but did not finish — {behind}"))
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


def gather(root: pathlib.Path | None = None, *, use_network: bool = True) -> dict:
    """Collect the timestamps evaluate() judges. All failures degrade to None."""
    root = root or REPO
    # A thrown kill-switch stops both trading jobs before they write anything, so
    # their artifacts go stale by design. Report that, don't alarm on it.
    halted = HALTED if (root / KILL_SWITCH).exists() else None
    return {
        "slow_loop":     _mtime(root / "research_store" / "current.json"),
        # (output, log): the pair is what makes a blocked run visible.
        "fast_loop":     halted or (_mtime(root / "research_store" / "rh" / "order_plan.json"),
                                    _mtime(root / "logs" / "fast.log")),
        "risk_review":   halted or (_mtime(root / "research_store" / "rh" / "risk_review_facts.json"),
                                    _mtime(root / "logs" / "risk_review.log")),
        "monitor":       _mtime(root / "research_store" / "monitor" / "state.json"),
        "ledger_backup": _mtime(root / "logs" / "backup.log"),
        "signal_panel":  _newest_journal_event(root, "signal_panel"),
        "newsletter":    _newest_in_dir(root / "research_store" / "newsletters", "*.sent"),
        "adaptive_tune": _last_actions_run() if use_network else SKIPPED,
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

    # a deliberately halted job is "unknown" too: not healthy, but never alerting.
    # Throwing the kill-switch is an operator decision, not a fault to nag about.
    r = {c.key: c for c in evaluate(now, {"fast_loop": HALTED, "risk_review": HALTED})}
    for k in ("fast_loop", "risk_review"):
        assert r[k].status == "unknown", r[k]
        assert not r[k].alertable, "a thrown kill-switch must not alert daily"
        assert not r[k].healthy, "halted is not a clean bill of health"
        assert "kill-switch" in r[k].detail, r[k]

    # REGRESSION (2026-07-30): the fast loop and risk review ran, wrote their logs,
    # and halted on a permission prompt for two sessions while this file reported
    # 7/8 healthy. Both were keyed to LOG mtime, and a blocked run still logs. They
    # are now keyed to the artifact each job exists to produce, so a fresh log with
    # a stale output must read STALE. Pin the sources so a future edit cannot
    # quietly point them back at a log.
    assert SPECS["fast_loop"][2] == "research_store/rh/order_plan.json", SPECS["fast_loop"]
    assert SPECS["risk_review"][2] == "research_store/rh/risk_review_facts.json", SPECS["risk_review"]
    assert not any(s[2].startswith("logs/") for k, s in SPECS.items()
                   if k in ("fast_loop", "risk_review")), \
        "a log proves the job started, not that it did its work"
    outage = {"fast_loop": now - 3 * day, "risk_review": now - 3 * day}   # logs were fresh
    r = {c.key: c for c in evaluate(now, outage)}
    assert all(r[k].status == "ok" for k in outage), "3d is inside the 4d window"
    outage = {"fast_loop": now - 5 * day, "risk_review": now - 5 * day}
    r = {c.key: c for c in evaluate(now, outage)}
    for k in outage:
        assert r[k].status == "stale" and r[k].alertable, r[k]

    # ...but staleness alone was too slow: at the moment Aaron noticed by hand, the
    # outage was 2.4d old and a 4d window still read "ok". The PAIR is what catches
    # it after one bad run. Replay the real shape: log fresh, output two days back.
    blocked = {"fast_loop": (now - 2 * day, now - dt.timedelta(hours=2)),
               "risk_review": (now - 2 * day, now - dt.timedelta(hours=2))}
    r = {c.key: c for c in evaluate(now, blocked)}
    for k in blocked:
        assert r[k].status == "blocked", r[k]
        assert r[k].alertable and not r[k].healthy, r[k]
        assert "did not finish" in r[k].detail, r[k]
    # and it must fire while the staleness window still reads clean — that gap is
    # the entire point, so assert the old check would NOT have caught this.
    assert {c.key: c for c in evaluate(now, {k: v[0] for k, v in blocked.items()})
            }["fast_loop"].status == "ok", "2d output age alone stays inside 4d"

    # a healthy run writes both within seconds — that must never read as blocked
    fine = {"fast_loop": (now - dt.timedelta(hours=3),
                          now - dt.timedelta(hours=3) + dt.timedelta(minutes=4))}
    assert {c.key: c for c in evaluate(now, fine)}["fast_loop"].status == "ok"
    # nor may a long weekend: on a good Friday run both moved together, so the
    # gap stays ~0 no matter how many days pass before the next run.
    weekend = {"fast_loop": (now - 3 * day, now - 3 * day + dt.timedelta(minutes=2))}
    assert {c.key: c for c in evaluate(now, weekend)}["fast_loop"].status == "ok", \
        "both artifacts move together on a good run — weekends must stay quiet"
    # a job that has run but NEVER produced its output is blocked, not "never"
    r = {c.key: c for c in evaluate(now, {"fast_loop": (None, now - dt.timedelta(hours=1))})}
    assert r["fast_loop"].status == "blocked" and "no research_store" in r["fast_loop"].detail, r["fast_loop"]
    # a missing log degrades to plain staleness rather than crashing
    r = {c.key: c for c in evaluate(now, {"fast_loop": (now - 5 * day, None)})}
    assert r["fast_loop"].status == "stale", r["fast_loop"]

    # boundary: exactly at the limit is still ok, a hair past is stale
    mon_max = SPECS["monitor"][1]
    r = {c.key: c for c in evaluate(now, {"monitor": now - mon_max * day})}
    assert r["monitor"].status == "ok", "exactly at threshold must not alarm"
    r = {c.key: c for c in evaluate(now, {"monitor": now - mon_max * day - dt.timedelta(hours=1)})}
    assert r["monitor"].status == "stale"

    # The monitor's artifact only advances during RTH, so the ordinary
    # Friday-close -> Monday-08:00 gap is ~2.7d of dead air with nothing wrong.
    # A 2d window made that alarm EVERY Monday; on 2026-07-27 it fired a false
    # "stale" push and filed an auto-fix issue against a phantom bug.
    fri_close = dt.datetime(2026, 7, 24, 19, 59, tzinfo=dt.timezone.utc)   # 15:59 ET
    mon_check = dt.datetime(2026, 7, 27, 12, 0, tzinfo=dt.timezone.utc)    # 08:00 ET
    r = {c.key: c for c in evaluate(mon_check, {"monitor": fri_close})}
    assert r["monitor"].status == "ok", f"weekend gap must not alarm: {r['monitor']}"

    # a Monday market holiday stretches the same gap a further day (Fri close ->
    # Tue 08:00 = 3.7d); still nothing wrong, still must be quiet.
    r = {c.key: c for c in evaluate(mon_check + day, {"monitor": fri_close})}
    assert r["monitor"].status == "ok", f"long-weekend gap must not alarm: {r['monitor']}"

    # ...but a monitor that genuinely stopped must still be caught.
    r = {c.key: c for c in evaluate(mon_check, {"monitor": fri_close - 3 * day})}
    assert r["monitor"].status == "stale", "a truly dead monitor must still alarm"

    # no credential-expiry check remains: the Schwab token was the only one, and
    # moomoo authenticates through OpenD (whose liveness IS the signal_panel /
    # monitor artifact checks above). Guard against it creeping back silently.
    assert not any(c.key == "schwab_token" for c in evaluate(now, {})), \
        "schwab_token check should be gone with the adapter"

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
