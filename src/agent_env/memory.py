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
import re
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
            # ⛔ THE FIELDS ARE NESTED, AND READING THEM FLAT FOUND NOTHING.
            # scripts/market_monitor.py journals one event per poll carrying
            # `triggers` = [{symbol, reason, price, level, fraction}] and NO
            # top-level symbol/reason/note/detail -- 0 of the 45 production
            # events carry one. So every stop this system has ever fired
            # reached the agent's own memory as {symbol: None, reason: None}:
            # 45 rows of nothing, in the tool the charter tells it to read
            # before acting. health.unrecorded_fills() and letter_facts.py
            # both already read `triggers`; this was the only reader that did
            # not, and its fixture used a flat shape production never wrote.
            #
            # ONE EVENT MAY CARRY SEVERAL TRIGGERS -- one row each, like the
            # risk_review branch above (5 events x2 and one x5 in production;
            # collapsing them would drop real breaches on the floor).
            for t in rec.get("triggers") or []:
                frac = t.get("fraction")
                why = f"price {t.get('price')} through level {t.get('level')}"
                if isinstance(frac, (int, float)) and 0 < frac < 1:
                    why += f" — {round(frac * 100)}% of the position"
                out.append({"when": when, "symbol": t.get("symbol"),
                            "action": t.get("reason") or "exit",
                            "reason": why, "source": "exit_signal"})
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


# Per-entry reason budgets in the DEFAULT view, and the caps on how many
# records it carries. Sized against the LIVE store rather than guessed: at 900
# chars the response still came to 71,110 -- the total is driven by COUNT x
# BUDGET, not by any one long entry, which is what the first attempt missed.
# Rule-outs get the largest budget because they BIND and a session must see them.
_REASON_CHARS = 350
_RULEOUT_CHARS = 600
_MAX_QUESTIONS = 8
_MAX_SYMBOL_DECISIONS = 8
_SYMBOL_DECISION_CHARS = 1200


def _clip(text, symbol=None, budget=None):
    """Truncate one reason, and SAY SO — never silently."""
    budget = budget or _REASON_CHARS
    if not isinstance(text, str) or len(text) <= budget:
        return text
    hint = f"research_log(symbol=\"{symbol}\")" if symbol else "research_log(symbol=...)"
    return (text[:budget]
            + f"  ...[TRUNCATED {len(text) - budget} chars. "
              f"Call {hint} for the full text — do NOT act on the clipped "
              f"version, the reversal condition is often at the end.]")


def entry_rationale(decisions: list, symbol: str, position_id) -> dict:
    """WHY this position is held, joined on position_id. Pure. Never raises.

    ⛔ EXACT LIFECYCLE JOIN, NEVER LATEST-BY-SYMBOL. A held name the ranking did
    not select carries a GENERATED thesis string ("HELD, not in the ranked
    selection ... whether to close it is YOUR call") and nothing else, so every
    session inherits the position with no reason attached and the cheapest
    correct-looking action is to hold it again. The reason does exist -- the
    session that bought it called record_decision -- but only as prose in the
    append-only stream, and the agent is stateless between sessions.

    ⛔ THE OBVIOUS IMPLEMENTATION IS WRONG. "Most recent decision mentioning
    SYM" silently attaches:
      - the PREVIOUS lifecycle's reasoning after a full close and re-entry, which
        is a rationale for a position that no longer exists;
      - a basket decision (`symbol: "AMD,MU,INTC"`) to one of its legs. Excluded
        by exact string equality: "AMD,MU,INTC" != "AMD". Do not "fix" that by
        splitting the string -- one scalar position_id cannot describe a basket.
      - a decision recorded BEFORE the buy, which has no active lifecycle to
        stamp and is never linked retroactively.
    Each of those reads as authoritative and is unfalsifiable by the reader.

    So: a rationale is reported ONLY when a decision carries the position_id of
    the lifecycle that is open right now. Otherwise the field is None and
    `rationale_join_status` says which of those cases applies. An honest "not
    linked" is worth more than a plausible wrong reason, because the wrong one
    gets acted on.

    `position_id` is derived by the ENVIRONMENT from the lifecycle replay
    (lifecycle_journal.active_position_id); the agent cannot supply or guess one.
    Absence is a permanent, valid state for records written before that field
    existed -- so this must degrade to "unlinked", never to an error.
    """
    out = {"position_id": position_id, "entry_rationale": None,
           "entry_decision_ts": None, "entry_decision_action": None,
           "rationale_source": None, "rationale_join_status": None,
           "rationale_missing": True}
    if not position_id:
        out["rationale_join_status"] = (
            "no_active_lifecycle — no open position_id for this symbol in the "
            "journal replay, so no decision can be linked to it")
        return out
    try:
        mine = [d for d in (decisions or [])
                if str(d.get("position_id") or "") == str(position_id)
                and str(d.get("symbol") or "") == str(symbol)
                and isinstance(d.get("reason"), str) and d.get("reason").strip()]
    except Exception:                                   # noqa: BLE001 — never raises
        mine = []
    if not mine:
        out["rationale_join_status"] = (
            f"unlinked — no reasoned decision carries position_id {position_id}. "
            f"The buy may predate the lifecycle stamp, or have been recorded as a "
            f"basket. Call research_log(symbol=\"{symbol}\") to read the history "
            f"yourself; do NOT assume the position is unreasoned.")
        return out
    mine.sort(key=lambda d: str(d.get("ts") or ""))
    # The OPENING decision is the thesis; a later trim/hold note is not.
    opens = [d for d in mine
             if any(k in str(d.get("action") or "").lower()
                    for k in ("open", "buy", "enter", "add"))]
    pick = (opens or mine)[0]
    out.update({
        "entry_rationale": _clip(pick.get("reason"), symbol, _SYMBOL_DECISION_CHARS),
        "entry_decision_ts": pick.get("ts"),
        "entry_decision_action": pick.get("action"),
        "rationale_source": ("record_decision, joined on the currently-open "
                             "position_id" + ("" if opens else
                             " (no opening decision found; earliest linked "
                             "decision shown instead)")),
        "rationale_join_status": "linked",
        "rationale_missing": False,
    })
    return out


def research_log(root: Path, journal: Path, limit: int = 25,
                 symbol: str = "") -> dict:
    """What past sessions decided and why — the agent reading its own record.

    Gathers reasoning from EVERY place it is actually written (see
    _reasoned_events and _level_reasons), plus current rule-outs and open
    questions. Without this the agent can record reasoning it never reads back,
    which is a write-only memory.

    ⛔ IT MUST FIT IN A TOOL RESULT, OR IT IS A WRITE-ONLY MEMORY AGAIN. On
    2026-08-21 this returned 107,619 characters and the session could not read it
    at all: rule-out reasons had grown to 1,500+ chars each, and nothing bounded
    the total. That session learned about two binding rule-outs only by tripping
    them at the order gate, and said so itself — "that was luck, not design: a
    rule-out I didn't happen to trip would have stayed invisible to me."

    So reasons are CLIPPED in the default view and the clip is announced. Pass
    `symbol` to get every record for one name in FULL — which is what you want
    before deciding about it, because a rule-out's reversal condition is usually
    the last thing in its text.
    """
    sym = (symbol or "").strip().upper()

    decisions = _reasoned_events(journal)
    decisions.sort(key=lambda r: str(r.get("when") or ""))
    if sym:
        decisions = [d for d in decisions
                     if str(d.get("symbol") or "").upper() == sym
                     or sym in str(d.get("symbol") or "").upper().split(",")]
    decisions = decisions[-int(limit):][::-1]

    mem = ruled_out(root)
    rules = mem["ruled_out"]
    levels = _level_reasons(root)
    qs = questions(root)["open"]

    if sym:
        # FULL TEXT, one name. Everything here is about that symbol, so nothing
        # is clipped -- this is the view you read before acting on a name, and
        # a rule-out's reversal condition is usually its last line.
        rules = {k: v for k, v in rules.items() if k == sym}
        levels = {k: v for k, v in levels.items() if k == sym}
        # ⛔ WORD BOUNDARY, NOT SUBSTRING. Matching "TER" anywhere pulled in every
        # question containing AFTER / COUNTER / QUARTER, unclipped, and pushed the
        # worst single-symbol view to 59,750 chars -- larger than the default view
        # this exists to shrink. Clipped and capped for the same reason.
        def _mentions(q):
            text = (q if isinstance(q, str) else str(q.get("question") or "")).upper()
            return re.search(rf"\b{re.escape(sym)}\b", text) is not None
        qs = [_clip(q, sym, _SYMBOL_DECISION_CHARS) if isinstance(q, str)
              else {**q, "question": _clip(q.get("question"), sym, _SYMBOL_DECISION_CHARS)}
              for q in qs if _mentions(q)][-_MAX_QUESTIONS:]
        # ⛔ RULE-OUTS AND LEVELS STAY FULL -- they are what BINDS, and the
        # reversal condition is usually the last line. Decisions are history, so
        # they are capped: measured on the live store, AMAT's 18 full decisions
        # made the single-name view LARGER than the default one it exists to
        # avoid (36,870 chars), which would reproduce the bug being fixed.
        shown = decisions[:_MAX_SYMBOL_DECISIONS]
        older = len(decisions) - len(shown)
        # Generous, but still bounded: a single session's record_decision can run
        # to several thousand characters, and eight of them alone came to 33,351.
        decisions = [{**d, "reason": _clip(d.get("reason"), sym, _SYMBOL_DECISION_CHARS)}
                     for d in shown]
        return {"symbol": sym, "recent_decisions": decisions,
                "older_decisions_not_shown": older,
                "levels_and_why": levels, "ruled_out": rules,
                "open_questions": qs, "full_text": True,
                "note": f"FULL text for {sym}, nothing clipped. Your own past "
                        f"reasoning, not market fact. Supersede it freely with "
                        f"a stated reason."}

    decisions = [{**d, "reason": _clip(d.get("reason"), d.get("symbol"))}
                 for d in decisions]
    rules = {k: {**v, "reason": _clip(v.get("reason"), k, _RULEOUT_CHARS)}
             for k, v in rules.items()}
    levels = {k: {**v, "reason": _clip(v.get("reason"), k)}
              for k, v in levels.items()}
    dropped_q = max(0, len(qs) - _MAX_QUESTIONS)
    qs = [_clip(q) if isinstance(q, str)
          else {**q, "question": _clip(q.get("question"))}
          for q in qs[-_MAX_QUESTIONS:]]

    return {"recent_decisions": decisions,
            "levels_and_why": levels,
            "ruled_out": rules,
            "open_questions": qs,
            "older_open_questions_not_shown": dropped_q,
            "full_text": False,
            "note": "Your own past reasoning, not market fact. Supersede it "
                    "freely with a stated reason. Long reasons are CLIPPED "
                    "here — call research_log(symbol=\"TICKER\") for the full "
                    "text of any name before you act on it."}


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
            # ⛔ THE PRODUCTION SHAPE, COPIED OFF THE JOURNAL, NOT INVENTED.
            # This fixture used to write a FLAT {"symbol","reason","note"}
            # exit_signal that market_monitor.py has never once produced, so it
            # passed while all 45 real events parsed to {symbol: None,
            # reason: None}. The monitor nests them under `triggers`, and one
            # event can carry several. Both real top-level shapes appear here:
            # with `halted` (28 events) and without it (17).
            '{"event":"exit_signal","ts":"2026-08-08T14:00:00Z","armed":true,'
            '"halted":false,"triggers":[{"symbol":"WDC","reason":"stop",'
            '"price":41.2,"level":41.35,"fraction":1.0},{"symbol":"STX",'
            '"reason":"stop","price":714.325,"level":738.4014,"fraction":1.0}]}\n'
            '{"event":"exit_signal","ts":"2026-08-08T15:00:00Z","armed":true,'
            '"triggers":[{"symbol":"MRK","reason":"target1","price":153.01,'
            '"level":152.9,"fraction":0.5}]}\n')
        log = research_log(root, j)
        srcs = {d["source"] for d in log["recent_decisions"]}
        assert srcs == {"risk_review", "execution", "exit_signal"}, log
        assert any("earnings in 2 days" == d["reason"] for d in log["recent_decisions"])
        assert log["recent_decisions"][0]["when"] > log["recent_decisions"][-1]["when"]

        # ⛔ ASSERT THE FIELDS, NOT THE SOURCE LABEL. Checking `source` alone is
        # exactly what let the flat-fixture defect survive: the label is set
        # even when every value underneath it is None.
        es = [d for d in log["recent_decisions"] if d["source"] == "exit_signal"]
        assert len(es) == 3, es            # 2 events, one carrying TWO triggers
        assert {d["symbol"] for d in es} == {"WDC", "STX", "MRK"}, es
        by_sym = {d["symbol"]: d for d in es}
        assert by_sym["WDC"]["action"] == "stop", by_sym["WDC"]
        assert by_sym["MRK"]["action"] == "target1", by_sym["MRK"]
        # the reason carries the breach, so a session can read what happened
        assert "41.35" in by_sym["WDC"]["reason"], by_sym["WDC"]
        assert "714.325" in by_sym["STX"]["reason"], by_sym["STX"]
        # a PARTIAL names the fraction; a full close does not
        assert "50%" in by_sym["MRK"]["reason"], by_sym["MRK"]
        assert "%" not in by_sym["STX"]["reason"], by_sym["STX"]

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
