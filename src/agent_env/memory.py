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
"this is untradeable". Nothing here gates an order.
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


def rule_out(root: Path, symbol: str, reason: str, now=None) -> dict:
    """Record that this name was considered and rejected, with the reason."""
    symbol = str(symbol).strip().upper()
    reason = str(reason).strip()
    if not symbol:
        return {"error": "symbol is required"}
    if not reason:
        return {"error": "a reason is required — a rule-out without one is "
                         "indistinguishable from an oversight next session"}
    rec = {"ts": _now_iso(now), "symbol": symbol, "reason": reason,
           "status": "ruled_out"}
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
