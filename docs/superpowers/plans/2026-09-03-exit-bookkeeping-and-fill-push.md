# Exit Bookkeeping and Fill Push Implementation Plan (Plan 1 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a filled exit whose ledger and snapshot stay silent impossible, by moving the exit path's bookkeeping from the model into the monitor; record the DELL trim the 2026-09-03 executor left unrecorded; and push session fills to the phone.

**Architecture:** A new pure module `src/exit_bookkeeping.py` decides which recorder scripts to run from which staging files are present, runs each under the venv interpreter, and archives a consumed staging file so it can never be re-fed. `scripts/market_monitor.py` calls it right after the exit executor returns, whichever path sold. The executor's four Bash grants are removed and `prompts/exit.md` stops telling it to run anything. The sessions' MCP `record_fills` gains the same one-line-per-order phone push the exit path's recorder already sends, through one shared formatter in `src/notify.py`.

**Tech Stack:** Python 3.12 (`.venv`) for the recorder scripts and the MCP server; **Python 3.10 (`/usr/bin/python3`) for the monitor and everything it imports** — `src/exit_bookkeeping.py` must compile under 3.10. `subprocess`, `json`, `pathlib` only. No new dependencies.

Spec: `docs/superpowers/specs/2026-09-03-model-fallback-and-exit-bookkeeping-design.md` §4 and §7. Plans 2–4 (models config + chain, code seller, custody mode) follow separately, in the spec's §9 order.

## Global Constraints

- The monitor runs under `/usr/bin/python3` (3.10). Any module it imports: `from __future__ import annotations`, no `match`, no `X | Y` at runtime (annotations only). Verify with `/usr/bin/python3 -m py_compile <file>`.
- ⛔ **Selftest and validator suites cannot be run by an agent in this repo** — a PreToolUse hook refuses them. Write the selftests (they are the repo's convention and the operator runs them), but VERIFY by direct invocation of the real function with printed output, and by reading the whole changed document as its consumer sees it. Never put the suite flag's literal text in a shell command, even inside `grep`; the hook matches the text.
- ⛔ After ANY edit to repo Python, run `.venv/bin/python scripts/reload_stale.py` (`--dry-run` first). It refuses to bounce `agentic-monitor` while an exit is in flight; if it refuses, wait and re-run. Exit 1 = something stale was NOT restarted; do not report done.
- Every `research_store/` file is git-ignored (`.gitignore:17`); the journal is mirrored off-box by `deploy/backup_ledger.sh`. Task 1 therefore commits only its OPSLOG line.
- Commit after each task; push (`git push`) after the final task. Commit trailer:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_012BXt93L5D51yXw1LPSBc6i
  ```
- The exit executor keeps `Read`, `Edit(...)`, `Write(...)` for its staging files. Only the four `Bash(...)` grants go.
- Never anchor on the account balance; never print `.env`.

---

### Task 1: Record the 2026-09-03 DELL partial outcome and archive the stale staging files

**Files:**
- Write: `research_store/rh/partial_closes.json` (staging input; git-ignored)
- Create dir: `research_store/rh/consumed/`
- Move: `research_store/rh/fills.json`, `research_store/rh/broker_state.json`, `research_store/rh/orders_dump.json`, `research_store/rh/partial_closes.json` → `research_store/rh/consumed/`
- Modify: `docs/OPSLOG.md` (one paragraph under the 2026-09-03 "Later" subsection)

**Interfaces:**
- Consumes: `scripts/record_partial_outcome.py` — argument-free; reads `research_store/rh/partial_closes.json`; idempotent keyed on symbol + date + price.
- Produces: a `partial_outcome` journal event for DELL 2026-09-03; the `consumed/` directory Task 2 archives into.

The five values are verified facts, not estimates: entry 444.38 is the post-FIFO average cost quoted in the 2026-09-02 19:22Z `set_levels` note ("avg_cost has moved 427.01 -> 444.38"); exit 510.7964 is `avg_price` in the 2026-09-03 15:56:24Z `execution` event for order `6a9983c7…`; fraction 0.5 and reason `target1` are from `exit_request.json` of 14:26:42Z.

- [ ] **Step 1: Confirm the label is absent (so this task records, not duplicates)**

Run:
```bash
cd /opt/agentic-trader && grep -c '"event": "partial_outcome".*2026-09-03\|2026-09-03.*"event": "partial_outcome"' research_store/journal.jsonl; grep '"event": "partial_outcome"' research_store/journal.jsonl | grep DELL | tail -1 | cut -c1-200
```
Expected: `0`, and the last DELL partial_outcome line shows `exit_date` `2026-09-02` (yesterday's 488.46 trim), not today.

- [ ] **Step 2: Write today's staging file (today's row ONLY)**

```bash
cd /opt/agentic-trader && mkdir -p research_store/rh/consumed && mv research_store/rh/partial_closes.json "research_store/rh/consumed/partial_closes.2026-09-02T15-19.json" && cat > research_store/rh/partial_closes.json <<'EOF'
[
  {"symbol": "DELL", "fraction": 0.5, "entry_price": 444.38, "exit_price": 510.7964, "exit_date": "2026-09-03", "exit_reason": "target1"}
]
EOF
cat research_store/rh/partial_closes.json
```
Expected: the one-row array echoed back.

- [ ] **Step 3: Run the recorder**

Run: `cd /opt/agentic-trader && .venv/bin/python scripts/record_partial_outcome.py; echo "rc=$?"`
Expected: a line naming DELL anchored to its thesis (the script prints per-symbol results; `UNANCHORED` would mean no thesis — report it, do not retry), then `rc=0`.

- [ ] **Step 4: Verify against the journal, not the script's exit code**

Run:
```bash
cd /opt/agentic-trader && grep '"event": "partial_outcome"' research_store/journal.jsonl | tail -1 | python3 -c "import sys,json; m=json.loads(sys.stdin.read()); print({k: m.get(k) for k in ('symbol','fraction','entry_price','exit_price','exit_date','exit_reason','pnl_pct')})"
```
Expected: `{'symbol': 'DELL', 'fraction': 0.5, 'entry_price': 444.38, 'exit_price': 510.7964, 'exit_date': '2026-09-03', 'exit_reason': 'target1', 'pnl_pct': <positive, ≈0.1495>}`.

- [ ] **Step 5: Archive the stale staging files from 10:27–10:28 and yesterday**

```bash
cd /opt/agentic-trader/research_store/rh && for f in fills.json broker_state.json orders_dump.json partial_closes.json; do [ -f "$f" ] && mv "$f" "consumed/${f%.json}.2026-09-03T10-28.json"; done; ls -la consumed/; ls fills.json broker_state.json orders_dump.json partial_closes.json 2>&1
```
Expected: four (plus the 09-02 one from Step 2) files in `consumed/`; the four `ls` lines each say `No such file or directory`.

- [ ] **Step 6: OPSLOG line**

In `docs/OPSLOG.md`, directly after the paragraph that begins `**REVERTED ~12:45 ET the same day.**`, add:

```markdown
**DELL partial_outcome recorded by hand ~13:30 ET** (fraction 0.5, entry
444.38, exit 510.7964, target1) via `scripts/record_partial_outcome.py`; the
10:27–10:28 staging files (`fills.json`, `broker_state.json`) and yesterday's
`orders_dump.json`/`partial_closes.json` moved to `research_store/rh/consumed/`
so no later run can re-feed them. The ledger for 09-03 is now whole.
```

- [ ] **Step 7: Commit**

```bash
cd /opt/agentic-trader && git add docs/OPSLOG.md && git commit -q -m "opslog: DELL 2026-09-03 partial_outcome recorded by hand; stale exit staging files archived

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012BXt93L5D51yXw1LPSBc6i"
```

---

### Task 2: `src/exit_bookkeeping.py` — the pure planner and runner

**Files:**
- Create: `src/exit_bookkeeping.py`

**Interfaces:**
- Produces (Task 3 consumes):
  - `STEPS: tuple[tuple[str, str], ...]` — `(script_relpath, staging_filename)` in run order.
  - `plan(sold: set, present: set) -> dict` — `{"run": [script...], "gap": bool, "warnings": [str...]}`. Pure.
  - `run_step(script: str, staging: str, *, rh: Path = RH, runner=subprocess.run, ts: str | None = None) -> dict` — `{"script", "rc", "tail", "archived"}`.
  - `record_exits(sold: set, *, rh: Path = RH, runner=subprocess.run, ts: str | None = None) -> dict` — `{"ran": [step...], "failed": [step...], "gap": bool, "warnings": [str...]}`.

- [ ] **Step 1: Write the module with its selftest**

```python
"""Monitor-owned exit bookkeeping (spec 2026-09-03 §4).

WHY THIS IS CODE AND NOT A PROMPT STEP. Until 2026-09-03 the exit executor —
a model — ran the four recorder scripts itself, under EXACT-MATCH Bash grants.
Every run typed `record_fills.py; echo "EXIT=$?"`, was refused, and usually
retried the bare form. On 09-03 it did not retry: the sale filled, the result
file was written, and the ledger, the snapshot and the partial-outcome label
were left unwritten. The monitor's staleness guard compares the snapshot
against the journal, and an unjournalled sale makes them AGREE, so an exit
after the 15:15 session leaves a ghost position on watch through the next
morning's unattended window. That is the 2026-08-14 class again.

So the monitor runs the recorders, whichever path sold, from the staging
files the executor writes. The executor keeps Write; it loses Bash.

RUNS UNDER /usr/bin/python3 (3.10) — the monitor's interpreter. Keep it
3.10-safe: annotations only via __future__, no match, no runtime `X | Y`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RH = REPO / "research_store" / "rh"
PY = REPO / ".venv" / "bin" / "python"

#: (script, staging file) in the order prompts/exit.md always ran them:
#: fills + snapshot first (so every later step sees the post-sell book),
#: outcome labels next, the broker reconcile last.
STEPS = (
    ("scripts/record_fills.py",           "fills.json"),
    ("scripts/record_exit_outcome.py",    "exit_closes.json"),
    ("scripts/record_partial_outcome.py", "partial_closes.json"),
    ("scripts/reconcile_ledger.py",       "orders_dump.json"),
)
#: record_fills.py publishes positions.json only when this file is present.
#: Its absence is not a gap (fills still journal) but it IS worth a line.
SNAPSHOT_INPUT = "broker_state.json"
STEP_TIMEOUT_S = 120


def plan(sold: set, present: set) -> dict:
    """Which recorders to run, given what sold and which staging files exist.

    Pure. `run` is every STEPS script whose staging file is present, in
    STEPS order — a file left by an earlier run is still run, because every
    recorder is idempotent (order_id / symbol+date+price keyed) and a leftover
    is exactly the case that must not be skipped silently. `gap` is True only
    when something SOLD and NO staging file exists at all: the executor
    placed and then wrote nothing, which is the loud finding.
    """
    run = [script for script, name in STEPS if name in present]
    warnings = []
    if "fills.json" in present and SNAPSHOT_INPUT not in present:
        warnings.append(f"{SNAPSHOT_INPUT} absent — positions.json will NOT be republished")
    gap = bool(sold) and not run
    return {"run": run, "gap": gap, "warnings": warnings}


def _stamp(ts: str | None) -> str:
    from datetime import datetime, timezone  # noqa: PLC0415
    raw = ts or datetime.now(timezone.utc).isoformat(timespec="seconds")
    return raw.replace(":", "-")


def run_step(script: str, staging: str, *, rh: Path = RH, runner=subprocess.run,
             ts: str | None = None) -> dict:
    """Run one recorder; archive its staging file on success.

    rc 0 → the staging file is MOVED to rh/consumed/<name>.<ts>.json so the
    next exit can never re-feed it. Non-zero → the file stays for retry and
    the last stderr/stdout line comes back as `tail`. A runner exception
    (timeout, missing interpreter) is rc -1 with the exception as tail —
    never raised, the monitor must keep polling.
    """
    try:
        cp = runner([str(PY), script], cwd=str(REPO), capture_output=True,
                    text=True, timeout=STEP_TIMEOUT_S)
        rc = int(cp.returncode)
        text = (cp.stderr or "").strip() or (cp.stdout or "").strip()
        tail = text.splitlines()[-1][:300] if text else ""
    except Exception as e:                      # noqa: BLE001
        rc, tail = -1, f"{type(e).__name__}: {e}"[:300]
    archived = None
    src = rh / staging
    if rc == 0 and src.exists():
        dest_dir = rh / "consumed"
        dest_dir.mkdir(parents=True, exist_ok=True)
        archived = dest_dir / f"{src.stem}.{_stamp(ts)}{src.suffix}"
        src.replace(archived)
    return {"script": script, "rc": rc, "tail": tail, "archived": archived}


def record_exits(sold: set, *, rh: Path = RH, runner=subprocess.run,
                 ts: str | None = None) -> dict:
    """Run every recorder whose staging file exists. -> summary for the monitor."""
    present = {name for _, name in STEPS if (rh / name).exists()}
    if (rh / SNAPSHOT_INPUT).exists():
        present.add(SNAPSHOT_INPUT)
    p = plan(set(sold), present)
    ran, failed = [], []
    for script, name in STEPS:
        if script not in p["run"]:
            continue
        r = run_step(script, name, rh=rh, runner=runner, ts=ts)
        (ran if r["rc"] == 0 else failed).append(r)
    return {"ran": ran, "failed": failed, "gap": p["gap"], "warnings": p["warnings"]}


def _selftest() -> None:
    import tempfile  # noqa: PLC0415

    # plan(): nothing sold, nothing present
    assert plan(set(), set()) == {"run": [], "gap": False, "warnings": []}
    # a normal partial exit: fills + snapshot + partial label present, in STEPS order
    p = plan({"DELL"}, {"partial_closes.json", "fills.json", "broker_state.json"})
    assert p["run"] == ["scripts/record_fills.py", "scripts/record_partial_outcome.py"], p
    assert p["gap"] is False and p["warnings"] == [], p
    # THE finding: sold, and the executor wrote nothing
    p = plan({"DELL"}, set())
    assert p["run"] == [] and p["gap"] is True, p
    # fills without broker_state: runs, no gap, but says the snapshot won't publish
    p = plan({"DELL"}, {"fills.json"})
    assert p["run"] == ["scripts/record_fills.py"] and p["gap"] is False, p
    assert p["warnings"] == ["broker_state.json absent — positions.json will NOT be republished"], p
    # a leftover file with nothing sold is still run (idempotent), never a gap
    p = plan(set(), {"orders_dump.json"})
    assert p["run"] == ["scripts/reconcile_ledger.py"] and p["gap"] is False, p

    class _CP:
        def __init__(self, rc, err="", out=""):
            self.returncode, self.stderr, self.stdout = rc, err, out

    with tempfile.TemporaryDirectory() as td:
        rh = Path(td)
        (rh / "fills.json").write_text("[]")
        calls = []

        def ok_runner(argv, **kw):
            calls.append((argv, kw.get("cwd")))
            return _CP(0, out="recorded 1 fill")

        r = run_step("scripts/record_fills.py", "fills.json", rh=rh, runner=ok_runner,
                     ts="2026-09-03T15:00:00+00:00")
        assert r["rc"] == 0 and r["tail"] == "recorded 1 fill", r
        assert not (rh / "fills.json").exists(), "consumed file must be archived"
        assert r["archived"] == rh / "consumed" / "fills.2026-09-03T15-00-00+00-00.json", r
        assert calls[0][0] == [str(PY), "scripts/record_fills.py"] and calls[0][1] == str(REPO), calls
        # failure keeps the file and surfaces the last line
        (rh / "orders_dump.json").write_text("[]")
        r = run_step("scripts/reconcile_ledger.py", "orders_dump.json", rh=rh,
                     runner=lambda *a, **k: _CP(2, err="x\nledger divergence: 1 unjournaled"))
        assert r["rc"] == 2 and r["tail"] == "ledger divergence: 1 unjournaled", r
        assert (rh / "orders_dump.json").exists() and r["archived"] is None, r
        # a runner that raises is rc -1, never an exception
        def boom(*a, **k):
            raise subprocess.TimeoutExpired("x", STEP_TIMEOUT_S)
        r = run_step("scripts/reconcile_ledger.py", "orders_dump.json", rh=rh, runner=boom)
        assert r["rc"] == -1 and "TimeoutExpired" in r["tail"], r
        # record_exits(): orchestration + the gap finding
        (rh / "orders_dump.json").unlink()
        s = record_exits({"DELL"}, rh=rh, runner=ok_runner)
        assert s["ran"] == [] and s["failed"] == [] and s["gap"] is True, s
        (rh / "fills.json").write_text("[]")
        (rh / "partial_closes.json").write_text("[]")
        s = record_exits({"DELL"}, rh=rh, runner=ok_runner, ts="2026-09-03T15:01:00+00:00")
        assert [x["script"] for x in s["ran"]] == ["scripts/record_fills.py",
                                                    "scripts/record_partial_outcome.py"], s
        assert s["gap"] is False and s["warnings"][0].startswith("broker_state.json absent"), s
        assert not (rh / "fills.json").exists() and not (rh / "partial_closes.json").exists()
    print("selftest OK: exit_bookkeeping -- plan orders steps, flags the sold-but-"
          "nothing-staged gap, warns on a missing snapshot input; run_step archives "
          "on rc 0, keeps the file on failure, never raises; record_exits orchestrates")


if __name__ == "__main__":
    import sys  # noqa: PLC0415
    if len(sys.argv) > 1 and sys.argv[1] == "--" + "selftest":
        _selftest()
```

- [ ] **Step 2: Verify it compiles under the monitor's interpreter**

Run: `cd /opt/agentic-trader && /usr/bin/python3 -m py_compile src/exit_bookkeeping.py && echo COMPILES_310`
Expected: `COMPILES_310`

- [ ] **Step 3: Verify the planner by direct invocation (the agent cannot run the suite)**

Run:
```bash
cd /opt/agentic-trader && /usr/bin/python3 -c "
import sys; sys.path.insert(0,'src'); import exit_bookkeeping as b
print(b.plan({'DELL'}, {'partial_closes.json','fills.json','broker_state.json'}))
print(b.plan({'DELL'}, set()))
print(b.plan({'DELL'}, {'fills.json'}))"
```
Expected, exactly:
```
{'run': ['scripts/record_fills.py', 'scripts/record_partial_outcome.py'], 'gap': False, 'warnings': []}
{'run': [], 'gap': True, 'warnings': []}
{'run': ['scripts/record_fills.py'], 'gap': False, 'warnings': ['broker_state.json absent — positions.json will NOT be republished']}
```

- [ ] **Step 4: Verify run_step archives on success and keeps the file on failure, against a scratch dir**

Run:
```bash
cd /opt/agentic-trader && /usr/bin/python3 -c "
import sys, tempfile; sys.path.insert(0,'src'); import exit_bookkeeping as b
from pathlib import Path
class CP:
    def __init__(s, rc, err=''): s.returncode, s.stderr, s.stdout = rc, err, ''
with tempfile.TemporaryDirectory() as td:
    rh = Path(td); (rh/'fills.json').write_text('[]')
    r = b.run_step('scripts/record_fills.py', 'fills.json', rh=rh, runner=lambda *a, **k: CP(0), ts='T')
    print('ok:', r['rc'], (rh/'fills.json').exists(), r['archived'].name)
    (rh/'orders_dump.json').write_text('[]')
    r = b.run_step('scripts/reconcile_ledger.py', 'orders_dump.json', rh=rh, runner=lambda *a, **k: CP(2, 'ledger divergence: 1'))
    print('fail:', r['rc'], (rh/'orders_dump.json').exists(), r['tail'])"
```
Expected:
```
ok: 0 False fills.T.json
fail: 2 True ledger divergence: 1
```

- [ ] **Step 5: Commit**

```bash
cd /opt/agentic-trader && git add src/exit_bookkeeping.py && git commit -q -m "exit_bookkeeping: pure planner + runner for monitor-owned exit recording (spec 2026-09-03 §4)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012BXt93L5D51yXw1LPSBc6i"
```

---

### Task 3: Wire the monitor to run the bookkeeping after every executor return

**Files:**
- Modify: `scripts/market_monitor.py` — the import block (after line 49 `from notify import push as notify`), the executor docstring comment block (lines ~1176–1186 and ~1233–1237), and the post-executor block inside `check_once` (the lines beginning `sold = {s["symbol"] for s in result.get("sold", [])}`).

**Interfaces:**
- Consumes: `exit_bookkeeping.record_exits(sold, ts=ts) -> {"ran","failed","gap","warnings"}` (Task 2); existing `notify(title, body, tags=...)`, `store.append_journal(dict)`.

- [ ] **Step 1: Add the import**

After the line `from notify import push as notify               # noqa: E402  shared ntfy helper` add:

```python
import exit_bookkeeping                          # noqa: E402  monitor-owned exit recording (§4, 2026-09-03)
```

- [ ] **Step 2: Insert the bookkeeping call**

Find, inside `check_once`, this exact line:
```python
        sold = {s["symbol"] for s in result.get("sold", [])}
```
Immediately AFTER it (before `failed = {t["symbol"] for t in act} - sold`), insert:

```python
        # ⛔ THE MONITOR RECORDS THE EXIT, NOT THE EXECUTOR (2026-09-03). The
        # executor writes staging files; the four recorder scripts run HERE,
        # whichever path sold, so a filled sale with a silent ledger/snapshot
        # is impossible — including when the executor dies after placing.
        bk = exit_bookkeeping.record_exits(sold, ts=ts)
        for r in bk["ran"]:
            print(f"  bookkeeping ok: {r['script']} -> archived {r['archived'].name if r['archived'] else '-'}")
        for r in bk["failed"]:
            print(f"  bookkeeping FAILED: {r['script']} rc={r['rc']}: {r['tail']}")
            notify("Exit bookkeeping FAILED",
                   f"{r['script']} rc={r['rc']}\n{r['tail']}\nStaging file kept for retry.",
                   tags="warning")
        for w in bk["warnings"]:
            print(f"  bookkeeping warning: {w}")
        if bk["gap"]:
            store.append_journal({"event": "exit_bookkeeping_gap", "ts": ts,
                                  "sold": sorted(sold),
                                  "reason": "executor reported a sale but wrote no staging "
                                            "file — ledger and positions.json NOT updated"})
            notify("EXIT BOOKKEEPING INCOMPLETE",
                   f"{', '.join(sorted(sold))} SOLD but the executor staged nothing: "
                   f"ledger + positions.json are stale until reconciled. The 08:00 "
                   f"health check will flag unrecorded fills.",
                   tags="rotating_light")
```

- [ ] **Step 3: Fix the two docstring blocks that now contradict the code**

In the `#:` comment block above `EXECUTOR_TOOLS` (begins `#: prompts/exit.md does need Bash, Read and Write`), replace the first sentence so the block reads:

```python
#: prompts/exit.md needs Read and Write — it writes its own result file and the
#: staging files the monitor's recorders consume (src/exit_bookkeeping.py). It
#: no longer needs Bash: until 2026-09-03 it ran the four recorder scripts
#: itself under exact-match grants, and one un-retried refusal left a filled
#: sale unrecorded. This is
```
(keep the rest of that block from `NOT the sessions' MCP architecture` onward unchanged).

In `executor_argv`'s docstring, replace the paragraph that begins `⚠️ NOT \`--tools ""\`.` with:

```
    ⚠️ NOT `--tools ""`. The sessions disable every built-in because they need
    no shell. This executor still needs Read and Write: it writes its own
    result file and the staging files (fills.json, broker_state.json,
    exit_closes.json, partial_closes.json, orders_dump.json) that the monitor
    hands to the recorder scripts after this process returns. It does NOT run
    those scripts any more — see exit_bookkeeping.record_exits — so it holds
    no Bash grant at all.
```

- [ ] **Step 4: Compile under 3.10 and read the changed block as the monitor executes it**

Run: `cd /opt/agentic-trader && /usr/bin/python3 -m py_compile scripts/market_monitor.py && echo COMPILES_310 && grep -n 'exit_bookkeeping' scripts/market_monitor.py`
Expected: `COMPILES_310` and three hits: the import, the `record_exits` call, and the docstring mention.

Then read `check_once` from `_request = {"ts": ts, ...` through `_save(STATE, st)` / `return len(act)` in one pass and confirm: `sold` is defined before the new block; `ts` and `store` are in scope (both are used by the `executor_breaker` journal write above); the `failed`/`unresolved` logic below is untouched.

- [ ] **Step 5: Reload the monitor**

Run: `cd /opt/agentic-trader && .venv/bin/python scripts/reload_stale.py --dry-run && .venv/bin/python scripts/reload_stale.py; echo "rc=$?"; systemctl is-active agentic-monitor; journalctl -u agentic-monitor --since '-2 min' --no-pager | tail -5`
Expected: dry-run names `agentic-monitor` as stale; the real run restarts it; `rc=0`; `active`; the journal tail shows the monitor's normal startup lines with no traceback. If reload refuses because an exit is in flight, wait for `exit_result.json` to appear and re-run.

- [ ] **Step 6: Commit**

```bash
cd /opt/agentic-trader && git add scripts/market_monitor.py && git commit -q -m "monitor: run the exit recorders itself after every executor return; gap + failure are paged

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012BXt93L5D51yXw1LPSBc6i"
```

---

### Task 4: The executor's contract — no Bash grants, and a prompt that stops at the staging files

**Files:**
- Modify: `deploy/exit_executor_settings.json` — `permissions.allow` (remove four entries) and `_comment` (one added line).
- Modify: `prompts/exit.md` — steps 6, 7, 7c, 7d, 7e.

**Interfaces:**
- Consumes: the staging filenames in `exit_bookkeeping.STEPS` and `SNAPSHOT_INPUT` (Task 2). The prompt must name exactly those files.

- [ ] **Step 1: Remove the four Bash grants**

```bash
cd /opt/agentic-trader && .venv/bin/python - <<'EOF'
import json
from pathlib import Path
p = Path("deploy/exit_executor_settings.json")
d = json.loads(p.read_text())
before = list(d["permissions"]["allow"])
d["permissions"]["allow"] = [r for r in before if not r.startswith("Bash(")]
removed = [r for r in before if r.startswith("Bash(")]
assert len(removed) == 4, removed
d["_comment"].append("2026-09-03: the four Bash(record_*.py) grants are GONE. The monitor runs the "
                     "recorders after this process returns (src/exit_bookkeeping.py); one "
                     "un-retried exact-match refusal had left a filled sale unrecorded. Bash "
                     "is now denied here by dontAsk with nothing allowed.")
p.write_text(json.dumps(d, indent=2) + "\n")
print("removed:", *removed, sep="\n  ")
print("Bash grants left:", [r for r in d["permissions"]["allow"] if "Bash" in r])
EOF
```
Expected: the four `Bash(.venv/bin/python scripts/record_*.py)` lines listed under `removed:`, then `Bash grants left: []`. The `Bash(cat .env*)` deny rule is untouched (it is under `deny`).

- [ ] **Step 2: Rewrite the prompt's recorder steps**

In `prompts/exit.md`:

(a) Step 6 — replace the last sentence pair
```
   Do NOT run the recorder yet — step 7 supplies the broker state it publishes
   alongside these fills, and the two must be written together.
```
with
```
   You do not run any recorder. The monitor runs them after you exit (see
   step 7). Your job is the files.
```

(b) Step 7 — replace the paragraph from `Then run \`.venv/bin/python scripts/record_fills.py\` ONCE.` through `shows positions that no longer exist.` with:
```
   ⛔ **DO NOT run `scripts/record_fills.py` — you have no Bash grant and it
   is not yours to run.** After you exit, the monitor runs the recorders in
   this order, each only if its file exists, and moves each consumed file to
   `research_store/rh/consumed/`:
     1. `record_fills.py`           ← `fills.json` (+ `broker_state.json`)
     2. `record_exit_outcome.py`    ← `exit_closes.json`
     3. `record_partial_outcome.py` ← `partial_closes.json`
     4. `reconcile_ledger.py`       ← `orders_dump.json`
   `record_fills.py` journals your fills AND publishes `positions.json`, both
   under one timestamp — which is why `fills.json` and `broker_state.json`
   must both be complete before you finish.
   ⛔ **DO NOT hand-write `positions.json`.** You have no permission to, and
   it is not a formatting preference: the publisher REJECTS a truncated or
   unreadable broker read rather than coercing it, because a partial book looks
   current and silently unprotects whatever it dropped. Passing the pages
   transcript is how completeness is PROVEN — `exhausted:true` with each
   page's cursor matching the prior response's next. If the publisher refuses
   the write the previous snapshot stays intact, `broker_state.json` is kept,
   and the monitor pages the operator; your fills are already journaled.
   The dashboard and equity log read this file; a stale snapshot after an exit
   shows positions that no longer exist.
```
Keep the `Also refresh the realized-P&L snapshot` paragraph that follows exactly as it is.

(c) Step 7c — change `— then run\n    \`.venv/bin/python scripts/record_exit_outcome.py\`:` to `. The monitor runs\n    \`record_exit_outcome.py\` on it after you exit; the fields:` and delete the sentence `Do NOT hand-edit \`journal.jsonl\` and do NOT write your own python snippet;\n    this helper is the only writer.` replacing it with `Do NOT hand-edit \`journal.jsonl\`; the monitor's recorder is the only writer.`

(d) Step 7d — change `— then run\n    \`.venv/bin/python scripts/record_partial_outcome.py\`:` to `. The monitor runs\n    \`record_partial_outcome.py\` on it after you exit; the fields:`. Change `Same\n    rules as 7c: \`UNANCHORED\` is reported not retried, and this helper is the\n    only writer.` to `Same rules as 7c.`

(e) Step 7e — replace the whole step with:
```
7e. **Ledger reconcile input.** Write `research_store/rh/orders_dump.json` (the
    raw `get_equity_orders` response shape) from `get_equity_orders`. The monitor
    runs `reconcile_ledger.py` on it after you exit; a divergence pages the
    operator.
```

- [ ] **Step 3: READ THE WHOLE PROMPT as the executor receives it**

Run: `cd /opt/agentic-trader && cat prompts/exit.md` and read every line. Check specifically:
- (a) no remaining instruction to *run* anything: `grep -n 'run \`\|\.venv/bin/python\|Don.t suppress' prompts/exit.md` must print only lines that say the MONITOR runs a script (steps 7, 7c, 7d, 7e) — zero lines instructing the executor to run one.
- (b) nothing says the same thing twice (the "do not hand-write positions.json" warning appears once, in step 7).
- (c) every filename in the prompt appears in `exit_bookkeeping.STEPS`/`SNAPSHOT_INPUT`, plus `exit_result.json` and `realized.json`, which are not recorder inputs: `grep -o 'research_store/rh/[a-z_]*\.json\|research_store/monitor/[a-z_]*\.json' prompts/exit.md | sort -u`.

Expected for (c), exactly:
```
research_store/monitor/exit_request.json
research_store/monitor/exit_result.json
research_store/rh/broker_state.json
research_store/rh/exit_closes.json
research_store/rh/fills.json
research_store/rh/orders_dump.json
research_store/rh/partial_closes.json
research_store/rh/realized.json
```

- [ ] **Step 4: Confirm the executor's own reference to the grants agrees**

Run: `cd /opt/agentic-trader && grep -n 'four specific\|record_\*\.py\|Bash' scripts/market_monitor.py | sed -n 1,12p`
Expected: the comment block edited in Task 3 Step 3 describes "no Bash grant"; the sentence `deploy/exit_executor_settings.json permitted only four specific ... commands` (in the `#:` block) refers to the PROBE result of the past and is left as history — read it and confirm it is phrased as past tense; if it reads as current, change `permitted` to `at the time permitted`.

- [ ] **Step 5: Commit**

```bash
cd /opt/agentic-trader && git add deploy/exit_executor_settings.json prompts/exit.md scripts/market_monitor.py && git commit -q -m "exit executor: no Bash grants; the prompt writes staging files and the monitor records them

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012BXt93L5D51yXw1LPSBc6i"
```

---

### Task 5: Session fills push to the phone

**Files:**
- Modify: `src/notify.py` — add `fill_line(f: dict) -> str` above `_selftest`; extend `_selftest`.
- Modify: `scripts/record_fills.py:53` import, and `_push_summary` (the `lines = [...]` for `placed`).
- Modify: `src/agent_env/server.py` — `record_fills`, after `store.append_journal({"event": "execution", "ts": ts, "source": "session", "fills": fills})`.

**Interfaces:**
- Produces: `notify.fill_line(f) -> str` — `"BUY DELL $4.00 @ $512.17"`; amount formatted to 2 dp when numeric, `avg_price` as-is when present, else `(status)`.

- [ ] **Step 1: Add the shared formatter and its assertions to `src/notify.py`**

Insert above `def _selftest() -> None:`:

```python
def fill_line(f: dict) -> str:
    """One phone line for one order: 'BUY DELL $4.00 @ $512.17'.

    Shared by scripts/record_fills.py (the exit path) and the sessions' MCP
    record_fills so the two paths read the same on the phone. `amount` is a
    float from the MCP path and a string from the exit path; both render.
    """
    amt = f.get("amount", "?")
    try:
        amt_s = f"{float(amt):.2f}"
    except (TypeError, ValueError):
        amt_s = str(amt)
    line = f"{str(f.get('side', '?')).upper()} {f.get('symbol', '?')} ${amt_s}"
    if f.get("avg_price"):
        return line + f" @ ${f['avg_price']}"
    return line + f" ({f.get('status', '?')})"
```

Inside `_selftest`, before its final `print`, add:

```python
    assert fill_line({"side": "buy", "symbol": "DELL", "amount": 4.0, "avg_price": "512.17"}) \
        == "BUY DELL $4.00 @ $512.17"
    assert fill_line({"side": "sell", "symbol": "MU", "amount": "3.65", "status": "skipped"}) \
        == "SELL MU $3.65 (skipped)"
    assert fill_line({}) == "? ? $? (?)"
```

- [ ] **Step 2: Verify by direct call**

Run: `cd /opt/agentic-trader && .venv/bin/python -c "import sys; sys.path.insert(0,'src'); import notify; print(notify.fill_line({'side':'buy','symbol':'DELL','amount':4.0,'avg_price':'512.17'})); print(notify.fill_line({'side':'sell','symbol':'MU','amount':'3.65','status':'skipped'}))"`
Expected:
```
BUY DELL $4.00 @ $512.17
SELL MU $3.65 (skipped)
```

- [ ] **Step 3: Use it in `scripts/record_fills.py`**

Change line 53 to `from notify import push, fill_line           # noqa: E402` and in `_push_summary` replace
```python
    lines = [f"{f.get('side', '?').upper()} {f.get('symbol', '?')} ${f.get('amount', '?')}"
             + (f" @ ${f['avg_price']}" if f.get("avg_price") else f" ({f.get('status', '?')})")
             for f in placed]
```
with
```python
    lines = [fill_line(f) for f in placed]
```

- [ ] **Step 4: Push from the sessions' MCP `record_fills`**

In `src/agent_env/server.py`, immediately after
```python
    store.append_journal({"event": "execution", "ts": ts,
                          "source": "session", "fills": fills})
```
add:
```python
    # Phone push, one line per NEW fill (the idempotent filter above means a
    # re-call never re-pushes). The exit path's recorder has always done this;
    # the sessions' recorder never did, and the principal expected it
    # (2026-09-03). push() never raises; the ledger write above never
    # depends on it.
    notify.push(f"Agentic session: {len(fills)} order{'s' if len(fills) != 1 else ''} filled",
                "\n".join(notify.fill_line(f) for f in fills), tags="money_with_wings")
```
(`notify` is already imported at `server.py:38`.)

- [ ] **Step 5: Verify the server tool still imports and the push helper is reachable**

Run: `cd /opt/agentic-trader && .venv/bin/python -c "import sys; sys.path.insert(0,'src'); import agent_env.server as s; import inspect; src=inspect.getsource(s.record_fills); print('push wired:', 'notify.fill_line' in src and 'money_with_wings' in src)"`
Expected: `push wired: True` (the module imports cleanly under the venv).

- [ ] **Step 6: Reload what is stale**

Run: `cd /opt/agentic-trader && .venv/bin/python scripts/reload_stale.py --dry-run; .venv/bin/python scripts/reload_stale.py; echo "rc=$?"`
Expected: `notify.py` reaches the monitor (it imports `push`) and the dashboard; both restart; `rc=0`. If the monitor refuses (exit in flight), wait and re-run.

- [ ] **Step 7: Commit**

```bash
cd /opt/agentic-trader && git add src/notify.py scripts/record_fills.py src/agent_env/server.py && git commit -q -m "notify: shared fill_line; sessions' record_fills pushes one line per filled order (principal, 2026-09-03)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012BXt93L5D51yXw1LPSBc6i"
```

---

### Task 6: Documentation, and the whole-document read

**Files:**
- Modify: `CLAUDE.md` — the `research_store/rh/positions.json` paragraph in the repo layout (the sentence beginning `The EXIT path (prompts/exit.md step 7) reaches it via`), and the `scripts/market_monitor.py` layout entry.
- Modify: `docs/OPSLOG.md` — a new dated entry at the top.

- [ ] **Step 1: CLAUDE.md**

In the `positions.json` paragraph, replace
```
The EXIT path (prompts/exit.md step 7) reaches it via
                        scripts/record_fills.py + research_store/rh/broker_state.json,
                        because deploy/exit_mcp.json deliberately does NOT mount
                        the agentic-trader MCP.
```
with
```
The EXIT path reaches it via scripts/record_fills.py +
                        research_store/rh/broker_state.json — RUN BY THE MONITOR
                        (src/exit_bookkeeping.py, 2026-09-03), not by the
                        executor: the executor writes the staging files and
                        holds no Bash grant. deploy/exit_mcp.json deliberately
                        does NOT mount the agentic-trader MCP.
```

In the `scripts/market_monitor.py` layout entry, append after `[monitor] config.`:
```
                        ⛔ SINCE 2026-09-03 THE MONITOR RECORDS EVERY EXIT
                        ITSELF (src/exit_bookkeeping.py): after the executor
                        returns it runs record_fills / record_exit_outcome /
                        record_partial_outcome / reconcile_ledger from the
                        staging files, archives each consumed file to
                        research_store/rh/consumed/, and pages on a failure or
                        on "sold but nothing staged". The executor used to run
                        those under exact-match Bash grants and one un-retried
                        refusal left a filled DELL trim unrecorded.
```

- [ ] **Step 2: OPSLOG entry (top of file, above the 2026-09-03 outage entry)**

```markdown
## 2026-09-03 (later) — the monitor records every exit; the executor keeps only its pen

Spec `docs/superpowers/specs/2026-09-03-model-fallback-and-exit-bookkeeping-design.md`
§4 and §7, plan 1 of 4. What changed:

- `src/exit_bookkeeping.py` (new, 3.10-safe): after `run_executor` returns,
  the monitor runs the four recorder scripts from whichever staging files
  exist, in the order the prompt always ran them, archives each consumed
  file to `research_store/rh/consumed/<name>.<ts>.json`, pages on a non-zero
  script, and pages + journals `exit_bookkeeping_gap` when something sold
  but nothing was staged. A leftover staging file is still run (every
  recorder is idempotent) — never skipped silently.
- `deploy/exit_executor_settings.json`: the four `Bash(record_*.py)` grants
  are gone. `prompts/exit.md` steps 6–7e write files and name the monitor as
  the runner. The prompt was read whole after the edit.
- `src/notify.fill_line` is shared by the exit recorder and the sessions'
  MCP `record_fills`, which now pushes one line per new fill — the
  principal's request after asking "no notifications?" on the 09-03 buys.
- The DELL 09-03 `partial_outcome` was recorded by hand (see the morning
  entry's addendum).

What this makes impossible: a filled exit whose ledger and snapshot stay
silent because a model did not type a command, including when the
executor dies after placing. What it does NOT change: the sale itself is
still the executor's (or, from plan 3, the code seller's); `exit_result.json`
is still the monitor's trigger-fired signal.
```

- [ ] **Step 3: Read both documents whole, as their consumers see them**

Run `cat CLAUDE.md` and read the full `positions.json` and `market_monitor.py` entries plus the `scripts/hooks/pretooluse_order_gate.py` entry, checking: (a) nothing elsewhere still says the executor runs the recorders — `grep -n 'record_fills' CLAUDE.md` and read each hit; (b) the new text is not a duplicate of an existing sentence; (c) every path named exists: `ls src/exit_bookkeeping.py research_store/rh/consumed deploy/exit_executor_settings.json`.

Run `sed -n 1,80p docs/OPSLOG.md` and confirm the new entry sits above the outage entry and the spec path it names exists.

- [ ] **Step 4: Commit and push**

```bash
cd /opt/agentic-trader && git add CLAUDE.md docs/OPSLOG.md && git commit -q -m "docs: monitor-owned exit bookkeeping (CLAUDE.md layout, OPSLOG 2026-09-03 later)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012BXt93L5D51yXw1LPSBc6i" && git push -q 2>&1 | grep -v '^remote:'; echo "HEAD $(git rev-parse --short HEAD) remote $(git ls-remote origin refs/heads/main | cut -c1-7)"
```
Expected: the two short hashes match.

- [ ] **Step 5: Final reload check**

Run: `cd /opt/agentic-trader && .venv/bin/python scripts/reload_stale.py --dry-run`
Expected: `reload_stale: nothing stale — every watched service matches disk`.
