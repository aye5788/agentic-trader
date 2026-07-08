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

## Strategy foundation

> ✅ **EDGE = MOMENTUM (settled).** The edge is **hybrid dual momentum**
> (cross-sectional rank + absolute-trend filter), long-only, equities-only
> (options OFF), swing horizon. All design axes are closed: 12-month lookback
> (12-0, no skip); relative rank = equal-weight rank-average of risk-adjusted
> return + trend (close/SMA200); absolute gate = 12mo return > 0; hold top-10
> (banded to 15) + top-4 ETF sleeve; weekly rebalance, nightly risk exits;
> off-switch = cash; **70/30 book/sleeve** capital split. The operational spec —
> written for the deployed agent — is [`docs/STRATEGY.md`](STRATEGY.md); params
> are in `config/strategy.toml`. **The PEAD prose in this section below is
> superseded** and kept only for design-history context; earnings-calendar use
> demotes from *primary signal* to *defensive event-awareness*. Infra (Schwab
> price history, Research Store, regime gate, config loader) carries over intact.

**Horizon: swing (multi-day to a few weeks).** Chosen for structural fit, not
regulation — our data is EOD-shaped, the slow loop runs nightly, and the edge
below plays out over days-to-weeks. Day trading is off the table on *data cadence
+ loop design*, **not** PDT: the Pattern-Day-Trader rule was eliminated Jun 4 2026,
and in any case it only ever applied to *margin* accounts. Ours is a **cash
account**, so the sole intraday constraint is **T+1 settlement** — a non-issue for
a days-to-weeks hold.

**Edge: PEAD-anchored (post-earnings-announcement drift).** Horizon ≠ edge —
"swing" is how long we hold, not why we profit. The signal stack, each source with
exactly one job:

| Signal | Role | Source |
| ------ | ---- | ------ |
| **Post-earnings drift (PEAD)** | **primary edge** — beats/misses drift for weeks | Finnhub surprises + RH earnings |
| Estimate-revision momentum | confirming edge | Finnhub recommendation *trends* |
| Price momentum / structure | *timing* of entry & exit | Schwab price history |
| Fundamental quality | *universe filter* (don't swing garbage) | Schwab + Finnhub metrics |
| News / catalysts | context + risk flag | Alpaca news |

**Instrument: equities-first** (fractional / dollar-notional). Options Level 2
(long-only) exists on RH but is **deferred** — theta bleed fights a multi-week
hold; prove the equity logic first, add options later only for tightly-timed
post-earnings plays where drift outpaces decay.

**Validate before trust.** PEAD is well-documented, but *our* implementation isn't
proven until backtested against Schwab history and tracked via outcome feedback
(see Research Store). Lean: backtest the drift signal before it touches live money.

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

### Research Store design

The handoff is the keystone — it's the basis for *every* trade, so it's designed to
stay coherent as history grows past any single context window.

- **Backend: file-memory** (JSON on the droplet) behind a small typed interface
  (`read_current` / `write_product`), so Airtable stays a drop-in later if a UI is
  wanted. Context window = working memory (RAM); the store = long-term memory
  (disk). You never load all of it — only the current watchlist's theses + recent
  fills + guardrails — so nightly context stays **bounded regardless of total
  history**.
- **Two stores, not one:**
  - **Beliefs** — the *current* view; small, curated, always loaded; one
    self-contained record per name.
  - **Journal** — append-only log of every run + fill; grows forever, *never*
    fully loaded (queried / summarized on demand for audit + backtest).
- **The slow loop revises, it doesn't append.** Each run, per name: does new
  evidence confirm / break / not-touch the existing thesis → **update / retire /
  keep**. The store is a *maintained set of beliefs*, not an accumulation. (This is
  what the bull/bear adversarial step is *for*.)
- **Staleness is mechanically detectable, not remembered.** Every record carries
  `as_of` + evidence `provenance` + a `review_by` trigger (for PEAD: "next
  earnings" / "drift window elapsed"), so rot is *visible* without the model
  recalling it.
- **Outcome tracking closes the loop.** Each thesis points to its `outcome` once
  known → confidence is calibrated against reality, not internal consistency.
  Guards against the confirmation-loop failure (entrenching your own past theses).
- **Model-independent:** plain structured data + prose, so any future agent (or a
  human) can pick it up cold — same "repo = single source of truth" principle.

Thesis record (draft): `symbol, rank, verdict, entry_zone, stop, targets[],
target_weight, confidence, thesis (prose), signals{evidence + provenance}, as_of,
review_by, outcome`.

## Layer 1 — Sensing (data sources)

| Source | Role | Status |
| ------ | ---- | ------ |
| **Schwab API** | Fundamentals, price history, options+greeks, movers, quotes (true SIP/NBBO) | ✅ connected (Market Data only) |
| **Finnhub API** | Analyst recommendation *trends*, earnings *surprises*, 133 basic-financial metrics | ✅ connected (free tier) |
| **Alpaca API** | News (live, free, symbol-tagged); movers/screeners via MCP | ✅ connected (news adapter, free tier) |
| **Robinhood MCP** | Fundamentals, **earnings calendar/results**, its own screeners | available |
| **FRED API** | Macro regime indicators (VIX, `T10Y2Y` curve, `BAMLH0A0HYM2` HY spread) + economic release calendar | planned (supplementary; cached + retry) |
| **Web Search / Fetch** | Filings, analyst commentary, macro | available |
| **CryptoQuant / HF** | On-chain, models/papers | available (thematic) |
| **Airtable / Drive** | Structured research DB / store | available |

> **Feed notes.** Alpaca's *price* data is IEX-only on the free plan (single-venue,
> not full NBBO) — so we lean on **Schwab/RH for quotes** and use Alpaca purely for
> **news + screeners**. Finnhub's **price-target** and **forward EPS-estimate**
> endpoints are premium-only; we deliberately skip the price-target *level* (weak,
> biased signal) and treat forward consensus estimates as a deferred maybe.

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

**Gap — now filled (free):** analyst / news sentiment. Schwab can't provide it, so:
- **News** → **Alpaca** (`src/adapters/alpaca/`, `get_news`, live on the free
  tier — verified: symbol-tagged + market-wide feed both return real-time).
- **Analyst signal** → **Finnhub** free tier (verified live): recommendation
  *trends* + earnings *surprises* + 133 basic-financial metrics. Only the
  price-target *level* and forward EPS consensus are paywalled — the former we
  don't want anyway, the latter is a deferred maybe.

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

## Trade management & risk rules (IBD-SwingTrader-derived)

The *signal* is ours (PEAD); the *trade management* is adapted from IBD
SwingTrader, whose entry/exit discipline is edge-agnostic and battle-tested. Each
rule becomes a hard field on the thesis record and/or a governance guardrail:

| Rule | Setting | Where enforced |
| ---- | ------- | -------------- |
| Entry zone | defined buy-price range; never chase above it | `entry_zone`; fast loop only buys if live price in-zone |
| Stop loss | defined + enforced, **volatility-adjusted** (below post-earnings low / ATR mult) — *not* IBD's flat 2–3% (too tight for earnings names) | `stop`; governance auto-exits on breach |
| Profit targets | tiered (IBD ≈ 5% / 10%); scale out | `targets: [t1, t2]` |
| Moving-average exit | exit if close < short-term MA (e.g. 21-day), even if stop not hit | daily fast-loop check (Schwab price history) |
| Position size | ≤ **10% / name** (IBD "full" = 10%; most trades ½–¾) | `target_weight`, capped in guardrails |
| Reward:risk | ≥ **2:1** = (target−entry)/(entry−stop) | **store validation gate** — reject bad-geometry theses on write |

The reward:risk gate is the key one: the store **structurally cannot** accept a
bad-geometry trade, so quality can't silently rot.

## Regime gate (macro) — mechanical floor + agent overlay

Both PEAD and IBD-style swing have **negative expectancy in down/sideways tape**,
so new entries are gated on market regime. The design is **asymmetric**:

- **Mechanical floor = the ON switch (backtestable, non-negotiable).** No new
  entries unless the broad market passes a mechanical trend test (SPX/QQQ > 50-day
  MA, optionally a VIX ceiling). Computed from **Schwab** — the load-bearing gate
  has **no FRED dependency**. Pure code, provable against history, immune to
  narrative.
- **Agent overlay = the OFF switch only (judgment; veto / downsize).** A tiny
  nightly read may *veto or downsize* on context the numbers can't see ("CPI
  tomorrow → stand down"; "macro-driven tape, stock-specific edge suppressed"). It
  **never green-lights on its own** — blocks the "news sounds great at the top"
  failure (sentiment peaks at tops/bottoms).

**Macro is a deterministic compiler, not agent news-reading** (token discipline +
backtestability):

- **`src/adapters/fred/`** (planned) — supplementary macro (VIX daily, curve, HY
  spread). ⚠️ FRED is **"as-is"** (Fed disclaims uptime; occasional throttling;
  daily-close / T-1 data) → wrap in **retry + cache-last-good** so an outage
  degrades to "trend-gate only", never blocks the run. Free key, `.env` as
  `FRED_API_KEY`; rate limit ~120/min (we use a handful nightly).
- **Earnings event calendar** — ✅ built (`src/event_calendar/`). Finnhub REST
  spine (per-symbol, code-callable) cross-checked against an agent-supplied
  **Robinhood snapshot** (RH's `report.verified` = the confirmed/estimated flag).
  Tags `confirmed` vs `estimated` (never infers confirmed from agreement;
  disagreement → estimated + earliest/conservative date), and **logs date
  revisions** (a pushed-back report skews negative). Never reads the clock (caller
  passes `as_of`/`today`) → reproducible + backtestable. Drives both the event-risk
  veto (`reports_within`) and PEAD entry timing (`fresh_reports`). Verified live.
- **Macro event calendar** (planned, with FRED) — FOMC (static list) + FRED release
  dates (CPI/jobs) for the macro-side event veto. Deterministic, zero tokens.
- **v1 = deterministic-only regime** (mechanical floor + calendar). Defer the agent
  qualitative overlay to v2, once the floor has earned trust.

Store the regime call **+ its rationale** each night → auditable, and
outcome-tracking applies to it ("on risk-off nights, did trades do worse?").

## Layer 6 — Orchestration

Cadence engine: scheduled cloud cron / loop / event triggers (earnings, price
moves). Fires the slow and fast loops on their own clocks.

## Deployment (VPS)

The dashed boundary in the diagram = this repo, deployed **headless on a VPS**
(a DigitalOcean droplet). External data providers sit outside it; the runtime
sits inside.

### In plain English

The droplet is just a small always-on computer in a data center. Today the
"worker" is a human + Claude in a chat; on the droplet the work runs **by itself,
on a timer, with nobody watching**. Three things make that work:

1. **The brain runs on the droplet and bills the right wallet.** Claude Code can
   authenticate two ways: the flat-fee **subscription** (what we want) or a
   **pay-per-token API key** (billed separately, per word). We run on the
   subscription. ⚠️ **The footgun:** if an `ANTHROPIC_API_KEY` is set *anywhere*
   on the box, it silently overrides the subscription and you get billed
   per-token. Rule: ensure `ANTHROPIC_API_KEY` is unset everywhere (crontab,
   `.bashrc`, `.env`, systemd).
2. **Robinhood lets the droplet trade.** RH's Agentic Trading is *designed for*
   unattended automation (their own example: "buy $100 of X every time it drops
   2% in a day"). It's a standard **remote HTTP MCP** (`claude mcp add
   robinhood-trading --transport http https://agent.robinhood.com/mcp/trading`),
   so it works from a headless CLI — not a claude.ai-web-only connector.
3. **A timer (cron) kicks it off** on the slow/fast cadences.

### Auth & billing specifics

- **Claude on the droplet (subscription, not API):** run `claude setup-token` on
  a real machine → produces a ~1-year OAuth token → set
  `CLAUDE_CODE_OAUTH_TOKEN` on the droplet → drive with `claude -p "..."` under
  cron. Keeps usage on the plan. Verify `ANTHROPIC_API_KEY` is absent.
- **Robinhood auth:** desktop needed **once** to open the agentic account +
  complete the OAuth paste flow (verify on the RH mobile app). After that,
  ongoing operation is headless. Token lifetime / re-auth cadence is **not
  documented by RH — treat as unknown and watch for it** (like Schwab's 7 days).
- **Schwab headless auth**: OAuth 2.0 manual paste flow (no local browser
  server) — see `scripts/schwab_auth.py`.
- **⚠️ Schwab 7-day token expiry**: Schwab refresh tokens expire every 7 days;
  re-run the auth script weekly to keep an unattended deployment alive.
- **Fair use**: RH explicitly supports automation, and Anthropic is an official
  launch partner for exactly this — so *policy* is not the concern. Plan
  *rate limits* (throughput per window) are real but a pacing problem: space the
  loops out, don't hammer.
- **Liability**: RH disclaims all responsibility for agent decisions/losses —
  "you assume all risk." Fine at $20 demo scale; the right mindset before more.
- **Secrets**: `.env` and `secrets/` are git-ignored; never committed.

## Open decisions

1. ~~**Research Store**~~ — RESOLVED: **file-memory** behind a swappable
   `read_current`/`write_product` interface; Airtable a drop-in later if a UI is
   wanted.
2. ~~**Analyst-data source**~~ — RESOLVED: Finnhub free (recommendation trends +
   earnings surprises) + Alpaca free (news). Paid consensus estimates deferred.
3. ~~**Regime approach**~~ — RESOLVED: mechanical floor (ON, Schwab-computed) +
   agent overlay (OFF-only); **v1 deterministic-only**, agent overlay in v2.
4. ~~**Single edge vs. blend**~~ — RESOLVED (revised): **hybrid dual momentum**
   is the edge (relative rank + absolute trend). PEAD was the first pass and was
   dropped; earnings data demotes to defensive event-awareness. See STRATEGY.md.
5. ~~**Trust before proof**~~ — RESOLVED: **backtest-first** (validate drift vs.
   Schwab history before the $20; seeds outcome-tracking).
6. **Verify depth** — single-pass research vs. full bull/bear/judge adversarial
   layer. *(still open)*
7. ~~**Same-day catalyst entries**~~ — RESOLVED: **nightly-only for v1** (drift
   persists for weeks; same-day reaction is a later add).
8. ~~**Universe**~~ — RESOLVED (revised for momentum): **fixed 150 single names**
   (`config/universe.csv`, human-seed reconciled with dollar-volume fill) **+ an
   18-ETF dual-momentum sleeve** (`config/etf_universe.csv`), run as two parallel
   engines at a **70/30** split. Replaces the PEAD earnings-dynamic screen.
9. ~~**ETF role**~~ — RESOLVED: not ballast — a **parallel dual-momentum rotation
   sleeve** (11 SPDR sectors + broad + intl + defensive); defensive assets rank
   in-sleeve as the built-in off-switch destination.

> **Strategy is codified** in [`config/strategy.toml`](../config/strategy.toml) —
> the single source of truth for risk gates, universe, PEAD signal thresholds,
> trade management, and the regime floor. The `[risk]` table IS the Research
> Store's validation mandate (`strategy.risk_mandate()`).

## Status

- [x] Repo scaffold + Schwab research adapter
- [x] Schwab connected (Market Data), fundamentals verified live
- [x] Full read-only API scope documented
- [x] Analyst/news source — Finnhub adapter connected (free tier verified live)
- [x] Wrap remaining Schwab endpoints — quote, price history, option chain,
      movers, market hours (all verified live)
- [x] Wire Alpaca news into the sensing layer (repo adapter, verified live)
- [x] Strategy foundation decided — PEAD-anchored swing, equities-first, cash acct
- [x] Trade-management + risk rules speced — IBD-derived, vol-adjusted stops, R:R gate
- [x] Regime-gate design — mechanical floor + agent overlay; FRED vetted as supplement
- [x] Build **earnings event calendar** (`src/event_calendar/`) — Finnhub spine +
      RH snapshot normalizer; confirmed/estimated tagging + revision log; verified live
- [x] Build **Research Store** (`src/research_store/`) — file-memory, belief +
      journal, mandate-validated (≤10%/name, R:R ≥ 2:1); verified live
- [x] Codify the strategy into a **mandate config** (`config/strategy.toml`) —
      risk gates + universe + signal + regime; wired into the store; verified live
- [x] Build FRED macro adapter (`src/adapters/fred/`) — VIX / 10y-2y curve / HY
      spread, retry + cache-last-good; verified live
- [x] **Momentum strategy designed + put to paper** — all axes closed; written
      for the deployed agent in `docs/STRATEGY.md`; params in `config/strategy.toml`
- [x] **Universe built** — fixed 150 (`config/universe.csv`) + 18-ETF sleeve
      (`config/etf_universe.csv`), 70/30 split
- [x] **Backtest the momentum signal** vs history — walk-forward harness built
      (`src/momentum.py` signal SSOT, `scripts/fetch_prices.py`, `scripts/backtest.py`)
      and a sensitivity sweep (`scripts/sweep.py`). First pass (2017–2026, 469 wks,
      70/30): CAGR 34.3% vs SPY 13.1%, Sharpe 1.19 vs 0.78, maxDD −31% ≈ SPY −32%.
      Sweep verdict: **edge is robust** (Sharpe 0.97–1.33 across 16 configs, all beat
      SPY). ⚠️ **survivorship-biased upper bound** (today's 150 names run over history)
      + no intra-week stops modeled — NOT a forecast yet.
- [ ] **Point-in-time universe rebuild** — kill survivorship bias ← next. Dead-name
      price history is free/available (Alpaca IEX bars serve delisted tickers, e.g.
      SIVB/BBBY); the open problem is reconstructing historical liquid-universe
      membership (which ~150 names were most-traded as-of each date, incl. since-dead).
- [ ] Macro event calendar (FOMC/CPI dates) — deterministic event-risk feed
- [ ] Slow loop (research → theses) + fast loop (theses → sized orders)
- [ ] Governance + orchestration (incl. mechanical regime floor)
- [ ] VPS deployment
