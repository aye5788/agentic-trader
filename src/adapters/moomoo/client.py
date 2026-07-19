"""Connection to the local OpenD gateway. Data-only."""
from moomoo import OpenQuoteContext

HOST = "127.0.0.1"
PORT = 11111


def quote_ctx(host: str = HOST, port: int = PORT) -> OpenQuoteContext:
    """Open a market-data context against the running OpenD. Caller closes it."""
    return OpenQuoteContext(host=host, port=port)
