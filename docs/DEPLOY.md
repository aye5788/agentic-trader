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

## Operations & troubleshooting (for a future debugger)

**Health check — is each piece alive?**
```bash
systemctl status agentic-monitor agentic-dashboard cloudflared   # all: active (running)
crontab -l                                    # 4 jobs: slow (Sun 20:00, M-F 18:00), fast (M-F 10:00), reauth
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
instantly; `rm` resumes. **Pause new buys:** set `[proof] live_approved=false`.

## What is NOT yet automated (know before unattended live)

- **Nightly *exits-only* mode** — the slow loop currently re-ranks fully each run
  (fine weekly; the nightly stop/MA-exit-only pass is not yet a separate mode).
- **Fill reconciliation / P&L journaling** beyond the basic order log.
- **Alerting** — cron logs to files; no push/email on failure yet.
- **Schwab weekly re-auth** — inherently manual (see Phase 3).
