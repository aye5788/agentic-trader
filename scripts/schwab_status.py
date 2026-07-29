"""Schwab token status — the one unambiguous "is my token good?" check.

Reads the refresh-token issue time straight from the shared tokens.db (WAL-
checkpointed first, so you NEVER mis-read a stale file date — that footgun is
exactly what this tool exists to kill), reports days left on the 7-day refresh
window, and makes a live Schwab call to prove the token actually works.

    python scripts/schwab_status.py            # status + live API check
    python scripts/schwab_status.py --no-call  # skip the live call

Prints NO secrets — only timestamps and a pass/fail.
"""
import argparse
import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from adapters.schwab import client as schwab_client  # noqa: E402  (import loads .env)

REFRESH_TTL_DAYS = 7  # Schwab refresh tokens live 7 days; access token ~30 min.

# The WAL-checkpointed reader lives in the adapter (one source of truth for token
# age). Kept re-exported under this name because src/health.py imports it.
_token_issued = schwab_client.refresh_token_issued


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-call", action="store_true", help="skip the live Schwab API check")
    args = ap.parse_args()

    issued = _token_issued()
    if issued is None:
        raise SystemExit("no Schwab token found — run: python scripts/schwab_auth.py")

    now = dt.datetime.now(dt.timezone.utc)
    expires = issued + dt.timedelta(days=REFRESH_TTL_DAYS)
    days_left = (expires - now).total_seconds() / 86400
    state = "OK" if days_left > 0 else "EXPIRED"

    print(f"Schwab refresh token: {state}")
    print(f"  issued : {issued.astimezone(dt.timezone.utc).isoformat(timespec='minutes')}")
    print(f"  expires: {expires.astimezone(dt.timezone.utc).isoformat(timespec='minutes')}"
          f"  ({days_left:.1f} days left)")
    if state == "OK" and days_left < 2:
        print("  ⚠️  re-auth soon: python scripts/schwab_auth.py")
    elif state == "EXPIRED":
        print("  ⚠️  re-auth now: python scripts/schwab_auth.py")

    if not args.no_call:
        try:
            from adapters.schwab import research
            ph = research.get_price_history("SPY", period_type="month", period=1,
                                            frequency_type="daily")
            if ph:
                d = dt.datetime.fromtimestamp(ph[-1]["datetime"] / 1000, dt.timezone.utc).date()
                print(f"  live check: OK — SPY latest {d} close={ph[-1]['close']}")
            else:
                print("  live check: EMPTY response (token may be invalid despite the dates above)")
        except Exception as e:  # noqa: BLE001 — surface any failure verbatim
            print(f"  live check: FAILED — {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
