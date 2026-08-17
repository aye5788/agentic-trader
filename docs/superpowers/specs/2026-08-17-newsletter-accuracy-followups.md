# Investor letter — accuracy follow-ups after issue 008

**Date:** 2026-08-17
**Status:** OPEN. Not started. Raised by the principal (Aaron) after reading issue 008.
**Owner:** whoever next touches `scripts/letter_facts.py` / `prompts/newsletter.md`

---

## Why this file exists

Issue 007 (2026-08-16) was factually wrong: it reported the account at $67.58 and
a −3.4% week when the equity curve had Friday at $75.47, roughly +7.9%. Root
cause was the `src/marks.py` cost-basis fallback, fixed 2026-08-17 (`0d1ca83`),
together with three letter-pipeline defects fixed the same day (`8836a9e`).

Issue 008 (2026-08-17) is **accurate about what happened**. Reviewing it turned
up three defects that are about **what the numbers MEAN** rather than whether
they are correctly sourced. All three are the same shape: a figure that is
technically true and misleading without the constraint that qualifies it. That
is the same failure class as the marks defect, one level up, so it is worth
fixing properly rather than asking the narrator to try harder.

⚠️ None of these are the narrator's fault. Each is a fact `facts.json` does not
carry, so the letter cannot state it. **Fix the facts file first, then the
prompt rule.** A prompt rule that depends on a fact the file lacks is a rule the
letter cannot follow.

---

## D1 — Cash overstates what can actually be spent (MATERIAL)

Issue 008 reports **41% cash** and frames it as "a real decision in front of
me". At the time of writing, of `$31.13` cash only **`$8.36` was spendable** —
the ETF sale proceeds were unsettled until T+1. Deployable was **11% of the
account, not 41%**.

The letter therefore reads as though the agent *chose* to sit on a third of the
book. Largely, it could not act. `src/marks.py` already exposes `buying_power`
and `unsettled_funds`, and `src/agent_env/server.py:account()` already carries a
`spendable` field warning that cash is not what can be spent on this cash
account — the letter pipeline simply never picked any of it up.

**Fix:** `scripts/letter_facts.py` — add `buying_power`, `unsettled_funds` and a
derived `deployable_pct` to the `account` block. `prompts/newsletter.md` — when
`buying_power` is materially below `cash`, the letter must say what is actually
spendable and why (T+1 settlement), and must not describe undeployed cash as a
choice when it is a constraint.

⛔ Do NOT state a threshold for "materially below" in the prompt. Give the
narrator both numbers and let it judge, per the standing rule that the code
supplies facts and the agent decides.

## D2 — The reported week overlaps the previous letter's week

Issue 008's header reads **"Week of August 17, 2026"** and reports **+9.6%**,
but `week_pnl_baseline` is `2026-08-10`. So the figure spans Aug 10 → Aug 17 —
the same days issue 007 covered and called **−3.4%**.

Issue 008 does acknowledge that 007 narrated a loss the marks did not show, but
it never says that *this* number re-reports those days. Two consecutive letters,
overlapping windows, opposite signs, and no reconciliation. A reader adding them
together gets a fiction.

Cause: `issue_date` is derived from `today`'s Monday, while `week_pnl` is
derived from the newest equity point at least ~7 days old. The two are computed
independently and can describe different spans — always, not just here.

**Fix:** `scripts/letter_facts.py` — carry the measured window explicitly
(`week_pnl_from` / `week_pnl_to` dates) beside `week_pnl`, and make `issue_date`
agree with it or state both. `prompts/newsletter.md` — the letter must name the
period a percentage covers whenever it differs from the issue's own header
period, and must say so plainly when it re-reports days a previous issue
already covered.

## D3 — A held name ranked outside the book at full target weight

The issue 008 holdings table shows **TER at momentum rank 12 with a 7.1% target
weight**, unremarked — the same name issue 007 half-trimmed as "the weakest-
ranked name in the book". A top-10 book showing a rank-12 name at full weight is
either a real inconsistency or a normal consequence of banded selection, and the
letter says nothing either way.

**Fix:** `prompts/newsletter.md` — when a held position's rank falls outside the
target book while carrying a non-zero target weight, say why in one clause
(banded selection, pending rotation, or an open question). The data is already
in `facts.json` (`positions[].rank`, `positions[].weight`); only the rule is
missing.

## D4 — Protected-gain is absent (resolves itself)

Issue 008 says nothing about **AMD and TER stopping out red** — both show a
profit while their stops sit below cost. It is the single most important risk
fact in the book. The letter could not know: `positions()` did not expose it.

**This is fixed by Task 5 of `docs/superpowers/plans/2026-08-17-agent-set-levels.md`**
(`peak_pct`, `giveback_pct`, `gain_protected_pct`). Once that lands:

**Fix:** `scripts/letter_facts.py` — carry the excursion fields per position.
`prompts/newsletter.md` — a negative `gain_protected_pct` (a position showing a
profit that would close at a loss) must be stated, not implied.

---

## Ordering

D4 depends on Task 5 of the levels plan. D1, D2 and D3 are independent and can
be done in any order. All four should land before the next Sunday letter
(2026-08-23) so the accuracy work is not spread across two issues.

## Verification

- `.venv/bin/python scripts/letter_facts.py --selftest`
- `.venv/bin/python scripts/letter_facts.py` then read `facts.json` and confirm
  every field a new prompt rule depends on is actually present. A rule citing an
  absent field is worse than no rule: it reads as coverage and silently no-ops.
