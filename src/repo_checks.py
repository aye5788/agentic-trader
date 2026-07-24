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

# Redirection / shell-operator tokens that are never path arguments to check.
_REDIRECT_STARTS = (">", "<", "2>", "&>", "1>")
_SHELL_OPS = {"&&", "||", "|", ";", "&"}
# A bare token with no `/` still names a script if it carries a script suffix.
_SCRIPT_SUFFIX_RE = re.compile(r"\.(py|sh|bash|pl|rb|js)$")


def _cron_line_id(parts: list[str]) -> str:
    """A publishable identifier for a cron line: the 5 schedule fields plus the
    command's FIRST token only.

    Never the whole line. This module's output is filed verbatim into a public
    GitHub issue, and a cron line can carry inline env assignments
    (`FOO=secret /usr/bin/thing`) whose values must not be republished. Even the
    first token is redacted past the `=` if it is itself such an assignment.
    """
    schedule = " ".join(parts[:5])
    toks = parts[5].split()
    head = toks[0] if toks else ""
    if "=" in head:
        head = head.split("=", 1)[0] + "=<redacted>"
    return f"{schedule} {head}"


def _relative_path_tokens(command: str) -> list[str]:
    """Tokens in `command` that name a path/script but are RELATIVE.

    Covers the whole command, not just the first token: an absolute interpreter
    with a relative script argument (`/usr/bin/python3 scripts/foo.py`) is the
    same cwd bug and used to pass. Skipped: flags (`-x`), redirect operators and
    their targets (`>> logs/x.log`, `2>&1`), shell operators, absolute/`~` paths,
    and `$VAR` expansions that cannot be resolved statically.
    """
    bad: list[str] = []
    expect_redirect_target = False
    for tok in command.split():
        if expect_redirect_target:
            expect_redirect_target = False
            continue
        if tok in _SHELL_OPS:
            continue
        if tok.startswith(_REDIRECT_STARTS):
            # bare `>>` takes the NEXT token as its target; `2>&1` / `>>foo` don't
            expect_redirect_target = tok in {">", ">>", "<", "2>", "&>", "1>"}
            continue
        if tok.startswith(("-", "/", "~", "$")):
            continue
        if "/" in tok or _SCRIPT_SUFFIX_RE.search(tok):
            bad.append(tok)
    return bad


def check_cron_paths(root: pathlib.Path) -> list[str]:
    """Every non-comment cron *command* must be absolute-path-rooted — the
    command itself AND any script/path argument it passes — or the line must
    `cd /opt/agentic-trader` first.

    Catches: the `cd`-prefix bugs where cron's cwd is `/root` and a relative
    path (`deploy/foo.sh`, `scripts/bar.py`) silently resolves to nothing and
    fails with no visible error — including the form where the interpreter is
    absolute but its script argument is not
    (`/usr/bin/python3 scripts/health_check.py`), which an inspection of only
    the first token reports clean.

    Failure text carries file:line plus schedule + command name only; the rest
    of the line is withheld because this output is published (see _cron_line_id).
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
        if "cd /opt/agentic-trader" in command:
            continue
        where = f"deploy/crontab.template:{lineno} [{_cron_line_id(parts)}]"
        if not command.startswith("/"):
            failures.append(
                f"{where}: command is neither absolute-path nor prefixed with "
                "`cd /opt/agentic-trader` — cron's cwd is /root, so a relative "
                "path here silently fails"
            )
            continue
        relative = _relative_path_tokens(command)
        if relative:
            failures.append(
                f"{where}: absolute command but RELATIVE path argument(s) "
                f"{relative!r} — cron's cwd is /root, so these silently resolve "
                "to nothing; use an absolute path or prefix `cd /opt/agentic-trader`"
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
    substring present on a NON-COMMENT line of deploy/crontab.template.

    Scope — what this does and does not cover, stated honestly:

    COVERS: template drift only. A job that health.SPECS says is scheduled but
    that has no live line in deploy/crontab.template (absent entirely, or
    present but commented out) — and, via the CRON_SUBSTRINGS/NOT_CRON
    completeness rule, a newly-added SPECS key nobody mapped.

    DOES NOT COVER: the live crontab. This check reads a checked-in template
    file and nothing else. It CANNOT see `crontab -l`.

    It would therefore NOT have caught the 2026-07-24 moomoo signal-panel
    incident: that line WAS present in this template, correct and uncommented,
    and the failure was that it was never appended to the live crontab on the
    box. Only src/health.py's artifact-freshness checks see that class of
    failure — a job that stopped producing its artifact. Comment lines are
    stripped before matching precisely so that this check cannot be satisfied
    by prose *about* a job (the substring appearing in a `#` narration, or in a
    commented-out line, used to count as armed).
    """
    path = root / "deploy" / "crontab.template"
    try:
        text = path.read_text()
    except OSError:
        return [f"missing {path} — cannot verify scheduled jobs are armed"]

    # Match against live lines ONLY: a `#`-commented line arms nothing, and
    # substring-matching the whole file made a commented-out job report armed.
    live_text = "\n".join(
        ln for ln in text.splitlines() if not ln.strip().startswith("#")
    )

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
        if expected not in live_text:
            commented_out = expected in text
            why = (
                "it appears ONLY on a commented-out/prose line, which arms nothing"
                if commented_out else "it is not present at all"
            )
            failures.append(
                f"deploy/crontab.template: health.SPECS key {key!r} ({label}) "
                f"expects {expected!r} on a live cron line but {why} — "
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

# What a value must look like once markdown/shell wrapping is peeled off, to be
# treated as plausibly a real credential: a bare opaque token, >= 9 chars, made
# only of the characters that appear in API keys.
_CREDENTIAL_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+/=\-]{7,}$")


def _looks_like_credential(raw: str) -> bool:
    """True iff the text assigned to ANTHROPIC_API_KEY plausibly IS a live key.

    The point is the FALSE-POSITIVE class, not stylistic tidiness: the naive
    "any non-empty value" rule accepted a bare backtick, so ordinary markdown
    prose of the shape  the literal `ANTHROPIC_API_KEY=`  matched and fired.
    This repo's docs discuss that footgun constantly (docs/superpowers/plans/,
    CLAUDE.md, DEPLOY.md), so with this check wired into a daily job that files
    a public issue, a sentence in a design doc would file an issue every day
    forever — and a checker that cries wolf daily is a checker nobody reads.

    Fixed at the root class rather than by excluding paths or file types: .md
    files are still scanned in full, so a REAL key committed in markdown is
    still caught. Rejected here are only values that cannot be a key —

      * prose/markdown wrapping that leaves nothing behind:
        `ANTHROPIC_API_KEY=` , "ANTHROPIC_API_KEY=" , trailing sentence
        punctuation, or end-of-line (no value at all);
      * shell-safe forms this repo uses on purpose: ${ANTHROPIC_API_KEY:-},
        $OTHER, "" / '' (and `unset ANTHROPIC_API_KEY`, which has no `=` and
        never reached the regex);
      * fragments too short or too punctuated to be a credential ("sk-ant-...").

    Note a leading quote is peeled, NOT treated as an automatic pass: a real
    shell assignment quotes its value (ANTHROPIC_API_KEY="sk-ant-real"), and
    that must stay caught. It is the peeled INNER text that has to look like a
    secret.
    """
    v = raw.strip()
    # peel markdown code fences / shell quoting from both ends, then any
    # sentence punctuation a prose line left clinging to the token.
    v = v.strip("`'\"")
    v = v.rstrip("`'\".,;:!?)]}>")
    v = v.strip()
    if not v:
        return False            # `ANTHROPIC_API_KEY=` in prose, or an empty placeholder
    if "$" in v or "{" in v:
        return False            # ${VAR:-} / $VAR expansion, not a literal secret
    return bool(_CREDENTIAL_VALUE_RE.match(v))


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
    """No tracked file may set `ANTHROPIC_API_KEY=` to a value that plausibly
    IS a credential (see _looks_like_credential — prose and `${VAR:-}` forms do
    not count), and deploy/crontab.template may only mention the name inside a
    `#` comment.

    Findings report file:line ONLY, never the matched value: this output is
    filed verbatim into a public GitHub issue, so echoing the offending text
    would leak the key it just found.

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
            if not _looks_like_credential(m.group(1)):
                continue  # prose, ${VAR:-}, or `.env.example`'s empty placeholder
            failures.append(
                f"{rel}:{lineno}: literal `ANTHROPIC_API_KEY=` assigned a "
                "credential-shaped value (withheld) — this silently switches "
                "billing to per-token API use"
            )

    crontab = root / "deploy" / "crontab.template"
    if crontab.exists():
        for lineno, line in enumerate(crontab.read_text(errors="ignore").splitlines(), start=1):
            if "ANTHROPIC_API_KEY" not in line:
                continue
            if line.strip().startswith("#"):
                continue
            # Report the LOCATION only. Interpolating the line here published
            # the very secret this check exists to find: if someone really did
            # write ANTHROPIC_API_KEY=sk-ant-... on a live cron line, the old
            # message pasted that key into a public GitHub issue and its
            # notification emails. The general scan above already reports
            # file:line with no value; this branch now matches it.
            failures.append(
                f"deploy/crontab.template:{lineno}: mentions ANTHROPIC_API_KEY "
                "outside a warning comment (line content withheld — this "
                "output is published; open the file at that line to inspect)"
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

    # FINDING 3 fixture: absolute interpreter, RELATIVE script argument. Only
    # the first token is absolute, so the old first-token-only check reported
    # this clean — yet cron's cwd is /root, so scripts/health_check.py resolves
    # to nothing and the job silently no-ops.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, "deploy/crontab.template", """\
0 8 * * * /usr/bin/python3 scripts/health_check.py >> logs/health.log 2>&1
""")
        bad = check_cron_paths(root)
        assert len(bad) == 1, bad
        assert "crontab.template:1" in bad[0], bad
        assert "scripts/health_check.py" in bad[0], bad
        # the redirect TARGET is relative too but must not be reported as the
        # bug class here -- only the executed script argument.
        assert "logs/health.log" not in bad[0], bad

    # ...and the same line made safe by a `cd` prefix must stay silent, as must
    # flags, absolute args, `$VAR`, redirects and shell operators.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, "deploy/crontab.template", """\
0 8 * * *  cd /opt/agentic-trader && /usr/bin/python3 scripts/health_check.py >> logs/health.log 2>&1
0 9 * * *  /usr/bin/python3 /opt/agentic-trader/scripts/health_check.py >> /opt/agentic-trader/logs/h.log 2>&1
0 7 * * *  /usr/bin/timeout 900 /opt/agentic-trader/deploy/run_slow_loop.sh --quiet $EXTRA 2>&1
""")
        assert check_cron_paths(root) == [], check_cron_paths(root)

    # FINDING 1 fixture: the reported text must locate the line WITHOUT
    # dumping it -- an inline env assignment's value must never be echoed.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, "deploy/crontab.template", """\
0 8 * * * SOME_TOKEN=hunter2supersecret deploy/run_slow_loop.sh >> logs/x.log 2>&1
""")
        bad = check_cron_paths(root)
        assert len(bad) == 1, bad
        assert "crontab.template:1" in bad[0], bad
        assert "hunter2supersecret" not in bad[0], bad
        assert "<redacted>" in bad[0], bad

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

    # FINDING 4a fixture: the arming line EXISTS but is commented out. A
    # commented line arms nothing, yet the old whole-file substring match
    # reported it armed. Must FAIL.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        commented_cron = "\n".join(
            ("# " + l) if "collect_signals.py" in l else l
            for l in clean_cron.splitlines()
        )
        _write(root, "deploy/crontab.template", commented_cron)
        bad = check_scheduled_jobs_armed(root)
        assert len(bad) == 1, bad
        assert "signal_panel" in bad[0], bad
        assert "commented-out" in bad[0], bad

    # ...and prose merely NAMING the script in a `#` narration must not count
    # as armed either.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        prose_cron = "\n".join(
            ("# see scripts/collect_signals.py -- TODO arm this" if "collect_signals.py" in l else l)
            for l in clean_cron.splitlines()
        )
        _write(root, "deploy/crontab.template", prose_cron)
        bad = check_scheduled_jobs_armed(root)
        assert len(bad) == 1 and "signal_panel" in bad[0], bad

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
        # FINDING 1: neither message may echo the offending value. This output
        # is filed verbatim into a public GitHub issue.
        assert not any("leaked-in-a-noncomment-line" in b for b in bad), bad

    # FINDING 1 (crontab branch, real-key shape): the value must never appear
    # in the finding text.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, "deploy/crontab.template", """\
0 20 * * 0   /opt/agentic-trader/deploy/run_slow_loop.sh >> /opt/x.log 2>&1
ANTHROPIC_API_KEY=sk-ant-api03-REDACTEDLOOKINGVALUE123
""")
        bad = check_no_api_key(root)
        assert any("crontab.template:2" in b for b in bad), bad
        assert not any("REDACTEDLOOKINGVALUE123" in b for b in bad), bad

    # -------------------- FINDING 2: prose must not trip -----------------
    # Markdown discussing the footgun — the exact shapes this repo's docs and
    # plan files use. The naive `(\\S+)` rule accepted a bare backtick as a
    # "non-empty value", so every one of these fired.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, "docs/plan.md", """\
- Auth is `CLAUDE_CODE_OAUTH_TOKEN` (subscription). **Never introduce `ANTHROPIC_API_KEY`**
4. **`check_no_api_key`** — no tracked file may contain the literal `ANTHROPIC_API_KEY=`
   A stray `ANTHROPIC_API_KEY=` flips billing to per-token API use.
   Written in prose without backticks: ANTHROPIC_API_KEY= is still harmless.
""")
        _write(root, "deploy/guard.sh", """\
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then exit 1; fi
ANTHROPIC_API_KEY=""
ANTHROPIC_API_KEY=$OTHER_VAR
unset ANTHROPIC_API_KEY
""")
        assert check_no_api_key(root) == [], check_no_api_key(root)

    # ...but a REAL-looking assignment inside a .md file is STILL caught. The
    # fix is to the value pattern, not a wholesale .md exclusion.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, "docs/leak.md", """\
Some prose about the footgun, then an actual pasted key:

    ANTHROPIC_API_KEY=sk-ant-api03-REDACTEDLOOKINGVALUE123
""")
        bad = check_no_api_key(root)
        assert len(bad) == 1, bad
        assert "leak.md:3" in bad[0], bad
        assert "REDACTEDLOOKINGVALUE123" not in bad[0], bad

    # quoted real key in a shell file: a leading quote is peeled, not a free
    # pass — the inner text is what must look like a secret.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, "deploy/w.sh", 'export ANTHROPIC_API_KEY="sk-ant-api03-REALLOOKING123"\n')
        bad = check_no_api_key(root)
        assert len(bad) == 1, bad
        assert "REALLOOKING123" not in bad[0], bad

    # ACCEPTANCE: this project's OWN plan file contains the prose shape at
    # issue and is about to be committed (repo policy: commit+push after any
    # change). It must not trip the check. Scanned DIRECTLY rather than via
    # check_no_api_key(REPO) so the assertion holds whether or not the file is
    # git-tracked at the moment the selftest runs.
    _plan = REPO / "docs/superpowers/plans/2026-07-24-auto-fix-oversight-loop.md"
    if _plan.exists():
        _hits = [
            lineno
            for lineno, line in enumerate(_plan.read_text(errors="ignore").splitlines(), 1)
            if (_m := _API_KEY_ASSIGN_RE.search(line)) and _looks_like_credential(_m.group(1))
        ]
        assert _hits == [], f"plan-file prose tripped check_no_api_key at lines {_hits}"

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
