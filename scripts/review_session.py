#!/usr/bin/env python3
"""INDEPENDENT REVIEW of a trading session, by a DIFFERENT MODEL.

    .venv/bin/python scripts/review_session.py [--selftest] [--dry]

WHY A DIFFERENT MODEL
    The trading agent and the assistant that built this system are the same
    model. They share priors, so they share blind spots — and a reviewer that
    shares the bias cannot see it. Measured on 2026-08-12: the agent justified
    trimming two winners on "sector concentration"; the same-model reviewer read
    that reasoning and found it persuasive, because it is the reasoning that
    model produces. Both missed that a MOMENTUM book is supposed to concentrate
    in whatever is working.

    So the reviewer is Codex. Not because it is better — because it is
    *different*, and difference is the only property that matters here.

WHAT IT CAN DO
    Read everything the agent can read, through the same functions
    (scripts/agent_view.py). Nothing else. It cannot place, cancel, set a level
    or record a decision. A reviewer that can trade is a second trader.

WHAT ITS VERDICT DOES
    Nothing automatic, deliberately. It is journalled, pushed to the operator on
    a dissent, and scored against the tape alongside the agent's decisions
    (scripts/score_reviews.py). It is NOT a veto: a veto puts a second model on
    the critical path of every order, adds a failure mode, and replaces the
    agent's judgment with the reviewer's rather than testing it.

    ⛔ AND IT DOES NOT CURRENTLY REACH THE AGENT. It used to be injected into the
    next session's brief; that path is switched off at
    scripts/session.py:SHOW_REVIEW_TO_AGENT (2026-08-13, by the principal, who
    is evaluating the reviewer's judgment against the agent's before deciding
    whether feeding it back is useful at all). The agent is STATELESS, so a
    verdict shown once was never learning, and nothing named the review process
    to it — an unexplained critique is ambiguity the agent resolves by inventing
    a reason for it. So today the OPERATOR is the audience. Read that flag's
    comment before changing anything here on the assumption the agent sees this.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

PROMPT = REPO / "prompts" / "review.md"
OUT = REPO / "research_store" / "reviews"
# ⏱ THE ARITHMETIC HAS TO CLOSE, not just look reasonable.
#   close session starts 15:15, worst case 900s -> ends 15:30
#   + this review, worst case 600s              -> ends 15:40
#   risk_review starts                             15:45  (5 min of margin)
# Anything longer and the review is still running when an ARMED de-risk pass
# starts, which is two headless model runs at once on a ~2GB box -- the
# combination that forced a reboot on 2026-08-12. If you lengthen this, shorten
# TIMEOUT_S["close"] in scripts/session.py by the same amount.
TIMEOUT_S = 600

# Codex gates third-party MCP tool calls behind an approval its headless mode
# auto-cancels, so the reviewer reaches the book through agent_view.py instead —
# the same functions over a CLI the read-only sandbox already permits.
# ⚠️ REASONING EFFORT IS EXPLICIT. Codex defaults to `reasoning effort: none`
# (seen in its own startup banner), which is wrong for this job: the review has
# to re-derive concentration arithmetic, weigh a momentum book's exposure
# against a drawdown limit, and hold its own view against a persuasive
# counter-argument. A no-effort reviewer produces a fast opinion, and a fast
# opinion from a different model is not independence -- it is noise with a
# second brand on it.
# ⛔ MEMORY-CAPPED AND DE-PRIORITISED. This box has ~2GB and swaps under load
# (CLAUDE.md), and a headless model run is ~500MB. On 2026-08-12 running this
# reviewer alongside a live session, the monitor service, OpenD and an
# interactive session took the droplet down hard enough to need a full reboot --
# during market hours, with the stop watcher on it. A review is the LEAST
# important process on this machine: it judges work that has already happened.
# It must never be able to starve the thing watching live stops.
#
# systemd-run gives a hard MemoryMax the kernel enforces (the process is killed,
# not the box), CPUWeight puts it behind everything else, and nice reinforces it.
_LIMITS = ["systemd-run", "--scope", "--quiet",
           "--property=MemoryMax=700M", "--property=MemorySwapMax=200M",
           "--property=CPUWeight=20", "--property=TasksMax=200",
           "nice", "-n", "19", "ionice", "-c", "3"]

CODEX = _LIMITS + ["codex", "exec", "--sandbox", "read-only",
                   "-c", 'approval_policy="never"',
                   "-c", 'model_reasoning_effort="high"']

_BLOCK = re.compile(r"===VERDICT===(.*?)===END===", re.S)
_STANCES = ("AFFIRM", "DISSENT", "SPLIT")


def parse_verdict(text: str) -> dict:
    """Pull the structured block out of the reviewer's prose. Never raises.

    A missing or malformed block is reported as UNPARSED rather than guessed at.
    Inferring a stance from the surrounding prose would mean this file deciding
    what the reviewer meant — which is the same error as a same-model reviewer:
    one party supplying the other's conclusion.
    """
    # ⛔ THE LAST BLOCK, NEVER THE FIRST. The captured log contains our own
    # prompt echoed back before the reviewer speaks, and that prompt CONTAINS
    # the template block. Taking the first match parsed our own instructions as
    # the verdict: `STANCE: AFFIRM | DISSENT | SPLIT` read as AFFIRM, the
    # headline came back as the literal placeholder, and a real SPLIT was
    # recorded as an affirmation with NO phone push. Measured 2026-08-12 on the
    # first live review, whose actual verdict was SPLIT.
    blocks = _BLOCK.findall(text or "")
    if not blocks:
        return {"stance": "UNPARSED", "headline": "",
                "error": "no ===VERDICT=== block in the reviewer's output"}
    body = blocks[-1]
    out = {}
    for key in ("STANCE", "HEADLINE", "WHAT_I_WOULD_HAVE_DONE",
                "STRONGEST_DISAGREEMENT", "WHAT_WOULD_CHANGE_MY_MIND"):
        mm = re.search(rf"^\s*{key}:\s*(.+?)\s*$", body, re.M)
        out[key.lower()] = mm.group(1).strip() if mm else ""
    raw = (out.get("stance") or "").upper()
    # An unfilled template still reads "AFFIRM | DISSENT | SPLIT". Taking the
    # first word turns our own placeholder into a confident verdict, so a line
    # offering a CHOICE is refused outright rather than resolved.
    if "|" in raw:
        return {"stance": "UNPARSED", "headline": "",
                "error": "the verdict block is an unfilled template, not a verdict"}
    stance = raw.split()[0] if raw.split() else ""
    out["stance"] = stance if stance in _STANCES else "UNPARSED"
    # Same for the placeholders: an angle-bracketed field was never filled in.
    for k, v in list(out.items()):
        if isinstance(v, str) and v.startswith("<") and v.endswith(">"):
            out[k] = ""
    return out


def session_decisions(journal: Path, day: str) -> list:
    """Every decision the agent recorded on `day`, oldest first."""
    rows = []
    try:
        for line in journal.read_text().splitlines():
            try:
                e = json.loads(line)
            except Exception:       # noqa: BLE001
                continue
            if e.get("event") == "agent_decision" and str(e.get("ts", "")).startswith(day):
                rows.append(e)
    except Exception:               # noqa: BLE001
        pass
    return rows


def build_prompt(decisions_path: Path) -> str:
    """The reviewer's brief, with the agent's decisions behind a PATH.

    The decisions are handed over as a FILE PATH rather than pasted inline, so
    the reviewer forms its own view before it opens them — see prompts/review.md
    Phase 1. Pasting them here would put the agent's reasoning at the top of the
    reviewer's context and anchor it, producing agreement dressed as review.
    """
    return (PROMPT.read_text()
            + "\n\n## THE AGENT'S STANDING CHARTER — JUDGE AGAINST THIS\n\n"
            + _charter()
            + f"\n\n## THE SESSION UNDER REVIEW\n\nAfter you have written your "
              f"own Phase 1 view, read the agent's actual decisions here:\n\n"
              f"    {decisions_path}\n\nThat file is JSON, one decision per "
              f"entry, with the agent's own stated reasoning.\n")


def _charter() -> str:
    """The SAME charter the agent was given, rendered from the same config.

    ⚠️ NOT OPTIONAL, AND NOT A SUMMARY. review.md used to merely SUGGEST calling
    `brief`, which is advisory -- a reviewer that skipped it would be judging a
    mandated book against its own instincts, and would flag as reckless or as
    timid whatever differs from how it would run money. It has to be handed the
    same terms: the concentration limit, the drawdown limit, the universe, the
    regime rule, the strategy the agent is actually working to.

    Rendered live from mandate.toml + strategy.toml, never a copy -- a stale
    charter here means the reviewer judges against limits that no longer apply,
    and it would be right on its own terms and wrong in fact.

    This does NOT anchor Phase 1: the charter is the shared mandate both parties
    work to, not the reviewed party's reasoning. What is withheld until Phase 2
    is what the agent DECIDED.
    """
    try:
        import charter as charter_mod              # noqa: PLC0415
        import mandate as mandate_mod              # noqa: PLC0415
        import strategy as strategy_mod            # noqa: PLC0415
        import importlib.util                      # noqa: PLC0415
        spec = importlib.util.spec_from_file_location(
            "_review_srv", REPO / "src" / "agent_env" / "server.py")
        srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(srv)
        tools = [t for t in dir(srv) if not t.startswith("_")]
        return charter_mod.render(mandate_mod.load(), strategy_mod.load(), tools)
    except Exception as e:                         # noqa: BLE001
        # FAIL LOUD IN THE PROMPT ITSELF. A silently missing charter produces a
        # confident review against the wrong standard, which is worse than none.
        return (f"[!] THE CHARTER COULD NOT BE RENDERED: {type(e).__name__}: {e}\n"
                f"You are missing the mandate this book is run under. Say so in "
                f"your verdict and do not assert a stance you cannot support.")


def busy_now() -> str | None:
    """Is this a bad moment to spend 700MB and a CPU? -> reason, or None.

    The reviewer is the lowest-priority process on the box. It must not run
    while a trading session holds the lock (two model runs at once is what took
    the droplet down), and it must not run during regular trading hours, when
    the stop watcher needs the machine. Nothing here is time-critical: the
    session being reviewed is already over.
    """
    import fcntl                                    # noqa: PLC0415
    lock = REPO / "research_store" / "session.lock"
    if lock.exists():
        try:
            fh = open(lock, "r+")
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
        except BlockingIOError:
            return "a trading session is running — it gets the machine, not me"
        except Exception:                           # noqa: BLE001
            pass
    try:
        avail = int([l for l in open("/proc/meminfo")
                     if l.startswith("MemAvailable")][0].split()[1]) // 1024
        if avail < 800:
            return f"only {avail}MB available — refusing to push this box into swap"
    except Exception:                               # noqa: BLE001
        pass
    return None


def run(dry: bool = False, force: bool = False) -> dict:
    if not force:
        busy = busy_now()
        if busy:
            return {"ok": True, "skipped": busy}
    day = datetime.now(timezone.utc).date().isoformat()
    journal = REPO / "research_store" / "journal.jsonl"
    decisions = session_decisions(journal, day)
    if not decisions:
        return {"ok": True, "skipped": "no agent decisions today — nothing to review"}

    OUT.mkdir(parents=True, exist_ok=True)
    dpath = OUT / f"{day}-decisions.json"
    dpath.write_text(json.dumps(decisions, indent=2))

    if dry:
        return {"ok": True, "dry": True, "decisions": len(decisions),
                "prompt_bytes": len(build_prompt(dpath))}

    # STREAM TO DISK AS IT RUNS, don't buffer. capture_output holds everything
    # until the process exits, so a review that takes minutes is invisible while
    # it happens and there is nothing to watch or debug if it hangs. Tee to a
    # live log instead: `tail -f research_store/reviews/live.log`.
    live = OUT / "live.log"
    try:
        with live.open("w") as fh:
            proc = subprocess.run(CODEX + [build_prompt(dpath)],
                                  cwd=str(REPO), stdout=fh,
                                  stderr=subprocess.STDOUT, text=True,
                                  timeout=TIMEOUT_S)
        text = live.read_text()
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"reviewer timed out after {TIMEOUT_S}s"}
    except Exception as e:          # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    verdict = parse_verdict(text)

    # ⛔ A REVIEWER THAT NEVER SPOKE IS NOT A VERDICT OF "UNPARSED".
    # proc.returncode went unread until 2026-08-13, and the two failures it
    # conflates are not the same event:
    #   - the reviewer RAN and its prose lacked the block  -> UNPARSED, a real
    #     (if useless) opinion, worth journalling and scoring against.
    #   - the reviewer NEVER LAUNCHED (binary off cron's PATH, MemoryMax kill,
    #     sandbox refusal) -> there is no opinion at all.
    # Recorded identically, the second one is INVISIBLE: it writes a codex_review
    # event, overwrites latest.json, and hands score_reviews.py a day's decisions
    # marked "reviewed, unscoreable" — a scorecard that reports the reviewer
    # examined work it never saw. Measured 2026-08-12: the first and ONLY cron
    # review recorded 8 such decisions while `codex` was not on cron's PATH.
    # Exit status is the general signal here — it catches the missing binary, an
    # OOM kill and a crash alike, so this stays ONE check rather than a preflight
    # per failure mode.
    if verdict["stance"] == "UNPARSED" and proc.returncode != 0:
        (OUT / f"{day}-review.txt").write_text(text)
        err = (f"the reviewer never produced a verdict: exit {proc.returncode}. "
               f"Last output: {(text or '').strip()[-300:] or '(nothing)'}")
        # Nothing else pages on this. run_session.sh runs this behind `|| true`
        # so a dead reviewer can never fail a trading run — which is right, and
        # is exactly why the silence has to be broken HERE. A reviewer that has
        # been dead for weeks is the failure this system exists to not have.
        try:
            import notify                          # noqa: PLC0415
            notify.push("Independent review did not run", err, tags="warning")
        except Exception:           # noqa: BLE001
            pass
        return {"ok": False, "error": err, "returncode": proc.returncode}

    (OUT / f"{day}-review.txt").write_text(text)
    (OUT / "latest.json").write_text(json.dumps(
        {"day": day, "reviewed_decisions": len(decisions), **verdict}, indent=2))

    try:
        from research_store import store          # noqa: PLC0415
        store.append_journal({
            "event": "codex_review",
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "reviewer": "codex", "reviewed_decisions": len(decisions),
            **{k: v for k, v in verdict.items() if k != "error"},
        })
    except Exception:               # noqa: BLE001
        pass

    # The operator hears about a DISSENT, not an affirmation. An alert that
    # fires when nothing is wrong is one that gets ignored when something is.
    if verdict.get("stance") in ("DISSENT", "SPLIT"):
        try:
            import notify                          # noqa: PLC0415
            notify.push(f"Reviewer {verdict['stance']}: today's session",
                        f"{verdict.get('headline','')}\n\n"
                        f"Would have: {verdict.get('what_i_would_have_done','')}",
                        tags="mag")
        except Exception:           # noqa: BLE001
            pass

    return {"ok": True, "stance": verdict.get("stance"),
            "headline": verdict.get("headline"), "decisions": len(decisions)}


def _selftest() -> None:
    good = """blah blah
===VERDICT===
STANCE: DISSENT
HEADLINE: Trimmed the strongest names in a rising tape.
WHAT_I_WOULD_HAVE_DONE: Held MU and TER, cut nothing.
STRONGEST_DISAGREEMENT: Concentration is the strategy working, not a risk.
WHAT_WOULD_CHANGE_MY_MIND: Drawdown room under 3 points.
===END===
trailing"""
    v = parse_verdict(good)
    assert v["stance"] == "DISSENT", v
    assert "strongest names" in v["headline"], v
    assert v["what_i_would_have_done"].startswith("Held MU"), v

    # ⛔ THE PROMPT IS ECHOED BACK BEFORE THE REVIEWER SPEAKS, and it contains
    # the template. Parsing the FIRST block read our own instructions as the
    # verdict and turned a real SPLIT into an AFFIRM with no push.
    echoed = ("===VERDICT===\n"
              "STANCE: AFFIRM | DISSENT | SPLIT\n"
              "HEADLINE: <one sentence, plain English>\n"
              "===END===\n"
              "...reviewer works...\n"
              "===VERDICT===\n"
              "STANCE: SPLIT\n"
              "HEADLINE: Risk cuts fine, idle cash not.\n"
              "===END===")
    v2 = parse_verdict(echoed)
    assert v2["stance"] == "SPLIT", v2
    assert v2["headline"] == "Risk cuts fine, idle cash not.", v2
    # the template ALONE must never resolve to a stance
    tmpl = ("===VERDICT===\nSTANCE: AFFIRM | DISSENT | SPLIT\n"
            "HEADLINE: <one sentence>\n===END===")
    assert parse_verdict(tmpl)["stance"] == "UNPARSED", parse_verdict(tmpl)
    # and an unfilled angle-bracket field must not be reported as content
    onep = ("===VERDICT===\nSTANCE: DISSENT\nHEADLINE: <one sentence>\n===END===")
    assert parse_verdict(onep)["headline"] == "", parse_verdict(onep)

    assert parse_verdict("STANCE: AFFIRM but no block")["stance"] == "UNPARSED"
    assert parse_verdict("")["stance"] == "UNPARSED"
    assert parse_verdict(None)["stance"] == "UNPARSED"
    # an unrecognised stance is UNPARSED, never coerced into a real one --
    # guessing would mean this file supplying the reviewer's conclusion
    assert parse_verdict("===VERDICT===\nSTANCE: MAYBE\n===END===")["stance"] == "UNPARSED"
    for s in ("AFFIRM", "SPLIT"):
        assert parse_verdict(f"===VERDICT===\nSTANCE: {s}\n===END===")["stance"] == s

    # decisions are filtered to TODAY and to agent_decision only
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"event": "agent_decision", "ts": "2026-08-12T14:00:00+00:00", "symbol": "MU"}) + "\n")
        f.write(json.dumps({"event": "agent_decision", "ts": "2026-08-11T14:00:00+00:00", "symbol": "OLD"}) + "\n")
        f.write(json.dumps({"event": "execution", "ts": "2026-08-12T14:00:00+00:00"}) + "\n")
        f.write("not json\n")
        p = Path(f.name)
    try:
        got = session_decisions(p, "2026-08-12")
        assert len(got) == 1 and got[0]["symbol"] == "MU", got
        assert session_decisions(p, "2026-01-01") == []
        assert session_decisions(Path("/nonexistent"), "2026-08-12") == []
    finally:
        p.unlink()

    # the agent's reasoning must NOT be inlined -- it is handed over as a path,
    # so the reviewer forms its own view before reading it (review.md Phase 1)
    pr = build_prompt(Path("/tmp/x-decisions.json"))
    assert "/tmp/x-decisions.json" in pr
    assert "Phase 1" in pr or "own view" in pr

    # THE REVIEWER IS BRIEFED THE SAME. It must receive the agent's actual
    # charter -- the mandate, limits and strategy -- or it judges a mandated
    # book against its own instincts and calls the difference an error.
    assert "STANDING CHARTER" in pr, "reviewer was not handed the charter"
    ch = _charter()
    assert "THE CHARTER COULD NOT BE RENDERED" not in ch, ch[:200]
    assert len(ch) > 5000, f"charter suspiciously short: {len(ch)} bytes"
    for must in ("THE JOB", "concentration", "universe"):
        assert must.lower() in ch.lower(), f"charter missing {must!r}"
    assert ch in pr, "the rendered charter did not reach the prompt"

    # the reviewer must not run at the CLI's default effort of "none"
    assert 'model_reasoning_effort="high"' in CODEX, CODEX
    assert 'approval_policy="never"' in CODEX, CODEX

    # ⛔ AND IT MUST BE CAPPED. Unbounded, this took the droplet down on
    # 2026-08-12 during market hours, with the live stop watcher on the same
    # box. The kernel kills the reviewer; it must never be able to kill the box.
    assert CODEX[0] == "systemd-run", CODEX[:3]
    assert any("MemoryMax" in a for a in CODEX), CODEX
    assert any("CPUWeight" in a for a in CODEX), CODEX
    assert "nice" in CODEX, CODEX

    # busy_now() must refuse rather than compete
    assert busy_now.__doc__ and "lowest-priority" in busy_now.__doc__

    # ⛔ "THE REVIEWER NEVER RAN" IS NOT A VERDICT. Exit status went unread
    # until 2026-08-13, so a reviewer that never launched (codex off cron's
    # PATH) was recorded as a UNPARSED *opinion*: journalled, latest.json
    # overwritten, and a day of decisions handed to the scorecard as "reviewed".
    # These three cases must stay distinguishable.
    import unittest.mock as _mock                  # noqa: PLC0415
    import notify as _notify                       # noqa: PLC0415
    from research_store import store as _store      # noqa: PLC0415

    _live_j = REPO / "research_store" / "journal.jsonl"
    _live_before = _live_j.stat().st_size if _live_j.exists() else 0

    class _P:
        def __init__(self, rc): self.returncode = rc

    _wrote = []

    def _run_with(rc, out, tmp):
        """run() against a fake reviewer that exits `rc` and prints `out`.

        ⛔ append_journal IS PATCHED, and that is not optional. run() journals on
        every path that reaches a verdict, so an unpatched selftest writes FAKE
        codex_review events into the LIVE ledger — which is a backed-up,
        append-only record that score_reviews.py then reads as real reviews.
        Done exactly that on 2026-08-13: four bogus events, removed by hand.
        Patch every writer run() can reach, not just the ones the case under
        test is aiming at.
        """
        with _mock.patch.object(sys.modules[__name__], "OUT", tmp), \
             _mock.patch.object(sys.modules[__name__], "session_decisions",
                                lambda *a, **k: [{"symbol": "MU", "action": "hold"}]), \
             _mock.patch.object(_notify, "push", lambda *a, **k: None), \
             _mock.patch.object(_store, "append_journal", _wrote.append), \
             _mock.patch.object(subprocess, "run",
                                lambda *a, **k: (tmp.joinpath("live.log")
                                                 .write_text(out), _P(rc))[1]):
            return run(force=True)

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # 1. never launched -> a FAILURE, not a stance. Nothing is journalled.
        r = _run_with(127, "ionice: failed to execute codex: No such file or "
                           "directory\n", tmp)
        assert r["ok"] is False, r
        assert r["returncode"] == 127, r
        assert "never produced a verdict" in r["error"], r
        assert "codex" in r["error"], "the cause must survive into the error"
        assert "stance" not in r, f"a dead reviewer must not report a stance: {r}"
        assert not (tmp / "latest.json").exists(), \
            "a reviewer that never ran must not overwrite the last real verdict"
        # THE LEDGER IS THE POINT. A dead reviewer that still journals is how a
        # scorecard comes to report decisions "reviewed" by something that never
        # saw them -- the 2026-08-12 defect this whole guard exists for.
        assert _wrote == [], f"a reviewer that never ran journalled anyway: {_wrote}"

        # 2. exited non-zero but DID speak -> the verdict stands. The opinion is
        #    what matters; a reviewer may say its piece and still exit badly.
        r = _run_with(1, "===VERDICT===\nSTANCE: SPLIT\nHEADLINE: Idle cash.\n"
                         "===END===", tmp)
        assert r["ok"] is True and r["stance"] == "SPLIT", r
        assert len(_wrote) == 1 and _wrote[0]["stance"] == "SPLIT", _wrote

        # 3. exited CLEAN but rambled -> a real (useless) opinion. Still UNPARSED,
        #    still recorded — that is the reviewer's failure, not the plumbing's.
        r = _run_with(0, "I have thoughts but no block.", tmp)
        assert r["ok"] is True and r["stance"] == "UNPARSED", r
        assert len(_wrote) == 2, _wrote

    # ⛔ AND THE LIVE LEDGER MUST BE BYTE-IDENTICAL. The asserts above only prove
    # the patched writer was called as expected; this proves nothing slipped past
    # it. A selftest that mutates the record it is testing against is worse than
    # no selftest.
    _live = REPO / "research_store" / "journal.jsonl"
    if _live.exists():
        assert _live.stat().st_size == _live_before, (
            f"THE SELFTEST WROTE TO THE LIVE JOURNAL: {_live_before} -> "
            f"{_live.stat().st_size} bytes. Patch every writer run() reaches.")

    print("review_session: OK -- verdict parsed, malformed never guessed at, "
          "decisions filtered to today, reasoning passed by path not inlined, "
          "a reviewer that never launched is a failure and not a stance")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="run even if the box is busy (you are watching it)")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
        raise SystemExit(0)
    r = run(dry=a.dry, force=a.force)
    print(json.dumps(r, indent=2))
    raise SystemExit(0 if r.get("ok") else 1)
