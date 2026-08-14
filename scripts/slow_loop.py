"""SLOW LOOP — compute today's target book and write it to the Research Store.

This is the deterministic ranking (docs/STRATEGY.md, "run as code, do not
eyeball"). It does NOT trade, and since 2026-08-14 nothing executes its output
unsupervised: the procedural fast loop was retired. What it writes is a PROPOSAL
a session reads and judges, not a book that gets filled.

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

The regime (SPY>50DMA and VIX<=ceiling) is COMPUTED AND RECORDED on the
product, and never enforced here: what a regime call means for the book is a
judgment, and it belongs to the session that can see the positions. It gated
this selection twice -- once by emptying it (which sold eleven positions in one
minute on 2026-07-27) and once by refusing new entries -- and both are gone.
Nothing-eligible -> an empty book is still a valid, intended state (cash).
"""
import argparse
import pathlib
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


def rotation_due(cfg: dict, today=None) -> bool:
    """Is a full re-rank + ROTATE due on this run? -> bool.

    `[portfolio] rebalance` has always said "weekly" and its own comment reads
    "full re-rank + rotate weekly; risk exits run nightly" -- but NOTHING read
    the knob, and cron runs this loop nightly, so the book was re-ranked and
    rotated every night. Both backtests (backtest.py, backtest_pit.py) model
    WEEKLY. So the live system rotated 5x more often than the numbers that
    justify it, and the config describing it was decorative.

    That gap was harmless while regime-off went flat immediately -- it is not
    now: the banded release is a RELATIVE-RANK trigger, so evaluating it nightly
    instead of weekly means a name that dips below the band for a single day is
    gone, and re-admission needs a free slot. Two names (PANW, TER) were dropped
    that way in the 2026-07 stretch and were back in the top ten days later.

    An unrecognised value rotates, with a warning: that is the behaviour the
    live system already had, so an unreadable config cannot silently FREEZE the
    book -- the far worse direction, since a frozen book stops releasing losers.
    """
    mode = str((cfg.get("portfolio") or {}).get("rebalance") or "").strip().lower()
    if mode in ("weekly", "week"):
        return (today or date.today()).weekday() == 6      # Sunday, per crontab
    if mode in ("nightly", "daily", "session", ""):
        return True
    print(f"  ⚠️ unrecognised [portfolio] rebalance={mode!r} — rotating")
    return True


def select_book(book_scored, etf_scored, held_book: set, held_etf: set,
                P: dict, *, regime: bool, rotate: bool) -> tuple[list, list]:
    """Choose the book and sleeve. -> (book_sel, etf_sel).

    ⛔ `regime` IS ACCEPTED AND DELIBERATELY UNUSED. It is a parameter so the
    selftest can call this twice -- regime on, regime off, everything else
    identical -- and assert the two selections are EQUAL. That is a behavioural
    proof that the market regime cannot move the selection; the previous guard
    scanned source text for banned spellings, which proved nothing and passed
    while broken (found by the independent reviewer, 2026-08-14).

    The regime has gated this selection twice. First as `book_sel = []`, which
    did not pause the book but SOLD it -- eleven positions in one minute on
    2026-07-27, essentially this book's entire drawdown. Then as regime_filter:
    keep what is held, refuse anything new. Both were a rule about SPY
    overruling an agent that can see the position, the marks and the reason.

    Nothing executes this product unsupervised any more, so a selection is a
    proposal to a session, not an order. Filtering here would only hide
    candidates from the one party able to weigh them. The regime is recorded on
    the product and reported by brief(); what it MEANS is the agent's call.
    """
    del regime                      # noqa: F841 — see docstring; never a filter
    book_sel = mom.select(book_scored, held_book, P["book_hold"], P["book_band"])
    etf_sel = mom.select(etf_scored, held_etf, P["sleeve_hold"], P["sleeve_hold"])
    if not rotate:
        book_sel = hold_selection(book_sel, book_scored, held_book)
        etf_sel = hold_selection(etf_sel, etf_scored, held_etf)
    return book_sel, etf_sel


def hold_selection(sel: list, scored, held: set) -> list:
    """Non-rotation night: hold exactly what is held, in rank order.

    NOT `sel` -- sel is a fresh re-rank, which is the thing a non-rotation night
    must not act on. Restricted to names the panel can still score, because
    build_theses does `scored.loc[sym]` and a delisted name would raise and take
    the whole book down with it.
    """
    return [t for t in scored.index if t in held] if len(scored) else []


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

    # ---- the regime is a FACT, not a gate ----------------------------------
    # ⛔ THIS PINS AN ABSENCE, which is the only way to keep a removed rule from
    # growing back. The regime has gated the selection twice: `book_sel = []`
    # (which SOLD the book -- eleven positions in one minute, 2026-07-27) and
    # then regime_filter (which kept holdings but refused new entries). Both
    # were a rule about SPY overriding an agent that can see the position, the
    # marks and the reason. Nothing executes this product unsupervised now, so a
    # selection is a proposal to a session, not an order.
    # ⛔ BEHAVIOURAL, NOT TEXTUAL. The first version of this guard scanned the
    # source for banned spellings ("book_sel = []", "if not regime:"). The
    # independent reviewer took it apart: the fact-survival assertion matched
    # ITSELF, so deleting the real product field still passed; and
    # `book_sel.clear()`, `book_sel[:] = []`, `if not (regime):` or any alias
    # walked straight through. "It proves syntax spellings, not behavior."
    #
    # So: run the REAL selection twice with everything identical except the
    # regime, and require the two results to be equal. There is no spelling
    # that defeats that, because it is the property itself.
    idx = pd.date_range("2026-01-01", periods=3, freq="D")
    scored = pd.DataFrame(
        {"score": [3.0, 2.0, 1.0, 0.5], "rank": [1, 2, 3, 4],
         "eligible": [True, True, True, True]},
        index=["AAA", "BBB", "CCC", "DDD"])
    etfs = pd.DataFrame(
        {"score": [2.0, 1.0], "rank": [1, 2], "eligible": [True, True]},
        index=["XLK", "XLE"])
    PP = {"book_hold": 2, "book_band": 3, "sleeve_hold": 1}

    for rotate in (True, False):
        for held_b, held_e in (({"CCC"}, {"XLE"}), (set(), set())):
            on = select_book(scored, etfs, held_b, held_e, PP,
                             regime=True, rotate=rotate)
            off = select_book(scored, etfs, held_b, held_e, PP,
                              regime=False, rotate=rotate)
            assert on == off, (
                f"the regime moved the selection (rotate={rotate}, "
                f"held={held_b}): on={on} off={off}")
            # ...and on a rotation night regime-off must still produce a book.
            # That is the failure that cost money on 2026-07-27. Scoped to
            # rotate=True on purpose: a NON-rotation night with nothing held is
            # legitimately empty (there is nothing to hold), and this assertion
            # fired on exactly that case when first written — which is the test
            # doing its job on its author.
            if rotate:
                assert off[0], "regime-off produced an empty book on a rotation night"

    # THE FACT MUST SURVIVE. Removing a rule without leaving the information
    # behind is a blind spot, which is worse than the rule. Assert against the
    # PRODUCT OBJECT, not the source text -- the previous version searched the
    # file for a string that its own assertion contained.
    payload = {"status": "off", "floor": "SPY>50DMA=False",
               "vix": 31.0, "vix_ceiling": 28.0, "vix_ok": False}
    rt = ResearchProduct.from_dict(
        ResearchProduct(as_of="2026-08-14", theses=[], regime=payload,
                        notes="selftest").to_dict())
    assert rt.regime == payload, rt.regime
    assert rt.regime["vix"] == 31.0, "the VIX half must survive the round trip too"

    print("selftest OK: the regime cannot move the selection (proved by running "
          "it both ways), regime-off is never empty, and the fact round-trips")

    # ---- the rebalance knob must actually BIND ----------------------------
    from datetime import date as _d
    wk = {"portfolio": {"rebalance": "weekly"}}
    assert rotation_due(wk, _d(2026, 8, 16)) is True,  "Sunday rotates"
    assert rotation_due(wk, _d(2026, 8, 12)) is False, "Wednesday does not"
    assert rotation_due(wk, _d(2026, 8, 14)) is False, "Friday does not"
    # nightly stays nightly; an unreadable value rotates rather than FREEZING
    assert rotation_due({"portfolio": {"rebalance": "nightly"}}, _d(2026, 8, 12)) is True
    assert rotation_due({}, _d(2026, 8, 12)) is True
    assert rotation_due({"portfolio": {"rebalance": "fortnightly"}}, _d(2026, 8, 12)) is True

    # ⚠️ the LIVE config must be the one that was reasoned about -- if someone
    # flips it to nightly, that is a strategy change and this test says so
    assert rotation_due(strat.load(), _d(2026, 8, 16)) is True
    assert rotation_due(strat.load(), _d(2026, 8, 12)) is False, \
        "config/strategy.toml no longer rebalances weekly — intended?"

    # a non-rotation night holds what is HELD, not the fresh re-rank
    sc = pd.DataFrame({"score": [3.0, 2.0, 1.0]}, index=["AAA", "BBB", "CCC"])
    assert hold_selection(["AAA", "BBB"], sc, {"BBB", "CCC"}) == ["BBB", "CCC"], \
        "a non-rotation night must ignore the re-rank and hold the book"
    # a held name the panel can no longer score is dropped, not crashed on
    assert hold_selection([], sc, {"CCC", "DELISTED"}) == ["CCC"]
    assert hold_selection([], pd.DataFrame(), {"CCC"}) == []

    print("selftest OK: rebalance=weekly binds (was decorative); non-rotation "
          "nights hold the book instead of re-ranking it")


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

    # ROTATE ONLY WHEN DUE. Geometry, stops, earnings and marks still refresh
    # below every night -- it is the SELECTION that is weekly. See rotation_due.
    rotate = rotation_due(cfg)
    book_sel, etf_sel = select_book(book_scored, etf_scored, held_book, held_etf,
                                    P, regime=regime, rotate=rotate)
    if not rotate:
        print("  rotation: not due (weekly) — holding the book, geometry refreshed")
    # ⛔ THE REGIME DOES NOT FILTER THE SELECTION. It is recorded on the product
    # (below) and reported by brief() as "an observation about the market, not a
    # rule that acts", and the agent decides what it means.
    #
    # It used to gate here. The first form was `book_sel = []`, which did not
    # pause the book, it SOLD it -- eleven positions in one minute on
    # 2026-07-27, essentially this book's whole drawdown. That was narrowed on
    # 2026-08-12 to "keep what is held, add nothing new", which stopped the
    # liquidation but was still the same shape: a rule about SPY deciding
    # whether the agent may open a position it can see, has reasons for, and is
    # accountable for.
    #
    # Nothing executes this product unsupervised any more -- the procedural fast
    # loop was retired 2026-08-14 -- so a selection is a proposal to a session,
    # not an order. Filtering it here would only hide candidates from the one
    # party able to weigh them.

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
    print(f"as_of {asof.date()} | regime {'ON' if regime else 'OFF (observation only)'} "
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
