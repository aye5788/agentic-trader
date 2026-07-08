# CLAUDE.md — operating context for any AI agent working in this repo

This file is auto-loaded by Claude Code. If you are an agent reviewing or
extending this repo (e.g. running on the VPS), read this first, then
[`docs/DESIGN.md`](docs/DESIGN.md) for the full architecture.

This is a **research → execution agentic trading system**. Robinhood is only the
thin execution "hands"; everything upstream is research. The whole design lives
in `docs/DESIGN.md`.

---

## ⛔ Hard rules (safety guardrails — do not violate)

1. **Execution happens ONLY through Robinhood, and ONLY in the dedicated
   "Agentic" account** (`agentic_allowed=true`). Every other Robinhood account is
   **read-only** to the agent. Never place, modify, or cancel orders in any other
   account.
2. **Every other data source is RESEARCH / DATA ONLY — never execution.**
   - **Schwab** is data-only *by credential*: its Accounts & Trading API returns
     HTTP 401. It structurally cannot trade. Do not attempt to route orders
     through it.
   - **Finnhub** and **Alpaca** are read-only data providers. No trading surface.
3. **This is LIVE money.** The Agentic account holds real funds (currently ~$20,
   demonstration scale). Real orders move real money. Prefer the
   review → place pattern; respect any human-approval / guardrail config.
4. **Secrets never leave the box.** `.env` and `secrets/` are git-ignored — never
   commit them, never print their contents, never paste a key/token into chat.

---

## Data sources and their roles

Full scope + verified availability is in `docs/DESIGN.md` (Layer 1). Summary:

| Source | Role | Notes |
| ------ | ---- | ----- |
| **Schwab** (`src/adapters/schwab/`) | Fundamentals, NBBO quotes, price history, options+greeks, movers | Market Data only. OAuth, **refresh token expires every 7 days** → weekly re-auth. |
| **Finnhub** (`src/adapters/finnhub/`) | Analyst *recommendation trends*, earnings *surprises*, 133 basic-financial metrics | Free tier. Price-target *level* and forward EPS estimates are **premium (403) — skipped**. Slow-loop only (~60 calls/min). |
| **Alpaca** (`src/adapters/alpaca/`) | Live symbol-tagged news; screeners (movers, most-active) via MCP | Free tier. Price data is **IEX-only (not full NBBO)** → do not use it for quotes; use Schwab/RH. |
| **Robinhood** (MCP) | **Execution** + its own fundamentals/earnings | The only execution venue. Agentic account only. |

---

## Deployment & billing (VPS)

See `docs/DESIGN.md` → "Deployment (VPS)" for the full plain-English version.
The operationally critical bit:

- Run headless via **Claude Code on the subscription plan**: `claude setup-token`
  → `CLAUDE_CODE_OAUTH_TOKEN` → `claude -p "..."` under cron.
- **⚠️ Footgun:** if `ANTHROPIC_API_KEY` is set anywhere on the box, it silently
  overrides the subscription and bills per-token to the API. Keep it **unset**.
- Robinhood connects as a standard remote HTTP MCP
  (`https://agent.robinhood.com/mcp/trading`) — works headless after a one-time
  desktop OAuth. RH token lifetime is undocumented; watch for re-auth prompts.

---

## Repo layout

```
src/adapters/schwab/    Schwab Market Data client + research functions
src/adapters/finnhub/   Finnhub analyst/estimates client + research functions
config/strategy.toml    CODIFIED STRATEGY — single source of truth: risk gates,
                        universe, signal, trade management, regime floor.
                        Tune the strategy HERE, not in code. `[risk]` = the store's
                        validation mandate. Load via src/strategy.py.
                        NOTE: edge is migrating PEAD -> momentum; [signal]/[meta]
                        still PEAD-era pending remaining design axes.
config/universe.csv     Fixed 150-name momentum universe (human-seed reconciled
                        with dollar-volume liquidity fill). `flag` col marks
                        adr/micro/spec/fresh-ipo model-caveats. Referenced by
                        [universe] in strategy.toml.
config/etf_universe.csv 18-ETF dual-momentum rotation sleeve (11 SPDR sectors +
                        broad + intl + defensive). A SECOND parallel engine to the
                        single-name book; defensive assets rank in-sleeve as the
                        built-in off-switch. Referenced by [etf_sleeve].
src/strategy.py         Strategy-config loader (tomllib) + risk_mandate()
src/research_store/     Research Store — validated slow→fast handoff (belief +
                        journal). write_product enforces the [risk] mandate.
src/adapters/alpaca/    Alpaca news client + get_news (data-only, no trading)
src/event_calendar/     Earnings/event calendar compiler (timing + risk spine).
                        Deterministic: Finnhub REST spine + optional agent-supplied
                        RH snapshot; tags confirmed/estimated, logs date revisions.
                        (Named `event_calendar`, NOT `calendar` — avoids shadowing
                        the Python stdlib module, which breaks imports.)
scripts/                One-off + weekly auth and API-scope probe scripts
docs/DESIGN.md          Full architecture (6 layers, two-clock model, scope tables)
docs/architecture.*     The architecture diagram (svg + excalidraw source)
.env / .env.example     Credentials (git-ignored) / template
secrets/                OAuth token store (git-ignored)
```

## Setup / env

Copy `.env.example` → `.env` and fill in credentials. Required keys:
`SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET`, `SCHWAB_CALLBACK_URL`, `FINNHUB_API_KEY`,
`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`.
See `README.md` for the Schwab weekly-login and API-scope commands.
