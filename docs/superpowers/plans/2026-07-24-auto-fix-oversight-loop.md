# Auto-fix oversight loop — implementation plan

**Date:** 2026-07-24
**Goal:** Replicate the `~/moomoo-data-collector` auto-fix loop in agentic-trader, so
system defects surface and get a proposed fix **without Aaron having to discover them
by accident in a chat session**.

## Why (read this before touching anything)

Today's session found ~7 code defects (workflow `exit 0` masking an expired token as a
green run; `OpenQuoteContext` hanging forever on a dead gateway; no deadline on a wedged
gateway; "not checked" mis-reported as "NEVER RAN"; wrong remedy text for never-vs-stale;
"all clear" printed while unresolved; two cron lines missing a `cd` prefix). Every one
surfaced because Aaron happened to ask. That is luck, not oversight, and re-aligning
context each time costs him hours.

**Aaron is not a coder.** A detector that only reports failures shifts work onto him and
is therefore not the deliverable. The agent half — Claude proposing a **PR he reviews and
merges** — is the point of this project, not an optional extra.

## Architecture (mirrors the sibling repo, adapted)

```
detector  ──files a deduped `bug`+`auto-fix` issue──▶  claude.yml  ──▶  PR  ──▶  Aaron merges
   │                                                      (agent proposes; never pushes to main)
   ├── validate.yml     off-box repo-state checks + droplet dead-man's switch
   └── health_check.py  on-box daily upkeep check (already exists)
```

## Global Constraints (bind every task)

- **LIVE TRADING REPO.** Follow `CLAUDE.md` hard rules verbatim. Never anchor on account
  balance; judge risk on mechanism and consequence.
- Nothing in this project may place, modify, or cancel an order, or add broker access to CI.
- Never print/commit `.env` or `secrets/` contents.
- Claude **proposes PRs only** — never pushes to `main`, never auto-merges.
- Auth is `CLAUDE_CODE_OAUTH_TOKEN` (subscription). **Never introduce `ANTHROPIC_API_KEY`**
  anywhere — it silently switches to per-token API billing (CLAUDE.md footgun).
- Every new Python entrypoint exposes `--selftest` and is wired into
  `deploy/run_selftests.sh`. Style-match existing selftests (`src/health.py`).
- Issue filing must be **deduped by title** — comment on an open issue rather than
  spawning a new one per run.
- `/opt/agentic-trader` is the live deploy; work on `main`.
- The two-runtime split is real: anything importing `moomoo` needs system python3.10 and
  is NOT importable in `.venv` or on a CI runner.

---

## Task 1 — `src/repo_checks.py`: static repo-state validator (pure + selftest)

**Files:** create `src/repo_checks.py`; modify `deploy/run_selftests.sh`

Pure, filesystem-only checks over repo files. No network, no secrets, no droplet. Each
check returns a list of human-readable failure strings (empty = pass).

Implement `checks(root: Path) -> list[str]` composed of these, each its own function:

1. **`check_cron_paths`** — every non-comment command line in `deploy/crontab.template`
   must either start its command with an absolute path or contain `cd /opt/agentic-trader`.
   *Catches: the two `cd`-prefix bugs introduced today, where cron's cwd is `/root` and a
   relative path silently fails.*
2. **`check_scheduled_jobs_armed`** — every key in `health.SPECS` that names a cron-run job
   must appear in `deploy/crontab.template`. Import `health` and read `SPECS`; use a
   module-level `dict` mapping SPECS key -> expected substring (e.g. `signal_panel` ->
   `collect_signals.py`). Keys with no cron line (e.g. `monitor` is a systemd service,
   `adaptive_tune` is GitHub Actions) go in an explicit `NOT_CRON` set with a comment.
   *Catches: the signal panel documented as scheduled but never armed.*
3. **`check_workflow_failure_exits`** — no `.github/workflows/*.yml` may contain `exit 0`
   on a line inside a block that also mentions `::error::` or `FAILED`/`failed`. Simple
   heuristic: flag any `exit 0` appearing within 3 lines after a line containing
   `::error::`. *Catches: the expired-token-shows-green bug.*
4. **`check_no_api_key`** — no tracked file may contain the literal `ANTHROPIC_API_KEY=`
   with a non-empty value, and `deploy/crontab.template` must not mention it except in a
   warning comment. *Catches: the per-token billing footgun.*

`main()`: run all checks, print each failure, `sys.exit(1)` if any failed, else print a
pass line and exit 0. Add `--selftest` that builds temporary fixture dirs (use
`tempfile.TemporaryDirectory`) proving **each check both passes clean input and fails
dirty input** — a detector that has never been seen to fail is not proven.

Add `src/repo_checks.py` to the `SELFTESTS` array in `deploy/run_selftests.sh`.

**Verify:** `.venv/bin/python src/repo_checks.py --selftest` passes;
`.venv/bin/python src/repo_checks.py` run against the real repo exits 0 (the repo is
currently clean — if a check fires, that is a real finding: report it, do not weaken the
check to make it pass).

---

## Task 2 — `.github/workflows/validate.yml`: off-box detector + dead-man's switch

**Files:** create `.github/workflows/validate.yml`

Two jobs of monitoring in one workflow, modelled on
`~/moomoo-data-collector/.github/workflows/validate.yml` (read it first).

Triggers: `schedule` daily at `0 13 * * *` (09:00 ET, after the 08:00 on-box health
check), `push` to `main` touching `src/repo_checks.py` or the workflow itself, and
`workflow_dispatch`.

`permissions: contents: read, issues: write`.

Steps:
1. checkout; setup-python 3.12; `pip install --quiet pandas numpy pyarrow python-dotenv`
2. **Repo-state check:** run `python3 src/repo_checks.py`, `continue-on-error: true`,
   `id: checks`, teeing output to `checks_output.txt`.
3. **Droplet dead-man's switch:** clone the ledger mirror using `secrets.LEDGER_TOKEN`
   (same pattern as `adaptive-tune.yml`, including the whitespace-strip on the token) and
   fail if its most recent commit is older than **72 hours**. The droplet pushes to the
   mirror on every slow/fast loop and nightly at 22:30, so a mirror that stops moving means
   the box is dead or its cron is. *This is the only check in the system that survives the
   droplet dying — `health_check.py` runs ON the droplet and cannot report its own death.*
   If `LEDGER_TOKEN` is empty, `::error::` and fail (do NOT `exit 0` — see Task 1 check 3).
   Tee output to `deadman_output.txt`.
4. **Open/update issue on failure:** if either step failed, `gh label create` both labels
   (idempotent, `|| true`), build a body from the captured outputs, dedupe by searching
   open issues for the exact title `🔴 Repo/deploy check failed`, and `gh issue comment`
   if found else `gh issue create --label bug --label auto-fix`. Include the standard
   note that ops failures have no code fix.
5. Final step: fail the run if either check failed.

**Verify:** `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/validate.yml'))"`
parses. Do not trigger the workflow (Task 4 handles live verification).

---

## Task 3 — `health_check.py --open-issue`: route on-box findings into the loop

**Files:** modify `scripts/health_check.py`

Add `--open-issue`. When set AND there are newly-alerting conditions, additionally file a
deduped GitHub issue (title exactly `🔴 Scheduled job unhealthy`) labelled `bug` +
`auto-fix`, using the box's already-authenticated `gh` CLI via `subprocess` — **no new
credential**. Body lists each condition (`label`, `status`, `detail`) plus its remedy
string, and carries the ops-vs-code note.

Requirements:
- Wrap all `gh` calls so a failure to file **never** breaks the check or the phone push —
  print a diagnostic and continue. The push is the primary channel; the issue is a bonus.
- Respect `--dry`: print what it would file, file nothing.
- Reuse the existing fire-once `flagged` state — do not file on every run for the same
  condition. Filing follows exactly the same trigger as the push (`to_alert` non-empty).
- Extend `_selftest` to cover body composition (pure string building) without invoking
  `gh`: assert the body names each condition and contains the ops-vs-code note, and that
  it never contains a `$` dollar figure.
- Update the module docstring usage block.
- **Do not** change the cron line in this task.

**Verify:** `.venv/bin/python scripts/health_check.py --selftest` passes;
`.venv/bin/python scripts/health_check.py --dry --open-issue` prints the would-file body
and files nothing; `deploy/run_selftests.sh` green.

---

## Task 4 — `.github/workflows/claude.yml`: the agent half

**Files:** create `.github/workflows/claude.yml`

Read `~/moomoo-data-collector/.github/workflows/claude.yml` first and mirror its
structure — including the `concurrency` group, and the `if:` that fires exactly once on
whichever of `bug`/`auto-fix` is applied **second** (order-independent), plus the
`interactive` job for `@claude` mentions.

Adapt the prompt for THIS repo. It must instruct the agent to:
- Read `CLAUDE.md` first and follow its hard rules verbatim.
- Treat this as live-money code; never use account size to discount a concern.
- **Refuse to modify** `config/strategy.toml`, `src/governance.py`, `scripts/fast_loop.py`,
  or `prompts/*.md` without an explicit human instruction in the issue — instead comment
  what it would change and why, and open no PR.
- Never reference `.env`/`secrets/` contents; if reproduction needs a credential, say it
  cannot reproduce.
- Recognise that most failures here are **operational** (Schwab token, OpenD down, cron not
  armed, expired PAT, droplet down) with **no code fix** — comment and open no PR.
- If it is a code bug: minimal diff, add/extend a `--selftest`, run the covering selftest
  and report the actual output (never claim a pass without it), note that `moomoo` is
  unimportable on the runner, open a PR against `main`, never push to `main`.

`claude_args`: `--max-turns 30` plus an allowlist covering Edit, Write, Read, Glob, Grep,
and scoped Bash (`python3`, `pip`, `grep`, `rg`, `ls`, `cat`, `head`, `tail`, `git`, `gh`).
Include a header comment documenting the one-time setup (install the Claude GitHub App,
`claude setup-token` -> `CLAUDE_CODE_OAUTH_TOKEN` secret, enable branch protection) and
stating that the workflow is inert until the secret exists.

**Verify:** YAML parses. Note in the report that live verification needs the one-time
human setup steps.

---

## Task 5 — Docs + cron wiring

**Files:** modify `deploy/crontab.template`, `CLAUDE.md`, `docs/OPERATOR_MANUAL.md`,
`docs/DEPLOY.md`

1. `deploy/crontab.template`: add `--open-issue` to the existing daily `health_check.py`
   line. (The live crontab is updated separately by the controller — this task only edits
   the template.)
2. `CLAUDE.md` repo-layout: add `src/repo_checks.py` and both new workflows, each with a
   one-line purpose.
3. `docs/OPERATOR_MANUAL.md`: new section **"§5 When the system files an issue"** — what
   the two issue titles mean, that Claude may open a PR, that Aaron reviews and merges,
   that ops failures get a comment instead of a PR, and the one-time setup checklist
   (GitHub App, `claude setup-token` secret, branch protection). Renumber the existing
   trailing sections if needed.
4. `docs/DEPLOY.md`: document the two new workflows alongside `adaptive-tune`.

**Verify:** `deploy/run_selftests.sh` green; `src/repo_checks.py` still exits 0 against
the repo (the crontab.template edit must not trip `check_cron_paths`).
