# 2026-08-19 — the scorecard measures a bias nothing acts on

Written mid-discussion with the principal. This is a DESIGN NOTE and an OPEN
QUESTION, not a completed change. Nothing in this note has been implemented.

## What prompted it

The agent's known failure mode — a systematic prior toward caution, articulate
justifications for doing less — is already named in `prompts/review.md`. The same
failure appeared in the assistant advising on the system: three objections to a
proposal, one of which (`deepeval` requiring PyTorch) was fabricated to support a
conclusion already reached. The principal's observation: it is one behaviour with
two surfaces, and in both cases inaction is never priced, so it is free.

## What was actually found (verified, 2026-08-19)

1. **`research_store/reviews/scorecard.json`: 86 decisions, 0 scored.** Both hit
   rates `null`. Head-to-head `0 decided`, `52 undecided`. The consequence layer
   has never returned a verdict.

2. **It is NOT broken — but that took a hand-trace to establish, and that is the
   defect.** The panel ends 2026-08-18; the oldest decision is 08-12; five trading
   sessions after 08-12 is 08-19. `forward_return()` returns `None` rather than a
   partial score, by design. First real scores land once 08-19's close is on file.

3. **`unscoreable` conflates three unrelated states** — too young, unpriceable
   (PORTFOLIO / symbol absent from the panel), and an action label the scorer does
   not recognise. All three emit the same blank. **A real defect is therefore
   indistinguishable from normal waiting**, for as long as the horizon lasts.

4. **The direction tally is computed over half the data.** 8 reduce / 21 increase
   / **32 "other"**. `reduce_share = 0.276` does not measure what its docstring
   says it measures. Of those 32, ~10 are genuine bookkeeping (`set_level`,
   `clear_level`, `reconcile`, `finding`) but **8 are directional and silently
   dropped: `skip` ×7, `skip_trim` ×1** — a decision NOT to act is exactly what
   `review.md` demands be judged by the same standard as a trade. The one metric
   built to catch standing caution is blind to the label the agent uses when it
   declines.

5. **The 5-session horizon is arbitrary.** It exists only as `default=5` in
   `score_reviews.py`'s argparse. `run_session.sh` passes no `--horizon`, so 5 is
   always what runs. It is not derived from `config/strategy.toml`, which declares
   `horizon = "swing"` ("multi-day to a few weeks") and `rebalance = "weekly"`.
   Grading a two-to-three week thesis at day five records "early" as "wrong".
   This also violates the standing rule that `config/strategy.toml` is the single
   source of truth and the strategy is tuned there, not in code.

6. **Doc drift:** `CLAUDE.md` gives the scorecard path as `reviews/scorecard.json`;
   it is `research_store/reviews/scorecard.json`.

## THE ACTUAL PROBLEM — no consumer

Fixing 1–6 produces better numbers in a JSON file that **no process reads**. The
principal's objection, and it is correct: *"if it just goes to me, so what — I'm
not the system."* A measurement a human must remember to open is the same class of
non-mechanism as a note-to-self. This repo already settled that argument in
writing: **"remembering is not a mechanism"** — the reason the order gate is a
PreToolUse hook and stale code gets a timer.

Current state of every path out of the scorecard:
- `scorecard.json` — written at the end of `run_session.sh`, opened by nothing.
- The `⚠️ 80% of decisions reduced exposure` warning — printed to `logs/session.log`.
- The Codex verdict — deliberately NOT shown to the agent
  (`session.py:SHOW_REVIEW_TO_AGENT`, off since 2026-08-13).
- The agent is **stateless**. It has never been shown its own track record and
  cannot know it has a habit.

## Proposed shape (NOT implemented — needs the principal's decision)

1. **Carry a live mark on every decision, always.** Return from decision date to
   the latest close on file, plus sessions elapsed, plus a *provisional* verdict
   labelled as such. The horizon governs when a verdict SETTLES; it must never
   govern whether the state is observable.
2. **Score at several marks (1 / 5 / 10 / 21 sessions), not one.** Removes the
   arbitrary number entirely, and distinguishes *right early, wrong later*
   (selling winners too soon) from *wrong early, right later* — which is the whole
   question for this failure mode and which no single horizon can show.
3. **Split `unscoreable` into `pending` / `unpriceable` / `unrecognised_action`.**
   A defect must never again be able to hide inside a wait.
4. **Count `skip` / `skip_trim` as directional.** Separate fix; not bundled.
5. **THE CONSUMER: the scorecard goes into `brief()`**, so the agent opens every
   session facing its own record. A fact to account for, NOT a rule — an automatic
   block on further trims would encode the play and is explicitly rejected.
6. **Second consumer: hand the same counts to Codex**, so `review.md`'s instruction
   to watch for one-directional caution becomes a number it can cite rather than a
   prior it has been asked to hold. Its dissent is then scored against the tape,
   which feeds the scorecard, which returns to both — a loop with a closing edge.

## Open question the principal must settle

(5) partially reverses the 2026-08-13 decision to withhold the review from the
agent. That decision's reasoning — an unexplained critique is ambiguity the agent
resolves by inventing a reason for it — applies here too. The distinction argued
for: a verdict is another model's opinion; a scorecard is what the market did.
**If it is fed back, the charter must name the process first.**
