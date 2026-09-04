"""The model fallback chain — walked only on a PROVABLY clean failure.

Spec: docs/superpowers/specs/2026-09-03-model-fallback-and-exit-bookkeeping-design.md §2.

THE ONE RULE. A spawn falls through to the next model only when it failed AND
its stream-json transcript contains zero tool_use blocks. A 400 (unknown
model, CLI too old), a 5xx/529, a transport error before any work, an empty
transcript — all clean. A timeout or crash AFTER any tool call is AMBIGUOUS:
the first attempt may already have placed an order, so the chain stops and
the caller's existing failure path runs (page, retryable:false, symbol
paused). This is the 2026-09-03 rule ("do not retry a session after an
ambiguous failure because it may already have placed orders") made
mechanical, and it is stricter than the runner's signature list.

The ONE exception to "next model": the CLI itself was unavailable ("claude
native binary not installed", its self-update window, observed 2026-09-04
08:07). That is a clean failure on EVERY step because every step is the same
binary, so the SAME model is retried once after `cli_retry_after_s`.

Pure over injected functions: `spawn(model) -> (rc, out, err)` and
`judge(rc, out, err) -> (ok, error)` come from the caller; the clock and
sleep are parameters so the selftest runs in zero seconds. Journal and
phone are the CALLER's (on_fallback callback) — this module touches no file.

RUNS UNDER /usr/bin/python3 (3.10): the monitor imports this.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

#: Failure classes, matched against the caller's error text AND the CLI's own
#: final `result` line (result_text). Order matters: first hit wins.
REASON_CLASSES = (
    ("cli_unavailable", ("native binary not installed",)),
    ("usage_limit",     ("hit your limit", "usage limit")),
    ("version_too_old", ("version_too_old", "does not support this model")),
    ("unknown_model",   ("issue with the selected model", "model not found",
                         "invalid model", "not a valid model")),
    ("model_outage",    ("api error: 5", "overloaded", "rate limit",
                         "connection error", "network error")),
    ("auth",            ("authentication_error", "invalid api key",
                         "credit balance is too low")),
    ("timeout",         ("timed out after",)),
    ("interrupted",     ("interrupted (",)),
    ("not_launched",    ("refusing to run", "refusing to start", "halt active")),
)
AMBIGUOUS = "ambiguous"


@dataclass
class Attempt:
    model: str
    rc: int
    ok: bool
    error: str | None
    out: str          # the stream-json transcript text
    err: str          # stderr
    tool_calls: int = 0
    orders: int = 0
    reason_class: str | None = None


def result_text(out: str) -> str:
    """The CLI's own final `result` text from a stream-json transcript, or "".

    Scanned from the END, and ONLY the `type: result` record — never agent
    prose, which is exactly where matching "rate limit" anywhere went wrong
    before (scripts/session.py _DEAD_SIGNATURES).
    """
    for raw in reversed((out or "").splitlines()):
        try:
            m = json.loads(raw)
        except Exception:               # noqa: BLE001
            continue
        if isinstance(m, dict) and m.get("type") == "result":
            return str(m.get("result") or "")
    return ""


def counts(out: str) -> tuple:
    """(tool calls, order placements) in a stream-json transcript. Pure."""
    calls = orders = 0
    for raw in (out or "").splitlines():
        try:
            m = json.loads(raw)
        except Exception:               # noqa: BLE001
            continue
        if not isinstance(m, dict) or m.get("type") != "assistant":
            continue
        content = (m.get("message") or {}).get("content") or []
        for c in content if isinstance(content, list) else []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                calls += 1
                if str(c.get("name") or "").endswith("place_equity_order"):
                    orders += 1
    return calls, orders


def reason_class(error: str | None, text: str = "") -> str | None:
    """Name the failure class for the record; None when the run was ok."""
    if not error:
        return None
    low = f"{error}\n{text or ''}".lower()
    for name, sigs in REASON_CLASSES:
        if any(s in low for s in sigs):
            return name
    return AMBIGUOUS


DRILL_PROMPT = "Reply with exactly: OK"


def drill_argv(model: str, empty_mcp_config: str) -> list:
    """A spawn that can reach NO tool and NO broker: empty MCP surface, no
    built-ins, dontAsk. What a drill proves is the chain, not the job. Shared
    by the session runner and the monitor's executor drill so the two prove
    the same thing."""
    return ["claude", "-p", "--output-format", "stream-json", "--verbose",
            "--model", model, "--setting-sources", "",
            "--strict-mcp-config", "--mcp-config", str(empty_mcp_config),
            "--tools", "", "--permission-mode", "dontAsk"]


def clean_failure(a: Attempt) -> bool:
    """Failed AND provably did no work: zero tool calls in the transcript."""
    return (not a.ok) and a.tool_calls == 0


def next_model(chain: list, tried: list, budget_left_s: float, per_attempt_s: float):
    """The next untried model that fits the remaining budget, or None."""
    for m in chain:
        if m in tried:
            continue
        if per_attempt_s > budget_left_s:
            return None
        return m
    return None


def run_chain(chain: list, spawn, judge, *, per_attempt_s: float, budget_s: float,
              cli_retry_after_s: float = 30, on_fallback=None,
              clock=time.monotonic, sleep=time.sleep) -> dict:
    """Walk `chain` under THE ONE RULE. -> {"attempts", "ok", "stopped"}.

    `stopped` says why the walk ended: "ok" (a spawn succeeded), "ambiguous"
    (a failed spawn had made tool calls — never retried), "exhausted" (every
    model tried cleanly and failed), "budget" (a model remains but does not
    fit the window), "empty" (no chain). `on_fallback(from_model, to_model,
    reason, attempt_no)` is called BEFORE each fallback spawn; with
    `to_model=None` once when the walk ends without success, so the caller
    can journal and page exactly once per transition.
    """
    attempts: list = []
    if not chain:
        return {"attempts": attempts, "ok": False, "stopped": "empty"}
    started = clock()
    model = chain[0]
    cli_retried = False
    while True:
        rc, out, err = spawn(model)
        ok, error = judge(rc, out, err)
        calls, orders = counts(out)
        a = Attempt(model, int(rc), bool(ok), error, out or "", err or "",
                    calls, orders, reason_class(error, result_text(out)))
        attempts.append(a)
        if a.ok:
            return {"attempts": attempts, "ok": True, "stopped": "ok"}
        if not clean_failure(a):
            if on_fallback:
                on_fallback(model, None, a.reason_class, len(attempts))
            return {"attempts": attempts, "ok": False, "stopped": "ambiguous"}
        if a.reason_class == "cli_unavailable" and not cli_retried:
            cli_retried = True
            if on_fallback:
                on_fallback(model, model, a.reason_class, len(attempts))
            sleep(cli_retry_after_s)
            continue
        tried = [x.model for x in attempts]
        budget_left = budget_s - (clock() - started)
        nxt = next_model(chain, tried, budget_left, per_attempt_s)
        if nxt is None:
            if on_fallback:
                on_fallback(model, None, a.reason_class, len(attempts))
            remaining = [m for m in chain if m not in tried]
            return {"attempts": attempts, "ok": False,
                    "stopped": "budget" if remaining else "exhausted"}
        if on_fallback:
            on_fallback(model, nxt, a.reason_class, len(attempts))
        model = nxt


def _selftest() -> None:
    def stream(*blocks, result=None, is_error=False):
        lines = [json.dumps({"type": "system", "subtype": "init"})]
        if blocks:
            lines.append(json.dumps({"type": "assistant", "message": {"content": list(blocks)}}))
        if result is not None:
            lines.append(json.dumps({"type": "result", "is_error": is_error, "result": result}))
        return "\n".join(lines)

    tool = {"type": "tool_use", "name": "mcp__agentic-trader__brief"}
    order = {"type": "tool_use", "name": "mcp__robinhood-trading__place_equity_order"}
    prose = {"type": "text", "text": "no rate limit hit, placed the BUY"}
    assert result_text(stream(result="You've hit your limit · resets 4:40pm")).startswith("You've hit")
    assert result_text("") == "" and result_text("not json\n{\"type\":\"x\"}") == ""
    assert counts(stream(prose, tool, order)) == (2, 1)
    assert counts("") == (0, 0)
    assert reason_class(None) is None
    assert reason_class("exit 1: {}", "You've hit your limit") == "usage_limit"
    assert reason_class("session died: api error: 529 overloaded") == "model_outage"
    assert reason_class("exit 1: x", "API Error: 400 claude_code_version_too_old") == "version_too_old"
    assert reason_class("exit 1: Error: claude native binary not installed") == "cli_unavailable"
    assert reason_class("exit 1: x", "There's an issue with the selected model (claude-nope)") == "unknown_model"
    assert reason_class("exit -1: timed out after 900s") == "timeout"
    assert reason_class("refusing to run a second session") == "not_launched"
    assert reason_class("something new") == AMBIGUOUS

    # ---- run_chain under a fake clock ----
    clock = {"t": 0.0}
    log: list = []

    def fake_clock():
        return clock["t"]

    def fake_sleep(s):
        clock["t"] += s

    def judge(rc, out, err):
        return (rc == 0, None if rc == 0 else f"exit {rc}: {err}")

    def spawner(script):
        """script: {model: (rc, out, err, secs)}; every spawn advances the clock."""
        def spawn(model):
            rc, out, err, secs = script[model]
            clock["t"] += secs
            return rc, out, err
        return spawn

    def on_fb(frm, to, reason, n):
        log.append((frm, to, reason, n))

    chain = ["m-a", "m-b", "m-c"]
    # 1. primary fails cleanly (400 before any tool), second answers -> ok after one fallback
    log.clear(); clock["t"] = 0
    r = run_chain(chain, spawner({"m-a": (1, stream(result="API Error: 400 claude_code_version_too_old", is_error=True), "", 1),
                                  "m-b": (0, stream(tool, result="done"), "", 10)}),
                  judge, per_attempt_s=100, budget_s=150, on_fallback=on_fb, clock=fake_clock, sleep=fake_sleep)
    assert r["ok"] and r["stopped"] == "ok" and [a.model for a in r["attempts"]] == ["m-a", "m-b"], r
    assert log == [("m-a", "m-b", "version_too_old", 1)], log
    # 2. AMBIGUOUS: the primary made a tool call then died -> no fallback, ever
    log.clear(); clock["t"] = 0
    r = run_chain(chain, spawner({"m-a": (1, stream(tool, result="You've hit your limit", is_error=True), "", 86)}),
                  judge, per_attempt_s=100, budget_s=150, on_fallback=on_fb, clock=fake_clock, sleep=fake_sleep)
    assert not r["ok"] and r["stopped"] == "ambiguous" and len(r["attempts"]) == 1, r
    assert r["attempts"][0].reason_class == "usage_limit" and r["attempts"][0].tool_calls == 1
    assert log == [("m-a", None, "usage_limit", 1)], log
    # 3. exhausted: every model fails cleanly -> three attempts, three callbacks, last to=None
    log.clear(); clock["t"] = 0
    dead = (1, stream(result="API Error: 529 overloaded", is_error=True), "", 1)
    r = run_chain(chain, spawner({"m-a": dead, "m-b": dead, "m-c": dead}),
                  judge, per_attempt_s=100, budget_s=1000, on_fallback=on_fb, clock=fake_clock, sleep=fake_sleep)
    assert r["stopped"] == "exhausted" and [a.model for a in r["attempts"]] == chain, r
    assert [x[1] for x in log] == ["m-b", "m-c", None] and log[-1][0] == "m-c", log
    # 4. budget: the second model does not fit the window -> stopped "budget", never spawned
    log.clear(); clock["t"] = 0
    r = run_chain(chain, spawner({"m-a": (1, stream(result="API Error: 529", is_error=True), "", 60)}),
                  judge, per_attempt_s=100, budget_s=150, on_fallback=on_fb, clock=fake_clock, sleep=fake_sleep)
    assert r["stopped"] == "budget" and len(r["attempts"]) == 1, r
    assert log == [("m-a", None, "model_outage", 1)], log
    # 5. CLI unavailable: SAME model retried once after the wait, then the chain
    calls = {"n": 0}

    def flaky(model):
        calls["n"] += 1
        if model == "m-a" and calls["n"] == 1:
            return 1, "", "Error: claude native binary not installed"
        if model == "m-a":
            return 0, stream(tool, result="ok"), ""
        raise AssertionError("must not reach the next model")
    log.clear(); clock["t"] = 0
    r = run_chain(chain, flaky, judge, per_attempt_s=100, budget_s=150, cli_retry_after_s=30,
                  on_fallback=on_fb, clock=fake_clock, sleep=fake_sleep)
    assert r["ok"] and [a.model for a in r["attempts"]] == ["m-a", "m-a"], r
    assert log == [("m-a", "m-a", "cli_unavailable", 1)] and clock["t"] == 30, (log, clock)
    # 6. empty chain
    assert run_chain([], flaky, judge, per_attempt_s=1, budget_s=1)["stopped"] == "empty"
    # 7. the first spawn succeeding calls no callback at all
    log.clear()
    r = run_chain(chain, spawner({"m-a": (0, stream(tool, result="ok"), "", 5)}), judge,
                  per_attempt_s=100, budget_s=150, on_fallback=on_fb, clock=fake_clock, sleep=fake_sleep)
    assert r["ok"] and log == [], log
    print("fallback: OK -- clean failure walks the chain, an ambiguous one never does, "
          "exhaustion and budget stop it, the CLI window retries the same model once, "
          "reason classes derive from the CLI's own result line")


if __name__ == "__main__":
    import sys  # noqa: PLC0415
    if len(sys.argv) > 1 and sys.argv[1] == "--" + "selftest":
        _selftest()
