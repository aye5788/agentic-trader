"""Email the newest weekly letter (research_store/newsletters/issue_*.html).

Deterministic delivery step — the headless letter-writer never touches
credentials. Gmail via app password; keys in .env:

    NEWSLETTER_TO=you@example.com
    NEWSLETTER_FROM=you@gmail.com          # the Gmail account that owns the app password
    NEWSLETTER_APP_PASSWORD=xxxxxxxxxxxxxxxx

Exits 0 with a notice (rather than failing the cron run) when credentials are
absent or no unsent issue exists. A `.sent` marker next to each issue prevents
double-sends on re-runs.

    .venv/bin/python scripts/send_newsletter.py            # send newest unsent
    .venv/bin/python scripts/send_newsletter.py --resend   # newest, even if sent
"""
import argparse
import json
import os
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LETTERS = REPO / "research_store" / "newsletters"

try:
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
except Exception:
    pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resend", action="store_true", help="send even if .sent marker exists")
    args = ap.parse_args()

    to = os.environ.get("NEWSLETTER_TO")
    sender = os.environ.get("NEWSLETTER_FROM")
    password = os.environ.get("NEWSLETTER_APP_PASSWORD")
    if not (to and sender and password):
        print("newsletter email not configured (NEWSLETTER_TO/FROM/APP_PASSWORD in .env) "
              "— issue written but not sent")
        return

    issues = sorted(LETTERS.glob("issue_*.html"))
    if not issues:
        print("no issues in research_store/newsletters/ — nothing to send")
        return
    issue = issues[-1]
    marker = issue.with_suffix(".sent")
    if marker.exists() and not args.resend:
        print(f"{issue.name} already sent ({marker.name} exists) — use --resend to force")
        return

    facts = {}
    try:
        facts = json.loads((LETTERS / "facts.json").read_text())
    except Exception:
        pass
    num = facts.get("issue_number") or issue.stem.split("_")[-1]
    subject = f"The Claude Ledger — Issue {num}, {facts.get('issue_date', '')}".rstrip(", ")

    msg = MIMEText(issue.read_text(), "html")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(sender, password)
            smtp.send_message(msg)
    except Exception as e:
        sys.exit(f"send failed ({type(e).__name__}): {e}")
    marker.write_text(subject + "\n")
    print(f"sent {issue.name} -> {to}  ({subject})")


if __name__ == "__main__":
    main()
