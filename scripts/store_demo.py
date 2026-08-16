"""Exercise the Research Store end-to-end (write → query → outcome → reject).

Writes a valid research product, reads it back, runs the consumer queries, records
an outcome, then proves the mandate gate rejects a bad-geometry / over-cap product.

    python scripts/store_demo.py
"""
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import research_store as rs  # noqa: E402
from research_store import ResearchProduct, Thesis  # noqa: E402


def main() -> None:
    now = datetime.now(timezone.utc)
    as_of = now.isoformat(timespec="seconds")

    product = ResearchProduct(
        as_of=as_of,
        regime={"status": "risk_on", "notes": "SPX > 50DMA; no macro event this week"},
        notes="demo product",
        theses=[
            Thesis(symbol="AAPL", rank=1, verdict="buy",
                   thesis="Post-Q3 beat drift; estimates revised up.",
                   entry_zone=[310, 312], stop=305, targets=[325, 340],
                   target_weight=0.08, confidence=0.7,
                   signals={"surprise_pct": 6.2, "rec_trend": "up",
                            "provenance": {"finnhub": "2026-07-06"}},
                   review_by="2026-10-28 (next earnings)"),
            Thesis(symbol="MSFT", rank=2, verdict="hold",
                   thesis="Constructive but extended; hold half.",
                   entry_zone=[495, 500], stop=485, targets=[525],
                   target_weight=0.06, confidence=0.55,
                   signals={"surprise_pct": 3.1}),
            Thesis(symbol="NFLX", rank=3, verdict="avoid",
                   thesis="Miss + guide-down; stand aside.",
                   target_weight=0.0, confidence=0.6),
        ],
    )

    print("Writing valid product...")
    rs.write_product(product)
    print("  ok\n")

    p = rs.read_current()
    print(f"read_current(): {len(p.theses)} theses, regime={p.regime['status']}")
    print(f"top(2): {[t.symbol for t in rs.top(2)]}")
    print(f"is_stale(24h): {rs.is_stale(24, as_of)}   "
          f"is_stale(0h, +1h): {rs.is_stale(0, (now + timedelta(hours=1)).isoformat())}")

    print("\nRecording an outcome for AAPL...")
    rs.record_outcome("AAPL", {"status": "closed", "pnl_pct": 7.4,
                               "closed_at": as_of, "note": "hit target 1"}, as_of)
    print(f"  AAPL.outcome = {rs.by_symbol('AAPL').outcome}")

    print("\nJournal:")
    for e in rs.recent_journal():
        print(f"  {e}")

    print("\nNow trying an INVALID product (over-cap weight + bad reward:risk)...")
    bad = ResearchProduct(
        as_of=as_of,
        theses=[
            Thesis(symbol="TSLA", rank=1, verdict="buy",
                   entry_zone=[240, 245], stop=238, targets=[248],  # R:R ≈ 0.6
                   target_weight=0.15, confidence=0.5),              # 0.15 > 0.10 cap
        ],
    )
    try:
        rs.write_product(bad)
        print("  ERROR: should have been rejected!")
    except rs.MandateViolation as e:
        print(f"  rejected as expected:\n    {e}")


if __name__ == "__main__":
    main()
