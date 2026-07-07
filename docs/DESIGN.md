# agentic-trader — Design

A research → execution system for agentic trading. Robinhood is only the thin
execution "hands"; everything valuable is the research stack upstream of it.

![Architecture](architecture.svg)

> Diagram source: [`architecture.excalidraw`](architecture.excalidraw) (open at
> [excalidraw.com](https://excalidraw.com/)) · last live export baked into the SVG above.

---

## Core idea

Trading is the easy part. The edge is in **informed decisions**, so the system is
layered and the broker is deliberately dumb:

```
LAYER 6  ORCHESTRATION   cadence engine: schedule / loop / event triggers
LAYER 5  GOVERNANCE      mandate + journal + guardrails + kill-switch  (memory)
────────────────────────────────────────────────────────────────────────
LAYER 3  DECISION        portfolio construction: rank -> weight -> size to $
LAYER 2  RESEARCH        turn raw data into a *view*
LAYER 1  SENSING         multi-source data ingestion
────────────────────────────────────────────────────────────────────────
LAYER 4  EXECUTION       Robinhood MCP: review -> place  (thin, dumb, reliable)
```

## The two-clock model (the key decoupling)

Do **not** run deep research at trade time. Split into two cadences that hand off
through a persisted **Research Store**:

```
  SLOW LOOP (nightly / weekly)              FAST LOOP (at trade time)
  - fan-out research (per lens)             - read stored theses + guardrails
  - adversarial bull/bear verify            - pull live quotes
  - synthesize ranked conviction list  -->  - diff target vs current portfolio
    + theses + target weights               - size to $ notional
        stored in Research Store            - review -> place -> journal fills
```

- **Slow loop** does the expensive thinking and writes a *research product*.
- **Fast loop** is cheap and deterministic; it only acts on that product.
- **Research Store** = the handoff (file memory or Airtable): ranked watchlist +
  written theses + target weights.

## Layer 1 — Sensing (data sources)

| Source | Role | Status |
| ------ | ---- | ------ |
| **Schwab API** | Fundamentals, price history, options+greeks, movers, quotes | ✅ connected (Market Data only) |
| **Alpaca MCP** | News, movers, bars, snapshots | available |
| **Robinhood MCP** | Fundamentals, earnings, its own screeners | available |
| **Web Search / Fetch** | Filings, analyst commentary, macro | available |
| **CryptoQuant / HF** | On-chain, models/papers | available (thematic) |
| **Airtable / Drive** | Structured research DB / store | available |

### Schwab scope (verified empirically — see `scripts/schwab_scope_full.py`)

**Available (Market Data API, HTTP 200):**
`quotes`/`quote` (realtime NBBO + field groups: quote, fundamental, extended,
regular, reference), `instruments` (`fundamental`, `symbol-search`, regex/desc
projections), `price_history` (OHLCV, minute→monthly), `option_chains` (call/put
maps **with greeks**, IV, rates), `option_expiration_chain`, `movers`
(`$DJI`/`$SPX`/`NASDAQ`), `market_hours`.

**NOT available:**
- ❌ **Accounts & Trading API** — every account/order/transaction call returns
  `HTTP 401 "Client not authorized"`. The app is **data-only by credential**, so
  it can never trade. (This is desirable — execution lives on Robinhood.)
- ❌ **Analyst ratings / price targets / research reports** — no such endpoint
  exists in the Schwab developer API at all. Fundamentals yes, analysts no.

**Open gap:** analyst / news sentiment. Must come from web/news or a vendor
(e.g. Finnhub, FMP) — Schwab cannot provide it.

## Layer 4 — Execution (Robinhood)

- Trades only in the dedicated **"Agentic" account** (`agentic_allowed=true`);
  every other Robinhood account is read-only to the agent.
- Currently funded with **$20** (demonstration scale).
- Robinhood enforces: per-trade push notifications, live activity/P&L feed,
  one-tap disconnect. Options are Level 2 there (long only), so equities +
  fractional/dollar-notional orders are the practical surface.

## Layer 5 — Governance

Mandate (universe, rules, weights), journal (every run + fills), guardrails
(max % per name, min/max holdings, halt-on-drawdown, whitelist universe), and a
kill-switch. Persisted so it survives across runs.

## Layer 6 — Orchestration

Cadence engine: scheduled cloud cron / loop / event triggers (earnings, price
moves). Fires the slow and fast loops on their own clocks.

## Deployment (VPS)

The dashed boundary in the diagram = this repo, deployed **headless on a VPS**.
External data providers sit outside it; the runtime sits inside.

- **Headless auth**: Schwab uses OAuth 2.0 with a manual paste flow (no local
  browser server needed) — see `scripts/schwab_auth.py`.
- **⚠️ 7-day token expiry**: Schwab refresh tokens expire every 7 days. The auth
  script must be re-run weekly to keep an unattended deployment alive. This is
  the single biggest operational constraint.
- **Secrets**: `.env` and `secrets/` are git-ignored; never committed.

## Open decisions

1. **Research Store** — file memory (simple, private) vs. Airtable (structured, UI).
2. **Verify depth** — single-pass research vs. full bull/bear/judge adversarial layer.
3. **Analyst-data source** — web/news scraping vs. a ratings vendor.

## Status

- [x] Repo scaffold + Schwab research adapter
- [x] Schwab connected (Market Data), fundamentals verified live
- [x] Full read-only API scope documented
- [ ] Wrap remaining Schwab endpoints (price history, options, movers, quotes)
- [ ] Analyst/news source
- [ ] Research store + slow/fast loops
- [ ] Governance + orchestration
- [ ] VPS deployment
