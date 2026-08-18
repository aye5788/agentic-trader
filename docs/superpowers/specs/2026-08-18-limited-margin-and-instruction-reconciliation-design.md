# Limited margin, and the instruction surfaces that no longer describe the system

**Date:** 2026-08-18
**Status:** design, approved in conversation; not yet implemented
**Trigger:** Aaron switched the Agentic account from cash to **limited margin**.

---

## 1. What actually changed at the broker

Verified live rather than assumed, via `get_accounts` and `get_portfolio` on
2026-08-18 (read-only):

| Field | 2026-08-17 (cash) | 2026-08-18 (limited margin) |
| --- | --- | --- |
| account `type` | `cash` | **`limited_margin`** |
| `cash` | 25.13 | 25.13 |
| `buying_power` | **2.36** | **25.13** |
| `unleveraged_buying_power` | — | 25.13 |
| `unsettled_funds` | 7.06 (08-10 reference) | **0** |
| `option_level` | — | `""` |

Three facts follow, and everything in this spec derives from them:

1. **T+1 settlement no longer constrains deployment.** Sale proceeds are usable
   the same session. The ~$22.80 from the 08-17 ETF liquidation, stranded
   unsettled yesterday, is spendable today.
2. **There is no leverage.** `buying_power == unleveraged_buying_power == cash`.
   The account cannot borrow, cannot short, and `option_level: ""` means the
   options surface is structurally absent, not merely disallowed.
3. **`buying_power` is still the field to size against.** It has not stopped
   being authoritative — it has stopped differing from `cash`.

## 2. Scope decisions (Aaron, 2026-08-18)

- **Truth-only.** Correct what the system asserts. Do **not** add a leverage
  tripwire reading `unleveraged_buying_power`, and do **not** add a pacing or
  churn norm to the charter now that settlement no longer rate-limits same-day
  sell-and-rebuy. Consistent with the standing preference against layering
  static governance: state the world accurately and let judgment operate.
- **Widen the sweep.** Every instruction surface is checked for *all* stale
  references, not only settlement ones.
- **The 12:00 session is removed** and every session reference reconciled to
  what actually runs.
- **`record_fills._EXPECTED_SKIP` is narrowed** — the one behavior change.

**Explicit non-goals:** no leverage guard; no churn constraint; no change to
`max_total_weight`, the order gate, the mandate, or any threshold; no
enforcement of `≤100% invested` (it was never enforced, and this spec does not
change that fact — it only stops the docs implying settlement enforced it).

---

## 3. Part A — the limited-margin correction

### 3a. Agent-facing text that is now false

These are read by the live agent every session. This is the material class.

| Site | What it says | Correction |
| --- | --- | --- |
| `src/charter.py:91` (`render_terms`) | "**Settlement** — T+1 on closes. Size buys against `buying_power`, never `cash`; the difference is real and often most of the balance." | State the account as limited margin: proceeds are available the same session, `buying_power` remains the figure to size against, and it now equals `cash`. Drop the "most of the balance" claim. |
| `prompts/charter.md:70` | "**Margin or leverage.** Cash account. You spend settled cash and nothing more." | Keep the prohibition — it is still true — and fix the premise. Limited margin, no borrowing, no leverage, no shorting; the account's spendable figure is its own cash. |
| `prompts/charter.md:420-424` | "This is a cash account… sell and rebuy the same day and the proceeds do not return until tomorrow." | Replace with the limited-margin reality: proceeds are usable immediately, so a same-day rotation is now mechanically possible. State it as a capability, **not** as encouragement and not paired with a new restriction (per §2). |
| `src/agent_env/server.py:280-286` (`account()`) | Both `spendable` branches assert "this is a CASH account… unsettled until T+1". | Rewrite both branches. The `bp is None` branch keeps its real advice (call `get_portfolio` for the authoritative figure). |
| `src/agent_env/server.py:1275-1276` (`check_order()`) | "Unsettled proceeds become available T+1." | The over-budget note keeps its first half (Robinhood will reject) and loses the T+1 sentence. |
| `src/agent_env/server.py:1077` (`announce()`) | "settlement/buying-power deferrals self-heal and are deliberately silent" | Narrow to buying-power deferrals; settlement deferrals are no longer an expected class. Must stay consistent with §3c. |

### 3b. Comments and docs carrying the dead premise

Non-agent-facing, but wrong and load-bearing for the next reader:
`src/marks.py:148-156`; `src/agent_env/server.py:1256-1258` and `:1803-1805`;
`src/mandate.py:161` and `:583`; `scripts/letter_facts.py:194`;
`src/ledger.py:157` (fixture string `pending_settlement`);
`docs/DESIGN.md:46-48`; `docs/DEPLOY.md:308-309`.

`src/mandate.py`'s negative-value guard **stays exactly as it is**. Its
conclusion is still correct — limited margin cannot short, so a negative
position value still means corrupt data — only the phrase "long-only cash
account" needs to become "long-only, no shorting, no leverage".

`scripts/sim_recycle.py` models a T+1 redeploy queue. It is a simulator with an
explicit `same_day` flag and is **left alone**; a note records that live
behaviour now matches its `same_day` branch.

### 3c. The one behavior change

`scripts/record_fills.py:86-89`:

```python
_EXPECTED_SKIP = ("settle", "buying_power", "insufficient", "pending")
```

Skips matching these are suppressed from the ntfy push and the weekly letter as
routine self-healing. That was right when settlement lag was designed behaviour.
It is now wrong: a `pending_settlement` skip would be an **anomaly**, and
suppressing it hides the first evidence that something about the account changed.

**Change:** narrow to `("buying_power", "insufficient")`. An underfunded order
is still ordinary and stays quiet. Update the comment above it to say why the
two were removed, and update the selftest at `:116`, which asserts an
`"unsettled cash"` reason is suppressed — that assertion must invert to prove it
now surfaces.

### 3d. Memory

`zero-cash-settlement-lag` ("T+1 buy deferral on rotations is designed, not an
error") is now false and will be deleted, replaced by a memory recording the
limited-margin switch and the `unleveraged_buying_power` proof.

---

## 4. Part B — stale references unrelated to margin

### 4a. Safety-relevant: an operator instruction that silently does nothing

`docs/OPERATOR_MANUAL.md:60-62` documents how to stop just the sessions:

```
crontab -l | grep -v run_session.sh | crontab -
```

The live crontab contains **zero** `run_session.sh` entries — the sessions have
been systemd timers since commit `b9719ab`. The command exits 0, changes
nothing, and leaves both trading sessions armed while the operator believes they
are stopped. This is the most serious finding in the sweep because it fails in
the direction of trading when a human intended to stop.

**Correction:** `systemctl disable --now agentic-session@open.timer
agentic-session@close.timer`, with `systemctl list-timers 'agentic-*'` as the
verification step, since a command that appears to succeed is exactly what
failed here.

### 4b. The charter describes a session that has never existed

`prompts/charter.md:79` states "Three run each weekday"; lines 199-246 are a
full `### 12:00 — WHAT CHANGED?` section. Only `agentic-session@open` (10:35)
and `agentic-session@close` (15:15) exist, and git history shows no midday unit
was ever created. An agent at 10:35 can defer a decision to a session that will
never run.

**Correction:** the charter states two sessions, at their real times.

- `### 10:00 — THE BOOK` → `### 10:35 — THE BOOK`
- `### 12:00 — WHAT CHANGED?` → **removed as a session**
- `### 15:45 — IS EVERYTHING STILL TRUE?` → `### 15:15 — IS EVERYTHING STILL TRUE?`
- `:79` "Three run each weekday" → two
- `:131` "The open has settled half an hour" → an hour at 10:35
- `:247-249` "fifteen minutes before you lose the ability to manage a position"
  → forty-five minutes at 15:15. The *argument* is unchanged and still correct:
  it is not when a new position gets opened.

⛔ **The 12:00 section's content must MOVE, not be deleted.** It carries the
trim discipline — "Trimming is a first-class action, not a half-measure", the
no-double-trim rule, the disposition-effect guard ("a trim is about EXPOSURE,
not profit… it applies equally to a loser"), and the `positions()` giveback
fields (`peak_pct`, `giveback_pct`, `gain_protected_pct`). This is the behaviour
with the best measured record in the system, and `src/charter.py`'s selftest
asserts each clause specifically to prevent its loss. It folds into the 15:15
session, which already has authority to reduce, close or hold. The 15:15
section's existing prohibition on *opening* is unaffected — trimming is not
opening.

`src/charter.py:343-350` currently asserts the three literal headings and the
"10:00 session reads these" handoff. Those assertions must be updated to the two
real sessions, and they are the mechanism that proves the trim content survived
the move.

### 4c. The charter justifies a live obligation with a deleted subsystem

`prompts/charter.md:272`: the `rule_out()` requirement is explained by "The
10:00 fast loop is deterministic: it rebuilds the book from the stored targets
and knows nothing about why you sold."

The fast loop was deleted 2026-08-14. **The obligation is still real** — the
order gate reads `binding_rule_outs()` and refuses a buy
(`pretooluse_order_gate.py:276-292`) — but its stated reason no longer exists,
so an agent that knows the loop is gone may conclude the requirement lapsed.

**Correction:** restate the reason as what is true now — a rule-out is how a
session's exit reasoning reaches the order gate and tomorrow's session, both of
which are otherwise blind to it. The AMAT history stays as the illustration, and
keeps its `<!-- historical -->` marker.

### 4d. The gate's refusal list is presented as complete and is not

`src/charter.py:101-118` (`render_gate`) introduces its list with "It refuses:"
and omits two real refusals: the **automatic drawdown halt**
(`pretooluse_order_gate.py:245-258`) and an **active `rule_out`** (`:276-292`).
`docs/OPERATOR_MANUAL.md`'s "It is still fenced" paragraph has the same gap.

**Correction:** add both to `render_gate`, with the drawdown limit interpolated
from `[governance] max_drawdown` rather than written as a literal, and note that
both are entry-only — neither ever blocks a sell. Add them to the manual too.

### 4e. `docs/STRATEGY.md` — the file that says "Read this before trading"

Three sections describe a system that no longer exists:

- **§2 and §4** specify top-10 names, a top-4 ETF sleeve and a 70/30 capital
  split. Config: `book_hold = 14`, `book_band = 20`, `sleeve_hold = 0`,
  `book_weight = 1.0`, `[etf_sleeve] enabled = false`. The sleeve was retired
  2026-08-16 and liquidated 2026-08-17.
- **§6** states the chase guard is "Enforced in `fast_loop.apply_chase_guard`" —
  deleted. §7 already carries the correct account (guidance, not a gate); §6
  must stop contradicting it.
- **§8** is titled "Execution — the fast-loop procedure" and lists its steps.
  Replace with how execution actually happens: a session decides and places
  through the order gate.

The ETF *scoring* stays — ETFs remain rankable and `config/etf_universe.csv`
stays for the residual-momentum sector regression and the whitelist. Only the
*sleeve as an allocation* is gone.

### 4f. Cosmetic, no agent impact

References to deleted code, to be corrected where touched:
`prompts/exit.md:116` ("fast loop's step 8"); `src/agent_env/server.py:104`;
`src/agent_env/memory.py:25,31,85,141`;
`scripts/hooks/pretooluse_order_gate.py:276`; `src/agent_env/decide.py:451` and
`src/agent_env/state.py:31` (both cite `scripts/risk_review.py`, which is not on
disk); `CLAUDE.md:178-181` and `:420` (ETF sleeve as a live parallel engine).

---

## 5. Verification

No new tests are invented; the repo's existing mechanisms already cover this,
which is the point of routing the changes through them.

1. `.venv/bin/python src/charter.py` — selftest. Updated assertions prove the
   two real sessions are named and that every trim clause survived the move.
2. `.venv/bin/python src/repo_checks.py` — must stay `PASS (10 checks, repo
   clean)`. `check_charter_no_literals` is unaffected: it matches thresholds,
   not times ("dates, step numbers and ordinary numerals are deliberately not
   matched").
3. `.venv/bin/python scripts/record_fills.py --selftest` — the inverted
   assertion proves a settlement skip now surfaces.
4. `.venv/bin/python src/mandate.py`, `src/agent_env/server.py` selftests — prove
   the reworded comments did not disturb the guards they describe.
5. **Render the charter and read it end to end** as the agent receives it. A
   selftest proves clauses exist; only reading proves the document is coherent
   after a section moved.
6. `.venv/bin/python scripts/reload_stale.py` — `src/marks.py`, `src/mandate.py`
   and `src/agent_env/*` reach the monitor and the dashboard. Editing them is not
   done until the services reload.
7. Manually confirm the corrected stop-the-sessions command disables both timers
   and that `systemctl list-timers 'agentic-*'` shows them gone — then re-enable.
   ⚠️ While the timers are off, `repo_checks` emits four "timer NOT ENABLED"
   findings that **mask all other drift**; always re-run it after re-arming.

## 6. Risks

- **The trim discipline is the thing most likely to be lost**, because it is
  being moved rather than edited. The selftest assertions are the guard; they
  must be updated to the new location before the section is moved, not after.
- **Rewriting the charter risks changing its meaning while fixing its facts.**
  Every edit here is a fact correction. The only intentional behavioural
  statement removed is the settlement rate-limit, and per §2 nothing replaces it.
- **The 15:15 session absorbing trim authority** must not be read as authority to
  open. Its opening prohibition is explicit and stays.

## 7. Out of scope, worth raising later

- The session times are literals in the charter, the operator manual, `DEPLOY.md`
  and the timer units — four places that can drift apart exactly as they just
  did. A single source (rendering the times from the timer units) is the durable
  fix and is a separate change.
- The 08-17 newsletter-accuracy follow-ups (`docs/superpowers/specs/2026-08-17-newsletter-accuracy-followups.md`)
  include "reported 41% cash as a choice when only 11% was spendable". Under
  limited margin that gap closes for future letters, but the 08-23 letter covers
  a week that was partly a cash account. That spec should be re-read against this
  one before Sunday.
