"""Scope what the Schwab Market Data API actually returns for research.

Run AFTER auth:
    python agentic-trader/scripts/schwab_scope.py [SYMBOL]

Dumps the fundamentals payload and the full field list so we can confirm exactly
what research data is available (and verify there is no analyst-rating field).
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from adapters.schwab.client import build_client  # noqa: E402


def show(title, resp, limit=2000):
    print(f"\n=== {title} -> HTTP {resp.status_code} ===")
    try:
        data = resp.json()
    except Exception:
        print(resp.text[:limit])
        return None
    print(json.dumps(data, indent=2)[:limit])
    return data


if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    client = build_client(interactive_auth=False)

    fund = show(f"instruments({symbol}, fundamental)", client.instruments(symbol, "fundamental"))
    show(f"quotes([{symbol}])", client.quotes([symbol]))

    try:
        block = fund["instruments"][0]["fundamental"]
        print("\n--- FUNDAMENTAL FIELDS AVAILABLE ---")
        for k in sorted(block):
            print(f"  {k} = {block[k]}")
    except Exception as e:
        print("Could not extract fundamental block:", e)

    print(
        "\nNOTE: scan the field list above — if you see no analyst/rating/"
        "priceTarget field, that confirms analyst data is not in the API."
    )
