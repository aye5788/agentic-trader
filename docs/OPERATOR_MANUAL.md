# Operator Manual — what *you* (Aaron) actually do

Plain, step-by-step instructions for every human-in-the-loop task. The system runs
itself on cron; this is the short list of things that need *you*. Keep this handy.

> **How to run these commands:** SSH into the droplet, then `cd /opt/agentic-trader`
> first. Commands that pull data use the repo venv: `.venv/bin/python …`. The **one
> exception** is anything moomoo (it needs system python: `/usr/bin/python3 …`).

---

## 0. The safety switches — know these before anything else

**Stop ALL trading instantly (kill switch):**
```
cd /opt/agentic-trader
touch research_store/HALT        # from now on the system trades NOTHING
```
Resume:
```
rm research_store/HALT
```
That's it. While `HALT` exists, no orders are placed by anything. Use it if
something looks wrong and you want to freeze the book.

**Disarm live order-placement (softer than HALT):** live trading is gated by a
master switch. In `config/strategy.local.toml`:
```
[proof]
live_approved = false      # fast loop will ALERT but place NO orders
```
Set it back to `true` to re-arm. When `live_approved = false`, the intraday
risk-review overlay is also forced to alert-only automatically.

---

## 1. WEEKLY — refresh the Schwab login  ⏰ your #1 recurring job

Schwab's token **expires every 7 days**. If you skip it, the price feed dies and
the book goes stale.

**The reminder is now smart, not calendar-based.** A daily 08:00 check reads the
token's *actual* age and phones you when you're inside 3 days of expiry — so it
stays silent on weeks you've already re-authed, and it escalates to "EXPIRED" if
you run out. (It replaced a blind Monday nag that fired regardless and never
followed up.) You'll get **one** push per expiry cycle; the dashboard's
"Scheduled jobs" card shows days-remaining continuously if you want to check
deliberately.

**Do this (in a real SSH terminal, not through chat — the prompt needs to block):**
```
cd /opt/agentic-trader
.venv/bin/python scripts/schwab_auth.py
```
1. It prints an authorization URL. Open it in a browser.
2. Log in → **click through every screen to the final "Allow"** (this matters).
3. The browser lands on a `https://127.0.0.1:8182/?code=…` page that **fails to
   load — that's normal**. Copy the **full** URL from the address bar.
4. Paste it at the `paste the address bar url here:` prompt, hit Enter.
5. You should see `✅ Auth complete`.

**Confirm it worked:**
```
.venv/bin/python scripts/schwab_status.py
```
Expect `Schwab refresh token: OK … (7.0 days left) … live check: OK`.
(Don't judge freshness by the `secrets/tokens.db` file date — WAL mode makes it
lag. Trust `schwab_status.py`.)

**If it fails with `invalid_grant` ("code invalid/expired"):** it's almost always
one of these (in order): (a) you didn't click all the way to the final Allow;
(b) the paste took longer than ~30 seconds — Schwab codes expire that fast, so use
the SSH method above, not the chat `!` two-step; (c) Schwab is having a moment —
wait 30–60 min and retry; (d) check the app is "Ready For Use" at
developer.schwab.com. Full checklist: `README.md` → "Troubleshooting invalid_grant".

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
| **"Schwab token — N days left"** | inside 3 days of expiry | Do §1. Silent if you're current. |
| **"stop-loss proposal waiting"** | the weekly tuner recommends a *change* | Do §2. Silent on "keep current setting" weeks. |
| **"weekly tuner FAILED"** | the off-box tuner errored | Usually the `LEDGER_TOKEN` PAT expired → §4. Nothing unsafe; it just stops learning. |
| **"<job> — NEVER RAN / STALE"** | a scheduled job stopped leaving evidence | "NEVER RAN" = probably not scheduled, check `crontab -l`. "STALE" = it ran before and stopped; check that job's log. |
| **cron failure** (ERR-trap) | a scheduled job errored | Check the log it names in `logs/`; usually a stale Schwab token → do §1. |
| **"feed down — stops unwatched"** | the intraday monitor can't get quotes | Schwab feed is down → do §1. Until fixed, your stops aren't auto-watched; eyeball positions if you care. |
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

**Rebuild the price cache** (needed if prices look stale, or after a long Schwab
outage — the Sunday slow loop does this automatically, but you can force it):
```
cd /opt/agentic-trader
.venv/bin/python scripts/fetch_prices.py --force      # ~2 min
```

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

- **Most of the time there's nothing to fix in code.** A stale Schwab token,
  the moomoo OpenD gateway logged out, a cron line never actually added to the
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
  close it. Branch protection on `main` (part of the setup below) makes this
  a hard rule, not just a polite convention.

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
3. **Turn on branch protection for `main`** — github.com → this repo →
   **Settings → Branches** → add a rule for `main` requiring a pull request
   before merging (no direct pushes). This is what guarantees a proposed fix
   can never land without you looking at it first.

---

## 6. Checking that everything's healthy

- **Schwab token:** `.venv/bin/python scripts/schwab_status.py`
- **Dashboard** (portfolio, equity curve): **dash.ethobs.uk** (login = `DASH_USER`/
  `DASH_PASS` from `.env`). Locally: `.venv/bin/python dashboard/app.py` → 127.0.0.1:8787.
- **Recent cron activity:** `tail logs/slow.log logs/fast.log logs/signals.log`
- **Adaptive runs:** GitHub → Actions → adaptive-tune.

---

## 7. Emergency: something's wrong, stop everything

1. **Freeze trading:** `touch /opt/agentic-trader/research_store/HALT`
2. If you want it disarmed longer-term: set `[proof] live_approved = false` in
   `config/strategy.local.toml`.
3. Nothing will place orders until you `rm research_store/HALT` (and re-arm
   `live_approved = true`). Existing positions are untouched — the freeze only stops
   *new* orders; sell manually in Robinhood if you need to exit.

---

## Cheat sheet

```
STOP TRADING NOW        touch research_store/HALT          (rm to resume)
Schwab re-auth (weekly) .venv/bin/python scripts/schwab_auth.py   (SSH, paste URL)
Check Schwab token      .venv/bin/python scripts/schwab_status.py
Review stop proposal    .venv/bin/python scripts/promote_proposal.py
Apply a stop value      .venv/bin/python scripts/promote_proposal.py --set 2.5
Rebuild prices          .venv/bin/python scripts/fetch_prices.py --force
Dashboard               dash.ethobs.uk
Adaptive proposals      github.com/aye5788/agentic-trader  → Actions → adaptive-tune
```

*Deeper detail: `CLAUDE.md` (system overview), `docs/DESIGN.md` (architecture),
`docs/DATA_SOURCES.md` (data), `docs/STRATEGY.md` (the trading strategy),
`README.md` (Schwab auth troubleshooting).*
