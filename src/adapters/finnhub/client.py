"""Finnhub API client.

Thin wrapper over the Finnhub REST API. Finnhub's role in this system is the
ANALYST / ESTIMATES slice that Schwab structurally cannot provide: recommendation
trends, earnings surprises, and (where the plan allows) consensus estimates.

Meant to be called from the SLOW research loop only. The free tier is ~60
calls/min — fine for a nightly pass over a modest watchlist, not for anything
real-time or broad-universe. Auth is a single API token (no OAuth, no weekly
expiry like Schwab), passed as a query param.
"""
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

# Anchor to repo root so behavior is identical under an interactive shell, cron,
# or systemd on the VPS (matches the Schwab adapter).
REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(REPO_ROOT / ".env")

BASE_URL = "https://finnhub.io/api/v1"


class PremiumEndpoint(RuntimeError):
    """Endpoint requires a paid Finnhub plan (HTTP 403 on the free tier)."""


class RateLimited(RuntimeError):
    """Finnhub rate limit exceeded (HTTP 429; free tier ~60 calls/min)."""


def _token() -> str:
    tok = os.environ.get("FINNHUB_API_KEY")
    if not tok:
        raise SystemExit(
            "Missing FINNHUB_API_KEY. Add it to .env "
            "(free key at https://finnhub.io/register)."
        )
    return tok


def get(path: str, **params):
    """GET a Finnhub endpoint with the token injected; return parsed JSON.

    Translates the common failure codes into clear, catchable errors so logs
    read cleanly instead of dumping a raw stack trace:
      401 -> SystemExit (bad key)   403 -> PremiumEndpoint   429 -> RateLimited
    """
    params["token"] = _token()
    resp = requests.get(f"{BASE_URL}/{path}", params=params, timeout=15)
    if resp.status_code == 401:
        raise SystemExit("Finnhub 401: invalid API key (check FINNHUB_API_KEY).")
    if resp.status_code == 403:
        raise PremiumEndpoint(f"Finnhub 403 (premium-only endpoint): {path}")
    if resp.status_code == 429:
        raise RateLimited("Finnhub 429: rate limit hit (free tier ~60/min).")
    resp.raise_for_status()
    return resp.json()
