"""FRED API client — supplementary macro for the regime gate.

FRED is **"as-is"** (the Fed disclaims uptime/accuracy) and occasionally throttles,
so every fetch RETRIES with backoff and, on persistent failure, falls back to the
**last-good cached value**. A FRED outage therefore degrades the regime read to
"trend-gate only" (Schwab carries the load-bearing gate) — it never blocks the
nightly run. Data is daily-close / often T-1, which is exactly right for a nightly
regime read.

Auth: a single `api_key` query param (free, no expiry). Rate limit ~120/min; we
use a handful nightly.
"""
import json
import os
import tempfile
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(REPO_ROOT / ".env")

BASE_URL = "https://api.stlouisfed.org/fred"
CACHE = REPO_ROOT / "research_store" / "macro" / "fred_cache.json"


class FredUnavailable(RuntimeError):
    """FRED could not be reached after retries (caller should use cached value)."""


def _key() -> str:
    k = os.environ.get("FRED_API_KEY")
    if not k:
        raise SystemExit(
            "Missing FRED_API_KEY. Add it to .env "
            "(free key at https://fredaccount.stlouisfed.org/apikeys)."
        )
    return k


def _load_cache() -> dict:
    if not CACHE.exists():
        return {}
    try:
        return json.loads(CACHE.read_text())
    except (ValueError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CACHE.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
        os.replace(tmp, CACHE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def get(path: str, *, retries: int = 3, backoff: float = 1.0, **params):
    """GET a FRED endpoint (api_key + json injected). Retries on 429/network.

    Raises SystemExit on a 400 (bad key/param — a config error worth failing on)
    and FredUnavailable if it can't reach FRED after `retries` (a transient outage
    the caller handles by falling back to cache).
    """
    params["api_key"] = _key()
    params["file_type"] = "json"
    url = f"{BASE_URL}/{path}"
    last = None
    for i in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 429:
                last = "429 rate-limited"
                time.sleep(backoff * (i + 1))
                continue
            if resp.status_code == 400:
                raise SystemExit(f"FRED 400 (bad key/param): {resp.text[:200]}")
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            last = str(e)
            time.sleep(backoff * (i + 1))
    raise FredUnavailable(f"FRED unreachable after {retries} tries: {last}")


def series_latest(series_id: str) -> dict | None:
    """Latest non-missing observation for a series: {value, date, stale}.

    Tries FRED live (updating the cache on success); on FredUnavailable falls back
    to the cached last-good value flagged `stale=True`. Returns None only if FRED
    is down AND there is no cache yet.
    """
    cache = _load_cache()
    try:
        data = get("series/observations", series_id=series_id,
                   sort_order="desc", limit=10)
    except FredUnavailable:
        cached = cache.get(series_id)
        if cached:
            return {**cached, "stale": True}
        return None

    for obs in data.get("observations", []):
        val = obs.get("value")
        if val not in (".", None, ""):  # FRED marks missing values with "."
            rec = {"value": float(val), "date": obs.get("date")}
            cache[series_id] = rec
            _save_cache(cache)
            return {**rec, "stale": False}
    return None
