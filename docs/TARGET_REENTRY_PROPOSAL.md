# Target re-entry design proposal — NOT IMPLEMENTED

Status: proposed 2026-09-09. This document changes no live behaviour, gate, or
strategy setting. It is the implementation brief for a later, reviewed change.

## Decision

Preserve agent discretion after a profit-target fill. Do **not** add a blunt
fixed-duration ban on re-entering a symbol. A target re-entry may be intelligent:
a genuine pullback, new structural evidence, a previously undersized position,
or remediation after a defective monitor action can all justify one.

But a target fill deliberately changed exposure. Restoring that exposure must be
a new, explicit underwriting decision—not an accidental consequence of a high
rank, stale position state, or a monitor/ranking race.

## Why this is needed

The existing cooldown is intentionally a **stop-out** control only: the monitor
writes it after a stop, and the order gate refuses a buy until it expires. It
does not apply to `target1`, which normally trims rather than closes a position.

This is not itself wrong, but it leaves two gaps:

1. On 2026-09-09, DELL's `target1` sold half at 559.40 and the session later
   added exposure around 554.35. The session documented its reasoning and moved
   the target first, but the system had no dedicated target-re-entry decision
   path.
2. On 2026-09-08, STX's first target was below market and re-fired repeatedly.
   The resulting re-add was remediation for a bad target state, not ordinary
   portfolio selection. A successful target tier must never re-fire merely
   because a broker snapshot is delayed or a quantity changes.

The STX failure is a system-integrity problem; the DELL question is a judgment
problem. The design must solve both without making judgment mechanical.

## Invariants — hard system rules

These are safety/consistency requirements, not investment opinions:

- A successfully filled target tier is consumed for that **position lifecycle**
  and cannot fire again at the same level.
- A target below or equal to a current actionable mark is invalid for a live
  position and must not repeatedly place sell orders.
- A target fill, a level update, a broker-position refresh, and a same-symbol
  add must be serialized per symbol. The monitor must not be able to sell stale
  levels while a session is rebuilding exposure.
- Before a target re-entry, the system must use a fresh confirmed broker
  position and account for every same-day target fill in the proposed
  pre-/post-trade exposure.
- Stops, targets, eligibility, sizing, concentration, kill switches, and all
  existing order-gate rules remain in force. A target re-entry never bypasses a
  stop-out cooldown or a `rule_out`.

## Agent discretion — explicit re-underwriting

A same-day re-add after a target remains permitted, but only through a named
`target_reentry` path. It is a new decision against the current price, not a
continuation of the prior order.

Before the buy, the agent must:

1. Read the confirmed target fill(s), current broker quantity, and live quote.
2. Reassess the original target's purpose: why was reducing exposure right, and
   why is adding now also right?
3. Write valid replacement stop/target levels **before** the buy; no live target
   may be at or below the actionable price.
4. Compare the re-add with other scoreable opportunities. This does not force a
   different symbol; it prevents treating a previous holding as the default.
5. Record a `target_reentry` decision before the order, naming:
   - target fill time, price, and fraction;
   - current price and proposed pre-/post-trade exposure;
   - revised stop and targets;
   - the changed fact or reasoning that makes both the prior trim and the new
     add coherent.
6. After the fill, verify once that the current position is watched and the new
   levels are actually in force.

“It remains highly ranked” is useful evidence but not sufficient on its own.
The record must explain why the target was an appropriate reduction and why the
new facts make renewed exposure appropriate.

## Bounded repair exception

The system needs a separate, auditable recovery route for a monitor defect such
as STX—not a general escape hatch. It should be available only when objective
evidence shows an invalid/repeated target action, require correction of the
level before the add, and record the defect evidence. Normal target re-entries
must not be able to label themselves “repair” merely to avoid scrutiny.

## Relation to cohort candidate selection

The cohort migration is not the fix for target re-entry. It eventually broadens
the **scoreable** opportunity set, and the charter correctly states that the
agent may choose any scoreable name rather than only the top `candidates(n)`
output. It does not—and should not—force the agent away from a still-strong
symbol. The target-reentry path supplies the missing decision friction while
preserving that freedom.

## Charter changes required with implementation

Update `prompts/charter.md` and its generated wording in `src/charter.py` only
when the supporting path exists:

- Explain that `target1` is a precommitted **exposure reduction**, while a final
  target may close the trade; do not describe every target as “when the trade is
  finished.”
- Add the target-reentry re-underwriting sequence above.
- Preserve the existing stop-out cooldown language; target re-entry is a
  different concept, not a shortened stop cooldown.
- Remove the stale claim that no position has reached a take-profit target.
- Correct fixed-list wording that currently suggests an off-list buy is possible
  with an explanation; the live order gate refuses it.
- Do not claim the new re-entry requirements are order-gate enforcement until
  the code actually enforces them.

## Acceptance tests for a future implementation

- A normal `target1` fill may not produce a second fill from the same tier while
  its position lifecycle is unchanged, including across delayed/stale broker
  snapshots.
- A same-day ordinary buy after a target is refused unless it uses the explicit
  target-reentry path and supplies all required facts.
- The explicit path permits a well-formed re-underwrite that passes every
  existing gate; it does not impose a numeric or arbitrary time ban.
- A stop-out cooldown and an active `rule_out` still refuse a target-reentry.
- A defective below-market/repeated target can take the bounded repair path only
  after its levels are corrected and the defect evidence is recorded.
- Concurrent monitor/session activity cannot place an order using obsolete
  levels or an unconfirmed quantity.
- The rendered charter matches the actual order-gate behavior in both
  `fixed_list` and `cohort` modes.

## Non-goals

- Do not turn the strategy into a fixed-duration “no rebuy after target” rule.
- Do not use cohort membership or rank as an automatic override for a recent
  target action.
- Do not alter current live behavior as part of documenting this proposal.

