"""Universe maintenance — WEEKLY (Friday) liquidity refresh. Data-only, offline.

⚠️ CADENCE CHANGED 2026-08-20: quarterly -> weekly. The candidate pool the agent
selects stocks from was being rescreened four times a year on paper and, in
practice, never — the job was armed 2026-07-20 and had not fired once. The day
now lives in `[universe_maintenance] screen_day` and is enforced HERE by
`universe_maint.screen_due()`, not by a `date +%u` literal in the bash wrapper.

See docs/superpowers/specs/2026-07-19-universe-maintenance-design.md."""
import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import strategy  # noqa: E402
import universe_maint as um  # noqa: E402
from notify import push  # noqa: E402
# adapters.moomoo is imported lazily inside run(): the moomoo SDK is only
# installed under /usr/bin/python3 (not the .venv used for --selftest), so a
# top-level import here would break --selftest under the venv.

UNI = REPO / "config" / "universe.csv"
POOL = REPO / "config" / "pit_pool.csv"
PROP_DIR = REPO / "research_store" / "universe" / "proposals"
WATCH = REPO / "research_store" / "universe" / "seed_watch.json"


def _selftest() -> None:
    # rank_pond: descending by turnover, drops non-positive
    r = um.rank_pond({"A": 30.0, "B": 100.0, "C": 0.0, "D": None})
    assert r == ["B", "A"], r

    # propose_membership: seeds protected, fills banded, adds fill open slots
    params = {"target_size": 3, "keep_rank_max": 3, "add_rank_max": 3,
              "add_dvol_floor_usd": 10.0}
    current = [
        {"ticker": "SEED1", "source": "seed", "sector": "X", "exchange": "NASDAQ", "flag": "", "as_of": "2026-01-01"},
        {"ticker": "FILL_OK", "source": "fill_dvol", "sector": "", "exchange": "NYSE", "flag": "", "as_of": "2026-01-01"},
        {"ticker": "FILL_GONE", "source": "fill_dvol", "sector": "", "exchange": "NYSE", "flag": "", "as_of": "2026-01-01"},
    ]
    turn = {"SEED1": 5.0, "FILL_OK": 90.0, "NEW": 80.0, "FILL_GONE": 1.0}
    ranked = ["FILL_OK", "NEW", "SEED1", "FILL_GONE"]  # ranks 1,2,3,4
    p = um.propose_membership(ranked, turn, current, set(), params)
    assert "FILL_GONE" in p["drop_fills"], p        # rank 4 <= keep_max 4? no: strictly beyond band? see rule
    assert "NEW" in p["add"], p                      # rank 2, above floor, open slot
    assert "SEED1" in p["keep"], p                   # seed always kept despite rank 3 / low $vol
    assert len(p["result"]) == 3, p                  # target size respected
    print("universe_maint selftest OK: rank_pond + propose_membership")

    cp = {"target_size": 150, "auto_apply_max_changes": 5}
    # routine → AUTO_APPLY
    small = {"add": ["NEW"], "drop_fills": ["OLD"], "flagged_seeds": []}
    assert um.classify(small, 400, cp)["decision"] == "AUTO_APPLY"
    # too many changes → HOLD
    big = {"add": ["A", "B", "C", "D"], "drop_fills": ["E", "F", "G"], "flagged_seeds": []}
    assert um.classify(big, 400, cp)["decision"] == "HOLD"
    # flagged seed → HOLD
    seed = {"add": [], "drop_fills": [], "flagged_seeds": ["MU"]}
    assert um.classify(seed, 400, cp)["decision"] == "HOLD"
    # short pond (broken data) → HOLD
    assert um.classify(small, 100, cp)["decision"] == "HOLD"
    # non-common-stock add (leveraged/odd ticker) → HOLD
    bad = {"add": ["SOXL"], "drop_fills": [], "flagged_seeds": []}
    assert um.classify(bad, 400, cp)["decision"] == "HOLD"
    print("universe_maint selftest OK: classify")

    w = {}
    w = um.update_seed_watch(w, {"MU": 120, "AAPL": 5}, max_history=3)
    w = um.update_seed_watch(w, {"MU": 130, "AAPL": 4}, max_history=3)
    assert w["MU"] == [120, 130] and w["AAPL"] == [5, 4], w
    sp = {"stale_seed_rank_floor": 100, "stale_seed_weeks": 2}
    assert um.flag_stale_seeds(w, sp) == ["MU"], um.flag_stale_seeds(w, sp)  # MU bottom-third 2x; AAPL not
    # history cap
    w2 = um.update_seed_watch({"X": [1, 2, 3]}, {"X": 4}, max_history=3)
    assert w2["X"] == [2, 3, 4], w2
    print("universe_maint selftest OK: seed-watch")

    # ---- screen_due: the cadence is config, and it fails toward RUNNING ----
    from datetime import date as _date
    fri, thu = _date(2026, 8, 21), _date(2026, 8, 20)
    assert fri.weekday() == 4 and thu.weekday() == 3
    cfg_fri = {"universe_maintenance": {"screen_day": "friday"}}
    assert um.screen_due(cfg_fri, fri) and not um.screen_due(cfg_fri, thu)
    # a different day is honoured...
    cfg_thu = {"universe_maintenance": {"screen_day": "Thursday"}}   # case-insensitive
    assert um.screen_due(cfg_thu, thu) and not um.screen_due(cfg_thu, fri)
    # ...and an unreadable/absent one falls back to Friday rather than never
    # running, which is what quarterly-and-never-fired actually looked like.
    assert um.screen_due({"universe_maintenance": {"screen_day": "blursday"}}, fri)
    assert um.screen_due({}, fri) and not um.screen_due({}, thu)
    print("universe_maint selftest OK: screen_due (weekly, config-driven, fails toward running)")

    import tempfile, os
    rows = [{"ticker": "AAA", "source": "seed", "sector": "S", "exchange": "NYSE", "flag": "", "as_of": "old"},
            {"ticker": "BBB", "source": "screen", "sector": "", "exchange": "", "flag": "", "as_of": ""}]
    fd, tmp = tempfile.mkstemp(suffix=".csv"); os.close(fd)
    for r in rows:
        r["as_of"] = "2026-07-19"
    um.write_universe(tmp, rows)
    back = um.read_universe(tmp)
    assert [r["ticker"] for r in back] == ["AAA", "BBB"], back
    assert all(r["as_of"] == "2026-07-19" for r in back), back
    assert list(back[0].keys()) == um.FIELDS, back[0]
    os.remove(tmp)
    print("universe_maint selftest OK: csv round-trip + as_of stamp")


def _render_md(proposal, decision, asof) -> str:
    L = [f"# Universe proposal {asof}", "",
         f"**Decision:** {decision['decision']}"]
    if decision["reasons"]:
        L += ["", "**Held because:**"] + [f"- {r}" for r in decision["reasons"]]
    L += ["", f"**Adds ({len(proposal['add'])}):** " + (", ".join(proposal["add"]) or "none"),
          f"**Drops — fills ({len(proposal['drop_fills'])}):** " + (", ".join(proposal["drop_fills"]) or "none"),
          f"**Flagged seeds (your decision):** " + (", ".join(proposal["flagged_seeds"]) or "none")]
    return "\n".join(L)


def apply_proposal(proposal, asof, cfg) -> None:
    """Stamp as_of, write config/universe.csv, commit. Git = the undo."""
    rows = [dict(r) for r in proposal["result"]]
    for r in rows:
        r["as_of"] = asof
    um.write_universe(str(UNI), rows)
    subprocess.run(["git", "add", str(UNI)], cwd=str(REPO), check=True)
    msg = (f"chore(universe): refresh {asof} "
           f"(+{len(proposal['add'])} −{len(proposal['drop_fills'])})")
    subprocess.run(["git", "commit", "-m", msg], cwd=str(REPO), check=True)
    print(f"applied: universe.csv written + committed ({msg})")


def apply_from_file(asof, cfg) -> None:
    """Human-gated apply of a previously HELD proposal (via a Claude session)."""
    data = json.loads((PROP_DIR / f"{asof}.json").read_text())
    current = um.read_universe(str(UNI))
    by = {r["ticker"]: r for r in current}
    # rebuild result rows: kept incumbents (existing rows) + adds (fresh rows)
    result = [by[t] for t in data["keep"] if t in by]
    result += [{"ticker": t, "source": "screen", "sector": "", "exchange": "",
                "flag": "", "as_of": ""} for t in data["add"]]
    apply_proposal({"result": result, "add": data["add"], "drop_fills": data["drop_fills"]}, asof, cfg)
    # Clear the dashboard "pending review" banner: this held proposal is now applied,
    # so flip its stored status off HOLD (the panel only surfaces HOLD proposals).
    data["decision"]["decision"] = "APPLIED"
    data["applied_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (PROP_DIR / f"{asof}.json").write_text(json.dumps(data, indent=2))


def run(asof: str, dry: bool) -> dict:
    from adapters.moomoo import research as mm  # lazy: moomoo SDK is system-python-only

    cfg = strategy.load()
    params = cfg["universe_maintenance"]
    current = um.read_universe(str(UNI))
    incumbents = [r["ticker"] for r in current]
    watch = json.loads(WATCH.read_text()) if WATCH.exists() else {}
    seed_flags = um.flag_stale_seeds(watch, params)

    pond = mm.candidate_pond(incumbents, str(POOL), params)
    turnovers = mm.snapshot_turnover(pond)
    ranked = um.rank_pond(turnovers)
    proposal = um.propose_membership(ranked, turnovers, current, seed_flags, params)
    decision = um.classify(proposal, len(ranked), params)

    # ⛔ A DRY RUN WRITES TO A SEPARATE DIRECTORY. It used to write the proposal
    # into PROP_DIR before the `if dry` check below, which had two consequences:
    # the dashboard's "pending review" banner surfaced an off-cycle rehearsal as
    # a real HOLD (OPSLOG 2026-07-28 tells the operator to go delete the files by
    # hand), and -- since 2026-08-20 -- health.SPECS["universe_refresh"] watches
    # exactly this directory's mtime, so any dry run would have made a screen
    # that never fired report GREEN. A liveness check a rehearsal can satisfy is
    # not a liveness check. Found by the independent reviewer, 2026-08-20.
    out_dir = (PROP_DIR / "dry") if dry else PROP_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    slim = {k: proposal[k] for k in ("keep", "drop_fills", "add", "flagged_seeds")}
    (out_dir / f"{asof}.json").write_text(json.dumps(
        {"asof": asof, "decision": decision, "dry_run": bool(dry), **slim}, indent=2))
    md = _render_md(proposal, decision, asof)
    (out_dir / f"{asof}.md").write_text(md)
    print(md)

    if dry:
        print(f"\n[dry-run] proposal written to {out_dir} (NOT the live proposals "
              f"dir — it must not satisfy the health check or raise the "
              f"dashboard's review banner); no notification sent")
        return {"proposal": proposal, "decision": decision}

    if decision["decision"] == "AUTO_APPLY":
        apply_proposal(proposal, asof, cfg)  # Task 6
        push("Universe auto-refreshed",
             f"−{','.join(proposal['drop_fills']) or 'none'}  +{','.join(proposal['add']) or 'none'} (committed)",
             tags="recycle")
    else:
        push("Universe proposal needs review",
             f"HOLD: {'; '.join(decision['reasons'])}\nApprove in a Claude session.",
             tags="warning")
        try:
            import os
            import importlib.util
            to = os.environ.get("NEWSLETTER_TO")
            sender = os.environ.get("NEWSLETTER_FROM")
            api_key = os.environ.get("RESEND_API_KEY")
            if to and sender and api_key:
                spec = importlib.util.spec_from_file_location(
                    "send_newsletter", str(REPO / "scripts" / "send_newsletter.py"))
                sn = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(sn)
                html = "<pre>" + md.replace("<", "&lt;") + "</pre>"
                sn._send_resend(api_key, sender, to, f"Universe proposal {asof} — needs review", html)
                print("held proposal emailed via resend")
            else:
                print("proposal email skipped (RESEND_API_KEY/NEWSLETTER_TO/FROM not set)")
        except Exception as e:  # email is best-effort; the push + dashboard still cover it
            print(f"proposal email skipped: {e}")
    return {"proposal": proposal, "decision": decision}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--run", action="store_true", help="run the refresh (auto-apply or hold)")
    ap.add_argument("--force", action="store_true",
                    help="run even when today is not the configured screen_day")
    ap.add_argument("--dry-run", action="store_true", help="compute + write proposal, change nothing")
    ap.add_argument("--asof", default=None, help="YYYY-MM-DD stamp (default: today UTC)")
    ap.add_argument("--apply", metavar="ASOF", default=None,
                    help="apply a previously held proposal by its YYYY-MM-DD id")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        return

    if args.apply:
        apply_from_file(args.apply, strategy.load())
        return

    if args.run or args.dry_run:
        cfg = strategy.load()
        today = date.today()
        # The day gate applies to the SCHEDULED path only. A --dry-run is a
        # human looking, and --force is a human deciding; neither is the cron
        # job, and refusing them would just teach people to edit the config.
        if args.run and not args.force and not um.screen_due(cfg, today):
            want = (cfg.get("universe_maintenance") or {}).get("screen_day", "friday")
            print(f"not the screen day (today={today:%A}, screen_day={want}) — "
                  f"nothing done. Use --force to override, or --dry-run to look.")
            return
        asof = args.asof or datetime.now(timezone.utc).date().isoformat()
        run(asof, dry=args.dry_run)
        return


if __name__ == "__main__":
    main()
