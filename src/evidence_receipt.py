"""THE DELIVERY RECEIPT — what evidence THIS session actually received.

WHY A RECEIPT AND NOT A LOOKUP
    Provenance has to name the evidence set the session was HANDED, not the one
    lying on disk when a tool call happens to run. Those differ the moment an
    operator rebuilds the artifact mid-session, and a decision stamped with the
    newer version would claim the agent saw numbers that did not exist when it
    reasoned. So the version is captured ONCE, at context construction, and
    every later read is of that capture.

WHY A FILE AND NOT A VARIABLE
    The session runner (scripts/session.py) renders the block; `record_decision`
    runs inside the MCP server, which the `claude` CLI starts as a SEPARATE
    PROCESS (.mcp.json). No in-process value can span the two. This receipt is
    the smallest thing that can: written by the runner once the session lock is
    held, removed by the same runner in its `finally`.

WHY IT CANNOT LEAK INTO ANOTHER SESSION
    A receipt is honoured only by a process that can PROVE it descends from the
    session runner that wrote it — the recorded pid must appear in this process's
    own /proc parent chain. An interactive `claude`, the monitor's exit executor
    and a second box are all outside that chain, so a receipt left behind by a
    crashed runner cannot stamp any of them. No proof, no provenance: the fields
    are omitted, which is the same truthful state as "no block was delivered".

⛔ NEVER WRITE THESE FIELDS AS NULL
    Absent means "this session was handed no evidence block". Null would mean
    "it was handed one and it was empty". Only the first is ever true here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PATH = REPO / "research_store" / "session" / "evidence_delivered.json"

#: Depth cap on the parent walk. A chain longer than this is a pathological
#: process tree, not a session, and looping forever on it is worse than refusing.
MAX_DEPTH = 64


def _ppid(pid: int):
    """The parent of `pid` from /proc, or None. Never raises.

    ⚠️ Parsed after the LAST ')' on purpose: field 2 is the executable name in
    parentheses and may itself contain spaces and parentheses, so splitting the
    whole line puts ppid in a different column for some processes.
    """
    try:
        text = Path(f"/proc/{int(pid)}/stat").read_text()
    except (OSError, ValueError):
        return None
    try:
        rest = text[text.rindex(")") + 1:].split()
        return int(rest[1])              # state, ppid
    except (ValueError, IndexError):
        return None


def process_chain(pid=None) -> list:
    """[pid, parent, grandparent, ...] up to init. Empty when /proc is unreadable."""
    current = os.getpid() if pid is None else int(pid)
    chain = [current]
    for _ in range(MAX_DEPTH):
        nxt = _ppid(current)
        if nxt is None or nxt <= 0 or nxt in chain:
            break
        chain.append(nxt)
        current = nxt
    return chain


def write(provenance, *, mode: str, session_pid=None, path=None) -> bool:
    """Record that a session was handed this evidence set. -> wrote anything.

    `provenance` is None when no block was delivered; the receipt is then CLEARED
    rather than written empty, so the absence of the file is the single
    representation of "no evidence reached this session".
    """
    target = Path(PATH if path is None else path)
    if not provenance:
        clear(target)
        return False
    payload = {
        "session_pid": int(session_pid if session_pid is not None else os.getpid()),
        "session_mode": str(mode),
        "evidence_version": provenance["evidence_version"],
        "evidence_ids_seen": list(provenance["evidence_ids_seen"]),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, target)
    return True


def clear(path=None) -> None:
    """Remove the receipt. Idempotent, and never raises — a session that has
    already traded must not fail in teardown over bookkeeping.

    ⚠️ `path=None` resolves to the module-level PATH AT CALL TIME, never as a
    default argument value — a default binds once at import and would silently
    ignore a redirected PATH, which is exactly how a test writes to the live
    file while believing it is writing to a scratch one."""
    try:
        Path(PATH if path is None else path).unlink()
    except (OSError, ValueError):
        pass


def read(path=None, pid=None):
    """The provenance THIS process may claim, or None. Never raises.

    Returns exactly `{"evidence_version": str, "evidence_ids_seen": [str, ...]}`
    — the two optional fields a decision may carry — or None when there is no
    receipt, when it is unreadable, when it is malformed, or when this process
    cannot prove it descends from the session that wrote it.
    """
    try:
        payload = json.loads(Path(PATH if path is None else path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    version = payload.get("evidence_version")
    ids = payload.get("evidence_ids_seen")
    session_pid = payload.get("session_pid")
    if not isinstance(version, str) or not version.strip():
        return None
    if not isinstance(ids, list) or not ids or not all(
            isinstance(i, str) and i.strip() for i in ids):
        return None
    if not isinstance(session_pid, int):
        return None
    if session_pid not in process_chain(pid):
        # Not our session. Silent by design: this is the normal state for an
        # interactive session and for the monitor's exit executor, both of which
        # correctly receive no evidence and must record none.
        return None
    return {"evidence_version": version, "evidence_ids_seen": list(ids)}


def _selftest() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "evidence_delivered.json"

        assert read(p) is None, "a missing receipt must read as no provenance"

        prov = {"evidence_version": "ev1-abc", "evidence_ids_seen": ["a:b"]}
        assert write(prov, mode="open", path=p) is True
        got = read(p)
        assert got == prov, got
        assert set(got) == {"evidence_version", "evidence_ids_seen"}, got

        # a receipt from a pid that is NOT an ancestor is refused
        payload = json.loads(p.read_text())
        payload["session_pid"] = 999999999
        p.write_text(json.dumps(payload))
        assert read(p) is None, "a foreign session's receipt must not be honoured"

        # ancestry, not equality: the parent of this process must also work
        chain = process_chain()
        assert chain[0] == os.getpid()
        if len(chain) > 1:
            payload["session_pid"] = chain[1]
            p.write_text(json.dumps(payload))
            assert read(p) == prov, "an ancestor's receipt must be honoured"

        for bad in ({"session_pid": os.getpid(), "evidence_ids_seen": ["a"]},
                    {"session_pid": os.getpid(), "evidence_version": "v"},
                    {"session_pid": os.getpid(), "evidence_version": "v",
                     "evidence_ids_seen": []},
                    {"session_pid": os.getpid(), "evidence_version": "",
                     "evidence_ids_seen": ["a"]},
                    {"evidence_version": "v", "evidence_ids_seen": ["a"]},
                    ["not", "an", "object"]):
            p.write_text(json.dumps(bad))
            assert read(p) is None, f"malformed receipt honoured: {bad}"

        p.write_text("{not json")
        assert read(p) is None, "unreadable receipt honoured"

        # None provenance CLEARS rather than writing an empty receipt
        write(prov, mode="open", path=p)
        assert p.exists()
        assert write(None, mode="open", path=p) is False
        assert not p.exists(), "no-block must leave NO receipt, not an empty one"

        clear(p)        # idempotent
        clear(p)
    print("evidence_receipt: OK")


if __name__ == "__main__":
    _selftest()
