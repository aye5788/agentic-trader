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
# ⏱ MEASURED, n=1 -- and it was nearly binding.
#
#   2026-08-13 close review: 8m18s (498s) to review FIVE decisions.
#   Against the old 600s limit that is 83% of budget consumed, on a LIGHT day.
#   A busier session would have hit the wall and the review would have failed.
#
# The old 600s existed for an arithmetic that no longer applies: close 15:15
# + 900s session -> 15:30, + 600s review -> 15:40, before risk_review at 15:45.
# ⛔ That job was RETIRED 2026-08-13 (deploy/crontab.template) and nothing armed
# runs after this any more, so the constraint that set 600s is gone.
#
# 1200s is ~2.4x the single observed run -- headroom, NOT a distribution. One
# sample says nothing about the tail. Deliberately generous: the failure this
# repo keeps repeating is a tight guessed limit that silently kills the job
# (the reviewer's 700M memory cap did exactly that, every run, for a day).
# Tighten from RECORDED durations once several have accumulated, never from a
# guess. Remaining downstream bound: log_equity at 16:15, which 15:15 + 900s
# + 1200s = 15:50 clears comfortably.
TIMEOUT_S = 1200

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
# ⚠️ 700M WAS TOO SMALL AND SILENTLY KILLED EVERY REVIEW. Raised to 1200M/300M
# on 2026-08-13 after the first review to survive the PATH fix was SIGKILLed:
#
#   memory: usage 716800kB, limit 716800kB, failcnt 31715
#   swap:   usage 204800kB, limit 204800kB, failcnt   190
#   oom-kill:constraint=CONSTRAINT_MEMCG ... task=codex
#
# CONSTRAINT_MEMCG, not CONSTRAINT_NONE: it hit ITS OWN ceiling with ~1GB free on
# the box. A cgroup cap is private and absolute — it does not widen when the
# machine is idle, so this would have died identically on a quiet droplet. Both
# ceilings were pinned at 100% and it was still asking (31,715 failed charges),
# so the job needs more than the 900M total it was allowed.
#
# Two things make it hungrier than it looks: the cap covers CHILDREN (every
# agent_view.py call spawns a fresh pandas-importing Python inside this cgroup —
# the kill log shows a python child dying first), and cgroup memory counts PAGE
# CACHE, so reading the parquet price panel charges against the limit.
#
# ⛔ THE CAP ITSELF IS NOT NEGOTIABLE, only its size. Uncapped, this took the
# whole droplet down on 2026-08-12 during market hours with the live stop watcher
# on it. The cap is what turns "the box dies" into "the review dies". Sizing:
# 1200M+300M on a 1963M box leaves ~500M for the monitor, OpenD, dashboard and
# cloudflared, and the review runs strictly AFTER session.py has exited, so it
# never overlaps another model run.
# ⛔ THE RESOURCE LIMITS ARE NOT HERE ANY MORE. They live in
# /etc/systemd/system/agentic-review.service, which is how this job is started
# (deploy/run_session.sh runs `systemctl start --wait agentic-review.service`).
#
# This file used to assemble a `systemd-run --scope --property=MemoryMax=...`
# command line out of Python strings. That was the service manager,
# reimplemented in a script: invisible to `systemctl show`, untestable without
# running it, and it put a tuned kill-limit on the critical path of every review.
# A guessed 700M in that list OOM-killed every review the system ever ran.
#
# Anything that needs to bound this job — memory, CPU share, IO priority, run
# time, no-overlap — belongs in the unit file, not here.
CODEX = ["codex", "exec", "--sandbox", "read-only",
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
    """Is this a bad moment to spend ~1.2GB and a CPU? -> reason, or None.

    The reviewer is the lowest-priority process on the box. It must not run
    while a trading session holds the lock — two model runs at once is what took
    the droplet down on 2026-08-12 — and it must not push a ~2GB machine that is
    already short of memory into swap while the stop watcher is live.

    ⛔ THERE IS DELIBERATELY NO REGULAR-TRADING-HOURS GATE, and this docstring
    used to claim one it never had. Do not "restore" it: BOTH reviews run inside
    RTH by construction, so a time window would mean no reviews at all.

        open  10:35 ET + up to 1800s session -> review starts <= 11:05
        close 15:15 ET + up to  900s session -> review starts <= 15:30

    RTH is 09:30-16:00. What actually protects the box is not a clock but the
    ordering (this runs only after session.py has returned and released the
    lock), the kernel-enforced MemoryMax the reviewer runs under, CPUWeight and
    nice/ionice behind everything else, and the memory floor below. A stale
    aspiration in a docstring is how a control that does nothing gets added, or
    a working one silently switched off — this repo's recurring defect.

    Nothing here is time-critical: the session being reviewed is already over.
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


def run(dry: bool = False, force: bool = False, day: str | None = None,
        replay: bool = False) -> dict:
    """Review a session. `day`/`replay` exist to TEST this path, not to use it.

    ⛔ REPLAY IS READ-ONLY AGAINST THE LEDGER. A replay re-reviews an earlier
    day so the full prompt -> codex -> verdict chain can be exercised on demand
    (there is otherwise no way to test it except waiting for a live session, and
    "we'll see tomorrow" is how the reviewer stayed broken for a day). It must
    therefore never look like a real review: it does NOT journal a codex_review
    event and does NOT overwrite latest.json, because a replayed verdict is not
    a new opinion about the book and score_reviews must not price it as one.
    Its artifacts go to reviews/replay/ instead.
    """
    if not force:
        busy = busy_now()
        if busy:
            return {"ok": True, "skipped": busy}
    day = day or datetime.now(timezone.utc).date().isoformat()
    journal = REPO / "research_store" / "journal.jsonl"
    decisions = session_decisions(journal, day)
    if not decisions:
        return {"ok": True,
                "skipped": f"no agent decisions on {day} — nothing to review"}

    global OUT
    if replay:
        OUT = REPO / "research_store" / "reviews" / "replay"
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
            # ⛔ stdin MUST be closed. `codex exec` reads stdin for extra input
            # even when the prompt is passed as an argument, and blocks until
            # EOF. Under cron/systemd stdin is /dev/null so it EOFs instantly
            # and this never showed -- but run from any context with an
            # inherited open stdin (an interactive invocation, a wrapper that
            # pipes) it hangs to TIMEOUT_S, writes nothing, and records NO
            # VERDICT. That is indistinguishable from "the reviewer never ran",
            # which OPSLOG 2026-08-13 already documents as a silent failure.
            # Observed twice on 2026-08-18 running codex by hand. One argument
            # removes the dependency on the caller's environment entirely.
            proc = subprocess.run(CODEX + [build_prompt(dpath)],
                                  cwd=str(REPO), stdout=fh,
                                  stderr=subprocess.STDOUT, text=True,
                                  stdin=subprocess.DEVNULL,
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
    # ⛔ A REPLAY IS NOT THE LATEST VERDICT. latest.json is what the operator and
    # (when reconnected) the brief read as "the current opinion about the book".
    # A re-review of an old day is a test artifact, not a new opinion.
    if not replay:
        (OUT / "latest.json").write_text(json.dumps(
            {"day": day, "reviewed_decisions": len(decisions), **verdict}, indent=2))

    # ⛔ A REPLAY NEVER TOUCHES THE LEDGER. score_reviews.py joins codex_review
    # events to the decisions they examined; a replayed verdict would be scored
    # as a second, later opinion on a day already judged — and under the
    # same-scope-latest-wins rule it would SUPERSEDE the real one.
    try:
        from research_store import store          # noqa: PLC0415
        if replay:
            raise RuntimeError("replay: ledger deliberately untouched")
        store.append_journal({
            "event": "codex_review",
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "reviewer": "codex", "reviewed_decisions": len(decisions),
            # ⛔ WHICH decisions, not just how many. score_reviews.py used to
            # join a verdict to a decision BY DATE, and there are two sessions a
            # day -- so the close verdict overwrote the open one and every
            # decision that day was scored against whichever came last. The
            # reviewer is the only party that knows its own scope; it is written
            # down here rather than guessed at downstream (OPSLOG 2026-08-13).
            "reviewed": [d.get("ts") for d in decisions if d.get("ts")],
            **{k: v for k, v in verdict.items() if k != "error"},
        })
    except Exception:               # noqa: BLE001
        pass

    # The operator hears about a DISSENT, not an affirmation. An alert that
    # fires when nothing is wrong is one that gets ignored when something is.
    if verdict.get("stance") in ("DISSENT", "SPLIT") and not replay:
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
    # ⛔ THE LIMITS MUST NOT COME BACK INTO THIS FILE. They belong to the unit.
    assert CODEX[0] == "codex", CODEX[:3]
    assert not any("systemd-run" in a or "MemoryMax" in a for a in CODEX), (
        "resource limits are being assembled in Python again — they belong in "
        "/etc/systemd/system/agentic-review.service, where `systemctl show` can "
        "display them and no guessed number sits in a script")

    # ...and the unit must actually carry them, so a deploy that forgets the
    # unit fails HERE and not at 15:25 on a live box.
    #
    # Reads the REPO copy, which is the source of truth and is what code review
    # sees. Whether the box is running that copy is a separate question, checked
    # by repo_checks.check_units_match_installed — deliberately not conflated:
    # this asserts the config is CORRECT, that one asserts it is DEPLOYED.
    _unit = REPO / "deploy" / "agentic-review.service"
    if _unit.exists():
        _u = _unit.read_text()
        # ⛔ DIRECTIVES ONLY, NEVER SUBSTRING MATCHES. This first checked
        # `"RuntimeMaxSec=" in text`, which passed on a COMMENT explaining that
        # RuntimeMaxSec is inert — a test that was green because of prose
        # describing the absence of the thing it was testing for. Parse the
        # actual settings.
        _set = dict(l.split("=", 1) for l in
                    (x.strip() for x in _u.splitlines())
                    if l and not l.startswith("#") and not l.startswith("[") and "=" in l)
        for _need in ("MemoryHigh", "MemoryMax", "MemoryAccounting",
                      "TimeoutStartSec", "Environment"):
            assert _need in _set, f"agentic-review.service is missing {_need}="
        assert "/root/.local/bin" in _u, "unit PATH lacks codex's directory"
        # ⛔ TimeoutStartSec is the bound for Type=oneshot; RuntimeMaxSec is
        # SILENTLY IGNORED there (`systemd-analyze verify`) while still being
        # reported by `systemctl show`. It must not reappear as a directive
        # pretending to guard something.
        assert "RuntimeMaxSec" not in _set, (
            "RuntimeMaxSec is inert for Type=oneshot — it enforces nothing here "
            "and reads as a guard. TimeoutStartSec is the real bound.")
        # MemoryHigh THROTTLES, MemoryMax KILLS. High must sit below Max or the
        # throttle never engages and this is a bare kill-limit again — the
        # 2026-08-13 failure, where a guessed 700M killed every run there was.
        _hi = int(_u.split("MemoryHigh=")[1].split("M")[0])
        _mx = int(_u.split("MemoryMax=")[1].split("M")[0])
        assert _hi < _mx, f"MemoryHigh={_hi}M must sit below MemoryMax={_mx}M"
        assert _mx >= 1200, (
            f"MemoryMax={_mx}M — the reviewer provably needs >900M (it exhausted "
            f"700M RAM + 200M swap and was still allocating). Tighten this from a "
            f"recorded peak, never from a guess.")

    # busy_now() must refuse rather than compete
    assert busy_now.__doc__ and "lowest-priority" in busy_now.__doc__
    # ...and it must NOT grow a clock. Both reviews run inside RTH by
    # construction (open <= 11:05, close <= 15:30 ET), so a trading-hours gate
    # would silently mean no reviews at all. Pinned against a future tidy-up
    # that reads the old aspiration and "restores" it. If you genuinely intend
    # to add one, you have to delete this line, which means reading why.
    assert "NO REGULAR-TRADING-HOURS GATE" in busy_now.__doc__
    import inspect                                  # noqa: PLC0415
    _body = inspect.getsource(busy_now).split('"""')[2]
    for clocky in ("datetime", "time.localtime", "strftime", "hour"):
        assert clocky not in _body, (
            f"busy_now() grew a clock ({clocky!r}). Both reviews run inside RTH; "
            f"a time gate disables the reviewer entirely — read the docstring.")

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
    ap.add_argument("--day", help="review this UTC date instead of today (test)")
    ap.add_argument("--replay", action="store_true",
                    help="TEST MODE: re-review an earlier day without journalling "
                         "a codex_review, overwriting latest.json, or pushing. "
                         "Artifacts go to research_store/reviews/replay/.")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
        raise SystemExit(0)
    r = run(dry=a.dry, force=a.force, day=a.day, replay=a.replay)
    print(json.dumps(r, indent=2))
    raise SystemExit(0 if r.get("ok") else 1)
