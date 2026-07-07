"""Probe exactly what this Alpaca key unlocks for news (read-only).

Mirrors scripts/finnhub_scope.py: exercises the news endpoint and prints a
compact result so we can confirm the free tier works empirically instead of
trusting the docs.

    python scripts/alpaca_scope.py [SYMBOL]
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from adapters.alpaca import news  # noqa: E402
from adapters.alpaca.client import RateLimited  # noqa: E402


def probe(title, fn):
    print(f"\n{'='*66}\n{title}")
    try:
        data = fn()
    except RateLimited as e:
        print(f"  RATE LIMITED: {e}")
        return None
    except Exception as e:  # noqa: BLE001
        print(f"  ERROR: {e}")
        return None
    print(f"  OK  [list len={len(data)}]")
    for art in data[:3]:
        syms = ",".join(art.get("symbols", []))
        print(f"    - [{art.get('created_at', '')[:10]}] ({syms}) {art.get('headline', '')[:70]}")
    return data


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    print(f"Alpaca news scope probe for {sym}")

    probe(f"symbol news: {sym}   (expect FREE)",
          lambda: news.get_news(sym, limit=5))
    probe("market-wide news       (expect FREE)",
          lambda: news.get_news(limit=5))

    print("\n" + "=" * 66)
    print("Done. 'OK' = news available on this key's plan.")
