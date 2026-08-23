"""ONE deterministic, model-free, READ-ONLY Robinhood account read.

⛔ WHY THIS EXISTS. `research_store/governance/state.json`'s `peak_value` is the
denominator of a HARD ENTRY GATE: the PreToolUse hook refuses a buy when equity
sits more than `[governance] max_drawdown` below it (via `governance.
drawdown_breach`). Whoever maintains that peak decides how tight the gate is.

Until 2026-08-23 the only thing that advanced it was `check_order()` -> `gates()`
-> `update_peak()`. That was wrong twice over: the peak moved only if the AGENT
elected to call a preview tool, and the preview therefore mutated the boundary it
was previewing. `check_order()` is read-only now, which left `update_peak()` with
no caller at all — a frozen peak understates every later drawdown, so the hard
gate loosens on its own as equity makes new highs.

So peak maintenance became deterministic infrastructure work, done by the session
runner before the model exists. That needs a CURRENT account value, and the three
numbers already on disk are all disqualified:

  * `research_store/rh/positions.json` (`marks.load()`) is a CACHE WRITTEN BY THE
    AGENT at the end of the previous session. A hard gate whose input is authored
    by the party being gated is not a gate. It is also routinely stale — two days
    old on 2026-08-14, and again on 2026-08-23.
  * `research_store/history/equity.jsonl` is the MANDATE's series, deliberately a
    different peak for a different gate (see governance.update_peak), and it is
    appended at 16:15, after both sessions.
  * asking the model is circular: the peak must be current BEFORE the model runs.

⛔ WHAT THIS IS NOT. It is not an execution path and must never become one. It
names exactly one tool, `get_portfolio`, as a module constant; no function here
takes a tool name, an order, a side, a symbol or a quantity, so there is no
argument anyone could pass to make it write. Robinhood stays the execution venue
for the AGENT, through the PreToolUse gate — this module is beneath all of that.

⛔ AUTHENTICATION IS BORROWED, NEVER INVENTED. The Claude Code CLI already holds
an OAuth grant for this exact server, in ~/.claude/.credentials.json. That entry
stays THE canonical grant. This module registers no second client, mints no new
consent, and implements no OAuth protocol of its own: the installed MCP SDK's
`OAuthClientProvider` performs every token exchange, and the class below is only
an adapter between Claude's on-disk representation and the SDK's `TokenStorage`.

⛔ AN EXPIRED ACCESS TOKEN IS REFRESHED, NOT A DEADLOCK. Refusing on normal
expiry was the first version of this module and it was wrong in a way that would
have taken the whole system down quietly: the preflight refuses, so Claude never
spawns, so the CLI's own MCP connection never comes up, so nothing ever refreshes
the grant — and every later unattended session refuses for the same reason,
forever. Failing closed when auth genuinely cannot be recovered is right; failing
closed because a token reached its ordinary 24-hour expiry is a self-inflicted
outage. Only an unrecoverable grant fails the session now.

⛔ AND THERE IS NO INTERACTIVE FALLBACK. The provider's normal behaviour on a
dead grant is to fall back to an authorization-code flow — open a browser, wait
for a callback. In an unattended 10:35 cron session that is not a recovery, it is
a hang. Both handlers are wired to raise instead, so the session fails fast with
an operator instruction rather than blocking until the runner's timeout.

⚠️ ONE INSTALLED-VERSION BEHAVIOUR THIS ADAPTER EXISTS TO WORK WITH, verified
against mcp==1.28.1 rather than assumed from upstream: `OAuthClientProvider.
_initialize()` loads stored tokens but NEVER calls `update_token_expiry`, so
`context.token_expiry_time` stays None and `is_token_valid()` returns True for
any stored token no matter how old. The proactive refresh branch
(`not is_token_valid() and can_refresh_token()`) would therefore never fire, the
stale bearer would go out, the server would 401, and the provider would jump
straight past refresh to the interactive flow. So `get_tokens()` below decides
expiry itself and, when the access token is spent, hands back an OAuthToken whose
access_token is empty and whose refresh_token is real — which is exactly the
state the provider's own two predicates are written for: `is_token_valid()` False
(no access token), `can_refresh_token()` True (refresh token + client info). The
SDK then runs a standards-compliant refresh_token grant. Nothing here builds a
token request; re-check this note if the SDK is upgraded.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile
import time
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# The ONE tool this module may call. A read. Not a parameter -- see the docstring.
READ_TOOL = "get_portfolio"
SERVER_NAME = "robinhood-trading"
# Matched together with serverName so the opaque key hash is never depended on.
EXPECTED_URL = "https://agent.robinhood.com/mcp/trading"
CREDENTIALS = Path.home() / ".claude" / ".credentials.json"

# Finite, and small. This runs on the critical path of starting a trading
# session: a hung broker must fail the session, never hang it until the runner's
# own timeout kills a run that never began.
CONNECT_TIMEOUT_S = 20.0
OVERALL_TIMEOUT_S = 45.0

# Refresh this many seconds BEFORE the stored expiry. A token that is valid when
# get_tokens() reads it and expired by the time the request lands is the one race
# a preflight must not lose, because losing it fails the trading session.
EXPIRY_SKEW_S = 60.0


class BrokerReadError(RuntimeError):
    """The account value could not be established. Never carries a fallback."""


def _account_number() -> str:
    """The Agentic account, from the OPERATOR-DECLARED constant.

    config/mandate.toml [account] number, which that file pins precisely so the
    identity cannot be bootstrapped out of something the agent wrote (see its
    own comment, and _expected_account() in src/agent_env/server.py).

    `get_portfolio` REQUIRES account_number -- verified against the live tool
    schema, not remembered -- so a number has to come from somewhere. It comes
    from here rather than from a get_accounts round trip: the pinned constant is
    already this repo's identity authority, and a second broker call on the
    critical path would buy nothing the constant does not already give.
    """
    cfg = tomllib.loads((REPO / "config" / "mandate.toml").read_text())
    num = str(cfg.get("account", {}).get("number", "")).strip()
    if not num:
        raise BrokerReadError(
            "config/mandate.toml [account] number is missing or empty — refusing "
            "to guess which account to read")
    return num


def _select_entry(doc: dict) -> tuple[str, dict]:
    """(key, entry) for THE Robinhood grant, or raise. Exactly one, or nothing.

    Matched on serverName AND serverUrl, never on the opaque hash in the key --
    that hash is Claude Code's own bookkeeping and is not ours to depend on.
    Zero matches and several matches both fail closed: with two grants for the
    same server there is no principled way to pick one, and refreshing the wrong
    one would rotate a token something else is using.
    """
    store = (doc or {}).get("mcpOAuth") or {}
    hits = [(k, v) for k, v in store.items()
            if isinstance(v, dict)
            and v.get("serverName") == SERVER_NAME
            and str(v.get("serverUrl") or "").rstrip("/") == EXPECTED_URL.rstrip("/")]
    if not hits:
        raise BrokerReadError(
            f"no stored OAuth grant for {SERVER_NAME!r} at {EXPECTED_URL} — run "
            f"`claude mcp login {SERVER_NAME}` once, interactively, then re-run")
    if len(hits) > 1:
        raise BrokerReadError(
            f"{len(hits)} stored OAuth grants match {SERVER_NAME!r} at "
            f"{EXPECTED_URL} — refusing to guess which one to use or refresh")
    return hits[0]


def _read_doc() -> dict:
    try:
        return json.loads(CREDENTIALS.read_text())
    except FileNotFoundError:
        raise BrokerReadError(
            f"no Claude Code credential store at {CREDENTIALS} — the Robinhood "
            f"OAuth grant lives there and this read cannot proceed without it")
    except Exception as e:                                  # noqa: BLE001
        raise BrokerReadError(f"credential store unreadable: {type(e).__name__}")


def _atomic_write(path: Path, doc: dict) -> None:
    """Replace `path` with `doc`. Never leaves partial JSON where a reader looks.

    Serialise FIRST, so a formatting error cannot truncate the real file; then
    write a temp file in the SAME directory (os.replace is only atomic within a
    filesystem), flush + fsync it, carry the original's mode and ownership over,
    and rename. A concurrent reader sees either the whole old document or the
    whole new one, and the credential never widens from 0600 on our account.
    """
    blob = json.dumps(doc, indent=2)
    try:
        st = os.stat(path)
        mode, uid, gid = st.st_mode & 0o777, st.st_uid, st.st_gid
    except FileNotFoundError:                               # pragma: no cover
        mode, uid, gid = 0o600, -1, -1
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".credentials.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        if uid != -1:
            try:
                os.chown(tmp, uid, gid)
            except PermissionError:                         # pragma: no cover
                pass        # not privileged enough to preserve ownership; mode still holds
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:                                     # pragma: no cover
            pass
        raise


# The fields whose change means somebody else re-authenticated or refreshed this
# grant underneath us. Compared before every write -- see set_tokens().
_MATERIAL = ("accessToken", "refreshToken", "expiresAt", "clientId")


class ClaudeCredentialStorage:
    """The MCP SDK's TokenStorage, backed by Claude Code's existing credential.

    ⛔ THIS ADAPTS; IT DOES NOT DECIDE. Every OAuth exchange is the SDK's. All
    this class does is translate between Claude's on-disk shape (absolute
    `expiresAt` in epoch MILLISECONDS) and the SDK's (`expires_in`, relative
    seconds) -- and remember what it read, so a refresh cannot overwrite a grant
    that changed while the request was in flight.
    """

    def __init__(self) -> None:
        self._basis: dict | None = None      # the entry this refresh is based on
        self._key: str | None = None

    # -- reads ------------------------------------------------------------
    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken                # noqa: PLC0415
        key, entry = _select_entry(_read_doc())
        self._key, self._basis = key, dict(entry)
        refresh = entry.get("refreshToken") or None
        scope = entry.get("scope") or None
        exp = entry.get("expiresAt")
        remaining = None
        if isinstance(exp, (int, float)):
            remaining = (float(exp) / 1000.0) - time.time()

        if remaining is not None and remaining <= EXPIRY_SKEW_S:
            # SPENT. Hand back the refresh token and no access token: that is the
            # provider's own "refresh me" state (is_token_valid False,
            # can_refresh_token True). See the module docstring for why the
            # installed version needs this said explicitly.
            return OAuthToken(access_token="", token_type="Bearer", expires_in=0,
                              scope=scope, refresh_token=refresh)
        access = entry.get("accessToken")
        if not access:
            return OAuthToken(access_token="", token_type="Bearer", expires_in=0,
                              scope=scope, refresh_token=refresh)
        return OAuthToken(
            access_token=str(access), token_type="Bearer",
            expires_in=int(remaining) if remaining is not None else None,
            scope=scope, refresh_token=refresh)

    async def get_client_info(self):
        """The registered public client, from the grant Claude already holds.

        `token_endpoint_auth_method="none"`: this is a PKCE public client and the
        stored credential carries no secret. Verified against the installed
        `OAuthContext.prepare_token_auth`, which then sends client_id in the form
        body and adds no Authorization header -- the correct shape for a public
        client refresh. No secret is invented and no client is registered.
        """
        from mcp.shared.auth import OAuthClientInformationFull  # noqa: PLC0415
        _key, entry = _select_entry(_read_doc())
        client_id = entry.get("clientId")
        if not client_id:
            return None      # -> can_refresh_token() False -> fail closed, never register
        redirect = entry.get("redirectUri")
        return OAuthClientInformationFull(
            client_id=str(client_id),
            redirect_uris=[redirect] if redirect else None,
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            scope=entry.get("scope") or None)

    # -- writes -----------------------------------------------------------
    async def set_tokens(self, tokens) -> None:
        """Persist refreshed material back into the SAME Claude entry.

        ⛔ COMPARE-AND-SWAP. The document is re-read immediately before writing
        and the target entry is compared against the one this refresh was based
        on. If Claude Code (or another session) re-authenticated in the meantime,
        the entry now holds a NEWER grant and writing ours over it would revoke
        a token something else is using. Refuse instead.

        ⛔ AND THE REFRESH TOKEN IS PRESERVED WHEN THE RESPONSE OMITS IT. RFC 6749
        §6 permits a refresh response with no new refresh_token, meaning "keep the
        one you have". The installed SDK does NOT do that for us -- verified:
        `_handle_refresh_response` validates the response into an OAuthToken whose
        `refresh_token` simply defaults to None, and hands that straight to this
        method. Trusting it verbatim would erase a working refresh token on the
        first such response and lock the system out permanently.
        """
        doc = _read_doc()
        key, entry = _select_entry(doc)
        if self._basis is None:                             # pragma: no cover
            raise BrokerReadError("set_tokens before get_tokens — refusing to write")
        if key != self._key or any(entry.get(f) != self._basis.get(f) for f in _MATERIAL):
            raise BrokerReadError(
                "the Robinhood credential changed while this refresh was in "
                "flight (another Claude session or `claude mcp login` wrote it) — "
                "refusing to overwrite the newer grant. Re-run the session.")

        updated = dict(entry)                # every unknown field carried through
        updated["accessToken"] = tokens.access_token
        if tokens.refresh_token:
            updated["refreshToken"] = tokens.refresh_token   # rotated
        # else: leave updated["refreshToken"] exactly as it was -- see above
        if tokens.expires_in is not None:
            updated["expiresAt"] = int((time.time() + float(tokens.expires_in)) * 1000)
        if tokens.scope:
            updated["scope"] = tokens.scope

        doc["mcpOAuth"][key] = updated       # only this entry; nothing else touched
        _atomic_write(CREDENTIALS, doc)
        self._basis = dict(updated)

    async def set_client_info(self, client_info) -> None:
        """Never. Registering a client here would create a SECOND Robinhood grant
        beside the canonical one Claude owns, and nothing would ever reconcile
        them. Reaching this means the provider tried to register, which only
        happens on a path we have already refused."""
        raise BrokerReadError(
            "refusing to register a new OAuth client — the Claude Code grant is "
            f"the only one this system uses. Repair it with `claude mcp login "
            f"{SERVER_NAME}`.")


async def _no_browser(url: str) -> None:
    """The authorization-code fallback, disarmed. Unattended sessions must not
    open a browser or block on a callback that will never come."""
    raise BrokerReadError(
        "Robinhood OAuth requires fresh interactive authorization — the stored "
        "grant could not be refreshed. An unattended session will not open a "
        f"browser. Repair it with `claude mcp login {SERVER_NAME}`, then re-run.")


async def _no_callback() -> tuple:
    await _no_browser("")
    raise AssertionError("unreachable")                      # pragma: no cover


def _discover_metadata(auth_server_url: str | None):
    """The authorization server's own metadata, or None. Public, unauthenticated.

    ⛔ WITHOUT THIS, REFRESH GOES TO THE WRONG HOST AND ALWAYS FAILS. Verified
    against the installed provider: `async_auth_flow` attempts the refresh on the
    FIRST request, before any discovery has run, so `context.oauth_metadata` is
    still None and `_refresh_token()` falls back to
    `urljoin("https://agent.robinhood.com", "/token")`. Robinhood's published
    metadata declares the token endpoint as `https://api.robinhood.com/oauth2/
    token/` -- a different host AND path. The refresh POST would 404, the stale
    bearer would 401, and the provider would jump to interactive re-auth, which
    an unattended session correctly refuses. The refresh would then never have
    worked in production even once.

    So the metadata is fetched here and handed to the provider before the flow
    starts, using the SDK's OWN discovery-URL builder and request/response
    helpers rather than a URL guessed here. Failure returns None: the provider
    then behaves exactly as it would have anyway, and the error surfaces as a
    refusal rather than as a wrong endpoint quietly retried.
    """
    import httpx                                             # noqa: PLC0415
    from mcp.client.auth.utils import (                      # noqa: PLC0415
        build_oauth_authorization_server_metadata_discovery_urls,
        create_oauth_metadata_request)
    from mcp.shared.auth import OAuthMetadata                # noqa: PLC0415
    try:
        urls = build_oauth_authorization_server_metadata_discovery_urls(
            auth_server_url, EXPECTED_URL)
        with httpx.Client(timeout=CONNECT_TIMEOUT_S, follow_redirects=True) as c:
            for u in urls:
                req = create_oauth_metadata_request(u)
                r = c.send(c.build_request(req.method, req.url, headers=req.headers))
                if r.status_code == 200:
                    return OAuthMetadata.model_validate_json(r.content)
    except Exception:                                        # noqa: BLE001
        return None
    return None


def _provider(storage):
    """The SDK's OAuth provider, with the interactive fallback disarmed."""
    from mcp.client.auth import OAuthClientProvider           # noqa: PLC0415
    from mcp.shared.auth import OAuthClientMetadata           # noqa: PLC0415
    _key, entry = _select_entry(_read_doc())
    redirect = entry.get("redirectUri")
    prov = OAuthClientProvider(
        server_url=EXPECTED_URL,
        client_metadata=OAuthClientMetadata(
            redirect_uris=[redirect] if redirect else None,
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            scope=entry.get("scope") or None),
        storage=storage,
        redirect_handler=_no_browser,      # raises; never opens anything
        callback_handler=_no_callback,     # raises; never waits on stdin
        timeout=CONNECT_TIMEOUT_S)
    # Seed the declared token endpoint BEFORE the flow runs -- see
    # _discover_metadata() for why the provider's own fallback is wrong here.
    md = _discover_metadata((entry.get("discoveryState") or {}).get("authorizationServerUrl"))
    if md is not None:
        prov.context.oauth_metadata = md
    return prov


async def _fetch(account: str) -> str:
    from mcp import ClientSession                            # noqa: PLC0415
    from mcp.client.streamable_http import streamablehttp_client   # noqa: PLC0415

    async with streamablehttp_client(
            EXPECTED_URL, auth=_provider(ClaudeCredentialStorage()),
            timeout=CONNECT_TIMEOUT_S) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(READ_TOOL, {"account_number": account})
            if getattr(res, "isError", False):
                raise BrokerReadError(f"{READ_TOOL} returned an error result")
            if not res.content:
                raise BrokerReadError(f"{READ_TOOL} returned no content")
            text = getattr(res.content[0], "text", None)
            if not text:
                raise BrokerReadError(f"{READ_TOOL} returned a non-text part")
            return text


def parse_account_value(text: str) -> float:
    """Extract and VALIDATE the account value from a get_portfolio response.

    Pure, so the validation is testable without a broker. The field is
    `data.total_value`, read off the live schema (its own `guide` says: 'Show
    total_value as "account value" or "portfolio value"'). It arrives as a
    STRING decimal -- "72.44291655" -- never a number, so it is parsed
    explicitly rather than trusted to already be one.

    Rejects missing / non-numeric / non-finite / non-positive. A zero or
    negative account value cannot produce a meaningful drawdown ratio, and
    silently feeding one into the peak would corrupt the gate permanently.
    """
    try:
        obj = json.loads(text)
    except Exception:                                        # noqa: BLE001
        raise BrokerReadError(f"{READ_TOOL} response is not JSON")
    if not isinstance(obj, dict) or not isinstance(obj.get("data"), dict):
        raise BrokerReadError(f"{READ_TOOL} response has no `data` object")
    raw = obj["data"].get("total_value")
    if raw is None:
        raise BrokerReadError(f"{READ_TOOL} response carries no `data.total_value`")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise BrokerReadError(f"`data.total_value` is not numeric: {raw!r}")
    if not math.isfinite(val):
        raise BrokerReadError(f"`data.total_value` is not finite: {raw!r}")
    if val <= 0:
        raise BrokerReadError(f"`data.total_value` is not positive: {raw!r}")
    return val


def _first_broker_error(exc):
    """The first BrokerReadError anywhere inside a (possibly nested) exception
    group or __cause__/__context__ chain, or None. Pure."""
    seen, stack = set(), [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, BrokerReadError):
            return e
        stack.extend(getattr(e, "exceptions", ()) or ())
        stack.append(getattr(e, "__cause__", None))
        stack.append(getattr(e, "__context__", None))
    return None


def read_current_account_value() -> float:
    """-> the account's current total value, straight from the broker.

    Model-free: a direct authenticated MCP call. No `claude` process, no
    inference, no prose parsed. Raises BrokerReadError on anything less than a
    clean, validated number -- there is no fallback and there must never be one.
    """
    account = _account_number()

    async def _go():
        return await asyncio.wait_for(_fetch(account), OVERALL_TIMEOUT_S)

    try:
        text = asyncio.run(_go())
    except BrokerReadError:
        raise
    except asyncio.TimeoutError:
        raise BrokerReadError(
            f"{READ_TOOL} did not answer within {OVERALL_TIMEOUT_S:.0f}s")
    except BaseException as e:                               # noqa: BLE001
        # ⛔ UNWRAP FIRST. anyio runs the transport in a task group, so our own
        # BrokerReadError -- including the one that says "repair it with `claude
        # mcp login`" -- arrives buried in an ExceptionGroup and renders as
        # "unhandled errors in a TaskGroup (1 sub-exception)". That string tells
        # a 10:35 operator nothing and hides the one instruction that fixes it.
        inner = _first_broker_error(e)
        if inner is not None:
            raise inner from None
        raise BrokerReadError(f"{READ_TOOL} failed: {type(e).__name__}: {e}")
    return parse_account_value(text)
