# agentic-trader

A **live-money** agentic trading system: deterministic research and portfolio
construction in Python, with a headless Claude agent as the execution hands.
Deployed on a VPS.

> ⚠️ This trades real money through Robinhood. If you are an agent working in this
> repo, read **[`CLAUDE.md`](CLAUDE.md)** first — it carries the hard safety rules —
> then [`docs/DESIGN.md`](docs/DESIGN.md) for the architecture.

**Layering:**

- **Sensing** — market/research data adapters (moomoo, FRED, Finnhub, Alpaca)
- **Slow loop** — momentum signal → ranked book + trade geometry (no LLM, no trading)
- **Research store** — the validated handoff (theses + target weights + journal)
- **Fast loop** — stored targets vs. live holdings → order plan
- **Execution** — Robinhood MCP (the thin "hands"); the Agentic account only
- **Governance** — mandate, journal, guardrails, kill switches, cadence

📐 **Full architecture & rationale: [`docs/DESIGN.md`](docs/DESIGN.md).**
📈 **The strategy itself: [`docs/STRATEGY.md`](docs/STRATEGY.md).**
🛠 **If you operate it: [`docs/OPERATOR_MANUAL.md`](docs/OPERATOR_MANUAL.md).**

## Data sources

Full detail (including the verified moomoo surface) in
[`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md).

| Source | Role | Key |
| --- | --- | --- |
| **moomoo** (via local OpenD) | **THE market feed** — daily OHLC panel, intraday quotes for the stop watcher, turnover/market-cap, capital flow, short interest, options | none — authenticates through OpenD |
| **FRED** | Macro regime: VIX, 10y-2y curve, HY spread. Deep history, backtestable. Sole VIX source. | `FRED_API_KEY` |
| **Finnhub** | Earnings spine for the event calendar; analyst trends, surprises, basic financials | `FINNHUB_API_KEY` |
| **Alpaca** | Symbol-tagged news; IEX close + $-vol for the survivorship-free point-in-time pool | `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` |
| **Robinhood** (MCP) | **Execution** — the only trading venue, Agentic account only | one-time OAuth |

Everything except Robinhood is **read-only data**. No other provider has a trading
surface wired, by design.

> ~~**Schwab**~~ was the primary feed until **2026-07-29**. Its 7-day OAuth token
> was the only recurring human chore in the system, and the signal consumed nothing
> from it but daily closes — so it was replaced wholesale by moomoo. The adapter,
> auth scripts, `SCHWAB_*` keys and `schwabdev` are deleted; **do not reintroduce
> them.** Migration + equivalence proof: `docs/OPSLOG.md` 2026-07-29.

## ⚠️ Two Python runtimes

This trips everyone, including agents:

- Most code runs under the repo **`.venv` (Python 3.12)**.
- The **moomoo SDK is installed only in system `/usr/bin/python3` (3.10)**.

So anything importing `moomoo` — `fetch_prices.py`, `market_monitor.py`,
`fast_loop.py`, `risk_review.py` — **must** run under `/usr/bin/python3`. Under
`.venv` the import fails, and because some guards deliberately fail open, a run can
proceed with a safety check silently skipped. That exact mistake cost real money on
2026-07-23.

moomoo data flows through an **OpenD** gateway on `127.0.0.1:11111`, **shared with
the sibling repo `moomoo-vol-desk`**. Never start a second one.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env      # then fill in the keys from the table above
```

Market data needs no key — moomoo authenticates through OpenD. There is **no
recurring credential chore** in this system.

Check the feed is alive:

```bash
systemctl status opend
/usr/bin/python3 scripts/fetch_prices.py     # appends today's OHLC row
```

## Safety switches

Two, and the difference matters — stops here are **software-only** (the monitor
process *is* the stop; Robinhood has no native stop for fractional shares).

```bash
touch research_store/HALT_ENTRIES   # stop BUYING. Stops + take-profits still fire.
touch research_store/HALT           # place NO order at all, buy or sell.
```

⚠️ Under `HALT` your open positions have **no stop**. The monitor keeps watching
and phones you on every breach ("MANUAL EXIT NEEDED … UNPROTECTED"), but you must
sell by hand. Use `HALT_ENTRIES` if you only meant to stop buying.

Placement is additionally gated by `[proof] live_approved`, which ships `false`.

## Tests

Module-local, assertion-based, no pytest:

```bash
.venv/bin/python src/momentum.py --selftest
.venv/bin/python src/governance.py --selftest
/usr/bin/python3 scripts/market_monitor.py --selftest    # system python — imports moomoo
python3 src/repo_checks.py                               # static repo/CI drift checks
```

## Confirm a provider's scope

```bash
.venv/bin/python scripts/finnhub_scope.py AAPL
.venv/bin/python scripts/alpaca_scope.py AAPL
.venv/bin/python scripts/fred_scope.py
```
