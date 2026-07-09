"""Email the newest weekly letter (research_store/newsletters/issue_*.html).

Deterministic delivery step — the headless letter-writer never touches
credentials. Two transports, tried in order (.env keys):

1. Resend HTTPS API (works on DigitalOcean, which blocks ALL outbound SMTP
   ports — verified 25/465/587/2525 dropped on this droplet 2026-07-09):
       RESEND_API_KEY=re_xxxxxxxx
       NEWSLETTER_TO=you@example.com
       NEWSLETTER_FROM=onboarding@resend.dev   # or letters@your-verified-domain
2. Gmail SMTP app password (kept for non-DO deployments):
       NEWSLETTER_TO / NEWSLETTER_FROM / NEWSLETTER_APP_PASSWORD

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
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LETTERS = REPO / "research_store" / "newsletters"

try:
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
except Exception:
    pass


def _send_resend(api_key, sender, to, subject, html) -> None:
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps({"from": sender, "to": [to],
                         "subject": subject, "html": html}).encode(),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def _send_gmail(password, sender, to, subject, html) -> None:
    msg = MIMEText(html, "html")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resend", action="store_true", help="send even if .sent marker exists")
    args = ap.parse_args()

    to = os.environ.get("NEWSLETTER_TO")
    sender = os.environ.get("NEWSLETTER_FROM")
    api_key = os.environ.get("RESEND_API_KEY")
    password = os.environ.get("NEWSLETTER_APP_PASSWORD")
    if not (to and sender and (api_key or password)):
        print("newsletter email not configured (NEWSLETTER_TO/FROM + RESEND_API_KEY "
              "or NEWSLETTER_APP_PASSWORD in .env) — issue written but not sent")
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

    html = issue.read_text()
    try:
        if api_key:
            _send_resend(api_key, sender, to, subject, html)
            via = "resend"
        else:
            _send_gmail(password, sender, to, subject, html)
            via = "gmail"
    except Exception as e:
        sys.exit(f"send failed ({type(e).__name__}): {e}")
    marker.write_text(subject + "\n")
    print(f"sent {issue.name} -> {to} via {via}  ({subject})")


if __name__ == "__main__":
    main()
