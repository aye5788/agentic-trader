"""One full-authority session at a time. fcntl.flock, not a PID file.

flock is held by the open file description, so the KERNEL releases it when the
holder dies however it dies -- SIGKILL, OOM, panic. There is no stale-lock
reaper here because there is nothing to reap. The lockfile CONTENTS ("<mode>
<pid>") are only ever a human-readable log line, never a liveness decision.

⚠️ os.open(O_RDWR|O_CREAT), NOT open(path, "w"). "w" TRUNCATES on open, so every
waiter erased the holder's identity before it had even tried the flock, and the
holder then read as "unknown" for a lock that was very much held.

⚠️ Never sleep PAST the deadline: a flat sleep(3) made a 1s timeout wait 3s and
succeed, which made the give-up path untestable.

Scheduled sessions WAIT; wakes YIELD. A scheduled session is the primary event
-- starting it late is enormously better than not starting it. Values stay far
inside the session budget so waiting can never consume the time the session
needs to work.

Not covered: this only prevents two IN-PROCESS-REACHABLE sessions on the SAME
machine from holding the lock at once (flock is local to a host and, on some
network filesystems, not even that -- this repo's lockfile lives on local
disk). It says nothing about correctness once a session holds the lock; it
only ever answers "is anyone else in here right now."
"""
from __future__ import annotations

import os
import time
from pathlib import Path

LOCK_WAIT_S = {"premarket": 900, "open": 1800, "close": 900, "wake": 120}
DEFAULT_LOCK_WAIT_S = 120


def holder(path: Path) -> str:
    try:
        return Path(path).read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def acquire(mode: str, path: Path, timeout_s=None):
    import fcntl                              # noqa: PLC0415
    if timeout_s is None:
        timeout_s = LOCK_WAIT_S.get(mode, DEFAULT_LOCK_WAIT_S)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    deadline = t0 + timeout_s
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    fh = os.fdopen(fd, "r+")
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.seek(0)
            fh.truncate()
            fh.write(f"{mode} {os.getpid()}\n")
            fh.flush()
            return fh
        except OSError:
            now = time.time()
            if now >= deadline:
                fh.close()
                return None
            time.sleep(min(3.0, deadline - now))


def release(fh) -> None:
    import fcntl                              # noqa: PLC0415
    try:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
    except Exception:                         # noqa: BLE001
        pass


def _selftest() -> None:
    import tempfile, pathlib, os
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "session.lock"
        fh = acquire("open", p, timeout_s=1)
        assert fh is not None
        assert f"open {os.getpid()}" in holder(p)
        # a second acquire must GIVE UP, not hang and not succeed
        t0 = time.time()
        second = acquire("wake", p, timeout_s=1)
        assert second is None, "two sessions must never hold the lock at once"
        assert time.time() - t0 < 3.0, "must not sleep past its own deadline"
        release(fh)
        third = acquire("wake", p, timeout_s=1)
        assert third is not None, "lock must be reusable after release"
        release(third)
    # scheduled modes wait, wakes yield
    assert LOCK_WAIT_S["open"] >= 900
    assert LOCK_WAIT_S["wake"] <= 300
    assert LOCK_WAIT_S["open"] > LOCK_WAIT_S["wake"] * 4
    print("session_lock: OK")


if __name__ == "__main__":
    _selftest()
