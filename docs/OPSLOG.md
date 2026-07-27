# OPSLOG — operations & maintainer notes

Dated log of operational events and technical flags, newest first. This is the
"separate entry elsewhere": the weekly investor letter (The Claude Ledger)
narrates the portfolio only — plumbing, broker blocks, code flags, and
settlement mechanics land HERE (written by the newsletter run from the week's
journal `notes`, or by hand). One `##` heading per entry.

---

## 2026-07-27 — Health check cried wolf every Monday; newsletter couldn't write this log

Two defects found during a routine "is everything running?" audit. Everything
*was* running — Friday 07-24 was the last trading session and all nine checks
were green through Sunday. Both findings are in the oversight plumbing itself.

**1. The intraday-monitor liveness check false-fired every Monday — and dragged
the auto-fix agent in with it.** `SPECS["monitor"]` allowed 2 days, but its
artifact (`research_store/monitor/state.json`) only advances during RTH. Friday's
close → Monday's 08:00 health run is ~2.7d of legitimate dead air (3.7d when
Monday is a market holiday). Every other weekday-only job already used 3–4d;
the monitor was the only one whose window was shorter than a weekend.

This was not theoretical. At 08:00 today it fired for real — the first Monday
since the health cron was armed on 07-24. It pushed to the OPS topic, and
because cron runs `health_check.py --open-issue`, it filed GitHub issue **#2**
(`bug`+`auto-fix`) and triggered two `claude.yml` runs to fix a bug that did not
exist. Confirmed against `research_store/health_state.json`, which shows
`monitor` flagged at `2026-07-27T12:00:03Z` with `"filed": true`.

Fix: threshold 2d → 4d, matching the other weekday-only jobs. The selftest now
encodes the *requirement* rather than the constant — a Friday-close → Monday-08:00
gap and a long-weekend (Tuesday) gap must both stay quiet, while a genuinely dead
monitor must still alarm. The old test asserted the 2d boundary and so passed
happily while the bug shipped.

**Accepted cost:** a monitor that dies mid-week is now caught up to ~4d later
instead of ~2d. That is the same latency every other weekday job already carries.
If that is too slow for the component that watches stops, the better fix is a
weekday-aware age (count only Mon–Fri) rather than a bigger constant — deferred,
not done.

**2. The newsletter run could not write to this file.** Step 3b of
`prompts/newsletter.md` prepends a dated entry to `docs/OPSLOG.md` — an `Edit`.
`.claude/settings.json` allowed `Read` and `Write` but never `Edit`, so the
headless Sunday run was denied and reported the step blocked. Newest entry
before today was 07-24, confirming nothing landed.

Nothing was actually lost this week: the entry it wanted to write described the
RH `second_trade` investor-profile block, already logged under 2026-07-08, and
the journal shows no recurrence (every `risk_review` 07-20→07-24 carries
`rejected: []`). But any genuinely new ops entry would have failed the same
silent way. Fix: added a scoped `Edit(docs/OPSLOG.md)` allow rule.

Issue #2 closed as a false positive; the runs it spawned completed without
opening a PR (the agent correctly refused to touch protected files).

**3. The investor letter republished stale events every week.**
`scripts/letter_facts.py` selected "this week's" journal events with:

```python
(e.get("ts") or "9999") >= since or e.get("as_of", "") >= since[:10]
```

Two clocks OR-ed together, and a missing `ts` substituted as `"9999"` — which
is `>=` every real timestamp. The two 2026-07-08 first-deployment `execution`
events carry `as_of` but no `ts`, so they passed the window test **forever** and
were republished in every subsequent issue. Measured on the real journal at
issue_004's window: 5 execution events / 33 fill legs selected, where the truth
was 3 / 18 — the 15 extra legs were all from 07-08.

Fix: `_in_window(event, since)`, a pure predicate that times an event by `ts`
when present and *only otherwise* by `as_of` — never both. Undatable events are
now excluded rather than waved through. `letter_facts.py --selftest` covers the
ts-less-and-old, ts-less-and-recent, and neither-clock cases; the bug shipped
because nothing tested this predicate at all.

**Blast radius — the trade table was fine, the prose was not.** The letter agent
defensively rendered the table only from entries carrying `status: "filled"`, so
issue_004's fills table (15 rows) is correct. But the narrative absorbed the
stale batch and told Aaron:

> "One planned batch of orders was mostly held back by a broker profile check —
> 13 of 14 never placed — and the details are in the ops log."

That is the 07-08 event reported as the week of 07-20. Nothing of the sort
happened that week — every `risk_review` 07-20→07-24 carries `rejected: []`. And
because defect #2 blocked the OPSLOG write, the "details are in the ops log"
pointer was broken too. Issues 002 and 003 were generated after 07-08 and are
likely to carry the same contamination; not audited.

Letters already sent cannot be recalled — recorded here so the record is
straight. Issue 005 is clean.

**Still open for Aaron:** an agent cannot grant itself Bash permissions (the
classifier blocks it, correctly), so `gh run cancel` still needs a rule added by
hand if you want a runaway oversight run stoppable from a session.

## 2026-07-24 — moomoo: OpenD-down HANGS forever; every "OpenD is down" handler was dead

Found while testing the signal panel's failure branch at Aaron's request (he wanted
it exercised before the weekend rather than discovered live). It is the most
serious thing found today.

**`OpenQuoteContext(host, port)` against an unreachable OpenD never returns and
never raises.** The SDK retries on a background thread with no overall deadline.
Verified by killing test runs at 40s and 180s with the log frozen at "constructing".

**Consequence: every `try/except` we wrote for "OpenD is down" was unreachable.**
`collect_signals.py` is the worst case — its except-branch is precisely what sets
`opend_ok=False` and fires the "signal panel gap" phone alert. So with OpenD down,
the Sunday 20:15 run would have hung at the first call, **never alerted**, and left
a stuck process behind every week on a ~2 GB box that already swaps. The documented
alert in OPERATOR_MANUAL §3 could not have fired. `universe_refresh` (quarterly,
same `quote_ctx`) had the identical exposure.

Note this was invisible to the obvious test: a `--dry` run with OpenD *up* passes
happily, and the failure only appears when the gateway is actually gone.

**Fix — TCP preflight in `src/adapters/moomoo/client.py`.** `quote_ctx()` now does a
5s `socket.create_connection` first and raises `OpenDUnavailable` if nothing is
listening, converting an unbounded hang into an immediate, catchable exception. It
is in the shared client deliberately, so every caller (collector + universe refresh
+ `research.py`) is covered at once.

**Verified:** dead port raises in **0.01s** (was: infinite) with a diagnostic naming
opend.service and the vol-desk sharing; the real gateway still connects in 0.02s;
the full collector against a dead port now exits 0 with `opend_ok=False`, one
descriptive gap, and **a real phone push delivered**; the healthy path is unchanged
(14 names, 0 gaps, opend_ok True). The gap test stubbed `append_journal` so no
fabricated "OpenD was down" row entered the research dataset.

**Residual CLOSED same day — the wedge case.** A gateway that is *listening but
wedged* passes preflight and can stall inside an SDK call. `collect_signals.py` now
bounds the whole pull with a SIGALRM deadline (`COLLECT_TIMEOUT=300`, `--timeout` to
override) plus a short separate bound on `ctx.close()` — the close talks to the same
wedged gateway, so without its own bound it would undo the timeout we just escaped.
`_Timeout` subclasses `Exception` on purpose, so it lands in the existing handler and
becomes opend_ok=False + a push rather than a traceback. The cron line gained an
outer `timeout 900` for the case where a C-level call masks the signal.

Verified against a simulated wedge (socket connects, then the call sleeps 600s):
returned in **8.2s** with an 8s deadline, `opend_ok=False`, gap
`_Timeout: moomoo collection exceeded 8s — OpenD wedged?`, and a real push
delivered. Healthy path unchanged and takes **2.3s**, so the 300s bound has ~130x
headroom. Both moomoo failure modes are now bounded and audible.

---

## 2026-07-24 — Upkeep reminders: a second ntfy topic + scheduled-job liveness

Aaron asked for phone reminders covering the human-action items in the operator
manual, and — separately — questioned whether ntfy was the right channel at all.
Both answered below.

**Channel: keep ntfy, add a SECOND topic.** The security review found the honest
answer was narrower than it first looked. Upkeep reminders ("re-auth due", "a job
stopped running", "a proposal is waiting") contain **no positions, prices or P&L**,
so the disclosure question mostly does not apply to them; what applies is "can
someone guess the address and send me fakes", and the existing topic already
measures 27 chars / 3 character classes / ~157 bits — not guessable. So no new
infrastructure: no self-hosted relay, no new service on a memory-tight box.

What WAS missing is that the off-box tuner needs a credential to reach the phone,
and handing GitHub the trade-alert topic would have given a CI secret **read**
access to the live book. Fixed with `NTFY_TOPIC_OPS`: a second topic carrying only
job names and ages, and the only one stored as a GitHub secret. A leak there costs
fake reminders, not visibility into positions. `notify.push()` gained a `topic=`
param (default unchanged, existing callers untouched) + `notify.ops_topic()`.
⚠️ The trade-alert topic still transits a third-party relay in readable form —
pre-existing, deliberately NOT bundled into this change, still open.

**New: `src/health.py` + `scripts/health_check.py` (daily 08:00).** Motivated
directly by the signal-panel finding below: a cron job that never runs cannot fire
its own alerts, so absence-of-complaint is not evidence of health. Every scheduled
job leaves an artifact (book, log, journal event, mirror commit), so a job that
stopped running shows up as an artifact that stopped moving. `evaluate()` is pure
and selftested; `gather()` is thin I/O. Thresholds are deliberately loose — a
cron-exact "missed a scheduled run" test has to model weekends, holidays and DST,
and a monitor that cries wolf gets muted, which is worse than one that notices a
day late. The Actions probe shells out to the already-authenticated `gh` CLI, so
it adds **no new PAT**.

**Alerting contract (Aaron's call): fire-once per condition.** Alerts once when a
condition appears, then silent however long it persists; healing clears the flag
with no "resolved" ping, so a recurrence is audible again. The obvious hole — miss
the buzz, miss the problem — is covered by pairing rather than nagging: the new
dashboard **"Scheduled jobs"** card renders the same checks continuously, so an
unresolved item stays visible after its one notification. Push = something
changed; dashboard = what is true now.

**Two bugs caught while building, both worth recording:**
- A check that was *not performed* was reporting as "NEVER RAN". The dashboard
  skips the network probe to avoid blocking a page render, so it confidently
  claimed the tuner had never run when it had run that morning. Added an explicit
  `unknown` status + `SKIPPED` sentinel; `alertable` (not `healthy`) now gates
  alerts, so an unperformed check can never page you.
- "Never ran" and "stopped running" were sharing remedy text, so a job that was
  simply never scheduled told you to go re-login to OpenD. They now diverge:
  "never" points at `crontab -l`, which is the actual 2026-07-24 root cause.

**Schwab reminder rewritten, `deploy/reauth_reminder.sh` DELETED.** The old one
nagged every Monday 09:00 whether or not you had already re-authed, and never
followed up if you missed it. The daily check reads real token age: silent when
current, fires inside 3 days, escalates to EXPIRED. Because flags clear on heal,
the weekly cadence now *emerges* from the token cycle instead of being hardcoded.

**adaptive-tune.yml:** pushes only when the proposal's own `moved` flag is true
(most weeks say "keep current setting" — a weekly buzz you always ignore is worse
than none), plus an `if: failure()` alert. Also fixed the clone-failure branch,
which did `exit 0` and made an **expired LEDGER_TOKEN look like a green run**.

**Verification.** `deploy/run_selftests.sh` green with both new suites wired in.
Live: `src/health.py` correctly flags the signal panel as never-run; a real push
delivered to the ops topic; a second run stayed silent (fire-once) and logged
`no NEW conditions; 1 still unresolved` rather than falsely claiming all clear;
both `moved` branches of the workflow gate exercised locally; dashboard restarted
and serving 9 health rows.

**Aaron must do two one-time steps** (OPERATOR_MANUAL §4): subscribe the phone to
the new topic (`grep NTFY_TOPIC_OPS .env`), and save that same value as the
`NTFY_TOPIC_OPS` repo secret on GitHub. Until the secret exists, the tuner's
alerts no-op silently; everything on-box already works.

---

## 2026-07-24 — Schedule audit: signal panel was NEVER armed; ledger backup half-armed

Aaron asked to verify that the mechanisms we built actually *recur*. Audited every
scheduled surface (GitHub Actions, live crontab, systemd timers, sibling repos).
Two real gaps, one benign non-gap.

**Non-gap — `adaptive-tune` (GitHub Actions) is fine.** `0 8 * * 1` is committed on
`main`, `gh workflow list --all` shows it `active`; the only reason the run history
shows nothing but `workflow_dispatch` is that the cron was added Thu 2026-07-23 and
had not yet reached a Monday. First scheduled fire **Mon 2026-07-27 08:00 UTC**;
its inputs (mirror `closes/highs/lows.parquet`) are staged. It is also the *only*
Actions mechanism by design — everything else is droplet cron. ⚠️ Standing caveat:
GitHub auto-disables scheduled workflows after 60 days with no repo commits.

**Gap 1 (fixed) — the moomoo signal panel had never run, once.** Built 2026-07-23;
`deploy/crontab.template` got the line, `DATA_SOURCES.md` was updated to say it
"runs Sunday 20:15" — but the line was never appended to the **live** crontab.
Evidence it had never fired: no `logs/signals.log`, no `research_store/signals/`,
and **0** `signal_panel` events across all 68 journal lines. Nothing else invoked
it either (`grep -rn collect_signals` hits only the script, the template, and docs
— never the loops, systemd, `/etc/cron.d`, or the sibling repos). So the forward-log
that is supposed to prospectively validate the moomoo edges was recording nothing,
and would have stayed silent — the OpenD-gap phone alert only fires *inside* a run
that never happened.

**Root cause = the template/live-crontab trap, already documented and re-hit.** The
2026-07-20 entry below records that the live crontab must be armed by *appending*,
never `crontab deploy/crontab.template` (which would wipe the box-only moomoo-desk
jobs). The signal-panel plan's Task 4 is titled "Schedule + document" but its steps
only modify the *template* — there was no arm-on-the-box step, and its checkboxes
are still unticked. Contrast Piece 1, which was armed by an explicit append and
flagged "NOT ARMED" in the interim; the panel got no such caveat, so the docs
asserted a live schedule that did not exist.

**Gap 2 (fixed) — nightly ledger backup was only half-armed.** `8657f37` added both
the `30 22 * * *` template line **and** the best-effort `backup_ledger.sh` calls in
`run_slow_loop.sh`/`run_fast_loop.sh` — belt-and-braces, the cron being the daily
guarantee. Only the piggybacks were live, and neither loop runs Saturday, so
Saturdays had no off-box backup. Not yet observed (mirror history starts 2026-07-22,
no Saturday had elapsed); first exposure would have been **Sat 2026-07-25**.

**Actions taken.**
- Live dry-run gate first: `/usr/bin/python3 scripts/collect_signals.py --dry` →
  14 held names, `opend_ok: true`, `gaps: []`, all 9 fields populated except
  `capflow_bignet_20d` on the 4 ETFs (moomoo capital-flow does not serve ETFs;
  null-safe, not an error — now documented in `DATA_SOURCES.md`).
- Appended both lines to the live crontab (backup → edit → install → `diff`
  confirmed **only** the two additions; the 3 box-only jobs survived):
  `15 20 * * 0` signal panel, `30 22 * * *` ledger backup. Smoke-tested the backup
  line as scheduled → `backup_ledger: no changes`, exit 0.
- Docs: corrected the false "runs Sunday 20:15" claim in `DATA_SOURCES.md` (now
  stamped ARMED 2026-07-24 + the ETF-null caveat); replaced the dangerous
  `crontab deploy/crontab.template` instruction in `DEPLOY.md` Phase 4 with the
  append-and-diff procedure and an explicit "editing the template arms nothing"
  rule; refreshed the stale "5 jobs" health check to the actual 10 + 3 box-only.

**First live fires to check:** signal panel **Sun 2026-07-26 20:15 ET** (expect a
`signal_panel` journal event + `logs/signals.log`); ledger backup **tonight 22:30**.

---

## 2026-07-23 — Docs audit: moomoo/FRED/finnhub + runtimes brought current

Closed real doc gaps that had been causing repeated rediscovery: `CLAUDE.md` and
`DESIGN.md` Layer-1 omitted the **moomoo** and **FRED** adapters entirely, marked
FRED "planned" (it's built), and implied finnhub was the analyst-ranking source
(it's actually event-calendar-only, not retired). Added: the **two-runtime split**
(moomoo → system `/usr/bin/python3` 3.10, not `.venv`), the shared **OpenD**
gateway, the **sibling repos** (`moomoo-vol-desk`, `moomoo-data-collector`,
`time-spread-lab`), and the **adaptive-input layer**. New `docs/DATA_SOURCES.md`
records the **live-verified moomoo surface** (capital flow, short interest,
put/call+IV overview, insider, earnings-price-move, institutional) with history
depths — key finding: moomoo history is **shallow (~1–2 yr)**, so those signals are
**forward-logged, not backtested**.

## 2026-07-23 — Adaptive-input layer (dial #1: stop_atr_mult)

Off-box weekly learner (GitHub Actions) proposes a bounded stop_atr_mult from
replayed + live outcomes. It NEVER applies. To act on a proposal:
`python scripts/promote_proposal.py` prints the exact strategy.local.toml stanza;
paste it to accept. Proposals live in research_store/adaptive/proposals/ (mirror-
backed). Spec: docs/superpowers/specs/2026-07-23-adaptive-input-layer-design.md

---

## 2026-07-20 — Piece 2 concentration cap: backtested, then ABANDONED (fails live 10% cap)

Executed the Phase-1 build+backtest plan and merged it to main (`27e395d`).

**Built (offline, no live path touched):** `src/concentration.py` — a pure
correlation "cluster cap" (`cap_weights` + `_clusters`): detect a positively
co-moving group of holdings (connected components at a correlation threshold) and,
when it exceeds a weight cap, trim it and redistribute the freed weight to holdings
outside the cluster. Weights only; stays fully invested; membership never changes.
Wired into `backtest_pit.py` as an optional layer (baseline byte-identical — verified)
+ a `--sweep` mode (baseline + 18 param combos). Added to `deploy/run_selftests.sh`.
Fresh Alpaca-IEX pool re-fetch (753/816 names, 2020-07..2026-07-17).

**Backtest result (survivorship-corrected PIT, 2021-08..2026-07, cap-on vs cap-off,
same engine):** the failure mode we guarded against — "concentration IS the edge,
capping craters returns" — did NOT occur. At `corr_threshold=0.6` (where clusters
actually form; 0.8 is inert) the cap trims max drawdown 2–5 pts for ≈nothing:
CAGR moves −1.0 to **+0.9**, Sharpe −0.04 to **+0.02**, turnover unchanged (3.0).
- Baseline: CAGR 21.6% / Sharpe 0.90 / maxDD −31.2%.
- **Gentle (lb126, thr0.6, cap50): 22.5% / 0.92 / −29.0%** — strictly beats baseline.
- Aggressive (lb63, thr0.6, cap30): 20.8% / 0.86 / **−25.8%** (−5.4 pts drawdown).
Verdict = qualified PASS; Aaron chose to proceed with the **gentle** setting.
Deliverable table saved at `/tmp/concentration_sweep.txt` (regenerate:
`.venv/bin/python scripts/backtest_pit.py --sweep`).

**Phase-2 live-wiring: ABANDONED same day — failed the go-live re-test.** Built the
per-name-ceiling handling (a `per_name_cap` water-fill in `cap_weights` +
`backtest_pit.py --per-name-cap`) and re-ran the gentle config with the LIVE
`[risk] max_weight_per_name = 0.10` enforced. The edge collapsed:

| gentle lb126/thr0.6/cap50 | CAGR | Sharpe | maxDD |
|---|---|---|---|
| no ceiling (Phase-1 "PASS") | 22.5% | 0.92 | −29.0% |
| **+10% ceiling (LIVE reality)** | **20.6%** | **0.87** | **−30.1%** |
| baseline (no cap) | 21.6% | 0.90 | −31.2% |

Under the real 10% cap the capped book is WORSE than no cap (CAGR & Sharpe below
baseline; drawdown cut shrinks to ~1 pt). **Root cause is structural:** the cap's
benefit came from concentrating freed weight into a few uncorrelated names — exactly
what `max_weight_per_name` forbids. The single-name 10% cap already does most of the
de-concentration this piece aimed to add, so there's little left to gain (a real
finding about the system). Likely generalises to the other configs, so not chased
further. **Aaron's call: shelve it.** Backed out the half-built live wiring
(`slow_loop.py`, `[concentration]` config, selftest entry all reverted); KEPT the
pure `cap_weights` (+ `per_name_cap`) and `backtest_pit.py --per-name-cap` as tested,
inert tooling that produced the finding. Spec marked ABANDONED. The 07-17 tail stays
managed by the intraday stops.

**Live money: untouched throughout.** No live path ever referenced the cap.

**Piece 3 scoping — flow-decomposition avenue TESTED and REJECTED; TI early-warning
speced.** Explored a March-2026 paper (Ding-Kang-Yu-Zhao, "Momentum and Reversal on the
Short-Term Horizon", commodities) Aaron supplied. Its edge = decompose weekly return on
speculator net-flow (Q, from CFTC COT) → the orthogonal residual R_nonQ carries
short-term momentum. Equity analog probed via moomoo `get_capital_flow` (daily
super+big-order net $ = "main"; ~1yr retained history, 30 req/30s per-interface limit).
Pulled 150 names (safe-paced, no throttle) and ran the transfer test: **it does NOT
transfer.** Contemporaneous flow→return b_1 = 0.010 (t=8.3) vs the paper's 0.41 — ~40×
weaker; flow→next-week (reversal) = 0.00005 (t=0.05, exact null); R_nonQ collapses to raw
return. Structural: equity markets too deep/liquid for order-flow to drive price the way
speculator positioning does in futures. Avenue closed (more history won't fix a 40×-weak
link). Reframed Piece 3 back to its actual scope — a **short-term momentum TI
early-warning into the risk review** (two established indicators in conjunction: MACD or
RSI-50 absolute + relative-strength-vs-SPY; advisory to the stateless review, fed as
values). Spec: `docs/superpowers/specs/2026-07-20-short-term-momentum-early-warning-design.md`.
Validate-first: a "would-it-have-caught-07-17" historical stop-out replay gates the wiring.

**Piece 1 quarterly cron ARMED (same day).** Installed the universe-refresh cron on
the box — `0 19 1-7 1,4,7,10 *  deploy/run_universe_refresh.sh` (first Sunday of
Jan/Apr/Jul/Oct, 19:00 ET; the wrapper self-guards to the first Sunday). First live
fire = **early Oct 2026** (Jul already passed). ⚠️ Installed by APPENDING the single
line, NOT `crontab deploy/crontab.template` — the live crontab carries two box-only
jobs (`moomoo-desk` 09:30/09:35 options runs, from `/root/moomoo-vol-desk/`) that the
template does NOT contain; a wholesale install would have wiped them. Template drift
noted (moomoo-desk crons live-only; left as-is — separate project). Dry-run preview
(`python3 scripts/universe_refresh.py --dry-run`) could NOT complete now — moomoo
snapshot API rate-limited (60/30s cap, shared with the options desk); retry at a quiet
time (not market hours) to eyeball the proposal. Not a code fault; real run is Sunday
19:00 when moomoo is idle.

---

## 2026-07-19 — Selection-engine robustness: Piece 1 shipped, Piece 2 planned

Long working session with Aaron. Framed a **3-piece "more robust selection
engine"** thread (motivated by the 07-17 storage/semi cluster all stopping out
together — momentum concentrates into the leading theme, amplified by a tech-heavy
universe) and executed the first piece, plus two monitor fixes and a moomoo
capability investigation.

**The roadmap:**
- **Piece 1 — universe maintenance** (keep the candidate list fresh/liquid). SHIPPED.
- **Piece 2 — concentration control** (stop a co-moving cluster dominating capital).
  SPEC + PLAN committed; backtest not yet run.
- **Piece 3 — short-term momentum (TI) overlay** (moomoo MACD/RSI early-warning into
  the risk review). SCOPED only, not started.

**Monitor fixes shipped early (commits `e68df74`, `74bdd40`).**
- Log the "not held" skip-set once per change, not every 15s poll; `PYTHONUNBUFFERED=1`
  in the unit (stdout was block-buffered → journald timestamps up to ~27 min stale).
- `refire_gate`: a failing exit sell now backs off (`refire_retry_secs=120`) and
  escalates after 3 consecutive failures — was re-spawning the Claude executor +
  re-alerting every 15s indefinitely. Added venv-pinned `deploy/run_selftests.sh`.

**Piece 1 — universe maintenance (merged to main, `f95c2ed`).** Quarterly, offline,
human-reviewed refresh of the fixed 150-name universe: moomoo market-cap screen +
`get_market_snapshot().turnover` (free, no quota) → seed-protected banded membership
proposal → AUTO-APPLY routine / HOLD on seed-drops + anomalies (email + read-only
dashboard panel + phone push) → approve in a Claude session. Weekly stale-seed watch
rides the slow loop. New: `src/adapters/moomoo/` (data-only — first moomoo
integration), `src/universe_maint.py`, `scripts/universe_refresh.py`,
`[universe_maintenance]` config. Built via subagent-driven TDD (8 tasks + whole-branch
review); the review caught & fixed 4 real bugs (snapshot batch-abort on a delisted
ticker; silent-corruption on rate-limit; a cron DOM/DOW OR-gotcha; a sticky dashboard
banner). **NOT ARMED — the quarterly cron is NOT installed;** arming is a deliberate
`crontab deploy/crontab.template` step. Inert until then; dry-run anytime:
`/usr/bin/python3 scripts/universe_refresh.py --dry-run`.

**Piece 2 — concentration cap (spec + plan committed; NOT built).** Objective: *cap
the tail, keep the edge* — down-weight a highly-correlated cluster when its aggregate
weight is too high, keeping all names + fully invested. Detection = realized
correlation from the price cache (NO sector taxonomy — so Piece 1's deferred
sector-tagging is moot). Discipline: **backtest FIRST, live wiring gated on the
result.** Phase 1 = build pure `src/concentration.py` `cap_weights` + wire into
`backtest_pit.py` + sweep params + a go/no-go table; pass = drawdown ↓ ≥~3-5pts with
≤~2pts CAGR give-up (Sharpe ≥ baseline) → write a Phase-2 live-wiring spec; else
abandon (concentration IS the edge).
→ **NEXT ACTION: execute `docs/superpowers/plans/2026-07-19-concentration-cap-backtest.md`**
(3 tasks, offline, no live money). Spec:
`docs/superpowers/specs/2026-07-19-concentration-cap-backtest-design.md`.

**Pending / watch:**
- **Schwab weekly re-auth due before Thu 2026-07-23 ~06:28 ET (10:28 UTC).** Hard
  7-day token expiry; doing it earlier does NOT extend it (slides the window). Mon/Tue.
- Piece 1 quarterly cron un-armed (above).
- Piece 3 not started.

---

## 2026-07-17 — Fast loop now honors the stop-out cooldown (churn guard)

**Found while double-checking holdings** (Aaron: "I saw it just bought XLK").
XLK stopped out 09:31 ET (sold 173.60) and the daily fast loop **rebought it
10:01 ET** (174.29) — the same session, while XLK was on cooldown until 07-22.

**Root cause.** The cooldown list (`monitor/cooldown.json`, written by the
monitor on every stop) was honored by `slow_loop.py` (excluded from the weekly
book rebuild) but **never read by `fast_loop.py`**. The book keeps a stopped
name until the next weekly rebuild, so the daily fast loop diffed "book wants
XLK, hold=0 → BUY" and rebought it — the exact stop-vs-momentum churn the
cooldown exists to prevent. A read-only replay of the live plan showed it queued
rebuys of **all 8 names stopped out today** (ALAB, AMD, BE, INTC, LRCX, SNDK,
STX + the AMAT phantom).

**Fix.** `fast_loop.py` gains `load_cooldown()` + `apply_cooldown()`: BUY orders
for names inside an active cooldown window are moved to `blocked`; sells/exits
and non-cooled buys pass through untouched (cooldown never blocks getting out).
Wired in `main()` right after `gov.vet_plan`, mirroring the slow loop. Fails
open (empty set) on an absent/torn cooldown file. `--selftest` covers it;
verified live — the 7 cooled names now block, XLK's existing position is left
alone (the monitor's stop protects it).

**Residual (self-healing):** AMAT is a book name that was never held, so it has
no cooldown entry and is still a buy candidate; its stale 07-15 stop (528) sits
above price (~518), so if bought it would stop out once. The Sunday weekly
rebuild re-ranks/re-geometries the book and clears this.

---

## 2026-07-17 — Intraday monitor: phantom-holding stop-loop (AMAT) fixed

**Symptom (Aaron):** repeated failed sell orders / "something stuck." The
`agentic-monitor` service was firing an `exit_signal` for **AMAT** (`reason:
stop`) every ~60s from ~13:41 UTC and re-running the headless exit executor each
time — journal + phone spam, a `claude -p` subprocess per tick. No money moved.

**Root cause — a phantom holding.** AMAT was in the slow-loop book
(`current.json` thesis, stop 528.63) but was **never actually bought** (RH order
history for AMAT is empty; the account was 100% invested — journal shows repeated
`insufficient_buying_power` skips). The monitor built its stop-watch list from
**book theses, not real holdings** (`market_monitor.py:173`). AMAT's price (518)
sat under its stop, so every tick it fired → executor found nothing to sell →
wrote an empty `sold` → so the monitor never marked AMAT `fired`
(`:253–255`, the legit retry-on-transient-failure path) → re-fired next tick.
Infinite loop. The account's 8 genuinely-sold names of the day (STX, XLK, LRCX,
AMD, SNDK, INTC, BE, ALAB) were suppressed only by the `fired` flag.

**Fix.** New `owned_symbols()` reads the reconcile snapshot
(`research_store/rh/positions.json`) and gates the watch list to names actually
held (`qty>0`). Fails **open** (watches all theses) if that snapshot is
absent/torn — never silently drops stop protection. Genuinely-held names whose
sell fails transiently stay in the snapshot, so the retry path is preserved;
phantoms can't re-fire. `--selftest` covers it.

**Verified live.** After restart (13:57 UTC) the watch list = the 5 real
holdings (DELL, IWM, MU, SPY, XLE); AMAT dropped. Two clean polls (13:57:33,
13:58:35) produced zero AMAT signals. Realized P&L for 07-17 stands at −$6.41
(8 stop exits in a broad semis selloff) — unrelated to the loop, which placed
no orders.

---

## 2026-07-16 — Slow loop outage: expired Schwab token + dangling tokens.db lock

The Mon–Fri 18:00 slow loop (nightly risk-exit recompute) died on 2026-07-15 —
`fetch_prices` returned **0/168** and the phone alert fired. Two stacked causes:

1. **Schwab refresh token expired.** The weekly re-auth (last done 2026-07-08)
   lapsed at 17:20 UTC on 07-15; the 7-day refresh token was dead, so no token
   could be minted. (A `schwab_auth.py` run that "looked done" earlier never
   wrote here — see the interactive-stdin note below.)
2. **`secrets/tokens.db` held a dangling exclusive lock.** The `market_monitor`
   systemd service's long-lived `schwabdev` connection issues `BEGIN EXCLUSIVE`
   around each token refresh (schwabdev holds it across the HTTP call to
   serialize refresh across instances). When the refresh failed it left the
   transaction open — journald showed `cannot start a transaction within a
   transaction` looping every 15s from ~11:57 ET. That RESERVED/EXCLUSIVE lock
   (rollback-journal mode) starved the 18:00 cron of even a read lock →
   `database is locked` on every ticker.

**Collateral:** `fetch_prices` wrote the empty panel over `closes.parquet` before
crashing, wiping the 10y cache; the book (`current.json`) went stale a day.

**Fixes (this commit + one runtime change):**
- **WAL mode on `secrets/tokens.db`** (`PRAGMA journal_mode=WAL`, one-time,
  persists in the file; `tokens.db-wal`/`-shm` sidecars now present). Readers are
  never blocked by the monitor's writer — proven live: a concurrent Schwab fetch
  succeeded while the monitor ran. This is the durable fix for the lock class.
  Not in git (secrets/ is ignored) — hence this note. Revert with
  `PRAGMA journal_mode=DELETE` if ever needed.
- **`fetch_prices.py` abort-guard:** if `<50%` of tickers fetch, it aborts
  (exit 2 → cron alert) and **leaves the existing cache intact** instead of
  overwriting it with a systemic-failure result. Per-ticker failures now print
  the real error (expired token / locked db) instead of a bare `FAILED`.
- **`slow_loop.py`:** refuses to rebuild the book off an empty panel (clear
  message, not a cryptic `IndexError`).
- **`scripts/schwab_finish_auth.py` (new):** completes the OAuth exchange with the
  pasted redirect URL passed as an *argument*, for agent shells where `input()`
  hits EOF (Claude Code `!`). The normal weekly re-auth still wants a real TTY:
  `.venv/bin/python scripts/schwab_auth.py`.

Re-auth done 2026-07-16 (good through ~07-23); prices rebuilt 168/168; book
rebuilt; monitor restarted clean under WAL; the 07-16 12:00 armed risk review
ran on the fresh book and held all 13 (0 orders, broker-confirmed).

## 2026-07-15 — Intraday risk-management overlay shipped + armed

The defensive, de-risk-only intraday risk overlay went live (branch
`feat/intraday-risk-review`, 14 commits, merged to `main` at `abc8498`). It adds
two headless-Claude reviews (cron 12:00 + 15:45 ET, Mon–Fri) that tend open
positions between weekly rebuilds: tighten stops / lower take-profits (written as
stricter-only overrides the always-on monitor enforces within 15s), and
trim/exit via the RH MCP — never loosen a stop, raise a target, or open a
position (enforced in `scripts/risk_review.py`'s one-way invariant, re-checked in
the monitor's `apply_overrides`). Core files: `scripts/risk_review.py` (new
deterministic core, `--selftest`), `prompts/risk_review.md` (agentic procedure),
monitor override overlay, `[risk_review]` config, weekly override-clear in
`slow_loop.py` (Sunday only — nightly recompute must not wipe intra-week
tightenings), and `risk_actions_this_week` in the letter facts (armed actions
only; trim/exit narrated from confirmed fills, never intents).

Built via a 10-task subagent-driven pass with per-task + whole-branch reviews;
the final review caught two integration issues since fixed (nightly-vs-weekly
override clear; a malformed agent decision aborting the whole `--apply` batch).

**Armed same day** (principal, 2026-07-15) via `strategy.local.toml`
`[risk_review] alert_only = false` (with `live_approved = true` already set) —
`armed = True`. No alert-only observation period was run first; the design's
observe-then-arm rollout was skipped at the principal's explicit direction.
Revert to alert-only = flip that flag back to `true`. First armed run: 12:00 ET
2026-07-15.

## 2026-07-10 — SPY→XLE rotation: buy leg deferred on unsettled cash

The ETF sleeve rotated SPY out for XLE (slow loop, as_of 2026-07-08). The fast
loop sold SPY (filled $4.55 @ $752.92, +$0.05 realized) but the paired XLE buy
was rejected by Robinhood with `EQUITY_NOT_ENOUGH_BP_DOLLAR_BASED`: this is a
cash account and dollar-based buys need *settled* funds, so a buy that follows
a sell defers ~1 trading day (T+1) and re-plans next run. Decision (principal,
2026-07-10): keep the 100%-invested target and accept the lag — a standing
~7.5% cash buffer would cost more in drag than the occasional one-day gap.
Skipped orders are now journaled first-class (`status:"skipped"` + `reason`,
e.g. `pending_settlement`) and every execution pushes a phone summary via
ntfy (`src/notify.py`, commit caeff3d) — before that, rebalance trades were
silent unless Robinhood's own app notified.

## 2026-07-08 — First deployment: investor-profile block halted 13 of 14 orders

During the initial book deployment, Robinhood blocked the run's second trade
with "investor profile incomplete" (account 948184924), halting 13 of the 14
approved orders after EEM. The block was operational, not a market call; it
cleared the same day and a second run placed all 14 orders. Watch item for
future account-level checks appearing mid-run.

## 2026-07-09 — fast_loop.py left a stale order_plan.json on empty plans

Flagged by the 2026-07-09 fast-loop run: when the computed plan is empty,
`scripts/fast_loop.py` did not rewrite `research_store/rh/order_plan.json`, so
yesterday's already-executed plan (with `live_approved: true`) sat on disk. A
run that blindly trusted the file could re-place old orders (~$54 double-buy
with zero cash). The 2026-07-09 run detected the staleness and placed nothing.
FIXED same day in commit c5a3e28 — the plan file is now rewritten every run.
