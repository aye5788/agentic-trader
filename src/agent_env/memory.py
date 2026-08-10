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


def research_log(root: Path, journal: Path, limit: int = 25) -> dict:
    """What past sessions decided and why — the agent reading its own record.

    Sources `agent_decision` events from the journal (written by record_decision)
    plus current rule-outs and open questions. Without this the agent can record
    reasoning it can never read back, which is a write-only memory.
    """
    decisions = []
    if journal.exists():
        for line in journal.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("event") == "agent_decision":
                decisions.append({k: rec.get(k) for k in
                                  ("ts", "symbol", "action", "reason")})
    decisions = decisions[-int(limit):][::-1]
    mem = ruled_out(root)
    return {"recent_decisions": decisions,
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
        assert "BBB" in log["ruled_out"]
        assert log["open_questions"] == []

        # a torn journal line must not blind the rest (proved above: 'not json')
        assert len(log["recent_decisions"]) == 2

        # empty tree degrades to empty, never raises
        with tempfile.TemporaryDirectory() as d2:
            r2 = Path(d2)
            assert ruled_out(r2)["ruled_out"] == {}
            assert questions(r2)["open"] == []
            assert research_log(r2, r2 / "nope.jsonl")["recent_decisions"] == []

    print("memory: OK — append-only, reasons mandatory, revisit supersedes")


if __name__ == "__main__":
    _selftest()
