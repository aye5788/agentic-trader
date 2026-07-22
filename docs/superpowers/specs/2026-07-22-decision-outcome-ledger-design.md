# Decision → Outcome Ledger — design spec

**Date:** 2026-07-22
**Author:** Aaron + agent (brainstorm)
**Status:** proposed (awaiting review)
**Supersedes framing of:** the "7 data gaps" review of 2026-07-22 (gaps #1, #3, #5, #6, #7)

---

## 1. Purpose

Make the system's own record of its trading **complete, trustworthy, and
learnable-from**. Today the data exists as disconnected snapshots; the goal is a
single append-only record that ties together *what the system saw*, *what it
decided and why*, *what actually executed*, and *how it turned out* — so it is
simultaneously (a) a real-money audit trail and (b) a training corpus the system
can be improved from.

The binding motivation (Aaron, 2026-07-22): **we need data to collect in order to
improve the system using that data.** A merely-correct audit trail is useless for
improvement if it doesn't also carry the context, the rationale, and the outcome
label. Improvement needs all four, joined.

## 2. The reframe

The original review listed 7 independent gaps. Five of them are not independent —
they are one missing structure:

| Gap | What it really is |
| --- | --- |
| #3 silent hole in trade log | the record is **incomplete** |
| #5 realized P&L ages out | the record is **not the source of truth** (RH's rolling window is) |
| #7 no held-quantity history | the record is **not reconstructable** |
| #6 outcomes all `null` | the record has **no labels** |
| #1 reasoning only in rotating logs | the record has **no features/rationale** |

Fixing these separately produces five half-measures. Fixing them together
produces one **Decision → Outcome Ledger**: an append-only sequence where every
position moves through four linked stages.

**Out of scope for this spec** (its own later piece): #2 intraday tick
time-series, and capture of the full agent transcript prose. Those are lower-value
observability items and do not belong in the learning corpus.

## 3. Current state (grounded)

- `research_store/journal.jsonl` — append-only, atomic (`store.append_journal`,
  `src/research_store/store.py:55`). Already carries event types `product`,
  `execution`, `outcome`, `exit_signal`, `risk_review`. **This is the ledger we
  extend — we do NOT add a new file.**
- `record_outcome(symbol, outcome, now_iso)` already exists
  (`src/research_store/__init__.py:98`) and writes an `outcome` event + attaches
  to the thesis. **It is never called.** Wiring it is most of #6.
- `Thesis` already has an `outcome: dict | None` field
  (`src/research_store/models.py:38`) — the slot exists, always `null`.
- Executions are journaled by `scripts/record_fills.py` (fills + optional
  reentry decisions), invoked by hand from `prompts/fast_loop.md` step 9 and
  `prompts/exit.md` step 6. Fills carry `order_id` — a natural idempotency key.
- Exit path (`prompts/exit.md`) already sells, then re-fetches
  `get_realized_pnl` and `get_equity_positions`. **The outcome data is already in
  the agent's hands at exit** — it just isn't computed into an outcome record.
- RH is reachable ONLY through the agent's MCP session. Deterministic Python in
  `src/` cannot call it. This constraint is load-bearing and unchanged.

## 4. Design

### 4.1 The four stages (one linked record per position)

Joined by a stable key: `decision_id = "<symbol>:<as_of>"` (weekly cadence makes
this unique; stored explicitly so joins never depend on parsing). Each stage is a
journal event carrying `decision_id`.

1. **Context** — features the system saw. Already produced by the slow loop into
   the `product` event / `current.json` thesis (`signals` R/σ/trend/score/rank,
   `regime`, VIX, `entry_zone`, `stop`, `targets`, `target_weight`). We **stamp
   `decision_id` onto each thesis** so context is frozen and joinable even after
   next week's rebuild overwrites `current.json`.
2. **Decision** — buy / sell / hold / skip / reentry + sizing + a **short
   structured rationale**. For opens this is implicit in the `product`; for
   reentry it is the existing `reentry_decisions` structure; we add rationale to
   both (§4.4).
3. **Execution** — the fills, extended with **`intended_price`** so slippage
   (`avg_price - intended_price`) becomes a recorded number rather than lost.
4. **Outcome** — the label: `status` (stopped / target / rebalanced / still_open),
   `pnl_pct`, `pnl_usd`, `holding_days`, `exit_reason`, `hit_stop`, `hit_target`,
   `return_vs_spy`, entry/exit prices. Written via the existing
   `record_outcome()`.

No new storage backend. Four event types on the existing `journal.jsonl`, plus a
`decision_id` field threaded through. `#5` and `#7` become **derived views** over
this log (§4.5), not new files.

### 4.2 Component A — reconcile · verify · alarm  (#3, highest risk)

New script `scripts/reconcile_ledger.py`. Robinhood is ground truth.

**Inputs (agent-fetched, since only the agent reaches RH):** an
`research_store/rh/orders_dump.json` — the executed-order history the agent pulls
with `get_equity_orders` for the Agentic account (order_id, symbol, side,
quantity, average_price, state, executed timestamp). The relevant prompt steps
(`fast_loop.md` §8, `exit.md` §7) already fetch order state; this widens that to
"dump all executed orders since the last reconcile watermark".

**Algorithm (deterministic, pure-ish, testable):**
1. Load journaled executions from `journal.jsonl` → set of recorded `order_id`s.
2. Load `orders_dump.json` → set of RH executed `order_id`s (state == filled).
3. `missing = rh_filled - journaled`.
4. **Auto-heal:** append an `execution` event (marked `source: "reconcile"`) for
   each `missing` order, built from the RH dump. Dedup is free — keyed on
   `order_id`, so re-running never double-counts.
5. **Verify:** re-load journal; assert `journaled ⊇ rh_filled`. If still
   divergent → `src/notify.py push()` an alarm ("ledger divergence: N orders in RH
   not journaled") **and exit non-zero**. Silent no longer possible.
6. Write a watermark (`research_store/rh/reconcile_state.json`: last-seen order
   timestamp) so each run only considers new orders.

`--selftest` covers: clean match (no-op), a missing order (auto-heal appends
once), a re-run (no double-append), and an un-healable divergence (alarms, exits
non-zero). No live path touched — it only reads RH's dump and appends to the log.

### 4.3 Component B — auto-outcome at exit  (#6)

When a position is closed (exit path, or a rebalance sell in the fast loop),
compute the outcome and call the **already-existing** `record_outcome()`.

- Placement: a helper `outcome_from_exit(symbol, exit_price, exit_reason, ...)` in
  a new pure module `src/ledger.py` (decided; see §9), computing `pnl_pct`, `holding_days` (entry `as_of` → today), `hit_stop`,
  `hit_target`, `return_vs_spy` from data the exit already has (thesis `stop`/
  `targets`/`entry_zone`, realized-P&L pull, SPY mark).
- Wire the call into `prompts/exit.md` (after the sell/reconcile, step ~7) and the
  fast-loop rebalance-sell branch (`prompts/fast_loop.md` step 9 region) — one new
  line: "for each fully-closed symbol, call record_outcome with the computed
  outcome". The arithmetic lives in code; the agent only supplies the exit price
  it already fetched.
- Result: theses stop being `outcome: null` for the exit path.

> **IMPLEMENTED SCOPE (2026-07-22 merge):** only `prompts/exit.md` (stop/take-profit
> closes) records outcomes. The fast-loop rotation full-close branch was NOT wired,
> because `record_outcome` requires the symbol to still be in the current book and a
> rotated-out name is not — naive wiring would raise on the live path. Rotation-close
> outcome recording (attaching to the ARCHIVED thesis by `decision_id`) is the tracked
> immediate follow-up. Until it lands, `realized_history()` covers stop/target closes
> only, not the dominant rotation path. See the follow-up note in §9.

### 4.4 Component C — structured rationale  (#1)

- **Reentry** already records `{symbol, decision, current_price, exit_price,
  reason}` — keep, add `decision_id`.
- **Opens/exits:** add a compact rationale to the execution event: a short
  free-text `note` (≤25 words, same budget as reentry) plus, where the driver is
  mechanical, an enum-ish tag (`momentum_rank`, `stop_breach`, `target_hit`,
  `rebalance`, `regime_off`). Written by the same `record_fills.py` /
  `record_outcome` path — no new agent workflow, no transcript dump.

### 4.45 Component E — off-box durability (backup)  [NEW — required, not optional]

**Why this is in-scope, not polish:** the `.gitignore` justification for excluding
`research_store/` is *"regenerated on the box each run."* That is TRUE for prices
(re-fetchable) and `current.json` (weekly rebuild) — and FALSE for the ledger. A
real-money trade record and its outcome labels **cannot be regenerated from code.**
Today they exist in exactly one copy, on one disk, with no backup anywhere
(verified: no rsync/S3/snapshot in `deploy/`, 0 files tracked in git). Losing that
disk loses the entire corpus — which defeats the purpose of collecting it.

**What to back up** — only the *non-regenerable* files:
`journal.jsonl` (the ledger), `history/equity.jsonl`, `flows.jsonl`, and the
`archive/` dir (dated beliefs = the context features). NOT prices, NOT `rh/*.json`
snapshots (both re-derivable).

**Mechanism (recommended):** a **private git mirror**. The box already has git and
push credentials and pushes code daily — reuse that. A new
`deploy/backup_ledger.sh` commits+pushes the files above to a **separate private
repo**, provisioned by Aaron: **`github.com/aye5788/agentic-trader-ledger`**
(private, empty). Build-time task: confirm the box has push access to it (the
existing push path is for the code repo; this repo may need its remote/credentials
wired — verify in step 5, don't assume). Append-only JSONL → tiny diffs → cheap
to commit after every run. This gives versioned, off-box, point-in-time history
for free (you can see the ledger as it was on any past day).

**Wiring:** called at the end of `run_fast_loop.sh`, `run_slow_loop.sh`, and the
exit path, plus a nightly crontab entry as a catch-all. **Non-fatal:** a failed
backup fires an `ntfy` alert but never blocks or fails a trading run.

**Alternative considered:** object storage (S3 / DO Spaces) — rejected for the
first cut (new account, new credentials, new tooling) in favor of reusing the git
path already on the box. Can revisit if the corpus outgrows git.

### 4.5 Component D — derived views  (#5, #7)

Two pure read-only functions over `journal.jsonl` (no new storage):

- `realized_history()` → full-life realized-P&L series reconstructed from
  `outcome`/`execution` events. Replaces reliance on `realized.json`'s rolling
  month window (which stays as a live convenience snapshot, no longer the source
  of truth).
- `position_history()` → held-quantity-over-time reconstructed from `execution`
  buys/sells. Gives #7 without a new snapshot cadence.

These are query functions the dashboard / letter_facts can adopt incrementally;
building them proves the ledger is genuinely complete (if a view can't be
reconstructed, the ledger has a hole — a good test).

## 5. Constraints honored

- **No new live-trading surface.** Reconcile only *reads* RH's dump and *appends*
  to the log. Outcome computation is arithmetic. Placement stays the agent's
  gated MCP step. Zero change to the order path.
- **RH agent-only** respected — RH data enters via agent-written dump files, same
  pattern as today's `positions.json` / `fills.json`.
- **Atomic append** via existing `store.append_journal`.
- **Idempotent** — `order_id` and `decision_id` make every write safe to repeat.

## 6. Testing

- `scripts/reconcile_ledger.py --selftest` — the four cases in §4.2.
- Pure outcome math unit-tested via `src/ledger.py` selftest (known entry/exit →
  expected pnl_pct/holding_days/hit_stop).
- Derived-view functions tested against a hand-built fixture journal.
- Backward compatibility: existing journal readers (`recent_journal`,
  `letter_facts.py`) must tolerate the new fields (additive only — verified by
  running them against a ledger carrying the new events).

## 7. Decisions already made (no open questions)

- Extend `journal.jsonl`, **not** a new file — it is already the append-only
  system of record.
- #3 fix = **auto-heal THEN verify+alarm** (strongest), because the corpus's value
  is its trustworthiness.
- Outcomes computed **automatically at exit**, no new agent judgment.
- Rationale is **structured + short**, not a transcript dump.
- #5/#7 are **derived**, not separately stored.
- `decision_id = symbol:as_of`.
- The ledger is **backed up off-box** to a private git mirror (Component E) —
  required, because the ledger is the one thing in `research_store/` that cannot
  be regenerated.

## 8. Risks / watch-items

- **Orders-dump completeness** — if the agent's `get_equity_orders` dump is
  itself partial, reconcile can't see the gap. Mitigation: dump is bounded by a
  timestamp watermark and paged to completion; verify step compares counts.
- **decision_id collisions** if a symbol is opened twice in one `as_of` — not
  possible under weekly cadence, but the reconcile keys on `order_id` (always
  unique) so execution integrity does not depend on `decision_id`.
- **Outcome for partial exits** (scale-outs) — first cut records outcome on FULL
  close only; partial exits are journaled as executions and rolled into the final
  outcome. Noted as a known simplification.

## 9. Build order (each its own plan step)

1. `src/ledger.py` — pure outcome math + derived views + selftest (no I/O to RH).
2. `scripts/reconcile_ledger.py` — reconcile/verify/alarm + selftest.
3. Thread `decision_id` through `product` write and `record_fills` /
   `record_outcome`.
4. Wire auto-outcome + rationale into `prompts/exit.md` and `prompts/fast_loop.md`.
5. `deploy/backup_ledger.sh` — private-git-mirror backup + selftest, wired into
   the deploy scripts and crontab (Component E).
6. Adopt derived views in dashboard / letter_facts (incremental, optional last).
