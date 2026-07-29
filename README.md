# agentic-trader

An agentic trading research → execution system. Deployable to a VPS.

**Layering** (see the architecture diagram we sketched):

- **Sensing** — market/research data adapters (Schwab, Alpaca, web, …)
- **Slow loop** — deep research → verify → synthesize a ranked conviction list
- **Research store** — the handoff (watchlist + theses + target weights)
- **Fast loop** — read theses → live quotes → size → execute
- **Execution** — Robinhood MCP (the thin "hands"); the Agentic account only
- **Governance / Orchestration** — mandate, journal, guardrails, kill-switch, cadence

This repo currently implements the **Schwab research adapter** (first sensing source).

📐 **Full architecture & rationale: [`docs/DESIGN.md`](docs/DESIGN.md)** (with diagram).

## Schwab adapter — scope

The Schwab **developer** API exposes:

| Available (Market Data API)                    | NOT available                       |
| ---------------------------------------------- | ----------------------------------- |
| `quotes`, `price_history`, `option_chains`     | ❌ analyst ratings / price targets   |
| `movers`, `market_hours`                       | ❌ research reports                  |
| `instruments` + `fundamental` projection       | (UI-only, not in the public API)    |

So Schwab's role here is **fundamentals**, not analysts.

## Finnhub adapter — the analyst slice

Schwab has no analyst data, so **Finnhub** (free tier) fills it. Verified live:

| Available (free tier)                                   | Premium-only        |
| ------------------------------------------------------- | ------------------- |
| recommendation *trends* (strongBuy…strongSell by month) | ❌ price targets     |
| earnings *surprises* (actual vs estimate, % beat/miss)  | ❌ forward EPS est.  |
| basic financials (133 metrics)                          |                     |

Slow-loop only (~60 calls/min). Get a free key at
[finnhub.io/register](https://finnhub.io/register), put it in `.env` as
`FINNHUB_API_KEY`, then confirm your plan's access with:

```bash
python scripts/finnhub_scope.py AAPL
```

## Alpaca adapter — the news slice

Schwab and Finnhub give hard numbers; **Alpaca** (free tier) gives the live
narrative — symbol-tagged market news. Verified live:

| Available (free tier)                                   | Not used here        |
| ------------------------------------------------------- | -------------------- |
| symbol news (`get_news("AAPL")`) — headline, summary, url | ❌ quotes (IEX-only)  |
| market-wide news feed (`get_news()`)                    | ❌ execution          |
| transparent paging past the 50-article/request cap      |                      |

Data-only by design — this adapter points at Alpaca's *data* host and has no
order surface. Get a free key pair at
[app.alpaca.markets](https://app.alpaca.markets/) → **Home → API Keys**, put them
in `.env` as `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`, then confirm with:

```bash
python scripts/alpaca_scope.py AAPL
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # then fill in SCHWAB_APP_KEY / SCHWAB_APP_SECRET
```

Your Schwab developer app must have the **Market Data Production** product enabled,
status **Ready For Use**, and its registered **callback URL** must match
`SCHWAB_CALLBACK_URL` in `.env`.

## One-time (and weekly) login

Schwab refresh tokens **expire every 7 days** — the single biggest operational
constraint for unattended running. Re-run this weekly:

```bash
.venv/bin/python scripts/schwab_auth.py   # prints a URL; log in, paste the redirected URL back
```

There is no bare `python` on the box and `schwabdev` is installed **only** in the
`.venv` — always spell out `.venv/bin/python`. The script forces a brand-new
refresh token and then verifies `refresh_token_issued` actually advanced, so it
can no longer report success without renewing anything (it used to: see
`docs/OPSLOG.md` 2026-07-29). Exit 0 + a printed issue/expiry date is proof.

The login is headless-friendly (no local browser server needed): you open the URL
yourself and paste the redirect URL back — which is what makes VPS deployment viable.

**Check status any time** with:

```bash
.venv/bin/python scripts/schwab_status.py   # issued/expiry + days left + a live API call
```

Use this, **not** the `secrets/tokens.db` file date — the db is WAL-mode, so a fresh
re-auth lands in the `-wal` sidecar and the main file's mtime lags (it can read as a
week old while the token is minutes old). `schwab_status.py` checkpoints the WAL and
reports the real issue time plus a live pass/fail.

### Re-auth: which method, and the 30-second window

Schwab's authorization **code expires in ~30 seconds** and is single-use, so how
you paste matters:

- **A real SSH terminal (most reliable):** run `.venv/bin/python
  scripts/schwab_auth.py`. It blocks at `paste the address bar url here:` — paste
  the FULL redirect URL (`https://127.0.0.1:8182/?code=…&session=…`, everything,
  not just the code) right at that prompt. Same-process, so the whole 30s budget
  goes to the browser→paste hop.
- **Claude Code `!` (two-step, race-prone):** `schwab_auth.py`'s `input()` EOFs
  under `!`, so use the split flow — `! .venv/bin/python scripts/schwab_auth.py`
  prints the URL, then reports `❌ RE-AUTH DID NOT TAKE` and exits 1 (expected —
  the prompt got no input; your existing token is untouched). Follow with
  `! .venv/bin/python scripts/schwab_finish_auth.py "<full redirect url>"`. The
  chat round-trip often blows the 30s window; prefer the SSH method.

### Troubleshooting `invalid_grant` ("Authorization code is invalid, expired or revoked")

This is Schwab rejecting the **code**, not your credentials (a bad secret returns
`invalid_client`). The scripts + schwabdev are correct; the exchange sends the
matching `redirect_uri` and Basic key:secret auth. When it fails on a fresh code,
check in order:

1. **Complete the FULL consent** — Schwab's flow is login → *select account(s) to
   link* → *final Allow*. The `127.0.0.1:8182` redirect is only valid if you
   reached it via that last button; grabbing the URL early yields a dead code.
2. **Beat the 30s window** — use the SSH-interactive method above.
3. **Clock** — `timedatectl` must show `System clock synchronized: yes` (skew
   makes every code look expired). Ours is NTP-synced.
4. **Config** — `SCHWAB_APP_KEY` must equal the authorize URL's `client_id`, and
   `SCHWAB_CALLBACK_URL` must be exactly `https://127.0.0.1:8182` (matches the app
   registration). Both verified correct as of 2026-07-23.
5. **Schwab-side** — if 1–4 are clean and it still fails on fresh codes, it's
   Schwab (often paired with a "we can't log you in right now" page). Wait
   30–60 min, confirm the app is **"Ready For Use"** at developer.schwab.com, and
   as a last resort regenerate the app secret there and update `.env`.

## Scope the data

```bash
python scripts/schwab_scope.py AAPL
```

Dumps the fundamentals payload + full field list to confirm exactly what's available.
