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
import marks                                          # noqa: E402
import strategy as strat                              # noqa: E402
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
            "ticker": sym, "type": "stock",
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

    # plan vs. actual: the last computed order plan + the last execution event
    plan = _read_json(RS / "rh" / "order_plan.json", {})
    recent = _tail_jsonl(RS / "journal.jsonl", 60)
    last_exec = next((e for e in reversed(recent) if e.get("event") == "execution"), None)

    equity = _tail_jsonl(RS / "history" / "equity.jsonl", 400)
    # Peak sourced from the audited equity.jsonl series, NOT from governance's
    # research_store/governance/state.json peak_value (FIX A, 2026-08-10): that
    # tracker is sampled at an arbitrary fast-loop moment and has already
    # drifted from this series (82.22 there vs 81.99 here on 2026-08-10). The
    # dashboard must show the same drawdown number the mandate uses, not a
    # second, silently-competing one. See src/mandate.py drawdown()'s docstring.
    equity_vals = [e.get("value") for e in equity
                   if isinstance(e.get("value"), (int, float))]
    peak = float(max(equity_vals)) if equity_vals else acct_value
    dd = 0.0 if peak <= 0 else min(0.0, acct_value / peak - 1.0)
    cooldown = _read_json(RS / "monitor" / "cooldown.json", {})
    mstate = _read_json(RS / "monitor" / "state.json", {})

    g = cfg["governance"]
    regime = (prod.regime if prod and prod.regime else {"status": "unknown"})
    return {
        "account": {"nickname": "Agentic", "masked": "••••4924",
                    "value": round(acct_value, 2), "invested": round(invested, 2),
                    "cash": valued["cash"], "marked_at": valued["marked_at"]},
        "asof": prod.as_of if prod else None,
        "regime": regime,
        "flags": {"live_approved": bool(cfg.get("proof", {}).get("live_approved")),
                  "kill_switch": (RS / "HALT").exists(),
                  # a forgotten HALT_ENTRIES silently stops the book growing —
                  # it has to be visible somewhere standing, not just in a log
                  "halt_entries": (RS / "HALT_ENTRIES").exists(),
                  "monitor_book": mstate.get("book_asof")},
        "holdings": holdings,
        "plan": {
            "generated": plan.get("generated"), "as_of": plan.get("as_of"),
            "live_approved": plan.get("live_approved"),
            "halted": plan.get("halted") or [],
            "approved": [{k: o.get(k) for k in ("symbol", "side", "amount", "reason")}
                         for o in plan.get("approved", [])],
            "review": [{"symbol": o.get("symbol"), "amount": o.get("amount"),
                        "tier": (o.get("reentry") or {}).get("tier"),
                        "floor": (o.get("reentry") or {}).get("knife_floor")}
                       for o in plan.get("review", [])],
            "blocked": [{"symbol": o.get("symbol"), "why": o.get("blocked")}
                        for o in plan.get("blocked", [])],
        } if plan else None,
        "last_execution": {
            "ts": last_exec.get("ts") or last_exec.get("as_of"),
            "fills": last_exec.get("fills", last_exec.get("placed", [])),
            "reentry_decisions": last_exec.get("reentry_decisions", []),
            "halt": last_exec.get("halt_reason"),
        } if last_exec else None,
        "equity": [{"date": e.get("date"), "value": e.get("value")} for e in equity],
        "guardrails": {"drawdown": round(dd, 4), "dd_limit": g["max_drawdown"],
                       "max_order_pct": g["max_order_pct"],
                       "max_order": round(g["max_order_pct"] * acct_value, 2),
                       "cooldown": list(cooldown.keys())},
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
