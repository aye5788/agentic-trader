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
                "ticker": t.symbol, "type": "etf" if t.rank >= 100 else "stock",
                "weight": round(t.target_weight, 4), "value": round(mv, 2),
                "qty": p.get("qty"), "avg_cost": p.get("avg_cost"),
                "last": p.get("mark"), "pnl": p.get("pnl"),
                "entry": round(sum(t.entry_zone) / 2, 2) if t.entry_zone else None,
                "stop": t.stop, "targets": t.targets or [],
                "rr": round(rr, 2) if rr else None,
                "score": t.signals.get("score"), "state": "held" if mv > 0 else "pending",
                "thesis": t.thesis,
            })

    equity = _tail_jsonl(RS / "history" / "equity.jsonl", 400)
    gov_state = _read_json(RS / "governance" / "state.json", {})
    peak = float(gov_state.get("peak_value", acct_value) or acct_value)
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
                  "monitor_book": mstate.get("book_asof")},
        "holdings": holdings,
        "equity": [{"date": e.get("date"), "value": e.get("value")} for e in equity],
        "guardrails": {"drawdown": round(dd, 4), "dd_limit": g["max_drawdown"],
                       "max_order_pct": g["max_order_pct"],
                       "max_order": round(g["max_order_pct"] * acct_value, 2),
                       "cooldown": list(cooldown.keys())},
        "activity": _tail_jsonl(RS / "journal.jsonl", 14)[::-1],
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
