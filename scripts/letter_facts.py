"""Assemble the deterministic facts for the weekly investor letter.

Division of labor (same as everywhere in this repo): numbers are computed HERE,
in plain Python from the Research Store; the headless letter-writer
(prompts/newsletter.md) turns them into prose and may not invent or recompute
any figure. Output: research_store/newsletters/facts.json.

    .venv/bin/python scripts/letter_facts.py
"""
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import marks                                    # noqa: E402
from research_store import read_current         # noqa: E402

RS = REPO / "research_store"
LETTERS = RS / "newsletters"


def _read_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _jsonl(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def main() -> None:
    LETTERS.mkdir(parents=True, exist_ok=True)
    today = date.today()
    monday = today - timedelta(days=today.weekday())

    issues = sorted(LETTERS.glob("issue_*.html"))
    issue_number = len(issues) + 1
    # events "this week" = since the previous issue was generated (or 7 days)
    if issues:
        since = datetime.fromtimestamp(issues[-1].stat().st_mtime,
                                       tz=timezone.utc).isoformat(timespec="seconds")
    else:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")

    valued = marks.load()
    if valued is None:
        sys.exit("no RH snapshot — cannot write a letter without account state")

    # --- week P&L from the equity curve (needs >= 2 points; else null) ---
    points = _jsonl(RS / "history" / "equity.jsonl")
    week_pnl = None
    baseline = None
    if len(points) >= 2:
        cutoff = (today - timedelta(days=7)).isoformat()
        prior = [p for p in points[:-1] if p.get("date", "") >= cutoff] or points[:-1]
        baseline = prior[0]
        if baseline.get("value"):
            week_pnl = round(valued["account_value"] / float(baseline["value"]) - 1.0, 4)

    # --- unrealized P&L on cost (always available; the honest fallback) ---
    cost = sum(p["qty"] * p["avg_cost"] for p in valued["positions"].values()
               if p.get("qty") and p.get("avg_cost"))
    unrealized = round(valued["invested"] / cost - 1.0, 4) if cost > 0 else None

    # --- the book, valued, in rank order ---
    prod = read_current()
    book = []
    if prod:
        for t in sorted(prod.theses, key=lambda x: x.rank):
            if t.target_weight <= 0:
                continue
            p = valued["positions"].get(t.symbol) or {}
            book.append({"symbol": t.symbol, "rank": t.rank,
                         "sleeve": t.rank >= 100, "thesis": t.thesis,
                         "weight": round(t.target_weight, 4),
                         "stop": round(t.stop, 2) if t.stop else None,
                         "targets": [round(x, 2) for x in (t.targets or [])],
                         "score": t.signals.get("score"),
                         "value": p.get("value"), "pnl": p.get("pnl"),
                         "review_by": t.review_by})

    # --- this week's journal events ---
    events = [e for e in _jsonl(RS / "journal.jsonl") if (e.get("ts") or "9999") >= since
              or e.get("as_of", "") >= since[:10]]
    fills, exits, notes, reentries = [], [], [], []
    for e in events:
        if e.get("event") == "execution":
            for f in e.get("fills", e.get("placed", [])):
                fills.append({k: f.get(k) for k in
                              ("symbol", "side", "amount", "status", "avg_price") if k in f})
            reentries.extend(e.get("reentry_decisions", []))
            if e.get("halt_reason"):
                notes.append(e["halt_reason"])
        elif e.get("event") == "exit_signal":
            exits.extend(e.get("triggers", []))

    days_ahead = (6 - today.weekday()) % 7 or 7           # next Sunday
    facts = {
        "issue_number": f"{issue_number:03d}",
        "issue_date": f"Week of {monday.strftime('%B %-d, %Y')}",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "account": {"value": valued["account_value"], "cash": valued["cash"],
                    "invested": valued["invested"],
                    "cash_pct": round(100 * valued["cash"] / valued["account_value"])
                    if valued["account_value"] else None,
                    "marked_at": valued["marked_at"]},
        "week_pnl": week_pnl,
        "week_pnl_baseline": baseline,                     # null on early issues
        "unrealized_pnl_on_cost": unrealized,
        "regime": (prod.regime if prod and prod.regime else {"status": "unknown"}),
        "book": book,
        "fills_this_week": fills,
        "exit_signals_this_week": exits,
        "reentry_decisions_this_week": reentries,
        "notes": notes,
        "cooldown": list(_read_json(RS / "monitor" / "cooldown.json", {})),
        "next_rebalance": (today + timedelta(days=days_ahead)).isoformat(),
        "kill_switch": (RS / "HALT").exists(),
    }
    out = LETTERS / "facts.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print(f"facts -> {out}  (issue {facts['issue_number']}, "
          f"{len(fills)} fills, {len(book)} positions, "
          f"week_pnl={'n/a' if week_pnl is None else f'{week_pnl:+.2%}'})")


if __name__ == "__main__":
    main()
