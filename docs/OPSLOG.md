# OPSLOG — operations & maintainer notes

Dated log of operational events and technical flags, newest first. This is the
"separate entry elsewhere": the weekly investor letter (The Claude Ledger)
narrates the portfolio only — plumbing, broker blocks, code flags, and
settlement mechanics land HERE (written by the newsletter run from the week's
journal `notes`, or by hand). One `##` heading per entry.

---

## 2026-07-17 — Intraday monitor: phantom-holding stop-loop (AMAT) fixed

**Symptom (Aaron):** repeated failed sell orders / "something stuck." The
`agentic-monitor` service was firing an `exit_signal` for **AMAT** (`reason:
stop`) every ~60s from ~13:41 UTC and re-running the headless exit executor each
time — journal + phone spam, a `claude -p` subprocess per tick. No money moved.

**Root cause — a phantom holding.** AMAT was in the slow-loop book
(`current.json` thesis, stop 528.63) but was **never actually bought** (RH order
history for AMAT is empty; the account was 100% invested — journal shows repeated
`insufficient_buying_power` skips). The monitor built its stop-watch list from
**book theses, not real holdings** (`market_monitor.py:173`). AMAT's price (518)
sat under its stop, so every tick it fired → executor found nothing to sell →
wrote an empty `sold` → so the monitor never marked AMAT `fired`
(`:253–255`, the legit retry-on-transient-failure path) → re-fired next tick.
Infinite loop. The account's 8 genuinely-sold names of the day (STX, XLK, LRCX,
AMD, SNDK, INTC, BE, ALAB) were suppressed only by the `fired` flag.

**Fix.** New `owned_symbols()` reads the reconcile snapshot
(`research_store/rh/positions.json`) and gates the watch list to names actually
held (`qty>0`). Fails **open** (watches all theses) if that snapshot is
absent/torn — never silently drops stop protection. Genuinely-held names whose
sell fails transiently stay in the snapshot, so the retry path is preserved;
phantoms can't re-fire. `--selftest` covers it.

**Verified live.** After restart (13:57 UTC) the watch list = the 5 real
holdings (DELL, IWM, MU, SPY, XLE); AMAT dropped. Two clean polls (13:57:33,
13:58:35) produced zero AMAT signals. Realized P&L for 07-17 stands at −$6.41
(8 stop exits in a broad semis selloff) — unrelated to the loop, which placed
no orders.

---

## 2026-07-16 — Slow loop outage: expired Schwab token + dangling tokens.db lock

The Mon–Fri 18:00 slow loop (nightly risk-exit recompute) died on 2026-07-15 —
`fetch_prices` returned **0/168** and the phone alert fired. Two stacked causes:

1. **Schwab refresh token expired.** The weekly re-auth (last done 2026-07-08)
   lapsed at 17:20 UTC on 07-15; the 7-day refresh token was dead, so no token
   could be minted. (A `schwab_auth.py` run that "looked done" earlier never
   wrote here — see the interactive-stdin note below.)
2. **`secrets/tokens.db` held a dangling exclusive lock.** The `market_monitor`
   systemd service's long-lived `schwabdev` connection issues `BEGIN EXCLUSIVE`
   around each token refresh (schwabdev holds it across the HTTP call to
   serialize refresh across instances). When the refresh failed it left the
   transaction open — journald showed `cannot start a transaction within a
   transaction` looping every 15s from ~11:57 ET. That RESERVED/EXCLUSIVE lock
   (rollback-journal mode) starved the 18:00 cron of even a read lock →
   `database is locked` on every ticker.

**Collateral:** `fetch_prices` wrote the empty panel over `closes.parquet` before
crashing, wiping the 10y cache; the book (`current.json`) went stale a day.

**Fixes (this commit + one runtime change):**
- **WAL mode on `secrets/tokens.db`** (`PRAGMA journal_mode=WAL`, one-time,
  persists in the file; `tokens.db-wal`/`-shm` sidecars now present). Readers are
  never blocked by the monitor's writer — proven live: a concurrent Schwab fetch
  succeeded while the monitor ran. This is the durable fix for the lock class.
  Not in git (secrets/ is ignored) — hence this note. Revert with
  `PRAGMA journal_mode=DELETE` if ever needed.
- **`fetch_prices.py` abort-guard:** if `<50%` of tickers fetch, it aborts
  (exit 2 → cron alert) and **leaves the existing cache intact** instead of
  overwriting it with a systemic-failure result. Per-ticker failures now print
  the real error (expired token / locked db) instead of a bare `FAILED`.
- **`slow_loop.py`:** refuses to rebuild the book off an empty panel (clear
  message, not a cryptic `IndexError`).
- **`scripts/schwab_finish_auth.py` (new):** completes the OAuth exchange with the
  pasted redirect URL passed as an *argument*, for agent shells where `input()`
  hits EOF (Claude Code `!`). The normal weekly re-auth still wants a real TTY:
  `.venv/bin/python scripts/schwab_auth.py`.

Re-auth done 2026-07-16 (good through ~07-23); prices rebuilt 168/168; book
rebuilt; monitor restarted clean under WAL; the 07-16 12:00 armed risk review
ran on the fresh book and held all 13 (0 orders, broker-confirmed).

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
