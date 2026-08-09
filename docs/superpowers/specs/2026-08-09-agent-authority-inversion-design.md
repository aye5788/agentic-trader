# Agent-authority inversion — design

**Date:** 2026-08-09 · **Status:** approved, not yet planned · **Author:** Claude + Aaron

## 1. The problem

This repo is a deterministic rules engine with an agent bolted to its output.
`momentum.py` selects the book, `slow_loop.py` computes trade geometry from a
formula, `fast_loop.py` diffs targets against holdings, and the agent's entire
discretionary surface is `prompts/fast_loop.md` step 7b — *"veto/downsize ONLY,
never more than the mechanical plan wanted."* Its whole authority is to do **less**
than the code already decided.

That architecture can only answer one kind of question. Three studies on 2026-08-09
(`calibrate_geometry.py`, `calibrate_targets.py`, `sim_recycle.py`) asked whether the
trade geometry was right and each produced a parameter verdict, because a parameter
verdict is the only shape of answer the system can accept. The agent is not in the
loop that matters.

**Target architecture:** code defines the environment (what tools exist, what facts
are available) and places guardrails (what is forbidden, what is unsafe), then the
agent decides. Reference implementation: `/opt/trading` on `165.227.74.110`
(`aye5788/ClaudeChallenge`) — a different domain, but the shape is the thing.

**This is an inversion, not a refactor.** The signal becomes a candidate generator,
the strategy config splits into mandate and environment, prompts become briefs
instead of procedures, and governance keeps only what rejects on safety.

## 2. Decisions locked in

| # | Decision | Rationale |
|---|---|---|
| 1 | Build the whole inversion, not a staged minimum | Aaron: "I want it done RIGHT and CAREFULLY... tired of testing and deferrals." Staging is an implementation ordering, not a series of half-shipped states. |
| 2 | Flatten to cash before the live path changes | Removes the migration hazard entirely — no positions cross the boundary, no stops to carry, no window where a half-inverted system holds risk. |
| 3 | Mandate = falsifiable criteria, measured mechanically | What makes autonomy auditable. Without a stated goal there is no mechanical way to say whether autonomy is working, which is how you end up back at tuning parameters. |
| 4 | **No epochs.** Continuous measurement + a monthly review ritual | Epochs were imported from a bounded *challenge* with a stamped equity and a termination condition. Perpetual capital has neither. An epoch reset would also hand an agent deep in drawdown a clean slate on the 1st — a gameable boundary in the one criterion that must never be gameable. |
| 5 | Momentum top-10 surfaced by default; full universe viewable on request | Aaron: "give it those 10 but also make the others available to view IF it wants to — that way it doesn't waste time cycling through 150 but knows it has other options." The screen is an **attention budget, not a boundary**. |
| 6 | Agent owns all four exits; machine keeps mandate-flatten only | The line is: **machines may enforce the agent's own instruction, or the mandate's terms; they may not form a market opinion.** Stop and target become levels the agent sets. Rank rotation and regime-off are market opinions and move to the agent. |
| 7 | Autonomous within the mandate; no per-trade approval | Aaron approves the terms and the environment, not individual trades. He watches continuously on his phone, so **per-trade visibility replaces per-trade approval**; the HALT switches are his hands. |
| 8 | Custom MCP server is the environment | The vehicle that makes "code defines the tools, agent decides" real. Today the agent gets Robinhood MCP + bash + a procedural prompt, which is why it can only follow steps. |
| 9 | Retire the adaptive tuner; keep the ledger | `tune_stop.py` tunes `stop_atr_mult`, a knob that ceases to exist. The Decision→Outcome Ledger underneath stays — it is the raw material for agent memory. |

## 3. Ownership

| Decision | Owner |
|---|---|
| Which names to hold, whether to hold anything at all | **Agent** |
| Position size, within the caps | **Agent** |
| Entry timing and price discipline | **Agent** |
| Stop price and target price, per position | **Agent** |
| When to exit — including on rank loss or regime change | **Agent** |
| What to reconsider and when (wake conditions) | **Agent** |
| Order rejection on safety grounds | Machine — gate. **Rejects, never selects.** |
| Enforcing the stop/target levels *the agent set* | Machine — monitor |
| Flatten on mandate breach | Machine — the **only** mechanical close |
| Marking, P&L, drawdown, criteria measurement | Machine — arithmetic |
| Candidate generation and terrain | Machine — a screen, not a decision |

## 4. Disposition of existing components

| Component | Becomes |
|---|---|
| `src/momentum.py` | Math unchanged, demoted. Produces the candidate view. `select()` (banded retention) **retires** — keeping a name is now the agent's call. |
| `scripts/slow_loop.py` | **Never writes targets again.** Becomes the brief builder: cross-section, facts, terrain, mandate status → session brief. |
| `config/strategy.toml` | Splits. `config/mandate.toml` = falsifiable criteria (the terms). `docs/ENVIRONMENT.md` = capability, limits, measured facts, explicitly **not** policy, with a self-defending clause. Tuning knobs (`stop_atr_mult`, `target_r_mults`, `book_hold`, `book_band`, regime thresholds) are **deleted** — they are strategy encoded as config. |
| `src/governance.py` | Keeps account scoping, `live_approved`, both HALT switches, fat-finger notional cap. Gains mandate-breach flatten. Universe whitelist stops being a *selection* restriction and becomes a liquidity gate. `write_product`'s reward:risk ≥ 2 test is **deleted** — tautological (targets are built as 2.2× the stop, so it always passes) and it is a strategy opinion sitting in a safety path. |
| `scripts/fast_loop.py` | **Dies.** There are no targets to diff against. |
| `prompts/fast_loop.md` | Procedure → brief. |
| `scripts/market_monitor.py` | Keeps enforcing, stops computing. Executes levels the agent chose. |
| `scripts/risk_review.py` | Folds away — a de-risk-only overlay is redundant once the agent has continuous full authority. |
| `scripts/tune_stop.py`, `src/adaptive.py`, `src/stop_replay.py` | Retire. |
| `src/research_store/` | Stops being a target book; becomes the decision + outcome record. |
| `scripts/letter_facts.py` | Converted **in the same change** — it reads `read_current()` for the book and breaks the moment targets stop being written. |
| **New** `src/mcp/` | The environment: the tool surface. |
| **New** mandate measurement module | Continuous evaluation of the four criteria. |

## 5. The mandate

All four measured **continuously**. No calendar boundaries.

1. **Drawdown ≤ 15%** from the all-time high-water mark (`governance.update_peak`
   already tracks it), measured **close-to-close, never intraday** — an intraday
   measure would fire the flatten on noise. Breach → mechanical flatten + loud alert.
   *Why 15%:* the reference system's 3% was for a one-month paper challenge; a
   long-only momentum book sees 35–49% max drawdown over ten years
   (`sim_recycle.py`), so 3% would fire on ordinary market beta. Current setting is
   25%; 15% is deliberately tighter.
2. **No position > 15% of equity** at any mark. (Store currently caps at 10%, slots
   are 7%; 15% gives room for conviction without betting the book.)
3. **Over trailing 90 days:** no single closed round-trip > 40% of realized P&L,
   **and** ≥ 4 distinct names closed. Stops one lucky trade carrying the verdict.
   *Undefined when trailing realized P&L ≤ 0* — the criterion reports `n/a` rather
   than passing or failing, because a share-of-profit test has no meaning without
   profit. It must never read as a pass by default.
4. **Beat SPY over trailing 60 trading days** on **total return including
   unrealized**, comparing the marked book (via `src/marks.py`) against SPY total
   return over the same window. Floor: realized + unrealized ≥ 0 over that window.
   *Why not an absolute target:* a month's return on an equity book is dominated by
   market beta — the agent would pass in a rising market and fail in a falling one
   regardless of skill. This is the criterion chosen for meaning over
   ease-of-measurement, and the one most likely to need revision.

A monthly report is a **review ritual**, carrying no measurement semantics.

## 6. The environment

### The brief (facts only, no instructions)

- Mandate status — all four criteria with real numbers **and room remaining on each**
- Positions: entry, mark, the stop and target *the agent set*, and its stated reason
- Candidate view: momentum top-10 with score, rank, R, σ, trend — plus a note that
  the full 168 is one call away
- Regime **facts**: SPY vs 50DMA, VIX — stated as facts, never as a switch
- Earnings proximity for anything held or ranked
- **Excursion terrain**: how far price actually travels in 5/10/20 days in σ terms,
  per candidate — so stops and targets are set against measured reality instead of a
  formula. (From `calibrate_geometry.py` / `calibrate_targets.py`, 2026-08-09.)
- Open questions and what has been ruled out

### Tool surface (`src/mcp/`)

| Tool | Notes |
|---|---|
| `positions`, `account`, `mandate_status` | State and terms |
| `candidates(n)`, `universe(...)` | Top-10 default, full list on request |
| `terrain(symbol)` | Excursion distribution for a name |
| `place_buy`, `place_sell` | Through the gate |
| `set_levels(symbol, stop, target, reason)` | **`reason` required.** Monitor enforces exactly this. |
| `wake_register`, `wake_status`, `wake_deregister` | Attention, with budget/priority/TTL |
| `record_decision(symbol, action, reason)` | Every action carries why |
| `rule_out(...)`, `open_question(...)`, `research_log()` | Memory across sessions |

### Sessions

Premarket, open, close — **full authority, no procedure**. Plus agent-registered
wakes firing full sessions. Stops and targets the agent set are enforced by the
monitor **without waking anything**, because that decision was already made — which
also deletes the exit-executor path and its 180s-timeout fragility.

## 7. The record

- **Agentic behavioral log** — full session transcript per session, plus a structured
  session report (decided / why / gate rejections / mandate state), cross-linked to
  ledger decision IDs. Distinct from the ledger: the ledger records *what* was decided
  and how it turned out; this records *how it reasoned*, which is the only way to
  audit judgment rather than results, and the only way to find brief defects.
- ⚠️ **Secret scanning ships with the log, not after an incident.** The reference
  project leaked four live credentials (`WEBULL_APP_KEY`, `RESEND_API_KEY`, and two
  more) inside archived Claude Code transcripts, invisible to a hand-rolled grep.
  This repo has no gitleaks. Their `.gitleaks.toml` + pre-commit wiring is portable.
- **Phone** — every agent action pushes what/which name/one-sentence why. Replaces
  per-trade approval. Settlement and buying-power deferrals stay silent (standing
  rule); mandate breach and unprotected-position stay loud and distinct.
- **Dashboard** — standing state: positions, agent-set levels with reasons, mandate
  status with room remaining, recent decisions.
- **Newsletter** — survives and improves. `letter_facts.py` → facts → narrate-only
  separation is preserved exactly (it is what keeps the letter honest). Inputs change
  to positions-with-reasons, decisions-and-why, outcomes, mandate status. The letter
  previously had to infer rationale from mechanical rotations; rationale is now
  first-class data. Standing rule holds: portfolio impact in the letter, plumbing to
  `docs/OPSLOG.md`.

## 8. Safety model

1. **Account scoping — upgraded from prose to code.** The reference system's Layer 1
   is *the endpoint*: a paper key that returns 401 against the live API, so real money
   is physically unreachable. **We have no equivalent.** Account scoping is therefore
   our Layer 1, and today it lives in `prompts/fast_loop.md` step 2 as an instruction
   the agent is asked to follow. Under autonomy that is not good enough: it moves
   inside `place_buy`/`place_sell` — resolve `agentic_allowed=true` at call time,
   require exactly one, abort otherwise.
2. **The gate** rejects on: wrong account, HALT active, HALT_ENTRIES for a buy,
   concentration > 15%, liquidity below threshold, malformed input, and a fat-finger
   notional cap (the existing `[governance] max_order_pct`, retained as a
   typo/runaway guard — **not** as a risk control, since cash already bounds
   exposure). Returns a reason the agent can read and report. A rejection is a
   finding, never a licence to work around it.
3. **Mandate enforcement** — continuous; drawdown breach fires the flatten.
4. **HALT / HALT_ENTRIES** — built 2026-08-09 (`a233351`), unchanged.
5. **Detective controls, stated honestly.** The agent has bash and can rewrite its own
   gate. We cannot prevent this — the reference project *measured* that
   `Bash(python3:*)` writes anywhere regardless of tool allowlists. So: SHA-256 the
   protected set (`src/mcp/`, `src/governance.py`, `config/mandate.toml`,
   `scripts/market_monitor.py`) before and after each session; any change halts and
   reports. The brief states the rule plainly — **the rule is the control, not the
   tooling.**
6. **`live_approved`** master switch — unchanged.

**What the broker does and does not cover.** This is a **cash** account: no leverage,
no shorting, no naked structures, so the reference system's unbounded-max-loss and
`max loss > $800` gates solve a margin-account problem that does not exist here, and
Robinhood enforces buying power, settlement and tradability itself. But **broker
rejection is a backstop, not a control** — RH rejects for its reasons, not ours, and
that can change without notice. Account scoping, concentration, liquidity, the HALT
switches and the mandate are structurally uncoverable by the broker and stay ours.

### The invariant

**Every position has an agent-set stop, or it is loudly flagged unprotected** —
checked every monitor cycle and in daily health. Stops here are software-only; a
position without one is invisible to the only protection the system has.

### Degraded mode

One central posture replacing today's scattered fail-open decisions (the 2026-08-09
audit found `no_chase` and the VIX read each failing open independently and
silently): **manage what is open, open nothing new** — whenever OpenD is down, quotes
are stale in RTH, the snapshot is stale, or the mandate is unmeasurable.

## 9. Explicitly NOT taken from the reference

Aaron: *"we are not just cloning it — some of it is not appropriate."*

- **The entire options surface** (`place_structure`, `option_chain`, `iv_vs_realized`,
  `assess_structure_risk`, VRP) — this book is equities-only, options off.
- **Their Layer 1 safety model** — no paper endpoint exists here. See §8.
- **The challenge framing** — bounded run, dollar target, terminate on reach.
- **Whole-market screening** — the 150 are curated for liquidity; "full list" means
  that list, not the market.
- **The intraday posture** — premarket movers, 90-minute sessions working unfilled
  limits. This is a multi-day momentum book.
- **The experiments framework** — likely more machinery than a book making a handful
  of decisions a week needs. Revisit later.
- **Margin-account gates** — see §8.

## 10. Rollout

**Flatten to cash before the live path changes**, then build against a flat book, then
re-enter under the new system. The flatten is not a consolation — it dissolves the
hardest problem in the migration: with no positions there is nothing to carry across
the boundary, no stops to migrate, and no window where a half-inverted system holds
real risk. The agent's first session opens its own positions with its own stops, from
flat, entirely under the new model.

**Verified with evidence before arming** — not as a deferral, but because shipping
unverified gates onto a live account is indefensible:

- Every gate rejection path exercised against fixtures: wrong account, HALT,
  HALT_ENTRIES, over-concentration, illiquid name, malformed order
- The unprotected-position invariant fires when a position has no stop
- Protected-file hashing detects a modification
- Mandate numbers match hand-computed values
- One session at `live_approved=false` proving the chain end to end — brief renders,
  tools respond, gates reject, decisions log, phone pushes. An hour, not an epoch.

Then arm.

## 11. Open items

1. **Wake registration mechanism.** The reference rides moomoo server-side conditions
   (`watcher.py: sync_server_triggers` / `drop_server_trigger`). We run OpenD here so
   the capability exists, but this is the part of that system understood least — it
   needs `session.py` and `watcher.py` read properly before building, not guessed at.
2. **Mandate criterion 4** — chosen for meaning over ease of measurement; most likely
   of the four to need revision after live observation.
3. **Liquidity threshold numbers** — the gate replacing the whitelist needs concrete
   minimum dollar-volume and market-cap values, derived from the current 150 rather
   than invented.
4. **Newsletter fact schema** — needs designing against the new decision record.
5. Whether the epistemic layer (`rule_out` / `open_question` / `research_log`) ships
   with v1 or immediately after. It is not needed for the agent to trade correctly,
   but every session run without it produces reasoning that is lost.
