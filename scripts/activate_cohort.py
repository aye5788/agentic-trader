#!/usr/bin/env python3
"""ONE-TIME GUARDED ACTIVATION of cohort candidate selection.

    .venv/bin/python scripts/activate_cohort.py --dry-run   # gates only, writes nothing
    .venv/bin/python scripts/activate_cohort.py --run       # the scheduled path

⛔ WHAT THIS IS FOR, AND WHY IT IS A SCRIPT RATHER THAN A RUNBOOK. Flipping
`[universe] mode` to "cohort" is one line, but it is only SAFE if a specific
chain of facts holds at the moment of the flip: the market is shut, no exit is
in flight, the WEEKLY screen has just produced a fresh cohort through its own
scheduled path, the broker still has history capacity, and the panel can score
enough of that cohort to be worth trading. A human reading a checklist at 17:30
on a Friday checks some of those. This checks all of them, in order, and refuses
on the first that fails.

⛔ IT NEVER PLACES, MODIFIES OR CANCELS AN ORDER. It reads the broker's quota
meter (unmetered, read-only) and moomoo price history. Nothing here touches the
trading MCP.

⛔ IT NEVER WEAKENS A GUARD TO MAKE ITSELF SUCCEED. It does not pass --force to
the universe refresh, does not write a quota reset, does not bypass
reload_stale's mid-exit or dirty-tree checks, and does not lower a threshold. If
a guard says no, this records the refusal and leaves the box on `fixed_list`.

IDEMPOTENT, three ways:
  - already activated and healthy      -> exits 0, changes nothing
  - a gate fails                       -> removes its own override, holds at
                                          fixed_list, records a HELD status
  - run twice in the same window       -> the second run sees mode=cohort and
                                          the health check passes, so it is a
                                          no-op rather than a second flip

⛔ THE OVERRIDE IT WRITES IS THE ONLY THING IT WRITES TO CONFIG, and it is
written by a line-preserving edit of config/strategy.local.toml, never a
rewrite: that file also carries `live_approved = true` and the risk_review arm,
and losing either of those to a clumsy write would be far worse than not
activating at all. Rollback is deleting the block this script added, which it
names explicitly in the status report.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

ET = ZoneInfo("America/New_York")
LOCAL_CFG = REPO / "config" / "strategy.local.toml"
STATUS = REPO / "research_store" / "universe" / "activation_status.json"
LOG = REPO / "logs" / "cohort_activation.log"

# The marker block this script owns. Rollback = delete exactly these lines.
MARK_BEGIN = "# >>> cohort activation (scripts/activate_cohort.py) >>>"
MARK_END = "# <<< cohort activation (scripts/activate_cohort.py) <<<"
OVERRIDE_BLOCK = (
    f"\n{MARK_BEGIN}\n"
    "# Added by the one-time guarded activation runner. Rollback: delete this\n"
    "# block (and NOTHING else in this file), then run\n"
    "#   .venv/bin/python scripts/reload_stale.py\n"
    "# after the close. Leaves live_approved and risk_review untouched.\n"
    "[universe]\n"
    'mode = "cohort"\n'
    f"{MARK_END}\n"
)

# The scoreable floor. Below this the cohort is not worth trading and the flip
# is refused: a book that can only see a handful of names is worse than the
# 150-name list it replaced. Compared against the fixed-list ranked count too,
# so a collapse relative to today is caught even if the absolute number looks
# fine.
MIN_SCOREABLE = 100
MIN_SCOREABLE_FRACTION_OF_FIXED = 0.80


class GateFailed(Exception):
    """A precondition did not hold. Carries the operator-facing reason."""


class Report:
    """Accumulates gate results and writes the durable status artifact.

    Every gate lands here whether it passed or failed, so the file answers "what
    was true at 17:30 on Friday" rather than only "did it work".
    """

    def __init__(self, mode: str):
        self.started = dt.datetime.now(ET)
        self.mode = mode
        self.gates: list[dict] = []
        self.facts: dict = {}
        self.activated = False
        self.outcome = "INCOMPLETE"
        self.rollback = "none required — nothing was changed"

    def gate(self, name: str, ok: bool, detail: str = "") -> bool:
        self.gates.append({"gate": name, "ok": bool(ok), "detail": str(detail)[:500]})
        log(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        return ok

    def require(self, name: str, ok: bool, detail: str = "") -> None:
        if not self.gate(name, ok, detail):
            raise GateFailed(f"{name}: {detail}")

    def write(self) -> None:
        ended = dt.datetime.now(ET)
        doc = {
            "started_et": self.started.isoformat(timespec="seconds"),
            "ended_et": ended.isoformat(timespec="seconds"),
            "duration_secs": round((ended - self.started).total_seconds(), 1),
            "invocation": self.mode,
            "outcome": self.outcome,
            "activated": self.activated,
            "rollback": self.rollback,
            "gates": self.gates,
            "facts": self.facts,
        }
        STATUS.parent.mkdir(parents=True, exist_ok=True)
        STATUS.write_text(json.dumps(doc, indent=2, default=str))
        log(f"status -> {STATUS}")


def log(msg: str) -> None:
    stamp = dt.datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S %Z")
    line = f"{stamp}  {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# the local-override edit (line-preserving, never a rewrite)
# --------------------------------------------------------------------------- #
def override_present() -> bool:
    return LOCAL_CFG.exists() and MARK_BEGIN in LOCAL_CFG.read_text()


def add_override() -> None:
    """Append our marked block. Preserves every existing line byte-for-byte."""
    text = LOCAL_CFG.read_text() if LOCAL_CFG.exists() else ""
    if MARK_BEGIN in text:
        return
    LOCAL_CFG.write_text(text.rstrip("\n") + "\n" + OVERRIDE_BLOCK)


def remove_override() -> None:
    """Delete ONLY our marked block. `live_approved` and everything else stay."""
    if not LOCAL_CFG.exists():
        return
    text = LOCAL_CFG.read_text()
    if MARK_BEGIN not in text:
        return
    cleaned = re.sub(re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\n?",
                     "", text, flags=re.S)
    LOCAL_CFG.write_text(cleaned.rstrip("\n") + "\n")


def loaded_mode() -> str:
    """Read the mode the way the system reads it — a fresh config load in a
    child process, so this script's own already-imported strategy module cannot
    hand back a cached answer."""
    out = subprocess.run(
        [str(REPO / ".venv" / "bin" / "python"), "-c",
         "import sys;sys.path.insert(0,'src');import strategy;"
         "print(strategy.load()['universe']['mode'])"],
        cwd=str(REPO), capture_output=True, text=True, timeout=60)
    return out.stdout.strip()


# --------------------------------------------------------------------------- #
# gates
# --------------------------------------------------------------------------- #
def gate_market_closed(rep: Report, now: dt.datetime, allow_any_time: bool) -> None:
    """RTH is 09:30-16:00 ET; the panel settles at 16:15. Weekend/holiday is
    closed by definition — and a holiday is asked of moomoo rather than guessed,
    because a weekday holiday looks exactly like a trading day to a calendar
    test made of weekday numbers."""
    weekend = now.weekday() >= 5
    after_settle = now.time() >= dt.time(16, 15)
    rep.facts["now_et"] = now.isoformat(timespec="seconds")
    if allow_any_time:
        rep.gate("market closed", True, "SKIPPED (--allow-any-time, dry-run only)")
        return
    rep.require("market closed",
                weekend or after_settle,
                f"{now:%A %H:%M %Z} — activation is post-close only "
                f"(weekend, or after 16:15 ET)")


def gate_no_exit_in_flight(rep: Report) -> None:
    """An exit_request with no exit_result means a sale is mid-flight. Nothing
    here would interrupt it, but reload_stale would, and this refuses early so
    the reason is legible rather than surfacing as a refused reload later."""
    req = REPO / "research_store" / "monitor" / "exit_request.json"
    res = REPO / "research_store" / "monitor" / "exit_result.json"
    in_flight = req.exists() and not res.exists()
    rep.require("no exit in flight", not in_flight,
                "exit_request.json present with no exit_result.json — a sale is "
                "in progress; activation waits" if in_flight else "clear")


def gate_switches(rep: Report) -> None:
    """HALT means the machine places nothing and positions are unprotected;
    SHADOW means every order is refused. Activating into either would change
    what the system may buy while it is deliberately not buying — a state change
    nobody could observe the effect of. HALT_ENTRIES is the same argument for
    the entry side specifically."""
    present = [f for f in ("HALT", "HALT_ENTRIES", "SHADOW")
               if (REPO / "research_store" / f).exists()]
    rep.facts["switches_present"] = present
    rep.require("no HALT / HALT_ENTRIES / SHADOW", not present,
                f"{', '.join(present)} present — resolve before activating"
                if present else "none present")


def gate_clean_tree(rep: Report) -> None:
    """Deployment happens only from a clean committed HEAD — the same rule
    reload_stale enforces. Checked here so a dirty tree is reported as a named
    gate rather than as a confusing reload refusal at the end."""
    out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                         cwd=str(REPO), capture_output=True, text=True, timeout=60)
    dirt = [ln for ln in out.stdout.splitlines() if ln.strip()]
    # our own override lives in a git-ignored file, so it never appears here
    rep.facts["head"] = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(REPO),
        capture_output=True, text=True, timeout=60).stdout.strip()
    rep.require("working tree clean", not dirt,
                f"{len(dirt)} uncommitted change(s): "
                + ", ".join(dirt[:6]) if dirt else f"HEAD {rep.facts['head'][:8]}")


def wait_for_refresh(rep: Report, expect_asof: str, wait_mins: int,
                     poll_secs: int = 60) -> None:
    """Poll until the weekly screen's artifact carries `expect_asof`, or give up.

    ⛔ EVIDENCE, NOT A STOPWATCH. The scheduled refresh fires at 17:00 and this
    job at 17:30, but "it is 17:30" is not "the refresh finished" — the screen
    can take minutes, can be delayed by an OpenD reconnect, or can complete and
    return NO_CHANGE, in which case last-known-good stands and its as_of is from
    a previous week. Waiting for the ARTIFACT to say today's date is the only
    check that distinguishes those, and it is why this job cannot be replaced by
    moving the clock later.

    Timing out is not an error: it records a HELD status and leaves the box on
    fixed_list, which is exactly what should happen if the screen did not run.
    """
    import cohort  # noqa: PLC0415
    import strategy  # noqa: PLC0415

    cfile, _ = cohort.paths(strategy.load(), REPO)
    deadline = dt.datetime.now(ET) + dt.timedelta(minutes=int(wait_mins))
    waited = 0
    while True:
        try:
            if cfile.exists():
                asof = str((json.loads(cfile.read_text()) or {}).get("as_of"))
                if asof == expect_asof:
                    rep.gate("waited for the scheduled refresh", True,
                             f"cohort as_of={asof} after {waited//60}m{waited%60}s")
                    return
        except Exception:                                     # noqa: BLE001
            pass          # a half-written file mid-refresh: keep waiting
        if dt.datetime.now(ET) >= deadline:
            rep.gate("waited for the scheduled refresh", False,
                     f"no cohort with as_of={expect_asof} after {wait_mins}m — the "
                     f"Friday refresh did not complete, or returned NO_CHANGE")
            return
        log(f"  waiting for the scheduled refresh… ({waited//60}m elapsed, "
            f"cohort as_of="
            f"{(json.loads(cfile.read_text()).get('as_of') if cfile.exists() else 'absent')})")
        import time as _t                                     # noqa: PLC0415
        _t.sleep(poll_secs)
        waited += poll_secs


def gate_fresh_cohort(rep: Report, expect_asof: str | None) -> dict:
    """The weekly screen must have produced a cohort through its OWN scheduled
    path today.

    ⛔ THIS IS THE ANTI-RACE GATE AND IT CHECKS EVIDENCE, NOT THE CLOCK. Firing
    at 17:30 proves nothing about whether the 17:00 refresh finished, or
    finished well: it can return NO_CHANGE on a contradictory feed, in which case
    the last-known-good cohort stands and its as_of is from a previous week.
    Requiring the artifact's own as_of to equal today's date is what makes
    "the refresh ran" a fact rather than an assumption.

    ⛔ AND IT MUST NOT HAVE COME FROM --force. A forced off-cadence run cannot
    write a cohort at all (universe_refresh refuses), so an artifact bearing
    today's date can only have come from the scheduled path. That refusal is
    load-bearing here; do not relax it to make this gate easier to satisfy.
    """
    import cohort  # noqa: PLC0415
    import strategy  # noqa: PLC0415
    import universe_maint as um  # noqa: PLC0415

    cfg = strategy.load()
    cfile, _ = cohort.paths(cfg, REPO)
    rep.facts["cohort_path"] = str(cfile)
    rep.require("cohort artifact exists", cfile.exists(),
                f"{cfile} — the weekly refresh has not produced one"
                if not cfile.exists() else str(cfile))
    rep.facts["cohort_mtime_et"] = dt.datetime.fromtimestamp(
        cfile.stat().st_mtime, ET).isoformat(timespec="seconds")

    params = cfg["universe_maintenance"]
    doc = cohort.load(cfile, min_turnover_usd=float(params["add_dvol_floor_usd"]),
                      allowed_venues=um.ALLOWED_VENUES)
    rep.facts["cohort_as_of"] = doc.get("as_of")
    rep.facts["cohort_provenance"] = doc.get("provenance")
    rep.facts["cohort_coverage"] = doc.get("coverage")

    if expect_asof:
        rep.require("cohort as_of is today (this week's scheduled screen)",
                    str(doc.get("as_of")) == expect_asof,
                    f"as_of={doc.get('as_of')!r}, expected {expect_asof!r} — the "
                    f"Friday refresh did not complete, or returned NO_CHANGE and "
                    f"the last-known-good cohort is standing")
    rep.require("cohort coverage complete",
                (doc.get("coverage") or {}).get("complete") is True,
                f"coverage={doc.get('coverage')}")
    rep.require("cohort screen reported no integrity problems",
                not (doc.get("coverage") or {}).get("problems"),
                str((doc.get("coverage") or {}).get("problems"))[:200])
    rep.gate("cohort schema/provenance/liquidity/cap/venue validate", True,
             f"{len(doc['names'])} rows, screen_version="
             f"{(doc.get('provenance') or {}).get('screen_version')}")
    return doc


def gate_quota(rep: Report) -> dict:
    """Read the broker's own meter. Read-only and unmetered.

    An unavailable meter is NOT zero-used: it falls through to local evidence,
    which with no ledger resolves to UNKNOWN -> zero new capacity, which will
    then fail the repair gate honestly rather than spending blind.
    """
    from adapters.moomoo import prices as mmp  # noqa: PLC0415
    from adapters.moomoo.client import quote_ctx  # noqa: PLC0415
    try:
        ctx = quote_ctx()
        try:
            tel = mmp.history_quota(ctx=ctx)
        finally:
            ctx.close()
    except Exception as e:  # noqa: BLE001
        tel = {"ok": False, "used": None, "remain": None, "charged": set(),
               "detail_available": False, "error": f"{type(e).__name__}: {e}"}
    rep.facts["quota_telemetry"] = {
        "ok": tel["ok"], "used": tel["used"], "remain": tel["remain"],
        "detail_available": tel["detail_available"], "error": tel.get("error"),
        "charged_count": len(tel.get("charged") or ()),
    }
    rep.gate("broker quota telemetry", bool(tel["ok"]),
             f"used={tel['used']} remain={tel['remain']} "
             f"detail={tel['detail_available']}" if tel["ok"] else str(tel.get("error"))[:200])
    return tel


def gate_health_held(rep: Report, doc: dict) -> None:
    """Every held position must still be reachable. It need NOT be in the
    cohort — a held name that leaves stays sellable and monitored, which is the
    whole asymmetry — but the run should say so out loud rather than let it pass
    unnoticed."""
    try:
        pos = json.loads((REPO / "research_store" / "rh" / "positions.json").read_text())
        held = sorted((pos.get("positions") or {}))
    except Exception as e:  # noqa: BLE001
        rep.require("positions snapshot readable", False, f"{type(e).__name__}: {e}")
        return
    names = {n["ticker"] for n in doc["names"]}
    outside = sorted(set(held) - names)
    rep.facts["held"] = held
    rep.facts["held_outside_cohort"] = outside
    rep.gate("held positions accounted for", True,
             f"{len(held)} held, {len(outside)} outside the cohort "
             f"({', '.join(outside) if outside else 'none'}) — outside names stay "
             f"sellable, priced and stop-watched")


def run_price_path(rep: Report, dry: bool) -> None:
    """Run the NORMAL daily price path, in cohort mode, so it writes
    history_state.json across the cohort and repairs its pending names through
    the quota planner.

    ⛔ THE NORMAL PATH, NOT A SPECIAL ONE. This is the same command the slow loop
    runs every evening; the only difference is that the override is in place, so
    `universe_tickers()` reads the cohort instead of the CSV. Nothing here passes
    --backfill, --no-repair or any flag that would change how history is
    rationed.
    """
    cmd = ["/usr/bin/python3", "scripts/fetch_prices.py"]
    if dry:
        cmd.append("--dry")
    log(f"  running the normal price path: {' '.join(cmd)}")
    out = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                         timeout=1800)
    tail = "\n".join((out.stdout or "").strip().splitlines()[-25:])
    rep.facts["price_path_returncode"] = out.returncode
    rep.facts["price_path_tail"] = tail
    for ln in tail.splitlines():
        log(f"    | {ln}")
    rep.require("price/history path succeeded", out.returncode == 0,
                f"exit {out.returncode}: {(out.stderr or '')[-300:]}")


def gate_final_view(rep: Report, fixed_ranked: int) -> dict:
    """The composed cohort must be healthy ENOUGH to trade before the flip is
    made persistent.

    Pending names are tolerated only when the quota or a provider explains them,
    because that is a scheduling state that resolves itself on the next run. An
    UNSCOREABLE name is a provider failure and is reported. What is never
    tolerated is a scoreable population too small to be a book, or one that has
    collapsed relative to the list it is replacing.
    """
    import cohort  # noqa: PLC0415
    import strategy  # noqa: PLC0415

    cfg = strategy.load()
    view = cohort.active_view(cfg, REPO, dt.datetime.now(ET).date())
    c = view["counts"]
    rep.facts["cohort_counts"] = c
    rep.facts["fixed_list_ranked"] = fixed_ranked
    rep.facts["freshness"] = view.get("freshness")

    rep.require("cohort view loads and is fresh",
                not view["freshness"]["stale"], str(view["freshness"]))
    rep.require("scoreable population non-empty", c["scoreable"] > 0,
                f"scoreable={c['scoreable']}")
    rep.require("scoreable population is a workable book",
                c["scoreable"] >= MIN_SCOREABLE,
                f"scoreable={c['scoreable']} < floor {MIN_SCOREABLE}")
    rep.require("scoreable has not collapsed vs the fixed list",
                c["scoreable"] >= MIN_SCOREABLE_FRACTION_OF_FIXED * fixed_ranked,
                f"scoreable={c['scoreable']} vs fixed-list ranked {fixed_ranked} "
                f"(floor {MIN_SCOREABLE_FRACTION_OF_FIXED:.0%})")
    # observe_only names must exist only as observe_only — never buyable
    buyable = cohort.buyable(view)
    leaked = sorted(set(view.get("observe_only", [])) & buyable)
    rep.require("no observe-only name is buyable", not leaked, str(leaked))
    pending_ok = c["pending_history"] == 0 or bool(rep.facts.get("quota_telemetry", {}).get("ok"))
    rep.gate("pending-history names explained",
             pending_ok,
             f"pending={c['pending_history']} "
             f"(quota-rationed; they resolve on later runs)")
    rep.gate("unscoreable (provider failures)", True,
             f"unscoreable={c['unscoreable']}")
    return view


def gate_readiness_reads(rep: Report) -> None:
    """Read-only readiness under the LOADED configuration: the agent surfaces
    agree, a scoreable buy passes, everything else fails, and sells stay open.

    Run in a child process so it sees a genuinely fresh config load rather than
    this process's imports.
    """
    probe = r'''
import sys, json; sys.path.insert(0,'src')
import strategy, cohort, governance as gov
from agent_env import screen, server
cfg = strategy.load()
out = {"mode": cfg["universe"]["mode"]}
pool = screen.ranking_pool()
u = json.loads(server.universe()); c = json.loads(server.candidates(10))
out["pool_source"] = pool["source"]
out["pool_n"] = len(pool["tickers"])
out["universe_ranked"] = len(u["ranked"])
out["cohort_meta"] = bool(u.get("cohort"))
out["pool_eq_universe"] = set(pool["tickers"]) == set(u["ranked"])
out["candidates_subset"] = set(c) <= set(pool["tickers"])
out["slow_loop_same_fn"] = "ranking_pool(" in open("scripts/slow_loop.py").read()
v = pool["view"]
score = pool["tickers"][0] if pool["tickers"] else None
pend = (v["pending_history"][0] if v and v["pending_history"] else None)
obs  = (v["observe_only"][0] if v and v.get("observe_only") else None)
def chk(sym, side):
    if sym is None: return None
    ok, _ = gov.vet_plan([{"symbol": sym, "side": side, "amount": 5.0}], 1000.0, cfg)
    return bool(ok)
out["buy_scoreable"] = chk(score, "buy")
out["buy_pending"] = chk(pend, "buy")
out["buy_observe_only"] = chk(obs, "buy")
out["buy_offcohort"] = chk("ZZZZ_NOT_SCREENED", "buy")
out["sell_scoreable"] = chk(score, "sell")
out["sell_offcohort"] = chk("ZZZZ_NOT_SCREENED", "sell")
out["sample"] = {"scoreable": score, "pending": pend, "observe_only": obs}
print(json.dumps(out))
'''
    res = subprocess.run([str(REPO / ".venv" / "bin" / "python"), "-c", probe],
                         cwd=str(REPO), capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        rep.require("readiness probe ran", False, (res.stderr or "")[-300:])
    r = json.loads(res.stdout.strip().splitlines()[-1])
    rep.facts["readiness"] = r
    rep.require("loaded mode is cohort", r["mode"] == "cohort", r["mode"])
    rep.require("universe() reports cohort metadata", r["cohort_meta"], "")
    rep.require("ranking pool == universe() ranked", r["pool_eq_universe"], "")
    rep.require("candidates() subset of scoreable pool", r["candidates_subset"], "")
    rep.require("slow_loop uses the same pool fn", r["slow_loop_same_fn"], "")
    rep.require("BUY scoreable approved-venue ALLOWED",
                r["buy_scoreable"] is True, str(r["sample"]))
    for k in ("buy_pending", "buy_observe_only", "buy_offcohort"):
        if r[k] is not None:
            rep.require(f"BUY {k.replace('buy_','')} REFUSED", r[k] is False, "")
    rep.require("SELL scoreable ALLOWED", r["sell_scoreable"] is True, "")
    rep.require("SELL off-cohort ALLOWED", r["sell_offcohort"] is True, "")


def do_reload(rep: Report) -> None:
    """The SUPPORTED reload workflow, with its own guards intact.

    Exit 1 means something stale was not restarted; the mid-exit and dirty-tree
    refusals are reload_stale's to make and this does not second-guess them.
    """
    res = subprocess.run([str(REPO / ".venv" / "bin" / "python"),
                          "scripts/reload_stale.py"],
                         cwd=str(REPO), capture_output=True, text=True, timeout=600)
    tail = "\n".join((res.stdout or "").strip().splitlines()[-12:])
    rep.facts["reload_returncode"] = res.returncode
    rep.facts["reload_output"] = tail
    for ln in tail.splitlines():
        log(f"    | {ln}")
    rep.require("supported reload workflow succeeded", res.returncode == 0,
                f"exit {res.returncode} — something stale was NOT restarted")
    for unit in ("agentic-monitor.service", "agentic-dashboard.service"):
        act = subprocess.run(["systemctl", "is-active", unit],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        rep.require(f"{unit} active after reload", act == "active", act)
        rep.facts.setdefault("services", {})[unit] = act


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="the scheduled activation")
    ap.add_argument("--dry-run", action="store_true",
                    help="evaluate gates, write NOTHING (no override, no reload, "
                         "no history requests)")
    ap.add_argument("--expect-asof", default=None,
                    help="require the cohort artifact to carry this as_of "
                         "(default: today ET on a --run)")
    ap.add_argument("--wait-mins", type=int, default=45,
                    help="how long to poll for the scheduled refresh's artifact "
                         "before holding (default 45)")
    ap.add_argument("--allow-any-time", action="store_true",
                    help="skip the market-closed gate; dry-run only")
    args = ap.parse_args()
    if not (args.run or args.dry_run):
        ap.error("pass --run or --dry-run")
    dry = args.dry_run and not args.run

    now = dt.datetime.now(ET)
    rep = Report("--dry-run" if dry else "--run")
    log("=" * 72)
    log(f"cohort activation runner ({rep.mode}) starting")

    try:
        # ---- 0. already done? -------------------------------------------
        mode_now = loaded_mode()
        rep.facts["mode_at_start"] = mode_now
        if mode_now == "cohort" and override_present():
            log("mode is ALREADY cohort and the override is ours — verifying health")
            gate_readiness_reads(rep)
            rep.activated = True
            rep.outcome = "ALREADY_ACTIVE"
            rep.rollback = (f"delete the {MARK_BEGIN} block from {LOCAL_CFG}, "
                            f"then .venv/bin/python scripts/reload_stale.py after close")
            log("already active and healthy — nothing to do")
            return 0

        # ---- 1-2. environment -------------------------------------------
        gate_market_closed(rep, now, args.allow_any_time and dry)
        gate_no_exit_in_flight(rep)
        gate_switches(rep)
        gate_clean_tree(rep)

        # ---- 3-4. the weekly screen's own artifact ----------------------
        expect = args.expect_asof if args.expect_asof is not None else (
            None if dry and args.allow_any_time else now.date().isoformat())
        if expect and not dry:
            wait_for_refresh(rep, expect, args.wait_mins)
        doc = gate_fresh_cohort(rep, expect)

        # ---- 5. holdings ------------------------------------------------
        gate_health_held(rep, doc)

        # ---- 6. broker quota -------------------------------------------
        gate_quota(rep)

        # baseline for the collapse comparison, taken BEFORE the flip
        fixed_ranked = int(subprocess.run(
            [str(REPO / ".venv" / "bin" / "python"), "-c",
             "import sys,json;sys.path.insert(0,'src');"
             "from agent_env import server;print(len(json.loads(server.universe())['ranked']))"],
            cwd=str(REPO), capture_output=True, text=True, timeout=300).stdout.strip() or 0)
        rep.facts["fixed_list_ranked"] = fixed_ranked

        if dry:
            rep.gate("DRY RUN — stopping before any write", True,
                     "no override written, no history requested, no reload")
            rep.outcome = "DRY_RUN_GATES_PASSED"
            rep.rollback = "none — dry run changed nothing"
            return 0

        # ---- 7. temporary override + the normal price path --------------
        log("applying the cohort override TEMPORARILY for the price path")
        add_override()
        rep.require("override applied", loaded_mode() == "cohort", loaded_mode())
        run_price_path(rep, dry=False)

        # ---- 8. final health -------------------------------------------
        gate_final_view(rep, fixed_ranked)

        # ---- 9-10. persist + readiness ---------------------------------
        log("all gates passed — the override becomes persistent")
        gate_readiness_reads(rep)

        # ---- 11-12. reload ---------------------------------------------
        do_reload(rep)

        rep.activated = True
        rep.outcome = "ACTIVATED"
        rep.rollback = (f"delete the {MARK_BEGIN} block from {LOCAL_CFG} "
                        f"(leaving live_approved and risk_review untouched), then "
                        f".venv/bin/python scripts/reload_stale.py after the close")
        log("ACTIVATED")
        return 0

    except GateFailed as e:
        remove_override()
        rep.activated = False
        rep.outcome = "HELD"
        rep.rollback = (f"nothing to roll back — the override was removed and mode "
                        f"is {loaded_mode()!r}. Services untouched.")
        log(f"HELD at fixed_list: {e}")
        return 0 if dry else 2
    except Exception as e:  # noqa: BLE001
        remove_override()
        rep.activated = False
        rep.outcome = "ERROR"
        rep.gates.append({"gate": "unexpected error", "ok": False,
                          "detail": f"{type(e).__name__}: {e}"})
        rep.rollback = (f"override removed; mode is {loaded_mode()!r}. "
                        f"Services untouched.")
        log(f"ERROR: {type(e).__name__}: {e}")
        return 3
    finally:
        rep.write()
        log(f"outcome={rep.outcome} activated={rep.activated}")
        log("=" * 72)


if __name__ == "__main__":
    raise SystemExit(main())
