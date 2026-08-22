"""THE EVIDENCE REBUILD — the one orchestration that produces the artifact.

WHY THIS MODULE EXISTS
    Two callers need the identical rebuild: the operator CLI
    (scripts/build_institutional_evidence.py) and the session runner
    (scripts/session.py), which rebuilds at startup so a stateless agent is
    never handed evidence assembled before the experience it is supposed to
    describe. Two copies of "load journal, load panel, measure, build, write"
    would drift, and the direction they drift is always the same: the operator
    checks one implementation and the session runs the other.

    So this is the ONLY implementation. It is thin on purpose — every number
    still comes from behavior_measurement and institutional_evidence, both pure
    and both untouched by this file.

TWO DATES, AND THEY ARE NOT THE SAME DATE
    ⛔ `measurement_as_of` — THE MARKET FRONTIER. The last session actually
    present in the panel. It governs, and only governs, which forward returns can
    truthfully be measured. It defaults to the panel's own last session, and
    nothing may push it past that: a horizon the tape has not reached is
    `pending`, never a number.

    ⛔ `evidence_as_of` — WHEN THE EVIDENCE IS BEING CONSUMED. The date of the
    session about to read it. It governs the 180-day window, the age of the
    newest supporting decision, and the 45-day staleness rule.

    THEY WERE ONE VALUE AND THAT WAS A BUG. With both taken from the panel, a
    panel that stopped updating froze evidence aging: a session on Aug 22 reading
    a panel that ends Aug 15 aged its evidence as though it were Aug 15, so a
    decision 52 days old measured 45 and stayed agent-visible one week past the
    staleness line. Evidence freshness is a property of the SESSION consuming it,
    never of how current the price file happens to be.

    Determinism is unchanged: neither date is read from a clock HERE. The caller
    supplies `evidence_as_of` (the session runner passes its own date), and the
    frontier is a fact about the data.

THE WRITE IS ATOMIC OR IT DOES NOT HAPPEN
    tmp-in-the-same-directory + os.replace, the discipline decide.write_levels
    and evidence_receipt.write already use. A session reads this artifact
    milliseconds after it is written and stamps its version onto real decisions;
    a torn read there is a false provenance, not a cosmetic glitch.

WHAT IT DOES NOT DO
    No journal append, no broker call, no order, no scheduling. It never decides
    what is validated — that is the pure builder's sample-size arithmetic — and
    it never edits an existing artifact: every run recomputes from source inside
    the window, reading the previous artifact for exactly one fact (whether an
    item ever met the validated thresholds).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

JOURNAL = REPO / "research_store" / "journal.jsonl"
PANEL = REPO / "research_store" / "prices" / "closes.parquet"
ARTIFACT = REPO / "research_store" / "reviews" / "institutional_evidence.json"


def read_journal(path=None) -> list:
    """The journal, read-only. A malformed line is REPORTED, never guessed at."""
    p = Path(JOURNAL if path is None else path)
    if not p.exists():
        return []
    out = []
    for n, line in enumerate(p.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            print(f"journal line {n} is not JSON ({exc}); skipped", file=sys.stderr)
    return out


def read_panel(path=None) -> dict:
    """The close panel in the shape behavior_measurement wants."""
    import behavior_measurement as bm                        # noqa: PLC0415
    p = Path(PANEL if path is None else path)
    if not p.exists():
        print(f"no price panel at {p}; every return will read unavailable",
              file=sys.stderr)
        return {"sessions": [], "series": {}}
    import pandas as pd                                      # noqa: PLC0415
    return bm.panel_from_frame(pd.read_parquet(p))


def read_previous(path=None):
    """The last artifact, or None. Unreadable is REPORTED and treated as absent.

    ⛔ ONE FACT IS CARRIED FORWARD AND NO OTHER: whether an item ever met the
    validated thresholds. Without it a validated item that decays to nothing
    reads as brand-new-insufficient and its staleness is silently lost. Every
    statistic is recomputed from source.
    """
    p = Path(ARTIFACT if path is None else path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"previous artifact at {p} is unreadable ({exc}); treating this "
              f"as the first build — no item will be reported stale on decay",
              file=sys.stderr)
        return None


def write_artifact(evidence: dict, path=None) -> Path:
    """Write the artifact ATOMICALLY. -> the path written.

    tmp + os.replace in the SAME directory, so a reader either sees the whole
    previous artifact or the whole new one and never a half-written file. The
    session runner reads this back within milliseconds of the write and stamps
    its `version` onto real decisions; a torn read is a false provenance.
    """
    target = Path(ARTIFACT if path is None else path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, target)
    return target


def rebuild(*, evidence_as_of=None, measurement_as_of=None, journal_path=None,
            panel_path=None, out_path=None, window_days=None, write=True) -> dict:
    """Rebuild institutional evidence from current source data. -> the artifact.

    `evidence_as_of` is WHEN THE EVIDENCE IS BEING CONSUMED — the 180-day window,
    the age of the newest supporting decision and the 45-day staleness rule are
    all measured from it. The session runner passes its own date. Absent, it
    falls back to the market frontier, which is only correct for an operator
    inspecting the artifact rather than a session about to act on it.

    `measurement_as_of` is THE MARKET FRONTIER — the last session the panel
    actually contains, which bounds what can be measured. It defaults to the
    panel's own last session and is never pushed past it: an unmeasurable horizon
    stays `pending`, and a stale panel narrows what is measurable without
    freezing how old the evidence is.

    RAISES rather than returning something plausible. A caller that cannot tell
    a failed rebuild from a thin one will deliver whatever is on disk, and the
    whole point of rebuilding before a session is that the session never reads
    evidence whose freshness is uncertain.

    `write=False` computes the artifact and touches nothing — the dry-run path,
    which holds no session lock and therefore must not write the file a live
    session may be reading.
    """
    import behavior_measurement as bm                        # noqa: PLC0415
    import institutional_evidence as ie                      # noqa: PLC0415
    import lifecycle_outcomes as lo                          # noqa: PLC0415

    journal = read_journal(journal_path)
    panel = read_panel(panel_path)

    frontier = measurement_as_of or (panel["sessions"][-1] if panel["sessions"] else None)
    stamp = evidence_as_of or frontier
    if not stamp:
        raise RuntimeError("no evidence_as_of and no price panel: there is no "
                           "date to build an evidence window against")

    # THE FRONTIER BOUNDS THE MEASUREMENT, THE STAMP AGES THE EVIDENCE. Passing
    # the frontier here (never the session date) is what keeps a stale panel from
    # inventing outcomes: `measure` treats as_of as a ceiling on what has
    # elapsed, so it can only ever shrink what is measurable, never invent it.
    measurement = bm.measure(journal, panel, as_of=frontier)
    evidence = ie.build(
        measurement,
        as_of=stamp,
        window_days=window_days or ie.WINDOW_DAYS,
        previous=read_previous(out_path),
        reasons=ie.reason_index(lo.project(journal)),
    )
    if write:
        write_artifact(evidence, out_path)
    return evidence


def _selftest() -> None:
    import tempfile                                          # noqa: PLC0415
    with tempfile.TemporaryDirectory() as d:
        j = Path(d) / "journal.jsonl"
        j.write_text('{"event":"note"}\nnot json\n\n{"event":"note2"}\n')
        assert read_journal(j) == [{"event": "note"}, {"event": "note2"}], \
            "a malformed line must be skipped, never guessed at"
        assert read_journal(Path(d) / "missing.jsonl") == []

        art = Path(d) / "reviews" / "institutional_evidence.json"
        assert read_previous(art) is None, "no artifact -> no history"
        payload = {"version": "ev1-test", "items": []}
        written = write_artifact(payload, art)
        assert written == art and json.loads(art.read_text()) == payload
        assert not art.with_suffix(".json.tmp").exists(), "the tmp file must be gone"
        assert read_previous(art) == payload
        art.write_text("{ truncated")
        assert read_previous(art) is None, "unreadable history -> absent, not fatal"

        # no panel and no as_of is a REFUSAL, never a guessed date
        try:
            rebuild(journal_path=j, panel_path=Path(d) / "nope.parquet",
                    out_path=art, write=False)
            raise AssertionError("should have refused: no date to build against")
        except RuntimeError as e:
            assert "no date" in str(e), e

        # ...and with an explicit evidence_as_of the same empty inputs build
        ev = rebuild(evidence_as_of="2026-08-21", journal_path=j,
                     panel_path=Path(d) / "nope.parquet", out_path=art, write=False)
        assert ev["as_of"] == "2026-08-21" and ev["window"]["days"] == 180
        assert ev["agent_visible_evidence_ids"] == [], "empty input cannot validate"
        assert art.read_text() == "{ truncated", \
            "write=False must leave the artifact byte-identical"
    print("evidence_build: OK — one rebuild, explicit as_of, atomic write")


if __name__ == "__main__":
    sys.path.insert(0, str(REPO / "src"))
    _selftest()
