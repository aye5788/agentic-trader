# Short-Term Momentum Early-Warning for the Risk Review (Piece 3)

**Date:** 2026-07-20
**Status:** Approved design (brainstormed with Aaron), pre-implementation
**Author:** Aaron + Claude

---

## 1. Problem / role

The intraday **risk review** (`scripts/risk_review.py` + `prompts/risk_review.md`, twice
daily) decides whether to tighten/trim/exit a held name. Today its only "getting worse"
signal is **`near_stop`** — price within 3% of the stored stop. That is *late by
construction*: by the time a name is 3% from its stop, most of the damage is done.

**Piece 3's role:** give the review an **earlier** read that a held name is *losing
short-term momentum* — while there's still room to act — using a short-term momentum
indicator built for this slot.

This is the third piece of the selection-engine thread. Piece 1 (universe maintenance)
shipped + armed. Piece 2 (concentration cap) was tested and shelved.

## 2. Design decisions (settled in brainstorming)

- **Two *established* indicators in conjunction — not one custom composite.** A bespoke
  score has no guarantee it measures what we intend, and — decisive here — the risk
  review is a **stateless `claude -p`**; a custom indicator would need a manual re-fed
  every run (context bloat + fragility). MACD / RSI / relative-strength the model reads
  cold: "RSI(14)=29 and falling, MACD histogram negative" needs zero explanation.
- **Cover two dimensions, confirm each other** (no TI used in isolation):
  - **Absolute leg** — the name's *own* momentum decelerating.
  - **Relative leg** — the name losing its *edge vs the market* (the dimension the
    strategy actually trades).
- **Advisory, not a hard trigger.** The indicator values + a light "weakening" tag are
  added to the per-position **facts** the review already weighs. The LLM judges; the
  existing one-way (risk-reducing-only) invariant and geometry validation are unchanged.
  No new automatic order path.
- **Feed values, not a verdict** ("MACD hist −0.4 and falling; RS-vs-SPY −5% / 10d"),
  per the stateless-agent constraint.

## 3. The indicators

Absolute leg — build **two candidates**, let the validation (§6) pick the earliest:
- **MACD (12,26,9)** — histogram rolling over / MACD crossing below signal. A
  *deceleration* read (turns before price does).
- **RSI(14), 50-line** — crossing below 50 = momentum regime flip. (External research:
  the 50-cross is more reliable than the classic 70/30, which often rides trend
  *continuation*.)

Relative leg:
- **Relative-strength line = close / SPY_close**, and its short trend (RS line vs its own
  ~20-day MA, or RS slope). "Soft" = RS line below its short MA / RS slope negative.
  Benchmark = **SPY** (what the regime gate uses; per-sector benchmarking deferred — no
  name→sector map exists yet).

Combined read: **`weakening`** = absolute leg soft **AND** relative leg soft;
**`watch`** = exactly one soft; else quiet. Both raw values and the tag are surfaced.

## 4. Architecture (pure core + thin wiring)

- **New pure module `src/ti_signals.py`** — no I/O; takes a price panel + asof, returns
  per-name `{macd, macd_signal, macd_hist, rsi, rs_vs_spy, rs_trend, abs_soft, rel_soft,
  tag}`. Deterministic, `_selftest()` with crafted series (a known MACD cross, an RSI
  below 50, an RS line rolling over). Added to `deploy/run_selftests.sh`. Same
  pure-function discipline as `momentum.py` / `concentration.py`.
- **Data:** the same daily panel the loops already use —
  `research_store/prices/closes.parquet` (150 names + ETFs + SPY, multi-year). MACD needs
  ~35 days, RS ~20; the cache has years. **No moomoo, no network, no new source.**
- **Wiring (Phase 2, gated on §6):** `risk_review.py` calls `ti_signals` for each tended
  position and adds the fields to the per-position facts JSON. `prompts/risk_review.md`
  gets a short note: the new fields, and "both soft = early weakening — consider
  tightening/trimming *before* `near_stop`." Kept light (established indicators need
  little explanation).

## 5. Phasing (validate first, wire second — the Piece-2 discipline)

- **Phase 1 (offline, no live path):** build `src/ti_signals.py` + the replay validation
  (§6). Produce the go/no-go + which absolute leg wins. **No change to `risk_review.py`.**
- **Phase 2 (gated on Phase 1 passing):** wire the winning pair into the risk-review
  facts + prompt. A separate, small change written only if Phase 1 earns it.

## 6. Validation — the "would it have caught July-17?" replay (the gate)

Not a survivorship backtest (the existing backtests are *weekly*; this overlay is
*intraday*, and it feeds a judgment, not a mechanical rule). The right-sized check is a
**historical replay on the events that matter**:

- Assemble a set of **real stop-out events** — the 2026-07-17 storage/semi cluster (ALAB,
  AMD, LRCX, SNDK, WDC, STX, INTC, XLK …) plus other stop-outs from the monitor cooldown
  / journal history.
- For each event, compute the candidate indicator pairings (MACD+RS, RSI50+RS) on the
  daily closes **leading up to** the stop date.
- Report, per event: did the `weakening` tag fire, and **how many trading days before**
  the stop? Plus a false-alarm read — how often it fires on names that did *not* stop out.
- **PASS** = the pairing leads the stop by ≥1–2 days on a majority of the events that
  matter, without firing indiscriminately → wire that pairing (Phase 2). **FAIL** = it
  fires *at* the stop, or everywhere → drop it (a valid outcome; stops already work).

Deliverable: a small table (event × pairing × days-of-lead × false-fire rate) + a
recommendation. This is the decision input, Aaron's call on the numbers.

## 7. Files

- **Create:** `src/ti_signals.py` (pure: MACD, RSI, RS-vs-SPY, weakening tag, `_selftest`).
- **Create:** `scripts/ti_replay.py` (the §6 replay over historical stop-outs → table).
- **Modify (Phase 2 only):** `scripts/risk_review.py` (add TI fields to per-position
  facts), `prompts/risk_review.md` (brief note on the fields), `deploy/run_selftests.sh`
  (add `src/ti_signals.py`).

## 8. Out of scope / non-goals

- No custom/bespoke composite indicator (rejected: construct-validity + context bloat).
- No moomoo, no flow data (the flow-decomposition avenue was tested 2026-07-20 and does
  not transfer to equities — see OPSLOG).
- No new automatic order path; the review's existing one-way risk-reducing gate is
  untouched.
- Per-sector relative benchmark (needs a name→sector map that doesn't exist yet) — future.
- Not a hard mechanical trigger in v1; advisory to the LLM review.

## 9. Risks / open questions

- **Intraday vs daily indicators:** MACD/RSI/RS are computed on *daily* closes; the review
  runs intraday. v1 uses the latest completed daily bar (+ the live mark for the RS/level
  read). Finer intraday bars are a later refinement if the replay says daily is too slow.
- **False alarms:** two-in-conjunction is the guard, but the replay must report the
  false-fire rate, not just the hits — a signal that fires on everything is useless.
- **Advisory ≠ free:** even as a fact, it changes the review's behavior; Phase 2 stays
  small and the one-way invariant means it can only ever *reduce* risk.
