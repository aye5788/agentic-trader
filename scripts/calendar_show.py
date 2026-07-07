"""Compile and eyeball the earnings calendar for a universe (read-only-ish).

Compiles from Finnhub (+ RH snapshot if present), persists to
research_store/calendar/, and prints the result plus a few consumer-query demos.

    python scripts/calendar_show.py [SYM ...]     # defaults to a small sample
"""
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import event_calendar as cal  # noqa: E402  (our package; named to avoid shadowing stdlib `calendar`)

DEFAULT_UNIVERSE = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "JPM", "WMT"]


def main() -> None:
    universe = [s.upper() for s in sys.argv[1:]] or DEFAULT_UNIVERSE
    today = date.today().isoformat()
    as_of = datetime.now(timezone.utc).isoformat(timespec="seconds")
    from_date = today
    to_date = (date.today() + timedelta(days=180)).isoformat()

    print(f"Compiling calendar for {len(universe)} names  (as_of {as_of})\n")
    events = cal.compiler.compile_calendar(
        universe, as_of=as_of, from_date=from_date, to_date=to_date)

    hdr = f"{'SYM':6} {'DATE':11} {'SESS':5} {'STATUS':9} {'AGREE':9} {'EPSest':>8} REV"
    print(hdr)
    print("-" * len(hdr))
    for symbol in universe:
        rec = events.get(symbol)
        if not rec:
            print(f"{symbol:6} (no upcoming report found)")
            continue
        eps = rec.get("eps_estimate")
        print(f"{symbol:6} {rec['report_date']:11} {rec['session']:5} "
              f"{rec['status']:9} {rec['agreement']:9} "
              f"{(f'{eps:.2f}' if eps is not None else '-'):>8} "
              f"{'yes' if rec.get('revised') else ''}")

    print("\n--- consumer queries ---")
    for symbol in universe:
        d = cal.days_until_earnings(symbol, today, events)
        if d is not None:
            gate = cal.reports_within(symbol, 14, today, events)
            print(f"  {symbol}: {d:>3}d to earnings"
                  + ("   ⚠ reports within 14d — do not open a straddling swing" if gate else ""))
    fresh = cal.fresh_reports(today, since_days=3, calendar=events)
    print(f"\n  fresh_reports (reported in last 3d): {fresh or 'none'}")
    revs = cal.recent_revisions(30, today)
    print(f"  recent_revisions (30d): {len(revs)} "
          + (str(revs[:3]) if revs else ""))


if __name__ == "__main__":
    main()
