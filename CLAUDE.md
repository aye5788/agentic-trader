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
   - **Finnhub**, **Alpaca**, **moomoo**, and **FRED** are read-only data
     providers here. No trading surface is used. (moomoo's API *can* trade, but
     this repo uses only its **data quote channel**; `unlock_trade` is never
     scripted here — see `docs/DATA_SOURCES.md`.)
3. **This is LIVE money.** The Agentic account holds real funds. Real orders move
   real money. Prefer the review → place pattern; respect any human-approval /
   guardrail config.
   ⛔ **Never anchor on the account balance.** Do not treat it as small, do not
   write "at this scale…" / "only a demo account…", and never use the size of the
   book to discount a security, correctness, or risk concern. Judge every risk on
   its *mechanism and consequence* — what can go wrong, how far it reaches, whether
   it is reversible — not on the stake. (This file previously stated a fixed
   figure; it was stale, and agents were anchoring on it to dismiss real risks.)
   If a task genuinely needs the balance, read it live from the snapshot via
   `src/marks.py` — never from memory or from any number written in the docs.
4. **Secrets never leave the box.** `.env` and `secrets/` are git-ignored — never
   commit them, never print their contents, never paste a key/token into chat.

---

## Data sources and their roles

Full scope + **verified moomoo surface** is in [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md);
architecture in `docs/DESIGN.md` (Layer 1). Summary:

| Source | Role | Notes |
| ------ | ---- | ----- |
| ~~**Schwab**~~ | **REMOVED 2026-07-29.** Was the primary market-data feed; its 7-day refresh token was the only recurring human chore in the system, and the signal consumed nothing from it but daily closes. All of it now comes from moomoo. Adapter, auth scripts, `SCHWAB_*` keys and `schwabdev` are deleted — do not reintroduce. | Migration + equivalence proof: `docs/OPSLOG.md` 2026-07-29. |
| **moomoo** (`src/adapters/moomoo/`) | **THE market-data feed** since 2026-07-29 — daily OHLC panel (`prices.snapshot_ohlc`), intraday quotes for the stop watcher (`prices.live_quotes`), universe turnover/market-cap, capital-flow, short-interest, put/call+IV. Data-only via the local **OpenD** gateway. Still unwired: insider, earnings-price-move, institutional; see `docs/DATA_SOURCES.md`. | ⚠️ Runs under **system `/usr/bin/python3` (3.10)**, NOT `.venv` (3.12) — this now includes `fetch_prices`, `market_monitor`, `fast_loop` and `risk_review`. OpenD on `127.0.0.1:11111`, **shared with sibling repo `moomoo-vol-desk`**. ⛔ `request_history_kline` is capped at **100 distinct stocks account-wide** — use `get_market_snapshot` (unmetered, 400/call) for anything universe-wide. moomoo history is **shallow (~1–2 yr)** → forward-log, don't backtest; the deep panel on disk is Schwab-era and now **non-regenerable**, so `research_store/prices/backup/` matters. |
| **Finnhub** (`src/adapters/finnhub/`) | Analyst *recommendation trends*, earnings *surprises*, basic financials. **NOT retired** — but currently consumed only by `src/event_calendar/` (earnings spine), not the ranking signal. | Free tier. Price-target *level* + forward EPS estimates are **premium (403)**. |
| **Alpaca** (`src/adapters/alpaca/`) | Symbol-tagged news; IEX close+$-vol for the survivorship-free **PIT pool** (only free feed serving DELISTED names). | Free tier. Price is **IEX-only (not NBBO)** → don't use for quotes. |
| **FRED** (`src/adapters/fred/`) | Macro regime indicators: VIX (`VIXCLS`), 10y-2y curve (`T10Y2Y`), HY OAS (`BAMLH0A0HYM2`). **Deep history (decades).** Confirms — does not replace — the momentum regime gate. Also the VIX source for `slow_loop.fetch_vix` and `risk_review`. | Needs `FRED_API_KEY`. |
| **Robinhood** (MCP) | **Execution** + its own fundamentals/earnings | The only execution venue. Agentic account only. |

---

## ⚙️ Runtimes, OpenD & sibling repos on the box  (READ — easy to get wrong)

- **Two Python runtimes.** The core system runs under the repo **`.venv` (Python
  3.12)**. The **moomoo SDK is installed ONLY in system `/usr/bin/python3`
  (3.10)** — so anything importing `moomoo` MUST run under `/usr/bin/python3`
  (`deploy/run_universe_refresh.sh` does this deliberately). A `.venv` script
  **cannot** `import moomoo`.
- **OpenD gateway.** moomoo data flows through a local **OpenD** daemon on
  `127.0.0.1:11111` (`opend.service`), **shared** with the sibling repos — never
  launch a second one. Data needs only the quote channel (`qot_logined: True`).
- **Sibling repos on the SAME droplet — NOT this project, don't conflate:**
  `~/moomoo-vol-desk` (a separate options/vol trading system with its own MCP +
  cron; owns the OpenD login), `~/moomoo-data-collector` (15m K-line for ~3–5
  index/vol symbols → feeds the vol-desk; **not** our signal panel),
  `~/time-spread-lab` (resident Streamlit options app). The droplet is
  memory-tight (~2 GB, often swapping); the heavy consumers are headless `claude`
  runs (~500 MB each) across all these. Keep new on-box work pure-Python and off
  the weekday market-hours pileup — prefer off-box (GitHub Actions).

## Adaptive-input layer (self-tuning strategy knobs)

An **off-box** background learner tunes `strategy.toml` knobs from the Decision→
Outcome Ledger. It NEVER trades — it emits a bounded PROPOSAL a human promotes.
- **Dial #1 (live): `stop_atr_mult`.** `scripts/tune_stop.py` (Bayesian grid +
  smoothness prior, `src/adaptive.py`; replay via `src/stop_replay.py`) runs
  weekly on **GitHub Actions** (`.github/workflows/adaptive-tune.yml`), reads
  OHLC+journal from the ledger mirror, writes a proposal. Review it in the Actions
  run; apply with `scripts/promote_proposal.py --apply` / `--set VALUE`, which
  writes **`config/strategy.adaptive.toml`** (git-ignored) that `strategy.load()`
  merges UNDER `strategy.local.toml` (**human always overrides the learner**).
  Band-guarded, provenance-stamped, journalled as `adaptive_apply`.
- **Methodology rule:** deep-history signals (price → residual momentum, from the
  survivorship-free pool) are **backtested** dials; shallow-history signals
  (all moomoo edges) are **forward-logged** into the ledger and validated
  prospectively (meta-labeling) — never backtested on 1 yr. Spec:
  `docs/superpowers/specs/2026-07-23-adaptive-input-layer-design.md`.

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
src/adapters/finnhub/   Finnhub analyst/estimates client + research (event-calendar spine)
src/adapters/moomoo/    Data-only moomoo client via OpenD — RUNS UNDER SYSTEM
                        /usr/bin/python3 (3.10), not .venv. prices.py is THE market
                        feed since 2026-07-29 (snapshot_ohlc = daily panel append,
                        unmetered; live_quotes = intraday marks; daily_panel =
                        backfill, capped at 100 distinct stocks account-wide).
                        Consumers: fetch_prices, market_monitor, fast_loop
                        (no_chase), risk_review — ALL now on system python3.
                        research.py serves scripts/universe_refresh.py (snapshot_turnover,
                        screen_top_marketcap, candidate_pond — quarterly universe
                        maintenance; ⚠️ has NEVER once run, next window Oct 1-7)
                        and scripts/collect_signals.py (capital_flow_daily,
                        short_interest, option_overview, snapshot_fields — the
                        weekly Sun 20:15 forward-log signal panel; running since
                        2026-07-26). All 7 research.py functions are consumed.
                        Still unwired (no adapter fn): insider, earnings-price-
                        move, institutional. ⚠️ capflow_bignet_20d silently nulls
                        for EVERY ETF and does NOT register in `gaps` — so a
                        regime-off (all-ETF) panel is 100% empty while reporting
                        clean. See docs/DATA_SOURCES.md + OPSLOG 2026-07-28.
src/adapters/fred/      Macro regime indicators (VIX, 10y-2y, HY spread) — deep
                        history; confirms the momentum regime gate. ALSO the VIX
                        source for slow_loop + risk_review. Needs FRED_API_KEY.
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
scripts/hooks/pretooluse_order_gate.py
                        THE UNBYPASSABLE ORDER GATE — a Claude Code PreToolUse
                        hook on mcp__robinhood-trading__place_equity_order,
                        wired in deploy/loop_settings.json (the LOOPS only, not
                        interactive sessions). Runs in the harness, so the gate
                        no longer depends on the agent remembering to call
                        check_order(). Calls only WRITE-FREE governance
                        (kill_switch_active/halt_entries_active/live_approved/
                        vet_plan) — ⛔ never gates(), which WRITES the drawdown
                        peak via update_peak and would ratchet a live gate on
                        every order. A SELL is refused by NOTHING but the kill
                        switch. Fails CLOSED on any exception. `touch
                        research_store/SHADOW` = deny every order while the loop
                        still runs (phased-rollout switch); rm to go live. Small
                        JSON + config only — NEVER the price panel (~0.1s
                        budget, on the critical path of every order).
scripts/market_monitor.py Intraday stop/take-profit watcher. Polls moomoo quotes
                        during RTH vs. each holding's stored stop/targets; fires
                        prompts/exit.md (market sell) on a breach. Pure-Python
                        watching, Claude only on an event. Runs as a systemd
                        service; alert-only unless live_approved. [monitor] config.
scripts/session.py      THE SESSION RUNNER — the inversion's entry point. LIVE since
                        2026-08-12 (cron: open 10:35, close 15:15 ET weekdays,
                        via deploy/run_session.sh). Starts ONE agent session and
                        outlives it: signal handlers -> session lock -> BUILD
                        BRIEF (facts gathered AFTER the lock, never before) ->
                        integrity snapshot -> spawn -> finally kill the process
                        GROUP, verify integrity, release. classify() exists
                        because `ok = bool(output)` logs a dead session as a
                        successful one — a headless claude that dies on a 529
                        prints its banner to STDOUT and exits 0. should_retry()
                        is deliberately narrow: a retry re-runs a session that
                        may have ALREADY PLACED ORDERS.
                        ⚠️ Unlike the legacy loops, a session is NOT handed a
                        procedure — it gets prompts/charter.md and decides.
scripts/review_session.py
                        THE INDEPENDENT REVIEW — a DIFFERENT model (Codex)
                        judges what the session just did. Runs SEQUENTIALLY
                        after session.py in deploy/run_session.sh, never beside
                        it (two headless model runs at once took the droplet
                        down 2026-08-12) and never during RTH; capped by
                        systemd-run MemoryMax=700M + nice/ionice, because a
                        review is the LEAST important process on this box — it
                        judges work that already happened and must never starve
                        the stop watcher. It is DATA-ONLY: it cannot place,
                        cancel, set a level or record a decision. Its verdict is
                        journalled (`codex_review`), pushed on DISSENT/SPLIT,
                        and injected ANONYMOUSLY into the next open/close brief,
                        which must answer it. NOT a veto — no second model sits
                        on the critical path of an order.
                        ⚠️ NEEDS THE `codex` BINARY (/root/.local/bin) — see
                        Setup. ⛔ Parse the LAST ===VERDICT=== block, never the
                        first: our own prompt echoes the template back. A
                        non-zero exit with no block = the reviewer NEVER RAN =
                        a failure, NOT a stance — it journals nothing and leaves
                        the last real verdict standing (OPSLOG 2026-08-13).
scripts/score_reviews.py
                        Scores BOTH the agent and its reviewer against the tape
                        `horizon` days later — pure pandas, no model, neither
                        party grades itself. This is what stops the reviewer
                        being decoration: a verdict nobody prices is just a
                        second opinion. `contested` (only decisions the reviewer
                        disputed) is the column that matters. -> reviews/
                        scorecard.json.
prompts/review.md       The reviewer's procedure. Phase 1 = form its OWN view
                        before reading the agent's reasoning (which is handed
                        over BY PATH, never inlined, so it cannot anchor).
prompts/charter.md      THE SESSION CHARTER — rendered from config by
                        src/charter.py (mandate.toml + strategy.toml + the live
                        MCP tool list), never hand-copied. A literal threshold in
                        the template is a DEFECT; check_charter_no_literals
                        enforces it.
prompts/fast_loop.md    The headless-Claude execution procedure (RH is MCP-only).
                        Includes step 7b: post-take-profit re-entry judgment
                        (full/half/skip — veto/downsize ONLY; [reentry] config,
                        hard 4% knife-guard lives in fast_loop.py, not judgment).
prompts/exit.md         Exit-executor procedure — market-sell the breached
                        positions the monitor flags, journal, reconcile the
                        snapshot + realized-P&L after selling.
src/notify.py           THE ntfy phone-push helper (push(); never raises; no
                        NTFY_TOPIC in .env -> no-op). Used by market_monitor
                        (stop/target alerts) and record_fills (trade placed/
                        skipped summaries). deploy/alert.sh mirrors it in shell.
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
                        any cron failure). See docs/DEPLOY.md.
src/health.py           SCHEDULED-JOB LIVENESS — did each moving part actually run?
                        Every job leaves an artifact (book/log/journal event/commit);
                        a job that stopped running = an artifact that stopped moving.
                        evaluate() pure + selftested, gather() thin I/O. Built after
                        the signal panel was found to have NEVER run (2026-07-24) —
                        a job that never runs can't fire its own alerts.
scripts/health_check.py THE UPKEEP REMINDER (daily 08:00). Runs health.checks() and
                        pushes anything unhealthy to the OPS ntfy topic. FIRE-ONCE
                        per condition (clears silently on heal); the dashboard
                        "Scheduled jobs" card carries standing status. Owns the
                        NO credential reminder any more — the Schwab 7-day token
                        was the only one and it is gone; every check is now
                        "did this job leave evidence it ran".
                        `--open-issue` also routes findings into the oversight
                        loop below (deduped `bug`+`auto-fix` GitHub issue).
src/repo_checks.py      Static repo-state validator — FIVE filesystem-only checks
                        (cron paths, scheduled-jobs-armed, workflow exit-0,
                        workflow swallowed-failures, no-api-key) that catch
                        config/CI drift no exception ever throws. Pure, no
                        network/secrets. Run: `python3 src/repo_checks.py`.
.github/workflows/validate.yml  Off-box (GitHub Actions) oversight detector —
                        TWO independent jobs: `deadman` (droplet dead-man's
                        switch via ledger-mirror freshness, 72h — the ONE
                        check that survives the droplet dying) and `checks`
                        (runs src/repo_checks.py). Each files/updates its own
                        deduped `bug`+`auto-fix` GitHub issue; `deadman` also
                        phones NTFY_TOPIC_OPS on droplet death.
.github/workflows/claude.yml  The agent half of the oversight loop — fires on
                        a GitHub issue carrying BOTH `bug` and `auto-fix`
                        labels, runs a 6-stage protocol (triage ops-vs-code,
                        minimal fix, MANDATORY adversarial self-review,
                        re-verify, open a PR written for a non-coder) and
                        opens a PR against `main` (never pushes to it). Also
                        an `interactive` job for @claude mentions. INERT until
                        CLAUDE_CODE_OAUTH_TOKEN exists as a repo secret.
src/momentum.py         THE SIGNAL — single source of truth for the ranking math
                        (docs/STRATEGY.md §3). compute(panel, asof, lookback=252)
                        -> per-ticker R/sigma/trend/score/eligible/rank; select()
                        (banded); regime_on() (SPY>50DMA). Backtest AND live loop
                        both call this so the numbers never drift. Pure, no I/O.
scripts/fetch_prices.py APPENDS the current session's OHLC row to the cached panels
                        in research_store/prices/ (git-ignored) from moomoo. ONE
                        get_market_snapshot call for the whole universe, ~0.2s, no
                        quota. RUNS UNDER /usr/bin/python3. `--force` is GONE — the
                        panel is appended to, never re-pulled (history is capped at
                        100 distinct stocks, so a 168-name re-pull cannot succeed;
                        that also makes the deep panel non-regenerable → backup/).
                        `--backfill N` gap-fills via the metered history API.
                        _drop_unsettled_session() still discards the current row
                        before 16:15 ET (a snapshot mid-RTH is a PARTIAL bar whose
                        close is just the last trade) and keeps it after — that
                        guard predates the feed switch and is unchanged.
                        risk_review._gather_highs now reads highs.parquet + folds in
                        today's session high from a live snapshot.
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
                        apply_chase_guard() enforces [trade_management] no_chase:
                        skips a BUY priced above entry_zone[1] by more than
                        chase_tol_sigma × sigma. ASYMMETRIC (cheaper never blocks
                        — the old doc wording would have refused better fills),
                        vol-scaled, FAILS OPEN. Wired 2026-07-28 after being
                        documented-but-dead since the start; see docs/OPSLOG.md.
src/adaptive.py         ADAPTIVE CORE — dial-agnostic Bayesian grid estimator
                        (smoothness prior + uncertainty-gated recommendation +
                        oos_gap). Pure. Reused by every adaptive dial.
src/stop_replay.py      Pure stop-aware single-position replay → realized-R (models
                        intra-week stop/target/horizon exits the backtest lacks).
scripts/tune_stop.py    Adaptive tuner for stop_atr_mult — off-box weekly on GitHub
                        Actions. PIT-replay + live → bounded proposal artifact.
scripts/promote_proposal.py  Human promotion of a proposal: --apply / --set writes
                        config/strategy.adaptive.toml (merged UNDER strategy.local.toml).
src/research_store/     Research Store — validated slow→fast handoff (belief +
                        journal). write_product enforces the [risk] mandate.
src/adapters/alpaca/    Alpaca news client + get_news (data-only, no trading)
src/event_calendar/     Earnings/event calendar compiler (timing + risk spine).
                        Deterministic: Finnhub REST spine + optional agent-supplied
                        RH snapshot; tags confirmed/estimated, logs date revisions.
                        (Named `event_calendar`, NOT `calendar` — avoids shadowing
                        the Python stdlib module, which breaks imports.)
src/agent_env/          THE AGENT'S ENVIRONMENT (Plan 2 of the inversion) — a
                        FastMCP server exposing what the agent can SEE and DO:
                        brief(), positions(), account(), mandate_status(),
                        candidates(n)/universe(), terrain(symbol),
                        set_levels(sym,stop,target,reason), record_decision(),
                        check_order(). Runs under .venv (3.12) with mcp==1.28.1;
                        needs NO moomoo (quotes reach disk via the monitor).
                        Registered in .mcp.json. NOT yet used by any cron job —
                        the live loops still run the old procedural prompts.
                        server.py = tools; state/screen/terrain/decide = pure
                        helpers, each selftested.
scripts/                One-off + weekly auth and API-scope probe scripts
docs/OPSLOG.md          Dated ops & maintainer log (newest first). Technical/
                        plumbing material goes HERE, never in the investor
                        letter (principal's standing instruction, 2026-07-10) —
                        the letter states portfolio impact in ≤1 sentence and
                        points here. Newsletter run appends entries (step 3b).
docs/STRATEGY.md        THE TRADING STRATEGY, written for the deployed agent —
                        dual momentum: exact signal math, portfolio rules, the
                        fast-loop execution procedure, guardrails, proof gate.
                        Read this before trading. (edge = momentum, options OFF)
docs/OPERATOR_MANUAL.md THE human-operator manual — step-by-step for every task the
                        principal (Aaron) does: reviewing/
                        applying adaptive proposals, phone-alert meanings, the kill
                        switch, emergency stop. Start here for "what do I do".
docs/DESIGN.md          Full architecture (6 layers, two-clock model, scope tables)
docs/architecture.*     The architecture diagram (svg + excalidraw source)
.env / .env.example     Credentials (git-ignored) / template
secrets/                OAuth token store (git-ignored)
```

## Setup / env

Copy `.env.example` → `.env` and fill in credentials. Keys:
`FINNHUB_API_KEY`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `FRED_API_KEY`.
(The `SCHWAB_*` keys are **gone** — removed with the adapter 2026-07-29.)
- **moomoo** has **no `.env` key** — it authenticates through the running **OpenD**
  gateway (`OpenD.xml`, gitignored, on the box); import needs `/usr/bin/python3`.
- **GitHub Actions** (adaptive tuner) uses repo secret **`LEDGER_TOKEN`** (a
  fine-grained PAT with *read* on the `agentic-trader-ledger` mirror).
Market data needs no key: moomoo authenticates via OpenD. There is no longer any
weekly credential step — that was Schwab's, removed 2026-07-29.

- **`codex`** (the independent reviewer, `scripts/review_session.py`) is a
  BINARY dependency, not a Python one: it lives in **`/root/.local/bin`** and
  authenticates through its own `/root/.codex/auth.json` (no `.env` key). That
  dir is NOT on a default cron PATH — it is appended explicitly in
  `deploy/crontab.template`, and its absence silently killed every cron review
  until 2026-08-13 while interactive runs all passed.
  ⚠️ **Check any new binary dependency the way cron sees it**, never with
  `which` in your own shell — a login shell's PATH is not cron's:
  `env -i PATH="$(crontab -l | grep '^PATH=' | cut -d= -f2-)" command -v <bin>`
