"""The momentum screen, exposed as a CANDIDATE GENERATOR.

Spec §3: a screen is not a decision. It ranks; the agent chooses — including
choosing nothing, or something outside the top N, with a stated reason. Nothing
here restricts what may be traded.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

import cohort     # noqa: E402
import momentum   # noqa: E402
import residual   # noqa: E402
import strategy   # noqa: E402


def read_universe(path: Path) -> list:
    """First column of a header-carrying CSV, blank lines skipped."""
    return [ln.split(",")[0].strip()
            for ln in path.read_text().splitlines()[1:] if ln.strip()]


def ranking_pool(cfg=None, repo: Path = None, today=None) -> dict:
    """WHICH names the momentum ranking is computed over, and where they came from.

    ⛔ ONE IMPLEMENTATION, THREE CALLERS, AND THAT IS THE WHOLE POINT.
    `candidates()`, `universe()` and `scripts/slow_loop.py` must rank the SAME
    names with the SAME signal, or the agent reads a different list from the one
    the book is built from. That exact divergence shipped once already: until
    2026-08-20 the agent-facing screen ranked without the residual tilt and with
    18 ETFs pooled in, so it could order the same names differently from the
    book. `rank_book()` fixed the SIGNAL half; this fixes the POOL half, which
    the cohort migration would otherwise re-open by giving each caller its own
    reason to read a different file.

    ⛔ `score` IS A PERCENTILE, SO THE POOL DEFINES IT. Two callers ranking
    slightly different name sets do not produce "almost the same" numbers — they
    produce different numbers for every name in common. There is no benign
    version of this drift.

    Returns::

        {"tickers": [...],      # rank these
         "source": "fixed_list" | "cohort" | "cohort_degraded",
         "view": composed cohort view | None,
         "note": one line for the run log / the agent}

    In `cohort` mode the pool is the SCOREABLE cohort only — a name whose panel
    cannot satisfy the momentum window has no number to rank, and including it
    would put a NaN row in front of the agent labelled as a candidate.
    Eligible-but-unscoreable names are reported separately, as research leads.

    ⛔ A DEGRADED COHORT FALLS BACK TO THE CSV **AND SAYS SO**. This is a
    READ path: ranking is information, not permission, and refusing to rank
    anything would blind the session rather than protect it. The order gate has
    the opposite polarity and never falls back — so during a cohort outage the
    agent can still see a ranking while every BUY is refused. Those two
    behaviours are meant to differ; do not "fix" one to match the other.
    """
    cfg = cfg if cfg is not None else strategy.load()
    repo = repo or REPO
    csv_path = repo / cfg["universe"]["source"]
    if cohort.mode(cfg) != "cohort":
        return {"tickers": read_universe(csv_path), "source": "fixed_list",
                "view": None,
                "note": f"ranking pool: {cfg['universe']['source']} (fixed_list mode)"}
    import datetime as _dt                                   # noqa: PLC0415
    today = today or _dt.date.today()
    try:
        view = cohort.active_view(cfg, repo, today)
    except cohort.CohortInvalid as e:
        return {"tickers": read_universe(csv_path), "source": "cohort_degraded",
                "view": None,
                "note": (f"ranking pool: FELL BACK to {cfg['universe']['source']} — "
                         f"{e}. You are seeing a ranking over the legacy list. "
                         f"New BUYS are refused by the order gate until the "
                         f"cohort is valid and fresh; exits are unaffected.")}
    c = view["counts"]
    return {"tickers": list(view["scoreable"]), "source": "cohort", "view": view,
            "note": (f"ranking pool: {c['scoreable']} scoreable of "
                     f"{c['eligible']} eligible (as of {view['as_of']}); "
                     f"{c['pending_history']} pending history, "
                     f"{c['unscoreable']} unscoreable — those are research "
                     f"leads, not candidates, and are not buyable")}


def rank(panel, asof, tickers: list, **compute_kwargs):
    """momentum.compute restricted to `tickers`, sorted best-first.

    Returns the full scored frame — the caller decides how many to show.
    `compute_kwargs` carries the residual tilt; pass what
    `residual.kwargs_from_config()` returns, or use `rank_sections()` which
    does it for you.
    """
    cols = [c for c in tickers if c in panel.columns]
    scored = momentum.compute(panel[cols], asof, **compute_kwargs)
    if scored.empty:
        return scored
    return scored.sort_values("score", ascending=False)


def rank_book(panel, asof, book_tickers: list, cfg=None):
    """THE ranked candidate list — the single-name universe, and nothing else.

    ⛔ TWO THINGS WERE WRONG HERE AND BOTH ARE FIXED (2026-08-20).

    1. **The tilt was missing.** The slow loop ranks with the adopted 0.75
       sector-residual blend (a structural signal choice made on the PIT
       backtest, OPSLOG 2026-07-24); `candidates()`/`universe()` called
       `momentum.compute()` bare. Same names, different signal, different sort.
       Now both go through `residual.kwargs_from_config()` — one implementation.

    2. **Funds were pooled into the ranking.** 18 index products were ranked
       alongside the 150 single names. That is not a cosmetic surplus: `score`
       is a PERCENTILE rank, so who is in the pool DEFINES it. A diversified
       basket carries structurally lower sigma, so it flattered itself on
       R/sigma and shifted every single name's percentile. Single names only.

    ⚠️ ETFs are still *priced*, and that is a different thing from being
    ranked. The residual tilt REGRESSES on the 11 SPDR sector ETFs and the
    regime read needs SPY, so their price columns stay load-bearing inputs to
    this very function. They are factors, not candidates.
    """
    cfg = cfg if cfg is not None else strategy.load()
    spy = panel["SPY"] if "SPY" in getattr(panel, "columns", []) else None
    # log silent: a fallback note belongs in the slow loop's run log, not
    # injected into an MCP tool's JSON response.
    rk = residual.kwargs_from_config(cfg, panel, spy, log=lambda _m: None)
    return rank(panel, asof, book_tickers, **rk)


def _selftest() -> None:
    import tempfile, numpy as np, pandas as pd
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "u.csv"
        p.write_text("ticker,sector\nAAA,tech\nBBB,fin\n\n")
        assert read_universe(p) == ["AAA", "BBB"], read_universe(p)

    n = 300
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    t = np.arange(n)
    panel = pd.DataFrame({
        "AAA": 100 * np.cumprod(1 + (0.002 + 0.005 * np.sin(2 * np.pi * t / 11))),
        "BBB": 100 * np.cumprod(1 + (0.0005 + 0.005 * np.sin(2 * np.pi * t / 13))),
        "CCC": 100 * np.cumprod(1 + (-0.001 + 0.005 * np.sin(2 * np.pi * t / 7))),
    }, index=idx)
    r = rank(panel, idx[-1], ["AAA", "BBB", "CCC"])
    assert list(r.columns) >= ["R", "sigma", "score"], list(r.columns)
    assert r.index[0] == "AAA", r.index.tolist()          # strongest first
    # restricting the universe must not change the remaining names' own numbers
    r2 = rank(panel, idx[-1], ["AAA", "BBB"])
    assert "CCC" not in r2.index, r2.index.tolist()
    assert abs(r2.loc["AAA", "R"] - r.loc["AAA", "R"]) < 1e-12
    # a ticker absent from the panel is simply not ranked, never an error
    r3 = rank(panel, idx[-1], ["AAA", "NOPE"])
    assert "NOPE" not in r3.index and "AAA" in r3.index
    print("selftest OK: screen ranks, restricts cleanly, tolerates unknown tickers")

    # ---- rank_book MUST equal what the slow loop computes ------------------
    # The regression that motivated the 2026-08-20 change: the agent's list was
    # ranked on a different signal AND a different peer set from the book's.
    # Assert BEHAVIOURAL equality against momentum.compute called the way
    # scripts/slow_loop.py calls it — not a spelling check, which is how a
    # vacuous selftest shipped on 2026-08-14.
    sectors = ["XLE", "XLF", "XLK", "XLV", "XLI", "XLP",
               "XLY", "XLU", "XLB", "XLRE", "XLC"]
    rng = np.random.default_rng(7)
    cols = {c: 100 * np.cumprod(1 + (0.001 + 0.004 * np.sin(2 * np.pi * t / (9 + i))))
            for i, c in enumerate(["AAA", "BBB", "CCC"])}
    for i, s in enumerate(sectors):
        cols[s] = 100 * np.cumprod(1 + (0.0004 + 0.003 * np.sin(2 * np.pi * t / (6 + i))))
    cols["SPY"] = 100 * np.cumprod(1 + (0.0006 + 0.002 * rng.standard_normal(n)))
    big = pd.DataFrame(cols, index=idx)
    book_t = ["AAA", "BBB", "CCC"]
    etf_t = sectors + ["SPY"]
    cfg = {"signal": {"residual_tilt": 0.75, "residual_factors": "sector"}}

    got = rank_book(big, idx[-1], book_t, cfg)
    want_rk = residual.kwargs_from_config(cfg, big, big["SPY"], log=lambda _m: None)
    assert "factors" in want_rk and want_rk["residual_tilt"] == 0.75, want_rk
    want = momentum.compute(big[book_t], idx[-1], **want_rk)
    assert np.allclose(got["score"].sort_index().values,
                       want["score"].sort_index().values), "rank drifted from the loop's"

    # ...and the tilt must actually BITE, else the equality above proves nothing.
    plain = momentum.compute(big[book_t], idx[-1])
    assert not np.allclose(want["score"].sort_index().values,
                           plain["score"].sort_index().values), \
        "tilt changed nothing — the equality above would be vacuous"

    # NO factor series may appear in the ranked list — they are regression
    # inputs, not candidates, and are not tradeable.
    assert not (set(got.index) & set(etf_t)), \
        f"factor series leaked into the ranked list: {sorted(set(got.index) & set(etf_t))}"

    # ...but their PRICES must still be load-bearing: strip the sector columns
    # and the tilt has nothing to regress on, so the rank changes.
    no_sectors = big[book_t + ["SPY"]]
    fallback = rank_book(no_sectors, idx[-1], book_t, cfg)
    assert np.allclose(fallback["score"].sort_index().values,
                       plain["score"].sort_index().values), \
        "without the sector columns the tilt must fail open to the plain rank"

    # pooling ETFs into the ranking would shift every percentile — proving why
    # the old combined list could not agree with the book's.
    pooled = momentum.compute(big[book_t + etf_t], idx[-1])
    assert not np.allclose(got["score"].sort_index().values,
                           pooled.reindex(sorted(book_t))["score"].values), \
        "ranked list equals the pooled rank — the peer set is not separated"

    # tilt off in config -> plain rank
    off = rank_book(big, idx[-1], book_t,
                    {"signal": {"residual_tilt": 0.0, "residual_factors": "sector"}})
    assert np.allclose(off["score"].sort_index().values,
                       plain["score"].sort_index().values), "tilt=0 must be the plain rank"
    print("selftest OK: rank_book matches the slow loop's ranking "
          "(tilt applied, ETFs excluded as candidates but kept as factors)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
