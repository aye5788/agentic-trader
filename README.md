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

So Schwab's role here is **fundamentals**, not analysts. (Analyst signal, if wanted,
must come from another source — e.g. web/news.)

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
python scripts/schwab_auth.py     # prints a URL; log in, paste the redirected URL back
```

The login is headless-friendly (no local browser server needed): you open the URL
yourself and paste the redirect URL back — which is what makes VPS deployment viable.

## Scope the data

```bash
python scripts/schwab_scope.py AAPL
```

Dumps the fundamentals payload + full field list to confirm exactly what's available.
