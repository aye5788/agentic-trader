# Giving the agent a working mechanism to set its own price levels

**Date:** 2026-08-17
**Status:** design, approved in principle by the principal (Aaron)
**Author:** session with Aaron, 2026-08-17

---

## The principle this serves

The agent decides where its levels sit. The code's job is to make that
*possible*, make it *stick*, and make it *visible* — never to pick the number.

This spec deliberately contains **no thresholds**. No 10% rule, no automatic
ratchet, no "protection may only move up" policy. Every one of those was
considered and rejected during design: each puts the code in charge of a
judgement that belongs to the agent, which is how a decision-maker becomes
decorative. What follows is plumbing only.

## Where this came from

Reviewing the open book on 2026-08-17 (8 single names, avg **+20.9%**
unrealized), the principal asked whether the agent was considering pulling its
take-profit targets in, and observed that unrealized gains are exposed if the
market corrects. Pricing it: if every stop fired, **only 21% of the $7.65
unrealized gain would be kept**, and AMD and TER — both showing a profit —
would close *red*, because their stops never followed the price up.

Investigating why the agent had never acted on this turned up the answer: it
has never had a working mechanism.

## What is NOT broken (verified, and initially mis-diagnosed)

An earlier reading of this session claimed the weekly `slow_loop` rebuild erases
agent-set levels. **That was wrong**, and the correction matters because it
removes the largest piece of proposed work:

- `market_monitor` re-reads `research_store/monitor/overrides.json` from disk
  **every tick** (market_monitor.py:926) and applies it on top of whatever
  theses the loop last computed.
- Nothing anywhere deletes `overrides.json`. `slow_loop` never touches it.
- `state.holdings()` merges overrides into the agent's own `positions()` view,
  and `memory._level_reasons()` reads the reason attached to each.

So an agent-set level already survives the Sunday rebuild automatically.
Durability needs no work. The live stops match loop geometry for one reason
only: **`overrides.json` has never existed.**

## The actual defects

### D1 — `set_levels` cannot express this book's theses (the blocker)

`set_levels(symbol, stop, target, reason)` accepts a **single** target. Every
thesis in the live book carries **two** (T1/T2). `decide.py` enforces a length
match:

> `thesis has {len(cur)} target(s); a single target here only matches a thesis
> with exactly 1 target -- ignored`

So for every position actually held, a target change is reported as ignored.
The agent has been correctly told it cannot set a take-profit. This is the
missing mechanism, and it is the reason the answer to "is the agent considering
ranging the targets in?" is no.

### D2 — the advisory contradicts the enforcement

`market_monitor.apply_overrides` was deliberately changed to let targets move
in **either** direction, carrying this comment:

> TARGETS MOVE IN EITHER DIRECTION, FREELY. This was min() … Combined with a
> stop that could only be raised, BOTH permitted directions shortened the trade
> … blocking it is why nothing in this book has ever reached a take-profit.

`decide.py` — the layer the agent reads — still reports the old rule:
*"the monitor only lowers targets, so this is ignored."* The enforcement was
fixed; the advice the agent acts on was not. An agent told a legal move is
illegal will not make it.

### D3 — the agent cannot see the problem the principal saw

`positions()` shows qty, cost, mark, P&L, stop, targets. It does **not** show
how far a position ran, what it peaked at, how much it has already handed back,
or how much of the current gain the stop would actually keep. The principal
identified the exposure from these figures in minutes; the agent is never shown
them, so it has no basis on which to decide a level should move.

### D4 — a recorded level decision is never checked against reality

On 2026-08-12 and 08-14 sessions recorded deliberate stop tightenings on SNDK,
STX and AMD. `overrides.json` does not exist, so none took effect. The decision
was journalled, and the 08-16 investor letter reported the tightening to the
account owner as a completed de-risking. Nothing compared the claim to the
artifact. This is the repo's recurring "control binding gap": a decision that
records but does not bind.

## Design

Four changes, each independently useful and independently testable.

### C1 — `set_levels` accepts a full target list

Signature becomes `set_levels(symbol, stop, targets, reason)` where `targets`
is a list matching the thesis's own count, with the single-value form still
accepted for a one-target thesis so existing callers and the charter keep
working.

Enforcement stays exactly where it already is: `apply_overrides` remains the
sole authority on what the monitor honours. `decide.py` only *reports*.

A count mismatch remains a refusal — the monitor genuinely ignores those — but
the note must say what count was expected so the agent can retry correctly
rather than infer it.

### C2 — `decide.py` reports what `apply_overrides` actually does

Target direction: report `enforced: true` for a raise as well as a lower, since
that is what the monitor does. Stops keep their existing asymmetry, including
the `widen` + reason opt-in that `apply_overrides` already honours — which
`decide.py` currently does not mention at all.

These two functions are a documented duplicate pair (`decide.py` cannot import
`market_monitor`, which loads the moomoo SDK). The duplication is the defect's
cause and cannot be removed here, so it must be **pinned by test**: a selftest
asserting that `decide`'s verdict and `apply_overrides`' actual effect agree
across a shared table of cases. Divergence becomes a failing test rather than a
silent lie.

### C3 — `positions()` reports run-up, peak, giveback, and protected gain

Per held position, from `highs.parquet` plus the current mark and cost:

- `peak_pct` — best unrealized gain reached while held
- `giveback_pct` — `peak_pct` minus current unrealized
- `gain_protected_pct` — share of the current gain that survives if the stop
  fires: `(stop − cost) / (mark − cost)`, and **negative when the stop sits
  below cost**, which is the AMD/TER case stated plainly rather than implied

`gain_protected_pct` is the number that makes the exposure legible in one
glance. No threshold attaches to any of them; they are facts the agent reads.

Entry date comes from the earliest still-open buy in the journal. Where that is
ambiguous (a symbol re-entered after a full exit) the field reports `null` with
a reason rather than a wrong number — a confident wrong peak is worse than an
absent one.

### C4 — session-end level verification

`session.py` already snapshots integrity around a session. Extend that pattern:
if a session recorded an `agent_decision` whose action implies a level change
(`tighten_stops`, `set_levels`, `lower_tp`, or similar) and `overrides.json` is
unchanged, that is surfaced as a failed expectation — the same way a claimed
fill with no execution is caught by the existing `unrecorded_fills` health
check.

This does not block or retry. It makes the D4 class visible: the agent said it
did something, and the artifact disagrees.

## Non-goals

- No threshold, trigger level, or automatic ratchet anywhere in code.
- No change to `apply_overrides`' enforcement semantics. It is already correct;
  only its advisory twin is wrong.
- No change to how the slow loop computes geometry for un-ruled positions.
- No new alert. D4 surfaces through existing health/journal surfaces.

## Testing

- `decide.py` selftest: shared case table asserting agreement with
  `apply_overrides` on stop raise / stop lower / widen+reason / target raise /
  target lower / count mismatch. This is the regression that would have caught
  D2.
- `decide.py` selftest: multi-target thesis accepts a matching list and refuses
  a mismatched one with the expected count named in the note.
- `state.holdings` selftest: peak/giveback/protected-gain arithmetic, including
  the stop-below-cost case yielding a negative `gain_protected_pct`, and the
  ambiguous-entry case yielding `null`.
- `session.py` selftest: a recorded level decision with an unchanged
  `overrides.json` is flagged; an unchanged file with no such decision is not.
- Live verification: after C1/C2, call `set_levels` against a real two-target
  thesis and confirm `overrides.json` appears and `positions()` reflects it.

## Open question, deliberately left to the agent

Whether any position should have its levels moved, and to what, is not decided
here. Once C1–C3 land, the agent can see the run-up and act. If it still does
not, that is a finding about the charter or the agent's reasoning — not about
the plumbing — and should be judged separately rather than pre-empted by a rule.
