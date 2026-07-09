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
                        Edge = momentum (settled); [signal]/[meta]/[portfolio] all
                        momentum. PEAD is fully retired — see docs/DESIGN.md.
config/universe.csv     Fixed 150-name momentum universe (human-seed reconciled
                        with dollar-volume liquidity fill). `flag` col marks
                        adr/micro/spec/fresh-ipo model-caveats. Referenced by
                        [universe] in strategy.toml.
config/etf_universe.csv 18-ETF dual-momentum rotation sleeve (11 SPDR sectors +
                        broad + intl + defensive). A SECOND parallel engine to the
                        single-name book; defensive assets rank in-sleeve as the
                        built-in off-switch. Referenced by [etf_sleeve].
src/strategy.py         Strategy-config loader (tomllib) + risk_mandate()
src/governance.py       Layer-5 guardrails: kill-switch file, drawdown halt,
                        per-order cap, universe whitelist, live_approved master
                        switch. The last gate before a live order (fast loop).
scripts/market_monitor.py Intraday stop/take-profit watcher. Polls Schwab quotes
                        during RTH vs. each holding's stored stop/targets; fires
                        prompts/exit.md (market sell) on a breach. Pure-Python
                        watching, Claude only on an event. Runs as a systemd
                        service; alert-only unless live_approved. [monitor] config.
prompts/fast_loop.md    The headless-Claude execution procedure (RH is MCP-only).
                        Includes step 7b: post-take-profit re-entry judgment
                        (full/half/skip — veto/downsize ONLY; [reentry] config,
                        hard 4% knife-guard lives in fast_loop.py, not judgment).
prompts/exit.md         Exit-executor procedure — market-sell the breached
                        positions the monitor flags, journal, reconcile the
                        snapshot + realized-P&L after selling.
src/marks.py            Position valuation — the ONE place snapshot positions
                        become dollars (qty × freshest mark: monitor quote >
                        snapshot last > cost). Dashboard, log_equity, and the
                        fast-loop diff all read through it. Snapshot schema
                        documented here.
dashboard/app.py        Flask monitor (127.0.0.1:8787), renders live from the
                        Research Store; password-gated (DASH_USER/DASH_PASS in
                        .env, fail-closed). Live at dash.ethobs.uk via cloudflared
                        tunnel. dashboard/dashboard.html = the page template.
scripts/log_equity.py   Appends a daily equity point (marked via src/marks.py) to
                        research_store/history/equity.jsonl — the dashboard curve.
                        Run by run_fast_loop.sh each day.
prompts/newsletter.md   Weekly investor letter ("The Claude Ledger") — headless
                        Claude narrates ONLY from letter_facts.py's facts.json
                        (never computes numbers), fills newsletter/template.html,
                        Sundays 21:00. scripts/send_newsletter.py delivers via
                        Resend HTTPS API (DO blocks ALL outbound SMTP ports).
deploy/                 run_slow_loop.sh (Python), run_fast_loop.sh (Claude, with
                        ANTHROPIC_API_KEY guard), run_newsletter.sh,
                        crontab.template, alert.sh (ERR-trap → ntfy phone push on
                        any cron failure), reauth_reminder.sh. See docs/DEPLOY.md.
src/momentum.py         THE SIGNAL — single source of truth for the ranking math
                        (docs/STRATEGY.md §3). compute(panel, asof, lookback=252)
                        -> per-ticker R/sigma/trend/score/eligible/rank; select()
                        (banded); regime_on() (SPY>50DMA). Backtest AND live loop
                        both call this so the numbers never drift. Pure, no I/O.
scripts/fetch_prices.py Cache ~10y daily closes (names+ETFs+SPY) from Schwab to
                        research_store/prices/ (git-ignored). Rate-limited 0.6s;
                        needs pyarrow (CSV fallback if absent).
scripts/backtest.py     Weekly walk-forward sim of the 70/30 book/sleeve vs SPY.
                        simulate() = parameterized core returning a metrics dict.
                        ⚠️ survivorship-biased (today's names over history) = upper
                        bound; no intra-week stops modeled. Point-in-time rebuild
                        is the pending fix (see docs/DESIGN.md status).
scripts/sweep.py        One-knob-at-a-time sensitivity sweep. Verdict: edge robust
                        (Sharpe 0.97-1.33 across 16 configs). Read the spread, not
                        the level — every row carries the same survivorship caveat.
scripts/build_pool.py   Assemble config/pit_pool.csv (816) = S&P-500 point-in-time
                        members + our 150 + ETFs + hand-listed dead names. The
                        survivorship-free candidate pool for the PIT backtest.
scripts/fetch_pool.py   Pull close + dollar-volume for the pool from Alpaca IEX
                        (only free feed serving DELISTED names). 2020-07 floor;
                        IEX volume = noisy-but-consistent liquidity proxy.
scripts/backtest_pit.py SURVIVORSHIP-CORRECTED backtest: ranks top-150 by trailing
                        $-vol AS OF each date from the pool (dead names included).
                        Honest result: CAGR 23.2% / Sharpe 0.97 vs SPY 12.4%/0.80
                        (2021-2026). This is the number to trust, not backtest.py's
                        biased 34%. held-into-death=0 (risk framework works).
config/pit_pool.csv     816-name survivorship-free pool (ticker, source, in_sp500_ever).
scripts/slow_loop.py    SLOW LOOP (deterministic brain, no LLM, no trading): momentum
                        signal → top-10 book + top-4 sleeve → IBD trade geometry
                        (vol-scaled targets, R:R≥2) → write validated book to the
                        Research Store. Regime-off/nothing-eligible → cash is valid.
scripts/fast_loop.py    FAST LOOP diff core: stored targets vs. RH holdings →
                        dollar-notional buy/sell plan. Enforces the one-Agentic-
                        account guardrail. NEVER places — placement is the agent's
                        review_equity_order→place_equity_order MCP step, gated by
                        the proof gate. --selftest covers the diff logic.
src/research_store/     Research Store — validated slow→fast handoff (belief +
                        journal). write_product enforces the [risk] mandate.
src/adapters/alpaca/    Alpaca news client + get_news (data-only, no trading)
src/event_calendar/     Earnings/event calendar compiler (timing + risk spine).
                        Deterministic: Finnhub REST spine + optional agent-supplied
                        RH snapshot; tags confirmed/estimated, logs date revisions.
                        (Named `event_calendar`, NOT `calendar` — avoids shadowing
                        the Python stdlib module, which breaks imports.)
scripts/                One-off + weekly auth and API-scope probe scripts
docs/STRATEGY.md        THE TRADING STRATEGY, written for the deployed agent —
                        dual momentum: exact signal math, portfolio rules, the
                        fast-loop execution procedure, guardrails, proof gate.
                        Read this before trading. (edge = momentum, options OFF)
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
