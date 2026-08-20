"""SLOW LOOP — compute today's target book and write it to the Research Store.

This is the deterministic ranking (docs/STRATEGY.md, "run as code, do not
eyeball"). It does NOT trade, and since 2026-08-14 nothing executes its output
unsupervised: the procedural fast loop was retired. What it writes is a PROPOSAL
a session reads and judges, not a book that gets filled.

  1. Load recent closes for the 150 single names + the read-only series the
     signal needs (11 sector factors for the residual tilt, SPY for regime).
  2. Regime floor: SPY (proxy $SPX) > 50DMA  AND  VIX <= [regime].vix_ceiling
     (VIX from a live Schwab $VIX quote, FRED VIXCLS prior close as fallback;
     both unavailable -> gate skipped FAIL-OPEN, the trend floor still rules).
  3. Rank the universe with the momentum signal (src/momentum.py).
  4. Select the book (top book_hold, banded). EQUITIES ONLY — the ETF sleeve
     was deleted 2026-08-20; there is no second engine.
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
import rank_history             # noqa: E402
import residual                 # noqa: E402
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




class _SkipAccrual(Exception):
    """Not an error — the stale-seed watch only accrues on the screen day."""


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
    """The BOOK-compute residual kwargs. Delegates to residual.kwargs_from_config.

    ⛔ THE IMPLEMENTATION MOVED (2026-08-20) and must not come back here. This
    loop was its only caller, which is precisely why the agent-facing screen
    (`candidates()` / `universe()`) ranked the same names WITHOUT the tilt while
    the book was built WITH it. One implementation, both callers.
    """
    return residual.kwargs_from_config(cfg, closes, spy)


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


def held_positions(path: Path | None = None) -> set[str] | None:
    """Symbols the AGENT actually holds at the broker. -> set, or None if unreadable.

    None is distinct from the empty set: empty means "we hold nothing", None
    means "the snapshot could not be read" and the caller must fall back rather
    than conclude anything.
    """
    path = path or (REPO / "research_store" / "rh" / "positions.json")
    try:
        import json                                   # noqa: PLC0415
        pos = json.loads(path.read_text()).get("positions") or {}
        return {str(k).upper() for k, v in pos.items()
                if float((v or {}).get("qty") or 0) > 0}
    except Exception:                                 # noqa: BLE001
        return None


def protective_theses(owned: set, covered: set, scored, closes, asof, tm,
                      start_rank: int) -> list:
    """Geometry for names the AGENT HOLDS that the ranking did not select.

    ⛔ THE BOOK IS WHATEVER THE AGENT HOLDS. This loop ranks candidates and
    supplies default levels; it does not decide the book. But the monitor's
    base stop comes from a thesis (apply_overrides overlays the agent's
    stricter-only set_levels ON TOP of it), and until now a thesis existed only
    for names THIS LOOP selected. A name the agent bought on its own judgement
    -- exactly what it is supposed to do -- got no thesis, therefore no base
    stop, and the monitor could only report it "unprotected".

    So these carry FULL GEOMETRY and target_weight 0.0: the loop is not
    prescribing the position, it is making sure the position the agent took is
    watched. verdict="hold" says the same thing -- this is not a buy
    recommendation, it is protection for something already owned.

    A held name with no price history (outside the panel entirely) still cannot
    be given geometry; it stays unprotected and the monitor still says so.
    """
    out = []
    rank = start_rank
    for sym in sorted(owned - covered):
        if sym not in scored.index or sym not in closes.columns:
            continue                       # no signal/price -> cannot size a stop
        row = scored.loc[sym]
        try:
            price = float(closes.loc[asof, sym])
            g = geometry(price, row["sigma"], tm["stop_atr_mult"], tm["target_r_mults"])
        except Exception:                  # noqa: BLE001 — one name, never the run
            continue
        # ⚠️ rank is NaN for a name that FAILS THE ABSOLUTE GATE (R <= 0), and
        # `int(NaN)` raises ValueError. That is exactly the case this function
        # exists for -- a held name whose momentum died -- and it sits OUTSIDE
        # the try above, so it would have taken down the whole slow loop on a
        # rotation night (non-rotation nights hide it, because hold_selection
        # keeps ineligible held names covered). Latent since 2026-08-14; found
        # 2026-08-20 while making the universe churn weekly, which makes a held
        # name dropping out of the ranked set materially more likely.
        rk = row["rank"]
        eligible = rk == rk                                   # NaN == NaN is False
        rank_txt = (f"rank {int(rk)}" if eligible
                    else "no longer passes the absolute gate (12-month return <= 0)")
        out.append(Thesis(
            symbol=sym, rank=rank, verdict="hold",
            thesis=f"HELD, not in the ranked selection ({rank_txt}). "
                   f"Geometry supplied so the monitor has a base stop; the loop "
                   f"is not prescribing this position. Whether to close it, add "
                   f"to it, or leave it is YOUR call -- this is a fact about the "
                   f"ranking, not an instruction.",
            entry_zone=g["entry_zone"], stop=g["stop"], targets=g["targets"],
            target_weight=0.0,
            confidence=round(float(row["score"]), 3),
            signals={"score": round(float(row["score"]), 4),
                     "sigma": round(float(row["sigma"]), 5),
                     # None, not a number: an ineligible name HAS no rank, and
                     # int(NaN) raises. Same defect as the thesis text above.
                     "rank": int(rk) if eligible else None,
                     "eligible": bool(eligible), "source": "protective"},
            as_of=str(asof.date()),
            review_by=f"{(asof + pd.Timedelta(days=7)).date()} (weekly rebalance)"))
        rank += 1
    return out


def select_book(book_scored, held_book: set, P: dict, *, regime: bool,
                rotate: bool, cooled=frozenset()) -> list:
    """Choose the book. -> book_sel (a list of symbols).

    ⛔ THE SLEEVE ARGUMENTS ARE GONE (2026-08-20). This took `etf_scored`,
    `held_etf` and `sleeve_enabled` and returned a second list. The sleeve was
    retired 2026-08-16 and its positions sold 08-17, but the engine stayed here
    behind a flag — so the code still read as a two-engine system to anyone
    opening it. Deleted rather than disabled: equities only.

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
    # Cooled names are dropped HERE, not from the scored frame, so a cooldown
    # cannot move anyone else's percentile score. Selection is byte-identical to
    # the previous behaviour: they were simply never in the frame before.
    if len(book_scored) and cooled:
        book_scored = book_scored.drop(
            index=[t for t in cooled if t in book_scored.index])
    book_sel = mom.select(book_scored, held_book, P["book_hold"], P["book_band"])
    if not rotate:
        book_sel = hold_selection(book_sel, book_scored, held_book)
    return book_sel


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
    PP = {"book_hold": 2, "book_band": 3}

    for rotate in (True, False):
        for held_b in ({"CCC"}, set()):
            on = select_book(scored, held_b, PP, regime=True, rotate=rotate)
            off = select_book(scored, held_b, PP, regime=False, rotate=rotate)
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

    # ---- THERE IS NO SLEEVE, AND IT CANNOT COME BACK -----------------------
    # `[etf_sleeve] enabled` was decorative until 2026-08-16, then a false flag
    # guarding live code until 2026-08-20. Both states let a retired allocation
    # read as part of the system. It is deleted now, so assert the SHAPE: this
    # function returns one list, takes no sleeve arguments, and a stale
    # [etf_sleeve] table left in a local override cannot resurrect it.
    import inspect as _inspect
    _sig = _inspect.signature(select_book)
    for gone in ("etf_scored", "held_etf", "sleeve_enabled"):
        assert gone not in _sig.parameters, f"the sleeve argument {gone!r} came back"
    _one = select_book(scored, set(), PP, regime=True, rotate=True)
    assert isinstance(_one, list) and all(isinstance(x, str) for x in _one), _one
    # (Deliberately NOT a source-text scan for the old universe filename: an
    # assertion containing the string it searches for is the vacuous selftest
    # OPSLOG 2026-08-14 records. The signature check above is behavioural.)

    # ⛔ ...AND THE REST OF THE PIPELINE CANNOT REINTRODUCE IT. The test above
    # proves select_book() ignores the regime; the reviewer pointed out that
    # proves only that ONE function does. "A caller-side filter immediately
    # after select_book(), or filtering during build_theses(), would restore
    # the gate while this test remained green."
    #
    # Two structural checks, neither of them substring matching:
    #  1. the theses builders cannot even RECEIVE the regime -- checked on the
    #     real signatures, so renaming a parameter cannot hide it;
    #  2. inside main(), the name `regime` may only be ASSIGNED (computed),
    #     recorded on the product, and printed. Any other use -- a comparison,
    #     an `if`, passing it to a call, or aliasing it into another variable --
    #     fails here. Walked over the parse tree, not the text.
    import ast as _ast
    import inspect as _inspect
    for fn in (build_theses, protective_theses):
        params = set(_inspect.signature(fn).parameters)
        assert not {p for p in params if "regime" in p.lower()}, \
            f"{fn.__name__} takes a regime argument; it must not see one"

    tree = _ast.parse(pathlib.Path(__file__).read_text())
    main_fn = next(n for n in tree.body
                   if isinstance(n, _ast.FunctionDef) and n.name == "main")
    for node in _ast.walk(main_fn):
        # aliasing: `x = regime` launders the flag past the checks below.
        # Narrow to a DIRECT alias on purpose — argument passing is covered by
        # the Call check further down, and a broader rule flagged the two uses
        # that are explicitly allowed (select_book and the product record).
        if isinstance(node, _ast.Assign) and isinstance(node.value, _ast.Name):
            assert node.value.id != "regime", \
                "main() copies `regime` into another name — it must not be reused"
        # any branch on it is a gate by definition
        if isinstance(node, (_ast.If, _ast.IfExp)):
            names = {n.id for n in _ast.walk(node.test) if isinstance(n, _ast.Name)}
            if "regime" in names:
                # the run-summary line is `'ON' if regime else 'OFF ...'` — a
                # report, allowed. Anything else branching on it is not.
                assert isinstance(node, _ast.IfExp) and all(
                    isinstance(b, _ast.Constant) for b in (node.body, node.orelse)), \
                    "main() branches on `regime`; it must not gate anything"
        # passing it into a call is how a filter would be reintroduced, EXCEPT
        # into select_book (which provably discards it) and the product record
        if isinstance(node, _ast.Call):
            fname = getattr(node.func, "id", getattr(node.func, "attr", ""))
            passed = any(isinstance(a, _ast.Name) and a.id == "regime"
                         for a in node.args) or any(
                isinstance(k.value, _ast.Name) and k.value.id == "regime"
                for k in node.keywords)
            if passed:
                assert fname in ("select_book", "ResearchProduct"), \
                    f"main() passes `regime` into {fname}() — only select_book " \
                    "(which discards it) and the product record may receive it"

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

    # ---- the AGENT's book gets geometry, not just this loop's picks --------
    # The monitor's base stop comes from a thesis; a name the agent bought that
    # the ranking never selected had none, so it could only be reported
    # "unprotected". These carry full geometry at weight 0 — protection for a
    # position already taken, not a recommendation to take one.
    sc = pd.DataFrame({"score": [1.0, 0.5], "rank": [30, 40],
                       "sigma": [0.02, 0.03]}, index=["HELD", "NOPRICE"])
    cl = pd.DataFrame({"HELD": [100.0]}, index=[idx[0]])
    TMx = {"stop_atr_mult": 2.5, "target_r_mults": [2.2, 4.0]}

    got = protective_theses({"HELD"}, set(), sc, cl, idx[0], TMx, 200)
    assert len(got) == 1 and got[0].symbol == "HELD", got
    assert got[0].target_weight == 0.0, "the loop must not prescribe a held position"
    assert got[0].verdict == "hold", got[0].verdict
    assert got[0].stop and got[0].stop < 100.0, "a protective thesis needs a real stop"
    assert got[0].targets and got[0].targets[0] > 100.0, got[0].targets
    # already in the product -> not duplicated
    assert protective_theses({"HELD"}, {"HELD"}, sc, cl, idx[0], TMx, 200) == []
    # held but no price/signal -> skipped, never a thesis with a fabricated stop
    assert protective_theses({"NOPRICE"}, set(), sc, cl, idx[0], TMx, 200) == []
    assert protective_theses({"UNKNOWN"}, set(), sc, cl, idx[0], TMx, 200) == []
    # ---- a HELD name that FAILS THE ABSOLUTE GATE still gets a stop --------
    # rank is NaN once R <= 0, and `int(NaN)` raises ValueError OUTSIDE the
    # per-name try -- so this took the whole slow loop down on a rotation night
    # (non-rotation nights hide it: hold_selection keeps ineligible held names
    # covered). Latent since 2026-08-14, found 2026-08-20.
    #
    # The REQUIREMENT, not just the absence of a crash: it must still produce a
    # protective stop, must still be weight 0, and must NOT be a sell. Whether
    # to close a name whose momentum died is the agent's decision, not this
    # loop's -- the loop's only job here is that the position stays watched.
    import numpy as _np
    dead = pd.DataFrame({"score": [0.1], "rank": [_np.nan], "sigma": [0.02]},
                        index=["DEAD"])
    dcl = pd.DataFrame({"DEAD": [100.0]}, index=[idx[0]])
    d = protective_theses({"DEAD"}, set(), dead, dcl, idx[0], TMx, 200)
    assert len(d) == 1, "an ineligible held name must not vanish -- it would be unprotected"
    assert d[0].stop and d[0].stop < 100.0, "no stop for a held name that lost the gate"
    assert d[0].target_weight == 0.0 and d[0].verdict == "hold", d[0]
    assert d[0].signals["rank"] is None and d[0].signals["eligible"] is False, d[0].signals
    assert "absolute gate" in d[0].thesis and "YOUR call" in d[0].thesis, d[0].thesis
    assert "sell" not in d[0].thesis.lower(), "the loop must not instruct an exit"

    # ---- COOLDOWN MUST NOT MOVE ANOTHER NAME'S SCORE ----------------------
    # It used to be applied by stripping names from the SCORING universe, and
    # `score` is a percentile rank -- so an unrelated cooled name shifted every
    # other name's score, and the agent-facing screen (no cooldown notion at all)
    # then ranked differently from the book. Cooldown is now a SELECTION rule.
    # Requirement: identical selection to the old behaviour, unchanged scores.
    csc = pd.DataFrame({"score": [0.9, 0.8, 0.7], "rank": [1, 2, 3],
                        "sigma": [0.02, 0.02, 0.02],
                        "eligible": [True, True, True]},
                       index=["AAA", "COOL", "CCC"])
    PC = {"book_hold": 2, "book_band": 3}
    sel_cool = select_book(csc, set(), PC, regime=True, rotate=True,
                           cooled={"COOL"})
    assert sel_cool == ["AAA", "CCC"], f"cooled name must not be selected: {sel_cool}"
    sel_free = select_book(csc, set(), PC, regime=True, rotate=True)
    assert sel_free == ["AAA", "COOL"], f"without cooldown the top 2 stand: {sel_free}"
    # ...and select_book must not have MUTATED the caller's scored frame -- the
    # snapshot, the rank diff and protective geometry all read it afterwards.
    assert list(csc.index) == ["AAA", "COOL", "CCC"], "scored frame was mutated"

    # ---- a HELD name in NEITHER universe must still be reachable ------------
    # protective_theses can only reach a name present in the scored frame it is
    # given. main() now scores such names separately; this pins the contract the
    # fix depends on -- given a frame that DOES contain it, a stop is produced.
    scl = pd.DataFrame({"score": [0.4], "rank": [88.0], "sigma": [0.03]},
                       index=["STRAY"])
    ccl = pd.DataFrame({"STRAY": [40.0]}, index=[idx[0]])
    sp = protective_theses({"STRAY"}, set(), scl, ccl, idx[0], TMx, 400)
    assert len(sp) == 1 and sp[0].stop and sp[0].stop < 40.0, sp
    assert sp[0].target_weight == 0.0, "a stray holding is protected, never prescribed"
    # and absent from the frame it is correctly skipped -- which is exactly why
    # main() must supply a frame that contains it.
    assert protective_theses({"STRAY"}, set(), pd.DataFrame(), ccl, idx[0], TMx, 400) == []

    # an unreadable snapshot is "unknown", never "holds nothing"
    assert held_positions(Path("/nonexistent/positions.json")) is None

    print("selftest OK: the regime cannot move the selection (proved by running "
          "it both ways), regime-off is never empty, the fact round-trips, and "
          "held-but-unselected names get a base stop at weight 0")

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
            # ⛔ DO NOT strip these from `names`. Until 2026-08-20 this filtered
            # the SCORING universe, and `score` is a PERCENTILE rank -- so a
            # cooled name silently shifted every other name's score, and the
            # agent-facing screen (which has no cooldown notion) then computed a
            # DIFFERENT ranking from the book's for as long as any cooldown was
            # active. Found by the independent reviewer, 2026-08-20.
            #
            # Cooldown is a rule about what may be RE-ENTERED, not about how
            # strong a name is. It is applied at selection instead; see
            # select_book(cooled=...). Selection behaviour is unchanged.
            print(f"cooldown: {sorted(cooled)} excluded from SELECTION "
                  f"(still scored — a cooldown must not move other names' ranks)")

    trend = mom.regime_on(spy, asof, cfg["regime"]["trend_ma_days"])
    vix, vix_src = fetch_vix()
    ceiling = float(cfg["regime"]["vix_ceiling"])
    vix_ok = vix is None or vix <= ceiling          # None = data outage -> fail-open
    regime = trend and vix_ok
    rk = residual_kwargs(cfg, closes, spy)
    if rk:
        print(f"  signal: residual tilt={rk['residual_tilt']} "
              f"factors={cfg['signal'].get('residual_factors')} "
              f"({len(rk['factors'].columns)} sector series)" if "factors" in rk
              else f"  signal: residual tilt={rk['residual_tilt']} factors=market(SPY)")
    book_scored = mom.compute(closes[names], asof, **rk)

    # ---- RECORD THE RE-RANK ------------------------------------------------
    # This runs every night. Until 2026-08-20 the result was DISCARDED on six
    # nights out of seven: rotation is weekly, so Mon-Fri hold_selection() threw
    # the fresh ranks away and nothing downstream ever saw them. The work was
    # being done and deleted -- a nightly job whose output nothing reads is
    # indistinguishable from one that never runs.
    #
    # It is persisted BEFORE selection deliberately: this is the raw ranking,
    # not the book. brief() diffs the two most recent snapshots and shows the
    # agent what moved. It still does not rotate anything -- what the movement
    # means is the agent's call.
    #
    # ⚠️ NOT on --dry. "--dry: print, don't write the store" has to mean it, or
    # a look-at-it run silently becomes tonight's recorded observation and the
    # next real diff is measured against a rehearsal.
    try:
        if args.dry:
            raise _SkipAccrual("--dry: nothing recorded")
        snap = rank_history.build(str(asof.date()), book_scored)
        rank_history.save(snap)
        rank_history.prune()
        d = rank_history.latest_diff()
        if d.get("status") == "ok":
            b = d["book"]
            print(f"  re-rank recorded ({d['prev_as_of']} -> {d['as_of']}): "
                  f"{b['moved_count']} moved, +{len(b['entered_top'])}/"
                  f"-{len(b['exited_top'])} top-{d['top_n']}, "
                  f"{len(b['became_ineligible'])} lost the absolute gate"
                  + (f", universe +{len(b['joined_universe'])}/"
                     f"-{len(b['left_universe'])}"
                     if b["joined_universe"] or b["left_universe"] else ""))
        else:
            print(f"  re-rank recorded ({d.get('status')})")
    except _SkipAccrual as e:
        print(f"  re-rank not recorded ({e})")
    except Exception as e:                                   # noqa: BLE001
        # Recording is observability, never a precondition for the book. A
        # failure here must not stop the loop writing tonight's product.
        print(f"  ⚠️ re-rank NOT recorded: {type(e).__name__}: {e}")

    # Banded holds need to know what the book ALREADY owns: a held name is kept
    # until it falls below rank book_band, so nightly re-ranks don't churn a
    # name that slips from 9th to 11th. (Passing set() here made every run a
    # fresh top-N pick — the band never engaged; fixed 2026-07-09.)
    prev = read_current()
    held_book = {t.symbol for t in prev.theses
                 if t.target_weight > 0 and t.rank < 100} if prev else set()

    # ROTATE ONLY WHEN DUE. Geometry, stops, earnings and marks still refresh
    # below every night -- it is the SELECTION that is weekly. See rotation_due.
    rotate = rotation_due(cfg)
    book_sel = select_book(book_scored, held_book, P, regime=regime,
                           rotate=rotate, cooled=cooled)
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
    # sleeve_hold is 0 once the sleeve is retired — guard the division rather
    # than letting a retired config crash the nightly run.
    book_held, book_drop = build_theses(book_sel, book_scored, closes, asof, per_book, TM, 1)

    theses = list(book_held)

    # ---- protect what the AGENT holds, not only what this loop picked -------
    # The base stop the monitor enforces comes from a thesis. Until now one
    # existed only for names this loop SELECTED, so a position the agent opened
    # on its own judgement had no stop to enforce and could only be reported
    # "unprotected". The book is whatever the agent holds; this loop's job is to
    # rank candidates and supply default levels for all of it.
    owned = held_positions()
    if owned is None:
        print("  ⚠️ positions snapshot unreadable — no protective geometry added "
              "for held names outside the selection")
    else:
        covered = {t.symbol for t in theses}
        extra = protective_theses(owned, covered, book_scored, closes, asof, TM, 200)

        # ⛔ HELD, BUT IN NEITHER RANKED UNIVERSE -> would get NO STOP.
        # Both passes above can only reach a name present in a scored frame, and
        # those frames are built from config/universe.csv + etf_universe.csv. A
        # HELD name that is not in either file therefore produced no thesis, so
        # market_monitor had no base stop for it and could only report it
        # unprotected.
        #
        # This was latent while the universe never changed. It stopped being
        # latent on 2026-08-20, when the screen became WEEKLY: a held "fill" name
        # whose dollar-volume rank slips past keep_rank_max is now dropped from
        # universe.csv on a Friday, and would have been silently unprotected from
        # that evening on. Found by the independent reviewer, who demonstrated it
        # rather than asserting it.
        #
        # These names are scored against the REAL universe (so the rank means
        # something) but only to obtain sigma for the geometry. They are never
        # selected and never weighted -- protection, not a recommendation.
        stray = {s for s in owned
                 if s not in covered
                 and s not in {t.symbol for t in extra}
                 and s in closes.columns
                 and s not in book_scored.index}
        if stray:
            print(f"  ⚠️ held but outside both universes: {sorted(stray)} — "
                  f"scoring them anyway so the monitor has a stop")
            stray_scored = mom.compute(closes[sorted(set(names) | stray)], asof, **rk)
            extra += protective_theses(
                owned, covered | {t.symbol for t in extra},
                stray_scored, closes, asof, TM, 400)
            still = sorted(s for s in stray
                           if s not in {t.symbol for t in extra})
            if still:
                # Not silent: no price history at all means no computable stop,
                # and the operator must hear that rather than read a clean log.
                print(f"  ⛔ STILL UNPROTECTED (no computable stop): {still}")
        if extra:
            print(f"  protective geometry (held, unselected, weight 0): "
                  f"{', '.join(t.symbol for t in extra)}")
        theses += extra

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
          f"book {len(book_held)}/{P['book_hold']} held")
    _soon = [t.symbol for t in theses if t.earnings_date
             and t.earnings_date <= str((asof + pd.Timedelta(days=5)).date())]
    print(f"earnings: {n_stamped}/{len(theses)} dated"
          + (f" | REPORTING WITHIN 5d: {', '.join(sorted(_soon))}" if _soon else ""))
    # The size comes from [portfolio] book_hold; hardcoding "top-10" printed a
    # header that disagreed with the 14 rows beneath it from 2026-08-16 onward.
    _bh = (cfg.get("portfolio") or {}).get("book_hold")
    print(f"\n{f'BOOK (top-{_bh}, R:R>=2 gated)':40}{'weight':>8}{'entry':>9}{'stop':>9}{'R:R':>6}")
    for t in book_held:
        print(f"  {t.symbol:<8}{t.thesis[:28]:<30}{t.target_weight:>8.2%}"
              f"{(t.entry_zone[0]+t.entry_zone[1])/2:>9.2f}{t.stop:>9.2f}{reward_risk(t):>6.2f}")
    if book_drop:
        print(f"  -- dropped for geometry (stop too wide for 2:1): "
              + ", ".join(f"{s}(σ={sg:.1%})" for s, _, sg in book_drop))
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
    # ⚠️ GATED ON THE SCREEN DAY since 2026-08-20. This block ran on EVERY slow
    # loop -- nightly -- while its own comment and `stale_seed_weeks` both said
    # "weeks", so 8 "weeks" was ~1.6 real weeks. A flagged seed forces the
    # universe screen to HOLD, so an over-fast accrual would have made the new
    # weekly screen refuse to auto-apply on spurious staleness. One reading per
    # week now, taken on the same day the screen runs.
    try:
        import json as _json
        import universe_maint as _um
        cfg_um = cfg["universe_maintenance"]
        if not _um.screen_due(cfg, date.today()):
            raise _SkipAccrual(
                f"not the screen day ({cfg_um.get('screen_day', 'friday')})")
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
    except _SkipAccrual as e:                 # not an error: the wrong weekday
        print(f"seed-watch: not accrued today ({e})")
    except Exception as e:  # never let the watch break the slow loop
        print(f"seed-watch accrual skipped: {e}")

if __name__ == "__main__":
    main()
