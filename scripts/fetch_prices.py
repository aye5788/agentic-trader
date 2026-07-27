"""Pull ~10y daily closes for the whole universe (names + ETF sleeve + SPY) from
Schwab and cache to research_store/prices/closes.parquet (git-ignored).

Idempotent-ish: skips the network pull if the cache exists and --force isn't set.
Run once; the backtest reads the cache. Records tickers that failed / are too
short (fresh IPOs) so the backtest can drop them honestly.

    python scripts/fetch_prices.py [--force] [--years 10]
"""
import argparse
import datetime as dt
import sys
import time
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

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


def _try_pull(sym: str, years: int):
    """Return (list[candle] | None, err_msg | None). Try a couple symbol spellings
    Schwab wants. On failure return the last error so the caller can tell a dead
    ticker apart from a systemic outage (expired token / locked DB / network)."""
    from adapters.schwab import research  # noqa: E402
    last_err = None
    # end_date is REQUIRED to see the current session: Schwab defaults endDate to
    # the previous trading day, so omitting it returns yesterday's panel even at
    # 18:00 ET. The (possibly partial) today bar is filtered by
    # _drop_unsettled_session before anything ranks on it.
    now = dt.datetime.now(MARKET_TZ)
    for cand in (sym, sym.replace(".", "/")):  # BRK.B -> BRK/B fallback
        try:
            ph = research.get_price_history(
                cand, period_type="year", period=years, frequency_type="daily",
                end_date=now,
            )
            if ph:
                return ph, None
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
    return None, last_err


_FIELDS = ("open", "high", "low", "close")


MARKET_TZ = ZoneInfo("America/New_York")
# RTH closes 16:00 ET; give Schwab a buffer to stamp the settled daily bar.
SETTLE_AFTER = dt.time(16, 15)


def _drop_unsettled_session(panels: dict, now_et: dt.datetime) -> tuple[dict, str | None]:
    """Drop the current session's bar unless it has closed AND settled.

    Why this exists: we now pass `endDate` to Schwab (see `_try_pull`), which is
    the ONLY way to get today's bar at all — without it Schwab silently defaults
    `endDate` to the PREVIOUS trading day, so an 18:00 ET run ranked on
    yesterday's close (the 2026-07-23 regime-gate lag). But `endDate=now` during
    RTH returns a LIVE, partial bar whose `close` is just the last trade. Feeding
    that to momentum/the regime gate would rank the book on an intraday snapshot.

    So: before 16:15 ET, drop today's row (falls back to the old, correct-if-late
    behaviour); after it, keep it — that is the whole point of the fix.

    Returns (panels, dropped_date_iso | None). Pure: no network, no I/O, no clock.
    """
    close = panels.get("close")
    if close is None or close.empty:
        return panels, None
    last = close.index.max()
    today = now_et.date()
    if last.date() != today:
        return panels, None                      # nothing from today — nothing to drop
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
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--years", type=int, default=10)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if CLOSES.exists() and not args.force:
        df = pd.read_parquet(CLOSES)
        print(f"cache exists: {CLOSES} ({df.shape[0]} rows x {df.shape[1]} tickers). "
              f"--force to re-pull.")
        return

    tickers = universe_tickers()
    print(f"pulling {len(tickers)} tickers, {args.years}y daily from Schwab...")
    raw = {}
    meta = []
    errors = []
    for i, sym in enumerate(tickers, 1):
        ph, err = _try_pull(sym, args.years)
        if not ph:
            meta.append((sym, 0, "FAILED"))
            if err:
                errors.append(err)
            print(f"  [{i:3}/{len(tickers)}] {sym:6} FAILED" + (f"  ({err[:90]})" if err else ""))
            continue
        raw[sym] = ph
        meta.append((sym, len(ph), "ok"))
        if i % 25 == 0:
            print(f"  [{i:3}/{len(tickers)}] ... {sym} ({len(ph)} candles)")
        time.sleep(0.6)

    ok = sum(1 for _, _, st in meta if st == "ok")
    # SAFETY GUARD (2026-07-15 incident): never overwrite a good cache with a
    # systemic-failure result. A handful of dead/illiquid names failing is normal;
    # a majority failing means auth/lock/network is broken. Writing that
    # (near-)empty panel would DESTROY the existing 10y cache — which is exactly
    # what turned a token hiccup into "everything's gone". Abort loudly instead
    # (non-zero exit -> cron ERR-trap phone alert) and leave the cache untouched.
    if ok < max(1, len(tickers) // 2):
        reason = Counter(errors).most_common(1)
        reason = reason[0][0] if reason else "unknown (no error captured)"
        print(f"\nABORT: only {ok}/{len(tickers)} tickers fetched — systemic "
              f"failure, not a few dead names.\n  most common error: {reason}")
        if CLOSES.exists():
            print(f"  existing cache LEFT INTACT (not overwritten): {CLOSES}")
        raise SystemExit(2)

    panels = _field_panels(raw)
    panels, dropped = _drop_unsettled_session(panels, dt.datetime.now(MARKET_TZ))
    if dropped:
        print(f"  dropped UNSETTLED session bar {dropped} (before "
              f"{SETTLE_AFTER.strftime('%H:%M')} ET — partial, not a close)")
    field_to_path = {"open": OPENS, "high": HIGHS, "low": LOWS, "close": CLOSES}
    for field, path in field_to_path.items():
        try:
            panels[field].to_parquet(path)
        except Exception as e:  # never lose a full pull to a missing parquet engine
            fallback = path.with_suffix(".csv")
            panels[field].to_csv(fallback)
            print(f"WARN parquet write failed for {field} ({e}); wrote CSV -> {fallback}")
    panel = panels["close"]   # keep existing summary prints working
    pd.DataFrame(meta, columns=["ticker", "candles", "status"]).to_csv(META, index=False)

    failed = [t for t, _, st in meta if st != "ok"]
    short = [t for t, n, st in meta if st == "ok" and n < 300]
    print(f"\ndone: {ok}/{len(tickers)} ok -> {CLOSES}")
    if not panel.empty:
        print(f"panel: {panel.shape[0]} dates x {panel.shape[1]} tickers, "
              f"{panel.index.min().date()} .. {panel.index.max().date()}")
    if failed:
        print(f"FAILED ({len(failed)}):", " ".join(failed))
    if short:
        print(f"SHORT <300 candles ({len(short)}):", " ".join(short))


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
    print("selftest OK: _drop_unsettled_session (partial dropped, settled kept)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
