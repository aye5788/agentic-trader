"""Alpaca Market Data API client.

Thin wrapper over Alpaca's REST data API. Alpaca's role in this system is the
NEWS slice — live, free, symbol-tagged market news that Schwab/Finnhub don't
provide. (Alpaca's *price* data is IEX-only on the free plan — single-venue, not
full NBBO — so we deliberately do NOT use it for quotes; quotes come from
Schwab/RH. See docs/DESIGN.md Layer 1.)

Meant to be called from the SLOW research loop. Auth is a key-id / secret-key
pair sent as headers (no OAuth, no weekly expiry like Schwab). The news endpoint
lives on the *data* host, not the trading host, and is available on the free tier.
"""
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

# Anchor to repo root so behavior is identical under an interactive shell, cron,
# or systemd on the VPS (matches the Schwab / Finnhub adapters).
REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(REPO_ROOT / ".env")

# Market-data host (news lives here). NOT the trading host — this adapter is
# read-only research; it has no order surface by design.
BASE_URL = "https://data.alpaca.markets"


class RateLimited(RuntimeError):
    """Alpaca rate limit exceeded (HTTP 429; free tier ~200 calls/min)."""


def _keys() -> tuple[str, str]:
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise SystemExit(
            "Missing ALPACA_API_KEY / ALPACA_SECRET_KEY. Add them to .env "
            "(free keys at https://app.alpaca.markets/ -> Home -> API Keys)."
        )
    return key, secret


def get(path: str, **params):
    """GET an Alpaca data endpoint with auth headers injected; return parsed JSON.

    Translates the common failure codes into clear, catchable errors so logs read
    cleanly instead of dumping a raw stack trace:
      401/403 -> SystemExit (bad key)    429 -> RateLimited
    """
    key, secret = _keys()
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    resp = requests.get(f"{BASE_URL}/{path}", params=params, headers=headers, timeout=15)
    if resp.status_code in (401, 403):
        raise SystemExit(
            f"Alpaca {resp.status_code}: invalid API keys "
            "(check ALPACA_API_KEY / ALPACA_SECRET_KEY)."
        )
    if resp.status_code == 429:
        raise RateLimited("Alpaca 429: rate limit hit (free tier ~200/min).")
    resp.raise_for_status()
    return resp.json()
