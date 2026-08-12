# OPSLOG — operations & maintainer notes

Dated log of operational events and technical flags, newest first. This is the
"separate entry elsewhere": the weekly investor letter (The Claude Ledger)
narrates the portfolio only — plumbing, broker blocks, code flags, and
settlement mechanics land HERE (written by the newsletter run from the week's
journal `notes`, or by hand). One `##` heading per entry.

---


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
