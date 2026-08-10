"""DEPLOYED-CODE DRIFT — is each long-running service actually running the code
on disk, or the code that was on disk when it started?

The failure class this exists for
---------------------------------
A long-running process holds whatever it loaded at start. `git pull` and an edit
both change the filesystem; neither touches a running process. So "deployed to
disk" and "deployed to the process" are different states, and nothing on this box
compared them.

This is NOT hypothetical. On 2026-08-10 `agentic-monitor.service` -- the intraday
stop watcher, which IS the stop, because Robinhood has no native stop for
fractional shares -- had been running since Sunday 16:22:55 while its own source
had been rewritten that morning at 06:14. The live process was still enforcing a
180s exit-executor timeout after that was raised to 300s precisely because the
executor had been measured spending 180s on post-sale reconciliation. A stop
firing that day could have killed its own executor mid-sale.

It surfaced only by luck: that commit happened to add a NEW artifact
(unprotected.json), so src/health.py noticed the artifact was missing. A
BEHAVIOURAL fix with no new artifact -- a corrected stop calculation, a changed
threshold -- would have run stale indefinitely with every check reporting green.

Why the scope is discovered, not listed
---------------------------------------
The cheap version compares one hardcoded file's mtime against the service start
time. That catches the case above and then rots: the first time a service gains
an import, the check silently under-covers while still reporting green -- the
same documented-but-unbound defect, one layer out. A check that under-reports is
worse than no check, because it is trusted.

So the file set is derived by walking the entry script's ACTUAL import graph
(AST, transitive, repo-local only). Add an import tomorrow and it is covered with
no edit here.

Scope limits, stated rather than hidden:
  - Repo-local Python only. A `pip install` inside .venv, an edited systemd unit,
    or a changed config file is real drift this does NOT see. Config is read at
    runtime by most of this repo, so it matters far less than code.
  - Dynamic imports (importlib, __import__ with a computed name) are invisible to
    a static walk. None exist in the watched services today; a selftest pins the
    closure against reality so a regression is caught rather than assumed.
  - mtime, not content hash. A `touch` with no edit therefore reads as drift.
    That is the correct direction to err for a safety service: a false "restart
    me" costs a restart, a false "all clear" costs a stale stop watcher.
"""
from __future__ import annotations

import ast
import datetime as dt
import pathlib

# Services whose code must match disk. label is what a human sees in an alert.
WATCHED = (
    {"unit": "agentic-monitor.service",
     "label": "Monitor running stale code",
     "entry": "scripts/market_monitor.py"},
    {"unit": "agentic-dashboard.service",
     "label": "Dashboard running stale code",
     "entry": "dashboard/app.py"},
)

# Where a bare `import governance` resolves from. These mirror the sys.path
# inserts the entry scripts perform at import time.
SEARCH_ROOTS = ("src", "")


def _candidates(module: str, root: pathlib.Path) -> list[pathlib.Path]:
    rel = module.replace(".", "/")
    out = []
    for base in SEARCH_ROOTS:
        stem = (root / base / rel) if base else (root / rel)
        out.append(stem.with_suffix(".py"))
        out.append(stem / "__init__.py")
    return out


def _resolve(module: str, root: pathlib.Path) -> pathlib.Path | None:
    """Map a dotted module name to a repo file, or None if it is not ours."""
    if not module:
        return None
    for cand in _candidates(module, root):
        if cand.is_file():
            return cand.resolve()
    return None


def import_closure(entry: pathlib.Path, root: pathlib.Path) -> set:
    """Every repo-local .py the entry script transitively imports, plus itself.

    Static AST walk. Unparseable files are skipped rather than raising -- a syntax
    error in an unrelated module must not blind the whole check -- but the entry
    itself failing to parse returns just the entry, which still catches the
    common case.
    """
    entry = entry.resolve()
    root = root.resolve()
    seen: set = set()
    queue = [entry]
    while queue:
        path = queue.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:          # relative import -- resolve against pkg
                    pkg = path.parent
                    for _ in range(node.level - 1):
                        pkg = pkg.parent
                    base = pkg / (node.module.replace(".", "/") if node.module else "")
                    for cand in (base.with_suffix(".py"), base / "__init__.py"):
                        if cand.is_file():
                            queue.append(cand.resolve())
                    for a in node.names:
                        sub = base / a.name
                        for cand in (sub.with_suffix(".py"), sub / "__init__.py"):
                            if cand.is_file():
                                queue.append(cand.resolve())
                    continue
                if node.module:
                    mods.append(node.module)
                    # `from pkg import mod` -- mod may itself be a module
                    mods += [f"{node.module}.{a.name}" for a in node.names]
            for m in mods:
                hit = _resolve(m, root)
                if hit is not None:
                    queue.append(hit)
    return seen


def evaluate(now: dt.datetime, services: list) -> list:
    """Pure. services -> [{key,label,last_seen,status,detail}] for health.Check.

    Each service dict: unit, label, active(bool), started(datetime|None),
    sources[(relpath, mtime datetime)], error(str|None).
    """
    out = []
    for svc in services:
        key = "deployed_" + svc["unit"].replace(".service", "").replace("-", "_")
        label = svc["label"]
        err = svc.get("error")
        if err:
            out.append({"key": key, "label": label, "last_seen": None,
                        "status": "unknown", "detail": err})
            continue
        if not svc.get("active"):
            # Deliberately "unknown", not an alert: a stopped service is caught
            # by its own artifact-freshness check, and you cannot judge the code
            # of a process that is not running. Double-alerting one fault trains
            # people to ignore both.
            out.append({"key": key, "label": label, "last_seen": None,
                        "status": "unknown",
                        "detail": "service is not running — code freshness cannot "
                                  "be judged (liveness is a separate check)"})
            continue
        started = svc.get("started")
        if started is None:
            out.append({"key": key, "label": label, "last_seen": None,
                        "status": "unknown",
                        "detail": "service start time unavailable"})
            continue
        sources = svc.get("sources") or []
        if not sources:
            # An empty closure means the walk failed, not that nothing is
            # imported. Never report that as healthy.
            out.append({"key": key, "label": label, "last_seen": started,
                        "status": "unknown",
                        "detail": "no source files resolved — the import walk "
                                  "failed, so drift cannot be ruled out"})
            continue
        newer = [(p, m) for p, m in sources if m > started]
        if not newer:
            out.append({"key": key, "label": label, "last_seen": started,
                        "status": "ok",
                        "detail": f"running code matches disk ({len(sources)} files)"})
            continue
        newer.sort(key=lambda pm: pm[1], reverse=True)
        newest_path, newest_m = newer[0]
        age_h = (newest_m - started).total_seconds() / 3600.0
        out.append({
            "key": key, "label": label, "last_seen": started, "status": "stale",
            "detail": (f"{len(newer)} source file(s) changed since the process "
                       f"started; newest is {newest_path} ({age_h:.1f}h newer). "
                       f"The running process does NOT have these changes — "
                       f"restart {svc['unit']}"),
        })
    return out


def systemd_start_times(units, run=None) -> dict:
    """unit -> (active, started_utc). Reads /proc/<pid>, NOT the systemd string.

    ⚠️ `systemctl show -p ExecMainStartTimestamp` renders in the box's LOCAL zone
    ("Mon 2026-08-10 08:54:13 EDT"). Parsing that with %Z silently yields a naive
    or wrong-offset datetime -- on this box it landed the start 4 hours early and
    reported a service restarted five minutes ago as running stale code. A check
    that cries wolf gets ignored, which is the same as having no check.

    The ctime of /proc/<pid> is the process start as real epoch seconds, with no
    zone to misread. That is what this uses.
    """
    import subprocess                      # noqa: PLC0415
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True).stdout)
    out = {}
    for unit in units:
        pid_s = (run(["systemctl", "show", unit, "-p", "ExecMainPID", "--value"]) or "").strip()
        active = (run(["systemctl", "is-active", unit]) or "").strip() == "active"
        started = None
        try:
            pid = int(pid_s)
            if pid > 0:
                st = pathlib.Path(f"/proc/{pid}").stat()
                started = dt.datetime.fromtimestamp(st.st_ctime, dt.timezone.utc)
        except (ValueError, OSError):
            started = None
        out[unit] = (active, started)
    return out


def gather(root: pathlib.Path, start_times: dict, watched=WATCHED) -> list:
    """Thin I/O: pair each unit's start time with its import closure's mtimes.

    `start_times` maps unit -> (active: bool, started: datetime|None) and is
    passed in so this stays testable without systemd.
    """
    utc = dt.timezone.utc
    out = []
    for w in watched:
        active, started = start_times.get(w["unit"], (False, None))
        rec = {"unit": w["unit"], "label": w["label"],
               "active": active, "started": started, "sources": []}
        entry = root / w["entry"]
        if not entry.is_file():
            rec["error"] = f"entry script missing: {w['entry']}"
            out.append(rec)
            continue
        try:
            for p in import_closure(entry, root):
                try:
                    mt = dt.datetime.fromtimestamp(p.stat().st_mtime, utc)
                except OSError:
                    continue
                try:
                    rel = str(p.relative_to(root.resolve()))
                except ValueError:
                    rel = str(p)
                rec["sources"].append((rel, mt))
        except Exception as e:              # noqa: BLE001
            rec["error"] = f"import walk failed: {type(e).__name__}: {e}"
        out.append(rec)
    return out


# ---------------------------------------------------------------- selftest

def _selftest() -> None:
    utc = dt.timezone.utc
    now = dt.datetime(2026, 8, 10, 12, 0, tzinfo=utc)
    t0 = dt.datetime(2026, 8, 10, 8, 0, tzinfo=utc)

    def svc(**kw):
        base = {"unit": "x.service", "label": "X", "active": True,
                "started": t0, "sources": [("a.py", t0 - dt.timedelta(hours=1))]}
        base.update(kw)
        return base

    # in sync -> ok
    r = evaluate(now, [svc()])[0]
    assert r["status"] == "ok", r

    # a file NEWER than the process -> stale, and it names the file
    r = evaluate(now, [svc(sources=[("a.py", t0 - dt.timedelta(hours=1)),
                                    ("scripts/m.py", t0 + dt.timedelta(hours=2))])])[0]
    assert r["status"] == "stale", r
    assert "scripts/m.py" in r["detail"] and "restart" in r["detail"], r
    assert "2.0h newer" in r["detail"], r

    # exactly equal mtime is NOT stale (no false alarm on a same-instant restart)
    r = evaluate(now, [svc(sources=[("a.py", t0)])])[0]
    assert r["status"] == "ok", r

    # an empty closure must never read healthy -- it means the walk failed
    r = evaluate(now, [svc(sources=[])])[0]
    assert r["status"] == "unknown" and "import walk failed" in r["detail"], r

    # a stopped service is "unknown", not an alert (liveness is another check)
    r = evaluate(now, [svc(active=False)])[0]
    assert r["status"] == "unknown" and "not running" in r["detail"], r

    # missing start time cannot be judged
    r = evaluate(now, [svc(started=None)])[0]
    assert r["status"] == "unknown", r

    # errors propagate as unknown, never as ok
    r = evaluate(now, [svc(error="entry script missing: nope.py")])[0]
    assert r["status"] == "unknown" and "nope.py" in r["detail"], r

    # key derives from the unit name
    assert evaluate(now, [svc(unit="agentic-monitor.service")])[0]["key"] \
        == "deployed_agentic_monitor"

    # --- the closure is bound to REALITY, not to a hardcoded list -----------
    root = pathlib.Path(__file__).resolve().parents[1]
    entry = root / "scripts" / "market_monitor.py"
    if entry.is_file():
        names = {p.name for p in import_closure(entry, root)}
        # market_monitor imports these directly; if the walk stops finding them
        # the check has silently gone blind and this must fail loudly.
        for required in ("market_monitor.py", "governance.py", "strategy.py",
                         "notify.py"):
            assert required in names, f"closure lost {required}: {sorted(names)}"
        # ...and transitively, through `from research_store import ...`
        assert len(names) > 6, f"closure suspiciously small: {sorted(names)}"

    # gather tolerates a missing entry script without raising
    recs = gather(root, {}, watched=({"unit": "z.service", "label": "Z",
                                      "entry": "does/not/exist.py"},))
    assert recs[0]["error"].startswith("entry script missing"), recs

    print("deployed: OK — drift detection, discovered scope, fail-loud on blind walk")


if __name__ == "__main__":
    _selftest()
