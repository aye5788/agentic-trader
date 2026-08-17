# Agent-Set Price Levels — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the agent a working mechanism to set its own stops and take-profit targets, report truthfully what will be enforced, and show it the evidence needed to decide.

**Architecture:** `scripts/market_monitor.py:apply_overrides()` remains the sole authority on what is enforced — it is already correct and is not changed. Its advisory twin `src/agent_env/decide.py:evaluate_enforcement()` currently lies about target behaviour, and cannot import the monitor (which loads the moomoo SDK at module scope). So a new pure module `src/level_rules.py` holds one shared case table that BOTH selftests assert against, turning the duplication from a silent divergence into a failing test. On top of that, `set_levels` gains multi-target support, `positions()` gains excursion facts, and `session.py` gains a check that a recorded level decision produced a real artifact.

**Tech Stack:** Python 3.12 (`.venv`), Python 3.10 (`/usr/bin/python3`, moomoo only), stdlib + pandas. No test framework — every module carries a `_selftest()` invoked via `--selftest`, run by `deploy/run_selftests.sh`.

## Global Constraints

- **Two runtimes.** Everything here runs under `.venv/bin/python` (3.12) EXCEPT `scripts/market_monitor.py`, which imports moomoo at module scope and MUST run under `/usr/bin/python3` (3.10). `src/level_rules.py` must therefore import **stdlib only** so both interpreters can read it.
- **`apply_overrides()` enforcement semantics are NOT changed by this plan.** Only its advisory twin is corrected. Any task that alters monitor behaviour is out of scope.
- **No literal threshold may enter `prompts/charter.md`.** `src/repo_checks.py:check_charter_no_literals` enforces this and must keep passing.
- **No threshold, trigger, or automatic ratchet anywhere in code.** The agent picks every number.
- **After any edit, run `.venv/bin/python scripts/reload_stale.py`** before claiming a task done — `market_monitor` and the dashboard are long-running and hold stale code otherwise (CLAUDE.md).
- **Full suite green before each commit:** `deploy/run_selftests.sh`.
- **Push after every commit** — standing policy that GitHub mirrors the droplet.
- The live overrides file is `research_store/monitor/overrides.json`. **No task may write to it on the live box**; every test redirects via `decide.OVERRIDES` or a temp path.

## File Structure

| File | Responsibility | Task |
| --- | --- | --- |
| `src/level_rules.py` | **Create.** Pure, stdlib-only shared case table describing what `apply_overrides` does. Imported by both selftests. | 1 |
| `scripts/market_monitor.py` | **Modify** selftest only — assert `apply_overrides` matches the table. Enforcement code untouched. | 1 |
| `src/agent_env/decide.py` | **Modify.** Correct `evaluate_enforcement` target reporting; accept target lists in `merge_levels`/`write_levels`/`evaluate_enforcement`; restore expiry pruning. | 2, 3, 4 |
| `src/agent_env/server.py` | **Modify.** `set_levels` accepts a list; `positions()` merges excursion facts. | 3, 5 |
| `prompts/charter.md` | **Modify.** Describe the mechanism that exists. Ships WITH task 3. | 3 |
| `src/excursion.py` | **Create.** Pure run-up/peak/giveback/protected-gain arithmetic + entry-date resolution. | 5 |
| `scripts/session.py` | **Modify.** Session-end level verification. | 6 |
| `deploy/run_selftests.sh` | **Modify.** Register the two new modules. | 1, 5 |

---

### Task 1: Shared case table, pinned against real enforcement

**Files:**
- Create: `src/level_rules.py`
- Modify: `scripts/market_monitor.py` (selftest only)
- Modify: `deploy/run_selftests.sh`

**Interfaces:**
- Produces: `level_rules.CASES` — list of dicts with keys `name`, `thesis_stop`, `thesis_targets`, `ov` (the override dict), `stop_after`, `targets_after`, `stop_enforced`, `target_enforced`. Task 2 consumes this to pin `decide.evaluate_enforcement`.

- [ ] **Step 1: Write the shared table**

Create `src/level_rules.py`:

```python
"""SHARED TRUTH TABLE for what an override actually does to a thesis.

scripts/market_monitor.py:apply_overrides() is the ONLY authority on enforcement.
src/agent_env/decide.py:evaluate_enforcement() only REPORTS what it will do --
and cannot import it, because market_monitor loads the moomoo SDK at module
scope and runs under a different interpreter (system 3.10 vs .venv 3.12).

That duplication silently diverged: apply_overrides was changed to let targets
move in EITHER direction, and decide.py kept reporting the old lower-only rule,
so the agent was told a legal move was illegal. This table is the pin. Both
sides assert against it, so the next divergence is a failing test instead of a
lie told to the agent.

⛔ STDLIB ONLY. This module is imported by BOTH interpreters.
"""

# Each case: a thesis (stop + targets), an override, and what apply_overrides
# leaves behind. `stop_enforced` / `target_enforced` mean "the level actually
# changed", which is exactly what decide.py claims to predict.
CASES = [
    {"name": "stop raised is applied",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"stop": 108.0, "reason": "tighter"},
     "stop_after": 108.0, "targets_after": [130.0],
     "stop_enforced": True, "target_enforced": False},

    {"name": "stop lowered is ignored without widen",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"stop": 90.0, "reason": "looser"},
     "stop_after": 100.0, "targets_after": [130.0],
     "stop_enforced": False, "target_enforced": False},

    {"name": "stop lowered IS applied with widen + reason",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"stop": 90.0, "widen": True, "reason": "inside the noise"},
     "stop_after": 90.0, "targets_after": [130.0],
     "stop_enforced": True, "target_enforced": False},

    {"name": "widen without a reason is ignored",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"stop": 90.0, "widen": True, "reason": ""},
     "stop_after": 100.0, "targets_after": [130.0],
     "stop_enforced": False, "target_enforced": False},

    # THE DIVERGENCE. apply_overrides permits this; decide.py reported it as
    # ignored, which is why no take-profit has ever been reached.
    {"name": "target RAISED is applied",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"targets": [160.0], "reason": "let it run"},
     "stop_after": 100.0, "targets_after": [160.0],
     "stop_enforced": False, "target_enforced": True},

    {"name": "target lowered is applied",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"targets": [118.0], "reason": "pull it in"},
     "stop_after": 100.0, "targets_after": [118.0],
     "stop_enforced": False, "target_enforced": True},

    # THE BLOCKER. Every live thesis has two targets; set_levels sent one.
    {"name": "target count mismatch is ignored",
     "thesis_stop": 100.0, "thesis_targets": [130.0, 150.0],
     "ov": {"targets": [140.0], "reason": "one of two"},
     "stop_after": 100.0, "targets_after": [130.0, 150.0],
     "stop_enforced": False, "target_enforced": False},

    {"name": "full target list on a two-target thesis is applied",
     "thesis_stop": 100.0, "thesis_targets": [130.0, 150.0],
     "ov": {"targets": [140.0, 170.0], "reason": "both"},
     "stop_after": 100.0, "targets_after": [140.0, 170.0],
     "stop_enforced": False, "target_enforced": True},

    {"name": "malformed override changes nothing",
     "thesis_stop": 100.0, "thesis_targets": [130.0],
     "ov": {"stop": "not-a-number", "targets": "nope", "reason": "junk"},
     "stop_after": 100.0, "targets_after": [130.0],
     "stop_enforced": False, "target_enforced": False},
]
```

- [ ] **Step 2: Add the pin to `market_monitor._selftest`**

Find the existing `apply_overrides` assertions in `scripts/market_monitor.py:_selftest()` (around line 90) and append after them:

```python
    # ---- SHARED TRUTH TABLE ------------------------------------------------
    # src/agent_env/decide.py mirrors this function's arithmetic and cannot
    # import it. Both sides assert against src/level_rules.py so a divergence
    # fails a test instead of quietly misinforming the agent.
    import level_rules                                        # noqa: PLC0415
    for c in level_rules.CASES:
        th = Thesis(symbol="AAA", rank=1, verdict="buy",
                    stop=c["thesis_stop"], targets=list(c["thesis_targets"]),
                    target_weight=0.07)
        got = apply_overrides({"AAA": th}, {"AAA": c["ov"]})["AAA"]
        assert got.stop == c["stop_after"], (c["name"], got.stop)
        assert list(got.targets) == c["targets_after"], (c["name"], got.targets)
        assert (got.stop != c["thesis_stop"]) == c["stop_enforced"], c["name"]
        assert (list(got.targets) != c["thesis_targets"]) == c["target_enforced"], \
            c["name"]
```

`Thesis` is already imported at the top of `_selftest` (`from research_store.models import Thesis`) and `src` is already on `sys.path` for that import to work, so `import level_rules` needs no extra path handling. Reuse that constructor exactly — its required fields are `symbol`, `rank`, `verdict`, `stop`, `targets`, `target_weight`.

- [ ] **Step 3: Run it — the table must match reality**

Run: `/usr/bin/python3 scripts/market_monitor.py --selftest`
Expected: PASS. If any case fails, **the table is wrong, not the monitor** — `apply_overrides` is the authority. Correct `CASES` to describe actual behaviour and re-run.

- [ ] **Step 4: Register the new module**

In `deploy/run_selftests.sh`, add to `VENV_SELFTESTS`:

```
    "src/level_rules.py"
```

`level_rules.py` has no `_selftest` yet — add one so the entry is honest:

```python
def _selftest() -> None:
    assert CASES, "the table must not be empty"
    keys = {"name", "thesis_stop", "thesis_targets", "ov", "stop_after",
            "targets_after", "stop_enforced", "target_enforced"}
    for c in CASES:
        assert keys <= set(c), (c.get("name"), keys - set(c))
    names = [c["name"] for c in CASES]
    assert len(names) == len(set(names)), "case names must be unique"
    print("selftest OK: level_rules -- shared enforcement table is well-formed")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
```

- [ ] **Step 5: Full suite + reload + commit**

```bash
deploy/run_selftests.sh
.venv/bin/python scripts/reload_stale.py
git add src/level_rules.py scripts/market_monitor.py deploy/run_selftests.sh
git commit -m "test(levels): pin apply_overrides against a shared truth table

decide.py mirrors this arithmetic and cannot import it (moomoo, different
interpreter). The duplication diverged silently on target direction. Both
sides now assert against src/level_rules.py, so the next divergence fails a
test instead of misinforming the agent."
git push origin main
```

---

### Task 2: `decide.py` reports what is actually enforced (C2)

**Files:**
- Modify: `src/agent_env/decide.py:79-205` (`evaluate_enforcement`)

**Interfaces:**
- Consumes: `level_rules.CASES` from Task 1.
- Produces: `evaluate_enforcement(...)` unchanged in signature, corrected in behaviour. Task 3 extends its `target` parameter to accept a list.

- [ ] **Step 1: Write the failing pin in `decide._selftest`**

Append to `src/agent_env/decide._selftest()`:

```python
    # ---- AGREEMENT WITH REAL ENFORCEMENT ----------------------------------
    # This is the regression that would have caught the target-direction lie.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import level_rules                                        # noqa: PLC0415
    for c in level_rules.CASES:
        ov = c["ov"]
        ov_targets = ov.get("targets")
        r = evaluate_enforcement(
            stop=ov.get("stop") if isinstance(ov.get("stop"), (int, float))
                 else c["thesis_stop"],
            target=ov_targets,
            has_thesis=True, target_weight=0.07, owned=True,
            current_stop=c["thesis_stop"],
            current_targets=list(c["thesis_targets"]))
        assert r["stop"]["enforced"] == c["stop_enforced"], (c["name"], r["stop"])
        if ov_targets is not None:
            assert r["target"]["enforced"] == c["target_enforced"], \
                (c["name"], r["target"])
```

- [ ] **Step 2: Run it — expect failures on the target cases**

Run: `.venv/bin/python src/agent_env/decide.py --selftest`
Expected: FAIL on `"target RAISED is applied"` (decide reports `enforced: False`, table says `True`) and on `"full target list on a two-target thesis is applied"`.

- [ ] **Step 3: Correct the target reporting**

In `evaluate_enforcement`, replace the target branch (currently requiring `len(cur) != 1` and `target < cur[0]`) with:

```python
    if target is None:
        target_result = {"enforced": False, "note": "no target was set"}
    else:
        cur = list(current_targets or [])
        new = list(target) if isinstance(target, (list, tuple)) else [target]
        if not all(isinstance(o, (int, float)) for o in new):
            target_result = {"enforced": False,
                             "note": "targets must all be numbers -- ignored" + unverified}
        elif len(new) != len(cur):
            # THE refusal an agent on this book actually meets: every live
            # thesis carries two targets. Name the expected count so the agent
            # can retry correctly instead of inferring it.
            target_result = {"enforced": False,
                             "note": f"this thesis has {len(cur)} target(s) and you supplied "
                                     f"{len(new)}; the monitor applies a target list only when "
                                     f"the count matches -- supply all {len(cur)}" + unverified}
        elif [float(o) for o in new] == [float(o) for o in cur]:
            target_result = {"enforced": False,
                             "note": "identical to the thesis's current targets -- "
                                     "nothing to change" + unverified}
        else:
            # EITHER DIRECTION. apply_overrides permits raising as well as
            # lowering: raising adds no risk of loss, since the stop is
            # unchanged. Reporting otherwise is why no take-profit in this book
            # has ever been reached.
            target_result = {"enforced": True,
                             "note": f"replaces the thesis's targets ({cur}) -- "
                                     "will be applied at the next monitor poll" + unverified}
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python src/agent_env/decide.py --selftest`
Expected: PASS, including the two previously failing cases.

- [ ] **Step 5: Full suite + reload + commit**

```bash
deploy/run_selftests.sh
.venv/bin/python scripts/reload_stale.py
git add src/agent_env/decide.py
git commit -m "fix(levels): decide.py now reports what the monitor actually enforces

apply_overrides was changed to let targets move in either direction, with a
comment noting the old one-way rule 'is why nothing in this book has ever
reached a take-profit'. decide.py -- the layer the agent reads -- kept
reporting the old rule, so the agent was told a legal move was illegal.

The count-mismatch note now names the expected count. That is the only
refusal an agent meets on this book, and the charter never explained it."
git push origin main
```

---

### Task 3: `set_levels` accepts a target list, and the charter says so (C1 + C5)

**Files:**
- Modify: `src/agent_env/decide.py` (`merge_levels`, `write_levels`)
- Modify: `src/agent_env/server.py` (`set_levels`)
- Modify: `prompts/charter.md`

**Interfaces:**
- Consumes: corrected `evaluate_enforcement` from Task 2.
- Produces: `merge_levels(existing, symbol, stop, targets, reason, ts)` and `write_levels(symbol, stop, targets, reason, ts, path=None)` where `targets` is `None`, a number, or a list of numbers. `set_levels(symbol, stop, targets=0.0, reason="")` accepts a number or a list.

- [ ] **Step 1: Write the failing test in `decide._selftest`**

```python
    # A two-target thesis must be expressible. This is the blocker: every live
    # thesis has two targets and set_levels only ever accepted one, so a target
    # change was refused 100% of the time.
    out = merge_levels({}, "AAA", 100.0, [140.0, 170.0], "both", "2026-08-17T00:00:00+00:00")
    assert out["AAA"]["targets"] == [140.0, 170.0], out
    # a bare number still works for a one-target thesis
    out = merge_levels({}, "AAA", 100.0, 140.0, "one", "2026-08-17T00:00:00+00:00")
    assert out["AAA"]["targets"] == [140.0], out
    # no target at all
    out = merge_levels({}, "AAA", 100.0, None, "stop only", "2026-08-17T00:00:00+00:00")
    assert out["AAA"]["targets"] == [], out
    # EVERY target must clear the stop, not just the first
    try:
        merge_levels({}, "AAA", 100.0, [140.0, 90.0], "bad", "2026-08-17T00:00:00+00:00")
        raise AssertionError("a target below the stop must be refused")
    except ValueError as e:
        assert "at or below stop" in str(e), e
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python src/agent_env/decide.py --selftest`
Expected: FAIL — `merge_levels` calls `float(target)` on a list and raises `ValueError: target [140.0, 170.0] is not a number`.

- [ ] **Step 3: Implement list support in `merge_levels`**

Replace the target-validation block in `merge_levels` with:

```python
    # `target` accepts None, a single number, or a list matching the thesis's
    # target count. The list form is the one that matters: every live thesis
    # carries two, and the single form was refused by the monitor every time.
    if target is None:
        ts_list = []
    elif isinstance(target, (list, tuple)):
        ts_list = list(target)
    else:
        ts_list = [target]
    parsed = []
    for raw in ts_list:
        try:
            t = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"target {raw!r} is not a number")
        if not math.isfinite(t) or t <= 0:
            raise ValueError(f"target {raw!r} must be a finite positive price")
        if t <= s:
            raise ValueError(f"target {t} is at or below stop {s}; it would trigger "
                             "immediately")
        parsed.append(t)
```

Then in the dict built below, replace the `"target"` / `"targets"` entries with:

```python
        "target": parsed[0] if parsed else None,   # legacy singular; the monitor
                                                    # does NOT read this key
        "targets": parsed,                          # the shape apply_overrides reads
```

Rename the parameter from `target` to `targets` in both `merge_levels` and
`write_levels` signatures, updating the docstrings to say a list is accepted.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python src/agent_env/decide.py --selftest`
Expected: PASS.

- [ ] **Step 5: Widen the MCP tool**

In `src/agent_env/server.py:set_levels`, change the signature to:

```python
def set_levels(symbol: str, stop: float, targets=0.0, reason: str = "") -> str:
```

Immediately after `sym = symbol.strip().upper()`, normalise:

```python
    # Accept 0/None (no target), one number, or the full list. A thesis with
    # two targets REQUIRES two here -- the monitor applies a target list only
    # when the count matches, so a single value on a two-target thesis is
    # silently ignored. positions() shows you the current list.
    if targets in (0, 0.0, None, ""):
        _targets = None
    elif isinstance(targets, (list, tuple)):
        _targets = list(targets)
    else:
        _targets = [targets]
```

Pass `_targets` wherever the old `target` was passed to `evaluate_enforcement`
and `write_levels`. Update the docstring's stale sentence — *"your target is
applied only if its count matches the thesis's existing targets and it LOWERS
every one"* — to say the count must match and the values may move in either
direction.

- [ ] **Step 6: Update the charter (ships WITH this task, never before)**

In `prompts/charter.md`, extend the enforcement-false list (the paragraph
beginning **"But a level is not enforced until the tool says it is."**) so the
final sentence reads:

```
will be **false** — and the position unprotected overnight — whenever the name
has no thesis in tonight's book, is not yet confirmed owned by the broker, your
stop is looser than the one already set, or the target list you supplied does
not match the number of targets the thesis carries. `positions()` shows you
that list; supply all of them.
```

Verify no literal number entered the template:

Run: `.venv/bin/python src/repo_checks.py`
Expected: `repo_checks: PASS`

- [ ] **Step 7: Live verification against a real two-target thesis**

```bash
.venv/bin/python -c "
import sys, json; sys.path.insert(0,'src'); sys.path.insert(0,'.')
from agent_env import decide
import tempfile, pathlib
decide.OVERRIDES = pathlib.Path(tempfile.mkdtemp())/'overrides.json'
from agent_env import server
print(server.set_levels('AMD', 460.0, [600.0, 700.0], 'plan task 3 verification'))
"
```

Expected: `enforcement.target.enforced: true`. **`decide.OVERRIDES` is redirected
to a temp path — this must NOT write the live overrides file.** Confirm:

```bash
ls research_store/monitor/overrides.json 2>&1
```
Expected: `No such file or directory`.

- [ ] **Step 8: Full suite + reload + commit**

```bash
deploy/run_selftests.sh
.venv/bin/python scripts/reload_stale.py
git add src/agent_env/decide.py src/agent_env/server.py prompts/charter.md
git commit -m "feat(levels): set_levels can express a two-target thesis

Every live thesis carries two targets; set_levels accepted one, and the
monitor applies a target list only when the count matches. So a take-profit
change was refused 100% of the time and the agent was correctly told it
could not do the thing the charter says is a normal move.

The charter ships in the same commit, never ahead of it: describing a
capability before its mechanism exists is what produced this defect."
git push origin main
```

---

### Task 4: Restore expiry pruning (adjacent defect)

**Files:**
- Modify: `src/agent_env/decide.py`

**Interfaces:**
- Produces: `prune_expired(overrides: dict, today: str) -> dict` — pure.

`merge_levels` mandates an `expires` key, commenting that without it "a stop the
agent set once kept arming the live monitor" forever. `scripts/risk_review.py`
was the only thing that pruned on it and was retired 2026-08-13.
`apply_overrides` never reads `expires`. So the key is inert today and an
agent-set level never expires.

- [ ] **Step 1: Write the failing test**

```python
    # EXPIRY IS INERT. risk_review was the only pruner and it was retired
    # 2026-08-13; apply_overrides never reads `expires`. An agent-set level
    # therefore lives forever, which is exactly what the mandatory `expires`
    # key was written to prevent.
    ov = {"AAA": {"stop": 1.0, "expires": "2026-01-01"},
          "BBB": {"stop": 2.0, "expires": "2099-01-01"},
          "CCC": {"stop": 3.0}}                      # no key -> never expires
    kept = prune_expired(ov, "2026-08-17")
    assert set(kept) == {"BBB", "CCC"}, kept
    assert kept["BBB"]["stop"] == 2.0, kept
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python src/agent_env/decide.py --selftest`
Expected: FAIL with `NameError: name 'prune_expired' is not defined`.

- [ ] **Step 3: Implement**

```python
def prune_expired(overrides: dict, today: str) -> dict:
    """Drop overrides whose `expires` date has passed. Pure.

    Mirrors the rule the retired scripts/risk_review.py applied:
    keep when `expires >= today`, with a "9999" default so an entry written
    without the key is kept rather than silently discarded.
    """
    out = {}
    for sym, rec in (overrides or {}).items():
        if not isinstance(rec, dict):
            continue
        if str(rec.get("expires", "9999")) >= str(today):
            out[sym] = rec
    return out
```

Call it inside `write_levels`, on the file just read, before merging:

```python
    existing = prune_expired(existing, _dt.date.today().isoformat())
```

(`import datetime as _dt` at the top of the module if not already present.)

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python src/agent_env/decide.py --selftest`
Expected: PASS.

- [ ] **Step 5: Full suite + reload + commit**

```bash
deploy/run_selftests.sh
.venv/bin/python scripts/reload_stale.py
git add src/agent_env/decide.py
git commit -m "fix(levels): expired overrides are pruned again

merge_levels mandates an expires key so 'a stop the agent set once' does not
keep arming the live monitor forever. risk_review.py was the only thing that
pruned on it and was retired 2026-08-13; apply_overrides never read it. The
key has been inert since, and an agent-set level never expired."
git push origin main
```

---

### Task 5: `positions()` shows run-up, peak, giveback and protected gain (C3)

**Files:**
- Create: `src/excursion.py`
- Modify: `src/agent_env/server.py` (`positions`)
- Modify: `deploy/run_selftests.sh`

**Interfaces:**
- Produces: `excursion.facts(cost, mark, stop, highs) -> dict` with keys
  `peak_pct`, `giveback_pct`, `gain_protected_pct` (all `float | None`), and
  `excursion.entry_date(events, symbol) -> str | None`.

- [ ] **Step 1: Write the failing test**

Create `src/excursion.py` containing ONLY this selftest first (implementation in step 3):

```python
def _selftest() -> None:
    # ran to 60, now 50, cost 40, stop 44
    f = facts(cost=40.0, mark=50.0, stop=44.0, highs=[42.0, 60.0, 50.0])
    assert round(f["peak_pct"], 4) == 0.5, f            # 60/40 - 1
    assert round(f["giveback_pct"], 4) == 0.25, f       # 0.50 - 0.25
    assert round(f["gain_protected_pct"], 4) == 0.4, f  # (44-40)/(50-40)

    # STOP BELOW COST: showing a profit, would close at a LOSS. This is the
    # AMD/TER case and it must read as negative, not as zero or absent.
    f = facts(cost=100.0, mark=110.0, stop=95.0, highs=[112.0])
    assert f["gain_protected_pct"] < 0, f
    assert round(f["gain_protected_pct"], 4) == -0.5, f  # (95-100)/(110-100)

    # no gain -> protected share is undefined, never a divide-by-zero
    f = facts(cost=100.0, mark=100.0, stop=90.0, highs=[100.0])
    assert f["gain_protected_pct"] is None, f

    # no price history -> null, never a fabricated peak
    f = facts(cost=100.0, mark=110.0, stop=95.0, highs=[])
    assert f["peak_pct"] is None and f["giveback_pct"] is None, f

    # entry date = earliest still-open buy
    ev = [{"event": "execution", "ts": "2026-08-03T14:00:00+00:00",
           "fills": [{"symbol": "AAA", "side": "buy", "status": "filled"}]},
          {"event": "execution", "ts": "2026-08-07T14:00:00+00:00",
           "fills": [{"symbol": "AAA", "side": "buy", "status": "filled"}]}]
    assert entry_date(ev, "AAA") == "2026-08-03", entry_date(ev, "AAA")
    assert entry_date(ev, "ZZZ") is None

    # AMBIGUOUS: a full exit between buys means the earliest buy is NOT this
    # lot's entry. Report nothing rather than a confident wrong peak.
    ev2 = ev[:1] + [{"event": "execution", "ts": "2026-08-05T14:00:00+00:00",
                     "fills": [{"symbol": "AAA", "side": "sell", "status": "filled"}]}] + ev[1:]
    assert entry_date(ev2, "AAA") is None, "a sell between buys makes entry ambiguous"

    print("selftest OK: excursion -- peak/giveback/protected-gain, negative when "
          "the stop sits below cost, null rather than guessed")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python src/excursion.py --selftest`
Expected: FAIL with `NameError: name 'facts' is not defined`.

- [ ] **Step 3: Implement**

Prepend to `src/excursion.py`:

```python
"""How far a position ran, and how much of that run is actually protected.

positions() showed qty/cost/mark/stop and nothing about the PATH. So the agent
could not see what a human reads off the same book in seconds: that a position
up 48% would keep only a fraction of that gain if its stop fired, or that a
position showing a profit would close red because the stop never followed the
price up.

No threshold lives here. These are facts the agent reads; what to do about them
is its decision.
"""
from __future__ import annotations


def facts(cost: float, mark: float, stop, highs) -> dict:
    """Excursion facts for one position. Pure. None where undefined."""
    out = {"peak_pct": None, "giveback_pct": None, "gain_protected_pct": None}
    try:
        cost = float(cost); mark = float(mark)
    except (TypeError, ValueError):
        return out
    if cost <= 0:
        return out
    prices = [float(h) for h in (highs or []) if isinstance(h, (int, float))]
    if prices:
        peak = max(max(prices), mark)          # today's mark can exceed the panel
        out["peak_pct"] = peak / cost - 1.0
        out["giveback_pct"] = out["peak_pct"] - (mark / cost - 1.0)
    gain = mark - cost
    if stop is not None and gain > 0:
        # NEGATIVE when the stop sits below cost: the position shows a profit
        # and would close at a loss. Stated, not implied.
        out["gain_protected_pct"] = (float(stop) - cost) / gain
    return out


def entry_date(events, symbol: str):
    """Date of the earliest buy in the CURRENT holding period, or None.

    A sell between buys means the earliest buy belongs to a closed lot, so the
    entry is ambiguous -- return None. A confident wrong peak is worse than an
    absent one.
    """
    first_buy = None
    for e in events or []:
        if e.get("event") != "execution":
            continue
        day = str(e.get("ts") or "")[:10]
        for f in e.get("fills") or []:
            if f.get("symbol") != symbol or f.get("status") != "filled":
                continue
            if f.get("side") == "buy" and first_buy is None:
                first_buy = day
            elif f.get("side") == "sell" and first_buy is not None:
                return None                    # ambiguous: lot closed and reopened
    return first_buy
```

Add the `__main__` guard used by every other module:

```python
if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python src/excursion.py --selftest`
Expected: PASS.

- [ ] **Step 5: Wire into `positions()`**

In `src/agent_env/server.py`, after `out = state.holdings(v, prod.theses if prod else [], _overrides())`:

```python
    # Excursion facts: what the path looked like, not just where it stands.
    # I/O lives here; the arithmetic is pure in src/excursion.py.
    try:
        import pandas as _pd                                   # noqa: PLC0415
        import excursion                                       # noqa: PLC0415
        _hi = _pd.read_parquet(REPO / "research_store" / "prices" / "highs.parquet")
        _events = [json.loads(l) for l in
                   (REPO / "research_store" / "journal.jsonl").read_text().splitlines()
                   if l.strip()]
    except Exception:
        _hi, _events = None, None
    if _hi is not None:
        for _sym, _p in out.items():
            if not isinstance(_p, dict) or _p.get("avg_cost") is None:
                continue
            _ent = excursion.entry_date(_events, _sym)
            _series = []
            if _ent is not None and _sym in _hi.columns:
                _series = [x for x in _hi.loc[_hi.index >= _ent, _sym].dropna().tolist()]
            _p.update(excursion.facts(_p.get("avg_cost"), _p.get("mark"),
                                      _p.get("stop"), _series))
            if _ent is None:
                _p["excursion_note"] = ("entry ambiguous (this name was exited and "
                                        "re-entered) — peak not computed")
```

- [ ] **Step 6: Verify against the live book**

```bash
.venv/bin/python -c "
import sys, json; sys.path.insert(0,'src'); sys.path.insert(0,'.')
from agent_env import server
p = json.loads(server.positions())
for s, d in p.items():
    if isinstance(d, dict) and 'gain_protected_pct' in d:
        print(s, 'peak', d['peak_pct'], 'giveback', d['giveback_pct'],
              'protected', d['gain_protected_pct'])
" 2>&1 | grep -v -i warning
```

Expected: every held symbol prints three values, and **AMD and TER show a
negative `gain_protected_pct`** — their stops sit below cost.

- [ ] **Step 7: Register, suite, reload, commit**

Add `"src/excursion.py"` to `VENV_SELFTESTS` in `deploy/run_selftests.sh`, then:

```bash
deploy/run_selftests.sh
.venv/bin/python scripts/reload_stale.py
git add src/excursion.py src/agent_env/server.py deploy/run_selftests.sh
git commit -m "feat(levels): positions() shows run-up, peak, giveback, protected gain

The agent saw a stop price and a mark, and nothing about the path -- so it
could not see that a position up 48% keeps only a fraction of that gain if
the stop fires, or that AMD and TER show a profit and would close red.
gain_protected_pct is negative in exactly that case, stated rather than
implied. No threshold attaches to any of it."
git push origin main
```

---

### Task 6: Session-end level verification (C4)

**Files:**
- Modify: `scripts/session.py`

**Interfaces:**
- Produces: `level_claim_unmet(decisions, overrides_before, overrides_after) -> str | None` — pure.

- [ ] **Step 1: Write the failing test in `session._selftest`**

```python
    # A session that RECORDS a level change and leaves overrides.json untouched
    # has not made one. On 2026-08-12 and 08-14 sessions recorded stop
    # tightenings on SNDK/STX/AMD; overrides.json never existed, so none took
    # effect -- and the 08-16 investor letter reported the de-risking as done.
    d = [{"event": "agent_decision", "action": "tighten_stops", "symbol": "PORTFOLIO"}]
    assert level_claim_unmet(d, {}, {}) is not None
    assert "tighten_stops" in level_claim_unmet(d, {}, {})
    # ...but not when the file actually changed
    assert level_claim_unmet(d, {}, {"SNDK": {"stop": 1.0}}) is None
    # ...and not when no level decision was recorded
    assert level_claim_unmet([{"event": "agent_decision", "action": "hold"}], {}, {}) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python scripts/session.py --selftest`
Expected: FAIL with `NameError: name 'level_claim_unmet' is not defined`.

- [ ] **Step 3: Implement**

```python
# Actions whose whole point is to move a level. If one of these is recorded and
# the override file is byte-identical afterwards, the decision did not bind.
LEVEL_ACTIONS = ("tighten_stops", "tighten_stop", "set_levels", "lower_tp",
                 "raise_target", "ratchet_stop")


def level_claim_unmet(decisions, overrides_before, overrides_after):
    """A recorded level change with no matching artifact. Pure. None if fine.

    Does NOT block or retry -- it makes the claim/artifact gap visible, the
    same way unrecorded_fills catches a claimed fill with no execution.
    """
    claimed = [d.get("action") for d in (decisions or [])
               if d.get("event") == "agent_decision"
               and str(d.get("action") or "") in LEVEL_ACTIONS]
    if not claimed:
        return None
    if (overrides_before or {}) != (overrides_after or {}):
        return None
    return (f"session recorded {sorted(set(claimed))} but "
            f"research_store/monitor/overrides.json is unchanged — the level "
            f"decision did not take effect; check the enforcement object "
            f"set_levels returned")
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python scripts/session.py --selftest`
Expected: PASS.

- [ ] **Step 5: Call it from `run()`**

Add near the other module constants in `scripts/session.py`:

```python
OVERRIDES = REPO / "research_store" / "monitor" / "overrides.json"


def _read_overrides() -> dict:
    """Current override file, {} when absent or torn. Never raises."""
    try:
        return json.loads(OVERRIDES.read_text())
    except Exception:                                          # noqa: BLE001
        return {}


def _decisions_since(journal, since_ts: str) -> list:
    """agent_decision events written at or after `since_ts`. Never raises."""
    out = []
    try:
        for line in journal.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get("event") == "agent_decision" and str(e.get("ts") or "") >= since_ts:
                out.append(e)
    except Exception:                                          # noqa: BLE001
        pass
    return out
```

In `run()`, immediately before `before = integrity.snapshot(REPO)`:

```python
        ov_before = _read_overrides()
        ov_started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
```

In the `finally` block, beside the existing integrity verification:

```python
        warn = level_claim_unmet(
            _decisions_since(REPO / "research_store" / "journal.jsonl", ov_started_at),
            ov_before, _read_overrides())
        if warn:
            print(f"LEVEL WARNING: {warn}")
            result["level_warning"] = warn
```

Use whatever name the `finally` block already uses for the dict it returns in
place of `result`; if it builds the dict after the `finally`, store the string
on a local and attach it where the dict is assembled.

⛔ Do NOT change the session's exit status. The trading already happened, and a
bookkeeping gap must never turn a completed session into a retryable one —
`should_retry()` is deliberately narrow because a retry re-runs a session that
may have ALREADY PLACED ORDERS.

- [ ] **Step 6: Full suite + reload + commit**

```bash
deploy/run_selftests.sh
.venv/bin/python scripts/reload_stale.py
git add scripts/session.py
git commit -m "feat(levels): flag a recorded level change that left no artifact

Sessions on 08-12 and 08-14 recorded deliberate stop tightenings on SNDK, STX
and AMD. overrides.json never existed, so none took effect -- and the 08-16
investor letter reported the de-risking to the account owner as done. Nothing
compared the claim to the artifact. Surfaces only; never blocks or retries."
git push origin main
```

---

## Post-Plan Verification

- [ ] `deploy/run_selftests.sh` → `ALL SELFTESTS PASSED`
- [ ] `.venv/bin/python src/repo_checks.py` → `PASS`
- [ ] `.venv/bin/python -c "import sys;sys.path.insert(0,'src');import health;print(sum(1 for c in health.checks() if c.status=='ok'))"` → 13
- [ ] `.venv/bin/python scripts/reload_stale.py` → nothing stale
- [ ] `ls research_store/monitor/overrides.json` → still absent (no task writes it live)
- [ ] Re-enable scheduling: `systemctl enable --now agentic-session@open.timer`
- [ ] Manual session when ready: `systemctl start agentic-session@open.service`
