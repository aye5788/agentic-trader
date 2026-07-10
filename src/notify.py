"""Phone push via ntfy (https://ntfy.sh/<NTFY_TOPIC>) — the ONE notify helper.

Plain HTTPS, so it works despite DO's SMTP block. Used by the market monitor
(stop/target alerts), record_fills.py (trade placed/skipped pushes), and
deploy/alert.sh reimplements the same contract in shell for cron ERR traps.
No NTFY_TOPIC in .env -> silently off. push() NEVER raises: an alert failure
must not break a trading run.
"""
import os
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

try:  # best-effort: callers that already loaded .env are unaffected
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
except Exception:
    pass


def push(title: str, message: str, tags: str = "rotating_light") -> None:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    try:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        req = urllib.request.Request(
            f"{server}/{topic}", data=message.encode(),
            headers={"Title": title, "Priority": "high", "Tags": tags,
                     "User-Agent": "agentic-trader-notify/1.0"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"  ntfy alert failed ({type(e).__name__}): {e}")
