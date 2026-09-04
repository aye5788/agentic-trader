# Model fallback chain, code seller, and monitor-owned exit bookkeeping — design

**Date:** 2026-09-03
**Status:** approved by the principal (Aaron) 2026-09-03. LANDED 2026-09-04
before the open: §1, §2, §4, §6, §7, §8.1–8.3 (OPSLOG 2026-09-04, two
entries). PENDING: §3 code seller, §5 custody, §8.4–8.5. Budget step: INERT
by the principal's decision 2026-09-04.
**Trigger:** the 2026-09-03 Claude model outage (`docs/OPSLOG.md`, same date).

## 0. What happened and what it exposed

On 2026-09-03 the Claude incident took Opus 5 down. The 10:35 OPEN session
was held. Investigating the hold surfaced five separate defects, none of
which the outage caused but all of which it made visible:

1. **Model pins are literals in code, in three places** (`scripts/session.py`,
   `scripts/market_monitor.py:executor_argv`, `deploy/run_newsletter.sh`), with
   nothing behind any of them. Swapping one under pressure meant a code edit,
   a reload, a commit of a temporary value to `main`, and a memory note to
   revert. The stop's execution arm (the exit executor) is bound to one model
   with no fallback: if that model is down, breaches are detected and paged,
   not sold.
2. **The exit executor's bookkeeping depends on a model typing a command
   byte-exactly.** Its four recorder commands are exact-match Bash grants.
   Every run tries `record_fills.py; echo "EXIT=$?"`, is refused, and on prior
   days retried the bare form. On 09-03 it did not retry: the sale filled, the
   result file was written, and the ledger, snapshot and partial-outcome
   label were left unwritten. The monitor's staleness guard compares the
   snapshot against the journal, and an unjournalled sale makes them agree,
   so an exit after the 15:15 session ends leaves a ghost position on watch
   through the next morning's unattended 09:30–10:35 window (the 08-14 class).
3. **Two Claude Code installs.** Cron and systemd resolve `/usr/bin/claude`
   (2.1.100, April, orphaned — the system npm prefix now points at the nvm
   tree so nothing can ever update it); the shell resolves the nvm install
   (2.1.259, auto-updating). "It works when I run it" proved nothing about
   cron — the 08-13 codex-PATH class.
4. **The DELL target1 trim of 09-03 has no `partial_outcome` ledger event**
   (consequence of 2), and two staging files were left describing a book two
   trades old.
5. **Session trades do not push to the phone.** Only the exit path's
   `scripts/record_fills.py` and `announce()` push; the sessions' MCP
   `record_fills` never did. As built, but the principal expected a push.

Standing policy decided 2026-09-03 (supersedes the in-the-moment "no fallback"
of the morning entry): **fallback is automatic, only on a clean failure, across
a cross-family chain; the exit path ends in a model-free code seller; sessions
end in a Codex custody mode that cannot trade.** Full cross-vendor *trading*
(a harness-independent order gate) is explicitly OUT of scope — see §10.

## 1. Model config with a local override

**New file `config/models.toml`** (committed):

```toml
[roles]
session    = "claude-opus-5"
exit       = "claude-opus-4-8"
newsletter = "claude-opus-5"

# Ordered, cross-family. A role's chain = [its primary] + fallbacks (minus the
# primary if it appears), then the role's terminal step (§2).
[chain]
fallbacks = ["claude-sonnet-5", "claude-fable-5-1"]

# Minimum Claude Code version that accepts each id (§6 health check; static).
[requires]
"claude-fable-5-1" = "2.1.251"
```

**`config/models.local.toml`** — git-ignored, human-written, merged OVER the
base file with the same `_merge` the strategy loader uses. An outage swap is
a local edit; reverting is deleting the file. Nothing temporary is ever
committed again.

**New module `src/models.py`**: `load() -> dict`, `primary(role) -> str`,
`chain(role) -> list[str]`, `provenance() -> {"base": path, "local": path|None}`.
Pure over two TOML files. Selftested: base-only, local override of one role,
local override of the fallback list, primary duplicated in fallbacks
(deduped), unknown role (raises).

**Consumers, all read at spawn time (no restart to pick up a change):**
- `scripts/session.py` — argv builder takes the model as a parameter; `run()`
  iterates `models.chain("session")` under §2.
- `scripts/market_monitor.py:executor_argv(model)` — pure, model parameter;
  `run_executor` iterates `models.chain("exit")` under §2. The monitor is a
  long-running process but reads the config per spawn.
- `deploy/run_newsletter.sh` — `MODEL=$(.venv/bin/python -m models newsletter)`.
  The newsletter has no chain (no money; a failed letter re-runs by hand).
- `src/health.py` + dashboard: a `models` row showing each role's resolved
  chain and whether a local override is active.

`check_charter_no_literals`-style rule: a model id literal in `scripts/`,
`deploy/` or `src/` outside `src/models.py` and its selftest is a defect.
Enforced by `src/repo_checks.py` (filesystem-only grep, no network).

## 2. The fallback chain

**New pure module `src/fallback.py`.**

```python
@dataclass
class Attempt:
    model: str
    rc: int
    stream_path: Path | None   # stream-json transcript
    stderr: str
    ok: bool                   # from session.classify() or the exit result file
    error: str | None

def clean_failure(a: Attempt) -> bool:
    """True only when the spawn PROVABLY did no work: it failed AND its
    stream-json transcript contains zero tool_use blocks."""

def next_step(chain: list[str], attempts: list[Attempt], budget_left_s: float,
              per_attempt_s: int) -> str | None:
    """The next model to try, or None (exhausted / ambiguous / no budget)."""
```

**The one rule.** A spawn falls through to the next step only when
`clean_failure` holds: the run failed (existing `classify()` for sessions; the
result file absent for the exit executor) AND the transcript has no
`tool_use` event. A 400 (`claude_code_version_too_old`, unknown model), a
5xx/529, a transport error before any work, or an empty transcript qualify.
A timeout or crash after any tool call is AMBIGUOUS: the chain stops, the
existing failure path runs (page, `retryable: false`, symbol paused). This is
the 09-03 morning entry's rule ("do not retry a session after an ambiguous
failure because it may already have placed orders") made mechanical. It is
STRICTER than `should_retry()`'s signature list, and it is the only condition
under which anything is retried.

**The budget failure class (added 2026-09-04).** The 09-03 close session
died on the operator's SUBSCRIPTION usage cap (`reason_class: usage_limit`,
now journalled by the runner — see OPSLOG 2026-09-04). A cap hits every
Claude model in the chain at once, so the chain above does not help for it.
The chain therefore needs one BUDGET-INDEPENDENT step, and which one is the
operator's decision, recorded here as open: (a) a dedicated subscription
token for the box (`claude setup-token` on a second account), so interactive
work can never starve the cron sessions — removes the cause; or (b) an
API-key-billed attempt of the same model, with `ANTHROPIC_API_KEY` set ONLY
in that spawn's environment and never in the box's (the CLAUDE.md footgun,
made deliberate and scoped). `usage_limit` is a clean failure only under the
same zero-tool-call rule; the 09-03 close had made 11 calls and would NOT
have been retried.

**Budget.** Each attempt keeps the role's timeout; the chain's total is capped
at the role's cron window (`session`: `TIMEOUT_S[mode]` × 1.5, so an OPEN
session chain never runs into the close; `exit`: `executor_timeout_secs` × 2).
A step that does not fit the remaining budget is skipped to the terminal step.

**Evidence.** Every fallback writes a journal event
`{"event": "model_fallback", "role", "mode", "from", "to", "reason",
"attempt", "ts"}` and pushes to the phone
(`"FALLBACK session: claude-opus-5 → claude-sonnet-5 (400 version_too_old)"`).
Exhaustion writes `model_fallback` with `"to": null` and pushes the existing
failure page. Health shows the last fallback event (fire-once, clears on a
clean primary run).

**Executor stream capture.** `run_executor` gains `--output-format
stream-json --verbose` and writes the transcript to
`research_store/monitor/exit_stream.jsonl` (overwritten per spawn), which is
what `clean_failure` inspects. This also fixes the gap that made 09-03
undiagnosable from the monitor's own log: today the executor's tool calls are
recoverable only from `~/.claude/projects/`.

**Chains as configured.**
- session: `claude-opus-5 → claude-sonnet-5 → claude-fable-5-1 → CUSTODY (§5)`
- exit: `claude-opus-4-8 → claude-sonnet-5 → claude-fable-5-1 → CODE SELLER (§3)`

## 3. The code seller (exit path, terminal step)

**New `scripts/code_seller.py`** (venv, `mcp` streamable-HTTP client). It is
the exit prompt's mechanical steps with no model:

1. Read `exit_request.json`; refuse unless its content hash equals
   `AGENTIC_EXIT_REQUEST_ID` — reuse the hashing function the exit-scope hook
   uses (`scripts/hooks/pretooluse_exit_scope.py`), never a copy of it.
2. Refuse if `research_store/HALT` or the kill switch is present (the same
   write-free governance predicates the order gate calls).
3. Broker auth: the CLI's stored token for `robinhood-trading` in
   `~/.claude/.credentials.json` (`accessToken`, `refreshToken`, `expiresAt`);
   refresh through the server's discovered token endpoint when within 5 min of
   expiry. The file is read; its only write is persisting a refreshed token
   in the same shape the CLI wrote, so the CLI keeps working afterwards.
4. `get_equity_positions(account)` for the pinned account. A paginated
   response (any `next` cursor) is an unexpected state → fail closed.
5. Per exit: quantity = `fraction × live quantity` (full: the live quantity),
   `review_equity_order` then `place_equity_order` as a market, share-quantity
   sell in RTH. Poll `get_equity_orders` up to the executor timeout for
   `filled`; append to `exit_result.json` after EACH placement (same lossy
   window as the prompt path).
6. Write the staging files the recorder scripts consume (`fills.json`,
   `broker_state.json`, `partial_closes.json` or the full-close equivalent,
   `orders_dump.json`) — the monitor runs the scripts (§4).
7. Anything unexpected — token refusal, pagination, a non-filled state at the
   deadline, a review mismatch — exits non-zero with NO order placed for that
   symbol and the result file absent for it. The monitor's existing path then
   pauses the symbol and pages.

**Drill mode** (`--drill`): steps 1–4 real, step 5 stops at
`review_equity_order` and prints the reviewed payload; nothing is placed.
This is the spike that answers the open question (does the broker accept the
CLI's token from a second client?) and it is run BEFORE the seller is added
to the chain. If the token is refused, the seller is not wired and the exit
chain ends at Fable with today's loud failure; that outcome is recorded here
and in OPSLOG.

The seller is a program the exit-scope hook does not see, so its
authorization is steps 1–2, verified in its selftest against a rewritten
request file (must refuse) and a HALT file (must refuse).

## 4. Monitor-owned exit bookkeeping

After `run_executor` returns — whichever step sold — the monitor runs, in
order, as subprocesses under `REPO/.venv/bin/python` with `cwd=REPO`:

1. `scripts/record_fills.py` — if `research_store/rh/fills.json` exists.
2. `scripts/record_exit_outcome.py` — if `exit_closes.json` (full closes)
   exists; `scripts/record_partial_outcome.py` — if `partial_closes.json`
   exists. Both may run (a request can carry a full and a partial exit).
3. `scripts/reconcile_ledger.py` — if `orders_dump.json` exists.

A sold symbol (present in `exit_result.json`) whose staging files are ALL
absent is a loud finding: push `"EXIT BOOKKEEPING INCOMPLETE: <sym> sold,
no staging files"`, journal `exit_bookkeeping_gap`, and the 08:00 health
check's `unrecorded_fills` stays as the backstop. A non-zero script exit is
pushed with its last stderr line; the remaining scripts still run.

**Executor contract change.** `deploy/exit_executor_settings.json` loses all
four `Bash(...)` grants (its `Bash` surface becomes empty; the tool stays
available for nothing). `prompts/exit.md` step 7 becomes "write the staging
files; the monitor records them" and names the files. The prompt's existing
retry-on-refusal language is deleted rather than strengthened: the class is
removed, not narrowed.

What this makes impossible: a filled sale whose ledger/snapshot are silent,
including when the executor dies after placing. The recorder scripts are
already idempotent (`reconcile_ledger` keyed on order_id;
`record_partial_outcome` documents its re-run behaviour), so a monitor
restart between the sale and the bookkeeping is safe.

The monitor restarts via `scripts/reload_stale.py`, which refuses while an
exit is in flight.

## 5. Custody mode (sessions, terminal step, Codex)

**Spike first.** A headless `codex exec` against the reviewer's existing
read-only `agentic-trader` server, launched with the cron PATH, calling
`positions`. Outcome A: the call returns data — the 08-14 cancellation bug
(openai/codex#16685) is gone in 0.148. Outcome B: still cancelled. The spike
is recorded in OPSLOG and `~/.codex/config.toml`'s comment block updated
either way.

**What custody mode is.** A session whose tool surface contains NO order tool
and NO broker. It mounts the `agentic-trader` MCP server started with a new
`--custody` flag that strips `record_fills` and `refresh_broker_snapshot`
(the two tools that take broker payloads) and keeps everything else:
`set_levels`, `clear_levels`, `record_decision`, `open_question`,
`close_question`, `rule_out`, `revisit`, `announce`, and all reads. Outcome B
routes the same tool set through the reviewer's shim (`scripts/agent_view.py`)
extended with those write commands — bounded work, done only if needed.

**Why it needs no gate.** The only money-moving consequence of anything it
can do is the monitor firing an exit on a level it set, and that path has its
own scope hook, fraction bounds and the level-setting guards in code
(`set_levels` price guards, `widen` requires a reason). It cannot buy, sell
at its discretion, or refresh the snapshot; it works from the last refreshed
snapshot and is told so.

**The charter variant.** `prompts/charter_custody.md`, rendered by
`src/charter.py` from the same config (no literals), DEFINES the mode
outright: why the session is in it (the Claude chain is exhausted; this is
the last resort), what it works from (snapshot age stated), what it cannot
do (buy, sell, refresh, diagnose the system), and what it should do (protect
the book: stops that are wrong for the position, targets below the mark,
questions for the next full session). Its decisions journal under the
existing events with `"custody": true`; the runner journals
`model_fallback` with `"to": "codex-custody"`.

**Launch.** `session.py` spawns `codex exec` with the reviewer's proven flags
(`--sandbox read-only`, `approval_policy="never"`) plus the custody MCP
config, the custody charter on stdin, and the same process-group kill,
integrity snapshot and lock as a Claude session. `classify()` gains the
Codex failure banners.

## 6. One Claude install, one health line

- Remove `/usr/lib/node_modules/@anthropic-ai/claude-code` and the
  `/usr/bin/claude` symlink (the orphan). Add
  `/usr/local/bin/claude → /root/.nvm/versions/node/v22.22.2/bin/claude`;
  `/usr/local/bin` is already first on the cron and systemd PATHs. Delete the
  runtime drop-in (already done in the 09-03 revert).
- `src/health.py` gains `claude_binary`: resolves `claude` with the session
  unit's PATH (read from `systemctl show`) and with the login PATH; unhealthy
  if they differ or if either is missing; the detail carries both versions.
  Rides `use_network` (it execs the binary).
- `claude_models`: for each model id in every chain, `claude -p --model <id>`
  is NOT probed (no model call at 08:00); instead the check asserts the
  installed CLI version ≥ the minimum recorded per model in `models.toml`
  (`[requires] "claude-fable-5-1" = "2.1.251"`). Static, no network.
- Auto-update stays ON for the nvm install; the health line makes a version
  change visible the next morning. ⚠️ Observed 2026-09-04 08:07: during the
  self-update to 2.1.260 the wrapper reported "claude native binary not
  installed" for a few seconds. Once the unattended path uses this install
  (§6), a spawn inside that window is a clean failure on EVERY chain step
  (same binary) — so the runner must treat `native binary not installed` as
  a `version_too_old`-class reason and RE-TRY THE SAME MODEL ONCE after 30 s
  before walking the chain. Or pin the update hour outside RTH; the
  implementer decides in plan 2 and records which. The trade-off is recorded: a retired
  model breaks a pinned unattended path harder than an updated CLI does, and
  09-03's failure was "too old".

## 7. Small items

- **DELL 09-03 partial_outcome.** Write `research_store/rh/partial_closes.json`
  = `[{"symbol":"DELL","fraction":0.5,"entry_price":444.38,
  "exit_price":510.7964,"exit_date":"2026-09-03","exit_reason":"target1"}]`
  (entry = pre-sale avg cost from the 09-02 19:22Z journal note; exit = the
  fill's `avg_price` in the 15:56:24Z execution event) and run
  `scripts/record_partial_outcome.py`. Remove the stale
  `research_store/rh/fills.json` and `broker_state.json` from 10:27–10:28.
- **Session fill push.** `src/agent_env/server.py:record_fills` calls
  `notify.push` with the same one-line-per-order body
  `scripts/record_fills.py` sends, tag `money_with_wings`. Never raises.
  The principal may strike this item.

## 8. Verification — the chain must be SEEN to fire before it is armed

The principal's condition. Nothing in §2/§3/§5 is enabled until each drill
below has been run on this box and its output pasted into OPSLOG.

1. **Pure selftests** (`src/fallback.py`): clean failure → next; ambiguous
   failure (one tool_use in the stream) → stop; exhausted chain → None;
   budget too small for the next step → terminal step; primary duplicated in
   fallbacks → deduped.
2. **Live session drill** — `scripts/session.py open --drill`: replaces the
   charter with a no-tool prompt ("Reply with exactly: OK"), no MCP config,
   `--tools ""`, real chain logic, real CLI. Distinct from the existing
   `--dry-run`, which builds the brief and returns WITHOUT spawning; a drill
   spawns, that is its point. Run once with
   `models.local.toml` naming a bogus primary (`claude-does-not-exist`):
   expected — attempt 1 clean 400, journal `model_fallback` from bogus → sonnet-5,
   push received, attempt 2 answers OK, rc 0. Run again with two bogus
   models ahead of sonnet-5: the chain walks two steps. Run once with the
   real config: no fallback event. Drill runs journal under
   `"drill": true` so `score_reviews`/`letter_facts` ignore them.
3. **Live exit drill** — `scripts/market_monitor.py --executor-drill`: the
   same no-tool prompt through `run_executor`'s chain with an EMPTY
   `AGENTIC_EXIT_REQUEST_ID` (so the scope hook would refuse any order even if
   one were attempted) and a bogus primary. Expected as in (2).
4. **Code seller drill** — `scripts/code_seller.py --drill` against the live
   request file of a real (already-executed) exit: proves token acceptance,
   positions read, and a reviewed payload. Its output is the spike verdict
   for §3.
5. **Custody spike** (§5) — recorded before any custody code is written.
6. **First real fallback** in production is journalled and paged; the OPSLOG
   entry for that day quotes the event.

## 9. Order of work (each step its own commit, reloaded via `reload_stale`)

1. §7 DELL label + stale staging files (closes today's ledger gap).
2. §4 monitor-owned bookkeeping + executor contract change (closes the
   overnight exposure). Monitor restart.
3. §1 models config + loader + consumers; the 09-03 temporary pin is already
   reverted, so the base file simply records the current pins.
4. §2 chain + §8.1–8.3 drills. Armed only after the drills are in OPSLOG.
5. §6 one install + health lines.
6. §3 code seller: §8.4 drill first; wired into the chain only on a clean
   drill.
7. §5 custody: spike first; then the charter variant, the `--custody` server
   flag, the runner step.
8. §7 session fill push.

## 10. Out of scope — one POTENTIAL FUTURE PROJECT, and things that stay out

- **POTENTIAL FUTURE PROJECT (principal's decision 2026-09-03: document it,
  do not build it here): a harness-independent order gate** — a local MCP
  proxy in front of the broker that runs the gate in-process, so Claude
  Code, Codex and the code seller are all gated by the same code and full
  cross-vendor TRADING sessions become possible. It is new code on the
  critical path of every order, it holds the broker token, and it has its
  own fail-closed question (a sell must never be refused by a proxy crash).
  When wanted, it gets its own spec; the custody mode in §5 is the safe
  stand-in until then.
- Automatic retry of ANY ambiguous failure. Never.
- Any change to what the sessions are asked to do. The charter is untouched
  except for the custody variant, which is additive.
- Sector/theme/correlation limits, or any new static rule on the play. None.

## 11. Testing summary

| Unit | Test |
| --- | --- |
| `src/models.py` | selftest: merge precedence, dedupe, unknown role |
| `src/fallback.py` | selftest: §8.1 cases; pure, no I/O |
| `session.py` chain | §8.2 drills on this box; `classify` unchanged and its selftest still passes |
| `market_monitor.run_executor` chain + §4 | §8.3 drill; selftest for the "sold but no staging files" finding (pure helper) |
| `code_seller.py` | selftest: hash mismatch refuses, HALT refuses, pagination refuses; §8.4 live drill |
| custody | §8.5 spike; charter-variant no-literals check; server `--custody` strips exactly two tools (asserted) |
| health | selftest with fake PATH resolution results |
| repo_checks | new literal-model-id check has a fixture that fails on a planted literal |

Per `CLAUDE.md`: a passing suite is not evidence. Every prompt/charter/doc
change in this work is read whole, rendered as its consumer sees it, before
it is reported done.
