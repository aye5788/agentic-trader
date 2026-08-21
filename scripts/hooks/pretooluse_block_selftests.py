#!/usr/bin/env python3
"""BLOCK the assistant from running selftest/validator suites. Operator-imposed.

WHY THIS EXISTS (2026-08-21, Aaron's instruction, verbatim): "DISABLE YOUR
ACCESS TO THOSE SUITES OR TOOLS OR SHORTCUTS ... YOU MUST MANUALLY REVIEW".

The assistant had fallen into reporting work as verified because `--selftest`
printed PASS. Those suites check that numbers are derived and that assertions
which ALREADY EXISTED still hold. They cannot tell you whether new prose is
true, whether it contradicts something elsewhere in the same document, whether
it duplicates what the document already said, or whether a cross-reference
points at a section nobody ever wrote. All three of those shipped on the same
day with every suite green, and the result was a fabricated theme-concentration
cap that made six of the book's top fourteen names unbuyable while cash ran from
3% to 29% of NAV.

⛔ A WRITTEN RULE WAS NOT ACCEPTED AS SUFFICIENT, and the operator was right:
the same reasoning that moved the order gate into a PreToolUse hook applies
here. Remembering is not a mechanism. This is the mechanism.

Reading a file, rendering a document, and inspecting real output are all
untouched. What is blocked is running a canned suite and treating its exit code
as evidence.

Fails OPEN on any internal error: this guard exists to change a habit, and must
never be the reason a session cannot do real work.
"""
import json
import re
import sys

# Matched against the full command string, anywhere in it.
BLOCKED = [
    (r"--selftest\b",            "a --selftest run"),
    (r"\brepo_checks\.py\b",     "src/repo_checks.py"),
    (r"\bpytest\b",              "pytest"),
    (r"\bunittest\b",            "unittest"),
    (r"python\s+-m\s+pytest",    "pytest"),
    (r"\bnose2?\b",              "nose"),
    (r"\btox\b",                 "tox"),
]

_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _strip_heredocs(cmd: str) -> str:
    """Drop heredoc BODIES before matching.

    ⛔ A COMMIT MESSAGE IS NOT A COMMAND. The first version searched the whole
    string, so `git commit -F - <<'MSG' ... MSG` was denied for merely
    DESCRIBING this ban in its own commit message — and the patch fixing that
    was denied for the same reason, because it quoted the flag it matches on.
    Prose that names a suite is not an invocation of one. Only the executable
    part of the command is searched.
    """
    lines = cmd.splitlines()
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = _HEREDOC.search(line)
        i += 1
        if not m:
            continue
        delim = m.group(2)
        while i < len(lines) and lines[i].strip() != delim:
            i += 1                       # body: skipped, never searched
        if i < len(lines):
            out.append(lines[i])         # keep the terminator
            i += 1
    return "\n".join(out)


MESSAGE = (
    "BLOCKED by the operator: {what}.\n"
    "\n"
    "You are not permitted to run selftest/validator suites in this repo. A "
    "passing suite is not evidence your change is correct -- it checks that "
    "numbers are derived and that pre-existing assertions still hold, and it "
    "passed on every defect that made this rule necessary.\n"
    "\n"
    "MANUALLY REVIEW INSTEAD. Read the whole document or module you changed, "
    "rendered the way its consumer actually receives it, and look specifically "
    "for: (a) something elsewhere that CONTRADICTS your edit, (b) something "
    "elsewhere that ALREADY SAYS it, (c) a cross-reference pointing at a "
    "section that does not exist. Then report what you read, not what exited "
    "zero.\n"
    "\n"
    "Do not work around this by invoking the same suite another way."
)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:                                    # noqa: BLE001
        return 0                                         # fail open
    try:
        cmd = str((payload.get("tool_input") or {}).get("command") or "")
        if not cmd:
            return 0
        cmd = _strip_heredocs(cmd)
        for pattern, what in BLOCKED:
            if re.search(pattern, cmd):
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": MESSAGE.format(what=what),
                    }
                }))
                return 0
    except Exception:                                    # noqa: BLE001
        return 0                                         # fail open
    return 0


if __name__ == "__main__":
    sys.exit(main())
