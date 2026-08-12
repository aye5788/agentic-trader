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
import strategy                     # noqa: E402

MODES = ("premarket", "open", "close", "wake")
LOCK = REPO / "research_store" / "session.lock"

# The session's own wall-clock ceiling. A hung `claude -p` holds the lock, so
# every LATER session is blocked behind it -- one wedged process silently ends
# the trading day rather than just its own slot. Generous, because a session
# that is merely slow must not be killed mid-order.
TIMEOUT_S = {"premarket": 900, "open": 1800, "close": 1200, "wake": 600}

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


def build_brief(mode: str) -> str:
    """Render the charter for this session. Called AFTER the lock is held."""
    text = charter.render(mandate_mod.load(), strategy.load(), _tool_names())
    stamp = datetime.now(timezone.utc).astimezone()
    return (f"{text}\n\n---\n\nTHIS SESSION: **{mode}**, "
            f"{stamp.strftime('%A %Y-%m-%d %H:%M %Z')}.\n")


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def _claude_argv(brief: str) -> list[str]:
    """The command line, with the tool surface sourced from session_tools.sh.

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
    return ["claude", "-p", "--model", "claude-opus-5", *args]


def run(mode: str, dry_run: bool = False) -> dict:
    """Run one session. -> {"ok", "mode", "error"} (plus detail when it ran)."""
    if mode not in MODES:
        return {"ok": False, "mode": mode, "error": f"unknown mode {mode!r}"}

    install_signal_handlers()

    if dry_run:
        # NOTHING that touches the world: no lock, no spawn, no snapshot. The
        # point is to prove the brief renders, and a dry run that took the lock
        # would block the real session it is being used to debug.
        brief = build_brief(mode)
        print(f"brief_bytes: {len(brief)}")
        print(brief[:400])
        return {"ok": True, "mode": mode, "error": None, "dry_run": True,
                "brief_bytes": len(brief)}

    fh = None
    proc = None
    started = None
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

        # FACTS AFTER THE LOCK. See the module docstring -- the wait can be
        # fifteen minutes, and a brief built before it is that much out of date.
        brief = build_brief(mode)

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
        proc = subprocess.Popen(
            _claude_argv(brief), cwd=str(REPO),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)     # own group, so _kill_group reaches the tree
        try:
            out, err = proc.communicate(input=brief,
                                        timeout=TIMEOUT_S.get(mode, 900))
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            # DRAIN AFTER KILLING. The first form discarded everything the
            # session had printed -- and a session that hung is the one MOST
            # likely to have placed orders and most in need of a transcript to
            # reconcile against. communicate() returns once the pipes close,
            # which the kill guarantees.
            _kill_group(proc)
            try:
                out, err = proc.communicate(timeout=10)
            except Exception:       # noqa: BLE001
                out, err = "", ""
            err = (err or "") + f"\ntimed out after {TIMEOUT_S.get(mode, 900)}s"
            rc = -1

        ok, error = classify(rc, out, err)
        # `retryable` is ADVISORY -- nothing consumes it today (no cron entry,
        # no systemd unit, and run() never retries itself). Before anything acts
        # on it, re-read should_retry: a retry re-runs a session that may have
        # already placed orders, so a wrong True here is an order-duplication
        # bug, not a wasted run.
        return {"ok": ok, "mode": mode, "error": error,
                "seconds": round(time.time() - started, 1),
                "retryable": should_retry(error),
                "output_bytes": len(out or "")}

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
    argv = _claude_argv("THE-BRIEF")
    assert "THE-BRIEF" not in argv, "the brief goes on stdin, never in argv"
    assert "" in argv, "the empty-string argument to --tools was lost"
    i = argv.index("--tools")
    assert argv[i + 1] == "", f"--tools must be followed by the empty string: {argv[i:i+3]}"
    assert "--permission-mode" in argv and argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[-1] != "--allowedTools", "allowlist must not be empty"

    # _kill_group on a dead/None process is a no-op, not an exception
    _kill_group(None)

    class _Dead:
        pid = -1
        def poll(self):  # noqa: D102
            return 0
    _kill_group(_Dead())

    print("session: OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", nargs="?", choices=[*MODES, "selftest"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest or a.mode == "selftest":
        _selftest()
        raise SystemExit(0)
    if not a.mode:
        ap.error("a mode is required")

    result = run(a.mode, dry_run=a.dry_run)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
