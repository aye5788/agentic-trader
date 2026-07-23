# moomoo Signal Panel — design spec

**Date:** 2026-07-23
**Author:** Aaron + agent (brainstorm)
**Status:** proposed (awaiting review)
**Related:** [[adaptive-input-layer]] (dial #1), the deferred dial #2 = residual
momentum (a separate *backtested* project). This spec is the **forward-logged**
half — see the methodology split in `docs/DATA_SOURCES.md` §5a.

---

## 1. Purpose

Start **forward-collecting** a small panel of differentiated moomoo signals,
stamped onto each weekly trading decision in the ledger, so that once realized
outcomes accumulate we can validate them prospectively (meta-labeling) — *"among
the names we held, did the ones the big money was buying / the crowd was shorting /
the options were positioned on actually do better?"*

moomoo history is shallow (~1–2 yr, `docs/DATA_SOURCES.md` §5a), so these signals
**cannot be honestly backtested**. The only rigorous path is to record their value
*at decision time* now and let the Decision→Outcome Ledger become their validation
set over the coming year. This spec builds that recording — nothing more.

## 2. Scope

**In scope:** a weekly, automatic, per-**held-name** snapshot of four moomoo
signals, distilled to compact scalars, appended to the ledger as one
`signal_panel` journal event.

**Explicitly out of scope:**
- Any *consumer* of the panel. It does not feed the ranking, sizing, or execution.
  It is inert collection. Meta-labeling is a later, separate project.
- The full 150-name universe. We log only the **book** (names actually held that
  week) — those are the names that get outcomes, and it keeps the pull tiny.
  Universe-wide capture (to study selection) is a later option.
- dial #2 / residual momentum (separate spec; it's backtested, not forward-logged).

## 3. Non-negotiable boundaries

- **Never trades, never gates anything.** It reads moomoo data and appends to the
  journal. It cannot affect a live decision. (moomoo `unlock_trade` is never
  imported.)
- **Non-fatal by contract.** OpenD down, a name failing, a partial pull — none of
  it blocks or errors the trading cycle. It logs what it can and records the gap.
- **Respects the runtime seam.** moomoo imports only under system
  `/usr/bin/python3` (3.10). This collector runs there, NOT under `.venv`.

## 4. Design

### 4.1 Where it runs

A new standalone script **`scripts/collect_signals.py`**, run under **system
`/usr/bin/python3`** as its own cron step **right after the Sunday weekly
rebalance** (`run_slow_loop.sh`). Sequence: slow loop writes the new book → this
reads that book → pulls the panel via OpenD → appends one journal event. The
`.venv` slow loop is untouched (no import of moomoo into it, no new dependency).

Crontab (added to `deploy/crontab.template`), Sunday 20:15 (after the 20:00 slow
loop):
```
15 20 * * 0  cd /opt/agentic-trader && /usr/bin/python3 scripts/collect_signals.py >> logs/signals.log 2>&1
```

### 4.2 What it reads (the book)

The held names + their `as_of` come from the Research Store's current product
(the theses the slow loop just wrote). `decision_id = "<symbol>:<as_of>"` — the
same join key the ledger already uses, so the panel joins to outcomes for free.
Read via plain JSON / the research_store read helper (no `strategy.load`, so no
`tomllib` dependency under 3.10).

### 4.3 What it pulls & how it's distilled

For each held `code` (`US.<symbol>`), from the moomoo adapter (extend
`src/adapters/moomoo/research.py`). Per-name calls: capital flow, short interest.
Batched calls: option overview, market snapshot (one call each for the whole book).

| Field (logged) | Distillation | moomoo source |
| --- | --- | --- |
| `capflow_bignet_20d` | Σ over last 20 daily rows of `(super_in_flow + big_in_flow)` (net large-order $), ÷ `total_market_val` → large-order net accumulation as a fraction of market cap | `get_capital_flow(code, period_type=DAY)` + snapshot mktcap |
| `short_pct` | latest `short_percent` | `get_short_interest(code)` |
| `days_to_cover` | latest `days_to_cover` | `get_short_interest(code)` |
| `short_pct_chg` | latest `short_percent` − previous reading's | `get_short_interest(code)` |
| `pc_vol_ratio` | `put_volume / call_volume` (0-guard) | `get_option_underlying_overview([codes])` |
| `pc_oi_ratio` | `put_open_interest / call_open_interest` (0-guard) | `get_option_underlying_overview([codes])` |
| `iv_rank` | `iv_rank` field verbatim | `get_option_underlying_overview([codes])` |
| `pct_52w_high` | `last_price / highest52weeks_price` | `get_market_snapshot([codes])` |
| `volume_ratio` | `volume_ratio` field verbatim | `get_market_snapshot([codes])` |

The **distillation functions are pure** (dataframe/row → scalar) and live in a new
pure module `src/signal_panel.py` with a `--selftest` over fixtures — no OpenD
needed to test the math. `collect_signals.py` is the thin I/O orchestrator.

### 4.4 What it writes (one journal event/week)

Append one `signal_panel` event via `store.append_journal` (append-only, atomic,
already backed up to the ledger mirror):
```json
{
  "event": "signal_panel",
  "as_of": "2026-07-27",
  "at": "2026-07-27T20:15:04Z",
  "source": "moomoo",
  "names": {
    "AAPL": {"decision_id": "AAPL:2026-07-27", "capflow_bignet_20d": 0.0012,
             "short_pct": 0.71, "days_to_cover": 1.4, "short_pct_chg": -0.03,
             "pc_vol_ratio": 0.81, "pc_oi_ratio": 0.73, "iv_rank": 77.7,
             "pct_52w_high": 0.956, "volume_ratio": 0.54}
  },
  "opend_ok": true,
  "gaps": []
}
```
Each name carries its `decision_id`; a per-name field that failed is `null` and the
reason recorded in `gaps` (e.g. `"MU: capital_flow failed"`). No change to the
Thesis schema or the slow-loop write path.

### 4.5 Failure handling & gap alerting

- Wrap every call; a failed name/field → `null` + a `gaps` entry, never a crash.
- **If OpenD is unreachable OR zero names were collected**, set `opend_ok=false`
  and fire a phone push via `src/notify.py` `push()` (never raises) — because the
  cron ERR-trap won't catch a clean-exit partial. A silent missing week is the real
  risk (it's a hole in next year's training set), so it must self-report.
- A missed week is just a missing `signal_panel` row — acceptable, no back-fill.

## 5. Constraints honored

- **No new live-trading surface** (§3). Append-only journal write; no order path.
- **Runtime seam** — collector is system-python3.10, imports moomoo + stdlib +
  pandas (present under 3.10) + the pure journal-append; **no `.venv`-only deps**.
- **No bloat** — one script, one journal event type, one cron line. No new repo, no
  new dataset, no new storage backend; rides the existing ledger + its backup.
- **Idempotent-ish** — keyed by `as_of`; a re-run overwrites/duplicates only that
  week's event (dedupe on `as_of` if re-run).

## 6. Testing

- `src/signal_panel.py --selftest` — the pure distillation functions against
  hand-built fixtures (a capital-flow frame → known `capflow_bignet_20d`; a
  short-interest frame → `short_pct`/`days_to_cover`/`short_pct_chg`; an
  option-overview row → the three ratios; a snapshot row → `pct_52w_high`,
  `volume_ratio`). 0-guards on the ratios covered.
- `scripts/collect_signals.py` — a **live dry-run** on the box (OpenD up) prints the
  panel for the current book without appending; confirm shapes + no crash. Full run
  verified by inspecting the appended `signal_panel` event.
- moomoo return-shape guard: normalize `(ret, data, …)` tuples defensively (some
  moomoo calls return >2 values — see `docs/DATA_SOURCES.md` §5d).

## 7. Decisions already made (no open questions)

- **Forward-log, not backtest** (moomoo history too shallow).
- **Book-scoped** (held names only) — the meta-labeling training set.
- **Four signals, locked fields** (§4.3). Insider dropped (gameable); PEAD dropped
  (redundant w/ momentum); analyst dropped (finnhub cleaner). `iv_rank` KEPT as a
  per-name fragility flag.
- **Storage = one `signal_panel` journal event/week**, keyed by `decision_id`/`as_of`.
- **Runs automatically** weekly after the Sunday rebalance, under system python3.
- **Non-fatal + OpenD-gap alerting** so silent misses surface.

## 8. Risks / watch-items

- **OpenD session drop.** The moomoo login is owned by `moomoo-vol-desk`; if it logs
  out (SMS re-auth), the panel silently gaps. Mitigation: the §4.5 alert. Watch-item:
  a shared OpenD-health signal across the sibling repos would be better long-term.
- **Field semantics drift.** moomoo field names/units could change; the distillation
  is pinned to the 2026-07-23 verified schema. Mitigation: selftest fixtures + the
  re-verify snippet in `docs/DATA_SOURCES.md`.
- **Book too small early.** With ~10–14 names/week, the meta-labeling set grows
  slowly (~500–700 name-weeks/yr). Accept — that's the honest data rate; it's why we
  start now.
- **`short_pct_chg` needs ≥2 readings**; a fresh name may have one → `null`, fine.

## 9. Build order (each its own plan step)

1. **`src/adapters/moomoo/research.py`** — add the four data pulls: `capital_flow`,
   `short_interest`, `option_overview` (batched), `snapshot_fields` (batched).
   Defensive tuple-normalization; return plain dicts/frames.
2. **`src/signal_panel.py`** — pure distillation functions + `--selftest`.
3. **`scripts/collect_signals.py`** — read book → pull → distill → append
   `signal_panel` event; non-fatal; OpenD-gap alert via `notify.push`. Live dry-run.
4. **`deploy/crontab.template`** — the Sunday-20:15 system-python3 cron line.
5. Docs: note the `signal_panel` event in `docs/DATA_SOURCES.md` / OPSLOG.

## 10. Future (not this spec)

- **Meta-labeling consumer** — once outcomes accumulate, join `signal_panel` ×
  `outcome` by `decision_id`, learn "is this pick trustworthy" as a sizing/filter.
- **Universe-wide capture** — to study selection, not just held-name reliability.
- **dial #2 = residual momentum** — the separate backtested dial.
