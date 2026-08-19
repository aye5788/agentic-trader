"""What the agent holds, and what it is worth. Pure assembly over marks.load()
and the current product — no new valuation logic, so the agent, the dashboard and
the equity log can never disagree about what a position is worth."""
from __future__ import annotations

import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

import levels                                   # noqa: E402


def _fin(x) -> bool:
    """Finite number, or False. NaN and +/-inf are floats, so an isinstance
    check alone lets them through and every later comparison silently reads
    False -- which is how a corrupt value reaches a field that looks measured."""
    try:
        return isinstance(x, (int, float)) and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def holdings(valued: dict, theses: list, overrides: dict | None = None,
             monitor_prices: dict | None = None) -> dict:
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
    scripts/market_monitor.py:283 (`t.target_weight > 0 and t.stop`). (The same
    condition gated scripts/risk_review.py's de-risk-eligible set until that was
    retired into the sessions on 2026-08-13.)
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
    # ⛔ STABLE, NON-SEMANTIC ORDER. Codex, 2026-08-18: "Sorting is not neutral
    # arithmetic. It determines salience... the agent may simply sell the top row
    # and explain afterward why proximity dominated everything else." Alphabetical
    # carries no policy meaning, so no ordering can be read as a recommendation.
    for sym, p in sorted((valued.get("positions") or {}).items()):
        t = by_sym.get(sym)
        book_stop = getattr(t, "stop", None) if t else None
        book_targets = list(getattr(t, "targets", []) or []) if t else []
        # A non-dict entry in overrides.json is IGNORED, matching
        # apply_overrides, rather than raising and taking positions() down.
        _raw_o = ov.get(sym)
        o = _raw_o if isinstance(_raw_o, dict) else {}
        agent_stop = o.get("stop")
        agent_targets = o.get("targets")
        # price_guard=True: the live monitor always passes a prices dict, so a
        # missing mark DROPS the override there. The display must fail closed
        # the same way or it would show a level the monitor is not using.
        # ⛔ THE MONITOR'S PRICE SOURCE, NOT THE MARK. apply_overrides validates
        # against monitor/quotes.json. `mark` here can be a snapshot price or
        # even COST BASIS when no monitor quote exists (src/marks.py), so
        # resolving against it reproduces the exact divergence this module was
        # written to remove: with no quote the monitor refuses an override and
        # the display would still show it. Found by review, 2026-08-18.
        _mp = (monitor_prices or {})
        _lv = levels.resolve(book_stop, book_targets, o,
                             price=_mp.get(sym), price_guard=True,
                             thesis_as_of=getattr(t, "as_of", None))
        _sig = dict(getattr(t, "signals", {}) or {}) if t else {}
        _qty, _ac, _mk = p.get("qty"), p.get("avg_cost"), p.get("mark")
        _eff = _lv["effective_stop"]
        _cost_basis = (round(float(_qty) * float(_ac), 4)
                       if _qty is not None and _ac is not None else None)
        _w = (float(p["value"]) / av) if av > 0 and p.get("value") is not None else None
        # prospective: what the position loses from HERE if the stop fires
        _m2s_pct = (round(float(_eff) / float(_mk) - 1.0, 6)
                    if _eff is not None and _mk else None)
        _m2s_dollars = (round((float(_eff) - float(_mk)) * float(_qty), 4)
                        if _eff is not None and _mk and _qty is not None else None)
        # accounting: what the trade books against entry if the stop fires
        _pnl_at_stop_d = (round((float(_eff) - float(_ac)) * float(_qty), 4)
                          if _eff is not None and _ac is not None and _qty is not None else None)
        _sg = _sig.get("sigma")
        # Exact domain: every input finite, mark > 0, sigma > 0. Truthiness
        # alone let a negative/NaN/inf stored sigma through. NO volatility floor
        # and NO cap on the result -- both would be thresholds, and an
        # arbitrarily small but VALID sigma legitimately produces a large value.
        _stop_sigma = (round(abs(float(_mk) - float(_eff)) / (float(_sg) * float(_mk)), 3)
                       if (_fin(_mk) and _fin(_eff) and _fin(_sg)
                           and float(_mk) > 0 and float(_sg) > 0) else None)
        # True iff the mark is above average cost AND the currently watched
        # effective stop is below it. None when any input, or the watched stop,
        # is unavailable -- claiming what a position "would close at" with no
        # stop in force would imply protection that does not exist.
        _prof_loss = (None if not (_fin(_ac) and _fin(_mk) and _fin(_eff))
                      else bool(float(_mk) > float(_ac) and float(_eff) < float(_ac)))
        _r12 = _sig.get("R")
        _eligibility = (None if t is None else
                        ("eligible: 12-month return positive" if isinstance(_r12, (int, float)) and _r12 > 0
                         else "INELIGIBLE: 12-month return not positive" if isinstance(_r12, (int, float))
                         else "unknown: no stored 12-month return"))
        _retention = (None if t is None else
                      ("in the target book" if getattr(t, "target_weight", 0) > 0
                       else "held but not selected by the ranking — geometry supplied "
                            "so the monitor can watch it"))
        rec = {
            "qty": p.get("qty"),
            "avg_cost": p.get("avg_cost"),
            "mark": p.get("mark"),
            "value": p.get("value"),
            "pnl": p.get("pnl"),
            "share_of_equity": (float(p["value"]) / av) if av > 0 and p.get("value") is not None else None,
            # ⛔ WHAT IS ENFORCED, not what was requested. This used to take the
            # agent's override whenever one existed, while the monitor applied
            # it only when stricter (or explicitly widened) and only when the
            # target COUNT matched. So the agent read back its own numbers and
            # believed them to be in force. Measured 2026-08-18: XOM, RTX and
            # BAC all displayed the stop and single target the session had just
            # reasoned out, while the monitor had rejected every one of them.
            # src/levels.resolve is now the single rule, proven equal to
            # market_monitor.apply_overrides on the live book.
            "stop": _lv["effective_stop"],
            "targets": list(_lv["effective_targets"]),
            # Mirrors market_monitor.in_book() EXACTLY — see this function's
            # docstring on why it must not approximate. A weight of 0 with
            # verdict "hold" is a HELD name the ranking did not select, given
            # geometry so it can be watched (slow_loop.protective_theses);
            # weight 0 with verdict "avoid" failed the R:R gate and is not held.
            # ---- Codex spec step 4: SAFE deterministic fields. Every one is
            # arithmetic over quantity, cost, mark and NAV, or a STORED thesis
            # fact carried with its source and as-of. No estimator is invented
            # here: `realized_vol_value` is the sigma momentum.compute already
            # wrote, not a new calculation.
            "shares": p.get("qty"),
            "market_value": p.get("value"),
            "weight_pct_nav": _w,
            "cost_basis": _cost_basis,
            "mark_as_of": valued.get("marked_at"),
            "unrealized_pnl_dollars": (
                None if _cost_basis is None or p.get("value") is None
                else round(float(p["value"]) - _cost_basis, 4)),
            "unrealized_pnl_pct_cost": (
                None if not _cost_basis or p.get("value") is None
                else round(float(p["value"]) / _cost_basis - 1.0, 6)),
            "strategy_rank": getattr(t, "rank", None) if t else None,
            "rank_as_of": getattr(t, "as_of", None) if t else None,
            "twelve_month_return": _sig.get("R"),
            "realized_vol_value": _sig.get("sigma"),
            "realized_vol_window": ("252 trading days (momentum.compute lookback)"
                                    if _sig.get("sigma") is not None else None),
            "realized_vol_source": _sig.get("source"),
            # as-of on EVERY stored fact, not only some. The review found
            # realized_vol_source present with no realized_vol_as_of, rank_as_of
            # present with no rank source, and thesis_as_of emitted only when an
            # override existed -- so a stored number could be read with no way
            # to tell how old it was.
            "realized_vol_as_of": getattr(t, "as_of", None) if t else None,
            "strategy_rank_source": _sig.get("source"),
            "thesis_as_of": getattr(t, "as_of", None) if t else None,
            "eligibility_state": _eligibility,
            "retention_state": _retention,
            # ---- Codex spec step 5: STOP-DERIVED. Unblocked only because the
            # resolver above now decides one effective stop and the replay
            # fixtures prove it equals what the monitor enforces.
            # mark_to_stop_* is PROSPECTIVE downside from here.
            # trade_pnl_at_stop_* is ACCOUNTING P&L against entry. They are
            # different quantities and are deliberately named apart: conflating
            # them is what made a position showing "+10.27% if stopped" read as
            # though it had no downside.
            "mark_to_stop_pct": _m2s_pct,
            "mark_to_stop_dollars": _m2s_dollars,
            "mark_to_stop_pct_nav": (
                None if _m2s_dollars is None or av <= 0 else round(_m2s_dollars / av, 6)),
            "trade_pnl_at_stop_dollars": _pnl_at_stop_d,
            "trade_pnl_at_stop_pct_cost": (
                None if not _cost_basis or _pnl_at_stop_d is None
                else round(_pnl_at_stop_d / _cost_basis, 6)),
            "stop_distance_sigma": _stop_sigma,
            # Replaces gain_protected_pct, removed 2026-08-19 for exploding as a
            # position sits near its entry and ranking two holdings backwards on
            # live money. A BOOLEAN cannot invert a comparison; magnitude comes
            # from trade_pnl_at_stop_* and mark_to_stop_*.
            "profitable_now_but_loss_at_stop": _prof_loss,
            "watched": bool(t is not None and getattr(t, "stop", None)
                            and (getattr(t, "target_weight", 0) > 0
                                 or getattr(t, "verdict", "") == "hold")),
        }
        if agent_stop is not None or agent_targets:
            rec["set_by_agent"] = True
            rec["book_stop"] = book_stop
            # Say plainly when what you asked for is NOT what is being watched.
            rec["override_written_at"] = _lv["override_written_at"]
            rec["your_stop"] = agent_stop
            rec["your_targets"] = list(agent_targets) if agent_targets else None
            rec["stop_status"] = _lv["effective_stop_status"]
            rec["targets_status"] = _lv["effective_targets_status"]
            rec["levels_in_force"] = (_lv["effective_stop_source"] == "override"
                                      or "REJECTED" not in _lv["effective_stop_status"]) and \
                                     "REJECTED" not in _lv["effective_targets_status"]
            _rej = [k for k in ("stop", "targets")
                    if "REJECTED" in _lv[f"effective_{k}_status"]]
            if _rej:
                rec["LEVELS_NOT_IN_FORCE"] = (
                    f"the monitor is NOT using the {' and '.join(_rej)} you set "
                    f"for {sym}. stop: {_lv['effective_stop_status']}. "
                    f"targets: {_lv['effective_targets_status']}. What is watched: stop "
                    f"{_lv['effective_stop']}, targets {_lv['effective_targets']}.")
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


def _levels_field_selftest() -> None:
    """The two fields that replaced an unstable ratio, and their domains.

    gain_protected_pct was removed 2026-08-19 for exploding as a position sits
    near entry; stop_distance_sigma survives but has the same asymptote as
    sigma -> 0 and was previously guarded only by truthiness, so a negative,
    NaN or infinite stored sigma passed straight through into a field that
    looks measured.
    """
    import types as _t

    def _one(cost, mark, stop, sigma=0.02, av=100.0):
        th = _t.SimpleNamespace(symbol="A", stop=stop, targets=[], target_weight=1.0,
                                verdict="buy", as_of="t", signals={"sigma": sigma})
        v = {"account_value": av,
             "positions": {"A": {"qty": 1.0, "avg_cost": cost, "mark": mark,
                                 "value": mark}}}
        return holdings(v, [th], {}, {"A": mark})["A"]

    # profitable now, stop below cost -> TRUE (the state the ratio existed for)
    assert _one(100.0, 110.0, 95.0)["profitable_now_but_loss_at_stop"] is True
    # profitable, stop at or above cost -> False (break-even is not a loss)
    assert _one(100.0, 110.0, 100.0)["profitable_now_but_loss_at_stop"] is False
    assert _one(100.0, 110.0, 105.0)["profitable_now_but_loss_at_stop"] is False
    # not profitable -> False regardless of where the stop sits
    assert _one(100.0, 100.0, 90.0)["profitable_now_but_loss_at_stop"] is False
    assert _one(100.0, 90.0, 80.0)["profitable_now_but_loss_at_stop"] is False
    # ⛔ NEAR-ENTRY STABILITY: a gain of one cent must not blow anything up.
    # This is the exact condition that made the old ratio rank two positions
    # backwards -- the boolean simply reports the state.
    assert _one(100.0, 100.01, 95.0)["profitable_now_but_loss_at_stop"] is True
    # no stop in force -> None, never False: claiming what a position "would
    # close at" with nothing watching implies protection that does not exist
    assert _one(100.0, 110.0, None)["profitable_now_but_loss_at_stop"] is None

    # stop_distance_sigma domain: finite inputs, mark > 0, sigma > 0
    assert _one(100.0, 110.0, 95.0, sigma=0.02)["stop_distance_sigma"] is not None
    for bad in (0.0, -0.02, float("nan"), float("inf"), None, "x"):
        assert _one(100.0, 110.0, 95.0, sigma=bad)["stop_distance_sigma"] is None, bad
    # NO cap: a small but VALID sigma legitimately yields a large number, and
    # capping it would be a threshold.
    big = _one(100.0, 110.0, 95.0, sigma=0.0001)["stop_distance_sigma"]
    assert big is not None and big > 1000, big

    print("selftest OK: profitable_now_but_loss_at_stop is a state flag stable "
          "at a one-cent gain and null with no stop; stop_distance_sigma is "
          "null outside its domain and uncapped inside it")


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
    _levels_field_selftest()


# The exact semantic warning Codex specified, carried verbatim so the tool
# description and the charter cannot drift from each other.
LEVELS_SEMANTIC_NOTE = (
    "These columns are measurements, not recommendations, and row order has no "
    "policy meaning. `trade_pnl_at_stop` is P&L relative to entry and is not "
    "prospective risk. `mark_to_stop` assumes execution exactly at the stop; "
    "gaps and slippage can produce a larger loss. `stop_distance_sigma` is "
    "standardized distance under the stated volatility estimator, not "
    "stop-hit probability. No single column identifies the correct trim. "
    "Portfolio exposure, cluster concentration, strategy state, and the "
    "proposed order's before/after effects remain separate considerations."
)


def compare_trims(held: dict, account_value: float, notional: float,
                  symbols=None) -> dict:
    """Factual before/after deltas for an equal-notional trim in each holding.

    Codex spec, 2026-08-18. It answers the counterfactual "what would this
    same-sized trim change?" and NOTHING else: it does not approve, reject,
    rank or select an action, and it returns no composite score.

    DELIBERATELY OMITTED, per that same review:
      - `estimated_portfolio_volatility_delta` -- needs a covariance model this
        system does not have. "Computable, yes; honestly specified today, no."
      - `estimated_slippage` -- likewise a model, not a measurement.
      - `cluster_weight_delta` -- correlation clustering exists in
        src/concentration.py but its model contract (lookback, threshold, panel
        as-of, minimum observations, version) is not approved yet.
    Each is absent rather than approximated, because an unvalidated estimate
    sitting beside measured facts is indistinguishable from one.
    """
    # A negative notional describes a PURCHASE as a trim: proceeds and quantity
    # go negative, post-trim value and gross exposure RISE. Refused rather than
    # computed. Found by review, 2026-08-18.
    if not isinstance(notional, (int, float)) or not (float(notional) > 0):
        return {"error": f"notional must be a positive number, got {notional!r}. "
                         "A trim reduces a position; this tool does not model "
                         "adding to one.", "trims": {}}
    out, av = {}, float(account_value or 0.0)
    names = sorted(held) if symbols is None else sorted(symbols)
    for sym in names:
        p = held.get(sym) or {}
        mk, qty = p.get("mark"), p.get("shares", p.get("qty"))
        val, eff = p.get("market_value", p.get("value")), p.get("stop")
        if not mk or qty is None or val is None:
            out[sym] = {"error": "no mark or quantity — trim cannot be measured"}
            continue
        sell_notional = min(float(notional), float(val))
        sell_qty = sell_notional / float(mk)
        post_val = float(val) - sell_notional
        d = {
            "trim_notional": round(sell_notional, 4),
            "trim_quantity": round(sell_qty, 8),
            "proceeds": round(sell_notional, 4),
            "post_trim_value": round(post_val, 4),
            "post_trim_weight_pct_nav": round(post_val / av, 6) if av > 0 else None,
            "position_weight_delta": (round(-sell_notional / av, 6) if av > 0 else None),
            "gross_exposure_delta": round(-sell_notional, 4),
            "strategy_rank_of_trimmed_name": p.get("strategy_rank"),
        }
        if eff is not None:
            before = (float(eff) - float(mk)) * float(qty)
            after = (float(eff) - float(mk)) * (float(qty) - sell_qty)
            d["mark_to_stop_downside_nav_delta"] = (
                round((after - before) / av, 6) if av > 0 else None)
        else:
            d["mark_to_stop_downside_nav_delta"] = None
        out[sym] = d
    return {"notional": float(notional), "note": LEVELS_SEMANTIC_NOTE, "trims": out}
