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

## 2. Universe — two parallel engines, one signal

Both are ranked by the *identical* signal in §3; they are just two lists.

- **Single-name book** — the fixed 150 in [`config/universe.csv`](../config/universe.csv).
  Respect the `flag` column: `fresh-ipo` (e.g. SpaceX) has no 12-month history —
  it is **unrankable until it seasons**; skip it, don't crash on it. `adr` /
  `micro` / `spec` are tradeable but note them.
- **ETF sleeve** — the 18 in [`config/etf_universe.csv`](../config/etf_universe.csv).
  The defensive assets (GLD, TLT, AGG) are *inside* the rank on purpose: when
  equities weaken they rise to the top and you rotate into them — that is the
  built-in off-switch destination, not a separate rule.

## 3. The signal — compute this exactly (both engines)

Inputs: daily closes from Schwab price history (~13 months / ≥260 trading days).

For each ticker:
1. **12-month return** `R = close_today / close_[252 trading days ago] − 1`.
   Window is **12-0**: includes the most recent month (no skip).
2. **Trailing volatility** `σ = stdev(daily returns over the same 252 days)`.
3. **Return view** = `R / σ` (risk-adjusted momentum).
4. **Trend view** = `close_today / SMA200 − 1` (distance above the 200-day mean).

**Relative rank score** (no tunable weights — do not add any):
- percentile-rank the *Return view* across the eligible universe → `p_ret`
- percentile-rank the *Trend view* across the eligible universe → `p_trend`
- **composite = (p_ret + p_trend) / 2**, ranked descending.

**Absolute gate:** a ticker is *eligible* only if **`R > 0`**. Fail the gate →
excluded from the held set (it can still be computed, but never held).

## 4. Portfolio construction

- **Single-name book:** hold the **top 10** eligible names. **Banded exit:** a
  name you already hold is kept until it falls **below rank 15**; a new name is
  added only when it is in the **top 10** and a slot is open. (The band stops
  churn at the rank boundary.)
- **ETF sleeve:** hold the **top 4** eligible ETFs by the same composite.
- **Capital split:** **70% to the single-name book, 30% to the ETF sleeve**
  (fixed). Size positions *within* each engine's allocation — the book's 10 names
  share its 70%, the sleeve's 4 ETFs share its 30%.
- **Sizing:** obey the `[risk]` mandate in `strategy.toml` — ≤10% per name, ≤10
  holdings, ≤100% invested, and **reward:risk ≥ 2:1** (the Research Store rejects
  any thesis that violates this on write; if a name can't be given ≥2:1 geometry,
  it doesn't get held).

## 5. Cadence

- **Weekly:** full rebalance — recompute §3, apply the §4 bands, rotate.
- **Nightly (between rebalances):** recompute *exits only* — stop breaches,
  21-day MA breaks, regime flips. A blowup does not wait for the weekly cycle;
  only the reassessment of *winners* is weekly.

## 6. Trade management (per position, IBD-derived)

- **Entry:** no chasing — only buy inside the thesis `entry_zone`; if live price
  is outside it, skip.
- **Stop:** volatility-adjusted (below the recent swing low / an ATR multiple) —
  **not** a flat 2–3%.
- **Targets:** tiered scale-out ~+5% then ~+10%.
- **MA exit:** exit if daily close < the 21-day MA, even before a target.

## 7. Regime gate & the off-switch

- **Mechanical floor (ON switch):** new entries allowed only if **$SPX > its
  50-day MA** and **VIX ≤ 28** (computed from Schwab; FRED is supplementary).
- **Off = cash.** When the floor is risk-off, or nothing passes the absolute
  gate: **hold nothing. Standing aside is a valid, intended state.** There is no
  hedge and no inverse ETF — cash is the position. (In the sleeve, the defensive
  ETFs may still rank in; that is allowed and is the same idea.)
- Agent qualitative overlay is **v2** and OFF-only when it exists (it may veto or
  downsize, never green-light). For now the gate is deterministic.

## 8. Execution — the fast-loop procedure

1. `get_accounts` → select the **one** account with `agentic_allowed = true`
   (nickname **"Agentic"**, a cash account). If zero or more than one match, or
   it's ambiguous — **abort and trade nothing.**
2. `get_equity_positions(that account)` → your actual holdings.
3. Read the target theses the slow loop wrote to the Research Store.
4. Diff target vs actual → the order list.
5. Per order: `review_equity_order` → (human approval if configured) →
   `place_equity_order`. **Fractional. Equities only. Options OFF.**
6. **Never** read-for-decision or write to any other account. Every non-Agentic
   account is off-limits for trading.

## 9. Before you go live — the proof gate

**Do not place live orders until the signal has been backtested against history
and a human has approved it.** This is v1 with real money; correctness first. If
the gate is unmet, you may compute and write research, but you may not trade.

---

*Params that this prose describes live in `config/strategy.toml`
(`[meta] [risk] [signal] [trade_management] [universe] [etf_sleeve] [regime]
[proof]`). Keep the two in sync; the TOML is authoritative for numbers.*
