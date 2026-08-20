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

import momentum   # noqa: E402
import residual   # noqa: E402
import strategy   # noqa: E402


def read_universe(path: Path) -> list:
    """First column of a header-carrying CSV, blank lines skipped."""
    return [ln.split(",")[0].strip()
            for ln in path.read_text().splitlines()[1:] if ln.strip()]


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

    2. **ETFs were pooled into the ranking.** The sleeve was retired 2026-08-16
       and the last four ETFs were sold 2026-08-17, but the agent-facing screen
       still ranked 18 ETFs alongside the 150 single names. That is not a
       cosmetic surplus: `score` is a PERCENTILE rank, so who is in the pool
       DEFINES it. ETFs carry structurally lower sigma, so they flattered
       themselves on R/sigma and shifted every single name's percentile. The
       ranked list is single names now.

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

    # NO ETF may appear in the ranked list. The sleeve is retired; ETFs are
    # regression factors, not candidates.
    assert not (set(got.index) & set(etf_t)), \
        f"ETFs leaked into the ranked candidate list: {sorted(set(got.index) & set(etf_t))}"

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
