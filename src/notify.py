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



# HTTP headers are latin-1 ONLY. urllib raises UnicodeEncodeError on anything
# else, `push` swallows it, and the alert is never sent -- it prints to the log
# and dies there.
#
# ⚠️ THIS WAS NOT THEORETICAL. Measured 2026-08-12 against the live journal:
# SEVEN of the eight alert titles in this repo failed to encode, and they were
# every safety-critical one -- unprotected position, stop-sell failing, monitor
# feed down, suspected split, HALT with an unprotected position. The only title
# that ever reached a phone was the routine "Exit executor result". The
# unprotected-position push failed on 2026-08-10 and again on 2026-08-11, both
# at 09:30, while the console line printed normally and the operator was told
# the alert had fired.
#
# Both an emoji AND an em-dash break it: latin-1 has neither. The emoji was
# never needed in the title -- ntfy renders `Tags` as emoji, which is what the
# tags argument is for.
_SUBS = {
    "\u2014": "-", "\u2013": "-",      # em / en dash
    "\u2018": "'", "\u2019": "'",      # curly single quotes
    "\u201c": '"', "\u201d": '"',      # curly double quotes
    "\u2026": "...",                   # ellipsis
    "\u00a0": " ",                     # non-breaking space
}


def _header_safe(value: str) -> str:
    """Make a string safe for an HTTP header, never raising and never empty.

    Typography is transliterated so the message survives; anything still
    unencodable is dropped rather than allowed to kill the send. A stripped
    title is worth infinitely more than a perfectly-punctuated one that never
    arrives.
    """
    s = str(value or "")
    for bad, good in _SUBS.items():
        s = s.replace(bad, good)
    s = s.encode("latin-1", "ignore").decode("latin-1").strip()
    return s or "agentic-trader alert"


def push(title: str, message: str, tags: str = "rotating_light",
         topic: str | None = None) -> None:
    """Send a phone push. `topic` picks the channel; default = NTFY_TOPIC.

    Pass topic=ops_topic() for UPKEEP REMINDERS (re-auth due, proposal waiting,
    a job went stale). Those carry no positions, prices or P&L, so that topic is
    safe to hand to third parties — it is the one shared with GitHub Actions.
    NTFY_TOPIC stays on the box because trade alerts carry the live book.
    """
    topic = topic or os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    try:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        req = urllib.request.Request(
            f"{server}/{topic}", data=message.encode(),
            headers={"Title": _header_safe(title), "Priority": "high",
                     "Tags": _header_safe(tags),
                     "User-Agent": "agentic-trader-notify/1.0"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"  ntfy alert failed ({type(e).__name__}): {e}")


def ops_topic() -> str | None:
    """The upkeep-reminder channel. Falls back to NTFY_TOPIC if unset, so a box
    that never configured it still gets its reminders (just on the main topic)."""
    return os.environ.get("NTFY_TOPIC_OPS") or os.environ.get("NTFY_TOPIC")


def _selftest() -> None:
    """Every alert title in this repo must survive an HTTP header."""
    for t in ("\U0001f6a8 Unprotected position(s) \u2014 no stop being watched",
              "\u26a0\ufe0f Position(s) with no take-profit target",
              "\U0001f6a8 MANUAL INTERVENTION \u2014 stop-sell failing",
              "\U0001f6a8 Monitor feed DOWN \u2014 stops unwatched",
              "\U0001f6a8 Suspected split/bad print \u2014 stop NOT actioned",
              "Exit executor result"):
        out = _header_safe(t)
        out.encode("latin-1")               # must not raise -- this WAS the bug
        assert out and out.strip(), t        # never empty
    # meaning survives transliteration, not just encodability
    assert _header_safe("a \u2014 b") == "a - b"
    assert _header_safe("it\u2019s") == "it's"
    # a title of nothing but emoji still yields a usable header
    assert _header_safe("\U0001f6a8") == "agentic-trader alert"
    assert _header_safe("") == "agentic-trader alert"
    assert _header_safe(None) == "agentic-trader alert"
    print("notify: OK -- every alert title survives a latin-1 header")


if __name__ == "__main__":
    _selftest()
