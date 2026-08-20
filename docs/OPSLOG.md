# OPSLOG — operations & maintainer notes

Dated log of operational events and technical flags, newest first. This is the
"separate entry elsewhere": the weekly investor letter (The Claude Ledger)
narrates the portfolio only — plumbing, broker blocks, code flags, and
settlement mechanics land HERE (written by the newsletter run from the week's
journal `notes`, or by hand). One `##` heading per entry.

---


## 2026-08-20 — RTX: the stop worked, and the permission to RECORD it did not

**The trade was correct. The bookkeeping was structurally impossible.** Second
day running that a stop-exit ended in an unknown outcome; the cause is not the
retry logic fixed on 08-19, it is one line in a permissions file.

### What happened

11:52:12 ET — RTX breached its stop (216.13 against a 216.32 level). The monitor
fired the exit executor, which placed a market sell. Verified against the broker,
not the cache: order `6a8722dd-97a6-40ef-b76f-f1799e8aac93`, **state filled,
cumulative_quantity 0.026963 of 0.026963, average_price 216.0601, one execution,
zero fees.** Exactly ONE RTX order exists for the day. The software stop did its
job.

11:53:59 — the executor then reported it could not write
`research_store/monitor/exit_result.json`, tried every path it had, and stopped
to ask a human for approval. It is headless. Nobody was there.

**Root cause: `deploy/loop_settings.json` carried a blanket
`Edit(./research_store/monitor/**)` deny, and that rule covers `Write` too.**
`exit_result.json` lives in that directory and is **the only thing the monitor
reads to learn a sale fired**. The deny was not wrong in spirit — that directory
also holds `overrides.json` (the enforced stops), `state.json` (fired flags) and
`exit_request.json` (the monitor's instruction TO the executor), none of which an
agent should touch. It was written as a blanket and swallowed the one file the
executor must write.

**The 08-19 guard held.** One executor launch, one EXECUTE line, `unresolved: {}`,
no retry, no duplicate sell. That fix worked exactly as designed; it bounded the
damage to bookkeeping.

### Reconciled from broker truth

`exit_result.json` written from the broker's order record; snapshot republished
through `refresh_broker_snapshot()` (11 positions, RTX gone) rather than
hand-edited — the 2026-08-16 lesson about hand-writing that file.

### The guard, so it cannot recur

Fixing the rule fixes today. What made this expensive is that **a permission rule
is only exercised on the path it guards**, so the defect was undiscoverable until
a real stop fired against real money — the same shape as the allowlist outage of
2026-07-30 and `check_settings_no_exec_wildcard` running nowhere that mattered
(2026-08-10).

`repo_checks.check_exit_path_can_record` now asserts this statically, daily,
without needing a position to break. It declares the files the exit path MUST be
able to write and the files it must NOT, models Claude Code's deny-beats-allow
precedence (the precedence that caused this), and fails in both directions:
a required write that is denied, and enforced state that became writable. It also
fails on any `ask` rule, because an approval prompt in a headless runner stalls
the procedure *after* an order is already placed — which is precisely what
happened here.

Mutation-tested: run against the pre-fix settings it reports
`the stop-exit executor CANNOT write research_store/monitor/exit_result.json`.
Against the current settings it is clean.

The permission itself is now per-file: `exit_result.json` allowed for Edit and
Write; `overrides.json`, `state.json`, `cooldown.json`, `quotes.json`,
`exit_request.json`, `unprotected.json`, `enforcement.json`, `wakes.json` denied
for both. Narrowing Edit without mirroring Write would have silently widened the
other tool, so every deny is now stated twice on purpose.

## 2026-08-20 — an agent-set stop with no thesis is now actually enforced

Same day, same class, found because JNJ was bought at 10:40 and sat unprotected.

The monitor's watch set was `theses ∩ owned`, and **theses are written by the
NIGHTLY slow loop** — so any position opened intraday was unwatched for the rest
of that session no matter how carefully the agent set its levels. `set_levels`
said so out loud ("no thesis for this symbol -- the monitor is not watching it at
all") and the position stayed unprotected anyway. It surfaced on 08-19 (BAC,
FTNT) and again on 08-20 (JNJ: stop 261.00 written, enforced by nothing).

An agent-set stop IS a stop. The stricter-only override machinery needs a base
thesis to be *stricter than*, but that is an argument about how to merge two
stops, not a reason to enforce neither.

`standalone_candidates()` + `arm_standalone()`: an owned symbol carrying an
agent-set stop and no thesis is quoted with everything else and watched.

⛔ **The price guard is the whole safety argument.** On 2026-08-12 `set_levels`
accepted a stop ABOVE the live price and reported it enforced; an armed monitor
reads that as an instant breach and market-sells the entire position — a full
liquidation reached without any order being placed. Creating watches out of
overrides re-opens exactly that door, so a candidate arms ONLY when a live price
is known AND sits strictly above the stop. Anything else is refused with a
reason, alarms, and stays in `unprotected` where it was already flagged.
Refusing costs a position protection it did not have a moment ago; arming
wrongly sells it at market. Those are not symmetric. Mutation-tested: deleting
the `px > stop` test fails the selftest.

Live within 2 seconds of the restart: `watching on agent-set stops (no thesis):
JNJ`, and `unprotected.json` went from `["JNJ"]` to `[]`.

## 2026-08-20 (later) — the ETF sleeve is DELETED, not disabled

Aaron, on being shown that the independent reviewer kept discussing ETFs:
*"WTF is this with the sector series?"* and *"I want it gone like it was supposed
to be two days ago when we retired it."* He was right, and the reviewer was only
talking about ETFs because the repo was still full of them.

**What "retired" had actually meant.** On 2026-08-16 the sleeve was retired by
setting `[etf_sleeve] enabled = false`; on 08-17 its four positions were sold.
Everything else stayed: an `[etf_sleeve]` config table, an 18-name universe
file, a second ranking engine inside `slow_loop.select_book()`, ETFs in the
order-gate whitelist, ETFs pooled into the agent's candidate screen, a `sleeve`
field in the investor letter, a SLEEVE table in the run report, and an "ETF
sleeve" clause in the CHARTER — the agent's entire standing account of the game
it is playing. A retirement that leaves the mechanism in place is not a
retirement. It is a dormant feature that reads as live to every later reader,
human or model, which is exactly how a second model came to spend an audit
discussing an allocation that had been dead for four days.

**Deleted:** `config/etf_universe.csv`, the `[etf_sleeve]` table, `sleeve_hold`
/ `sleeve_weight` from `[portfolio]`, the sleeve arguments and second engine in
`select_book()`, the ETF branch of `protective_theses`, `_all_tickers()` (which
had zero callers), the letter's always-false `sleeve` field, the empty SLEEVE
report table, and the sleeve clauses in `charter.py` + `prompts/charter.md`.
The backtests (`backtest.py`, `backtest_pit.py`, `sweep.py`) modelled a 70/30
two-engine book and now model the one that runs — ⚠️ **their numbers are not
comparable with anything published before today.**

**The whitelist is the part that bites.** `governance.whitelist()` was
`universe ∪ etf_sleeve`, and the config's stated reason for keeping the ETF half
was *"four are HELD right now"* — sold on 08-17, which voided the reason and
left the entry. It is the single-name universe alone now, so **a buy naming a
fund is refused**. A SELL is refused by nothing but the kill switch, unchanged:
blocking one would strip a position of its only protection. Pinned by a test
that forces both sides through `vet_plan` and by one asserting a stale
`[etf_sleeve]` table in a local override cannot re-open it.

### Second Codex audit, same day — DISSENT, and it was right twice

Aaron asked for an independent check of the deletion. Verdict DISSENT. Verified
each claim rather than accepting it; two were real defects in code written hours
earlier, two were overstated, and the rest were stale strings.

**REAL — the equity probe said "FAILS CLOSED" and did not.** The comment was
written by me and was false in the one place it mattered. If the moomoo probe
threw, or returned partial rows, every unprobed candidate fell through to
`row is None -> True` and was added on the NAME-SHAPE regex alone — the regex
that passes SPY, GLD and XLK — on the single path that writes the order-gate
whitelist. Second hole: `cand` excluded incumbents, but `propose_membership()`
can re-add a DROPPED incumbent, so a name it would consider was never probed.
Now: probe the full add set (`ranked[:add_rank_max]`, incumbent or not), and a
probe that fails or comes back incomplete means **no adds at all** plus a forced
HOLD with a stated reason. A week with no adds is a non-event; a fund in the
whitelist is not. Pinned by a test.

**REAL — stale strings.** `prompts/newsletter.md` still told the letter-writer
what a `sleeve` field meant (the field was deleted); `sweep.py`'s baseline
markers still said `top-10`/`70/30` so the `*` marked a row that was no longer
the baseline; `backtest_pit.py` still printed "70/30" as its header; and
`[risk] max_weight_per_name` still called 10% "of the sleeve".

**OVERSTATED — "whitelist bypass".** The reviewer marked claim A DISPROVEN
because `require_whitelist=false` would disable the check and because
`.claude/settings.json` carries no order hook. Live config has
`require_whitelist = true` (verified), and the hook is deliberately wired in
`deploy/loop_settings.json` for the LOOPS and not for interactive sessions —
documented in CLAUDE.md since 2026-08-11, not a regression from this work. A
config value that COULD be set wrong is not the same as one that IS.

**KNOWN AND ACCEPTED — non-sector funds have no computable stop.** IWM, EFA,
EEM, GLD, TLT and AGG are no longer price-fetched, so a manually-bought one
would get no protective geometry. Nothing in the system can buy them, none are
held, and the case is LOUD rather than silent: the slow loop prints
`⛔ STILL UNPROTECTED (no computable stop)` and `market_monitor` writes
`unprotected.json` every tick with the health check watching it. Fetching six
fund series permanently, to protect a position that can only arrive by hand, is
the wrong trade — but it is stated here rather than left for someone to
discover.

**F, on "nothing mandates a trade":** the reviewer is technically right that
slow-loop geometry feeds an automatic SELL, because the monitor enforces the
stop it supplies. That is the pre-existing stop mechanism and it is protection,
not a mandate — but the distinction is worth stating plainly rather than
claiming the loop can never cause an order.

### The hole that would have undone all of it, found by asking

Aaron: *"check the screen the universe is built from to make sure it is
screening equities only."* It was not.

The weekly screen's ADDs come from a candidate pond that is moomoo's market-cap
screen — **documented as UNFILTERED**, so funds, preferred shares and SPAC units
all rank (`docs/DATA_SOURCES.md` §5e) — falling back to `config/pit_pool.csv`,
which `build_pool.py` had been stuffing with all 18 ETFs by construction. The
only guard was `_looks_like_common_stock()`, a regex that **`SPY`, `GLD` and
`XLK` all pass**. An ADD is written into `config/universe.csv`, which IS the
order-gate whitelist. So the Friday screen could have put a fund straight back
into the tradeable universe and silently undone the deletion by another route.

Fixed in layers, the real one first:

1. **A positive test, not a denylist.** moomoo serves NO `total_market_val` for
   a fund — the documented cause of the capital-flow ETF null (OPSLOG
   2026-07-28). `universe_refresh` probes the actual add candidates and rejects
   anything without a market cap. Injected as a predicate so it is testable
   without OpenD, and it **fails closed**: an unverifiable candidate falls back
   to the name-shape check rather than being added on trust.
2. **A `NON_EQUITY` denylist backstop**, because a denylist can only exclude the
   funds someone remembered to list.
3. **The filter moved into the ADD PATH**, not merely a HOLD reason in
   `classify()` — a screen that only flagged a fund would still have made it
   buyable the moment a human approved an otherwise routine weekly proposal.
   Rejections are reported (`rejected_non_equity`), never silent.
4. **The pond source cleaned:** `build_pool.py` no longer folds funds in, and
   the 18 were removed from the committed `config/pit_pool.csv` (816 -> 798).

### What was NOT deleted, and why the name changed

The residual-momentum tilt (`[signal] residual_tilt = 0.75`, adopted 2026-07-24
on the PIT backtest) regresses every name on 11 sector return series to separate
a stock's own move from its sector's. Those are **factor inputs**: nothing can
buy, sell or hold them, they are not in the universe, not in the whitelist and
not in the screen. Aaron: *"fine then leave it but it shouldn't be using 'ETF',
that muddies things."* Correct — calling a factor-model input an ETF is what let
a deleted allocation look alive. `SECTOR_ETFS` is now **`SECTOR_FACTORS`**, and
`fetch_prices` builds its list from that constant plus SPY (the regime series)
rather than from a universe file.

Deleting those columns would not remove an allocation — it would silently drop
the signal to plain momentum with only a printed note. That remains a one-line
choice (`residual_tilt = 0.0`) and it is the principal's, not a side effect of
tidying.

**Verified, not assumed:** whitelist = 150 names, 0 funds; `candidates()` returns
single names only; the slow loop runs equities-only with the tilt alive
(`11 sector series`); a fund BUY is refused and a fund SELL is not; no live code
reads the deleted file. ALL SELFTESTS PASSED, repo_checks PASS.


## 2026-08-20 — the candidate pipeline was doing work nobody could see

Aaron, on being walked through how the ranking is maintained: *"this whole
process sounds like it's a useless mess ... just useless junk that serves no
purpose but to look like something was done."* He was describing three separate
things, and on inspection each was real.

### 1. The universe screen had never run. Not "rarely" — never.

`run_universe_refresh.sh` rescreens the 150-name pool the agent picks stocks
from. It was armed 2026-07-20 as `0 19 1-7 1,4,7,10 *` plus a
`[ "$(date +%u)" -eq 7 ]` guard in the wrapper — the first Sunday of
Jan/Apr/Jul/Oct. July had already passed when it was armed, so the first real
fire would have been **Sun 2026-10-04**. In the meantime the candidate pool was
frozen at its inception list while the docs described a maintained universe.

Now **weekly, Fridays 17:00 ET** — after the close, before the 18:00 slow loop,
so Friday's ranking already sees the fresh pool.

The cadence had been spelled in two languages (a cron field and a bash `date +%u`
test) and stated plainly in neither. It now lives in `[universe_maintenance]
screen_day` and is enforced by `universe_maint.screen_due()` — a pure, selftested
predicate the script itself checks, so a wrong cron line cannot fire it on the
wrong day and the config cannot be contradicted by a shell literal. It fails
toward RUNNING on an unreadable value, because a screen that silently stops is
precisely what the last month looked like.

It also had **no liveness check at all**, which is why nobody noticed — added as
`health.SPECS["universe_refresh"]`, keyed to the PROPOSAL artifact rather than
`logs/universe.log` (the log moves even when the script exits early; same
output-not-log reasoning as 2026-07-30).

⚠️ **First weekly run will almost certainly HOLD, not auto-apply.** The 2026-07-28
dry run proposed 16 changes against an `auto_apply_max_changes` of 5. That is
correct behaviour after a month of drift, not a fault — it phones and waits.

### 2. The nightly re-rank was computed and thrown away

Rotation is weekly, so on the five weeknight runs `hold_selection()` discarded
the fresh ranking entirely and nothing downstream ever saw it. The slow loop was
doing the work and deleting it — the same shape as the signal panel that never
ran (2026-07-24) and the reviewer that never launched (2026-08-13), except here
the job ran perfectly and its output had no consumer.

`src/rank_history.py` now records each run to `research_store/ranks/<as_of>.json`
and `brief()` carries the diff into every session: top-20 entries and exits,
absolute-gate flips, the biggest rank moves, and universe joins/leaves (which is
how Friday's screen shows up on Friday night). Verified on two real consecutive
sessions, 08-18 -> 08-19: 80 names moved, MRK/KO/MNST entered the top 20,
**BAC and RTX — both held — fell out of it**, and LIN/MDT/PLTR regained the
absolute gate. That is decision-relevant and it was being deleted nightly.

`diff()` is pure and distinguishes `no_snapshot` / `no_comparison` / `ok`, so a
first run can never render as "nothing changed" — the `unscoreable` mistake from
docs/2026-08-19-scorecard-consumer-gap.md §3. `--dry` records nothing.

⛔ **It rotates nothing.** The re-rank is a fact delivered to the session; what
it means is the agent's call, as with everything else since 2026-08-14.

### 3. The agent was reading a different ranking from the one the book uses

Found while explaining the above; Aaron: *"there is no reason the scoring should
be different ... the two should be consistent."* There were **two** divergences
producing one symptom:

- **The tilt was missing.** `scripts/slow_loop.py` ranks with the adopted 0.75
  sector-residual blend (PIT-backtested, adopted 2026-07-24).
  `candidates()`/`universe()` called `momentum.compute()` bare. The agent's list
  was ranked on a different signal from the book's.
- **ETFs were pooled in.** The agent-facing screen ranked all 18 ETFs alongside
  the 150 single names. `score` is a PERCENTILE rank, so the pool DEFINES it:
  ETFs carry structurally lower sigma, flattering themselves on R/sigma and
  shifting every single name's percentile. The slow loop has always kept them
  apart.

`residual.kwargs_from_config()` is now the single source of the tilt (moved out
of `slow_loop`, which was its only caller — which is exactly why the screen never
got it), and `screen.rank_book()` is the one ranking both paths produce. The
selftest asserts BEHAVIOURAL equality against `momentum.compute()` called the way
the loop calls it, and separately asserts the tilt actually changes the order —
without that second assertion the first would be vacuous, the 2026-08-14 lesson.

**ETFs are out of the ranked candidate list entirely.** The sleeve was retired
2026-08-16 and its four positions sold 08-17; ranking them as candidates
re-elevated an allocation that had been retired. They remain scored for the three
narrow reasons CLAUDE.md gives — tilt regression factors, order-gate whitelist,
and a computable stop for anything held — none of which is "put them in front of
the agent as things to buy". Their PRICE columns stay load-bearing and a selftest
proves it: strip the sector columns and the tilt correctly falls back to a plain
rank.

### 4. A latent crash, found on the way, that the weekly churn made likelier

`protective_theses()` — the function that gives a held-but-unselected name a base
stop — built its thesis text with `int(row["rank"])`, and **rank is NaN for a
name that fails the absolute gate**, which is precisely the case it exists for.
`int(NaN)` raises `ValueError`, outside the per-name `try`, so it would have
taken down the whole slow loop on a rotation night. Non-rotation nights hid it
because `hold_selection()` keeps ineligible held names covered.

Latent since 2026-08-14. Found now because a weekly universe rescreen makes a
held name leaving the ranked set materially more likely. Fixed, and the fix was
**mutation-tested**: reverting it reproduces
`ValueError: cannot convert float NaN to integer`.

The requirement pinned by the new test is not "does not crash" — it is that such
a name still gets **a real stop**, still carries `target_weight 0.0`, and is
never told to sell. Aaron, explicitly: *"that is up to the agent to decide to
close or not."* The thesis text now states the fact and says the choice is the
agent's.

### What was NOT changed

- Nothing here places, sizes, or mandates a trade. The selection remains a
  proposal; the sessions decide.
- The Sunday rotation cadence, `book_hold`, `book_band`, the signal math and the
  stop geometry are all untouched.
- `agentic-monitor` imports none of the changed modules — the stop watcher's
  behaviour is unaffected. Verified rather than assumed.

One knock-on worth stating: `config/universe.csv` **is** the order-gate
whitelist, so a weekly screen that drops a name now blocks new BUYS of it. It can
never block a sell, and a held name keeps its protective stop.

## 2026-08-18 — limited margin, and the instructions that had stopped describing the system

Aaron switched the Agentic account from cash to **limited margin**. Verified at
the broker rather than taken on trust, because the whole point of the change is
what the system is allowed to believe:

| | 08-17 (cash) | 08-18 (limited margin) |
| --- | --- | --- |
| `type` | `cash` | `limited_margin` |
| `cash` | 25.13 | 25.13 |
| `buying_power` | **2.36** | **25.13** |
| `unleveraged_buying_power` | — | 25.13 |
| `unsettled_funds` | 7.06 (08-10 ref) | 0 |

`buying_power == unleveraged_buying_power` is the machine-readable proof that
this is settlement, not borrowing: proceeds are usable the same session and
there is no leverage to draw on. `option_level` is empty, so options remain
structurally absent. The ~$22.80 stranded unsettled from the 08-17 ETF
liquidation became spendable.

**The change itself was small; the sweep it triggered was not.** Sixteen sites
asserted the cash-account premise, six of them read by the live agent every
session — `account()` returned "this is a CASH account" on every call, and the
charter told the agent that selling and rebuying the same day was impossible.
The agent was pacing against a constraint that no longer existed. `buying_power`
remains the figure to size against everywhere: its authority never depended on
the gap being large, so no sizing logic changed, only the claims about why.

One behaviour change. `record_fills._EXPECTED_SKIP` muted `settle`/`pending`
skips from the phone and the letter as routine self-healing. Under same-session
settlement that deferral should not occur at all, so a settlement skip is now an
anomaly and the first evidence the account changed underneath us — it surfaces.
`_expected_skip` had **no test at all**, which is precisely how the settlement
entries survived the account change unexamined; it has one now.

### What the widened sweep found, which was worse than the margin text

Aaron asked for every instruction surface to be checked for *all* stale
references, not only settlement ones. Four findings, in descending order of how
badly they could bite:

- **⛔ The documented way to stop just the sessions did nothing.** The operator
  manual said `crontab -l | grep -v run_session.sh | crontab -`. There has never
  been a `run_session.sh` line in cron — the sessions are systemd timers. The
  command exits 0, reports nothing wrong, and leaves both trading sessions
  armed. A stop that fails toward trading. Replaced with `systemctl disable
  --now agentic-session@{open,close}.timer` plus a `list-timers` VERIFY line,
  because a command that appears to succeed is exactly what failed here.
- **The charter promised a session that has never existed.** It said "Three run
  each weekday" and carried a full `### 12:00 — WHAT CHANGED?` section; only
  `@open` (10:35) and `@close` (15:15) have ever existed, and it named both of
  those at the wrong times (10:00, 15:45 — the latter a fossil of the retired
  risk review). An agent told a midday session is coming can defer a decision to
  a session that never runs. Nothing caught it because `charter.py`'s selftest
  asserted the three headings — it was testing the charter against itself. The
  12:00 section's trim/giveback discipline (the behaviour with the best measured
  record here) was MOVED into 15:15, not deleted, and the phantom times are now
  asserted ABSENT so they cannot come back.
- **The charter justified a live obligation with a deleted subsystem.**
  `rule_out()` was explained by "the 10:00 fast loop is deterministic: it
  rebuilds the book from the stored targets" — deleted 08-14. The obligation is
  still enforced at the order gate; only its stated reason had died, which is
  the shape of staleness most likely to get a real rule ignored.
- **`render_gate` presented an incomplete refusal list as complete.** It said
  "It refuses:" and omitted the automatic drawdown halt and an active
  `rule_out` — the drawdown halt being the one refusal an agent cannot diagnose
  from its own tools. Both added, interpolated from config, with the entries-only
  scope stated.

`docs/STRATEGY.md` — the file whose header says "Read this before trading" —
still specified top-10 names, a top-4 ETF sleeve and a 70/30 split (retired
08-16, liquidated 08-17), cited `fast_loop.apply_chase_guard` as enforcing the
chase rule (it enforces nothing anywhere), and had a section titled "Execution —
the fast-loop procedure". §4 now defers to `strategy.toml` rather than restating
its numbers, which is the only version of that section that cannot rot again.

**The pattern worth keeping:** every one of these was a doc describing a system
that had been changed underneath it, and each was found by checking a claim
against the machine rather than reading for plausibility. The claims all read
fine. What made them false was invisible from the text.

Not done, deliberately: no leverage tripwire reading `unleveraged_buying_power`,
and no churn norm to replace the pacing that settlement used to impose by
accident. Aaron's call — state the world accurately and let judgement operate,
rather than layering a new static rule the moment an old accidental one falls
away.


## 2026-08-17 — the ETF sleeve was retired, then actually liquidated

The sleeve was retired in config on 08-16 (`22151c1`: `[etf_sleeve] enabled =
false`, book 10 → 14 names at 7.14%, band 15 → 20). **Retiring it in config does
not sell anything** — the commit deliberately does not force-sell held ETFs, it
drops them from the weighted book and leaves the exit to judgement. So for one
session the account held four funds that the ranking assigned 0% and no thesis
argued for.

The 09:30 session on 08-17 closed that gap on a one-time instruction from Aaron:
IWM, XLK, XLE and XLV sold in full as market orders at the open. All four
gate-checked clean, filled complete, zero fees, no partials — IWM $6.19 @
303.7081, XLK $5.76 @ 191.0601, XLE $5.41 @ 62.2401, XLV $5.41 @ 166.2828.

Two things worth keeping:

- **The session verified the live broker book before selling rather than
  trusting `positions.json`.** Quantities matched Sunday's table exactly
  (IWM 0.020380, XLK 0.030134, XLE 0.086913, XLV 0.032554) and no fifth ETF
  existed, which is what let it assert "no ETFs remain" as a fact rather than a
  hope. This is the 08-16 stale-snapshot lesson being applied one day later.
- **The proceeds (~$22.80) were not redeployed**, so the book ended the week at
  8 names and 41% cash against a 14-name target with six unowned (LITE, WDC,
  FTNT, LRCX, NBIS, AMAT). Several are in cooldown. No `PORTFOLIO` decision was
  journalled explaining the non-deployment — the letter states the cash position
  as fact and does not invent a reason for it.

`config/etf_universe.csv` stays: the residual-momentum tilt regresses on the 11
SPDR sectors, and the order-gate whitelist still needs held ETFs to be nameable.
ETFs are also still *scored* — skipping the scoring rather than the selection
would leave a live position with no computable stop.


## 2026-08-16 — the account snapshot went stale and nothing could repair it

**Found by a rehearsal, not by a check.** A mock run of the newsletter agent read
`positions[]` and said, unprompted, that the session decisions recorded no AMAT
at the broker while the snapshot listed it. It was right and I was wrong: I had
spent the afternoon verifying "13 positions, all protected" against a file that
was two days old.

`research_store/rh/positions.json` was written 08-14 10:02 ET. The journal:

```
08-14 14:02Z  BUY  AMAT filled   (the 10:00 procedural loop, deleted that day)
08-14 14:41Z  SELL AMAT filled   (the 10:35 session exited it, 39 min later)
```

Nothing rewrote it after the sell. Robinhood held **12** positions; the file said
**13**. For two days the stop watcher tracked a phantom, `brief()` showed the
agent a book it did not own, the valuation was wrong, and tonight's investor
letter would have reported a position that had been sold.

**Root cause: deleting the fast loop removed the only writer.** `record_fills()`
journalled executions and nothing else. `prompts/exit.md` required reconciliation
only for monitor-triggered exits. A session-initiated sell left no trace in the
snapshot — a consequence of the 08-14 retirement that nobody noticed at the time.

**And it could not be repaired.** The snapshot write was gated behind
`if filled_orders:`, so correcting the file required a trade. Handing it the true
broker state with no fills returned `{"ok":true,"snapshot":null}` and did nothing.

**⛔ THE PUBLISHER COERCED WHERE ITS DOCSTRING SAID "VALIDATE".** Enumerated by
the independent reviewer, case by case: an EMPTY list wrote `positions: {}` and
returned ok — erasing every holding; a TRUNCATED list (three of twelve) published
the three and deleted the other nine; malformed rows were silently skipped; a
missing average cost became 0.0; NaN and Infinity passed into the file. All
returned success. The word was in the docstring and the behaviour was not, which
is the defect class this repo keeps paying for.

**Fixed:**

- `refresh_broker_snapshot()` — the no-fill repair path that did not exist.
- The publisher REJECTS rather than coerces; the previous file survives
  byte-identical on every refusal. A partial book is more dangerous than a stale
  one — stale is detectable and WAS detected; partial looks current, carries a
  fresh timestamp, and silently unprotects whatever it dropped.
- **Completeness is now proven, not inferred.** A truncated page is well-formed,
  so no amount of row validation catches it. The publisher requires a
  cursor-linked pagination transcript and verifies the chain itself. A caller can
  assert `exhausted:true` and still be refused when the transcript contradicts it.
  The empty-book case was already guarded on exactly this reasoning ("the read
  came back empty" and "the account is flat" are the same bytes); it had been
  applied to 0-of-n and nothing else.
- Charter: EVERY session refreshes, trading or not.
- Staleness surfaces in health as `positions_snapshot`.

Reconciled live from the broker: 12 positions, AMAT gone, all consumers agree.

**⚠️ I CORRUPTED THE LIVE SNAPSHOT WHILE TESTING THE FIX.** My adversarial run
included a VALID transcript as a control and I pointed it at the real file rather
than a tempdir, so two fake positions overwrote the real twelve for ~3 minutes.
Sunday, market closed, no harm — on a weekday ten positions would have lost stop
coverage. The system behaved exactly as designed; I aimed it at production. The
reviewer's own selftests redirect `RH_POSITIONS` to a tempdir for this reason.

**And I then wrote the snapshot without an `account_number`**, because the payload
I hand-assembled omitted it — which left the new account-identity guard inert,
since it compares against the value in the snapshot. Restored and verified by
attempting to publish a foreign account (refused).

**The pattern, again:** every failure here was silent. The snapshot did not error,
it just stopped moving. Nothing in the test suite could have caught it — a
liveness gap is not a unit-test failure. What found it was a rehearsal of the
real thing and a second model reading the code.


## 2026-08-14 — the loop moved before the session and reversed it; it is gone

**The failure, in three days of the journal.** 08-12: a session exited AMAT to
be flat into earnings. 08-13 10:02: the 10:00 fast loop rebought it @556.18.
12:20: the session sold it back @554.81 and wrote *"THE POSITION WAS NOT MINE —
the deterministic loop opened it at 10:01 today, reinstating the exact exposure
the 08-12 session paid a spread to remove"*, plus a warning that it would recur.
AMAT reported that night and gapped down. 08-14 10:02: the loop bought it
**again**, 4.4% below the prior close, into the gap.

LITE was queued by the loop on 08-10, 08-11 and 08-13 and stopped every time
only by a lack of settled cash — *"never by a decision"*, in a `rule_out` a
session wrote knowing the loop could not read it.

**Root cause is ordering, not any missing rule.** The loop placed at 10:00; the
open session reasoned at 10:35. The 10:35 slot existed *specifically* to clear
the loop (OPSLOG 2026-08-12). So the procedural component got first move every
day and the judgment layer spent its session undoing it.

**What was done:**

1. **`rule_out()` now binds** (`59c9626`). It recorded the right fact and gated
   nothing — its own docstring said *"Nothing here gates an order"*. Now read by
   the fast loop's plan and by the PreToolUse gate. Buys only; a rule-out can
   never strand an exit.
2. **The fast loop is retired** (`c765578`) — timer disabled, units, runner and
   `prompts/fast_loop.md` deleted, `health`/`repo_checks` updated. `repo_checks`
   caught the drift within a minute of the timer being disabled, which is the
   check working.
3. **The regime stopped gating the selection** (`3c1481c`). It is computed,
   recorded and reported; what it means is the session's call.
4. **`scripts/fast_loop.py` deleted**, and with it — unnoticed by me — `no_chase`,
   the `[reentry]` knife-guard, the order-time cooldown check, and the
   **automatic drawdown halt**, which it was the only caller of. The halt is
   restored at the order gate via a write-free `governance.drawdown_breach()`
   (`9c8628e`); the others are now the session's judgement and the config says
   so instead of claiming enforcement.
5. **The loop protects what the AGENT holds** (`386d277`), not only what it
   picked. The monitor's base stop comes from a thesis, and one existed only for
   selected names — so a position the agent opened on its own judgement had no
   stop. Held-but-unselected names now get geometry at weight 0.

**⚠️ The independent reviewer caught what the test suite did not.** Codex
returned PROBLEMS, then OUTSTANDING, on work that was green on every selftest
and `repo_checks`. It found the drawdown regression, that `brief()` never
surfaced the recorded regime, that stale instructions could recreate the veto at
the judgment layer, and that my selftest was **vacuous** — it scanned source text
and its own assertion contained the string it searched for, so deleting the real
field still passed. *"It proves syntax spellings, not behavior."* Replaced with a
behavioural test that runs the selection both ways and requires equality.

**Later the same day, the rest of it:**

6. **The order-time cooldown was restored** (`1d7bb24`). market_monitor writes a
   cooldown when a stop fires; TWO readers honoured it and one of them was
   fast_loop.py, so deleting that left only the slow loop's next-rebuild half. A
   session could rebuy a name the monitor stopped minutes earlier.
   `governance.cooldown_until()` is read-only and fails OPEN on a torn file.
7. **The regime guard was made structural** (`1d7bb24`). It now walks `main()`'s
   parse tree: `regime` may only be assigned, recorded and printed. **The guard
   was then PROVEN to fail** — the regression the reviewer described was injected
   deliberately and the selftest caught it. A guard nobody has watched fail is a
   guard nobody has tested, which is how the previous one shipped vacuous.
8. **The protective theses were not actually watched** (`9eab32a`). Item 5 above
   gave held-but-unselected names geometry at `target_weight 0.0` — and the
   monitor's watch set was `target_weight > 0 and t.stop`, so every one of them
   was excluded by the very field chosen to mean "not prescribing this". The
   geometry existed and the protection did not. Four call sites plus
   `agent_env/state.py` all agreed with each other; they were consistently wrong.
   One `in_book()` predicate now distinguishes verdict `hold` (HELD, watch it)
   from `avoid` (R:R-dropped, do not). Live effect: AMAT and XLV were carrying
   unenforced stops.
9. **The orphans were deleted** (`2c84da4`, `602e5d7`): risk_review.py, its
   prompt and runner; `get_targets()` (the last API handing out weights as an
   allocation to fill); dead remedy entries in health_check keyed to SPECS keys
   that no longer exist; and the `rule_out_block` control registered that morning
   with evidence pointing at `fast_loop order_plan` — **the control that exists
   to catch guards pointing at nothing was itself pointing at nothing.**
   `docs/OPERATOR_MANUAL.md` had been telling the operator the risk-review
   overlay was "**armed**, places real trades" for three days after it stopped
   existing.

⚠️ **`risk_review.py --facts` was the ONLY producer** of the earnings_soon /
near_stop / giveback / vol_expansion / ma_break flags in `src/controls.py`.
Nothing consumes them (controls.py has never been wired live), which is why
deleting it was safe — but if it is ever wired those five must be REBUILT, not
re-enabled. There is no script left to put back on cron.

**The lesson that generalises: unit selftests of pure functions do not catch this
class.** Every defect above was a control that read as present and did nothing,
or a component that should not exist. The checks that worked were the ones
comparing two independent sources of truth — `repo_checks` against systemd — and
a different model reading the diff.


## 2026-08-13 — the intraday risk-review overlay was retired into the sessions

Written 2026-08-14, because it never got an entry. The change shipped as
`a007e3f` and its rationale existed **only in that commit message** — so
`CLAUDE.md` went on describing the overlay as live in four places, and a session
the next day (this one) reported the system's state from it and was wrong. That
is the same failure class as everything above: a decision recorded where the next
reader does not look.

The record that justified it: 40 runs → 3 orders intended, and **none at all**
after the sessions went live, because the sessions do the same work with
judgment. `market_monitor` watches every stop continuously through RTH, which
dominates a twice-daily snapshot, and with no extended-hours trading the last
decision that matters is the 15:15 close, not a 15:45 pass. Removing it also
deleted a race: it was one of four unlocked writers to `monitor/overrides.json`.

`deploy/run_risk_review.sh` remains on disk, unused and unscheduled.


## 2026-08-13 — the reviewer's memory cap was killing every run; it stays on the droplet

The first review to survive the PATH fix was **SIGKILLed**, and the new
returncode check caught it as a failure rather than recording a fake verdict:

```
memory: usage 716800kB, limit 716800kB, failcnt 31715
swap:   usage 204800kB, limit 204800kB, failcnt   190
oom-kill:constraint=CONSTRAINT_MEMCG ... task=codex
```

**`CONSTRAINT_MEMCG`, not `CONSTRAINT_NONE`** — it hit its *own* cgroup ceiling
with ~1 GB free on the box. A cgroup cap is private and absolute: it does not
widen when the machine is idle, so this would have died identically on a quiet
droplet. Both ceilings were pinned at 100% and it was still asking (31,715
failed charges), so the job needs more than the 900 MB it was allowed.

Two reasons it is hungrier than it looks: the cap covers **children** (every
`agent_view.py` call spawns a fresh pandas-importing Python inside the same
cgroup — the kill log shows a python child dying first), and cgroup memory
counts **page cache**, so reading the parquet price panel charges against it.

Raised to **1200M / 300M swap**, with a selftest that now enforces a *floor* as
well as the cap's existence — a cap that always kills is indistinguishable from
having no reviewer. The cap itself is not negotiable, only its size: uncapped,
this took the whole droplet down on 2026-08-12 during market hours. The cap is
what turns "the box dies" into "the review dies".

**Why NOT GitHub Actions** — recorded so it is not re-proposed:

- **Minutes are fine.** Measured 54 min/month across 67 runs (claude auto-fix
  42, validate 6, adaptive-tune 4). Adding ~44 review runs/month at ≤600s caps
  the projection at ~275–495 min against a **2,000 min Free-tier allowance for
  private repos** — 14–25%. Both repos are private, so minutes are billed.
- **Auth is the blocker, not minutes.** `codex` here runs on **ChatGPT
  subscription OAuth** (`auth_mode: chatgpt`; `OPENAI_API_KEY` is null; rotating
  id/access/refresh tokens). On Actions it would need an API key — **per-token
  billing, a new metered cost on top of a subscription already paid** — and
  transplanting rotating OAuth tokens into a repo secret breaks on next refresh.
  Exactly the trap CLAUDE.md documents for `ANTHROPIC_API_KEY`, other vendor.

So the reviewer stays on the droplet. The earlier off-box proposal was withdrawn
on this evidence, not on preference.

⚠️ **The cap raise is not yet proven live.** A manual test was deliberately not
run: with an interactive session resident (~510 MB of a 1963 MB box) only 976 MB
was available, and firing a 1200 MB-capped reviewer into that during RTH with the
stop watcher live is the 2026-08-12 shape. Unattended headroom is ~1.5 GB. The
first close-session review is the real proof.


## 2026-08-13 — the reviewer's verdict is disconnected from the agent (OPEN question)

Switched off the same day the reviewer was fixed, and for a better reason than
the bug: **it was never going to teach the agent anything in the form it had.**

The agent is **stateless**. Each session is a fresh context, so a verdict shown
once is one extra input to one session, not learning. The design said the loop
closed — the agent "must answer" the verdict, and its answer is journalled — but
**nothing has ever read that answer back**. `answer_review` decisions have no
direction, so `score_reviews.py` files them as unscoreable and they are never
looked at again. An open loop wearing a closed loop's description.

For a stateless agent, learning cannot live in the agent; it can only live in
the environment. There are three channels and the review used the weakest:

| Channel | Carries between sessions? | Review's use |
| ------- | ------------------------- | ------------ |
| Charter (config-derived, standing) | yes | **none — silent** |
| Brief (per-session facts) | no, one shot | the last verdict |
| Ledger / scorecard | yes, but shown to nobody | answer written, never read |

**And the process was never named.** The charter — the agent's entire standing
account of the game it is playing — contains no mention of being reviewed or
scored (`grep` of `charter.md`, `charter.py`, `mandate.toml`: nothing). So an
anonymous critique arrived with no provenance and no stated purpose, and an
agent resolving that ambiguity **invents** a reason for it. That is uncontrolled
anchoring, and it is the same failure this repo already fixed once in
`render_gate()`: the charter claimed orders were pushed "so a human can veto the
unusual", which was false, and *an agent that believed it had been vetted by a
human would defer to a review that never happened*. A process described wrongly
— or not at all — is worse than one not shown.

**So: the operator is the audience for now.** The reviewer still runs after every
session, still journals, still pushes on DISSENT/SPLIT, and is still scored
against the tape. Only the agent-facing path is off, at
`scripts/session.py:SHOW_REVIEW_TO_AGENT`. The principal is weighing the
reviewer's judgment against the agent's before deciding whether feeding it back
is useful at all.

**This makes `scorecard.json` the primary instrument**, which promotes the
day-keying defect below from a nuisance to the thing that matters.

Corrected three places that would otherwise have stated something false —
including `prompts/review.md`, which told the *reviewer* its verdict would be
read by the agent it was reviewing. Same failure class, other party.

**OPEN — do not treat as settled or quietly re-enable.** Whether to feed the
review back, and in what form, is undecided. The live candidates: show the
standing *behavioural* pattern (a tally of what the agent actually did) rather
than a single opinion, since that is a fact about itself it cannot otherwise
see; withhold outcome hit-rates until n stops being noise; and name the process
in the charter first, since reconnecting without that reintroduces the exact
ambiguity this turned off. Reconnecting is one boolean — the renderer, the
anonymity guard and the mode filter are all kept live and selftested with the
flag forced on, so nothing rots meanwhile.


## 2026-08-13 — a verdict was scored against decisions it never examined

Found while reading the review subsystem end to end, and fixed the same day
because disconnecting the agent (above) makes `scorecard.json` the *only*
instrument for judging reviewer against agent.

`score_reviews.py` joined a verdict to a decision **by calendar date**:

```python
stance_by_day[str(e["ts"])[:10]] = e["stance"]      # last write wins
```

There are **two sessions every weekday**. So the close verdict overwrote the
open one, and then every decision that day was scored against whichever came
last. On 2026-08-12 three verdicts collapsed to one:

```
15:29 AFFIRM    (the parser bug, reading our own echoed template)
15:31 SPLIT     (the correction — the reviewer's real verdict)
19:24 UNPARSED  (the close review, which never ran)
                -> stance_by_day["2026-08-12"] = UNPARSED
```

The real SPLIT was discarded, and four morning decisions it *had* examined were
attributed to a verdict produced by a reviewer that never launched. The number
this file exists to produce was quietly wrong.

**The failure class: a join key coarser than the thing it joins.** A date cannot
address a session when there are two a day. The fix is not a finer timestamp
heuristic — it is to stop inferring. The reviewer is the only party that knows
its own scope, and it already writes that scope to disk, so it now records it:
`codex_review` carries `reviewed`, the list of decision timestamps it actually
read. Two rules resolve overlaps, both selftested:

- **Same scope → latest wins.** A re-run covering exactly what an earlier run
  covered is a *correction*, not a second opinion — which is precisely the
  15:29 → 15:31 sequence above.
- **Different scope → earliest wins.** The close review re-reads the whole day,
  so the morning's decisions appear in its list too; but the open review is the
  one whose verdict was formed about that work, and scoring a decision under
  both would count it twice.

A decision no review claims is **not guessed at**. It is reported as uncovered,
in the JSON and on the console beside the hit rate, because a reviewer hit rate
read without its denominator is how a number from three decisions becomes a
record. All 8 existing decisions predate the `reviewed` field and now read
`0/8 carry a verdict` — an honest gap where there used to be a false
attribution. Scoring starts clean from the next review.

**Also, from the same read-through: a control that was documented and never
existed.** `busy_now()` claimed it "must not run during regular trading hours,
when the stop watcher needs the machine". It never checked the time — only the
session lock and free memory. And it must not: both reviews run inside RTH by
construction (open 10:35 + up to 1800s → review by 11:05; close 15:15 + 900s →
by 15:30, against an RTH of 09:30–16:00), so a trading-hours gate would mean no
reviews at all. The box is protected by the ordering, the kernel MemoryMax, the
nice/ionice deprioritisation and the 800MB floor — not by a clock. Docstring
corrected to say so, and a selftest now fails if anything clock-shaped appears
in that function, because the stale aspiration was an open invitation to
"restore" a check that would have silently switched the reviewer off. That is
the same defect class as the two above: a control that is present and does
nothing, and a job that stops running without saying so.


## 2026-08-13 — the independent reviewer had never once run under cron

Checked before the open. The book, the services and the scheduled jobs were
fine (health `11/11`, the two stale-service flags from 08-12 healed once
`agentic-monitor` and `agentic-dashboard` were restarted). **The reviewer was
not.** Its one and only cron run — after the 08-12 close session — produced a
59-byte artifact reading:

```
ionice: failed to execute codex: No such file or directory
```

**`codex` was never on cron's PATH.** It installs to `/root/.local/bin`, which
`crontab.template` omitted. Every interactive test passed, because a login shell
has that dir — the two real verdicts in the journal (08-12 `AFFIRM`, then the
corrected `SPLIT`) were both hand-run. `claude` survived the identical gap by
the accident of living in `/usr/bin`. Fixed by appending `/root/.local/bin` to
PATH in the live crontab **and** the template. Verify a dependency the way cron
sees it, never with `which` in your own shell:

```
env -i PATH=<the crontab PATH> command -v codex
```

**The failure class, which is the part worth keeping.** The missing binary was
a one-line environment bug. What made it *invisible* is that
`review_session.py` never read `proc.returncode`, so two different events were
recorded identically:

- the reviewer **ran** and its prose lacked the block → `UNPARSED`, a real (if
  useless) opinion, worth journalling and scoring against;
- the reviewer **never launched** → there is no opinion at all.

Collapsed into one, the second wrote a `codex_review` event, overwrote
`latest.json`, and handed `score_reviews.py` a day's work marked *reviewed*.
The 08-12 scorecard duly reports **8 decisions examined by a reviewer that never
saw them** — a subsystem reporting clean while wholly dead, the same shape as
the capital-flow ETF nulls (08-28) and the signal panel that had never run
(07-24). `run_session.sh` runs the review behind `|| true` so it can never fail
a trading run, which is right, and is exactly why nothing else could notice.

Now: a non-zero exit with no verdict block is a **failure, not a stance** — it
journals nothing, leaves the last real verdict standing, and pushes the operator
once. A reviewer that exits badly having *said its piece* is still honoured;
that is the reviewer's opinion and the exit code is not. Pinned by three
selftest cases (never-launched / spoke-then-failed / clean-but-rambled).

**Blast radius: none to the book.** No order, stop or position was touched — the
reviewer cannot trade. `session.last_review()` accepts only
`AFFIRM|DISSENT|SPLIT`, so the garbage verdict resolved to `None` and no
corrupted review reached this morning's brief. The cost was one lost verdict on
the 08-12 close (8 decisions, unrecoverable — the reviewer reads *today's*
journal) and a scorecard row that has to be read as unreviewed.

**Also: the subsystem was undocumented.** Five commits shipped the reviewer live
and neither `CLAUDE.md` nor `docs/` mentioned `review_session.py`,
`score_reviews.py`, `prompts/review.md` or the `codex` dependency — so the file
that is auto-loaded as operating context for every agent working here described
a system without its reviewer in it. Added to the repo layout and setup notes.

**Closed the same day: nothing checked that a review actually happened.** Now
`health.SPECS["review"]` watches the `codex_review` journal event on the same 4-day
window as the other weekday-only jobs. The event is the right artifact precisely
because a failed review journals *nothing* after the fix above — a reviewer that
cannot launch goes silent there, which is the signal. (Not the `reviews/`
directory mtime: that moves even when the run failed, which is how this stayed
invisible in the first place.)

Two things fell out of wiring it, both of which were themselves defects:

- `_newest_journal_event()` read only `at`/`as_of`. Every session-era event —
  `agent_decision`, `codex_review`, `risk_review` — carries `ts` instead, so the
  probe would have returned `None` forever and reported a perfectly healthy
  reviewer as one that had never run: a liveness check that is itself dead. Now
  falls back to `ts`, pinned by a selftest.
- `repo_checks.py` refused the new key until it was mapped to a cron line —
  working exactly as intended. The review has no cron line of its own (it runs
  *inside* `run_session.sh`), so the session's arming is its arming: comment out
  the session and this check correctly reports the reviewer unarmed too.


## 2026-08-12 — the agent sessions go live, and six defects found by review

**The inversion landed.** `scripts/session.py` runs on cron — `open` 10:35,
`close` 15:15 ET weekdays. A session is not handed a procedure: it gets the
charter and decides. The legacy loops are untouched and still run. Orders still
pass the PreToolUse gate (kill switch, order cap, whitelist, live_approved).

Times are collision-avoidance, not preference: 10:35 clears `run_fast_loop.sh`
(10:00, finishes ~10:04); 15:15 lands before `risk_review` (15:45) because both
read-modify-write `monitor/overrides.json` with no shared lock, and the
interleaving was measured **dropping a risk_review protective stop**. No
premarket slot — nothing gates an order to market hours, so a 09:00 session
could queue a market order filling at the opening print.

**Adversarial review found six defects in the runner I had verified myself**,
two critical: a lock timeout did not stop the session (it ran UNLOCKED and
reported ok — `acquire()` returns None on timeout, it does not raise, so the
`except TimeoutError` was dead code), and no session could start at all
(`--allowedTools` is variadic and ate the brief as a 47th tool name). Both lived
in the spawn path, which `--selftest` and `--dry-run` never touch.

**Three defects in the EXISTING system, unrelated to the cutover:**
- The exit executor had **never been gated**. `market_monitor` spawned it with
  no `--settings`, so every stop-triggered market sell this system has placed
  went out with the order gate not bound.
- `set_levels` accepted a stop **above** the live price and reported it
  enforced. The armed monitor reads that as an instant breach and sells the
  whole position at market — a full liquidation reached without any order being
  placed, which is how it bypassed the gate. Demonstrated on AMD: 474.00 stop
  against a 473.65 mark.
- Agent-set levels **never expired** (`read_overrides` prunes on a "9999"
  default), so one stop armed the monitor forever. Now 5 trading days.

**Also:** the panel recorded MNST's 2:1 split as a real −50.2% return
(`scripts/adjust_splits.py` — 21 candidates, 1 confirmed, 20 real crashes left
alone); the liquidity floor measured Alpaca IEX against a consolidated-tape
threshold **and was 26 days stale**; `wakes.due()` had no caller anywhere;
regime-off sold the book rather than pausing it; and `rebalance = "weekly"` was
read by nothing while the loop rotated nightly.

**Two self-inflicted, same day:** a latency probe fired three real "UNUSUAL BUY:
AMAT" pushes for an order that never existed (no trade occurred; `GATE_PROBE=1`
now severs delivery), and `touch research_store/SHADOW` — believed to sandbox
the new sessions — actually disarmed order placement for the LIVE fast loop and
the armed risk-review overlay. Removed at 08:07, two hours before either ran.

**The pattern worth keeping:** every defect above is the same class — a control
that reads as present and does nothing. Three tests found this session had never
run at all (`slow_loop`, `moomoo/prices`, and one I wrote that morning which
passed identically with the code reverted).

## 2026-08-12 — the trade record cannot yet judge the trade geometry

Written down because it was nearly written into the CHARTER instead, where it
would have made the agent hesitant without changing a single decision.

**The question.** The inherited geometry puts the first target ~5.5 sigma out on
a hold of a few days (`stop_atr_mult = 2.5`, `target_r_mults = [2.2, 4.0]`). It
guarantees reward:risk >= 2 BY CONSTRUCTION -- and the ratio is satisfiable in
exactly one easy way, by moving the target further out. Nothing ever asked
whether price could reach it in the time a position is actually held. The
validation gate that "checked" this was tautological and was deleted (a99b052).

**Why the record cannot answer it.** All 18 closed outcomes are confounded:

  regime_off  11   avg -7.65%, worst -18.16%  -- ALL single names, one timestamp
                   2026-07-27T14:05:34. Static liquidation, being removed.
  rebalanced   6   avg +1.22%, best  +3.19%   -- ALL ETF sleeve. Weekly rotation,
                   small by construction.
  stop         1   -24.09% (WDC)              -- entry_price 542.78 -> 412.01 in
                   ONE day. An earnings gap. `earnings_soon` was inert in
                   production until 2026-08-09, three days AFTER this close.

So: 0 targets ever hit, and 1 stop ever hit -- on a position that gapped through
it overnight on an event the system could not see. There is no clean observation
of the geometry anywhere in the record.

**The trap this creates.** The aggregate reads as a textbook failure -- 33% win
rate, avg win +1.70%, avg loss -9.26%, payoff ratio 0.18, best trade ever
+3.19%. It is tempting (I did it, mid-conversation, with numbers attached) to
diagnose "cuts winners, runs losers" and reach for the override mechanism as the
cause. The pairing refutes it: the small wins are ETF rotations and the large
losses are one liquidation event. NO target was ever pulled in and NO stop was
ever tightened into a winner. The pattern is produced entirely by two static
mechanisms operating on two different sleeves.

**What is nonetheless true by inspection, not by evidence.** `apply_overrides`
is a one-way ratchet: a stop may only be RAISED and a target may only be LOWERED.
Both permitted directions shorten the trade; both blocked directions let it run.
Raising a target increases no loss risk at all -- the stop is unchanged -- so
blocking it has no safety justification. Whether to open that direction is a
design decision on principle, NOT one this record supports either way. Left open
deliberately.

**When it becomes answerable.** Only after the regime liquidation is removed and
the agent is setting its own levels: until then every position is closed by
something else before its geometry can be tested. Outcome recording now covers
all seven close paths (62a280c), so the evidence will accumulate from here.

**Charter consequence.** The charter states only what changes an action: use
`terrain()`, and do not inherit a target without checking it, because none has
ever been reached. The confound analysis, the payoff ratio and the "we cannot
yet tell" live here. A trader does not need a methodology note; given this
model's documented bias toward overcaution, one would cost more than it bought.


## 2026-08-11 — the safety gate stopped being optional: a PreToolUse hook in front of every order

`check_order()` (`src/agent_env/`) was **advisory**. It is a tool the agent MAY call, and
nothing compelled it to: an agent that skipped the step, or mis-read the verdict, could call
`mcp__robinhood-trading__place_equity_order` directly and move real money with no gate
consulted at all. The gate's correctness was never the problem — its *reachability* was.

`scripts/hooks/pretooluse_order_gate.py` moves the gate into the **harness**. Claude Code runs
a `PreToolUse` hook before the tool call is dispatched, and its `deny` is not something the
model can forget or argue with. Wired in **`deploy/loop_settings.json`** (the cron loops'
`--settings` file) — deliberately NOT in `.claude/settings.json`, so an interactive human
session is not gated by it.

What it enforces, using only the **write-free** governance functions: kill switch → shadow
mode → side sanity → `live_approved` / `HALT_ENTRIES` / `max_order_pct` / whitelist via
`governance.vet_plan`. It does **not** call `gates()`: `gates()` writes
`research_store/governance/state.json` through `update_peak`, so a hook calling it would
ratchet a live drawdown gate as a side effect of merely being consulted. The drawdown halt is
therefore still the fast loop's job, not the hook's.

**A SELL is refused by nothing but the kill switch.** Stops here are software-only
(`scripts/market_monitor.py` IS the stop), so a gate that blocks a sell does not pause trading
— it strips an open position of its only protection. `live_approved`, `HALT_ENTRIES`, the
order cap and the whitelist are all BUY-only in the hook, matching `governance.vet_plan`. A
full exit also arrives as a *share quantity* (RH rejects a dollar-notional order that consumes
a whole position: `EQUITY_DOLLAR_BASED_SELL_ALL_ERROR`), which the hook handles rather than
choking on. The mirror case — a market BUY priced in shares — cannot be sized without a quote,
and the hook refuses it rather than guessing.

**Shadow mode: `touch research_store/SHADOW`** (same one-file pattern as `HALT`) makes the gate
refuse EVERY order while the loop still runs end to end — the switch for a phased rollout, and
a way to exercise a new procedure with placement mechanically impossible. `rm` it to go live.

Two properties it was built to hold: it **fails closed** (any exception — unparseable payload,
unreadable config, a bug — prints a `deny` naming the exception type; a hook that crashes
prints nothing, and nothing reads as "no opinion" and lets the order through), and it stays
**fast** — it may read small JSON and config only, never `research_store/prices/*.parquet` and
never the signal. Measured over 20 cold starts including imports: **mean 0.104s** (min 0.072s,
max 0.180s).

Not covered: order *cancellation*, option orders (denied outright in the loop settings), and
any session not started with `--settings deploy/loop_settings.json`.

---

## 2026-08-10 — the stop-loss prompt had an unpermittable step; the permission check had no on-box runner

Two follow-ups to `87260f7` (path-scoped tool permissions). Same failure class in both:
a guardrail that exists but is not reachable from where it is needed.

### 1. `prompts/exit.md` step 7c could not be granted

87260f7 dropped the `Bash(.venv/bin/python *)` wildcard and fixed `fast_loop.md` step 9b
(the inline snippet became `scripts/record_rotation_outcome.py`). **It missed the identical
step in `exit.md`** — step 7c told the agent to compose "a short python snippet run with
`.venv/bin/python`". No exact allow rule can cover a snippet the agent composes, so that
step now stalls on an approval no headless run can give. `exit.md` is not a weekly job: it
is fired by `scripts/market_monitor.py::run_executor()` when a stop breaks, on a 300s
wall-clock timeout.

Consequence is bounded but real: 7c is bookkeeping, and the sale (step 4) and its journal
entry (step 6) both come first, so **no exit would have failed to sell**. What would have
been lost is the outcome LABEL — and 7c is the *only* producer of `hit_stop: true` outcomes,
which is the sole input to the live half of the `stop_atr_mult` adaptive dial (see
2026-08-04). A dial that already reports `live_n: 0` would have kept reporting it forever,
now for a second, different reason. There are still zero live stop-outs in the ledger, so
nothing has been lost yet — this was caught before its first fire.

Fix: `scripts/record_exit_outcome.py`, same pattern as `record_rotation_outcome.py` —
reads `research_store/rh/exit_closes.json`, exact argument-free command, exact allow rule.
It also **removes stop/targets/as_of from the agent's hands**: the old snippet had the agent
copy the levels out of `current.json` into the arithmetic, and `hit_stop` is computed by
comparing the fill against them. The script reads the thesis itself (current book, falling
back to the last HELD archived thesis), so the levels the label is scored against cannot be
mistyped by the thing being scored. Unanchored symbols are reported, never given an invented
parent. Idempotent — the monitor may kill and retry the executor.

`record_rotation_outcome.py` was also never registered in `deploy/run_selftests.sh`; both are
now.

### 2. `check_settings_no_exec_wildcard` ran nowhere that mattered

87260f7 added check 8 to `src/repo_checks.py` specifically because Claude Code writes
"don't ask again" grants back into `.claude/settings.local.json` by itself, so an
arbitrary-execution grant WILL come back. But nothing on the box ran `repo_checks` —
`grep -cE "repo_checks|run_selftests" deploy/crontab.template` returned 0 — and the only
scheduled runner is `.github/workflows/validate.yml`, **off-box, against a git checkout,
where `settings.local.json` is git-ignored and therefore does not exist**. The check was
structurally blind to the exact file it was written for, and CI would have reported clean
forever.

`scripts/health_check.py` (daily 08:00, already pushing to the OPS ntfy topic and filing a
deduped GitHub issue under `--open-issue`) now also runs `repo_checks.checks(REPO)` and
reports each finding as its own `Check` row, keyed by a digest of the message so the
fire-once contract works per-finding: a new drift is audible while an older one is still
flagged, and a resolved one clears via `diff()`'s retired-check clause. Rows are appended to
`health.checks()` rather than folded into it, so the dashboard still renders jobs only.

Publishing safety is unchanged: `repo_checks` messages are built under its own rule
(file:line + fixed prose + its own constants + `_publishable()` tokens) and are passed
through verbatim — nothing here re-interpolates scanned file content. Verified: the
exec-wildcard finding names the file, the entry index and the interpreter, and explicitly
withholds the rule text.

---

## 2026-08-04 — the adaptive layer's live half was a stub; now wired

Routine system review. Everything scheduled is running (all 8 `health.checks()` green,
`repo_checks` PASS, moomoo feed clean at 168/168 bars). The finding was in the adaptive
dial, and it is the same failure class as the signal panel that had never run.

### What was wrong

`scripts/tune_stop.py::_live_stop_samples()` **hard-returned `np.zeros(...)`** with the
comment *"reserved for realized_history join; live_n=0 first cut"*. Every weekly proposal
since 2026-07-23 has reported `live_n: 0`. That was written as a placeholder to be filled
in "once live outcomes exist" — but it was **unconditional**, so it could never turn itself
on. The promotion standard recorded at build time says *don't promote unless `live_n` is
not ~0*; taken together, the tuner's own graduation criterion was **unsatisfiable by
construction**. It would have gone on emitting confident, clean-looking proposals built
purely on survivorship-biased replay, forever.

**It has cost nothing so far.** There have been **zero live stop-outs in the entire
history** — all 14 ledger outcomes are `rebalanced`/`regime_off` with `hit_stop: false`.
A perfect implementation would also have contributed zero samples. The bug was a landmine,
not an active loss: it would have first bitten on the first real stop-out, silently.

### The join

Live stop-outs now fold into the grid bucket for the multiple that was **in force for that
decision**, recovered per-decision as `(entry - stop) / (entry * sigma)` from the archived
thesis rather than read from today's config — so samples stay correctly bucketed across a
promotion that moves the dial. Verified exact against all 14 live theses (2.5 ± 0.0008).
Objective is `realized_R = pnl_pct / risk_pct`, the same mean-realized-R the replay half
optimises. Only `hit_stop` outcomes contribute; a rotation or regime-flip exit says nothing
about where the stop belongs. A live sample lands **only** in its own bucket — it carries no
counterfactual, which is precisely what the replay half is for.

### The second, quieter bug

`deploy/backup_ledger.sh` ships `archive/*.json` to the mirror, but the CI workflow only
copied `journal.jsonl` into the working tree. The archive holds the `entry_zone`/`stop`/
`sigma` the join needs — so the join would have been **dead on arrival off-box** even once
implemented, and dead in the same silent way. Workflow now copies the archive too.

### Making the silence impossible to repeat

The artifact now reports `live_stopouts_seen` (what the ledger holds) alongside `live_n`
(what the join used), plus `live_join_healthy`. Equal = healthy; `seen > live_n` means the
archive is missing or mismatched and the live half is being starved. A future starvation
is now visible in the proposal itself rather than hiding behind a plausible `live_n: 0`.

### Verification

TDD throughout — test written first, failure witnessed (`NameError`), then implementation.
`--selftest` covers bucketing, realized-R, empty-ledger, and the drift count on
hand-computed vectors. End-to-end on a **real** archived thesis (`MU:2026-08-03`) with a
synthetic stop-out at exactly the stop price: lands in bucket 2.5, realized R = −1.0000,
`live_n` 0→1. Full tuner re-run reproduces the 2026-08-03 CI numbers **exactly**
(`p_better` 0.6602, `replay_n` 13040, identical posterior), confirming the change is
behaviour-neutral while there is nothing live to fold in.

**Dial state unchanged:** `stop_atr_mult` is still 2.5. No proposal has ever been promoted;
`config/strategy.adaptive.toml` does not exist. The latest proposal retains the incumbent —
challenger 2.0 leads but at `p_better=0.66`, below the 0.90 gate. The gate is working.

---

## 2026-07-30 (later) — the two deferred follow-ups, both closed

Same evening as the outage entry below. Aaron: "just fix so it runs correctly."

### 1. `risk_review.py --selftest` was a decayed fixture, not a live bug

The `KeyError: 'NVDA'` that `90123ce` flagged as needing its own fix turned out to be
**test-data rot, not a defect in the trading logic.** Every `expires` in the selftest
was pinned to the literal `"2026-07-18"`. `read_overrides`/`read_intents` prune against
the real `date.today()`, so on **2026-07-19** the fixture began pruning itself before
the assertion could read it. The production pruning was correct the whole time.

Nothing in the armed order path was broken — but the selftest guarding it had been red
for 11 days, which is its own kind of broken.

**The subtler damage.** The last assertion in that block guards the ships-safe property
`armed=False must write NO override file`, and it read:

```python
assert read_overrides(path=...) == {}
```

Once the fixture date went stale, pruning returned `{}` **whether or not the file had
been wrongly written** — so the property silently stopped being tested. A decayed date
turned a real safety assertion vacuous. It now asserts `not o4.exists()`, which pruning
cannot fake. Fixture dates are computed relative to today (`LIVE`/`DEAD`), so this
cannot rot again.

### 2. Health checks could not see a job that runs and stalls

Root cause of the blindness described below: `fast_loop` and `risk_review` were keyed to
**log mtime**, and a blocked run still writes its log.

Both now key to the artifact the job exists to *produce* — `order_plan.json` and
`risk_review_facts.json`. `fast_loop.py` rewrites its plan on every run by design (it
must never leave a stale plan for the placement agent), so an unchanged mtime genuinely
means the work did not happen.

**Staleness alone was still too slow.** At the moment Aaron noticed by hand the outage
was 2.4d old, and the 4d window (needed for weekend/holiday gaps) still read `ok` — it
would not have fired until ~Aug 2. So the sharper signal was added: **log fresh + output
stale = `blocked`**. On a healthy run both timestamps move together within seconds, so
the gap needs no modelling of weekends, holidays or DST, and it fires after **one** bad
run instead of waiting out a multi-day window. Verified against the real fingerprint
still on disk:

```
--> Fast loop (execution)  RAN 9h ago but did not finish — output 2.4d old
```

A thrown kill-switch suppresses both (status `unknown`, never alerting) — the operator
chose that, and nagging daily about it is exactly the cry-wolf noise this module exists
to avoid.

### 3. Two smaller things found on the way

- **Orphaned flags leaked forever.** `health_check.diff()` derived `healed` only from
  current rows, so a flag for a *deleted* check could never match and never clear. That
  is why `schwab_token` sat in `health_state.json` from Jul 28 to Jul 30 printing "1
  still unresolved" for a check removed with the adapter. Not inert: fire-once keys off
  `flagged`, so the leak would also permanently mute any future check of the same name.
  Retired checks are now dropped.
- **`blocked` needed its own remedy.** "Check logs/fast.log" is the right advice for a
  stale job but useless for a stalled one — it points you at a fresh log without saying
  what to look for. The blocked remedy names the actual cause: a gated command waiting
  on an approval headless cron can never give.

Dashboard renders `blocked` red (an actively-failing job outranks a merely quiet one).
**18/18 selftests pass on both 3.10 and 3.12; `repo_checks` clean.**

The 08:00 check was run by hand once tonight so the fire-once flag is already set —
otherwise tomorrow's cron would have filed a `bug`+`auto-fix` issue and spawned an agent
against an already-fixed problem. `claude.yml` is armed; that matters now.

---

## 2026-07-30 — fast loop + risk review were dark for two sessions: an allowlist the prompt rewrite outran

**Aaron noticed before any alert did** — "haven't heard anything in two days." He was
right, and the reason nothing alerted is the more interesting half of this entry.

**Root cause.** `90123ce` (2026-07-29 **09:50 EDT**) rewrote both agent prompts to call
the moomoo-capable interpreter by absolute path — `/usr/bin/python3 scripts/fast_loop.py`,
`/usr/bin/python3 scripts/risk_review.py --facts|--apply`. Correct change; the moomoo SDK
is not in `.venv`. But the permission allowlist was not moved with it. Every entry was
bare-name or venv-scoped (`Bash(python3 scripts/fast_loop.py)`, `Bash(.venv/bin/python *)`)
and **none matched an absolute path**. Under headless `claude -p` there is no one to
approve, so both jobs stopped at their first gated command.

The fast loop fired at **10:00 EDT the same morning — ten minutes after the commit.** It
halted at step 5 with `Run halted at step 5, pending approval. Nothing placed.` Risk review
halted at step 2 across four runs (Jul 29 + Jul 30, 12:00 and 15:45).

**Neither alerting path could see it.** This is the part worth remembering:

- `deploy/alert.sh` traps a **non-zero exit**. Claude exits **0** — it successfully wrote a
  message explaining that it was blocked. A well-behaved agent reporting its own blockage
  looks exactly like a clean run to the ERR trap.
- `src/health.py` measures **log mtime** (`"fast_loop": _mtime("logs/fast.log")`). A blocked
  run still writes to its log. Live during the outage it reported
  `OK Fast loop (execution) last ran 9h ago` — **7/8 healthy**. The check is structurally
  incapable of catching a job that runs, logs, and accomplishes nothing.

The health model is "did the job leave evidence it ran." That was the right lesson from the
signal panel that never ran (2026-07-24), but it does not cover a job that runs and *halts*.
The distinguishing artifact already exists and was visibly stale throughout:
`order_plan.json` (Jul 28 10:00) and `risk_review_facts.json` (Jul 28 15:45). **Deferred to
its own pass** — Aaron's call, root cause first.

**Consequences while dark.**

- The book flipped **XLI → XLV** on Jul 28 evening. The Jul 29 10:00 fast loop was the run
  that would have executed it, and is the run that got blocked. Rotation still pending.
- **XLI is held but in neither risk system.** The monitor stop-watches book∩held, and the
  risk review does the same — today's regenerated facts cover **3 positions (IWM, XLK, XLE)**,
  not 4. An off-book holding is invisible to both by design, because normally the fast loop
  closes it the same session. Here the window is open-ended. Worth its own thought: the
  assumption "off-book positions are transient" is load-bearing and undocumented.

**Fix.** Three allowlist entries in `.claude/settings.json` for the absolute-path forms.
Verified by hand after applying: `risk_review.py --facts` ran clean under system python3
(OpenD connected and closed cleanly — no non-daemon thread hang), writing fresh facts at
23:17:55Z. All three covered names carry **no flags**, 3.0–4.0% above stop; the XLK
watch-note open since Jul 28 resolves as *firmed* (+4.2%, giveback 0.21%). XLI rotation left
to the normal 10:00 ET fast loop rather than an out-of-band order.

**Standing flag, not addressed here.** `scripts/risk_review.py --selftest` fails
(`KeyError 'NVDA'`) — pre-existing, called out in `90123ce`'s own message, confirmed there by
stashing. This job is **armed and places real orders** with a red selftest. Needs its own fix.

**The generalisable lesson:** a permission allowlist is a coupling to the *exact command
string*, and prompts are code. Changing how a script is invoked is an interface change even
though nothing in `src/` moved. Grep the allowlists whenever a prompt's command line changes.

---

## 2026-07-29 — Schwab removed; moomoo is the market-data feed

**Aaron's call, and the right one.** Schwab's 7-day refresh token was the only
recurring human chore in the whole system, and the momentum signal consumed nothing
from Schwab but daily closes. Everything else it nominally provided —
fundamentals, options, movers — had no consumer. The survivorship-free backtest
already ran on Alpaca. So the feed was carrying a weekly maintenance burden for one
column of data.

Corroborating evidence found mid-migration: the monitor journal shows the
Schwab-backed stop watcher throwing `401 Unauthorized` on **2026-07-28** — the
stop-loss watcher had already gone blind at least once on that feed.

**Equivalence proved BEFORE switching.** Across the full 168-ticker universe,
**166 matched the cached Schwab close exactly** and two by <0.15% (TER 0.13%,
SPCX 0.07% — a persistent per-name close convention, not noise; TER showed the
same 0.13% on the independent history path). `momentum.compute` on the old vs new
panel is **bit-identical**: max |delta| 0.0 on R/sigma/trend/score/rank, same 111
eligible names in the same rank order.

**Three findings that shaped the design:**

- **`AuType.NONE`, not `QFQ`.** The ~10y panel on disk is Schwab **unadjusted**, so
  the adjustment mode must agree or the splice date gets a fake overnight return.
  QFQ (dividend-adjusted) drifts to 0.2569%; HFQ is wildly off (NVDA 48,020%). The
  trap: only names whose ex-dividend date fell *inside* the compared window
  differed, so QFQ passed a 10-name spot check. Don't "fix" it back.
- **`request_history_kline` is capped at 100 DISTINCT stocks, account-wide.** Hit
  live at exactly `stock: 100/100` with 98 of 168 names unfetched. A full re-pull of
  this universe is therefore **impossible** — which also makes the deep panel
  **non-regenerable**. Backed up to `research_store/prices/backup/`.
- **`get_market_snapshot` is unmetered and carries a full daily OHLC bar.** 400
  codes/call, so the whole universe is ONE call in ~0.2s at zero quota. That is now
  the daily-append path; history is backfill-only behind `--backfill`.

**Two real bugs introduced and fixed during the switch** (both absent from the
Schwab path, which is why they were easy to miss):

- **Non-daemon threads blocked interpreter shutdown.** `OpenQuoteContext` spawns a
  `network_manager` poll thread and a `callback_executor`, neither a daemon. The
  monitor's `--once` path never closed the context, so the process finished its work
  and then hung forever in `threading._shutdown` — exiting only on SIGKILL and
  tripping the cron ERR trap on a pass that had *succeeded*. Also fixed on the
  `sys.exit(1)` feed-down path (systemd never got the exit it waits on to restart)
  and in the rebuild loop, which leaked a context + two threads + a socket on every
  failure tick — an OOM path on a ~2GB box.
- **`_is_transient` missed `"timed out"`.** moomoo emits BOTH spellings —
  `PacketErr.Timeout` from the snapshot call, `Get Historical Candlestick request
  timed out.` from the history call. Matching only `"timeout"` classified a
  retryable history timeout as a dead ticker. `live_quotes` now also retries
  transients in place rather than escalating: a `PacketErr.Timeout` appeared within
  a minute of cutover, and in the monitor a raise costs a full context rebuild.

**Moved to `/usr/bin/python3`** (moomoo SDK is not in `.venv`): `fetch_prices`,
`market_monitor` (+ `run_monitor.sh`, service restarted and verified polling),
`fast_loop`, `risk_review`. `prompts/fast_loop.md` and `prompts/risk_review.md`
updated — running `fast_loop.py` under `.venv` would fail the moomoo import and,
because `no_chase` is deliberately fail-open, silently skip the chase guard that
cost real money on 2026-07-23.

**Also rerouted:** `risk_review._gather_highs` now reads `highs.parquet` (already on
disk, no API call) and folds in today's session high from a live snapshot;
`_gather_vix` and `slow_loop.fetch_vix` now use FRED `VIXCLS`, which was already the
documented fallback.

**Deleted:** `src/adapters/schwab/`, `schwab_auth.py`, `schwab_status.py`,
`schwab_finish_auth.py`, `schwab_scope*.py`, the `schwab_token` health check and its
phone remedy, `schwabdev` from requirements, `SCHWAB_*` from `.env.example`.
`secrets/tokens.db` and the live `.env` keys are left on the box, inert — remove at
leisure.

⚠️ **Unrelated pre-existing failure found:** `scripts/risk_review.py --selftest`
fails (`KeyError: 'NVDA'` in the trail-up assertion) on **both** runtimes and on the
**unmodified** file — confirmed by stashing. Not caused by this migration, but
risk_review is armed and places real trades, so it needs its own fix.

---

## 2026-07-29 — `schwab_auth.py` printed "✅ Auth complete" without re-authing

**The weekly Schwab re-auth was a no-op unless the token was already inside its
last hour.** Aaron ran `.venv/bin/python scripts/schwab_auth.py`, got the ✅, and
`refresh_token_issued` in `secrets/tokens.db` was still **2026-07-23** — six days
stale, one day from expiry.

**Root cause.** `schwab_auth.py` did nothing but `build_client(interactive_auth=
True)`. But `interactive_auth` only gates a *"does a token file exist"* check —
both branches construct the identical `schwabdev.Client`. `Client.__init__` calls
`tokens.update_tokens()` with no force flags, and that renews the refresh token
**only when `rt_delta < 3630s` (60.5 min)** (`schwabdev/tokens.py:297`). With a
day left it silently took the access-token branch and returned. The script then
printed `✅ Auth complete — token stored. Good for 7 days.` unconditionally. A
re-auth that renewed nothing was byte-identical to one that worked.

Implication: every re-auth run with >1h of headroom has been a no-op since the
repo was written. The ones that appeared to work were the late ones.

**Second decoy.** `secrets/tokens.db` mtime *does* move — every ~30 min, when the
**access** token rotates. So the file looks freshly written on a six-day-old
refresh token. This is the footgun `schwab_status.py` already existed to kill;
the trap is that the auth script itself never checked.

**Same bug in `schwab_finish_auth.py`** — the non-TTY variant. Its `call_on_auth`
hook is only invoked from the refresh path, so with headroom it was never called
and it too printed success having exchanged nothing.

**Fix — force it, then prove it (`src/adapters/schwab/client.py`):**

- `force_reauth(client)` → `update_tokens(force_refresh_token=True)`. The force
  flag is the actual fix; without it schwabdev never prompts.
- `reauth_took(before, after)` → pure. Success is `refresh_token_issued` **moving
  forward**, never a zero exit or an absent exception. schwabdev returns `False`
  (not raises) on a stale auth code, on losing the tokens.db write lock to
  `agentic-monitor`, and logs-and-continues on a short paste.
- `refresh_token_issued()` — the WAL-checkpointed reader, moved here from
  `schwab_status.py` so all four consumers share one source of truth for token
  age (`schwab_status._token_issued` is now an alias; `src/health.py` imports it).
- `force_reauth` maps `EOFError` → `False`, so a no-TTY run prints the actionable
  diagnostic instead of a traceback.
- Both auth scripts now exit **1** with a cause list when the timestamp didn't
  move, and print the new issue + expiry date when it did.

**Verification.** `scripts/schwab_auth.py --selftest` covers the force kwarg (the
regression that started this), the EOF path, and the `reauth_took` truth table.
End-to-end with stdin closed: the script now *reaches* the Schwab authorize
prompt — which it never did before — and on EOF fails loudly, exit 1, token left
intact. **Confirmed live the same day:** Aaron re-authed and `schwab_status.py`
read `issued 2026-07-29T12:08+00:00 … (7.0 days left) … live check: OK` — the
first re-auth ever verified to have actually renewed. `health_check --dry` then
reported `healed: schwab_token`, 9/9 healthy.

**Follow-up: the instructions were also wrong.** Auditing what the system tells
you to run turned up two separate bad commands:

- **Bare `python scripts/schwab_auth.py`** in `README.md` (×2), the
  `schwab_status.py` docstring, its two runtime `⚠️ re-auth` prints, and
  `client.py`'s no-token error. There is **no bare `python` on the box**, and
  `schwabdev` is installed **only** in the `.venv` — so every one of those was a
  guaranteed `ModuleNotFoundError`. Now sourced from single constants,
  `client.REAUTH_CMD` / `STATUS_CMD` (built off `REPO_ROOT`, so they emit a
  copy-pasteable `cd /opt/agentic-trader && .venv/bin/python …`). The phone-alert
  remedy in `health_check.py` mirrors the string as a literal on purpose — that
  daily ops alert must not depend on importing schwabdev to tell you what to run.
- **`schwab_auth.py`'s own docstring recommended Claude Code's `!` prefix** —
  header line, first thing you read. `!` has no interactive stdin, so `input()`
  EOFs and nothing renews. It now says real SSH terminal, and points at the
  two-step `schwab_finish_auth.py` flow for agent shells.

`OPERATOR_MANUAL.md` §1 step 5 no longer says "you should see ✅ Auth complete" —
it says the printed **issue + expiry dates** are the proof, because the bare ✅ was
exactly the thing that lied. README's `!` two-step now describes the new clean
`❌ RE-AUTH DID NOT TAKE` + exit 1 instead of a trailing `EOFError` traceback.

Not fixed (out of scope, worth a sweep): ~25 other scripts' docstrings across the
repo still say `python scripts/<name>.py`. Same footgun, no bare `python` exists.
The GitHub Actions workflows are fine — CI does have `python` on PATH.

---

## 2026-07-28 — `no_chase` was set, documented three times, and read by zero code

**The third "documented but never wired" defect in five days.** `no_chase = true`
sat in `config/strategy.toml`. The rule was written in `docs/STRATEGY.md` §6,
in the `docs/DESIGN.md` enforcement table ("fast loop only buys if live price
in-zone"), and in the `Thesis` dataclass itself (`entry_zone: [low, high];
never chase above`). `grep -rn no_chase --include=*.py` returned **nothing**;
`scripts/fast_loop.py` contained no reference to `entry_zone` at all. Every buy
went in at market at whatever the price was.

**Cost.** Of the 10 names opened 2026-07-23, five filled *above* their entry
zone: LITE +5.0%, KEEL +3.6%, NBIS +3.4%, MU +2.0%, AMAT +1.4%. LITE exited
−18.1% and KEEL −14.1% — between them most of that day's −$4.26. LITE gapped
only +1.4% at the open and then ran another ~4% before the 10:00 ET fast loop
bought it at market.

**Fix — `fast_loop.apply_chase_guard()`, three deliberate choices:**

- **Asymmetric.** Only an over-price blocks. The documented wording ("if live
  price is outside it, skip") would also refuse a name trading *below* its zone.
  That is not a safety rule, it is a bug: on 2026-07-28 it would have skipped
  XLK at **3.8% below** zone. Cheap never blocks. STRATEGY.md corrected.
- **Vol-scaled.** `entry_zone` is a flat ±0.5% band while stops and targets are
  vol-scaled. Measured over 2y × 168 names, a literal ±0.5% gate fills only
  **46%** of the time and blocks **24.7%** of entries *for being cheaper*.
  Ceiling is now `entry_zone[1] × (1 + chase_tol_sigma × sigma)`.
- **Fails open.** Missing quote/sigma/zone passes the order through, logged.
  A guard that halts all buying on a quote hiccup is a new outage; passing
  through is exactly the old behaviour, never worse.

**`chase_tol_sigma = 0.5` measured, not guessed.** Blocks 7.8% of entries across
2y/168 names (open-based proxy; true rate is higher since fills land ~30min
after the open) and blocks exactly LITE and KEEL in the live cohort. 1.0σ blocks
neither — LITE's sigma is 5.69%, so a 1.0σ ceiling sits above the bad fill.

**Live 2026-07-28:** guard evaluated both buys, no fetch failure, nothing
blocked — XLK filled 168.58 vs a 176.54 ceiling, XLI 182.05 vs 185.08. It has
**not yet fired in anger**: regime is off, so only low-vol ETFs near their zones
are being bought. The real test is the next regime-on single-name entry.

**The pattern is now the finding.** Signal panel never ran (07-24), price panel
never saw today (07-27), no_chase never wired (07-28). Three for three: behaviour
documented as working, silently dead, no alert, each found only via downstream
damage. Nothing in this repo checks that a documented rule or a config flag is
actually read by code. `src/repo_checks.py` is the natural home for a
"config key with no reader" check — not yet built.

---

## 2026-07-28 — moomoo: capital flow is silently null for every ETF, and reports 0 gaps

Found while auditing what the moomoo surface actually does. The weekly signal
panel (Sun 20:15) **is** running — `logs/signals.log` and the `signal_panel`
journal events confirm `opend_ok=True`, OpenD active on 127.0.0.1:11111.

But `capflow_bignet_20d` is **null for every ETF** (EEM, IWM, SPY, XLE, XLI,
XLK) while single names get real values — and the event still reports
**`gaps: []`**. The 2026-07-27 panel is 4-for-4 ETFs, i.e. the capital-flow
signal was 100% empty across the whole panel while the run looked clean.

**Mechanism.** `collect_signals._panel_for` records a gap only when
`capital_flow_daily()` returns no rows. `signal_panel.distill_capflow(rows,
market_val)` returns `None` when `rows or market_val` is falsy — and moomoo's
snapshot carries no `total_market_val` for ETFs. So rows arrive, market cap
doesn't, the field nulls, and **no gap is recorded**. Same species as the three
above: a health signal that reads green while carrying nothing.

Not yet fixed — the honest options are to record a gap when the distill returns
None (not just when rows are empty), or to normalise ETF capital flow by AUM /
shares × price instead of market cap. Needs a decision, not a reflex.

**`run_universe_refresh.sh` had never executed once — so we ran it, and it was
broken.** No `logs/universe.log` existed. Cron is `0 19 1-7 1,4,7,10 *` and the
wrapper additionally exits unless `date +%u -eq 7`, so the real cadence is the
**first Sunday** of Jan/Apr/Jul/Oct. Jobs were armed 2026-07-24, after Sun Jul 5
had passed; next natural fire **Sun Oct 4**.

Exercised manually with `--dry-run` on 2026-07-28. It **crashed twice**, both
times for the same design error in `moomoo/research._snap_chunk`:

```
RuntimeError: snapshot failed for US.AUR (non-ticker error):
  Get Market Snapshot request failed due to high frequency. Maximum 60 times per 30 seconds.
RuntimeError: snapshot failed for US.HSBC (non-ticker error): PacketErr.Timeout
```

Neither ticker was at fault. `_snap_chunk` bisects a failed batch to isolate a
bad ticker — correct for a ticker-level error, catastrophic for a connection-level
one. moomoo's ceiling is 60 `get_market_snapshot` calls / 30s; an 817-name pond
trips it, bisection splits into two more calls *against the ceiling just hit*,
those fail, split again — ~800 calls in seconds, then it blames whichever single
code the recursion reaches first. **The retry strategy was manufacturing the
failure it reported.**

Fixed: `_snap_call` paces to 0.55s/request; `_TRANSIENT_MARKERS` (rate limit,
timeout, connect/disconnect, network) retry the SAME batch with bounded linear
backoff and never bisect; bisection now only runs for errors that implicate a
ticker. Selftested with a fake ctx on all three paths — including an assertion
that the retried batch stays full-size rather than split.

**Third run: exit 0, 5m06s.** Decision **HOLD** — "16 changes > 5 (possible data
glitch)" plus 20 stale seeds (ACHR, BMNR, CAG, CMCSA, CRWV, HTZ, MARA, MSFT,
MSTR, NFLX, NKE, ORCL, PLTR, RGTI, SMCI, SMR, SNAP, SOC, SOFI, T). Proposed
+8 (TSM, SKHY, ASML, DHR, UPS, TMO, CDNS, ARM) / −8 (CAH, CVNA, D, DASH, HON,
MNST, PGR, RDDT), keep 142. It correctly refused to auto-apply and
`config/universe.csv` was untouched.

Note the HOLD reason is probably *not* a data glitch: the threshold of 5 assumes
a universe already being maintained quarterly, and this one has never been
refreshed since inception. 16 changes after that long is plausible drift. A
dry-run proposal file now sits in `research_store/universe/proposals/` and the
dashboard surfaces HOLD proposals, so expect a pending-review banner for an
off-cycle proposal — delete the two `2026-07-28.*` files to clear it.

---

## 2026-07-27 — The price panel could never see today: Schwab `endDate` defaults to the PREVIOUS trading day

**Every book this system has ever produced was ranked on one-day-stale data.**
Not a scheduling accident — a silent API default we never passed a value for.

`adapters/schwab/research.get_price_history()` omitted `endDate`. Schwab defaults
that to the **previous trading day**, so the daily panel structurally could not
contain the current session no matter when we ran. The slow loop fires at 18:00
ET, two hours after the close, and still got yesterday.

Proven directly, 2026-07-27 10:27 ET, same call twice:

```
omitted end_date -> last bar 2026-07-24   (the bug)
end_date=now     -> last bar 2026-07-27   (the fix)
```

Corroborated across every logged run (`logs/slow.log`, run wall-clock from the
trailing `backup_ledger: pushed` stamp vs. the panel's own last date):

| ran (ET) | panel last | as_of | regime | lag |
| --- | --- | --- | --- | --- |
| Thu 2026-07-23 18:03 | 2026-07-22 | 2026-07-22 | ON | 1d |
| Fri 2026-07-24 18:03 | 2026-07-23 | 2026-07-23 | **OFF** | 1d |
| Sun 2026-07-26 20:03 | 2026-07-24 | 2026-07-24 | OFF | 2d (correct — weekend) |

**Consequence.** SPY closed below its 50DMA on **Thu 07-23** (738.18 vs 745.05).
That regime-off signal did not reach a book until the **Fri 07-24** run, and was
not executed until the **Mon 07-27** fast loop — two sessions late. In the gap,
Friday's fast loop *bought* PANW into a market that had already tripped the gate.

**Measured cost of the delay on this episode: ≈ $0.69.** Exiting at Friday's
close instead of Monday's fills would have been +$0.69 across the 11 names; the
stray PANW round-trip was +$0.05. The day's realized −$4.26 is therefore
**not** mostly attributable to this defect — it came from the 50DMA whipsaw
(OFF 07-17/20 → ON 07-21/22 → OFF 07-23/24, SPY oscillating inside ~1% of the
line) and from five of ten entries filling **above** their entry zone (LITE
+5.0%, KEEL +3.6%, NBIS +3.4%, MU +2.0%, AMAT +1.4%). The fix would **not** have
prevented those entries: Jul 22's close (747.41) was genuinely above the 50DMA,
so the gate was legitimately ON when they were bought.

Severity is about mechanism, not this week's dollars: a two-session lag on the
*risk switch* is only cheap when the tape drifts. In a fast selloff it is the
difference between exiting on the signal and exiting after the damage.

**Fix.** `get_price_history()` takes `end_date` and passes `endDate` through;
`fetch_prices._try_pull` sends `end_date=now`. Because `endDate=now` during RTH
returns a **live, partial** bar (its `close` is the last trade — verified: today's
bar read 739.62 while the NBBO last was 739.69), a pure guard
`_drop_unsettled_session()` discards the current day's row before 16:15 ET and
keeps it after. Covered by `scripts/fetch_prices.py --selftest`, mutation-tested
both directions (never-drop and always-drop, the latter being the original bug).

**No trades were reversed.** The regime is still off — at 10:27 ET SPY was 739.69
live and 738.93 settled against a 745.07 50DMA. Today's liquidation was *late,
not wrong*; re-entering would have churned real money a second time. The two
sleeve buys (XLI, XLK) skipped on `EQUITY_NOT_ENOUGH_BP` re-plan on the next
run once Monday's proceeds settle.

**Still open (flagged, not bundled):** `scripts/risk_review.py::_gather_highs`
uses the same call without `end_date`, so the give-back high-water mark misses
the current session's high. It runs at 12:00 and 15:45 ET, both intraday, so the
partial-bar question needs its own decision rather than a copy of this fix.

**VERIFIED 2026-07-28.** The Mon 07-27 18:03 ET run produced
`panel: ... 2016-07-27 .. 2026-07-27` and `as_of 2026-07-27` — lag **0d**, the
first same-day book this system has made. `current.json` carries `as_of
2026-07-27`. Tuesday's fast loop then traded off it, so signal→execution is now
one session instead of two.

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
pointer was broken too.

**Back-audit of every issue** (replaying each letter's real window against only
the events that existed when it ran):

| Issue | Selected (buggy) | Actually in window | Leak | Table | Prose |
| ----- | ---------------- | ------------------ | ---- | ----- | ----- |
| 001 | 2 ev / 15 legs | 2 ev / 15 legs | none | correct | correct |
| 002 | 3 ev / 16 legs | **1 ev / 1 leg** | 2 ev / 15 legs | **CONTAMINATED** | contaminated |
| 003 | 14 ev / 52 legs | 12 ev / 37 legs | 2 ev / 15 legs | correct | contaminated |
| 004 | 5 ev / 33 legs | 3 ev / 18 legs | 2 ev / 15 legs | correct | contaminated |

Issue 001 is clean — the 07-08 build fell legitimately inside its opening 7-day
window. Every issue after it republished that same build.

**Issue 002 is the worst and the only one whose fills TABLE is wrong**: exactly
one real fill leg occurred that week (the SPY trim @ $752.92), but the table
lists 15 rows — the entire 07-08 build, re-reported a second time after issue
001 had already covered it. Its prose likewise opens "This was the week the book
actually got built," describing the previous issue's news.

Issues 003 and 004 have correct tables — their agents rendered only entries
carrying `status: "filled"`, and the leaked 07-08 legs carry no `status`. But
both narratives absorbed the stale broker-profile block and reported it as
current ("A broker profile check held back most of a planned rebuild this week",
003; "13 of 14 never placed", 004).

**Scope of the ts-less class:** 24 of 73 journal events carry no `ts` — under the
old predicate ALL of them were force-included in every letter forever. Only the 2
`execution` events are letter-facing, though: `letter_facts` consumes `execution`,
`exit_signal`, and `risk_review` only, and every `exit_signal`/`risk_review` has a
`ts`. The other 22 (`product`, `signal_panel`, one undatable `outcome`) never
reached the letter. So the blast radius above is complete, not a sample.

**Latent, not fixed:** `research_store.append_journal()` writes whatever dict it
is handed and does NOT stamp `ts` — each caller must remember. `_in_window` now
degrades safely (falls back to `as_of`; excludes the undatable), so a forgotten
`ts` can no longer leak forever. But stamping `ts` at that single chokepoint
would kill the class outright. Not done here: it touches the ledger write path,
which is non-regenerable data and deserves its own deliberate change.

Letters already sent cannot be recalled — recorded here so the record is
straight. Issue 005 regenerates clean (0 fills, 0 notes, no un-statused legs).

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

## 2026-08-19 — execution retry / unexpected order incident

**Status:** emergency HALT active; monitor and session timers disabled pending
controlled recovery. Codex review disabled. Do not remove HALT or restart units
manually until the broker state has been checked.

### What happened

1. MRK crossed its inherited target-1. The monitor launched the Claude exit
   executor, which placed two partial sells. The broker position went from
   `0.044229` shares to `0.011057` shares; there are no open MRK orders.
2. The executor did not leave `research_store/monitor/exit_result.json`.
   The monitor therefore classified the outcome as a failed exit even though
   the broker had sold shares.
3. The monitor's retry policy treated a fractional target as retryable and
   computed each retry as a fraction of the *current* remaining quantity. It
   repeatedly paged `Exit executor result` and could sell more of the same
   position. `monitor/state.json` reached 22 unresolved MRK failures before the
   service was stopped.
4. During the same morning, the scheduled open session ran and intentionally
   bought FTNT: `0.039290` shares at `$152.71`, order
   `6a85c11a-5ff0-422c-b525-8ad9075ea820`. This was a session execution, not a
   Codex review action. It completed before the emergency HALT was activated.

### Immediate containment

- Created `research_store/HALT`. This makes the monitor alert-only and blocks
  the session wrapper from launching any new model session.
- Stopped and disabled `agentic-monitor.service`.
- Stopped and disabled `agentic-session@open.timer` and
  `agentic-session@close.timer` so no scheduled run can start during review.
- Confirmed no Claude, session, monitor, or review process remained active.
- Disabled the post-session `systemctl start --wait agentic-review.service` call
  in `deploy/run_session.sh`; the review is advisory and consumes a second
  model budget.

### Code changes

`scripts/market_monitor.py` now treats a missing `exit_result.json` as an
**unknown broker outcome**, not a normal transient failure. The affected symbol
is marked `paused` and `refire_gate()` refuses all automatic retries until a
human reconciles Robinhood. This is deliberately conservative: an unknown
outcome must never be retried when the order may already have filled.

`deploy/run_session.sh` now refuses to launch if `research_store/HALT` exists,
and the Codex review invocation is commented out. `deploy/resume_after_halt.sh`
is the only controlled recovery path: it verifies the review remains disabled,
enables/starts the monitor and session timers, then removes HALT.

### Recovery procedure

Before recovery, confirm in Robinhood:

- FTNT position and the fill above; decide separately whether it is retained or
  manually unwound.
- MRK position is `0.011057` shares and no MRK sell order is open.
- No unexpected open orders exist in the Agentic account.
- `research_store/rh/positions.json`, `fills.json`, `orders_dump.json`, and
  `realized.json` are reconciled from broker truth.

The scheduled resume is intentionally delayed two hours from the incident
response. It must re-enable the monitor and timers while leaving Codex review
off. After resume, watch the first monitor poll and the next close session
closely. Do not remove the HALT manually; use the scheduled recovery script.

### Future-proofing recommendations

These are follow-up safeguards for the next Claude session; do not implement
them by assumption during recovery:

1. **Make exit requests idempotent.** Store the intended absolute quantity,
   broker account, trigger ID, and a deterministic client reference in
   `exit_request.json`. A retry must never reinterpret `fraction` against the
   newly reduced position. The executor should refuse a second order when the
   original trigger ID already has a broker fill.
2. **Reconcile broker truth before retrying.** When `exit_result.json` is absent,
   query Robinhood orders and positions before deciding whether anything is
   still owed. Missing local bookkeeping is not evidence that an order failed.
3. **Separate target and stop retry policies.** A missing result on a target
   should pause quietly because holding is safe; a missing result on a stop
   should raise one manual-intervention alert and never create an unbounded
   Claude retry loop.
4. **Add a persistent notification circuit breaker.** Any alert family that
   exceeds a small hourly budget should deduplicate and disable itself while
   preserving a local journal entry. ntfy must never be able to amplify a
   model/broker failure into a second outage.
5. **Add model-budget telemetry.** Record executor/session start, model,
   duration, exit status, and provider error class locally without recording
   credentials or prompt contents. A daily budget alarm should fire before a
   quota exhaustion causes a trading-path failure.
6. **Fail closed on startup and deployment drift.** Before enabling services,
   verify the broker snapshot age, no open unexpected orders, no unresolved
   exit request, the active code hash, and that review/notification settings
   match the operator’s declared mode.
7. **Use a non-LLM emergency executor path.** A small deterministic emergency
   routine should be able to cancel pending orders, query positions, and
   reconcile an already-filled exit without spending Claude quota. It should
   remain sell-only and account-scoped.
8. **Test crash windows explicitly.** Self-tests should simulate: fill then
   process kill, timeout before result write, result write then bookkeeping
   failure, duplicate monitor ticks, and a quota/auth error. The invariant is
   “at most the intended quantity is sold, and at most one alert per incident
   window.”

The highest-priority follow-up is items 1–3: absolute quantities plus broker
reconciliation are what prevent a successful-but-unreported fractional exit
from becoming a chain of duplicate sells.

The next session also receives `research_store/RECOVERY_HANDOFF.md` directly in
its brief. That handoff records that the open session already ran, the FTNT fill,
the MRK retained stub and repaired levels, the retry incident, and the disabled
Codex review. It is intentionally a one-time operator context file; archive it
after the first post-recovery close session has verified broker truth.
