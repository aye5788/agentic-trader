# Universe Maintenance — Design Spec

> ⛔ **SUPERSEDED IN PART, 2026-08-27.** The `HOLD_FOR_REVIEW` outcome and the
> human-gated apply described below NO LONGER EXIST. Three of the five HOLD
> conditions regenerated themselves every week, so the screen could not apply
> and `config/universe.csv` was frozen 2026-07-08 → 2026-08-27 while every
> check read green. The outcomes are now AUTO_APPLY and NO_CHANGE, the latter
> reachable only from a data-integrity failure that self-clears. See
> `docs/OPSLOG.md` 2026-08-27 and `universe_maint.classify`.


**Date:** 2026-07-19
**Status:** Approved design, pre-implementation
**Author:** Aaron + Claude (brainstorming session)

---

## 1. Problem

The single-name candidate universe (`config/universe.csv`, 150 names = 52 human
"seed" + 98 dollar-volume "fill") was built **once** (all rows `as_of=2026-07-08`)
and has **no refresh mechanism and no prompt**. The "human-reviewed whitelist"
described in `docs/DESIGN.md` was real in intent but never operationalized, so the
list silently froze. Over months it will drift: names go illiquid or delist, new
liquid leaders never enter, and stale human seeds accumulate.

This is **Piece 1 of 3** in a "more robust selection engine" thread:
- **Piece 1 (this spec):** universe maintenance — keep the candidate pool fresh.
- **Piece 2 (future):** concentration-aware book construction (cap correlated
  clusters in `momentum.select`). Owns the sector-taxonomy decision (moomoo
  industries vs. our sector buckets) — see §3 "Sector tags".
- **Piece 3 (future):** short-term momentum overlay (moomoo technical indicators
  as early-deterioration context for the risk review).

**Out of scope for Piece 1:** anything touching the live selection/trading path,
Pieces 2 & 3, and the broader moomoo live-data migration (failover quotes, Finnhub
retirement). The moomoo adapter built here is minimal, **data-only, and offline**.

## 2. What the universe is (context that constrains the design)

The universe is stage ① of a three-stage pipeline: **universe (candidate pool) →
momentum signal (ranker) → book (top 10 held)**. The universe's only job is
*"liquid + tradable + broad enough"* — it never decides what to buy (the signal
does). It is also a **Layer-5 governance whitelist**: `governance.py` forbids
orders in any name not on the list. Maintenance must preserve that whitelist
property (every member is a human-or-rules-justified, liquid name) while keeping
the contents fresh.

## 3. Approved design decisions

| Decision | Choice |
|---|---|
| **Seed policy** | **Hybrid** — seeds sticky-but-reviewable: never auto-dropped by the liquidity pass; surfaced for human decision when stale. Fills rotate systematically by dollar-volume. |
| **Refresh rhythm** | **Quarterly** membership pass + **wide band** (hysteresis); plus a **continuous weekly stale-seed watch** riding the slow loop. |
| **Candidate pond** | **incumbents ∪ a broad reference set** (a market-cap screen if `MARKET_VAL` filters, else the existing 816-name `pit_pool`), ranked by **dollar-volume from `get_market_snapshot().turnover`** (free, no quota). |
| **Automation posture** | **Auto-apply the routine case; escalate to human ONLY for seed-drops and anomalies.** The pipeline must not depend on the human for the common path (that dependence is what caused the original freeze). |
| **Sector tags** | **Deferred to Piece 2.** moomoo industries (`PlateSetType.INDUSTRY`) are a different, more granular taxonomy than our FactSet-style sectors; the mapping decision belongs to Piece 2 (the only consumer). Piece 1 carries existing sectors forward, leaves new names untagged. |

## 4. Membership yardstick

- **Dollar-volume** = each name's `turnover` field from `get_market_snapshot`
  (tested live: AAPL = $21.1B; snapshots are 400/call, **free, no subscription
  quota**). This is a single-session figure — acceptable for v1 given the wide
  band + human review. Trailing-average (to damp one-day spikes) is a noted later
  refinement, not v1.
- **Membership band (target size 150):** an **incumbent is kept** while its
  trailing-$vol rank ≤ **180**; open slots are filled from the best
  **non-incumbents ranked ≤ 150**. ~30 ranks of hysteresis mirrors
  `momentum.select`'s band and prevents boundary thrash.
- **Hard liquidity floor for adds:** a new name must clear an absolute minimum
  trailing avg dollar-volume (default **$50M/day**) — junk cannot enter even if
  ranking is odd.

## 5. Components (each independently testable)

### 5.1 `src/adapters/moomoo/` — data-only client (NEW)
Thin adapter over the already-running OpenD gateway (`127.0.0.1:11111`, moomoo
SDK 10.09.6908 on `/usr/bin/python3`). Exposes only what Piece 1 needs:
`get_market_snapshot(codes)` → per-name `turnover` (dollar-volume) in ≤400-name
batches, and optionally `get_stock_filter(Market.US, [SimpleFilter on MARKET_VAL])`
to source the candidate pond. **No trading surface. No live-quote subscription.**
Mirrors the `adapters/schwab` interface style. First moomoo integration, scoped to
offline batch use only.

### 5.2 `scripts/universe_refresh.py` — the quarterly job (NEW)
Cron entrypoint. Orchestration + **pure, testable decision functions**:
- `rank_pond(incumbents, screened, dvol) -> ranked list`
- `propose_membership(ranked, current, seeds, seed_flags, params) -> Proposal`
  (applies the band + seed protection; returns adds / fill-drops / flagged-seeds)
- `classify(proposal, params) -> AUTO_APPLY | HOLD_FOR_REVIEW` with reasons
  (anomaly + sanity rules, §6)
On `AUTO_APPLY`: writes `universe.csv`, commits, sends FYI push.
On `HOLD_FOR_REVIEW`: writes the proposal artifact, sends "needs review" push,
does **not** modify `universe.csv`.

### 5.3 `scripts/universe_apply.py` — human-gated apply (NEW)
For held proposals. Takes a proposal id, re-validates it hasn't gone stale,
rewrites `universe.csv` (swap names, refresh `as_of`, set `source` = seed /
fill_dvol / screen, preserve `flag`, carry `sector` forward — new names untagged
until Piece 2), and commits to git.
(May be implemented as `universe_refresh.py --apply <proposal>`.)

### 5.4 Weekly stale-seed watch (hook in `scripts/slow_loop.py`)
The slow loop already ranks every name weekly. After ranking, append each seed's
current momentum rank to `research_store/universe/seed_watch.json`. A seed is
**flagged stale** when it sits in the bottom third (rank > 100 of 150) for ≥ **8
consecutive** weekly runs, **or** falls below the hard liquidity floor. Flagged
seeds appear in the next quarterly proposal's "your decision" list. The watch
**never acts** — it only accrues flags.

### 5.5 Scheduling
- **Quarterly** membership pass: cron, first Sunday of Jan/Apr/Jul/Oct (aligned
  with existing cron style; ERR-trap → phone push already covers job failure).
- **Weekly** stale-seed accrual: rides the existing slow loop, no new schedule.

## 6. Safety: what auto-applies vs. what escalates

**Auto-apply (routine)** when ALL hold:
- Total changes (adds + fill-drops) ≤ **5**.
- Every add passes sanity: normal common stock, on NASDAQ/NYSE/AMEX, not a
  leveraged/inverse product, trailing $vol ≥ the hard floor.
- Screener returned a sane count (≥ 150 names) — guards against broken/empty data.
- **No seed is proposed for drop.**

**Hold for human review** if ANY of: > 5 changes (possible data glitch); an add
fails sanity; screener data looks broken; or a seed is up for drop.

**Why auto-apply is safe:**
1. **Git is the undo** — every change committed → full audit trail + one-command
   revert. Auto-apply is not irreversible.
2. Adds are pre-screened to a hard liquidity floor + instrument sanity.
3. The universe only makes a name *eligible*; the momentum signal + all governance
   guardrails still gate any actual buy. Auto-adding to the pool is low-consequence.
4. Job failure → existing cron ERR-trap phone alert; broken data → anomaly hold,
   never applies garbage.

## 7. Human interaction (view + approve)

The pipeline must not depend on a human for the routine path (that dependence
caused the original freeze). It reuses existing channels only — **no new web
write-surface**; the dashboard stays read-only and all mutation flows through the
agent/CLI apply path.

**Routine (auto-applied) — nothing required.** An FYI is sent (ntfy push, optional
email): "Universe refreshed: −TSLA, +ABCD (committed `<sha>`)". No action.

**Held proposals (seed-drop or anomaly) — you review, then approve:**
- **View channels (existing infra):**
  - **Email** — the held proposal rendered as a readable diff, sent via the
    existing Resend pipe (the newsletter's path): each drop / add / flagged-seed
    with its reason.
  - **Dashboard** — a "Pending universe proposal" panel on `dash.ethobs.uk`
    renders the same artifact from `research_store/universe/proposals/`.
  - **Phone push** — ntfy notification that a proposal is waiting (+ change count).
- **Approval channel — a Claude Code session.** You instruct the agent: "show me
  the pending universe proposal", then "approve it", or approve-with-edits
  ("approve but keep NVDA and skip the TSLA drop"). The agent runs
  `universe_apply.py` against the (possibly edited) proposal, writes `universe.csv`,
  commits. **There is no human recipient — you instruct the system.**
- **Why session-based, not one-tap:** held cases are the judgment cases (your
  convictions / an anomaly) and benefit from approve-with-edits, which a single
  button can't express. A one-tap phone approve is a possible future add-on but
  needs a new authenticated write endpoint and can't handle edits — out of scope.

## 8. Parameters (config: new `[universe_maintenance]` in `strategy.toml`)

| Param | Default | Meaning |
|---|---|---|
| `cadence` | quarterly | membership-pass schedule |
| `target_size` | 150 | universe size |
| `keep_rank_max` | 180 | incumbent kept while $vol rank ≤ this |
| `add_rank_max` | 150 | non-incumbent eligible to add while rank ≤ this |
| `dvol_window_days` | 20 | trailing avg window for dollar-volume |
| `add_dvol_floor_usd` | 50_000_000 | hard min trailing avg $vol for an add |
| `screen_top_n` | 200 | moomoo screener depth |
| `auto_apply_max_changes` | 5 | > this ⇒ hold for review |
| `stale_seed_rank_floor` | 100 | seed in bottom third (of 150) counts toward stale |
| `stale_seed_weeks` | 8 | consecutive weeks flagged ⇒ surface for decision |

## 9. Data flow

```
quarterly cron
  └─ universe_refresh.py
       ├─ read config/universe.csv (current: seeds + fills + flags)
       ├─ read research_store/universe/seed_watch.json (stale-seed flags)
       ├─ pond = incumbents ∪ broad reference (MARKET_VAL screen or pit_pool)
       ├─ moomoo get_market_snapshot(pond) → per-name turnover ($vol); rank
       ├─ propose_membership() → adds / fill-drops / flagged-seeds  (banded, seeds protected)
       ├─ classify() → AUTO_APPLY or HOLD_FOR_REVIEW
       ├─ AUTO_APPLY → write universe.csv + git commit + FYI push (optional email)
       └─ HOLD       → write proposal artifact + email + dashboard panel + "needs review" push
                          └─ (human, via Claude session) universe_apply.py <proposal> → write + commit

weekly slow loop
  └─ append seed momentum ranks → seed_watch.json → flag stale seeds
```

Artifacts: `research_store/universe/proposals/YYYY-MM-DD.{json,md}`,
`research_store/universe/seed_watch.json`.

## 10. Testing

- `universe_refresh.py --selftest`: pure-function coverage of `rank_pond`,
  `propose_membership` (band retain/drop, seed protection, size discipline), and
  `classify` (each auto-apply / hold branch: large diff, failed sanity, seed-drop,
  broken screener). No live calls.
- `--dry-run`: run the real pass but print the proposal and write nothing.
- Runs under `deploy/run_selftests.sh` (venv-pinned).

## 11. Risks / open questions (resolve at implementation)

- **RESOLVED (probe 2026-07-19):** dollar-volume = `get_market_snapshot().turnover`
  (free, no quota) — the `get_stock_filter` turnover field is unsupported, so
  snapshots are the source. Sector-tagging deferred to Piece 2 (moomoo INDUSTRY
  plates ≠ our sector taxonomy).
- **Candidate pond source:** confirm `MARKET_VAL` is a supported `get_stock_filter`
  field (turnover was not); if not, fall back to the existing 816-name `pit_pool`
  as the pond. Either way the ranking is by snapshot turnover.
- Shared OpenD with `moomoo-vol-desk`: the refresh is a brief quarterly/weekly
  batch → negligible load, but confirm no subscription-slot contention.
- First run will likely be small (universe only 11 days stale) but may trip the
  ">5 changes" hold — which is fine and desirable (you see the first one).
