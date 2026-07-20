# Concentration Cap — Live Wiring (Piece 2, Phase 2)

**Date:** 2026-07-20
**Status:** ⛔ ABANDONED (2026-07-20) — failed the go-live re-test. NOT wired live.
**Author:** Aaron + Claude

> **Why abandoned:** the Phase-1 backtest that favoured the gentle config
> (lb126/thr0.6/cap50) let freed weight concentrate into a few names with no
> per-name ceiling. When the same config was re-backtested with the LIVE
> `[risk] max_weight_per_name = 0.10` ceiling enforced (§4), the edge collapsed:
> CAGR 22.5%→**20.6%** (below the 21.6% baseline), Sharpe 0.92→**0.87** (below the
> 0.90 baseline), and the drawdown cut shrank from ~2.2 pts to ~1.1 pts. The
> benefit *was* the concentration the mandate forbids, so under the real 10% cap
> the cap makes the book slightly WORSE than no cap. This is a structural conflict
> (the finding likely generalises to the other configs), not a tuning miss.
> Aaron's call: shelve it. The single-name 10% cap already does much of the
> de-concentration work, and the intraday stops handle the 07-17 tail. The pure
> `cap_weights` (+ `per_name_cap` water-fill) and `backtest_pit.py --per-name-cap`
> remain as tested tooling that produced this finding; NO live path uses them.
> Full record: docs/OPSLOG.md 2026-07-20. The design below is kept for provenance.

---

---

## 1. Where this comes from

Phase 1 built a pure correlation "cluster cap" (`src/concentration.py`) and backtested it
against the survivorship-free engine. The result (offline, no live money):

| | worst drop | growth/yr | risk-adj (Sharpe) |
|---|---|---|---|
| today (no cap) | −31.2% | 21.6% | 0.90 |
| **gentle cap** (lb126, thr0.6, cap50) | **−29.0%** | **22.5%** | **0.92** |

The gentle setting improved every number while trimming the worst crash by ~2 points — it
strictly beats today's behavior in the backtest. Aaron approved proceeding to live wiring
**with this gentle setting**. This spec is how we'd switch it on, safely and reversibly.

Plain-English recap of what the rule does: your strategy buys whatever's rising fastest,
which tends to be the same *kind* of stock all at once (the July-17 "everything sank
together" event). The cap notices when a group of holdings is moving in lockstep and has
grown too large, trims that group, and spreads the freed money to the holdings that
*aren't* moving with the pack. It never sells a name to cash and never adds a name — it
only adjusts position **sizes**.

## 2. Objective & scope

**In scope:** apply `concentration.cap_weights` inside the live slow loop
(`scripts/slow_loop.py`) so the book it writes to the Research Store carries de-concentrated
weights; make it **config-gated and reversible**; keep the backtest and live using the
*same* function so they can never diverge; observe before fully trusting.

**Out of scope:** any change to the fast loop (it already reads `{symbol: weight}` from the
store and converts to dollars — no change needed), the momentum signal, the geometry/stop
logic, or the universe. The cap is a pure re-weighting layer bolted on at one point.

## 3. The integration point (one place)

In `slow_loop.main()`, after `build_theses` has produced `book_held` + `etf_held` (each a
`Thesis` with a `target_weight`, equal slots today: 7% book / 7.5% sleeve) and **before**
building the `ResearchProduct`:

1. Collect the combined held weights: `{t.symbol: t.target_weight for t in book_held + etf_held}`
   — book and sleeve **together** (a sector ETF like XLK genuinely co-moves with the semis it
   holds, so it belongs in the same cluster analysis).
2. Pass through `cap_weights(weights, closes, asof, cap_params)` using the same `closes`
   panel the loop already loaded.
3. Write the adjusted weights back onto each `Thesis.target_weight`.

Names dropped by the R:R≥2 geometry gate already have `target_weight = 0` (cash) and are
**not** in the dict — so the cap only ever moves money *among the names we're actually
holding*, and the total invested weight is unchanged (geometry-rejected cash stays cash).

## 4. THE KEY DESIGN ISSUE — the 10%-per-name mandate

The risk mandate (`[risk] max_weight_per_name = 0.10`) is enforced by `write_product`: if
**any** name's weight exceeds 10%, the whole book is rejected and the slow loop fails to
write — i.e. no book for the fast loop to execute that day. This is a hard gate, and the
cap's redistribution **pushes receiver weights up**. Today's names sit at 7–7.5% with only
~2.5–3 points of headroom; if the cap frees a chunk of weight and only a few names are
outside the capped cluster, a receiver can cross 10% and break the write.

The backtest never hit this because `backtest_pit.py` has no mandate check — it let
receiver weights grow unbounded. So there are **two problems in one**: (a) live would
occasionally fail to write, and (b) the backtested numbers were produced by a version that
*ignored* the 10% ceiling, so they don't cleanly transfer to a live path that enforces it.

**Resolution (recommended):** extend `cap_weights` with an optional `per_name_cap` (a
water-fill redistribution: pour freed weight onto receivers pro-rata, but never above the
ceiling; any overflow spills to the remaining under-ceiling names, repeat until placed or
everyone's full). Then **re-run the Phase-1 sweep with `per_name_cap = 0.10`** and confirm
the gentle config still delivers (worst-drop still down, growth still ~flat). Only wire live
after that confirmation. This keeps the discipline intact — live uses the *exact* function
the (re-)backtest validated, so the approved numbers are the numbers we ship. It is a small
change to the pure module and one extra sweep, no live risk.

(If the re-run shows the ceiling materially changes the gentle result, that's a real finding
and we reconsider the config — better to learn it offline than in production.)

## 5. Configuration (new, reversible)

Add a `[concentration]` block to `config/strategy.toml`:

```toml
[concentration]
enabled       = false   # master switch — built OFF, armed by a deliberate edit (like Piece 1's cron)
lookback      = 126     # ~6 months of daily returns for the correlation window
corr_threshold = 0.6    # pairwise correlation that counts as "co-moving"
cluster_cap   = 0.50    # a co-moving group may hold at most 50% of invested weight
# per_name_cap is NOT set here — it is READ FROM the risk mandate (max_weight_per_name)
# so the two can never drift apart.
```

Reversibility mirrors the risk-review overlay precedent (`strategy.local.toml` flip): set
`enabled = false` and the slow loop reverts to plain equal-slot weights instantly, no code
change. When `enabled = false`, `slow_loop` does not call `cap_weights` at all — behavior is
byte-for-byte today's.

## 6. Observe-before-trust (arming discipline)

Consistent with Piece 1 ("built but not armed") and [[no-layering-static-governance]] — don't
flip a live weighting change blind:

1. **Ship enabled=false.** The code path exists but is inert. `slow_loop --dry` with the flag
   temporarily on shows what the cap *would* do to this week's real book (which names get
   trimmed, where the money goes) without writing anything.
2. **Observe window.** Run `--dry` across a few weekly rebuilds (and eyeball the risk note in
   §7) to confirm the cap behaves sanely on the live book — that it only bites on genuine
   clusters and doesn't do anything perverse (e.g. lifting a weak name far above a strong
   one).
3. **Arm.** Flip `enabled = true`. From then on the written book carries capped weights and
   the fast loop executes them like any other book.

This is the same "a No is a valid outcome" stance as Phase 1: if the dry-run observation looks
wrong on the *live* book (as opposed to the backtest), we don't arm.

## 7. Observability (so we can see it working)

When the cap is enabled and bites, the slow loop should make it visible — otherwise a silent
re-weighting is unauditable:

- Print, in the slow-loop report, which cluster was capped and the before→after weights of
  affected names (e.g. `concentration: trimmed {NVDA,AMD,LRCX,...} 56%→50%, +2%→GLD +2%→XLV`).
- Record the same as a structured `note` on the ResearchProduct / journal so the dashboard and
  the weekly newsletter facts can surface "the cap moved X% this week" if we want it later.
- No-op weeks (no cluster over cap) say nothing — matches the [[no-as-designed-skip-noise]]
  principle; only report when it actually acted.

## 8. Safety properties (why this is low-risk)

- **Pure & shared:** live calls the identical `cap_weights` the backtest validated — no
  parallel implementation to drift.
- **Weights only:** never adds/removes a name, never moves money to cash; total invested
  weight preserved to floating-point tolerance. Every existing guardrail (R:R gate, kill
  switch, per-order cap, universe whitelist, live_approved) is untouched and still upstream/
  downstream of this.
- **Mandate-respecting:** with §4's `per_name_cap` tied to `max_weight_per_name`, the written
  book always passes `write_product` validation.
- **Reversible:** one config flag back to today's behavior.
- **Degenerate-safe:** regime-off / <2 held names → `cap_weights` returns weights unchanged
  (an empty or single-name book can't have a cluster). Cash books are unaffected.

## 9. Testing

- Extend `src/concentration.py` `_selftest`: a `per_name_cap` case — freed weight water-fills
  onto receivers, no receiver exceeds the ceiling, overflow spills correctly, total preserved.
- A `slow_loop` selftest (or `--dry` assertion) that on a crafted held-book the written
  `target_weight`s (a) sum to the same total as uncapped, (b) never exceed
  `max_weight_per_name`, (c) equal the uncapped weights when `enabled=false`.
- The re-run sweep (§4) is the integration evidence: gentle config still passes with the
  ceiling enforced.
- Add to `deploy/run_selftests.sh` (concentration already there; add slow_loop if it gains a
  `--selftest`).

## 10. Open decisions (need Aaron before the build plan)

1. **Per-name-cap handling (§4).** Recommended: add `per_name_cap` to `cap_weights` + re-run
   the sweep to confirm the gentle config survives the 10% ceiling, then wire. Alternative:
   wire without it and accept occasional write-rejections (not recommended — breaks the live
   book on bite-days). *This is the one real technical fork.*
2. **Arming (§6).** Recommended: ship OFF, dry-run observe on the live book for a few weeks,
   then flip ON — matching Piece 1. Alternative: arm ON at merge (faster, less cautious).

## 11. Files (anticipated)

- **Modify:** `src/concentration.py` (add optional `per_name_cap` water-fill + selftest).
- **Modify:** `scripts/backtest_pit.py` (pass `per_name_cap` in the sweep; re-confirm gentle).
- **Modify:** `scripts/slow_loop.py` (apply the cap at §3's integration point, config-gated).
- **Modify:** `config/strategy.toml` (`[concentration]` block, `enabled=false`).
- **Modify:** `src/strategy.py` if a loader/accessor is needed for the new block.
- **Deliverable:** confirmed re-run sweep + the armed (or observe-pending) live book.
