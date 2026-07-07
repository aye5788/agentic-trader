"""News research over Alpaca — the live-headline slice Schwab/Finnhub don't give.

SLOW-LOOP ONLY (nightly/weekly sensing pass). The signal here is *narrative and
catalyst awareness* — what's being said about a name, and when — to complement the
hard numbers from Schwab (fundamentals/quotes) and Finnhub (estimates/surprises).

Each article: {id, headline, author, created_at, updated_at, summary, content,
url, symbols, source, images}. Free tier, symbol-tagged, real-time.
"""
from .client import get

# Alpaca caps `limit` at 50 per request; we page under the hood to honor larger
# asks without making the caller deal with page tokens.
_MAX_PER_PAGE = 50


def get_news(
    symbols=None,
    *,
    limit: int = 10,
    start: str | None = None,
    end: str | None = None,
    sort: str = "desc",
    include_content: bool = False,
    exclude_contentless: bool = True,
) -> list:
    """Return recent news articles, newest first by default.

    symbols: one ticker ("AAPL"), an iterable of tickers, or None for the whole
        market feed. start/end: RFC-3339 / "YYYY-MM-DD" bounds (optional).
        include_content: pull full article body (heavier) vs summary only.
        exclude_contentless: drop headline-only items with no summary/body.

    Pages transparently until `limit` articles are collected (or the feed ends).
    """
    if isinstance(symbols, str):
        sym_param = symbols
    elif symbols:
        sym_param = ",".join(symbols)
    else:
        sym_param = None

    collected: list = []
    page_token: str | None = None
    while len(collected) < limit:
        params: dict = {
            "limit": min(_MAX_PER_PAGE, limit - len(collected)),
            "sort": sort,
            "include_content": str(include_content).lower(),
            "exclude_contentless": str(exclude_contentless).lower(),
        }
        if sym_param:
            params["symbols"] = sym_param
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        if page_token:
            params["page_token"] = page_token

        payload = get("v1beta1/news", **params)
        collected.extend(payload.get("news", []))
        page_token = payload.get("next_page_token")
        if not page_token:
            break

    return collected[:limit]
