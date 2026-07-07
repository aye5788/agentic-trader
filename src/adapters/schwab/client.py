"""Schwab API client factory.

Wraps `schwabdev` and wires it up from environment variables so the rest of the
codebase never touches credentials directly. Designed to run headless (no local
browser) so the same code works on a VPS.
"""
import os
from pathlib import Path

import schwabdev
from dotenv import load_dotenv

# Anchor everything to the repo root so behavior is identical regardless of the
# current working directory (interactive shell, cron, or systemd on a VPS).
REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(REPO_ROOT / ".env")


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
            f"    python scripts/schwab_auth.py"
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
