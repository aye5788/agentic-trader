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
