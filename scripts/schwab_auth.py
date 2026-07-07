"""One-time (and weekly) Schwab OAuth login.

RUN THIS IN A REAL TERMINAL — in Claude Code, prefix with `!`:
    ! python agentic-trader/scripts/schwab_auth.py

It prints an authorization URL. Open it, log in and approve. Schwab redirects to
your callback URL (the page will fail to load — that is expected). Copy the FULL
URL from the browser address bar and paste it back at the prompt.

Schwab refresh tokens expire every 7 days, so this must be re-run weekly to keep
an unattended VPS deployment alive.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from adapters.schwab.client import build_client  # noqa: E402

if __name__ == "__main__":
    build_client(interactive_auth=True)
    print("\n✅ Auth complete — token stored. Next: python scripts/schwab_scope.py")
