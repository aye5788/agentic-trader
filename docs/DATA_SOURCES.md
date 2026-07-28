# Data sources — detailed reference

The single place that documents **what data each adapter can actually pull**, its
**history depth**, and **what is wired vs. merely available**. Read this before
assuming a signal is or isn't reachable. `CLAUDE.md` has the one-paragraph
summary; this is the detail.

> **Last verified against live APIs: 2026-07-23.** The moomoo surface below was
> probed live on the box that day (OpenD up). Re-verify with the probe snippets if
> a year has passed.

---

## 0. Runtimes (get this wrong and nothing imports)

- Core system → repo **`.venv` (Python 3.12)**.
- **moomoo → system `/usr/bin/python3` (3.10) ONLY.** The `moomoo` SDK is not in
  `.venv`. Any moomoo pull must run under `/usr/bin/python3` (see
  `deploy/run_universe_refresh.sh`). It talks to a local **OpenD** daemon on
  `127.0.0.1:11111` (`opend.service`), **shared with `~/moomoo-vol-desk`** — never
  start a second OpenD. Data needs only the quote channel.

---

## 1. Schwab (`src/adapters/schwab/`) — PRIMARY market data

`get_price_history`, `get_quote(s)`, `get_fundamentals`, `get_option_chain`,
`get_movers`, `get_market_hours`. OHLC price history is the backbone (10y daily via
`scripts/fetch_prices.py` → `research_store/prices/{closes,highs,lows,opens}.parquet`).
OAuth refresh token **expires every 7 days** — `scripts/schwab_auth.py` (interactive)
or `scripts/schwab_finish_auth.py "<redirect-url>"`; check freshness with
`scripts/schwab_status.py`. Data-only by credential (trading API returns 401).

## 2. FRED (`src/adapters/fred/`) — macro regime, DEEP history

`get_vix` (`VIXCLS`, since 1990), `get_yield_curve` (`T10Y2Y`, since 1976),
`get_hy_spread` (`BAMLH0A0HYM2`, since ~2023 on this key), `snapshot()`. Needs
`FRED_API_KEY`. Decades of daily history via `series/observations` (the adapter's
`series_latest` only pulls the latest, but the API has full history). Deep history
makes FRED **backtestable** — good for a momentum-crash regime overlay (VIX + curve
+ credit stress). Currently only *confirms* the Schwab `SPY>50DMA` regime gate.

## 3. Finnhub (`src/adapters/finnhub/`) — analyst/earnings, NOT retired

`get_recommendation_trends`, `get_earnings_surprises`, `get_basic_financials`
(free); `get_price_target`, `get_eps_estimates` (**premium → 403**). Depth probed
2026-07-23: rec-trends ~4 recent months; earnings-surprises ~4–8 quarters; basic
financials carry a **long historical `series`** (e.g. quarterly metrics back to
1990). **Currently consumed only by `src/event_calendar/`** (the earnings-date
spine) — NOT the ranking signal. Available if a non-price analyst tilt is ever
wanted.

## 4. Alpaca (`src/adapters/alpaca/`) — news + PIT-pool prices

`get_news` (symbol-tagged; probed depth ≈ recent weeks per query), and IEX
close+$-volume for the survivorship-free **PIT pool** (`scripts/fetch_pool.py` →
`pool_closes.parquet`/`pool_dvol.parquet`) — the only free feed serving **delisted**
names. Price is IEX-only (not NBBO); never use for quotes.

## 5. moomoo (`src/adapters/moomoo/`) — the deep, under-used source

**Wired today (universe maintenance only):** `snapshot_turnover`,
`screen_top_marketcap`, `candidate_pond`. **Everything below is available but
UNWIRED.** Quota: re-pulling a known symbol costs **0** (verified); tier cap 100.

### 5a. The defining constraint: moomoo history is SHALLOW

Every moomoo time-series/event feed is **~1 year to at most ~2.5 years** deep. It
is a real-time/recent feed, **not** a backtest archive. **Consequence:** moomoo
signals **cannot be rigorously backtested** across regimes — the methodology is to
**forward-log them into the ledger** at each weekly decision and validate
prospectively (meta-labeling), NOT to fit a 1-year backtest.

### 5b. Verified endpoints (2026-07-23, US.AAPL), depth, and use

| Endpoint | Returns (key fields) | Depth (verified) | Signal use |
| --- | --- | --- | --- |
| `get_market_snapshot(codes)` **[batch ≤400]** | last/OHLC, volume, `turnover`, `turnover_rate`, `volume_ratio`, `amplitude`, `highest52weeks_price`/`lowest`, `pe_ratio`/`pb_ratio`/`pe_ttm_ratio`/`ps`, `dividend_ttm`, `earning_per_share`, `total_market_val`, pre/after/overnight session | snapshot | 52wk-high proximity, abnormal-volume attention, valuation/quality — cheap batched |
| `get_capital_flow(code, period_type=DAY)` | `in_flow`, `super/big/mid/sml_in_flow` (order-size net $ flow), `main_in_flow` | **~252 daily (1 yr)** | **smart-money accumulation** (super+big net) — the standout, orthogonal to price |
| `get_capital_distribution(code)` | current `capital_in/out_super/big/mid/small` | snapshot | standing large/small composition |
| `get_option_underlying_overview(codes)` **[batch]** | `call_volume`/`put_volume`, `call_open_interest`/`put_open_interest`, `iv`, `iv_rank`, `iv_percentile`, `hv_30/60/90/120/365d` (+ percentiles) | snapshot | **put/call ratio** (directional positioning) + IV rank (per-name fragility flag) — one batched call |
| `get_short_interest(code)` | `short_percent`, `days_to_cover`, `shares_short`, `avg_daily_share_volume` | ~10 readings (~4.5 mo, bi-monthly) | short crowding / squeeze fuel |
| `get_daily_short_volume(code)` | daily `short_percent`, nasdaq/nyse shares short | ~10 days | daily selling pressure (noisier; redundant w/ short interest) |
| `get_financials_earnings_price_move(code)` | per-quarter earnings-reaction price path, `day_offset` | **~10 quarters (2.5 yr — deepest)** | PEAD (largely already in the momentum score) |
| `get_option_underlying_his_volatility(code)` | daily `iv`, `hv`, `underlying_price` | ~251 daily (1 yr) | IV/HV time series (superseded by overview's iv_rank) |
| `get_insider_trade_list(code)` | `trade_shares`, min/max price+date, `is_proposed_sale_of_securities` | ~10 recent (~2 mo) | insider activity — **noisy/gameable** (sales dominate, publicly disclosed); deliberately NOT in the panel |
| `get_research_analyst_consensus(code)` | analyst consensus rows | ~10 rows | analyst view (finnhub is cleaner) |

### 5c. Available but needs work / not per-stock

- `get_institution_holding_change/list/distribution(market, institution_id, …)` —
  **institution-centric**, keyed by fund, not by stock. "Is a fund accumulating
  stock X" is not a direct call; would need aggregation. `get_shareholders_*`
  (`_institutional`, `_holding_changes`, `_overview`) are the stock-centric cousins
  — unprobed.
- `get_rating_change(market, change_type, …)` — market-WIDE upgrade/downgrade
  screen (cross-reference against the universe), not per-name.
- `get_corporate_actions_buybacks(code)` — **US NOT supported** (HK/A-share only).
- `get_stock_filter` with **`StockField` (133 fields)** — a screening catalog
  (fundamentals: ROE/margins/growth/PE/PB/PS; technicals: MACD/RSI/KDJ/BOLL/EMA;
  52wk ratios; turnover). Snapshot screening, no history.
- Also present (categories, mostly unexplored): ARK fund flows
  (`get_ark_*`), full options analytics (`get_option_chain`, `get_option_rank`,
  `get_option_screen`, IV/greeks), breadth (`get_rise_fall_distribution`,
  `get_heat_map_data`), macro/calendars (`get_economic_calendar`,
  `get_fed_watch_*`, `get_earnings_calendar`, `get_ipo_list`), plates/sectors,
  `get_short_selling_rank`, `get_top_movers_rank`, pre/after/overnight rank.

### 5d. Probe snippet (re-verify anything above)

```python
# /usr/bin/python3  (NOT .venv)
import sys, pathlib; sys.path.insert(0, "src")
from adapters.moomoo.client import quote_ctx
from moomoo import RET_OK
ctx = quote_ctx()
ret, data = ctx.get_capital_flow("US.AAPL")        # (ret, dataframe)
print(ret == RET_OK, list(getattr(data, "columns", [])), len(data))
ctx.close()
```
Some endpoints return **>2-tuples** — normalize with `data = out[1]`, never blindly
`ret, data = out` (that raised "expected 2, got 6" on 2026-07-23).

---

## 6. Robinhood (MCP) — execution + fundamentals

The only execution venue (Agentic account only). Also serves its own fundamentals,
earnings, positions, quotes via MCP tools. See `CLAUDE.md` hard rules.

---

## The signal-panel (BUILT)

A lean, forward-logged **moomoo signal panel** is logged weekly to the ledger's
`signal_panel` journal event (book-scoped, per held name) for later meta-labeling,
written by `scripts/collect_signals.py`. Fields: `capflow_bignet_20d`, `short_pct`,
`days_to_cover`, `short_pct_chg`, `pc_vol_ratio`, `pc_oi_ratio`, `iv_rank`,
`pct_52w_high`, `volume_ratio`. This is *forward-collection*, not a backtested
dial (per §5a).

**ARMED 2026-07-24** — `15 20 * * 0` (Sun 20:15 ET, after the 20:00 rebalance),
appended to the live crontab. Note the trap that delayed this: the line was added
to `deploy/crontab.template` on 2026-07-23, but **the template is not the live
crontab** — arming is a separate append step on the box (see `docs/DEPLOY.md`
Phase 4). Between those dates this section claimed the collector was running when
it had never fired. First scheduled fire: **Sun 2026-07-26 20:15 ET**.

**Known null: `capflow_bignet_20d` is ETF-only-null.** The 2026-07-24 live dry-run
(14 held names) filled all 9 fields for every name *except* capital flow on the 4
ETFs (XLE, IWM, SPY, EEM); the 2026-07-27 run nulled it on all four (XLE, XLK,
IWM, XLI). The distiller is null-safe and this does **not** register in `gaps`.

⚠️ **Corrected 2026-07-28 — the cause is NOT "the endpoint doesn't serve ETFs".**
`get_capital_flow` returns rows fine. `signal_panel.distill_capflow(rows,
market_val)` divides by market cap and returns `None` when *either* input is
falsy — and moomoo's ETF snapshot carries no `total_market_val`. Meanwhile
`collect_signals._panel_for` appends a gap only when `capital_flow_daily()`
returns **no rows**. So rows arrive, market cap doesn't, the field nulls, and the
event still reports `gaps: []`.

Why it matters: **in a regime-off book the sleeve is 100% ETFs**, so the entire
capital-flow signal is empty across the whole panel while the run reports clean
(`opend_ok=True, gaps: []`, 2026-07-27). That is a health signal reading green
while carrying nothing — do not treat "0 gaps" as "panel complete". Fix options
(undecided): record a gap when the distill returns None rather than only when
rows are empty, or normalise ETF flow by AUM / (shares × price).
