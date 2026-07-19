"""Data-only moomoo research calls for universe maintenance."""
import csv

from moomoo import RET_OK, Market, SimpleFilter, SortDir, StockField

from .client import quote_ctx


def _us(ticker: str) -> str:
    """Bare ticker -> moomoo US code. 'AAPL' -> 'US.AAPL'; passes through 'US.AAPL'."""
    return ticker if ticker.startswith("US.") else f"US.{ticker}"


def _bare(code: str) -> str:
    """moomoo code -> bare ticker. 'US.AAPL' -> 'AAPL'."""
    return code.split(".", 1)[1] if "." in code else code


def snapshot_turnover(tickers, ctx=None) -> dict:
    """{bare_ticker: turnover ($-volume)} via get_market_snapshot, batched <=400.
    turnover is a single-session figure; NaN/missing -> 0.0."""
    own = ctx is None
    q = ctx or quote_ctx()
    out = {}
    try:
        codes = [_us(t) for t in tickers]
        for i in range(0, len(codes), 400):
            ret, df = q.get_market_snapshot(codes[i:i + 400])
            if ret != RET_OK:
                raise RuntimeError(f"get_market_snapshot failed: {df}")
            for _, r in df.iterrows():
                tv = r["turnover"]
                out[_bare(r["code"])] = float(tv) if tv == tv else 0.0  # tv==tv filters NaN
    finally:
        if own:
            q.close()
    return out


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


def _selftest() -> None:
    assert _us("AAPL") == "US.AAPL"
    assert _us("US.AAPL") == "US.AAPL"
    assert _bare("US.AAPL") == "AAPL"
    assert _bare("AAPL") == "AAPL"
    print("moomoo.research selftest OK: code<->ticker helpers")


if __name__ == "__main__":
    _selftest()
