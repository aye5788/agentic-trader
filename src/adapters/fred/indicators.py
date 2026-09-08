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


def _pct_rank(window: list, value: float) -> float | None:
    """Where `value` sits inside `window`, 0.0 = lowest seen, 1.0 = highest."""
    vals = [w["value"] for w in window]
    if len(vals) < 2:
        return None
    return round(sum(1 for v in vals if v <= value) / len(vals), 3)


def _change(window: list, days: int) -> float | None:
    """Change vs the observation closest to `days` ago (None if not covered)."""
    if len(window) < 2:
        return None
    from datetime import date, datetime, timedelta      # noqa: PLC0415
    try:
        asof = datetime.strptime(window[-1]["date"], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    target = asof - timedelta(days=days)
    prior = [w for w in window
             if datetime.strptime(w["date"], "%Y-%m-%d").date() <= target]
    if not prior:
        return None
    assert isinstance(asof, date)
    return round(window[-1]["value"] - prior[-1]["value"], 4)


def context(days: int = 400) -> dict:
    """Each indicator as a LEVEL PLUS ITS RECENT PAST -> {name: {...} | error}.

    `snapshot()` gives the latest print and nothing else, which is why the
    weekly letter could only ever recite "VIX 14.3 against a ceiling of 28" --
    a number with no story attached. This adds what makes a level mean
    something: how far it moved over the last week and month, and where it sits
    in its own year (`pct_1y`, 0.0 = the year's low, 1.0 = its high).

    ⛔ INTERPRETATION IS NOT DONE HERE, AND MUST NOT BE. No "calm", no
    "stressed", no thresholds. This module reports where the number is; what
    that means for a book is the reader's call. A judgement hardcoded here
    would become a rule nobody voted for -- the failure this repo has recorded
    repeatedly.

    Fails SOFT per indicator: one series erroring leaves the others intact and
    records `{"error": ...}` for that one, because a letter with two of three
    macro readings is worth writing and a missing reading must never read as a
    benign zero.
    """
    from .client import FredUnavailable, series_window        # noqa: PLC0415
    out = {}
    for name, sid in SERIES.items():
        try:
            window = series_window(sid, days=days)
        except (FredUnavailable, Exception) as e:             # noqa: BLE001
            out[name] = {"series_id": sid,
                         "error": f"{type(e).__name__}: {e}"}
            continue
        if not window:
            out[name] = {"series_id": sid, "error": "no observations returned"}
            continue
        latest = window[-1]
        vals = [w["value"] for w in window]
        out[name] = {
            "series_id": sid,
            "value": latest["value"],
            "as_of": latest["date"],
            "change_1w": _change(window, 7),
            "change_1m": _change(window, 30),
            "pct_1y": _pct_rank(window, latest["value"]),
            "min_1y": round(min(vals), 4),
            "max_1y": round(max(vals), 4),
            "observations": len(window),
        }
    return out
