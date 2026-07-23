"""Data-only moomoo research calls for universe maintenance."""
import csv

from moomoo import RET_OK, Market, PeriodType, SimpleFilter, SortDir, StockField

from .client import quote_ctx


def _us(ticker: str) -> str:
    """Bare ticker -> moomoo US code. 'AAPL' -> 'US.AAPL'; passes through 'US.AAPL'."""
    return ticker if ticker.startswith("US.") else f"US.{ticker}"


def _bare(code: str) -> str:
    """moomoo code -> bare ticker. 'US.AAPL' -> 'AAPL'."""
    return code.split(".", 1)[1] if "." in code else code


def snapshot_turnover(tickers, ctx=None) -> dict:
    """{bare_ticker: turnover ($-volume)} via get_market_snapshot, batched <=400.
    turnover is a single-session figure; NaN/missing -> 0.0. Resilient to unquotable
    tickers: a batch that moomoo rejects (e.g. a delisted name) is bisected so the
    good tickers still resolve and only the bad ones are skipped."""
    own = ctx is None
    q = ctx or quote_ctx()
    out = {}
    try:
        codes = [_us(t) for t in tickers]
        for i in range(0, len(codes), 400):
            _snap_chunk(q, codes[i:i + 400], out)
    finally:
        if own:
            q.close()
    return out


# Error-message markers that mean a specific ticker simply can't be quoted
# (delisted / OTC / unknown) — safe to skip. Anything else (rate limit, network,
# systemic) must NOT be silently skipped: dropping a good name corrupts the ranking.
_UNQUOTABLE_MARKERS = ("not available", "unknown stock", "no data")


def _snap_chunk(q, codes, out) -> None:
    """Snapshot `codes` into `out`. A batch moomoo rejects is bisected to isolate the
    offending code(s). A single code that fails with a *ticker-unavailable* error is
    skipped; any OTHER single-code failure (rate limit / systemic) is RAISED so it
    surfaces loudly rather than silently dropping a good name."""
    if not codes:
        return
    ret, df = q.get_market_snapshot(codes)
    if ret == RET_OK:
        for _, r in df.iterrows():
            tv = r["turnover"]
            out[_bare(r["code"])] = float(tv) if tv == tv else 0.0
        return
    if len(codes) == 1:
        if any(m in str(df).lower() for m in _UNQUOTABLE_MARKERS):
            return  # genuinely unquotable single ticker — skip it
        raise RuntimeError(f"snapshot failed for {codes[0]} (non-ticker error): {df}")
    mid = len(codes) // 2
    _snap_chunk(q, codes[:mid], out)
    _snap_chunk(q, codes[mid:], out)


def screen_top_marketcap(n: int = 400, min_mktcap: float = 2e9, ctx=None) -> list:
    """Top US names by market cap (a liquidity proxy for the pond). Paginated (200/call)."""
    own = ctx is None
    q = ctx or quote_ctx()
    out = []
    try:
        begin = 0
        while len(out) < n:
            sf = SimpleFilter()
            sf.stock_field = StockField.MARKET_VAL
            sf.filter_min = min_mktcap
            sf.is_no_filter = False
            sf.sort = SortDir.DESCEND
            ret, data = q.get_stock_filter(
                market=Market.US, filter_list=[sf], begin=begin, num=200)
            if ret != RET_OK:
                raise RuntimeError(f"get_stock_filter failed: {data}")
            last_page, _all, lst = data
            out.extend(_bare(s.stock_code) for s in lst)
            if last_page or not lst:
                break
            begin += 200
    finally:
        if own:
            q.close()
    return out[:n]


def candidate_pond(incumbents, pit_pool_path, params, ctx=None) -> list:
    """incumbents ∪ broad reference. Reference = market-cap screen; on any failure or
    empty result, fall back to the pre-built pit_pool CSV. Returns bare tickers, sorted."""
    ref = []
    try:
        ref = screen_top_marketcap(params["screen_top_n"], params["screen_min_mktcap"], ctx=ctx)
    except Exception as e:  # noqa: BLE001 — offline batch; degrade to the static pool
        print(f"  market-cap screen failed ({e}); falling back to pit_pool")
    if not ref:
        with open(pit_pool_path, newline="") as f:
            ref = [row["ticker"] for row in csv.DictReader(f)]
    return sorted(set(incumbents) | set(ref))


def _unwrap(out):
    """moomoo calls return (ret, data) — but some return longer tuples. Normalize."""
    if isinstance(out, tuple):
        return out[0], (out[1] if len(out) > 1 else None)
    return RET_OK, out


def capital_flow_daily(ctx, ticker):
    """Daily capital-flow records for one US name (newest ~1yr). [] on any failure."""
    try:
        ret, data = _unwrap(ctx.get_capital_flow(_us(ticker), period_type=PeriodType.DAY))
        if ret != RET_OK or data is None or not len(data):
            return []
        return data.to_dict("records")
    except Exception:
        return []


def short_interest(ctx, ticker):
    """Short-interest readings for one US name. [] on any failure."""
    try:
        ret, data = _unwrap(ctx.get_short_interest(_us(ticker)))
        if ret != RET_OK or data is None or not len(data):
            return []
        return data.to_dict("records")
    except Exception:
        return []


def option_overview(ctx, tickers):
    """Batched option overview → {bare: {call/put volume+OI, iv_rank}}. {} on failure."""
    try:
        ret, data = _unwrap(ctx.get_option_underlying_overview([_us(t) for t in tickers]))
        if ret != RET_OK or data is None or not len(data):
            return {}
        keep = ["call_volume", "put_volume", "call_open_interest",
                "put_open_interest", "iv_rank"]
        out = {}
        for rec in data.to_dict("records"):
            out[_bare(rec.get("code", ""))] = {k: rec.get(k) for k in keep}
        return out
    except Exception:
        return {}


def snapshot_fields(ctx, tickers):
    """Batched snapshot → {bare: {last_price, highest52weeks_price, volume_ratio,
    total_market_val}}. {} on failure."""
    try:
        ret, data = _unwrap(ctx.get_market_snapshot([_us(t) for t in tickers]))
        if ret != RET_OK or data is None or not len(data):
            return {}
        keep = ["last_price", "highest52weeks_price", "volume_ratio", "total_market_val"]
        out = {}
        for rec in data.to_dict("records"):
            out[_bare(rec.get("code", ""))] = {k: rec.get(k) for k in keep}
        return out
    except Exception:
        return {}


def _selftest() -> None:
    assert _us("AAPL") == "US.AAPL"
    assert _us("US.AAPL") == "US.AAPL"
    assert _bare("US.AAPL") == "AAPL"
    assert _bare("AAPL") == "AAPL"
    print("moomoo.research selftest OK: code<->ticker helpers")


if __name__ == "__main__":
    _selftest()
