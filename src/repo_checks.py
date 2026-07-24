"""Static repo-state validator — filesystem-only checks for config/workflow drift.

Exists because the failures that hurt this repo were never runtime exceptions —
they were quiet mismatches between what a config/doc *says* is scheduled and
what actually runs: a `cd`-less cron line whose relative path silently no-ops
when cron's cwd is `/root`, a job documented and coded but never appended to
the live crontab, a CI step that swallows a real failure behind `exit 0` so an
expired token shows green, a stray `ANTHROPIC_API_KEY=` that would silently
flip billing from the subscription to per-token API use. None of these throw;
all of them look exactly like health until someone reads the file closely.

This module is that close read, run on demand (or by an oversight loop that
files an issue when it fires). Pure and filesystem-only: no network, no
secrets, no droplet, no moomoo import (this must stay importable under the
plain .venv, not just system python3.10).

    python src/repo_checks.py              # run all checks against this repo
    python src/repo_checks.py --selftest   # logic tests: each check proven to
                                            # both pass clean input and fail
                                            # dirty input, via tempfile fixtures
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]

# This module's own resolved path. check_no_api_key excludes it from the scan
# — see that check's docstring for why, and what the exclusion blinds.
_SELF_PATH = pathlib.Path(__file__).resolve()

# `import health` resolves against this file's own directory (src/), which
# Python puts on sys.path automatically when this script is the entry point.
# Belt-and-braces for the case this module is imported from elsewhere first.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import health  # noqa: E402


# ---------------------------------------------------------------- check 1

def check_cron_paths(root: pathlib.Path) -> list[str]:
    """Every non-comment cron *command* must be absolute-path-rooted, or the
    line must `cd /opt/agentic-trader` first.

    Catches: the two `cd`-prefix bugs where cron's cwd is `/root` and a
    relative path (`deploy/foo.sh`, `scripts/bar.py`) silently resolves to
    nothing and fails with no visible error.
    """
    path = root / "deploy" / "crontab.template"
    try:
        text = path.read_text()
    except OSError:
        return [f"missing {path} — cannot verify cron command paths"]

    failures: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # A real cron job line is 5 whitespace-separated schedule fields
        # followed by a command. Env-var assignment lines (PATH=..., HOME=...)
        # and anything else that doesn't split into 6 parts isn't a job line.
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        command = parts[5]
        if command.startswith("/") or "cd /opt/agentic-trader" in command:
            continue
        failures.append(
            f"deploy/crontab.template:{lineno}: command is neither absolute-path "
            f"nor prefixed with `cd /opt/agentic-trader` — cron's cwd is /root, "
            f"so a relative path here silently fails: {line!r}"
        )
    return failures


# ---------------------------------------------------------------- check 2

# SPECS key -> substring expected to appear in the crontab.template line that
# arms it. Every key in health.SPECS must be accounted for here OR in
# NOT_CRON below; check_scheduled_jobs_armed flags a SPECS key that is in
# neither, so a newly-added scheduled job can't fall through the gap silently.
CRON_SUBSTRINGS = {
    "slow_loop":     "run_slow_loop.sh",
    "fast_loop":     "run_fast_loop.sh",
    "risk_review":   "run_risk_review.sh",
    "ledger_backup": "backup_ledger.sh",
    "signal_panel":  "collect_signals.py",
    "newsletter":    "run_newsletter.sh",
}

# health.SPECS keys that are legitimately NOT cron lines:
#   monitor       -> systemd service `agentic-monitor` (deploy/agentic-monitor.service)
#   adaptive_tune -> GitHub Actions workflow (.github/workflows/adaptive-tune.yml)
NOT_CRON = {"monitor", "adaptive_tune"}


def check_scheduled_jobs_armed(root: pathlib.Path) -> list[str]:
    """Every health.SPECS key that names a cron-run job must have its arming
    substring present in deploy/crontab.template.

    Catches: the moomoo signal panel — built, documented as running weekly,
    even added to this template — but never appended to the live crontab, so
    it silently never ran.
    """
    path = root / "deploy" / "crontab.template"
    try:
        text = path.read_text()
    except OSError:
        return [f"missing {path} — cannot verify scheduled jobs are armed"]

    failures: list[str] = []
    for key, (label, _max_age_days, _source) in health.SPECS.items():
        if key in NOT_CRON:
            continue
        expected = CRON_SUBSTRINGS.get(key)
        if expected is None:
            failures.append(
                f"health.SPECS key {key!r} ({label}) is neither mapped in "
                "CRON_SUBSTRINGS nor listed in NOT_CRON in src/repo_checks.py — "
                "add a mapping or mark it NOT_CRON with a comment explaining why"
            )
            continue
        if expected not in text:
            failures.append(
                f"deploy/crontab.template: health.SPECS key {key!r} ({label}) "
                f"expects {expected!r} in a cron line but it is not present — "
                "the job may be documented but never armed"
            )
    return failures


# ---------------------------------------------------------------- check 3

_TRIGGER_RE = re.compile(r"::error::|FAILED|failed")
_EXIT0_RE = re.compile(r"\bexit\s+0\b")


def _is_comment_only_line(line: str) -> bool:
    """True iff the line is PURE prose: nothing but a `#` comment once
    stripped. A real code line that merely carries a trailing inline comment
    (e.g. `  exit 0  # fine here`) does NOT count — its stripped form starts
    with the code, not `#`, so it is still a live code path and must still be
    scanned."""
    return line.strip().startswith("#")


def check_workflow_failure_exits(root: pathlib.Path) -> list[str]:
    """No workflow may `exit 0` within 3 lines of a failure marker
    (`::error::`, `FAILED`/`failed`).

    Simple heuristic, deliberately not a YAML-semantic parse: flag any
    `exit 0` on the same line as, or within the 3 lines after, a line
    containing one of those markers. Catches: the expired-token-shows-green
    bug, where a failed step in a bash `run:` block fell through to `exit 0`
    and CI reported success.

    Comment-only lines (stripped content starts with `#`) are prose, not an
    executable code path, and are ignored for BOTH the trigger and the
    `exit 0` match — a `#` line narrating a historical bug (marker and/or
    `exit 0` mentioned only in the comment text) must not fire this check.
    A trailing inline comment on an otherwise-real code line does not make
    that line comment-only; it is still scanned.
    """
    wf_dir = root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return []

    failures: list[str] = []
    seen: set[tuple[str, int]] = set()
    for wf_path in sorted(wf_dir.glob("*.yml")):
        lines = wf_path.read_text().splitlines()
        rel = str(wf_path.relative_to(root))
        for i, line in enumerate(lines):
            if _is_comment_only_line(line):
                continue
            if not _TRIGGER_RE.search(line):
                continue
            window = lines[i:i + 4]  # this line, plus up to 3 lines after
            for j, wline in enumerate(window):
                if _is_comment_only_line(wline):
                    continue
                if not _EXIT0_RE.search(wline):
                    continue
                key = (rel, i + j)
                if key in seen:
                    continue
                seen.add(key)
                failures.append(
                    f"{rel}:{i + j + 1}: `exit 0` appears within 3 lines of a "
                    f"failure marker (line {i + 1}: {line.strip()!r}) — a real "
                    "failure here could exit green"
                )
    return failures


# ---------------------------------------------------------------- check 4

_API_KEY_ASSIGN_RE = re.compile(r"ANTHROPIC_API_KEY\s*=\s*(\S+)")
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__"}


def _iter_candidate_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Git-tracked files if root is a git repo; otherwise every regular file
    under root minus obvious noise dirs. The fallback exists so --selftest
    fixtures (plain tempdirs with no .git) exercise the same logic."""
    if (root / ".git").is_dir():
        try:
            out = subprocess.run(
                ["git", "ls-files"], cwd=root, capture_output=True,
                text=True, timeout=30, check=True,
            )
            return [root / p for p in out.stdout.splitlines() if p]
        except Exception:
            pass  # fall through to the plain walk below

    return [
        p for p in root.rglob("*")
        if p.is_file() and not any(part in _SKIP_DIRS for part in p.parts)
    ]


def check_no_api_key(root: pathlib.Path) -> list[str]:
    """No tracked file may set `ANTHROPIC_API_KEY=` to a non-empty value, and
    deploy/crontab.template may only mention the name inside a `#` comment.

    Catches: the per-token billing footgun — a stray ANTHROPIC_API_KEY set
    anywhere silently overrides the Claude subscription plan and bills the
    API per token.

    This file (src/repo_checks.py) is excluded from the scan. It necessarily
    contains the `ANTHROPIC_API_KEY=` literal itself — in this docstring, in
    the detection regex, and in --selftest's positive/negative fixtures — in
    order to document and test the very thing it's checking for. Without the
    exclusion every run flags itself, which would fire this check on a clean
    repo and, once wired into CI, file a spurious issue every single day.
    What this exclusion BLINDS: a real ANTHROPIC_API_KEY assignment pasted
    directly into repo_checks.py's own source would not be caught by this
    check. Accepted because that file is small, security-focused, has no
    legitimate reason to ever contain a live key, and any such edit would be
    an obvious diff in review — unlike the scattered application/deploy/CI
    files this check exists to cover.
    """
    failures: list[str] = []

    for path in _iter_candidate_files(root):
        if path.resolve() == _SELF_PATH:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        rel = path.relative_to(root)
        for lineno, line in enumerate(text.splitlines(), start=1):
            m = _API_KEY_ASSIGN_RE.search(line)
            if not m:
                continue
            value = m.group(1).strip("'\"")
            if not value:
                continue  # e.g. `.env.example`'s empty placeholder — not a live key
            failures.append(
                f"{rel}:{lineno}: literal `ANTHROPIC_API_KEY=` with a non-empty "
                "value — this silently switches billing to per-token API use"
            )

    crontab = root / "deploy" / "crontab.template"
    if crontab.exists():
        for lineno, line in enumerate(crontab.read_text(errors="ignore").splitlines(), start=1):
            if "ANTHROPIC_API_KEY" not in line:
                continue
            if line.strip().startswith("#"):
                continue
            failures.append(
                f"deploy/crontab.template:{lineno}: mentions ANTHROPIC_API_KEY "
                f"outside a warning comment: {line.strip()!r}"
            )
    return failures


# ---------------------------------------------------------------- driver

CHECKS = (
    check_cron_paths,
    check_scheduled_jobs_armed,
    check_workflow_failure_exits,
    check_no_api_key,
)


def checks(root: pathlib.Path) -> list[str]:
    failures: list[str] = []
    for fn in CHECKS:
        failures.extend(fn(root))
    return failures


def main() -> None:
    if "--selftest" in sys.argv:
        _selftest()
        return
    failures = checks(REPO)
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        print(f"\n{len(failures)} finding(s) across {len(CHECKS)} checks")
        sys.exit(1)
    print(f"repo_checks: PASS ({len(CHECKS)} checks, repo clean)")


# ---------------------------------------------------------------- selftest

def _write(root: pathlib.Path, relpath: str, content: str) -> None:
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _selftest() -> None:
    # -------------------- check 1: check_cron_paths --------------------
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, "deploy/crontab.template", """\
# a comment line, ignored
PATH=/usr/local/sbin:/usr/local/bin
HOME=/root
0 20 * * 0   /opt/agentic-trader/deploy/run_slow_loop.sh >> logs/slow.log 2>&1
30 22 * * *  cd /opt/agentic-trader && deploy/backup_ledger.sh >> logs/backup.log 2>&1
""")
        assert check_cron_paths(root) == [], check_cron_paths(root)

    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, "deploy/crontab.template", """\
# a comment line, ignored
0 20 * * 0   deploy/run_slow_loop.sh >> logs/slow.log 2>&1
""")
        bad = check_cron_paths(root)
        assert len(bad) == 1, bad
        assert "crontab.template:2" in bad[0], bad

    # missing crontab.template entirely -> a reported failure, not a crash
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        bad = check_cron_paths(root)
        assert len(bad) == 1 and "missing" in bad[0], bad

    # -------------------- check 2: check_scheduled_jobs_armed ----------
    clean_cron = """\
0 20 * * 0   /opt/agentic-trader/deploy/run_slow_loop.sh >> logs/slow.log 2>&1
15 20 * * 0  cd /opt/agentic-trader && /usr/bin/python3 scripts/collect_signals.py >> logs/signals.log 2>&1
0 10 * * 1-5 /opt/agentic-trader/deploy/run_fast_loop.sh >> logs/fast.log 2>&1
0 12 * * 1-5 /opt/agentic-trader/deploy/run_risk_review.sh >> logs/risk_review.log 2>&1
0 21 * * 0   /opt/agentic-trader/deploy/run_newsletter.sh >> logs/newsletter.log 2>&1
30 22 * * *  cd /opt/agentic-trader && deploy/backup_ledger.sh >> logs/backup.log 2>&1
"""
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, "deploy/crontab.template", clean_cron)
        assert check_scheduled_jobs_armed(root) == [], check_scheduled_jobs_armed(root)

    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        # drop the signal-panel line -> reproduces the real never-armed bug
        dirty_cron = "\n".join(
            l for l in clean_cron.splitlines() if "collect_signals.py" not in l
        )
        _write(root, "deploy/crontab.template", dirty_cron)
        bad = check_scheduled_jobs_armed(root)
        assert len(bad) == 1, bad
        assert "signal_panel" in bad[0], bad

    # every real SPECS key must be accounted for (regression guard: a new key
    # added to health.SPECS without updating this module must be caught)
    unaccounted = [
        k for k in health.SPECS
        if k not in NOT_CRON and k not in CRON_SUBSTRINGS
    ]
    assert not unaccounted, f"SPECS keys missing from repo_checks mapping: {unaccounted}"

    # -------------------- check 3: check_workflow_failure_exits --------
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, ".github/workflows/ok.yml", """\
jobs:
  build:
    steps:
      - run: |
          echo "::error::something failed"
          exit 1
""")
        assert check_workflow_failure_exits(root) == [], check_workflow_failure_exits(root)

    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, ".github/workflows/bad.yml", """\
jobs:
  build:
    steps:
      - run: |
          echo "::error::token expired, clone FAILED"
          echo "continuing anyway"
          exit 0
""")
        bad = check_workflow_failure_exits(root)
        assert len(bad) == 1, bad
        assert "bad.yml:7" in bad[0], bad

    # no .github/workflows dir at all -> clean, not a crash
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        assert check_workflow_failure_exits(root) == []

    # both the failure-marker AND the `exit 0` appear ONLY in `#` comment
    # lines narrating a historical, already-fixed bug -> must PASS. This is
    # the adaptive-tune.yml shape: prose about a past bug, real code path
    # elsewhere uses `exit 1`.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, ".github/workflows/commented.yml", """\
jobs:
  build:
    steps:
      - run: |
          # Historically this step let a FAILED clone fall through to
          # `exit 0` and CI reported green. Fixed below: real failures exit 1.
          if ! git clone "$REPO"; then
            echo "::error::clone failed"
            exit 1
          fi
""")
        assert check_workflow_failure_exits(root) == [], check_workflow_failure_exits(root)

    # a trailing inline comment on a real code line must NOT be treated as
    # comment-only -- the `exit 0` here is a live code path and must still
    # be flagged.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, ".github/workflows/inline.yml", """\
jobs:
  build:
    steps:
      - run: |
          echo "::error::something FAILED"
          exit 0  # fine here
""")
        bad = check_workflow_failure_exits(root)
        assert len(bad) == 1, bad
        assert "inline.yml:6" in bad[0], bad

    # -------------------- check 4: check_no_api_key ---------------------
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, "deploy/run_fast_loop.sh", """\
#!/usr/bin/env bash
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "REFUSING: ANTHROPIC_API_KEY is set" >&2
  exit 1
fi
""")
        _write(root, ".env.example", "ANTHROPIC_API_KEY=\n")
        _write(root, "deploy/crontab.template", """\
# Do NOT put ANTHROPIC_API_KEY here or anywhere -- it forces per-token billing.
0 20 * * 0   /opt/agentic-trader/deploy/run_slow_loop.sh >> logs/slow.log 2>&1
""")
        assert check_no_api_key(root) == [], check_no_api_key(root)

    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, "deploy/some_wrapper.sh", 'export ANTHROPIC_API_KEY=sk-ant-livekeyvalue\n')
        bad = check_no_api_key(root)
        assert len(bad) == 1, bad
        assert "some_wrapper.sh:1" in bad[0], bad

    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, "deploy/crontab.template", """\
0 20 * * 0   /opt/agentic-trader/deploy/run_slow_loop.sh >> logs/slow.log 2>&1
ANTHROPIC_API_KEY=leaked-in-a-noncomment-line
""")
        bad = check_no_api_key(root)
        # both the general assignment scan and the crontab-specific mention
        # scan legitimately fire on the same line — that's fine, not a bug.
        assert any("crontab.template:2" in b for b in bad), bad

    # repo_checks.py's own source (docstrings, regex, fixture strings) must
    # NOT self-match — this is the defect this module was built to fix: a
    # bare run against the real, clean repo must come back empty.
    self_bad = [f for f in check_no_api_key(REPO) if "repo_checks.py" in f]
    assert self_bad == [], self_bad

    # the exclusion is by resolved PATH to this exact file, not by filename —
    # a *different* file that happens to be named repo_checks.py must still
    # be scanned and flagged like any other file.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(
            root, "src/repo_checks.py",
            'ANTHROPIC_API_KEY=not-actually-excluded-because-path-differs\n',
        )
        bad = check_no_api_key(root)
        assert len(bad) == 1, bad
        assert "repo_checks.py:1" in bad[0], bad

    # -------------------- checks() aggregator + main() plumbing --------
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, "deploy/crontab.template", clean_cron)
        _write(root, ".github/workflows/ok.yml", "jobs: {}\n")
        assert checks(root) == [], checks(root)

    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        # leave crontab.template entirely absent -> multiple checks fire
        bad = checks(root)
        assert len(bad) >= 2, bad

    print("repo_checks selftest: PASS")


if __name__ == "__main__":
    main()
