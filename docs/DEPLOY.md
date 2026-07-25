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
```

**Scheduling — ⚠️ do NOT run `crontab deploy/crontab.template` on the live box.**
That is the *first-install* command only. The live crontab has since diverged: it
carries box-only jobs the template does **not** contain (the `moomoo-vol-desk`
09:30/09:35 options runs, the `moomoo-data-collector` 16:30 update), and a
wholesale install would silently wipe them.

On an already-deployed box, arm a new job by **appending the single line**, and
verify nothing was lost:

```bash
crontab -l > /tmp/crontab.backup            # always back up first
crontab -l > /tmp/crontab.new
# ...append the new line to /tmp/crontab.new with an editor...
crontab /tmp/crontab.new
diff /tmp/crontab.backup <(crontab -l)      # MUST show only your addition
```

The corollary that has bitten us twice: **editing `deploy/crontab.template` arms
nothing.** The template is documentation of intent; the box is the source of
truth. Any plan step that says "schedule X" is not done until the line is in
`crontab -l`. (2026-07-20 universe refresh, 2026-07-24 signal panel.)

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

## Dashboard (Cloudflare Tunnel + in-app auth)

A read-only monitor of the live account. **Live at `dash.ethobs.uk`.** Served by
Flask on the droplet (127.0.0.1:8787 only), fronted by a Cloudflare Tunnel, and
**password-gated by the app itself** (not Cloudflare Access — see the note).

```bash
# 1. dashboard service (Flask, binds 127.0.0.1:8787 only)
.venv/bin/pip install -r requirements.txt          # picks up flask
# set the password (fail-CLOSED: no DASH_PASS -> app serves 503, never public):
#   add to .env:   DASH_USER=<you>   DASH_PASS=<strong password>
cp deploy/agentic-dashboard.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now agentic-dashboard
curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:8787   # want 401 (gated). NOT localhost — see gotchas.

# 2. Cloudflare Tunnel — hides the droplet IP, free TLS, on the domain
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared
cloudflared tunnel login                            # browser: pick the domain (ethobs.uk)
cloudflared tunnel create agentic-dash
cloudflared tunnel route dns agentic-dash dash.ethobs.uk
# config /root/.cloudflared/config.yml (service install copies it to /etc/cloudflared/config.yml):
#   tunnel: <tunnel-id>
#   credentials-file: /root/.cloudflared/<tunnel-id>.json
#   ingress:
#     - hostname: dash.ethobs.uk
#       service: http://127.0.0.1:8787
#     - service: http_status:404
cloudflared service install && systemctl enable --now cloudflared
```

**Auth note (why NOT Cloudflare Access):** we tried Cloudflare Access first; it
would not reliably gate the tunnel hostname (page loaded with no login, even in
incognito). We switched to **in-app HTTP Basic Auth** (`dashboard/app.py`,
`DASH_USER`/`DASH_PASS` from `.env`, constant-time, fail-closed). Basic auth runs
over the tunnel's HTTPS so creds are encrypted. Access can be added later as a
second layer, but the app-level password is the load-bearing gate — do not remove
`DASH_PASS` thinking Access covers it.

The equity curve fills from `scripts/log_equity.py`, which `run_fast_loop.sh` runs
each day — one point per trading day from first run.

## GitHub Actions (off-box automation)

Three workflows run entirely on GitHub's runners, never the droplet — the box
only ever *feeds* them (the ledger mirror push) or is *checked on* by them:

| Workflow | Trigger | What it does | Secrets |
| -------- | ------- | ------------ | ------- |
| `adaptive-tune.yml` | Mondays 08:00 UTC + manual | Off-box weekly learner for `stop_atr_mult` — reads price + ledger data from the mirror, surfaces a bounded PROPOSAL. Never trades, never writes config. See `docs/OPERATOR_MANUAL.md` §2. | `LEDGER_TOKEN`; optional `NTFY_TOPIC_OPS` |
| `validate.yml` | Daily `0 13 * * *` (09:00 ET, after the droplet's own 08:00 check) + push touching `src/repo_checks.py`/itself + manual | TWO independent jobs. `deadman`: droplet dead-man's switch — fails if the ledger mirror hasn't been pushed to in 72h (the one check that survives the droplet dying; `scripts/health_check.py` runs *on* the droplet and can't report its own death). `checks`: runs `src/repo_checks.py`, the static filesystem-only config/CI validator. Each job files/updates its own deduped `bug`+`auto-fix` GitHub issue; `deadman` also phones `NTFY_TOPIC_OPS`. | `LEDGER_TOKEN` (same PAT as adaptive-tune.yml); optional `NTFY_TOPIC_OPS` |
| `claude.yml` | An issue gets both `bug` and `auto-fix` labels (fires once, whichever lands second) — the droplet's path; **or** a `repository_dispatch` of type `auto-fix` carrying an issue number — the path `validate.yml` must use, because GitHub does not create workflow runs from events triggered by the automatic `GITHUB_TOKEN` (`workflow_dispatch`/`repository_dispatch` are the two documented exceptions). Both entrances run the SAME job. Or a `@claude` mention on an issue/PR | The agent half of the oversight loop the two above feed. Reads the issue, decides operational-vs-code-defect, and — only for a genuine code bug — implements the minimal fix, runs a **mandatory adversarial self-review** (a second pass whose job is to falsify its own fix), re-verifies with real command output, and opens a pull request against `main` written for a non-coder (never pushes to `main` itself). Operational findings get a plain-language comment and no PR. See `docs/OPERATOR_MANUAL.md` §5 for what the PR looks like. | `CLAUDE_CODE_OAUTH_TOKEN` — **without it the workflow is INERT**: issues still get filed by the two workflows above exactly as before, they just draw no PR |

**One-time setup for `validate.yml` + `claude.yml`** (`adaptive-tune.yml`'s
`LEDGER_TOKEN` should already exist from its own setup):

1. `LEDGER_TOKEN` — reuse the same fine-grained PAT `adaptive-tune.yml` uses
   (read-only Contents on the private `agentic-trader-ledger` mirror); no new
   secret needed for `validate.yml`'s dead-man's switch.
2. `CLAUDE_CODE_OAUTH_TOKEN` — install the Claude GitHub App on this repo,
   run `claude setup-token`, and save the printed token as this repo secret.
   Never set `ANTHROPIC_API_KEY` anywhere — it silently switches billing from
   the subscription to per-token API use (`src/repo_checks.py`'s
   `check_no_api_key` flags a real assignment of it).
3. Branch protection on `main` — **enabled**, with
   `required_approving_review_count: 1` and `enforce_admins: false`. The
   *review* requirement is the guarantee that nothing `claude.yml` proposes can
   land unreviewed: a PR-required rule alone would not stop it, since the
   workflow holds `contents: write` + `pull-requests: write` and could merge its
   own PR. `enforce_admins: false` leaves the owner and the droplet able to push
   to `main` directly — the rule constrains the Actions token's path to `main`,
   not the human's.
4. Optional but wired: `NTFY_TOPIC_OPS` — `validate.yml`'s two jobs and
   `claude.yml`'s auto-fix job all push to it when they trip or fail. Unset →
   those pushes are skipped silently and the GitHub issue is the only channel.

Full plain-language walkthrough of what an issue/PR from this loop looks like:
`docs/OPERATOR_MANUAL.md` §5, "When the system files an issue."

## Operations & troubleshooting (for a future debugger)

**Health check — is each piece alive?**
```bash
systemctl status agentic-monitor agentic-dashboard cloudflared   # all: active (running)
crontab -l                                    # 10 agentic-trader lines (+3 box-only, see below)
# slow (Sun 20:00, M-F 18:00) · signal panel (Sun 20:15) · fast (M-F 10:00)
# risk review (M-F 12:00 + 15:45) · letter (Sun 21:00) · upkeep check (daily 8:00)
# ledger backup (daily 22:30) · universe refresh (quarterly, 1st Sun 19:00)
# box-only, NOT this project: moomoo-vol-desk 9:30/9:35, data-collector 16:30
timedatectl                                   # MUST be America/New_York or cron fires at wrong times
cat research_store/monitor/state.json         # {"book_asof": <date>, "fired": {}} = monitor polled OK
tail logs/slow.log logs/fast.log              # last loop runs
journalctl -u agentic-monitor -n 50           # monitor: silent unless a stop/target tripped (or market closed)
```

**Gotchas we actually hit (check these first):**
- **Timezone:** the box must be `America/New_York`. On UTC, cron's `10:00` fast
  loop fires 6am ET (pre-market) and RH rejects the fractional orders. Fix:
  `timedatectl set-timezone America/New_York && systemctl restart cron`.
- **`curl localhost:8787` returns `000`** — Flask binds IPv4 `127.0.0.1`; `localhost`
  resolves to IPv6 `::1`. Use `127.0.0.1` explicitly. (The tunnel uses 127.0.0.1, unaffected.)
- **The live switch is box-local, not in git.** The committed
  `config/strategy.toml` ships SAFE (`live_approved=false`); a fresh clone can
  never trade. The droplet is armed by the git-ignored
  `config/strategy.local.toml` (`[proof] live_approved = true`), which
  `src/strategy.py` deep-merges over the base. Pause new buys: edit the local
  file, or delete it to fully disarm. `git pull` never conflicts on this.
- **Schwab price feed dies weekly** — the 7-day OAuth token. Re-run
  `scripts/schwab_auth.py` (paste flow). If the slow loop errors on quotes/history, this is why.
- **RH blocks the 2nd trade** — one-time "investor profile" KYC on the Agentic
  account; complete it in the RH app. Non-recurring.
- **No native stop orders** — RH rejects stops on sub-1-share fractional positions
  (verified against the order API). That's *why* the software monitor exists; don't
  try to place broker stops.
- **Terminal paste mangling** — the droplet's paste indents lines / splits long
  redirects (broke heredocs, printf, and nano YAML). Prefer short single-line
  commands or type into an editor; fix stray YAML indent with `sed '2,$ s/^  //'`.

**Kill switch:** `touch research_store/HALT` stops the monitor and fast loop
instantly; `rm` resumes. **Pause new buys:** set `live_approved=false` in
`config/strategy.local.toml` (or delete that file to fully disarm).

## What is NOT yet automated (know before unattended live)

- **Nightly *exits-only* mode** — the slow loop still re-ranks fully each run,
  but since 2026-07-09 the banded holds engage (held names kept until below
  rank `book_band`), so nightly runs no longer churn on small rank slips.
- **Schwab weekly re-auth** — inherently manual (see Phase 3). Monday's cron
  now pushes the reminder to the phone (ntfy) as well as the log.

Done since this list was written (2026-07-09): fill reconciliation + realized
P&L journaling (fast_loop/exit prompts reconcile positions and snapshot
`get_realized_pnl` after sells); failure alerting (deploy/alert.sh ERR-traps
every cron wrapper to ntfy; the monitor pushes stop/target triggers, execution
results, and re-entry judgments to the same topic).

Added 2026-07-10 — **trade notifications**: `scripts/record_fills.py` now pushes
a phone summary (ntfy) of every journaled execution — placed fills AND skipped
orders — so rebalance trades are no longer silent (before this, only cron
failures and monitor stop/target breaches pushed; a routine fast-loop rotation
produced no alert at all). Skips are journaled first-class: the fast loop
records review-rejected orders in `fills.json` with `status:"skipped"` +
`reason` — notably `pending_settlement`, the expected one-day deferral when a
buy follows a sell in this cash account (T+1; the leg re-plans next run). The
ntfy sender itself was deduplicated into `src/notify.py`, shared by the monitor
and record_fills (deploy/alert.sh keeps its own shell copy of the contract).
