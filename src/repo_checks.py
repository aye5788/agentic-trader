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


# ---------------------------------------------------------------- driver

CHECKS = (
    check_cron_paths,
    check_scheduled_jobs_armed,
    check_workflow_failure_exits,
    check_workflow_swallowed_failures,
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
