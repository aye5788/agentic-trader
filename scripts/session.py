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
# placed. Matched case-insensitively against stdout+stderr. Deliberately narrow:
# a false positive here retries a session that may have ALREADY PLACED ORDERS.
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

# Below this, output is a banner or a stub rather than a session transcript.
_MIN_OUTPUT = 600


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
    blob = f"{out or ''}\n{err or ''}"
    low = blob.lower()

    if rc != 0:
        tail = (err or out or "").strip()[-300:] or "no output"
        return False, f"exit {rc}: {tail}"

    for sig in _DEAD_SIGNATURES:
        if sig in low:
            i = low.find(sig)
            return False, f"session died: ...{blob[max(0, i - 60):i + 160].strip()}..."

    if not (out or "").strip():
        return False, "exit 0 but no output — the session produced nothing"

    if len(out.strip()) < _MIN_OUTPUT:
        return False, (f"exit 0 but only {len(out.strip())} bytes of output "
                       f"(under {_MIN_OUTPUT}) — a banner, not a session")

    return True, None


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
    args = [ln for ln in probe.stdout.splitlines() if ln.strip()]
    if not args:
        raise RuntimeError("session_tools.sh produced no tool args — refusing "
                           "to start a session with an unknown tool surface")
    return ["claude", "-p", "--model", "claude-opus-5", *args, brief]


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
        try:
            fh = session_lock.acquire(mode, LOCK)
        except TimeoutError as e:
            return {"ok": False, "mode": mode, "error": f"lock: {e}"}

        # FACTS AFTER THE LOCK. See the module docstring -- the wait can be
        # fifteen minutes, and a brief built before it is that much out of date.
        brief = build_brief(mode)

        started = time.time()
        before = integrity.snapshot(REPO)

        proc = subprocess.Popen(
            _claude_argv(brief), cwd=str(REPO),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)     # own group, so _kill_group reaches the tree
        try:
            out, err = proc.communicate(timeout=TIMEOUT_S.get(mode, 900))
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            out, err = "", f"timed out after {TIMEOUT_S.get(mode, 900)}s"
            rc = -1

        ok, error = classify(rc, out, err)
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

    # a long transcript that CONTAINS a death banner is still dead -- the
    # signature check runs before the length check for exactly this case
    ok, err = classify(0, "x" * 900 + "\nAPI Error: 529 Overloaded", None)
    assert ok is False and "529" in err, (ok, err)
    # ...and a transcript that merely DISCUSSES an error is not one. The agent
    # writing "I checked for a rate limit" must not fail its own session.
    ok, err = classify(0, "I considered whether a 529 was possible. " + "x" * 700, None)
    assert ok is True, (ok, err)

    # a short transcript is a banner, not a session
    ok, err = classify(0, "done.", None)
    assert ok is False and "banner" in err, (ok, err)
    # stderr carries signatures too (they do not always reach stdout)
    ok, err = classify(0, "x" * 900, "authentication_error: invalid token")
    assert ok is False, (ok, err)
    # a timeout is not retryable: it may have placed orders before hanging
    assert should_retry("timed out after 1800s") is False
    assert should_retry(None) is False
    assert should_retry("exit 0 but no output — the session produced nothing") is False
    # transport failures ARE retryable — the model never ran
    assert should_retry("session died: ...Connection error...") is True

    # an unknown mode is refused rather than run with a missing timeout
    r = run("lunchtime")
    assert r["ok"] is False and "unknown mode" in r["error"], r
    assert set(r) >= {"ok", "mode", "error"}, r

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
