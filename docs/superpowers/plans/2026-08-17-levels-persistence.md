# Levels Persist Until The Agent Changes Them — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** An agent-set level lasts until the agent itself changes or clears it — with a mechanism to clear it, an instruction to do so on exit, and a guard so a stale level can never liquidate a position.

**Architecture:** Supersedes Task 4 of `2026-08-17-agent-set-levels.md`, which is reverted. Expiry is removed rather than repaired: a stop that silently vanishes on a timer nobody chose is not protection, and the principal decided levels are the agent's until it says otherwise. Removing expiry creates one new hazard — a level outliving its position — which parts 2–4 close.

**Tech Stack:** Python 3.12 (`.venv`); Python 3.10 (`/usr/bin/python3`) for `scripts/market_monitor.py` only.

## Global Constraints

- **Two runtimes.** `scripts/market_monitor.py` imports moomoo at module scope — run it ONLY with `/usr/bin/python3`. Everything else `.venv/bin/python`.
- **⛔ NEVER create, write, or delete the live `research_store/monitor/overrides.json`.** It does not currently exist. All tests redirect via `decide.OVERRIDES` or temp paths.
- **⛔ No literal threshold in `prompts/charter.md`** — `src/repo_checks.py:check_charter_no_literals` enforces it.
- **No threshold or automatic action anywhere.** The agent picks every number. Part 3 is a *safety refusal*, not a strategy rule: it can only decline to apply something, never choose a level.
- **Part 3 modifies the live stop watcher.** After it, `.venv/bin/python scripts/reload_stale.py` MUST restart `agentic-monitor`. Verify it did.
- Full suite green before each commit: `deploy/run_selftests.sh`.
- Known-expected: `src/repo_checks.py` reports 4 "session timer NOT ENABLED" findings while the trading timers are deliberately disabled. Ignore those four; report any other.

## Why expiry is being removed, not fixed

`merge_levels` has always written a mandatory `expires` key so "a stop the agent set once" would not arm the monitor forever. `scripts/risk_review.py` was the only thing that pruned on it and was retired 2026-08-13; `apply_overrides` never read it. Task 4 (`6f9a4d0`) restored pruning inside `write_levels`, but review established that only mitigates the problem for symbols the agent keeps writing — a level set once and never revisited stayed unbounded, which is the original defect verbatim.

Closing it properly meant either making the monitor enforce a timer, or abandoning the timer. The principal chose the latter: **levels are the agent's judgement and should not expire on their own.** That is coherent with the rest of this system — the agent owns the decision, the code owns the mechanism.

## The hazard this creates, and why part 3 exists

With no expiry, an override outlives the position it was written for. While the name is not held it is inert (`apply_overrides` only sees names both in the book and owned). But on **re-entry** it wakes up:

1. Agent sets SNDK's stop at 1600 while it trades 1777, then sells. Override remains.
2. Weeks later SNDK re-enters the book at 900.
3. `apply_overrides` applies a stop when it *raises* the thesis stop — 1600 does, so it applies.
4. The monitor polls, sees 900 below a 1600 stop, and **sells the whole position at market within 15s.**

`set_levels` already refuses exactly this at **set** time, in its own words: *"it is breached the moment it is set, and the monitor would sell this whole position at market within seconds."* A stale override re-applying at **apply** time never passes through that guard. Same catastrophe, different door. Part 3 puts the same invariant at the other end.

---

### Part 1: Remove the expiry machinery

**Files:** Modify `src/agent_env/decide.py`

- [ ] **Step 1: Revert the expiry commit's logic**

Remove `prune_expired`, its call inside `write_levels`, the `_expiry` helper, and the `expires` key written by `merge_levels`. Remove the selftest assertions that cover them. Keep everything else in those functions untouched.

- [ ] **Step 2: Replace the comment that justified the key**

Where `merge_levels` explained that expiry was mandatory, state the decision instead:

```python
        # NO EXPIRY. A level is the agent's judgement and lasts until the agent
        # changes or clears it. An earlier design expired levels on a timer,
        # which meant protection could vanish on a schedule nobody chose; the
        # pruner that enforced it died with risk_review (2026-08-13) and went
        # unnoticed. The hazard this creates -- a level outliving its position --
        # is closed by clear_levels(), by the charter making a clear part of an
        # exit, and by apply_overrides refusing a stop at or above the current
        # price. Decision: principal, 2026-08-17.
```

- [ ] **Step 3: Run the selftests**

Run: `.venv/bin/python src/agent_env/decide.py --selftest` then `.venv/bin/python src/agent_env/server.py --selftest`
Expected: both PASS. `server.py`'s selftest asserts a written override carries a future `expires` — that assertion must be removed as part of this step, since the key no longer exists. If any other test still references `expires`, remove that reference too and say which in the report.

- [ ] **Step 4: Suite, reload, commit**

```bash
deploy/run_selftests.sh
.venv/bin/python scripts/reload_stale.py
git add src/agent_env/decide.py src/agent_env/server.py
git commit -m "revert(levels): levels do not expire — they last until the agent changes them

Task 4 restored pruning inside write_levels, but that only bounded levels for
symbols the agent kept writing; one set and never revisited stayed armed
forever, which is the original defect verbatim.

Expiry is removed rather than repaired. A stop that vanishes on a timer nobody
chose is not protection, and a level is the agent's judgement. The hazard this
creates -- a level outliving its position -- is closed by clear_levels(), by
the charter, and by the apply-time guard. Decision: principal 2026-08-17."
```

---

### Part 2: A mechanism to clear a level

**Files:** Modify `src/agent_env/decide.py`, `src/agent_env/server.py`

There is currently **no way for the agent to remove a level** — `set_levels` only writes. So "clear it when you close" is an instruction it cannot obey.

**Interfaces produced:** `decide.clear_level(existing: dict, symbol: str) -> dict` (pure) and MCP tool `clear_levels(symbol: str, reason: str) -> str`.

- [ ] **Step 1: Write the failing test in `decide._selftest`**

```python
    # A level must be removable: with no expiry, an override outlives its
    # position and wakes up on re-entry at a price it was never written for.
    ov = {"AAA": {"stop": 1.0, "reason": "x"}, "BBB": {"stop": 2.0, "reason": "y"}}
    out = clear_level(ov, "AAA")
    assert set(out) == {"BBB"}, out
    assert ov == {"AAA": {"stop": 1.0, "reason": "x"}, "BBB": {"stop": 2.0, "reason": "y"}}, \
        "clear_level must not mutate its input"
    assert clear_level(ov, "ZZZ") == ov, "clearing an absent symbol is a no-op, not an error"
    assert clear_level({}, "AAA") == {}, "clearing from an empty file is a no-op"
    assert clear_level(ov, "aaa") == clear_level(ov, "AAA"), "symbol match is case-insensitive"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python src/agent_env/decide.py --selftest`
Expected: FAIL, `NameError: name 'clear_level' is not defined`.

- [ ] **Step 3: Implement the pure helper**

```python
def clear_level(existing: dict, symbol: str) -> dict:
    """Return a NEW overrides dict with `symbol` removed. Never mutates.

    Clearing an absent symbol is a NO-OP, deliberately: the agent calling this
    after an exit should not have to know whether a level was ever set, and an
    error there would train it to skip the call.
    """
    sym = str(symbol).strip().upper()
    return {k: v for k, v in (existing or {}).items() if k != sym}
```

- [ ] **Step 4: Add the writer, mirroring `write_levels`**

```python
def clear_levels_file(symbol: str, path: Path | None = None) -> dict:
    """Remove one symbol's levels from overrides.json ATOMICALLY.

    Same os.replace discipline as write_levels: the monitor reads this file every
    poll and a torn read makes it drop ALL overrides for that tick.
    """
    path = path or OVERRIDES
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except Exception:                                   # noqa: BLE001
            existing = {}
    remaining = clear_level(existing, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(remaining, indent=2))
    os.replace(tmp, path)
    return remaining
```

- [ ] **Step 5: Expose the MCP tool in `server.py`**

Add beside `set_levels`:

```python
@mcp.tool()
def clear_levels(symbol: str, reason: str = "") -> str:
    """Remove YOUR stop/target for a symbol. Do this when you close a position.

    Levels do not expire. An override you leave behind is inert while the name
    is not held, but it WAKES UP if the name re-enters the book later -- at a
    price it was never written for. Clearing is part of closing a position, not
    bookkeeping after it.

    `reason` is required, and is journalled like any other decision.
    """
    sym = symbol.strip().upper()
    if not reason or not reason.strip():
        return json.dumps({"ok": False,
                           "error": "reason is required: a level cleared for no "
                                    "recorded reason cannot be judged later"}, indent=2)
    remaining = decide.clear_levels_file(sym)
    return json.dumps({"ok": True, "symbol": sym, "cleared": True,
                       "remaining_symbols": sorted(remaining)}, indent=2)
```

Journal the decision through the same path `set_levels` uses, so a clear appears in `research_log` alongside every other decision. Read `set_levels`' journalling and mirror it exactly; do not invent a second pattern.

- [ ] **Step 6: Add a server selftest**

Redirect `decide.OVERRIDES` to a temp path in a `try/finally`, write two symbols with `set_levels`, clear one, assert the other survives and the live file was never touched. Assert `clear_levels` refuses an empty reason.

- [ ] **Step 7: Suite, reload, commit**

```bash
deploy/run_selftests.sh
.venv/bin/python scripts/reload_stale.py
git add src/agent_env/decide.py src/agent_env/server.py
git commit -m "feat(levels): the agent can clear a level, not only set one

With expiry removed, an override outlives its position and wakes on re-entry
at a price it was never written for. There was no way to remove one:
set_levels only writes. 'Clear it when you close' was an instruction the agent
could not obey."
```

---

### Part 3: The apply-time guard (TOUCHES THE LIVE STOP WATCHER)

**Files:** Modify `scripts/market_monitor.py`, `src/level_rules.py`

⚠️ This is the highest-risk change in either plan. `apply_overrides` decides what the stop watcher enforces, and the watcher sells whole positions at market.

**Design constraint, already established — do not re-derive:** `apply_overrides` is called BEFORE quotes are fetched in the tick, so it has no live price. It must read the monitor's own `research_store/monitor/quotes.json`, which the monitor itself writes every 15s during RTH.

**⛔ FAIL CLOSED on an unknown price.** Refusing a stop override leaves the thesis stop in place — the position stays protected, merely less tightly. Applying a stale one liquidates it. So when no price is available for a symbol, the stop override is REFUSED. Targets are unaffected either way.

- [ ] **Step 1: Add the cases to the shared truth table**

In `src/level_rules.py`, add to `CASES` (keeping the existing key structure, and adding a `prices` key that existing cases omit):

```python
    {"name": "override stop at or above the current price is refused",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"stop": 160.0, "reason": "stale from a previous holding"},
     "prices": {"AAA": 150.0},
     "stop_after": 100.0, "targets_after": [130.0],
     "stop_enforced": False, "target_enforced": False},

    {"name": "override stop below the current price still applies",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"stop": 120.0, "reason": "genuine tightening"},
     "prices": {"AAA": 150.0},
     "stop_after": 120.0, "targets_after": [130.0],
     "stop_enforced": True, "target_enforced": False},

    {"name": "unknown price refuses the stop override (fail closed)",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"stop": 120.0, "reason": "tightening with no quote available"},
     "prices": {},
     "stop_after": 100.0, "targets_after": [130.0],
     "stop_enforced": False, "target_enforced": False},

    {"name": "unknown price does NOT block a target change",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"targets": [160.0], "reason": "target with no quote available"},
     "prices": {},
     "stop_after": 100.0, "targets_after": [160.0],
     "stop_enforced": False, "target_enforced": True},
```

Existing cases have no `prices` key. Update `level_rules._selftest` so the required-key check treats `prices` as optional, and document that a case without it means "prices not supplied".

- [ ] **Step 2: Update both selftests to pass `prices`, and watch them fail**

In `scripts/market_monitor.py:_selftest()`'s truth-table loop, pass `c.get("prices")` into `apply_overrides`.

Run: `/usr/bin/python3 scripts/market_monitor.py --selftest`
Expected: FAIL — `apply_overrides` takes no `prices` argument, and the three new stop cases do not match.

- [ ] **Step 3: Implement the guard**

Change the signature to `apply_overrides(held, overrides, prices=None)`. In the stop branch, before the existing raise/widen logic:

```python
            # ⛔ A STOP AT OR ABOVE THE CURRENT PRICE IS NOT A STOP -- it is
            # already breached, and this process sells the whole position at
            # market within one poll. set_levels refuses exactly this when a
            # level is SET; an override that outlives its position and wakes on
            # RE-ENTRY never passes through that check, so the same invariant
            # has to hold here. Levels no longer expire (principal, 2026-08-17),
            # which makes a stale override a permanent possibility rather than a
            # transient one.
            #
            # FAIL CLOSED: with no price we refuse the override and keep the
            # thesis stop -- the position stays protected, just less tightly.
            # Applying a stale stop instead would liquidate it. Refusing can
            # never unprotect; applying wrongly can.
            px = (prices or {}).get(sym)
            if not isinstance(px, (int, float)) or px <= 0 or float(ov_stop) >= float(px):
                ov_stop = None
```

Then keep the existing `if isinstance(ov_stop, (int, float)) and t.stop is not None:` logic, which now skips when `ov_stop` is None. Do NOT alter the target branch.

- [ ] **Step 4: Run both selftests**

Run: `/usr/bin/python3 scripts/market_monitor.py --selftest` → PASS
Run: `.venv/bin/python src/level_rules.py --selftest` → PASS

- [ ] **Step 5: Feed the live quotes at the call site**

At the `apply_overrides` call (~line 931), read the monitor's own last-written quotes and pass them:

```python
    try:
        _px = (json.loads((MON / "quotes.json").read_text()) or {}).get("prices", {})
    except Exception:                                       # noqa: BLE001
        _px = {}        # no quotes -> every stop override is refused (fail closed)
    if _ov:
        held = apply_overrides(held, _ov, _px)
```

- [ ] **Step 6: Update `decide.evaluate_enforcement` to report the refusal**

`decide.py` must not now claim `enforced: true` for a stop the monitor will refuse. Add the same price test there, reading the same `quotes.json`, and report `enforced: false` with a note naming the price it compared against. Add a `decide` selftest case. If the price is unknown, say so in the note rather than asserting either way.

- [ ] **Step 7: Suite, reload — CONFIRM THE MONITOR RESTARTED**

```bash
deploy/run_selftests.sh
.venv/bin/python scripts/reload_stale.py
systemctl show agentic-monitor.service -p ActiveEnterTimestamp
```

`reload_stale` MUST report restarting `agentic-monitor.service`. If it says nothing was stale, STOP — the running watcher does not have this guard and that is the whole point of the change.

- [ ] **Step 8: Commit**

```bash
git add scripts/market_monitor.py src/level_rules.py src/agent_env/decide.py
git commit -m "fix(monitor): refuse an override stop at or above the current price

set_levels already refuses a stop at or above spot when a level is SET -- it is
breached the instant it exists and the monitor sells the whole position at
market within seconds. An override that outlives its position and wakes on
RE-ENTRY never passes through that check. Levels no longer expire, so a stale
override is now a permanent possibility rather than a transient one.

Fails CLOSED: with no quote the override is refused and the thesis stop stands.
Refusing can never unprotect a position; applying a stale stop can liquidate it."
```

---

### Part 4: Show a level with no position

**Files:** Modify `src/agent_env/server.py`

A cleared level is only reliable if the agent can see what it left behind.

- [ ] **Step 1: Write the failing selftest**

Assert that when `overrides.json` holds a symbol the book does not hold, `positions()` surfaces it — under a distinct key such as `levels_without_positions`, listing symbol and the stored reason — and that the key is absent when every override corresponds to a held name.

- [ ] **Step 2: Run to verify it fails**, then implement, then re-run. Use the existing `_overrides()` helper and the held set already computed in `positions()`.

- [ ] **Step 3: Charter — clearing is part of closing**

In `prompts/charter.md`, near the level-adjustment paragraphs, add (no literal numbers):

```
**A level you set outlives the position.** Levels do not expire; they are yours
until you change or clear them. That is deliberate — protection should not
vanish on a timer you did not choose — but it means an override left behind
after an exit is still on file, and it wakes up if the name re-enters the book
later, at a price it was never written for. So clearing the level is part of
closing the position, not bookkeeping afterwards: call `clear_levels` when you
sell out of a name. `positions()` lists any level you hold for a name you do
not, so you can see what you left behind.
```

Run: `.venv/bin/python src/repo_checks.py` → no charter findings.

- [ ] **Step 4: Suite, reload, commit**

```bash
deploy/run_selftests.sh
.venv/bin/python scripts/reload_stale.py
git add src/agent_env/server.py prompts/charter.md
git commit -m "feat(levels): surface a level held for a name that is not

A cleared level is only reliable if the agent can see what it left behind.
positions() now lists any override whose symbol is not held, and the charter
makes clearing part of closing a position."
```

---

## Post-Plan Verification

- [ ] `deploy/run_selftests.sh` → ALL SELFTESTS PASSED
- [ ] `/usr/bin/python3 scripts/market_monitor.py --selftest` → PASS
- [ ] `.venv/bin/python scripts/reload_stale.py` → nothing stale, AND `agentic-monitor` was restarted during part 3
- [ ] `grep -rn "expires" src/agent_env/decide.py` → no matches
- [ ] `ls research_store/monitor/overrides.json` → still absent
- [ ] `.venv/bin/python src/repo_checks.py` → PASS once the trading timers are re-enabled
