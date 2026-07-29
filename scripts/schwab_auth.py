"""Weekly Schwab OAuth re-login — FORCES a brand-new refresh token.

RUN THIS IN A REAL SSH TERMINAL:
    cd /opt/agentic-trader && .venv/bin/python scripts/schwab_auth.py

It prints an authorization URL. Open it, log in and approve. Schwab redirects to
your callback URL (the page will fail to load — that is expected). Copy the FULL
URL from the browser address bar and paste it back at the prompt.

⚠️  NOT via Claude Code's `!` — that has no interactive stdin, so the paste prompt
hits EOF and nothing is renewed. (This docstring used to *recommend* `!`, which
was flatly wrong.) In an agent shell use the two-step flow instead: run this to
print the URL, then hand the redirect URL to `scripts/schwab_finish_auth.py` as an
argument, within ~30s. See README "Re-auth: which method".

⚠️  WHY THE FORCE MATTERS (this silently broke every early re-auth):
schwabdev renews the refresh token ONLY when it has <60.5 minutes left. Merely
constructing a Client — which is all this script used to do — is therefore NOT a
re-auth: with days left on the clock it quietly no-ops and never prompts. The old
version then printed "✅ Auth complete" regardless, so a re-auth that did nothing
looked identical to one that worked. We now pass force_refresh_token=True AND
verify refresh_token_issued actually advanced before claiming success.

Schwab refresh tokens expire every 7 days, so this must be re-run weekly to keep
an unattended VPS deployment alive.

There is no bare `python` on the box and schwabdev lives ONLY in the .venv, so
always spell out the interpreter:

    .venv/bin/python scripts/schwab_auth.py
    .venv/bin/python scripts/schwab_auth.py --selftest   # no network, no prompt
"""
import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from adapters.schwab.client import (  # noqa: E402
    build_client,
    force_reauth,
    reauth_took,
    refresh_token_issued,
)

REFRESH_TTL_DAYS = 7


def _selftest() -> None:
    """Guards the two things that made a dead re-auth look alive."""

    # 1. force_reauth MUST pass force_refresh_token=True. Without it schwabdev
    #    skips the OAuth login whenever the token has >60.5 min left — the exact
    #    bug that let six-days-stale tokens survive a "successful" re-auth.
    class FakeClient:
        def __init__(self):
            self.calls = []

        def update_tokens(self, *a, **kw):
            self.calls.append((a, kw))
            return True

    fc = FakeClient()
    force_reauth(fc)
    assert fc.calls == [((), {"force_refresh_token": True})], (
        f"force_reauth must force the refresh token, got {fc.calls}"
    )

    # No TTY => schwabdev's input() raises EOFError. Report it as "did not take",
    # not a traceback, so the caller prints the actionable diagnostic.
    class NoTtyClient:
        def update_tokens(self, *a, **kw):
            raise EOFError

    assert force_reauth(NoTtyClient()) is False, "EOF on the paste prompt = not taken"

    # 2. reauth_took is the post-condition: success is the stored issue time
    #    MOVING, never the mere absence of an exception.
    t0 = dt.datetime(2026, 7, 23, 12, 41, tzinfo=dt.timezone.utc)
    t1 = t0 + dt.timedelta(days=6)
    assert reauth_took(t0, t1) is True, "advanced timestamp = took"
    assert reauth_took(t0, t0) is False, "unchanged timestamp = did NOT take"
    assert reauth_took(t1, t0) is False, "backwards timestamp = did NOT take"
    assert reauth_took(None, t1) is True, "first-ever auth = took"
    assert reauth_took(t0, None) is False, "vanished token = did NOT take"
    assert reauth_took(None, None) is False, "still no token = did NOT take"

    print("schwab_auth selftest: OK")


def main() -> None:
    if "--selftest" in sys.argv[1:]:
        _selftest()
        return

    before = refresh_token_issued()
    if before is not None:
        left = (before + dt.timedelta(days=REFRESH_TTL_DAYS)
                - dt.datetime.now(dt.timezone.utc)).total_seconds() / 86400
        print(f"current refresh token issued {before.isoformat(timespec='minutes')} "
              f"({left:.1f} days left) — forcing a new one.\n")

    client = build_client(interactive_auth=True)
    force_reauth(client)
    after = refresh_token_issued()

    if not reauth_took(before, after):
        raise SystemExit(
            "\n❌ RE-AUTH DID NOT TAKE — refresh_token_issued is unchanged "
            f"({before.isoformat(timespec='minutes') if before else 'none'}).\n"
            "   Nothing was renewed. Common causes:\n"
            "     • the pasted URL was stale (Schwab's code expires in ~30s) — retry fast\n"
            "     • no TTY, so the paste prompt hit EOF — run in a real terminal, or use\n"
            "       scripts/schwab_finish_auth.py \"<redirect-url>\"\n"
            "     • agentic-monitor held the tokens.db write lock — retry, or stop it first\n"
            "   Verify with: .venv/bin/python scripts/schwab_status.py"
        )

    expires = after + dt.timedelta(days=REFRESH_TTL_DAYS)
    print(f"\n✅ Auth complete — refresh token issued {after.isoformat(timespec='minutes')}, "
          f"good until {expires.isoformat(timespec='minutes')}.")


if __name__ == "__main__":
    main()
