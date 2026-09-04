#!/usr/bin/env python3
"""THE SESSION RUNNER — starts one agent trading session and outlives it.

    scripts/session.py <premarket|open|close|wake> [--dry-run]

WHAT THIS IS
    The legacy loops (`run_fast_loop.sh` and friends) hand a fixed procedure to
    a headless Claude and let it run. This runs a SESSION instead: the agent
    gets the charter (rendered live from config, never a stale copy), the MCP
    tool surface, and its own judgment. This file's whole job is the part the
    agent cannot do for itself — take the lock, gather the facts at the right
    moment, supervise the process, and make sure a dead session is recorded as
    dead.

ORDERING IS LOAD-BEARING. In exactly this sequence:

    signal handlers -> acquire lock -> BUILD BRIEF -> t0 -> integrity snapshot
    -> spawn -> finally: kill the group, verify integrity, release the lock

    The brief is built AFTER the lock, never before. A session that waits on
    the lock and then reasons from facts gathered before the wait is trading on
    stale prices, and the wait can be fifteen minutes: a 09:35 session holding
    a 09:30 quote reasons through the fastest five minutes of the day and does
    not know it. Waiting is normal here (the previous session may still be
    placing orders), so this is the common path, not the edge case.

WHY `classify` EXISTS AND WHY IT IS NOT `ok = bool(output)`
    A headless `claude -p` that dies on an overload prints its banner to STDOUT
    and exits 0. Under `ok = bool(out)` that is a SUCCESS: the run logs ok,
    exits 0, systemd reports "Finished successfully", and nothing anywhere
    registers that the session never happened. A trading day silently does not
    occur. So an error DOMINATES output: any error signature in stdout makes
    the run a failure regardless of the exit code.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import charter                      # noqa: E402
import integrity                    # noqa: E402
import mandate as mandate_mod       # noqa: E402
import session_lock                 # noqa: E402
import evidence_receipt             # noqa: E402
import evidence_build               # noqa: E402
import strategy                     # noqa: E402
import snapshot_freshness            # noqa: E402
import broker_read                  # noqa: E402
import governance as gov            # noqa: E402
from research_store import store    # noqa: E402  session_run journal events
import models                       # noqa: E402  the session's model + chain (config/models.toml)
import fallback                     # noqa: E402  the chain walk (clean failure only)
from fallback import (              # noqa: E402  stream parsing + reason classes live there
    result_text as stream_result, counts as stream_counts, reason_class)
import notify                       # noqa: E402  phone push on a fallback

MODES = ("premarket", "open", "close", "wake")
LOCK = REPO / "research_store" / "session.lock"
OVERRIDES = REPO / "research_store" / "monitor" / "overrides.json"

# The session's own wall-clock ceiling. A hung `claude -p` holds the lock, so
# every LATER session is blocked behind it -- one wedged process silently ends
# the trading day rather than just its own slot. Generous, because a session
# that is merely slow must not be killed mid-order.
# `close` is 900, NOT 1200: the independent review runs immediately after this
# in deploy/run_session.sh, and 15:15 + 900s + a 600s review must land before
# risk_review at 15:45. Today's first live session took 433s, so 900 is ample.
# If you raise this, lower TIMEOUT_S in scripts/review_session.py to match.
TIMEOUT_S = {"premarket": 900, "open": 1800, "close": 900, "wake": 600}

# Signatures that mean the MODEL never ran, so no work was done and no order was
# placed.
#
# ⛔ THESE ARE PROSE. An earlier form matched them anywhere in stdout, which is
# the AGENT'S OWN WRITING -- and the charter puts "rate limits" in front of the
# agent directly (src/charter.py: "Observe the data feed's rate limits"). An
# agent restating its own instructions, or narrating a flaky quote tool ("no
# rate limit hit", "recovered from a connection error and placed the BUY"),
# failed its own session -- a session that RAN AND PLACED ORDERS, marked failed
# AND retryable, which is the double-placing outcome should_retry exists to
# prevent. So stdout is matched ONLY at the start of a line, where the CLI
# prints its banners and the agent's prose does not begin.
_DEAD_SIGNATURES = (
    "api error: 5",             # 5xx -- 529 overloaded is the common one
    "overloaded",
    "rate limit",
    "connection error",
    "network error",
    "authentication_error",
    "invalid api key",
    "credit balance is too low",
)

# Banner prefixes: on stdout these mark a line as CLI diagnostics rather than
# agent prose, so a signature ANYWHERE on such a line counts.
_BANNER_PREFIXES = ("api error", "error:", "error ", "fatal", "usage:")


# --------------------------------------------------------------------------- #
# verdict
# --------------------------------------------------------------------------- #
def classify(rc: int, out: str, err: str | None) -> tuple[bool, str | None]:
    """Did the session actually happen? -> (ok, error_reason).

    An ERROR DOMINATES OUTPUT. `claude -p` prints its failure banner to stdout
    and exits 0, so neither the exit code nor the presence of output can be
    trusted alone -- see the module docstring. Checked in order of certainty:
    a non-zero exit, then a death signature anywhere in the streams, then an
    implausibly short transcript.
    """
    if rc != 0:
        tail = (err or out or "").strip()[-300:] or "no output"
        return False, f"exit {rc}: {tail}"

    # stderr is CLI diagnostics, never agent prose -- match anywhere in it.
    hit = _signature_in(err or "", anywhere=True)
    # stdout is the agent's own writing -- match only where the CLI speaks.
    hit = hit or _signature_in(out or "", anywhere=False)
    if hit:
        return False, f"session died: {hit}"

    if not (out or "").strip():
        return False, "exit 0 but no output — the session produced nothing"

    # ⚠️ NO MINIMUM-LENGTH CHECK. An earlier form failed any session under 600
    # bytes as "a banner, not a session". But `claude -p` prints only the FINAL
    # assistant message, not a transcript, and a correct quiet session -- regime
    # off, nothing eligible, no action taken -- is legitimately one short
    # sentence. That heuristic failed real sessions for doing the right thing.
    # A genuine banner is caught by the signature check above, on its own merits.
    return True, None


def _signature_in(text: str, anywhere: bool) -> str | None:
    """Find a death signature. -> the offending line, or None.

    `anywhere=False` restricts the match to lines that look like CLI output
    (a banner prefix) rather than agent prose. See _DEAD_SIGNATURES.
    """
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if not anywhere and not low.startswith(_BANNER_PREFIXES):
            continue
        for sig in _DEAD_SIGNATURES:
            if sig in low:
                return line[:220]
    return None


def should_retry(err: str | None) -> bool:
    """May this failure be retried? -> True only when NOTHING can have happened.

    ⛔ A retry re-runs a whole session. If the first attempt got far enough to
    place an order, the second attempt places it AGAIN -- and the agent has no
    way to know it is a repeat. So this returns True only for failures that
    prove the model never ran at all: transport, auth, capacity. Anything else
    (a crash mid-session, a short transcript, an unrecognised error) is treated
    as possibly-having-traded and is NOT retried. Failing a session is cheap;
    double-placing is not.
    """
    if not err:
        return False
    low = err.lower()
    return any(s in low for s in _DEAD_SIGNATURES)


# --------------------------------------------------------------------------- #
# level claim vs. artifact
# --------------------------------------------------------------------------- #
# Actions whose whole point is to move a level. If one of these is recorded and
# the override file is byte-identical afterwards, the decision did not bind.
LEVEL_ACTIONS = ("tighten_stops", "tighten_stop", "set_levels", "lower_tp",
                 "raise_target", "ratchet_stop")


def level_claim_unmet(decisions, overrides_before, overrides_after):
    """A recorded level change with no matching artifact. Pure. None if fine.

    Does NOT block or retry -- it makes the claim/artifact gap visible, the
    same way unrecorded_fills catches a claimed fill with no execution.
    """
    claimed = [d.get("action") for d in (decisions or [])
               if d.get("event") == "agent_decision"
               and str(d.get("action") or "") in LEVEL_ACTIONS]
    if not claimed:
        return None
    if (overrides_before or {}) != (overrides_after or {}):
        return None
    return (f"session recorded {sorted(set(claimed))} but "
            f"research_store/monitor/overrides.json is unchanged — the level "
            f"decision did not take effect; check the enforcement object "
            f"set_levels returned")


def _read_overrides() -> dict:
    """Current override file, {} when absent or torn. Never raises."""
    try:
        return json.loads(OVERRIDES.read_text())
    except Exception:                                          # noqa: BLE001
        return {}


def _decisions_since(journal, since_ts: str) -> list:
    """agent_decision events written at or after `since_ts`. Never raises."""
    out = []
    try:
        for line in journal.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get("event") == "agent_decision" and str(e.get("ts") or "") >= since_ts:
                out.append(e)
    except Exception:                                          # noqa: BLE001
        pass
    return out


# --------------------------------------------------------------------------- #
# process supervision
# --------------------------------------------------------------------------- #
def _kill_group(proc, term_wait: float = 5.0, kill_wait: float = 2.0) -> None:
    """TERM the whole process GROUP, then KILL. Never raises.

    The group, not the process: `claude` spawns MCP servers as children, and
    killing only the parent orphans them. They hold the OpenD connection and
    their own file handles, and the next session then races live children of a
    session that is supposedly over. Spawned with start_new_session=True so
    the group id is the child's pid and os.killpg cannot reach our own group.
    """
    if proc is None or proc.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, term_wait), (signal.SIGKILL, kill_wait)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


_INTERRUPTED = {"why": None}


def install_signal_handlers() -> None:
    """Turn TERM/INT into a normal exception path so `finally:` still runs.

    Default SIGTERM ends the interpreter outright: no `finally`, so the child
    process group is orphaned, integrity is never verified and -- worst -- the
    lock file is left held by a pid that no longer exists. Every later session
    then blocks on a ghost. Raising instead means the teardown that already
    exists does its job.
    """
    def _handler(signum, _frame):
        _INTERRUPTED["why"] = signal.Signals(signum).name
        raise KeyboardInterrupt(f"received {signal.Signals(signum).name}")

    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(s, _handler)
        except (ValueError, OSError):
            pass        # not the main thread; the default behaviour stands


# --------------------------------------------------------------------------- #
# the brief
# --------------------------------------------------------------------------- #
def _tool_names() -> list[str]:
    """The MCP tool surface, DISCOVERED. Never a hardcoded list.

    The charter tells the agent what it has; if that list is written by hand it
    is wrong the first time a tool is added or renamed, and the agent is told it
    has a tool that does not exist (or, worse, never told about one it does).
    """
    out = subprocess.run(
        [str(REPO / ".venv" / "bin" / "python"),
         str(REPO / "scripts" / "session_tools.py"), "--print"],
        capture_output=True, text=True, timeout=120)
    names = [ln.strip() for ln in out.stdout.splitlines()
             if ln.strip().startswith("mcp__")]
    if not names:
        raise RuntimeError("MCP tool discovery returned nothing — refusing to "
                           "brief the agent on a tool surface we cannot read")
    return names


# The reviewer's verdict is shown to OPEN and CLOSE sessions only. Those are
# the sessions that decide the book, so they are the ones a dissent is about.
# The risk-review overlay is a separate legacy loop that never reads a brief,
# and a `wake` fires on a price condition the agent pre-registered — handing
# either one an argument about yesterday's rotation is noise at the moment it
# most needs to act.
REVIEWED_MODES = ("open", "close")

# ---------------------------------------------------------------------------
# ⛔ THE VERDICT DOES NOT REACH THE AGENT RIGHT NOW. THIS IS DELIBERATE.
#
# Turned off 2026-08-13 by the principal. The reviewer still runs after every
# session, still journals, still pushes to the phone and is still scored against
# the tape — the OPERATOR is the audience. What is switched off is the one path
# that put a verdict in front of the agent.
#
# WHY, so a future session does not "fix" this:
#   1. The agent is STATELESS. Showing it yesterday's verdict once is not
#      learning; it is a single extra input to one session. The answer it
#      records lands in the journal and nothing has ever read it back. The loop
#      was open while looking closed.
#   2. Nothing here named the PROCESS to the agent. The charter — its whole
#      standing account of the game — never mentioned that sessions are
#      reviewed or scored, so an anonymous critique arrived with no provenance
#      and no stated purpose. An agent resolving that ambiguity invents a reason
#      for it, and that is uncontrolled anchoring. Compare render_gate()'s
#      "a human can veto the unusual", which was false and had the agent
#      deferring to a review that never happened: a process described wrongly —
#      or not at all — is worse than one not shown.
#   3. So the principal is evaluating the reviewer's judgment against the
#      agent's FIRST, and will then decide whether feeding it back is useful and
#      what would have to be true for it to work. That question is OPEN, not
#      forgotten. See docs/OPSLOG.md 2026-08-13.
#
# TO RECONNECT: set this True. That is the whole change — every piece below
# (render_review, last_review, the anonymity guard, the mode filter) is kept
# live and selftested precisely so this stays a one-line flip and not a rewrite.
# If you reconnect it, resolve (2) first: name the process in the charter, or
# the same ambiguity comes back with it.
# ---------------------------------------------------------------------------
SHOW_REVIEW_TO_AGENT = False


def last_review() -> dict | None:
    """The most recent independent verdict, or None. Never raises."""
    try:
        d = json.loads((REPO / "research_store" / "reviews" / "latest.json").read_text())
        return d if d.get("stance") in ("AFFIRM", "DISSENT", "SPLIT") else None
    except Exception:      # noqa: BLE001 — no review yet is normal, not an error
        return None


def render_review(rev: dict | None, mode: str) -> str:
    """The dissent block for the brief. -> "" when there is nothing to answer.

    ⛔ CURRENTLY RETURNS "" ALWAYS — SHOW_REVIEW_TO_AGENT is False (see there for
    why, and for how to reconnect). Everything below still runs, and is still
    selftested with the flag forced on, so the behaviour is proven rather than
    remembered. Read the rest as "what this does WHEN reconnected".

    ⚠️ THE AGENT MUST ANSWER IT, not merely receive it. A verdict the agent can
    read and ignore is the same as one nobody wrote: this system's recurring
    defect is a control that is present and does nothing. So the block ends in
    an instruction to record a response, which lands in the journal and is
    therefore checkable after the fact.

    An AFFIRM is shown too. Telling the agent only when it was wrong trains it
    toward the reviewer's preferences rather than toward being right, and a
    session that was right deserves to know that as much as one that was not.
    """
    # The flag is checked HERE, at the single point the block is built, rather
    # than at the call site — so there is exactly one gate and no second path
    # can be added later that quietly bypasses it.
    if not SHOW_REVIEW_TO_AGENT:
        return ""
    if not rev or mode not in REVIEWED_MODES:
        return ""
    stance = rev.get("stance")
    head = (rev.get("headline") or "").strip()
    would = (rev.get("what_i_would_have_done") or "").strip()
    dis = (rev.get("strongest_disagreement") or "").strip()
    change = (rev.get("what_would_change_my_mind") or "").strip()

    # ⛔ THE REVIEWER IS NEVER IDENTIFIED. Not its model, not its vendor, not
    # that it differs from you. Told the source, an agent reasons about the
    # source: it discounts a critic it decides does not understand momentum, or
    # defers to one it decides is more objective. Both are anchoring on who
    # spoke instead of what was said, and both are available as excuses. The
    # argument has to stand on the numbers in it, so only the argument is shown.
    out = [f"## INDEPENDENT REVIEW OF YOUR LAST SESSION — {stance}",
           "",
           "Your last session was reviewed against your own charter, by a",
           "reviewer with the same view of the book that you have. It cannot",
           "trade and has no authority over you.",
           "",
           "Do not speculate about who or what produced this. You are not told,",
           "deliberately. Judge it on its numbers — check them yourself against",
           "the book — and on nothing else.",
           ""]
    if head:
        out += [f"**{head}**", ""]
    if would:
        out += [f"- It would have: {would}", ""]
    if dis:
        out += [f"- Its strongest disagreement: {dis}", ""]
    if change:
        out += [f"- What would change its mind: {change}", ""]

    if stance == "AFFIRM":
        out += ["It agreed with you. That is information, not permission — the "
                "tape has not settled it yet.", ""]
    else:
        out += ["**You must answer this.** Not defer to it — answer it. If it is "
                "right, act differently today and say so. If it is wrong, say "
                "why, with the number that makes it wrong. Record either as a "
                "`record_decision` so the answer is on the record and can be "
                "priced later alongside its claim.",
                "",
                "Do not treat a disagreement as an instruction to trade, or as "
                "one to stand still. It is one more fact.", ""]
    return "\n".join(out) + "\n---\n\n"


# --------------------------------------------------------------------------- #
# session_run — every verdict leaves a journal event, and the brief shows them
# --------------------------------------------------------------------------- #
# ⛔ WHY (2026-09-03). The 15:15 CLOSE session died 86 s in on "You've hit your
# limit" — the operator's subscription cap. The runner classified it correctly,
# paged the phone, and wrote NOTHING to the journal: a failed session was the
# only event in the system that left no trace where the next session reads.
# The 10:35 session would have seen a journal that simply stopped at noon, and
# the charter forbids it to go looking for why. So: every verdict is journalled
# with a NAMED reason class, and the brief renders the last few sessions with
# what the class means and what to do about it. Define, don't imply.

#: The failure classes themselves (fallback.REASON_CLASSES) are matched
#: against the runner's error AND the CLI's own final `result` line; this is
#: what each one MEANS to the next session. Define, don't imply.
CLASS_MEANING = {
    "cli_unavailable": "the box's Claude CLI was mid self-update for a few "
                       "seconds — the runner retried; the operator's to watch.",
    "usage_limit":     "the operator's Claude subscription cap was hit — an "
                       "operator-side budget, not this system, not a gate.",
    "model_outage":    "the model vendor was down or overloaded — nothing this "
                       "system did.",
    "version_too_old": "the box's CLI rejected the pinned model — the operator's "
                       "to fix.",
    "unknown_model":   "the pinned model id does not exist (retired or mistyped "
                       "in config/models.toml) — the operator's to fix.",
    "auth":            "the runner could not authenticate — the operator's to fix.",
    "timeout":         "it hit its wall-clock limit; anything it recorded first "
                       "is in the journal.",
    "interrupted":     "the operator stopped it.",
    "not_launched":    "the runner refused to start it (lock, halt, or a broker "
                       "read it could not trust) — see its error.",
    "ambiguous":       "it died mid-run; whatever it recorded is in the journal.",
}
SESSION_HISTORY_N = 4


SESSION_CHAIN_BUDGET_X = 1.5     # the whole chain fits 1.5 session timeouts
DRILL_MCP = REPO / "deploy" / "drill_mcp.json"
DRILL_PROMPT = "Reply with exactly: OK"
DRILL_TIMEOUT_S = 120


def _journal_fallback(role: str, mode: str, frm: str, to: str | None,
                      reason: str | None, attempt: int, drill: bool = False) -> None:
    """One journal event + one phone push per chain transition. Never raises."""
    rec = {"event": "model_fallback",
           "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "role": role, "mode": mode, "from": frm, "to": to,
           "reason": reason, "attempt": attempt}
    if drill:
        rec["drill"] = True
    try:
        store.append_journal(rec)
    except Exception as e:              # noqa: BLE001
        print(f"⚠️ model_fallback journal write failed: {e}", file=sys.stderr)
    title = f"FALLBACK {role}:{mode}" + (" (DRILL)" if drill else "")
    body = f"{frm} → {to or 'EXHAUSTED'} ({reason}) attempt {attempt}"
    print(f"model fallback: {body}")
    notify.push(title, body, tags="warning" if to else "rotating_light")


def _drill_argv(model: str) -> list[str]:
    """A spawn that can reach NO tool and NO broker: empty MCP surface, no
    built-ins, dontAsk. What it proves is the chain, not the session."""
    return ["claude", "-p", "--output-format", "stream-json", "--verbose",
            "--model", model, "--setting-sources", "",
            "--strict-mcp-config", "--mcp-config", str(DRILL_MCP),
            "--tools", "", "--permission-mode", "dontAsk"]


def render_session_history(events: list) -> str:
    """The 'since your last run' block for the brief. -> "" with no history.

    One line per recent session (ran / MISSED: class), a legend ONLY for the
    classes that actually appear among the missed ones, and the standing
    instruction: a missed session is the operator's problem; do its review as
    part of this one; do not diagnose. Pure.
    """
    runs = [e for e in (events or []) if isinstance(e, dict)
            and e.get("event") == "session_run"]
    runs = sorted(runs, key=lambda e: str(e.get("ts") or ""))[-SESSION_HISTORY_N:]
    if not runs:
        return ""
    out = ["## SINCE YOUR LAST RUN — the scheduled sessions before this one", ""]
    missed_classes = []
    for e in reversed(runs):
        day = str(e.get("ts") or "")[:10]
        mode = str(e.get("mode") or "?")
        secs = e.get("seconds")
        stats = (f"{secs}s, " if secs is not None else "") + \
                f"{e.get('tool_calls', 0)} tool calls, {e.get('orders', 0)} orders"
        if e.get("ok"):
            out.append(f"- {day} {mode:<5} — ran ({stats})")
        else:
            cls = str(e.get("reason_class") or "ambiguous")
            missed_classes.append(cls)
            out.append(f"- {day} {mode:<5} — MISSED: {cls} ({stats})")
    if missed_classes:
        out += ["", "What the classes mean:"]
        for cls in dict.fromkeys(missed_classes):
            out.append(f"  {cls} — {CLASS_MEANING.get(cls, CLASS_MEANING['ambiguous'])}")
        out += ["",
                "A MISSED session is the operator's problem, not yours: do not "
                "diagnose it and do not go looking for why. Do that session's "
                "review — levels, trims considered, questions to tomorrow — as "
                "part of this one, then trade the book."]
    return "\n".join(out) + "\n\n---\n\n"


def session_history() -> str:
    """render_session_history over the real journal. Never raises."""
    try:
        return render_session_history(store.read_journal())
    except Exception as e:              # noqa: BLE001
        print(f"⚠️ session history unavailable: {e}", file=sys.stderr)
        return ""


def _journal_session_run(result: dict, out: str) -> None:
    """Journal one session verdict — ran or not. Never raises."""
    try:
        calls, orders = stream_counts(out)
        store.append_journal({
            "event": "session_run",
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mode": result.get("mode"),
            "ok": bool(result.get("ok")),
            "launched": bool(result.get("launched")),
            "reason_class": reason_class(result.get("error"), stream_result(out)),
            "error": (result.get("error") or "")[:200] or None,
            "seconds": result.get("seconds"),
            "tool_calls": calls, "orders": orders,
            "model": result.get("model"), "attempts": result.get("attempts"),
        })
    except Exception as e:              # noqa: BLE001
        print(f"⚠️ session_run journal write failed: {e}", file=sys.stderr)


EVIDENCE = REPO / "research_store" / "reviews" / "institutional_evidence.json"


def session_date() -> str:
    """The date THIS session is running, in the box's local (market) timezone.

    ⛔ THE EVIDENCE AGES AGAINST THIS, NOT AGAINST THE PRICE PANEL. Evidence
    freshness is a property of the session consuming it: a panel that stopped
    updating a week ago must narrow what is MEASURABLE, and must not make a
    52-day-old observation report as 45 and stay agent-visible past the
    staleness line. Same source as the "THIS SESSION" stamp on the brief, so the
    date the agent is told it is and the date its evidence is aged against
    cannot disagree.
    """
    return datetime.now(timezone.utc).astimezone().date().isoformat()


def rebuild_evidence(write: bool = True) -> tuple:
    """Rebuild the institutional evidence artifact. -> (ok, error).

    ⛔ THIS RUNS BEFORE ANYTHING RENDERS EVIDENCE, EVERY SESSION. The artifact
    is the agent's only view of what past decisions were followed by, and a
    stateless session that reads one assembled before its own experience is
    reading a lie of omission. There is no cron and no timer behind this on
    purpose: the only moment the evidence has to be fresh is the moment before
    a session consumes it, and that moment is here.

    ⛔ A FAILED REBUILD IS NOT A REASON TO DELIVER WHAT IS ON DISK. Evidence is
    advisory, so a failure must never stop the session trading — but the artifact
    left behind by an earlier run has unknown freshness for THIS session, and
    delivering it would stamp its version onto decisions taken under experience
    it does not describe. The caller renders nothing, writes no receipt, and says
    so loudly. Silence and stale evidence are the two outcomes forbidden here.

    `write=False` computes without touching the artifact — the dry run holds no
    session lock and must not write a file a live session may be reading.
    """
    try:
        evidence_build.rebuild(evidence_as_of=session_date(), write=write)
        return True, None
    except Exception as e:                                 # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def evidence_block(path=EVIDENCE, evidence=None) -> tuple:
    """(rendered block, provenance) for this session. ("", None) when nothing
    is validated, the artifact is missing, or it cannot be read.

    ⛔ ABSENCE IS DELIVERED AS ABSENCE. No artifact, nothing validated and an
    unreadable artifact all render the empty string — the session context gets no
    section at all rather than a line announcing that there is no evidence. A
    stateless agent told "no evidence available" has been told something, and
    what it would infer from that is not a fact anyone here has.

    ⛔ READ ONCE, AND ONLY HERE. The pair returned is what the session was
    handed; `record_decision` reads it back from the receipt rather than from
    this file, so an operator rebuilding the artifact mid-session cannot change
    what a decision claims the agent saw.

    Never raises: an evidence layer that can stop a trading session from starting
    is a worse failure than one that goes quiet.
    """
    try:
        import institutional_evidence as ie                # noqa: PLC0415
        if evidence is None:
            # READ BACK FROM DISK on the real path, deliberately: the artifact
            # the session just wrote is the artifact whose version gets stamped
            # onto decisions, so the block must come from the file itself and
            # not from an in-memory object that was never persisted.
            evidence = json.loads(Path(path).read_text())
        items = ie.select(evidence)
        block = ie.render_agent_block(items)
        if not block:
            return "", None
        problems = ie.check_block(block)
        if ie.version_of(items) != evidence.get("version"):
            # The stored version is what a decision would be stamped with, so it
            # has to be the hash of what is actually being rendered. A mismatch
            # means the artifact was edited after it was built, and stamping it
            # would put a false provenance on real decisions.
            problems.append("the artifact's version does not match the evidence "
                            "it contains — it was edited after it was built")
        if problems:
            # REFUSE TO DELIVER, rather than deliver something that broke its own
            # contract. A block over the cap or carrying verdict vocabulary is a
            # defect in this repo, not a fact about the market, and the session is
            # strictly better off with no section than with that one.
            print("evidence block withheld: " + "; ".join(problems), file=sys.stderr)
            return "", None
        return block, ie.provenance(evidence, items)
    except FileNotFoundError:
        return "", None
    except Exception as e:                                 # noqa: BLE001
        print(f"evidence block unavailable ({type(e).__name__}: {e})", file=sys.stderr)
        return "", None


def build_brief(mode: str, evidence: str = "") -> str:
    """Render the charter for this session. Called AFTER the lock is held."""
    text = charter.render(mandate_mod.load(), strategy.load(), _tool_names())
    stamp = datetime.now(timezone.utc).astimezone()
    # ⛔ ONE SESSION ONLY. This is operator context for the FIRST session after a
    # recovery -- "the open session already ran, do not replay it", "MRK's stub
    # was retained deliberately". Repeated every session it becomes standing
    # background noise describing a morning that is no longer today, and a
    # stateless agent has no way to tell a stale handoff from a live one.
    #
    # It was delivered unconditionally for as long as the file existed, with the
    # OPSLOG noting the operator should archive it afterwards. Remembering is not
    # a mechanism -- the same reason agentic-reload.timer exists as a backstop.
    #
    # Consumption is recorded in a SIBLING marker rather than by deleting the
    # file: the operator keeps the evidence, and a session that crashes after
    # the brief was built does not silently resurrect the handoff for the next
    # one. The marker names which session consumed it, so the choice is auditable.
    handoff_path = REPO / "research_store" / "RECOVERY_HANDOFF.md"
    consumed_path = REPO / "research_store" / "RECOVERY_HANDOFF.consumed"
    try:
        handoff = "" if consumed_path.exists() else handoff_path.read_text().strip()
    except Exception:                                      # noqa: BLE001
        handoff = ""
    if handoff:
        try:
            consumed_path.write_text(
                f"consumed by the {mode} session at "
                f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
                f"RECOVERY_HANDOFF.md is left in place as operator evidence; "
                f"delete this marker to deliver it again.\n")
        except Exception:                                  # noqa: BLE001
            pass    # never let bookkeeping stop a session being briefed
    handoff_block = ("\n\n---\n\nOPERATOR RECOVERY HANDOFF — read this before acting:\n"
                     + handoff + "\n") if handoff else ""
    # Read-only, and last of the fact blocks: it describes the PAST, and the
    # charter above it describes the mandate. It is empty on almost every day and
    # renders nothing at all when it is.
    # The text above already ends in a `---` rule EXCEPT when a recovery handoff
    # was appended, which ends in prose. Adding a second rule unconditionally
    # produced a doubled separator on the ordinary day.
    evidence_section = (("\n---\n\n" if handoff_block else "")
                        + evidence + "\n---\n\n") if evidence else ""
    return (f"{text}\n\n---\n\n{render_review(last_review(), mode)}"
            f"{session_history()}"
            f"{handoff_block}{evidence_section}"
            f"THIS SESSION: **{mode}**, "
            f"{stamp.strftime('%A %Y-%m-%d %H:%M %Z')}.\n")


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
# ⛔ THE SESSION'S MEMORY IS THIS REPOSITORY'S, NEVER CLAUDE CODE'S.
#
# The whole design rests on the agent being STATELESS at the model layer: a
# fresh inference, the charter rendered from config, the institutional evidence
# rebuilt seconds earlier, and current facts from the MCP tools. Everything the
# agent is supposed to carry between sessions is carried by an artifact this
# repo builds, versions and stamps onto the decisions it produced — which is
# what makes a decision auditable against what the agent actually saw.
#
# But `claude` was spawned FROM THE REPO ROOT with the ambient environment, and
# a headless run auto-discovers instruction sources that this system never
# authored, never versions and never stamps. Measured 2026-08-22 against the
# real session configuration, an open session was silently receiving:
#
#   /opt/agentic-trader/CLAUDE.md                              (45KB, Project)
#   /root/.claude/projects/-opt-agentic-trader/memory/MEMORY.md (auto-memory
#       index — 30 pointers written by INTERACTIVE coding sessions, carrying
#       one human's working preferences and half-finished to-do lists)
#
# CLAUDE.md is written for a coding agent maintaining this box; it is not a
# trading mandate, it disagrees with the charter in places, and it is not
# rendered from config, so it cannot be checked the way the charter is. The
# auto-memory index is worse in kind: it is SUBJECTIVE CROSS-SESSION STATE, it
# reaches the trader through a channel no part of this system controls, and it
# routes around the evidence artifact — the one place experience is supposed to
# enter. That is the defect. Not the content of any one line.
#
# So the isolation is mechanical and it is scoped to THIS CHILD PROCESS. Both
# variables are set in the spawn environment only: no export, nothing in the
# systemd unit, nothing in a settings file — an interactive coding session on
# this box still gets its CLAUDE.md and its memories, which is correct, because
# a human debugging the droplet needs them and a trading session must not have
# them.
#
#   CLAUDE_CODE_DISABLE_CLAUDE_MDS  every CLAUDE.md / .claude/rules loader in
#       the CLI returns [] on this, for User, Project and Local memory alike —
#       repo CLAUDE.md, .claude/CLAUDE.md, CLAUDE.local.md, .claude/rules, the
#       user's own, and nested discovery as the agent moves. It is the same
#       variable the CLI sets for its own hermetic eval runs and for safe mode.
#   CLAUDE_CODE_DISABLE_AUTO_MEMORY  checked first and unconditionally, before
#       the settings file is even read, so it cannot be undone by a setting
#       someone adds later. Disables the auto-memory directory for READ AND
#       WRITE: the trader neither inherits an interactive session's memories
#       nor deposits its own for one to inherit.
#
# ⛔ WHY NOT --setting-sources, WHICH ALSO SUPPRESSES THESE FILES: it suppresses
# them by disabling the user/project/local SETTINGS SOURCES, which is a much
# larger blast radius than the problem — and the order gate lives in a settings
# file. It survives (`--settings` is `flagSettings`, always enabled, as is
# policy) but the isolation would then be a SIDE EFFECT of a permissions change
# rather than a statement about memory, and the next person to touch the
# permission surface would silently re-open the leak. These two variables say
# what they mean.
#
# ⛔ WHY NOT --bare, WHICH LOOKS LIKE EXACTLY THIS FLAG: it also skips HOOKS.
# The PreToolUse order gate is a hook. `--bare` would place the trader outside
# the kill switch, the drawdown halt, the per-order cap and shadow mode, and it
# would look like a lockdown while doing it.
#
# ⛔ NOT --no-session-persistence. Nothing here passes -c/--continue or
# --resume, so no previous transcript can enter a session; persistence is a
# WRITE, and the file it writes is forensic evidence of a run that placed real
# orders. Disabling it would delete evidence and close no hole.
ISOLATION_ENV = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}


def _claude_env() -> dict:
    """The child's environment: the ambient one, plus the isolation above.

    SET, never defaulted -- `{**os.environ, **ISOLATION_ENV}` and not
    `setdefault`. An operator who runs deploy/run_session.sh from inside an
    interactive Claude Code session hands this process that session's whole
    CLAUDE_CODE_* environment, and a "0" inherited from there would otherwise
    re-enable the very thing this exists to switch off.
    """
    return {**os.environ, **ISOLATION_ENV}


def _claude_argv(brief: str, model: str) -> list[str]:
    """The command line for one spawn of `model`, tool surface from session_tools.sh.

    `model` is a parameter, never a literal: config/models.toml names the
    session role's primary and the chain behind it (src/models.py), read at
    spawn time. The 2026-09-03 outage swap was a code edit committed to main;
    this is why it never is again.

    Sourced rather than reimplemented: that file is the single definition of the
    session's tool surface, and a second copy here would drift from it silently
    -- in the direction of granting a tool the lockdown believes is revoked.
    """
    probe = subprocess.run(
        ["bash", "-c",
         f'source "{REPO}/deploy/session_tools.sh" >/dev/null 2>&1 && '
         f'printf "%s\\n" "${{SESSION_TOOL_ARGS[@]}}"'],
        capture_output=True, text=True, timeout=120)
    # ⛔ SPLIT, NEVER FILTER ON TRUTHINESS. `--tools ""` is an EMPTY-STRING
    # argument and it is load-bearing: it disables every built-in tool. A
    # `if ln.strip()` filter silently deleted it, and `--tools` is variadic, so
    # it then swallowed `--permission-mode dontAsk` as its own values -- the
    # session ran in the CLI's DEFAULT permission mode, which prompts and, with
    # nobody headless to answer, hangs until timeout. A silent no-trade day, and
    # the `if not args` guard below still passed.
    lines = probe.stdout.split("\n")
    args = lines[:-1] if lines and lines[-1] == "" else lines   # trailing newline only
    if not args:
        raise RuntimeError("session_tools.sh produced no tool args — refusing "
                           "to start a session with an unknown tool surface")
    if "--tools" in args and "" not in args:
        raise RuntimeError("the empty-string argument to --tools was lost — "
                           "refusing to start a session whose built-in tools "
                           "may not actually be disabled")
    # stream-json emits one JSON line per event AS IT HAPPENS instead of a
    # single blob at exit, so the run can be watched live. --verbose is required
    # for stream-json to include tool calls rather than only the final message.
    return ["claude", "-p", "--output-format", "stream-json", "--verbose",
            "--model", model, *args]


def _spawn_once(argv: list, stdin_text: str, stream_path: Path,
                timeout_s: int) -> tuple:
    """ONE headless spawn. -> (rc, stream text, stderr). Never raises.

    ⛔ THE PROMPT GOES ON STDIN, NEVER IN argv. `--allowedTools` is VARIADIC,
    so a trailing prompt argument is consumed as one more tool name and the
    session starts with no prompt at all. stdin also sidesteps MAX_ARG_STRLEN
    (128KiB per argument) -- the brief is ~28KB today and grows.
    ⛔ STDOUT GOES TO A FILE, NOT A PIPE. communicate() only returns once the
    child exits, so a piped stdout is invisible until the run is over. A file
    is written as the child produces each line, so it can be tailed, and the
    transcript is read back off disk afterwards so classify() sees exactly
    what the operator saw.
    A timeout KILLS THE GROUP AND THEN DRAINS: a session that hung is the one
    most likely to have placed orders and most in need of a transcript.
    """
    stream_path.parent.mkdir(parents=True, exist_ok=True)
    proc = None
    try:
        with open(stream_path, "w") as sfh:
            proc = subprocess.Popen(
                argv, cwd=str(REPO), env=_claude_env(),
                stdin=subprocess.PIPE, stdout=sfh, stderr=subprocess.PIPE,
                text=True, start_new_session=True)
            try:
                _, err = proc.communicate(input=stdin_text, timeout=timeout_s)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                try:
                    _, err = proc.communicate(timeout=10)
                except Exception:       # noqa: BLE001
                    err = ""
                err = (err or "") + f"\ntimed out after {timeout_s}s"
                rc = -1
    except Exception as e:              # noqa: BLE001  a spawn that cannot start
        rc, err = -1, f"spawn failed: {type(e).__name__}: {e}"
    finally:
        if proc is not None:
            _kill_group(proc)
    try:
        out = stream_path.read_text()
    except OSError:
        out = ""
    return rc, out, err or ""


def drill(mode: str, chain: list) -> dict:
    """Walk `chain` with a no-tool prompt and NO MCP. -> a printable verdict.

    The principal's condition (spec §8): the chain must be SEEN to fire before
    it is armed. This is that: the real CLI, the real chain logic, a prompt
    that can do nothing. Run it with a bogus primary and the fallback event,
    the phone push and the second model's answer are the proof. No lock, no
    brief, no broker read, no session_run event; the journal rows carry
    `drill: true`.
    """
    stream_path = REPO / "logs" / "session_stream.drill.jsonl"
    walk = fallback.run_chain(
        chain,
        lambda m: _spawn_once(_drill_argv(m), DRILL_PROMPT, stream_path, DRILL_TIMEOUT_S),
        classify,
        per_attempt_s=DRILL_TIMEOUT_S, budget_s=DRILL_TIMEOUT_S * len(chain) + 60,
        cli_retry_after_s=models.cli_retry_after_s(),
        on_fallback=lambda frm, to, reason, n: _journal_fallback(
            "session", mode, frm, to, reason, n, drill=True))
    return {"ok": walk["ok"], "drill": True, "mode": mode, "chain": chain,
            "stopped": walk["stopped"],
            "attempts": [{"model": a.model, "rc": a.rc, "reason_class": a.reason_class,
                          "tool_calls": a.tool_calls,
                          "answer": stream_result(a.out)[:60]} for a in walk["attempts"]]}


def run(mode: str, dry_run: bool = False) -> dict:
    """Run one session. -> {"ok", "mode", "error"} (plus detail when it ran)."""
    if mode not in MODES:
        return {"ok": False, "mode": mode, "error": f"unknown mode {mode!r}"}

    install_signal_handlers()

    if dry_run:
        # NOTHING that touches the world: no lock, no spawn, no snapshot. The
        # point is to prove the brief renders, and a dry run that took the lock
        # would block the real session it is being used to debug.
        # The same rebuild the real path runs, with write=False: this proves the
        # startup ordering and the rendered block against live sources without
        # taking the lock or overwriting the artifact a live session may be
        # reading. It is the safe way to inspect what a session would receive.
        try:
            dry_evidence = evidence_build.rebuild(evidence_as_of=session_date(),
                                                  write=False)
            build_error = None
        except Exception as e:                             # noqa: BLE001
            dry_evidence, build_error = None, f"{type(e).__name__}: {e}"
        if build_error:
            print(f"⚠️ INSTITUTIONAL EVIDENCE: rebuild failed ({build_error}) — a "
                  f"real session would deliver NO evidence block", file=sys.stderr)
            block, _prov = "", None
        else:
            block, _prov = evidence_block(evidence=dry_evidence)
        brief = build_brief(mode, evidence=block)
        print(f"brief_bytes: {len(brief)}  evidence_block_bytes: {len(block)}  "
              f"evidence_version: {(dry_evidence or {}).get('version')}")
        print(brief[:400])
        return {"ok": True, "mode": mode, "error": None, "dry_run": True,
                "brief_bytes": len(brief)}

    fh = None
    proc = None
    fill_before = snapshot_freshness.latest_fill_ts(
        REPO / "research_store" / "journal.jsonl")
    started = None
    result = None
    try:
        # ⛔ acquire() RETURNS None ON TIMEOUT -- it does not raise. An earlier
        # form of this guarded `except TimeoutError`, which is dead code: the
        # exception never comes, `fh` was simply None, execution fell through,
        # and the session spawned a full-authority agent WHILE ANOTHER SESSION
        # HELD THE LOCK -- two agents diffing the same book against the same
        # targets, neither aware of the other. `if fh is not None` in the
        # teardown then skipped the release, so nothing logged it, and the run
        # reported ok:true. Check the return value, never the exception.
        fh = session_lock.acquire(mode, LOCK)
        if fh is None:
            return {"ok": False, "mode": mode,
                    "error": (f"lock: {mode} timed out after "
                              f"{session_lock.LOCK_WAIT_S.get(mode)}s waiting for "
                              f"{session_lock.holder(LOCK)} — refusing to run a "
                              f"second session against the same book")}

        # ---- FRESH BROKER FACT -> GOVERNANCE PEAK -------------------------
        # ⛔ FIRST THING AFTER THE LOCK, AND ALWAYS BEFORE THE SPAWN.
        #
        # research_store/governance/state.json's peak_value is the denominator of
        # a HARD ENTRY GATE (the PreToolUse hook's drawdown refusal, via
        # governance.drawdown_breach). Until 2026-08-23 the only thing that ever
        # advanced it was the agent electing to call check_order() -> gates() ->
        # update_peak(): the boundary moved as a side effect of being previewed,
        # and only if the agent happened to look. check_order() is read-only now,
        # so maintaining the peak is infrastructure work and it belongs here,
        # where it is deterministic and happens whether the agent looks or not.
        #
        # AFTER THE LOCK, because the wait can be fifteen minutes and a peak set
        # from a pre-lock reading is a peak from a different market. BEFORE the
        # spawn, because the gate must already be current the first time the
        # agent can attempt a buy.
        #
        # ⛔ AND IT FAILS THE SESSION. Not a warning, not a skip: proceeding would
        # launch a full-authority agent against a drawdown boundary this run knows
        # it could not establish, which is exactly the silent loosening this whole
        # change exists to end. update_peak() is never reached on this path, so a
        # failure LEAVES THE STORED PEAK UNTOUCHED -- it is never lowered, never
        # zeroed, never written from a value that failed validation.
        try:
            account_value = broker_read.read_current_account_value()
        except Exception as e:                              # noqa: BLE001
            return {"ok": False, "mode": mode,
                    "error": (f"broker account read failed ({type(e).__name__}: "
                              f"{e}) — refusing to start a session whose drawdown "
                              f"gate could not be brought up to date. The stored "
                              f"peak is unchanged and no order was placed.")}
        peak = gov.update_peak(account_value)["peak_value"]
        print(f"governance peak maintained: account_value={account_value:.8f} "
              f"peak={peak}")

        # FACTS AFTER THE LOCK. See the module docstring -- the wait can be
        # fifteen minutes, and a brief built before it is that much out of date.
        # ⛔ REBUILD FIRST, RENDER SECOND. This order is the whole point of the
        # step: rendering before rebuilding would hand the agent evidence
        # assembled before its own most recent experience, and then stamp THAT
        # version onto the decisions it took. The session lock is already held,
        # so no second normal session can be rebuilding the same artifact.
        built, build_error = rebuild_evidence()
        if not built:
            # ADVISORY, SO IT DOES NOT STOP THE SESSION -- and NOT a licence to
            # deliver the artifact already on disk, whose freshness for THIS
            # session is exactly what just failed to be established.
            print(f"⚠️ INSTITUTIONAL EVIDENCE: rebuild failed ({build_error}) — "
                  f"this session runs with NO evidence block and NO provenance; "
                  f"the artifact on disk is deliberately NOT delivered",
                  file=sys.stderr)

        # THE EVIDENCE SET IS FIXED HERE, ONCE, AND RECORDED. Everything after
        # this point — including every record_decision the agent makes — reads
        # the receipt, not the artifact, so a mid-session rebuild cannot change
        # what a decision claims the session saw. No block -> no receipt -> the
        # decisions carry no evidence fields at all, which is the truthful state.
        block, delivered = evidence_block() if built else ("", None)
        brief = build_brief(mode, evidence=block)
        evidence_receipt.write(delivered, mode=mode)
        if delivered:
            print(f"evidence delivered: {delivered['evidence_version']} "
                  f"({', '.join(delivered['evidence_ids_seen'])})")

        # Read BEFORE the clock too, same reasoning as `before`/`started` right
        # below: `finally` keys the level check on `started`, so these are read
        # here rather than after, or a NameError in the teardown could skip the
        # tripwire silently the same way an out-of-order `before` once did.
        ov_before = _read_overrides()
        ov_started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # snapshot first, then the clock: `finally` keys the integrity check on
        # `started`, so assigning it first left a window where started was set
        # and `before` was not -- a NameError inside the teardown that skipped
        # the tripwire (caught, so the lock still released, but the check was
        # silently lost).
        before = integrity.snapshot(REPO)
        started = time.time()

        # ⛔ THE BRIEF GOES ON STDIN, NEVER IN argv. `--allowedTools` is
        # VARIADIC, so a trailing prompt argument is consumed as one more tool
        # name and the session starts with no prompt at all: every run exited 1
        # with "Input must be provided either through stdin or as a prompt
        # argument". stdin also sidesteps MAX_ARG_STRLEN (128KiB per argument,
        # not the 2MB ARG_MAX) -- the brief is ~28KB today and grows.
        # ⛔ STDOUT GOES TO A FILE, NOT A PIPE. communicate() only returns once
        # the child exits, so a piped stdout is invisible until the run is over
        # -- which is useless for watching a live session and, worse, gives the
        # operator nothing to look at while real orders are being placed. A file
        # is written as the child produces each line, so it can be tailed. The
        # transcript is read back off disk afterwards, so classify() and the
        # timeout drain below see exactly what they saw before.
        stream_path = REPO / "logs" / f"session_stream.{mode}.jsonl"
        # ⛔ THE CHAIN (config/models.toml, src/fallback.py). One spawn per
        # model, in order, and the next model is tried ONLY when the previous
        # spawn failed with ZERO tool calls in its transcript. A failure after
        # any tool call is ambiguous -- it may have placed an order -- and stops
        # here, exactly as before the chain existed. Each transition is
        # journalled (`model_fallback`) and pushed to the phone.
        per_attempt = TIMEOUT_S.get(mode, 900)
        chain = models.chain("session")
        walk = fallback.run_chain(
            chain,
            lambda m: _spawn_once(_claude_argv(brief, m), brief, stream_path, per_attempt),
            classify,
            per_attempt_s=per_attempt, budget_s=per_attempt * SESSION_CHAIN_BUDGET_X,
            cli_retry_after_s=models.cli_retry_after_s(),
            on_fallback=lambda frm, to, reason, n: _journal_fallback("session", mode, frm, to, reason, n))
        last = walk["attempts"][-1]
        rc, out, err = last.rc, last.out, last.err
        ok, error = last.ok, last.error
        if not ok and walk["stopped"] in ("exhausted", "budget"):
            error = (f"{error} [model chain {walk['stopped']} after "
                     f"{len(walk['attempts'])} attempt(s): "
                     f"{', '.join(a.model for a in walk['attempts'])}]")
        fill_after = snapshot_freshness.latest_fill_ts(
            REPO / "research_store" / "journal.jsonl")
        if fill_after is not None and fill_after != fill_before:
            snap = snapshot_freshness.status(
                REPO / "research_store" / "rh" / "positions.json",
                REPO / "research_store" / "journal.jsonl")
            if snap["stale"]:
                ok = False
                error = ("session recorded a fill but positions.json was not "
                         "refreshed afterward — broker reconciliation required")
        # `retryable` is ADVISORY -- nothing consumes it today (no cron entry,
        # no systemd unit, and run() never retries itself). Before anything acts
        # on it, re-read should_retry: a retry re-runs a session that may have
        # already placed orders, so a wrong True here is an order-duplication
        # bug, not a wasted run.
        #
        # Named (not returned inline) so `finally` below can attach
        # `level_warning` to this SAME dict before it actually returns -- a
        # `return` statement's value is evaluated before `finally` runs, so a
        # mutation there still lands, but only if `finally` has a reference to
        # the object. An inline `return {...}` gives it none.
        result = {"ok": ok, "mode": mode, "error": error,
                  "seconds": round(time.time() - started, 1),
                  "retryable": should_retry(error),
                  "output_bytes": len(out or ""),
                  "launched": True,
                  "model": last.model, "attempts": len(walk["attempts"]),
                  "chain_stopped": walk["stopped"]}
        _journal_session_run(result, out)
        return result

    except KeyboardInterrupt:
        return {"ok": False, "mode": mode,
                "error": f"interrupted ({_INTERRUPTED['why'] or 'INT'})"}
    except Exception as e:      # noqa: BLE001 — the runner reports, never crashes
        return {"ok": False, "mode": mode, "error": f"{type(e).__name__}: {e}"}
    finally:
        # Order matters here too: kill the children BEFORE verifying integrity,
        # or a still-running session can edit a protected file between the check
        # and the release and the tripwire reports clean.
        _kill_group(proc)
        if started is not None:
            try:
                changed = integrity.verify(REPO, before)
                if changed:
                    print(f"⚠️ INTEGRITY: protected files changed during the "
                          f"session: {', '.join(changed)}", file=sys.stderr)
            except Exception as e:      # noqa: BLE001
                print(f"integrity check failed: {e}", file=sys.stderr)
            # A recorded level change with no matching artifact -- SNDK/STX/AMD,
            # 2026-08-12/08-14. Surfaces only; never blocks or retries, and
            # never touches `ok`/`error`/`retryable` above. Wrapped defensively
            # even though its helpers already never raise, because nothing here
            # is allowed to crash a session that has already traded.
            try:
                warn = level_claim_unmet(
                    _decisions_since(REPO / "research_store" / "journal.jsonl",
                                      ov_started_at),
                    ov_before, _read_overrides())
                if warn:
                    print(f"LEVEL WARNING: {warn}")
                    if result is not None:
                        result["level_warning"] = warn
            except Exception as e:      # noqa: BLE001
                print(f"level check failed: {e}", file=sys.stderr)
        # THE RECEIPT DIES WITH THE SESSION. Left behind it could not stamp
        # another session anyway -- the reader demands process ancestry -- but a
        # stale file describing a run that is over is exactly the kind of
        # artifact a later reader mistakes for a live one.
        evidence_receipt.clear()
        if fh is not None:
            session_lock.release(fh)


# --------------------------------------------------------------------------- #
def _selftest() -> None:
    # An error must DOMINATE stdout. `ok = bool(out)` recorded a dead session as
    # successful: a 529 banner went to stdout, so the run logged "ok" and exited
    # 0, systemd said "Finished successfully", and NOTHING registered a failure.
    ok, err = classify(0, "API Error: 529 Overloaded. Retry later.", None)
    assert ok is False and "529" in err, (ok, err)
    ok, err = classify(0, "x" * 700, None)
    assert ok is True and err is None, (ok, err)
    ok, err = classify(1, "boom", None)
    assert ok is False, (ok, err)
    ok, err = classify(0, "", None)
    assert ok is False and "no output" in err, (ok, err)
    # never retry past real work: the session may already have PLACED ORDERS
    assert should_retry("API Error: 529") is True
    assert should_retry("x" * 700) is False

    # ---- FALSE POSITIVES ARE THE DANGEROUS DIRECTION ----------------------
    # These are all sessions that RAN and may have PLACED ORDERS. An earlier
    # form matched _DEAD_SIGNATURES anywhere in stdout -- which is the agent's
    # OWN PROSE -- so each of these failed AND came back retryable, i.e. queued
    # to place its orders a second time. The charter itself says "Observe the
    # data feed's rate limits", so the agent quoting its instructions was enough.
    for legit in (
        "I observed the data feed's rate limits as the charter requires.",
        "Checked the moomoo feed: no rate limit hit, all quotes fresh.",
        "Placed BUY 3 NVDA. Retried once after a transient network error.",
        "The book showed a connection error earlier but recovered; I bought.",
        "The sector is overloaded with semis, so I trimmed.",
        "No API error: 5xx today.",
    ):
        ok, err = classify(0, legit, None)
        assert ok is True, f"legitimate transcript failed: {legit!r} -> {err}"
        assert should_retry(err) is False, legit

    # ...while a real CLI banner on its own line is still caught, at any length
    ok, err = classify(0, "x" * 900 + "\nAPI Error: 529 Overloaded", None)
    assert ok is False and "529" in err, (ok, err)
    ok, err = classify(0, "Error: Connection error (ECONNREFUSED)", None)
    assert ok is False, (ok, err)
    # stderr is CLI diagnostics, not prose -- matched anywhere in it
    ok, err = classify(0, "x" * 900, "authentication_error: invalid token")
    assert ok is False, (ok, err)

    # ---- NO MINIMUM-LENGTH RULE -------------------------------------------
    # `claude -p` prints only the FINAL message. A correct quiet session --
    # regime off, nothing eligible, no action -- is one short sentence, and an
    # earlier 600-byte floor failed it for doing the right thing.
    ok, err = classify(0, "Regime off, nothing eligible. No action taken.", None)
    assert ok is True, (ok, err)

    # a timeout is not retryable: it may have placed orders before hanging
    assert should_retry("timed out after 1800s") is False
    assert should_retry(None) is False
    assert should_retry("exit 0 but no output — the session produced nothing") is False
    assert should_retry("session died: Error: Connection error") is True

    # ---- A LOCK TIMEOUT MUST STOP THE SESSION -----------------------------
    # acquire() RETURNS None on timeout; it does not raise. An `except
    # TimeoutError` guard was dead code, so the session ran UNLOCKED alongside
    # the holder -- two full-authority agents on one book -- and reported ok.
    real_acq, real_rel = session_lock.acquire, session_lock.release
    released, spawned = [], []
    session_lock.acquire = lambda mode, path, timeout_s=None: None
    session_lock.release = lambda fh: released.append(fh)
    real_popen = subprocess.Popen
    subprocess.Popen = lambda *a, **k: spawned.append(a) or (_ for _ in ()).throw(
        AssertionError("SPAWNED A SESSION WITHOUT THE LOCK"))
    try:
        r = run("open")
        assert r["ok"] is False, r
        assert "lock" in r["error"], r
        assert spawned == [], "a lock timeout must not spawn anything"
    finally:
        session_lock.acquire, session_lock.release = real_acq, real_rel
        subprocess.Popen = real_popen

    # an unknown mode is refused rather than run with a missing timeout
    r = run("lunchtime")
    assert r["ok"] is False and "unknown mode" in r["error"], r
    assert set(r) >= {"ok", "mode", "error"}, r

    # ---- THE ARGV MUST NOT SWALLOW THE PROMPT OR DROP THE EMPTY STRING ----
    argv = _claude_argv("THE-BRIEF", "claude-test-model")
    assert "THE-BRIEF" not in argv, "the brief goes on stdin, never in argv"
    assert argv[argv.index("--model") + 1] == "claude-test-model", "the model is the parameter"
    assert "" in argv, "the empty-string argument to --tools was lost"
    i = argv.index("--tools")
    assert argv[i + 1] == "", f"--tools must be followed by the empty string: {argv[i:i+3]}"
    assert "--permission-mode" in argv and argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[-1] != "--allowedTools", "allowlist must not be empty"

    # ⛔ THE MCP SURFACE MUST BE DECLARED, NOT INHERITED. Without
    # --strict-mcp-config a headless run also loads /root/.claude.json and every
    # claude.ai account connector; on 2026-08-13 that killed the open session
    # with "Autocompact is thrashing" (~54KB of tool definitions it could never
    # call). A connected server costs context whether or not it is allowlisted.
    assert "--strict-mcp-config" in argv, (
        "the session would inherit the user's MCP config and every claude.ai "
        "connector — see deploy/session_tools.sh and OPSLOG 2026-08-13")
    _cfg = Path(argv[argv.index("--mcp-config") + 1])
    assert _cfg.is_file(), f"--mcp-config points at nothing: {_cfg}"
    # ...and it must declare EXACTLY the two servers the session uses. Adding a
    # third is a real decision about the context budget, so it has to be made here.
    _declared = set(json.loads(_cfg.read_text())["mcpServers"])
    assert _declared == {"agentic-trader", "robinhood-trading"}, (
        f"unexpected MCP surface {sorted(_declared)} — every connected server "
        f"costs context in every session; add one only deliberately")
    # --allowedTools is variadic: anything after it is eaten as a tool name.
    assert all(t.startswith("mcp__") for t in argv[argv.index("--allowedTools") + 1:]), (
        "a flag was placed after --allowedTools and will be swallowed as a tool")

    # ⛔ THE CONTEXT ISOLATION MUST STILL BE ON THE CHILD'S ENVIRONMENT.
    # Asserted here because it is invisible in the argv, in the settings file
    # and in the systemd unit -- there is nowhere else a reader would look and
    # notice it had been dropped. Both must be present AND truthy: the CLI
    # treats "0"/"false"/"no"/"off" as "leave the feature on".
    _env = _claude_env()
    for _k in ("CLAUDE_CODE_DISABLE_CLAUDE_MDS", "CLAUDE_CODE_DISABLE_AUTO_MEMORY"):
        assert _env.get(_k) == "1", (
            f"{_k} is not set to 1 on the session's environment — the trading "
            f"session would inherit CLAUDE.md / auto-memory as cross-session "
            f"state, around the institutional-evidence artifact")

    # ⏱ THE SESSION + ITS REVIEW MUST STILL FIT THE AFTERNOON.
    # This used to assert against risk_review at 15:45 — that overlay was
    # RETIRED 2026-08-13, so the 30-minute ceiling it justified is gone with it.
    # What remains: close starts 15:15, and log_equity runs at 16:15 and wants a
    # settled book. 15:15 + 900s session + 1200s review = 15:50 clears it.
    # The budget stays asserted rather than left to whoever edits these next,
    # because two headless model runs overlapping on this ~2GB box forced a
    # reboot on 2026-08-12 — the review being SEQUENTIAL is what prevents that,
    # and a budget that overran would be the way it stopped being sequential.
    import importlib.util as _il
    _sp = _il.spec_from_file_location("_rev", REPO / "scripts" / "review_session.py")
    _rev = _il.module_from_spec(_sp)
    _sp.loader.exec_module(_rev)
    _budget = TIMEOUT_S["close"] + _rev.TIMEOUT_S
    assert _budget <= 60 * 60, (
        f"close ({TIMEOUT_S['close']}s) + review ({_rev.TIMEOUT_S}s) = {_budget}s "
        f"exceeds the 15:15->16:15 window before log_equity")

    # ---- the verdict is DISCONNECTED from the agent, and the renderer is kept
    #      alive underneath it -----------------------------------------------
    # Two separate claims, tested separately on purpose:
    #   (a) as shipped, no verdict reaches the agent by ANY mode or stance;
    #   (b) with the flag forced on, the renderer still behaves correctly.
    # (b) is what makes reconnecting a one-line flip instead of a rewrite: the
    # anonymity guard, the mode filter and the AFFIRM rule cannot rot while
    # switched off, because they are still exercised on every selftest run.
    import unittest.mock as _mock
    rev = {"stance": "SPLIT", "headline": "Idle cash was not justified.",
           "what_i_would_have_done": "Deployed the $4.64 into FTNT.",
           "strongest_disagreement": "$4.64 was 91% of a full position, not a stub.",
           "what_would_change_my_mind": "A liquidity or event fact about FTNT."}
    # (a) AS SHIPPED: nothing reaches the agent, whatever the stance or mode.
    assert SHOW_REVIEW_TO_AGENT is False, (
        "SHOW_REVIEW_TO_AGENT was flipped on. That is a real decision, not a "
        "tidy-up -- read the block above it, and name the review process in the "
        "charter before letting a verdict reach the agent again.")
    for st in ("SPLIT", "DISSENT", "AFFIRM", "UNPARSED"):
        for m in ("open", "close", "wake", "premarket"):
            assert render_review({**rev, "stance": st}, m) == "", (st, m)
    with _mock.patch.object(sys.modules[__name__], "last_review", lambda: rev), \
         _mock.patch.object(sys.modules[__name__], "_tool_names", lambda: ["mcp__x__y"]):
        for m in ("open", "close", "wake", "premarket"):
            assert "INDEPENDENT REVIEW" not in build_brief(m), m
            assert "91% of a full position" not in build_brief(m), m

    # (b) THE RENDERER UNDERNEATH, forced on. Everything here is the contract
    #     that must still hold on the day the flag goes back to True.
    with _mock.patch.object(sys.modules[__name__], "SHOW_REVIEW_TO_AGENT", True):
        for m in ("open", "close"):
            blk = render_review(rev, m)
            assert "SPLIT" in blk and "91% of a full position" in blk, m
            assert "You must answer this" in blk, "a verdict the agent may ignore is decoration"
            # ⛔ THE REVIEWER'S IDENTITY MUST NOT LEAK. Told the source, the agent
            # reasons about the source -- discounting a critic it decides does not
            # understand momentum, or deferring to one it decides is objective.
            low = blk.lower()
            for leak in ("codex", "openai", "gpt", "different model", "another model",
                         "does not share your priors", "anthropic", "claude", "gemini"):
                assert leak not in low, f"reviewer identity leaked to the agent: {leak!r}"
        # ...but NOT to a wake (it fires on a price condition and must act) and not
        # to premarket. The risk-review overlay never reads a brief at all.
        for m in ("wake", "premarket"):
            assert render_review(rev, m) == "", m
        # no review yet is normal on a fresh deploy, not an error
        assert render_review(None, "open") == ""

        # an AFFIRM is shown too -- telling the agent only when it was wrong trains
        # it toward the reviewer's taste rather than toward being right
        aff = render_review({**rev, "stance": "AFFIRM"}, "open")
        assert "AFFIRM" in aff and "It agreed with you" in aff, aff
        assert "You must answer this" not in aff, "an affirmation is not a demand"

        # and it lands in the brief the agent receives
        with _mock.patch.object(sys.modules[__name__], "last_review", lambda: rev), \
             _mock.patch.object(sys.modules[__name__], "_tool_names", lambda: ["mcp__x__y"]):
            b = build_brief("open")
            assert "INDEPENDENT REVIEW" in b, "the verdict never reached the brief"
            assert "91% of a full position" in b, b[-800:]
            assert "INDEPENDENT REVIEW" not in build_brief("wake")

    # _kill_group on a dead/None process is a no-op, not an exception
    _kill_group(None)

    class _Dead:
        pid = -1
        def poll(self):  # noqa: D102
            return 0
    _kill_group(_Dead())

    # ---- A CLAIMED LEVEL CHANGE WITH NO MATCHING ARTIFACT MUST SURFACE -----
    # A session that RECORDS a level change and leaves overrides.json untouched
    # has not made one. On 2026-08-12 and 08-14 sessions recorded stop
    # tightenings on SNDK/STX/AMD; overrides.json never existed, so none took
    # effect -- and the 08-16 investor letter reported the de-risking as done.
    d = [{"event": "agent_decision", "action": "tighten_stops", "symbol": "PORTFOLIO"}]
    assert level_claim_unmet(d, {}, {}) is not None
    assert "tighten_stops" in level_claim_unmet(d, {}, {})
    # ...but not when the file actually changed
    assert level_claim_unmet(d, {}, {"SNDK": {"stop": 1.0}}) is None
    # ...and not when no level decision was recorded
    assert level_claim_unmet([{"event": "agent_decision", "action": "hold"}], {}, {}) is None

    # session_run: the failure CLASS is derived from the runner's error and the
    # CLI's own result line, never from agent prose (2026-09-03 usage limit).
    stream = "\n".join([
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "no rate limit hit, placed the BUY"},
            {"type": "tool_use", "name": "mcp__agentic-trader__brief"},
            {"type": "tool_use", "name": "mcp__robinhood-trading__place_equity_order"}]}}),
        json.dumps({"type": "result", "is_error": True,
                    "result": "You've hit your limit · resets 4:40pm (America/New_York)"}),
    ])
    assert stream_result(stream).startswith("You've hit your limit"), stream_result(stream)
    assert stream_result("") == "" and stream_result("not json") == ""
    assert stream_counts(stream) == (2, 1), stream_counts(stream)
    assert reason_class(None) is None
    assert reason_class("exit 1: {\"usage\": 5781}", stream_result(stream)) == "usage_limit"
    assert reason_class("session died: api error: 529 overloaded") == "model_outage"
    assert reason_class("exit 1: x", "API Error: 400 claude_code_version_too_old") == "version_too_old"
    assert reason_class("exit -1: timed out after 900s") == "timeout"
    assert reason_class("refusing to run a second session") == "not_launched"
    assert reason_class("something new") == "ambiguous"
    # the brief block: nothing -> ""; a missed session -> its row, the legend
    # for ITS class only, and the standing instruction; all-ran -> no legend.
    assert render_session_history([]) == ""
    ok_run = {"event": "session_run", "ts": "2026-09-03T16:01:00+00:00", "mode": "open",
              "ok": True, "seconds": 375.1, "tool_calls": 40, "orders": 2}
    missed = {"event": "session_run", "ts": "2026-09-03T19:16:36+00:00", "mode": "close",
              "ok": False, "reason_class": "usage_limit", "seconds": 86.4,
              "tool_calls": 11, "orders": 0}
    blk = render_session_history([ok_run, missed, {"event": "agent_decision"}])
    assert "- 2026-09-03 close — MISSED: usage_limit (86.4s, 11 tool calls, 0 orders)" in blk, blk
    assert "- 2026-09-03 open  — ran (375.1s, 40 tool calls, 2 orders)" in blk, blk
    assert blk.index("close — MISSED") < blk.index("open  — ran"), "newest first"
    assert "usage_limit — the operator's Claude subscription cap" in blk, blk
    assert "model_outage —" not in blk, "legend lists only the classes present"
    assert "do not diagnose it" in blk and blk.endswith("\n\n---\n\n"), blk
    assert "What the classes mean" not in render_session_history([ok_run])
    # only the newest N are shown
    many = [dict(ok_run, ts=f"2026-08-{d:02d}T16:00:00+00:00") for d in range(1, 10)]
    assert render_session_history(many).count("- 2026-08-") == SESSION_HISTORY_N
    # the drill spawn can reach NO tool and NO broker: empty MCP file, no built-ins
    d = _drill_argv("claude-test-model")
    assert d[d.index("--model") + 1] == "claude-test-model"
    assert d[d.index("--tools") + 1] == "" and "--strict-mcp-config" in d
    assert json.loads(DRILL_MCP.read_text())["mcpServers"] == {}, DRILL_MCP
    assert "--allowedTools" not in d, "a drill grants nothing"

    print("session: OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", nargs="?", choices=[*MODES, "selftest"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--drill", action="store_true",
                    help="walk the model chain with a no-tool prompt: no lock, no "
                         "brief, no broker, no MCP. Proves the chain, not the session.")
    ap.add_argument("--drill-chain", default="",
                    help="comma-separated model ids for --drill (default: the "
                         "configured session chain); a bogus first id is the test")
    a = ap.parse_args()

    if a.selftest or a.mode == "selftest":
        _selftest()
        raise SystemExit(0)
    if not a.mode:
        ap.error("a mode is required")
    if a.drill:
        chain = [m.strip() for m in a.drill_chain.split(",") if m.strip()] or models.chain("session")
        result = drill(a.mode, chain)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["ok"] else 1)

    result = run(a.mode, dry_run=a.dry_run)
    # A session that never reached the spawn (lock wait, HALT, broker read
    # refused, an exception) still leaves its verdict in the journal.
    if not result.get("launched") and not result.get("dry_run"):
        _journal_session_run(result, "")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
