"""Finish a Schwab OAuth re-login WITHOUT an interactive prompt.

The normal scripts/schwab_auth.py reads the pasted redirect URL via input(), which
needs a real TTY. In an agent shell (e.g. Claude Code's `!`), stdin is not
interactive, so input() hits EOFError and the login never completes — which is
exactly how a "re-login" can silently fail to take.

This variant takes the pasted redirect URL as a command-line ARGUMENT and completes
the same OAuth exchange non-interactively. schwabdev's call_on_auth hook returns the
URL string, which schwabdev uses in place of the input() prompt (client.py:123).

⚠️  Constructing the Client is NOT enough to trigger the exchange: schwabdev only
renews the refresh token when it has <60.5 min left, so on a token with days
remaining the call_on_auth hook is never invoked and this script used to print
success having done nothing. We force the renewal and verify the stored issue
time advanced — see client.force_reauth / client.reauth_took.

Run within ~30s of approving in the browser — Schwab's auth code expires fast.

    .venv/bin/python scripts/schwab_finish_auth.py "https://127.0.0.1:8182/?code=...&session=..."
"""
import datetime as dt
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from adapters.schwab import client as schwab_client  # noqa: E402  (import loads .env)

import schwabdev  # noqa: E402

REFRESH_TTL_DAYS = 7


def main() -> None:
    if len(sys.argv) != 2 or "code=" not in sys.argv[1]:
        raise SystemExit(
            "Paste the FULL redirect URL as a single quoted argument, e.g.:\n"
            '    .venv/bin/python scripts/schwab_finish_auth.py '
            '"https://127.0.0.1:8182/?code=...&session=..."'
        )
    pasted = sys.argv[1]
    before = schwab_client.refresh_token_issued()

    # call_on_auth supplies the pasted URL in place of the input() prompt. The
    # exchange fires during construction only if the token is already near expiry;
    # otherwise force_reauth below is what actually invokes the hook.
    client = schwabdev.Client(
        os.environ["SCHWAB_APP_KEY"],
        os.environ["SCHWAB_APP_SECRET"],
        os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1"),
        tokens_db=schwab_client.tokens_db_path(),
        call_on_auth=lambda _auth_url: pasted,
        open_browser_for_auth=False,
    )
    if not schwab_client.reauth_took(before, schwab_client.refresh_token_issued()):
        schwab_client.force_reauth(client)

    after = schwab_client.refresh_token_issued()
    if not schwab_client.reauth_took(before, after):
        raise SystemExit(
            "\n❌ AUTH EXCHANGE DID NOT TAKE — refresh_token_issued is unchanged "
            f"({before.isoformat(timespec='minutes') if before else 'none'}).\n"
            "   Nothing was renewed. Almost always the auth code expired (~30s):\n"
            "   re-open the authorize URL, approve, and paste the new redirect URL fast.\n"
            "   Verify with: .venv/bin/python scripts/schwab_status.py"
        )

    expires = after + dt.timedelta(days=REFRESH_TTL_DAYS)
    print(f"\n✅ Auth exchange complete — refresh token issued "
          f"{after.isoformat(timespec='minutes')}, good until "
          f"{expires.isoformat(timespec='minutes')}.")


if __name__ == "__main__":
    main()
