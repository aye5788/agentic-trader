"""Daily OHLC bars from moomoo — the price feed behind the momentum signal.

Replaces the Schwab price pull (`adapters.schwab.research.get_price_history`).
Schwab's refresh token expired every 7 days and had to be renewed by hand at a
browser; moomoo authenticates through the already-running OpenD gateway, so the
price feed has no recurring credential chore at all. That is the whole reason
this module exists.

Emits candles in the SAME shape the Schwab path did — `{"datetime": <epoch ms>,
"open","high","low","close"}` — so `scripts/fetch_prices.py::_field_panels` and
`_drop_unsettled_session` are unchanged and the cached panels stay byte-compatible
with everything already on disk.

TWO paths, and picking the wrong one hits a wall:
  • `snapshot_ohlc()` — today's bar, ANY number of tickers, **no quota**. This is
    the daily append, and what the scheduled job should call.
  • `daily_panel()`   — a date range via `request_history_kline`, which is metered
    against a hard **100 distinct stocks account-wide**. Backfill only.

⚠️ RUNTIME: the moomoo SDK is installed ONLY under system `/usr/bin/python3`
(3.10), never the repo `.venv` (3.12). Any caller of this module must run under
`/usr/bin/python3` — which has pandas/pyarrow/numpy available.
"""
import datetime as dt
import math
import time

from moomoo import RET_OK, AuType, KLType, TradeDateMarket

# Relative imports break when this file is run DIRECTLY (`python3 prices.py
# --selftest`) rather than imported as a package member -- "attempted relative
# import with no known parent package". That is why its _selftest() had never
# once run: it was written, it passed, and nothing could invoke it. The fallback
# makes the module executable so deploy/run_selftests.sh can actually cover it.
try:
    from .client import quote_ctx
    from .research import _bare, _us
except ImportError:                        # run as a script, not as a package
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
    from adapters.moomoo.client import quote_ctx
    from adapters.moomoo.research import _bare, _us

# moomoo ceiling is 60 history calls / 30s and OpenD is shared, so pace every
# call rather than discovering the limit. Mirrors research.py's snapshot pacing.
_MIN_INTERVAL = 0.55
_MAX_RETRY = 5
_BACKOFF = 6.0
_last_call = [0.0]

# Bars come back with a `time_key` like "2026-07-28 00:00:00" (US/Eastern session
# date). Only these four fields are consumed downstream.
_FIELDS = ("open", "high", "low", "close")


def _paced(fn, *a, **kw):
    wait = _MIN_INTERVAL - (time.monotonic() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    try:
        return fn(*a, **kw)
    finally:
        _last_call[0] = time.monotonic()


def _is_transient(msg: str) -> bool:
    """Connection/limit conditions worth retrying. A dead ticker is NOT transient —
    retrying it just burns the rate budget, so those return empty and get recorded."""
    s = str(msg).lower()
    # "timed out" is listed separately from "timeout" on purpose: moomoo uses BOTH
    # spellings ("PacketErr.Timeout" from the snapshot call, "Get Historical
    # Candlestick request timed out." from the history call), and matching only the
    # first silently treated a retryable history timeout as a dead ticker.
    return any(m in s for m in
               ("frequency", "limit", "timeout", "timed out",
                "connect", "disconnect", "network"))


def _to_candles(df) -> list:
    """moomoo frame -> the Schwab-shaped candle list. Drops rows with any missing
    OHLC field: a partial bar silently becomes a real-looking price otherwise."""
    out = []
    for rec in df.to_dict("records"):
        if any(rec.get(f) is None for f in _FIELDS):
            continue
        ts = rec.get("time_key")
        if not ts:
            continue
        d = dt.datetime.fromisoformat(str(ts)).replace(tzinfo=dt.timezone.utc)
        c = {"datetime": int(d.timestamp() * 1000)}
        for f in _FIELDS:
            c[f] = float(rec[f])
        out.append(c)
    return out


def daily_ohlc(ticker: str, start: str, end: str, ctx=None):
    """Daily QFQ-adjusted bars for one ticker in [start, end] (YYYY-MM-DD).

    Returns (candles, err): candles is Schwab-shaped and possibly empty; err is a
    message when the pull failed outright.

    ⚠️ `AuType.NONE` is deliberate and load-bearing. The ~10y panel already on disk
    came from Schwab, which returns UNADJUSTED closes, and moomoo appends to that
    same series — so the adjustment mode has to agree or the splice date gets a fake
    overnight return. Verified 2026-07-29 against the cached panel: NONE matches to
    0.0000% on SPY/XLK/MU, while QFQ (dividend-adjusted) drifts up to 0.2569% and
    HFQ is wildly off (NVDA 48,020%). Names whose ex-dividend date fell outside the
    compared window matched under either mode, which is exactly what made QFQ look
    fine at first glance — do not "fix" this back to QFQ on a spot check.
    """
    own = ctx is None
    q = ctx or quote_ctx()
    try:
        code = _us(ticker)
        for attempt in range(_MAX_RETRY):
            out = _paced(q.request_history_kline, code, start=start, end=end,
                         ktype=KLType.K_DAY, autype=AuType.NONE, max_count=None)
            ret, data = out[0], out[1]
            if ret == RET_OK:
                return _to_candles(data), None
            if not _is_transient(data):
                return [], f"{data}"
            time.sleep(_BACKOFF * (attempt + 1))
        return [], f"rate-limited after {_MAX_RETRY} attempts"
    except Exception as e:  # noqa: BLE001 — offline batch: record and move on
        return [], f"{type(e).__name__}: {e}"
    finally:
        if own:
            q.close()


def is_trading_day(day, ctx=None):
    """Was/is `day` (YYYY-MM-DD) a US trading session? True / False / None=unknown.

    Exists because the time-of-day settle guard in `fetch_prices` cannot tell a
    closed market from a settled one: `get_market_snapshot` on a shut market
    returns the PREVIOUS session's close, so a Sunday 20:15 ET run appended a
    byte-identical copy of Friday stamped Sunday (2026-08-02, 2026-08-09 — 100%
    of 168 names matched). Each injects a 0% return and deflates sigma, which
    sets stop distance.

    None means "could not tell", never a guess. The caller treats None as
    "fall back to the weekend test", which needs no network and is never wrong;
    this call only adds weekday HOLIDAYS on top of that.

    ⚠️ The SDK method is `request_trading_days`, NOT `get_trading_days` — the
    latter is only the name of a wrapper script in moomoo's own examples, and
    calling it raises AttributeError. Both WHOLE and HALF days are trading days.
    """
    day = str(day)[:10]
    own = ctx is None
    q = None
    try:
        q = ctx or quote_ctx()
        ret, data = _paced(q.request_trading_days,
                           market=TradeDateMarket.US, start=day, end=day)
        if ret != RET_OK:
            return None
        return any(str(r.get("time", ""))[:10] == day for r in (data or []))
    except Exception:                      # noqa: BLE001 -- unknown, never a guess
        return None
    finally:
        if own and q is not None:
            try:
                q.close()
            except Exception:              # noqa: BLE001
                pass


def splits(ticker: str, ctx=None):
    """Split history for one ticker -> [{"ex_date": "YYYY-MM-DD", "ratio": float}].

    `ratio` is moomoo's `split_ratio`: the factor a PRE-split price must be
    MULTIPLIED by to express it on the post-split basis. A 2:1 split is 0.5, a
    3:1 is 0.3333. Verified against MNST, whose real history reads
    2012-02-16 0.5, 2016-11-10 0.3333, 2023-03-28 0.5, 2026-08-11 0.5.

    WHY THIS MATTERS: the daily panel stores raw closes, so a split lands in it
    as a genuine one-day return -- MNST's 2:1 on 2026-08-11 appears as -50.2%.
    That is not cosmetic: momentum's R, sigma and trend are all computed off
    these returns, so one split poisons a name's score for a full lookback year,
    and sigma is what sets stop distance and target geometry.

    Returns [] on any failure -- an empty split history is indistinguishable
    from an unreachable one HERE, so the caller must never treat [] as proof a
    name did not split; scripts/adjust_splits.py only ever ADDS an adjustment on
    positive evidence, and does nothing on [].

    ⚠️ get_rehab is PER-SYMBOL (one call per code), so this is not something to
    sweep across a 168-name universe daily. The caller detects split-SHAPED
    moves from the panel for free and confirms only those.
    """
    own = ctx is None
    q = None
    try:
        q = ctx or quote_ctx()
        ret, data = _paced(q.get_rehab, _us(ticker))
        if ret != RET_OK or data is None or not len(data):
            return []
        out = []
        for rec in data.to_dict("records"):
            r = rec.get("split_ratio")
            d = str(rec.get("ex_div_date") or "")[:10]
            try:
                r = float(r)
            except (TypeError, ValueError):
                continue
            # NaN-safe, and 1.0 is a no-op row (a dividend-only record)
            if not (r == r) or r <= 0 or r == 1.0 or len(d) != 10:
                continue
            out.append({"ex_date": d, "ratio": r})
        return sorted(out, key=lambda x: x["ex_date"])
    except Exception:                      # noqa: BLE001
        return []
    finally:
        if own and q is not None:
            try:
                q.close()
            except Exception:              # noqa: BLE001
                pass


def snapshot_ohlc(tickers, ctx=None):
    """Today's daily OHLC bar for `tickers` -> ({ticker: candle}, {ticker: err}).

    THIS is the daily-append path, and the reason it exists rather than using
    `daily_panel`: `request_history_kline` is metered against a hard **100 distinct
    stocks, account-wide** quota (verified 2026-07-29 — the pull died at exactly
    `stock: 100/100` with 98 of 168 universe names unfetched). `get_market_snapshot`
    is rate-limited but NOT quota-metered and takes 400 codes per call, so the whole
    universe costs ONE call and zero quota, forever.

    The bar is the CURRENT session's and is partial during RTH — `last_price` is just
    the last trade until the close settles. Callers must run it through
    `fetch_prices._drop_unsettled_session`, which already handles exactly this.
    """
    own = ctx is None
    q = ctx or quote_ctx()
    out, errs = {}, {}
    try:
        codes = [_us(t) for t in tickers]
        for i in range(0, len(codes), 400):
            batch = codes[i:i + 400]
            res = _paced(q.get_market_snapshot, batch)
            ret, data = res[0], res[1]
            if ret != RET_OK:
                for c in batch:
                    errs[_bare(c)] = str(data)[:200]
                continue
            for rec in data.to_dict("records"):
                t = _bare(rec.get("code", ""))
                ts = rec.get("update_time")
                o, h, lo, c = (rec.get("open_price"), rec.get("high_price"),
                               rec.get("low_price"), rec.get("last_price"))
                if not ts or any(v is None for v in (o, h, lo, c)):
                    errs[t] = "snapshot missing OHLC"
                    continue
                # A halted/never-traded name reports 0.0 across the bar. That is not
                # a price — letting it through would post a -100% return.
                if min(float(o), float(h), float(lo), float(c)) <= 0:
                    errs[t] = "non-positive OHLC (halted / no trades)"
                    continue
                d = dt.datetime.fromisoformat(str(ts).split(".")[0])
                # turnover ($-volume) rides along FREE: get_market_snapshot
                # already returns it in this same record and it was being
                # discarded. It is the consolidated-tape figure, which is what
                # [governance] min_dollar_volume_20d is actually calibrated
                # against -- the fallback source (Alpaca IEX) is one venue and a
                # fraction of the tape, so it reads genuinely liquid names as
                # illiquid. Missing/NaN -> None rather than 0.0: absent is not
                # "traded nothing", and a zero would drag a 20-day mean down.
                tv = rec.get("turnover")
                try:
                    tv = float(tv)
                    tv = tv if tv == tv and tv > 0 else None      # NaN-safe
                except (TypeError, ValueError):
                    tv = None
                out[t] = {"datetime": int(d.replace(tzinfo=dt.timezone.utc).timestamp() * 1000),
                          "open": float(o), "high": float(h),
                          "low": float(lo), "close": float(c),
                          "turnover": tv}
    finally:
        if own:
            q.close()
    return out, errs


def live_quotes(tickers, ctx=None):
    """Live marks for the intraday stop watcher -> {ticker: {last, open, high, low}}.

    Same unmetered `get_market_snapshot` call as `snapshot_ohlc`, but shaped for
    `market_monitor`: it wants the current price to compare against a stored stop,
    plus the session high/low. One call covers every holding — the Schwab path this
    replaces made a per-name request.

    A name whose snapshot has no positive last price is OMITTED rather than given a
    zero. The monitor treats a missing symbol as "no data this tick" (safe: no
    trigger), whereas a 0.0 would read as a catastrophic gap and fire a market sell.

    Transient failures are retried in-place before raising. Observed live within a
    minute of the feed cutover: `PacketErr.Timeout` on a single tick. OpenD is shared
    with moomoo-vol-desk, so brief timeouts are expected rather than exceptional —
    and in the monitor a raise costs a full context teardown/rebuild, so absorbing
    the blip here is much cheaper than escalating it. A raise still happens when
    retries are exhausted, which keeps the recover/alert/exit ladder intact for a
    genuinely wedged feed.
    """
    own = ctx is None
    q = ctx or quote_ctx()
    out = {}
    try:
        codes = [_us(t) for t in tickers]
        for i in range(0, len(codes), 400):
            for attempt in range(_MAX_RETRY):
                res = _paced(q.get_market_snapshot, codes[i:i + 400])
                ret, data = res[0], res[1]
                if ret == RET_OK or not _is_transient(data):
                    break
                time.sleep(min(_BACKOFF, 1.5 * (attempt + 1)))
            if ret != RET_OK:
                raise RuntimeError(f"get_market_snapshot failed: {str(data)[:200]}")
            for rec in data.to_dict("records"):
                last = rec.get("last_price")
                # ⛔ FINITE ONLY. `last <= 0` does not reject inf/NaN: inf would
                # be served as a live price, comparing above every stop (so it
                # arms any watch and satisfies any target) and NaN makes every
                # comparison false. Found by the independent reviewer,
                # 2026-08-20; scripts/market_monitor.py:_last_price had the
                # identical hole.
                if (isinstance(last, bool)
                        or not isinstance(last, (int, float))
                        or not math.isfinite(last) or last <= 0):
                    continue
                out[_bare(rec.get("code", ""))] = {
                    "last": float(last),
                    "open": rec.get("open_price"),
                    "high": rec.get("high_price"),
                    "low": rec.get("low_price"),
                }
    finally:
        if own:
            q.close()
    return out


def daily_panel(tickers, start: str, end: str, ctx=None, progress=None):
    """Pull `tickers` sequentially -> ({ticker: candles}, {ticker: err}).

    One context for the whole batch (reconnecting per ticker is what made the
    first universe_refresh run so slow). Sequential because history is per-symbol;
    there is no batch history call.
    """
    own = ctx is None
    q = ctx or quote_ctx()
    raw, errs = {}, {}
    try:
        for i, t in enumerate(tickers, 1):
            candles, err = daily_ohlc(t, start, end, ctx=q)
            if candles:
                raw[t] = candles
            if err:
                errs[t] = err
            if progress:
                progress(i, len(tickers), t, len(candles), err)
    finally:
        if own:
            q.close()
    return raw, errs


def _selftest() -> None:
    import math

    # _to_candles: shape, ordering preserved, partial bars dropped
    class _DF:
        def __init__(self, rows): self.rows = rows
        def to_dict(self, _): return self.rows

    rows = [
        {"time_key": "2026-07-27 00:00:00", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5},
        {"time_key": "2026-07-28 00:00:00", "open": 2.0, "high": 3.0, "low": 1.5, "close": 2.5},
        {"time_key": "2026-07-29 00:00:00", "open": 3.0, "high": None, "low": 2.0, "close": 3.5},
        {"time_key": None, "open": 4.0, "high": 5.0, "low": 3.0, "close": 4.5},
    ]
    c = _to_candles(_DF(rows))
    assert len(c) == 2, f"partial + keyless bars must drop, got {len(c)}"
    assert set(c[0]) == {"datetime", "open", "high", "low", "close"}, c[0]
    assert c[0]["datetime"] < c[1]["datetime"], "order must survive"
    assert c[1]["close"] == 2.5 and isinstance(c[1]["close"], float), c[1]
    # epoch ms must round-trip to the same session date the API named
    got = dt.datetime.fromtimestamp(c[1]["datetime"] / 1000, dt.timezone.utc).date()
    assert got == dt.date(2026, 7, 28), got

    # transient classification: retry the connection, never the dead ticker
    assert _is_transient("request frequency limit exceeded")
    assert _is_transient("Timeout waiting for response")
    # both spellings moomoo actually emits — snapshot vs history call
    assert _is_transient("PacketErr.Timeout"), "snapshot timeout must be retryable"
    assert _is_transient("Get Historical Candlestick request timed out."), \
        "'timed out' is a DIFFERENT string from 'timeout' — both must match"
    assert not _is_transient("code not exist"), "a dead ticker must not be retried"
    assert not _is_transient("Insufficient historical K-line quota"), \
        "quota exhaustion is not transient — retrying cannot help"

    assert not math.isnan(float(c[0]["open"]))

    # --- snapshot_ohlc: the guards that keep bad bars out of the panel ----------
    class _Q:
        def __init__(self, rows): self.rows, self.batches = rows, []
        def get_market_snapshot(self, codes):
            self.batches.append(list(codes))
            return RET_OK, _DF([r for r in self.rows if r["code"] in set(codes)])

    rows = [
        {"code": "US.GOOD", "update_time": "2026-07-28 16:00:00.123",
         "open_price": 10.0, "high_price": 11.0, "low_price": 9.0, "last_price": 10.5},
        {"code": "US.HALTED", "update_time": "2026-07-28 16:00:00",     # never traded
         "open_price": 0.0, "high_price": 0.0, "low_price": 0.0, "last_price": 0.0},
        {"code": "US.PARTIAL", "update_time": "2026-07-28 16:00:00",    # missing a leg
         "open_price": 5.0, "high_price": None, "low_price": 4.0, "last_price": 4.5},
    ]
    global _MIN_INTERVAL
    _MIN_INTERVAL = 0.0                                   # keep the selftest instant
    fake = _Q(rows)
    out, errs = snapshot_ohlc(["GOOD", "HALTED", "PARTIAL"], ctx=fake)
    assert set(out) == {"GOOD"}, f"only the good bar may pass, got {sorted(out)}"
    assert out["GOOD"]["close"] == 10.5, out["GOOD"]
    assert "HALTED" in errs and "non-positive" in errs["HALTED"], errs
    assert "PARTIAL" in errs and "missing OHLC" in errs["PARTIAL"], errs
    # fractional-second update_time must not break the timestamp parse
    assert dt.datetime.fromtimestamp(out["GOOD"]["datetime"] / 1000,
                                     dt.timezone.utc).date() == dt.date(2026, 7, 28)

    # batching: 400 codes per call is the snapshot ceiling
    many = _Q([])
    snapshot_ohlc([f"T{i}" for i in range(900)], ctx=many)
    assert [len(b) for b in many.batches] == [400, 400, 100], [len(b) for b in many.batches]

    print("moomoo.prices selftest OK: candle shape, retry classification, "
          "snapshot guards, 400-code batching")


if __name__ == "__main__":
    _selftest()
