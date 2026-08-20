"""Nightly ranking history — what the re-rank actually CHANGED.

⛔ WHY THIS EXISTS. The slow loop recomputes the momentum ranking every
weeknight, and until 2026-08-20 that ranking was **discarded on six nights out
of seven**: the book only rotates on Sunday, so Mon–Fri `hold_selection()`
threw the fresh ranks away and nothing downstream ever saw them. A nightly job
whose output nothing reads is indistinguishable from a nightly job that does not
run — the same defect class as the signal panel that had never fired (OPSLOG
2026-07-24) and the reviewer that never launched (2026-08-13). The work was
being done and then deleted.

This module keeps it. The re-rank still does not move the book — the agent
decides that, and nothing here executes anything — but it is now **visible**:

    slow_loop  ──save()──>  research_store/ranks/<as_of>.json
                                      │
                            diff(prev, cur)   ← pure, selftested
                                      │
                                 brief()  →  the agent's morning intel

`diff()` reports rank movement, top-N entries/exits, absolute-gate flips, and —
because the candidate universe is rescreened every Friday — names that entered
or left the universe itself. It takes no clock and does no I/O.

⛔ SINGLE NAMES ONLY. The ETF sleeve was deleted on 2026-08-20 (retired
2026-08-16, positions sold 08-17). The 11 sector series the residual tilt
regresses on are read-only factor inputs — not tradeable, not ranked, refused by
the order gate — so they are not in this diff either.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RANKS_DIR = REPO / "research_store" / "ranks"

# How many names count as "the top" for entry/exit reporting. Deliberately not
# `book_hold`: this is an attention window for a human-readable diff, not a
# selection boundary, and it must not silently change when the book size does.
TOP_N = 20
# How many movers to report per section. A diff nobody finishes reading is the
# same as no diff.
MAX_MOVERS = 8


# ------------------------------------------------------------------ snapshot

def snapshot_section(scored) -> dict:
    """One ranked universe -> {ticker: {rank, score, eligible}}.

    `scored` is a momentum.compute() frame. Ineligible names carry a NaN rank
    (the absolute gate), which JSON cannot hold — they are stored with
    `rank: None` so "ranked last" and "not ranked at all" stay distinguishable.
    """
    out = {}
    if scored is None or len(scored) == 0:
        return out
    for sym, row in scored.iterrows():
        rank = row.get("rank")
        try:
            rank = int(rank) if rank == rank and rank is not None else None  # NaN != NaN
        except (TypeError, ValueError):
            rank = None
        score = row.get("score")
        try:
            score = float(score)
            # NaN/inf serialise as bare `NaN`/`Infinity`, which json.dump emits
            # happily and which is NOT valid JSON -- any non-Python reader of
            # this file breaks on it. None is the honest encoding of "no score".
            score = round(score, 6) if score == score and abs(score) != float("inf") else None
        except (TypeError, ValueError):
            score = None
        # bool(float("nan")) is True -- a NaN eligibility flag would silently
        # read as ELIGIBLE, which is the wrong direction for an absolute gate.
        el = row.get("eligible")
        out[str(sym).upper()] = {
            "rank": rank,
            "score": score,
            "eligible": False if el is None or el != el else bool(el),
        }
    return out


def build(as_of: str, book_scored=None) -> dict:
    """Assemble the snapshot the slow loop persists each run.

    `book_scored` is the single-name ranking. There is deliberately no ETF
    section: see the module docstring.
    """
    return {"as_of": str(as_of), "book": snapshot_section(book_scored)}


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def save(snap: dict, dirpath: Path | None = None) -> Path:
    """Write one snapshot, named by its as_of date. Same-day re-runs overwrite:
    a second run on one date is a correction, not a second observation."""
    d = Path(dirpath) if dirpath else RANKS_DIR
    path = d / f"{snap['as_of']}.json"
    _atomic_write(path, snap)
    return path


def load_recent(dirpath: Path | None = None, n: int = 2) -> list:
    """The `n` most recent snapshots, NEWEST FIRST. Unreadable files are skipped
    rather than raising — a torn snapshot must not take down the brief."""
    d = Path(dirpath) if dirpath else RANKS_DIR
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json"), reverse=True):
        try:
            snap = json.loads(p.read_text())
        except Exception:                                    # noqa: BLE001
            continue
        if isinstance(snap, dict) and snap.get("as_of"):
            out.append(snap)
        if len(out) >= n:
            break
    return out


def prune(dirpath: Path | None = None, keep: int = 90) -> int:
    """Keep the newest `keep` snapshots; delete the rest. Returns how many were
    removed. These are small, but a nightly file is a nightly file."""
    d = Path(dirpath) if dirpath else RANKS_DIR
    if not d.exists():
        return 0
    files = sorted(d.glob("*.json"), reverse=True)
    removed = 0
    for p in files[keep:]:
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------------- diff

def _diff_section(prev: dict, cur: dict, top_n: int, max_movers: int) -> dict:
    """Pure comparison of two {ticker: {rank, score, eligible}} maps."""
    prev = prev or {}
    cur = cur or {}
    p_names, c_names = set(prev), set(cur)

    def _rank(d, sym):
        r = (d.get(sym) or {}).get("rank")
        return r if isinstance(r, int) else None

    def _in_top(d, sym):
        r = _rank(d, sym)
        return r is not None and r <= top_n

    entered = sorted(
        [{"symbol": s, "rank": _rank(cur, s), "was": _rank(prev, s)}
         for s in c_names if _in_top(cur, s) and not _in_top(prev, s)],
        key=lambda x: x["rank"])
    exited = sorted(
        [{"symbol": s, "rank": _rank(cur, s), "was": _rank(prev, s)}
         for s in c_names if _in_top(prev, s) and not _in_top(cur, s)],
        key=lambda x: x["was"])

    # Absolute-gate flips. This is the off-switch crossing (12-month return
    # through zero), so it is reported separately from rank movement — a name
    # can hold its rank and still stop being holdable.
    became_eligible = sorted(
        s for s in (c_names & p_names)
        if cur[s].get("eligible") and not prev[s].get("eligible"))
    became_ineligible = sorted(
        s for s in (c_names & p_names)
        if prev[s].get("eligible") and not cur[s].get("eligible"))

    # Universe membership. The screen is rescreened weekly (Friday), so these
    # are normally empty Mon–Thu and carry the week's screen result on Friday.
    joined = sorted(c_names - p_names)
    left = sorted(p_names - c_names)

    movers = []
    for s in c_names & p_names:
        rp, rc = _rank(prev, s), _rank(cur, s)
        if rp is None or rc is None:
            continue
        delta = rp - rc                      # positive = improved (moved up)
        if delta:
            movers.append({"symbol": s, "from": rp, "to": rc, "delta": delta})
    movers.sort(key=lambda m: (-abs(m["delta"]), m["symbol"]))

    return {
        "entered_top": entered,
        "exited_top": exited,
        "became_eligible": became_eligible,
        "became_ineligible": became_ineligible,
        "joined_universe": joined,
        "left_universe": left,
        "movers": movers[:max_movers],
        "moved_count": len(movers),
        "ranked": sum(1 for s in c_names if _rank(cur, s) is not None),
    }


def diff(prev: dict | None, cur: dict | None, *, top_n: int = TOP_N,
         max_movers: int = MAX_MOVERS) -> dict:
    """Compare two snapshots. Pure: no clock, no I/O, no config.

    With no previous snapshot this returns an explicit `no_comparison` shape
    rather than an empty diff — "nothing changed" and "nothing to compare
    against" must never render the same, which is the mistake the scorecard
    made with `unscoreable` (docs/2026-08-19-scorecard-consumer-gap.md §3).
    """
    if not cur:
        return {"status": "no_snapshot",
                "note": "the nightly re-rank has not been recorded yet"}
    if not prev:
        return {"status": "no_comparison",
                "as_of": cur.get("as_of"),
                "note": "first recorded ranking — nothing to diff against yet"}
    out = {
        "status": "ok",
        "as_of": cur.get("as_of"),
        "prev_as_of": prev.get("as_of"),
        "top_n": top_n,
        "book": _diff_section(prev.get("book"), cur.get("book"), top_n, max_movers),
    }
    out["quiet"] = (not out["book"]["moved_count"]) and all(
        not out["book"][f]
        for f in ("entered_top", "exited_top", "became_eligible",
                  "became_ineligible", "joined_universe", "left_universe"))
    out["note"] = ("what the nightly re-rank changed since the previous run. "
                   "FACTS, not instructions — the ranking does not move the book, "
                   "you do. `joined_universe`/`left_universe` are the Friday "
                   "universe rescreen.")
    return out


def latest_diff(dirpath: Path | None = None, *, top_n: int = TOP_N,
                max_movers: int = MAX_MOVERS) -> dict:
    """The diff between the two most recent snapshots on disk."""
    recent = load_recent(dirpath, n=2)
    cur = recent[0] if recent else None
    prev = recent[1] if len(recent) > 1 else None
    return diff(prev, cur, top_n=top_n, max_movers=max_movers)


# ------------------------------------------------------------------ selftest

def _selftest() -> None:
    import tempfile as _tf

    # ---- snapshot_section: NaN rank must survive as None, not as a number ---
    import pandas as pd
    import numpy as np
    scored = pd.DataFrame(
        {"score": [0.9, 0.5, 0.1], "eligible": [True, True, False],
         "rank": [1.0, 2.0, np.nan]},
        index=["AAA", "BBB", "CCC"])
    sec = snapshot_section(scored)
    assert sec["AAA"]["rank"] == 1 and isinstance(sec["AAA"]["rank"], int), sec
    assert sec["CCC"]["rank"] is None, "ineligible must be rank None, not NaN/0"
    assert sec["CCC"]["eligible"] is False, sec
    assert snapshot_section(None) == {}, "no frame must not raise"
    assert snapshot_section(pd.DataFrame()) == {}, "empty frame must not raise"

    # build() carries the single-name ranking and NOTHING ELSE. An "etf" key
    # here would put a deleted allocation back in front of the agent.
    # A NaN score/eligibility must never reach the file. json.dump emits a bare
    # `NaN` token, which is NOT valid JSON, and bool(nan) is True -- an absolute
    # gate that fails OPEN. Both were live until the reviewer caught them.
    import json as _json
    nanf = pd.DataFrame({"score": [float("nan")], "rank": [np.nan],
                         "eligible": [float("nan")]}, index=["NAN"])
    nsec = snapshot_section(nanf)
    assert nsec["NAN"]["score"] is None, nsec
    assert nsec["NAN"]["rank"] is None, nsec
    assert nsec["NAN"]["eligible"] is False, "NaN eligibility must not read as eligible"
    txt = _json.dumps(build("2026-08-20", nanf))
    assert "NaN" not in txt and "Infinity" not in txt, txt
    _json.loads(txt, parse_constant=lambda c: (_ for _ in ()).throw(
        AssertionError(f"invalid JSON token {c}")))
    inff = pd.DataFrame({"score": [float("inf")], "rank": [1.0], "eligible": [True]},
                        index=["INF"])
    assert snapshot_section(inff)["INF"]["score"] is None, "inf must not serialise"

    built = build("2026-08-20", scored)
    assert set(built) == {"as_of", "book"}, built
    assert "etf" not in built, "the retired ETF sleeve must not reappear in the diff"

    # ---- diff: the three states must be distinguishable --------------------
    assert diff(None, None)["status"] == "no_snapshot"
    assert diff(None, {"as_of": "2026-08-20", "book": {}})["status"] \
        == "no_comparison", "first snapshot must not read as 'nothing changed'"

    prev = {"as_of": "2026-08-18", "book": {
        "AAA": {"rank": 1, "score": 0.9, "eligible": True},
        "BBB": {"rank": 2, "score": 0.8, "eligible": True},
        "CCC": {"rank": 25, "score": 0.3, "eligible": True},
        "DDD": {"rank": None, "score": 0.1, "eligible": False},
        "GONE": {"rank": 5, "score": 0.6, "eligible": True},
    }}
    cur = {"as_of": "2026-08-19", "book": {
        "AAA": {"rank": 1, "score": 0.9, "eligible": True},
        "BBB": {"rank": 30, "score": 0.2, "eligible": True},   # falls out of top
        "CCC": {"rank": 3, "score": 0.7, "eligible": True},    # climbs in
        "DDD": {"rank": 12, "score": 0.5, "eligible": True},   # gate flip
        "NEW": {"rank": 8, "score": 0.6, "eligible": True},    # joined universe
    }}
    d = diff(prev, cur, top_n=20)
    b = d["book"]
    assert d["status"] == "ok" and d["prev_as_of"] == "2026-08-18", d
    assert [x["symbol"] for x in b["entered_top"]] == ["CCC", "NEW", "DDD"], b["entered_top"]
    assert [x["symbol"] for x in b["exited_top"]] == ["BBB"], b["exited_top"]
    assert b["became_eligible"] == ["DDD"], b
    assert b["became_ineligible"] == [], b
    assert b["joined_universe"] == ["NEW"], b
    assert b["left_universe"] == ["GONE"], b
    assert d["quiet"] is False, d

    # movers: sorted by absolute move, sign convention = positive is UP
    m = {x["symbol"]: x["delta"] for x in b["movers"]}
    assert m["CCC"] == 22, m          # 25 -> 3 is an improvement of 22
    assert m["BBB"] == -28, m         # 2 -> 30 is a deterioration
    assert b["movers"][0]["symbol"] == "BBB", b["movers"]   # biggest |move| first
    assert "AAA" not in m, "an unchanged rank is not a mover"
    assert b["moved_count"] == 2, b

    # a name that is ineligible in BOTH snapshots is not a mover and not a flip
    still = {"as_of": "x", "book": {"Z": {"rank": None, "score": 0.0, "eligible": False}}}
    d2 = diff(still, {**still, "as_of": "y"})
    assert d2["book"]["movers"] == [] and d2["book"]["became_ineligible"] == [], d2
    assert d2["quiet"] is True, "two identical snapshots must read as quiet"

    # ---- round-trip through disk, and the newest-first ordering ------------
    with _tf.TemporaryDirectory() as td:
        p = Path(td)
        save({"as_of": "2026-08-18", "book": prev["book"]}, p)
        save({"as_of": "2026-08-19", "book": cur["book"]}, p)
        recent = load_recent(p, n=2)
        assert [s["as_of"] for s in recent] == ["2026-08-19", "2026-08-18"], recent
        ld = latest_diff(p)
        assert ld["status"] == "ok" and ld["as_of"] == "2026-08-19", ld
        assert [x["symbol"] for x in ld["book"]["exited_top"]] == ["BBB"], ld

        # a torn file must be skipped, not raise, and must not become the diff
        (p / "2026-08-20.json").write_text("{not json")
        recent2 = load_recent(p, n=2)
        assert [s["as_of"] for s in recent2] == ["2026-08-19", "2026-08-18"], recent2
        assert latest_diff(p)["status"] == "ok", "a torn snapshot must not break the brief"

        # same-day re-run overwrites rather than duplicating
        save({"as_of": "2026-08-19", "book": {}}, p)
        assert len([f for f in p.glob("*.json")]) == 3, sorted(p.glob("*.json"))

        # prune keeps the newest N
        (p / "2026-08-20.json").unlink()
        for day in ("2026-08-10", "2026-08-11", "2026-08-12"):
            save({"as_of": day, "book": {}}, p)
        assert prune(p, keep=2) == 3, "prune must delete the oldest"
        assert sorted(x.stem for x in p.glob("*.json")) == ["2026-08-18", "2026-08-19"]

    # empty dir -> no snapshot, and it says so
    with _tf.TemporaryDirectory() as td:
        assert latest_diff(Path(td))["status"] == "no_snapshot"

    print("rank_history selftest OK: single-name snapshot (no ETF section), diff, "
          "gate flips, universe churn, disk round-trip, torn-file tolerance, prune")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(json.dumps(latest_diff(), indent=2))
