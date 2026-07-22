#!/usr/bin/env bash
# Off-box backup of the NON-REGENERABLE ledger data to a private git mirror.
# The trade record + outcome labels cannot be rebuilt from code (unlike prices
# and current.json), and research_store/ is git-ignored with no other backup.
# Non-fatal by contract: a failure phone-alerts but never blocks a trading run.
#
# One-time setup on the box:
#   git clone git@github.com:aye5788/agentic-trader-ledger.git "$HOME/agentic-trader-ledger"
#   (or https with a stored credential/token; verify `git -C ... push` works)
set -uo pipefail   # NOT -e: this script must never hard-fail its caller
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
MIRROR="${LEDGER_MIRROR:-$HOME/agentic-trader-ledger}"

fail() {
  MSG="$1" "$REPO_ROOT/.venv/bin/python" -c "import os, sys; sys.path.insert(0, '$REPO_ROOT/src'); from notify import push; push('Agentic: ledger backup FAILED', os.environ['MSG'], tags='floppy_disk')" 2>/dev/null || true
  echo "backup_ledger: $1" >&2
  exit 0
}

[ -d "$MIRROR/.git" ] || fail "mirror not cloned at $MIRROR — run the one-time git clone (see script header)"

# Copy only the non-regenerable files.
mkdir -p "$MIRROR/history" "$MIRROR/archive"
cp -f research_store/journal.jsonl        "$MIRROR/journal.jsonl"        2>/dev/null || true
cp -f research_store/history/equity.jsonl "$MIRROR/history/equity.jsonl" 2>/dev/null || true
cp -f research_store/flows.jsonl          "$MIRROR/flows.jsonl"          2>/dev/null || true
cp -f research_store/archive/*.json       "$MIRROR/archive/"             2>/dev/null || true

cd "$MIRROR" || fail "cannot cd $MIRROR"
git add -A
if git diff --cached --quiet; then
  echo "backup_ledger: no changes"; exit 0
fi
STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
git commit -q -m "ledger backup $STAMP" || fail "git commit failed"
git push -q || fail "git push failed (check credentials/network)"
echo "backup_ledger: pushed $STAMP"
