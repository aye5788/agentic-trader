"""Append one equity point to research_store/history/equity.jsonl — the data
behind the dashboard's performance curve AND the close-to-close series
src/mandate.py's drawdown() reads to decide whether to flatten the book.

Values the RH snapshot through src/marks.py (qty × freshest mark — same math as
the dashboard), so it needs no extra broker call. Runs on its OWN cron entry,
Mon-Fri 16:15 ET — AFTER the session settles, deliberately independent of the
10:00 ET fast loop and the 18:00/Sunday 20:00 slow loop. mandate.drawdown()'s
whole guarantee is "close-to-close, never intraday"; logging at 16:15 (this
codebase's settled-session boundary — see _drop_unsettled_session() in
scripts/fetch_prices.py) is what keeps that guarantee true. Folding this into
the fast loop stamped the point ~10:02 ET, 32 minutes after the open, the
noisiest stretch of the session — the exact thing the mandate promises to
avoid. Folding it into the slow loop instead would couple the equity track
record to the book rebuild succeeding, and the Sunday 20:00 rebalance would
write a spurious weekend point duplicating Friday's marks; a dedicated job
yields exactly one point per trading day, post-close, independent of either
loop. Append-only, idempotent per day (a second run the same date overwrites
that day's point).

    .venv/bin/python scripts/log_equity.py
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import marks  # noqa: E402

HIST = REPO / "research_store" / "history" / "equity.jsonl"


def main() -> None:
    valued = marks.load()
    if valued is None:
        sys.exit("no positions snapshot yet — run the fast loop first")
    value = valued["account_value"]
    invested = valued["invested"]
    date = valued["as_of"] or datetime.now(timezone.utc).date().isoformat()
    point = {"date": date,
             "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "value": round(value, 2), "invested": round(invested, 2),
             "cash": valued["cash"]}

    HIST.parent.mkdir(parents=True, exist_ok=True)
    prior = [json.loads(l) for l in HIST.read_text().splitlines() if l.strip()] if HIST.exists() else []
    prior = [p for p in prior if p.get("date") != date]     # replace same-day point
    prior.append(point)
    HIST.write_text("".join(json.dumps(p) + "\n" for p in prior))
    print(f"logged equity ${value:.2f} for {date} -> {HIST} ({len(prior)} points)")


if __name__ == "__main__":
    main()
