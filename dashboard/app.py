"""agentic-trader dashboard — a tiny Flask app that renders the live account
monitor from the Research Store files (no extra broker calls). Meant to sit
behind a Cloudflare Tunnel + Access (auth-gated), see docs/DEPLOY.md.

    .venv/bin/python dashboard/app.py           # serve on 127.0.0.1:8787
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import strategy as strat                              # noqa: E402
from research_store import read_current               # noqa: E402
from research_store.validate import reward_risk       # noqa: E402

RS = REPO / "research_store"
TEMPLATE = (Path(__file__).parent / "dashboard.html")
app = Flask(__name__)


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
    snap = _read_json(RS / "rh" / "positions.json", {})
    positions = snap.get("positions", {})
    acct_value = float(snap.get("account_value", 0) or 0)
    invested = sum(float(v) for v in positions.values())

    holdings = []
    if prod:
        for t in sorted(prod.theses, key=lambda x: x.rank):
            if t.target_weight <= 0:
                continue
            mv = float(positions.get(t.symbol, 0) or 0)
            rr = reward_risk(t)
            holdings.append({
                "ticker": t.symbol, "type": "etf" if t.rank >= 100 else "stock",
                "weight": round(t.target_weight, 4), "value": round(mv, 2),
                "entry": round(sum(t.entry_zone) / 2, 2) if t.entry_zone else None,
                "stop": t.stop, "targets": t.targets or [],
                "rr": round(rr, 2) if rr else None,
                "score": t.signals.get("score"), "state": "held" if mv > 0 else "pending",
                "thesis": t.thesis,
            })

    equity = _tail_jsonl(RS / "history" / "equity.jsonl", 400)
    gov_state = _read_json(RS / "governance" / "state.json", {})
    peak = float(gov_state.get("peak_value", acct_value) or acct_value)
    dd = 0.0 if peak <= 0 else (acct_value / peak - 1.0)
    cooldown = _read_json(RS / "monitor" / "cooldown.json", {})
    mstate = _read_json(RS / "monitor" / "state.json", {})

    g = cfg["governance"]
    regime = (prod.regime if prod and prod.regime else {"status": "unknown"})
    return {
        "account": {"nickname": snap.get("nickname", "Agentic"), "masked": "••••4924",
                    "value": round(acct_value, 2), "invested": round(invested, 2),
                    "cash": round(acct_value - invested, 2)},
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
