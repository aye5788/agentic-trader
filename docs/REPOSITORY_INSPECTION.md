# Repository Inspection

Inspection scope: `aye5788/agentic-trader` at commit `d9ec9ef`, 2026-08-09.

This is a read-only architectural and operational assessment of the repository as
it existed at the stated commit. Confirmed facts are identified directly. Where a
conclusion is interpretive, it is marked as an inference. Items that require live
service, broker, or repository-settings access are listed as unknowns.

## 1. Executive summary

`agentic-trader` is a live-money, long-only equity trading system deployed on a
VPS. Its defining architectural decision is to separate deterministic research
and portfolio construction from broker execution:

```text
moomoo / FRED / Finnhub / Alpaca
               |
               v
deterministic momentum engine
               |
               v
validated Research Store
               |
               v
deterministic target-vs-actual order plan
               |
               v
headless Claude -> Robinhood MCP (Agentic account only)
```

Confirmed:

- Python computes signals, portfolio selection, trade geometry, risk facts, and
  order plans. Claude does not select the primary portfolio.
- Robinhood is the sole execution venue. Every other provider is data-only.
- The strategy is dual momentum: a 150-name stock universe and an 18-ETF
  rotation sleeve, using a 70/30 capital split.
- Stops are software-enforced because broker-native fractional stop orders are
  unavailable.
- Persistent state is mainly JSON, JSONL, CSV, and Parquet under
  `research_store/`.
- Committed configuration ships with live execution disabled. A git-ignored local
  override can arm the deployed machine.
- No `AGENTS.md` exists. Repository agent guidance is in `CLAUDE.md`.
- No Git submodules are configured.
- At inspection time, GitHub reported no open pull requests or open issues.

The most consequential findings are:

1. The fast loop does not enforce research-product freshness before planning
   live orders.
2. Stop protection depends on a multi-service application chain rather than a
   native broker order.
3. Positions absent from the target book are outside the monitor and risk-review
   protection envelope.
4. The advertised aggregate selftest runner is broken by the repository's two
   Python runtimes and omits many existing selftests.
5. Core documentation remains materially inconsistent following Schwab's removal.
6. Several safety controls deliberately fail open when evidence is unavailable.
7. The new control-binding monitor is not connected to live evidence.

## 2. Repository and technology map

| Area | Responsibility |
| --- | --- |
| `src/` | Signals, governance, storage, valuation, health, controls, adapters |
| `scripts/` | Operational loops, backtests, reconciliation, tuning, maintenance |
| `prompts/` | Claude execution, exit, risk-review, and newsletter procedures |
| `config/` | Strategy mandate and tradeable universes |
| `research_store/` | Beliefs, journal, prices, broker snapshots, monitor state |
| `deploy/` | Cron wrappers, systemd units, alerts, and ledger backup |
| `dashboard/` | Password-gated Flask dashboard |
| `.github/workflows/` | Tuning, validation/dead-man checks, automated repair |
| `docs/` | Architecture, strategy, operations, and source documentation |
| `newsletter/` | Investor-letter template and sample |
| `.superpowers/sdd/` | Historical task briefs, review diffs, and reports |

### Languages and runtime

- Python is the main implementation language.
- Bash supplies scheduling wrappers, alerts, and backups.
- HTML/JavaScript implement dashboard and newsletter views.
- YAML, TOML, CSV, JSON, JSONL, SQLite, and Parquet are used for configuration
  and data.

Two Python environments are load-bearing:

- Project `.venv`: Python 3.12 for most code.
- System `/usr/bin/python3`: Python 3.10 with the moomoo SDK for every process
  importing `moomoo`.

`requirements.txt` declares `python-dotenv`, `requests`, `pandas`, `numpy`,
`pyarrow`, and Flask. It is not a complete reproducible environment: the moomoo
SDK, Python 3.10 compatibility dependency `tomli`, OpenD, Claude CLI, and other
host services are managed outside it.

## 3. Architecture and component responsibilities

### Sensing

- `src/adapters/moomoo/`: primary OHLC and live quote source; also turnover,
  market-cap, flow, short-interest, and options-derived data.
- `src/adapters/fred/`: VIX, yield curve, and high-yield spread with retry and a
  last-good cache.
- `src/adapters/finnhub/`: earnings-calendar inputs, analyst trends, surprises,
  and financials.
- `src/adapters/alpaca/`: news and historical IEX data for the point-in-time
  backtest pool.
- Robinhood MCP: broker state and all order execution.

`src/adapters/moomoo/client.py:15` performs a TCP preflight before constructing
the SDK context. This converts a known unbounded SDK connection hang into a
bounded, catchable error.

### Research and decision

`src/momentum.py:29` computes 252-session return, daily volatility,
risk-adjusted return, distance above the 200-day mean, rank-average score,
positive-return eligibility, and an optional sector-residual momentum blend.

`src/momentum.py:81` implements banded selection. The nominal stock target is ten
names, while current holdings remain eligible through rank 15.

`scripts/slow_loop.py:196` orchestrates price-panel loading, regime evaluation,
selection, earnings stamping, trade geometry, validation, and persistence.

### Research Store and ledger

The Research Store is file-backed:

- `current.json`: current validated beliefs and targets.
- `archive/YYYY-MM-DD.json`: historical products.
- `journal.jsonl`: append-only event stream.
- `prices/`, `rh/`, `monitor/`, `governance/`, `calendar/`: supporting state.

Schemas are dataclasses in `src/research_store/models.py:23`. Products pass the
mandate in `src/research_store/validate.py:37` before
`src/research_store/__init__.py:46` makes them current.

Current-product writes are atomic. Many ancillary files use direct writes,
creating inconsistent durability guarantees across concurrently running cron,
systemd, dashboard, and Claude processes.

### Execution planning and governance

`scripts/fast_loop.py:41` diffs target weights against marked broker holdings. It
opens, adds, trims, and exits positions; skips dust and small rebalances; orders
sells first; and attaches exact quantity to full exits.

The script does not call the broker. It writes `order_plan.json`, consumed by the
headless execution prompt.

`src/governance.py:25` provides the kill switch, local live switch, drawdown halt,
universe whitelist, and per-buy order cap. Research-product validation separately
enforces position weight, total weight, holdings count, and reward/risk geometry.

## 4. End-to-end execution and data flow

### Slow path

1. `deploy/run_slow_loop.sh:14` runs `fetch_prices.py` under system Python.
2. moomoo snapshots append settled OHLC values to Parquet panels.
3. `slow_loop.py` loads those panels and configured universes.
4. Active stop-out cooldown names are excluded.
5. SPY's 50-day trend and FRED VIX determine the regime.
6. Stocks are ranked with the configured sector-residual tilt; ETFs use the base
   momentum signal.
7. The previous **target product** is read so banded retention can operate. This
   is not a read of actual Robinhood holdings: a target whose order failed or was
   skipped can still be treated as "held" by the selector.
8. A regime-off decision clears the single-name target list; the ETF sleeve is
   still independently selected.
9. Each target receives entry zone, volatility-scaled stop, and R-multiple
   targets.
10. The event-calendar layer attempts to stamp earnings dates.
11. The validated product becomes current belief, archive, and journal evidence.
12. Sunday rebuilds clear intraweek risk-review overrides and deferred intents.

Although portfolio membership is described as weekly, the same full slow loop
runs after every weekday close. It reconstructs each selected thesis's entry zone,
stop, targets, `as_of`, and `decision_id` every night. Membership is banded, but
trade geometry and decision identity are not preserved for the life of a position.

Important inconsistency: documentation commonly says regime-off blocks "new
entries," but `scripts/slow_loop.py:258` clears all stock targets. The fast loop
will therefore plan exits for existing stock holdings.

### Fast path

1. Headless Claude follows `prompts/fast_loop.md`.
2. It selects exactly one Robinhood account with `agentic_allowed=true`.
3. It fetches portfolio, positions, and quotes and writes a broker snapshot.
4. `src/marks.py:38` values that snapshot using the freshest available marks.
5. `fast_loop.py` generates the target-vs-actual plan.
6. Governance, cooldown, no-chase, and re-entry-review controls modify the plan.
7. The plan is always overwritten so a prior nonempty plan cannot be replayed.
8. When armed, Claude reviews and places approved orders through Robinhood MCP.
9. State, orders, fills, realized P&L, and outcomes are reconciled into the store.

`src/research_store/__init__.py:96` supplies `is_stale()`, but
`scripts/fast_loop.py:321` checks only whether a product exists. An old product
can therefore generate an execution plan.

When the snapshot contains cash, `marks.load()` ignores the snapshot's supplied
account total and reconstructs account value as cash plus locally marked positions.
That reconstructed value drives target sizing, drawdown state, and order caps.

### Intraday exit path

1. `agentic-monitor.service` runs continuously under systemd.
2. During weekday regular hours, it polls moomoo quotes every 15 seconds.
3. It intersects target-book names with the last broker snapshot's holdings.
4. It applies stricter-only risk-review overrides.
5. A stop or target breach writes an exit request and triggers journal/phone
   evidence.
6. If armed, the monitor launches Claude with `prompts/exit.md`.
7. Claude confirms the account, sells only requested positions, refreshes state,
   records fills, and reconciles outcomes.
8. Failed exits back off and eventually escalate.
9. Repeated feed failures alert, then terminate the monitor so systemd restarts
   it.

Monitor state is keyed to the product's `as_of`. Each nightly product date resets
the fired stop/target flags. In alert-only mode, the monitor treats a signalled
breach as acted upon and marks it fired even though no sale occurred, suppressing
repeat alerts for that level until the next product generation.

## 5. Agent and prompt system

There is no in-process agent framework. Agents are headless Claude CLI invocations
driven by procedural Markdown prompts.

- `prompts/fast_loop.md`: broker-state acquisition, approved-plan placement,
  reconciliation, and reporting. Its only strategic discretion is post-target
  re-entry, which may veto or downsize but not increase the plan.
- `prompts/exit.md`: event-triggered, sell-only stop/target executor.
- `prompts/risk_review.md`: twice-daily defensive review; may tighten stops, lower
  targets, trim, exit, or watch, but never increase risk.
- `prompts/newsletter.md`: narrative generation from deterministic facts.
- `.github/workflows/claude.yml`: issue-to-PR repair agent with operational triage,
  mandatory adversarial self-review, re-verification, and no direct merge.

## 6. Trading and risk-management workflow

Confirmed configuration from `config/strategy.toml`:

- 252-session lookback with no skipped recent month.
- Risk-adjusted return plus 200-day trend percentile ranks.
- Positive 12-month-return eligibility gate.
- 0.75 sector-residual momentum tilt for stocks.
- Ten stock positions and four ETF positions.
- 70% stock / 30% ETF allocation.
- Weekly banded rebalance with nightly recomputation.
- SPY 50-day trend and VIX 28 regime conditions.

Each stock slot is nominally 7%; each ETF slot is 7.5%. The store caps any name
at 10%. Stops are approximately `entry * (1 - 2.5 * daily_sigma)`, with targets
near 2.2R and 4R. First-target reward/risk must be at least 2:1.

Lifecycle:

- Stop breach: full exit and five-day cooldown.
- First target: partial scale-out.
- Second target: remaining exit.
- Rank/regime removal: fast-loop full exit.
- Target exit: five-day re-entry review.
- Risk review: stricter geometry or discretionary de-risking.

The configured 21-day moving-average exit is not automatic. Current code exposes
an `ma_break` review flag, but automatic selling remains unwired.

The weekly adaptive tuner proposes a bounded `stop_atr_mult` change using replay
and live stop-out outcomes. It never trades or edits live config. A human promotes
a proposal; local human config has final precedence.

## 7. External integrations and configuration

Expected environment keys, without values:

- `FINNHUB_API_KEY`
- `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`
- `FRED_API_KEY`
- `DASH_USER`, `DASH_PASS`
- `NEWSLETTER_TO`, `NEWSLETTER_FROM`
- `RESEND_API_KEY` or `NEWSLETTER_APP_PASSWORD`
- `NTFY_TOPIC`, optional `NTFY_SERVER`, `NTFY_TOPIC_OPS`

Other operational credentials include Claude OAuth, GitHub `LEDGER_TOKEN`,
Robinhood MCP OAuth, SSH deploy keys, and Cloudflare tunnel credentials.

External services are moomoo OpenD on `127.0.0.1:11111`, Robinhood MCP, Finnhub,
FRED, Alpaca, ntfy, Resend/Gmail, Cloudflare Tunnel, GitHub Actions, and a private
ledger mirror.

Positive controls:

- `.env`, `secrets/`, runtime state, logs, overrides, and virtual environments
  are git-ignored.
- `repo_checks.py` scans for credentials and CI/config drift.
- The dashboard fails closed without `DASH_PASS`, compares passwords in constant
  time, and binds only to `127.0.0.1`.
- Execution wrappers reject `ANTHROPIC_API_KEY` to prevent unintended billing.
- Broker prompts re-identify the uniquely authorized account at runtime.
- Position-bearing and operational notification topics are separated.

Concerns:

- Dashboard authentication has no application-level throttling.
- Dashboard and monitor services run as root.
- A broker account identifier is embedded in a prompt as an expectation, although
  runtime account validation is authoritative.
- GitHub Actions mostly use mutable major-version tags rather than immutable SHAs.
- Several provider credentials must travel as query parameters; request URLs must
  never be logged.

## 8. Testing, deployment, and operations

There is no conventional `tests/` tree, pytest configuration, coverage tooling,
type checking, or lint configuration. Tests are module-local assertion-based
`--selftest` entry points.

Read-only verification performed during inspection:

- Nineteen directly runnable non-moomoo selftests passed.
- `src/repo_checks.py` passed all six repository checks.
- `deploy/run_selftests.sh` failed.

The aggregate runner forces `.venv/bin/python`, but its first test imports the
monitor, which imports the system-only moomoo SDK. It therefore fails with
`ModuleNotFoundError`. It also enumerates only eight files despite at least 23
scripts/modules exposing selftests. Its claim to run every selftest is inaccurate.

Coverage is strongest for pure algorithms and known regressions. Important gaps
include real OpenD behavior, Robinhood MCP contracts, prompt execution, full
monitor-to-exit integration, concurrent/torn state, stale-research rejection,
partial fills, settlement, deployment state, and market holidays.

Deployment uses cron for scheduled jobs, systemd for monitor/dashboard services,
Cloudflare Tunnel for dashboard ingress, ntfy for alerts, and a private Git mirror
for selected non-regenerable state.

GitHub workflows:

- `adaptive-tune.yml`: weekly/manual tuning proposal.
- `validate.yml`: ledger dead-man plus static repository checks.
- `claude.yml`: automated issue-to-PR repair and interactive `@claude` work.

There is no general CI workflow running all selftests on every pull request.

The monitor's market-hours logic is weekday and clock based; it does not consult
an exchange holiday or early-close calendar.

## 9. Current development state

Recent commits show active work on previously configured-but-inert controls:

- `d9ec9ef`: repaired adaptive-tune YAML and corrected workflow-health evidence.
- `6116b2f`: wired earnings-proximity and moving-average-break risk flags.
- `06b0302`: ignored the stray `v2env`.
- `aa2e7d1`: connected live stop-out outcomes to adaptive tuning.
- `08e9508`: changed full exits from dollar-notional to exact-share sells.

`src/controls.py` is the architectural response to inert controls: guards declare
expected firing evidence. However, its non-selftest entry point currently exits
with "live roll-call not wired yet" (`src/controls.py:174`). It is a registry and
test harness, not yet operational telemetry.

## 10. Implementation re-audit

A second pass traced the production paths more narrowly and corrected several
high-level descriptions in the original inspection.

### Corrections and important omitted mechanics

- **Banded retention follows prior targets, not broker holdings.** The slow loop's
  `held_book` and `held_etf` sets come from `read_current()`. A target that failed
  to fill can be retained as held, while an actual off-book position cannot
  participate in the band.
- **Nightly geometry is mutable.** A continuously held position receives a new
  stop, targets, entry zone, `as_of`, and `decision_id` after every close. Only
  risk-review overrides are constrained to become stricter; the base slow-loop
  stop may move down as well as up.
- **Thesis rank has two meanings.** `Thesis.rank` is assigned as a sequential
  portfolio-slot marker (stocks from 1, ETFs from 100). The true cross-sectional
  signal rank lives in `Thesis.signals["rank"]`. Risk-review facts expose the
  synthetic field, so a band-retained stock actually ranked 14th may appear as a
  top-ten slot.
- **Actual positions are filtered through current theses.** Risk review iterates
  snapshot positions but discards any symbol lacking a current thesis and stop.
  The monitor similarly begins with weighted current theses and intersects them
  with the snapshot. Neither system tends an off-book holding.
- **Global halts suppress exits as well as entries.** Fast-loop kill-switch and
  drawdown preflight return an empty plan before per-order handling. The monitor
  and risk-review prompt also idle on the kill switch. This is a true stop-all
  switch, not merely a stop-buying switch.
- **Fill recording is not idempotent.** Re-running `record_fills.py` with the same
  `fills.json` appends a duplicate execution event. The separate reconciler is
  idempotent only for fills carrying an order ID.
- **Outcome labels do not identify an immutable entry decision.** Rotation
  outcomes locate the most recent archived held thesis. Because theses are
  regenerated nightly, reported holding days and stop/target geometry may be
  anchored to the latest nightly plan rather than the actual position opening.
- **The journal is not bounded in code.** `read_journal()` loads the complete
  JSONL file, and outcome idempotency scans it. The documented bounded-context
  property is a usage aspiration, not an enforced storage/query behavior.
- **Ledger reconciliation is conditional evidence.** It checks only the
  agent-written `orders_dump.json`; no dump is a successful no-op, and no run ID,
  freshness requirement, or broker-wide completeness check exists.
- **OHLC persistence is not transactional across fields.** Open, high, low, and
  close Parquet files are written sequentially. A mid-sequence failure can leave
  panels from different generations.
- **Health detection has deliberate blind periods.** Monitor staleness allows four
  days to avoid weekend/holiday false alarms. This can delay detection of a
  midweek monitor outage. The health module also hardcodes the kill-switch path
  separately from strategy configuration.

### Code, documentation, test, and configuration contradictions

- **Weekly book vs nightly thesis replacement:** documentation describes weekly
  portfolio rebuilding and nightly risk exits, while cron invokes the full
  thesis-producing slow loop nightly.
- **No-new-entry regime wording vs target liquidation:** code clears all stock
  targets when regime is off. Existing stocks consequently become sell plans.
- **`OFF (cash)` vs ETF exposure:** slow-loop output says cash while the ETF sleeve
  can remain invested up to its configured allocation.
- **Configured signal parameters vs hardcoded implementation:** production code
  does not consume `[signal].lookback_days`, `skip_recent_days`, `rank_method`, or
  `absolute_gate`; `momentum.py` defaults and formulas govern them.
- **ETF switches and counts:** `[etf_sleeve].enabled` and `hold_top` are not used by
  the slow loop. ETF selection is unconditional and uses
  `[portfolio].sleeve_hold`.
- **Risk-review switch:** `[risk_review].enabled` is not checked by the risk-review
  script or wrapper; cron scheduling and alert/live gates control behavior.
- **Other descriptive-only keys:** `[proof].entry_cadence` and
  `[regime].agent_overlay` have no production consumer.
- **Moving-average exit:** strategy prose describes an exit below the 21-day mean;
  implementation supplies only an advisory `ma_break` flag.
- **Fast-loop freshness contract:** Research Store documentation presents
  `is_stale()` as a fast-loop guard, but the fast loop never calls it.
- **Ledger completeness claim:** reconciliation documentation says silent
  incompleteness is impossible, while missing or incomplete agent dumps are not
  independently detectable.
- **Selftest runner claim:** `run_selftests.sh` says it runs every selftest in the
  venv, but omits most selftests and fails on the monitor's system-only moomoo
  import.
- **Monitor evidence:** health documentation treats monitor state as proof of a
  poll, but the monitor can return without updating state when no product, no held
  thesis, or a kill switch is present.
- **Alert-only meaning:** alert-only mode does more than avoid selling; it marks a
  breach fired and suppresses repeat alerts for that product generation.
- **Broker total vs local valuation:** snapshots contain broker account value, but
  sizing and governance use cash plus locally marked positions whenever cash is
  present.
- **Removed Schwab paths:** README, DESIGN, DEPLOY, strategy comments, source
  docstrings, and some remedies still describe removed commands or data paths.

## 11. Prioritized risks, gaps, and technical debt

### High

1. **Nightly thesis identity and geometry drift.** A continuously held position
   receives a new entry zone, stop, targets, date, and decision ID every night.
   This can loosen stops, reset monitor state, and misattribute outcomes.
2. **Protection coverage for actual holdings.** Monitor and risk review are
   thesis-first. Dust, incomplete exits, skipped rotations, manual activity, and
   stale-snapshot mismatches can create actual positions outside both systems.
3. **Execution freshness and cross-file consistency.** There is no product-age
   gate or shared generation identifier across product, snapshot, quotes, plan,
   governance, broker dumps, and OHLC panels.
4. **Broker reconciliation and outcome correctness.** Reconciliation trusts a
   partial agent-produced dump; fill recording can duplicate; outcome identity
   follows nightly regenerated theses; partial exits are not outcome-labeled.
5. **Unbound or degraded guardrails.** Multiple config keys have no consumer,
   control-binding telemetry is not operational, and missing data can remove
   protections without a unified degraded-mode decision.

### Medium

6. **Control-binding monitoring is not operational.** The system designed to
   detect disconnected guards does not yet gather live evidence.
7. **The aggregate test runner is broken and incomplete.** CI does not supply a
   complete alternative.
8. **Documentation is stale.** `README.md`, `docs/DESIGN.md`, `docs/DEPLOY.md`,
   strategy comments, and several source docstrings retain removed Schwab flows.
9. **State writes have inconsistent atomicity and locking.** Concurrent processes
   can observe mismatched generations of related files.
10. **Market-calendar handling is simplistic.** Holidays and half-days are not
    modeled.
11. **Configuration is not uniformly authoritative.** Several strategy keys are
    duplicated, ignored, or represented by code defaults rather than explicitly
    passed values.
12. **Deployment is not reproducible from the repository alone.** SDKs, services,
    CLI tools, versions, and credentials are substantially out of band.

### Deliberate offline or incomplete areas

- `src/concentration.py` is tested but deliberately abandoned from the live path.
- `src/ti_signals.py` and `scripts/ti_replay.py` remain offline research.
- Some moomoo insider, institutional, and earnings-price-move features are unwired.
- ETF capital-flow values can silently null while the collection reports no gap.
- Removed Schwab bytecode remains locally, although no tracked Schwab source
  package remains.
- Historical plans and SDD artifacts contain useful provenance but can be confused
  with current behavior.

## 12. Questions and unresolved unknowns

1. What freshness limit should execution enforce, and should stale data permit
   exits while blocking buys?
2. Is regime-off intended to liquidate the stock book or only prevent entries?
3. Should every actual Robinhood holding be monitored even when off-book?
4. What are the currently effective box-local live and alert-only switches?
5. Are installed cron, systemd, OpenD, Cloudflare, and timezone settings healthy?
6. Is branch protection still configured as documented?
7. Are required GitHub and provider credentials present and unexpired?
8. Do current Robinhood MCP schemas exactly match prompt assumptions?
9. What is the policy for partial fills, pending orders, and externally created
   positions?
10. Should the 21-day moving-average rule become deterministic, remain advisory,
    or be removed from the strategy prose?
11. Should control-binding telemetry be completed before new guards are added?
12. What at-rest protection and process privilege model is required for live state?

## 13. Recommended next steps

1. Enforce research freshness in fast-loop preflight, with distinct buy/exit rules.
2. Reconcile actual holdings against watched holdings and alarm on any unprotected
   position.
3. Decide and codify the regime-off liquidation policy.
4. Repair the two-runtime selftest runner, cover every selftest, and run it in CI.
5. Add store-to-plan-to-prompt integration tests using recorded broker fixtures.
6. Connect the control-binding registry to live evidence and health alerts.
7. Audit every strategy key for a real consumer; remove or flag dead/duplicate keys.
8. Remove Schwab-era instructions from current documentation and docstrings.
9. Use atomic writes and locking/version stamps for cross-process state.
10. Add exchange holiday and early-close awareness.
11. Reduce service privileges and remove embedded account identifiers from prompts.
12. Pin runtime dependencies and GitHub Actions reproducibly.
13. Define a central degraded-data policy instead of independent fail-open choices.
14. Document recovery for OpenD, Claude auth, Robinhood auth, stale snapshots, and
    incomplete exits.
15. Require sufficient live evidence before adaptive proposals can be promoted.

## Inspection limits

The inspection did not read or expose secret values, raw account balances, full
positions, unnecessary P&L, private keys, OAuth stores, the private ledger mirror,
or large generated datasets. It did not make broker calls or verify installed
cron/systemd/Cloudflare/OpenD state. Repository settings and secret validity also
require external administrative access.

## Working mental model

This is a deterministic dual-momentum portfolio engine wrapped in an agentic
execution shell. moomoo supplies prices, FRED supplies VIX, Finnhub supplies
earnings, and Alpaca supplies news and point-in-time research data. The slow loop
produces a validated ten-stock/four-ETF target book in a file-backed Research Store.
The fast loop marks actual Robinhood holdings, diffs them against targets, applies
governance, and emits an order plan. Headless Claude performs MCP-only broker steps
with deliberately narrow discretion. A continuous monitor enforces software stops
and targets; a twice-daily reviewer can only reduce risk. JSON, JSONL, and Parquet
are shared memory between processes. Operational correctness depends on freshness,
cross-process file coherence, OpenD availability, prompt adherence, and keeping
every actual holding inside the protection envelope.

## Context brief for future development prompts

This is a live-money, long-only Robinhood system. Read `CLAUDE.md` first. Trade
only the single account whose live response says `agentic_allowed=true`; every
other account and every non-Robinhood provider is read-only. The signal is
deterministic dual momentum over a 150-stock universe plus an ETF sleeve. moomoo
via OpenD supplies prices and quotes, FRED supplies VIX, Finnhub supplies earnings,
and Alpaca supplies news and point-in-time research data. Most code uses the
Python 3.12 `.venv`; anything importing moomoo must use system Python 3.10.

The slow loop runs Sunday and every weekday evening. It rebuilds entry zones,
stops, targets, dates, and decision IDs nightly, even when banding preserves
membership. Its held set comes from the previous target product, not the broker.
Regime-off clears stock targets while leaving the ETF sleeve active. Several
strategy keys are descriptive rather than bound to production code.

The fast loop requires Claude because Robinhood is MCP-only. Python creates
`order_plan.json`; Claude fetches broker state, reviews, places, and records
orders. There is no fast-loop research-age check. Global kill-switch and drawdown
halts suppress sells as well as buys. Full exits must use exact share quantity.

The monitor polls moomoo during weekday RTH and watches only actual snapshot
positions that also have current weighted theses. Stops are software-only, and
off-book holdings are not protected. Risk review is defensive-only and normally
alert-only; it likewise omits positions without current theses. Overrides can
only tighten, but the next nightly base geometry can move in either direction.
The 21-day moving-average rule is a flag, not an automatic exit.

The JSONL ledger has no inter-process lock. Reconciliation sees only the
agent-written order dump, and a missing dump is not an error. Fill recording is
not idempotent. Outcome labels currently attach to regenerated nightly theses
rather than an immutable entry record.

Before changing a trading path, trace its producer and every consumer, verify that
each configuration key is actually read, distinguish target holdings from broker
holdings, preserve intended exit ability during safety halts, test under the
correct Python runtime, and judge risk by mechanism rather than account size.
