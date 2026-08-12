"""ANNOUNCE-FIRST — which orders the operator hears about AS THEY HAPPEN.

WHAT FAILURE THIS PREVENTS
    prompts/charter.md tells the agent that three classes of unusual action are
    pushed BEFORE it acts. Until this module existed there was no notify surface
    anywhere in the agent's environment (29 MCP tools, none of them a push), and
    no code path anywhere turned an unusual ORDER into a message. The charter was
    describing a control that did not exist, which is the worst kind of safety
    claim: the agent cannot check it, and a reader of the charter would believe
    the operator is being told about off-mandate entries when nobody is.

WHAT THIS IS NOT
    ⚠️ NOT AN APPROVAL GATE. `needs_announcement` decides what gets SAID; nothing
    here decides what gets DONE. The loops are headless — no human is waiting to
    answer at 10:00 on a Tuesday — so the announcement goes out and the order
    proceeds in the same breath. The operator's actual intervention is the KILL
    SWITCH, exercised after the fact, on a position that is already open. Read
    "announce-first" as "you will see it as it happens", never as "you get a
    veto". Anything that claims otherwise is wrong.

    It also is NOT a limit. The blocking limits live in src/governance.py and
    scripts/hooks/pretooluse_order_gate.py; an announcement fires strictly INSIDE
    them, on orders that are already allowed.

WHAT IT CANNOT SEE
    "Abandoning the house view wholesale" — the first announceable class in the
    charter — is NOT detectable from a single order. Whether an entry is a
    considered deviation or the abandonment of cross-sectional momentum is a
    property of the SESSION'S REASONING, not of any field on the order. This
    module does not pretend to infer it; the agent announces that one itself,
    via the `announce` tool. Do not add a heuristic here that guesses at it — a
    guess would either cry wolf on every rotation or, worse, stay silent and be
    read as evidence that no abandonment occurred.

A SELL IS NEVER ANNOUNCED
    Not "rarely" — never. Stops in this system are software (scripts/
    market_monitor.py IS the stop), so anything that adds friction, hesitation or
    a "should I have announced this first?" beat in front of an exit strips a
    position of its only protection. Announcements attach to RISK BEING TAKEN.
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

# The announce trigger is a FRACTION OF the blocking concentration limit, never a
# second number — imported from the one module that defines it, so the charter's
# rendered threshold and the threshold that actually fires cannot drift apart.
from charter import ANNOUNCE_FRACTION       # noqa: E402


def _to_float(v):
    """Parse to a finite float, else None. Mirrors the order gate's parser:

    the RH MCP passes numbers as STRINGS while the fast loop's own plan dicts
    carry floats. `bool` is excluded (it is an `int` to Python but can never be a
    dollar amount), and a non-finite value returns None so it can only ever end
    up NOT announcing — never sliding through a `>` comparison the way NaN does.
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def _held_value(valued: dict, symbol: str) -> float:
    """Marked dollar value already held in `symbol`, 0.0 if none/unreadable."""
    pos = (valued or {}).get("positions") or {}
    row = pos.get(symbol)
    if isinstance(row, dict):
        return _to_float(row.get("value")) or 0.0
    return _to_float(row) or 0.0        # legacy schema: dollars at cost


def needs_announcement(order: dict, valued: dict, universe, mandate_cfg: dict) -> str | None:
    """-> a human-readable reason the operator should hear about this order, or None.

    PURE: no clock, no I/O, no push. The caller decides what to do with the
    string; returning one is not a refusal and never blocks anything.

    `order`   — {symbol, side, amount}. `amount` is the DOLLAR NOTIONAL, already
                resolved by the caller (the order gate multiplies out a limit
                order priced in shares); `dollar_amount` is accepted as the RH
                MCP spells it.
    `valued`  — src/marks.load(): account_value + marked positions.
    `universe`— the configured whitelist (any iterable of tickers). FALSY means
                "not known here", and the off-universe check is then SKIPPED
                rather than firing on every symbol — an unreadable universe file
                must not turn every routine buy into a phone alert, which is how
                a channel gets muted.
    `mandate_cfg` — config/mandate.toml, for [concentration] max_position_pct.

    Two conditions fire:

      1. a BUY of a symbol outside the configured universe — the agent is
         allowed to trade off-list (the charter says so), and this is what makes
         that allowance visible rather than silent.
      2. a BUY whose RESULTING POSITION crosses ANNOUNCE_FRACTION of the
         concentration limit. Resulting position, NOT order size: the order gate
         caps a single order's notional and is blind to what is already held, so
         two adds that each pass cleanly are exactly how a concentration breach
         gets built. The existing holding is counted.

    A SELL returns None, always, first, before anything else is even computed.
    See the module docstring.

    Wholesale abandonment of the house view is NOT detectable here — it is a
    property of the session's reasoning, not of an order — so this function does
    not look for it. The agent announces that one itself.
    """
    side = str((order or {}).get("side") or "").strip().lower()
    if side != "buy":
        return None

    symbol = str((order or {}).get("symbol") or "").strip().upper()
    reasons: list[str] = []

    if universe:
        known = {str(t).strip().upper() for t in universe}
        if symbol and symbol not in known:
            reasons.append(
                f"{symbol} is OUTSIDE the configured universe ({len(known)} names) "
                f"— off-list entries carry no deep price history here, so they "
                f"cannot be scored or given measured stop geometry")

    raw = (order or {}).get("amount")
    if raw is None:
        raw = (order or {}).get("dollar_amount")
    amount = _to_float(raw)
    av = _to_float((valued or {}).get("account_value"))
    cap = _to_float(((mandate_cfg or {}).get("concentration") or {}).get("max_position_pct"))
    if amount is not None and amount > 0 and av and av > 0 and cap and cap > 0:
        held = _held_value(valued, symbol)
        resulting = held + amount
        trigger = ANNOUNCE_FRACTION * cap
        if resulting > trigger * av:
            reasons.append(
                f"{symbol} POSITION would reach ${resulting:,.2f} = "
                f"{resulting / av:.1%} of ${av:,.2f} equity "
                f"(${held:,.2f} already held + ${amount:,.2f} this order), past the "
                f"{trigger:.1%} announce line — {ANNOUNCE_FRACTION:.0%} of the "
                f"{cap:.0%} concentration limit that refuses outright")

    return "; ".join(reasons) or None


# --------------------------------------------------------------------------- #
# transport: push WITHOUT paying for it
# --------------------------------------------------------------------------- #
def _default_send(title: str, message: str, tags: str, topic: str | None) -> None:
    import notify                                    # noqa: PLC0415
    notify.push(title, message, tags=tags, topic=topic)


def push_detached(title: str, message: str, tags: str = "loudspeaker",
                  topic: str | None = None, _send=None) -> bool:
    """Send a phone push in a DETACHED grandchild. Returns immediately. Never raises.

    WHY THIS EXISTS AND WHY A PLAIN notify.push() WILL NOT DO
        The caller that matters is scripts/hooks/pretooluse_order_gate.py, which
        sits on the critical path of every live order under a measured ~0.1s
        budget. `notify.push` is a blocking HTTPS POST with a 10-second timeout:
        an ntfy server that is slow, DNS-broken or simply down would hold up a
        real order by up to ten seconds — every order, for as long as the outage
        lasts. An alert that delays the trade it is describing is worse than no
        alert. So the network call happens in a process the caller does not wait
        for, and the caller's cost is a fork.

    THE THREE WAYS A "BACKGROUND" PUSH SILENTLY DOESN'T WORK, and what is done:
      - a daemon thread is KILLED when the interpreter exits, and the hook exits
        microseconds later — the POST would be aborted mid-flight. A non-daemon
        thread is worse: the process then waits for the full 10s timeout before
        exiting, which is the exact latency this avoids. Hence a process, not a
        thread.
      - an inherited stdout keeps the pipe OPEN. The harness reads the hook's
        stdout to EOF, so a child holding fd 1 would stall the order for the
        life of the POST even though the parent already exited. The child's
        stdin/stdout/stderr are therefore redirected to /dev/null before any
        network work, and it gets its own session.
      - the first child would linger as a ZOMBIE in a long-lived parent (the MCP
        server calls this too). Hence the double fork: the first child forks the
        real worker and exits at once, and the parent reaps only that — a wait
        measured in a millisecond, never on the network.

    `notify` is imported HERE, IN THE PARENT, before the fork: the MCP server is
    a threaded process, and a fork that inherits a held import lock can deadlock
    the child. The child then only touches os.* and an already-loaded module.

    Returns True if a worker was spawned, False if it could not be — the caller
    treats False as "the alert did not go out", never as an error to raise.
    """
    send = _send
    if send is None:
        try:
            import notify                            # noqa: PLC0415,F401 — parent-side import
        except Exception:
            return False
        send = _default_send

    try:                        # never let a buffered byte be written twice
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass

    try:
        pid = os.fork()
    except (AttributeError, OSError):
        return _push_subprocess(title, message, tags, topic)

    if pid > 0:                 # parent: reap the intermediate child and move on
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        return True

    # ---- first child: detach and hand off -------------------------------- #
    try:
        if os.fork() > 0:
            os._exit(0)
        os.setsid()
        fd = os.open(os.devnull, os.O_RDWR)
        for target in (0, 1, 2):
            os.dup2(fd, target)
        send(title, message, tags, topic)
    except BaseException:       # noqa: BLE001 — a detached alert never reports
        pass
    finally:
        os._exit(0)             # _exit, not exit: never flush the parent's buffers
    return True                 # unreachable; present so the signature is honest


def _push_subprocess(title: str, message: str, tags: str, topic: str | None) -> bool:
    """Fallback for a platform without fork. Same contract: detached, unwaited."""
    import subprocess                                # noqa: PLC0415
    code = ("import sys;sys.path.insert(0,%r);import notify;"
            "notify.push(sys.argv[1],sys.argv[2],tags=sys.argv[3],"
            "topic=(sys.argv[4] or None))" % str(REPO / "src"))
    try:
        subprocess.Popen([sys.executable, "-c", code, title, message, tags, topic or ""],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, cwd=str(REPO))
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
def _selftest() -> None:
    """No network, ever: the transport test injects its own `_send`.

    The load-bearing assertions are (a) a SELL is never announced, (b) the
    concentration trigger counts what is ALREADY HELD, and (c) push_detached
    returns without waiting for the send.
    """
    import tempfile
    import time

    MCFG = {"concentration": {"max_position_pct": 0.15}}
    UNI = {"MU", "AAPL", "XLK"}
    V = {"account_value": 1000.0,
         "positions": {"MU": {"qty": 1, "value": 100.0},
                       "AAPL": {"qty": 1, "value": 20.0}}}
    # trigger = 0.80 * 15% = 12% of equity = $120 on a $1,000 book

    # --- an ordinary in-universe buy that stays small: silence ---------------
    ok = {"symbol": "AAPL", "side": "buy", "amount": 50.0}      # 20 + 50 = 70 < 120
    assert needs_announcement(ok, V, UNI, MCFG) is None

    # --- off-universe BUY is announced --------------------------------------
    off = {"symbol": "NFLX", "side": "buy", "amount": 10.0}
    r = needs_announcement(off, V, UNI, MCFG)
    assert r and "OUTSIDE" in r and "NFLX" in r, r

    # --- ⚠️ RESULTING POSITION, not order size ------------------------------
    # $30 into MU is a small order by any measure, and the order gate passes it
    # without a second look. It is the $100 already held that puts the POSITION
    # over the line. An implementation that only looked at `amount` would stay
    # silent here, which is precisely how a concentration breach gets built out
    # of individually-innocent adds.
    add = {"symbol": "MU", "side": "buy", "amount": 30.0}       # 100 + 30 = 130 > 120
    r = needs_announcement(add, V, UNI, MCFG)
    assert r and "13.0%" in r and "already held" in r, r
    assert needs_announcement({**add, "amount": 10.0}, V, UNI, MCFG) is None, \
        "100 + 10 = 110 is under the 120 line and must stay silent"

    # a fresh name can cross on the order alone
    assert needs_announcement({"symbol": "XLK", "side": "buy", "amount": 200.0},
                              V, UNI, MCFG) is not None
    # exactly ON the line is not yet OVER it
    assert needs_announcement({"symbol": "XLK", "side": "buy", "amount": 120.0},
                              V, UNI, MCFG) is None

    # both conditions at once -> both reasons, one message
    both = needs_announcement({"symbol": "NFLX", "side": "buy", "amount": 500.0},
                              V, UNI, MCFG)
    assert both and "OUTSIDE" in both and "announce line" in both, both

    # --- ⚠️ A SELL IS NEVER ANNOUNCED. Not off-universe, not huge, not ever. --
    for amt in (10.0, 5_000.0):
        for sym in ("MU", "NFLX"):
            s = {"symbol": sym, "side": "sell", "amount": amt}
            assert needs_announcement(s, V, UNI, MCFG) is None, s
    # ...including the share-quantity shape a full exit actually arrives as
    assert needs_announcement({"symbol": "NFLX", "side": "sell", "quantity": "3.14"},
                              V, UNI, MCFG) is None

    # --- unreadable inputs must go QUIET, never noisy ------------------------
    # An empty/absent universe means "not known here", not "everything is off-list":
    # firing on every buy is how an operator learns to ignore the channel.
    assert needs_announcement(ok, V, None, MCFG) is None
    assert needs_announcement(off, V, set(), MCFG) is None
    assert needs_announcement(off, V, [], MCFG) is None
    # ...but the concentration limb still works with no universe
    assert needs_announcement({"symbol": "MU", "side": "buy", "amount": 30.0},
                              V, None, MCFG) is not None
    # no snapshot / no account value / no mandate -> no concentration claim
    for bad_v in ({}, None, {"account_value": None}, {"account_value": float("nan")},
                  {"account_value": 0.0}):
        assert needs_announcement(add, bad_v, UNI, MCFG) is None, bad_v
    for bad_m in ({}, None, {"concentration": {}}, {"concentration": {"max_position_pct": None}}):
        assert needs_announcement(add, V, UNI, bad_m) is None, bad_m
    # garbage amounts are never announced ON (they are the gate's problem, and it
    # already refuses them)
    for bad in (None, "not-a-number", float("nan"), float("inf"), True, {}, -50.0):
        assert needs_announcement({"symbol": "MU", "side": "buy", "amount": bad},
                                  V, UNI, MCFG) is None, bad
    # a side that is neither buy nor sell is not a buy -> silence
    for bad_side in ("", None, "short", "BUYY"):
        assert needs_announcement({"symbol": "NFLX", "side": bad_side, "amount": 9e9},
                                  V, UNI, MCFG) is None, bad_side
    # case and whitespace on a real buy must still be understood
    for ok_side in ("BUY", " buy ", "Buy"):
        assert needs_announcement({"symbol": "nflx", "side": ok_side, "amount": 10.0},
                                  V, UNI, MCFG) is not None, ok_side
    # the RH wire spelling (strings, `dollar_amount`) parses like the loop's own
    assert needs_announcement({"symbol": "MU", "side": "buy", "dollar_amount": "30.00"},
                              V, UNI, MCFG) is not None
    # a legacy position row (bare dollars) is still counted, not skipped
    assert needs_announcement(add, {"account_value": 1000.0, "positions": {"MU": 100.0}},
                              UNI, MCFG) is not None

    # the trigger tracks the mandate, it is not a second number
    loose = needs_announcement(add, V, UNI, {"concentration": {"max_position_pct": 0.50}})
    assert loose is None, "a looser concentration limit must move the announce line"

    # --- transport: the parent must NOT wait for the send -------------------
    with tempfile.TemporaryDirectory() as d:
        marker = Path(d) / "sent.txt"

        def slow_send(title, message, tags, topic):
            time.sleep(0.5)                 # stands in for a wedged ntfy server
            marker.write_text(f"{title}|{message}|{tags}")

        t0 = time.monotonic()
        spawned = push_detached("T", "M", tags="x", _send=slow_send)
        elapsed = time.monotonic() - t0
        assert spawned is True
        assert elapsed < 0.15, f"push_detached blocked the caller for {elapsed:.3f}s"
        # no zombie is left behind for a long-lived parent to accumulate
        try:
            os.waitpid(-1, os.WNOHANG)
            raise AssertionError("push_detached left an unreaped child")
        except ChildProcessError:
            pass
        # ...and the send really does happen, in the detached worker
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), "the detached worker never ran the send"
        assert marker.read_text() == "T|M|x", marker.read_text()

    print("announce: OK — sells never announced, resulting position counted, "
          "push detached without blocking the caller")


if __name__ == "__main__":
    _selftest()
