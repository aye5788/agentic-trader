"""Probe the FRED macro indicators (read-only) and confirm the key works.

    python scripts/fred_scope.py

Prints each regime series' latest value + date, and whether it came back fresh or
from the last-good cache (FRED being "as-is" / occasionally throttled).
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from adapters.fred import indicators  # noqa: E402


def main() -> None:
    print("FRED macro regime indicators\n")
    snap = indicators.snapshot()
    for name, sid in indicators.SERIES.items():
        rec = snap.get(name)
        if rec is None:
            print(f"  {name:20} ({sid}): UNAVAILABLE (FRED down, no cache)")
            continue
        tag = "cached/stale" if rec.get("stale") else "fresh"
        print(f"  {name:20} ({sid}): {rec['value']}  @ {rec['date']}  [{tag}]")

    print("\nDone. These CONFIRM the mechanical regime read; the load-bearing gate "
          "(index > 50-day MA) is Schwab-computed and FRED-independent.")


if __name__ == "__main__":
    main()
