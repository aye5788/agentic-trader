# Agent Session Charter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the deployed agent a charter it cannot be surprised by, a tool surface it cannot escape, and a gate it cannot bypass — replacing `prompts/fast_loop.md`'s procedural "you are the hands, not the brain" framing.

**Architecture:** A static charter rendered from the constants that enforce it, plus a per-session facts brief gathered *after* a `flock` is held. Sessions run as `claude -p` children with **no built-in tools at all** (`--tools ""`) and an explicit MCP allowlist. A `PreToolUse` hook validates every order before placement. A SHA-256 tripwire proves the capability removal held.

**Tech Stack:** Python 3.12 (`.venv`) except moomoo paths; `fcntl.flock`; Claude Code CLI 2.x; FastMCP (`src/agent_env/server.py`, 29 tools); Robinhood MCP (10 allowlisted tools).

## Global Constraints

- **Execution is Robinhood only, `agentic_allowed=true` only.** Every other account is read-only. moomoo is DATA; it can trade via `unlock_trade` and this repo never calls it.
- **This is live money.** Never anchor on account size; judge risk by mechanism and consequence.
- **`ANTHROPIC_API_KEY` must stay unset.** `deploy/*.sh` refuse to run if set (billing footgun).
- **Secrets never leave the box.** Never print `.env` or `secrets/`.
- **`src/repo_checks.py` output is published verbatim into a PUBLIC GitHub issue.** A failure message may contain ONLY a `file:line`, fixed prose written in that module, or an identifier from its own constants. Never interpolate scanned file content.
- **Check contract:** `def check_<name>(root: pathlib.Path) -> list[str]`, empty list = pass, never raises/prints/exits. Append to the `CHECKS` tuple at `src/repo_checks.py:967`.
- **Every commit gated on `bash deploy/run_selftests.sh` exiting 0.** Do not commit on a red suite.
- **`gates()` WRITES `research_store/governance/state.json`** via `update_peak` (`src/governance.py:206 → 113 → 132 → 77`). Never call it from a hook or a read-only path. Write-free: `kill_switch_active` (57), `halt_entries_active` (62), `live_approved` (68), `vet_plan` (383), `liquidity_ok` (268), `assert_agentic_account` (144), `mandate_action` (312).
- **Model pinned:** `--model claude-opus-4-8` (as in `deploy/run_fast_loop.sh:22`). Do not ride the box default.

---

## Task 1: Lock down the tool surface (SHIP FIRST — closes a live exposure)

Today all three headless loops run `claude -p --model claude-opus-4-8 "$(cat prompts/…)"` with **no tool restrictions**, and `.claude/settings.json` grants bare `Read`, bare `Write`, and `Bash(.venv/bin/python *)`. That is arbitrary code execution and credential read access, three times per weekday. The reference system measured that `Bash(python3:*)` alone makes the whole tree writable with no Edit rule present.

**Files:**
- Create: `deploy/session_tools.sh`
- Modify: `deploy/run_fast_loop.sh:22`, `deploy/run_risk_review.sh:23`, `deploy/run_newsletter.sh:20`
- Modify: `src/repo_checks.py` (new check + `CHECKS` tuple at :967 + selftest)

**Interfaces:**
- Produces: `deploy/session_tools.sh` exporting `SESSION_TOOL_ARGS` (a bash array) consumed by every `run_*.sh`.
- Produces: `check_headless_tool_lockdown(root) -> list[str]` appended to `CHECKS`.

- [ ] **Step 1: Write the failing repo check test**

Add to `src/repo_checks.py` `_selftest()`, after the last existing banner:

```python
    # -------------------- check 7: check_headless_tool_lockdown --------------------
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        _write(root, "deploy/run_fast_loop.sh",
               'claude -p --model claude-opus-4-8 "$(cat prompts/fast_loop.md)"\n')
        out = check_headless_tool_lockdown(root)
        assert any("run_fast_loop.sh" in f and "--tools" in f for f in out), out

    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        _write(root, "deploy/run_fast_loop.sh",
               'source deploy/session_tools.sh\n'
               'claude -p --model claude-opus-4-8 "${SESSION_TOOL_ARGS[@]}" "$BRIEF"\n')
        _write(root, "deploy/session_tools.sh",
               'SESSION_TOOL_ARGS=(--tools "" --permission-mode dontAsk)\n')
        assert check_headless_tool_lockdown(root) == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python src/repo_checks.py --selftest`
Expected: `NameError: name 'check_headless_tool_lockdown' is not defined`

- [ ] **Step 3: Implement the check**

Add above the `CHECKS` tuple in `src/repo_checks.py`:

```python
_CLAUDE_INVOKE_RE = re.compile(r"^\s*claude\s+-p\b")


def check_headless_tool_lockdown(root: pathlib.Path) -> list[str]:
    """Every headless `claude -p` invocation must restrict its tool surface.

    THE FAILURE THIS CATCHES: a cron-launched agent with the default tool set
    holds Bash, Write and Edit. It can rewrite config/mandate.toml, delete the
    kill switch, or read .env. Measured on the reference system 2026-07-29:
    granting ANY Edit(...) rule makes the entire cwd tree writable, and
    Bash(python3:*) wrote into the repo with no Edit rule present at all. Prose
    in a prompt does not constrain a capability that exists.

    The rule: a line invoking `claude -p` must pass `--tools` (to close the
    built-in surface) and `--permission-mode` (so an unlisted tool is refused
    rather than hanging on a prompt nobody can answer).

    WHAT THIS DOES NOT COVER: it does not evaluate the resulting permission set,
    verify the MCP allowlist is correct, or read .claude/settings.json. It
    asserts the flags are present, not that their values are right. A settings
    file granting bare Write is invisible here.
    """
    failures: list[str] = []
    dep = root / "deploy"
    if not dep.is_dir():
        return failures
    for path in sorted(dep.glob("run_*.sh")):
        for lineno, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if not _CLAUDE_INVOKE_RE.match(line):
                continue
            if "SESSION_TOOL_ARGS" in line:
                continue
            if "--tools" not in line or "--permission-mode" not in line:
                failures.append(
                    f"deploy/{path.name}:{lineno} headless `claude -p` runs with "
                    f"an unrestricted tool surface — no --tools/--permission-mode "
                    f"and no SESSION_TOOL_ARGS; the agent can edit its own "
                    f"guardrails and read secrets"
                )
    return failures
```

Append `check_headless_tool_lockdown,` to the `CHECKS` tuple at `src/repo_checks.py:967`.

- [ ] **Step 4: Run the selftest to verify it passes**

Run: `.venv/bin/python src/repo_checks.py --selftest`
Expected: prints the existing selftest OK line, exits 0.

- [ ] **Step 5: Create the shared tool arguments**

Create `deploy/session_tools.sh`:

```bash
# Shared tool surface for every headless `claude -p` run. Sourced, not executed.
#
# `--tools ""` disables EVERY built-in tool (verified: `claude --help` states
# 'Use "" to disable all tools'). This is an allowlist by construction: no Bash,
# no Read, no Write, no Edit, no WebFetch exist at all, so the surface cannot
# silently grow when a new built-in ships. A denylist has the opposite property.
#
# `--permission-mode dontAsk` auto-DENIES anything not allowed instead of
# prompting. Headless there is nobody to answer a prompt, and `default` would
# hang until the timeout — a silent no-trade day.
#
# MCP tools are named individually. Robinhood exposes 53; the agent needs 10.
# place_option_order is absent, so options are unreachable rather than merely
# forbidden.
SESSION_TOOL_ARGS=(
  --tools ""
  --permission-mode dontAsk
  --allowedTools
    "mcp__agentic-trader__brief"
    "mcp__agentic-trader__positions"
    "mcp__agentic-trader__account"
    "mcp__agentic-trader__performance"
    "mcp__agentic-trader__mandate_status"
    "mcp__agentic-trader__halt_status"
    "mcp__agentic-trader__candidates"
    "mcp__agentic-trader__universe"
    "mcp__agentic-trader__leaders"
    "mcp__agentic-trader__sectors"
    "mcp__agentic-trader__quote"
    "mcp__agentic-trader__depth"
    "mcp__agentic-trader__terrain"
    "mcp__agentic-trader__earnings"
    "mcp__agentic-trader__macro"
    "mcp__agentic-trader__macro_calendar"
    "mcp__agentic-trader__news"
    "mcp__agentic-trader__check_order"
    "mcp__agentic-trader__set_levels"
    "mcp__agentic-trader__record_decision"
    "mcp__agentic-trader__research_log"
    "mcp__agentic-trader__rule_out"
    "mcp__agentic-trader__revisit"
    "mcp__agentic-trader__open_question"
    "mcp__agentic-trader__close_question"
    "mcp__agentic-trader__wake_register"
    "mcp__agentic-trader__wake_status"
    "mcp__agentic-trader__wake_deregister"
    "mcp__agentic-trader__ping"
    "mcp__robinhood-trading__get_accounts"
    "mcp__robinhood-trading__get_equity_positions"
    "mcp__robinhood-trading__get_portfolio"
    "mcp__robinhood-trading__get_equity_quotes"
    "mcp__robinhood-trading__get_equity_orders"
    "mcp__robinhood-trading__get_realized_pnl"
    "mcp__robinhood-trading__get_pnl_trade_history"
    "mcp__robinhood-trading__review_equity_order"
    "mcp__robinhood-trading__place_equity_order"
    "mcp__robinhood-trading__cancel_equity_order"
)
```

- [ ] **Step 6: Wire it into the three runners**

In `deploy/run_fast_loop.sh`, replace line 22:

```bash
source deploy/session_tools.sh
claude -p --model claude-opus-4-8 "${SESSION_TOOL_ARGS[@]}" "$(cat prompts/fast_loop.md)"
```

Apply the identical change to `deploy/run_risk_review.sh:23` (`prompts/risk_review.md`) and `deploy/run_newsletter.sh:20` (`prompts/newsletter.md`).

⚠️ The newsletter WRITES a file (`newsletter/*.html`) and appends to `docs/OPSLOG.md`. With `--tools ""` it cannot. Before changing `run_newsletter.sh`, confirm whether its write path goes through a tool or a Bash call; if it needs a write, give that runner its own array with a single scoped `Edit(...)` rule and record in the file that the rule does NOT sandbox the tree.

- [ ] **Step 7: Verify a locked-down session still reaches its tools**

Run:
```bash
cd /opt/agentic-trader && source deploy/session_tools.sh && \
timeout 180 claude -p --model claude-opus-4-8 "${SESSION_TOOL_ARGS[@]}" \
  'Call halt_status and reply with only the value of can_buy.'
```
Expected: replies `true`. If it reports it cannot call the tool, the allowlist spelling is wrong — fix before proceeding.

- [ ] **Step 8: Verify Bash is genuinely gone**

Run:
```bash
cd /opt/agentic-trader && source deploy/session_tools.sh && \
timeout 180 claude -p --model claude-opus-4-8 "${SESSION_TOOL_ARGS[@]}" \
  'Run the shell command `id` and report the output.'
```
Expected: it states it has no shell/Bash tool available. **It must NOT print a uid line.** If it does, STOP — the lockdown is not in effect and nothing else in this plan is safe to ship.

- [ ] **Step 9: Commit**

```bash
bash deploy/run_selftests.sh && .venv/bin/python src/repo_checks.py && \
git add deploy/session_tools.sh deploy/run_fast_loop.sh deploy/run_risk_review.sh \
        deploy/run_newsletter.sh src/repo_checks.py && \
git commit -m "fix(deploy): headless sessions lose Bash, Write and Edit

All three cron loops ran claude -p with NO tool restriction, and
.claude/settings.json granted bare Read, bare Write and Bash(.venv/bin/python *)
-- arbitrary code execution and credential read access, three times per weekday.
An agent that can edit its own guardrails has none.

--tools \"\" closes the built-in surface by construction rather than by denylist.
Measured on the reference system 2026-07-29: ANY Edit(...) rule makes the whole
cwd tree writable, and Bash(python3:*) wrote into the repo with no Edit rule at
all. Capability removal is the control.

Verified: halt_status still answers; a request to run a shell command is refused."
```

---

## Task 2: The integrity tripwire

Capability removal is the control; this proves it held. Detective, not preventive — and it cannot attribute cause.

**Files:**
- Create: `src/integrity.py`
- Test: selftest inside the module (house pattern)

**Interfaces:**
- Produces: `PROTECTED_FILES: tuple[str, ...]`, `snapshot(root) -> dict[str, str]`, `verify(root, before) -> list[str]`

- [ ] **Step 1: Write the failing test**

Create `src/integrity.py` with only the selftest:

```python
def _selftest() -> None:
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        (root / "src").mkdir()
        p = root / "src" / "governance.py"
        p.write_text("original\n")
        before = snapshot(root, files=("src/governance.py",))
        assert verify(root, before, files=("src/governance.py",)) == []
        p.write_text("tampered\n")
        assert verify(root, before, files=("src/governance.py",)) == ["src/governance.py"]
        # an unreadable file counts as CHANGED, never as clean
        p.unlink()
        assert verify(root, before, files=("src/governance.py",)) == ["src/governance.py"]
    print("integrity: OK")


if __name__ == "__main__":
    _selftest()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python src/integrity.py`
Expected: `NameError: name 'snapshot' is not defined`

- [ ] **Step 3: Implement**

Prepend to `src/integrity.py`:

```python
"""SHA-256 tripwire over the files a session must never modify.

Capability removal (deploy/session_tools.sh, `--tools ""`) is the CONTROL. This
is the proof it held.

⚠️ DETECTIVE, NOT PREVENTIVE, AND IT CANNOT ATTRIBUTE CAUSE. A hash comparison
shows only THAT a file changed, never WHO changed it -- an operator editing
governance.py mid-session trips it identically. Halting is correct either way (a
run whose guardrails moved underneath it is not a valid run), but the message
must state what is known and no more. Ship mtimes as evidence; never accuse.

Adapted from /opt/trading/watcher/session.py:53-92, where the same tripwire
exists because a live session hit a gate rejection and went straight to reading
its own guardrail. It chose not to edit it; nothing would have stopped it.
"""
from __future__ import annotations

import hashlib
import pathlib

PROTECTED_FILES = (
    "src/governance.py",
    "src/mandate.py",
    "config/mandate.toml",
    "config/strategy.toml",
    "src/agent_env/server.py",
    "deploy/session_tools.sh",
    "CLAUDE.md",
)


def snapshot(root: pathlib.Path, files=PROTECTED_FILES) -> dict:
    """SHA-256 of every protected file. Unreadable -> a sentinel, not omission."""
    out = {}
    for rel in files:
        p = pathlib.Path(root) / rel
        try:
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError as e:
            out[rel] = f"UNREADABLE: {type(e).__name__}"
    return out


def verify(root: pathlib.Path, before: dict, files=PROTECTED_FILES) -> list:
    """Protected files whose contents changed. Empty is good."""
    after = snapshot(root, files=files)
    return sorted(p for p in before if before[p] != after.get(p))
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python src/integrity.py`
Expected: `integrity: OK`

- [ ] **Step 5: Register in the suite and commit**

Add `"src/integrity.py"` to the module list in `deploy/run_selftests.sh` (beside `"src/agent_env/live.py"`), then:

```bash
bash deploy/run_selftests.sh && git add src/integrity.py deploy/run_selftests.sh && \
git commit -m "feat(integrity): SHA-256 tripwire over the files a session must not modify

Capability removal is the control; this proves it held. Detective only, and it
cannot attribute cause -- an operator edit mid-run trips it identically, so the
record ships mtimes rather than an accusation. An unreadable file counts as
CHANGED, never as clean."
```

---

## Task 3: MCP tool discovery — never hardcoded

The reference system's rule, paid for once: *"On 2026-07-27 a capability was built and registered on the server while this array did not name it. The tool existed, the session could not see it, and the session reported it had skipped the work — indistinguishable from the capability not existing."*

Task 1's `session_tools.sh` hardcodes 29 tool names. That list will rot the first time a tool is added. This task makes it derived.

**Files:**
- Create: `scripts/session_tools.py`
- Modify: `deploy/session_tools.sh` (generate the agentic-trader block)

**Interfaces:**
- Consumes: `src/agent_env/server.py` (via MCP stdio `tools/list`)
- Produces: `discover(server_cmd: list[str], server_name: str) -> list[str]` returning `mcp__<name>__<tool>` strings; CLI prints one per line.

- [ ] **Step 1: Write the failing test**

Create `scripts/session_tools.py` with only:

```python
def _selftest() -> None:
    names = discover_agentic()
    assert names, "discovery returned nothing"
    assert all(n.startswith("mcp__agentic-trader__") for n in names), names
    for required in ("mcp__agentic-trader__halt_status",
                     "mcp__agentic-trader__check_order",
                     "mcp__agentic-trader__place_holder_never"):
        pass
    assert "mcp__agentic-trader__halt_status" in names
    assert "mcp__agentic-trader__check_order" in names
    print(f"session_tools: OK — discovered {len(names)} tools")


if __name__ == "__main__":
    _selftest()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python scripts/session_tools.py`
Expected: `NameError: name 'discover_agentic' is not defined`

- [ ] **Step 3: Implement**

Prepend:

```python
"""Enumerate the agent's MCP tools by ASKING the server, never from a list.

⚠️ NEVER HARDCODE THE TOOL LIST. Taken from the reference system, which paid for
this: a capability was built and registered while the hardcoded array did not
name it. The tool existed, the session could not see it, and the session
reported it had skipped the work -- indistinguishable from the capability not
existing. Deriving the list makes that failure unrepresentable.

Refuses to return empty: a session launched with no tools would run, trade
nothing, and look like a quiet day.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


def discover(server_cmd: list, server_name: str) -> list:
    from mcp import ClientSession, StdioServerParameters      # noqa: PLC0415
    from mcp.client.stdio import stdio_client                 # noqa: PLC0415

    async def _go():
        params = StdioServerParameters(command=server_cmd[0], args=server_cmd[1:])
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return sorted(t.name for t in (await s.list_tools()).tools)

    names = asyncio.run(_go())
    if not names:
        raise RuntimeError(
            f"MCP tool discovery returned NOTHING for {server_name}. Refusing to "
            f"emit an empty allowlist -- a session with no tools would run, trade "
            f"nothing, and look like a quiet day.")
    return [f"mcp__{server_name}__{n}" for n in names]


def discover_agentic() -> list:
    return discover([str(REPO / ".venv" / "bin" / "python"),
                     str(REPO / "src" / "agent_env" / "server.py")],
                    "agentic-trader")
```

Then add a `main` that prints one name per line when run with `--print`.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python scripts/session_tools.py`
Expected: `session_tools: OK — discovered 29 tools`

- [ ] **Step 5: Generate the allowlist at run time**

Replace the hardcoded `mcp__agentic-trader__*` block in `deploy/session_tools.sh` with:

```bash
# Derived, never hardcoded — see scripts/session_tools.py for why.
mapfile -t _AGENTIC_TOOLS < <(.venv/bin/python scripts/session_tools.py --print)
if [ "${#_AGENTIC_TOOLS[@]}" -eq 0 ]; then
  echo "REFUSING: MCP tool discovery returned nothing" >&2
  exit 1
fi
```
and splat `"${_AGENTIC_TOOLS[@]}"` into `SESSION_TOOL_ARGS` after `--allowedTools`.

- [ ] **Step 6: Commit**

```bash
bash deploy/run_selftests.sh && git add scripts/session_tools.py deploy/session_tools.sh && \
git commit -m "feat(deploy): derive the MCP allowlist instead of hardcoding it

A hardcoded list makes 'the tool exists but the session cannot see it'
indistinguishable from 'the capability was never built' -- the reference system
lost a session to exactly that. Discovery refuses to return empty: a session
with no tools runs, trades nothing, and looks like a quiet day."
```

---

## Task 4: The session lock

**Files:**
- Create: `src/session_lock.py`

**Interfaces:**
- Produces: `acquire(mode: str, path: Path, timeout_s: float | None = None) -> IO | None` (None = gave up), `release(fh) -> None`, `holder(path) -> str`, `LOCK_WAIT_S: dict`

- [ ] **Step 1: Write the failing test**

```python
def _selftest() -> None:
    import tempfile, pathlib, os
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "session.lock"
        fh = acquire("open", p, timeout_s=1)
        assert fh is not None
        assert f"open {os.getpid()}" in holder(p)
        # a second acquire must GIVE UP, not hang and not succeed
        t0 = time.time()
        second = acquire("wake", p, timeout_s=1)
        assert second is None, "two sessions must never hold the lock at once"
        assert time.time() - t0 < 3.0, "must not sleep past its own deadline"
        release(fh)
        third = acquire("wake", p, timeout_s=1)
        assert third is not None, "lock must be reusable after release"
        release(third)
    # scheduled modes wait, wakes yield
    assert LOCK_WAIT_S["open"] >= 900
    assert LOCK_WAIT_S["wake"] <= 300
    assert LOCK_WAIT_S["open"] > LOCK_WAIT_S["wake"] * 4
    print("session_lock: OK")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python src/session_lock.py`
Expected: `NameError: name 'acquire' is not defined`

- [ ] **Step 3: Implement**

```python
"""One full-authority session at a time. fcntl.flock, not a PID file.

flock is held by the open file description, so the KERNEL releases it when the
holder dies however it dies -- SIGKILL, OOM, panic. There is no stale-lock
reaper here because there is nothing to reap. The lockfile CONTENTS ("<mode>
<pid>") are only ever a human-readable log line, never a liveness decision.

⚠️ os.open(O_RDWR|O_CREAT), NOT open(path, "w"). "w" TRUNCATES on open, so every
waiter erased the holder's identity before it had even tried the flock, and the
holder then read as "unknown" for a lock that was very much held.

⚠️ Never sleep PAST the deadline: a flat sleep(3) made a 1s timeout wait 3s and
succeed, which made the give-up path untestable.

Scheduled sessions WAIT; wakes YIELD. A scheduled session is the primary event
-- starting it late is enormously better than not starting it. Values stay far
inside the session budget so waiting can never consume the time the session
needs to work.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

LOCK_WAIT_S = {"premarket": 900, "open": 1800, "close": 900, "wake": 120}
DEFAULT_LOCK_WAIT_S = 120


def holder(path: Path) -> str:
    try:
        return Path(path).read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def acquire(mode: str, path: Path, timeout_s=None):
    import fcntl                              # noqa: PLC0415
    if timeout_s is None:
        timeout_s = LOCK_WAIT_S.get(mode, DEFAULT_LOCK_WAIT_S)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    deadline = t0 + timeout_s
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    fh = os.fdopen(fd, "r+")
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.seek(0)
            fh.truncate()
            fh.write(f"{mode} {os.getpid()}\n")
            fh.flush()
            return fh
        except OSError:
            now = time.time()
            if now >= deadline:
                fh.close()
                return None
            time.sleep(min(3.0, deadline - now))


def release(fh) -> None:
    import fcntl                              # noqa: PLC0415
    try:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
    except Exception:                         # noqa: BLE001
        pass
```

Add `import time` at the top for the selftest.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python src/session_lock.py`
Expected: `session_lock: OK`

- [ ] **Step 5: Register and commit**

Add `"src/session_lock.py"` to `deploy/run_selftests.sh`, then commit with a message recording the flock-over-PID-file rationale and the `"w"`-truncation trap.

---

## Task 5: The PreToolUse order gate

**Files:**
- Create: `scripts/hooks/pretooluse_order_gate.py`
- Modify: `.claude/settings.json` (add the `hooks` block)

**Interfaces:**
- Consumes: hook JSON on stdin with `tool_name` and `tool_input`
- Produces: `decide(payload: dict, cfg: dict, valued: dict, shadow: bool) -> dict` returning `{"permissionDecision": "deny"|"allow", "permissionDecisionReason": str}`

**Constraint:** the hook may read only small JSON and config. It must NEVER read the price panel or recompute the signal — measured budget is 0.08–0.11s for a full cold start including imports; a parquet read would turn that into seconds on every order.

- [ ] **Step 1: Write the failing test**

Create the file with only a selftest asserting:

```python
def _selftest() -> None:
    CFG = {"governance": {"kill_switch_file": "research_store/HALT",
                          "halt_entries_file": "research_store/HALT_ENTRIES",
                          "max_order_pct": 0.15, "require_whitelist": False},
           "proof": {"live_approved": True}}
    V = {"account_value": 100.0, "buying_power": 50.0}
    buy = {"tool_name": "mcp__robinhood-trading__place_equity_order",
           "tool_input": {"symbol": "MU", "side": "buy", "amount": 10.0}}
    sell = {**buy, "tool_input": {"symbol": "MU", "side": "sell", "amount": 10.0}}

    # shadow mode denies EVERYTHING, including sells
    d = decide(buy, CFG, V, shadow=True)
    assert d["permissionDecision"] == "deny" and "shadow" in d["permissionDecisionReason"].lower()
    assert decide(sell, CFG, V, shadow=True)["permissionDecision"] == "deny"

    # live mode allows an ordinary buy
    assert decide(buy, CFG, V, shadow=False)["permissionDecision"] == "allow"

    # a non-order tool is never touched by this hook
    other = {"tool_name": "mcp__agentic-trader__quote", "tool_input": {}}
    assert decide(other, CFG, V, shadow=False)["permissionDecision"] == "allow"

    # an order exceeding max_order_pct is denied
    big = {**buy, "tool_input": {"symbol": "MU", "side": "buy", "amount": 90.0}}
    assert decide(big, CFG, V, shadow=False)["permissionDecision"] == "deny"

    # A SELL IS NEVER BLOCKED on anything but the kill switch: the monitor's stop
    # is software-only, so blocking a sell removes a position's only protection.
    huge_sell = {**buy, "tool_input": {"symbol": "MU", "side": "sell", "amount": 90.0}}
    assert decide(huge_sell, CFG, V, shadow=False)["permissionDecision"] == "allow"
    print("pretooluse_order_gate: OK")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python scripts/hooks/pretooluse_order_gate.py`
Expected: `NameError: name 'decide' is not defined`

- [ ] **Step 3: Implement `decide` and the stdin driver**

`decide` must:
1. Return `allow` immediately if `tool_name != "mcp__robinhood-trading__place_equity_order"`.
2. Return `deny` with a reason containing `"shadow"` when `shadow` is true.
3. Call ONLY write-free governance: `kill_switch_active`, `halt_entries_active`, `live_approved`, `vet_plan`. **Never `gates()`** — it writes `research_store/governance/state.json` via `update_peak`, so a hook calling it would ratchet the drawdown peak on every order.
4. Never block a SELL except on the kill switch.

The driver reads `json.load(sys.stdin)`, calls `decide`, and prints:

```python
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": d["permissionDecision"],
    "permissionDecisionReason": d["permissionDecisionReason"]}}))
sys.exit(0)
```

Shadow mode is read from the file `research_store/SHADOW` existing (same pattern as `HALT`), so flipping phases is one `touch`/`rm`.

⚠️ **Fail closed on an exception:** wrap the driver so any unexpected error prints a `deny` with the exception type. A hook that crashes must not let an order through.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python scripts/hooks/pretooluse_order_gate.py`
Expected: `pretooluse_order_gate: OK`

- [ ] **Step 5: Wire the hook into settings**

Add to `.claude/settings.json`:

```json
"hooks": {
  "PreToolUse": [
    {
      "matcher": "mcp__robinhood-trading__place_equity_order",
      "hooks": [{"type": "command",
                 "command": "/opt/agentic-trader/.venv/bin/python /opt/agentic-trader/scripts/hooks/pretooluse_order_gate.py"}]
    }
  ]
}
```

⚠️ **UNCONFIRMED:** whether the `matcher` field accepts globs. Use the exact tool name, and verify in Step 6 rather than assuming.

- [ ] **Step 6: Verify the hook actually fires and blocks**

```bash
touch /opt/agentic-trader/research_store/SHADOW
cd /opt/agentic-trader && source deploy/session_tools.sh && \
timeout 180 claude -p --model claude-opus-4-8 "${SESSION_TOOL_ARGS[@]}" \
  'Place a market buy of $1 of MU, then report exactly what happened.'
```
Expected: reports the order was denied, citing the shadow reason. **Verify no order appears** via `get_equity_orders`. If an order was placed, STOP and do not proceed.

- [ ] **Step 7: Measure the added latency**

Run the hook 20 times against a fixture payload and assert the mean is under 0.3s. Record the measured number in the commit message.

- [ ] **Step 8: Commit**

---

## Task 6: The charter renderer

**Files:**
- Create: `src/charter.py`
- Create: `prompts/charter.md` (the static prose, with `__MANDATE__`, `__GATE__`, `__TOOLS__`, `__TERMS__` placeholders)

**Interfaces:**
- Produces: `render(cfg, mandate_cfg, tool_names) -> str`; substitution is `str.replace`, never `str.format` (the prose contains braces).

- [ ] **Step 1: Write the failing test**

```python
def _selftest() -> None:
    cfg = {"governance": {"max_order_pct": 0.15, "max_drawdown": 0.25,
                          "min_dollar_volume_20d": 5e7}}
    mcfg = {"drawdown": {"max_pct": 0.20}, "concentration": {"max_position_pct": 0.15}}
    out = render(cfg, mcfg, ["mcp__agentic-trader__quote"])
    # every number is INTERPOLATED, never a literal in the template
    assert "0.20" in out or "20%" in out
    assert "0.15" in out or "15%" in out
    # no placeholder survives
    for ph in ("__MANDATE__", "__GATE__", "__TOOLS__", "__TERMS__"):
        assert ph not in out, ph
    # the tool index is COMPLETE
    assert "quote" in out
    # the venue split is stated before anything else about tools
    assert out.index("moomoo is DATA") < out.index("quote")
    print("charter: OK")
```

- [ ] **Step 2–4:** implement, run, verify.

- [ ] **Step 5: Add a repo check that the template holds no bare numbers**

A literal threshold in `prompts/charter.md` is a defect (spec §2). Add `check_charter_has_no_literals(root)` asserting no line in that file matches a percentage or a bare decimal outside a placeholder. Follow the Task 1 check contract and the publishing rule.

---

## Task 7: The session runner

**Files:**
- Create: `scripts/session.py`

**Interfaces:**
- Consumes: `src.session_lock.acquire/release`, `src.integrity.snapshot/verify`, `src.charter.render`
- Produces: `run(mode: str, dry_run: bool = False) -> dict` with keys `ok`, `mode`, `error`

**Ordering is load-bearing.** Implement in exactly this sequence:
`install_signal_handlers()` → `acquire(lock)` → **build brief (facts gathered HERE, after the lock)** → `t0` → `integrity.snapshot()` → spawn loop → `finally:` kill group, `integrity.verify()`, release lock.

Facts after the lock, never before: a session that waited for the lock and then reasoned on pre-wait prices was a real incident on the reference system — a 09:35 session holding a 09:30 quote through the fastest five minutes of the day.

- [ ] **Step 1: Write failing tests** for `classify()` specifically:

```python
def _selftest() -> None:
    # An error must DOMINATE stdout. `ok = bool(out)` recorded a dead session as
    # successful: a 529 banner went to stdout, so the run logged "ok" and exited
    # 0, systemd said "Finished successfully", and NOTHING registered a failure.
    ok, err = classify(0, "API Error: 529 Overloaded. Retry later.", None)
    assert ok is False and "529" in err
    ok, err = classify(0, "x" * 700, None)
    assert ok is True and err is None
    ok, err = classify(1, "boom", None)
    assert ok is False
    ok, err = classify(0, "", None)
    assert ok is False and "no output" in err
    # never retry past real work: the session may already have PLACED ORDERS
    assert should_retry("API Error: 529") is True
    assert should_retry("x" * 700) is False
    print("session: OK")
```

- [ ] **Steps 2–4:** implement `classify`, `should_retry`, `_kill_group` (TERM 5s → KILL 2s, `start_new_session=True` at spawn so `os.killpg` reaches the whole tree), run, verify.

- [ ] **Step 5: Dry run**

Run: `.venv/bin/python scripts/session.py close --dry-run`
Expected: prints `brief_bytes` and the first 400 chars; **takes no lock and spawns nothing.**

---

## Task 8: Cutover

- [ ] **Step 1: Shadow.** `touch research_store/SHADOW`. Add the session cron entries alongside — not replacing — the existing loops.

⚠️ The live crontab ends with `CRON_TZ=America/New_York` from a sibling repo. **Appending naively puts new entries under that variable.** Insert new lines BEFORE it, and verify with `crontab -l` afterwards.

- [ ] **Step 2: Compare.** For each shadow session, record what the agent decided against what `fast_loop` actually did. Run at least 5 sessions. The question to answer: does the agent use the deviation allowance, or does it reproduce `slow_loop`'s book?

- [ ] **Step 3: Enable the close session.** `rm research_store/SHADOW`, but keep the hook denying `premarket`/`open` by mode. Close first — least time-critical, no open-auction volatility.

- [ ] **Step 4: Enable premarket, then open.** One per week minimum.

- [ ] **Step 5: Retire `prompts/fast_loop.md`** and its cron entry only once no session depends on it. Rollback at every phase is `touch research_store/SHADOW` plus re-enabling the `fast_loop` cron line.

---

## Task 9: Announce-first (spec §4)

Full autonomy inside the gate for ordinary trades; three classes get pushed to
Aaron's phone BEFORE acting, so he keeps veto on the unusual without gating the
routine.

**Files:**
- Create: `src/announce.py`
- Modify: `scripts/hooks/pretooluse_order_gate.py` (call it before allowing)

**Interfaces:**
- Produces: `needs_announcement(order: dict, valued: dict, universe: set, mandate_cfg: dict) -> str | None`

- [ ] **Step 1: Write the failing test**

```python
def _selftest() -> None:
    MCFG = {"concentration": {"max_position_pct": 0.15}}
    V = {"account_value": 100.0}
    UNI = {"MU", "NVDA"}

    # ordinary in-universe buy -> silent
    assert needs_announcement({"symbol": "MU", "side": "buy", "amount": 5.0},
                              V, UNI, MCFG) is None

    # off-universe entry -> announce
    r = needs_announcement({"symbol": "ZZZZ", "side": "buy", "amount": 5.0},
                           V, UNI, MCFG)
    assert r and "outside" in r.lower()

    # size at 80% of the BLOCKING limit -> announce BEFORE the gate refuses.
    # 0.15 * 0.8 = 0.12 -> $12 on $100. One concentration number in the system.
    r = needs_announcement({"symbol": "MU", "side": "buy", "amount": 12.5},
                           V, UNI, MCFG)
    assert r and "concentration" in r.lower()
    assert needs_announcement({"symbol": "MU", "side": "buy", "amount": 11.0},
                              V, UNI, MCFG) is None

    # a SELL is never announced -- exits must never be slowed down
    assert needs_announcement({"symbol": "ZZZZ", "side": "sell", "amount": 90.0},
                              V, UNI, MCFG) is None
    print("announce: OK")
```

- [ ] **Step 2: Run to verify it fails.** Expected `NameError: needs_announcement`.

- [ ] **Step 3: Implement.** The threshold is `mandate_cfg["concentration"]["max_position_pct"] * ANNOUNCE_FRACTION` where `ANNOUNCE_FRACTION = 0.80` — rendered from the mandate, never a second literal (spec §2, §10). Wholesale strategy abandonment is not detectable from a single order and is announced by the agent itself via `record_decision`; document that in the module docstring rather than pretending the code detects it.

- [ ] **Step 4: Run to verify it passes.** Expected `announce: OK`.

- [ ] **Step 5: Wire into the hook.** In `decide()`, when the order is otherwise allowed and `needs_announcement` returns a reason, `notify.push(...)` with that reason and still ALLOW. Announce-first means Aaron sees it as it happens, not that the order waits for a reply — a blocking approval would reintroduce the review-then-place pattern the inversion removes, and there is nobody to answer at 09:30.

- [ ] **Step 6: Commit.**

---

## Deferred (recorded, not dropped)

- **Wake polling.** `wakes.due()` is selftested but has no caller; wakes register and never fire. Needs a poller in `market_monitor` or the session runner.
- **Panel-level split adjustment.** `0e5c67b` stopped a phantom split SALE; the panel still records the split as a real return.
- **#30 liquidity source.** Swap to moomoo `turnover` (already wired). Alpaca IEX undercounts by 7–21× and the ratio varies 3× between names.
- **Settled-cash in the snapshot.** `prompts/fast_loop.md` step 4 now records `buying_power`/`unsettled_funds`; the session runner must do the same or `account()` reports UNKNOWN.
