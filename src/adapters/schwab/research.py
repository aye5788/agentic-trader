"""Research adapter over the Schwab Market Data API.

Clean, typed entry points over the endpoints verified in scripts/schwab_scope_full.py:
fundamentals, quotes (true NBBO), price history, option chains (with greeks),
movers, and market hours. Each function returns the *useful* slice of the payload
(not the raw HTTP response) and raises on non-200.

Scope note: the Schwab developer API exposes FUNDAMENTALS but does NOT expose
analyst ratings / price targets / research reports — there is no such endpoint.
Analyst signal comes from Finnhub instead (see src/adapters/finnhub/). Schwab is
also Market Data ONLY — its Accounts & Trading API returns 401, so it structurally
cannot trade. Execution lives on Robinhood.
"""
from .client import build_client


def get_fundamentals(symbol: str, client=None) -> dict:
    """Return the `fundamental` block for a symbol (P/E, margins, EPS, div, etc.)."""
    client = client or build_client(interactive_auth=False)
    resp = client.instruments(symbol, "fundamental")
    resp.raise_for_status()
    data = resp.json()
    instruments = data.get("instruments") or [{}]
    return instruments[0].get("fundamental", {})


def get_quote(symbol: str, client=None) -> dict:
    """Return the live quote payload for one symbol.

    The full per-symbol block (assetMainType, quote, reference, regular, …); the
    NBBO / last / OHLC / volume live under the `quote` sub-dict. True SIP data —
    this is the source of truth for prices (Alpaca free is IEX-only).
    """
    client = client or build_client(interactive_auth=False)
    resp = client.quotes([symbol])
    resp.raise_for_status()
    return resp.json().get(symbol, {})


def get_quotes(symbols: list, client=None) -> dict:
    """Return live quotes for several symbols at once: {symbol: {…block…}}."""
    client = client or build_client(interactive_auth=False)
    resp = client.quotes(list(symbols))
    resp.raise_for_status()
    return resp.json()


def get_price_history(
    symbol: str,
    *,
    period_type: str = "month",
    period: int = 1,
    frequency_type: str = "daily",
    frequency: int = 1,
    client=None,
) -> list:
    """Return OHLCV candles for a symbol (default: 1 month of daily bars).

    Each candle: {open, high, low, close, volume, datetime}. Tune period_type
    (day/month/year/ytd), period, frequency_type (minute/daily/weekly/monthly),
    and frequency for finer/coarser history.
    """
    client = client or build_client(interactive_auth=False)
    resp = client.price_history(
        symbol,
        periodType=period_type,
        period=period,
        frequencyType=frequency_type,
        frequency=frequency,
    )
    resp.raise_for_status()
    return resp.json().get("candles", [])


def get_option_chain(
    symbol: str,
    *,
    contract_type: str = "ALL",
    strike_count: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    client=None,
) -> dict:
    """Return the option chain (calls/puts with greeks + IV).

    contract_type: "CALL" | "PUT" | "ALL". strike_count limits strikes around ATM
    (keeps payloads small). from_date/to_date ("YYYY-MM-DD") bound expirations.
    Returns the full chain dict incl. callExpDateMap / putExpDateMap.
    """
    client = client or build_client(interactive_auth=False)
    kwargs: dict = {"contractType": contract_type}
    if strike_count is not None:
        kwargs["strikeCount"] = strike_count
    if from_date is not None:
        kwargs["fromDate"] = from_date
    if to_date is not None:
        kwargs["toDate"] = to_date
    resp = client.option_chains(symbol, **kwargs)
    resp.raise_for_status()
    return resp.json()


def get_movers(index: str = "$SPX", *, sort: str | None = None,
               frequency: int | None = None, client=None) -> list:
    """Return top movers for an index ("$DJI" | "$SPX" | "NASDAQ").

    Returns the `screeners` list (each: symbol, description, lastPrice, netChange,
    netPercentChange, volume, …). sort/frequency are optional Schwab filters.
    """
    client = client or build_client(interactive_auth=False)
    kwargs: dict = {}
    if sort is not None:
        kwargs["sort"] = sort
    if frequency is not None:
        kwargs["frequency"] = frequency
    resp = client.movers(index, **kwargs)
    resp.raise_for_status()
    return resp.json().get("screeners", [])


def get_market_hours(markets=("equity", "option"), *, client=None) -> dict:
    """Return market-hours / open status for the given markets.

    Useful for the fast/execution loop's "is the market open right now?" gate.
    """
    client = client or build_client(interactive_auth=False)
    resp = client.market_hours(list(markets))
    resp.raise_for_status()
    return resp.json()
