#!/usr/bin/env python3
"""READ-ONLY view of everything the trading agent can see. For the REVIEWER.

    .venv/bin/python scripts/agent_view.py <tool> [arg=value ...]
    .venv/bin/python scripts/agent_view.py --list

WHY THIS EXISTS
    The reviewer is a DIFFERENT MODEL (Codex) deliberately, because the trading
    agent and the assistant that built it are the same model and share the same
    priors. A reviewer that shares the bias cannot see it: when the agent
    rationalised trimming winners on "concentration", the same-model reviewer
    read the argument and found it persuasive, because it is the argument that
    model produces.

    So the reviewer needs the SAME VIEW, not a summary written by the thing it
    is reviewing. A summary is the reviewed party choosing the evidence.

WHY NOT JUST GIVE IT THE MCP SERVER
    Codex gates third-party MCP tool calls behind an approval its headless mode
    auto-cancels ("user cancelled MCP tool call"). Rather than defeat that
    guard, this exposes the same functions over a plain CLI the sandbox already
    permits. Same code path, same numbers -- server.py's own functions are
    called, nothing is reimplemented here, so the reviewer cannot be shown a
    different book than the agent saw.

⛔ READ-ONLY BY CONSTRUCTION. The allowlist below contains no tool that writes:
   no set_levels, no record_decision, no rule_out, no place_equity_order. The
   reviewer's job is to judge, and a reviewer that can trade is a second trader.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "agent_env"))

# Every tool here must be free of side effects. Adding a writer to this list
# turns the reviewer into a second trading agent -- which is the one thing it
# must never become.
READ_ONLY = {
    "brief", "account", "positions", "halt_status", "mandate_status",
    "universe", "candidates", "terrain", "quote", "sectors", "leaders",
    "macro", "macro_calendar", "earnings", "news", "depth",
    "performance", "research_log", "check_order",
}


# --------------------------------------------------------------------------- #
# PERSISTENT SERVICE + THIN CLIENT
#
# The reviewer's own recommendation, after it diagnosed the cost problem it
# lives with: "a persistent, read-only local service plus a thin CLI client,
# with a heterogeneous batch endpoint. Load pandas once, keep one bounded
# server process."
#
# Before: every invocation exec'd server.py -> pandas + numpy + FastMCP,
# ~134 MB and ~2.3 s, even for the 13 of 19 tools that touch no dataframe.
# 120 concurrent on 2026-08-14 took a 1963 MB box to 44 MB free during market
# hours, with a software-only stop watcher on a live book.
#
# After: the FIRST call starts a daemon that pays the import once; every call
# after it is a socket round-trip from a client that never imports pandas at
# all. The daemon exits on its own after IDLE_EXIT_SECS, so it costs nothing
# between reviews.
#
# ⛔ THE ALLOWLIST IS ENFORCED IN THE DAEMON, not just in the client. A client
# is just a process anyone can write; the process holding the loaded server is
# the only place the read-only guarantee can actually live.
#
# ⚠️ NOTHING IS CACHED. Each request re-invokes the tool, which re-reads the
# book, the journal and the panel from disk. A reviewer served a cached number
# would be judging a stale book -- worse than a slow reviewer, because it looks
# right. Only the IMPORT is reused.
SOCKET = Path(os.environ.get(
    "AGENT_VIEW_SOCKET", REPO / "research_store" / "locks" / "agent_view.sock"))
IDLE_EXIT_SECS = float(os.environ.get("AGENT_VIEW_IDLE", "900"))
SPAWN_WAIT_SECS = float(os.environ.get("AGENT_VIEW_SPAWN_WAIT", "60"))

_SERVER = None


def _server():
    """Load server.py once per process, then reuse it.

    ⚠️ THIS IMPORT IS THE WHOLE COST. It pulls pandas and numpy (directly, and
    again via agent_env.screen -> momentum) plus the FastMCP/Pydantic stack:
    ~134 MB peak RSS, measured on this box 2026-08-14, for every invocation --
    even for the 13 of 19 tools that touch no dataframe at all.

    That was survivable at one call. On 2026-08-14 a review issued 120 of them
    concurrently and the box went from 1963 MB to 44 MB free during market
    hours, with a software-only stop watcher as the sole protection on a live
    book. The reviewer was killed to save the machine.

    Caching here is what makes --batch worth having: N calls in one process pay
    this once instead of N times.
    """
    global _SERVER
    if _SERVER is None:
        spec = importlib.util.spec_from_file_location(
            "_review_server", REPO / "src" / "agent_env" / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SERVER = mod
    return _SERVER


def _call(name: str, kwargs: dict) -> dict:
    """Run one allowlisted tool. -> {"tool","args","ok",...}. Never raises.

    A failure is returned as data rather than thrown, because in a batch one
    bad symbol must not discard the other thirty answers.
    """
    rec = {"tool": name, "args": kwargs}
    if name not in READ_ONLY:
        return {**rec, "ok": False,
                "error": f"{name!r} is not available to the reviewer",
                "reason": ("either it does not exist or it WRITES state. The "
                           "reviewer reads and judges; it does not act."),
                "available": sorted(READ_ONLY)}
    fn = getattr(_server(), name, None)
    if fn is None:
        return {**rec, "ok": False, "error": f"{name} not found on the server"}
    fn = getattr(fn, "fn", fn)          # FastMCP wraps tools; .fn is the plain one
    try:
        return {**rec, "ok": True, "result": fn(**kwargs)}
    except TypeError as e:
        return {**rec, "ok": False, "error": f"bad arguments for {name}: {e}"}
    except Exception as e:              # noqa: BLE001 — one bad call, not the batch
        return {**rec, "ok": False, "error": f"{type(e).__name__}: {e}"}


def _serve(sock_path: Path = SOCKET, idle: float = IDLE_EXIT_SECS) -> None:
    """Run the daemon: load the server ONCE, then answer batches until idle.

    One connection at a time, deliberately. Requests are cheap once the import
    is paid, and serialising them means this process cannot itself become the
    fan-out it exists to prevent. Batching is what removes the need for
    parallelism: thirty calls arrive on one connection.
    """
    import socket

    sock_path.parent.mkdir(parents=True, exist_ok=True)
    # A socket file left by a crashed daemon is not a live listener. Probe it
    # before unlinking, so we can never kill a healthy running instance.
    if sock_path.exists():
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(2)
            probe.connect(str(sock_path))
            probe.close()
            print(f"agent_view: a daemon is already listening on {sock_path}",
                  file=sys.stderr)
            return
        except OSError:
            sock_path.unlink(missing_ok=True)   # stale
        finally:
            probe.close()

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    os.chmod(sock_path, 0o600)          # owner only; this serves the live book
    srv.listen(16)
    srv.settimeout(idle)

    _server()                            # pay the 134 MB now, once
    print(f"agent_view daemon: ready on {sock_path} "
          f"({len(READ_ONLY)} read-only tools, idle-exit {idle:g}s)",
          file=sys.stderr)

    while True:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            break                        # idle long enough — release the memory
        with conn:
            try:
                conn.settimeout(120)
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf.decode() or "{}")
                calls = [(c[0], c[1] if len(c) > 1 and c[1] else {})
                         for c in req.get("calls", [])]
                # ⛔ allowlist enforced HERE — _call() rejects anything outside it
                results = [_call(name, kwargs) for name, kwargs in calls]
                payload = {"results": results}
            except Exception as e:       # noqa: BLE001 — one bad client, not the daemon
                payload = {"error": f"{type(e).__name__}: {e}"}
            try:
                conn.sendall((json.dumps(payload, default=str) + "\n").encode())
            except OSError:
                pass                     # client hung up; keep serving
    sock_path.unlink(missing_ok=True)
    print("agent_view daemon: idle, exiting", file=sys.stderr)


def _spawn_daemon() -> None:
    """Start the daemon detached, so the first caller does not host it.

    Guarded by an exclusive lock: several clients starting at once (which is
    exactly how the reviewer works) must produce ONE daemon, not twelve.
    """
    import fcntl
    import subprocess

    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    lock = os.open(SOCKET.parent / "spawn.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return                       # someone else is spawning; just wait
        if SOCKET.exists():
            return
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--serve"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
    finally:
        os.close(lock)


def _via_socket(calls: list[tuple[str, dict]]) -> list[dict] | None:
    """Ask the daemon. -> results, or None if the socket route is unavailable.

    Returning None rather than raising is the point: the caller falls back to
    loading the server in-process. A daemon that fails to start must slow the
    reviewer down, never blind it.
    """
    import socket

    deadline = time.monotonic() + SPAWN_WAIT_SECS
    spawned = False
    while True:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(180)
            s.connect(str(SOCKET))
            s.sendall((json.dumps({"calls": [[n, k] for n, k in calls]}) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            reply = json.loads(buf.decode() or "{}")
            return reply.get("results") if "results" in reply else None
        except (OSError, ValueError):
            if not spawned:
                _spawn_daemon()
                spawned = True
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.25)
        finally:
            s.close()


def _run_calls(calls: list[tuple[str, dict]], *, use_daemon: bool = True) -> list[dict]:
    """One entry point for both modes: daemon if we can, in-process if not."""
    if use_daemon and os.environ.get("AGENT_VIEW_NO_DAEMON") != "1":
        got = _via_socket(calls)
        if got is not None:
            return got
    return [_call(name, kwargs) for name, kwargs in calls]


def _parse_batch(raw: str) -> list[tuple[str, dict]]:
    """Accept either shape, because both are natural to write:

        [["account", {}], ["terrain", {"symbol": "MU"}]]
        [{"tool": "account"}, {"tool": "terrain", "args": {"symbol": "MU"}}]
    """
    spec = json.loads(raw)
    if not isinstance(spec, list):
        raise ValueError("batch must be a JSON array of calls")
    out = []
    for item in spec:
        if isinstance(item, str):
            out.append((item, {}))
        elif isinstance(item, list) and item:
            out.append((item[0], item[1] if len(item) > 1 and item[1] else {}))
        elif isinstance(item, dict) and item.get("tool"):
            out.append((item["tool"], item.get("args") or {}))
        else:
            raise ValueError(f"unrecognised call in batch: {item!r}")
    return out


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--"]
    if not args or args[0] in ("--list", "-l", "--help", "-h"):
        print("read-only tools:\n  " + "\n  ".join(sorted(READ_ONLY)))
        print("\nusage: agent_view.py <tool> [arg=value ...]")
        print("example: agent_view.py terrain symbol=MU")
        print("\nBATCH (STRONGLY PREFERRED — one process, one pandas import):")
        print("  agent_view.py --batch '[[\"account\",{}],"
              "[\"terrain\",{\"symbol\":\"MU\"}],[\"terrain\",{\"symbol\":\"NVDA\"}]]'")
        print("  ... or pipe the same JSON on stdin: agent_view.py --batch -")
        print("  Returns a JSON array; each entry carries tool/args/ok and "
              "result-or-error.")
        print("  A single call costs ~134 MB (it imports pandas). A batch of 30 "
              "costs the same 134 MB once, not 30 times.")
        return

    # ---- daemon mode: load once, serve many -------------------------------
    if args[0] == "--serve":
        _serve()
        return

    # ---- batch: many calls, ONE import, served by the daemon --------------
    if args[0] == "--batch":
        raw = args[1] if len(args) > 1 and args[1] != "-" else sys.stdin.read()
        try:
            calls = _parse_batch(raw)
        except (ValueError, json.JSONDecodeError) as e:
            print(json.dumps({"error": f"could not parse batch: {e}"}, indent=2))
            raise SystemExit(2)
        print(json.dumps(_run_calls(calls), indent=2, default=str))
        # A batch is a success if it RAN; individual failures are in the data.
        raise SystemExit(0)

    name = args[0]
    kwargs = {}
    for a in args[1:]:
        if "=" not in a:
            continue
        k, v = a.split("=", 1)
        try:
            kwargs[k] = int(v)
        except ValueError:
            try:
                kwargs[k] = float(v)
            except ValueError:
                kwargs[k] = v

    # Same path as --batch, so the two modes cannot drift into different answers.
    rec = _run_calls([(name, kwargs)])[0]
    if rec["ok"]:
        print(rec["result"])
        return
    print(json.dumps({k: v for k, v in rec.items()
                      if k not in ("ok", "tool", "args")}, indent=2))
    raise SystemExit(2)


if __name__ == "__main__":
    main()
