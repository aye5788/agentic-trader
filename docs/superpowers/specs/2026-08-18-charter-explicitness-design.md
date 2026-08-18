# The charter assumes comprehension it never supplied

**Date:** 2026-08-18
**Status:** design, approved in conversation; not yet implemented
**Trigger:** the 2026-08-18 open session, and an audit of the rendered charter by
Claude (me) and by an independent Codex run given the same document.

---

## 1. What happened

The 10:35 session on 2026-08-18 bought XOM, RTX and BAC — ranks 34–38 — with
$18 of $25.13, explicitly recording the reason as **correlation**:

> "DEVIATION FROM THE HOUSE VIEW, stated: these rank 34-38, not top-14. Reason
> is correlation, which the momentum screen does not measure."

Aaron's objection: momentum does not care about correlation. The book is a
days-to-weeks swing book, and the session was reasoning like a long-horizon
diversified portfolio manager.

**Measured, not inferred.** Across 1,388 words of that session's recorded
decision reasoning:

| concept | mentions |
| --- | --- |
| concentration / correlation / theme / sector | **17** |
| momentum / rank | 7 |
| trend, swing, days-to-weeks, weekly rebalance, multi-day | **0** |

It never once reasoned from the holding period. The two horizon-shaped mentions
("horizon", "hold window") were both about earnings dates.

The independent Codex reviewer of that session, handed the *same* charter,
also never cited the strategy or the horizon; its entire objection was stop
enforcement. **Two models, same document, same blind spot** — which makes this
a property of the instructions, not a lapse by one session.

## 2. The diagnosis

The rendered charter is **5,994 words**, split:

| | words | share |
| --- | --- | --- |
| mechanics / vigilance | 3,969 | 66% |
| trading judgment | 2,025 | 34% |

and the "trading judgment" third is mostly `THE JOB`'s 563 words on *not sitting
in cash* — activity level, not strategy. The document tells the agent to act,
tells it exhaustively how not to break the machinery, and tells it almost
nothing about what it is trading. Term counts in the rendered document:

```
enforced 7 · snapshot 6 · unprotected 3      momentum 2 · rank 3 · trend 1 · swing 0
```

So the agent filled the vacuum with the strongest available prior — generic
portfolio management — and reached for correlation on a five-day trade. It was
not overriding the strategy. It was working without one.

**Aaron's rule, which governs this whole spec:**

> "you LLMs literally need things explicitly spelled out as you are really
> unreliable in properly contextualizing things — we need to DEFINE swing
> trading not assume it 'knows' what that means... it does not forbid day
> trades (exceptions to the rule need to be explicit!)"

Two consequences:

1. **Define the concept, do not name it.** Naming an idea and expecting the
   model to unpack it does not work.
2. **State the exceptions.** A boundary with no stated exception becomes an
   invented prohibition. "Your horizon is days to weeks" silently becomes "I
   must not close same-day" — a rule nobody wrote.

## 3. Scope

**In scope:** definitions, stated exceptions, corrected factual errors, and
converting dispositions into *conditions* (the specific circumstances that
distinguish acting from not acting).

⛔ **Explicitly OUT of scope: new rules, thresholds, limits or guardrails.**
The Codex audit asks for several — a test defining an "acceptable" stop, a rule
choosing one-third versus one-half on a trim, prescribed responses to mandate
breaches. Those convert judgment into thresholds and are refused. The line this
spec holds: **define the concepts and state the exceptions; leave the play to
the agent.** See `[[agent-runs-the-system]]`, `[[no-layering-static-governance]]`.

Nothing here changes `config/strategy.toml`, `config/mandate.toml`, the order
gate, or any limit.

---

## 4. Class 1 — FACTUAL ERRORS (do these first; safety-relevant)

Found by Codex, verified against the code. `render_gate` in `src/charter.py`
tells the agent things that are not true.

| Charter claim | Reality |
| --- | --- |
| "A SELL is refused by nothing but the kill switch." | **False.** Shadow mode refuses sells: the gate's own text reads *"every order is refused, including sells"* (`pretooluse_order_gate.py:200-205`). |
| "any order whose notional exceeds **15% of equity**" | **False for sells.** `governance.py:461`: *"Only BUYS are capped by max_order_pct — capping a sell would strand a position the system is trying to exit."* |
| "any order in an account other than the one Agentic account" | The PreToolUse hook performs **no account check**. Whatever protection exists is at the broker (`agentic_allowed`), not at the gate. |

The dangerous one is the first: an agent in shadow mode believing it can always
exit. Note this section was edited earlier on 2026-08-18 to add the drawdown
halt and rule-out limbs, and these surrounding errors were not noticed then.

**Replacement (`render_gate`)** — refusals split by side, so neither claim can
be read across the other:

> Before any order reaches the broker it passes a gate that runs in the harness,
> not in your judgment.
>
> **Refused whatever the side, buy or sell:**
> - any order while the **kill switch** is set — exits must then be placed by hand
> - **every** order while shadow mode is set, sells included
>
> **Refused for a BUY only** (an exit is never blocked by these, because the stop
> here is software and blocking a sell removes a position's only protection):
> - a buy while entries are halted
> - a buy for a symbol outside the configured universe
> - a buy while the book is more than {max_drawdown} below its tracked equity peak
> - a buy in a name carrying an active `rule_out`, until `revisit()` clears it
> - a buy whose notional exceeds {max_order_pct} of equity
>
> The account you may trade in is enforced at the broker, not by this gate.

## 5. Class 2 — DEFINE (named, never defined)

### 5a. The trade itself — the missing section

Insert as its own section. This is the spec's centrepiece.

> **HOW YOU TRADE THIS BOOK**
>
> A swing trade here means, concretely:
> - **Holding period: typically 3–15 trading sessions.** Positions usually live
>   through several sessions. This is an *expectation, not a minimum* — nothing
>   requires you to hold a position for any length of time.
>
> ⚠️ **AARON MUST SET THIS NUMBER.** "3–15 trading sessions" is my invention. It
> is consistent with "days to weeks" and a weekly rotation, but it is a strategy
> parameter and it appears in no config file and no doc. Writing a number the
> agent will treat as authoritative, derived from nothing, is precisely the
> defect this spec exists to remove. Either Aaron states the range, or it is
> derived from the ledger (measured holding period of closed round trips) and
> stamped with that provenance.
> - **What opens one:** a name high in the cross-sectional momentum rank that
>   also passes the absolute filter, entered with a stop and a target set from
>   that name's own measured behaviour.
> - **What closes one:** the stop, the target, a break of the trade's own
>   structure, or the thesis you wrote no longer being true.
>
> **Exceptions — stated explicitly so you do not infer a rule nobody made:**
> - **You MAY close a position the same day you opened it.** If the reason you
>   opened it is gone by 15:15, close it. Since 2026-08-18 proceeds settle the
>   same session, so nothing mechanical prevents this; a same-day exit costs you
>   the spread and nothing else. "Days to weeks" is not a commitment to hold.
> - **You MAY hold beyond weeks** while the trend persists and the name stays
>   ranked.

### 5b. The signal

Rendered from config, not hand-written. The agent is currently told the words
"cross-sectional momentum" and nothing else:

> Each name is scored by percentile-ranking two views across the eligible
> universe and averaging them: risk-adjusted 12-month return, and trend
> (close ÷ its 200-day mean). A name is **eligible only if its 12-month return
> is positive** — that is the absolute filter, and it is what takes this book to
> cash when a trend breaks, since it has no short leg. The book holds the top
> {book_hold}. A name you already hold is retained until it falls below rank
> {book_band} — that band exists so a name dipping one place does not churn out
> and back in.

### 5c. Weekly rebalance versus daily authority

Codex #2, #4, #19. "Rebalanced weekly" and "full authority to rotate" are never
reconciled; a 10:35 session can read itself as a rebalance, or conversely read
"weekly" as forbidding intraweek exits.

> The full re-rank and rotation happens **once a week, on Sunday**. Weekday
> sessions are not rebalances: they manage the book you already hold. That does
> **not** restrict exits — a stop, a target, a trim, a thesis that broke, or a
> same-day close are all available to you on any session. What is weekly is the
> rank-driven rotation, not risk.

### 5d. What a thesis is

Used five times, never defined, and "are the theses intact?" is a core 15:15
question.

> A **thesis** is the reason you opened a position, written so a later session
> can test it: what you observed, and the specific thing that would prove it
> wrong. "Momentum is strong" is not a thesis. "Rank 3, holding above its
> 20-day mean at 156.25, and I am wrong if it closes below that" is one.
> "Intact" means the thing you named as falsifying it has not happened.

### 5e. Smaller definitions

Each currently relies on the reader already knowing: "the window" (earnings
proximity), "the short-term mean", "a full complement" (= `book_hold` names),
"at any mark" (which price the 15% concentration test uses), and what the
monitor actually does when a **target** is reached — automatic sale, wake, or
advisory. That last one matters: the agent may assume a target is enforced the
way a stop is.

## 6. Class 3 — STATE THE EXCEPTION

Each is a boundary the agent can read as a prohibition nobody wrote.

| Boundary | Exception to state |
| --- | --- |
| "Your horizon is days to weeks / you are not scalping" | same-day close permitted (§5a) |
| "Do not trim the same name twice in one day" | *"This forbids a second partial trim only. It does not forbid selling the remainder — if the thesis breaks after you trimmed, close it."* |
| "Do not treat your entry price as information" | *"This is about anchoring: entry price is not a reason to keep an exposure. It remains the correct input for stop distance, protected gain, position size and realised P&L, and several of your tools compute from it."* |
| "`sectors()` for concentration the position count hides" | *"There is no sector or theme limit in the mandate; `sectors()` is information for judgment, not an eligibility rule."* |
| "deploying ALL of it in one session is not [the default]" | *"…except when executing the scheduled weekly rotation, where filling the target book in one session is the intended behaviour."* |

## 7. Class 4 — CONDITIONS, not dispositions

### 7a. Theme concentration (the one that caused this)

Aaron's correction to an earlier draft of mine: stating flatly that
"concentration is the expected output" is *also* too blunt — the agent would
then ignore concentration entirely. It needs the specific conditions.

> **Theme concentration: when it is the strategy working, and when it is a real
> problem.**
>
> The screen selecting six names from one theme is **not** in itself a reason to
> act. Ranking by relative strength repeatedly picks from whatever is leading;
> that is the mechanism, and the backtest behind this book carries those
> episodes in its returns.
>
> **These are NOT reasons to reduce a theme:**
> - The theme fell hard today, even 5–8%. Momentum names fall together
>   routinely; a one-day drawdown is noise, and it is the single most common
>   trigger for a bad de-risking decision.
> - The theme is a large share of equity. There is no mandate limit on theme
>   exposure — the only cap is 15% per single position.
> - The names are correlated. Correlation is not measured by the signal and is
>   not a selection input.
>
> **These ARE reasons, and each names a specific mechanism:**
> - **Clustered stop risk** — several positions now sit close enough to their
>   stops that one event would fire them together.
> - **A shared binary inside the hold window** — one print, ruling or release
>   that resolves the whole theme at once, which you cannot stop out of.
> - **The momentum itself degrading across the theme** — not one bad day, but
>   the absolute filter weakening across its names at once. That is the edge
>   leaving, and it is the condition the strategy actually cares about.
> - **A single position through the 15% cap** — the one hard limit.
>
> **If one of those holds, the only instrument that reduces theme exposure is
> selling or trimming those names.** Buying a lower-ranked name alongside them
> does not reduce concentration measured against equity — cash converting to
> equity leaves the denominator unchanged — and it allocates capital at lower
> expected return by the strategy's own signal.

The final paragraph encodes the arithmetic the 2026-08-18 session discovered and
corrected on the record after the fact.

### 7b. The deviation clause

Currently: *"You may deviate on one name or abandon it wholesale… the only
thing it costs you is a recorded reason."* Permission with no criteria — the
sentence that licensed the off-rank buys.

> Deviating is permitted and does not need approval. What makes a deviation
> sound is **evidence that the signal is failing for these names**, not a
> preference about how a book should look. Deviating on **risk** grounds — one
> of the conditions above — is well-founded. Deviating on **selection** grounds
> — buying names the screen ranks lower because you prefer their mix — allocates
> capital against the only policy here with measured evidence behind it, and the
> burden of proof is correspondingly higher. Say which of the two you are doing.

### 7c. Chasing, and re-entry after a stop-out

Both were code guards until `fast_loop.py` was deleted on 2026-08-14, both were
reclassified as "the session's judgement", and **neither concept appears in the
charter at all.** The judgment-holder was never told the judgment exists. Stated
as prompts to judge, not as thresholds:

> **Before a buy, compare the live price with the thesis entry zone.** A name
> that has run far above the level its thesis was written at is a different
> trade from the one that was planned. Nothing blocks you; decide deliberately
> and say what you decided.
>
> **Before rebuying a name that recently stopped out, look at why it stopped.**
> An automatic cooldown used to block this and no longer exists. A name can
> stop out and remain top-ranked — that is exactly when the question is live.

## 8. Class 5 — LEAVE ALONE (deliberately judgment)

Recorded so a later reader does not "fix" them, and so the Codex findings are
not adopted wholesale:

- **stop and target placement** (Codex #12) — the charter deliberately says
  "against measured behaviour, not a formula". A test defining an acceptable
  stop is the formula it removed.
- **trim size** (#17) — one-third versus one-half is the judgment.
- **mandate-breach response** (#23) — the mandate is falsifiable criteria, not a
  playbook; what to do about a FAIL is the agent's.
- **environmental materiality** (#26) and **liquidity response** (#28) — same.

Codex could not distinguish "undefined" from "deliberately left open", because
nothing told it which was which. That filter is applied here, not by it.

---

## 9. Where the changes land

`prompts/charter.md` is a template with placeholders; numbers must come from
config via `src/charter.py`. Any literal threshold in the template is a defect
that `repo_checks.check_charter_no_literals` fails on — so §5b's `book_hold`
and `book_band`, and §4's limits, are interpolated, not typed.

`src/charter.py`'s selftest asserts specific clauses survive edits. New
assertions are needed for: the same-day-close exception, the "not a minimum"
wording, the concentration NOT-reasons list, and the split-by-side gate text.

## 10. Verification

1. `.venv/bin/python src/charter.py` — selftest, with the new assertions.
2. `.venv/bin/python src/repo_checks.py` — must stay `PASS (10 checks)`;
   `check_charter_no_literals` proves no threshold was hard-typed.
3. **Render and read it end to end.** A selftest proves clauses exist; only
   reading proves the document still coheres after inserting a section.
4. Re-measure the split. The mechanics share should fall meaningfully below 66%
   — not by padding the trading half, but because §4's corrections and the
   compression of prose that duplicates code-enforced guarantees shorten it.
5. `.venv/bin/python scripts/reload_stale.py`.

## 11. Risk

The charter is the agent's entire operating context and it trades real money on
it twice a day. The main risk is **volume**: this spec adds definitional text to
a document already too long, and length is itself part of the defect. Every
addition must be paid for by removing mechanics prose that guards something the
code now enforces unbypassably — the order gate and the snapshot publisher both
refuse rather than rely on the agent remembering.

A second risk is **over-specification**: the same instinct that produced the
Codex findings in §8 will be present when writing §5–§7. The test for every
sentence added: *does this define something, state an exception, or correct an
error?* If it instead tells the agent what to decide, it does not go in.
