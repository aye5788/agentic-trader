"""Analyst / estimates research over Finnhub — the slice Schwab can't provide.

SLOW-LOOP ONLY (free tier ~60 calls/min; keep watchlists modest).

The signal we actually value here is earnings SURPRISES and recommendation/estimate
TRENDS (documented anomalies — post-earnings drift, estimate-revision momentum),
NOT the raw price-target *level*, which is a weak, biased signal. Some endpoints
(price target, EPS estimates) are premium-only and raise PremiumEndpoint on the
free tier — callers should treat those as optional.
"""
from .client import get


def get_recommendation_trends(symbol: str) -> list:
    """Analyst buy/hold/sell counts over recent months (most recent first).

    Each row: {period, strongBuy, buy, hold, sell, strongSell}. FREE tier.
    Use the *trend* (are analysts migrating toward buy or sell?), not the level.
    """
    return get("stock/recommendation", symbol=symbol)


def get_earnings_surprises(symbol: str, limit: int = 8) -> list:
    """Historical quarterly EPS actual-vs-estimate surprises. FREE tier.

    Each row: {period, actual, estimate, surprise, surprisePercent}.
    The single most useful Finnhub signal for this system.
    """
    return get("stock/earnings", symbol=symbol, limit=limit)


def get_basic_financials(symbol: str) -> dict:
    """Company metrics: valuation, margins, 52wk range, growth, etc. FREE tier.

    Returns the full {metric, series, metricType, symbol} payload; the useful
    part is the `metric` dict. Overlaps Schwab fundamentals but adds ratios/
    52-week stats Schwab doesn't surface.
    """
    return get("stock/metric", symbol=symbol, metric="all")


def get_price_target(symbol: str) -> dict:
    """Consensus price target {targetHigh, targetLow, targetMean, targetMedian}.

    Often PREMIUM-only -> raises PremiumEndpoint on the free tier. We treat the
    target level as a weak signal anyway, so this is optional/best-effort.
    """
    return get("stock/price-target", symbol=symbol)


def get_eps_estimates(symbol: str, freq: str = "quarterly") -> dict:
    """Forward consensus EPS estimates. Often PREMIUM-only on the free tier."""
    return get("stock/eps-estimate", symbol=symbol, freq=freq)
