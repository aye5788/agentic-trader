# Operator Manual — what *you* (Aaron) actually do

Plain, step-by-step instructions for every human-in-the-loop task. The system runs
itself on cron; this is the short list of things that need *you*. Keep this handy.

> **How to run these commands:** SSH into the droplet, then `cd /opt/agentic-trader`
> first. Commands that pull data use the repo venv: `.venv/bin/python …`. The **one
> exception** is anything moomoo (it needs system python: `/usr/bin/python3 …`).

---

## 0a. WHAT ACTUALLY RUNS, AND WHEN  (changed 2026-08-12)

Two kinds of thing trade this book now. They are different and it matters.

**The legacy loops — a script the agent follows.** Unchanged, running for months:

| Time (ET) | What |
| --- | --- |
| 10:00 | `run_fast_loop.sh` — buys/sells the difference between the stored book and what you hold |
| 12:00, 15:45 | `run_risk_review.sh` — intraday de-risk overlay, **armed**, places real trades |
| 16:15 | equity logged for the dashboard curve |
| 18:00 (Sun 20:00) | `run_slow_loop.sh` — rebuilds the target book |
| always | `agentic-monitor.service` — watches stops/targets every 15s during RTH |

**The agent sessions — the agent DECIDES.** New, live since 2026-08-12:

| Time (ET) | What |
| --- | --- |
| 10:35 | `run_session.sh open` |
| 15:15 | `run_session.sh close` |

A session is not handed a procedure. It gets the charter (`prompts/charter.md`,
rendered live from config), its tools, and its own judgment about what this book
should hold. That is the whole point of the change — the code defines the field
and the guardrails; the agent plays.

**It is still fenced.** Every order goes through the same gate as everything
else: kill switch, per-order size cap, universe whitelist, `live_approved`. The
gate runs in the harness, so the agent cannot skip it by forgetting.

**Why those odd times.** Not preference — collision avoidance. 10:35 is after
the 10:00 loop finishes (~10:04) so two Claude processes are never writing at
once. 15:15 is *before* risk_review at 15:45 because both write the same
overrides file with no lock, and a session write was measured erasing a
risk_review protective stop.

**There is deliberately no premarket session.** Nothing in this system stops an
order being placed into a closed market, so a 09:00 session could queue a market
order that fills at whatever the opening print turns out to be.

**To stop just the sessions** and leave everything else running:
```
crontab -l | grep -v run_session.sh | crontab -
```
To stop *everything*, use the kill switch below.

---

## 0. The safety switches — know these before anything else

There are **two** stop switches. Which one you want depends on whether the
**market** scares you or the **code** scares you.

**A. Stop BUYING, keep protection (usually the one you want):**
```
cd /opt/agentic-trader
touch research_store/HALT_ENTRIES   # no new buys; stops + take-profits still fire
rm research_store/HALT_ENTRIES      # resume
```
The book stops growing, but the monitor still sells anything that breaches its
stop. Use this when you don't like the market.

**B. Stop EVERYTHING, take the wheel yourself (kill switch):**
```
touch research_store/HALT           # the system places NO order, buy or sell
rm research_store/HALT              # resume
```
Use this when you think the *system* is misbehaving and you don't want it
touching the account at all.

> ⚠️ **`HALT` means your stops stop firing.** The stops in this system are
> software — the monitor process *is* the stop; there is no stop order sitting at
> Robinhood. So while `HALT` exists, **every open position is unprotected and you
> must sell by hand.**
>
> The monitor knows this and will not go quiet on you: it keeps watching and
> phones you on every breach with "MANUAL EXIT NEEDED … UNPROTECTED", repeating
> until you act. But nothing will sell for you. If you only wanted to stop
> *buying*, use `HALT_ENTRIES` instead.

The drawdown halt (account >25% below its peak) behaves like `HALT_ENTRIES`: it
blocks new buys and never blocks an exit.

**Disarm live order-placement (softer than HALT):** live trading is gated by a
master switch. In `config/strategy.local.toml`:
```
[proof]
live_approved = false      # fast loop will ALERT but place NO orders
```
Set it back to `true` to re-arm. When `live_approved = false`, the intraday
risk-review overlay is also forced to alert-only automatically.

---

## 1. There is no weekly credential chore any more

This used to be your #1 recurring job: Schwab's OAuth token expired every 7 days
and the price feed died if you missed it. **Schwab was removed on 2026-07-29** and
the market feed is now moomoo via the local OpenD gateway, which needs no
scheduled re-auth from you. `scripts/schwab_auth.py` and `schwab_status.py` no
longer exist — if you find a reference to either anywhere, it is stale.

**You now have no recurring credential task at all.** Every remaining health check
asks "did this job leave evidence it ran", not "is a token about to expire".

What replaced the failure mode: if the feed does break, you learn from the daily
08:00 health check (`scripts/health_check.py`, pushed to the OPS ntfy topic) or
from the monitor's "feed down — stops unwatched" alert, rather than from a
calendar. See §5 for what the alerts mean.

The one thing worth knowing: moomoo data flows through **OpenD** on
`127.0.0.1:11111`, which is **shared with the sibling repo `moomoo-vol-desk`**.
Never start a second one. If prices go stale, check OpenD is up before anything
else: `systemctl status opend`.

---

## 2. WEEKLY — review & (maybe) apply the stop-loss proposal

Every Monday the off-box learner proposes a value for `stop_atr_mult` (how far your
stops sit). It **never changes anything on its own** — you decide.

**Where to look:** github.com/aye5788/agentic-trader → **Actions** tab →
**adaptive-tune** → the latest run → read the summary (plain English). Or on the
droplet:
```
cd /opt/agentic-trader
.venv/bin/python scripts/promote_proposal.py
```

**How to read it:** most weeks it says *"Keep your current setting — no change."*
Do nothing. If it says *"Consider moving your stop X → Y,"* only accept it if **all**
of these hold:
- it actually recommends a move,
- `evidence` shows some **live** trades backing it (not `live_n: 0` — pure history),
- **overfitting** reads "looks solid" (not "CAUTION"),
- the winner clearly beats the incumbent (not a flat curve),
- lean toward accepting a move that **widens** stops more than one that tightens.

**To apply your decision (on the droplet):**
```
.venv/bin/python scripts/promote_proposal.py --set 3.0     # the value you approved
```
(or `--apply` if you're on the droplet and the proposal file is local). That's it —
it takes effect on the next Sunday rebalance. **To undo:** `--set 2.5` (the default)
or delete `config/strategy.adaptive.toml`.

---

## 3. When your phone buzzes (ntfy alerts) — what each means

**Two topics, on purpose.** Trade alerts (fills, stops, P&L) go to your main ntfy
topic, which lives only on the droplet. **Upkeep reminders** go to a second topic
(`NTFY_TOPIC_OPS`) and carry job names and ages *only* — never a position, price
or dollar figure. That second topic is the one stored as a GitHub secret so the
off-box tuner can reach you: if it ever leaked, the damage is fake reminders, not
read access to your book. Subscribe to both in the ntfy app.

| Alert | Meaning | What to do |
| --- | --- | --- |
| **"MANUAL EXIT NEEDED … UNPROTECTED"** | a stop or target was breached while `research_store/HALT` is active, so nothing was sold | **Act now.** HALT means the machine places no orders; the position has no stop. Sell in Robinhood, or `rm research_store/HALT` to hand exits back to the monitor. |
| **"stop-loss proposal waiting"** | the weekly tuner recommends a *change* | Do §2. Silent on "keep current setting" weeks. |
| **"weekly tuner FAILED"** | the off-box tuner errored | Usually the `LEDGER_TOKEN` PAT expired → §4. Nothing unsafe; it just stops learning. |
| **"<job> — NEVER RAN / STALE"** | a scheduled job stopped leaving evidence | "NEVER RAN" = probably not scheduled, check `crontab -l`. "STALE" = it ran before and stopped; check that job's log. |
| **cron failure** (ERR-trap) | a scheduled job errored | Check the log it names in `logs/`. Most often OpenD is down or logged out — `systemctl status opend`. |
| **"feed down — stops unwatched"** | the intraday monitor can't get quotes | The moomoo feed is down — check OpenD (`systemctl status opend`; it's shared with `moomoo-vol-desk`). Until fixed your stops aren't auto-watched; eyeball positions if you care. |
| **"ledger backup FAILED"** | off-box backup push failed | Usually transient (network). If it repeats, check the box has push access to `agentic-trader-ledger`. |
| **"signal panel gap"** | the moomoo panel couldn't collect | OpenD (moomoo) likely logged out — see §4. Non-urgent: it just skips that week's data. |

(No alert at all is normal — the system is quiet when healthy. Settlement/buying-power
skips are by design and don't alert.)

**Each condition alerts once.** It fires when it first goes bad and then stays
quiet, even if it stays bad; fixing it clears the flag silently, so the next
occurrence is audible again. That keeps the channel rare enough to be worth
reading. The trade-off is that a missed buzz is a missed message — which is why
the dashboard's **"Scheduled jobs"** card exists: it shows what is *true right
now*, so anything unresolved stays visible long after its notification is gone.
Push = something changed. Dashboard = current state.

---

## 4. Occasional tasks

**Gap-fill the price cache** (needed if prices look stale after a feed outage):
```
cd /opt/agentic-trader
/usr/bin/python3 scripts/fetch_prices.py --backfill 10    # system python3, NOT .venv
```
⚠️ There is no `--force` any more, and no way to re-pull the whole panel: moomoo
caps history at **100 distinct stocks account-wide**, so a full 168-name re-pull
cannot succeed. The panel is **appended to, never rebuilt**, which makes the deep
Schwab-era history on disk **non-regenerable** — that is why
`research_store/prices/backup/` exists. `--backfill N` fills the last N sessions
through the metered history API; keep N small.

**moomoo / OpenD re-login** (if you got a "signal panel gap" alert): the moomoo
session is shared with the `moomoo-vol-desk` project and needs a one-time SMS code
when it logs out. This lives in that project's setup — re-run its OpenD login
(`~/moomoo-vol-desk` SETUP). Verify with:
```
/usr/bin/python3 -c "import sys; sys.path.insert(0,'src'); from adapters.moomoo.client import quote_ctx; c=quote_ctx(); print(c.get_market_snapshot(['US.AAPL'])[0]); c.close()"
```
(prints `RET_OK` = 0 when the data channel is up).

**Subscribe your phone to the upkeep topic** (one-time). The reminder channel is a
second ntfy topic, separate from your trade alerts. Read its value on the box —
it's a secret, so retrieve it yourself rather than having it pasted into a chat:
```
grep NTFY_TOPIC_OPS /opt/agentic-trader/.env
```
Then in the ntfy app: **+ → Subscribe to topic →** paste that value. You'll now
have two subscriptions: trades (existing) and upkeep (new).

**Store the upkeep topic as a GitHub secret** (one-time). This is how the off-box
weekly tuner reaches your phone — it runs on GitHub's runners and cannot see your
`.env`. github.com/aye5788/agentic-trader → Settings → Secrets and variables →
Actions → **New repository secret**, name `NTFY_TOPIC_OPS`, value = the same
string. ⚠️ Never store `NTFY_TOPIC` (your trade-alert topic) in GitHub — that's
the whole point of having two: a leak of the upkeep topic costs you fake
reminders, not visibility into your positions.

**Renew the GitHub Actions token** (when the `LEDGER_TOKEN` PAT expires — GitHub
emails you): github.com → Settings → Developer settings → Fine-grained tokens →
regenerate the `actions-read-ledger` token (repo access = **agentic-trader-ledger**,
Contents = Read). Then re-save it as the **`LEDGER_TOKEN`** repo secret on
`agentic-trader` (Settings → Secrets → Actions).

---

## 5. When the system files an issue

The system watches itself, off-box, and when something looks wrong it files a
GitHub issue — you don't have to be in a chat session for a problem to surface.
You'll see this on github.com/aye5788/agentic-trader → **Issues**.

**The two issue titles you'll see, and what each means:**

| Issue title | Filed by | Means |
| --- | --- | --- |
| **"🔴 Repo-state check failed"** | `validate.yml` (`checks` job, runs daily 09:00 ET) | A static read of the repo found a config/CI defect that would otherwise fail silently — e.g. a cron line that would quietly do nothing, a job documented as scheduled but never armed, a workflow step that could report "green" on a real failure, or a stray key accidentally checked in. |
| **"🔴 Droplet dead-man's switch tripped"** | `validate.yml` (`deadman` job, same daily run) | The check can no longer confirm the droplet is alive (it watches how recently the ledger backup was pushed). This is the **one** alarm that still works even if the box itself is completely dead — every other alert in this manual runs *on* the droplet and can't report its own death. |

You may also see **"🔴 Scheduled job unhealthy"** — that's the existing daily
08:00 upkeep check (§3 above) now also filing an issue for the same conditions
it already pushes to your phone, instead of only pushing.

**What happens next — Claude may open a pull request.** A second automation
(`claude.yml`) watches for these issues and, when one appears, reads it and
decides whether it's worth a fix:

- **Most of the time there's nothing to fix in code.** The moomoo OpenD gateway
  logged out, a cron line never actually added to the
  box, an expired GitHub token, the droplet being down — these are all things
  *you* fix on the droplet (this manual tells you how), not bugs in the code.
  When that's the case, Claude just leaves a plain-language **comment** on the
  issue explaining what's wrong and what to do, and opens **no pull request**.
  That is the normal, healthy outcome, not a failure of the system.
- **When it genuinely is a code defect**, Claude opens a **pull request** —
  a proposed change for you to look at and decide on, never something that
  applies itself. The PR is written **for you, not a programmer**:
  - a plain-language explanation of what broke and why it matters,
  - a plain-language explanation of what the fix does,
  - what its own adversarial self-review found (it deliberately tries to
    poke holes in its own fix before showing it to you — read this section;
    it's the most important one),
  - the actual commands it ran and their actual output (never a bare claim
    that something "passed"),
  - an explicit **"what this does NOT fix"** — anything still broken or
    anything it couldn't verify from here,
  - and one final line telling you plainly whether it thinks this is
    **"safe to merge"** or **"needs your judgement."**
- **You always review and merge — nothing merges itself.** Read the PR body
  (it's written so you don't need to read the code), and either merge it,
  ask a follow-up (comment `@claude ...` on the PR — it will respond), or
  close it. Branch protection on `main` (already enabled, see setup below) is
  what backs this up mechanically: **the agent cannot merge its own pull
  request**, because merging needs one approving review from someone other
  than the author. Be precise about what that rule does and does not do — it
  blocks the *agent's* path to `main`; it does **not** stop you (or the
  droplet's own scripts) pushing to `main` directly, which is deliberate, so
  ordinary maintenance is not locked out.

**One-time setup — until you do this, the loop is INERT.** Issues still get
filed exactly as described above either way; the only thing missing without
this setup is the pull request:

1. **Install the Claude GitHub App** on this repo — either run
   `/install-github-app` inside Claude Code, or visit
   github.com/apps/claude and install it on `aye5788/agentic-trader`.
2. **Create a subscription-billed token** (not a per-token API key):
   - anywhere you have Claude Code: run `claude setup-token` and copy the
     token it prints;
   - on github.com → this repo → **Settings → Secrets and variables →
     Actions → New repository secret** → name it exactly
     `CLAUDE_CODE_OAUTH_TOKEN` → paste the value.
   - ⚠️ Never store an `ANTHROPIC_API_KEY` here or anywhere — that silently
     switches billing from your subscription to per-token API use.
3. **Branch protection on `main` — ALREADY ENABLED** (done for you). What is
   actually set, so you can check it at github.com → this repo →
   **Settings → Branches**:
   - a pull request is required before merging, **and** it needs
     **1 approving review**. The review requirement is the part that matters:
     a PR-only rule would still let the agent open a PR and merge it itself.
     With this, an agent's PR sits until *you* approve it.
   - **"Do not allow bypassing" is deliberately OFF** (`enforce_admins:false`),
     so you as owner — and the droplet's own scripts — can still push to
     `main` directly. This is not a loophole in the agent's path: the agent
     runs as GitHub Actions, not as you.
   So: nothing the agent proposes can land without you. It does *not* mean
   `main` is frozen.
4. **Arm the daily upkeep check to file issues — ALREADY DONE** (done for you).
   The droplet's 08:00 job runs with `--open-issue`, which is what turns an
   unhealthy scheduled job into a GitHub issue the loop above can act on. To
   confirm on the box: `crontab -l | grep health_check` should show
   `scripts/health_check.py --open-issue`. Without that flag the check still
   texts you, but files nothing and so never draws a PR.

---

## 6. Checking that everything's healthy

- **Market feed:** `systemctl status opend` — OpenD is the moomoo gateway and the
  single point of failure for prices and intraday quotes. Shared with
  `moomoo-vol-desk`; never start a second instance.
- **Dashboard** (portfolio, equity curve): **dash.ethobs.uk** (login = `DASH_USER`/
  `DASH_PASS` from `.env`). Locally: `.venv/bin/python dashboard/app.py` → 127.0.0.1:8787.
- **Recent cron activity:** `tail logs/slow.log logs/fast.log logs/signals.log`
- **Adaptive runs:** GitHub → Actions → adaptive-tune.

---

## 7. Emergency: something's wrong, stop everything

**First decide which of these you actually mean:**

- *"The market looks bad, stop buying"* → `touch research_store/HALT_ENTRIES`.
  Stops and take-profits keep working. This is the safe default.
- *"The system is misbehaving, hands off my account"* → `touch research_store/HALT`.
  **This also switches off your stops** — see below.

1. **Freeze:** `touch /opt/agentic-trader/research_store/HALT_ENTRIES` (or
   `HALT` for a full stop).
2. If you want it disarmed longer-term: set `[proof] live_approved = false` in
   `config/strategy.local.toml`.
3. Nothing will place orders until you remove the file (and re-arm
   `live_approved = true`).
4. ⚠️ **If you used `HALT`: your open positions now have no stop.** The monitor
   will keep phoning you on each breach ("MANUAL EXIT NEEDED"), but you must sell
   in Robinhood yourself. To hand exits back to the monitor:
   `rm research_store/HALT`.

---

## Cheat sheet

```
STOP BUYING             touch research_store/HALT_ENTRIES  (stops STILL fire)
STOP EVERYTHING         touch research_store/HALT          (⚠ stops STOP firing —
                                                            sell by hand; rm to resume)
Review stop proposal    .venv/bin/python scripts/promote_proposal.py
Apply a stop value      .venv/bin/python scripts/promote_proposal.py --set 2.5
Gap-fill prices         /usr/bin/python3 scripts/fetch_prices.py --backfill 10
Check the market feed   systemctl status opend
Dashboard               dash.ethobs.uk
Adaptive proposals      github.com/aye5788/agentic-trader  → Actions → adaptive-tune
```

*Deeper detail: `CLAUDE.md` (system overview), `docs/DESIGN.md` (architecture),
`docs/DATA_SOURCES.md` (data), `docs/STRATEGY.md` (the trading strategy),
`docs/DATA_SOURCES.md` (what each feed provides).*
