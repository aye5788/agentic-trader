"""Research adapter over the Schwab Market Data API.

Scope note: the Schwab developer API exposes FUNDAMENTALS (via the instruments
`fundamental` projection) but does NOT expose analyst ratings / price targets /
research reports — there is no such endpoint. Those live only in the Schwab &
thinkorswim UIs. Analyst signal, if we want it, must come from another source.
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
