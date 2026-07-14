# Intraday risk-management overlay — design spec

**Date:** 2026-07-14
**Status:** design approved (brainstorm), pending implementation plan
**Author:** Aaron + Claude (brainstorming session)

---

## 1. Goal

Give the system an active, **defensive risk-management brain** that tends open
positions *between* the weekly rebalances, instead of leaving each holding on a
single frozen catastrophe stop until the next Monday rebuild.

Today the only thing standing between a position and a bad outcome is one
vol-scaled hard stop (full liquidation) plus a fixed take-profit ladder — all
three numbers frozen at entry, never adapted. The `ma_exit_days = 21` rule the
strategy already declares is **specced but dead** (enforced nowhere). There is
**no downside trimming and no adaptive geometry** at all.

This overlay adds a rules-founded, judgment-assisted layer that can trim, exit,
and — critically — **adjust a position's stop and take-profit as conditions
evolve**, always in the risk-reducing direction only.

## 2. Design principles (the non-negotiables)

1. **Strict mechanical rules are the foundation; the LLM adds judgment only where
   things don't fall neatly.** The systematic edge is preserved; the AI is a
   defensive overlay, never a discretionary trader.
2. **One-way, de-risk only.** Every action and every geometry change may only
   make a position *safer*: stops trail up / tighten / go to breakeven; targets
   pull *in* to bank sooner; positions trim or exit. **Never** loosen a stop,
   **never** extend a target, **never** open a new entry. This is enforced in
   code, not trusted to the prompt.
3. **The free, always-on monitor watches so the LLM doesn't have to.** Continuous
   watching + the catastrophe stop stay pure Python. The LLM runs on a bounded
   schedule, not a polling loop.
4. **Nothing dead-ends in a file.** Every produced output terminates at one of
   the system's two actuators (the monitor, or an agentic MCP order) or is an
   explicit note. No collected-but-unused data.
5. **Ships SAFE.** Alert-only until explicitly armed, exactly like the existing
   loops (`live_approved`).

## 3. Scope

**In scope (this spec):** position-level risk management of *currently held*
names — trim/exit decisions and one-way geometry adjustment, driven by two
scheduled agentic reviews plus the existing real-time monitor.

**Used as inputs but not as separate engines:** relative strength / momentum-rank
drift, symbol-tagged news, VIX/regime — these inform the per-position judgment.

**Explicitly out of scope / deferred to v2:**
- Portfolio-level market-wide risk-off (de-risking the whole book on an index
  break / VIX spike) as its own mechanism.
- News-driven autonomous reaction outside the scheduled passes.
- Relative-strength *rotation* (selling a fading leader to buy strength) — that is
  an offensive/entry action; this overlay is defensive only.
- Continuous *mechanical* stop-trailing in the monitor between passes (all
  geometry changes flow through the judgment passes in v1).
- Rendered chart images (the passes reason over numeric price/technical series).

## 4. Architecture

Three layers, ordered by how often they run.

### Layer 1 — the always-on monitor (unchanged role, one new overlay step)
`scripts/market_monitor.py`, pure Python, every 15s during RTH, free. Continues to
enforce the **Tier-1 catastrophe stop** (full exit) and the take-profit ladder in
real time. The LLM is never in this loop. This is the safety net that covers a
fast breakdown *between* the scheduled passes.

**One addition:** before checking each held name's stop/targets, the monitor
overlays `research_store/monitor/overrides.json` — any *stricter* stop/target the
risk review has written for that name. Overlay is the same pattern `src/marks.py`
already uses to overlay `quotes.json`. The monitor re-checks stricter-only as
defense-in-depth: a stop override is honored only if it is **≥** the stored stop;
a target override only if **≤** the stored target. A malformed/looser override is
ignored, never enforced.

### Layer 2 — two scheduled agentic risk reviews
A single templated procedure, `prompts/risk_review.md`, fired by cron twice per
trading day (ET, matching the monitor's `America/New_York` clock):

- **~12:00 ET — midday check.** Intraday. May act *now* (trim / exit / tighten /
  lower TP) when a name warrants it.
- **~15:45 ET — end-of-day tend.** Same scan; job is setting up for the overnight
  gap and next session: tighten stops, lower TPs, queue risk-management orders for
  the next open, and leave watch-notes. (Runs before 16:00 so orders can place
  pre-close.)

Both passes review **every held name** (≤14) unconditionally — no wake-up trigger
needed at this book size on a subscription plan.

### Layer 3 — deterministic core + agentic procedure (the slow/fast pattern)
Mirrors `slow_loop.py` ↔ `fast_loop.py` so guardrails are code, not prompt-trust:

- **`scripts/risk_review.py`** (deterministic, no LLM, no placement): builds the
  per-name checklist facts; validates any agent-proposed change against the
  one-way invariant + `[risk]` mandate; writes `overrides.json` /
  `deferred_intents.json`; appends the `risk_review` journal event. Has
  `--selftest` covering the invariant and overlay logic.
- **`prompts/risk_review.md`** (agentic): the judgment — reads the facts, decides
  per name, and performs any *immediate* placement via the RH MCP.

## 5. The per-position checklist

For each held name, `risk_review.py` assembles a compact, readable text/numeric
readout (no images). Ranked by decisiveness:

1. **Relative strength & rank drift** — recent return vs SPY; current
   rank/score from `current.json` and distance to the band-exit (rank 15). A
   leader that stops leading is thesis erosion *before* a chart break. *(Most
   on-strategy signal.)*
2. **Price vs 21- & 50-day MA** — revives `ma_exit_days`; the 21-day is the
   IBD "sell into weakness" line, the 50-day the deeper trend.
3. **Give-back from high-water mark since entry** — catches a sharp reversal off
   a run-up, protecting open profit before price reaches the MA.
4. **Distance to catastrophe stop / open-profit-at-risk** — how much is actually
   on the line right now (PnL reframed as risk, not score).
5. **Volatility expansion** — daily sigma now vs the sigma stored at entry
   (`slow_loop` records it); a blown-out vol means the position is riskier than
   it was sized for.
6. **Earnings proximity** — earnings within N days (`Thesis.review_by` /
   `get_earnings_calendar`); the biggest uncontrolled overnight-gap risk.
7. **Material news** — symbol-tagged headlines since the last pass (Alpaca).

**Shared backdrop (computed once):** VIX vs the 28 ceiling; SPX vs its 50-day
(regime on/off). Sets how defensive to be today across the whole book.

## 6. Output: verdict + bounded action menu

Each name gets a **verdict** — `healthy` / `watch` / `de-risk` — with a one-line
reason. The review **defaults to HOLD** and only proposes an action when a
concrete flag trips; `watch` is the pressure-relief valve (note concern, touch
nothing). This is the guard against manufacturing activity (daily micro-adjusting
is overtrading, just quieter).

A `de-risk` verdict picks from the **de-risk-only** menu:
- **Tighten stop** — trail up / to breakeven / tighter.
- **Lower take-profit** — pull in to bank sooner (materially weakened thesis).
- **Trim** — partial exit now.
- **Exit** — full exit now.
- **Watch-note / deferred intent** — "if X by date Y, do Z" (mainly the close pass).

## 7. Actuation map — produced → stored → actuated

The system has exactly **two actuators**; every output ends at one or is a note:
(a) **the monitor** turns stored price *levels* into real-time sells; (b) **an
agentic pass** turns a *decision* into a placed order via the RH MCP.

| Output | Stored in | Consumer → action |
|---|---|---|
| Tighten stop / lower TP | `monitor/overrides.json` `{sym:{stop?,targets?,ts,reason,expires}}` | **Monitor** overlays & enforces the stricter level live within 15s. A tightened stop *is* an armed sell trigger. |
| Trim / exit now | placed immediately | **The pass** places via RH MCP (`review→place`), journals via `record_fills.py`, phone push. Same path as `exit.md`. |
| Queued order (next open) | `monitor/deferred_intents.json` | Price-conditional → written as an override, monitor triggers it mechanically. Judgment-conditional → re-evaluated by the next pass. |
| Watch-note | `monitor/deferred_intents.json` | Surfaced into the next pass's checklist for resolution. No order. |
| Verdict + reasoning | journal `{"event":"risk_review",…}` | Audit trail + newsletter facts + continuity. Does not actuate. |

**Override mechanism:** non-destructive overlay on top of the product's stored
levels — the product (the validated weekly belief) is never mutated in place.

**Reconciliation — bounded lifetime, no drift:** overrides are intra-week
overlays. The weekly `slow_loop.py` rebuild recomputes geometry from scratch for
held names and **clears that week's overrides**. A stop tightened on Tuesday
holds all week and is reset at the next weekly rebuild.

## 8. Guardrails (enforced in `risk_review.py`, before anything actuates)

- **One-way invariant:** written stop ≥ current stop; written TP ≤ current TP;
  trim/exit only reduce size; no new entries. Any violation is rejected before it
  can persist. Re-checked by the monitor on overlay (defense-in-depth).
- **`[risk]` mandate round-trip:** adjusted geometry must still satisfy the store's
  validation (R:R, stop-below-entry, etc.).
- **`live_approved` / `alert_only`:** unarmed → the pass writes verdicts +
  watch-notes + a phone push and places **nothing** and writes **no overrides**.
- **Governance:** kill-switch, drawdown halt, per-order cap, whitelist all still
  apply (reuse `src/governance.py`).

## 9. New / changed components

- **`scripts/risk_review.py`** (new) — deterministic core + `--selftest`.
- **`prompts/risk_review.md`** (new) — agentic procedure for both passes (mode =
  midday|close passed in or inferred from time).
- **`scripts/market_monitor.py`** (change) — overlay `overrides.json` (stricter-
  only) when reading each held name's levels; optionally evaluate price-conditional
  deferred intents.
- **`research_store/monitor/overrides.json`** (new artifact) — geometry overlay.
- **`research_store/monitor/deferred_intents.json`** (new artifact) — watch-notes +
  queued conditional intents.
- **`config/strategy.toml` → `[risk_review]`** (new) — `enabled`, pass times,
  `ma_break_days` (=21), give-back / vol-expansion attention thresholds,
  `earnings_window_days`; inherits `live_approved` / `alert_only`.
- **`deploy/run_risk_review.sh`** (new) + **`deploy/crontab.template`** (change) —
  wrapper mirroring `run_fast_loop.sh`: `ANTHROPIC_API_KEY` guard, model pinned to
  `claude-opus-4-8`, two cron entries (12:00, 15:45 ET).
- **`scripts/letter_facts.py`** (optional) — surface `risk_review` verdicts/actions
  in the weekly letter facts (de-risk actions are real PM decisions worth
  narrating; routine `healthy`/`watch` verdicts stay out — consistent with the
  no-plumbing-in-the-letter rule).

## 10. Data sources (grounding — nothing un-pluggable)

| Checklist item | Source (already available) |
|---|---|
| RS vs SPY, rank/score | cached closes (`fetch_prices`), `current.json` |
| 21/50-day MA, price | cached closes / `momentum.py`; `get_equity_technical_indicators` |
| High-water mark, give-back | tracked from price history / monitor quotes since entry |
| Distance-to-stop, PnL | `src/marks.py` + stored thesis geometry |
| Vol expansion | current sigma vs entry sigma stored by `slow_loop` |
| Earnings proximity | `Thesis.review_by`, `get_earnings_calendar`, `event_calendar` |
| News | Alpaca `get_news` (symbol-tagged) |
| VIX, regime | Schwab `$VIX` / index quotes; SPX vs 50-day |

## 11. Rollout & testing

1. **Build deterministic core first**, with `--selftest` proving: one-way
   invariant rejects loosening a stop / extending a target / adding an entry; the
   monitor overlay honors stricter-only and ignores looser.
2. **Ship alert-only** (`alert_only = true` / `live_approved = false`): both passes
   run live, assemble facts, produce verdicts + would-be actions, journal, and
   phone-push — but place nothing and write no overrides. Observe for a period to
   confirm the judgments are sane and non-churny.
3. **Arm** via the git-ignored `config/strategy.local.toml` once trusted, exactly
   like the fast loop / monitor.

## 12. Open items (post-v1)

- Continuous mechanical breakeven-trail in the monitor between passes.
- Portfolio-level market-wide risk-off.
- Optional rendered chart image attached to the phone alert.
- Whether `enable_targets = false` ("let winners run") interacts with the
  lower-TP action (both are risk-management-consistent; confirm no conflict).
