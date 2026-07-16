"""Finish a Schwab OAuth re-login WITHOUT an interactive prompt.

The normal scripts/schwab_auth.py reads the pasted redirect URL via input(), which
needs a real TTY. In an agent shell (e.g. Claude Code's `!`), stdin is not
interactive, so input() hits EOFError and the login never completes — which is
exactly how a "re-login" can silently fail to take.

This variant takes the pasted redirect URL as a command-line ARGUMENT and completes
the same OAuth exchange non-interactively. schwabdev's call_on_auth hook returns the
URL string, which schwabdev uses in place of the input() prompt (client.py:123).

Run within ~30s of approving in the browser — Schwab's auth code expires fast.

    .venv/bin/python scripts/schwab_finish_auth.py "https://127.0.0.1:8182/?code=...&session=..."
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from adapters.schwab import client as schwab_client  # noqa: E402  (import loads .env)

import schwabdev  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2 or "code=" not in sys.argv[1]:
        raise SystemExit(
            "Paste the FULL redirect URL as a single quoted argument, e.g.:\n"
            '    .venv/bin/python scripts/schwab_finish_auth.py '
            '"https://127.0.0.1:8182/?code=...&session=..."'
        )
    pasted = sys.argv[1]
    # Constructing the client with call_on_auth triggers the exchange immediately,
    # writing the fresh access + refresh tokens to the shared tokens.db.
    schwabdev.Client(
        os.environ["SCHWAB_APP_KEY"],
        os.environ["SCHWAB_APP_SECRET"],
        os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1"),
        tokens_db=schwab_client.tokens_db_path(),
        call_on_auth=lambda _auth_url: pasted,
        open_browser_for_auth=False,
    )
    print("\n✅ Auth exchange complete — token written to tokens.db.")


if __name__ == "__main__":
    main()
