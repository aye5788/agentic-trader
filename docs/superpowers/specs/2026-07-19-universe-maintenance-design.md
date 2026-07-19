# Universe Maintenance — Design Spec

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
  clusters in `momentum.select`). Piece 1 delivers the sector tags it needs.
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
| **Candidate pond** | moomoo screener: **top ~200 US names by dollar-volume ∪ current 150 incumbents**, ranked by trailing dollar-volume. |
| **Automation posture** | **Auto-apply the routine case; escalate to human ONLY for seed-drops and anomalies.** The pipeline must not depend on the human for the common path (that dependence is what caused the original freeze). |
| **Sector tags** | Refresh each name's sector via moomoo `get_owner_plate` while connected — feeds Piece 2. |

## 4. Membership yardstick

- **Dollar-volume** = trailing ~20-trading-day average of (close × share volume).
  A trailing average, not a single day, so a one-day spike can't move membership.
  Source: moomoo daily bars (`get_cur_kline` / snapshots); the existing
  `research_store/prices/` cache is the fallback. Exact source confirmed at build.
- **Membership band (target size 150):** an **incumbent is kept** while its
  trailing-$vol rank ≤ **180**; open slots are filled from the best
  **non-incumbents ranked ≤ 150**. ~30 ranks of hysteresis mirrors
  `momentum.select`'s band and prevents boundary thrash.
- **Hard liquidity floor for adds:** a new name must clear an absolute minimum
  trailing avg dollar-volume (default **$50M/day**) — junk cannot enter even if
  ranking is odd.

## 5. Components (each independently testable)

### 5.1 `src/adapters/moomoo/` — data-only client (NEW)
Thin adapter over the already-running OpenD gateway (`127.0.0.1:11111`). Exposes
only what Piece 1 needs: a dollar-volume screener (`get_stock_filter`), daily
bars/snapshots for trailing $vol, and sector membership (`get_owner_plate`).
**No trading surface. No live-quote subscription.** Mirrors the
`adapters/schwab` interface style. This is the project's first moomoo integration
but is deliberately scoped to offline batch use.

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
fill_dvol / screen, preserve `flag`, update `sector`), and commits to git.
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

## 7. Parameters (config: new `[universe_maintenance]` in `strategy.toml`)

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

## 8. Data flow

```
quarterly cron
  └─ universe_refresh.py
       ├─ read config/universe.csv (current: seeds + fills + flags)
       ├─ read research_store/universe/seed_watch.json (stale-seed flags)
       ├─ moomoo: screen top-200 by $vol  ∪  incumbents
       ├─ moomoo: trailing 20d avg $vol per name;  get_owner_plate → sector
       ├─ propose_membership() → adds / fill-drops / flagged-seeds  (banded, seeds protected)
       ├─ classify() → AUTO_APPLY or HOLD_FOR_REVIEW
       ├─ AUTO_APPLY → write universe.csv + git commit + FYI push
       └─ HOLD       → write proposal artifact + "needs review" push
                          └─ (human) universe_apply.py <proposal> → write + commit

weekly slow loop
  └─ append seed momentum ranks → seed_watch.json → flag stale seeds
```

Artifacts: `research_store/universe/proposals/YYYY-MM-DD.{json,md}`,
`research_store/universe/seed_watch.json`.

## 9. Testing

- `universe_refresh.py --selftest`: pure-function coverage of `rank_pond`,
  `propose_membership` (band retain/drop, seed protection, size discipline), and
  `classify` (each auto-apply / hold branch: large diff, failed sanity, seed-drop,
  broken screener). No live calls.
- `--dry-run`: run the real pass but print the proposal and write nothing.
- Runs under `deploy/run_selftests.sh` (venv-pinned).

## 10. Risks / open questions (resolve at implementation)

- moomoo screener: exact filter field for dollar-volume/turnover on US market.
- Trailing-$vol data path: `get_cur_kline` quota/subscription vs. snapshots vs.
  the existing price cache — pick the one that stays within moomoo quota.
- Shared OpenD with `moomoo-vol-desk`: the refresh is a brief quarterly/weekly
  batch → negligible load, but confirm no subscription-slot contention.
- First run will likely be small (universe only 11 days stale) but may trip the
  ">5 changes" hold — which is fine and desirable (you see the first one).
