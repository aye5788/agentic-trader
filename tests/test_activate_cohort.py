"""Isolated tests for the one-shot cohort activation runner.

    .venv/bin/python tests/test_activate_cohort.py

⛔ NOTHING LIVE. No moomoo call, no systemd call, no reload, no touch of the real
`config/strategy.local.toml`, and no history quota spent. The runner's module
globals (`REPO`, `LOCAL_CFG`, `STATUS`, `LOG`) are redirected into a temporary
tree for every test, and the functions that would reach outside it — the price
path, the readiness probe, the reload — are replaced with fakes that RECORD
whether they were called. "Was it called?" is the assertion that matters for the
dry-run contract; a test that only checked the return value would pass on a
runner that shelled out to `fetch_prices` first.

WHAT THESE PIN
  A. the final-view gate BLOCKS on unscoreable > 0 and on pending_history > 0,
     and a clean view passes it;
  B. a hold removes the runner-owned override and leaves every other local
     setting byte-identical;
  C. `--dry-run` writes ONLY its two declared diagnostic artifacts and changes
     no state.

(C) exists because the docstring used to say dry-run "writes NOTHING" while
writing exactly two files. The fix was to state the contract accurately; this is
what stops the statement drifting away from the behaviour again.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

_TMPDIRS: list[Path] = []


def _load_runner():
    """Fresh module object per test, so redirected globals never leak."""
    spec = importlib.util.spec_from_file_location(
        "_activate_cohort", REPO / "scripts" / "activate_cohort.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


LOCAL_BEFORE = """# Box-local strategy overrides
[proof]
live_approved = true

[risk_review]
alert_only = false
"""


def _sandbox(ac):
    """Redirect every path the runner writes into a throwaway tree."""
    d = Path(tempfile.mkdtemp())
    _TMPDIRS.append(d)
    (d / "config").mkdir()
    (d / "research_store" / "universe").mkdir(parents=True)
    (d / "logs").mkdir()
    (d / "config" / "strategy.local.toml").write_text(LOCAL_BEFORE)
    ac.REPO = d
    ac.LOCAL_CFG = d / "config" / "strategy.local.toml"
    ac.STATUS = d / "research_store" / "universe" / "activation_status.json"
    ac.LOG = d / "logs" / "cohort_activation.log"
    return d


def _view(scoreable, pending=0, unscoreable=0, observe=(), stale=False):
    n = list(scoreable)
    return {
        "counts": {"eligible": len(n) + pending + unscoreable + len(observe),
                   "scoreable": len(n), "pending_history": pending,
                   "unscoreable": unscoreable, "observe_only": len(observe)},
        "scoreable": n,
        "pending_history": [f"P{i:03d}" for i in range(pending)],
        "unscoreable": [f"U{i:03d}" for i in range(unscoreable)],
        "observe_only": list(observe),
        "freshness": {"stale": stale, "as_of": "2026-09-11", "age_days": 0,
                      "max_age_days": 10, "reason": ""},
        "by_ticker": {t: {"buyable": True} for t in n},
    }


def _stub_cohort(ac, view):
    """Feed gate_final_view a composed view without touching disk or config."""
    import types                                             # noqa: PLC0415
    fake_cohort = types.SimpleNamespace(
        active_view=lambda cfg, repo, today: view,
        buyable=lambda v: {t for t, r in v["by_ticker"].items() if r["buyable"]},
    )
    fake_strategy = types.SimpleNamespace(load=lambda: {"universe": {"mode": "cohort"}})
    sys.modules["cohort"] = fake_cohort
    sys.modules["strategy"] = fake_strategy


def _unstub():
    for m in ("cohort", "strategy"):
        sys.modules.pop(m, None)


# =========================================================================== #
# A. the final-view gate
# =========================================================================== #
def test_unscoreable_blocks_activation():
    """⛔ THE DEFECT. This was `rep.gate(..., True, ...)` — a gate that cannot
    fail, so a provider failure was reported and waved through. An
    eligible_unscoreable name spent a distinct-symbol quota unit and returned
    nothing usable; activating on top of it bakes a broken repair in as healthy."""
    ac = _load_runner(); _sandbox(ac)
    try:
        _stub_cohort(ac, _view([f"T{i:03d}" for i in range(140)], unscoreable=1))
        rep = ac.Report("--run")
        raised = False
        try:
            ac.gate_final_view(rep, fixed_ranked=148)
        except ac.GateFailed as e:
            raised = True
            assert "unscoreable" in str(e), e
        assert raised, "unscoreable > 0 MUST block activation"
        failed = [g for g in rep.gates if not g["ok"]]
        assert failed and "unscoreable" in failed[0]["gate"], failed
        assert "U000" in failed[0]["detail"], "the failing names must be named"
    finally:
        _unstub()


def test_pending_history_blocks_this_friday_activation():
    """The verified plan measured 48 names needing history against 88 free
    slots. A survivor means that assumption did not hold — stricter than the
    everyday design on purpose, because a one-shot unattended flip gets one
    clean shot."""
    ac = _load_runner(); _sandbox(ac)
    try:
        _stub_cohort(ac, _view([f"T{i:03d}" for i in range(140)], pending=3))
        rep = ac.Report("--run")
        rep.facts["quota_telemetry"] = {"ok": True, "used": 12, "remain": 88}
        raised = False
        try:
            ac.gate_final_view(rep, fixed_ranked=148)
        except ac.GateFailed as e:
            raised = True
            assert "pending" in str(e), e
        assert raised, "pending_history > 0 MUST block this activation"
        failed = [g for g in rep.gates if not g["ok"]]
        assert "pending-history" in failed[0]["gate"], failed
        # the refusal must carry the telemetry it was judged against
        assert "remain=88" in failed[0]["detail"], failed[0]["detail"]


        # ⛔ AND HEALTHY TELEMETRY MUST NOT EXCUSE IT. The old code passed the
        # pending gate whenever telemetry merely answered, which is why a
        # survivor could have slipped through on a run where the quota was fine.
        assert not any(g["ok"] and "pending" in g["gate"] for g in rep.gates)
    finally:
        _unstub()


def test_a_clean_view_passes_the_final_gate():
    """Zero pending, zero unscoreable, a workable population -> advances."""
    ac = _load_runner(); _sandbox(ac)
    try:
        _stub_cohort(ac, _view([f"T{i:03d}" for i in range(180)]))
        rep = ac.Report("--run")
        rep.facts["quota_telemetry"] = {"ok": True, "used": 60, "remain": 40}
        view = ac.gate_final_view(rep, fixed_ranked=148)
        assert view["counts"]["scoreable"] == 180
        assert all(g["ok"] for g in rep.gates), [g for g in rep.gates if not g["ok"]]
        names = [g["gate"] for g in rep.gates]
        assert any("unscoreable" in n for n in names), "the gate must still RUN"
        assert any("pending-history" in n for n in names)
    finally:
        _unstub()


def test_the_scoreable_floor_and_collapse_checks_survive():
    """The corrections must not have displaced the existing population checks."""
    ac = _load_runner(); _sandbox(ac)
    try:
        # below the absolute floor
        _stub_cohort(ac, _view([f"T{i:03d}" for i in range(20)]))
        rep = ac.Report("--run")
        try:
            ac.gate_final_view(rep, fixed_ranked=148); raise AssertionError("no block")
        except ac.GateFailed as e:
            assert "workable book" in str(e), e
        # above the floor but collapsed vs the fixed list
        _stub_cohort(ac, _view([f"T{i:03d}" for i in range(110)]))
        rep = ac.Report("--run")
        try:
            ac.gate_final_view(rep, fixed_ranked=148); raise AssertionError("no block")
        except ac.GateFailed as e:
            assert "collapsed" in str(e), e
        # a stale view blocks too
        _stub_cohort(ac, _view([f"T{i:03d}" for i in range(180)], stale=True))
        rep = ac.Report("--run")
        try:
            ac.gate_final_view(rep, fixed_ranked=148); raise AssertionError("no block")
        except ac.GateFailed as e:
            assert "fresh" in str(e), e
    finally:
        _unstub()


# =========================================================================== #
# B. a hold removes the runner's override and nothing else
# =========================================================================== #
def test_hold_removes_only_the_runner_override():
    ac = _load_runner(); d = _sandbox(ac)
    before = ac.LOCAL_CFG.read_text()
    ac.add_override()
    assert ac.override_present()
    assert "live_approved = true" in ac.LOCAL_CFG.read_text()
    ac.remove_override()
    assert not ac.override_present()
    assert ac.LOCAL_CFG.read_text() == before, \
        "removing the override must restore the file byte-for-byte"
    # idempotent both ways
    ac.remove_override()
    ac.add_override(); ac.add_override()
    assert ac.LOCAL_CFG.read_text().count(ac.MARK_BEGIN) == 1, "no duplicate block"


# =========================================================================== #
# C. the dry-run contract, asserted against the DECLARED artifact list
# =========================================================================== #
def test_dry_run_writes_only_the_declared_diagnostics_and_changes_no_state():
    """⛔ THE CONTRACT THE DOCSTRING USED TO GET WRONG. It claimed dry-run
    "writes NOTHING" while writing the status file and the log. The fix was to
    state the contract accurately; this pins the behaviour to the statement.

    Asserted by SNAPSHOTTING the whole sandbox tree before and after, so a file
    written somewhere nobody thought to check still fails the test."""
    ac = _load_runner(); d = _sandbox(ac)

    called = {"price_path": False, "reload": False, "readiness": False}
    ac.run_price_path = lambda rep, dry: called.__setitem__("price_path", True)
    ac.do_reload = lambda rep: called.__setitem__("reload", True)
    ac.gate_readiness_reads = lambda rep: called.__setitem__("readiness", True)
    # quota telemetry is read-only, but stub it so no OpenD connection is made
    ac.gate_quota = lambda rep: rep.gate("broker quota telemetry", True, "stubbed")
    # a cohort artifact carrying the expected date, so the run reaches the end
    doc = {"schema_version": 1, "as_of": "2026-09-11",
           "coverage": {"complete": True, "problems": []},
           "provenance": {"screen_version": "v2_turnover/1"}, "names": []}
    ac.gate_fresh_cohort = lambda rep, expect: doc
    ac.gate_health_held = lambda rep, d_: None
    ac.gate_clean_tree = lambda rep: rep.gate("working tree clean", True, "stubbed")
    ac.loaded_mode = lambda: "fixed_list"

    def snapshot(root: Path) -> dict:
        return {str(p.relative_to(root)): p.stat().st_mtime_ns
                for p in root.rglob("*") if p.is_file()}

    before = snapshot(d)
    before_cfg = ac.LOCAL_CFG.read_text()

    sys.argv = ["activate_cohort.py", "--dry-run", "--expect-asof", "2026-09-11",
                "--allow-any-time"]
    rc = ac.main()
    after = snapshot(d)

    assert rc == 0, rc
    # 1. nothing was invoked that changes state
    assert called == {"price_path": False, "reload": False, "readiness": False}, called
    # 2. config untouched, byte-for-byte
    assert ac.LOCAL_CFG.read_text() == before_cfg, "dry run modified the local config"
    assert not ac.override_present(), "dry run wrote the activation override"
    # 3. the ONLY new/changed files are the two declared diagnostics
    declared = {"research_store/universe/activation_status.json",
                "logs/cohort_activation.log"}
    changed = {k for k in after if before.get(k) != after[k]}
    assert changed <= declared, f"dry run wrote undeclared files: {changed - declared}"
    assert changed, "the dry run should have written its diagnostics"
    # 4. no cohort / history / ledger / reset artifact was created
    for forbidden in ("research_store/universe/cohort.json",
                      "research_store/universe/history_state.json",
                      "research_store/universe/history_quota.json",
                      "research_store/universe/history_quota_reset.json"):
        assert forbidden not in after, forbidden
    # 5. the status file DECLARES what it was allowed to touch, and says so
    st = json.loads(ac.STATUS.read_text())
    assert st["outcome"] == "DRY_RUN_GATES_PASSED", st["outcome"]
    assert st["activated"] is False
    assert st["writes_state"] is False
    assert set(Path(p).name for p in st["diagnostic_artifacts"]) == \
        {"activation_status.json", "cohort_activation.log"}
    assert "changed no state" in st["rollback"], st["rollback"]


def test_the_docstring_and_help_no_longer_claim_writes_nothing():
    """The statement and the behaviour are now the same thing; keep them so."""
    src = (REPO / "scripts" / "activate_cohort.py").read_text()
    # The phrase may appear ONCE, inside the sentence that records it as a past
    # error. Anywhere else it is the claim itself, back again.
    occurrences = src.count("writes NOTHING")
    assert occurrences <= 1, f"{occurrences} occurrences — the claim is back"
    if occurrences:
        assert 'used to claim it "writes NOTHING"' in src, \
            "the only permitted occurrence is the recorded correction"
    assert "activation_status.json" in src and "cohort_activation.log" in src, \
        "the docstring must name the artifacts dry-run does write"
    manual = (REPO / "docs" / "OPERATOR_MANUAL.md").read_text()
    assert "writes **only**" in manual, "the manual must state the dry-run contract"


# =========================================================================== #
def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    try:
        for name, fn in tests:
            try:
                fn()
                print(f"  ok   {name}")
            except Exception as e:                            # noqa: BLE001
                failed.append((name, e))
                print(f"  FAIL {name}: {type(e).__name__}: {e}")
    finally:
        for d in _TMPDIRS:
            shutil.rmtree(d, ignore_errors=True)
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
