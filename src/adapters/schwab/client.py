"""Schwab API client factory.

Wraps `schwabdev` and wires it up from environment variables so the rest of the
codebase never touches credentials directly. Designed to run headless (no local
browser) so the same code works on a VPS.
"""
import datetime as dt
import os
import sqlite3
from pathlib import Path

import schwabdev
from dotenv import load_dotenv

# Anchor everything to the repo root so behavior is identical regardless of the
# current working directory (interactive shell, cron, or systemd on a VPS).
REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(REPO_ROOT / ".env")

# THE re-auth command — one string, every consumer (schwab_status, health_check's
# phone alert, the no-token error below, OPERATOR_MANUAL). Copy-pasteable as-is:
# absolute cd, and the .venv interpreter spelled out because there is no bare
# `python` on the box and schwabdev is installed ONLY in the venv — a bare
# `python scripts/schwab_auth.py` is a guaranteed ModuleNotFoundError. Several
# call sites had drifted to exactly that; keep new ones pointed here.
REAUTH_CMD = f"cd {REPO_ROOT} && .venv/bin/python scripts/schwab_auth.py"
STATUS_CMD = f"cd {REPO_ROOT} && .venv/bin/python scripts/schwab_status.py"


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise SystemExit(
            f"Missing {name}. Copy .env.example to .env and fill it in."
        )
    return val


def tokens_db_path() -> str:
    configured = os.environ.get("SCHWAB_TOKENS_DB")
    if configured:
        return configured
    return str(REPO_ROOT / "secrets" / "tokens.db")


def refresh_token_issued() -> dt.datetime | None:
    """Freshest `refresh_token_issued` from tokens.db, or None if never authed.

    Checkpoints the WAL first so the read reflects a just-completed re-auth and
    not the stale main-file snapshot. THE single source of truth for token age —
    never infer it from the tokens.db file mtime, which moves every ~30 min when
    the *access* token rotates and so looks fresh on a six-day-old refresh token.
    """
    db = tokens_db_path()
    if not Path(db).exists():
        return None
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA wal_checkpoint(FULL)")   # fold WAL -> main
        row = con.execute("SELECT refresh_token_issued FROM schwabdev").fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if not (row and row[0]):
        return None
    issued = dt.datetime.fromisoformat(row[0])
    return issued if issued.tzinfo else issued.replace(tzinfo=dt.timezone.utc)


def force_reauth(client) -> bool:
    """Force schwabdev to mint a NEW refresh token (runs the OAuth login).

    ⚠️  The force flag is the whole point. schwabdev's update_tokens() renews the
    refresh token only when it has <60.5 min left, so constructing a Client — or
    calling update_tokens() bare — is a NO-OP on a token with days remaining. It
    returns quietly without ever prompting, which is how a weekly re-auth can
    appear to succeed while renewing nothing. Always pair with reauth_took().

    Returns False rather than raising when there is no TTY: schwabdev reads the
    pasted redirect URL via input(), so a non-interactive shell (Claude Code's
    `!`, cron) hits EOFError. Callers report that through reauth_took()'s
    did-it-actually-move check instead of a traceback.
    """
    try:
        return bool(client.update_tokens(force_refresh_token=True))
    except EOFError:
        return False


def reauth_took(before: dt.datetime | None, after: dt.datetime | None) -> bool:
    """Did a re-auth actually land? Pure.

    The ONLY honest success signal is the stored issue time moving forward — not
    a zero exit code, and not an absent exception. schwabdev swallows several
    failure paths (stale auth code, EOF on the paste prompt, losing the tokens.db
    write lock to agentic-monitor) and returns False rather than raising.
    """
    if after is None:
        return False
    if before is None:
        return True
    return after > before


def build_client(*, interactive_auth: bool) -> "schwabdev.Client":
    """Construct a schwabdev client.

    interactive_auth=True  -> allowed to run the one-time browser login flow,
                              which blocks on stdin. Run only from a real terminal.
    interactive_auth=False -> refuses to block: errors out if no token exists yet.
                              Use for automated / data-pull scripts.
    """
    db = tokens_db_path()
    Path(db).parent.mkdir(parents=True, exist_ok=True)

    if not interactive_auth and not Path(db).exists():
        raise SystemExit(
            f"No Schwab token found at {db}.\n"
            f"Run the one-time login first (in a real terminal):\n"
            f"    {REAUTH_CMD}"
        )

    # NB: do NOT pass call_on_auth here. When schwabdev is given that callback it
    # uses the callback's RETURN VALUE as the pasted redirect URL — a print-only
    # callback returns None and crashes the flow. With no callback and
    # open_browser_for_auth=False, schwabdev prints the auth URL and reads the
    # pasted redirect URL via input() — the headless flow we want.
    return schwabdev.Client(
        _require("SCHWAB_APP_KEY"),
        _require("SCHWAB_APP_SECRET"),
        os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1"),
        tokens_db=db,
        open_browser_for_auth=False,  # headless / VPS friendly
    )
