"""SHA-256 tripwire over the files a session must never modify.

Capability removal (deploy/session_tools.sh, `--tools ""`) is the CONTROL. This
is the proof it held.

⚠️ DETECTIVE, NOT PREVENTIVE, AND IT CANNOT ATTRIBUTE CAUSE. A hash comparison
shows only THAT a file changed, never WHO changed it -- an operator editing
governance.py mid-session trips it identically. Halting is correct either way (a
run whose guardrails moved underneath it is not a valid run), but the message
must state what is known and no more. Ship mtimes as evidence; never accuse.

Adapted from /opt/trading/watcher/session.py:53-92, where the same tripwire
exists because a live session hit a gate rejection and went straight to reading
its own guardrail. It chose not to edit it; nothing would have stopped it.
"""
from __future__ import annotations

import hashlib
import pathlib

PROTECTED_FILES = (
    "src/governance.py",
    "src/mandate.py",
    "config/mandate.toml",
    "config/strategy.toml",
    "src/agent_env/server.py",
    "deploy/session_tools.sh",
    "CLAUDE.md",
)


def snapshot(root: pathlib.Path, files=PROTECTED_FILES) -> dict:
    """SHA-256 of every protected file. Unreadable -> a sentinel, not omission."""
    out = {}
    for rel in files:
        p = pathlib.Path(root) / rel
        try:
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError as e:
            out[rel] = f"UNREADABLE: {type(e).__name__}"
    return out


def verify(root: pathlib.Path, before: dict, files=PROTECTED_FILES) -> list:
    """Protected files whose contents changed. Empty is good."""
    after = snapshot(root, files=files)
    return sorted(p for p in before if before[p] != after.get(p))


def _selftest() -> None:
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        (root / "src").mkdir()
        p = root / "src" / "governance.py"
        p.write_text("original\n")
        before = snapshot(root, files=("src/governance.py",))
        assert verify(root, before, files=("src/governance.py",)) == []
        p.write_text("tampered\n")
        assert verify(root, before, files=("src/governance.py",)) == ["src/governance.py"]
        # an unreadable file counts as CHANGED, never as clean
        p.unlink()
        assert verify(root, before, files=("src/governance.py",)) == ["src/governance.py"]
    print("integrity: OK")


if __name__ == "__main__":
    _selftest()
