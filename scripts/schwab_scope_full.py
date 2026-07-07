"""Full read-only scope of the Schwab API surface available to this app.

Exercises every non-destructive endpoint and prints a compact summary so we can
see exactly what data is reachable. Does NOT place/preview/cancel any orders.

    python agentic-trader/scripts/schwab_scope_full.py [SYMBOL]
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from adapters.schwab.client import build_client  # noqa: E402


def summarize(obj, depth=0, maxkeys=40):
    """Return a short description of a JSON structure (keys / lengths)."""
    if isinstance(obj, dict):
        keys = list(obj.keys())
        return "{" + ", ".join(keys[:maxkeys]) + ("...}" if len(keys) > maxkeys else "}")
    if isinstance(obj, list):
        return f"[list len={len(obj)}]" + (f" e.g. {summarize(obj[0])}" if obj else "")
    return repr(obj)[:60]


def probe(title, fn):
    print(f"\n{'='*70}\n{title}")
    try:
        resp = fn()
    except Exception as e:
        print(f"  ERROR calling: {e}")
        return None
    code = getattr(resp, "status_code", "?")
    print(f"  HTTP {code}")
    if code == 200:
        try:
            data = resp.json()
            print("  ->", summarize(data))
            return data
        except Exception:
            print("  (non-JSON)", resp.text[:200])
    else:
        print("  ", resp.text[:220])
    return None


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    c = build_client(interactive_auth=False)

    # ---- MARKET DATA -----------------------------------------------------
    q = probe(f"quotes([{sym}])  — realtime quote + field groups", lambda: c.quotes([sym]))
    if q:
        print("  field groups:", list(q.get(sym, {}).keys()))

    probe(f"quote({sym}, fields='quote')  — single-symbol quote", lambda: c.quote(sym, fields="quote"))

    for proj in ("symbol-search", "fundamental"):
        probe(f"instruments({sym}, '{proj}')", lambda p=proj: c.instruments(sym, p))

    ph = probe(f"price_history({sym}, 5d daily)",
               lambda: c.price_history(sym, periodType="month", period=1,
                                       frequencyType="daily", frequency=1))
    if ph and ph.get("candles"):
        print(f"  candles: {len(ph['candles'])}, sample: {ph['candles'][-1]}")

    oc = probe(f"option_chains({sym}, 1 strike, calls)",
               lambda: c.option_chains(sym, contractType="CALL", strikeCount=1))
    if oc:
        print("  chain keys:", summarize(oc))

    probe("market_hours(['equity','option'])", lambda: c.market_hours(["equity", "option"]))

    for idx in ("$DJI", "$SPX", "NASDAQ"):
        probe(f"movers('{idx}')", lambda i=idx: c.movers(i))

    probe("option_expiration_chain(" + sym + ")", lambda: c.option_expiration_chain(sym))

    # ---- ACCOUNTS / TRADING (read-only) ---------------------------------
    # Tells us whether the app has the Accounts & Trading scope at all.
    probe("linked_accounts()  — is Accounts&Trading scope granted?", lambda: c.linked_accounts())
    probe("account_details_all()", lambda: c.account_details_all())
    probe("transactions(...)  — (skipped unless accounts granted)", lambda: c.linked_accounts())

    print("\n" + "=" * 70)
    print("Done. Endpoints returning HTTP 200 above are available to this app.")
