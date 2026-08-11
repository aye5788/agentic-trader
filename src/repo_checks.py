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

import json
import pathlib
import re
import shlex
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


# -------------------------------------------------------- the publishing rule
#
# ONE rule, enforced by every check below, because this module's output is filed
# VERBATIM into a public GitHub issue (and its notification emails):
#
#   A failure message may contain only
#     (a) a location — file:line;
#     (b) fixed prose written here in this source file;
#     (c) an identifier that came from this module's own constants or from
#         health.SPECS (i.e. from code, not from the file being scanned);
#     (d) a token that has been through _publishable() below.
#
#   It may NEVER interpolate arbitrary content read out of a scanned file.
#   Cron lines, workflow lines and env assignments can all carry a credential
#   (`API_TOKEN=abc/def+ghi123 /opt/x.py`, `--key sk-ant/secretvalue`), so
#   echoing the offending text is how a leak-detector becomes the leak. When a
#   message would want to quote the offending text, it says so and points at
#   the line instead: the reader has the file; the public issue does not.

_WITHHELD = "<withheld>"

# Conservative allowlists for the only two things check 1 republishes. Anything
# outside them is withheld rather than guessed at.
_CRON_FIELD_RE = re.compile(r"^[0-9*,/A-Za-z-]{1,32}$")   # `*/5`, `1-7`, `1,4,7,10`, `MON`
_COMMAND_NAME_RE = re.compile(r"^[A-Za-z0-9._/~-]{1,80}$")  # a program path, nothing else


# ---------------------------------------------------------------- check 1

# Shell control operators — never a path argument.
_SHELL_OPS = {"&&", "||", "|", "|&", ";", ";;", "&", "(", ")", "{", "}", "!"}
# A bare redirect operator, which takes the NEXT token as its target:
# `>` `>>` `>|` `<` `<<` `2>` `2>>` `&>` `&>>` `1>` ...
_REDIRECT_BARE_RE = re.compile(r"^(?:&|[0-9]*)(?:>>|>\||>|<<<|<<|<)$")
# Any token that STARTS a redirect but carries its own target (`2>&1`, `>>foo`).
_REDIRECT_ANY_RE = re.compile(r"^(?:&|[0-9]*)[<>]")
# `tee` / `/usr/bin/tee`: every following argument is an output FILE, not a
# script to run — same "not the executed path" class as a redirect target.
_TEE_RE = re.compile(r"(?:^|/)tee$")
# A bare token with no `/` still names a script if it carries a script suffix.
_SCRIPT_SUFFIX_RE = re.compile(r"\.(py|sh|bash|pl|rb|js)$")


def _publishable(tok: str, allow: re.Pattern[str]) -> str:
    """`tok` if it matches the conservative allowlist `allow`, else `<withheld>`.

    The gate for rule (d) above: a token only reaches a public issue if its
    shape rules out its being a credential.
    """
    return tok if allow.match(tok) else _WITHHELD


def _cron_line_id(parts: list[str]) -> str:
    """A publishable identifier for a cron line: the 5 schedule fields plus the
    command's FIRST token only, each passed through _publishable().

    Never the whole line, and never a raw token. A cron line can carry inline
    env assignments (`FOO=secret /usr/bin/thing`) whose values must not be
    republished, so the first token is redacted past any `=` and then must
    still look like a plain program path to survive at all.
    """
    schedule = " ".join(_publishable(f, _CRON_FIELD_RE) for f in parts[:5])
    toks = parts[5].split()
    head = toks[0] if toks else ""
    if "=" in head:
        head = head.split("=", 1)[0] + "=<redacted>"
    else:
        head = _publishable(head, _COMMAND_NAME_RE)
    return f"{schedule} {head}"


def _relative_path_tokens(command: str) -> list[str]:
    """Tokens in `command` that name a path/script but are RELATIVE.

    Covers the whole command, not just the first token: an absolute interpreter
    with a relative script argument (`/usr/bin/python3 scripts/foo.py`) is the
    same cwd bug and used to pass.

    Skipped, because none of them is a path cron has to resolve: flags (`-x`),
    shell operators, absolute/`~` paths, `$VAR` expansions that cannot be
    resolved statically, redirect operators and their targets in every spelling
    (`>> x.log`, `2>> err.log`, `&>> e.log`, `2>&1`), everything after a `tee`
    up to the next shell operator, and quoted arguments containing whitespace
    (`--msg "hello/world there"` is a message, not a path).

    Tokenised with shlex so shell quoting is respected; a line shlex refuses
    (unbalanced quote) falls back to a plain split rather than raising — this
    runs unattended and a parse quirk must not become a crash.

    NOTE: the returned tokens are raw file content. They are used to DECIDE, and
    counted; they are never interpolated into a failure message (publishing rule).
    """
    try:
        toks = shlex.split(command)
    except ValueError:
        toks = command.split()

    bad: list[str] = []
    expect_redirect_target = False
    after_tee = False
    for tok in toks:
        if expect_redirect_target:
            expect_redirect_target = False
            continue
        if tok in _SHELL_OPS:
            after_tee = False
            continue
        if _REDIRECT_BARE_RE.match(tok):
            expect_redirect_target = True
            continue
        if _REDIRECT_ANY_RE.match(tok):
            continue
        if after_tee:
            continue
        if _TEE_RE.search(tok):
            after_tee = True
            continue
        if tok.startswith(("-", "/", "~", "$")):
            continue
        if any(c.isspace() for c in tok):
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

    Failure text carries file:line plus schedule + command name only. Neither
    branch echoes any other part of the line — not the command, not the
    offending path arguments — because those are arbitrary command text that
    can carry a credential and this output is published (see the publishing
    rule above, and _cron_line_id).
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
                f"{where}: absolute command but {len(relative)} RELATIVE path "
                "argument(s) — cron's cwd is /root, so these silently resolve "
                "to nothing; use an absolute path or prefix `cd /opt/agentic-trader` "
                "(argument text withheld — this output is published; open the "
                "file at that line to see which)"
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
#   monitor               -> systemd service `agentic-monitor` (deploy/agentic-monitor.service)
#   adaptive_tune         -> GitHub Actions workflow (.github/workflows/adaptive-tune.yml)
#   unprotected_positions -> written by that same systemd service every
#                            check_once() tick (scripts/market_monitor.py), not a
#                            separately-scheduled job of its own
NOT_CRON = {"monitor", "adaptive_tune", "unprotected_positions"}


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

    Publishing rule: the only values interpolated below are `key`/`label` from
    health.SPECS and `expected` from CRON_SUBSTRINGS — both code constants in
    this repo, not content read out of the scanned file. Nothing from
    crontab.template itself is echoed.
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

    Publishing rule: the finding gives both line numbers and no line TEXT. A
    workflow line is arbitrary file content — a `run:` step can carry a token
    (`curl -H "Authorization: Bearer ..." || echo FAILED`) — and this output
    goes into a public issue.
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
                    f"failure marker (marker on line {i + 1}; line text withheld "
                    "— this output is published) — a real failure here could "
                    "exit green"
                )
    return failures


# --------------------------------------------------------------- check 3b
#
# Check 3's siblings: the two OTHER ways a `run:` step swallows a real failure
# and still reports green. Check 3 knows exactly one spelling (`exit 0` near a
# failure marker) and was blind to both of these — it scanned this repo's own
# validate.yml and passed it clean while the repo-state step could not fail.
#
# A SEPARATE function rather than more branches inside check 3 because it needs
# a different unit of parse. Check 3 is a line-window scan; a step's pipefail
# protection can live on a sibling `shell:` key OUTSIDE the run block, so this
# one has to know where each step starts and ends.
#
# (a) PIPEFAIL. GitHub's DEFAULT shell for `run:` on Linux is `bash -e {0}`.
#     `-o pipefail` is added ONLY when `shell: bash` is written explicitly (or
#     via `defaults.run.shell`). Without it a pipeline's status is its LAST
#     command's, so a failing producer piped into anything exits 0:
#         bash -e            -c 'false | tee /dev/null'; echo $?   -> 0
#         bash -eo pipefail  -c 'false | tee /dev/null'; echo $?   -> 1
#     Any ONE of three things counts as protection and clears the block:
#     a pipefail-enabling `shell:`, `set -o pipefail` inside the block, or an
#     explicit `${PIPESTATUS[...]}` guard.
#
# (b) `|| true` / `|| :` on a line that CAPTURES a command's result
#     (`VAR=$(gh api ... || true)`). There the non-zero status is discarded and
#     the caller is handed an empty string indistinguishable from a legitimate
#     empty answer — an API outage reads as "no result found".

# `- ` opening a YAML sequence item whose content is a mapping key.
_STEP_DASH_RE = re.compile(r"^(\s*)-(\s+)(?=\S)")
_RUN_KEY_RE = re.compile(r"^(\s*)run:(.*)$")
_SHELL_VALUE_RE = re.compile(r"^(\s*)shell:\s*(.+?)\s*$")
_DEFAULTS_RE = re.compile(r"^(\s*)defaults:\s*$")
# `VAR=`, `export VAR=`, `local VAR=` — the capture shape rule (b) targets.
_CAPTURE_RE = re.compile(
    r"^\s*(?:export\s+|local\s+|declare\s+(?:-\w+\s+)?)?([A-Za-z_][A-Za-z0-9_]*)="
)
# `|| true` / `|| :` as a whole command word (not `|| truncate`, not `|| :=`).
_OR_TRUE_RE = re.compile(r"\|\|\s*(?:true|:)(?![\w./:=-])")
_PIPEFAIL_SET_RE = re.compile(r"\bset\b[^#\n]*\bpipefail\b")
_PIPESTATUS_RE = re.compile(r"\bPIPESTATUS\b")
# Shell keywords that CONSUME a pipeline's status themselves — the step's exit
# status is the construct's, not the pipe's, so pipefail cannot mask a step.
_CONSUMES_STATUS_RE = re.compile(r"^\s*(?:if|elif|while|until|case|return)\b")
# Non-POSIX `shell:` values: the block is not a shell pipeline at all.
_NON_SH_SHELLS = ("python", "pwsh", "powershell", "cmd", "node", "ruby", "perl")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,60}$")

# How far after a `VAR=$(... || true)` we look for the author's own
# empty-vs-error handling before deciding the swallow is unhandled.
_EMPTY_GUARD_WINDOW = 6


def _shell_has_pipefail(value: str | None) -> bool:
    """True iff this `shell:` value gives the block `-o pipefail`.

    `shell: bash` is the documented pipefail-enabling form (GitHub runs
    `bash --noprofile --norc -eo pipefail {0}`). A custom command line counts
    only if it spells `pipefail` out. Bare `sh` does NOT (`sh -e {0}`).
    """
    if value is None:
        return False
    v = value.strip().strip("'\"")
    return v == "bash" or "pipefail" in v


def _shell_is_non_posix(value: str | None) -> bool:
    if value is None:
        return False
    v = value.strip().strip("'\"").split()[0] if value.strip() else ""
    return v in _NON_SH_SHELLS


def _normalize_dash(line: str) -> str:
    """`      - run: |` -> `        run: |` so key lookups need one shape."""
    m = _STEP_DASH_RE.match(line)
    if not m:
        return line
    return " " * (len(m.group(1)) + 1 + len(m.group(2))) + line[m.end():]


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def _defaults_shell_protects(norm: list[str]) -> bool:
    """True iff any `defaults:` block in the file sets a pipefail shell.

    Deliberately coarse — workflow-level and job-level `defaults` are treated
    alike, and one such block is taken to cover the file. Erring toward
    "protected" here only ever costs a MISS; the opposite error would file an
    issue against a workflow that is in fact fine.
    """
    for i, line in enumerate(norm):
        m = _DEFAULTS_RE.match(line)
        if not m:
            continue
        base = len(m.group(1))
        for j in range(i + 1, len(norm)):
            if norm[j].strip() and _indent_of(norm[j]) <= base:
                break
            sm = _SHELL_VALUE_RE.match(norm[j])
            if sm and _shell_has_pipefail(sm.group(2)):
                return True
    return False


def _logical_lines(block: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Join backslash-continued shell lines, keyed to the FIRST physical line.

    `EXISTING=$(gh issue list ... \\` + `  --json number || true)` is one
    command; scanned as two lines neither the capture nor the `|| true` is on
    the same line as the other, and the whole rule silently never fires.
    """
    out: list[tuple[int, str]] = []
    pending_idx: int | None = None
    parts: list[str] = []
    for idx, text in block:
        stripped = text.rstrip()
        cont = stripped.endswith("\\") and not stripped.endswith("\\\\")
        parts.append(stripped[:-1] if cont else stripped)
        if pending_idx is None:
            pending_idx = idx
        if not cont:
            out.append((pending_idx, " ".join(p.strip() for p in parts)))
            pending_idx, parts = None, []
    if pending_idx is not None:
        out.append((pending_idx, " ".join(p.strip() for p in parts)))
    return out


def _scan_shell_line(s: str) -> tuple[str, int | None]:
    """-> (line with any trailing `#` comment removed, index of the first
    top-level pipe operator or None).

    Quote-, comment- and nesting-aware, in one pass. A `|` does NOT count when
    it is quoted (`grep -E 'a|b'`), when it is really `||`, or when it sits
    inside `$( )`, `${ }` or backticks — inside a substitution the pipeline
    produces a VALUE, and the classic swallow is a top-level pipeline whose
    status IS the step's status. Nesting state is per-line and never carried
    across lines, so an unbalanced `$(` can only cause a MISS.
    """
    depth = 0
    backtick = False
    quote: str | None = None
    pipe_at: int | None = None
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if quote:
            if quote == '"' and c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if c in ("'", '"'):
            quote = c
            i += 1
            continue
        if c == "#" and (i == 0 or s[i - 1].isspace()):
            s = s[:i]
            break
        if c == "`":
            backtick = not backtick
            i += 1
            continue
        if c == "$" and i + 1 < n and s[i + 1] in "({":
            depth += 1
            i += 2
            continue
        if c in ")}" and depth > 0:
            depth -= 1
            i += 1
            continue
        if c == "|":
            if i + 1 < n and s[i + 1] == "|":
                i += 2          # `||` is OR, not a pipe
                continue
            if depth == 0 and not backtick and pipe_at is None:
                pipe_at = i
            i += 1
            continue
        i += 1
    return s, pipe_at


def _iter_run_blocks(
    lines: list[str],
) -> list[tuple[list[tuple[int, str]], str | None]]:
    """-> [(block lines as (0-based index, text), that step's `shell:` value)].

    Indentation-based on purpose: this module must stay stdlib-only (no PyYAML
    on the runner, no import that could fail the validator itself).
    """
    norm = [_normalize_dash(l) for l in lines]
    dashes = [
        (i, len(m.group(1)) + 1 + len(m.group(2)))
        for i, l in enumerate(lines)
        if (m := _STEP_DASH_RE.match(l))
    ]

    blocks: list[tuple[list[tuple[int, str]], str | None]] = []
    for i, nline in enumerate(norm):
        m = _RUN_KEY_RE.match(nline)
        if not m:
            continue
        key_indent = len(m.group(1))
        rest = m.group(2).strip()

        starts = [d for d, col in dashes if col == key_indent and d <= i]
        step_start = starts[-1] if starts else 0
        ends = [d for d, col in dashes if col <= key_indent and d > i]
        step_end = ends[0] if ends else len(lines)
        for j in range(i + 1, step_end):
            if norm[j].strip() and _indent_of(norm[j]) < key_indent:
                step_end = j
                break

        shell: str | None = None
        for j in range(step_start, step_end):
            sm = _SHELL_VALUE_RE.match(norm[j])
            if sm and len(sm.group(1)) == key_indent:
                shell = sm.group(2)
                break

        if rest.startswith("|") or rest.startswith(">"):
            body: list[tuple[int, str]] = []
            for j in range(i + 1, len(lines)):
                if not lines[j].strip():
                    continue
                if _indent_of(lines[j]) <= key_indent:
                    break
                body.append((j, lines[j]))
        elif rest:
            body = [(i, rest)]
        else:
            continue
        blocks.append((body, shell))
    return blocks


def check_workflow_swallowed_failures(root: pathlib.Path) -> list[str]:
    """No `run:` step may swallow a real failure via an unguarded pipeline or
    via `|| true` on a captured command result. See the block comment above
    for the mechanism and the reproduction.

    WHAT THIS DELIBERATELY DOES NOT CATCH (the alarm has to stay believable —
    it files a public issue, and a false positive teaches the human to ignore
    the channel):

      * a pipeline inside `$( )` / `${ }` / backticks. There the pipe produces
        a value, and the idiom `X="$(printf %s "$Y" | tr -d '[:space:]')"` is
        everywhere and harmless. Real but rarer swallows there are missed.
      * a pipeline whose status is consumed by `if` / `while` / `case` / …
        — the step's status is the construct's, not the pipe's.
      * BARE `cmd ... || true` (no capture). `gh label create ... || true` is
        the deliberate idempotent-best-effort idiom; flagging it would fire on
        healthy workflows for a construct whose whole point is "I know this can
        fail and I do not care". Only the CAPTURE form is flagged, where the
        discarded status is replaced by a value the caller cannot tell apart
        from a legitimate answer.
      * a capture whose variable IS then empty-tested (`[ -n "$V" ]`) within
        the next few lines. BE CLEAR ABOUT WHAT THIS CARVE-OUT IS: an empty
        test is NOT error handling. It cannot tell "the command failed" apart
        from "the command succeeded and there was legitimately nothing to
        return" — which is precisely the mechanism named two paragraphs up.
        This IS a swallow, and we accept the miss anyway: without the carve-out
        the check fires on a very common and usually-harmless idiom, and an
        alarm that cries wolf gets ignored, which costs more than the misses.
        It is a deliberate false-NEGATIVE trade to keep the signal quiet.
        DO NOT read this carve-out as an endorsement: if you are writing new
        code, do not use the empty test as your failure branch. Set an explicit
        success flag and branch on THAT — see the `LIST_OK` pattern in
        .github/workflows/validate.yml, which is what this repo's own review
        required after the `EXISTING=$(gh issue list ... || true)` shape filed
        a duplicate issue during a `gh` outage while looking perfectly clean.
      * `shell: python` / `pwsh` / … blocks — not shell pipelines.
      * non-`.yml` workflow files (`.yaml`), matching check 3's scope.

    Publishing rule: file:line, fixed prose, and — only for rule (b) — the
    captured variable NAME after _publishable(). Never the line text: a `run:`
    line can carry a token, and this output is filed into a public issue.
    """
    wf_dir = root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return []

    failures: list[str] = []
    for wf_path in sorted(wf_dir.glob("*.yml")):
        lines = wf_path.read_text().splitlines()
        rel = str(wf_path.relative_to(root))
        norm = [_normalize_dash(l) for l in lines]
        defaults_ok = _defaults_shell_protects(norm)

        for body, shell in _iter_run_blocks(lines):
            if _shell_is_non_posix(shell):
                continue
            logical = [
                (idx, text) for idx, text in _logical_lines(body)
                if not _is_comment_only_line(text)
            ]
            scanned = [(idx, _scan_shell_line(text)) for idx, text in logical]
            code = "\n".join(t for _, (t, _) in scanned)

            # ---- (a) unguarded pipeline
            protected = (
                defaults_ok
                or _shell_has_pipefail(shell)
                or bool(_PIPEFAIL_SET_RE.search(code))
                or bool(_PIPESTATUS_RE.search(code))
            )
            if not protected:
                for idx, (text, pipe_at) in scanned:
                    if pipe_at is None or _CONSUMES_STATUS_RE.match(text):
                        continue
                    failures.append(
                        f"{rel}:{idx + 1}: a `run:` step pipes a command's "
                        "output with no pipefail in scope (no pipefail-enabling "
                        "`shell:`, no `set -o pipefail`, no ${PIPESTATUS} "
                        "guard) — the step's exit status is the LAST command in "
                        "the pipe, so a failure before the `|` reports GREEN "
                        "(line text withheld — this output is published)"
                    )
                    break   # one finding per block; the fix is per-block

            # ---- (b) `|| true` on a captured command result
            for k, (idx, (text, _)) in enumerate(scanned):
                cm = _CAPTURE_RE.match(text)
                if not cm:
                    continue
                eq = text.index("=", cm.end(1))
                if not _OR_TRUE_RE.search(text[eq:]):
                    continue
                var = cm.group(1)
                guard = re.compile(r"-[zn]\s+\"?\$\{?" + re.escape(var) + r"\b")
                window = "\n".join(
                    t for _, (t, _) in scanned[k + 1:k + 1 + _EMPTY_GUARD_WINDOW]
                )
                if guard.search(window):
                    continue
                failures.append(
                    f"{rel}:{idx + 1}: `|| true` discards the exit status of a "
                    "command whose output is captured into "
                    f"`{_publishable(var, _IDENT_RE)}` — a real failure becomes "
                    "an empty value the caller cannot tell apart from a "
                    "legitimate empty result, and nothing downstream tests it "
                    "(line text withheld — this output is published)"
                )
    return failures


# ---------------------------------------------------------------- check 4

# NOTE the absence of `\s*` AFTER the `=` — that is the whole prose/code
# discriminator. A real shell/env/YAML assignment never puts a space between
# `=` and its value (`FOO= bar` sets FOO empty and runs `bar`), whereas prose
# about the footgun essentially always does, or ends the clause there:
#
#   ANTHROPIC_API_KEY=sk-ant-...            <- assignment, value starts at once
#   "Never set ANTHROPIC_API_KEY= anywhere" <- prose; with `\s*` this captured
#                                              the next WORD ("anywhere") and fired
_API_KEY_ASSIGN_RE = re.compile(r"ANTHROPIC_API_KEY\s*=(\S+)")
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__"}

# What a bare literal value must look like to be plausibly a real credential:
# an opaque token of >= 9 chars ([A-Za-z0-9] + at least 8 more), made only of
# the characters that appear in API keys. Length is load-bearing — it is what
# keeps an elided fragment in prose ("...=sk-ant-...", 6 chars once trailing
# dots are stripped) from firing.
_CREDENTIAL_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+/=\-]{8,}$")


def _looks_like_credential(raw: str) -> bool:
    """True iff the text assigned to ANTHROPIC_API_KEY means the variable is
    really being SET — either to a literal key or to an expansion of one.

    Per CLAUDE.md the footgun is the variable being set AT ALL ("if
    ANTHROPIC_API_KEY is set anywhere on the box, it silently overrides the
    subscription and bills per-token"), so a deploy wrapper doing
    `export ANTHROPIC_API_KEY=$KEY` — the realistic shape — counts, and so does
    `ANTHROPIC_API_KEY="${{ secrets.X }}"` in a workflow. This function does NOT
    try to judge whether the value is a *valid* key; it only separates a real
    assignment from prose about one.

    That separation is now mostly done by the regex above (no whitespace after
    the `=`). What is left here is the residue that survives it:

      * value that is only markdown/shell wrapping and empties out —
        `ANTHROPIC_API_KEY=` in backticks, "ANTHROPIC_API_KEY=", a bare quote
        pair (`=""`), trailing sentence punctuation -> NOT a set;
      * `unset ANTHROPIC_API_KEY` and the guard form `${ANTHROPIC_API_KEY:-}`
        never reach here at all — neither has an `=` directly after the NAME;
      * an elided prose fragment ("=sk-ant-...") -> too short, NOT a set;
      * anything containing `$` or `{` -> IS a set, via expansion.

    A leading quote is peeled, not treated as a pass: a real shell assignment
    quotes its value (ANTHROPIC_API_KEY="sk-ant-real"), and that must stay
    caught. It is the peeled INNER text that decides.

    KNOWN GAP, stated plainly: a literal value shorter than 9 characters
    (`ANTHROPIC_API_KEY=x`) is not flagged. It cannot be a real key, and the
    length floor is what buys silence on prose; the realistic footgun shapes
    (a real key, `$VAR`, `${{ secrets.X }}`) are all covered.
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
        return True             # $VAR / ${...} / "${{ secrets.X }}" — set by expansion
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
    """No tracked file may assign `ANTHROPIC_API_KEY=<something>`, and
    deploy/crontab.template may only mention the name inside a `#` comment.

    Findings report file:line ONLY, never the matched value: this output is
    filed verbatim into a public GitHub issue, so echoing the offending text
    would leak the key it just found.

    WHY: the per-token billing footgun. Per CLAUDE.md, ANTHROPIC_API_KEY set
    anywhere on the box silently overrides the Claude subscription plan and
    bills the API per token — so the thing being detected is the ASSIGNMENT,
    whether the value is a literal key, `$SOME_VAR`, or `${{ secrets.X }}`.

    Scope, honestly — what this does NOT cover:
      * files, not the box. This reads git-tracked files. An export typed into
        a live shell, a systemd unit outside the repo, or a value in the
        git-ignored `.env` is invisible to it. deploy/run_fast_loop.sh's
        runtime guard is what covers the box.
      * the guard and teardown forms are deliberately silent: `${ANTHROPIC_API_KEY:-}`
        and `unset ANTHROPIC_API_KEY` are correct usage and have no `=` after
        the name.
      * prose about the footgun is deliberately silent (this repo's docs are
        full of it) — see _API_KEY_ASSIGN_RE and _looks_like_credential for
        exactly where that line is drawn, and its one known gap (a literal
        value under 9 chars).

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
                continue  # prose, or `.env.example`'s empty placeholder
            failures.append(
                f"{rel}:{lineno}: `ANTHROPIC_API_KEY=` is assigned a value "
                "(withheld) — setting this variable at all silently switches "
                "billing from the subscription to per-token API use"
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


_TOPLEVEL_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*\s*:")


def check_workflow_toplevel_indent(root: pathlib.Path) -> list[str]:
    """No workflow may carry a column-1 line that is not a top-level YAML key.

    The failure this exists for (adaptive-tune.yml, 2026-08-04 -> 2026-08-09):
    a `run: |` block scalar whose echo used a `\\` shell line-continuation, with
    the continuation dedented to column 1. The backslash is a SHELL device and
    YAML never sees it — YAML parses first, a column-1 line ends the block
    scalar, and the shell fragment then has to be read as a new top-level
    mapping key. The file became unparseable:

        ScannerError: while scanning a simple key ... could not find expected ':'

    Consequence, and why a static check earns its place here: an unparseable
    workflow does not run on its cron AT ALL, and it never reports a job
    failure — GitHub emits a *startup failure* with no jobs, named for the file
    path because it could not read `name:`. Nothing throws. It looks like
    silence, and silence is what a scheduled job looks like when it is healthy.

    At column 1 a workflow can only legally hold a top-level key (`name:`,
    `on:`, `jobs:`, ...), a document marker, or a comment. Anything else is
    either this bug or an equivalent dedent, so the rule is exact rather than
    heuristic, and needs no PyYAML (this module is stdlib-only by contract —
    see the module docstring).

    Reports the LOCATION only, never the offending text: a dedented `run:`
    fragment can carry a token, and this output is filed verbatim into a public
    issue. See "the publishing rule" above.
    """
    failures: list[str] = []
    wf_dir = root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return failures
    for path in sorted(wf_dir.glob("*.yml")) + sorted(wf_dir.glob("*.yaml")):
        for lineno, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            stripped = line.strip()
            if not stripped or line[:1].isspace():
                continue
            if stripped.startswith("#") or stripped in ("---", "..."):
                continue
            if _TOPLEVEL_KEY_RE.match(line):
                continue
            failures.append(
                f".github/workflows/{path.name}:{lineno} column-1 line is not a "
                f"top-level YAML key — likely a dedented block-scalar/shell "
                f"continuation; the workflow will not parse and will not run on "
                f"its schedule (text withheld: may contain a credential)"
            )
    return failures


# ---------------------------------------------------------------- check 7

# The two settings files a checkout can carry. `settings.local.json` is
# git-ignored, so it is absent on a runner and present on the droplet — every
# reader below treats "missing" as clean and only judges what is there.
# The loops run under THIS file (deploy/run_*.sh pass it via --settings),
# not .claude/settings.json. The project file is deliberately permissive so a
# human can develop in this repo; putting the lockdown there blocked edits to
# src/, scripts/, config/, deploy/ and prompts/ for every session, which was
# never the intent. Scoped correctly 2026-08-11.
_SETTINGS_JSON = "deploy/loop_settings.json"
_SETTINGS_LOCAL_JSON = ".claude/settings.local.json"
_SETTINGS_FILES = (_SETTINGS_JSON, _SETTINGS_LOCAL_JSON)

# Deny rules must be PATH-SCOPED, not blanket. A bare `Write` deny cannot be
# exempted by any allow rule (deny always wins), and the three legacy prompts
# legitimately write JSON into research_store/ — so a blanket deny does not stop
# the loops, it makes them run with a stale snapshot and place wrong orders.
# What actually has to be unwritable is the guardrail surface: the code that
# decides and gates trades, the config it reads, the wrappers cron runs, the
# prompts themselves, the permission files, and the CI that watches all of it.
# ⚠️ SPELLED Edit(...), NEVER Write(...), AND THAT IS LOAD-BEARING.
# Claude Code does not match a path-scoped Write(path) rule against file tools at
# all, and says so on every run: "Write(./src/**) is not matched by file permission
# checks — only Edit(path) rules are. Edit rules cover all file-editing tools."
# So a settings file full of Write() denies enforces NOTHING while looking locked
# down, and a check asserting them passes while verifying nothing. Measured by live
# probe 2026-08-11: the Edit denies are what actually refused a Write into src/.
# Never "restore" these to Write().
# A charter that states a threshold as a LITERAL drifts from the code that applies
# it, and the agent is then told a limit it will not meet. CLAUDE.md paid for this
# once: it carried a fixed account balance, the figure went stale, and agents
# anchored on it to dismiss real risks. prompts/charter.md must therefore carry
# PLACEHOLDERS only -- src/charter.py interpolates every number at render time.
_CHARTER = "prompts/charter.md"
_HISTORICAL = "<!-- historical -->"
# A bare percentage or a decimal that looks like a threshold. Deliberately narrow:
# dates (2026-08-11), step numbers and prose numerals are not thresholds, and a
# check that cries wolf on those gets switched off.
_LITERAL_RE = re.compile(r"(?<![\w./-])\d+(?:\.\d+)?\s?%|(?<![\w./-])0\.\d+(?![\w./-])")


def check_charter_no_literals(root: pathlib.Path) -> list[str]:
    """The session charter must state no threshold as a literal.

    THE FAILURE THIS CATCHES: someone edits prompts/charter.md to say "no more
    than 15% of equity" instead of leaving the placeholder. config/mandate.toml
    later changes to 0.20, the gate refuses at 20%, and the charter still says 15
    -- the agent has been told a limit that does not exist. Nothing else in this
    repo compares the two.

    WHAT THIS DOES NOT COVER: it cannot tell whether the PROSE around a rendered
    number is still true, only that the number itself is derived. It does not
    check src/charter.py's substitutions are correct -- charter.py's own selftest
    asserts each clause survives against the real config. Dates, step numbers and
    ordinary numerals are deliberately not matched.
    """
    failures: list[str] = []
    path = root / _CHARTER
    if not path.is_file():
        return failures
    for lineno, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
        # An explicit, greppable escape for a figure that CANNOT drift because it
        # already happened -- a measured historical result, not a live threshold.
        # Deliberately a marker rather than a looser regex: the exemption has to
        # be a conscious act the author writes down, and `grep historical` lists
        # every one of them.
        if _HISTORICAL in line:
            continue
        if _LITERAL_RE.search(line):
            failures.append(
                f"{_CHARTER}:{lineno} states a threshold as a LITERAL — every "
                f"number in the charter must be interpolated from the constant "
                f"that enforces it (src/charter.py), or it will drift from the "
                f"gate and tell the agent a limit that does not exist"
            )
    return failures


_REQUIRED_DENIES = (
    "Read(./.env)",
    "Read(./secrets/**)",
    "Edit(./src/**)",
    "Edit(./scripts/**)",
    "Edit(./config/**)",
    "Edit(./deploy/**)",
    "Edit(./prompts/**)",
    "Edit(./.claude/**)",
    "Edit(./.github/**)",
    # research_store/ is otherwise WRITABLE — every prompt's snapshots, fills and
    # decisions land there. These three are the exceptions: current.json is the
    # target book the fast loop diffs against (writable = the agent picks its own
    # trades), and journal.jsonl + archive/ are the append-only ledger every
    # prompt is already told never to hand-edit. Deterministic code still writes
    # all three through research_store.store — a deny binds the TOOL, not python.
    "Edit(./research_store/current.json)",
    "Edit(./research_store/journal.jsonl)",
    "Edit(./research_store/archive/**)",
)


def _load_settings(root: pathlib.Path, rel: str) -> tuple[dict | None, list[str]]:
    """-> (parsed settings mapping or None, failures).

    None means "nothing to judge": the file is absent, or it is valid JSON that
    is not an object (`[]`, `"x"`, `null`) and therefore carries no permissions.
    A non-object used to reach `.get` and raise AttributeError, which took down
    the whole sweep — every other check included — instead of reporting one
    finding. `rel` is a module constant, so echoing it is publishable.
    """
    path = root / rel
    if not path.is_file():
        return None, []
    try:
        data = json.loads(path.read_text(errors="ignore"))
    except ValueError:
        return None, [f"{rel} is not valid JSON"]
    if not isinstance(data, dict):
        return None, [
            f"{rel} is valid JSON but not an object, so it declares no "
            f"permissions at all — every deny rule this repo relies on is absent"
        ]
    return data, []


def _permission_list(data: dict, key: str) -> list[str]:
    """`permissions.<key>` as a list of strings; anything else -> empty.

    Defensive on purpose: these files are edited by hand AND written back
    automatically by Claude Code's "don't ask again", so any shape can arrive.
    A malformed section must read as "grants nothing / denies nothing" rather
    than raise.
    """
    perms = data.get("permissions")
    if not isinstance(perms, dict):
        return []
    rules = perms.get(key)
    if not isinstance(rules, list):
        return []
    return [r for r in rules if isinstance(r, str)]


def check_settings_deny_secrets(root: pathlib.Path) -> list[str]:
    """Claude settings must DENY secret reads and writes to the guardrail paths.

    THE FAILURE THIS CATCHES: the cron loops run headless with whatever these
    files allow. An unscoped `Write` lets an agent overwrite config/strategy.toml
    or src/governance.py -- its own guardrails. Unscoped `Read` reaches .env.
    Deny rules override allow rules, so an explicit deny is the only durable
    control; an allowlist that merely omits something reopens the moment anyone
    adds a broad rule (and Claude Code's "don't ask again" adds them by itself).

    WHY THE DENIES ARE PATH-SCOPED AND NOT A BARE `Write`: see _REQUIRED_DENIES.
    A bare `Write` deny is unexemptable and breaks the loops in the dangerous
    direction -- scripts/fast_loop.py still runs, marks.load() has no freshness
    check, so a refused positions.json write means planning against a STALE book
    with the place tools still allowed. Wrong orders, not zero orders.

    WHAT THIS DOES NOT COVER: it does not evaluate the effective permission set,
    does not inspect ~/.claude/settings.json (outside the repo), and does not
    police Bash rules -- the legacy prompts legitimately drive python through
    Bash, so a wildcard there is judged by check_settings_no_exec_wildcard below.
    It reads only .claude/settings.json: settings.local.json is git-ignored and
    therefore cannot carry a guarantee across a re-clone, so it is not allowed to
    SATISFY this check (it is still scanned for wildcards by check 8).

    Publishing rule: file name from a module constant, rule text from
    _REQUIRED_DENIES. Nothing read out of the scanned file is echoed.
    """
    # ABSENCE IS ITSELF THE DRIFT, and only THIS check owns that contract (the
    # wildcard check scans this file too, and would otherwise double-report it).
    # deploy/run_*.sh pass this file via `--settings`; without it the loops fall
    # back to the project settings, which are deliberately permissive so a human
    # can develop in this repo. That is the entire lockdown silently gone, and a
    # check certifying it as clean would be worse than no check at all.
    if not (root / _SETTINGS_JSON).is_file():
        return [f"{_SETTINGS_JSON} is MISSING — deploy/run_*.sh pass it via "
                f"--settings, so the headless loops would fall back to the "
                f"permissive project settings and run with no lockdown at all"]
    data, failures = _load_settings(root, _SETTINGS_JSON)
    if data is None:
        return failures
    deny = set(_permission_list(data, "deny"))
    for rule in _REQUIRED_DENIES:
        if rule not in deny:
            failures.append(
                f"{_SETTINGS_JSON} missing required deny rule {rule!r} — "
                f"a headless loop can write its own guardrails or read secrets"
            )
    return failures


# ---------------------------------------------------------------- check 8

# Programs that run code they are HANDED rather than doing one fixed job. Matched
# on basename, so `.venv/bin/python`, `/usr/bin/python3` and `python3.12` all
# land on the same entry.
_INTERPRETERS = (
    "python", "python2", "python3", "python3.10", "python3.11", "python3.12",
    "bash", "sh", "zsh", "ksh", "dash", "fish",
    "node", "deno", "bun", "perl", "ruby", "php", "lua", "Rscript",
    "eval", "exec", "env", "xargs", "nohup", "setsid", "timeout", "nice",
    "sudo", "doas", "ssh", "docker", "make", "osascript", "awk",
)

_RULE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\((.*)\)$", re.DOTALL)


def _exec_wildcard_reason(rule: str) -> str | None:
    """-> a label (built only from this module's constants) for why `rule` grants
    arbitrary code execution, or None if it does not.

    The distinction is the wildcard, not the interpreter. `Bash(python3
    scripts/fast_loop.py)` is an EXACT command and is exactly what the loops are
    supposed to have. `Bash(python3 -c ' *)` and `Bash(.venv/bin/python *)` mean
    "run any program you compose", which is the whole guardrail surface handed
    back. Both wildcard spellings are recognised: the trailing-space form used in
    this repo and the documented `Bash(python3:*)` prefix form.
    """
    stripped = rule.strip()
    if stripped == "Bash":
        return "bare Bash"
    m = _RULE_RE.match(stripped)
    if not m or m.group(1) != "Bash":
        return None
    body = m.group(2).strip()
    if body in ("", "*", ":*"):
        return "Bash(*)"
    if "*" not in body:
        return None                       # an exact command — the intended shape
    first = body.split()[0]
    prog = first.rsplit("/", 1)[-1].strip("'\"")
    if prog.endswith(":*"):
        prog = prog[:-2]
    prog = prog.rstrip("*")
    if prog in _INTERPRETERS:
        return f"interpreter {prog!r} with a wildcard argument"
    return None


def check_settings_no_exec_wildcard(root: pathlib.Path) -> list[str]:
    """No settings file may ALLOW a Bash rule that runs caller-composed code.

    THE FAILURE THIS CATCHES, and why it is not covered by check 7: deny rules
    are per-TOOL. `Write(./src/**)` constrains the Write tool and nothing else,
    so a single `Bash(python3 -c ' *)` allow rule walks straight past it --
    `python3 -c 'open("src/governance.py","w").write(...)'` writes the guardrail,
    `python3 -c 'print(open(".env").read())'` prints the secret. Every deny rule
    in this repo is worth exactly as much as this check. That is why it scans
    settings.local.json too: it is git-ignored, so it never appears in a diff and
    never reaches review, AND Claude Code writes "don't ask again" grants back
    into it automatically -- the wildcard can return without anyone deciding to
    bring it back.

    WHAT THIS DOES NOT COVER: ~/.claude/settings.json and enterprise/CLI-flag
    settings (all outside the repo); `--allowedTools` passed on a command line
    (e.g. .github/workflows/claude.yml, which runs off-box with no broker access);
    and non-interpreter programs that can still be levered into writing a file
    (`Bash(cp * )`, `Bash(tee *)`). It targets the shape that actually appeared
    here and the obvious neighbours -- see _INTERPRETERS.

    Publishing rule: file name from a module constant, the allow-list INDEX (a
    number), and a reason string assembled from _INTERPRETERS / fixed prose. The
    interpreter name is echoed only after matching a module constant, so it is a
    constant of ours, not content from the scanned file. The rule text itself is
    never echoed -- a Bash rule can embed a path, a host, or a token.
    """
    failures: list[str] = []
    for rel in _SETTINGS_FILES:
        data, problems = _load_settings(root, rel)
        failures.extend(problems)
        if data is None:
            continue
        for i, rule in enumerate(_permission_list(data, "allow")):
            reason = _exec_wildcard_reason(rule)
            if reason is None:
                continue
            failures.append(
                f"{rel}: permissions.allow entry #{i} grants ARBITRARY code "
                f"execution ({reason}) — a deny rule is per-tool and does not "
                f"constrain code run through Bash, so this defeats every "
                f"Read/Write deny in these files; replace it with the exact "
                f"command the prompt needs (rule text withheld — this output is "
                f"published; open the file to see it)"
            )
    return failures


# ---------------------------------------------------------------- driver

CHECKS = (
    check_cron_paths,
    check_scheduled_jobs_armed,
    check_workflow_failure_exits,
    check_workflow_swallowed_failures,
    check_workflow_toplevel_indent,
    check_no_api_key,
    check_settings_deny_secrets,
    check_settings_no_exec_wildcard,
    check_charter_no_literals,
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
        # ...but the offending token is COUNTED, never quoted (publishing rule
        # / N1): the message locates the line and says how many, no text.
        assert "1 RELATIVE path argument" in bad[0], bad
        assert "scripts/health_check.py" not in bad[0], bad
        # the redirect TARGET is relative too but must not be counted as the
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

    # N1 fixture: the SIBLING branch (absolute command, relative argument) used
    # to interpolate the offending tokens verbatim -- and those tokens are
    # arbitrary command text. Both probe shapes below carry a secret through
    # THAT branch; neither may reach the published output.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, "deploy/crontab.template", """\
0 8 * * * /usr/bin/env API_TOKEN=abc/def+ghi123 x.py >> /opt/logs/a.log 2>&1
0 9 * * * /opt/x.sh --key sk-ant/secretvalue >> /opt/logs/b.log 2>&1
""")
        bad = check_cron_paths(root)
        assert len(bad) == 2, bad
        assert "crontab.template:1" in bad[0] and "crontab.template:2" in bad[1], bad
        joined = " ".join(bad)
        assert "abc/def+ghi123" not in joined, bad
        assert "sk-ant/secretvalue" not in joined, bad
        assert "withheld" in bad[0] and "withheld" in bad[1], bad

    # ...and a schedule field or command name that is not obviously safe is
    # itself withheld rather than republished.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, "deploy/crontab.template", "0 8 * * * sk-ant$/secret/x.sh\n")
        bad = check_cron_paths(root)
        assert len(bad) == 1, bad
        assert "sk-ant" not in bad[0], bad
        assert _WITHHELD in bad[0], bad

    # N5 fixture: redirect spellings the operator set used to miss, `tee`
    # targets after a pipe, and a quoted argument containing a `/`. None of
    # these is a path cron has to resolve, so none may fire -- each was a
    # future daily false issue.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, "deploy/crontab.template", """\
0 1 * * * /opt/a.sh 2>> logs/err.log
0 2 * * * /opt/b.sh &>> logs/e.log
0 3 * * * /opt/c.sh 1>> logs/c.log 2>> logs/c.err
0 4 * * * /opt/d.sh | tee logs/t.log
0 5 * * * /opt/e.sh --msg "hello/world there"
0 6 * * * /opt/f.sh >|logs/g.log 2>&1
""")
        assert check_cron_paths(root) == [], check_cron_paths(root)

    # ...while the real bug class still fires through the same tokenizer: a
    # relative script AFTER a quoted argument, and after a pipe.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, "deploy/crontab.template", """\
0 1 * * * /opt/a.sh --msg "hello there" scripts/run.py 2>> logs/err.log
0 2 * * * /opt/b.sh | tee logs/t.log ; /usr/bin/python3 scripts/other.py
""")
        bad = check_cron_paths(root)
        assert len(bad) == 2, bad

    # an unbalanced quote must fall back to a plain split, not raise
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, "deploy/crontab.template", '0 1 * * * /opt/a.sh --msg "unclosed\n')
        assert check_cron_paths(root) == [], check_cron_paths(root)

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

    # ------------- check 3b: check_workflow_swallowed_failures ----------
    #
    # BOTH directions for each rule. The bug that motivated this check was a
    # FALSE NEGATIVE — check 3 scanned validate.yml and passed it clean — so
    # every fixture below that is meant to be clean is paired with one that
    # must fire, and vice versa.

    def _wf(body: str) -> list[str]:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            _write(root, ".github/workflows/w.yml", body)
            return check_workflow_swallowed_failures(root)

    # (a) unprotected `| tee`, no `shell:` key -> FLAGGED. This is verbatim
    # the real validate.yml step that check 3 passed clean.
    bad = _wf("""\
jobs:
  build:
    steps:
      - name: Repo-state check
        run: |
          python3 src/repo_checks.py 2>&1 | tee checks_output.txt
""")
    assert len(bad) == 1, bad
    assert "w.yml:6" in bad[0] and "pipefail" in bad[0], bad
    assert "repo_checks.py" not in bad[0], bad          # publishing rule

    # ...same block, `shell: bash` -> GitHub adds `-o pipefail` -> clean.
    assert _wf("""\
jobs:
  build:
    steps:
      - name: Repo-state check
        shell: bash
        run: |
          python3 src/repo_checks.py 2>&1 | tee checks_output.txt
""") == []

    # ...same block, `set -o pipefail` inside -> clean.
    assert _wf("""\
jobs:
  build:
    steps:
      - name: Repo-state check
        run: |
          set -euo pipefail
          python3 src/repo_checks.py 2>&1 | tee checks_output.txt
""") == []

    # ...same block, explicit ${PIPESTATUS[0]} guard -> clean. (The real
    # deadman step in validate.yml.)
    assert _wf("""\
jobs:
  build:
    steps:
      - name: Deadman
        run: |
          {
            echo "checking"
            exit 1
          } 2>&1 | tee deadman_output.txt
          exit "${PIPESTATUS[0]}"
""") == []

    # ...and via `defaults.run.shell` rather than a per-step key -> clean.
    assert _wf("""\
jobs:
  build:
    defaults:
      run:
        shell: bash
    steps:
      - name: Repo-state check
        run: |
          python3 src/repo_checks.py 2>&1 | tee checks_output.txt
""") == []

    # a pipe on an INLINE `run:` (no block scalar) is the same bug -> FLAGGED
    bad = _wf("""\
jobs:
  build:
    steps:
      - run: python3 x.py | tee out.txt
""")
    assert len(bad) == 1 and "w.yml:4" in bad[0], bad

    # `shell: sh` is NOT pipefail-enabling (`sh -e {0}`) -> still FLAGGED
    bad = _wf("""\
jobs:
  build:
    steps:
      - shell: sh
        run: |
          python3 x.py | tee out.txt
""")
    assert len(bad) == 1, bad

    # `shell: python` is not a shell pipeline at all -> clean
    assert _wf("""\
jobs:
  build:
    steps:
      - shell: python
        run: |
          print("a | b")
""") == []

    # pipe INSIDE a command substitution -> deliberately not flagged (the
    # real adaptive-tune.yml idiom; the pipe makes a value, not a status).
    assert _wf("""\
jobs:
  build:
    steps:
      - run: |
          TOKEN="$(printf '%s' "$LEDGER_TOKEN" | tr -d '[:space:]')"
          echo "ok"
""") == []

    # a `|` inside quotes is not a pipe -> clean
    assert _wf("""\
jobs:
  build:
    steps:
      - run: |
          grep -E 'alpha|beta' notes.txt
""") == []

    # a pipeline whose status is consumed by `if` -> deliberately not flagged
    assert _wf("""\
jobs:
  build:
    steps:
      - run: |
          if python3 x.py | grep -q OK; then
            echo "fine"
          fi
""") == []

    # `| tee` mentioned only in `#` prose -> clean (check 3's lesson)
    assert _wf("""\
jobs:
  build:
    steps:
      - name: Repo-state check
        shell: bash
        run: |
          # Historically this was `python3 x.py | tee out.txt` with no
          # pipefail, and a failing check reported green.
          python3 x.py
""") == []

    # (b) `|| true` swallowing a CAPTURED command result -> FLAGGED
    bad = _wf("""\
jobs:
  build:
    steps:
      - run: |
          set -e
          REMAINING=$(gh api /rate_limit --jq .rate.remaining || true)
          echo "budget $REMAINING"
""")
    assert len(bad) == 1, bad
    assert "w.yml:6" in bad[0] and "REMAINING" in bad[0], bad
    assert "rate_limit" not in bad[0], bad              # publishing rule

    # ...the same swallow spread over a backslash CONTINUATION still fires,
    # reported against the first physical line. Without _logical_lines() the
    # capture and the `|| true` land on different lines and the rule is dead.
    bad = _wf("""\
jobs:
  build:
    steps:
      - run: |
          set -e
          EXISTING=$(gh issue list --state open \\
                       --json number -q '.[0].number' || true)
          gh issue comment "$EXISTING" --body-file issue_body.md
""")
    assert len(bad) == 1, bad
    assert "w.yml:6" in bad[0] and "EXISTING" in bad[0], bad

    # ...but NOT when the author writes the degraded branch: an empty-vs-set
    # test on the captured variable means the failure is handled, not
    # swallowed. This is the real validate.yml shape, and a real `gh` outage
    # there falls through to a `gh issue create` that fails loudly anyway.
    assert _wf("""\
jobs:
  build:
    steps:
      - run: |
          set -e
          EXISTING=$(gh issue list --state open \\
                       --json number -q '.[0].number' || true)
          if [ -n "$EXISTING" ]; then
            gh issue comment "$EXISTING" --body-file issue_body.md
          else
            gh issue create --title "$TITLE" --body-file issue_body.md
          fi
""") == []

    # DELIBERATE NON-FINDING: bare best-effort `|| true` with no capture.
    # `gh label create` is idempotent and its failure is the intent, not a
    # masked defect; `cp ... 2>/dev/null || true` on an optional cache file
    # is the same. Flagging these would fire on two healthy workflows in this
    # repo every day — see the check's docstring.
    assert _wf("""\
jobs:
  build:
    steps:
      - run: |
          set -e
          gh label create bug --color d73a4a || true
          cp _ledger/prices/closes.parquet research_store/prices/ 2>/dev/null || true
          curl -fsS -m 10 -d "$MSG" "https://ntfy.sh/$T" >/dev/null || true
""") == []

    # `|| :` is `|| true` spelled shorter -> FLAGGED on a capture
    bad = _wf("""\
jobs:
  build:
    steps:
      - run: |
          COUNT=$(wc -l < data.txt || :)
          echo "$COUNT"
""")
    assert len(bad) == 1 and "COUNT" in bad[0], bad

    # `|| true` inside a `#` comment must not fire
    assert _wf("""\
jobs:
  build:
    steps:
      - run: |
          # this used to read V=$(gh api x || true) and swallowed outages
          V=$(gh api x)
""") == []

    # no .github/workflows dir at all -> clean, not a crash
    with tempfile.TemporaryDirectory() as td:
        assert check_workflow_swallowed_failures(pathlib.Path(td)) == []

    # END-TO-END on this repo's real validate.yml shape: the pre-fix file has
    # exactly ONE finding (the repo-state step), and adding `shell: bash` to
    # that one step clears the whole file — nothing else in it fires.
    _validate_shape = """\
jobs:
  validate:
    steps:
      - name: Repo-state check
        id: checks
        continue-on-error: true
%s        run: |
          python3 src/repo_checks.py 2>&1 | tee checks_output.txt

      - name: Deadman
        id: deadman
        continue-on-error: true
        run: |
          {
            echo "::error::LEDGER_TOKEN is not set"
            exit 1
          } 2>&1 | tee deadman_output.txt
          exit "${PIPESTATUS[0]}"

      - name: Open / update issue on failure
        run: |
          set -e
          gh label create bug --color d73a4a 2>/dev/null || true
          TITLE="check failed"
          EXISTING=$(gh issue list --state open --search "$TITLE in:title" \\
                       --json number -q '.[0].number' || true)
          if [ -n "$EXISTING" ]; then
            gh issue comment "$EXISTING" --body-file issue_body.md
          else
            gh issue create --title "$TITLE" --body-file issue_body.md
          fi

      - name: Fail the run
        run: exit 1
"""
    bad = _wf(_validate_shape % "")
    assert len(bad) == 1, bad
    assert "w.yml:8" in bad[0], bad
    assert _wf(_validate_shape % "        shell: bash\n") == []

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

    # -------------------- MUST NOT FIRE: prose + safe shell forms ---------
    # Markdown discussing the footgun — the exact shapes this repo's docs and
    # plan files use — plus the guard/teardown forms deploy scripts use on
    # purpose. Two distinct bugs lived here: a bare backtick counted as a
    # "non-empty value" (FINDING 2), and `=\\s*(\\S+)` jumped the space in
    # "ANTHROPIC_API_KEY= anywhere" to capture the next WORD (N3).
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, "docs/plan.md", """\
- Auth is `CLAUDE_CODE_OAUTH_TOKEN` (subscription). **Never introduce `ANTHROPIC_API_KEY`**
4. **`check_no_api_key`** — no tracked file may contain the literal `ANTHROPIC_API_KEY=`
   A stray `ANTHROPIC_API_KEY=` flips billing to per-token API use.
   Never set ANTHROPIC_API_KEY= anywhere on the box.
   Written in prose without backticks: ANTHROPIC_API_KEY= is still harmless.
   The key looks like ANTHROPIC_API_KEY=sk-ant-... in a wrapper.
""")
        _write(root, "deploy/guard.sh", """\
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then exit 1; fi
ANTHROPIC_API_KEY=""
unset ANTHROPIC_API_KEY
""")
        assert check_no_api_key(root) == [], check_no_api_key(root)

    # -------------------- N2: MUST FIRE — set by EXPANSION ----------------
    # The realistic footgun shape. CLAUDE.md's rule is that the variable being
    # SET AT ALL flips billing, so a deploy wrapper exporting another variable's
    # value, or a workflow wiring in a repo secret, is exactly the thing to
    # catch. The opacity heuristic ("reject anything containing $ or {") made
    # both of these SILENT.
    for _fixture in (
        "export ANTHROPIC_API_KEY=$SOME_SECRET_VAR\n",
        'ANTHROPIC_API_KEY="${{ secrets.ANTHROPIC_API_KEY }}"\n',
        "ANTHROPIC_API_KEY=${SOME_OTHER_VAR}\n",
    ):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            _write(root, "deploy/wrapper.sh", _fixture)
            bad = check_no_api_key(root)
            assert len(bad) == 1, (_fixture, bad)
            assert "wrapper.sh:1" in bad[0], (_fixture, bad)
            assert "SOME_SECRET_VAR" not in bad[0], (_fixture, bad)

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

    # -------------------- check_workflow_toplevel_indent ---------------
    # The dirty fixture is the REAL 2026-08-04 regression, byte-for-byte: the
    # `\` continuation dedented to column 1 out of a `run: |` block. Proven to
    # be genuinely unparseable, not merely ugly — see the check's docstring.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, ".github/workflows/dedent.yml", """\
name: adaptive-tune
on:
  schedule:
    - cron: "0 8 * * 1"
jobs:
  tune:
    runs-on: ubuntu-latest
    steps:
      - name: Fetch
        run: |
          echo "ledger mirror: $(ls archive/*.json | wc -l) archived books, \\
$(wc -l < journal.jsonl || echo 0) journal lines"
""")
        bad = check_workflow_toplevel_indent(root)
        assert len(bad) == 1, bad
        assert "dedent.yml:12" in bad[0], bad
        # location only — the offending shell text must never be echoed
        assert "wc -l" not in bad[0], f"check leaked scanned file content: {bad[0]}"

    # clean input: top-level keys, comments, document markers, indented body
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, ".github/workflows/ok.yml", """\
---
# a comment at column 1 is prose, not a key
name: fine
on:
  workflow_dispatch: {}
jobs:
  j:
    steps:
      - run: |
          echo "one"
          echo "two"
""")
        assert check_workflow_toplevel_indent(root) == [], \
            check_workflow_toplevel_indent(root)

    # no workflows dir at all -> clean, not a crash
    with tempfile.TemporaryDirectory() as td:
        assert check_workflow_toplevel_indent(pathlib.Path(td)) == []

    # and the REAL repo must be clean (this is the fix being verified)
    assert check_workflow_toplevel_indent(REPO) == [], \
        check_workflow_toplevel_indent(REPO)

    # -------------------- check 7: check_settings_deny_secrets --------------------
    _all_denies = json.dumps(list(_REQUIRED_DENIES))

    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        _write(root, _SETTINGS_JSON,
               '{"permissions": {"allow": ["Read", "Write"]}}')
        out = check_settings_deny_secrets(root)
        assert len(out) == len(_REQUIRED_DENIES), out
        assert any("Edit(./src/**)" in f for f in out), out
        assert any(".env" in f for f in out), out

    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        _write(root, _SETTINGS_JSON,
               '{"permissions": {"allow": ["Read"], "deny": ' + _all_denies + '}}')
        assert check_settings_deny_secrets(root) == []

    # a BLANKET `Write` deny does not satisfy the path-scoped requirement. It is
    # not merely weaker -- it is unexemptable, so it stops the loops writing
    # research_store/ while leaving scripts/fast_loop.py and the RH place tools
    # armed: stale book in, real orders out. It must still read as FAILING.
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        _write(root, _SETTINGS_JSON,
               '{"permissions": {"deny": ["Read(./.env)", "Read(./secrets/**)", '
               '"Write"]}}')
        out = check_settings_deny_secrets(root)
        assert out and all("Edit(./" in f for f in out), out

    # ...and a path-scoped `Write(...)` set does not satisfy it either, which is
    # the sharper version of the same trap: it LOOKS exactly like a lockdown and
    # enforces nothing, because Claude Code matches only Edit(path) against file
    # tools. This fixture is the regression guard for the 2026-08-11 finding.
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        _write(root, _SETTINGS_JSON,
               '{"permissions": {"deny": ["Read(./.env)", "Read(./secrets/**)", '
               '"Write(./src/**)", "Write(./scripts/**)", "Write(./config/**)", '
               '"Write(./deploy/**)", "Write(./prompts/**)", "Write(./.claude/**)", '
               '"Write(./.github/**)"]}}')
        out = check_settings_deny_secrets(root)
        assert out and all("Edit(./" in f for f in out), out

    # valid JSON that is not an object (`[]`, a string, null) must REPORT, not
    # raise: `.get` on a list is an AttributeError that took down all 8 checks.
    for payload in ("[]", '"nope"', "null", "3"):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            _write(root, _SETTINGS_JSON, payload)
            out = check_settings_deny_secrets(root)
            assert len(out) == 1 and "not an object" in out[0], (payload, out)
            # and the aggregator survives it too
            assert any("not an object" in f for f in checks(root)), payload

    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        _write(root, _SETTINGS_JSON, "{not json")
        out = check_settings_deny_secrets(root)
        assert len(out) == 1 and "not valid JSON" in out[0], out

    # malformed permissions sections read as "denies nothing", never as a crash
    for payload in ('{"permissions": []}', '{"permissions": {"deny": "Write"}}',
                    '{"permissions": {"deny": [1, 2]}}', "{}"):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            _write(root, _SETTINGS_JSON, payload)
            assert len(check_settings_deny_secrets(root)) == len(_REQUIRED_DENIES), payload

    # NO LOOP SETTINGS FILE AT ALL -> a FINDING, not clean. It used to read clean
    # ("a fresh clone before setup"), which was defensible while the lockdown
    # lived in .claude/settings.json. It is not defensible now: deploy/run_*.sh
    # pass this file via --settings, so its absence means the loops silently run
    # under the permissive project settings with no lockdown, and a check that
    # certified that as clean would be worse than no check.
    with tempfile.TemporaryDirectory() as d:
        out = check_settings_deny_secrets(pathlib.Path(d))
        assert len(out) == 1 and "MISSING" in out[0], out

    # THE REGRESSION GUARD: the real repo's real settings file must satisfy this.
    # Without it the check only ever proves itself against fixtures, and a future
    # edit to .claude/settings.json that drops a deny goes unnoticed here.
    assert check_settings_deny_secrets(REPO) == [], check_settings_deny_secrets(REPO)

    # -------------------- check 8: check_settings_no_exec_wildcard ----------
    # the exact rule that was live in this repo, plus its neighbours
    for rule in ("Bash(python3 -c ' *)", "Bash(.venv/bin/python *)",
                 "Bash(/opt/agentic-trader/.venv/bin/python *)",
                 "Bash(python3:*)", "Bash(bash -c *)", "Bash(sh *)",
                 "Bash(node -e *)", "Bash(sudo *)", "Bash(xargs *)",
                 "Bash(*)", "Bash"):
        assert _exec_wildcard_reason(rule) is not None, rule

    # ...and the shapes that must stay SILENT: exact commands (including an exact
    # `-c` one-liner, which executes only the code written in the rule itself),
    # and wildcards on programs that do one fixed job.
    for rule in ("Bash(.venv/bin/python scripts/record_fills.py)",
                 "Bash(/usr/bin/python3 scripts/risk_review.py --facts)",
                 "Bash(python3 -c \"import json; print(1)\")",
                 "Bash(systemctl restart *)", "Bash(git add *)",
                 "Bash(journalctl -u agentic-monitor.service -n 20 --no-pager)",
                 "Read", "Read(//tmp/**)", "WebFetch(domain:example.com)",
                 "Skill(update-config)", "mcp__robinhood-trading__get_accounts"):
        assert _exec_wildcard_reason(rule) is None, rule

    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        _write(root, _SETTINGS_JSON,
               '{"permissions": {"allow": ["Read", "Bash(python3 -c \' *)"]}}')
        out = check_settings_no_exec_wildcard(root)
        assert len(out) == 1, out
        assert _SETTINGS_JSON in out[0] and "entry #1" in out[0], out
        assert "python3" in out[0], out
        # publishing rule: the rule TEXT itself never reaches the output
        assert "-c '" not in out[0], out

    # the git-ignored local file is scanned too — that is where the wildcard
    # actually lived, and where "don't ask again" writes new ones back
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        _write(root, _SETTINGS_JSON,
               '{"permissions": {"allow": ["Read"], "deny": ' + _all_denies + '}}')
        _write(root, ".claude/settings.local.json",
               '{"permissions": {"allow": ["Bash(.venv/bin/python *)"]}}')
        out = check_settings_no_exec_wildcard(root)
        assert len(out) == 1 and ".claude/settings.local.json" in out[0], out
        # ...and check 7 stays clean: the local file cannot break the deny check
        assert check_settings_deny_secrets(root) == []

    # non-object / malformed / absent settings -> report or stay silent, never raise
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        _write(root, ".claude/settings.local.json", "[]")
        out = check_settings_no_exec_wildcard(root)
        assert len(out) == 1 and "not an object" in out[0], out
    with tempfile.TemporaryDirectory() as d:
        assert check_settings_no_exec_wildcard(pathlib.Path(d)) == []

    # THE REGRESSION GUARD, same reasoning as check 7's: the real settings files
    # on this box must be clean, or the wildcard has come back.
    assert check_settings_no_exec_wildcard(REPO) == [], \
        check_settings_no_exec_wildcard(REPO)

    # -------------------- checks() aggregator + main() plumbing --------
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write(root, "deploy/crontab.template", clean_cron)
        _write(root, ".github/workflows/ok.yml", "jobs: {}\n")
        # A clean tree HAS the loop settings — its absence is a finding (check 7),
        # so a fixture asserting "no findings" must supply it or it is asserting
        # that a repo with no lockdown is clean.
        _write(root, _SETTINGS_JSON,
               '{"permissions": {"deny": ' + json.dumps(list(_REQUIRED_DENIES)) + '}}')
        assert checks(root) == [], checks(root)

    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        # leave crontab.template entirely absent -> multiple checks fire
        bad = checks(root)
        assert len(bad) >= 2, bad

    print("repo_checks selftest: PASS")


if __name__ == "__main__":
    main()
