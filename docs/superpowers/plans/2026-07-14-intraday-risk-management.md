# Intraday Risk-Management Overlay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a defensive, de-risk-only risk-management overlay that tends open positions between weekly rebalances via two scheduled agentic reviews plus a new stricter-only geometry override the always-on monitor enforces.

**Architecture:** A deterministic core (`scripts/risk_review.py`, no LLM) builds per-position facts, validates every proposed change against a hard one-way (risk-reducing only) invariant, and writes geometry *overrides* + *deferred intents* to the Research Store. An agentic procedure (`prompts/risk_review.md`) run twice daily by cron supplies the judgment and places immediate orders via the RH MCP. The existing pure-Python monitor (`scripts/market_monitor.py`) overlays those overrides (stricter-only) and enforces them in real time — the same actuator that already runs the catastrophe stop.

**Tech Stack:** Python 3 (stdlib `tomllib`, `json`, `dataclasses`), the repo's existing `src/` modules (`strategy`, `governance`, `research_store`, `marks`, `momentum`, `notify`, `adapters.schwab`, `adapters.alpaca`), headless Claude Code (`claude -p`), cron. **Testing is inline `--selftest` functions run via `python scripts/<name>.py --selftest` — the `scripts/fast_loop.py` pattern. This repo has no `tests/` dir and no pytest; do NOT introduce them.**

## Global Constraints

- **De-risk only, code-enforced:** a written stop must be `>=` the current stop; a written take-profit must be `<=` the current one; trim/exit only reduce size; never open an entry. Reject any violation before it persists. Copied verbatim from spec §2.2 / §8.
- **Ships SAFE / alert-only:** when `governance.live_approved(cfg)` is false OR `[risk_review].alert_only` is true, the review journals + phone-pushes but places NO orders and writes NO overrides. (spec §8, §11)
- **Only two actuators:** the monitor (turns stored levels → real-time sells) and an agentic MCP order. Every output ends at one or is a note. (spec §7)
- **Reuse, don't reinvent:** use `src/governance.py`, `src/marks.py`, `src/notify.py`, `research_store.store.append_journal`, `research_store.read_current`, `src/strategy.py` — do not duplicate their logic.
- **Model pinned:** every headless `claude -p` call passes `--model claude-opus-4-8` (matches `deploy/run_fast_loop.sh`). `ANTHROPIC_API_KEY` must be unset in the wrapper (per CLAUDE.md footgun).
- **Overrides are intra-week overlays** cleared by the weekly `slow_loop.py` rebuild; never mutate the product in place. (spec §7 reconciliation)
- **Commit after every task.** Do not push unless asked (the maintainer syncs the repo).

---

## File Structure

- **Create `scripts/risk_review.py`** — deterministic core: config access, the one-way invariant validator, override/deferred-intent read-write helpers, the per-position fact-builder, `main()` orchestration, and `_selftest()`.
- **Create `prompts/risk_review.md`** — the agentic procedure (both passes; mode inferred from ET time).
- **Create `deploy/run_risk_review.sh`** — cron wrapper (KEY guard, model pin).
- **Modify `config/strategy.toml`** — add the `[risk_review]` table.
- **Modify `scripts/market_monitor.py`** — overlay stricter-only overrides onto held levels.
- **Modify `scripts/slow_loop.py`** — clear the week's overrides/intents on the weekly rebuild (spec §7).
- **Modify `scripts/letter_facts.py`** — surface confirmed de-risk actions in the weekly letter facts.
- **Modify `deploy/crontab.template`** — two entries (12:00, 15:45 ET).
- **New runtime artifacts (created at runtime, git-ignored under `research_store/`):** `research_store/monitor/overrides.json`, `research_store/monitor/deferred_intents.json`, `research_store/rh/risk_review_facts.json`, `research_store/rh/risk_review_decisions.json`.

Reviewer note: Tasks 1–6 are pure Python with `--selftest` coverage and can be reviewed/rejected independently. Task 7 (prompt) depends on the file contract from Task 6. Tasks 8–9 are deployment; Task 10 (weekly override clear) is an independent one-liner in `slow_loop.py`.

---

### Task 1: Add the `[risk_review]` config table

**Files:**
- Modify: `config/strategy.toml` (append a new table after the `[reentry]` table)

**Interfaces:**
- Produces: a `[risk_review]` table readable via `strategy.load()["risk_review"]` with keys `enabled` (bool), `alert_only` (bool), `midday_et` (str "HH:MM"), `close_et` (str "HH:MM"), `ma_break_days` (int), `giveback_flag_pct` (float), `vol_expansion_mult` (float), `earnings_window_days` (int).

- [ ] **Step 1: Add the table**

Append to `config/strategy.toml`:

```toml
# --- Intraday risk-management overlay (docs/superpowers/specs/2026-07-14-*) ---
# Two scheduled agentic reviews tend held positions between weekly rebuilds.
# DE-RISK ONLY: may tighten stops, lower take-profits, trim, or exit — never
# loosen a stop, extend a target, or open an entry (enforced in risk_review.py).
[risk_review]
enabled              = true
alert_only           = true    # ships SAFE: journal + push, place/override NOTHING
                               #   (also forced alert-only whenever live_approved=false)
midday_et            = "12:00" # intraday check (may act now)
close_et             = "15:45" # pre-close tend (before 16:00 so orders place in RTH)
# Attention thresholds — starting points that surface a name for closer LLM look;
# they are NOT hard triggers (the agent judges every held name each pass).
ma_break_days        = 21      # revives the specced-but-dead ma_exit line
giveback_flag_pct    = 0.10    # flag when a name gives back >10% from its high since entry
vol_expansion_mult   = 1.75    # flag when daily sigma now > 1.75x the sigma at entry
earnings_window_days = 5       # flag earnings within this many days (overnight-gap risk)
```

- [ ] **Step 2: Verify it loads**

Run: `.venv/bin/python -c "import sys; sys.path.insert(0,'src'); import strategy; c=strategy.load()['risk_review']; print(c['enabled'], c['midday_et'], c['ma_break_days'])"`
Expected: `True 12:00 21`

- [ ] **Step 3: Commit**

```bash
git add config/strategy.toml
git commit -m "feat(risk-review): add [risk_review] config table"
```

---

### Task 2: The one-way de-risk invariant validator

The single most important guardrail. A pure function that decides whether a proposed geometry change is risk-*reducing* versus the current stored level.

**Files:**
- Create: `scripts/risk_review.py`

**Interfaces:**
- Produces:
  - `validate_geometry(current: dict, proposed: dict) -> tuple[dict, list[str]]` — `current` and `proposed` are `{"stop": float|None, "targets": list[float]|None}`. Returns `(accepted, rejections)`: `accepted` contains only the fields that pass the one-way rule (a stop kept only if `proposed_stop >= current_stop`; targets kept only if **every** proposed target `<=` the correspondingly-indexed current target and the list length matches); `rejections` is a list of human-readable strings for each dropped field.
  - `validate_action(action: dict) -> tuple[bool, str|None]` — an action is `{"symbol","kind","fraction"?}` with `kind` in `{"hold","tighten_stop","lower_tp","trim","exit","watch"}`. Returns `(ok, reason_if_rejected)`. Rejects any `kind` not in that set (e.g. an attempted `buy`/`add`/`enter`), and rejects `trim` whose `fraction` is not in `(0,1)`.

- [ ] **Step 1: Write the failing test**

Create `scripts/risk_review.py` with only the module docstring, imports, and a `_selftest()` that asserts the intended behavior, plus an `argparse` `--selftest` entrypoint:

```python
"""RISK REVIEW — deterministic core for the intraday risk-management overlay.

Builds per-position facts for the two scheduled agentic reviews, validates every
proposed change against a hard ONE-WAY (risk-reducing only) invariant, and writes
stricter-only geometry overrides + deferred intents the monitor enforces. No LLM,
no order placement here — that is prompts/risk_review.md's job. See
docs/superpowers/specs/2026-07-14-intraday-risk-management-design.md.

    .venv/bin/python scripts/risk_review.py --selftest
"""
import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

OVERRIDES = REPO / "research_store" / "monitor" / "overrides.json"
INTENTS = REPO / "research_store" / "monitor" / "deferred_intents.json"
FACTS = REPO / "research_store" / "rh" / "risk_review_facts.json"
DECISIONS = REPO / "research_store" / "rh" / "risk_review_decisions.json"

_ACTION_KINDS = {"hold", "tighten_stop", "lower_tp", "trim", "exit", "watch"}


def _selftest() -> None:
    # --- one-way geometry: stop may only move UP, targets only pull IN ---
    acc, rej = validate_geometry({"stop": 100.0, "targets": [120.0, 140.0]},
                                 {"stop": 105.0, "targets": [118.0, 135.0]})
    assert acc == {"stop": 105.0, "targets": [118.0, 135.0]}, acc
    assert rej == [], rej

    acc, rej = validate_geometry({"stop": 100.0, "targets": [120.0, 140.0]},
                                 {"stop": 95.0, "targets": [125.0, 140.0]})
    assert "stop" not in acc, acc            # loosening the stop is dropped
    assert "targets" not in acc, acc         # raising a target is dropped
    assert len(rej) == 2, rej

    acc, rej = validate_geometry({"stop": 100.0, "targets": [120.0, 140.0]},
                                 {"stop": 100.0})    # equal stop = no-op, allowed but harmless
    assert acc.get("stop") == 100.0

    # a stop may rise but must stay STRICTLY BELOW entry (the [risk] mandate's
    # stop-below-entry rule — see Self-Review; folded in here, not deferred)
    acc, rej = validate_geometry({"stop": 100.0, "targets": [120.0]},
                                 {"stop": 118.0}, entry_low=115.0)
    assert "stop" not in acc and len(rej) == 1, (acc, rej)

    # --- actions: only the de-risk kinds; never an entry; trim fraction sane ---
    assert validate_action({"symbol": "X", "kind": "exit"}) == (True, None)
    ok, why = validate_action({"symbol": "X", "kind": "buy"})
    assert ok is False and "buy" in why
    ok, why = validate_action({"symbol": "X", "kind": "trim", "fraction": 1.5})
    assert ok is False
    assert validate_action({"symbol": "X", "kind": "trim", "fraction": 0.5}) == (True, None)
    print("selftest OK: one-way geometry + action validation")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        return


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python scripts/risk_review.py --selftest`
Expected: FAIL — `NameError: name 'validate_geometry' is not defined`.

- [ ] **Step 3: Write minimal implementation**

Insert these two functions above `_selftest()`:

```python
def validate_geometry(current: dict, proposed: dict, *, entry_low=None) -> tuple[dict, list[str]]:
    """Keep only strictly risk-reducing edits. Stop may move UP (>=) but must stay
    STRICTLY BELOW entry (the [risk] mandate's stop-below-entry rule — passing
    entry_low re-guards it without a full re-validate). Targets may only move IN
    (each <= the same-index current target). Anything else is dropped with a
    reason. Missing fields in `proposed` are simply not changed."""
    accepted, rejections = {}, []
    cur_stop = current.get("stop")
    if "stop" in proposed and proposed["stop"] is not None:
        ps = float(proposed["stop"])
        if cur_stop is not None and ps < cur_stop:
            rejections.append(f"stop {ps} < current {cur_stop} — loosening rejected")
        elif entry_low is not None and ps >= entry_low:
            rejections.append(f"stop {ps} >= entry_low {entry_low} — must stay below entry")
        else:
            accepted["stop"] = ps
    cur_t = current.get("targets") or []
    if "targets" in proposed and proposed["targets"] is not None:
        pt = proposed["targets"]
        if len(pt) == len(cur_t) and all(p <= c for p, c in zip(pt, cur_t)):
            accepted["targets"] = [float(x) for x in pt]
        else:
            rejections.append(f"targets {pt} not all <= current {cur_t} — extending rejected")
    return accepted, rejections


def validate_action(action: dict) -> tuple[bool, str | None]:
    """An action may only ever de-risk. Reject any non-de-risk kind (e.g. an
    attempted entry) and any nonsensical trim fraction."""
    kind = action.get("kind")
    if kind not in _ACTION_KINDS:
        return False, f"action kind {kind!r} is not a de-risk action"
    if kind == "trim":
        f = action.get("fraction")
        if not (isinstance(f, (int, float)) and 0.0 < f < 1.0):
            return False, f"trim fraction {f!r} must be in (0,1)"
    return True, None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python scripts/risk_review.py --selftest`
Expected: `selftest OK: one-way geometry + action validation`

- [ ] **Step 5: Commit**

```bash
git add scripts/risk_review.py
git commit -m "feat(risk-review): one-way de-risk invariant validator"
```

---

### Task 3: Override & deferred-intent read/write helpers

**Files:**
- Modify: `scripts/risk_review.py`

**Interfaces:**
- Consumes: `validate_geometry` (Task 2).
- Produces:
  - `read_overrides(path=OVERRIDES) -> dict` — `{sym: {"stop"?, "targets"?, "ts", "reason", "expires"}}`; `{}` if absent/unreadable; prunes entries whose `expires` < today.
  - `write_override(sym, accepted, reason, expires, *, path=OVERRIDES) -> None` — merges one name's accepted (already-validated) geometry into the file with a fresh UTC `ts`.
  - `read_intents(path=INTENTS) -> list` and `append_intent(intent: dict, *, path=INTENTS) -> None` — a JSON array of watch-notes / queued conditional intents (`{"symbol","note","condition"?,"expires","ts"}`).

- [ ] **Step 1: Add assertions to `_selftest()`**

Append inside `_selftest()`, before the final `print`, using a temp dir so the real store is untouched:

```python
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "overrides.json"
        write_override("NVDA", {"stop": 105.0}, "trail up", "2026-07-18", path=p)
        got = read_overrides(path=p)
        assert got["NVDA"]["stop"] == 105.0 and got["NVDA"]["reason"] == "trail up", got
        # an already-expired override is pruned on read
        write_override("OLD", {"stop": 1.0}, "stale", "2000-01-01", path=p)
        assert "OLD" not in read_overrides(path=p)
        ip = Path(d) / "intents.json"
        append_intent({"symbol": "AMD", "note": "watch 21d reclaim", "expires": "2026-07-18"}, path=ip)
        assert read_intents(path=ip)[0]["symbol"] == "AMD"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python scripts/risk_review.py --selftest`
Expected: FAIL — `NameError: name 'write_override' is not defined`.

- [ ] **Step 3: Implement the helpers**

Insert above `_selftest()`:

```python
def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _atomic_write(path: Path, text: str) -> None:
    """Write via temp-file + os.replace so the always-on 15s monitor never reads a
    half-written overrides.json. A torn read would make the monitor drop ALL
    overrides for that tick (not just the one being written) — exactly the wrong
    moment during a fast breakdown. os.replace is atomic on POSIX."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def read_overrides(path: Path = OVERRIDES) -> dict:
    """Active geometry overrides, pruning any past their `expires` date."""
    ov = _read_json(path, {})
    today = date.today().isoformat()
    live = {s: o for s, o in ov.items() if str(o.get("expires", "9999")) >= today}
    if live != ov:
        _atomic_write(path, json.dumps(live, indent=2))
    return live


def write_override(sym: str, accepted: dict, reason: str, expires: str,
                   *, path: Path = OVERRIDES) -> None:
    """Merge one name's ALREADY-VALIDATED geometry (from validate_geometry) into
    the overrides file. Caller must pass only accepted (stricter) fields."""
    ov = _read_json(path, {})
    ov[sym] = {**accepted, "reason": reason, "expires": expires,
               "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _atomic_write(path, json.dumps(ov, indent=2))


def read_intents(path: Path = INTENTS) -> list:
    ints = _read_json(path, [])
    today = date.today().isoformat()
    return [i for i in ints if str(i.get("expires", "9999")) >= today]


def append_intent(intent: dict, *, path: Path = INTENTS) -> None:
    ints = _read_json(path, [])
    ints.append({**intent, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    _atomic_write(path, json.dumps(ints, indent=2))
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python scripts/risk_review.py --selftest`
Expected: `selftest OK: one-way geometry + action validation`

- [ ] **Step 5: Commit**

```bash
git add scripts/risk_review.py
git commit -m "feat(risk-review): override + deferred-intent store helpers"
```

---

### Task 4: Monitor overlays stricter-only overrides

Make the always-on monitor enforce risk-review overrides in real time, honoring only stricter levels (defense-in-depth even though `risk_review.py` already validated).

**Files:**
- Modify: `scripts/market_monitor.py` (add an `apply_overrides` helper + call it in `check_once` right after `held` is built)

**Interfaces:**
- Consumes: the `Thesis` objects in `held` (fields `.stop`, `.targets`), and `research_store/monitor/overrides.json`.
- Produces: `apply_overrides(held: dict, overrides: dict) -> dict` — returns a NEW `{sym: thesis}` map where a name's `.stop` is raised to `max(stop, override.stop)` and `.targets` are lowered element-wise to `min(t, override.t)` — but ONLY when the override is stricter; looser/malformed overrides are ignored. Mutates copies, not the stored product.

- [ ] **Step 1: Add a failing overlay selftest**

Add near the top of `scripts/market_monitor.py` (after imports) a small pure helper stub and extend the file's `main()` to accept `--selftest`. First add the test by inserting this function and an entrypoint branch:

```python
def _selftest() -> None:
    from research_store.models import Thesis
    held = {"NVDA": Thesis(symbol="NVDA", rank=1, verdict="buy", stop=100.0,
                           targets=[120.0, 140.0], target_weight=0.07)}
    # stricter override: stop up, first target pulled in
    out = apply_overrides(held, {"NVDA": {"stop": 108.0, "targets": [118.0, 140.0]}})
    assert out["NVDA"].stop == 108.0 and out["NVDA"].targets[0] == 118.0, out["NVDA"]
    # looser override is IGNORED (stop can't move down, target can't move up)
    out = apply_overrides(held, {"NVDA": {"stop": 90.0, "targets": [130.0, 150.0]}})
    assert out["NVDA"].stop == 100.0 and out["NVDA"].targets == [120.0, 140.0], out["NVDA"]
    print("monitor selftest OK: stricter-only override overlay")
```

And in `main()` add, before building the client:

```python
    if args.selftest:
        _selftest()
        return
```

plus `ap.add_argument("--selftest", action="store_true")` alongside the existing args.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python scripts/market_monitor.py --selftest`
Expected: FAIL — `NameError: name 'apply_overrides' is not defined`.

- [ ] **Step 3: Implement `apply_overrides` and wire it in**

Add the function (after the `_last_price` helper):

```python
import copy


def apply_overrides(held: dict, overrides: dict) -> dict:
    """Overlay stricter-only risk-review geometry onto held theses (copies).
    Stop may only be raised; each target may only be lowered. Looser or malformed
    overrides are ignored — a bad file can never loosen a live stop."""
    out = {}
    for sym, th in held.items():
        ov = overrides.get(sym)
        if not ov:
            out[sym] = th
            continue
        t = copy.copy(th)
        if isinstance(ov.get("stop"), (int, float)) and t.stop is not None and ov["stop"] > t.stop:
            t.stop = float(ov["stop"])
        ot = ov.get("targets")
        if isinstance(ot, list) and t.targets and len(ot) == len(t.targets):
            t.targets = [min(cur, float(o)) for cur, o in zip(t.targets, ot)]
        out[sym] = t
    return out
```

Then in `check_once`, immediately after the line `held = {t.symbol: t for t in prod.theses if t.target_weight > 0 and t.stop}`, insert:

```python
    try:
        _ov = json.loads((MON / "overrides.json").read_text())   # json already imported at module top
    except Exception:
        _ov = {}   # absent OR a torn read → ignore ALL overrides this tick (self-heals next tick;
                   # risk_review.py writes atomically via os.replace, so torn reads are rare)
    if _ov:
        held = apply_overrides(held, _ov)
```

(This inline read avoids a scripts-to-scripts import; expiry pruning is handled by `risk_review.py`'s writer. `apply_overrides` enforces stricter-only regardless, so a stale-but-well-formed override can still only tighten.)

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python scripts/market_monitor.py --selftest`
Expected: `monitor selftest OK: stricter-only override overlay`

- [ ] **Step 5: Sanity-check the monitor still starts a single pass**

Run: `.venv/bin/python scripts/market_monitor.py --once --force`
Expected: it runs one pass without traceback (prints quote/breach lines or nothing if flat). No override file yet ⇒ `_ov = {}` path.

- [ ] **Step 6: Commit**

```bash
git add scripts/market_monitor.py
git commit -m "feat(risk-review): monitor overlays stricter-only geometry overrides"
```

---

### Task 5: Per-position fact-builder

Assemble the readable per-name checklist readout the agent reasons over. Pure(ish): takes already-loaded inputs so it is unit-testable with synthetic data.

**Files:**
- Modify: `scripts/risk_review.py`

**Interfaces:**
- Consumes: a valued snapshot from `marks.load()` (`{"positions":{sym:{qty,avg_cost,mark,value,pnl}}, ...}`), the product from `research_store.read_current()` (its `.theses` list of `Thesis`), and the `[risk_review]` config.
- Produces: `build_facts(valued: dict, theses: list, cfg: dict, *, highs: dict, spy_ret: dict, entry_sigma: dict, vix: float|None, regime_on: bool, now_iso: str) -> dict` returning `{"generated": now_iso, "backdrop": {"vix", "vix_ceiling", "regime_on"}, "positions": [ {symbol, mark, stop, targets, pnl_pct, dist_to_stop_pct, giveback_from_high_pct, rank, review_by, flags:[...] }, ... ]}`. `flags` lists the tripped attention thresholds (e.g. `"near_stop"`, `"giveback"`, `"earnings_soon"`, `"vol_expansion"`) using the config thresholds. `highs`/`spy_ret`/`entry_sigma` are per-symbol dicts the caller supplies (Task 6 fills them from adapters); when a value is missing the corresponding flag is simply omitted.

- [ ] **Step 1: Add a failing fact-builder selftest**

Append to `_selftest()` before the final print:

```python
    from research_store.models import Thesis
    valued = {"positions": {"BE": {"qty": 1.0, "avg_cost": 200.0, "mark": 210.0,
                                   "value": 210.0, "pnl": 0.05}}}
    theses = [Thesis(symbol="BE", rank=3, verdict="buy", stop=190.0,
                     targets=[230.0, 260.0], target_weight=0.07,
                     review_by="2026-07-16 (next earnings)")]
    cfg = {"ma_break_days": 21, "giveback_flag_pct": 0.10,
           "vol_expansion_mult": 1.75, "earnings_window_days": 5}
    facts = build_facts(valued, theses, cfg, highs={"BE": 250.0},
                        spy_ret={"BE": -0.04}, cur_sigma={"BE": 0.06},
                        entry_sigma={"BE": 0.03}, vix=22.0,
                        regime_on=True, now_iso="2026-07-14T16:00:00+00:00")
    pos = facts["positions"][0]
    assert pos["symbol"] == "BE"
    assert round(pos["giveback_from_high_pct"], 2) == 0.16     # (250-210)/250
    assert "giveback" in pos["flags"]                          # 0.16 > 0.10
    assert "vol_expansion" in pos["flags"]                     # 0.06 > 1.75*0.03
    assert pos["rel_return_vs_spy"] == -0.04                   # lagging SPY
    assert facts["backdrop"]["vix"] == 22.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python scripts/risk_review.py --selftest`
Expected: FAIL — `NameError: name 'build_facts' is not defined`.

- [ ] **Step 3: Implement `build_facts`**

Insert above `_selftest()`:

```python
def build_facts(valued, theses, cfg, *, highs, spy_ret, cur_sigma, entry_sigma,
                vix, regime_on, now_iso) -> dict:
    """Per-name risk readout. Flags are attention hints for the agent, not
    triggers — the agent judges every held name each pass. `spy_ret` = recent
    return minus SPY's (spec §5 #1); `cur_sigma` = current trailing daily sigma
    (spec §5 #5), compared against `entry_sigma` for vol expansion."""
    by_sym = {t.symbol: t for t in theses}
    out = []
    for sym, p in (valued.get("positions") or {}).items():
        th = by_sym.get(sym)
        if th is None or th.stop is None:
            continue
        mark = p.get("mark") or 0.0
        flags = []
        dist_to_stop = (mark - th.stop) / mark if mark else None
        if dist_to_stop is not None and dist_to_stop <= 0.03:
            flags.append("near_stop")
        high = highs.get(sym)
        giveback = (high - mark) / high if (high and mark) else None
        if giveback is not None and giveback >= cfg["giveback_flag_pct"]:
            flags.append("giveback")
        es = entry_sigma.get(sym)
        cs = cur_sigma.get(sym)
        if es and cs and cs > cfg["vol_expansion_mult"] * es:
            flags.append("vol_expansion")
        rb = (th.review_by or "")[:10]
        if rb and rb <= _days_ahead_iso(now_iso, cfg["earnings_window_days"]):
            flags.append("earnings_soon")
        out.append({
            "symbol": sym, "mark": round(float(mark), 4), "stop": th.stop,
            "targets": th.targets, "pnl_pct": p.get("pnl"),
            "dist_to_stop_pct": round(dist_to_stop, 4) if dist_to_stop is not None else None,
            "giveback_from_high_pct": round(giveback, 4) if giveback is not None else None,
            "rel_return_vs_spy": spy_ret.get(sym), "rank": th.rank,
            "review_by": th.review_by, "flags": flags,
        })
    return {"generated": now_iso,
            "backdrop": {"vix": vix, "vix_ceiling": 28.0, "regime_on": regime_on},
            "positions": out}


def _days_ahead_iso(now_iso: str, days: int) -> str:
    from datetime import timedelta
    base = datetime.fromisoformat(now_iso).date()
    return (base + timedelta(days=days)).isoformat()
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python scripts/risk_review.py --selftest`
Expected: `selftest OK: one-way geometry + action validation`

- [ ] **Step 5: Commit**

```bash
git add scripts/risk_review.py
git commit -m "feat(risk-review): per-position fact-builder with attention flags"
```

---

### Task 6: `main()` — build-facts mode and apply-decisions mode

Wire the core into two invocations the prompt uses: `--facts` (gather + write `risk_review_facts.json`) and `--apply` (read the agent's `risk_review_decisions.json`, validate, write overrides/intents, journal). Data-fetch failures degrade gracefully (a missing input just drops its flag).

**Files:**
- Modify: `scripts/risk_review.py`

**Interfaces:**
- Consumes: everything above; `strategy.load`, `governance.live_approved`, `marks.load`, `research_store.read_current`, `research_store.store.append_journal`, `notify.push`.
- Produces: CLI `--facts` and `--apply` (and existing `--selftest`). `--apply` reads `DECISIONS` = a JSON array of `{"symbol","kind","reason","stop"?,"targets"?,"fraction"?,"note"?,"expires"?}`, validates each via `validate_action` + `validate_geometry`, and — only when armed — writes overrides/intents; always journals a `risk_review` event and pushes a phone summary.

- [ ] **Step 1: Add a failing `--apply` selftest (armed path, temp files)**

Append to `_selftest()` before the final print:

```python
    with tempfile.TemporaryDirectory() as d:
        ovp, dp = Path(d) / "ov.json", Path(d) / "dec.json"
        dp.write_text(json.dumps([
            {"symbol": "NVDA", "kind": "tighten_stop", "reason": "trail",
             "stop": 108.0, "expires": "2026-07-18"},
            {"symbol": "AMD", "kind": "buy", "reason": "sneaky entry"},   # must be rejected
        ]))
        applied, orders, rejected = apply_decisions(
            json.loads(dp.read_text()),
            current_geom={"NVDA": {"stop": 100.0, "targets": [120.0], "entry_low": 130.0}},
            armed=True, overrides_path=ovp, intents_path=Path(d) / "i.json")
        assert "NVDA" in [a["symbol"] for a in applied], applied
        assert any(r["symbol"] == "AMD" for r in rejected), rejected      # buy rejected
        assert read_overrides(path=ovp)["NVDA"]["stop"] == 108.0
        # a trim/exit is an INTENT (orders), never counted as an applied change
        applied2, orders2, _ = apply_decisions(
            [{"symbol": "TSLA", "kind": "exit", "reason": "thesis broke"}],
            current_geom={}, armed=True,
            overrides_path=Path(d) / "o2.json", intents_path=Path(d) / "i2.json")
        assert orders2 and orders2[0]["symbol"] == "TSLA" and not applied2, (orders2, applied2)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python scripts/risk_review.py --selftest`
Expected: FAIL — `NameError: name 'apply_decisions' is not defined`.

- [ ] **Step 3: Implement `apply_decisions` + CLI wiring**

Insert `apply_decisions` above `_selftest()`:

```python
def apply_decisions(decisions, current_geom, *, armed,
                    overrides_path=OVERRIDES, intents_path=INTENTS):
    """Validate each agent decision (one-way invariant) and, only when armed,
    persist geometry overrides / watch-notes. Returns (applied, orders, rejected):
      - applied = geometry tightenings + watch-notes THIS FUNCTION persisted;
      - orders  = trim/exit the prompt must still PLACE via the RH MCP — recorded
                  here as INTENTS only, never as confirmed fills (record_fills.py
                  logs the actual fill; the letter narrates from that, not from here);
      - rejected = decisions that failed the one-way / action-kind guard.
    Keeping orders separate stops the journal (and the weekly letter) from ever
    claiming a sale that has not actually filled."""
    applied, orders, rejected = [], [], []
    for dcn in decisions:
        sym = dcn.get("symbol")
        ok, why = validate_action(dcn)
        if not ok:
            rejected.append({"symbol": sym, "reason": why})
            continue
        kind = dcn["kind"]
        if kind in ("tighten_stop", "lower_tp"):
            cg = current_geom.get(sym, {})
            acc, rej = validate_geometry(cg, {k: dcn.get(k) for k in ("stop", "targets")},
                                         entry_low=cg.get("entry_low"))
            if not acc:
                rejected.append({"symbol": sym, "reason": "; ".join(rej) or "no stricter change"})
                continue
            if armed:
                write_override(sym, acc, dcn.get("reason", ""),
                               dcn.get("expires", date.today().isoformat()),
                               path=overrides_path)
            applied.append({"symbol": sym, "kind": kind, "geometry": acc})
        elif kind == "watch":
            if armed:
                append_intent({"symbol": sym, "note": dcn.get("note", dcn.get("reason", "")),
                               "expires": dcn.get("expires", date.today().isoformat())},
                              path=intents_path)
            applied.append({"symbol": sym, "kind": "watch"})
        elif kind in ("trim", "exit"):  # an ORDER — the prompt places it via MCP
            orders.append({"symbol": sym, "kind": kind, "fraction": dcn.get("fraction"),
                           "reason": dcn.get("reason", "")})
        # kind == "hold" is a no-op: nothing to persist, nothing to place.
    return applied, orders, rejected
```

Extend `main()` (add args, keep `--selftest`):

```python
    ap.add_argument("--facts", action="store_true", help="gather + write risk_review_facts.json")
    ap.add_argument("--apply", action="store_true", help="apply risk_review_decisions.json")
    # ... after the --selftest branch:
    import strategy as strat            # noqa: E402
    import governance as gov            # noqa: E402
    import marks                        # noqa: E402
    from research_store import read_current, store   # noqa: E402
    cfg = strat.load()
    rc = cfg["risk_review"]
    armed = gov.live_approved(cfg) and not rc.get("alert_only", True)

    if args.facts:
        valued = marks.load()
        prod = read_current()
        if valued is None or prod is None:
            sys.exit("no snapshot/product yet")
        # NOTE: highs/spy_ret/entry_sigma/vix are filled from adapters here;
        # each is best-effort — a failed fetch leaves its flag off (see build_facts).
        facts = build_facts(valued, prod.theses, rc, highs=_gather_highs(prod),
                            spy_ret=_gather_rel_strength(prod), cur_sigma=_gather_cur_sigma(prod),
                            entry_sigma=_gather_entry_sigma(prod),
                            vix=_gather_vix(), regime_on=(prod.regime.get("status") == "on"),
                            now_iso=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        FACTS.parent.mkdir(parents=True, exist_ok=True)
        FACTS.write_text(json.dumps(facts, indent=2))
        print(f"facts -> {FACTS} ({len(facts['positions'])} positions, armed={armed})")
        return

    if args.apply:
        decisions = _read_json(DECISIONS, [])
        prod = read_current()
        current_geom = {t.symbol: {"stop": t.stop, "targets": t.targets,
                                   "entry_low": (t.entry_zone or [None])[0]}
                        for t in (prod.theses if prod else [])}
        applied, orders, rejected = apply_decisions(decisions, current_geom, armed=armed)
        store.append_journal({"event": "risk_review",
                              "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "armed": armed, "applied": applied,
                              "orders_intended": orders, "rejected": rejected})
        # Phone push ONLY when something de-risking actually happened — a routine
        # "0 actions" twice a day is exactly the as-designed noise Aaron doesn't
        # want (see docs/OPSLOG.md / the no-skip-noise rule).
        acted = applied + orders
        if acted:
            from notify import push
            push(f"Risk review: {len(acted)} de-risk action(s)" + ("" if armed else " (alert-only)"),
                 "\n".join(f"{a['symbol']} {a['kind']}" for a in acted),
                 tags="shield")
        print(f"applied {len(applied)}, orders {len(orders)}, rejected {len(rejected)} (armed={armed})")
        return
```

Add best-effort gather stubs (kept small; each returns `{}`/`None` on any failure so facts degrade gracefully):

```python
def _held(prod):
    """The held names the review tends (weighted, with a stop)."""
    return [t for t in prod.theses if t.target_weight > 0 and t.stop]


def _gather_highs(prod) -> dict:
    """Highest intraday HIGH since ENTRY per held name, from Schwab price history.
    NOTE: get_price_history returns a LIST of candle dicts (NOT {"candles":[...]}),
    each {open,high,low,close,volume,datetime(epoch ms)}. Bounding to bars at/after
    the thesis entry (`as_of`) keeps give-back honest — a pre-entry spike we never
    held through must not count as our high-water mark."""
    try:
        from adapters.schwab import research
        out = {}
        for t in _held(prod):
            candles = research.get_price_history(t.symbol, period_type="year", period=1)
            entry_ms = None
            if t.as_of:
                try:
                    entry_ms = datetime.fromisoformat(t.as_of[:10]).replace(
                        tzinfo=timezone.utc).timestamp() * 1000
                except Exception:
                    entry_ms = None
            highs = [c["high"] for c in candles if c.get("high")
                     and (entry_ms is None or (c.get("datetime") or 0) >= entry_ms)]
            if highs:
                out[t.symbol] = max(highs)
        return out
    except Exception:
        return {}


def _gather_rel_strength(prod) -> dict:
    """Recent (~1mo) return MINUS SPY's, per held name, from cached closes
    (research_store/prices/closes.parquet). Spec §5 #1 — the most on-strategy
    signal; positive = still leading the market. Best-effort: any failure omits
    the value and the agent simply sees no RS number for that name."""
    try:
        import pandas as pd
        closes = pd.read_parquet(REPO / "research_store" / "prices" / "closes.parquet")
        WIN = 21  # ~1 trading month
        if "SPY" not in closes.columns:
            return {}
        spy = closes["SPY"].dropna()
        if len(spy) <= WIN:
            return {}
        spy_ret = float(spy.iloc[-1] / spy.iloc[-1 - WIN] - 1.0)
        out = {}
        for t in _held(prod):
            if t.symbol not in closes.columns:
                continue
            s = closes[t.symbol].dropna()
            if len(s) > WIN:
                out[t.symbol] = round(float(s.iloc[-1] / s.iloc[-1 - WIN] - 1.0) - spy_ret, 4)
        return out
    except Exception:
        return {}


def _gather_cur_sigma(prod) -> dict:
    """Current trailing daily sigma (~3mo) per held name from cached closes —
    compared in build_facts against the entry sigma to flag vol expansion
    (spec §5 #5). Best-effort."""
    try:
        import pandas as pd
        closes = pd.read_parquet(REPO / "research_store" / "prices" / "closes.parquet")
        out = {}
        for t in _held(prod):
            if t.symbol not in closes.columns:
                continue
            s = closes[t.symbol].dropna()
            if len(s) > 64:
                out[t.symbol] = round(float(s.pct_change().iloc[-64:].std()), 5)
        return out
    except Exception:
        return {}


def _gather_entry_sigma(prod) -> dict:
    return {t.symbol: (t.signals or {}).get("sigma") for t in prod.theses
            if (t.signals or {}).get("sigma")}


def _gather_vix() -> float | None:
    try:
        from adapters.schwab import research
        q = research.get_quote("$VIX")
        return _q_last(q)
    except Exception:
        return None


def _q_last(q: dict):
    blk = (q or {}).get("quote", q) or {}
    for k in ("lastPrice", "mark", "closePrice"):
        if isinstance(blk.get(k), (int, float)):
            return float(blk[k])
    return None
```

> Reviewer note (verified 2026-07-14): `get_price_history(symbol, *, period_type, period, ...)` at `src/adapters/schwab/research.py:48` returns a **LIST of candle dicts**, not `{"candles":[...]}` — the gatherers above are written against that. The rel-strength / cur-sigma gatherers read `research_store/prices/closes.parquet` (needs pandas + the weekly `fetch_prices.py` cache present); if it is stale/absent they degrade to empty, dropping only those flags. The `--selftest` path does not call any gatherer.

- [ ] **Step 4: Run to verify the selftest passes**

Run: `.venv/bin/python scripts/risk_review.py --selftest`
Expected: `selftest OK: one-way geometry + action validation`

- [ ] **Step 5: Smoke-test facts mode against the live store (read-only)**

Run: `.venv/bin/python scripts/risk_review.py --facts`
Expected: `facts -> .../risk_review_facts.json (N positions, armed=False)` (armed False because `alert_only=true`). If no snapshot/product yet, it exits with that message — acceptable.

- [ ] **Step 6: Commit**

```bash
git add scripts/risk_review.py
git commit -m "feat(risk-review): facts + apply modes with graceful data degradation"
```

---

### Task 7: The agentic procedure `prompts/risk_review.md`

**Files:**
- Create: `prompts/risk_review.md`

**Interfaces:**
- Consumes: `scripts/risk_review.py --facts` output (`risk_review_facts.json`) and writes `risk_review_decisions.json` for `--apply`; places trim/exit orders via the RH MCP exactly as `prompts/fast_loop.md`/`exit.md` do (Agentic account `948184924` only).

- [ ] **Step 1: Write the procedure**

Create `prompts/risk_review.md` with this content:

```markdown
You are the RISK-MANAGEMENT REVIEWER for the agentic-trader system, running
headless twice a trading day (~12:00 and ~15:45 ET). You are DEFENSIVE ONLY.
Your entire job is to protect open positions — you may tighten stops, lower
take-profits, trim, or exit. You may NEVER loosen a stop, extend a target, add
to a position, or open a new one. Those are impossible by construction (the code
rejects them); do not attempt them.

HARD RULES (CLAUDE.md overrides everything):
- Trade ONLY the Robinhood account with agentic_allowed=true ("Agentic"). Every
  other account is off-limits.
- Equities only. Fractional, dollar-notional orders.

PROCEDURE — follow exactly:

1. Kill-switch: if research_store/HALT exists → STOP, do nothing.
2. Build facts: run `.venv/bin/python scripts/risk_review.py --facts`. Read
   research_store/rh/risk_review_facts.json. Also read
   research_store/monitor/deferred_intents.json — resolve any watch-note you left
   last pass ("did NVDA reclaim its 21-day?").
3. For EACH position, judge from the facts (rank/RS, price vs 21/50-day, giveback
   from high, distance to stop, vol-expansion, earnings proximity, news via the
   Alpaca get_news MCP tool if a name looks impaired). Assign a verdict —
   healthy / watch / de-risk — and DEFAULT TO HOLD. Only act on a concrete flag.
4. Write research_store/rh/risk_review_decisions.json — a JSON array. One object
   per name you are acting on (omit pure "healthy" holds):
     {"symbol","kind","reason", plus fields by kind}
   kind ∈ hold | watch | tighten_stop | lower_tp | trim | exit
   - tighten_stop: add "stop" (must be >= current), "expires" (YYYY-MM-DD, default
     this Friday). lower_tp: add "targets" (each <= current), "expires".
   - trim: add "fraction" in (0,1). exit: no extra fields.
   - watch: add "note" and "expires".
5. Apply non-order changes: run `.venv/bin/python scripts/risk_review.py --apply`.
   It validates the one-way invariant, writes stricter-only overrides (the monitor
   enforces them live), records watch-notes, journals, and pushes your phone. If a
   decision is rejected there, it was not risk-reducing — do not fight it.
6. Place trim/exit ORDERS (only if the apply step ran armed — i.e. live_approved
   AND not alert_only): for each trim/exit, get_equity_positions to size it, then
   review_equity_order → place_equity_order (a SELL: full quantity for exit, or
   fraction × quantity for trim) in account 948184924. Then journal the fills with
   scripts/record_fills.py (status/side/amount/reason="risk_review") exactly as the
   fast loop does. If alert-only, place NOTHING — the apply step already pushed the
   would-be actions to the phone.
7. Report concisely: per-name verdict, what you changed, what you placed.
```

- [ ] **Step 2: Verify the facts command it invokes exists**

Run: `.venv/bin/python scripts/risk_review.py --facts && echo OK`
Expected: prints the facts line then `OK`.

- [ ] **Step 3: Commit**

```bash
git add prompts/risk_review.md
git commit -m "feat(risk-review): agentic reviewer procedure (defensive-only)"
```

---

### Task 8: Cron wrapper and schedule

**Files:**
- Create: `deploy/run_risk_review.sh`
- Modify: `deploy/crontab.template`

**Interfaces:**
- Consumes: `prompts/risk_review.md`, the same env/guards as `deploy/run_fast_loop.sh`.

- [ ] **Step 1: Read the existing wrapper to copy its guards**

Run: `cat deploy/run_fast_loop.sh`
Expected: note its `ANTHROPIC_API_KEY` guard, `cd`, and `claude -p --model claude-opus-4-8` invocation — mirror them.

- [ ] **Step 2: Create the wrapper**

Create `deploy/run_risk_review.sh` (mirror `run_fast_loop.sh`; adjust the prompt path):

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# FOOTGUN GUARD: a stray ANTHROPIC_API_KEY silently bills per-token. Refuse.
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "ANTHROPIC_API_KEY is set — refusing to run (would bill per-token)." >&2
  exit 1
fi

exec claude -p --model claude-opus-4-8 "$(cat prompts/risk_review.md)"
```

- [ ] **Step 3: Make it executable + syntax-check**

Run: `chmod +x deploy/run_risk_review.sh && bash -n deploy/run_risk_review.sh && echo OK`
Expected: `OK`

- [ ] **Step 4: Add cron entries**

Append to `deploy/crontab.template` (match the file's existing TZ handling — the monitor uses ET; if the crontab documents `CRON_TZ`, reuse it, else convert to the server TZ the other jobs use):

```cron
# Intraday risk review — defensive de-risk overlay (docs/superpowers/specs/2026-07-14-*)
# 12:00 ET midday check + 15:45 ET pre-close tend, Mon–Fri.
0 12 * * 1-5  /opt/agentic-trader/deploy/run_risk_review.sh  >> /opt/agentic-trader/deploy/logs/risk_review.log 2>&1
45 15 * * 1-5 /opt/agentic-trader/deploy/run_risk_review.sh  >> /opt/agentic-trader/deploy/logs/risk_review.log 2>&1
```

> Reviewer note: confirm whether `deploy/crontab.template` sets `CRON_TZ=America/New_York`. If it does not and other jobs are in UTC, convert 12:00/15:45 ET to the correct UTC hours (accounting for EDT vs EST) and add a comment. Do not assume server local time is ET.

- [ ] **Step 5: Commit**

```bash
git add deploy/run_risk_review.sh deploy/crontab.template
git commit -m "feat(risk-review): cron wrapper + twice-daily schedule"
```

---

### Task 9: Surface risk-review actions in the weekly letter facts

Keep de-risk *actions* narratable (they are real PM decisions) while keeping routine verdicts out (the no-plumbing-in-the-letter rule).

**Files:**
- Modify: `scripts/letter_facts.py` (in the journal-events loop, around line 121)

**Interfaces:**
- Consumes: `risk_review` journal events written by Task 6.
- Produces: a `risk_actions_this_week` list in `facts.json`.

- [ ] **Step 1: Add the collector**

In `scripts/letter_facts.py`, initialise `risk_actions = []` alongside the existing
`fills, exits, notes, reentries = [], [], [], []` line. Then narrate only things
that TRULY happened, keeping the recorded-vs-placed distinction from Task 6:

1. Geometry tightenings — real PM decisions, not trades — come from the
   `risk_review` journal's `applied` list. Add a branch:

```python
        elif e.get("event") == "risk_review":
            # Only geometry tightenings that were actually persisted. trim/exit are
            # NOT taken from here — they are intents; their CONFIRMED fills arrive as
            # `execution` events (handled above), tagged reason="risk_review".
            for a in e.get("applied", []):
                if a.get("kind") in ("tighten_stop", "lower_tp"):
                    risk_actions.append({"symbol": a.get("symbol"), "kind": a.get("kind")})
```

2. Confirmed risk-driven sells come from the `execution` fills the prompt places
   with `reason="risk_review"`. Inside the existing execution branch, right after
   the non-skipped `fills.append({...})` line, add:

```python
            if f.get("reason") == "risk_review":
                risk_actions.append({"symbol": f.get("symbol"), "kind": f.get("side")})
```

Finally add `"risk_actions_this_week": risk_actions,` to the `facts` dict.

> This closes Fix 5: the letter can only ever report a trim/exit that actually
> filled (from `record_fills.py`), never one that was merely decided.

- [ ] **Step 2: Verify facts still build**

Run: `.venv/bin/python scripts/letter_facts.py`
Expected: prints the usual `facts -> ...` line with no traceback (`risk_actions_this_week` present, likely empty).

- [ ] **Step 3: Commit**

```bash
git add scripts/letter_facts.py
git commit -m "feat(risk-review): surface de-risk actions in weekly letter facts"
```

---

### Task 10: Weekly rebuild clears the week's overrides (spec §7 reconciliation)

Spec §7 promises "the weekly `slow_loop.py` rebuild … clears that week's
overrides" so an intra-week tightening never stacks on top of Monday's freshly
rebuilt geometry. That was described but not built (overrides would only ever
lapse via their `expires` date). Build the promised clear.

**Files:**
- Modify: `scripts/slow_loop.py` (right after the `write_product(...)` call at ~line 202)

**Interfaces:**
- Consumes: nothing new. After a successful product write, deletes
  `research_store/monitor/overrides.json` and `deferred_intents.json`.

- [ ] **Step 1: Add the clear after `write_product`**

Immediately after the `write_product(product, mandate=strat.risk_mandate(cfg))`
line in `main()`, append:

```python
    # Intraday risk-review overrides/intents are strictly INTRA-WEEK overlays: the
    # fresh weekly geometry supersedes them. Clear them so a Tuesday-tightened stop
    # is never re-applied on top of next week's rebuilt levels. (spec §7)
    for _f in ("overrides.json", "deferred_intents.json"):
        try:
            (REPO / "research_store" / "monitor" / _f).unlink()
        except FileNotFoundError:
            pass
```

(`REPO` is already defined in `slow_loop.py` — it is used for the cooldown path
at ~line 130.)

- [ ] **Step 2: Verify it still parses**

Run: `.venv/bin/python -c "import ast; ast.parse(open('scripts/slow_loop.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add scripts/slow_loop.py
git commit -m "feat(risk-review): weekly rebuild clears intra-week overrides (spec §7)"
```

---

## Self-Review

**Spec coverage:**
- §4 Layer 1 monitor overlay → Task 4. §4 Layer 2 two passes → Tasks 7–8. §4 Layer 3 core/prompt split → Tasks 2–7.
- §5 checklist → Task 5 (`build_facts`) + Task 6 gatherers (RS-vs-SPY + cur-sigma now computed from cached closes, not stubbed) + Task 7 (news via MCP, rank from product).
- §6 verdict + de-risk menu + default-hold → Task 7 prompt; action validity → Task 2.
- §7 actuation map: overrides → Tasks 3/4/6; immediate orders → Task 7; deferred intents/watch-notes → Tasks 3/6/7; verdict journal → Task 6; **weekly reconciliation clear → Task 10**.
- §8 guardrails: one-way invariant + stop-below-entry → Task 2; live_approved/alert_only → Task 6; governance reuse → Task 6/7.
- §9 components → all created. §10 data sources → Task 6 gatherers. §11 rollout alert-only → Task 1 default + Task 6 `armed`. §12 open items → left unbuilt intentionally.

**Gaps found & closed (this revision, 2026-07-14):**
- **§8 `[risk]` round-trip / stop-below-entry** — `validate_geometry` now takes `entry_low` and rejects a stop `>= entry_low`; the check and its selftest are folded directly into Task 2 (no longer a deferred reviewer instruction). `apply_decisions` / `main --apply` thread `entry_low` from each thesis's `entry_zone`. Because overrides may only tighten a stop / lower a target, no full `validate_product` re-run is needed.
- **§7 weekly reconciliation** — was described but unbuilt; now Task 10 clears `overrides.json` + `deferred_intents.json` on the weekly `slow_loop.py` rebuild.
- **Give-back signal (Fix 1)** — `_gather_highs` corrected: `get_price_history` returns a **list** of candles (verified), uses candle `high` bounded to bars at/after entry.
- **RS-vs-SPY (§5 #1) & vol-expansion (§5 #5) (Fix 2)** — no longer inert stubs; both are computed from `research_store/prices/closes.parquet`. `build_facts` takes an explicit `cur_sigma` input (the prior `__sigma`-key hack is gone).
- **Atomic overrides file (Fix 4)** — writes go through `_atomic_write` (temp + `os.replace`) so the 15s monitor can never read a torn file and blank all overrides.
- **Recorded-vs-placed (Fix 5)** — `apply_decisions` returns `(applied, orders, rejected)`; trim/exit are `orders` (intents), never `applied`. The journal records `orders_intended`; the weekly letter (Task 9) narrates trim/exit only from confirmed `execution` fills tagged `reason="risk_review"`, plus geometry tightenings — never an unfilled intent.
- **Noise (minor)** — the `--apply` phone push fires only when ≥1 de-risk action actually happened (no twice-daily "0 actions" text).

**Placeholder scan:** no TBD/TODO; every code step shows complete code. Two explicit "reviewer note" callouts (Schwab `get_price_history` kwargs; crontab TZ) are verification instructions, not placeholders — the code runs as written for `--selftest`; only the live adapter kwargs need confirming against the real signature.

**Type consistency:** `validate_geometry` returns `(accepted: dict, rejections: list)` and is consumed that way in Task 6. `apply_overrides` (monitor) vs `write_override`/`read_overrides` (core) are deliberately separate — the monitor never imports the core; both honor stricter-only. `build_facts` kwargs match its Task 6 call site. `apply_decisions` returns `(applied, rejected)` consumed in `main --apply`.

---

## Execution Handoff

All self-review actions are now folded directly into the task bodies (the
`entry_low` stop check lives in Task 2's code + selftest; §7 reconciliation is
Task 10). Implement top-to-bottom; each task's `--selftest` is the gate before its
commit. The two remaining live-only confirmations are labelled "reviewer note" in
Tasks 6 and 8 (cached-closes/parquet presence for the gatherers; crontab TZ) —
both degrade safely and do not block `--selftest`.
