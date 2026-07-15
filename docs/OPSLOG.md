# OPSLOG — operations & maintainer notes

Dated log of operational events and technical flags, newest first. This is the
"separate entry elsewhere": the weekly investor letter (The Claude Ledger)
narrates the portfolio only — plumbing, broker blocks, code flags, and
settlement mechanics land HERE (written by the newsletter run from the week's
journal `notes`, or by hand). One `##` heading per entry.

---

## 2026-07-15 — Intraday risk-management overlay shipped + armed

The defensive, de-risk-only intraday risk overlay went live (branch
`feat/intraday-risk-review`, 14 commits, merged to `main` at `abc8498`). It adds
two headless-Claude reviews (cron 12:00 + 15:45 ET, Mon–Fri) that tend open
positions between weekly rebuilds: tighten stops / lower take-profits (written as
stricter-only overrides the always-on monitor enforces within 15s), and
trim/exit via the RH MCP — never loosen a stop, raise a target, or open a
position (enforced in `scripts/risk_review.py`'s one-way invariant, re-checked in
the monitor's `apply_overrides`). Core files: `scripts/risk_review.py` (new
deterministic core, `--selftest`), `prompts/risk_review.md` (agentic procedure),
monitor override overlay, `[risk_review]` config, weekly override-clear in
`slow_loop.py` (Sunday only — nightly recompute must not wipe intra-week
tightenings), and `risk_actions_this_week` in the letter facts (armed actions
only; trim/exit narrated from confirmed fills, never intents).

Built via a 10-task subagent-driven pass with per-task + whole-branch reviews;
the final review caught two integration issues since fixed (nightly-vs-weekly
override clear; a malformed agent decision aborting the whole `--apply` batch).

**Armed same day** (principal, 2026-07-15) via `strategy.local.toml`
`[risk_review] alert_only = false` (with `live_approved = true` already set) —
`armed = True`. No alert-only observation period was run first; the design's
observe-then-arm rollout was skipped at the principal's explicit direction.
Revert to alert-only = flip that flag back to `true`. First armed run: 12:00 ET
2026-07-15.

## 2026-07-10 — SPY→XLE rotation: buy leg deferred on unsettled cash

The ETF sleeve rotated SPY out for XLE (slow loop, as_of 2026-07-08). The fast
loop sold SPY (filled $4.55 @ $752.92, +$0.05 realized) but the paired XLE buy
was rejected by Robinhood with `EQUITY_NOT_ENOUGH_BP_DOLLAR_BASED`: this is a
cash account and dollar-based buys need *settled* funds, so a buy that follows
a sell defers ~1 trading day (T+1) and re-plans next run. Decision (principal,
2026-07-10): keep the 100%-invested target and accept the lag — a standing
~7.5% cash buffer would cost more in drag than the occasional one-day gap.
Skipped orders are now journaled first-class (`status:"skipped"` + `reason`,
e.g. `pending_settlement`) and every execution pushes a phone summary via
ntfy (`src/notify.py`, commit caeff3d) — before that, rebalance trades were
silent unless Robinhood's own app notified.

## 2026-07-08 — First deployment: investor-profile block halted 13 of 14 orders

During the initial book deployment, Robinhood blocked the run's second trade
with "investor profile incomplete" (account 948184924), halting 13 of the 14
approved orders after EEM. The block was operational, not a market call; it
cleared the same day and a second run placed all 14 orders. Watch item for
future account-level checks appearing mid-run.

## 2026-07-09 — fast_loop.py left a stale order_plan.json on empty plans

Flagged by the 2026-07-09 fast-loop run: when the computed plan is empty,
`scripts/fast_loop.py` did not rewrite `research_store/rh/order_plan.json`, so
yesterday's already-executed plan (with `live_approved: true`) sat on disk. A
run that blindly trusted the file could re-place old orders (~$54 double-buy
with zero cash). The 2026-07-09 run detected the staleness and placed nothing.
FIXED same day in commit c5a3e28 — the plan file is now rewritten every run.
