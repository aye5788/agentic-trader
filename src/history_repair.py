"""Does a universe member have the price history `momentum.compute()` needs?

⛔ THE DEFECT THIS EXISTS TO PREVENT (2026-09-06). `config/universe.csv` is
rewritten every Friday by an UNATTENDED `scripts/universe_refresh.py`. The daily
price path (`scripts/fetch_prices.py`, no args) is an APPEND: one unmetered
`get_market_snapshot` that adds the current session's bar. Neither one has ever
fetched history. So a name the screen admits starts life as a column holding a
single close and gains one bar a day — `momentum.compute()` drops it silently,
and the book cannot rank a name it was just told to consider.

That gap used to be closed by a HUMAN. The 2026-08-21 rotation was applied by
hand and followed by a manual `fetch_prices.py --backfill`, which is why those
eight names carry ~280 observations each (commit `dbaf602` records it). The
refresh was later automated; the backfill was not. The 08-28 cohort (SLB, ARM,
ABNB, SNPS) and the 09-04 cohort (HPE) were auto-admitted with nobody to run it,
and sat unscoreable — ARM among them, three years listed.

⛔ SUFFICIENCY IS A WINDOW TEST, NEVER A COLUMN TEST. The two intuitive checks
are both wrong:

  - "does the column exist?" — a name that LEAVES the universe keeps its column,
    frozen at its departure date (20 such orphans today). Re-admit it and the
    column is present, long, and stale in the middle of the very window the
    signal reads.
  - "how many non-null closes are there?" — `momentum.compute()` does not count
    a lifetime. It requires the last `TREND_MA` rows to be ENTIRELY non-null and
    the row `LOOKBACK` back to be present. A name with 2,500 observations and a
    two-week hole inside the trend window is not scoreable.

So the predicate below mirrors `momentum.compute()`'s own `valid` mask, and the
constants are IMPORTED from it rather than restated — if the signal's window
ever changes, the repair target follows it instead of silently diverging.

⛔ AND YOUTH IS NOT A DEFECT. A genuinely new listing cannot have 252 sessions
and never will until it seasons; SPCX (listed 2026-06-12) and SKHY (2026-07-10)
hold every bar that exists for them. Treating those as failures would spend
metered history quota every single day fetching bars that do not exist. The
listing date is what separates "too young to have it" from "old enough that we
should have it", and it comes from an UNMETERED metadata call — never from
spending a history-quota unit to discover a name is young.

Pure: no I/O, no clock, no network. `today` and the listing dates are injected.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

# ⛔ IMPORTED, NOT RESTATED. The repair must target exactly the window the signal
# reads; a second copy of these numbers is a second definition that can drift.
from momentum import LOOKBACK, TREND_MA

# `momentum.compute()` slices `hist.iloc[-(lookback + 1):]` -- lookback returns
# need lookback+1 closes.
REQUIRED_ROWS = LOOKBACK + 1

# Calendar days a listing must have existed for REQUIRED_ROWS *trading* days to
# be obtainable AT ALL. 253 sessions is ~367 calendar days; the margin absorbs
# holiday clustering so a name is not called "mature" the week before it can
# actually satisfy the window.
MIN_LISTED_DAYS = 370

# How far back a repair pull reaches. Deliberately LONGER than MIN_LISTED_DAYS:
# a name that has only just crossed the seasoning threshold must come back with
# a complete window, not one that is exactly one bar wide at the far end.
BACKFILL_DAYS = 420

# moomoo's UNKNOWN listing date. 76.7% of US names carry it (measured 2026-09-06,
# 3,234 of 4,214) -- it is a 1970-01-01 epoch default, NOT an IPO on that day.
# Every genuinely recent listing carries a real date: of 466 US listings under
# 400 days old, 466 had real dates and ZERO carried this sentinel. So the
# sentinel is positive evidence a name is NOT a recent listing.
UNKNOWN_LISTING = dt.date(1970, 1, 1)


def window_complete(series: pd.Series) -> bool:
    """True when `series` carries everything `momentum.compute()`'s `valid` mask
    needs, evaluated the same positional way the signal evaluates it.

    Mirrors, in order: enough rows at all; `close_t` present; `close_0` (the row
    LOOKBACK back) present; and the whole TREND_MA trend window non-null, which
    is the clause that makes this a window test rather than a count.
    """
    if series is None or len(series) < REQUIRED_ROWS:
        return False
    window = series.iloc[-REQUIRED_ROWS:]
    if pd.isna(window.iloc[-1]) or pd.isna(window.iloc[0]):
        return False
    trend = series.iloc[-TREND_MA:]
    return int(trend.notna().sum()) >= TREND_MA


def incomplete(close_panel: pd.DataFrame, tickers) -> list:
    """Which `tickers` cannot currently be scored for want of history.

    A ticker with NO column is incomplete (a brand-new admission), and so is one
    whose column exists but fails the window test (a re-admitted orphan).
    """
    if close_panel is None or close_panel.empty:
        return sorted(set(tickers))
    out = [t for t in set(tickers)
           if t not in close_panel.columns or not window_complete(close_panel[t])]
    return sorted(out)


def is_seasoning(listed_on, today: dt.date, min_days: int = MIN_LISTED_DAYS) -> bool:
    """True ONLY when the listing is POSITIVELY known to be too young.

    ⛔ Unknown age (None -- absent from the roster, or moomoo's 1970 sentinel) is
    NOT seasoning. The failure directions are not symmetric: calling a mature
    name "young" leaves a tradeable name permanently unscoreable and silent,
    while calling an unknown-age name "mature" costs at most one history request
    that returns what it returns. So unknown fails toward attempting the repair.
    """
    if listed_on is None or listed_on == UNKNOWN_LISTING:
        return False
    return (today - listed_on).days < min_days


def plan_repair(close_panel: pd.DataFrame, tickers, listing_dates: dict,
                today: dt.date, quota: int = None) -> dict:
    """Split `tickers` into what to repair now, what is legitimately seasoning,
    and what is already fine.

    Returns {"repair", "seasoning", "complete", "deferred"}. `deferred` is the
    overflow past `quota` -- reported, never silently dropped, because a name
    that was needed and skipped is exactly the thing that went unnoticed for the
    two weeks this module exists to end.
    """
    need = incomplete(close_panel, tickers)
    complete = sorted(set(tickers) - set(need))
    seasoning = [t for t in need if is_seasoning(listing_dates.get(t), today)]
    repair = [t for t in need if t not in set(seasoning)]
    deferred = []
    if quota is not None and len(repair) > quota:
        repair, deferred = repair[:quota], repair[quota:]
    return {"repair": repair, "seasoning": seasoning,
            "complete": complete, "deferred": deferred}


def backfill_window(today: dt.date, days: int = BACKFILL_DAYS) -> tuple:
    """(start, end) ISO dates for a repair pull."""
    return str(today - dt.timedelta(days=days)), str(today)


def _selftest() -> None:
    import numpy as np

    idx = pd.date_range("2024-01-01", periods=400, freq="B")

    def _full():
        return pd.Series(np.linspace(10.0, 20.0, len(idx)), index=idx)

    today = dt.date(2026, 9, 6)
    mature = dt.date(2020, 1, 2)          # comfortably older than MIN_LISTED_DAYS
    young = today - dt.timedelta(days=60)  # a genuine fresh listing

    # ---- the window predicate mirrors momentum.compute -----------------------
    assert window_complete(_full()), "a full column must be scoreable"
    short = _full().copy()
    short.iloc[:-REQUIRED_ROWS + 1] = np.nan          # one row too few
    assert not window_complete(short), "REQUIRED_ROWS is the floor"
    exact = _full().copy()
    exact.iloc[:-REQUIRED_ROWS] = np.nan              # exactly enough
    assert window_complete(exact), "exactly REQUIRED_ROWS must pass"

    # ⛔ the clause that makes this a WINDOW test and not a COUNT test
    holed = _full().copy()
    holed.iloc[-50:-40] = np.nan                      # hole INSIDE the trend window
    assert holed.notna().sum() > REQUIRED_ROWS, "still has plenty of observations"
    assert not window_complete(holed), "a hole in the trend window is not scoreable"

    # a hole OUTSIDE the trend window but inside the lookback is tolerated,
    # because momentum.compute() tolerates it -- do not over-fetch.
    older_hole = _full().copy()
    older_hole.iloc[-250:-245] = np.nan
    assert window_complete(older_hole), "must not repair what the signal accepts"

    # ---- 1. mature newly-admitted name: a few bars -> REPAIR -----------------
    new = pd.Series(np.nan, index=idx)
    new.iloc[-6:] = 12.0
    panel = pd.DataFrame({"FULL": _full(), "NEWMATURE": new})
    p = plan_repair(panel, ["FULL", "NEWMATURE"],
                    {"FULL": mature, "NEWMATURE": mature}, today)
    assert p["repair"] == ["NEWMATURE"], p
    assert p["complete"] == ["FULL"], p

    # ---- 2. genuine fresh IPO: too young -> SEASONING, never repaired --------
    p = plan_repair(panel, ["FULL", "NEWMATURE"],
                    {"FULL": mature, "NEWMATURE": young}, today)
    assert p["repair"] == [], p
    assert p["seasoning"] == ["NEWMATURE"], p
    # ...and it stays out of the repair set on every subsequent run, so a young
    # listing can never burn quota fetching bars that do not exist.
    for _ in range(3):
        assert plan_repair(panel, ["NEWMATURE"], {"NEWMATURE": young},
                           today)["repair"] == [], "young name re-selected"

    # ---- 3. re-admitted orphan: long, stale column -> REPAIR -----------------
    # Column exists and carries MORE observations than a freshly repaired name,
    # but the recent window is frozen. Count says fine; window says no.
    stale = _full().copy()
    stale.iloc[-30:] = np.nan                         # froze 30 sessions ago
    panel2 = pd.DataFrame({"ORPHAN": stale})
    assert stale.notna().sum() > REQUIRED_ROWS, "long history, deliberately"
    p = plan_repair(panel2, ["ORPHAN"], {"ORPHAN": mature}, today)
    assert p["repair"] == ["ORPHAN"], p

    # ---- 4. complete mature name -> NO backfill ------------------------------
    p = plan_repair(pd.DataFrame({"FULL": _full()}), ["FULL"], {"FULL": mature}, today)
    assert p["repair"] == [] and p["complete"] == ["FULL"], p

    # ---- 5. unknown / sentinel age must NOT get the young exemption ----------
    for unknown in (None, UNKNOWN_LISTING):
        p = plan_repair(panel, ["NEWMATURE"], {"NEWMATURE": unknown}, today)
        assert p["repair"] == ["NEWMATURE"], f"unknown age must repair: {unknown} {p}"
        assert p["seasoning"] == [], p
    # a ticker missing from the roster entirely is the same case
    p = plan_repair(panel, ["NEWMATURE"], {}, today)
    assert p["repair"] == ["NEWMATURE"], p

    # a column that does not exist at all is incomplete, not an error
    assert incomplete(pd.DataFrame({"FULL": _full()}), ["FULL", "ABSENT"]) == ["ABSENT"]

    # ---- quota overflow is reported, never silently dropped ------------------
    many = pd.DataFrame({t: new for t in ("A", "B", "C")})
    p = plan_repair(many, ["A", "B", "C"], {}, today, quota=2)
    assert len(p["repair"]) == 2 and p["deferred"] == ["C"], p

    # ---- seasoning boundary --------------------------------------------------
    assert is_seasoning(today - dt.timedelta(days=MIN_LISTED_DAYS - 1), today)
    assert not is_seasoning(today - dt.timedelta(days=MIN_LISTED_DAYS), today)

    s, e = backfill_window(today)
    assert e == str(today) and s < str(today - dt.timedelta(days=MIN_LISTED_DAYS)), (s, e)
    print("history_repair selftest OK: window mirrors momentum, youth exempt, "
          "unknown age repairs, orphans caught")


if __name__ == "__main__":
    _selftest()
