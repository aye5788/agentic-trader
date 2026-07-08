# DEPLOY.md — putting agentic-trader on the droplet

The full rationale is in [`DESIGN.md`](DESIGN.md) → "Deployment (VPS)". This is
the ordered runbook. Two things run on the box, on different clocks:

| Loop | What runs | Needs Claude? | Cadence |
| ---- | --------- | ------------- | ------- |
| **Slow** | `deploy/run_slow_loop.sh` — fetch prices, rank, write the target book | **No** (pure Python) | weekly rebalance + nightly exits |
| **Fast** | `deploy/run_fast_loop.sh` — read book, fetch live account, place approved plan | **Yes** (RH is MCP-only) | weekday, after open |

Claude only runs to *execute*. All ranking is free Python.

---

## Phase 0 — once, on a machine with a browser (NOT the droplet)

1. `claude setup-token` → a ~1-year `CLAUDE_CODE_OAUTH_TOKEN`. Keeps billing on
   the subscription, not per-token.
2. Robinhood desktop OAuth to connect the **Agentic** account — **already done**
   (the account is live: `agentic_allowed=true`, cash, nickname "Agentic").

## Phase 1 — provision + code

```bash
# Ubuntu droplet
sudo apt update && sudo apt install -y python3.12 python3.12-venv git
# Node (for the Claude Code CLI)
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt install -y nodejs
sudo npm i -g @anthropic-ai/claude-code

sudo git clone <repo> /opt/agentic-trader          # deploy key or PAT (private repo)
cd /opt/agentic-trader
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt           # pandas/numpy/pyarrow now included
```

## Phase 2 — secrets + auth (all git-ignored → copy securely, never commit)

```bash
# From your machine — these are NOT in the repo:
scp .env        droplet:/opt/agentic-trader/.env          # Schwab/Finnhub/Alpaca/FRED keys
scp -r secrets/ droplet:/opt/agentic-trader/secrets/       # Schwab OAuth token store

# On the droplet — subscription auth + the footgun check:
export CLAUDE_CODE_OAUTH_TOKEN=<from phase 0>
unset ANTHROPIC_API_KEY          # MUST be unset everywhere (crontab/.bashrc/.env)

# Robinhood MCP (headless HTTP; works after the one-time desktop OAuth):
claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading
claude -p "call get_accounts and report which are agentic_allowed"   # smoke test
```

## Phase 3 — Schwab login (and the weekly wart)

```bash
.venv/bin/python scripts/schwab_auth.py    # prints a URL; log in, paste redirect back
```

⚠️ **Schwab's refresh token expires every 7 days and cannot be renewed
unattended.** Re-run this weekly or the price feed dies. The crontab logs a loud
reminder every Monday.

## Phase 4 — dry run, THEN schedule

```bash
deploy/run_slow_loop.sh          # writes the target book — inspect research_store/current.json
.venv/bin/python scripts/fast_loop.py   # prints the plan; live_approved=false → places nothing
crontab deploy/crontab.template  # edit the token + paths first
```

## Going live — the deliberate switch

The fast loop **cannot place an order** until you flip the master switch. Nothing
before this touches money:

1. `config/strategy.toml` → `[proof] live_approved = true`.
2. That's the whole gate. Governance still applies every run:
   - **Kill-switch:** `touch research_store/HALT` stops all trading instantly;
     `rm` resumes. (Your panic button.)
   - **Drawdown halt:** auto-stops new buys if the account falls >25% from its
     tracked peak (`[governance] max_drawdown`).
   - **Order cap:** rejects any single order >15% of account value.
   - **Whitelist:** only names in `config/universe.csv` + `etf_universe.csv`.

## What is NOT yet automated (know before unattended live)

- **Nightly *exits-only* mode** — the slow loop currently re-ranks fully each run
  (fine weekly; the nightly stop/MA-exit-only pass is not yet a separate mode).
- **Fill reconciliation / P&L journaling** beyond the basic order log.
- **Alerting** — cron logs to files; no push/email on failure yet.
- **Schwab weekly re-auth** — inherently manual (see Phase 3).
