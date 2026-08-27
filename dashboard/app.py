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
    positions = valued["positions"]
    acct_value = valued["account_value"]
    invested = valued["invested"]

    holdings = []
    if prod:
        for t in sorted(prod.theses, key=lambda x: x.rank):
            if t.target_weight <= 0:
                continue
            p = positions.get(t.symbol) or {}
            mv = p.get("value", 0.0)
            rr = reward_risk(t)
            holdings.append({
                # rank >= 100 was the ETF sleeve's band; the sleeve is deleted
                # (2026-08-20) and ranks >= 200 are now protective-only theses.
                "ticker": t.symbol, "type": "stock",
                "weight": round(t.target_weight, 4), "value": round(mv, 2),
                "qty": p.get("qty"), "avg_cost": p.get("avg_cost"),
                "last": p.get("mark"), "pnl": p.get("pnl"),
                "entry": round(sum(t.entry_zone) / 2, 2) if t.entry_zone else None,
                "stop": t.stop, "targets": t.targets or [],
                "rr": round(rr, 2) if rr else None,
                "score": t.signals.get("score"), "state": "held" if mv > 0 else "pending",
                "thesis": t.thesis,
            })

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
