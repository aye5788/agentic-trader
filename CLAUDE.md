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
   **Account shape (changed 2026-08-18): `type: limited_margin`, was `cash`.**
   Sale proceeds are spendable the SAME SESSION — there is no T+1 wait and no
   settlement deferral any more. Limited margin settles proceeds; it does not
   lend: the broker reports `unleveraged_buying_power` equal to `buying_power`,
   so there is no leverage to draw on, no short leg, and `option_level` is empty
   (options are structurally absent, not merely disallowed). Size against
   `buying_power` — it is what an order is checked against — and read it live,
   never from memory or from a number written in these docs.
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

### Active incident state (2026-08-19)

The system experienced a serious execution/retry incident and is intentionally
HALTED while it is reconciled. If `research_store/HALT` exists:

- Do not remove it, place orders, or start sessions manually.
- `deploy/run_session.sh` must refuse to launch any model session.
- The scheduled Codex review is intentionally disabled in that wrapper because
  it consumes a second model budget and is advisory only.
- `agentic-monitor.service` and the open/close session timers may be disabled
  during the recovery window. The controlled resume path is
  `deploy/resume_after_halt.sh`; it must be used only after the operator has
  reviewed the broker state and incident log.

The root execution bug was a missing `research_store/monitor/exit_result.json`
after an exit executor placed an order but failed during Claude exhaustion or
post-order bookkeeping. The monitor treated the unknown outcome as a normal
failure and retried a fractional target against the reduced current quantity,
which can sell the same position repeatedly. `scripts/market_monitor.py` now
pauses a symbol when the result file is absent; it must never automatically
retry that unknown outcome. Reconcile against Robinhood first.

On this incident, MRK target-1 was retried and 75% of the original position was
sold; the remaining `0.011057` shares were retained and levels were repaired.
Later, the scheduled open session bought FTNT (`0.039290` shares at `$152.71`,
order `6a85c11a-5ff0-422c-b525-8ad9075ea820`). This was a real session decision,
not a Codex review action. Do not infer authorization from the fact that a
session was scheduled: while HALT is present, no session may launch.

Full chronology and recovery steps are in `docs/OPSLOG.md` under
`2026-08-19 — execution retry / unexpected order incident`.

---

## ⛔ A PASSING SELFTEST IS NOT EVIDENCE YOUR CHANGE IS RIGHT  (agents: read this)

`--selftest`, `src/repo_checks.py` and friends verify that NUMBERS ARE DERIVED
and that ASSERTIONS THAT ALREADY EXISTED still hold. They cannot tell you whether
what you wrote is TRUE, whether it CONTRADICTS something 200 lines away, or
whether it says a thing the document already said. They passed on every defect
listed below.

⛔ **DO NOT report a change as verified because a suite passed, and do not run one
in place of reading.** Instruction text — CLAUDE.md, prompts/charter.md,
src/charter.py, docs/ — has NO meaningful automated check at all: the only one
that exists asserts that literal thresholds are not hardcoded. That is spelling,
not meaning.

**What is required instead: READ THE WHOLE DOCUMENT YOU ARE CHANGING**, rendered
as its consumer sees it, and specifically look for
  (a) a statement elsewhere that contradicts your edit,
  (b) a statement elsewhere that already says it, and
  (c) a cross-reference pointing at a section that does not exist.

This rule was written 2026-08-21 after all three happened at once, and cost real
money — and the assistant then committed (b) and (c) AGAIN while writing this
very section, which is why it is stated so bluntly.

What actually happened: `prompts/charter.md` has a THEME CONCENTRATION section
that is CORRECT and explicit — "a large share of equity in one theme is NOT a
reason to act, there is no theme limit in your mandate" — and lists the four
mechanisms that ARE reasons (clustered stop risk, a shared binary in the hold
window, momentum degrading across the theme, one position through the
per-position cap). Sessions cited "52.4% of equity", then "~35%", then "~40%" —
the exact reason that section rules out, with invented thresholds that disagreed
with each other. Each went into a `rule_out()`, which the ORDER GATE ENFORCES,
which the next session inherited as binding fact.

What made that easy was a CONTRADICTION 250 lines away: THE TERMS described the
per-name concentration criterion as "blind to sector — several names in one
industry read as diversified" and sent the agent to `sectors()`, naming no
threshold because none exists. Two sections of one document disagreeing is how
an agent ends up choosing the one that lets it act.

⛔ AND THE ASSISTANT'S OWN FAILURE, recorded because it is the same class: it
grepped case-sensitively for "Theme concentration", got one hit, and announced
the section "was never written" — then wrote a replacement that DUPLICATED the
real section and CONTRADICTED it (stating theme concentration is never risk,
when four cases are). A `grep` is not a read. That is what (b) and (c) above
are for.

By 2026-08-21 six of the top fourteen ranked names were unbuyable, cash had run
from 3% to 29% of NAV with NAV falling, and no human had ever decided any of it.
Every selftest passed throughout. The agent itself finally reported that the
rule-outs "collectively encode a theme cap that exists nowhere in the mandate".

⛔ **THE STANDING RULE THAT FELL OUT OF IT:** this book is CROSS-SECTIONAL
MOMENTUM. When momentum concentrates in one complex, that concentration IS THE
SIGNAL, not a fault to correct. The only concentration limit is the per-position
cap in `config/mandate.toml`. There is no sector, theme or correlation limit —
do not add one, in code, in a prompt, or in a rule-out.

---

## Data sources and their roles

Full scope + **verified moomoo surface** is in [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md);
architecture in `docs/DESIGN.md` (Layer 1). Summary:

| Source | Role | Notes |
| ------ | ---- | ----- |
| ~~**Schwab**~~ | **REMOVED 2026-07-29.** Was the primary market-data feed; its 7-day refresh token was the only recurring human chore in the system, and the signal consumed nothing from it but daily closes. All of it now comes from moomoo. Adapter, auth scripts, `SCHWAB_*` keys and `schwabdev` are deleted — do not reintroduce. | Migration + equivalence proof: `docs/OPSLOG.md` 2026-07-29. |
| **moomoo** (`src/adapters/moomoo/`) | **THE market-data feed** since 2026-07-29 — daily OHLC panel (`prices.snapshot_ohlc`), intraday quotes for the stop watcher (`prices.live_quotes`), universe turnover/market-cap, capital-flow, short-interest, put/call+IV. Data-only via the local **OpenD** gateway. Still unwired: insider, earnings-price-move, institutional; see `docs/DATA_SOURCES.md`. | ⚠️ Runs under **system `/usr/bin/python3` (3.10)**, NOT `.venv` (3.12) — this now includes `fetch_prices` and `market_monitor`. (`fast_loop` was deleted and `risk_review` retired into the sessions, 2026-08-13/14.) OpenD on `127.0.0.1:11111`, **shared with sibling repo `moomoo-vol-desk`**. ⛔ `request_history_kline` is capped at **100 distinct stocks account-wide** — use `get_market_snapshot` (unmetered, 400/call) for anything universe-wide. moomoo history is **shallow (~1–2 yr)** → forward-log, don't backtest; the deep panel on disk is Schwab-era and now **non-regenerable**, so `research_store/prices/backup/` matters. |
| **Finnhub** (`src/adapters/finnhub/`) | Analyst *recommendation trends*, earnings *surprises*, basic financials. **NOT retired** — but currently consumed only by `src/event_calendar/` (earnings spine), not the ranking signal. | Free tier. Price-target *level* + forward EPS estimates are **premium (403)**. |
| **Alpaca** (`src/adapters/alpaca/`) | Symbol-tagged news; IEX close+$-vol for the survivorship-free **PIT pool** (only free feed serving DELISTED names). | Free tier. Price is **IEX-only (not NBBO)** → don't use for quotes. |
| **FRED** (`src/adapters/fred/`) | Macro regime indicators: VIX (`VIXCLS`), 10y-2y curve (`T10Y2Y`), HY OAS (`BAMLH0A0HYM2`). **Deep history (decades).** Confirms — does not replace — the momentum regime gate. Also the VIX source for `slow_loop.fetch_vix`. | Needs `FRED_API_KEY`. |
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

## ♻️ CHANGING CODE IS NOT DONE UNTIL THE SERVICES RELOAD  (agents: do this)

A long-running process holds whatever it imported at start. Editing a file — or
`git pull`, or merging an auto-fix PR — changes DISK, not the running process.
Two services on this box therefore keep running last week's logic while every
other check reads green: **`agentic-monitor`** (which IS the stop, since
Robinhood has no native stop for fractional shares) and **`agentic-dashboard`**.

**So: after ANY edit to repo Python, before you report the work finished, run**

```bash
.venv/bin/python scripts/reload_stale.py        # --dry-run to look first
```

It asks `src/deployed.py` which units are actually stale (transitive import
walk, so a file you have never heard of still counts) and restarts exactly
those — never a blanket restart. It refuses to bounce the stop watcher while a
sale is in flight and says so. Exit 1 = something stale was NOT restarted.

⚠️ **Do not decide by eye whether your edit "affects" a service.** The closure
is transitive: `src/marks.py` reaches the dashboard, `src/governance.py`
reaches the monitor. Run it and let it answer; it is a no-op when nothing is
stale.

`agentic-reload.timer` runs the same script every 10 minutes as a BACKSTOP, and
the 08:00 health check still reports drift that somehow survives both. The
backstop exists because remembering is not a mechanism — the same reasoning
that moved the order gate into a PreToolUse hook. Do not treat it as a reason
to skip the command: between your edit and the next tick, the stop watcher is
running code you have already replaced.

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
                        Consumers: fetch_prices, market_monitor — both on system
                        python3. (fast_loop deleted 2026-08-14; risk_review retired
                        into the sessions 2026-08-13.)
                        research.py serves scripts/universe_refresh.py (snapshot_turnover,
                        screen_top_marketcap, candidate_pond — WEEKLY universe
                        maintenance, Fridays 17:00 ET, `[universe_maintenance]
                        screen_day`. ⚠️ WAS QUARTERLY and had NEVER once run
                        (armed 2026-07-20, zero fires); changed 2026-08-20)
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
                        source for slow_loop. Needs FRED_API_KEY.
config/strategy.toml    CODIFIED STRATEGY — single source of truth: risk gates,
                        universe, signal, trade management, regime floor.
                        Tune the strategy HERE, not in code. `[risk]` = the store's
                        validation mandate. Load via src/strategy.py.
                        Edge = momentum (settled); [signal]/[meta]/[portfolio] all
                        momentum. PEAD is fully retired — see docs/DESIGN.md.
config/models.toml      THE MODEL PINS for every unattended role (session, exit
                        executor, newsletter) + the fallback chain + the
                        terminal/budget steps (declared "none" until built).
                        Loaded by src/models.py AT SPAWN TIME — no restart.
                        ⛔ NEVER commit a temporary pin: config/models.local.toml
                        (git-ignored, human) merges over it; an outage swap is
                        a local edit and reverting is deleting that file
                        (2026-09-03 committed one to main; never again).
                        ⛔ A model id literal anywhere in scripts/, deploy/,
                        src/ is a defect — src/repo_checks.py enforces it.
src/fallback.py         THE CHAIN WALK (2026-09-04). One rule: the next model
                        is tried ONLY when the spawn failed with ZERO tool
                        calls in its stream-json transcript. Any failure after
                        a tool call is ambiguous (it may have placed an order)
                        and stops. The CLI's self-update window retries the
                        same model once. Every transition journals
                        `model_fallback` and pushes. Used by session.py and
                        market_monitor.run_executor. Reason classes
                        (usage_limit, model_outage, version_too_old,
                        unknown_model, cli_unavailable, timeout, …) derive from
                        the runner's error + the CLI's own final result line,
                        never agent prose. DRILLS prove it fires:
                        `scripts/session.py open --drill --drill-chain
                        claude-bogus,claude-sonnet-5` and
                        `scripts/market_monitor.py --executor-drill …` — no
                        MCP (deploy/drill_mcp.json), no tools, no broker; run
                        them through the unit's PATH. A usage cap hits every
                        Claude model at once: the budget step is declared
                        INERT (principal, 2026-09-04). The EXIT chain ends in
                        the code seller below; the SESSION chain ends at Fable
                        (Codex custody mode DEFERRED 2026-09-04).
scripts/code_seller.py  THE MODEL-FREE EXIT (spec §3, live 2026-09-04): the
                        exit chain's terminal step, reached ONLY after every
                        model failed cleanly. Same request id (the hook's own
                        request_id — imported, never copied), refuses on
                        HALT/SHADOW/live_approved or a non-pinned account,
                        reads live positions from the broker MCP with the
                        CLI's stored token (proven accepted from a second
                        client), sells floor(fraction × live, 6dp) as a market
                        share-quantity order bounded by the hook's own
                        _scope_verdict, rewrites exit_result.json after EACH
                        placement, stages the recorder files. Fails CLOSED on
                        pagination, token refusal, non-fill: nothing placed
                        for that symbol, result absent, monitor pauses + pages.
                        `--drill` = review only, nothing placed (run 08:33 ET
                        2026-09-04, clean, before wiring). A Claude-wide
                        outage cannot leave a breached position unsold while
                        the broker is up.
config/universe.csv     Fixed 150-name momentum universe (human-seed reconciled
                        with dollar-volume liquidity fill). `flag` col marks
                        adr/micro/spec/fresh-ipo model-caveats. Referenced by
                        [universe] in strategy.toml.
⛔ config/etf_universe.csv  DELETED 2026-08-20 — along with the whole ETF sleeve.
                        Retired 2026-08-16 (enabled=false), positions sold
                        2026-08-17, and the MACHINERY deleted 08-20: the file,
                        the [etf_sleeve] table, the second ranking engine in
                        slow_loop, ETFs in the order-gate whitelist, and ETFs in
                        the agent's candidate screen. A retirement that leaves
                        the mechanism in place is not a retirement — it reads as
                        live to every later reader. THE STRATEGY IS EQUITIES
                        ONLY and the code says so by absence, not by a flag.
                        ⛔ Do NOT reintroduce it. A buy naming a fund is refused
                        by governance.whitelist() (single-name universe only);
                        a SELL is never refused.
                        ⚠️ WHAT SURVIVES, AND IS NOT AN ETF SLEEVE: the 11
                        sector PRICE SERIES in src/residual.py:SECTOR_FACTORS
                        (renamed from SECTOR_ETFS) plus SPY. The residual tilt
                        regresses every name on them to separate its own move
                        from its sector's; SPY is the regime observation.
                        fetch_prices keeps those columns. They are FACTOR INPUTS
                        — not in the universe, not in the whitelist, not in the
                        screen, not buyable. Deleting them would silently drop
                        the signal to plain momentum.
                        ⛔ The weekly universe screen is EQUITIES-ONLY too: its
                        candidate pond (moomoo's market-cap screen, documented
                        UNFILTERED, falling back to config/pit_pool.csv) could
                        otherwise ADD a fund straight back into universe.csv =
                        the whitelist. Two layers now: a positive market-cap
                        test (moomoo serves no total_market_val for a fund) and
                        a NON_EQUITY denylist backstop; pit_pool.csv had its 18
                        funds removed.
src/strategy.py         Strategy-config loader (tomllib) + risk_mandate()
src/governance.py       Layer-5 guardrails: kill-switch file, drawdown halt,
                        per-order cap, universe whitelist, live_approved master
                        switch. The last gate before a live order (every session).
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
scripts/market_monitor.py Intraday stop/take-profit watcher.
                        ⛔ AN UNREADABLE overrides.json IS NOT AN EMPTY ONE
                        (2026-08-31). It used to swallow the parse error and
                        apply no overrides, which silently reverted all 12
                        positions to the loop's looser thesis stops with nothing
                        reporting it. The fallback is unchanged and still
                        fail-safe; it now ALERTS, fire-on-transition. Polls moomoo quotes
                        during RTH vs. each holding's stored stop/targets; fires
                        prompts/exit.md (market sell) on a breach. Pure-Python
                        watching, Claude only on an event. Runs as a systemd
                        service; alert-only unless live_approved. [monitor] config.
                        ⛔ SINCE 2026-09-03 THE MONITOR RECORDS EVERY EXIT
                        ITSELF (src/exit_bookkeeping.py): after the executor
                        returns it runs record_fills / record_exit_outcome /
                        record_partial_outcome / reconcile_ledger from the
                        staging files, archives each consumed file to
                        research_store/rh/consumed/, and pages on a failure or
                        on "sold but nothing staged". The executor used to run
                        those under exact-match Bash grants and one un-retried
                        refusal left a filled DELL trim unrecorded.
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
                        SINCE 2026-09-04: every verdict — ran, failed, never
                        launched — journals `session_run` with a reason class,
                        and the brief renders SINCE YOUR LAST RUN (the newest
                        4, MISSED rows explained, "not yours to diagnose").
                        The spawn walks config/models.toml's chain via
                        src/fallback.py; `--drill` proves the chain.
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
                        and scored against the tape by score_reviews.py. NOT a
                        veto — no second model sits on the critical path.
                        ⛔ THE VERDICT DOES NOT REACH THE AGENT. It used to be
                        injected into the next open/close brief; switched OFF
                        2026-08-13 at session.py:SHOW_REVIEW_TO_AGENT by the
                        principal, who is judging reviewer-vs-agent himself
                        first. The agent is STATELESS (a verdict shown once is
                        not learning, and its recorded answer was never read
                        back) and the charter never named the review process at
                        all, so an unexplained critique was ambiguity the agent
                        would resolve by inventing a reason for it. Whether and
                        HOW to feed it back is an OPEN question, not an
                        oversight. The renderer is kept live and selftested with
                        the flag forced on, so reconnecting is a one-line flip —
                        but name the process in the charter first.
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
prompts/charter.md      ⛔ THE SESSION'S JOB IS TO TRADE. DIAGNOSING THIS SYSTEM
                        IS NOT ITS JOB (principal's ruling, 2026-08-31, in THE
                        DIVISION OF LABOUR). Raised 2026-08-25, recorded as "NOT
                        YET ADDRESSED — for a later charter pass", and it
                        recurred six days later. TWICE it has moved money on a
                        code opinion: a stop WIDENED on 08-25 because the session
                        judged the enforcement logic wrong, and on 08-31 a DECIDED
                        SELL of FCX reversed on a drawdown halt that did not
                        exist (~40% of that session's reasoning written under a
                        false premise, then re-sold 7 minutes later). The session
                        must reconcile once, re-test, write ONE open_question if
                        still blocked, and GO BACK TO THE BOOK. Never work around
                        a gate — refusing to circumvent is REQUIRED, investigating
                        it is FORBIDDEN; they are different things.
                        THE SESSION CHARTER — rendered from config by
                        src/charter.py (mandate.toml + strategy.toml + the live
                        MCP tool list), never hand-copied. A literal threshold in
                        the template is a DEFECT; check_charter_no_literals
                        enforces it.
⛔ prompts/fast_loop.md and scripts/fast_loop.py are DELETED (2026-08-14).
                        The procedural executor placed the stored book at 10:00,
                        35 minutes BEFORE the open session reasoned — so it moved
                        first every day and the session spent its run undoing it
                        (it re-opened AMAT twice after a session had deliberately
                        exited, the second time into a post-earnings gap).
                        Execution is now the SESSIONS, with judgment. The guards
                        that lived inside it — no_chase and the [reentry] 4%
                        knife-guard — went with it and are NOT enforced anywhere;
                        their config keys are documented defaults for the
                        session's judgement. What IS enforced, unbypassably, is
                        the PreToolUse order gate below.
prompts/exit.md         Exit-executor procedure — market-sell the breached
                        positions the monitor flags, journal, reconcile the
                        snapshot + realized-P&L after selling.
src/notify.py           THE ntfy phone-push helper (push(); never raises; no
                        NTFY_TOPIC in .env -> no-op). Used by market_monitor
                        (stop/target alerts), scripts/record_fills.py (exit-path
                        fill summaries) and, since 2026-09-03, the sessions'
                        MCP record_fills (one line per new fill) — both via
                        notify.fill_line. deploy/alert.sh mirrors it in shell.
src/marks.py            Position valuation — the ONE place snapshot positions
                        become dollars (qty × freshest mark: monitor quote >
                        snapshot last > cost). Dashboard, log_equity and the
                        sessions all read through it. Snapshot schema here.
research_store/rh/positions.json
                        ⛔ THE ACCOUNT SNAPSHOT — what the stop watcher, brief(),
                        the valuation and the weekly letter all read as "what we
                        hold". It is NOT authoritative; Robinhood is. This file
                        is a cache, and it went 2 days stale on 2026-08-14 while
                        the monitor stop-watched a position that had been sold.
                        WRITTEN ONLY by agent_env.refresh_broker_snapshot() (every
                        session, trading or not) and record_fills() (after a
                        trade).
                        ⛔ REFRESHED AT THE START OF A SESSION AS WELL AS THE END
                        (2026-08-31). It used to be end-only — charter and tool
                        docstring both said so — which meant a session SIZED THE
                        WHOLE BOOK against a snapshot that could be days old and
                        reconciled afterwards. On 2026-08-31 it was short a $30
                        deposit (30% of NAV) and the session planned against NAV
                        71.31 versus a true 101.51; it was caught only because the
                        order gate refused a buy on the mismatch. A SMALLER
                        DEPOSIT REFUSES NOTHING. The refresh now returns
                        `cash_delta`/`account_value_delta` against the file it
                        replaces and flags UNEXPLAINED_CASH — reported, never
                        classified (deposit vs withdrawal vs dividend is not its
                        call). ⛔ A REFUSED REFRESH MUST NOT STOP TRADING: work
                        from the stale snapshot, say it is stale, record it. AGE
                        IS NOT CONFIRMATION — _staleness() returned None that
                        morning because the file was one trading day old and
                        parsed perfectly. Only the broker settles it. The deleted fast loop used to write it; when it
                        went, nothing took over.
                        ⛔ EVERY writer goes through _write_broker_snapshot(), and
                        the one that journals the fill passes it the SAME ts, so
                        snapshot and journal can never disagree. The EXIT path
                        reaches it via scripts/record_fills.py +
                        research_store/rh/broker_state.json — RUN BY THE MONITOR
                        (src/exit_bookkeeping.py, 2026-09-03), not by the
                        executor: the executor writes the staging files and
                        holds no Bash grant. deploy/exit_mcp.json deliberately
                        does NOT mount the agentic-trader MCP. Until 2026-08-25 that path
                        HAND-WROTE this file from a prompt template — no
                        validation, and a second clock, which made a correct
                        snapshot read stale-after-fill (OPSLOG 2026-08-25, "the
                        exit path's snapshot was written by a second clock").
                        Do not reintroduce a hand write; the permission to do it
                        was removed from deploy/exit_executor_settings.json.
                        ⛔ THE PUBLISHER REFUSES rather than coerces. Any
                        unreadable row, missing/negative/non-finite qty or cost,
                        duplicate symbol, or wrong account REJECTS THE WHOLE
                        WRITE and leaves the previous file byte-identical. An
                        EMPTY book needs liquidated=True; a partial read needs a
                        cursor-linked pagination transcript proving exhaustion.
                        Completeness is EVIDENCE THE CALLER SUPPLIES, never
                        inferred — a truncated page is well-formed, and
                        publishing one silently unprotects everything it omits.
                        `account_number` must be present: _expected_account()
                        compares against it, so a snapshot written without one
                        leaves the identity guard inert.
                        Staleness is reported by src/snapshot_freshness.py and
                        surfaces in health as `positions_snapshot`. See OPSLOG
                        2026-08-16.
dashboard/app.py        Flask monitor (127.0.0.1:8787), renders live from the
                        Research Store; password-gated (DASH_USER/DASH_PASS in
                        .env, fail-closed). Live at dash.ethobs.uk via cloudflared
                        tunnel. dashboard/dashboard.html = the page template.
scripts/log_equity.py   Appends a daily equity point (marked via src/marks.py) to
                        research_store/history/equity.jsonl — the dashboard curve.
                        Run by deploy/run_session.sh each day.
prompts/newsletter.md   Weekly investor letter ("The Claude Ledger") — headless
                        Claude narrates ONLY from letter_facts.py's facts.json
                        (never computes numbers), fills newsletter/template.html,
                        Sundays 21:00. scripts/send_newsletter.py delivers via
                        Resend HTTPS API (DO blocks ALL outbound SMTP ports).
deploy/                 run_slow_loop.sh (Python), run_session.sh (Claude, with
                        ANTHROPIC_API_KEY guard), run_newsletter.sh,
                        crontab.template, alert.sh (ERR-trap → ntfy phone push on
                        any cron failure). See docs/DEPLOY.md.
src/health.py           SCHEDULED-JOB LIVENESS — did each moving part actually run?
                        Every job leaves an artifact (book/log/journal event/commit);
                        a job that stopped running = an artifact that stopped moving.
                        evaluate() pure + selftested, gather() thin I/O. Built after
                        the signal panel was found to have NEVER run (2026-07-24) —
                        a job that never runs can't fire its own alerts.
                        ⛔ IT IS NO LONGER ONLY LIVENESS (2026-08-31). Liveness
                        cannot see a job that runs and produces something WRONG,
                        which is what every defect of 2026-08-31 was. Two content
                        checks now ride alongside:
                        • `unreadable_artifacts` — the 7 files where being wrong
                          costs money or protection, PARSED not just stat'd.
                          ABSENT IS DELIBERATELY NOT A FINDING: a missing file is
                          a real state. PRESENT-BUT-UNREADABLE is the event,
                          because every reader in this repo catches a parse error
                          and substitutes the SAME empty default the absent case
                          produces — so a corrupt overrides.json silently reverts
                          every stop in the book (measured, all 12 positions).
                        • `py310_compatible` — compiles the monitor's transitive
                          import closure with the REAL /usr/bin/python3. The stop
                          watcher runs under 3.10; a module it imports using
                          3.12-only syntax is a watcher that cannot START.
                          health.py itself had already lapsed this way. Derived
                          from deployed.import_closure(), never hardcoded, and
                          SKIPPED on the dashboard render (4.1s: fine daily, not
                          on a page load) — it rides `use_network`.
                        ⚠️ A new Check status must also be added to the dashboard's
                        word/colour map in dashboard/dashboard.html or it renders
                        as raw lowercase in amber.
                        • `claude_binary` / `claude_models` (2026-09-04): the
                          session unit and a login shell must resolve the SAME
                          `claude` (the box had two — an un-updatable April
                          2.1.100 on the cron PATH and an auto-updating nvm
                          install in the shell; the orphan is deleted and
                          /usr/local/bin/claude → the nvm binary), and the
                          installed version must accept every pinned model
                          (`[requires]` in config/models.toml). Ride `use_network`.
scripts/health_check.py THE UPKEEP REMINDER (daily 08:00). Runs health.checks() and
                        pushes anything unhealthy to the OPS ntfy topic. FIRE-ONCE
                        per condition (clears silently on heal); the dashboard
                        "Scheduled jobs" card carries standing status. Owns the
                        NO credential reminder any more — the Schwab 7-day token
                        was the only one and it is gone. Most checks ask "did this
                        job leave evidence it ran"; since 2026-08-31 two ask
                        whether what it produced is USABLE — see src/health.py.
                        `--open-issue` files a deduped `bug`+`ops` GitHub issue
                        FOR THE RECORD. ⛔ NEVER `auto-fix` (2026-09-04): that
                        label runs a Claude Code session on Actions billed to
                        the principal's subscription, and 9 such runs since
                        08-10 all ended "ops, no PR". The agent runs only when
                        a human @-mentions it. A job the operator switched off
                        on purpose is marked by research_store/disabled/<key>
                        (the review, since 08-19, via deploy/run_session.sh)
                        and reads `disabled`: never alerts, never files.
scripts/reload_stale.py RUN THIS AFTER EDITING REPO PYTHON (see "CHANGING CODE
                        IS NOT DONE UNTIL THE SERVICES RELOAD" above). Restarts
                        exactly the long-running services whose code no longer
                        matches disk, per src/deployed.py's transitive import
                        walk. Refuses to bounce agentic-monitor while an exit is
                        in flight (exit_request.json with no exit_result.json).
                        Backstopped every 10 min by agentic-reload.timer. Built
                        2026-08-17 because the detector had always computed the
                        remedy — the unit name is literally in its alert text —
                        and then paged a human instead of applying it, making
                        "stale code" the most frequent alert the system produced.
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
                        deduped `bug`+`ops` GitHub issue (⛔ not `auto-fix`,
                        and no `repository_dispatch` — removed 2026-09-04, see
                        the file's header); `deadman` also phones
                        NTFY_TOPIC_OPS on droplet death.
.github/workflows/claude.yml  The agent half of the oversight loop — fires on
                        a GitHub issue carrying BOTH `bug` and `auto-fix`
                        labels (⛔ SINCE 2026-09-04 ONLY A HUMAN ADDS THAT
                        LABEL — no detector does; it is the principal's
                        subscription being spent), runs a 6-stage protocol (triage ops-vs-code,
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
                        (risk_review, retired 2026-08-13, read highs.parquet and
                        folded in today's session high from a live snapshot.)
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
                        signal → top-14 book (sleeve retired) → IBD trade geometry
                        (vol-scaled targets, R:R≥2) → write validated book to the
                        Research Store. Regime-off/nothing-eligible → cash is valid.
                        (scripts/fast_loop.py was the diff core — stored targets
                        vs. holdings → a buy/sell plan. DELETED 2026-08-14 with
                        the rest of the procedural executor. Its guards went too:
                        apply_chase_guard (no_chase), the [reentry] knife-guard,
                        the order-time cooldown check, and — found only by the
                        independent reviewer — the AUTOMATIC DRAWDOWN HALT, which
                        it was the sole caller of. The drawdown halt has been
                        restored at the order gate via a write-free
                        governance.drawdown_breach(); the others have not, and
                        are now the session's judgement. See OPSLOG 2026-08-14.)
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
                        history(symbol,days), set_levels(symbol,stop,targets,reason)
                        (targets: one number, a list, or 0/None/""/"0" for no
                        target — a stop with no target is legal), clear_levels
                        (symbol,reason) (call on every exit — levels do not
                        expire and outlive a closed position otherwise),
                        record_decision(), check_order(). Runs under .venv
                        (3.12) with mcp==1.28.1;
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
                        dual momentum: exact signal math, portfolio rules, how
                        execution actually happens (the SESSIONS, §8 — the
                        fast-loop procedure it used to describe is deleted),
                        guardrails, proof gate. ⚠️ config/strategy.toml is
                        authoritative for every number; §4 defers to it.
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
