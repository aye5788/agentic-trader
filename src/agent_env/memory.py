"""Agent memory across sessions — what was ruled out, what is still open.

Sessions are separate processes with separate contexts. Without this, every
session re-derives the same conclusions and re-litigates the same rejections,
and the record shows no reasoning at all between one decision and the next.

Design spec §6 lists rule_out / open_question / research_log as the memory tools.

Three notes on why this is files and not a database:
  - The journal is already the system's append-only record and is mirrored
    off-box to the ledger repo. Memory belongs beside it, in the same format,
    so one backup covers both.
  - Entries are APPEND-ONLY. A rule-out that quietly vanishes is worse than one
    that is visibly superseded, so `revisit` writes a new entry rather than
    deleting the old one, and the history stays readable.
  - Everything here is pure given (path, now). The I/O is a single read and a
    single append, which keeps it selftestable without a fixture harness.

⚠️ These records are the agent's OWN reasoning, not facts about the market. A
rule-out means "I decided against this, for this reason, on this date" — never
"this is untradeable".

⛔ A RULE-OUT BINDS THE BUY SIDE. This used to read "nothing here gates an
order", and that sentence was the defect. A session would exit AMAT to be flat
into earnings, record the rule-out, and the deterministic fast loop — which
never opened this file — would rebuy it the next morning. That happened three
times (2026-08-12, 08-13, 08-14); the third bought straight into a post-earnings
gap 4.4% below the prior close. A decision the machine records but cannot act on
is not memory, it is a diary.

So: `binding_rule_outs()` is read by scripts/hooks/pretooluse_order_gate.py,
which refuses the buy at placement. That is the unbypassable chokepoint and, since
scripts/fast_loop.py was deleted on 2026-08-14, the only one — the loop that used
to drop these from its plan no longer exists. It does not matter which code path
proposed the order; nothing reaches the broker without passing the gate.

It binds BUYS ONLY. A rule-out can never block a sell, an exit, or a trim — same
rule as the kill-switch split and the cooldown: stops here are software-only, so
anything that blocks an exit strips a position of its only protection.

`revisit()` clears it. `until` time-boxes it for a reason with a known expiry
(earnings, a lockup, a pending filing); omit `until` and it holds until a session
explicitly revisits, which is what "I decided against this" should mean.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

MEM_DIRNAME = "memory"
RULED_OUT = "ruled_out.jsonl"
QUESTIONS = "open_questions.jsonl"


def _now_iso(now=None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def _read(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue                    # a torn line must not blind the rest
    return out


def _append(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")


def rule_out(root: Path, symbol: str, reason: str, now=None,
             until: str | None = None) -> dict:
    """Record that this name was considered and rejected, with the reason.

    ⛔ THIS BLOCKS BUYS until revisited (or until `until` passes). See the module
    docstring: the order gate reads it at placement. It cannot block a sell or an
    exit.

    `until` is an ISO date (YYYY-MM-DD) for a reason with a known expiry —
    "flat into earnings on the 13th" should not exclude the name forever.
    Omit it and the rule-out holds until a session calls revisit().
    """
    symbol = str(symbol).strip().upper()
    reason = str(reason).strip()
    if not symbol:
        return {"error": "symbol is required"}
    if not reason:
        return {"error": "a reason is required — a rule-out without one is "
                         "indistinguishable from an oversight next session"}
    rec = {"ts": _now_iso(now), "symbol": symbol, "reason": reason,
           "status": "ruled_out"}
    if until is not None:
        until = str(until).strip()
        try:
            datetime.strptime(until, "%Y-%m-%d")
        except ValueError:
            return {"error": f"until must be an ISO date (YYYY-MM-DD), got {until!r} "
                             "— an unparseable expiry would silently never expire"}
        rec["until"] = until
    _append(root / MEM_DIRNAME / RULED_OUT, rec)
    return {"recorded": rec}


def revisit(root: Path, symbol: str, reason: str, now=None) -> dict:
    """Supersede an earlier rule-out. Appends; never deletes the original."""
    symbol = str(symbol).strip().upper()
    reason = str(reason).strip()
    if not symbol or not reason:
        return {"error": "symbol and reason are both required"}
    rec = {"ts": _now_iso(now), "symbol": symbol, "reason": reason,
           "status": "revisited"}
    _append(root / MEM_DIRNAME / RULED_OUT, rec)
    return {"recorded": rec}


def ruled_out(root: Path) -> dict:
    """Current rule-outs — latest entry per symbol wins, revisits drop out."""
    latest: dict = {}
    for rec in _read(root / MEM_DIRNAME / RULED_OUT):
        sym = rec.get("symbol")
        if sym:
            latest[sym] = rec
    active = {s: r for s, r in latest.items() if r.get("status") == "ruled_out"}
    return {"ruled_out": active,
            "revisited": sorted(s for s, r in latest.items()
                                if r.get("status") == "revisited")}


def binding_rule_outs(root: Path, today: str | None = None) -> dict:
    """{symbol: record} for rule-outs that BLOCK A BUY right now.

    This is the function the order gate calls. It is deliberately separate from
    ruled_out(): that one is a reading surface for the agent (it reports revisited
    names too), this one is a gate input and returns only what is currently
    binding.

    - latest entry per symbol wins (append-only file, revisit supersedes)
    - status must be "ruled_out" — a revisited name binds nothing
    - an `until` in the past has expired and binds nothing

    A missing file returns {} — that is "nothing was ruled out", which is the
    truth, not a swallowed error. A torn line is skipped by _read() rather than
    blinding the rest of the file.
    """
    today = today or datetime.now(timezone.utc).date().isoformat()
    latest: dict = {}
    for rec in _read(root / MEM_DIRNAME / RULED_OUT):
        sym = rec.get("symbol")
        if sym:
            latest[sym] = rec
    out = {}
    for sym, rec in latest.items():
        if rec.get("status") != "ruled_out":
            continue
        until = rec.get("until")
        if until and str(until) < today:
            continue
        out[sym] = rec
    return out


def open_question(root: Path, question: str, now=None) -> dict:
    """Record something worth resolving that this session could not."""
    question = str(question).strip()
    if not question:
        return {"error": "question text is required"}
    rec = {"ts": _now_iso(now), "question": question, "status": "open"}
    _append(root / MEM_DIRNAME / QUESTIONS, rec)
    return {"recorded": rec}


def close_question(root: Path, question: str, answer: str, now=None) -> dict:
    """Answer an open question. Appends the answer; the original stays."""
    question, answer = str(question).strip(), str(answer).strip()
    if not question or not answer:
        return {"error": "question and answer are both required"}
    rec = {"ts": _now_iso(now), "question": question, "answer": answer,
           "status": "closed"}
    _append(root / MEM_DIRNAME / QUESTIONS, rec)
    return {"recorded": rec}


def questions(root: Path) -> dict:
    """Still-open questions, newest first, with answered ones separated."""
    latest: dict = {}
    for rec in _read(root / MEM_DIRNAME / QUESTIONS):
        q = rec.get("question")
        if q:
            latest[q] = rec
    return {
        "open": [r for r in sorted(latest.values(), key=lambda r: r.get("ts", ""),
                                   reverse=True) if r.get("status") == "open"],
        "closed": [r for r in sorted(latest.values(), key=lambda r: r.get("ts", ""),
                                     reverse=True) if r.get("status") == "closed"],
    }


def _reasoned_events(journal: Path) -> list:
    """Every past decision WITH ITS REASON, from wherever it was actually written.

    ⚠️ THIS READ USED TO BE `agent_decision` ONLY, AND FOUND NOTHING. Measured
    2026-08-11: the live journal held 138 events -- 37 risk_review, 31 execution
    (26 carrying notes), 17 exit_signal, all reasoned -- and ZERO agent_decision,
    because the deterministic loops that have been trading this book do not call
    record_decision. So the tool the charter calls "the only mechanism by which
    today's thinking reaches tomorrow" returned an empty list while the reasoning
    sat in the same file under different keys.

    A memory that reads only the channel a future system will write is empty in
    every session until that system exists. This reads the record as it IS, and
    keeps working unchanged once agent_decision events start appearing.
    """
    out = []
    if not journal.exists():
        return out
    for line in journal.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        ev, when = rec.get("event"), rec.get("ts") or rec.get("at")

        if ev == "agent_decision":
            out.append({"when": when, "symbol": rec.get("symbol"),
                        "action": rec.get("action"), "reason": rec.get("reason"),
                        "source": "agent_decision"})
        elif ev == "risk_review":
            for o in rec.get("orders_intended") or []:
                out.append({"when": when, "symbol": o.get("symbol"),
                            "action": o.get("kind"), "reason": o.get("reason"),
                            "fraction": o.get("fraction"), "source": "risk_review"})
        elif ev == "execution":
            for f in rec.get("fills") or []:
                why = f.get("note") or f.get("reason")
                if not why:
                    continue
                out.append({"when": when, "symbol": f.get("symbol"),
                            "action": f"{f.get('side')} ({f.get('status')})",
                            "reason": why, "source": "execution"})
        elif ev == "exit_signal":
            out.append({"when": when, "symbol": rec.get("symbol"),
                        "action": rec.get("reason") or "exit",
                        "reason": rec.get("note") or rec.get("detail"),
                        "source": "exit_signal"})
    return out


def _level_reasons(root: Path) -> dict:
    """The reason attached to each agent-set level, which lives OUTSIDE the
    journal in the monitor's override file and is invisible to any journal read.
    """
    path = root / "monitor" / "overrides.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for sym, rec in (data or {}).items():
        if isinstance(rec, dict) and rec.get("reason"):
            out[sym] = {"stop": rec.get("stop"), "reason": rec["reason"],
                        "set_at": rec.get("ts")}
    return out


def research_log(root: Path, journal: Path, limit: int = 25) -> dict:
    """What past sessions decided and why — the agent reading its own record.

    Gathers reasoning from EVERY place it is actually written (see
    _reasoned_events and _level_reasons), plus current rule-outs and open
    questions. Without this the agent can record reasoning it never reads back,
    which is a write-only memory.
    """
    decisions = _reasoned_events(journal)
    decisions.sort(key=lambda r: str(r.get("when") or ""))
    decisions = decisions[-int(limit):][::-1]
    mem = ruled_out(root)
    return {"recent_decisions": decisions,
            "levels_and_why": _level_reasons(root),
            "ruled_out": mem["ruled_out"],
            "open_questions": questions(root)["open"],
            "note": "Your own past reasoning, not market fact. Supersede it "
                    "freely with a stated reason."}


def _selftest() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # a reason is mandatory in both directions
        assert "error" in rule_out(root, "AAA", "")
        assert "error" in rule_out(root, "", "why")
        assert "error" in open_question(root, "  ")

        rule_out(root, "aaa", "balance sheet too levered")
        rule_out(root, "BBB", "no liquidity")
        r = ruled_out(root)
        assert set(r["ruled_out"]) == {"AAA", "BBB"}, r      # symbol upcased
        assert r["ruled_out"]["AAA"]["reason"].startswith("balance")

        # revisit supersedes without deleting history
        revisit(root, "AAA", "refinanced, thesis changed")
        r = ruled_out(root)
        assert set(r["ruled_out"]) == {"BBB"}, r
        assert r["revisited"] == ["AAA"], r
        raw = (root / MEM_DIRNAME / RULED_OUT).read_text().splitlines()
        assert len(raw) == 3, "append-only: the original must still be on disk"

        # ⛔ THE BINDING HALF — this is what the fast loop and the order gate read.
        # The defect this replaces: rule_out() recorded AMAT and the loop rebought
        # it the next morning three days running, because nothing read the file.
        b = binding_rule_outs(root, today="2026-08-14")
        assert set(b) == {"BBB"}, b               # AAA was revisited -> not binding
        assert b["BBB"]["reason"] == "no liquidity"

        # an `until` time-boxes it: binding up to and including the date, then not
        rule_out(root, "AMAT", "flat into earnings 08-13", until="2026-08-15")
        assert "AMAT" in binding_rule_outs(root, today="2026-08-14")
        assert "AMAT" in binding_rule_outs(root, today="2026-08-15"), "inclusive"
        assert "AMAT" not in binding_rule_outs(root, today="2026-08-16"), "expired"
        # ...and no `until` binds indefinitely, until revisited
        assert "BBB" in binding_rule_outs(root, today="2099-01-01")
        rule_out(root, "CCC", "thesis broken")
        assert "CCC" in binding_rule_outs(root, today="2099-01-01")
        revisit(root, "CCC", "thesis re-formed")
        assert "CCC" not in binding_rule_outs(root, today="2026-08-14")

        # an unparseable expiry is REFUSED, never stored — a bad date that silently
        # never expires would be a permanent, invisible block on the name
        assert "error" in rule_out(root, "ZZZ", "why", until="next tuesday")
        assert "error" in rule_out(root, "ZZZ", "why", until="2026-13-45")
        assert "ZZZ" not in binding_rule_outs(root, today="2026-08-14")

        # a missing file is "nothing ruled out", not an error
        with tempfile.TemporaryDirectory() as d3:
            assert binding_rule_outs(Path(d3)) == {}

        # questions open then close
        open_question(root, "is the sleeve carrying its weight?")
        assert len(questions(root)["open"]) == 1
        close_question(root, "is the sleeve carrying its weight?", "no — 0 of 4 hit")
        q = questions(root)
        assert q["open"] == [] and len(q["closed"]) == 1, q

        # research_log folds in journal decisions, newest first
        j = root / "journal.jsonl"
        j.write_text(
            '{"event":"agent_decision","ts":"2026-08-01T00:00:00Z","symbol":"MU",'
            '"action":"buy","reason":"first"}\n'
            'not json\n'
            '{"event":"execution","ts":"2026-08-02T00:00:00Z"}\n'
            '{"event":"agent_decision","ts":"2026-08-03T00:00:00Z","symbol":"STX",'
            '"action":"skip","reason":"second"}\n')
        log = research_log(root, j)
        assert [d["symbol"] for d in log["recent_decisions"]] == ["STX", "MU"], log
        # a torn journal line must not blind the rest (the 'not json' line above)
        assert len(log["recent_decisions"]) == 2
        assert "BBB" in log["ruled_out"]
        assert log["open_questions"] == []

        # ⚠️ REGRESSION GUARD: reasoning written by the DETERMINISTIC loops must
        # surface too. Reading agent_decision alone returned an empty log against
        # a journal holding 85 reasoned events.
        j.write_text(
            '{"event":"risk_review","ts":"2026-08-09T16:00:00Z","orders_intended":'
            '[{"kind":"trim","symbol":"AMAT","fraction":0.5,"reason":"earnings in 2 days"}]}\n'
            '{"event":"execution","ts":"2026-08-09T16:04:00Z","fills":'
            '[{"symbol":"AMAT","side":"sell","status":"filled","note":"trim 50%"}]}\n'
            '{"event":"exit_signal","at":"2026-08-08T14:00:00Z","symbol":"WDC",'
            '"reason":"stop","note":"breached 41.20"}\n')
        log = research_log(root, j)
        srcs = {d["source"] for d in log["recent_decisions"]}
        assert srcs == {"risk_review", "execution", "exit_signal"}, log
        assert any("earnings in 2 days" == d["reason"] for d in log["recent_decisions"])
        assert log["recent_decisions"][0]["when"] > log["recent_decisions"][-1]["when"]

        # level reasons live OUTSIDE the journal and must still surface
        (root / "monitor").mkdir(parents=True, exist_ok=True)
        (root / "monitor" / "overrides.json").write_text(
            '{"AMD": {"stop": 468.0, "reason": "third pass below the 21-day"}}')
        assert research_log(root, j)["levels_and_why"]["AMD"]["reason"].startswith("third")

        # empty tree degrades to empty, never raises
        with tempfile.TemporaryDirectory() as d2:
            r2 = Path(d2)
            assert ruled_out(r2)["ruled_out"] == {}
            assert questions(r2)["open"] == []
            assert research_log(r2, r2 / "nope.jsonl")["recent_decisions"] == []

    print("memory: OK — append-only, reasons mandatory, revisit supersedes")


if __name__ == "__main__":
    _selftest()
