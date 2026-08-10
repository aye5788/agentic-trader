"""What the agent holds, and what it is worth. Pure assembly over marks.load()
and the current product — no new valuation logic, so the agent, the dashboard and
the equity log can never disagree about what a position is worth."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))


def holdings(valued: dict, theses: list, overrides: dict | None = None) -> dict:
    """Merge broker positions with the levels the agent set for them.

    `overrides` is research_store/monitor/overrides.json — the levels the agent
    set via set_levels, which is what the MONITOR actually enforces. Without it
    this reported the slow loop's thesis stop while a tighter agent-set stop was
    live, so the agent could not read back its own decision and would re-decide
    against a number nothing was using. `stop`/`targets` therefore report the
    ENFORCED level, with the thesis value preserved alongside as `book_stop` and
    the agent's own reason as `level_reason`.

    `valued` is marks.load(). `theses` is product.theses (may be empty).
    A held symbol with no thesis is reported with stop/target None and
    `watched=False` — that is the unprotected case and must be visible, not
    silently dropped.

    `watched` mirrors the monitor's OWN watch condition exactly — see
    scripts/market_monitor.py:283 (`t.target_weight > 0 and t.stop`) and the
    matching de-risk-eligible set in scripts/risk_review.py:250 (`_held`).
    A thesis with a stop but `target_weight == 0` (or missing) is NOT watched
    by either of those — reporting `watched=True` for it here would be a false
    "protected" signal, worse than reporting unwatched, so this must match the
    system of record rather than approximate it (e.g. via `stop is not None`).
    """
    by_sym = {t.symbol: t for t in (theses or [])}
    valued = valued or {}
    av = float(valued.get("account_value") or 0.0)
    out = {}
    ov = overrides or {}
    for sym, p in (valued.get("positions") or {}).items():
        t = by_sym.get(sym)
        book_stop = getattr(t, "stop", None) if t else None
        book_targets = list(getattr(t, "targets", []) or []) if t else []
        o = ov.get(sym) or {}
        agent_stop = o.get("stop")
        agent_targets = o.get("targets")
        rec = {
            "qty": p.get("qty"),
            "avg_cost": p.get("avg_cost"),
            "mark": p.get("mark"),
            "value": p.get("value"),
            "pnl": p.get("pnl"),
            "share_of_equity": (float(p["value"]) / av) if av > 0 and p.get("value") is not None else None,
            "stop": agent_stop if agent_stop is not None else book_stop,
            "targets": agent_targets if agent_targets else book_targets,
            "watched": bool(t is not None and getattr(t, "target_weight", 0) > 0
                            and getattr(t, "stop", None)),
        }
        if agent_stop is not None or agent_targets:
            rec["set_by_agent"] = True
            rec["book_stop"] = book_stop
            if o.get("reason"):
                rec["level_reason"] = o["reason"]
        out[sym] = rec
    return out


def equity_series(path: Path) -> list:
    """Ordered daily equity closes, oldest first. Skips malformed rows rather
    than raising — mandate.drawdown() applies its own missing-data discipline.

    Bare values, no dates — fine for drawdown/concentration, which only care
    about the shape of the book's own track record. NOT safe to pair
    positionally against a series from a different source/schedule (e.g. a
    price panel) — use equity_series_with_dates() + pair_with_benchmark() for
    that; see their docstrings for why positional pairing is a defect."""
    import json
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line)["value"])
        except Exception:
            continue
    return out


def equity_series_with_dates(path: Path) -> list[tuple[str, float]]:
    """Ordered (date, value) pairs, oldest first, straight from equity.jsonl.

    Same tolerance as equity_series(): a malformed row is skipped, never
    raised. Malformed here also covers an unparseable `date` field (not a
    valid ISO calendar date) — a row this tool cannot place on a calendar
    cannot be paired against anything, so it is dropped exactly like a row
    with a missing/non-numeric `value`.

    This exists so a caller can pair each equity point against another
    series (e.g. a benchmark close) BY CALENDAR DATE. equity_series() throws
    the date away, which is only safe when nothing downstream needs to align
    it against a second, independently-scheduled series.
    """
    import datetime
    import json
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            date = row["date"]
            value = float(row["value"])
        except Exception:
            continue
        try:
            datetime.date.fromisoformat(str(date))
        except (TypeError, ValueError):
            continue
        out.append((str(date), value))
    return out


def pair_with_benchmark(eq_dated: list[tuple[str, float]],
                        bench_by_date: dict) -> tuple[list, list]:
    """Pair each equity point with the benchmark's close on the SAME calendar
    date, never by position. `eq_dated` is equity_series_with_dates()'s
    output; `bench_by_date` maps an ISO date string to a benchmark close.

    An equity date with no matching benchmark close drops the pair entirely
    — both the equity value and the (nonexistent) benchmark value — rather
    than substituting a nearby day. Forward-filling or interpolating a
    missing benchmark close would invent a number to keep a pair alive,
    which is exactly how a confident-looking wrong answer gets produced; the
    honest outcome is fewer pairs, reported as INSUFFICIENT_DATA upstream if
    too few survive.

    Duplicate equity dates are not deduplicated here — each row is paired
    independently on its own merits, same as equity_series()/
    equity_series_with_dates() never deduplicate. That is a property of the
    upstream log, not something this pairing step should paper over.

    Returns (equity_values, benchmark_values): two lists, always the same
    length, positionally aligned to each other (index i is one surviving
    pair) even though the INPUT was aligned by date, not position.
    """
    eq_out: list = []
    bm_out: list = []
    for date, value in eq_dated:
        if date in bench_by_date:
            eq_out.append(value)
            bm_out.append(bench_by_date[date])
    return eq_out, bm_out


def _selftest() -> None:
    import types
    T = lambda s, stop, tgts: types.SimpleNamespace(
        symbol=s, stop=stop, targets=tgts, target_weight=0.07)
    valued = {"account_value": 100.0, "cash": 10.0, "invested": 90.0,
              "positions": {"AAA": {"qty": 1.0, "avg_cost": 50.0, "mark": 60.0,
                                    "value": 60.0, "pnl": 0.2},
                            "BBB": {"qty": 1.0, "avg_cost": 30.0, "mark": 30.0,
                                    "value": 30.0, "pnl": 0.0}}}
    h = holdings(valued, [T("AAA", 55.0, [70.0, 80.0])])
    assert h["AAA"]["stop"] == 55.0 and h["AAA"]["watched"] is True, h
    assert h["AAA"]["value"] == 60.0 and h["AAA"]["share_of_equity"] == 0.6, h
    # held with NO thesis -> visible, flagged unwatched, never dropped
    assert "BBB" in h and h["BBB"]["stop"] is None and h["BBB"]["watched"] is False, h
    # a thesis for something NOT held must not appear as a holding
    h2 = holdings(valued, [T("ZZZ", 1.0, [2.0])])
    assert "ZZZ" not in h2, h2
    assert holdings({"account_value": 100.0, "positions": {}}, []) == {}

    # FIX 1: `watched` must match the monitor's own condition exactly —
    # target_weight > 0 AND stop — not just "stop is not None". Cover every
    # divergent combination against a single held position.
    valued2 = {"account_value": 100.0,
               "positions": {"CCC": {"qty": 1.0, "avg_cost": 10.0, "mark": 10.0,
                                     "value": 10.0, "pnl": 0.0}}}
    # stop set but target_weight == 0 -> the false-"protected" case this fix closes
    zero_weight = types.SimpleNamespace(symbol="CCC", stop=9.0, targets=[11.0], target_weight=0.0)
    assert holdings(valued2, [zero_weight])["CCC"]["watched"] is False
    # stop set with a positive target_weight -> genuinely watched
    positive_weight = types.SimpleNamespace(symbol="CCC", stop=9.0, targets=[11.0], target_weight=0.05)
    assert holdings(valued2, [positive_weight])["CCC"]["watched"] is True
    # positive target_weight but no stop -> not watched
    no_stop = types.SimpleNamespace(symbol="CCC", stop=None, targets=[11.0], target_weight=0.05)
    assert holdings(valued2, [no_stop])["CCC"]["watched"] is False
    # thesis missing target_weight entirely -> must not raise, must not be watched
    no_weight_attr = types.SimpleNamespace(symbol="CCC", stop=9.0, targets=[11.0])
    assert holdings(valued2, [no_weight_attr])["CCC"]["watched"] is False

    # FIX 2: valued=None (no snapshot yet) must degrade to an empty dict, not raise.
    assert holdings(None, []) == {}
    assert holdings(None, [T("AAA", 55.0, [70.0, 80.0])]) == {}

    import tempfile, json as _json
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "equity.jsonl"
        f.write_text('{"date":"2026-08-01","value":100.0}\n'
                     'not json\n'
                     '{"date":"2026-08-02","value":95.0}\n')
        assert equity_series(f) == [100.0, 95.0], equity_series(f)
        assert equity_series(Path(d) / "absent.jsonl") == []

    # DATE-PAIRING: equity_series_with_dates() + pair_with_benchmark(). This
    # is the fix for the defect where the benchmark was paired by POSITION —
    # equity and benchmark can happen to match in LENGTH while every date
    # diverges, and a length-only guard can never catch that.
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "equity.jsonl"
        f.write_text(
            '{"date":"2026-08-01","value":100.0}\n'
            'not json\n'
            '{"date":"2026-08-02","value":95.0}\n'
            '{"date":"bogus-date","value":50.0}\n'
            '{"date":"2026-08-03","value":"nan-ish"}\n'
        )
        eqd = equity_series_with_dates(f)
        # unparseable date row and non-numeric value row are both skipped,
        # same tolerance as equity_series()'s "skip, don't raise" discipline
        assert eqd == [("2026-08-01", 100.0), ("2026-08-02", 95.0)], eqd
        assert equity_series_with_dates(Path(d) / "absent.jsonl") == []

    # all dates match -> every pair survives, values correctly aligned
    eqd = [("2026-08-01", 100.0), ("2026-08-02", 95.0), ("2026-08-03", 110.0)]
    bench = {"2026-08-01": 400.0, "2026-08-02": 401.0, "2026-08-03": 402.0}
    eq_out, bm_out = pair_with_benchmark(eqd, bench)
    assert eq_out == [100.0, 95.0, 110.0], eq_out
    assert bm_out == [400.0, 401.0, 402.0], bm_out

    # some equity dates missing from the benchmark -> those pairs dropped on
    # BOTH sides; the surviving pairs stay correctly aligned to each other
    bench_gappy = {"2026-08-01": 400.0, "2026-08-03": 402.0}   # 08-02 missing
    eq_out, bm_out = pair_with_benchmark(eqd, bench_gappy)
    assert eq_out == [100.0, 110.0], eq_out
    assert bm_out == [400.0, 402.0], bm_out
    assert len(eq_out) == len(bm_out)

    # this is the exact failure mode being fixed: same LENGTH, dates offset
    # by a few days (e.g. equity log skips a weekend the price panel has, or
    # vice versa). Positional pairing would silently mismatch every point;
    # date pairing must drop every one of them since none of the dates match.
    eqd_shifted = [("2026-08-03", 100.0), ("2026-08-04", 95.0), ("2026-08-05", 110.0)]
    bench_shifted = {"2026-08-01": 400.0, "2026-08-02": 401.0, "2026-08-03": 402.0}
    eq_out, bm_out = pair_with_benchmark(eqd_shifted, bench_shifted)
    # 08-03 is the only overlapping date -> exactly one surviving pair, not
    # three misaligned ones
    assert eq_out == [100.0], eq_out
    assert bm_out == [402.0], bm_out

    # no dates match at all -> both sides empty, never a synthesized pair
    eq_out, bm_out = pair_with_benchmark(eqd, {"2099-01-01": 999.0})
    assert eq_out == [] and bm_out == [], (eq_out, bm_out)
    eq_out, bm_out = pair_with_benchmark(eqd, {})
    assert eq_out == [] and bm_out == [], (eq_out, bm_out)

    # duplicate dates in the equity log: each row is paired independently
    # (no dedup here — that is a property of the upstream log, not this
    # function's job), and every surviving pair still lines up correctly
    eqd_dup = [("2026-08-01", 100.0), ("2026-08-01", 101.0), ("2026-08-02", 95.0)]
    eq_out, bm_out = pair_with_benchmark(eqd_dup, bench)
    assert eq_out == [100.0, 101.0, 95.0], eq_out
    assert bm_out == [400.0, 400.0, 401.0], bm_out

    print("selftest OK: holdings merges marks with agent levels, unwatched visible")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
