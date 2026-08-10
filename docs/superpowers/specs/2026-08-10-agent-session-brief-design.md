# The agent's session charter — design

**Status:** design agreed 2026-08-10 (Aaron), pending implementation.
**Parent:** `2026-08-09-agent-authority-inversion-design.md`. That spec settled
*who decides*. This one settles *what the agent is told at the start of a session,
and what it can reach* — the thing that decides whether the inversion is real or
decorative.

Aaron's framing: **"so there are no surprises."** Both directions — the agent is
never surprised by a limit it could not see, and Aaron is never surprised by an
action he had no way to anticipate.

---

## 1. Two documents, not one

| | **Charter** (static) | **Brief** (per session) |
|---|---|---|
| Changes | rarely, by hand | every session |
| Contains | who the agent is, the terms, the baseline, the limits | facts as of this moment |
| Analogue | the rules of the game | the position on the board |

The reference system (`/opt/trading/watcher/session.py`) does exactly this:
`PROMPTS[mode]` with a `__FACTS__` placeholder, rendered at spawn.

**Facts are gathered AFTER the session lock, never before.** Their hard-won
reason: a session that waited five minutes for the lock spawned its child holding
prices read *before* the wait — on 2026-08-07, a 09:35 session reasoning on a
09:30 quote, through the fastest five minutes of the day.

---

## 2. Every stated rule is rendered from the code that enforces it

The single most important mechanic, and the direct answer to "no surprises."

The charter never *restates* a threshold. It renders it from the constant the gate
actually applies — `mandate.toml` for the criteria, `governance.py` for the gate,
the live tool list for the tool index. The agent therefore cannot be told a limit
that differs from the one enforced.

This is the fix `CLAUDE.md` already applied to the account balance: a fixed figure
in prose went stale and agents anchored on it to dismiss real risks. The remedy
was to delete the number and force a live read.

**Rule:** if a number appears in the charter, it is interpolated from its source
of truth. A literal is a defect.

### The tool index is grouped, never ranked, and complete

Taken verbatim from the reference, whose comment earns its place:

> A partial list is a recommendation: the tools somebody thought to type are the
> tools that get used, and the ones omitted may as well not exist.

Groups describe what each tool *touches* — a fact about the tool — never when to
reach for it.

---

## 3. The baseline strategy (decision 1)

The charter states the momentum strategy plainly: 10 single names + 4 ETFs, 70/30,
residual tilt 0.75, with the evidence (PIT backtest CAGR 23.2% / Sharpe 0.97 vs
SPY 12.4% / 0.80, survivorship-corrected).

It is a **standing belief, not a rule.** The agent may deviate on one name or
wholesale, with a recorded reason. Deviation is a decision, not a violation.

**The allowance must be stated with the same force as the baseline.** Aaron's
concern: an agent that reads the baseline as law and treats the permission as
decoration. Mitigations, in the charter itself:

- The baseline ships with its own counter-evidence: the 3.0σ optimum won only 1
  of 5 regime sub-periods; targets sat 5.5σ out and hit 0 of 14; the pre-PIT
  34% figure was survivorship-inflated.
- Aaron's standing instruction is quoted directly: *"I'd rather have a consistent
  strategic framework than a mathematically sound backtest."*
- Declarative grammar only. "The book holds MU" — never "trade momentum."

Deliberately **not** adopted: hiding the baseline behind a tool call. That
mechanism suited a project with no strategy by design; here the baseline is
legitimate and earned, and burying it would be theatre.

---

## 4. What must be announced before it happens (decision 2)

Full autonomy inside the gate for ordinary trades; the phone push after the fact
replaces per-trade approval. Three classes are pushed **before** acting:

1. Abandoning the baseline strategy wholesale (not a single-name deviation).
2. Entering a name outside the 168-name universe.
3. Any single position above the stated share of equity.

Aaron keeps veto on the unusual without gating the routine. Standing rule holds:
settlement and buying-power deferrals stay silent — they self-heal.

### 4a. Standing terms (Aaron, 2026-08-10)

These are not announce-first items; they are terms the charter states outright.
Each is marked by **how it is enforced**, because a term that lives only in prose
is the failure pattern this repo keeps repeating.

| Term | Enforcement |
|---|---|
| **Day trading is ALLOWED** | Nothing to enforce — a *permission*. Stated because the agent would otherwise assume the PDT rule applies. It does not: PDT governs margin accounts, and this is cash. Intraday round trips are bounded only by settled cash, not by a trade count. |
| **Options are NOT** | **Structural.** §5's allowlist names 10 Robinhood tools; no option tool is among them, so `place_option_order` is unreachable rather than merely forbidden. |
| **Cash account, T+1 settlement on CLOSES** | **Partly unenforced — see below.** Proceeds from a sale are unavailable until T+1. |
| **Observe moomoo rate limits** | **Code, as of `f59022e`.** `live.py` now paces every endpoint on a sliding window. Previously unenforced: an agent looping `quote()` would have breached `get_market_state`'s 10-per-30s ceiling on an OpenD shared with the sibling repos, degrading their feed too. |

**The settlement gap is real and open.** Nothing exposes settled vs unsettled
cash. `pending_settlement` exists only as a recorded *outcome* — the reason a buy
was skipped after the fact (XLI and LITE, 2026-08-10). So the agent sees a cash
balance, plans buys against it, and discovers only at placement that the money
was not available. With no shell, it cannot investigate why.

This is a required addition before lockdown: `account()` must distinguish
**settled** from **unsettled** cash, and the charter must state the T+1 rule as a
fact the agent plans around. Until then the agent will repeatedly plan
unfundable rotations — the single most likely cause of a wasted session.

Note this interacts with day trading: in a cash account, selling and rebuying the
same day spends settled cash, and the proceeds do not return until T+1. Day
trading is permitted, but it is **rate-limited by settlement**, not by rule.

---

## 5. The environment limit: MCP only (decision 3)

The deployed agent runs as headless Claude Code, which by default carries Bash,
file write and network. A "full authority" session could therefore edit
`mandate.toml`, delete the kill switch, rewrite `governance.py`, push to git, or
read `.env`. **An agent that can edit its own guardrails has none.**

Sessions launch with an explicit allowlist:

- **`agentic-trader` MCP** — all 29 tools (§7), none of which can place an order.
- **`robinhood-trading` MCP** — 10 tools only: `get_accounts`,
  `get_equity_positions`, `get_portfolio`, `get_equity_quotes`,
  `get_equity_orders`, `get_realized_pnl`, `get_pnl_trade_history`,
  `review_equity_order`, `place_equity_order`, `cancel_equity_order`. The other
  ~43 (options, watchlists, scanners) stay denied — so `place_option_order` is
  unreachable, not merely forbidden (§4a).
- **Nothing else.** No Bash, no Read/Write/Edit, no WebFetch.

Enforced in settings, and asserted by a new `src/repo_checks.py` check so it
cannot silently drift — prose enforcement of a safety boundary is the pattern
that has failed repeatedly in this repo.

**Sequencing, non-negotiable:** the allowlist is tightened only *after* the tool
surface is complete. Removing the shell first does not harden the agent, it
bricks it — mid-session, with live positions. The surface was completed in
`8161c2a` (28 tools); this precondition is now met.

---

## 6. The gate becomes unbypassable (decision 4)

`check_order()` is advisory — it answers "may I?" and nothing compels the agent to
ask. Placement runs through the Robinhood MCP, so with both servers allowlisted
the gate is honour-system.

Robinhood is a **client-side OAuth MCP** (`~/.claude.json`); our FastMCP server
holds no Robinhood credentials and cannot place orders itself, so "move placement
into our MCP" is not available without coupling the only execution path to
undocumented client token-refresh internals.

**A `PreToolUse` hook** on `mcp__robinhood-trading__place_equity_order` therefore
validates every order against governance, the mandate, and the Agentic-account
rule, and denies it outright on failure. The harness enforces it; the agent
cannot place an order the gate rejects even if it never calls `check_order`.

**Latency, measured not assumed:** a full cold hook process — every import plus
the gate decision — is **0.08–0.11s**; the decision logic alone is 0.65ms. The
Robinhood network round-trip dwarfs it.

> **Constraint:** the hook may read only in-memory state and small JSON. It must
> never read the price panel or recompute the signal. That is what would turn
> 0.1s into minutes.

**Second layer:** a reconciler checks every fill had a matching approval and halts
loudly on a mismatch, so a hook that silently stops firing is still caught.

---

## 7. The tool surface — grouped BY VENUE first

**The division of labour is the first thing the charter states, because confusing
it is a class of error no gate catches.** The agent must never reach for moomoo to
place an order, nor Robinhood to research one.

> **moomoo is DATA. Robinhood is ORDERS. There is no overlap, and no second
> execution venue exists.**
>
> moomoo's API *can* trade — `unlock_trade` — and this system never calls it, in
> any repo. Nothing in the tool surface exposes it, so an order cannot be placed
> at moomoo even by mistake. If you find yourself looking for one, the tool you
> want is Robinhood's `place_equity_order`.

| Venue | Group | Tools |
|---|---|---|
| — (local state) | **Orientation** | `brief`, `positions`, `account`, `mandate_status`, `halt_status`, `performance` |
| moomoo + local panel | **Selection** | `candidates(n)`, `universe`, `leaders`, `sectors` |
| moomoo | **Pricing** | `quote`, `depth` |
| local panel | **Terrain** | `terrain` |
| moomoo / FRED / Alpaca | **Events** | `earnings`, `macro_calendar`, `macro` (FRED), `news` (Alpaca) |
| local | **Deciding** | `check_order`, `set_levels`, `record_decision` |
| local | **Memory** | `research_log`, `rule_out`, `revisit`, `open_question`, `close_question` |
| local | **Attention** | `wake_register`, `wake_status`, `wake_deregister` |
| local | **Liveness** | `ping` |
| **Robinhood** | **THE ONLY EXECUTION** | `place_equity_order`, `cancel_equity_order`, `review_equity_order` |
| **Robinhood** | Account truth | `get_accounts`, `get_equity_positions`, `get_portfolio`, `get_equity_orders`, `get_equity_quotes`, `get_realized_pnl`, `get_pnl_trade_history` |

Groups say what each tool *touches* — a fact about the tool — never when to reach
for it (§2).

**Robinhood allowlist corrected 2026-08-10:** the original 8 omitted
`get_realized_pnl` and `get_pnl_trade_history`, so the agent could not see its own
realised results from the broker at all. Now 10.

### The basics, audited

Prompted by Aaron: the exotic tools were built before anyone checked the obvious
questions. Verified live against the real account:

- *What is the account worth?* → `account()` — value, cash, invested, `marked_at`.
- *What do I hold?* → `positions()` — 12 positions with qty, cost, mark, P&L,
  share of equity, stop, targets, and `watched`.
- *Am I within the terms?* → `mandate_status()` — all four criteria with room.
- *Are the switches on?* → `halt_status()`.
- *Is what I am doing working?* → **`performance()`, added in response.** Nothing
  exposed the equity curve or a single closed trade before; `state.equity_series`
  had zero callers. The agent could not see its own track record.

`performance()` on first run: 17 closed trades, 29.4% win rate, average win
+1.5%, average loss −9.26%, **0 of 17 targets hit**. That is the trade-geometry
mismatch showing in realised results, and it was invisible to every other tool.
Win rate is therefore reported ONLY beside average win and average loss — this
system's own backtest produced 78% winners that lost money.

Two distinctions the charter must state explicitly, because confusing either is a
safety failure:

- **A wake is not a stop.** A stop is enforced by `market_monitor`, which places
  the order itself and needs no agent. A wake only *starts a session*. Registering
  a wake in place of a stop leaves a position unprotected while looking protected.
- **`leaders` looks outside the universe.** Names found there have no deep price
  history, so `terrain` and `candidates` cannot score them. Acting on one requires
  saying so.

---

## 8. Sessions

Premarket, open, close — **full authority, no procedure** — plus agent-registered
wakes firing full sessions. One lock: two full-authority sessions must never run
at once.

Stops and targets the agent set are enforced by the monitor **without waking
anything**: that decision was already made.

`prompts/fast_loop.md` is retired by this. Its framing — *"You are the hands, not
the brain… do NOT improvise, re-rank, or second-guess"* — is the exact inversion
of this design and must not survive alongside it.

---

## 9. Deferred, with reasons

Recorded so they are decisions rather than omissions:

- **Panel-level split adjustment.** `0e5c67b` made a phantom split *sale*
  impossible; the panel still records the split as a real return, corrupting
  momentum and inflating sigma. Needs `get_rehab` wired into `fetch_prices` and
  stored levels re-based on an ex-date.
- **#30 liquidity floor.** Measured: Alpaca IEX undercounts consolidated dollar
  volume by 7–21×, and the ratio varies 3× *between names*, so it mis-ranks names
  against each other. Fix is to swap to moomoo `turnover` (already wired) — not
  `get_orderbook`, which measures spread, not volume.
- **Wake polling.** `wakes.due()` exists and is selftested; nothing calls it yet.
  Until something does, wakes register but never fire.
- **Settled-cash visibility.** NOT deferred — it is a precondition for lockdown
  (§4a). Listed here only so it is not lost: `account()` must separate settled
  from unsettled cash before the shell is removed.

---

## 10. Resolved: the announce-first threshold

§4.3 ("above a stated share of equity") takes its number from
`mandate.toml [concentration] max_position_pct` (0.15), rendered per §2 — not a
second literal.

The two are different actions on the same limit, which is the point: at 0.15 the
gate **refuses** the order, so announce-first fires as the position *approaches*
it. The charter renders the announce trigger at **80% of the blocking limit**, so
Aaron hears about a position getting large before the gate stops it, and there is
exactly one concentration number in the system.
