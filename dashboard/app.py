"""agentic-trader dashboard — a tiny Flask app that renders the live account
monitor from the Research Store files (no extra broker calls). Meant to sit
behind a Cloudflare Tunnel + Access (auth-gated), see docs/DEPLOY.md.

    .venv/bin/python dashboard/app.py           # serve on 127.0.0.1:8787
"""
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, request

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import health                                         # noqa: E402
import governance as gov                              # noqa: E402
import mandate                                        # noqa: E402
import marks                                          # noqa: E402
import strategy as strat                              # noqa: E402
from agent_env import memory                          # noqa: E402
from agent_env import state                           # noqa: E402
from research_store import read_current               # noqa: E402
from research_store.validate import reward_risk       # noqa: E402

try:                                                  # load DASH_USER/DASH_PASS from .env
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
except Exception:
    pass

RS = REPO / "research_store"
TEMPLATE = (Path(__file__).parent / "dashboard.html")
DASH_USER = os.environ.get("DASH_USER", "admin")
DASH_PASS = os.environ.get("DASH_PASS")               # unset -> serve nothing (fail closed)
app = Flask(__name__)


@app.before_request
def _require_auth():
    """Password-gate the whole dashboard. Fails CLOSED: with no DASH_PASS set it
    serves nothing, so it can never accidentally be public."""
    if not DASH_PASS:
        return Response("Dashboard password not set — add DASH_USER/DASH_PASS to "
                        ".env and restart.", 503)
    a = request.authorization
    ok = a and a.username == DASH_USER and hmac.compare_digest(a.password or "", DASH_PASS)
    if not ok:
        return Response("Authentication required.", 401,
                        {"WWW-Authenticate": 'Basic realm="agentic-trader"'})


def _read_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _tail_jsonl(path, n):
    if not path.exists():
        return []
    lines = [l for l in path.read_text().splitlines() if l.strip()]
    out = []
    for l in lines[-n:]:
        try:
            out.append(json.loads(l))
        except Exception:
            pass
    return out


def _pending_universe_proposal():
    """Latest universe screen that CHANGED NOTHING, if any (read-only surface).

    Was "latest HOLD proposal". HOLD no longer exists: the screen applies or it
    reports a data-integrity failure as NO_CHANGE (universe_maint.classify,
    2026-08-27). Left matching only "HOLD", this panel would have been dead code
    that silently never fired -- so it now surfaces the outcome that can occur.
    NO_CHANGE needs no human action; it self-clears when the feed recovers. It is
    shown so a run of empty weeks is visible rather than silent.
    """
    pd = RS / "universe" / "proposals"
    if not pd.exists():
        return None
    files = sorted(pd.glob("*.json"))
    if not files:
        return None
    d = _read_json(files[-1], {})
    if d.get("decision", {}).get("decision") in ("NO_CHANGE", "HOLD"):
        return d
    return None


def _health_rows() -> list[dict]:
    """Scheduled-job liveness for the header strip.

    use_network=False deliberately: the GitHub Actions probe shells out to `gh`,
    and a page render must never block on a network call. The daily
    health_check.py run does the networked version and is what actually alerts —
    here the adaptive-tune row simply reads as unknown rather than stalling the
    dashboard. Never raises: a broken health check must not take the page down.
    """
    try:
        rows = health.checks(use_network=False)
    except Exception as e:                            # noqa: BLE001 — page > check
        return [{"key": "health", "label": "Health check", "status": "stale",
                 "detail": f"could not evaluate: {type(e).__name__}"}]
    return [{"key": c.key, "label": c.label, "status": c.status,
             "detail": c.detail, "healthy": c.healthy} for c in rows]


def build_data() -> dict:
    cfg = strat.load()
    prod = read_current()
    valued = marks.load() or {"positions": {}, "invested": 0.0, "cash": 0.0,
                              "account_value": 0.0, "marked_at": None}
    acct_value = valued["account_value"]
    invested = valued["invested"]

    # ⛔ THE ROWS ARE WHAT WE HOLD, AND THE LEVELS ARE WHAT THE MONITOR ENFORCES.
    # Until 2026-08-31 this walked prod.theses and skipped `target_weight <= 0`,
    # which dropped every PROTECTIVE-ONLY thesis (rank >= 200) — FCX, KO and MRK
    # were held and absent from the page, $14.40 of $59.20 invested, a quarter of
    # the book invisible on the one screen that exists to show it. It also printed
    # `t.stop`, the slow loop's geometry, while the agent's set_levels overrides
    # were what would actually fire.
    #
    # state.holdings() is the ONE merge of positions and levels — the same
    # function the agent's own positions() tool reads — so this page cannot
    # diverge from what the agent and the monitor see. Its `stop`/`targets` are
    # the ENFORCED values, with the book's kept alongside as `book_stop`.
    #
    # Arguments mirror agent_env/server.py:210, including the monitor's OWN
    # quotes.json rather than the position mark: marks fall back to snapshot
    # price or even cost basis (src/marks.py), and resolving an override against
    # cost basis is exactly how display and enforcement came apart before.
    # ⛔ WHAT THE ORDER GATE WILL ACTUALLY REFUSE. memory.binding_rule_outs() is
    # the function the PreToolUse hook calls (pretooluse_order_gate.py:454), so
    # this reports the gate's own answer rather than a second reading of the
    # file: only status "ruled_out", latest entry per symbol, unexpired.
    #
    # WHY THIS IS ON THE PAGE AT ALL. Rule-outs are binding BUY exclusions that
    # a session writes and every later session inherits as fact. In August they
    # silently accumulated into a theme cap that exists nowhere in the mandate —
    # six of the top fourteen ranked names became unbuyable, cash ran from 3% to
    # 29% of NAV, and no human ever decided any of it (CLAUDE.md, 2026-08-21).
    # They were invisible here throughout. A standing exclusion nobody can see
    # is how that happens again.
    try:
        _ruled = memory.binding_rule_outs(RS)
    except Exception:                                 # noqa: BLE001 — page > panel
        _ruled = {}
    ov = _read_json(RS / "monitor" / "overrides.json", {})
    mprices = (_read_json(RS / "monitor" / "quotes.json", {}) or {}).get("prices") or {}
    theses = prod.theses if prod else []
    by_sym = {t.symbol: t for t in theses}
    enforced = state.holdings(
        valued, theses, ov, mprices,
        float((cfg.get("governance") or {}).get("min_fractional_order_usd", 1.0)))

    def _row(sym, t, h, held):
        # R:R stays THESIS geometry (entry-zone mid), unchanged: it describes the
        # setup the ranking proposed, not the position. Re-basing it on avg_cost
        # would silently change what the column means.
        rr = reward_risk(t) if t is not None else None
        return {
            "ticker": sym,
            # A held row carries its ACTUAL share of NAV. A protective thesis has
            # target_weight 0.0 and would otherwise render "0.00%" beside a real
            # position. A pending row has no actual weight, so it shows the TARGET
            # and is flagged, so the two are never read as the same number.
            "weight": (h.get("weight_pct_nav") if held else round(t.target_weight, 4)),
            "weight_is_target": not held,
            "value": round((h.get("value") or 0.0) if held else 0.0, 2),
            "qty": h.get("qty") if held else None,
            "avg_cost": h.get("avg_cost") if held else None,
            "last": h.get("mark") if held else None,
            "pnl": h.get("pnl") if held else None,
            "entry": (round(sum(t.entry_zone) / 2, 2)
                      if t is not None and t.entry_zone else None),
            # enforced; book_stop is set only when an override is in force, so a
            # divergence between what fires and what the loop proposed is visible
            "stop": h.get("stop") if held else (t.stop if t is not None else None),
            "targets": (list(h.get("targets") or []) if held
                        else list((t.targets if t is not None else []) or [])),
            "book_stop": h.get("book_stop") if held else None,
            "set_by_agent": bool(h.get("set_by_agent")) if held else False,
            # False = the monitor is NOT watching this position. It must be
            # visible: an unprotected holding is the one thing this page exists
            # to surface. None for a pending row — nothing is held to watch.
            "watched": bool(h.get("watched")) if held else None,
            "rr": round(rr, 2) if rr else None,
            "score": (t.signals.get("score") if t is not None else None),
            "state": "held" if held else "pending",
            # a PENDING row that is ruled out is not "not bought yet" — it is
            # "cannot be bought", and the two looked identical here. AMAT and
            # WDC sat in this table as pending, with no reason given, while the
            # gate refused every buy on them.
            "ruled_out": bool(_ruled.get(sym)),
            "ruled_out_reason": (_ruled.get(sym) or {}).get("reason"),
            "thesis": (t.thesis if t is not None else None),
        }

    holdings = [_row(s, by_sym.get(s), h, True) for s, h in enforced.items()]
    # book names selected but not yet held: no position, so no enforced level —
    # their stop/targets are the book's by nature, not by omission.
    holdings += [_row(t.symbol, t, {}, False) for t in theses
                 if t.target_weight > 0 and t.symbol not in enforced]
    # Preserve the page's existing order (book slot). A held name with no thesis
    # sorts last rather than raising — that is the unprotected case, and it must
    # still render.
    holdings.sort(key=lambda r: (by_sym[r["ticker"]].rank
                                 if r["ticker"] in by_sym else 9_999))

    # ⛔ THE ORDER-PLAN HALF IS GONE (2026-08-31). research_store/rh/order_plan.json
    # was written by scripts/fast_loop.py, DELETED 2026-08-14 with the procedural
    # executor. Nothing has written it since; the file on disk is frozen at
    # 2026-08-14 and the card rendered it as current for seventeen days -- most
    # recently "BLOCKED LITE", a rule-out lifted long ago, directly above a
    # holdings table showing LITE held. A panel that contradicts the live table
    # beside it is worse than an absent one: a reader has to know which to
    # distrust. There is no replacement artifact because sessions now REASON
    # rather than emit a plan; rebuilding this from agent_decision events would
    # be a different card, not a repair, and is a decision for the principal.
    #
    # What survives is the half that was never stale: the last execution, read
    # from the journal.
    recent = _tail_jsonl(RS / "journal.jsonl", 60)
    last_exec = next((e for e in reversed(recent) if e.get("event") == "execution"), None)

    equity = _tail_jsonl(RS / "history" / "equity.jsonl", 400)
    # ⛔ TWO DIFFERENT DRAWDOWNS EXIST, AND THIS CARD USED TO MIX THEM.
    # Until 2026-08-31 it measured against max(equity.jsonl) — correct, and for
    # the right reason (FIX A, 2026-08-10: governance's persisted peak_value is
    # sampled at an arbitrary moment and had already drifted, 82.22 vs 81.99) —
    # and then rendered that number against `[governance] max_drawdown`, which
    # belongs to the OTHER measure and its OTHER peak. It read "-13.1% / 25%",
    # implying the entries-halt was half-consumed, while the gate that actually
    # halts entries read -1.7%. Numerator from one criterion, denominator from
    # another.
    #
    #   MANDATE drawdown   peak = max(equity.jsonl), limit [drawdown] max_pct
    #                      (0.20). BLOCKING — this is the one that can flatten
    #                      the book. Close-to-close only.
    #   GOVERNANCE halt    peak = governance/state.json, limit [governance]
    #                      max_drawdown (0.25). Blocks NEW ENTRIES only.
    #                      config/mandate.toml:53 states the difference outright.
    #
    # Both are now shown, each against its own limit.
    #
    # Read through mandate.drawdown() rather than recomputed here: the previous
    # inline version also measured INTRADAY, dividing live marks by the peak,
    # which that function's docstring forbids for this criterion ("an intraday
    # measure fires the flatten on noise"). It is close-to-close by definition.
    # The FULL series is read, not the 400-point chart tail — the peak is an
    # all-time high-water mark and a tail would silently understate it once the
    # log outgrows the window.
    _eq_all = _tail_jsonl(RS / "history" / "equity.jsonl", 10_000_000)
    try:
        dd_m = mandate.drawdown([e.get("value") for e in _eq_all],
                                mandate.load()["drawdown"]["max_pct"])
    except Exception as e:                            # noqa: BLE001 — page > check
        dd_m = {"value": None, "limit": None, "state": "INSUFFICIENT_DATA",
                "reason": f"could not evaluate: {type(e).__name__}"}
    # ⛔ drawdown_breach, NEVER gates()/drawdown_halt(): those call update_peak(),
    # which WRITES. A page that ratchets a live gate every time it is rendered is
    # the same defect the PreToolUse hook was built to avoid. breach() is also
    # the variant the hook itself judges on, so this shows what the authority
    # will decide, not an approximation of it.
    try:
        _gov_breached, _gov_dd = gov.drawdown_breach(acct_value, cfg)
    except Exception:                                 # noqa: BLE001
        _gov_breached, _gov_dd = None, None
    _acct_no = _read_json(RS / "rh" / "positions.json", {}).get("account_number")
    cooldown = _read_json(RS / "monitor" / "cooldown.json", {})
    mstate = _read_json(RS / "monitor" / "state.json", {})

    g = cfg["governance"]
    regime = (prod.regime if prod and prod.regime else {"status": "unknown"})
    return {
        # ⛔ DERIVED, NOT HARDCODED. This read "••••4924" as a literal. It
        # happened to be right, which is the problem: a pinned account number
        # that silently stops matching the account being traded is exactly the
        # failure _expected_account() exists to catch, and a hardcoded display
        # cannot participate in that. Falls back to the literal only when the
        # snapshot carries no account_number at all.
        "account": {"nickname": "Agentic",
                    "masked": ("••••" + str(_acct_no)[-4:]) if _acct_no else "••••????",
                    "buying_power": valued.get("buying_power"),
                    "value": round(acct_value, 2), "invested": round(invested, 2),
                    "cash": valued["cash"], "marked_at": valued["marked_at"]},
        "asof": prod.as_of if prod else None,
        # ⛔ EVERY STRATEGY LABEL ON THE PAGE COMES FROM HERE. The template used
        # to hardcode "70 / 30 book & sleeve · weekly rebalance" (the sleeve was
        # deleted 2026-08-20 and there is no 70/30 split), "Robinhood cash" (the
        # account became limited_margin on 2026-08-18) and "dual-momentum-v1".
        # Static copy describing a live system is a defect with a delay on it;
        # config/strategy.toml is the single source of truth, so read it.
        "strategy": {
            "name": (cfg.get("meta") or {}).get("name"),
            "edge": (cfg.get("meta") or {}).get("edge"),
            "instrument": (cfg.get("meta") or {}).get("instrument"),
            "max_holdings": (cfg.get("risk") or {}).get("max_holdings"),
            "rebalance": (cfg.get("portfolio") or {}).get("rebalance"),
        },
        "regime": regime,
        "flags": {"live_approved": bool(cfg.get("proof", {}).get("live_approved")),
                  "kill_switch": (RS / "HALT").exists(),
                  # ⛔ research_store/SHADOW makes the order gate refuse EVERY
                  # order (pretooluse_order_gate.py:202) while the loop keeps
                  # running — the phased-rollout switch. Unsurfaced, the page
                  # would read LIVE / kill-switch CLEAR while nothing could be
                  # placed at all. Same class as the HALT_ENTRIES gap closed in
                  # cf6329c.
                  "shadow": (RS / "SHADOW").exists(),
                  # a forgotten HALT_ENTRIES silently stops the book growing —
                  # it has to be visible somewhere standing, not just in a log
                  "halt_entries": (RS / "HALT_ENTRIES").exists(),
                  "monitor_book": mstate.get("book_asof")},
        "holdings": holdings,
        "last_execution": {
            "ts": last_exec.get("ts") or last_exec.get("as_of"),
            "fills": last_exec.get("fills", last_exec.get("placed", [])),
            "reentry_decisions": last_exec.get("reentry_decisions", []),
            "halt": last_exec.get("halt_reason"),
        } if last_exec else None,
        "equity": [{"date": e.get("date"), "value": e.get("value")} for e in equity],
        "guardrails": {
            # the BLOCKING mandate criterion, against ITS limit
            "drawdown": (round(dd_m["value"], 4) if dd_m.get("value") is not None
                         else None),
            "dd_limit": dd_m.get("limit"),
            "dd_state": dd_m.get("state"),
            "dd_reason": dd_m.get("reason"),
            # the entries-halt: different peak, different limit, different action
            "gov_drawdown": (round(_gov_dd, 4) if _gov_dd is not None else None),
            "gov_dd_limit": g["max_drawdown"],
            "gov_breached": _gov_breached,
            "max_order_pct": g["max_order_pct"],
            "max_order": round(g["max_order_pct"] * acct_value, 2),
            # ⛔ ACTIVE ONLY, AND THE RULE COMES FROM governance.
            # This listed every KEY in cooldown.json. Nothing prunes that file --
            # market_monitor.add_cooldown() writes and never deletes -- so it is
            # a permanent accumulation, and the card reported 11 names "on
            # cooldown" when one was. Two of them (EEM, XLK) were ETFs from the
            # sleeve deleted on 2026-08-20, which the book can no longer buy at
            # all. A guardrail panel overstating what is restricted is not a
            # harmless cosmetic: it invites working around a limit that expired
            # in July.
            #
            # Filtered through governance.cooldown_until() rather than comparing
            # dates here, because that function IS the rule the order gate
            # applies (pretooluse_order_gate.py:267) -- a second comparison in
            # this file could disagree with the gate about what is restricted.
            # It reads only, and fails open exactly as the gate does.
            "cooldown": [{"symbol": _s, "until": _u} for _s, _u in
                         sorted((k, gov.cooldown_until(k)) for k in cooldown)
                         if _u]},
        "rule_outs": [{"symbol": k,
                       "reason": (v or {}).get("reason"),
                       "until": (v or {}).get("until")}
                      for k, v in sorted(_ruled.items())],
        # The independent reviewer's verdict. DATA-ONLY and advisory: it is not a
        # veto, it never reached the agent (session.py:SHOW_REVIEW_TO_AGENT is
        # off), and it is shown here for the human who is judging reviewer vs
        # agent. `day` is its own, so a stale verdict reads as stale.
        "review": _read_json(RS / "reviews" / "latest.json", None),
        "realized": _read_json(RS / "rh" / "realized.json", None),
        "health": _health_rows(),
        "pending": _pending_universe_proposal(),
        "activity": recent[-14:][::-1],
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


@app.route("/")
def index():
    html = TEMPLATE.read_text()
    return html.replace("__DASHBOARD_DATA__", json.dumps(build_data()))


@app.route("/api/data.json")
def data():
    return app.response_class(json.dumps(build_data()), mimetype="application/json")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8787)
