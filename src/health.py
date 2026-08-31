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

import snapshot_freshness

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
# Hardcoded rather than read from the config on purpose: this module keeps its
# imports to the standard library plus snapshot_freshness, and must stay
# importable under system python3.10 (no tomllib). If the config value is ever
# changed from the default, change it here too.
#
# ⛔ WHY 3.10 STILL, and it is NOT the reason this comment used to give. It said
# "the dashboard and the 08:00 cron check run on different interpreters"; both
# now run under .venv 3.12, so that reason is stale. The constraint survives
# because scripts/market_monitor.py runs under system python3.10 (the moomoo SDK
# is installed only there) and this is exactly the module a stop watcher might
# reasonably need. The cost of keeping it is one line; the cost of finding out
# the hard way is a monitor that cannot import its own health model.
#
# It had already lapsed: a conditional spanning newlines inside an f-string
# (PEP 701, 3.12+) at the snapshot_identity branch made this file fail to compile
# under 3.10 for an unknown period, while this very comment promised it did not.
# Nothing detected that, because nothing on the box runs it under 3.10 today.
# If you add syntax here, compile it under BOTH interpreters, not just .venv.
KILL_SWITCH = "research_store/HALT"

# A job whose log advanced while its output did not has RUN AND FAILED — the exact
# 2026-07-30 signature. This is the sharpest signal in the module: on a healthy run
# both timestamps move together within seconds, so the gap needs no modelling of
# weekends, holidays or DST, and it fires after ONE bad run instead of waiting out
# a multi-day staleness window. 12h is far above the normal seconds-to-minutes gap
# (the log keeps appending through the post-Claude steps) and far below the ~24h
# that a single missed daily run produces.
BLOCKED_GAP = dt.timedelta(hours=12)


# STATUSES THAT MUST NEVER PAGE THE OPERATOR, and the difference between them.
#
#   "ok"          healthy.
#   "unknown"     the check COULD NOT BE PERFORMED. You cannot act on it, and it
#                 must NOT clear a prior flag — a probe that was skipped tells
#                 you nothing about whether the underlying condition healed.
#   "unverified"  the check WAS performed and the answer is KNOWN: the evidence
#                 it needs does not exist on the surface it must come from. Not
#                 actionable either, but unlike "unknown" it IS a settled state,
#                 so it DOES clear a prior flag (see KNOWN_NON_ALERTING).
#
# ⛔ Non-alerting is not the same as harmless, and it is never a way to quiet a
# condition somebody could act on. It is only for conditions where NO operator
# action exists. A finding with a remedy belongs in the push channel.
NON_ALERTING = ("ok", "unknown", "unverified")

# Non-alerting AND settled -> safe to clear a stale flag in health_state.json.
# "unknown" is deliberately absent: see scripts/health_check.py:diff().
KNOWN_NON_ALERTING = ("ok", "unverified")


@dataclass(frozen=True)
class Check:
    key: str
    label: str
    last_seen: dt.datetime | None
    status: str      # "ok" | "stale" | "never" | "blocked" | "due" | "expired" | "unknown"
                     # | "unverified" | "missing" | "unprotected" | "empty_snapshot"
    detail: str

    @property
    def healthy(self) -> bool:
        # Only "ok" is healthy. The two statuses below are NOT healthy — they
        # still render on the dashboard — but they must never ALERT.
        return self.status == "ok"

    @property
    def alertable(self) -> bool:
        return self.status not in NON_ALERTING


# Keys evaluate() can emit that are NOT scheduled-job specs, plus the prefixes
# for the ones carrying a per-unit or per-finding identifier.
DERIVED_KEYS = frozenset({
    "unprotected_positions", "unrecorded_fills", "snapshot_identity",
    "positions_snapshot", "unreadable_artifacts",
})
KEY_PREFIXES = ("deployed_", "repo_check:")


def is_known_key(key: str) -> bool:
    """Is `key` a check this system can still PRODUCE? -> bool.

    ⛔ THE DISTINCTION THIS EXISTS TO DRAW: "this check was deleted" and "this
    check could not answer today" arrive as the same thing — an absent row.
    health_check.diff() drops a flag whose check is absent, which is right for a
    retired check (`schwab_token`, 2026-07-29) and WRONG for a live one that
    merely went quiet: an unreadable journal or a failed systemd query would
    silently clear a standing finding and report it "healed".

    So absence is only treated as retirement when the key is not one this module
    can emit at all. A live check that goes silent keeps its flag.
    """
    return (key in SPECS or key in DERIVED_KEYS
            or any(key.startswith(pre) for pre in KEY_PREFIXES))


# key -> (human label, stale after N days, what proves it ran)
SPECS = {
    "slow_loop":     ("Slow loop (rebalance)",   3,  "research_store/current.json"),
    # These track their OUTPUT, not their log — see the 2026-07-30 note in the
    # module docstring.
    # (The risk-review overlay was watched here the same way until it was retired
    # 2026-08-13 and folded into the sessions — see deploy/crontab.template.)
    #
    # ⛔ `fast_loop` WAS WATCHED HERE AND IS GONE, retired 2026-08-14 for the same
    # reason as the overlay: the sessions do its work with judgment. It was the
    # last procedural order-placer, it ran at 10:00 and the open session reasons
    # at 10:35, so it moved first every day and the session spent its run undoing
    # it — it re-opened AMAT twice after a session had deliberately exited, the
    # second time into a post-earnings gap. Deleting the entry (rather than
    # leaving it to age out) is the point: a liveness check for a job that is
    # supposed to be dead pages about the absence of a thing we removed.
    # 4d below, not 2d: these artifacts only advance during RTH, so Friday's close
    # -> Monday's 08:00 check is ~2.7d of legitimate dead air (3.7d when Monday is
    # a market holiday). 2d alarmed every Monday. Cost: a monitor that dies
    # mid-week is caught ~4d later.
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
    # ⚠️ THE JOB THAT HAD NEVER RUN. Armed 2026-07-20 as a QUARTERLY screen
    # (first Sunday of Jan/Apr/Jul/Oct) and it fired exactly zero times before
    # the cadence was changed to weekly on 2026-08-20 — so the candidate pool
    # the agent picks from was frozen at its inception list for a month while
    # every check read green. It had no liveness check at all; that is why.
    #
    # 10d for a weekly job, matching signal_panel: a Friday 17:00 run -> the
    # following Monday 08:00 check is ~2.6d, and one missed week must alarm
    # without a holiday-shifted run crying wolf.
    #
    # The artifact is the PROPOSAL, not logs/universe.log: run() writes a
    # proposal on every real screen, whereas the log moves even when the script
    # exits early on the wrong weekday. Same reasoning as the 2026-07-30
    # output-not-log switch.
    "universe_refresh": ("Universe refresh (weekly)", 10,
                         "research_store/universe/proposals/"),
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

    ⚠️ ONE DECISION MAY NAME SEVERAL SYMBOLS. A session that exits four ETFs in
    one move journals `symbol: "IWM,XLK,XLE,XLV"` -- one record, four trades.
    Comparing that string against individual fill symbols never matches, so the
    check reported a fully-recorded liquidation as missing (fired 2026-08-18,
    issue #11). The symbol field is therefore SPLIT, which also means a
    partially-executed multi-symbol decision now reports the specific names that
    are missing instead of an opaque blob nobody can act on.

    ⛔ AND A MONITOR-TRIGGERED EXIT COUNTS TOO (added 2026-08-20). This keyed
    ONLY off `agent_decision`, so it was structurally blind to the path that
    places the most urgent orders in the system: a stop breach journals
    `exit_signal` and the fill is journalled separately by the exit executor.
    On 2026-08-20 an RTX stop fired, the market sell FILLED, and the executor was
    then denied permission to write its result — so nothing recorded the trade,
    and this check reported clean because RTX had never been an `agent_decision`.
    The one check whose job is "an order executed and nobody wrote it down" could
    not see the case where that actually happened.

    Only ARMED, un-halted signals count: in alert-only mode or under the kill
    switch the monitor deliberately places nothing, so expecting a fill there
    would cry wolf on the system working as designed.
    """
    traded = {"exit", "trim", "sell", "buy", "add", "open", "increase", "reduce"}
    want, got = set(), set()
    for e in journal:
        if str(e.get("ts", ""))[:10] != day:
            continue
        if e.get("event") == "exit_signal":
            if not e.get("armed") or e.get("halted"):
                continue          # nothing was meant to be placed
            # ⛔ AND ONLY IF THE EXECUTOR ACTUALLY LAUNCHED. The signal is
            # journalled BEFORE the launch ceiling is applied, so a
            # ceiling-blocked breach placed no order and must not read as a
            # missing fill; likewise a breach the executor found already flat.
            # `launched` is written by market_monitor; a signal predating that
            # field is treated as launched, which preserves the old behaviour
            # for history rather than silently forgiving it (reviewer,
            # 2026-08-20).
            if e.get("launched") is False or e.get("sold_nothing"):
                continue
            for t in (e.get("triggers") or []):
                sym = str(t.get("symbol") or "").strip().upper()
                if sym:
                    want.add(sym)
        elif e.get("event") == "agent_decision":
            sym = e.get("symbol")
            if sym and sym != "PORTFOLIO" and str(e.get("action", "")).lower() in traded:
                for one in str(sym).upper().split(","):
                    one = one.strip()
                    if one and one != "PORTFOLIO":
                        want.add(one)
        elif e.get("event") == "execution":
            for f in (e.get("fills") or []):
                if f.get("symbol"):
                    got.add(str(f["symbol"]).upper())
    # ⛔ THE DAY BOUNDARY IS NOT A WALL. A signal at 23:59:59Z and its fill at
    # 00:00:01Z land on different calendar days and produced a false positive.
    # Executions from the FOLLOWING day's first hours also count, because a fill
    # cannot precede the order it settles (reviewer, 2026-08-20).
    if want - got:
        try:
            nxt = (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()
        except ValueError:
            nxt = None
        if nxt:
            for e in journal:
                ts = str(e.get("ts", ""))
                if e.get("event") != "execution" or ts[:10] != nxt:
                    continue
                if ts[11:13].isdigit() and int(ts[11:13]) >= 6:
                    continue          # only the small hours can be a spillover
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

    # SNAPSHOT IDENTITY: A SETTLED CONDITION WITH NO OPERATOR REMEDY.
    # refresh_broker_snapshot() records `identity_verified: false` when the
    # broker payload carried no account number, so identity rests on the
    # operator-pinned account rather than the broker's own bytes. Surfacing that
    # was right — it was a fact nobody acted on (reviewer, 2026-08-20).
    #
    # ⚠️ WHAT CHANGED 2026-08-21, AND WHAT DID NOT. This was `alertable`, and the
    # remedy it printed — "re-publish from a broker read that includes the
    # account" — asks for a read that this broker surface does not offer. It was
    # checked live: get_portfolio names no account in any field, and
    # get_equity_positions rows carry only symbol/quantity/average_buy_price/
    # shares_*/type (single page, no `next`, so not a truncation artifact).
    # Republishing from a clean, fully-paginated live read left the flag false.
    #
    # ⛔ That is evidence about ONE observed response shape, not proof about
    # every future one. The writer still reads the field from three places and
    # this check still flips to "ok" the moment the broker supplies it — nothing
    # here assumes it never will.
    #
    # The alerting contract is fire-once, so this was never a daily buzz: it
    # alerted once and then sat flagged. The defect was the CHANNEL, not the
    # frequency — a condition with no available action was routed to the place
    # reserved for conditions that need one, carrying an instruction that cannot
    # be carried out. `unverified` keeps that state visible and truthful on the
    # dashboard while never paging, and unlike `unknown` it says what is
    # actually true: the check ran and the evidence does not exist.
    #
    # NOTHING IS RELAXED. _write_broker_snapshot's mismatch branch still REFUSES
    # the write outright whenever the broker DOES name an account and it is not
    # the pinned one.
    #
    # ⛔ Do NOT "fix" this by making identity_verified true — not from the
    # declared/pinned account, and not from get_accounts. get_accounts names the
    # account but cannot bind it to THIS holdings read: the account number is
    # what we SEND to get_equity_positions, never what the broker echoes back.
    # Deriving it would make the payload assert the very thing being verified
    # and turn the mismatch guard into a formality that always passes.
    # ⛔ PRESENT-BUT-UNREADABLE, which is NOT the same as absent. See
    # _unreadable_probe: every reader in this system catches a parse error and
    # substitutes the same empty default the absent case produces, so a corrupt
    # file silently becomes "there is nothing here" and protection reverts with
    # nothing reporting it. `unreadable` is deliberately NOT in NON_ALERTING:
    # this pages.
    _bad = probes.get("unreadable_artifacts")
    if _bad:
        _names = ", ".join(f"{rel.split('/')[-1]} ({label})" for rel, label in _bad)
        out.append(Check(
            "unreadable_artifacts", "Unreadable critical files", None, "unreadable",
            f"{len(_bad)} file(s) present but will not parse: {_names}. Readers "
            "fall back to an empty default, so levels/valuation may silently be "
            "standing on the loop's numbers instead of the agent's — repair or "
            "rewrite before relying on any figure derived from them."))
    elif _bad is not None:
        out.append(Check("unreadable_artifacts", "Unreadable critical files",
                         None, "ok",
                         f"all {len(CRITICAL_ARTIFACTS)} critical artifacts parse"))

    ident = probes.get("snapshot_identity")
    if ident is not None and ident.get("unreadable"):
        # The probe FAILED. Not a finding and not a heal — see the probe's
        # docstring: `unknown` never pages and never clears a standing flag.
        out.append(Check("snapshot_identity", "Snapshot account identity",
                         None, "unknown",
                         "could not read research_store/rh/positions.json — "
                         "identity not checked this run"))
    elif ident is not None and ident.get("present") is False:
        # ⛔ A LEGACY FILE MUST STILL EMIT A ROW. Emitting nothing made the key
        # absent from `rows`, and health_check.diff() drops a flag whose check
        # is absent (the retired-check rule) -- so a rolled-back snapshot format
        # would silently clear a standing finding (reviewer, 2026-08-21).
        # `unknown` says the true thing: this file cannot answer the question.
        out.append(Check("snapshot_identity", "Snapshot account identity",
                         None, "unknown",
                         "positions.json predates the identity field — this "
                         "snapshot cannot say which account it describes"))
    elif ident is not None and ident.get("present"):
        if ident.get("verified"):
            out.append(Check("snapshot_identity", "Snapshot account identity",
                             None, "ok",
                             f"account {ident.get('account') or '?'} confirmed "
                             f"by the broker payload"))
        else:
            # ⛔ HOISTED OUT OF THE f-STRING ON PURPOSE. A conditional spanning
            # newlines INSIDE {...} is PEP 701 syntax, accepted only on 3.12+.
            # Written that way, this module stopped compiling under system
            # python3.10 while its own docstring still promised it did — and
            # nothing caught it, because everything that runs it today is 3.12.
            _pinned = ("the operator-pinned account " + ident["account"]
                       if ident.get("account")
                       else "no account at all (none pinned)")
            out.append(Check("snapshot_identity", "Snapshot account identity",
                             None, "unverified",
                             f"identity rests on {_pinned}, not on "
                             f"the broker's own bytes — this surface returns no "
                             f"account number to confirm it against. No operator "
                             f"action exists; the mismatch guard still refuses "
                             f"any payload that names a different account"))

    snapshot = probes.get("positions_snapshot")
    if snapshot is not None:
        if snapshot.get("snapshot_ts") is None:
            # ⛔ NO SNAPSHOT IS NOT "UP TO DATE". snapshot_freshness.status()
            # only compares against the newest FILL, so with an empty journal a
            # missing positions.json fell through to `ok` and the dashboard
            # reported the book healthy while the stop watcher had nothing to
            # read (reviewer, 2026-08-21). Predates the identity work; fixed
            # here because it is the same false-green class.
            # ⛔ NOT status "never" — that word means "this scheduled job has
            # never run" and routes to the generic crontab remedy, which is the
            # wrong place entirely for a missing artifact (reviewer).
            out.append(Check("positions_snapshot", "Broker positions snapshot",
                             None, "missing",
                             "no readable positions.json — valuation, the "
                             "dashboard and the weekly letter have no book to "
                             "read. The stop watcher FAILS OPEN here (it keeps "
                             "watching every eligible thesis, so protection is "
                             "not dropped) but it cannot tell a real holding "
                             "from a phantom until a snapshot is published"))
        elif snapshot["stale"]:
            out.append(Check("positions_snapshot", "Broker positions snapshot",
                             snapshot["snapshot_ts"], "stale_after_fill",
                             "positions.json predates the newest execution event — "
                             "holdings, stop coverage, valuation, and the investor "
                             "letter may be wrong; reconcile from the broker"))
        else:
            out.append(Check("positions_snapshot", "Broker positions snapshot",
                             snapshot["snapshot_ts"], "ok",
                             "positions.json is at least as new as the newest execution"))

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
            # ⛔ A MONITOR-ONLY DAY MUST BE SELECTED TOO. This considered only
            # `agent_decision`, so a day whose only trade was a stop-triggered
            # exit was never chosen for checking -- the pure function had been
            # taught about exit_signal while the probe that invokes it still
            # could not reach that day (reviewer, 2026-08-20).
            if d.get("event") in ("agent_decision", "exit_signal"):
                day = str(d.get("ts", ""))[:10]
                if day and day < today:
                    days.add(day)
    except OSError:
        return None
    if not days:
        return None
    newest = max(days)
    return (newest, unrecorded_fills(rows, newest))


def _snapshot_identity_probe(root: pathlib.Path):
    """-> {present, verified, account} | {unreadable: True} | None.

    Thin I/O only. THREE outcomes, and conflating any two of them is a bug:

      * `present: False` — the file parsed but PREDATES the field, so it cannot
        say which account it describes. evaluate() emits an `unknown` row (NOT
        no row: an absent key is read as a retired check and drops the flag).
      * `unreadable: True` — the file is missing, truncated or not JSON. The
        probe FAILED; we learned nothing.
      * `present: True` — a real answer.

    ⛔ A FAILED PROBE MUST NOT LOOK LIKE A RETIRED CHECK. This returned None on
    any exception, and None means "emit no row" — which health_check.diff()
    reads as `k not in live`, i.e. "this check no longer exists", and SILENTLY
    CLEARS its flag (reviewer, 2026-08-21). So a corrupt or briefly-unreadable
    positions.json would drop a live finding and, because the fire-once rule
    keys off that flag, re-arm the alert as though nothing had happened. It
    reports `unknown` instead: never alerts, never clears a prior flag.
    """
    p = root / "research_store" / "rh" / "positions.json"
    try:
        d = json.loads(p.read_text())
    except Exception:                                # noqa: BLE001
        return {"unreadable": True}
    if not isinstance(d, dict):
        return {"unreadable": True}
    if "identity_verified" not in d:
        return {"present": False, "verified": None,
                "account": d.get("account_number")}
    v = d.get("identity_verified")
    # ⛔ STRICT BOOLEAN. `bool(v)` coerced the STRING "false" -- and every other
    # truthy junk value -- to True, which reads as "the broker confirmed this
    # account" and, being `ok`, would clear a standing flag (reviewer,
    # 2026-08-21). A field we cannot interpret is a probe that did not answer.
    if not isinstance(v, bool):
        return {"unreadable": True}
    acct = d.get("account_number")
    acct = acct.strip() if isinstance(acct, str) else ""
    # ⛔ AN EMPTY ACCOUNT IS ONLY INCOHERENT WHEN THE CLAIM IS "CONFIRMED".
    # _write_broker_snapshot legitimately writes account_number "" together with
    # identity_verified false when no account is pinned (server.py:_expected_
    # account() empty), so rejecting that outright would turn a real, honest
    # artifact into "unreadable" (reviewer, 2026-08-21). Unverified-and-unnamed
    # is exactly what this check exists to report. Verified-and-unnamed is not:
    # a confirmation that names nothing cannot have come from the broker.
    if v and not acct:
        return {"unreadable": True}
    return {"present": True, "verified": v, "account": acct}


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


# The artifacts where being wrong costs money or protection. A file NOT in this
# list is research data: wrong is cheap and a page about it is noise.
CRITICAL_ARTIFACTS = (
    ("research_store/rh/positions.json",        "broker snapshot"),
    ("research_store/monitor/overrides.json",   "agent-set levels"),
    ("research_store/monitor/quotes.json",      "monitor quotes"),
    ("research_store/monitor/cooldown.json",    "stop-out cooldowns"),
    ("research_store/monitor/enforcement.json", "enforced levels"),
    ("research_store/current.json",             "the book"),
    ("research_store/flows.jsonl",              "external cash flows"),
)


def _unreadable_probe(root: pathlib.Path) -> list:
    """Which critical artifacts are PRESENT but will not parse. -> [(rel, label)]

    ⛔ ABSENT IS NOT A FINDING. A missing file is a real state — no levels set,
    the monitor has not quoted yet, no flows recorded — and paging about it is
    the cry-wolf noise this module exists to avoid. PRESENT-BUT-UNREADABLE is a
    different thing entirely, and until 2026-08-31 nothing in this system could
    tell the two apart: every reader caught the parse error and substituted the
    same empty default the absent case produces.

    That is not theoretical. Measured on 2026-08-31: an unreadable overrides.json
    silently reverted all 12 held positions from their agent-set stops to the
    loop's looser thesis stops (MU 843.00 -> 813.259), left one position with no
    stop at all, and was invisible to the monitor, positions() and the dashboard
    alike — because all three then AGREED, and all three were CORRECT. The
    protection really had gone. No cross-check between surfaces can find that;
    only asking whether the file itself is readable can.

    Never raises: a probe that takes health down teaches nothing.
    """
    bad = []
    for rel, label in CRITICAL_ARTIFACTS:
        path = root / rel
        try:
            if not path.exists():
                continue
            text = path.read_text()
            if rel.endswith(".jsonl"):
                for line in text.splitlines():
                    if line.strip():
                        json.loads(line)
            else:
                json.loads(text)
        except OSError:
            bad.append((rel, f"{label} — unreadable"))
        except Exception:                       # noqa: BLE001 — malformed content
            bad.append((rel, label))
    return bad


def gather(root: pathlib.Path | None = None, *, use_network: bool = True) -> dict:
    """Collect the timestamps evaluate() judges. All failures degrade to None."""
    root = root or REPO
    # A thrown kill-switch stops both trading jobs before they write anything, so
    # their artifacts go stale by design. Report that, don't alarm on it.
    halted = HALTED if (root / KILL_SWITCH).exists() else None
    return {
        "slow_loop":     _mtime(root / "research_store" / "current.json"),
        # (`fast_loop` was gathered here as an (output, log) PAIR until it was
        # retired 2026-08-14. evaluate()'s paired branch is still live and still
        # selftested against a synthetic spec, because it is the only thing that
        # catches a job which runs, logs, and produces nothing — wire the next
        # paired job to it rather than rediscovering the need.)
        "monitor":       _mtime(root / "research_store" / "monitor" / "state.json"),
        "ledger_backup": _mtime(root / "logs" / "backup.log"),
        "signal_panel":  _newest_journal_event(root, "signal_panel"),
        "session":       _mtime(root / "logs" / "session.log"),
        "review":        _newest_journal_event(root, "codex_review"),
        "newsletter":    _newest_in_dir(root / "research_store" / "newsletters", "*.sent"),
        "universe_refresh": _newest_in_dir(
            root / "research_store" / "universe" / "proposals", "*.json"),
        "adaptive_tune": _last_actions_run() if use_network else SKIPPED,
        "unprotected_positions": _unprotected_probe(root),
        "unrecorded_fills": _unrecorded_fills_probe(root),
        "snapshot_identity": _snapshot_identity_probe(root),
        "positions_snapshot": snapshot_freshness.status(
            root / "research_store" / "rh" / "positions.json",
            root / "research_store" / "journal.jsonl"),
        # Is each long-running service running the code on disk? Scheduling
        # liveness (everything above) cannot see this: a stale process keeps
        # writing fresh artifacts, so every other check reads green while the
        # process enforces last week's logic. See src/deployed.py.
        "deployed_code": _deployed_probe(root),
        # Content, not liveness: a file can be freshly written and unparseable.
        "unreadable_artifacts": _unreadable_probe(root),
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

    # ⛔ A MONITOR-TRIGGERED STOP EXIT IS A TRADE THIS CHECK MUST SEE.
    # It keyed only off `agent_decision` and was therefore blind to the path
    # that places the most urgent orders in the system. On 2026-08-20 an RTX
    # stop fired, the sell FILLED, and the executor was denied permission to
    # write its result -- nothing recorded the trade, and this check reported
    # clean because RTX had never been an `agent_decision`.
    je = [{"event": "exit_signal", "ts": "2026-08-20T15:52:12Z", "armed": True,
           "triggers": [{"symbol": "RTX", "reason": "stop", "fraction": 1.0}]}]
    assert unrecorded_fills(je, "2026-08-20") == ["RTX"], unrecorded_fills(je, "2026-08-20")
    # ...and it goes quiet once the fill IS journalled
    je_ok = je + [{"event": "execution", "ts": "2026-08-20T15:53:01Z",
                   "fills": [{"symbol": "RTX"}]}]
    assert unrecorded_fills(je_ok, "2026-08-20") == [], unrecorded_fills(je_ok, "2026-08-20")
    # ⚠️ ALERT-ONLY AND HALTED SIGNALS PLACE NOTHING, so expecting a fill there
    # would cry wolf on the system working exactly as designed -- which is how a
    # real alarm gets ignored (the 2026-08-18 issue #11 lesson).
    assert unrecorded_fills(
        [dict(je[0], armed=False)], "2026-08-20") == []
    assert unrecorded_fills(
        [dict(je[0], halted=True)], "2026-08-20") == []
    # a multi-symbol trigger list reports each missing name
    jm = [{"event": "exit_signal", "ts": "2026-08-20T15:52:12Z", "armed": True,
           "triggers": [{"symbol": "RTX"}, {"symbol": "MRK"}]},
          {"event": "execution", "ts": "2026-08-20T15:53:00Z",
           "fills": [{"symbol": "MRK"}]}]
    assert unrecorded_fills(jm, "2026-08-20") == ["RTX"], unrecorded_fills(jm, "2026-08-20")
    # a trigger with no symbol must not become an empty-string finding
    assert unrecorded_fills(
        [{"event": "exit_signal", "ts": "2026-08-20T15:52:12Z", "armed": True,
          "triggers": [{"reason": "stop"}]}], "2026-08-20") == []

    # ⛔ A SIGNAL THAT PLACED NOTHING MUST NOT READ AS A MISSING FILL.
    # The signal is journalled BEFORE the launch ceiling is applied, so a
    # ceiling-blocked breach (and a breach the executor found already flat)
    # would otherwise be reported as an unrecorded trade — and a check that
    # cries wolf gets muted, which is how a REAL missing fill gets ignored
    # (reviewer, 2026-08-20).
    blocked = [{"event": "exit_signal", "ts": "2026-08-20T15:52:12Z", "armed": True,
                "launched": False,
                "triggers": [{"symbol": "RTX", "reason": "stop"}]}]
    assert unrecorded_fills(blocked, "2026-08-20") == [], unrecorded_fills(blocked, "2026-08-20")
    flat = [dict(blocked[0], launched=True, sold_nothing=True)]
    assert unrecorded_fills(flat, "2026-08-20") == [], unrecorded_fills(flat, "2026-08-20")
    # ...and a signal from BEFORE the field existed is still checked, so history
    # is not silently forgiven
    legacy = [{"event": "exit_signal", "ts": "2026-08-20T15:52:12Z", "armed": True,
               "triggers": [{"symbol": "RTX"}]}]
    assert unrecorded_fills(legacy, "2026-08-20") == ["RTX"], unrecorded_fills(legacy, "2026-08-20")

    # ⛔ THE UTC DAY BOUNDARY IS NOT A WALL. A signal at 23:59:59Z whose fill
    # lands at 00:00:01Z is one trade, not a missing one.
    midnight = [{"event": "exit_signal", "ts": "2026-08-20T23:59:59Z", "armed": True,
                 "launched": True, "triggers": [{"symbol": "RTX"}]},
                {"event": "execution", "ts": "2026-08-21T00:00:01Z",
                 "fills": [{"symbol": "RTX"}]}]
    assert unrecorded_fills(midnight, "2026-08-20") == [], unrecorded_fills(midnight, "2026-08-20")
    # ...but an execution LATE the next day is a different trade, not a spillover
    late = [midnight[0], {"event": "execution", "ts": "2026-08-21T14:30:00Z",
                          "fills": [{"symbol": "RTX"}]}]
    assert unrecorded_fills(late, "2026-08-20") == ["RTX"], unrecorded_fills(late, "2026-08-20")

    # ⚠️ A MULTI-SYMBOL DECISION IS ONE RECORD, NOT ONE SYMBOL. The real
    # 2026-08-17 ETF liquidation journalled symbol="IWM,XLK,XLE,XLV" for a
    # single exit decision. All four filled and all four were journalled, and
    # this check still reported the whole comma-joined blob as an unrecorded
    # fill -- it fired on 2026-08-18, filed issue #11, and was wrong. A check
    # that cries wolf is how a real one gets ignored.
    j3 = [
        {"event": "agent_decision", "ts": "2026-08-17T13:32:00Z",
         "symbol": "IWM,XLK,XLE,XLV", "action": "exit"},
        {"event": "execution", "ts": "2026-08-17T13:31:00Z",
         "fills": [{"symbol": "IWM"}, {"symbol": "XLK"},
                   {"symbol": "XLE"}, {"symbol": "XLV"}]},
    ]
    assert unrecorded_fills(j3, "2026-08-17") == [], unrecorded_fills(j3, "2026-08-17")
    # ...and a PARTIAL multi-symbol execution must still report the ones that
    # are genuinely missing, BY NAME rather than as an opaque blob.
    j4 = [
        {"event": "agent_decision", "ts": "2026-08-17T13:32:00Z",
         "symbol": "IWM, XLK , XLE", "action": "exit"},
        {"event": "execution", "ts": "2026-08-17T13:31:00Z",
         "fills": [{"symbol": "IWM"}]},
    ]
    assert unrecorded_fills(j4, "2026-08-17") == ["XLE", "XLK"], unrecorded_fills(j4, "2026-08-17")
    # a multi-symbol HOLD is still not a trade
    j5 = [{"event": "agent_decision", "ts": "2026-08-17T18:38:00Z",
           "symbol": "AMD,DELL,INTC", "action": "hold"}]
    assert unrecorded_fills(j5, "2026-08-17") == []

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

    # A recorded fill newer than ownership state is a distinct, loud failure.
    stale_snap = {"stale": True, "snapshot_ts": now - day,
                  "last_fill_ts": now}
    c = {x.key: x for x in evaluate(now, {"positions_snapshot": stale_snap})}
    assert c["positions_snapshot"].status == "stale_after_fill", c
    fresh_snap = {"stale": False, "snapshot_ts": now,
                  "last_fill_ts": now - day}
    c = {x.key: x for x in evaluate(now, {"positions_snapshot": fresh_snap})}
    assert c["positions_snapshot"].status == "ok", c

    # SNAPSHOT IDENTITY. There was no coverage of this branch at all until
    # 2026-08-21 (reviewer), which is how it went alertable-with-an-impossible-
    # remedy unnoticed. Pin all three states AND their alerting behaviour.
    def _ident(probe):
        return {x.key: x for x in evaluate(now, {"snapshot_identity": probe})}

    c = _ident({"present": True, "verified": True, "account": "948184924"})
    i = c["snapshot_identity"]
    assert i.status == "ok" and i.healthy and not i.alertable, i
    assert "948184924" in i.detail, i

    # broker named no account -> settled, visible, NOT healthy, NEVER pages.
    c = _ident({"present": True, "verified": False, "account": "948184924"})
    i = c["snapshot_identity"]
    assert i.status == "unverified", i
    assert not i.healthy, "an unconfirmed identity must not read as healthy"
    assert not i.alertable, "there is no operator action — it must never page"
    assert "948184924" in i.detail, i
    # the old text told the operator to do something impossible; never again.
    assert "re-publish" not in i.detail.lower(), i

    # a file predating the field cannot answer -> a row that never pages and
    # never clears a flag. It must NOT be absent: an absent key is read as a
    # retired check and drops the flag.
    i = _ident({"present": False, "verified": None, "account": None})["snapshot_identity"]
    assert i.status == "unknown" and not i.alertable and not i.healthy, i
    assert i.status not in KNOWN_NON_ALERTING, "a legacy file must not clear a flag"
    assert "snapshot_identity" not in {x.key for x in evaluate(now, {})}, "no probe -> no row"

    # ⛔ A FAILED PROBE IS NOT A RETIRED CHECK. Emitting no row here would let
    # health_check.diff() clear a standing flag as though the check had been
    # deleted, dropping a live finding (reviewer, 2026-08-21).
    i = _ident({"unreadable": True})["snapshot_identity"]
    assert i.status == "unknown", i
    assert not i.alertable, "a failed probe has nothing to act on"
    assert not i.healthy, "a failed probe is not a clean bill of health"
    assert i.status not in KNOWN_NON_ALERTING, "it must NOT clear a prior flag"

    # and the probe itself must classify the three cases apart
    import tempfile as _tf                            # noqa: PLC0415
    with _tf.TemporaryDirectory() as _pd:
        _r = pathlib.Path(_pd)
        _f = _r / "research_store" / "rh"
        _f.mkdir(parents=True)
        assert _snapshot_identity_probe(_r) == {"unreadable": True}, "missing file"
        (_f / "positions.json").write_text("{not json")
        assert _snapshot_identity_probe(_r) == {"unreadable": True}, "corrupt file"
        (_f / "positions.json").write_text('["a list"]')
        assert _snapshot_identity_probe(_r) == {"unreadable": True}, "not an object"
        (_f / "positions.json").write_text('{"account_number": "1"}')
        assert _snapshot_identity_probe(_r)["present"] is False, "legacy file"
        # ⛔ the STRING "false" must never read as verified
        (_f / "positions.json").write_text(
            '{"account_number": "1", "identity_verified": "false"}')
        assert _snapshot_identity_probe(_r) == {"unreadable": True}, "string bool"
        (_f / "positions.json").write_text(
            '{"account_number": "1", "identity_verified": 1}')
        assert _snapshot_identity_probe(_r) == {"unreadable": True}, "int bool"
        # an identity claim with no account names nothing
        (_f / "positions.json").write_text('{"identity_verified": true}')
        assert _snapshot_identity_probe(_r) == {"unreadable": True}, "no account"
        (_f / "positions.json").write_text(
            '{"account_number": "1", "identity_verified": false}')
        _p = _snapshot_identity_probe(_r)
        assert _p["present"] is True and _p["verified"] is False, _p

    # a MISSING snapshot must never read as healthy just because nothing filled
    c = {x.key: x for x in evaluate(now, {"positions_snapshot":
         {"stale": False, "snapshot_ts": None, "last_fill_ts": None}})}
    i = c["positions_snapshot"]
    assert i.status == "missing" and i.alertable and not i.healthy, i
    assert "fails open" in i.detail.lower(), "must not misstate stop coverage"

    # unverified-and-unnamed is the writer's own honest artifact, not a fault
    with _tf.TemporaryDirectory() as _pd2:
        _r2 = pathlib.Path(_pd2)
        (_r2 / "research_store" / "rh").mkdir(parents=True)
        (_r2 / "research_store" / "rh" / "positions.json").write_text(
            '{"account_number": "", "identity_verified": false}')
        _q = _snapshot_identity_probe(_r2)
        assert _q == {"present": True, "verified": False, "account": ""}, _q
        # ...but a CONFIRMED identity naming no account is impossible
        (_r2 / "research_store" / "rh" / "positions.json").write_text(
            '{"account_number": "", "identity_verified": true}')
        assert _snapshot_identity_probe(_r2) == {"unreadable": True}

    # the two non-alerting statuses are NOT interchangeable: only the settled
    # one may clear a stale flag (scripts/health_check.py:diff relies on this).
    assert "unknown" in NON_ALERTING and "unknown" not in KNOWN_NON_ALERTING
    assert "unverified" in NON_ALERTING and "unverified" in KNOWN_NON_ALERTING
    assert not Check("k", "l", None, "unverified", "d").alertable
    assert not Check("k", "l", None, "unverified", "d").healthy

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
    # ⛔ These used `fast_loop` as their fixture until it was retired 2026-08-14.
    # It was the only PAIRED (output, log) job, so its removal would have deleted
    # the only coverage of evaluate()'s blocked-run branch — the branch that
    # catches a job which runs, logs, and produces nothing. Rather than lose it,
    # the paired assertions now run against a synthetic spec injected for the
    # duration. When a real paired job appears, point these back at it.
    PAIRED = "_paired_probe"
    SPECS[PAIRED] = ("Synthetic paired job (test only)", 4,
                     "research_store/rh/order_plan.json")
    try:
        for _label, _probes in (("empty", {}),
                                ("skipped", {k: SKIPPED for k in SPECS}),
                                ("halted", {k: HALTED for k in SPECS}),
                                ("paired", {PAIRED: (None, now)})):
            assert {c.key for c in evaluate(now, _probes)} == set(SPECS), _label

        # a deliberately halted job is "unknown" too: not healthy, but never
        # alerting. Throwing the kill-switch is an operator decision, not a fault.
        r = {c.key: c for c in evaluate(now, {PAIRED: HALTED})}
        assert r[PAIRED].status == "unknown", r[PAIRED]
        assert not r[PAIRED].alertable, "a thrown kill-switch must not alert daily"
        assert not r[PAIRED].healthy, "halted is not a clean bill of health"
        assert "kill-switch" in r[PAIRED].detail, r[PAIRED]

        # REGRESSION (2026-07-30): the fast loop and risk review ran, wrote their
        # logs, and halted on a permission prompt for two sessions while this file
        # reported 7/8 healthy. Both were keyed to LOG mtime, and a blocked run
        # still logs. Every spec is now keyed to the ARTIFACT the job exists to
        # produce, so a fresh log with a stale output reads STALE. Pinned as an
        # invariant over the whole table so a future edit cannot point any check
        # back at a log — which is what made the original outage invisible.
        assert not any(s[2].startswith("logs/") for k, s in SPECS.items()
                       if k not in ("ledger_backup", "session")), \
            "a log proves the job started, not that it did its work"
        outage = {PAIRED: now - 3 * day}   # logs were fresh
        r = {c.key: c for c in evaluate(now, outage)}
        assert all(r[k].status == "ok" for k in outage), "3d is inside the 4d window"
        outage = {PAIRED: now - 5 * day}
        r = {c.key: c for c in evaluate(now, outage)}
        for k in outage:
            assert r[k].status == "stale" and r[k].alertable, r[k]

        # ...but staleness alone was too slow: at the moment Aaron noticed by hand,
        # the outage was 2.4d old and a 4d window still read "ok". The PAIR is what
        # catches it after one bad run. Replay the real shape: log fresh, output
        # two days back.
        blocked = {PAIRED: (now - 2 * day, now - dt.timedelta(hours=2))}
        r = {c.key: c for c in evaluate(now, blocked)}
        for k in blocked:
            assert r[k].status == "blocked", r[k]
            assert r[k].alertable and not r[k].healthy, r[k]
            assert "did not finish" in r[k].detail, r[k]
        # and it must fire while the staleness window still reads clean — that gap
        # is the entire point, so assert the old check would NOT have caught this.
        assert {c.key: c for c in evaluate(now, {k: v[0] for k, v in blocked.items()})
                }[PAIRED].status == "ok", "2d output age alone stays inside 4d"

        # a healthy run writes both within seconds — never read as blocked
        fine = {PAIRED: (now - dt.timedelta(hours=3),
                         now - dt.timedelta(hours=3) + dt.timedelta(minutes=4))}
        assert {c.key: c for c in evaluate(now, fine)}[PAIRED].status == "ok"
        # nor may a long weekend: on a good Friday run both moved together, so the
        # gap stays ~0 no matter how many days pass before the next run.
        weekend = {PAIRED: (now - 3 * day, now - 3 * day + dt.timedelta(minutes=2))}
        assert {c.key: c for c in evaluate(now, weekend)}[PAIRED].status == "ok", \
            "both artifacts move together on a good run — weekends must stay quiet"
        # a job that has run but NEVER produced its output is blocked, not "never"
        r = {c.key: c for c in evaluate(now, {PAIRED: (None, now - dt.timedelta(hours=1))})}
        assert r[PAIRED].status == "blocked" and "no research_store" in r[PAIRED].detail, r[PAIRED]
        # a missing log degrades to plain staleness rather than crashing
        r = {c.key: c for c in evaluate(now, {PAIRED: (now - 5 * day, None)})}
        assert r[PAIRED].status == "stale", r[PAIRED]
    finally:
        SPECS.pop(PAIRED, None)

    # the retired job must be gone from BOTH tables — a spec without a gather
    # entry reads "never ran" forever, and a gather entry without a spec is
    # silently dropped by evaluate(). This is the assertion that would have
    # caught the risk-review overlay's half-removal.
    assert "fast_loop" not in SPECS, "fast_loop was retired 2026-08-14"
    assert "fast_loop" not in gather(REPO, use_network=False), \
        "gather() still probes a retired job"

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
