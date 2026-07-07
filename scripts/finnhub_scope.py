"""Probe exactly what this Finnhub key unlocks on its plan (read-only).

Mirrors scripts/schwab_scope_full.py: exercises each analyst/estimate endpoint
and prints a compact result so we can see FREE vs PREMIUM empirically instead of
trusting the docs.

    python scripts/finnhub_scope.py [SYMBOL]
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from adapters.finnhub import research  # noqa: E402
from adapters.finnhub.client import PremiumEndpoint, RateLimited  # noqa: E402


def probe(title, fn):
    print(f"\n{'='*66}\n{title}")
    try:
        data = fn()
    except PremiumEndpoint as e:
        print(f"  PREMIUM (not on free tier): {e}")
        return None
    except RateLimited as e:
        print(f"  RATE LIMITED: {e}")
        return None
    except Exception as e:  # noqa: BLE001
        print(f"  ERROR: {e}")
        return None
    if isinstance(data, list):
        print(f"  OK  [list len={len(data)}]" + (f"  e.g. {data[0]}" if data else ""))
    elif isinstance(data, dict):
        keys = list(data.keys())
        print(f"  OK  {{{', '.join(keys[:12])}{'...' if len(keys) > 12 else ''}}}")
        if "metric" in data and isinstance(data["metric"], dict):
            print(f"      metric fields: {len(data['metric'])}")
    else:
        print("  OK ", repr(data)[:120])
    return data


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    print(f"Finnhub scope probe for {sym}")

    probe("recommendation trends  (expect FREE)",
          lambda: research.get_recommendation_trends(sym))
    probe("earnings surprises     (expect FREE)",
          lambda: research.get_earnings_surprises(sym))
    probe("basic financials       (expect FREE)",
          lambda: research.get_basic_financials(sym))
    probe("price target           (often PREMIUM)",
          lambda: research.get_price_target(sym))
    probe("EPS estimates          (often PREMIUM)",
          lambda: research.get_eps_estimates(sym))

    print("\n" + "=" * 66)
    print("Done. 'OK' = available on this key's plan; 'PREMIUM' = paid-only.")
