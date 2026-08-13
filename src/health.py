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
                     # | "unprotected" | "empty_snapshot" (unprotected_positions only)
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
    # ⚠️ THE PRIMARY TRADING EVENT, and it was the LAST thing here to be watched
    # — added 2026-08-13, the day the review that JUDGES it got a check while the
    # session itself had none. Live since 2026-08-12: open 10:35, close 15:15 ET.
    #
    # The artifact is the LOG, not the journal, and that is deliberate. This
    # answers only "did cron fire the session", which is the gap nothing else
    # covers. A session that fires and DIES is already caught by a different
    # mechanism: session.py exits non-zero on a failed session and
    # run_session.sh's ERR trap pages the phone immediately — so failure is
    # loud, and only silence was invisible. Watching agent_decision events
    # instead would conflate the two and would alarm on a session that
    # legitimately recorded nothing.
    # 4d for the weekday-only reason above: Friday 15:15 -> Monday 08:00 is
    # ~2.7d of legitimate dead air, 3.7d across a holiday Monday.
    "session":       ("Agent session (open/close)", 4, "logs/session.log"),
    # 4d for the same weekday-only reason as monitor/fast_loop above: the last
    # review of the week lands ~15:30 ET Friday, so Monday 08:00 is ~2.7d of
    # legitimate dead air (3.7d when Monday is a holiday).
    #
    # ⚠️ THIS IS THE CHECK THAT WAS MISSING ON 2026-08-13, when the reviewer was
    # found never to have run under cron -- `codex` was off cron's PATH, the
    # review died on every scheduled run, and nothing noticed because
    # run_session.sh runs it behind `|| true` so it can never fail a trading run.
    # The event is the right artifact precisely BECAUSE a failed review now
    # journals nothing at all: a reviewer that cannot launch goes silent here,
    # which is the whole point. Do not switch this to the reviews/ directory
    # mtime -- that moves even when the run failed.
    "review":        ("Independent review",      4,  "codex_review journal event"),
    "newsletter":    ("Investor letter",        10,  "research_store/newsletters/"),
    "adaptive_tune": ("Adaptive tuner (CI)",    10,  "GitHub Actions run"),
    # Design spec 2026-08-09 §8 invariant: "every position has an agent-set
    # stop, or it is loudly flagged unprotected — checked every monitor cycle
    # and in daily health." scripts/market_monitor.py writes this artifact
    # every check_once() tick (not just on a change); the 4d window matches
    # "monitor" above for the same reason — it only advances during RTH, so a
    # normal Friday-close -> Monday-08:00 gap must not read stale.
    "unprotected_positions": ("Unprotected positions", 4,
                              "research_store/monitor/unprotected.json"),
}


def unrecorded_fills(journal: list, day: str) -> list:
    """Symbols the agent DECIDED to trade on `day` with no execution recorded.

    PURE. -> list of symbols, empty when the record is complete.

    ⚠️ THE FAILURE THIS CATCHES. A session records WHY it acted
    (`agent_decision`) and, separately, WHAT EXECUTED (`execution`). Those are
    different events and only the second reaches the weekly letter, the ledger
    reconciler and realized P&L. On 2026-08-12 two real sells filled at the
    broker and neither was journalled: the legacy loop records fills with a
    shell script and a session has no shell, so the letter would have reported a
    week in which the agent did nothing.

    The charter now requires `record_fills()`. That is a PROMPT instruction, and
    this session has proved repeatedly that prompt instructions get skipped — so
    the gap is also checked here, where forgetting is caught rather than assumed
    away.

    Only trading actions count: `hold`, `skip` and portfolio-level findings
    execute nothing and must never be reported as a missing fill.
    """
    traded = {"exit", "trim", "sell", "buy", "add", "open", "increase", "reduce"}
    want, got = set(), set()
    for e in journal:
        if str(e.get("ts", ""))[:10] != day:
            continue
        if e.get("event") == "agent_decision":
            sym = e.get("symbol")
            if sym and sym != "PORTFOLIO" and str(e.get("action", "")).lower() in traded:
                want.add(str(sym).upper())
        elif e.get("event") == "execution":
            for f in (e.get("fills") or []):
                if f.get("symbol"):
                    got.add(str(f["symbol"]).upper())
    return sorted(want - got)


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

        # unprotected_positions carries a different probe SHAPE (content, not
        # just a "did it run" timestamp) because the thing being judged is a
        # live safety condition, not job liveness — see _eval_unprotected.
        if key == "unprotected_positions":
            out.append(_eval_unprotected(key, label, ts, now, max_days, source))
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

    # Deployed-code drift. Not a scheduled job -- a different question about the
    # same box: is the process running what is on disk? Absent probe -> nothing
    # appended rather than a fabricated pass.
    # Decided to trade, nothing executed. A CONTENT check, not liveness — it has
    # no SPECS entry because "stale after N days" means nothing here; the journal
    # is always present, and the question is what is IN it.
    #
    # ⛔ COUNT ONLY, NEVER SYMBOL NAMES. Check.detail is forwarded verbatim to the
    # shared ops ntfy topic and to a PUBLIC GitHub issue (--open-issue), under the
    # "job names/ages only, never positions" contract that _eval_unprotected
    # documents. The symbols are in the journal for whoever opens it.
    fills = probes.get("unrecorded_fills")
    if fills is not None:
        fday, missing = fills
        if missing:
            n = len(missing)
            out.append(Check("unrecorded_fills", "Unrecorded fills", None, "unrecorded",
                             f"{n} decided trade{'s' if n != 1 else ''} on {fday} "
                             f"reached no `execution` event — the letter, the ledger "
                             f"reconciler and realized P&L will all miss them; check "
                             f"research_store/journal.jsonl"))
        else:
            out.append(Check("unrecorded_fills", "Unrecorded fills", None, "ok",
                             f"every decided trade on {fday} has an execution recorded"))

    svcs = probes.get("deployed_code")
    if svcs:
        try:
            import deployed                   # noqa: PLC0415
            for d in deployed.evaluate(now, svcs):
                out.append(Check(d["key"], d["label"], d["last_seen"],
                                 d["status"], d["detail"]))
        except Exception as e:                # noqa: BLE001
            out.append(Check("deployed_code", "Deployed-code drift", None,
                             "unknown", f"drift check failed: {e}"))

    return out


def _eval_unprotected(key: str, label: str, probe, now: dt.datetime,
                       max_days: int, source: str) -> Check:
    """Judge the unprotected_positions probe: None (never written), or
    (ts, unprotected_tuple, suspect_empty_snapshot_bool) from gather(). Pure.

    Staleness still applies first — an artifact that stopped updating tells
    you nothing about CURRENT protection, so it must not silently read "ok".
    Below that, the artifact's own content decides: any currently-unprotected
    symbol is the loud, distinct finding the spec's §8 invariant demands;
    a well-formed-but-empty snapshot against a book that expects holdings is
    the softer "check the snapshot" finding; otherwise it's a clean "ok".

    Deliberately COUNT-ONLY, never symbol names: health_check.py forwards
    Check.detail verbatim into the shared ops ntfy topic AND a PUBLIC GitHub
    issue (--open-issue) under a stated "job names/ages only, never positions"
    contract (scripts/health_check.py docstring + issue_body()'s PUBLIC-SAFETY
    CONTRACT). The symbol-naming alert lives in scripts/market_monitor.py's
    own notify() push instead, on the private book-carrying topic where every
    other stop/target alert already names names.
    """
    if probe is None:
        return Check(key, label, None, "never", f"no {source} has ever appeared")
    ts, unprotected, suspect_empty = probe
    age = now - ts
    age_d = age.total_seconds() / 86400
    if age_d > max_days:
        return Check(key, label, ts, "stale",
                     f"last checked {_ago(age)} ago (expected within {max_days}d)")
    if unprotected:
        n = len(unprotected)
        return Check(key, label, ts, "unprotected",
                     f"{n} position{'s' if n != 1 else ''} held with no stop being "
                     "watched — see the monitor's phone alert or "
                     "research_store/monitor/unprotected.json for which")
    if suspect_empty:
        return Check(key, label, ts, "empty_snapshot",
                     "snapshot reports zero positions while the book expects "
                     "holdings — check the snapshot, not the stops")
    return Check(key, label, ts, "ok", f"last checked {_ago(age)} ago, all positions protected")


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
            # `ts` LAST, and it is not optional: the journal has three time-field
            # conventions (outcome uses `at`, product uses `as_of`, and every
            # session-era event -- agent_decision, codex_review, risk_review --
            # uses `ts`). Reading only the first two silently returns None for a
            # whole class of events, which reads as "this job has never run"
            # rather than as a probe that cannot see it. Order matters only for
            # `execution`, which carries both; `as_of` stays authoritative there.
            raw = d.get("at") or d.get("as_of") or d.get("ts")
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
    """Newest SUCCESSFUL run of a workflow, via the already-authenticated `gh` CLI.

    Uses gh rather than a fresh PAT precisely so this adds no new credential:
    the box is already logged in for ordinary repo work. Any failure (gh missing,
    logged out, network) returns None -> reported as "never", which is honest:
    we genuinely do not know that it ran.

    ⚠️ `conclusion == "success"` is the whole point, and it was missing until
    2026-08-09. This probe read only `createdAt` off `--limit 1`, so ANY run
    counted as liveness — including a startup failure, which GitHub creates on
    every push to the default branch when the workflow file will not parse.
    That inverts the monitor: a workflow broken by bad YAML (adaptive-tune.yml,
    2026-08-04) generates a fresh failed run per push and thereby reports itself
    healthy — "last ran 2m ago" for a job structurally incapable of running.
    The failure was feeding its own alarm. Freshness of an ATTEMPT is not
    evidence of work; only a successful conclusion is.

    `--limit 20` because the startup-failure runs pile up in front of the last
    real one; the newest success can be well down the list.
    """
    try:
        out = subprocess.run(
            ["gh", "run", "list", "--workflow", f"{workflow}.yml",
             "--limit", "20", "--json", "createdAt,conclusion"],
            capture_output=True, text=True, timeout=30, cwd=REPO)
        if out.returncode != 0:
            return None
        runs = json.loads(out.stdout or "[]")
        ok = [r for r in runs if r.get("conclusion") == "success"]
        if not ok:
            return None
        newest = max(r["createdAt"] for r in ok)
        return dt.datetime.fromisoformat(newest.replace("Z", "+00:00"))
    except Exception:
        return None


def _unrecorded_fills_probe(root: pathlib.Path, today: str | None = None):
    """-> (day, [symbols]) for the most recent SETTLED trading day, or None.

    ⚠️ NEVER TODAY. A session records its decision when it decides and its fills
    a few tool-calls later, so a check run between the two reports a gap that is
    about to close on its own. Only a day that is fully over can be judged.
    (health_check.py runs 08:00 ET, before the 10:35 session, so in practice this
    is always yesterday or earlier — but the guard is here, not in the schedule.)

    Returns None when no settled day has any decision on it: a system that has
    not traded yet has nothing missing, and must not read as a pass OR a fail.
    """
    today = today or dt.datetime.now(dt.timezone.utc).date().isoformat()
    jp = root / "research_store" / "journal.jsonl"
    rows, days = [], set()
    try:
        for line in jp.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            rows.append(d)
            if d.get("event") == "agent_decision":
                day = str(d.get("ts", ""))[:10]
                if day and day < today:
                    days.add(day)
    except OSError:
        return None
    if not days:
        return None
    newest = max(days)
    return (newest, unrecorded_fills(rows, newest))


def _unprotected_probe(root: pathlib.Path):
    """Read market_monitor's unprotected.json -> (ts, unprotected, suspect) or
    None. Thin I/O only — all judgement lives in _eval_unprotected."""
    p = root / "research_store" / "monitor" / "unprotected.json"
    ts = _mtime(p)
    if ts is None:
        return None
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    return (ts, tuple(d.get("unprotected") or ()), bool(d.get("suspect_empty_snapshot")))


def _deployed_probe(root: pathlib.Path) -> list | None:
    """Service records for deployed.evaluate(). None if it could not be read."""
    try:
        import deployed                       # noqa: PLC0415
        units = [w["unit"] for w in deployed.WATCHED]
        return deployed.gather(root, deployed.systemd_start_times(units))
    except Exception:                         # noqa: BLE001
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
        "session":       _mtime(root / "logs" / "session.log"),
        "review":        _newest_journal_event(root, "codex_review"),
        "newsletter":    _newest_in_dir(root / "research_store" / "newsletters", "*.sent"),
        "adaptive_tune": _last_actions_run() if use_network else SKIPPED,
        "unprotected_positions": _unprotected_probe(root),
        "unrecorded_fills": _unrecorded_fills_probe(root),
        # Is each long-running service running the code on disk? Scheduling
        # liveness (everything above) cannot see this: a stale process keeps
        # writing fresh artifacts, so every other check reads green while the
        # process enforces last week's logic. See src/deployed.py.
        "deployed_code": _deployed_probe(root),
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

    # ---- unrecorded fills: decided to trade, no execution on the record ----
    j = [
        {"event": "agent_decision", "ts": "2026-08-12T14:38:00Z", "symbol": "AMAT", "action": "exit"},
        {"event": "agent_decision", "ts": "2026-08-12T14:39:00Z", "symbol": "TER", "action": "trim"},
        {"event": "agent_decision", "ts": "2026-08-12T14:41:00Z", "symbol": "AMD", "action": "hold"},
        {"event": "agent_decision", "ts": "2026-08-12T14:41:00Z", "symbol": "PORTFOLIO", "action": "derisk"},
        {"event": "execution", "ts": "2026-08-12T14:40:00Z", "fills": [{"symbol": "AMAT"}]},
    ]
    miss = unrecorded_fills(j, "2026-08-12")
    assert miss == ["TER"], miss          # AMAT recorded; TER traded and was not
    # a HOLD executes nothing and must never be reported as a missing fill
    assert "AMD" not in miss and "PORTFOLIO" not in miss, miss
    # a complete record is silent
    j2 = j + [{"event": "execution", "ts": "2026-08-12T14:40:00Z",
               "fills": [{"symbol": "TER"}]}]
    assert unrecorded_fills(j2, "2026-08-12") == []
    # other days are not this day's problem
    assert unrecorded_fills(j, "2026-08-11") == []
    assert unrecorded_fills([], "2026-08-12") == []

    # ---- unrecorded fills, now actually WIRED --------------------------------
    # It was pure, selftested and called by nothing until 2026-08-13: a control
    # that is present and does nothing, which is this repo's recurring defect.
    r = {c.key: c for c in evaluate(now, {"unrecorded_fills": ("2026-08-12", ["TER"])})}
    assert r["unrecorded_fills"].status == "unrecorded", r["unrecorded_fills"]
    assert "1 decided trade " in r["unrecorded_fills"].detail, r["unrecorded_fills"].detail
    # ⛔ COUNT ONLY. detail goes verbatim to a PUBLIC GitHub issue and the shared
    # ops topic -- the same contract _eval_unprotected keeps.
    assert "TER" not in r["unrecorded_fills"].detail, r["unrecorded_fills"].detail
    r = {c.key: c for c in evaluate(now, {"unrecorded_fills": ("2026-08-12", ["TER", "MU"])})}
    assert "2 decided trades " in r["unrecorded_fills"].detail, r["unrecorded_fills"].detail
    for sym in ("TER", "MU"):
        assert sym not in r["unrecorded_fills"].detail, r["unrecorded_fills"].detail
    # a complete record is a clean pass
    r = {c.key: c for c in evaluate(now, {"unrecorded_fills": ("2026-08-12", [])})}
    assert r["unrecorded_fills"].status == "ok", r["unrecorded_fills"]
    # absent probe -> nothing appended, never a fabricated pass
    assert "unrecorded_fills" not in {c.key for c in evaluate(now, {})}

    # the probe must never judge TODAY -- a decision and its fill are minutes
    # apart, so a same-day check reports a gap that is about to close itself
    import tempfile                                # noqa: PLC0415
    with tempfile.TemporaryDirectory() as _d:
        _root = pathlib.Path(_d)
        (_root / "research_store").mkdir()
        (_root / "research_store" / "journal.jsonl").write_text("\n".join(
            json.dumps(e) for e in [
                {"event": "agent_decision", "ts": "2026-08-12T14:38:00Z",
                 "symbol": "AMAT", "action": "exit"},
                {"event": "execution", "ts": "2026-08-12T14:40:00Z",
                 "fills": [{"symbol": "AMAT"}]},
                {"event": "agent_decision", "ts": "2026-08-13T14:38:00Z",
                 "symbol": "TER", "action": "trim"},      # today: fill not in yet
            ]) + "\n")
        got = _unrecorded_fills_probe(_root, today="2026-08-13")
        assert got == ("2026-08-12", []), got     # yesterday judged, today ignored
        # ...and a system that has never traded on a settled day says nothing
        (_root / "research_store" / "journal.jsonl").write_text(
            json.dumps({"event": "agent_decision", "ts": "2026-08-13T14:38:00Z",
                        "symbol": "TER", "action": "trim"}) + "\n")
        assert _unrecorded_fills_probe(_root, today="2026-08-13") is None

    # ---- the session is the PRIMARY trading event and must be watched -------
    r = {c.key: c for c in evaluate(now, {"session": now - day})}
    assert r["session"].status == "ok", r["session"]
    # a Friday-close session read on Monday morning is NOT stale
    r = {c.key: c for c in evaluate(now, {"session": now - 3 * day})}
    assert r["session"].status == "ok", r["session"]
    # cron stopped firing it -> stale. This is the gap nothing else covered: a
    # session that RUNS and fails already pages via run_session.sh's ERR trap,
    # so only silence was invisible.
    r = {c.key: c for c in evaluate(now, {"session": now - 5 * day})}
    assert r["session"].status == "stale", r["session"]
    r = {c.key: c for c in evaluate(now, {})}
    assert r["session"].status == "never", r["session"]

    # ---- the independent review is a scheduled job like any other -----------
    # 2026-08-13: it had never once run under cron and nothing noticed, because
    # run_session.sh runs it behind `|| true`. It is now watched.
    r = {c.key: c for c in evaluate(now, {"review": now - day})}
    assert r["review"].status == "ok", r["review"]
    r = {c.key: c for c in evaluate(now, {"review": now - 5 * day})}
    assert r["review"].status == "stale", r["review"]
    # a Friday-close review read on Monday morning is NOT stale
    r = {c.key: c for c in evaluate(now, {"review": now - 3 * day})}
    assert r["review"].status == "ok", r["review"]
    # and a reviewer that has never run at all is reported, not assumed absent
    r = {c.key: c for c in evaluate(now, {})}
    assert r["review"].status == "never", r["review"]

    # ⛔ THE PROBE MUST SEE `ts`. codex_review (like every session-era event)
    # carries only `ts`; _newest_journal_event read `at`/`as_of` alone, so
    # reusing it would have returned None forever and reported a HEALTHY
    # reviewer as one that had never run -- a liveness check that is itself dead.
    import tempfile as _tf, os as _os
    with _tf.TemporaryDirectory() as _d:
        _root = pathlib.Path(_d)
        (_root / "research_store").mkdir()
        (_root / "research_store" / "journal.jsonl").write_text(
            json.dumps({"event": "codex_review", "ts": "2026-08-13T19:24:13+00:00",
                        "stance": "SPLIT"}) + "\n"
            + json.dumps({"event": "signal_panel", "as_of": "2026-08-10T20:15:00+00:00"}) + "\n")
        got = _newest_journal_event(_root, "codex_review")
        assert got is not None, "the review probe cannot see a `ts`-stamped event"
        assert got.isoformat() == "2026-08-13T19:24:13+00:00", got
        # the existing `as_of` convention still resolves -- `ts` is a fallback,
        # not a replacement
        sp = _newest_journal_event(_root, "signal_panel")
        assert sp is not None and sp.isoformat() == "2026-08-10T20:15:00+00:00", sp

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

    # evaluate() must emit a row for EVERY spec key on every path, sentinels
    # included. health_check.diff() clears any flag whose key is absent from the
    # rows (that is how a retired check's flag gets dropped), so a branch that
    # silently skipped a key would clear its fire-once flag and re-alert a
    # condition that never healed. Cheap to assert, silent and confusing to hit.
    for _label, _probes in (("empty", {}),
                            ("skipped", {k: SKIPPED for k in SPECS}),
                            ("halted", {k: HALTED for k in SPECS}),
                            ("paired", {"fast_loop": (None, now)})):
        assert {c.key for c in evaluate(now, _probes)} == set(SPECS), _label

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

    # unprotected_positions: design spec 2026-08-09 §8 — "checked every
    # monitor cycle AND in daily health." Content-based, not a plain "did it
    # run" timestamp, so it gets its own branch in evaluate() (_eval_unprotected).
    fresh = now - dt.timedelta(hours=1)
    # never written -> "never" (same shape as any other job that's never run)
    r = {c.key: c for c in evaluate(now, {})}
    assert r["unprotected_positions"].status == "never", r["unprotected_positions"]
    assert r["unprotected_positions"].alertable
    # fresh, nothing unprotected, snapshot not suspect -> ok
    r = {c.key: c for c in evaluate(now, {"unprotected_positions": (fresh, (), False)})}
    assert r["unprotected_positions"].status == "ok", r["unprotected_positions"]
    assert r["unprotected_positions"].healthy
    # a genuinely unprotected holding -> loud, distinct status. COUNT only, no
    # symbol name: this Check.detail flows verbatim into scripts/health_check.py's
    # shared ops ntfy push AND a PUBLIC GitHub issue (--open-issue) under a
    # stated "job names/ages only, never positions" contract — the symbol-naming
    # alert belongs on market_monitor's own private notify() push instead.
    r = {c.key: c for c in evaluate(
        now, {"unprotected_positions": (fresh, ("TSLA",), False)})}
    c = r["unprotected_positions"]
    assert c.status == "unprotected" and "1 position" in c.detail, c
    assert "TSLA" not in c.detail, "symbol names must never reach the public issue path"
    assert c.alertable and not c.healthy, c
    # a well-formed empty snapshot the book didn't expect -> distinct softer
    # finding ("check the snapshot"), not conflated with the unprotected case
    r = {c.key: c for c in evaluate(
        now, {"unprotected_positions": (fresh, (), True)})}
    c = r["unprotected_positions"]
    assert c.status == "empty_snapshot" and "snapshot" in c.detail, c
    assert c.alertable and not c.healthy, c
    assert c.status != "unprotected", "empty-snapshot and unprotected are distinct findings"
    # stale artifact overrides content — an old "all clear" is not evidence of
    # CURRENT protection, so it must not read "ok" just because it once was
    old = now - dt.timedelta(days=10)
    r = {c.key: c for c in evaluate(now, {"unprotected_positions": (old, (), False)})}
    assert r["unprotected_positions"].status == "stale", r["unprotected_positions"]

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
