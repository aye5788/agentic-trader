"""Named macro regime indicators from FRED (supplementary to the Schwab trend gate).

These *confirm* the mechanical regime floor; they don't replace it — the load-
bearing gate (index > 50-day MA) is Schwab-computed and FRED-independent. Each
value carries a `stale` flag so the regime logic knows when it's using a cached
last-good number (FRED hiccup) rather than fresh data.
"""
from .client import series_latest

# series_id → friendly name
SERIES = {
    "vix": "VIXCLS",            # CBOE VIX (equity fear gauge)
    "yield_curve_10y2y": "T10Y2Y",   # 10y-2y Treasury spread (inversion = recession flag)
    "hy_spread": "BAMLH0A0HYM2",     # ICE BofA US High-Yield OAS (credit stress)
}


def get_vix() -> dict | None:
    """Latest VIX close: {value, date, stale}."""
    return series_latest(SERIES["vix"])


def get_yield_curve() -> dict | None:
    """Latest 10y-2y spread (negative = inverted)."""
    return series_latest(SERIES["yield_curve_10y2y"])


def get_hy_spread() -> dict | None:
    """Latest high-yield OAS (wider = risk-off / credit stress)."""
    return series_latest(SERIES["hy_spread"])


def snapshot() -> dict:
    """All regime indicators at once, keyed by friendly name (value or None each)."""
    return {name: series_latest(sid) for name, sid in SERIES.items()}
