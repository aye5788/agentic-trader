"""Maintain the daily OHLC panel for the universe (names + ETF sleeve + SPY) in
research_store/prices/*.parquet (git-ignored). Source: **moomoo**, via OpenD.

Was Schwab. Schwab's refresh token expired every 7 days and had to be renewed by
hand at a browser — a standing chore whose only job was keeping a price feed alive.
moomoo authenticates through the already-running OpenD gateway, so there is no
recurring credential work at all. Verified byte-identical on the switch: 166/168
tickers matched the cached Schwab close exactly, the other two by <0.15%.

DEFAULT MODE IS APPEND, not re-pull. The panel already holds ~10y from the Schwab
era; each run adds the current session's row. That is one `get_market_snapshot`
call for the whole universe (~0.2s, no quota) instead of 168 sequential history
pulls. moomoo meters `request_history_kline` against a hard **100 distinct stocks
account-wide**, so a full re-pull of a 168-name universe is IMPOSSIBLE — do not
reintroduce one.

    /usr/bin/python3 scripts/fetch_prices.py              # append today's bar
    /usr/bin/python3 scripts/fetch_prices.py --backfill 30  # gap-fill, <=100 names
    /usr/bin/python3 scripts/fetch_prices.py --selftest

⚠️ RUNTIME: must run under system /usr/bin/python3 (the moomoo SDK is not in .venv).
"""
import argparse
import datetime as dt
import sys
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

# moomoo meters request_history_kline against this many DISTINCT stocks,
# account-wide and cumulative — hit live on 2026-07-29 at exactly "stock: 100/100"
# with 98 of 168 universe names still unfetched. The snapshot path is unmetered,
# which is why the daily append uses it instead.
MOOMOO_HISTORY_QUOTA = 100

OUT_DIR = REPO / "research_store" / "prices"
OPENS = OUT_DIR / "opens.parquet"
HIGHS = OUT_DIR / "highs.parquet"
LOWS = OUT_DIR / "lows.parquet"
CLOSES = OUT_DIR / "closes.parquet"
META = OUT_DIR / "fetch_meta.csv"


def universe_tickers() -> list[str]:
    names = pd.read_csv(REPO / "config" / "universe.csv")["ticker"].tolist()
    etfs = pd.read_csv(REPO / "config" / "etf_universe.csv")["ticker"].tolist()
    # SPY = benchmark + regime proxy for $SPX. Dedupe, keep order.
    tickers = list(dict.fromkeys(names + etfs + ["SPY"]))
    return tickers


_FIELDS = ("open", "high", "low", "close")


def _read_panels() -> dict:
    """Load the cached OHLC panels. Missing files -> empty frames (first-ever run)."""
    paths = {"open": OPENS, "high": HIGHS, "low": LOWS, "close": CLOSES}
    return {f: (pd.read_parquet(p) if p.exists() else pd.DataFrame())
            for f, p in paths.items()}


def _merge_bars(panels: dict, bars: dict) -> dict:
    """Fold {ticker: candle} into the cached panels as one dated row. Pure.

    An existing row for the same date is UPDATED, not duplicated — so re-running
    after the close correctly upgrades a partial bar to the settled one, and running
    twice in a day is a no-op rather than a corruption. New tickers widen the panel;
    tickers absent from `bars` keep their history and get NaN for the new date.
    """
    if not bars:
        return panels
    out = {}
    for f in _FIELDS:
        col = {t: c[f] for t, c in bars.items()}
        dates = {pd.Timestamp(c["datetime"], unit="ms").normalize() for c in bars.values()}
        if len(dates) != 1:
            raise ValueError(f"snapshot spans {len(dates)} dates, expected 1: {sorted(dates)}")
        row = pd.DataFrame(col, index=[dates.pop()])
        base = panels.get(f)
        if base is None or base.empty:
            out[f] = row.sort_index()
            continue
        base = base.drop(index=row.index, errors="ignore")   # replace same-date row
        out[f] = pd.concat([base, row]).sort_index()
    return out


MARKET_TZ = ZoneInfo("America/New_York")
# RTH closes 16:00 ET; give Schwab a buffer to stamp the settled daily bar.
SETTLE_AFTER = dt.time(16, 15)


def _drop_unsettled_session(panels: dict, now_et: dt.datetime,
                            trading_day: bool | None = None) -> tuple[dict, str | None]:
    """Drop the current session's bar unless it is a REAL, settled trading day.

    Why this exists: we now pass `endDate` to Schwab (see `_try_pull`), which is
    the ONLY way to get today's bar at all — without it Schwab silently defaults
    `endDate` to the PREVIOUS trading day, so an 18:00 ET run ranked on
    yesterday's close (the 2026-07-23 regime-gate lag). But `endDate=now` during
    RTH returns a LIVE, partial bar whose `close` is just the last trade. Feeding
    that to momentum/the regime gate would rank the book on an intraday snapshot.

    So: before 16:15 ET, drop today's row (falls back to the old, correct-if-late
    behaviour); after it, keep it — that is the whole point of the fix.

    NON-TRADING DAYS (added 2026-08-10). The time-of-day test alone is not enough.
    On a Sunday the weekly 20:15 ET job passes "after 16:15" and the row was kept
    — but `get_market_snapshot` on a closed market returns the PREVIOUS session's
    close, so the row is a byte-identical duplicate of Friday stamped Sunday.
    Measured: 2026-08-02 and 2026-08-09 matched the prior Friday for 100% of 168
    names. Each one injects a 0% return, deflating sigma — and sigma sets stop
    distance, so stops end up tighter than the name's real volatility. Two rows
    cost only 0.05–0.23% today, but they accrue ~52/yr and never self-heal. Ten
    years of Schwab-era history contain none; this began with the moomoo feed.

    `trading_day`: True / False / None (calendar unavailable). The WEEKEND test is
    applied unconditionally and needs no network — Saturday and Sunday are never
    US trading days, so that half can never be wrong and works with OpenD down.
    `trading_day=False` additionally catches weekday holidays. Passing None
    degrades to weekend-only rather than dropping a real session: this panel is
    NON-REGENERABLE (history is capped at 100 distinct stocks account-wide), so
    wrongly discarding a genuine close is the expensive error.

    Returns (panels, dropped_date_iso | None). Pure: no network, no I/O, no clock.
    """
    close = panels.get("close")
    if close is None or close.empty:
        return panels, None
    last = close.index.max()
    today = now_et.date()
    if last.date() != today:
        return panels, None                      # nothing from today — nothing to drop
    if today.weekday() >= 5 or trading_day is False:
        # Market shut: whatever the feed returned is the prior session restamped.
        return ({f: p.drop(index=last, errors="ignore") for f, p in panels.items()},
                str(last.date()))
    if now_et.time() >= SETTLE_AFTER:
        return panels, None                      # settled close — keep it
    return {f: p.drop(index=last, errors="ignore") for f, p in panels.items()}, str(last.date())


def _field_panels(raw: dict) -> dict:
    """Turn {sym: [candle,...]} into one dates×tickers DataFrame per OHLC field.
    Pure: no network, no I/O. `close` panel is byte-for-byte what we cached before."""
    out = {f: {} for f in _FIELDS}
    for sym, candles in raw.items():
        for f in _FIELDS:
            out[f][sym] = pd.Series(
                {pd.Timestamp(c["datetime"], unit="ms").normalize(): c[f] for c in candles}
            )
    return {f: pd.DataFrame(series).sort_index() for f, series in out.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, metavar="DAYS",
                    help="gap-fill DAYS of history via request_history_kline. "
                         "Capped by moomoo at 100 DISTINCT stocks account-wide.")
    ap.add_argument("--dry", action="store_true", help="show the change, write nothing")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(REPO / "src"))
    from adapters.moomoo import prices as mmp          # noqa: PLC0415
    from adapters.moomoo.client import OpenDUnavailable, quote_ctx  # noqa: PLC0415

    tickers = universe_tickers()
    panels = _read_panels()
    before = panels["close"].shape if not panels["close"].empty else (0, 0)
    print(f"cached panel: {before[0]} dates x {before[1]} tickers"
          + (f", latest {panels['close'].index.max().date()}" if before[0] else " (empty)"))

    try:
        ctx = quote_ctx()
    except OpenDUnavailable as e:
        print(f"ABORT: {e}")
        print(f"  cache LEFT INTACT: {CLOSES}")
        raise SystemExit(2)

    meta, errs = [], {}
    try:
        if args.backfill:
            # Backfill is quota-metered. Only ask for names that actually need it,
            # and stop at 100 so the request doesn't fail mid-batch.
            end_d = dt.datetime.now(MARKET_TZ).date()
            start_d = end_d - dt.timedelta(days=args.backfill)
            have = panels["close"]
            need = [t for t in tickers
                    if have.empty or t not in have.columns
                    or have[t].loc[str(start_d):].isna().any()]
            if len(need) > MOOMOO_HISTORY_QUOTA:
                print(f"  {len(need)} tickers need backfill but moomoo's history quota is "
                      f"{MOOMOO_HISTORY_QUOTA} distinct stocks — taking the first "
                      f"{MOOMOO_HISTORY_QUOTA}. Re-run to continue.")
                need = need[:MOOMOO_HISTORY_QUOTA]
            print(f"backfilling {len(need)} tickers, {start_d} .. {end_d} (history API)")
            raw, errs = mmp.daily_panel(need, str(start_d), str(end_d), ctx=ctx)
            fetched = _field_panels(raw)
            for f in _FIELDS:                     # union old+new, new wins on overlap
                base = panels.get(f)
                panels[f] = (fetched[f] if base is None or base.empty
                             else fetched[f].combine_first(base).sort_index())
            meta = [(t, len(c), "ok") for t, c in raw.items()]
            got = len(raw)
            wanted = len(need)
        else:
            bars, errs = mmp.snapshot_ohlc(tickers, ctx=ctx)
            got, wanted = len(bars), len(tickers)
            print(f"snapshot: {got}/{wanted} bars (1 call, no history quota)")
            # SAFETY GUARD (2026-07-15 incident, preserved): a systemic failure must
            # never be written over a good cache. A few dead names failing is normal;
            # a majority failing means OpenD/network is broken.
            if got < max(1, wanted // 2):
                common = Counter(errs.values()).most_common(1)
                print(f"\nABORT: only {got}/{wanted} tickers returned a bar — systemic "
                      f"failure, not a few dead names.\n  most common: "
                      f"{common[0][0] if common else 'unknown'}")
                print(f"  cache LEFT INTACT: {CLOSES}")
                raise SystemExit(2)
            panels = _merge_bars(panels, bars)
            meta = [(t, 1, "ok") for t in bars]
        # Ask the calendar while the context is still open, so this costs no
        # extra connection. None = could not tell -> the weekend test still
        # applies; only weekday HOLIDAYS go unrecognised.
        now_et = dt.datetime.now(MARKET_TZ)
        trading_day = mmp.is_trading_day(now_et.date(), ctx=ctx)
    finally:
        ctx.close()

    panels, dropped = _drop_unsettled_session(panels, now_et, trading_day)
    if dropped:
        why = ("market CLOSED that day — the feed returns the prior session's "
               "close, so this row is a duplicate"
               if (now_et.date().weekday() >= 5 or trading_day is False)
               else f"before {SETTLE_AFTER.strftime('%H:%M')} ET — partial, not a close")
        print(f"  dropped session bar {dropped} ({why})")

    panel = panels["close"]
    print(f"panel now: {panel.shape[0]} dates x {panel.shape[1]} tickers, "
          f"{panel.index.min().date()} .. {panel.index.max().date()}")
    if errs:
        print(f"no bar ({len(errs)}): " + " ".join(sorted(errs)[:20])
              + (" ..." if len(errs) > 20 else ""))
        for t, e in list(errs.items())[:3]:
            print(f"    {t}: {str(e)[:100]}")

    if args.dry:
        print("[dry] nothing written")
        return

    field_to_path = {"open": OPENS, "high": HIGHS, "low": LOWS, "close": CLOSES}
    for field, path in field_to_path.items():
        try:
            panels[field].to_parquet(path)
        except Exception as e:  # never lose a full pull to a missing parquet engine
            fallback = path.with_suffix(".csv")
            panels[field].to_csv(fallback)
            print(f"WARN parquet write failed for {field} ({e}); wrote CSV -> {fallback}")
    pd.DataFrame(meta, columns=["ticker", "candles", "status"]).to_csv(META, index=False)
    print(f"wrote {CLOSES} (+ opens/highs/lows)")


def _selftest() -> None:
    raw = {
        "AAA": [
            {"datetime": 1609459200000, "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5},
            {"datetime": 1609545600000, "open": 10.5, "high": 12.0, "low": 10.0, "close": 11.8},
        ],
        "BBB": [
            {"datetime": 1609459200000, "open": 20.0, "high": 21.0, "low": 19.0, "close": 20.5},
        ],
    }
    panels = _field_panels(raw)
    assert set(panels) == {"open", "high", "low", "close"}, panels.keys()
    # close panel preserves the pre-refactor content/shape
    assert panels["close"].loc[panels["close"].index[1], "AAA"] == 11.8
    assert panels["high"].loc[panels["high"].index[0], "AAA"] == 11.0
    assert panels["low"].loc[panels["low"].index[0], "BBB"] == 19.0
    # sorted by date, aligned index across tickers
    assert list(panels["close"].index) == sorted(panels["close"].index)
    print("selftest OK: _field_panels open/high/low/close")

    # --- unsettled-session guard (2026-07-27 regime-lag fix) -------------------
    def _panels_ending(day: str) -> dict:
        idx = pd.to_datetime([pd.Timestamp(day) - pd.Timedelta(days=1), pd.Timestamp(day)])
        return {f: pd.DataFrame({"AAA": [1.0, 2.0]}, index=idx) for f in _FIELDS}

    def _et(day: str, h: int, m: int = 0) -> dt.datetime:
        return dt.datetime.combine(dt.date.fromisoformat(day), dt.time(h, m), tzinfo=MARKET_TZ)

    D = "2026-07-27"
    # mid-session: today's bar is a LIVE partial -> must be dropped
    p, dropped = _drop_unsettled_session(_panels_ending(D), _et(D, 10, 24))
    assert dropped == D, f"expected partial {D} bar dropped, got {dropped}"
    assert p["close"].index.max().date() == dt.date(2026, 7, 26), p["close"].index
    for f in _FIELDS:                                    # every field, not just close
        assert len(p[f]) == 1, (f, p[f])
    # after settle: today's bar is the real close -> must be KEPT (the whole fix)
    p, dropped = _drop_unsettled_session(_panels_ending(D), _et(D, 18, 3))
    assert dropped is None and p["close"].index.max().date() == dt.date(2026, 7, 27)
    # boundary is inclusive at 16:15
    _, dropped = _drop_unsettled_session(_panels_ending(D), _et(D, 16, 15))
    assert dropped is None, "16:15 ET must count as settled"
    _, dropped = _drop_unsettled_session(_panels_ending(D), _et(D, 16, 14))
    assert dropped == D, "16:14 ET is still unsettled"
    # panel that doesn't reach today (weekend/holiday run) -> untouched
    _, dropped = _drop_unsettled_session(_panels_ending("2026-07-24"), _et(D, 10, 0))
    assert dropped is None, "no bar for today -> nothing to drop"
    # empty panel must not explode
    _, dropped = _drop_unsettled_session({f: pd.DataFrame() for f in _FIELDS}, _et(D, 10, 0))
    assert dropped is None

    # --- non-trading days (2026-08-10) --------------------------------------
    # A Sunday 20:15 ET run passes the settle test but the market was SHUT, so
    # the feed returned Friday's close restamped. Must drop, calendar or not.
    SUN, SAT = "2026-08-09", "2026-08-08"
    for day in (SUN, SAT):
        _, dropped = _drop_unsettled_session(_panels_ending(day), _et(day, 20, 15))
        assert dropped == day, f"{day} is a weekend — must drop even after settle"
        # ...and the weekend test must not need the calendar to be right
        _, dropped = _drop_unsettled_session(_panels_ending(day), _et(day, 20, 15),
                                             trading_day=None)
        assert dropped == day, f"{day} must drop with no calendar available"
        # ...nor may a wrong calendar answer override it
        _, dropped = _drop_unsettled_session(_panels_ending(day), _et(day, 20, 15),
                                             trading_day=True)
        assert dropped == day, f"{day} must drop even if the calendar says otherwise"

    # A weekday HOLIDAY is only caught when the calendar says so.
    HOL = "2026-12-25"                                   # Friday, market closed
    assert dt.date.fromisoformat(HOL).weekday() < 5, "fixture must be a weekday"
    _, dropped = _drop_unsettled_session(_panels_ending(HOL), _et(HOL, 20, 15),
                                         trading_day=False)
    assert dropped == HOL, "a weekday holiday must drop when the calendar says closed"
    _, dropped = _drop_unsettled_session(_panels_ending(HOL), _et(HOL, 20, 15),
                                         trading_day=None)
    assert dropped is None, ("unknown calendar must NOT discard a weekday bar — "
                            "this panel is non-regenerable")

    # A normal settled weekday is still KEPT (the original fix must survive).
    _, dropped = _drop_unsettled_session(_panels_ending(D), _et(D, 18, 3),
                                         trading_day=True)
    assert dropped is None, "a real settled session must still be kept"
    print("selftest OK: _drop_unsettled_session (partial dropped, settled kept, "
          "weekend/holiday duplicates dropped)")

    # --- _merge_bars: the append must never corrupt the 10y panel ---------------
    idx = pd.to_datetime(["2026-07-27", "2026-07-28"])
    cached = {f: pd.DataFrame({"AAA": [1.0, 2.0], "BBB": [10.0, 20.0]}, index=idx)
              for f in _FIELDS}
    ms = int(pd.Timestamp("2026-07-29").timestamp() * 1000)
    bars = {"AAA": {"datetime": ms, "open": 3.0, "high": 3.5, "low": 2.5, "close": 3.2},
            "CCC": {"datetime": ms, "open": 9.0, "high": 9.5, "low": 8.5, "close": 9.2}}
    m = _merge_bars(cached, bars)
    c = m["close"]
    assert len(c) == 3, f"one new dated row, got {len(c)}"
    assert c.loc["2026-07-29", "AAA"] == 3.2, c
    assert c.loc["2026-07-28", "AAA"] == 2.0, "history must be preserved verbatim"
    assert pd.isna(c.loc["2026-07-29", "BBB"]), "ticker with no bar -> NaN, not stale carry"
    assert pd.isna(c.loc["2026-07-27", "CCC"]), "new ticker gets NaN history, not backfill"
    assert c.loc["2026-07-28", "BBB"] == 20.0, "existing ticker history intact"
    assert list(c.index) == sorted(c.index), "index must stay sorted"
    for f in _FIELDS:                                   # all four panels, not just close
        assert m[f].shape == (3, 3), (f, m[f].shape)

    # re-running the same day UPDATES the row (partial -> settled), never duplicates
    bars2 = dict(bars); bars2["AAA"] = {**bars["AAA"], "close": 3.9}
    m2 = _merge_bars(m, bars2)
    assert len(m2["close"]) == 3, f"same-date re-run must not duplicate, got {len(m2['close'])}"
    assert m2["close"].loc["2026-07-29", "AAA"] == 3.9, "settled close must overwrite partial"

    # empty bars (OpenD returned nothing) must be a no-op, never a wipe
    assert _merge_bars(cached, {})["close"].equals(cached["close"]), "empty -> untouched"

    # a mixed-date snapshot means something is wrong upstream; refuse it
    try:
        _merge_bars(cached, {"AAA": {**bars["AAA"]},
                             "CCC": {**bars["CCC"],
                                     "datetime": int(pd.Timestamp("2026-07-30").timestamp() * 1000)}})
        raise AssertionError("mixed-date snapshot must raise")
    except ValueError as e:
        assert "spans 2 dates" in str(e), e

    # first-ever run: empty cache + bars -> a valid one-row panel
    fresh = _merge_bars({f: pd.DataFrame() for f in _FIELDS}, bars)
    assert fresh["close"].shape == (1, 2), fresh["close"].shape
    print("selftest OK: _merge_bars (append, same-date update, no-op, mixed-date guard)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
