"""SLOW LOOP — compute today's target book and write it to the Research Store.

This is the deterministic brain (docs/STRATEGY.md, "run as code, do not eyeball").
It does NOT trade. It produces the research product the fast loop later executes.

  1. Load recent closes for the 150 names + 18 ETFs + SPY.
  2. Regime floor: SPY (proxy $SPX) > 50DMA  AND  VIX <= [regime].vix_ceiling
     (VIX from a live Schwab $VIX quote, FRED VIXCLS prior close as fallback;
     both unavailable -> gate skipped FAIL-OPEN, the trend floor still rules).
  3. Rank both engines with the momentum signal (src/momentum.py).
  4. Select the book (top-10, banded) + sleeve (top-4).
  5. Attach IBD trade geometry per name (entry_zone, vol-adjusted stop, +5/+10%
     targets) and the reward:risk >= 2 gate — a name that can't be given >=2:1
     geometry is NOT held (STRATEGY.md §4); its slot stays cash.
  6. write_product() -> the Research Store validates against the [risk] mandate
     and persists current.json (belief) + journal.

    python scripts/slow_loop.py [--dry]   # --dry: print, don't write the store

Regime-off -> NO NEW single-name entries; names already held are kept while
they stay in the band (see regime_filter). Nothing-eligible -> an empty book
is still a valid, intended state (cash).
"""
import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import momentum as mom          # noqa: E402
import strategy as strat        # noqa: E402
from research_store import read_current, write_product         # noqa: E402
from research_store.models import Thesis, ResearchProduct      # noqa: E402
from research_store.validate import reward_risk                # noqa: E402

PANEL = REPO / "research_store" / "prices" / "closes.parquet"


def fetch_vix() -> tuple[float | None, str]:
    """Current VIX level for the regime ceiling, from FRED VIXCLS.

    Was a live Schwab '$VIX' quote with FRED as fallback; the Schwab branch is gone
    with the rest of that adapter, so FRED is now the only source. Practical effect
    is small — VIXCLS is the settled prior close rather than an intraday tick, and
    the slow loop runs pre-open anyway, so the prior close is the number a pre-open
    regime decision should use.

    Returns (None, reason) on failure — the caller treats that FAIL-OPEN (VIX gate
    skipped, trend floor still gates) because a data outage should not push the book
    to cash."""
    try:
        from adapters.fred import indicators
        v = indicators.get_vix()
        if v and v.get("value") is not None:
            stale = " stale" if v.get("stale") else ""
            return float(v["value"]), f"FRED VIXCLS {v.get('date', '')}{stale}"
    except Exception as e:
        print(f"  VIX via FRED failed ({type(e).__name__})")
    return None, "unavailable (FRED) — gate skipped fail-open"


def geometry(price: float, sigma: float, stop_mult: float, r_mults: list[float]) -> dict:
    """IBD trade geometry, made self-consistent by scaling BOTH stop and targets
    to the name's own volatility (params from [trade_management]). stop =
    stop_mult * daily-sigma below entry; targets at r_mults x that risk distance
    -> reward:risk >= r_mults[0] by construction. For a ~2%/day stock this is
    near IBD's classic stop/target sizing; a 5%/day semi scales up in step,
    instead of being rejected by a fixed +5% target it can never reach at 2:1."""
    entry = price
    stop_dist = stop_mult * float(sigma)                    # fractional risk
    stop = entry * (1.0 - stop_dist)
    targets = [round(entry * (1.0 + r * stop_dist), 4) for r in r_mults]
    return {"entry_zone": [round(price * 0.995, 4), round(price * 1.005, 4)],
            "stop": round(stop, 4), "targets": targets}


def build_theses(sel, scored, closes, asof, per_slot, tm, start_rank):
    """Turn a selection list into weighted Thesis records with geometry, dropping
    any name that can't clear reward:risk >= 2 (it doesn't get held). `tm` is the
    [trade_management] config table (stop_atr_mult, target_r_mults)."""
    held, dropped = [], []
    rank = start_rank
    for sym in sel:
        row = scored.loc[sym]
        price = float(closes.loc[asof, sym])
        g = geometry(price, row["sigma"], tm["stop_atr_mult"], tm["target_r_mults"])
        t = Thesis(symbol=sym, rank=rank, verdict="buy",
                   thesis=f"momentum rank {int(row['rank'])}: score {row['score']:.2f} "
                          f"(R={row['R']:.1%}, trend={row['trend']:.1%})",
                   entry_zone=g["entry_zone"], stop=g["stop"], targets=g["targets"],
                   target_weight=round(per_slot, 4),
                   confidence=round(float(row["score"]), 3),
                   signals={"score": round(float(row["score"]), 4),
                            "R": round(float(row["R"]), 4),
                            "trend": round(float(row["trend"]), 4),
                            "sigma": round(float(row["sigma"]), 5),
                            "rank": int(row["rank"]), "source": "momentum.compute"},
                   as_of=str(asof.date()),
                   review_by=f"{(asof + pd.Timedelta(days=7)).date()} (weekly rebalance)")
        rr = reward_risk(t)
        if rr is None or rr < strat.load()["risk"]["min_reward_risk"]:
            t.verdict, t.target_weight = "avoid", 0.0
            dropped.append((sym, rr, row["sigma"]))
        else:
            held.append(t)
        rank += 1
    return held, dropped


SECTOR_ETFS = ["XLE", "XLF", "XLK", "XLV", "XLI", "XLP",
               "XLY", "XLU", "XLB", "XLRE", "XLC"]


def stamp_earnings(theses, lookup) -> int:
    """Stamp each thesis with its next real report date. Returns how many bound.

    Split out and injected (`lookup(symbol) -> "YYYY-MM-DD" | None`) so the
    binding is testable without Finnhub. Fails OPEN by design: a lookup that
    raises or returns nothing leaves `earnings_date=None`, which downstream
    means "unknown", never "no earnings". A guard must never invent certainty.

    Before 2026-08-05 nothing populated this at all, and `risk_review`'s
    earnings flag read `review_by` — which this loop always writes as
    "(weekly rebalance)". The flag therefore fired zero times in production
    while its selftest passed green. See src/controls.py.
    """
    n = 0
    for t in theses:
        try:
            d = lookup(t.symbol)
        except Exception:
            d = None
        if d:
            t.earnings_date = str(d)[:10]
            n += 1
    return n


def residual_kwargs(cfg, closes, spy):
    """Build the BOOK-compute residual kwargs from [signal] config.

    residual_tilt<=0 or residual_factors="none" -> {} (plain momentum, unchanged).
    "sector" -> {residual_tilt, factors=<11 SPDR ETF closes present in cache>}.
    "market" -> {residual_tilt, market=spy}. Missing sector data or an unknown
    mode falls back to {} (plain rank) with a printed note — never crashes the loop.
    Pure: reads config + panels, no I/O.
    """
    sig = cfg.get("signal", {})
    tilt = float(sig.get("residual_tilt", 0.0) or 0.0)
    mode = str(sig.get("residual_factors", "none")).lower()
    if tilt <= 0.0 or mode == "none":
        return {}
    if mode == "market":
        return {"residual_tilt": tilt, "market": spy}
    if mode == "sector":
        have = [s for s in SECTOR_ETFS if s in closes.columns]
        if not have:
            print("  residual: no sector ETFs in price cache -> plain rank this run")
            return {}
        return {"residual_tilt": tilt, "factors": closes[have]}
    print(f"  residual: unknown residual_factors={mode!r} -> plain rank this run")
    return {}


def owned_symbols(path: Path | None = None) -> set[str] | None:
    """Symbols ACTUALLY held at the broker. -> set, or None if unreadable.

    None means "unknown", and it is distinct from the empty set: an empty set is
    "we own nothing", which under regime-off means sell everything. A missing or
    torn snapshot must never be read as that.
    """
    path = path or (REPO / "research_store" / "rh" / "positions.json")
    try:
        import json                                   # noqa: PLC0415
        pos = json.loads(path.read_text()).get("positions") or {}
        return {str(k).upper() for k, v in pos.items()
                if float((v or {}).get("qty") or 0) > 0}
    except Exception:                                 # noqa: BLE001
        return None


def regime_filter(book_sel: list, held_book: set, owned: set | None) -> list:
    """Regime-off selection: pause new entries, do NOT liquidate what is held.

    The previous form was `book_sel = []`, whose comment already claimed "no new
    single-name entries" -- but an empty selection is not "add nothing", it is
    "hold nothing", and build_theses drops every name absent from it. So a
    regime flip did not pause the book, it SOLD it, on a signal about SPY rather
    than anything about the positions. That fired on 2026-07-27: nine single
    names closed in one tick (the other two closes that timestamp were ETF
    rotations this branch never touched).

    ⚠️ INTERSECTED WITH ACTUAL OWNERSHIP, not just the previous product.
    `held_book` is PRODUCT MEMBERSHIP -- a name can carry a target and never
    have been bought (TER, 2026-07-24, skipped for pending settlement and still
    in the product). Filtering on membership alone leaves such a name in the
    selection, and the fast loop treats a target with no position as an OPENING
    BUY -- so regime-off would open a brand-new position, which the old form
    made impossible. Ownership is the anchor; membership alone is not.

    `owned=None` means the ownership snapshot could not be read. That falls back
    to membership rather than to the empty set: failing toward "keep what the
    product says" risks re-opening one pending name, bounded by the order gate
    and the concentration cap, while failing toward "own nothing" SELLS THE
    WHOLE BOOK on an unreadable file. The reversible direction wins.
    """
    keep = [t for t in book_sel if t in held_book]
    if owned is None:
        return keep
    return [t for t in keep if t in owned]


def _selftest() -> None:
    """Covers stamp_earnings' binding + its fail-open contract."""
    from research_store.models import Thesis
    ts = [Thesis(symbol="WDC", rank=1, verdict="buy"),
          Thesis(symbol="MU", rank=2, verdict="buy")]

    # a real lookup binds only the names it knows; the rest stay None (unknown)
    n = stamp_earnings(ts, {"WDC": "2026-08-05"}.get)
    assert n == 1, n
    assert ts[0].earnings_date == "2026-08-05", ts[0]
    assert ts[1].earnings_date is None, "unknown must stay unknown, not be invented"

    # a lookup that BLOWS UP must not take the book down, and must not fabricate
    def boom(_sym):
        raise RuntimeError("finnhub down")
    ts2 = [Thesis(symbol="AMD", rank=1, verdict="buy")]
    assert stamp_earnings(ts2, boom) == 0
    assert ts2[0].earnings_date is None

    # the stamped field must survive the store round-trip, or risk_review
    # reads None off disk and the flag silently dies again
    from research_store.models import ResearchProduct
    rp = ResearchProduct.from_dict(
        ResearchProduct(as_of="2026-08-05", theses=ts).to_dict())
    assert rp.by_symbol()["WDC"].earnings_date == "2026-08-05", \
        "earnings_date did not survive to_dict/from_dict"

    print("selftest OK: earnings stamping binds, fails open, and round-trips")

    # ---- regime-off must PAUSE the book, never liquidate it ----------------
    # ⚠️ THESE CALL regime_filter DIRECTLY. The first version of this test
    # hand-wrote `[t for t in sel if t in held]` and asserted on its own copy --
    # so it passed identically with the shipped line reverted to `book_sel = []`
    # and pinned nothing at all. A test that re-implements the code under test
    # is not a test. The branch was extracted into regime_filter for this reason.
    sel = ["AAA", "BBB", "CCC", "DDD"]
    held = {"CCC", "DDD"}
    owned = {"CCC", "DDD"}

    assert regime_filter(sel, held, owned) == ["CCC", "DDD"], "held names must survive"
    assert regime_filter(sel, held, owned) != [], "regime-off must not empty the book"
    assert "AAA" not in regime_filter(sel, held, owned), "no new entries"

    # PRODUCT MEMBERSHIP IS NOT OWNERSHIP. DDD carries a target but was never
    # bought (the TER/pending-settlement case) -- it must NOT survive, or the
    # fast loop opens a brand-new position while the regime is off.
    assert regime_filter(sel, held, {"CCC"}) == ["CCC"], \
        "a name in the product but NOT owned must not survive regime-off"

    # unreadable ownership falls back to membership, never to the empty set:
    # failing toward "own nothing" would SELL THE WHOLE BOOK on a missing file
    assert regime_filter(sel, held, None) == ["CCC", "DDD"], \
        "unknown ownership must not be read as owning nothing"

    # nothing held -> genuinely empty (there is no book to pause)
    assert regime_filter(sel, set(), owned) == []
    # a held name dropped from the ranking is still released: regime-off
    # suspends new entries, it does not suspend the exit discipline
    assert regime_filter(["AAA", "CCC"], held, owned) == ["CCC"]

    # owned_symbols distinguishes "unknown" from "nothing" -- the whole point
    import tempfile as _tf, json as _js, os as _os
    assert owned_symbols(Path("/nonexistent/positions.json")) is None
    with _tf.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        _js.dump({"positions": {"mu": {"qty": 3}, "ZERO": {"qty": 0}}}, f)
        tmp = f.name
    try:
        got = owned_symbols(Path(tmp))
        assert got == {"MU"}, got          # upper-cased, zero-qty excluded
    finally:
        _os.unlink(tmp)

    print("selftest OK: regime-off pauses new entries, does NOT liquidate, and "
          "cannot open an unowned name")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="print, don't write the store")
    ap.add_argument("--selftest", action="store_true", help="logic tests, no I/O")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        return

    if not PANEL.exists():
        sys.exit("no price cache — run scripts/fetch_prices.py first")
    cfg = strat.load()
    P, TM = cfg["portfolio"], cfg["trade_management"]

    closes = pd.read_parquet(PANEL).sort_index()
    if closes.empty:
        sys.exit("price cache is empty — fetch_prices likely failed (auth/lock/"
                 "network); refusing to rebuild the book off empty data. "
                 "Fix the fetch, then run scripts/fetch_prices.py --force.")
    names = [t for t in pd.read_csv(REPO / "config" / "universe.csv")["ticker"] if t in closes]
    etfs = [t for t in pd.read_csv(REPO / "config" / "etf_universe.csv")["ticker"] if t in closes]
    asof = closes.index[-1]
    spy = closes["SPY"]

    # exclude names the intraday monitor stopped out (cooldown) so we don't rebuy
    # them the next morning — the stop vs. momentum-rank churn guard.
    cd_path = REPO / "research_store" / "monitor" / "cooldown.json"
    if cd_path.exists():
        import json as _json
        cd = _json.loads(cd_path.read_text())
        cooled = {s for s, until in cd.items() if until >= str(asof.date())}
        if cooled:
            names = [t for t in names if t not in cooled]
            etfs = [t for t in etfs if t not in cooled]
            print(f"cooldown: excluding {sorted(cooled)} until their date")

    trend = mom.regime_on(spy, asof, cfg["regime"]["trend_ma_days"])
    vix, vix_src = fetch_vix()
    ceiling = float(cfg["regime"]["vix_ceiling"])
    vix_ok = vix is None or vix <= ceiling          # None = data outage -> fail-open
    regime = trend and vix_ok
    rk = residual_kwargs(cfg, closes, spy)
    if rk:
        print(f"  signal: residual tilt={rk['residual_tilt']} "
              f"factors={cfg['signal'].get('residual_factors')} "
              f"({len(rk['factors'].columns)} ETFs)" if "factors" in rk
              else f"  signal: residual tilt={rk['residual_tilt']} factors=market(SPY)")
    book_scored = mom.compute(closes[names], asof, **rk)
    etf_scored = mom.compute(closes[etfs], asof)

    # Banded holds need to know what the book ALREADY owns: a held name is kept
    # until it falls below rank book_band, so nightly re-ranks don't churn a
    # name that slips from 9th to 11th. (Passing set() here made every run a
    # fresh top-N pick — the band never engaged; fixed 2026-07-09.)
    prev = read_current()
    held_book = {t.symbol for t in prev.theses
                 if t.target_weight > 0 and t.rank < 100} if prev else set()
    held_etf = {t.symbol for t in prev.theses
                if t.target_weight > 0 and t.rank >= 100} if prev else set()

    book_sel = mom.select(book_scored, held_book, P["book_hold"], P["book_band"])
    etf_sel = mom.select(etf_scored, held_etf, P["sleeve_hold"], P["sleeve_hold"])
    if not regime:
        # Whether a regime call justifies EXITING is a trading judgment, and it
        # belongs to the agent, which can see the positions, the marks and the
        # reason. This branch only declines to ADD. See regime_filter.
        owned = owned_symbols()
        book_sel = regime_filter(book_sel, held_book, owned)
        if owned is None:
            print("  ⚠️ regime-off: ownership snapshot unreadable — held names "
                  "kept on product membership alone")

    per_book = P["book_weight"] / P["book_hold"]
    per_etf = P["sleeve_weight"] / P["sleeve_hold"]
    book_held, book_drop = build_theses(book_sel, book_scored, closes, asof, per_book, TM, 1)
    etf_held, etf_drop = build_theses(etf_sel, etf_scored, closes, asof, per_etf, TM, 100)

    theses = book_held + etf_held

    # ---- stamp real earnings dates onto the book ----------------------------
    # The book is the ONLY place this can live: risk_review runs at 12:00/15:45
    # ET, and nothing that exists only during RTH can see an overnight gap. The
    # date rides the thesis so the monitor, the fast loop and the ledger all read
    # the same number. Whole step is fail-open — no calendar, no flag, no crash.
    n_stamped = 0
    try:
        import event_calendar as evcal                       # noqa: E402
        today = str(asof.date())
        cal = evcal.compiler.compile_calendar(
            [t.symbol for t in theses], as_of=today, from_date=today,
            to_date=str((asof + pd.Timedelta(days=45)).date()))
        n_stamped = stamp_earnings(
            theses, lambda s: (cal.get(s.upper()) or {}).get("report_date"))
    except Exception as e:                                   # noqa: BLE001
        print(f"  !! earnings calendar unavailable ({type(e).__name__}: {e}) — "
              f"theses carry earnings_date=None (unknown, not 'no earnings')")

    product = ResearchProduct(
        as_of=str(asof.date()), theses=theses,
        regime={"status": "on" if regime else "off",
                "floor": f"SPY>{cfg['regime']['trend_ma_days']}DMA={trend}",
                "vix": (f"{vix:.1f}{'<=' if vix_ok else '>'}{ceiling:g}"
                        f"{'' if vix_ok else ' CEILING BREACHED'} ({vix_src})"
                        if vix is not None else f"n/a — {vix_src}"),
                "notes": ""},
        notes=f"slow_loop dual-momentum book as of {asof.date()}")

    # ---- report ----
    print(f"as_of {asof.date()} | regime {'ON' if regime else 'OFF (no new entries)'} "
          f"(trend={trend}, vix={f'{vix:.1f}' if vix is not None else 'n/a'}/{ceiling:g}) | "
          f"book {len(book_held)}/{P['book_hold']} held, sleeve {len(etf_held)}/{P['sleeve_hold']} held")
    _soon = [t.symbol for t in theses if t.earnings_date
             and t.earnings_date <= str((asof + pd.Timedelta(days=5)).date())]
    print(f"earnings: {n_stamped}/{len(theses)} dated"
          + (f" | REPORTING WITHIN 5d: {', '.join(sorted(_soon))}" if _soon else ""))
    print(f"\n{'BOOK (top-10, R:R>=2 gated)':40}{'weight':>8}{'entry':>9}{'stop':>9}{'R:R':>6}")
    for t in book_held:
        print(f"  {t.symbol:<8}{t.thesis[:28]:<30}{t.target_weight:>8.2%}"
              f"{(t.entry_zone[0]+t.entry_zone[1])/2:>9.2f}{t.stop:>9.2f}{reward_risk(t):>6.2f}")
    if book_drop:
        print(f"  -- dropped for geometry (stop too wide for 2:1): "
              + ", ".join(f"{s}(σ={sg:.1%})" for s, _, sg in book_drop))
    print(f"\n{'SLEEVE (top-4)':40}{'weight':>8}{'entry':>9}{'stop':>9}{'R:R':>6}")
    for t in etf_held:
        print(f"  {t.symbol:<8}{t.thesis[:28]:<30}{t.target_weight:>8.2%}"
              f"{(t.entry_zone[0]+t.entry_zone[1])/2:>9.2f}{t.stop:>9.2f}{reward_risk(t):>6.2f}")
    if etf_drop:
        print("  -- dropped: " + ", ".join(s for s, _, _ in etf_drop))
    total_w = sum(t.target_weight for t in theses)
    print(f"\ntotal invested weight {total_w:.0%} | cash {1-total_w:.0%}")

    if args.dry:
        print("\n[--dry] not written to store")
        return
    write_product(product, mandate=strat.risk_mandate(cfg))
    print("\nwritten to Research Store (current.json + journal)")

    # Intraday risk-review overrides/intents are strictly INTRA-WEEK overlays: the
    # fresh weekly geometry supersedes them. Clear them so a Tuesday-tightened stop
    # is never re-applied on top of next week's rebuilt levels. (spec §7) BUT this
    # script also runs nightly (Mon-Fri 18:00, per deploy/crontab.template) for the
    # daily recompute — that run must NOT clear overrides, or a stop the 15:45 risk
    # review tightened earlier the same day would be wiped before the next open.
    # Only the Sunday 20:00 weekly rebuild clears (weekday(): Mon=0 ... Sun=6).
    if date.today().weekday() == 6:
        for _f in ("overrides.json", "deferred_intents.json"):
            try:
                (REPO / "research_store" / "monitor" / _f).unlink()
            except FileNotFoundError:
                pass

    # --- weekly stale-seed watch accrual (universe maintenance, Piece 1) ---
    try:
        import json as _json
        import universe_maint as _um
        cfg_um = cfg["universe_maintenance"]
        uni_rows = _um.read_universe(str(REPO / "config" / "universe.csv"))
        seeds = {r["ticker"] for r in uni_rows if r["source"] == "seed"}

        def _rk(sym):
            r = book_scored.loc[sym, "rank"]
            return int(r) if r == r else 9999  # NaN (ineligible seed) => worst-rank ⇒ counts as stale

        seed_ranks = {s: _rk(s) for s in names if s in seeds and s in book_scored.index}
        watch_path = REPO / "research_store" / "universe" / "seed_watch.json"
        watch_path.parent.mkdir(parents=True, exist_ok=True)
        watch = _json.loads(watch_path.read_text()) if watch_path.exists() else {}
        watch = _um.update_seed_watch(watch, seed_ranks, cfg_um["seed_watch_history"])
        watch_path.write_text(_json.dumps(watch, indent=2))
        print(f"seed-watch: recorded ranks for {len(seed_ranks)} seeds -> {watch_path}")
    except Exception as e:  # never let the watch break the slow loop
        print(f"seed-watch accrual skipped: {e}")

if __name__ == "__main__":
    main()
