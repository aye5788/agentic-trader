# Adaptive-Input Layer — design spec

**Date:** 2026-07-23
**Author:** Aaron + agent (brainstorm)
**Status:** proposed (awaiting review)
**Builds on:** the Decision→Outcome Ledger ([`2026-07-22-decision-outcome-ledger-design.md`](2026-07-22-decision-outcome-ledger-design.md)) — this is the "learnable-from" half the ledger was collected for.

---

## 1. Purpose

Turn the ledger from a *record* into a *feedback loop*. The ledger collects
`context → decision → execution → outcome`; this layer closes the loop by using
those outcomes to **adaptively retune the deterministic strategy's own inputs**.

The binding reframe (Aaron, 2026-07-23): we do **not** put an LLM in the decision
loop and we do **not** try to make the agent's judgment smarter (rejected as too
complex/risky). The deterministic slow loop is already the decision-maker; it
already turns inputs → book. So we make its **inputs adaptive** and inherit the
learning value for free. `config/strategy.toml` graduates from a hand-set static
config into a **learned-but-bounded** one — same file, same consumers
(`slow_loop.py`, `momentum.py`), but a subset of values become functions of
realized outcomes, each carrying provenance.

## 2. Scope

**In scope (this spec):** the adaptive-input *framework* — the Bayesian estimator,
the off-box compute home, the propose-and-bound governance — instantiated on **one
dial: `[trade_management] stop_atr_mult`** (currently `2.5`). The substrate is
built to be **reusable**, not throwaway, so the next dial reuses it.

**Explicitly out of scope (future dials, their own specs):**
- The **signal** (`momentum.py` component weights / lookback) — the highest-value
  target, but the highest overfit surface and slowest to validate live. It reuses
  this substrate later. See §12.
- Gate thresholds (VIX ceiling, regime floor), target geometry (`target_r_mults`,
  `enable_targets`). Candidates for later dials.
- Per-name / universe learning — deliberately rejected: the candidate pool shifts
  (quarterly refresh, PIT membership), so per-name facts are non-stationary and
  not worth learning. We learn **invariant policy parameters only.**

## 3. Non-negotiable boundary — this layer NEVER executes

Everything here is **background research**. It cannot place, modify, or cancel an
order — by construction. The architecture already settles execution: the slow loop
writes a validated book to the Research Store; the `claude` agent is the only thing
that trades, via the gated MCP path. This layer sits **upstream of the slow loop**.
Its only output is an **artifact** (a proposed config value). That artifact flows
into the *existing* pipeline, where the `[risk]` mandate validation, the
`src/governance.py` gates, and human review already gate everything before an order
is placed. We invent **no new safety machinery** — the learning layer is just a new
*producer* of the same config the slow loop already validates.

## 4. Where it lives — off-box, on GitHub Actions

**The droplet does not run this.** The droplet (verified 2026-07-23) is a shared,
memory-constrained 1.9 GB box already ~268 MB into swap, running three trading
projects; its ~500 MB spikes are all headless-`claude` invocations. Adding heavy
compute (replay, model fits) there is unacceptable, and the end-state goal is the
droplet as a lean "final stop before execution."

- **Compute → GitHub Actions** (Aaron's GitHub **Pro** plan, $4/mo). Verified live:
  3,000 Actions min/month, standard Linux runner ≈ 2 vCPU / ~7 GB RAM (**~3.5× the
  whole droplet**), scales to zero. Verified current usage: peak month 25% of quota,
  now ~2% after archiving dead repos. A weekly heavy job (~15 min) is ~2–3% of quota
  — no overage, no new spend.
- **Data in → the ledger mirror.** The runner reads the ledger from the private
  backup repo (`aye5788/agentic-trader-ledger`, Component E) and the price cache —
  it never touches the droplet.
- **Result out → a committed artifact.** The workflow commits a *proposal* file to
  the repo; the droplet consumes it on its next `git pull`. This is the same
  git-artifact delivery Component E already established.
- **Pure Python, never `claude`.** The single most important footprint rule: this
  layer is deterministic Python. The droplet's memory pressure is *all* Claude
  processes; an LLM-based learner would be the worst possible addition. (It also
  can't be, per §3.)

## 5. The spine — a Bayesian posterior per knob

Each adaptive dial carries a **posterior distribution over its best value**,
updated as evidence (replayed + live outcomes) accumulates.

- The knob's bounded band is discretized into a small grid of candidate values
  (e.g. `stop_atr_mult ∈ {1.8, 2.0, … 3.2}`). Each grid point holds a posterior
  over its **objective** (§6.2) — a running Normal (mean, variance) fed by samples.
- **The recommendation rule is uncertainty-aware:** stay on the incumbent value
  unless a challenger's posterior *confidently* dominates it by a margin (Bayesian
  best-arm style). This is what makes **the uncertainty itself the activation
  gate** — the layer simply does not move a dial until the data justifies it. It is
  correct and inert on day 1 (N=0 → flat posteriors → no move), and it "switches
  on" automatically as the posterior tightens. No hard min-N cutoff needed; the
  posterior width *is* the gate.
- This dissolves the batch-vs-online question: updates are incremental (append
  samples, update sufficient statistics), and a periodic full recompute is just a
  consistency audit, not a separate mechanism.

## 6. The first dial — `stop_atr_mult`

The stop is volatility-adjusted: `stop = stop_atr_mult × daily-σ` below entry
(`config/strategy.toml [trade_management]`). We learn the multiple.

### 6.1 Evidence — hybrid (replay-primary, live-correction)

- **Replay-primary (the prior):** re-simulate history under each candidate
  multiple. For every historical entry, a **stop-aware replay** determines whether
  that stop would have been touched and what the position then did — yielding many
  pseudo-outcomes per candidate, across multiple regimes (use the survivorship-clean
  PIT pool, 2021–2026, so we don't overfit one regime). This seeds each grid point's
  posterior richly *today*, without waiting for live trades.
- **Live-correction (the update):** as real stop-outs land in the ledger, append
  them as recency-weighted samples. Live is the honest out-of-sample truth that
  corrects any replay bias; the Bayesian update combines the two naturally
  (replay = prior, live = update).

### 6.2 Objective

Not raw "recovery rate" — that is only a *diagnostic*. The objective the posterior
scores is **expected realized-R per position** (or terminal per-position return)
under the candidate stop policy, because the multiple trades off two failure modes
the objective must both see: too tight → death by a thousand premature stop-outs;
too wide → oversized individual losses. Recovery rate is reported alongside for
interpretability.

### 6.3 Data dependency — OHLC (known, bounded task)

Stop-aware replay needs daily **high/low**, to know whether price *touched* the
stop intraday. `scripts/fetch_prices.py` currently caches **closes only**
(`closes.parquet`). Schwab's price-history candles already carry OHLC — the fetch
discards it. **Task:** extend the fetch + cache to retain daily O/H/L/C. Side
benefit: a stop-aware replay is exactly the intra-week-stop modeling
`scripts/backtest.py` explicitly lacks today — so this also improves backtest
honesty and is reusable.

## 7. Reusable substrate (pays forward to the signal)

Per the 2026-07-23 correction: validating the *pipeline* on an easy dial does
**not** reduce the signal's intrinsic statistical risks (slow live validation, wide
overfit surface). The only thing that transfers is **overfit defense**, and only if
we build it into the shared substrate now rather than bolting it onto the stop dial:

- **Walk-forward out-of-sample splits** in the replay (never score a candidate on
  the window it was fit on).
- **Bounded, priored posteriors** (a knob can never leave its hard band; the prior
  is centered on the current hand-set value).
- **Held-out scoring** and a reported in-vs-out-of-sample gap as an overfit alarm.

These are built as dial-agnostic primitives (`src/adaptive.py`) so the signal dial
later inherits them. The slow-live-validation risk is *not* addressed here — it is
handled later by leaning on replay as primary evidence for the signal.

## 8. Governance — propose, don't auto-apply

- **Default: propose.** The workflow commits a proposal artifact
  (`research_store/adaptive/proposals/stop_atr_mult.json`): `{current, recommended,
  posterior_summary, evidence_n (replay/live), in_out_sample_gap, rationale}`, and
  surfaces it (phone push via `src/notify.py` / a newsletter line). A human promotes
  it into `config/strategy.local.toml` (the existing local-override mechanism).
- **Bounded auto-promote is a later opt-in**, not the first cut: a flag could let a
  recommendation *inside its hard band* apply automatically. Off by default.
- **Provenance is mandatory.** Any adapted value records where it came from:
  `set by adaptive layer on <date> from N=<n> outcomes; was <old>`.
- **Inherits every existing gate.** A promoted value is still validated by the
  `[risk]` mandate on the next research-product write and by `src/governance.py`
  before any order. Nothing about the order path changes.

## 9. Footprint

- **Droplet:** zero new resident process, zero new heavy deps, no new `claude`
  invocation. It gains only a plain file to `git pull` and a config value to read —
  lighter than the backup script.
- **GitHub Actions:** one scheduled workflow, weekly, ~minutes. ~2–3% of the Pro
  quota even run weekly with heavy deps.
- **Cost as history grows:** live-side updates are incremental sufficient
  statistics (O(new outcomes), not O(all history)). Replay is bounded by the fixed
  PIT window and runs off-box regardless.

## 10. Constraints honored

- **No new live-trading surface** (§3) — output is a config proposal; placement
  stays the agent's gated MCP step.
- **RH agent-only** respected — this layer never touches RH; it reads the ledger
  mirror and price cache.
- **Droplet stays lean** — all compute off-box; consumes an artifact.
- **Idempotent / auditable** — proposals are committed files with provenance;
  re-running reproduces the same recommendation from the same evidence.

## 11. Testing

- `src/adaptive.py --selftest` — pure Bayesian estimator: known samples → expected
  posterior; the uncertainty gate holds the incumbent at N=0 and on a
  low-separation challenger; moves only when a challenger confidently dominates.
- Stop-aware replay unit-tested against a hand-built OHLC fixture (stop touched vs
  not; recovery vs not).
- Walk-forward split correctness: a candidate is never scored on its fit window;
  the in/out-of-sample gap is computed and surfaced.
- OHLC fetch: verify O/H/L/C retained and backward-compatible with existing
  closes-only readers.

## 12. Decisions already made (no open questions)

- **Spine = Bayesian posterior-per-knob**; uncertainty is the activation gate.
- **First dial = `stop_atr_mult`**; the signal is the strategic target but comes
  *after* this, reusing the substrate.
- **Evidence = hybrid (c)** — replay-primary prior + live-correction update;
  accepts the OHLC sourcing task.
- **Compute off-box on GitHub Actions (Pro)**; droplet consumes a committed
  artifact; pure Python, never `claude`.
- **Governance = propose-not-apply**; bounded auto-promote is a later opt-in;
  provenance mandatory; inherits existing gates.
- **Learn invariant policy parameters only** — no per-name/universe learning.
- **Substrate built reusable** with overfit defenses (walk-forward OOS, bounded
  priors, held-out scoring) as dial-agnostic primitives.

## 13. Risks / watch-items

- **Replay bias.** Closes-only history under-counts intraday stop touches → replay
  must use the new OHLC; even then daily H/L is an approximation of the true
  intraday path. Mitigation: live-correction, and treat replay as prior not truth.
- **Overfit surface.** Even a 1-D knob can overfit a lucky window. Mitigation:
  walk-forward OOS + the in/out-of-sample gap alarm; bounded band caps the damage.
- **Regime confound.** A multiple tuned on one regime may not generalize.
  Mitigation: replay over the multi-regime PIT pool, not just the recent live book.
- **Objective mis-specification.** Optimizing the wrong objective (e.g. recovery
  rate instead of realized-R) yields a "better" but worse dial. Mitigation: §6.2
  objective is portfolio-relevant; recovery rate stays diagnostic only.
- **Proposal staleness.** If nobody promotes proposals, the loop is inert (safe but
  useless). Watch-item: surface unpromoted proposals in the newsletter.

## 14. Build order (each its own plan step)

1. **OHLC fetch** — extend `fetch_prices.py` + cache to retain daily O/H/L/C;
   backward-compatible with closes-only readers. (§6.3)
2. **Stop-aware replay** — pure function: given OHLC + entry + candidate multiple →
   per-position outcome (stop touched? recovery? realized-R). Reusable by the
   backtest. Selftest on a fixture. (§6.1, §6.2)
3. **`src/adaptive.py`** — dial-agnostic Bayesian estimator: grid posteriors,
   incremental update, uncertainty-gated recommendation, walk-forward OOS, held-out
   gap. Selftest. (§5, §7)
4. **`scripts/tune_stop.py`** — wire dial #1: read ledger mirror + OHLC, run replay
   across the PIT pool + live outcomes, emit the proposal artifact with provenance.
5. **GitHub Actions workflow** — scheduled weekly; runs step 4 off-box; commits the
   proposal; (optional) notifies. Reads the ledger mirror, never the droplet. (§4)
6. **Consumption + governance** — `strategy.py` reads a promoted adaptive override
   from `strategy.local.toml`; document the human promotion step; surface
   unpromoted proposals. (§8)

## 15. Future (not this spec)

- **Dial #2 — the signal.** Same substrate, 1-D first (a single tilt parameter, not
  the full weight vector), replay-primary over the PIT pool, live as slow
  correction. High leverage, high overfit surface — the reason it comes second.
- Additional dials (gates, target geometry) as the substrate proves out.
- Bounded auto-promote as an opt-in once proposals have a track record.
