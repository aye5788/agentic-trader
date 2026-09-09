# STRATEGY.md — the trading strategy, written for the agent that will trade it

**You are reading this because you are the deployed agent.** You booted blank.
You have no memory of how this strategy was chosen or of your last run. This
document + [`config/strategy.toml`](../config/strategy.toml) (machine params) +
the **Research Store** (what your last slow-loop self wrote) are your entire
brain for this run. Read this fully before you act.

- **Why the strategy is this way:** [`docs/DESIGN.md`](DESIGN.md).
- **Hard safety rules:** [`CLAUDE.md`](../CLAUDE.md). They override everything here.
- **This file** = *what to compute and how to decide.* Numbers live in
  `strategy.toml`; if a value here and there ever disagree, `strategy.toml` wins
  and you should flag it.

Golden rule of division of labor: **the ranking/sizing is deterministic math —
run it as code, do not eyeball it.** Your judgment is for orchestration,
verification, edge cases, and the guardrails. You are the hands and the
supervisor, not the calculator.

---

## 1. The edge (one paragraph)

**Hybrid dual momentum**, long-only, **equities only (options are OFF)**, swing
horizon (days–weeks). Two independent signals must agree for you to hold a name:
it must be **strong relative to its peers** (cross-sectional rank) **and** in its
**own uptrend** (absolute filter). Winners tend to keep winning over a
6–12-month horizon; the absolute filter is what takes you to cash when the trend
breaks, since long-only has no short leg to hedge you.

## 2. Universe — eligibility, scoring, and selection are THREE things

⚠️ This section described **two parallel engines** until 2026-08-20. There is
one: the single-name book. Equities only.

⛔ **AND SINCE 2026-09-09 IT DESCRIBES TWO MODES, ONE OF WHICH IS NOT YET
SWITCHED ON.** Read `[universe] mode` in `config/strategy.toml` — that key is
the whole answer, and this page defers to it:

| `mode` | what is your hunting ground |
| --- | --- |
| `fixed_list` (**what runs today**) | the curated 150 in `config/universe.csv` |
| `cohort` | the persisted **eligibility cohort**, `research_store/universe/cohort.json` |

**The distinction the cohort mode exists to draw**, because one file used to
answer all three questions at once and that is what made the pool a list
somebody typed:

- **ELIGIBILITY** — *is this security liquid and safe enough to consider?* A
  fact about the instrument, established weekly by the V2 liquidity screen over
  the whole US market. Deterministic code decides it.
- **SCOREABILITY** — *can `momentum.compute()` produce a number for it?* A fact
  about **our price panel**, established daily by `fetch_prices.py`. Also
  deterministic.
- **SELECTION** — *which of the eligible, scored names should this book hold?*
  **Yours.** Nothing in the eligibility or scoring layers picks a position.

An eligible name that is **not scoreable** is a **research lead**: you can see
it, read it and form a view on it, and you **cannot buy it**. That is not
bureaucracy. This book is cross-sectional momentum — a name with no computed
score cannot be compared against the names that have one, so buying it would
mean choosing on prose where every other holding was chosen on the measurement.
The order gate enforces this independently of anything you decide.

Two states carry that: `eligible_pending_history` (too recently listed to have
252 sessions, or its history is queued behind moomoo's 100-distinct-symbol
quota — see §2a) and `eligible_unscoreable` (the history repair failed for a
provider reason). `universe()` shows both, with reasons.

⛔ **A name you HOLD that leaves the cohort is still sellable, still monitored,
and still carries its stop.** Eligibility is an ENTRY question. Stops here are
software, so a gate that could refuse an exit would strip an open position of
its only protection.

### 2a. Why the cohort can be broad and the candidate set cannot grow as fast

moomoo's **screening is unmetered** but `request_history_kline` is capped at
**100 distinct symbols per rolling window, account-wide**. This repo is the
current primary consumer, but the cap is a broker-level limit rather than a
private process allowance. So ranking the whole market weekly costs
nothing, while giving ~200 names a 252-session window cannot be done in one run
at all. `src/quota_planner.py` rations it — current holdings first, then the
names closest to becoming scoreable, then cohort rank — and names past the
budget are **deferred with a reason and retried**, never dropped. This asymmetry
is the shape of the system, not a defect: it is exactly why "eligible" and
"candidate" are different words here.

### 2b. The curated list (what runs today)

- **Single-name book** — the 150 in [`config/universe.csv`](../config/universe.csv),
  **rescreened every Friday** (17:00 ET) by `scripts/universe_refresh.py`. ⚠️ This
  was a QUARTERLY job that had never once fired (armed 2026-07-20, zero runs), so
  the pool was frozen at its inception list; changed 2026-08-20. Seeds are
  protected and incumbents are kept while their liquidity rank holds — it is a
  liquidity screen, not a signal.
  **Ranked on 20-DAY average dollar turnover since 2026-09-06**, over the whole
  US market above the market-cap floor, on real US exchanges. It previously took
  the top 400 names BY MARKET CAP and ranked those on ONE session's volume, which
  made the effective cap floor ~$57B instead of the configured one and hid 1,084
  names that met both standing policies. The thresholds themselves did not change
  — see `[universe_maintenance]` in `config/strategy.toml`, which is authoritative.
  ⛔ There are exactly two outcomes and NEITHER waits on a human: AUTO_APPLY, or
  NO_CHANGE when the data contradicts itself (the last-known-good universe then
  stands and the condition self-clears). There has been no HOLD-for-review state
  since 2026-08-27; this page claimed otherwise until 2026-09-06.
  Respect the `flag` column: `fresh-ipo` (e.g. SpaceX) has no 12-month history —
  it is **unrankable until it seasons**; skip it, don't crash on it. `adr` /
  `micro` / `spec` are tradeable but note them.
- ~~**ETF sleeve**~~ — **DELETED 2026-08-20** (retired 08-16, positions sold
  08-17). ⛔ **You cannot buy a fund.** `config/etf_universe.csv` is gone, the
  order gate's buy-eligibility set is single names alone in **either** mode
  (the cohort's adds pass a positive market-cap test, which a genuine fund
  fails because moomoo serves it no market cap, plus a denylist backstop), and
  a buy naming a fund is refused like any other ineligible symbol. (A SELL is
  never refused.)
  You will still see sector tickers in the price panel: the signal regresses
  each name on its sector to measure its own strength. That is read-only market
  data, not an instrument. The design
  intent was that the defensive assets (GLD, TLT, AGG) sat *inside* the rank, so
  that when equities weakened they rose to the top and you rotated into them —
  a built-in off-switch destination. With the sleeve retired there is no
  automatic rotation into them; going defensive is a judgement you make and
  record, like any other.

## 3. The signal — compute this exactly (both engines)

Inputs: daily closes from the cached OHLC panel in `research_store/prices/`
(~13 months / ≥260 trading days needed). The panel is appended to daily from
moomoo; its deep history is Schwab-era and non-regenerable.

For each ticker:
1. **12-month return** `R = close_today / close_[252 trading days ago] − 1`.
   Window is **12-0**: includes the most recent month (no skip).
2. **Trailing volatility** `σ = stdev(daily returns over the same 252 days)`.
3. **Return view** = `R / σ` (risk-adjusted momentum).
4. **Trend view** = `close_today / SMA200 − 1` (distance above the 200-day mean).

⚠️ **The ranking is over the SINGLE NAMES only, and it carries the residual
tilt** (`[signal] residual_tilt`, 0.75 sector variant). Until 2026-08-20 the
agent-facing `candidates()`/`universe()` did neither — no tilt, and all 18 ETFs
pooled into the same percentile — so the list the agent read could order the same
names differently from the list the book was built from. One ranking now:
`screen.rank_book()` and `scripts/slow_loop.py` both go through
`residual.kwargs_from_config()`, and a selftest asserts the two agree.

⛔ **AND ONE POOL, since 2026-09-09.** `score` is a PERCENTILE, so *who is in
the pool defines it* — two callers ranking slightly different name sets do not
produce almost the same numbers, they produce different numbers for every name
in common. `screen.ranking_pool()` is the single implementation of "which names
are ranked"; `candidates()`, `universe()` and `scripts/slow_loop.py` all call
it. Fixing the signal in 2026-08-20 closed half of this; reading the pool from
a file in each caller would have re-opened the other half.

**Relative rank score** (no tunable weights — do not add any):
- percentile-rank the *Return view* across the eligible universe → `p_ret`
- percentile-rank the *Trend view* across the eligible universe → `p_trend`
- **composite = (p_ret + p_trend) / 2**, ranked descending.

**Absolute gate:** a ticker is *eligible* only if **`R > 0`**. Fail the gate →
excluded from the held set (it can still be computed, but never held).

## 4. Portfolio construction

⚠️ **These are the shapes `config/strategy.toml` actually holds. The TOML is
authoritative — read `[portfolio]` rather than trusting this paragraph.**

- **Single-name book:** hold the **top `book_hold`** eligible names (14).
  **Banded exit:** a name you already hold is kept until it falls below rank
  `book_band` (20); a new name is added only when it is in the top `book_hold`
  and a slot is open. (The band stops churn at the rank boundary.)
- **ETF sleeve: RETIRED 2026-08-16.** `[etf_sleeve] enabled = false`,
  `sleeve_hold = 0`, `sleeve_weight = 0.0`, `book_weight = 1.0` — the single-name
  book is the whole book. The four held ETFs were sold on 2026-08-17; retiring a
  sleeve in config does not sell anything, so that was a separate decision.
  Note `book_hold` moved 10 → 14 at the same time, deliberately: dropping the
  sleeve would otherwise have concentrated per-name size from ~7% to 10%.
- **The sleeve is deleted, not disabled.** `[etf_sleeve]`, the sleeve keys in
  `[portfolio]`, the second ranking engine and `config/etf_universe.csv` are all
  gone as of 2026-08-20. What survives is the 11 sector **price series** the
  residual tilt regresses on (`src/residual.py:SECTOR_FACTORS`) and SPY for the
  regime line — factor inputs, not holdings.
- **Sizing:** obey the `[risk]` mandate in `strategy.toml` — ≤10% per name,
  ≤`max_holdings` holdings, ≤100% invested, and **reward:risk ≥ 2:1** (the
  Research Store rejects any thesis that violates this on write; if a name can't
  be given ≥2:1 geometry, it doesn't get held).

## 5. Cadence

- **Friday 17:00:** **rescreen the universe** — the candidate pool itself
  (§2), refreshed on trailing dollar-volume before the evening's ranking sees it.
- **Weekly (Sunday):** full rebalance — recompute §3, apply the §4 bands, rotate.
- **Nightly (between rebalances):** recompute the ranking and *exits* — stop
  breaches, 21-day MA breaks, regime flips. A blowup does not wait for the weekly
  cycle; only the reassessment of *winners* is weekly.
  ⚠️ The nightly re-rank used to be **discarded**: rotation is weekly, so Mon–Fri
  the fresh ranks were computed and thrown away and nothing ever saw them. Since
  2026-08-20 each run records its ranking (`research_store/ranks/`) and `brief()`
  shows the agent what moved — entries and exits from the top 20, absolute-gate
  flips, the biggest rank moves, and any universe churn from the Friday screen.
  It still rotates nothing: what the movement means is the session's call.

## 6. Trade management (per position, IBD-derived)

- **Entry:** no chasing — **ASYMMETRIC**. A buy is skipped only when the live
  price has run *above* `entry_zone[1]` by more than `chase_tol_sigma` × the
  name's daily sigma. A price *below* the zone is a better fill and is **never**
  blocked. (This doc used to say "if live price is outside it, skip" — that
  wording would also refuse a cheaper price, which is nonsense; on 2026-07-28 it
  would have skipped XLK at 3.8% *below* its zone.) The tolerance is vol-scaled
  because `entry_zone` is a flat ±0.5% band while the rest of the geometry
  scales with vol: flat is noise for a 7%/day mover and a straitjacket for a
  1.5%/day ETF. ⛔ **NOT ENFORCED ANYWHERE since 2026-08-14** — the only
  implementation was `fast_loop.apply_chase_guard`, which went with the deleted
  executor. `[trade_management] no_chase` + `chase_tol_sigma` (0.5σ) remain in
  config as the documented default for a judgement the SESSION now makes;
  `brief()` and `terrain()` carry the entry zone it was computed from. Stated
  plainly rather than left implied: this clause was documented as enforced for
  months while wired to nothing, and §7 records what that cost.
- **Stop:** volatility-adjusted (below the recent swing low / an ATR multiple) —
  **not** a flat 2–3%.
- **Targets:** tiered scale-out at **multiples of risk** (R = entry−stop):
  ~2.2R then ~4R. Scaling targets to each name's volatility (not a fixed +5/+10%)
  keeps reward:risk ≥ 2:1 by construction — a fixed +5% target is unreachable at
  2:1 for a 5%/day mover and would reject the whole book.
- **MA exit:** exit if daily close < the 21-day MA, even before a target.

## 7. Regime — an observation, not a gate

⚠️ **CHANGED 2026-08-14. This section described a mechanical gate for months
after the gate stopped existing in one form and was removed in the other.**

- **What it is:** **$SPX > its 50-day MA** (from the cached panel) and **VIX ≤
  28** (FRED `VIXCLS`). The slow loop computes it, records it on the product,
  and `brief()` reports both the live SPY reading and the recorded compound
  call.
- **It does not gate anything.** It does not filter the selection, it does not
  refuse entries, and it does not liquidate. What a regime call means for this
  book is the session's judgement — the session can see the positions, the
  marks and the reason; a rule about SPY cannot.
- **Why it was removed.** The first form set the target book to EMPTY, which
  the execution pass read as "sell everything": eleven positions closed in one
  minute on 2026-07-27, worst −18.16%, essentially this book's entire drawdown.
  The second form kept holdings but refused new entries. Both overrode an
  accountable agent with a signal about an index. The deterministic executor
  that consumed the product was retired the same day, so a selection is now a
  proposal to a session rather than an order — filtering it would only hide
  candidates from the party able to weigh them.
- **⚠️ The backtests in §9 still model regime-gated entry.** Their numbers
  assume entries were suppressed when the floor was off. Live behaviour no
  longer matches that assumption, so treat those figures as evidence about the
  SIGNAL, not as a forecast of the system as it now runs.
- Standing aside remains a valid, intended state — as a decision, with a stated
  reason, like any other. Names already held are released by the ordinary exit
  discipline, not dumped because SPY crossed a line. There is no hedge and no
  inverse ETF; unspent weight is cash. (In the sleeve, the defensive ETFs may
  still rank in; that is
  allowed and is the same idea.)
  - ⚠️ This wording changed 2026-08-12. It previously read "hold nothing", and
    the code matched it: a regime flip sold the entire single-name book. It
    fired once, 2026-07-27 — nine single names closed in one tick, mean -7.65%.
    Both backtests had modelled the keep-held behaviour since July, so the live
    code was the outlier, not the docs' intent.
  - **What a regime call means is yours to judge — entering as well as
    exiting.** As of 2026-08-14 the loop does not decline to add either: it
    ranks, records the regime as a fact, and proposes. You can see the
    positions, the marks and the reason, and a rule keyed on one index cannot.
- **⚠️ THE MECHANICAL ENTRY GUARDS ARE GONE, 2026-08-14.** `no_chase` (never
  buy above the thesis entry zone) and the `[reentry]` 4% knife-guard were both
  enforced inside `scripts/fast_loop.py`, and that script was deleted with the
  procedural executor. Their config keys still exist and are now READ BY NOTHING
  — kept as the documented defaults for a judgement the session makes, not as
  gates. `brief()` and `terrain()` carry the entry zone and the excursion data
  those rules were derived from.
  This is a real change in behaviour and it is stated rather than buried: the
  `no_chase` ceiling would have blocked LITE at +5.0% over its zone on
  2026-07-23, which then exited −18.1%. What replaces it is the session
  looking at the same number and deciding.
  What remains ENFORCED at the order gate, unbypassably: the kill switch,
  live_approved, HALT_ENTRIES, the automatic drawdown halt, the per-order cap,
  the buy-eligibility check (§2), and an active `rule_out`.

## 8. Execution — the sessions, with judgment

⚠️ **CHANGED 2026-08-14. This section described a procedural fast loop that no
longer exists.** `scripts/fast_loop.py` diffed the stored book against holdings
and placed the difference at 10:00 — 35 minutes before the open session reasoned
— so it moved first every day and the session spent its run undoing it. It is
deleted. Nothing executes the slow loop's output; that output is a PROPOSAL to a
session, not an order.

Execution is now two agent sessions per weekday (10:35 and 15:15), each handed
`prompts/charter.md` and its own judgment rather than a procedure. What holds
regardless of who is executing:

1. `get_accounts` → act **only** in the one account with `agentic_allowed = true`
   (nickname **"Agentic"**, `type: limited_margin` since 2026-08-18). If zero or
   more than one match, or it is ambiguous — **abort and trade nothing.**
2. `get_equity_positions(that account)` → actual holdings. The cached snapshot is
   not authoritative; the broker is.
3. Size against `buying_power`. **Fractional. Equities only. Options OFF** — the
   account carries no option level, so the surface is absent, not merely barred.
4. Every order passes the PreToolUse order gate, which runs in the harness and
   cannot be skipped by forgetting. It refuses on the kill switch, `live_approved`,
   `HALT_ENTRIES`, the automatic drawdown halt, an active `rule_out`, the
   per-order cap, the buy-eligibility check (§2 — the curated list under
   `mode = "fixed_list"`, the scoreable eligibility cohort under `"cohort"`)
   and shadow mode. **A sell is refused by
   nothing but the kill switch** — stops here are software, so blocking a sell
   would remove a position's only protection.
5. **Never** read-for-decision or write to any other account. Every non-Agentic
   account is off-limits for trading.

## 9. Before you go live — the proof gate

**Do not place live orders until the signal has been backtested against history
and a human has approved it.** This is v1 with real money; correctness first. If
the gate is unmet, you may compute and write research, but you may not trade.

---

*Params that this prose describes live in `config/strategy.toml`
(`[meta] [risk] [signal] [trade_management] [universe] [etf_sleeve] [regime]
[proof]`). Keep the two in sync; the TOML is authoritative for numbers.*
