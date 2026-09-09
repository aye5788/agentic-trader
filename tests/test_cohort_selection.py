"""Assertion tests for the two-tier candidate universe.

    .venv/bin/python tests/test_cohort_selection.py

⛔ FIXTURES ONLY. Nothing here opens a socket, imports moomoo, or spends a single
unit of the 100-distinct-symbol history quota — that meter is ACCOUNT-WIDE and
shared with the sibling repo `~/moomoo-vol-desk`, so a test suite that consumed
it would degrade a live system to prove a point about a pure function.

⛔ AND A PASS HERE IS NOT EVIDENCE THE DESIGN IS RIGHT. CLAUDE.md says this
bluntly and it was written after three defects shipped green. What these
assertions can show is that specific stated behaviours hold on specific inputs —
principally that NOTHING except the kill switch can refuse a SELL, and that
every degraded shape of the eligibility artifact refuses a BUY. What they cannot
show is that the artifact describes the market, that the freshness ceiling is the
right number, or that the priority order is the right one. Read the modules.

The tests are grouped by the question each answers:

  A. the eligibility artifact — complete, incomplete, stale, malformed, and
     last-known-good preservation
  B. membership transitions — newly admitted, incumbent leaving, HELD leaving
  C. scoreability — pending, seasoning, provider failure
  D. the quota planner — overflow, exhaustion, determinism, free repeats
  E. the order gate — what may be bought, what may not, and what may be sold
  F. one ranking — candidates/universe/slow_loop over one pool
  G. instruments — no fund, option, short or non-approved instrument is buyable
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import cohort                                              # noqa: E402
import governance as gov                                   # noqa: E402
import quota_planner as qp                                 # noqa: E402
import universe_maint as um                                # noqa: E402

TODAY = dt.date(2026, 9, 11)

CFG = {
    "universe": {"mode": "cohort", "source": "config/universe.csv",
                 "cohort_max_age_days": 10},
    "universe_maintenance": {"add_dvol_floor_usd": 50_000_000.0,
                             "keep_rank_max": 180, "add_rank_max": 150,
                             "cohort_rank_max": 180, "screen_min_mktcap": 2e9,
                             "screen_backend": "v2_turnover", "target_size": 150},
    "governance": {"require_whitelist": True, "max_order_pct": 0.15,
                   "kill_switch_file": "research_store/HALT",
                   "halt_entries_file": "research_store/HALT_ENTRIES"},
}

_TMPDIRS = []


def _row(t, rank, adtv=8e8, cap=5e10, venue="US_NASDAQ", membership="new"):
    return {"ticker": t, "rank": rank, "avg_daily_turnover_usd": adtv,
            "market_cap": cap, "venue": venue, "status": "eligible",
            "membership": membership}


def _doc(names=None, **over):
    d = {"schema_version": cohort.SCHEMA_VERSION, "as_of": "2026-09-11",
         "generated_at": "2026-09-11T21:00:00+00:00",
         "provenance": {"screen_version": "v2_turnover/1",
                        "add_dvol_floor_usd": 50_000_000.0},
         "coverage": {"complete": True, "problems": []},
         "names": names if names is not None else [_row("AAA", 1)]}
    d.update(over)
    return d


def _repo(doc=None, states=None, raw=None, omit_cohort=False):
    """A throwaway repo root carrying the two artifacts. Returns the Path."""
    d = Path(tempfile.mkdtemp())
    _TMPDIRS.append(d)
    (d / "research_store" / "universe").mkdir(parents=True)
    (d / "config").mkdir()
    (d / "config" / "universe.csv").write_text("ticker,flag\nLEGACY,\n")
    if not omit_cohort:
        (d / "research_store" / "universe" / "cohort.json").write_text(
            raw if raw is not None else json.dumps(doc if doc is not None else _doc()))
    (d / "research_store" / "universe" / "history_state.json").write_text(
        json.dumps({"as_of": "2026-09-11", "states": states or {"AAA": "complete"}}))
    return d


def _gate(repo, symbol, side, cfg=None, amount=5.0, account_value=1000.0):
    """(allowed?, reason) for one order through the REAL governance path."""
    orig = gov.REPO
    gov.REPO = repo
    try:
        ok, blocked = gov.vet_plan(
            [{"symbol": symbol, "side": side, "amount": amount}],
            account_value, cfg or CFG)
    finally:
        gov.REPO = orig
    return (bool(ok), "" if ok else blocked[0]["blocked"])


# =========================================================================== #
# A. the eligibility artifact
# =========================================================================== #
def test_complete_artifact_is_usable():
    view = cohort.active_view(CFG, _repo(), TODAY)
    assert view["scoreable"] == ["AAA"], view
    assert cohort.buyable(view) == {"AAA"}
    assert view["freshness"]["stale"] is False
    assert view["provenance"]["screen_version"] == "v2_turnover/1"


def test_incomplete_screen_is_refused():
    """⛔ A TRUNCATED SCREEN IS WELL-FORMED. Length cannot detect it; only the
    writer's own `coverage.complete` claim can, so its absence must refuse."""
    d = _doc(); d["coverage"]["complete"] = False
    assert _raises(lambda: cohort.active_view(CFG, _repo(d), TODAY), "complete")
    d2 = _doc(); d2["coverage"]["problems"] = ["screen not sorted descending at X"]
    assert _raises(lambda: cohort.active_view(CFG, _repo(d2), TODAY), "integrity")


def test_stale_artifact_is_refused_and_says_so():
    d = _doc(as_of="2026-08-12")                      # 30 days old, ceiling is 10
    msg = _raises(lambda: cohort.active_view(CFG, _repo(d), TODAY), "STALE")
    assert "30" in msg, msg
    # ...and one day INSIDE the ceiling is still fine: the tolerance is real, so
    # a single missed Friday does not stop the book trading.
    ok = _doc(as_of=str(TODAY - dt.timedelta(days=10)))
    assert cohort.active_view(CFG, _repo(ok), TODAY)["freshness"]["stale"] is False


def test_unparseable_as_of_reads_STALE_not_fresh():
    """Every 'is this old?' check in this repo has at some point said 'no'
    because it could not read the date at all (OPSLOG 2026-08-31)."""
    f = cohort.freshness({"as_of": "not-a-date"}, TODAY)
    assert f["stale"] is True and "not a date" in f["reason"], f


def test_malformed_and_absent_are_distinguished_and_both_refuse():
    assert _raises(lambda: cohort.load(_repo(omit_cohort=True) /
                                       "research_store/universe/cohort.json"),
                   "has not produced one")
    assert _raises(lambda: cohort.load(_repo(raw="{not json") /
                                       "research_store/universe/cohort.json"),
                   "unreadable")


def test_empty_cohort_is_a_problem_not_an_answer():
    """An empty cohort and an unreadable one are the same value to a naive
    reader and mean opposite things. Neither may pass as 'nothing eligible'."""
    assert any("empty cohort" in p or "no names" in p
               for p in cohort.validate(_doc(names=[])))


def test_evidence_is_revalidated_at_read_time():
    """The difference between this artifact and a curated CSV: a row states the
    turnover that made it eligible, so the claim can be re-tested."""
    thin = _doc(names=[_row("AAA", 1, adtv=1e6)])         # below the $50M/day floor
    assert _raises(lambda: cohort.active_view(CFG, _repo(thin), TODAY), "floor")
    for bad in (None, "x", 0, -1, float("nan")):
        assert cohort.validate(_doc(names=[_row("AAA", 1, adtv=bad)])), bad
        assert cohort.validate(_doc(names=[_row("AAA", 1, cap=bad)])), bad


def test_schema_version_mismatch_refuses():
    assert _raises(lambda: cohort.active_view(CFG, _repo(_doc(schema_version=99)),
                                              TODAY), "schema_version")


def test_duplicate_and_blank_tickers_refuse():
    assert any("duplicate" in p for p in
               cohort.validate(_doc(names=[_row("AAA", 1), _row("AAA", 2)])))
    assert any("blank" in p for p in cohort.validate(_doc(names=[_row("", 1)])))


def test_missing_evidence_field_refuses():
    for field in ("rank", "avg_daily_turnover_usd", "market_cap", "venue",
                  "status", "membership"):
        row = _row("AAA", 1)
        row.pop(field)
        assert any("missing evidence" in p for p in
                   cohort.validate(_doc(names=[row]))), field


def test_builder_output_passes_its_own_reader():
    """⛔ THE ROUND TRIP. A writer that emits something its reader rejects is a
    weekly job that silently stops maintaining the thing it exists to maintain."""
    rows = [{"symbol": f"T{i:03d}", "name": "n", "market_cap": 5e9,
             "turnover_20d_cum": 4.0e10 - i * 1e8} for i in range(200)]
    ex = {r["symbol"]: "US_NASDAQ" for r in rows}
    ranked, turn, rep = um.build_ranked_screen(rows, ex, 180, incumbents=["T005"])
    assert rep["problems"] == [], rep
    doc = cohort.build(ranked, turn, rep["market_caps"], rep["venues"],
                       as_of="2026-09-11", generated_at="2026-09-11T21:00:00+00:00",
                       incumbents=["T005"], rank_max=180,
                       provenance={"screen_version": "v2_turnover/1"},
                       coverage={"complete": True, "problems": []})
    assert cohort.validate(doc, min_turnover_usd=50_000_000.0) == []
    assert len(doc["names"]) == 180, "rank_max must bound the artifact"
    assert doc["names"][0]["rank"] == 1 and doc["names"][-1]["rank"] == 180


def test_cumulative_turnover_is_divided_exactly_once():
    """⛔ moomoo's field is named AVG_TURNOVER and is a 20-day CUMULATIVE total.
    $1B cumulative IS the $50M/day add floor. Undivided it reads 20x too liquid,
    which would admit names at $2.5M/day."""
    assert um.to_avg_daily(1_000_000_000) == 50_000_000
    rows = [{"symbol": "EDGE", "name": "e", "market_cap": 5e9,
             "turnover_20d_cum": 1_000_000_000}]
    _, turn, rep = um.build_ranked_screen(rows, {"EDGE": "US_NYSE"}, 0)
    doc = cohort.build(["EDGE"], turn, rep["market_caps"], rep["venues"],
                       as_of="2026-09-11", generated_at="x",
                       provenance={"screen_version": "v2_turnover/1"},
                       coverage={"complete": True, "problems": []})
    assert doc["names"][0]["avg_daily_turnover_usd"] == 50_000_000
    assert cohort.validate(doc, min_turnover_usd=50_000_000.0) == [], \
        "the floor must be met exactly at the boundary, not one division away"


def test_last_known_good_is_preserved_on_every_unhealthy_screen():
    """⛔ THE WHOLE POINT OF A PERSISTED ARTIFACT. A screen that cannot prove
    itself must leave the previous file BYTE-IDENTICAL, never write a partial
    one and never fall back to a different discovery mechanism."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_ur", REPO / "scripts" / "universe_refresh.py")
    ur = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ur)

    rows = [{"symbol": f"T{i:03d}", "name": "n", "market_cap": 5e9,
             "turnover_20d_cum": 4.0e10 - i * 1e8} for i in range(200)]
    ex = {r["symbol"]: "US_NASDAQ" for r in rows}
    ranked, turn, rep = um.build_ranked_screen(rows, ex, 180)
    ev = {"market_caps": rep["market_caps"], "venues": rep["venues"],
          "excluded_venue": [], "unknown_venue": [],
          "ranked_count": rep["ranked_count"], "all_count": 3000,
          "retrieved_rows": 200, "pages": 1, "screen_version": "v2_turnover/1"}
    out = Path(tempfile.mkdtemp()) / "cohort.json"
    _TMPDIRS.append(out.parent)
    good = {"screen_evidence": ev, "v2_problems": []}
    ur.write_cohort(ranked, turn, good, "2026-09-11", CFG,
                    {"decision": "AUTO_APPLY"}, out=out)
    before = out.read_bytes()
    assert before, "a healthy screen must write"

    for cov, dec in (
        ({"screen_evidence": ev, "v2_problems": ["not sorted"]}, {"decision": "AUTO_APPLY"}),
        ({"screen_evidence": ev, "v2_problems": []}, {"decision": "NO_CHANGE"}),
        ({"v2_problems": []}, {"decision": "AUTO_APPLY"}),          # legacy backend
        ({"v2_problems": ["V2 screen unavailable"]}, {"decision": "AUTO_APPLY"}),
    ):
        note = ur.write_cohort(ranked, turn, cov, "2026-09-18", CFG, dec, out=out)
        assert out.read_bytes() == before, note
        assert "last-known-good stands" in note, note
    # a dry run must not write either
    ur.write_cohort(ranked, turn, good, "2026-09-18", CFG,
                    {"decision": "AUTO_APPLY"}, out=out, dry=True)
    assert out.read_bytes() == before


# =========================================================================== #
# B. membership transitions
# =========================================================================== #
def test_newly_admitted_name_is_marked_new_and_is_not_buyable_yet():
    """A name the screen admits this week has no panel history: eligible, and a
    research lead until the repair gives it the momentum window."""
    d = _doc(names=[_row("OLD", 1, membership="incumbent"),
                    _row("FRESH", 2, membership="new")])
    repo = _repo(d, states={"OLD": "complete"})       # FRESH: no record at all
    view = cohort.active_view(CFG, repo, TODAY)
    assert view["by_ticker"]["FRESH"]["membership"] == "new"
    assert view["by_ticker"]["FRESH"]["status"] == cohort.PENDING_HISTORY
    assert _gate(repo, "OLD", "buy")[0] is True
    assert _gate(repo, "FRESH", "buy")[0] is False


def test_incumbent_leaving_the_cohort_stops_being_buyable():
    gone = _repo(_doc(names=[_row("STILL", 1)]), states={"STILL": "complete",
                                                         "LEFT": "complete"})
    assert _gate(gone, "STILL", "buy")[0] is True
    ok, why = _gate(gone, "LEFT", "buy")
    assert ok is False and "not in the eligibility cohort" in why, why


def test_a_HELD_name_that_leaves_the_cohort_stays_sellable():
    """⛔ THE LOAD-BEARING ONE. Stops here are SOFTWARE — the monitor process IS
    the stop — so a gate that could refuse a sell does not pause risk, it strips
    an open position of its only protection."""
    repo = _repo(_doc(names=[_row("STILL", 1)]), states={"STILL": "complete"})
    ok, why = _gate(repo, "LEFT", "sell")
    assert ok is True, why
    # ...and it is still refused as a BUY, which is the asymmetry, not a bug.
    assert _gate(repo, "LEFT", "buy")[0] is False


def test_no_degraded_artifact_can_ever_block_a_sell():
    """Absent, malformed, stale, empty, wrong-schema, incomplete — every one
    refuses a BUY and NONE of them touches a SELL."""
    cases = {
        "absent": _repo(omit_cohort=True),
        "malformed": _repo(raw="{not json"),
        "stale": _repo(_doc(as_of="2026-01-01")),
        "empty": _repo(_doc(names=[])),
        "schema": _repo(_doc(schema_version=99)),
        "incomplete": _repo(_doc(coverage={"complete": False, "problems": []})),
    }
    for label, repo in cases.items():
        buy_ok, why = _gate(repo, "AAA", "buy")
        assert buy_ok is False, f"{label}: a degraded cohort must refuse a BUY"
        assert "exits are unaffected" in why, f"{label}: {why}"
        assert _gate(repo, "AAA", "sell")[0] is True, \
            f"{label}: a degraded cohort MUST NOT block a sell"


# =========================================================================== #
# C. scoreability
# =========================================================================== #
def test_three_states_and_only_complete_is_buyable():
    d = _doc(names=[_row("SCORED", 1), _row("YOUNG", 2), _row("QUEUED", 3),
                    _row("BROKEN", 4), _row("NEVERSEEN", 5)])
    repo = _repo(d, states={"SCORED": "complete", "YOUNG": "seasoning",
                            "QUEUED": "deferred_quota", "BROKEN": "failed_provider"})
    view = cohort.active_view(CFG, repo, TODAY)
    assert view["scoreable"] == ["SCORED"]
    assert view["pending_history"] == ["YOUNG", "QUEUED", "NEVERSEEN"]
    assert view["unscoreable"] == ["BROKEN"]
    assert cohort.buyable(view) == {"SCORED"}
    for sym in ("YOUNG", "QUEUED", "BROKEN", "NEVERSEEN"):
        assert _gate(repo, sym, "buy")[0] is False, sym
        assert _gate(repo, sym, "sell")[0] is True, sym


def test_an_unrecognised_history_state_composes_to_pending_never_scoreable():
    """⛔ THE BRANCH THAT MUST NOT EXIST: a typo in a state string making a name
    buyable. `compose` reaches `scoreable` only on an exact `complete`."""
    repo = _repo(states={"AAA": "compleet"})
    view = cohort.active_view(CFG, repo, TODAY)
    assert view["by_ticker"]["AAA"]["status"] == cohort.PENDING_HISTORY
    assert cohort.buyable(view) == set()


def test_a_missing_history_file_makes_nothing_scoreable():
    """Fails toward pending, which REFUSES entries — the opposite polarity from
    the cohort file, which is the thing that says yes and so must raise."""
    repo = _repo()
    (repo / "research_store" / "universe" / "history_state.json").unlink()
    view = cohort.active_view(CFG, repo, TODAY)
    assert view["counts"]["scoreable"] == 0
    assert _gate(repo, "AAA", "buy")[0] is False
    assert _gate(repo, "AAA", "sell")[0] is True


def test_explain_names_which_question_failed():
    """A refusal has to say whether ELIGIBILITY or SCOREABILITY failed: they
    have different remedies, and one of them ('wait') is not a remedy at all."""
    d = _doc(names=[_row("SCORED", 1), _row("YOUNG", 2)])
    view = cohort.active_view(CFG, _repo(d, states={"SCORED": "complete",
                                                    "YOUNG": "seasoning"}), TODAY)
    assert "not in the eligibility cohort" in cohort.explain(view, "NOPE")
    assert "PENDING" in cohort.explain(view, "YOUNG")
    assert "eligible" in cohort.explain(view, "SCORED").lower()


# =========================================================================== #
# D. the quota planner
# =========================================================================== #
def test_never_schedules_more_than_the_quota():
    cands = [f"T{i:03d}" for i in range(250)]
    ranks = {t: i + 1 for i, t in enumerate(cands)}
    p = qp.plan(cands, cohort_ranks=ranks, quota=100)
    assert len(p["request"]) == 100
    assert len(p["new_symbols"]) <= 100
    assert len(p["deferred"]) == 150
    assert p["budget"]["exhausted"] is True


def test_priority_is_held_then_nearest_then_cohort_rank():
    p = qp.plan(["ZZZZ", "AAAA", "NEAR", "FAR"], held={"ZZZZ"},
                missing_rows={"NEAR": 6, "FAR": 240},
                cohort_ranks={"AAAA": 1, "NEAR": 90, "FAR": 91}, quota=10)
    assert p["request"] == ["ZZZZ", "NEAR", "FAR", "AAAA"], p["request"]
    # a HELD name must never be deferred behind a merely-liquid one
    p2 = qp.plan(["AAAA", "HELD"], held={"HELD"},
                 cohort_ranks={"AAAA": 1, "HELD": 900}, quota=1)
    assert p2["request"] == ["HELD"], p2["request"]


def test_planning_is_deterministic_and_totally_ordered():
    cands = [f"T{i:03d}" for i in range(250)]
    ranks = {t: i + 1 for i, t in enumerate(cands)}
    a = qp.plan(cands, cohort_ranks=ranks, quota=100)
    b = qp.plan(list(reversed(cands)), cohort_ranks=ranks, quota=100)
    assert a["request"] == b["request"], "input order must not change the plan"
    # ties broken by ticker, so no name can starve by flapping across runs
    t = qp.plan(["B", "A"], cohort_ranks={"A": 5, "B": 5}, quota=1)
    assert t["request"] == ["A"]


def test_deferred_names_carry_a_reason():
    cands = [f"T{i:03d}" for i in range(250)]
    ranks = {t: i + 1 for i, t in enumerate(cands)}
    first = qp.plan(cands, cohort_ranks=ranks, quota=100)
    assert all(first["reasons"][t] for t in first["deferred"])
    assert "quota" in first["reasons"][first["deferred"][0]] or \
           "capacity" in first["reasons"][first["deferred"][0]] or \
           "ceiling" in first["reasons"][first["deferred"][0]]


def test_a_second_run_in_the_same_window_schedules_ZERO_new_distinct_symbols():
    """⛔ THE DEFECT THIS REPLACES. The previous version of this test ran the
    deferred names through a second `plan()` and asserted it scheduled T100 —
    i.e. it asserted that a second run could spend another 100 distinct symbols
    inside the same rolling broker window. It encoded a 2x overspend as the
    requirement. The quota is 100 distinct symbols ACROSS the window, not per
    call, and the daily price path calls this every day.

    Run 1 spends the window. Run 2, with those symbols still charged, must
    schedule nothing new at all."""
    cands = [f"T{i:03d}" for i in range(250)]
    ranks = {t: i + 1 for i, t in enumerate(cands)}

    run1 = qp.plan(cands, cohort_ranks=ranks, quota=100, charged=set())
    assert len(run1["new_symbols"]) == 100, run1["budget"]
    assert run1["budget"]["already_charged"] == 0
    assert run1["budget"]["remaining_new_distinct"] == 100

    # Run 1's requests are now charged against the shared window.
    charged = set(run1["request"])
    run2 = qp.plan(run1["deferred"], cohort_ranks=ranks, quota=100,
                   charged=charged)
    assert run2["budget"]["already_charged"] == 100, run2["budget"]
    assert run2["budget"]["remaining_new_distinct"] == 0, run2["budget"]
    assert run2["new_symbols"] == [], \
        "a second run inside the window must schedule ZERO new distinct symbols"
    assert run2["request"] == [], "none of the deferred names is a repeat"
    assert set(run2["deferred"]) == set(run1["deferred"])
    assert "capacity" in run2["reasons"][run2["deferred"][0]], \
        "the deferral must say WHY, and the why is exhausted shared capacity"


def test_partial_capacity_is_subtracted_not_reset():
    """40 already charged -> exactly 60 new may be scheduled, not 100."""
    cands = [f"T{i:03d}" for i in range(250)]
    ranks = {t: i + 1 for i, t in enumerate(cands)}
    charged = {f"C{i:03d}" for i in range(40)}          # charged, not candidates
    p = qp.plan(cands, cohort_ranks=ranks, quota=100, charged=charged)
    assert p["budget"]["already_charged"] == 40
    assert p["budget"]["remaining_new_distinct"] == 60
    assert len(p["new_symbols"]) == 60, p["budget"]
    assert len(p["request"]) == 60


def test_repeats_stay_eligible_without_spending_new_capacity():
    """A fully spent window still permits retrying the names already charged —
    that is what makes a failed repair retryable at zero cost — but it permits
    no new ones, and the absolute per-run ceiling still binds."""
    charged = {f"T{i:03d}" for i in range(100)}
    cands = [f"T{i:03d}" for i in range(150)]
    ranks = {t: i + 1 for i, t in enumerate(cands)}
    p = qp.plan(cands, cohort_ranks=ranks, quota=100, charged=charged)
    assert p["budget"]["remaining_new_distinct"] == 0
    assert p["new_symbols"] == [], "no new distinct symbol may be scheduled"
    assert set(p["repeat_symbols"]) == charged, "every repeat stays eligible"
    assert len(p["request"]) == 100 <= 100, "absolute per-run ceiling holds"
    assert set(p["deferred"]) == {f"T{i:03d}" for i in range(100, 150)}


def _lstate(reqs, present=True, readable=True):
    return {"requests": dict(reqs), "present": present, "readable": readable}


def test_day8_schedules_zero_new_without_an_established_broker_window():
    """⛔ THE AUDIT BLOCKER. 100 requests recorded on day 1. On day 8 the old
    `repeat_credit_days = 7` timer dropped them out of the charged set, remaining
    capacity rose to 100, and the planner scheduled 100 fresh distinct symbols —
    while moomoo may still have been counting the originals. The window's real
    duration is undocumented, so nothing expires on a timer any more."""
    day1 = dt.date(2026, 9, 1)
    led = _lstate({f"T{i:03d}": str(day1) for i in range(100)})
    for day in (dt.date(2026, 9, 2), dt.date(2026, 9, 8), dt.date(2026, 9, 30),
                dt.date(2027, 1, 1)):
        cap = qp.capacity_state(ledger_state=led, today=day, quota=100)
        assert cap["source"] == qp.SRC_LEDGER, (day, cap["source"])
        assert cap["already_charged"] == 100, (day, cap)
        assert cap["remaining_new"] == 0, f"{day}: capacity must NOT return on a timer"
        p = qp.plan(["BRANDNEW"], quota=100, charged=cap["charged"],
                    remaining_new=cap["remaining_new"])
        assert p["new_symbols"] == [], day
        assert p["deferred"] == ["BRANDNEW"], day


def test_broker_telemetry_is_the_source_of_truth_when_it_answers():
    """Mocked telemetry — no live call. `remain` is the answer; the local ledger
    does not override it in either direction."""
    led = _lstate({f"T{i:03d}": "2026-09-01" for i in range(100)})
    tel = {"ok": True, "used": 40, "remain": 60, "detail_available": True,
           "charged": {f"T{i:03d}" for i in range(40)}}
    cap = qp.capacity_state(telemetry=tel, ledger_state=led, today=TODAY, quota=100)
    assert cap["source"] == qp.SRC_TELEMETRY, cap
    assert cap["remaining_new"] == 60, cap
    assert cap["charged"] == tel["charged"]
    p = qp.plan([f"N{i:03d}" for i in range(200)], quota=100,
                charged=cap["charged"], remaining_new=cap["remaining_new"])
    assert len(p["new_symbols"]) == 60, p["budget"]

    # telemetry saying ZERO remaining beats a local ledger that thinks it is free
    tel0 = {"ok": True, "used": 100, "remain": 0, "detail_available": True,
            "charged": set()}
    cap0 = qp.capacity_state(telemetry=tel0, ledger_state=_lstate({}),
                             today=TODAY, quota=100)
    assert cap0["remaining_new"] == 0 and cap0["source"] == qp.SRC_TELEMETRY

    # telemetry with NO per-symbol detail: capacity is trusted, repeats are not
    tel_nd = {"ok": True, "used": 30, "remain": 70, "detail_available": False,
              "charged": {"WHATEVER"}}
    cap_nd = qp.capacity_state(telemetry=tel_nd, ledger_state=led,
                               today=TODAY, quota=100)
    assert cap_nd["remaining_new"] == 70 and cap_nd["charged"] == set(), cap_nd


def test_unavailable_telemetry_falls_back_and_never_expands_capacity():
    led = _lstate({f"T{i:03d}": "2026-09-01" for i in range(100)})
    for tel in (None, {"ok": False, "error": "OpenD down"},
                {"ok": True, "remain": None}, {"ok": True, "remain": -1}):
        cap = qp.capacity_state(telemetry=tel, ledger_state=led, today=TODAY,
                                quota=100)
        assert cap["remaining_new"] == 0, tel
        assert cap["source"] in (qp.SRC_LEDGER, qp.SRC_UNKNOWN), tel


def test_an_absent_ledger_is_UNKNOWN_not_unused():
    """⛔ An absent local record is NOT evidence the ACCOUNT's quota is unused —
    it is the state of a fresh clone, a restored droplet or a deleted file. It
    used to read as 'first run, full capacity assumed', the most permissive
    reading of missing information."""
    cap = qp.capacity_state(ledger_state=_lstate({}, present=False),
                            today=TODAY, quota=100)
    assert cap["capacity_unknown"] is True and cap["source"] == qp.SRC_UNKNOWN
    assert cap["remaining_new"] == 0, cap
    p = qp.plan(["AAA"], quota=100, charged=cap["charged"],
                remaining_new=cap["remaining_new"],
                capacity_unknown=cap["capacity_unknown"])
    assert p["request"] == [] and p["deferred"] == ["AAA"]
    assert "unreadable" in p["reasons"]["AAA"] or "UNKNOWN" in p["reasons"]["AAA"]


def test_an_operator_confirmed_reset_grants_one_window_and_then_refills():
    """A valid reset makes pre-reset requests stop counting. It is self-limiting:
    requests made AFTER it count normally, so it grants one fresh window rather
    than a standing exemption."""
    reset = {"present": True, "readable": True,
             "effective_from": dt.date(2026, 9, 10), "reason": "read 0/100",
             "confirmed_by": "aaron"}
    old = {f"OLD{i:03d}": "2026-09-01" for i in range(100)}
    cap = qp.capacity_state(ledger_state=_lstate(old), reset_state=reset,
                            today=TODAY, quota=100)
    assert cap["source"] == qp.SRC_RESET and cap["remaining_new"] == 100, cap

    # ...and once 100 fresh requests land after the reset, it is spent again —
    # the same reset file does not keep handing capacity back.
    after = dict(old, **{f"NEW{i:03d}": "2026-09-11" for i in range(100)})
    cap2 = qp.capacity_state(ledger_state=_lstate(after), reset_state=reset,
                             today=TODAY, quota=100)
    assert cap2["already_charged"] == 100 and cap2["remaining_new"] == 0, cap2


def test_an_undated_or_unattributed_reset_is_ignored():
    """A reset that cannot say FROM WHEN, or who confirmed it, is not evidence —
    honouring it would be the unverified-duration failure in a new costume."""
    d = Path(tempfile.mkdtemp()); _TMPDIRS.append(d)
    for body in ('{"confirmed_by": "aaron"}',                 # no effective_from
                 '{"effective_from": "2026-09-10"}',          # nobody confirmed it
                 '{"effective_from": "soon", "confirmed_by": "aaron"}',
                 "{not json"):
        f = d / "r.json"
        f.write_text(body)
        st = qp.load_reset_state(f)
        assert st["effective_from"] is None, body
        cap = qp.capacity_state(
            ledger_state=_lstate({f"T{i:03d}": "2026-09-01" for i in range(100)}),
            reset_state=st, today=TODAY, quota=100)
        assert cap["source"] != qp.SRC_RESET, body
        assert cap["remaining_new"] == 0, body
    # a well-formed one IS honoured, and round-trips
    (d / "r.json").write_text(json.dumps(
        {"effective_from": "2026-09-10", "confirmed_by": "aaron", "reason": "0/100"}))
    st = qp.load_reset_state(d / "r.json")
    assert st["effective_from"] == dt.date(2026, 9, 10) and st["confirmed_by"] == "aaron"


def test_a_documented_rolling_window_is_the_only_duration_that_expires_anything():
    """`rolling_window_days` is opt-in and accurately named. Unset means UNKNOWN
    and nothing expires; set means an operator documented the real duration."""
    led = _lstate({f"T{i:03d}": "2026-09-01" for i in range(100)})
    unset = qp.capacity_state(ledger_state=led, today=dt.date(2026, 10, 30),
                              quota=100, rolling_window_days=None)
    assert unset["remaining_new"] == 0 and unset["source"] == qp.SRC_LEDGER
    documented = qp.capacity_state(ledger_state=led, today=dt.date(2026, 10, 30),
                                   quota=100, rolling_window_days=30)
    assert documented["source"] == qp.SRC_WINDOW
    assert documented["remaining_new"] == 100, documented
    # an unparseable ledger timestamp COUNTS rather than expiring
    odd = _lstate({"A": "not-a-date"})
    assert qp.charged_symbols(odd["requests"], TODAY, 30) == {"A"}
    assert qp.charged_symbols(odd["requests"], TODAY, None) == {"A"}


def test_a_malformed_or_missing_ledger_cannot_EXPAND_the_budget():
    """⛔ BROKEN IS NOT EMPTY, AND EMPTY IS NOT "UNUSED". Both resolve to zero
    new distinct-symbol capacity — one because we cannot read what was spent,
    the other because this box's silence says nothing about the account."""
    d = Path(tempfile.mkdtemp())
    _TMPDIRS.append(d)

    absent = qp.load_ledger_state(d / "nope.json")
    assert absent == {"requests": {}, "present": False, "readable": True}
    assert qp.capacity_state(ledger_state=absent, today=TODAY,
                             quota=100)["remaining_new"] == 0

    for bad in ("{not json", '{"requests": "nope"}', "[]"):
        f = d / "bad.json"
        f.write_text(bad)
        st = qp.load_ledger_state(f)
        assert st["present"] and not st["readable"], bad
        cap = qp.capacity_state(ledger_state=st, today=TODAY, quota=100)
        assert cap["capacity_unknown"] is True, bad
        assert cap["remaining_new"] == 0, f"{bad}: unreadable must not grant capacity"
        pl = qp.plan(["AAA", "BBB"], quota=100, charged=cap["charged"],
                     remaining_new=cap["remaining_new"],
                     capacity_unknown=cap["capacity_unknown"])
        assert pl["request"] == [], bad
        assert "unreadable" in pl["reasons"]["AAA"], pl["reasons"]["AAA"]
        # ...and a reset cannot rescue it: there is no readable record of what
        # happened after `effective_from`.
        cap_r = qp.capacity_state(
            ledger_state=st, today=TODAY, quota=100,
            reset_state={"present": True, "readable": True,
                         "effective_from": dt.date(2026, 9, 10),
                         "confirmed_by": "aaron"})
        assert cap_r["remaining_new"] == 0, bad

    assert qp.remaining_new_distinct(100, set(), capacity_unknown=True) == 0
    assert qp.remaining_new_distinct(100, {f"T{i}" for i in range(150)}) == 0


def test_record_preserves_an_unreadable_ledger_instead_of_clobbering_it():
    """Overwriting it would discard the record of what was already charged and
    hand back full capacity next run — a corrupt file becoming a licence."""
    d = Path(tempfile.mkdtemp())
    _TMPDIRS.append(d)
    p = d / "q.json"
    p.write_text("{corrupt")
    qp.record(p, ["AAA"], TODAY)
    assert (d / "q.json.corrupt").exists(), "the corrupt file must be preserved"
    assert qp.load_ledger_state(p)["readable"] is True
    assert set(qp.load_ledger(p)) == {"AAA"}


def test_the_planner_default_is_the_SAFE_computation():
    """A caller that passes only `charged` must get capacity subtraction, not
    the per-run counting that was the defect. The safe path is the default."""
    charged = {f"T{i:03d}" for i in range(100)}
    p = qp.plan(["NEWNAME"], quota=100, charged=charged)   # no remaining_new given
    assert p["budget"]["remaining_new_distinct"] == 0
    assert p["new_symbols"] == [] and p["deferred"] == ["NEWNAME"]


def test_quota_exhaustion_defers_and_corrupts_nothing():
    p = qp.plan(["A", "B"], quota=0)
    assert p["request"] == [] and p["deferred"] == ["A", "B"]
    assert all(p["reasons"][t] for t in p["deferred"])


def test_a_repeat_request_costs_no_new_symbol_unit():
    """⛔ VERIFIED AGAINST THE LIVE METER 2026-09-06: a second NVDA pull left
    `used_quota` unchanged. So a failed repair is retried without compounding
    cost — but the ABSOLUTE request ceiling still holds even if the ledger is
    wrong, which is why both numbers are asserted."""
    cands = [f"T{i:03d}" for i in range(150)]
    ranks = {t: i + 1 for i, t in enumerate(cands)}
    p = qp.plan(cands, cohort_ranks=ranks, quota=100, charged=set(cands[:50]))
    assert len(p["repeat_symbols"]) == 50
    assert len(p["new_symbols"]) == 50
    assert len(p["new_symbols"]) + len(p["repeat_symbols"]) == len(p["request"])
    assert len(p["request"]) <= 100, "the absolute ceiling holds regardless"
    # with EVERY candidate already charged, the new-symbol meter spends nothing
    q = qp.plan(cands[:80], cohort_ranks=ranks, quota=100, charged=set(cands))
    assert q["new_symbols"] == [] and len(q["repeat_symbols"]) == 80


def test_ledger_round_trip_keeps_everything_by_default():
    """⛔ THIS TEST USED TO ASSERT THE DEFECT. It required that a write 200 days
    later left ONLY the new symbol — i.e. it encoded unconditional 90-day
    pruning as the requirement, which is time-based capacity reclamation and is
    exactly what the no-expiry policy forbids."""
    p = Path(tempfile.mkdtemp()) / "q.json"
    _TMPDIRS.append(p.parent)
    qp.record(p, ["AAA", "BBB"], TODAY)
    assert set(qp.load_ledger(p)) == {"AAA", "BBB"}
    qp.record(p, ["CCC"], TODAY + dt.timedelta(days=200))
    assert set(qp.load_ledger(p)) == {"AAA", "BBB", "CCC"}, \
        "age alone must never remove an entry"


def test_a_repeat_after_90_days_cannot_reclaim_capacity():
    """⛔ THE BACK DOOR OUT OF 'NO EXPIRY', END TO END. 100 symbols charged on
    day 0; day 91 arrives with telemetry down, no reset and no documented
    window. A REPEAT is still allowed (it costs no distinct-symbol unit) and
    persists — and the old `record()` pruned the other 99 while writing it, so
    the next capacity read counted 1 and granted 99 new slots."""
    d = Path(tempfile.mkdtemp()); _TMPDIRS.append(d)
    led = d / "q.json"
    day0 = dt.date(2026, 1, 1)
    old100 = [f"T{i:03d}" for i in range(100)]
    qp.record(led, old100, day0)
    assert len(qp.load_ledger(led)) == 100

    day91 = day0 + dt.timedelta(days=91)
    cap = qp.capacity_state(telemetry={"ok": False, "error": "OpenD down"},
                            ledger_state=qp.load_ledger_state(led),
                            reset_state=qp.load_reset_state(d / "reset.json"),
                            today=day91, quota=100, rolling_window_days=None)
    assert cap["source"] == qp.SRC_LEDGER, cap["source"]
    assert cap["already_charged"] == 100 and cap["remaining_new"] == 0
    assert cap["retention"] == {}, "no authority answered — nothing may be pruned"

    # the repeat is permitted, and persisting it must not forget the other 99
    plan = qp.plan(["T000"], quota=100, charged=cap["charged"],
                   remaining_new=cap["remaining_new"])
    assert plan["request"] == ["T000"] and plan["repeat_symbols"] == ["T000"]
    qp.record(led, plan["request"], day91, **cap["retention"])

    after = qp.load_ledger(led)
    assert set(after) == set(old100), \
        f"all 100 prior entries must survive; {100 - len(after)} were lost"

    # ...and capacity is STILL zero for a brand-new distinct symbol
    cap2 = qp.capacity_state(ledger_state=qp.load_ledger_state(led),
                             today=day91 + dt.timedelta(days=400), quota=100)
    assert cap2["already_charged"] == 100 and cap2["remaining_new"] == 0, cap2
    p2 = qp.plan(["BRANDNEW"], quota=100, charged=cap2["charged"],
                 remaining_new=cap2["remaining_new"])
    assert p2["new_symbols"] == [] and p2["deferred"] == ["BRANDNEW"]


def test_only_an_authority_may_prune_the_ledger():
    """Retention is an act of an authority, never of a clock — and the
    permission travels with the capacity figure it was derived from."""
    d = Path(tempfile.mkdtemp()); _TMPDIRS.append(d)
    day0 = dt.date(2026, 1, 1)
    old100 = [f"T{i:03d}" for i in range(100)]

    # (a) an operator-confirmed reset intentionally supersedes pre-reset entries
    led_a = d / "a.json"
    qp.record(led_a, old100, day0)
    (d / "reset.json").write_text(json.dumps(
        {"effective_from": "2026-06-01", "confirmed_by": "aaron",
         "reason": "get_history_kl_quota read 0/100"}))
    cap = qp.capacity_state(ledger_state=qp.load_ledger_state(led_a),
                            reset_state=qp.load_reset_state(d / "reset.json"),
                            today=dt.date(2026, 6, 2), quota=100)
    assert cap["source"] == qp.SRC_RESET and cap["remaining_new"] == 100
    assert cap["retention"] == {"prune_before": dt.date(2026, 6, 1)}
    qp.record(led_a, ["FRESH"], dt.date(2026, 6, 2), **cap["retention"])
    assert set(qp.load_ledger(led_a)) == {"FRESH"}, \
        "a reset may intentionally drop what it superseded"

    # (b) a DOCUMENTED rolling window intentionally expires old entries
    led_b = d / "b.json"
    qp.record(led_b, old100, day0)
    cap = qp.capacity_state(ledger_state=qp.load_ledger_state(led_b),
                            today=dt.date(2026, 4, 1), quota=100,
                            rolling_window_days=30)
    assert cap["source"] == qp.SRC_WINDOW and cap["remaining_new"] == 100
    qp.record(led_b, ["FRESH"], dt.date(2026, 4, 1), **cap["retention"])
    assert set(qp.load_ledger(led_b)) == {"FRESH"}

    # (c) broker telemetry WITH per-symbol detail may narrow to what it counts
    led_c = d / "c.json"
    qp.record(led_c, old100, day0)
    cap = qp.capacity_state(
        telemetry={"ok": True, "used": 2, "remain": 98, "detail_available": True,
                   "charged": {"T000", "T001"}},
        ledger_state=qp.load_ledger_state(led_c), today=TODAY, quota=100)
    assert cap["retention"] == {"keep_only": {"T000", "T001"}}
    qp.record(led_c, [], TODAY, **cap["retention"])
    assert set(qp.load_ledger(led_c)) == {"T000", "T001"}

    # (d) telemetry WITHOUT detail prunes nothing — we will not guess which
    led_d = d / "d.json"
    qp.record(led_d, old100, day0)
    cap = qp.capacity_state(
        telemetry={"ok": True, "used": 2, "remain": 98,
                   "detail_available": False, "charged": set()},
        ledger_state=qp.load_ledger_state(led_d), today=TODAY, quota=100)
    assert cap["retention"] == {}
    qp.record(led_d, [], TODAY, **cap["retention"])
    assert len(qp.load_ledger(led_d)) == 100


def test_retention_cannot_raise_capacity_under_unknown():
    """Every branch that could not establish capacity returns NO retention, so
    a write under UNKNOWN can only ever ADD to the ledger."""
    for cap in (
        qp.capacity_state(ledger_state={"requests": {}, "present": False,
                                        "readable": True},
                          today=TODAY, quota=100),
        qp.capacity_state(ledger_state={"requests": {}, "present": True,
                                        "readable": False},
                          today=TODAY, quota=100),
        qp.capacity_state(ledger_state={"requests": {"A": "2020-01-01"},
                                        "present": True, "readable": True},
                          today=TODAY, quota=100),
        qp.capacity_state(telemetry={"ok": False}, ledger_state=None,
                          today=TODAY, quota=100),
    ):
        assert cap["retention"] == {}, cap["source"]


def test_an_unparseable_timestamp_is_never_dropped_by_a_write():
    """⛔ THE TWO HALVES USED TO DISAGREE. `charged_symbols()` deliberately
    COUNTS an entry whose date will not parse (unknown-when is conservative),
    while `record()` discarded it via `except ValueError: continue`. So it
    counted against the budget until the next write and then vanished, leaking a
    slot."""
    d = Path(tempfile.mkdtemp()); _TMPDIRS.append(d)
    led = d / "q.json"
    led.write_text(json.dumps({"requests": {"ODD": "not-a-date",
                                            "GOOD": "2026-01-01"}}))
    assert qp.charged_symbols(qp.load_ledger(led), TODAY, None) == {"ODD", "GOOD"}
    qp.record(led, ["NEW"], TODAY)
    assert set(qp.load_ledger(led)) == {"ODD", "GOOD", "NEW"}
    # even an authorised prune keeps it: unknown-when means still-counting
    qp.record(led, [], TODAY, prune_before=dt.date(2026, 6, 1))
    assert "ODD" in qp.load_ledger(led), "unknown-when must survive a prune"
    assert "GOOD" not in qp.load_ledger(led), "a dated old entry is superseded"


# =========================================================================== #
# E. the order gate — the account gates must be UNCHANGED
# =========================================================================== #
def test_buy_allowed_only_for_eligible_and_scoreable():
    repo = _repo(_doc(names=[_row("GOOD", 1), _row("PENDING", 2)]),
                 states={"GOOD": "complete", "PENDING": "seasoning"})
    assert _gate(repo, "GOOD", "buy")[0] is True
    assert _gate(repo, "PENDING", "buy")[0] is False
    assert _gate(repo, "NEVER", "buy")[0] is False


def test_existing_account_and_order_gates_are_unchanged():
    """The order cap, the invalid-amount refusal and the invalid-account-value
    refusal behave exactly as before, and none of them touches a sell."""
    repo = _repo()
    # order cap: 15% of 1000 = 150
    assert _gate(repo, "AAA", "buy", amount=149.0)[0] is True
    ok, why = _gate(repo, "AAA", "buy", amount=151.0)
    assert ok is False and "exceeds max order" in why, why
    # a sell is never capped — capping one would strand a position
    assert _gate(repo, "AAA", "sell", amount=10_000.0)[0] is True
    # malformed amount / account value refuse BUYS only
    for bad in (None, "x", float("nan"), float("inf"), True):
        assert _gate(repo, "AAA", "buy", amount=bad)[0] is False, bad
    assert _gate(repo, "AAA", "buy", account_value=float("nan"))[0] is False
    assert _gate(repo, "AAA", "sell", account_value=float("nan"))[0] is True


def test_require_whitelist_false_disables_the_check_in_both_modes():
    off = copy.deepcopy(CFG)
    off["governance"]["require_whitelist"] = False
    assert _gate(_repo(omit_cohort=True), "ANYTHING", "buy", cfg=off)[0] is True


def test_fixed_list_mode_is_byte_for_byte_the_old_behaviour():
    """⛔ THE ROLLBACK. With mode = fixed_list nothing reads the cohort at all —
    an ABSENT cohort must not change a single verdict."""
    legacy = copy.deepcopy(CFG)
    legacy["universe"]["mode"] = "fixed_list"
    repo = _repo(omit_cohort=True)                # no cohort on disk whatsoever
    assert _gate(repo, "LEGACY", "buy", cfg=legacy)[0] is True
    assert _gate(repo, "AAA", "buy", cfg=legacy)[0] is False
    assert _gate(repo, "AAA", "sell", cfg=legacy)[0] is True
    # an unrecognised mode string falls back to fixed_list, never to something wider
    typo = copy.deepcopy(CFG)
    typo["universe"]["mode"] = "cohortt"
    assert cohort.mode(typo) == "fixed_list"
    assert _gate(repo, "LEGACY", "buy", cfg=typo)[0] is True


def test_an_active_rule_out_still_blocks_a_buy():
    """The rule-out lives in the PreToolUse hook, ahead of vet_plan, and is
    unaffected by the cohort. Asserted through the hook's own decide()."""
    sys.path.insert(0, str(REPO / "scripts" / "hooks"))
    import pretooluse_order_gate as hook                    # noqa: PLC0415
    payload = {"tool_name": hook.ORDER_TOOL,
               "tool_input": {"symbol": "AAA", "side": "buy", "dollar_amount": "5"}}
    cfg = copy.deepcopy(CFG)
    cfg["proof"] = {"live_approved": True}
    orig = gov.REPO
    gov.REPO = _repo()
    try:
        ruled = {"AAA": {"ts": "2026-09-01", "reason": "thesis broken", "until": ""}}
        d = hook.decide(payload, cfg, {"account_value": 1000.0}, False, ruled)
        assert d["permissionDecision"] == "deny", d
        assert "RULED THIS OUT" in d["permissionDecisionReason"], d
        # ...and the same rule-out never blocks the SELL
        sell = {"tool_name": hook.ORDER_TOOL,
                "tool_input": {"symbol": "AAA", "side": "sell", "quantity": "1"}}
        assert hook.decide(sell, cfg, {"account_value": 1000.0}, False,
                           ruled)["permissionDecision"] == "allow"
    finally:
        gov.REPO = orig


def test_hook_denies_a_pending_buy_and_allows_the_sell():
    sys.path.insert(0, str(REPO / "scripts" / "hooks"))
    import pretooluse_order_gate as hook                    # noqa: PLC0415
    cfg = copy.deepcopy(CFG)
    cfg["proof"] = {"live_approved": True}
    repo = _repo(_doc(names=[_row("GOOD", 1), _row("PENDING", 2)]),
                 states={"GOOD": "complete", "PENDING": "seasoning"})
    orig = gov.REPO
    gov.REPO = repo
    try:
        def call(sym, side, **ti):
            return hook.decide({"tool_name": hook.ORDER_TOOL,
                                "tool_input": {"symbol": sym, "side": side, **ti}},
                               cfg, {"account_value": 1000.0}, False, {})
        assert call("GOOD", "buy", dollar_amount="5")["permissionDecision"] == "allow"
        pend = call("PENDING", "buy", dollar_amount="5")
        assert pend["permissionDecision"] == "deny"
        assert "PENDING" in pend["permissionDecisionReason"]
        assert call("PENDING", "sell", quantity="1")["permissionDecision"] == "allow"
        assert call("NEVER_SCREENED", "sell", quantity="1")["permissionDecision"] == "allow"
    finally:
        gov.REPO = orig


# =========================================================================== #
# F. one ranking
# =========================================================================== #
def test_candidates_universe_and_slow_loop_share_one_pool_and_one_signal():
    """⛔ `score` IS A PERCENTILE, SO THE POOL DEFINES IT. Two callers ranking
    slightly different sets do not produce 'almost the same' numbers — they
    produce different numbers for every name in common. That divergence shipped
    once (OPSLOG 2026-08-20: the agent's list had no residual tilt and pooled 18
    ETFs). Assert BEHAVIOURALLY, by source-reading AND by value."""
    src_server = (REPO / "src" / "agent_env" / "server.py").read_text()
    src_loop = (REPO / "scripts" / "slow_loop.py").read_text()
    # no caller may read the universe file directly to build a ranking pool
    for fn in ("def candidates(", "def universe("):
        body = src_server.split(fn, 1)[1][:2500]
        assert "ranking_pool()" in body, f"{fn} must use screen.ranking_pool()"
        assert "read_universe(UNIVERSE)" not in body, \
            f"{fn} still reads the universe file directly — the pool can drift"
    assert "ranking_pool(" in src_loop, "slow_loop must use screen.ranking_pool()"

    from agent_env import screen                            # noqa: PLC0415
    import momentum, residual                               # noqa: PLC0415
    import numpy as np, pandas as pd                        # noqa: PLC0415
    n = 300
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    t = np.arange(n)
    cols = {c: 100 * np.cumprod(1 + (0.001 + 0.004 * np.sin(2 * np.pi * t / (9 + i))))
            for i, c in enumerate(["AAA", "BBB", "CCC"])}
    for i, s in enumerate(list(residual.SECTOR_FACTORS) + ["SPY"]):
        cols[s] = 100 * np.cumprod(1 + (0.0004 + 0.003 * np.sin(2 * np.pi * t / (6 + i))))
    panel = pd.DataFrame(cols, index=idx)
    scfg = {"signal": {"residual_tilt": 0.75, "residual_factors": "sector"}}
    got = screen.rank_book(panel, idx[-1], ["AAA", "BBB", "CCC"], scfg)
    rk = residual.kwargs_from_config(scfg, panel, panel["SPY"], log=lambda _m: None)
    want = momentum.compute(panel[["AAA", "BBB", "CCC"]], idx[-1], **rk)
    assert np.allclose(got["score"].sort_index().values,
                       want["score"].sort_index().values)
    # ...and the tilt must BITE, else the equality above proves nothing
    plain = momentum.compute(panel[["AAA", "BBB", "CCC"]], idx[-1])
    assert not np.allclose(want["score"].sort_index().values,
                           plain["score"].sort_index().values)


def test_the_ranking_pool_is_the_scoreable_set_in_cohort_mode():
    from agent_env import screen                            # noqa: PLC0415
    d = _doc(names=[_row("SCORED", 1), _row("YOUNG", 2), _row("BROKEN", 3)])
    repo = _repo(d, states={"SCORED": "complete", "YOUNG": "seasoning",
                            "BROKEN": "failed_provider"})
    pool = screen.ranking_pool(CFG, repo, TODAY)
    assert pool["source"] == "cohort"
    assert pool["tickers"] == ["SCORED"], pool["tickers"]
    assert pool["view"]["counts"]["pending_history"] == 1


def test_a_degraded_cohort_still_shows_a_ranking_but_authorises_nothing():
    """⛔ TWO POLARITIES ON PURPOSE. Ranking is INFORMATION: refusing to rank
    would blind the session rather than protect it, so the read path falls back
    to the CSV and says so. The order gate is PERMISSION and never falls back.
    Do not 'fix' one to match the other."""
    from agent_env import screen                            # noqa: PLC0415
    repo = _repo(_doc(as_of="2026-01-01"))                  # stale
    pool = screen.ranking_pool(CFG, repo, TODAY)
    assert pool["source"] == "cohort_degraded"
    assert pool["tickers"] == ["LEGACY"], pool["tickers"]
    assert "BUYS are refused" in pool["note"]
    assert _gate(repo, "LEGACY", "buy")[0] is False, \
        "the read fallback must not become a buy authorisation"


# =========================================================================== #
# G. instruments
# =========================================================================== #
def test_no_fund_option_short_or_off_venue_instrument_becomes_buyable():
    """The cohort is built from a screen whose adds already pass a POSITIVE
    market-cap test (a fund carries none) plus a denylist backstop; the account
    has no option level at all and no short leg. Assert the layers that are
    testable here: the factor series are absent from the pool, a fund is
    refused, and a SELL side is the only thing that is ever permissive."""
    import residual                                         # noqa: PLC0415
    repo = _repo(_doc(names=[_row("GOOD", 1)]), states={"GOOD": "complete"})
    for fund in list(residual.SECTOR_FACTORS) + ["SPY", "QQQ", "GLD", "TLT"]:
        assert _gate(repo, fund, "buy")[0] is False, fund
    for lev in ("SOXL", "TQQQ", "UVXY"):
        assert _gate(repo, lev, "buy")[0] is False, lev
    # the screen's own add path refuses them before they could reach a cohort
    for fund in ("SPY", "XLK", "GLD", "SOXL"):
        assert not um._looks_like_common_stock(fund), fund
    # ...and must NOT refuse a REIT, which moomoo mislabels SecurityType.ETF
    for reit in ("PLD", "EQIX", "AMT", "DLR", "SPG", "O"):
        assert um._looks_like_common_stock(reit), reit


def test_approved_venue_and_complete_history_is_scoreable_and_buyable():
    for venue in um.ALLOWED_VENUES:
        repo = _repo(_doc(names=[_row("GOOD", 1, venue=venue)]),
                     states={"GOOD": "complete"})
        view = cohort.active_view(CFG, repo, TODAY)
        assert view["scoreable"] == ["GOOD"], venue
        assert view["by_ticker"]["GOOD"]["entry_eligible"] is True, venue
        assert _gate(repo, "GOOD", "buy")[0] is True, venue


def test_unknown_or_disallowed_venue_incumbents_are_NEVER_buyable():
    """⛔ THE DEFECT. `build_ranked_screen` exempts INCUMBENTS from the venue
    allowlist so a metadata gap cannot silently de-list a name we may hold. That
    exemption is right for KEEPING and catastrophic for BUYING: without a
    separate classification such an incumbent becomes `scoreable` the moment its
    backfill finishes, and is then authorised for a NEW entry on a venue nobody
    verified. Complete history is evidence about our panel, never about where
    the thing trades."""
    d = _doc(names=[_row("OK", 1, venue="US_NYSE", membership="incumbent"),
                    _row("PINK", 2, venue="US_PINK", membership="incumbent"),
                    _row("NOVENUE", 3, venue=None, membership="incumbent")])
    # every one of them has a COMPLETE window — history cannot rescue the venue
    repo = _repo(d, states={"OK": "complete", "PINK": "complete",
                            "NOVENUE": "complete"})
    view = cohort.active_view(CFG, repo, TODAY)
    assert view["scoreable"] == ["OK"], view["scoreable"]
    assert set(view["observe_only"]) == {"PINK", "NOVENUE"}, view["observe_only"]
    assert cohort.buyable(view) == {"OK"}
    for sym in ("PINK", "NOVENUE"):
        assert view["by_ticker"][sym]["status"] == cohort.OBSERVE_ONLY
        assert view["by_ticker"][sym]["entry_eligible"] is False
        assert view["by_ticker"][sym]["buyable"] is False
        ok, why = _gate(repo, sym, "buy")
        assert ok is False, sym
        assert "OBSERVE-ONLY" in why and "not buyable" in why, why


def test_those_incumbents_remain_sellable_and_observable():
    d = _doc(names=[_row("PINK", 1, venue="US_PINK", membership="incumbent")])
    repo = _repo(d, states={"PINK": "complete"})
    view = cohort.active_view(CFG, repo, TODAY)
    # OBSERVABLE: still a full record in the cohort, with its evidence intact
    rec = view["by_ticker"]["PINK"]
    assert rec["rank"] == 1 and rec["avg_daily_turnover_usd"] == 8e8
    assert rec["history_state"] == "complete"
    assert "PINK" in view["by_ticker"], "it must not be dropped from view"
    # SELLABLE: eligibility is an entry question, always
    assert _gate(repo, "PINK", "sell")[0] is True
    assert _gate(repo, "PINK", "sell", amount=10_000.0)[0] is True


def test_read_time_venue_enforcement_cannot_be_bypassed_by_the_artifact():
    """⛔ A PERMISSION CHECKED ONLY BY THE WRITER IS ONE ANY OTHER WRITER GRANTS
    ITSELF. An artifact that reached disk without passing the producer — hand
    edited, older build, restored backup — must not be able to assert its own
    buy-authorisation. `compose()` re-derives entry eligibility from `venue` and
    ignores the stored flag entirely."""
    forged = _doc(names=[_row("PINK", 1, venue="US_PINK")])
    forged["names"][0]["entry_eligible"] = True          # the lie
    # 1. it is refused at LOAD, because the claim contradicts its own evidence
    assert _raises(lambda: cohort.active_view(CFG, _repo(forged), TODAY),
                   "asserts a permission")
    # 2. and even if validation were somehow skipped, compose ignores the flag
    view = cohort.compose(forged, {"PINK": "complete"},
                          allowed_venues=um.ALLOWED_VENUES)
    assert view["scoreable"] == [], view["scoreable"]
    assert view["observe_only"] == ["PINK"]
    assert cohort.buyable(view) == set()
    assert view["by_ticker"]["PINK"]["entry_eligible"] is False


def test_an_off_venue_row_does_not_reject_the_whole_artifact():
    """It is CLASSIFIED, not fatal. Rejecting the cohort because one incumbent
    has a missing exchange string would turn a metadata gap into a box-wide buy
    freeze — while the other 179 names are perfectly verifiable."""
    d = _doc(names=[_row("OK", 1, venue="US_NASDAQ"),
                    _row("PINK", 2, venue="US_PINK")])
    assert cohort.validate(d, allowed_venues=um.ALLOWED_VENUES) == []
    repo = _repo(d, states={"OK": "complete", "PINK": "complete"})
    assert _gate(repo, "OK", "buy")[0] is True, "the good names still trade"


def test_the_producer_stamps_entry_eligibility_and_validate_agrees():
    rows = [{"symbol": "GOOD", "name": "g", "market_cap": 5e9,
             "turnover_20d_cum": 4e10},
            {"symbol": "INCUMB", "name": "i", "market_cap": 5e9,
             "turnover_20d_cum": 3e10}]
    ex = {"GOOD": "US_NYSE", "INCUMB": "US_PINK"}
    ranked, turn, rep = um.build_ranked_screen(rows, ex, 0, incumbents=["INCUMB"])
    assert ranked == ["GOOD", "INCUMB"], "the incumbent keeps its rank"
    doc = cohort.build(ranked, turn, rep["market_caps"], rep["venues"],
                       as_of="2026-09-11", generated_at="x",
                       incumbents=["INCUMB"], allowed=um.ALLOWED_VENUES,
                       provenance={"screen_version": "v2_turnover/1"},
                       coverage={"complete": True, "problems": []})
    flags = {n["ticker"]: n["entry_eligible"] for n in doc["names"]}
    assert flags == {"GOOD": True, "INCUMB": False}, flags
    assert cohort.validate(doc, min_turnover_usd=50_000_000.0,
                           allowed_venues=um.ALLOWED_VENUES) == []


# =========================================================================== #
# H. history repair persistence (integration, fake provider — no network)
# =========================================================================== #
def _fetch_prices():
    import importlib.util                                    # noqa: PLC0415
    spec = importlib.util.spec_from_file_location(
        "_fp", REPO / "scripts" / "fetch_prices.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _FakeMMP:
    """Stands in for adapters.moomoo.prices. Records what was asked for; returns
    candles in the shape `_field_panels` expects. NO network, NO moomoo import,
    NO quota spent."""

    def __init__(self, bars_by_ticker, errors=None, remain=100, used=0,
                 telemetry_ok=True):
        self.bars, self.errors = bars_by_ticker, errors or {}
        self.requested = None
        self._tel = {"ok": telemetry_ok, "used": used, "remain": remain,
                     "charged": set(), "detail_available": True, "error": None}

    def history_quota(self, ctx=None, detail=True):
        """Stands in for the broker's get_history_kl_quota(). Mocked, never live."""
        return dict(self._tel)

    def daily_panel(self, tickers, start, end, ctx=None):
        self.requested = list(tickers)
        return ({t: self.bars[t] for t in tickers if t in self.bars},
                {t: self.errors[t] for t in tickers if t in self.errors})


class _FakeMMR:
    def __init__(self, ages): self.ages = ages
    def listing_dates(self, tickers, ctx=None):
        return {t: self.ages.get(t) for t in tickers}


def _candles(dates, price):
    import pandas as pd                                      # noqa: PLC0415
    return [{"datetime": int(pd.Timestamp(d).value // 10**6), "open": price,
             "high": price, "low": price, "close": price, "turnover": 1e6}
            for d in dates]


def test_repair_merges_fetched_bars_into_every_panel_and_completes_the_window():
    """⛔ THE REGRESSION THIS PINS. The merge — `_field_panels(raw)` folded into
    `panels` with `combine_first` — was silently dropped while the quota
    bookkeeping was added around it. The request still went out, the ledger was
    still stamped, and the completeness check then ran against UNCHANGED panels,
    so every repaired name reported STILL INCOMPLETE for ever while spending a
    quota unit per run. It hit fixed_list mode too. Assert the DATA lands."""
    import numpy as np, pandas as pd                         # noqa: PLC0415
    fp = _fetch_prices()
    import history_repair as hr                              # noqa: PLC0415

    idx = pd.date_range("2025-01-01", periods=hr.REQUIRED_ROWS + 40, freq="B")
    full = pd.Series(np.linspace(10.0, 20.0, len(idx)), index=idx)
    # GAP is complete except for the last 30 sessions — unscoreable by the
    # window test, and repairable.
    gap = full.copy()
    gap.iloc[-30:] = np.nan
    panels = {f: pd.DataFrame({"FULL": full, "GAP": gap}) for f in fp._FIELDS}
    assert not hr.window_complete(panels["close"]["GAP"]), "fixture must start broken"

    mmp = _FakeMMP({"GAP": _candles(idx[-30:], 99.0)})
    d = Path(tempfile.mkdtemp()); _TMPDIRS.append(d)
    cfg = {"history_acquisition": {"distinct_symbol_quota": 100,
                                   "ledger_file": "q.json",
                                   "reset_file": "reset.json"},
           "universe": {"mode": "fixed_list"}}
    orig_repo = fp.REPO
    fp.REPO = d
    try:
        out, plan = fp._repair_incomplete(
            panels, ["FULL", "GAP"], object(), mmp,
            _FakeMMR({"GAP": dt.date(2015, 1, 1), "FULL": dt.date(2015, 1, 1)}),
            dt.date(2026, 9, 11), cfg=cfg)

        assert mmp.requested == ["GAP"], mmp.requested
        # 1. the bars merged into EVERY field panel, not just closes. turnover
        #    carries its own value in the fixture, so assert per field rather
        #    than one number — a single expected value here would have passed
        #    vacuously on four panels and told us nothing about the fifth.
        want = {"open": 99.0, "high": 99.0, "low": 99.0, "close": 99.0,
                "turnover": 1e6}
        for f in fp._FIELDS:
            assert out[f].loc[idx[-1], "GAP"] == want[f], (f, out[f].loc[idx[-1], "GAP"])
        # 2. the ticker is now window-complete
        assert hr.window_complete(out["close"]["GAP"]), \
            "the merge must make the repaired name scoreable"
        # 3. EXISTING data is retained where the fetch had nothing
        assert out["close"].loc[idx[0], "GAP"] == full.iloc[0], \
            "a partial fetch must fill gaps, never erase stored observations"
        assert out["close"]["FULL"].equals(full), "an untouched name is untouched"
        # 4. the quota ledger recorded the attempted request
        assert set(qp.load_ledger(d / "q.json")) == {"GAP"}

        # 5. the persisted history state says `complete`
        hpath = d / "history_state.json"
        states = fp.write_history_state(out, ["FULL", "GAP"], plan, hpath,
                                        dt.date(2026, 9, 11))
        assert states == {"FULL": "complete", "GAP": "complete"}, states
        assert cohort.load_history_state(hpath)["GAP"] == cohort.HIST_COMPLETE
    finally:
        fp.REPO = orig_repo


def test_a_failed_repair_is_not_marked_complete_but_is_still_charged():
    """No-data / error responses must not become `complete`, and must still be
    charged: the meter charges a SYMBOL, not a successful response."""
    import numpy as np, pandas as pd                         # noqa: PLC0415
    fp = _fetch_prices()
    import history_repair as hr                              # noqa: PLC0415

    idx = pd.date_range("2025-01-01", periods=hr.REQUIRED_ROWS + 40, freq="B")
    full = pd.Series(np.linspace(10.0, 20.0, len(idx)), index=idx)
    gap = full.copy(); gap.iloc[-30:] = np.nan
    panels = {f: pd.DataFrame({"FULL": full, "DEAD": gap}) for f in fp._FIELDS}

    mmp = _FakeMMP({}, errors={"DEAD": "no data for US.DEAD"})
    d = Path(tempfile.mkdtemp()); _TMPDIRS.append(d)
    cfg = {"history_acquisition": {"distinct_symbol_quota": 100,
                                   "ledger_file": "q.json",
                                   "reset_file": "reset.json"},
           "universe": {"mode": "fixed_list"}}
    orig_repo = fp.REPO
    fp.REPO = d
    try:
        out, plan = fp._repair_incomplete(
            panels, ["FULL", "DEAD"], object(), mmp,
            _FakeMMR({"DEAD": dt.date(2015, 1, 1), "FULL": dt.date(2015, 1, 1)}),
            dt.date(2026, 9, 11), cfg=cfg)
        assert not hr.window_complete(out["close"]["DEAD"])
        assert plan["errors"] == {"DEAD": "no data for US.DEAD"}, plan["errors"]
        assert set(qp.load_ledger(d / "q.json")) == {"DEAD"}, \
            "an attempted request is charged even when it returns nothing"
        states = fp.write_history_state(out, ["FULL", "DEAD"], plan,
                                        d / "hs.json", dt.date(2026, 9, 11))
        assert states["DEAD"] == cohort.HIST_FAILED, states
        assert states["FULL"] == cohort.HIST_COMPLETE, states
        # ...and a failed_provider name is a research lead, never buyable
        view = cohort.compose(_doc(names=[_row("DEAD", 1)]), states,
                              allowed_venues=um.ALLOWED_VENUES)
        assert view["unscoreable"] == ["DEAD"] and cohort.buyable(view) == set()
    finally:
        fp.REPO = orig_repo


def test_nothing_is_requested_or_charged_when_capacity_is_exhausted():
    """A spent window must defer rather than request — and therefore must not
    stamp the ledger either, since nothing was attempted."""
    import numpy as np, pandas as pd                         # noqa: PLC0415
    fp = _fetch_prices()
    import history_repair as hr                              # noqa: PLC0415

    idx = pd.date_range("2025-01-01", periods=hr.REQUIRED_ROWS + 40, freq="B")
    full = pd.Series(np.linspace(10.0, 20.0, len(idx)), index=idx)
    gap = full.copy(); gap.iloc[-30:] = np.nan
    panels = {f: pd.DataFrame({"GAP": gap}) for f in fp._FIELDS}

    d = Path(tempfile.mkdtemp()); _TMPDIRS.append(d)
    # the broker itself reports the window fully spent
    mmp = _FakeMMP({"GAP": _candles(idx[-30:], 99.0)}, remain=0, used=100)
    cfg = {"history_acquisition": {"distinct_symbol_quota": 100,
                                   "ledger_file": "q.json",
                                   "reset_file": "reset.json"},
           "universe": {"mode": "fixed_list"}}
    orig_repo = fp.REPO
    fp.REPO = d
    try:
        out, plan = fp._repair_incomplete(
            panels, ["GAP"], object(), mmp,
            _FakeMMR({"GAP": dt.date(2015, 1, 1)}), dt.date(2026, 9, 11), cfg=cfg)
        assert mmp.requested is None, "no request may be issued with no capacity"
        assert plan["deferred"] == ["GAP"], plan["deferred"]
        assert "GAP" not in qp.load_ledger(d / "q.json"), \
            "a name that was never requested must not be charged"
        states = fp.write_history_state(out, ["GAP"], plan, d / "hs.json",
                                        dt.date(2026, 9, 11))
        assert states["GAP"] == cohort.HIST_DEFERRED, states
    finally:
        fp.REPO = orig_repo


# =========================================================================== #
# I. --backfill is governed by the SAME quota authority (no bypass)
# =========================================================================== #
def _backfill_fixture(remain, *, ledger=None):
    """A repo dir + panels + a fake provider whose telemetry reports `remain`."""
    import numpy as np, pandas as pd                         # noqa: PLC0415
    fp = _fetch_prices()
    idx = pd.date_range("2025-01-01", periods=60, freq="B")
    full = pd.Series(np.linspace(10.0, 20.0, len(idx)), index=idx)
    gap = full.copy(); gap.iloc[-10:] = np.nan
    names = ["A001", "A002", "A003", "A004"]
    panels = {f: pd.DataFrame({n: gap for n in names}) for f in fp._FIELDS}
    d = Path(tempfile.mkdtemp()); _TMPDIRS.append(d)
    if ledger:
        qp.record(d / "q.json", ledger, dt.date(2026, 9, 11))
    mmp = _FakeMMP({n: _candles(idx[-10:], 55.0) for n in names},
                   remain=remain, used=100 - remain)
    cfg = {"history_acquisition": {"distinct_symbol_quota": 100,
                                   "ledger_file": "q.json",
                                   "reset_file": "reset.json"},
           "universe": {"mode": "fixed_list"}}
    return fp, d, panels, names, mmp, cfg, idx


def test_backfill_with_exhausted_capacity_calls_nothing_and_records_nothing():
    """⛔ `--backfill` USED TO BYPASS THE METER ENTIRELY: it sliced its list at
    100, called `daily_panel()` directly, and wrote NO ledger entry — so an
    operator backfill was invisible to the next automatic repair, which then
    planned as if the quota were untouched. It is a deliberate operator action;
    that makes it exempt from nothing."""
    fp, d, panels, names, mmp, cfg, idx = _backfill_fixture(remain=0)
    orig = fp.REPO
    fp.REPO = d
    try:
        out, res = fp.authorized_history_fetch(
            panels, names, cfg=cfg, ctx=object(), mmp=mmp,
            today=dt.date(2026, 9, 11), start="2025-01-01", end="2026-09-11",
            label="backfill")
        assert mmp.requested is None, "no history endpoint may be called"
        assert res["request"] == [], res["request"]
        assert set(res["deferred"]) == set(names), res["deferred"]
        assert not (d / "q.json").exists(), \
            "nothing was attempted, so no ledger entry may be written"
        for f in fp._FIELDS:                       # panels untouched
            assert out[f].isna().sum().sum() == panels[f].isna().sum().sum()
    finally:
        fp.REPO = orig


def test_backfill_with_partial_capacity_requests_only_the_permitted_subset():
    """Two slots left -> exactly two symbols requested, and exactly those two
    recorded. The operator's ordering is honoured within the permitted subset."""
    import pandas as pd                                      # noqa: PLC0415
    fp, d, panels, names, mmp, cfg, idx = _backfill_fixture(remain=2)
    orig = fp.REPO
    fp.REPO = d
    try:
        out, res = fp.authorized_history_fetch(
            panels, names, cfg=cfg, ctx=object(), mmp=mmp,
            today=dt.date(2026, 9, 11), start="2025-01-01", end="2026-09-11",
            cohort_ranks={t: i + 1 for i, t in enumerate(names)},
            label="backfill")
        assert len(res["request"]) == 2, res["request"]
        assert res["request"] == ["A001", "A002"], res["request"]
        assert len(res["deferred"]) == 2, res["deferred"]
        assert mmp.requested == res["request"], mmp.requested
        # recorded EXACTLY what was attempted — no more, no less
        assert set(qp.load_ledger(d / "q.json")) == set(res["request"])
        # and the bars for those two merged into the panels
        for t in res["request"]:
            assert out["close"].loc[idx[-1], t] == 55.0, t
        for t in res["deferred"]:
            assert pd.isna(out["close"].loc[idx[-1], t]), t
    finally:
        fp.REPO = orig


def test_backfill_and_repair_share_one_quota_authority():
    """Both paths call the same function, so neither can hold its own
    accounting. Asserted on the CODE, because this is a structural property."""
    src = (REPO / "scripts" / "fetch_prices.py").read_text()
    body = src.split("if args.backfill:", 1)[1].split("else:", 1)[0]
    assert "authorized_history_fetch(" in body, \
        "--backfill must obtain its plan through the shared quota-aware path"
    assert "daily_panel" not in body.replace("daily_panel()`", ""), \
        "--backfill must not call the history endpoint directly"


def test_no_history_request_path_bypasses_the_quota_planner():
    """⛔ STRUCTURAL REGRESSION GUARD. There must be exactly ONE call site for
    the metered endpoint in the whole price path, and it must live inside
    `authorized_history_fetch`, which is the only function that plans against
    remaining capacity and records what it attempted. A second call site added
    later — a new flag, a one-off repair, a 'quick' rebuild — would reopen
    precisely the hole `--backfill` was."""
    import re                                                # noqa: PLC0415
    src = (REPO / "scripts" / "fetch_prices.py").read_text()
    # strip comments and docstrings so prose about daily_panel does not count
    code = "\n".join(ln.split("#")[0] for ln in src.splitlines())
    code = re.sub(r'"""1?.*?"""', "", code, flags=re.S)
    calls = re.findall(r"\bmmp\.daily_panel\s*\(|\bdaily_panel\s*\(", code)
    assert len(calls) == 1, f"expected exactly 1 daily_panel call site, got {len(calls)}"

    fn = src.split("def authorized_history_fetch(", 1)[1].split("\ndef ", 1)[0]
    assert "mmp.daily_panel(" in fn, \
        "the single call site must be inside authorized_history_fetch"
    # ⛔ STRIP THE DOCSTRING BEFORE ORDERING. It NAMES these calls in prose, in
    # a different order from the code, so indexing the raw text measures the
    # comment rather than the behaviour — a test that would have "passed" on a
    # function whose body did them backwards.
    fn = fn.split('"""', 2)[-1] if fn.count('"""') >= 2 else fn
    # ...and that function must plan and record around it, in that order
    assert fn.index("qp.plan(") < fn.index("mmp.daily_panel("), \
        "capacity must be planned BEFORE the request is issued"
    assert fn.index("mmp.daily_panel(") < fn.index("qp.record("), \
        "the ledger must be stamped AFTER the request is attempted"
    assert fn.index("combine_first") < fn.index("qp.record("), \
        "fetched bars must merge into the panels before bookkeeping"
    # every other module must reach history through this path, not the adapter
    for other in ("scripts/slow_loop.py", "src/agent_env/server.py",
                  "scripts/universe_refresh.py"):
        text = (REPO / other).read_text()
        code2 = "\n".join(ln.split("#")[0] for ln in text.splitlines())
        code2 = re.sub(r'"""1?.*?"""', "", code2, flags=re.S)
        assert "daily_panel(" not in code2, f"{other} calls the metered endpoint"


# =========================================================================== #
def _raises(fn, needle):
    try:
        fn()
    except cohort.CohortInvalid as e:
        assert needle.lower() in str(e).lower(), f"expected {needle!r} in {e}"
        return str(e)
    raise AssertionError(f"expected CohortInvalid containing {needle!r}")


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    try:
        for name, fn in tests:
            try:
                fn()
                print(f"  ok   {name}")
            except Exception as e:                          # noqa: BLE001
                failed.append((name, e))
                print(f"  FAIL {name}: {type(e).__name__}: {e}")
    finally:
        for d in _TMPDIRS:
            shutil.rmtree(d, ignore_errors=True)
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
