"""THE MANDATE — continuous, falsifiable evaluation of the terms the agent works
under (spec: docs/superpowers/specs/2026-08-09-agent-authority-inversion-design.md §5).

Pure functions over data that already exists on disk. No network, no broker, no
clock of its own — callers pass `asof`. Three-state by design: a criterion that
cannot be computed reports INSUFFICIENT_DATA and MUST NOT read as a pass.

Run the tests:  .venv/bin/python src/mandate.py --selftest
"""
from __future__ import annotations

import math
import sys
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

PASS = "PASS"
FAIL = "FAIL"
INSUFFICIENT = "INSUFFICIENT_DATA"


def load(path: str = "config/mandate.toml") -> dict:
    """Load the mandate terms. Raises if absent — an unstated mandate is not a
    permissive one, and must never silently default."""
    p = REPO / path
    if not p.exists():
        raise FileNotFoundError(f"mandate not found at {p}; the terms must be explicit")
    with p.open("rb") as fh:
        return tomllib.load(fh)


def drawdown(equity: list[float], max_pct: float) -> dict:
    """Criterion 1 (BLOCKING). Close-to-close drawdown from the all-time high-water
    mark. `equity` is the ordered daily close series, oldest first.

    THE PEAK USED HERE IS max(equity), recomputed fresh on every call — this is
    the repo's ONE authoritative high-water mark for anything that can flatten
    the book (2026-08-10, FIX A). It is deliberately NOT read from
    research_store/governance/state.json's `peak_value` (src/governance.py's
    update_peak()/drawdown_halt()), even though the design spec originally
    said "governance.update_peak already tracks it" (now corrected in
    docs/superpowers/specs/2026-08-09-agent-authority-inversion-design.md §5).
    That tracker is sampled at an arbitrary moment (whenever scripts/
    fast_loop.py happens to call gates()) with no fixed cadence and no way to
    reproduce the number from disk; it has already drifted from this series in
    the live state file (82.22 there vs 81.99 = max of the real equity.jsonl on
    2026-08-10) and would drift further, monotonically, since a stale peak can
    never come back down. `equity` here, by contrast, is an append-only,
    audit-recomputable track record written once daily by scripts/
    log_equity.py — exactly the "close-to-close, never intraday" discipline
    this criterion requires. Do not add a code path that substitutes
    governance's tracker for this recomputation; if a caller wants the
    mandate's peak, it must pass this function the equity series, not a
    cached number.

    Never measured intraday: an intraday measure fires the flatten on noise.

    Missing/unusable data discipline: a missing or non-numeric MOST RECENT point
    means current equity is unknown, so this reports INSUFFICIENT_DATA rather than
    silently promoting an older value to "current" (which would report a stale
    drawdown as if it were live). Missing points elsewhere in the series are
    dropped from the computation as before, but the count is surfaced via
    `nulls_dropped` and folded into `reason` so a gapped series can never read as
    a clean one — a dropped point may also have been the true high-water peak,
    understating the drawdown.

    Interior non-finite discipline (FIX B, 2026-08-10): an interior NaN/+inf/-inf
    is dropped and counted in `nulls_dropped`, EXACTLY like an interior None --
    it is NOT an abort. `equity.jsonl` is append-only and permanent, and this is
    a BLOCKING criterion: aborting to INSUFFICIENT_DATA on any single corrupt
    interior point would make criterion 1 unmeasurable FOREVER (permanent
    tradeable=False / degraded mode), unfixable without hand-editing the track
    record. The MOST RECENT point keeps the strict abort above -- a stale value
    must never masquerade as current, so "unknown right now" still reports
    INSUFFICIENT_DATA rather than silently reusing an older close. Since that
    check already runs first and validates equity[-1] specifically, any
    non-finite value reached by the loop below is by construction interior.
    """
    out = {"criterion": "drawdown", "state": INSUFFICIENT, "value": None,
           "limit": max_pct, "room": None, "reason": "", "nulls_dropped": 0}

    if not equity:
        out["reason"] = "need 2+ daily closes, have 0"
        return out

    last_raw = equity[-1]
    if last_raw is None:
        out["reason"] = "most recent equity value is missing; current equity is unknown"
        return out
    try:
        last_val = float(last_raw)
    except (TypeError, ValueError):
        out["reason"] = (f"most recent equity value is non-numeric ({last_raw!r}); "
                          f"current equity is unknown")
        return out
    if not math.isfinite(last_val):
        out["reason"] = (f"most recent equity value is non-finite ({last_raw!r}); "
                          f"current equity is unknown")
        return out

    vals: list[float] = []
    nulls_dropped = 0
    for v in equity:
        if v is None:
            nulls_dropped += 1
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            out["reason"] = f"non-numeric equity value ({v!r}) in series"
            out["nulls_dropped"] = nulls_dropped
            return out
        if not math.isfinite(fv):
            # Interior non-finite -- dropped and counted exactly like an
            # interior None, never an abort. See the docstring's "Interior
            # non-finite discipline (FIX B)" note above for why: the MOST
            # RECENT point was already validated finite above, so reaching
            # this branch means the corrupt value is interior, and this is a
            # BLOCKING criterion whose input log is permanent/append-only.
            nulls_dropped += 1
            continue
        vals.append(fv)

    if len(vals) < 2:
        out["reason"] = f"need 2+ daily closes, have {len(vals)}"
        return out
    peak = max(vals)
    if peak <= 0:
        out["reason"] = "non-positive peak equity; drawdown undefined"
        return out
    dd = vals[-1] / peak - 1.0
    out["value"] = dd
    out["room"] = abs(max_pct) + dd          # how much further it may fall
    out["state"] = FAIL if dd < (-abs(max_pct) - 1e-9) else PASS
    out["nulls_dropped"] = nulls_dropped
    reason = (f"{dd:.2%} from peak {peak:.2f} "
              f"(limit {abs(max_pct):.0%})")
    if nulls_dropped:
        reason += f"; {nulls_dropped} missing value(s) dropped from series"
    out["reason"] = reason
    return out


def concentration(positions: dict, account_value: float, max_pct: float) -> dict:
    """Criterion 2 (BLOCKING). Largest single position as a share of equity.

    A position carrying no usable mark returns INSUFFICIENT rather than being
    skipped: an unmeasurable position is exactly the one most likely to be the
    concentrated one.

    Same non-finite trap as `drawdown()`: NaN and +/-Infinity are valid Python
    floats, so `isinstance(v, (int, float))` alone would let them through --
    and because every comparison against NaN is False, the FAIL branch would
    then be unreachable and this would report a confident PASS. Both a
    position's `value` and `account_value` are validated with
    `math.isfinite()`, not just a type check.

    This account is long-only and cannot short or borrow -- limited margin
    settles proceeds, it does not lend. A negative
    position `value` therefore cannot be a real short exposure -- it means the
    data is corrupt. `max(shares, key=...)` would pick the signed maximum, so
    a negative mark either (a) is alone and reads as a small negative "PASS",
    or (b) sits next to a smaller positive mark and vanishes from the
    assessment entirely while the positive one is reported as worst. Both are
    silent failures on a criterion that authorises action, so a negative
    value is rejected up front, same as None/NaN/+-inf. Exactly 0.0 (dust, or
    a written-down position) is legitimate and handled normally.
    """
    out = {"criterion": "concentration", "state": INSUFFICIENT, "value": None,
           "limit": max_pct, "room": None, "worst_symbol": None, "reason": ""}
    try:
        av = float(account_value)
    except (TypeError, ValueError):
        out["reason"] = "account value unusable; concentration undefined"
        return out
    if not math.isfinite(av) or av <= 0:
        out["reason"] = "account value unusable; concentration undefined"
        return out
    shares = {}
    for sym, p in (positions or {}).items():
        v = p.get("value")
        try:
            fv = float(v)
        except (TypeError, ValueError):
            out["reason"] = f"{sym} has no usable mark; concentration unmeasurable"
            return out
        if not math.isfinite(fv):
            out["reason"] = f"{sym} has a non-finite mark; concentration unmeasurable"
            return out
        if fv < 0.0:
            out["reason"] = (
                f"{sym} has a negative market value ({fv:.2f}); a long-only cash "
                "account cannot hold a negative position, so this indicates "
                "corrupt data, not a short exposure; concentration unmeasurable"
            )
            return out
        shares[sym] = fv / av
    if not shares:
        out.update(state=PASS, value=0.0, room=abs(max_pct),
                   reason="no positions held")
        return out
    worst = max(shares, key=lambda s: shares[s])
    out["worst_symbol"] = worst
    out["value"] = shares[worst]
    out["room"] = abs(max_pct) - shares[worst]
    out["state"] = FAIL if shares[worst] > abs(max_pct) else PASS
    out["reason"] = (f"{worst} at {shares[worst]:.1%} of equity "
                     f"(limit {abs(max_pct):.0%})")
    return out


def _as_date(ts: str):
    """Parse an ISO timestamp to a date. Returns None if unparseable — callers
    must treat that as missing data, never as in-window."""
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()
    except Exception:
        return None


def pnl_concentration(outcomes: list[dict], asof: str, window_days: int,
                      max_share: float, min_names: int) -> dict:
    """Criterion 3 (INFORMATIONAL — never gates an order). Over the trailing
    window: no single closed round-trip above `max_share` of realized P&L, and at
    least `min_names` distinct names closed.

    Requires `realized_usd` on the outcome record. Records predating 2026-08-09
    carry only `pnl_pct` and cannot support a share-of-dollars test; they report
    INSUFFICIENT rather than passing by default.

    Same non-finite trap as `drawdown()`/`concentration()`: NaN and +/-Infinity
    are valid Python floats, so an `isinstance(v, (int, float))` check alone does
    not exclude them, and because every comparison against NaN is False, a NaN
    folded into the running total could make the FAIL branch unreachable and
    read as a confident PASS. Missing `realized_usd` (the key absent, or
    explicitly `None`) is legitimate legacy data -- pre-2026-08-09 records never
    carried it -- and is excluded, counted in `missing`, letting the remaining
    rows still produce a result. But a *present* `realized_usd` that is
    non-numeric or non-finite is corrupt, not legacy: like `concentration()`'s
    treatment of a negative mark, it aborts the whole computation with
    INSUFFICIENT naming the offending symbol, rather than being silently
    excluded and let the rest of the window read as clean -- the corrupt
    record might have been the largest, and dropping it would understate the
    concentration exactly like a dropped drawdown peak.
    """
    out = {"criterion": "pnl_concentration", "state": INSUFFICIENT, "value": None,
           "limit": max_share, "room": None, "distinct_names": 0, "reason": ""}
    cutoff = _as_date(asof)
    if cutoff is None:
        out["reason"] = f"unparseable asof {asof!r}"
        return out
    start = cutoff - timedelta(days=window_days)

    rows, missing = [], 0
    for e in outcomes or []:
        if e.get("event") != "outcome":
            continue
        d = _as_date(e.get("at", ""))
        if d is None or not (start <= d <= cutoff):
            continue
        outcome_data = e.get("outcome") or {}
        if "realized_usd" not in outcome_data or outcome_data.get("realized_usd") is None:
            missing += 1
            continue
        usd = outcome_data.get("realized_usd")
        try:
            fusd = float(usd)
        except (TypeError, ValueError):
            out["reason"] = (f"{e.get('symbol')!r} has a non-numeric realized_usd "
                             f"({usd!r}); pnl_concentration unmeasurable")
            return out
        if not math.isfinite(fusd):
            out["reason"] = (f"{e.get('symbol')!r} has a non-finite realized_usd "
                             f"({usd!r}); pnl_concentration unmeasurable")
            return out
        rows.append((e.get("symbol"), fusd))

    if not rows:
        out["reason"] = (f"no closed round-trip in the last {window_days}d carries "
                         f"realized_usd ({missing} lacked it)")
        return out
    total = sum(u for _, u in rows)
    if total <= 0:
        out["reason"] = (f"trailing realized P&L is {total:.2f}; a share-of-profit "
                         "test has no meaning without profit")
        return out

    biggest = max(u for _, u in rows)
    share = biggest / total
    names = len({s for s, _ in rows})
    out.update(value=share, distinct_names=names, room=abs(max_share) - share)
    ok = share <= abs(max_share) and names >= min_names
    out["state"] = PASS if ok else FAIL
    out["reason"] = (f"largest round-trip {share:.0%} of {total:.2f} realized "
                     f"(limit {abs(max_share):.0%}), {names} distinct names "
                     f"(min {min_names})")
    if missing:
        out["reason"] += f"; {missing} legacy record(s) lacked realized_usd and were excluded"
    return out


def relative_return(equity: list[float], benchmark: list[float],
                    window_days: int) -> dict:
    """Criterion 4 (INFORMATIONAL -- never gates an order). Total return of the
    marked book against the benchmark over the trailing window, with a floor of
    >= 0 on the book's own return.

    Assumes no deposits or withdrawals across the window: the equity series is a
    pure mark-to-market curve, so a change in value is entirely P&L.

    Same non-finite trap as `drawdown()`/`concentration()`/`pnl_concentration()`:
    NaN and +/-Infinity are valid Python floats, so an `isinstance(v, (int,
    float))` check alone would not exclude them, and because every comparison
    against NaN is False, a NaN could make a FAIL branch unreachable and read as
    a confident PASS. The math here only reads each series' two window
    endpoints, but a non-finite value ANYWHERE in the window -- including an
    interior point neither endpoint touches -- means the series is corrupt and
    the window cannot be trusted, so the whole series (not just the endpoints)
    is scanned with `math.isfinite()` before any ratio is taken.
    """
    out = {"criterion": "relative_return", "state": INSUFFICIENT, "value": None,
           "benchmark_return": None, "limit": 0.0, "room": None, "reason": ""}
    if len(equity) != len(benchmark):
        out["reason"] = (f"series misaligned: {len(equity)} equity vs "
                         f"{len(benchmark)} benchmark points")
        return out
    if len(equity) < window_days:
        out["reason"] = (f"need {window_days} daily closes, have {len(equity)} "
                         "-- criterion not yet mature")
        return out

    window_eq = equity[-window_days:]
    window_bm = benchmark[-window_days:]
    for label, series in (("equity", window_eq), ("benchmark", window_bm)):
        for v in series:
            try:
                fv = float(v)
            except (TypeError, ValueError):
                out["reason"] = f"non-numeric {label} value ({v!r}) in window"
                return out
            if not math.isfinite(fv):
                out["reason"] = f"non-finite {label} value ({v!r}) in window"
                return out

    e0, e1 = float(window_eq[0]), float(window_eq[-1])
    b0, b1 = float(window_bm[0]), float(window_bm[-1])
    if e0 <= 0 or b0 <= 0:
        out["reason"] = "non-positive starting value; return undefined"
        return out
    er, br = e1 / e0 - 1.0, b1 / b0 - 1.0
    out.update(value=er, benchmark_return=br, room=er - br)
    if er < 0:
        out["state"] = FAIL
        out["reason"] = (f"book {er:+.2%} vs benchmark {br:+.2%} over {window_days}d "
                         "-- below the >= 0 floor")
        return out
    out["state"] = PASS if er >= br else FAIL
    out["reason"] = f"book {er:+.2%} vs benchmark {br:+.2%} over {window_days}d"
    return out


BLOCKING = ("drawdown", "concentration")
INFORMATIONAL = ("pnl_concentration", "relative_return")


def status(equity: list[float], benchmark: list[float], positions: dict,
           account_value: float, outcomes: list[dict], asof: str,
           cfg: dict | None = None) -> dict:
    """Evaluate all four criteria. THE single entry point -- the MCP tool, the
    monitor and the dashboard all read this, so there is exactly one answer to
    "are we passing".

    `tradeable` is False when a BLOCKING criterion fails or cannot be measured.
    Informational criteria never affect it: they judge whether autonomy is
    working, not whether an order is safe.

    EXCEPTION HANDLING: this function does NOT catch exceptions. A malformed `cfg`
    (e.g. missing required keys) or an unexpected bug in a criterion function will
    propagate as a crash rather than being converted into a degraded status. This
    is intentional and consistent with the module's fail-loud design: load() raises
    rather than defaulting, and callers (such as the intraday monitor) must be
    aware that a crash is possible and handle it in their own error boundaries.
    """
    m = cfg or load()
    crit = {
        "drawdown": drawdown(equity, m["drawdown"]["max_pct"]),
        "concentration": concentration(positions, account_value,
                                       m["concentration"]["max_position_pct"]),
        "pnl_concentration": pnl_concentration(
            outcomes, asof, m["pnl_concentration"]["window_days"],
            m["pnl_concentration"]["max_single_share"],
            m["pnl_concentration"]["min_distinct_names"]),
        "relative_return": relative_return(
            equity, benchmark, m["relative_return"]["window_days"]),
    }
    b_fail = [k for k in BLOCKING if crit[k]["state"] == FAIL]
    b_dark = [k for k in BLOCKING if crit[k]["state"] == INSUFFICIENT]
    i_fail = [k for k in INFORMATIONAL if crit[k]["state"] == FAIL]
    return {"asof": asof, "criteria": crit,
            "blocking_fail": b_fail, "blocking_unmeasurable": b_dark,
            "informational_fail": i_fail,
            "tradeable": not (b_fail or b_dark),
            "degraded": bool(b_dark)}


def _selftest() -> None:
    m = load()
    # Pins the CURRENT mandate value on purpose: changing a term the agent is judged
    # against must be a deliberate act that also updates this line, never a quiet config
    # edit. If you are here because this failed, that is the mechanism working.
    assert m["drawdown"]["max_pct"] == 0.20, m["drawdown"]
    assert m["concentration"]["max_position_pct"] == 0.15, m["concentration"]
    assert m["pnl_concentration"]["window_days"] == 90
    assert m["pnl_concentration"]["max_single_share"] == 0.40
    assert m["pnl_concentration"]["min_distinct_names"] == 4
    assert m["relative_return"]["window_days"] == 60
    assert m["relative_return"]["benchmark"] == "SPY"
    # Pairwise, not chained: `PASS != FAIL != INSUFFICIENT` reads as a three-way
    # check but only asserts the two adjacent pairs, never PASS vs INSUFFICIENT.
    # These three being distinguishable IS the module's central safety property.
    assert PASS != FAIL and FAIL != INSUFFICIENT and PASS != INSUFFICIENT

    # --- criterion 1: drawdown ------------------------------------------------
    # Fixed literals below, deliberately NOT read from config/mandate.toml. These
    # test the FUNCTIONS, and their expected values are derived from 0.15. Reading
    # the live limit coupled function tests to a tunable mandate value, so retuning
    # the drawdown limit broke tests that were not about it. The live config values
    # are pinned separately in the config-load assertions above -- that pin is the
    # thing that should fail when the mandate changes, and only that.
    md = 0.15
    # peak 100 -> 95 is a 5% drawdown against a 15% limit: PASS, 10% of room left
    r = drawdown([80.0, 100.0, 95.0], md)
    assert r["state"] == PASS, r
    assert abs(r["value"] + 0.05) < 1e-9, r
    assert abs(r["room"] - 0.10) < 1e-9, r
    # peak 100 -> 84 breaches 15%
    assert drawdown([100.0, 84.0], md)["state"] == FAIL
    # exactly at the limit is NOT a breach (breach is strictly worse than the limit)
    assert drawdown([100.0, 85.0], md)["state"] == PASS
    # the peak is all-time and does not follow the book down
    assert abs(drawdown([100.0, 90.0, 92.0], md)["value"] + 0.08) < 1e-9
    # fewer than two points cannot express a drawdown
    assert drawdown([100.0], md)["state"] == INSUFFICIENT
    assert drawdown([], md)["state"] == INSUFFICIENT
    # a non-positive peak is undefined, not a pass
    assert drawdown([0.0, 0.0], md)["state"] == INSUFFICIENT
    # a clean series (no gaps) reports nulls_dropped == 0
    assert r["nulls_dropped"] == 0, r

    # --- missing/unusable-data discipline (review finding) --------------------
    # trailing None: the MOST RECENT point is missing -> current equity is
    # unknown, must NOT silently fall back to an older value as if it were live
    r_trail = drawdown([100.0, 95.0, None], md)
    assert r_trail["state"] == INSUFFICIENT, r_trail
    assert "current equity is unknown" in r_trail["reason"], r_trail
    # leading + interior None: still computable from the remaining points, but
    # the drop count must be surfaced so a gapped series can't read as clean
    r_gap = drawdown([None, 100.0, None, 95.0], md)
    assert r_gap["state"] == PASS, r_gap
    assert r_gap["nulls_dropped"] == 2, r_gap
    assert "2 missing value(s) dropped" in r_gap["reason"], r_gap
    assert abs(r_gap["value"] + 0.05) < 1e-9, r_gap
    # the two-day-outage example from the finding: [100.0, None, None, 95.0]
    r_outage = drawdown([100.0, None, None, 95.0], md)
    assert r_outage["state"] == PASS, r_outage
    assert r_outage["nulls_dropped"] == 2, r_outage
    assert "missing value(s) dropped" in r_outage["reason"], r_outage
    # non-numeric value anywhere -> INSUFFICIENT_DATA, never an uncaught exception
    r_bad_last = drawdown([100.0, 95.0, "oops"], md)
    assert r_bad_last["state"] == INSUFFICIENT, r_bad_last
    assert "non-numeric" in r_bad_last["reason"], r_bad_last
    r_bad_interior = drawdown([100.0, "oops", 95.0], md)
    assert r_bad_interior["state"] == INSUFFICIENT, r_bad_interior
    assert "non-numeric" in r_bad_interior["reason"], r_bad_interior

    # --- non-finite discipline (review finding) --------------------------------
    # a NaN/inf equity reading must NEVER read as a PASS on the criterion that
    # authorises a mechanical flatten of the book.
    nan = float("nan")
    pos_inf = float("inf")
    neg_inf = float("-inf")
    # NaN as the MOST RECENT point -> current equity is unknown, same treatment
    # as a missing/non-numeric trailing value
    r_nan_last = drawdown([100.0, 95.0, nan], md)
    assert r_nan_last["state"] == INSUFFICIENT, r_nan_last
    assert "non-finite" in r_nan_last["reason"], r_nan_last
    assert "current equity is unknown" in r_nan_last["reason"], r_nan_last
    # NaN in an interior slot (FIX B, 2026-08-10): dropped and counted, exactly
    # like an interior None -- NOT an abort. A single corrupt interior point in
    # the permanent, append-only equity.jsonl must never make this BLOCKING
    # criterion unmeasurable forever. peak=100 (nan dropped), last=95 -> -5%,
    # PASS against the 15% test limit.
    r_nan_interior = drawdown([100.0, nan, 95.0], md)
    assert r_nan_interior["state"] == PASS, r_nan_interior
    assert r_nan_interior["nulls_dropped"] == 1, r_nan_interior
    assert abs(r_nan_interior["value"] + 0.05) < 1e-9, r_nan_interior
    # +inf as the MOST RECENT point -> still the strict abort (current equity
    # unknown), unaffected by the interior-drop change above
    r_pos_inf = drawdown([100.0, 95.0, pos_inf], md)
    assert r_pos_inf["state"] == INSUFFICIENT, r_pos_inf
    assert "non-finite" in r_pos_inf["reason"], r_pos_inf
    # -inf as the MOST RECENT point -> same strict abort
    r_neg_inf = drawdown([100.0, 95.0, neg_inf], md)
    assert r_neg_inf["state"] == INSUFFICIENT, r_neg_inf
    assert "non-finite" in r_neg_inf["reason"], r_neg_inf
    # -inf in an interior slot -> dropped and counted, same as the NaN-interior
    # case above, never an abort
    r_neg_inf_interior = drawdown([100.0, neg_inf, 95.0], md)
    assert r_neg_inf_interior["state"] == PASS, r_neg_inf_interior
    assert r_neg_inf_interior["nulls_dropped"] == 1, r_neg_inf_interior
    assert abs(r_neg_inf_interior["value"] + 0.05) < 1e-9, r_neg_inf_interior
    # a dropped None AND a dropped interior non-finite together must both be
    # reflected in nulls_dropped -- neither aborts, both are counted
    r_null_then_nan = drawdown([100.0, None, nan, 95.0], md)
    assert r_null_then_nan["state"] == PASS, r_null_then_nan
    assert r_null_then_nan["nulls_dropped"] == 2, r_null_then_nan
    assert abs(r_null_then_nan["value"] + 0.05) < 1e-9, r_null_then_nan
    # a genuinely non-numeric interior value ("oops", not NaN/inf) is corrupt
    # data of a DIFFERENT kind -- not a float at all -- and keeps aborting to
    # INSUFFICIENT_DATA; FIX B only changes non-finite FLOAT handling
    r_null_then_bad = drawdown([100.0, None, "oops", 95.0], md)
    assert r_null_then_bad["state"] == INSUFFICIENT, r_null_then_bad
    assert r_null_then_bad["nulls_dropped"] == 1, r_null_then_bad
    # FIX B core regression: a single corrupt interior point must never make
    # this BLOCKING criterion unmeasurable FOREVER (permanent tradeable=False /
    # degraded mode from one bad write to a permanent, append-only log). The
    # criterion must stay LIVE and able to FAIL -- peak 100 (interior nan
    # dropped) -> 83 is a genuine breach of the 15% test limit, not an
    # INSUFFICIENT_DATA cop-out.
    r_fixb_breach = drawdown([100.0, nan, 83.0], md)
    assert r_fixb_breach["state"] == FAIL, r_fixb_breach
    assert r_fixb_breach["nulls_dropped"] == 1, r_fixb_breach
    # ordinary finite values are entirely unaffected by the isfinite check
    r_finite_unaffected = drawdown([80.0, 100.0, 95.0], md)
    assert r_finite_unaffected["state"] == PASS, r_finite_unaffected
    assert abs(r_finite_unaffected["value"] + 0.05) < 1e-9, r_finite_unaffected
    assert abs(r_finite_unaffected["room"] - 0.10) < 1e-9, r_finite_unaffected
    assert r_finite_unaffected["nulls_dropped"] == 0, r_finite_unaffected

    # --- criterion 2: concentration -------------------------------------------
    mc = 0.15
    pos = {"AAA": {"value": 10.0}, "BBB": {"value": 5.0}}
    r = concentration(pos, 100.0, mc)          # worst is 10% against a 15% limit
    assert r["state"] == PASS, r
    assert r["worst_symbol"] == "AAA" and abs(r["value"] - 0.10) < 1e-9, r
    assert abs(r["room"] - 0.05) < 1e-9, r
    # 20% of equity in one name breaches
    r = concentration({"AAA": {"value": 20.0}}, 100.0, mc)
    assert r["state"] == FAIL and r["worst_symbol"] == "AAA", r
    # exactly at the limit is not a breach
    assert concentration({"AAA": {"value": 15.0}}, 100.0, mc)["state"] == PASS
    # a flat book trivially passes
    assert concentration({}, 100.0, mc)["state"] == PASS
    # unusable account value is undefined, not a pass
    assert concentration(pos, 0.0, mc)["state"] == INSUFFICIENT
    # a position with no mark cannot be assessed -- must not be silently skipped
    assert concentration({"AAA": {"value": None}}, 100.0, mc)["state"] == INSUFFICIENT

    # --- non-finite discipline (same trap as drawdown's review findings) -------
    # NaN/inf are valid Python floats and pass an isinstance(float) check, so a
    # bad mark must be caught by math.isfinite(), not just a type check -- else
    # the FAIL comparison silently evaluates False and this reads as a PASS.
    r_nan_pos = concentration({"AAA": {"value": float("nan")}}, 100.0, mc)
    assert r_nan_pos["state"] == INSUFFICIENT, r_nan_pos
    assert "AAA" in r_nan_pos["reason"], r_nan_pos
    r_inf_pos = concentration({"AAA": {"value": float("inf")}}, 100.0, mc)
    assert r_inf_pos["state"] == INSUFFICIENT, r_inf_pos
    assert "AAA" in r_inf_pos["reason"], r_inf_pos
    # a non-finite account_value must not be silently coerced/compared away
    r_nan_av = concentration(pos, float("nan"), mc)
    assert r_nan_av["state"] == INSUFFICIENT, r_nan_av
    r_inf_av = concentration(pos, float("inf"), mc)
    assert r_inf_av["state"] == INSUFFICIENT, r_inf_av
    # an ordinary book with finite marks is entirely unaffected by the isfinite check
    r_ord = concentration(pos, 100.0, mc)
    assert r_ord["state"] == PASS, r_ord
    assert r_ord["worst_symbol"] == "AAA" and abs(r_ord["value"] - 0.10) < 1e-9, r_ord
    assert abs(r_ord["room"] - 0.05) < 1e-9, r_ord

    # --- negative market value discipline (review finding) ---------------------
    # A long-only account that cannot short will not hold a negative position:
    # a negative
    # `value` means the data is corrupt, not that there is a short exposure.
    # `max(shares, key=...)` picks the signed maximum, so a lone negative mark
    # used to read as a small negative "PASS" -- must be INSUFFICIENT instead.
    r_neg_solo = concentration({"AAA": {"value": -20.0}}, 100.0, mc)
    assert r_neg_solo["state"] == INSUFFICIENT, r_neg_solo
    assert r_neg_solo["value"] is None, r_neg_solo
    assert r_neg_solo["worst_symbol"] is None, r_neg_solo
    assert "AAA" in r_neg_solo["reason"], r_neg_solo
    assert "negative" in r_neg_solo["reason"], r_neg_solo
    # a negative mark next to a smaller positive one must NOT be shadowed: the
    # old signed-max bug picked BBB as worst and dropped AAA from the
    # assessment entirely. Must be INSUFFICIENT naming AAA, not a PASS on BBB.
    r_neg_shadow = concentration(
        {"AAA": {"value": -20.0}, "BBB": {"value": 5.0}}, 100.0, mc
    )
    assert r_neg_shadow["state"] == INSUFFICIENT, r_neg_shadow
    assert r_neg_shadow["worst_symbol"] != "BBB", r_neg_shadow
    assert r_neg_shadow["worst_symbol"] is None, r_neg_shadow
    assert "AAA" in r_neg_shadow["reason"], r_neg_shadow
    # order must not matter -- the negative mark is caught regardless of
    # dict iteration order
    r_neg_shadow_rev = concentration(
        {"BBB": {"value": 5.0}, "AAA": {"value": -20.0}}, 100.0, mc
    )
    assert r_neg_shadow_rev["state"] == INSUFFICIENT, r_neg_shadow_rev
    assert "AAA" in r_neg_shadow_rev["reason"], r_neg_shadow_rev
    # a zero-value position is LEGITIMATE (dust, or fully written down) --
    # 0.0 < 0.0 is False, so it must NOT be caught by the negative check and
    # must be handled exactly as any other ordinary position.
    r_zero = concentration({"AAA": {"value": 0.0}, "BBB": {"value": 5.0}}, 100.0, mc)
    assert r_zero["state"] == PASS, r_zero
    assert r_zero["worst_symbol"] == "BBB" and abs(r_zero["value"] - 0.05) < 1e-9, r_zero
    assert abs(r_zero["room"] - 0.10) < 1e-9, r_zero
    r_zero_solo = concentration({"AAA": {"value": 0.0}}, 100.0, mc)
    assert r_zero_solo["state"] == PASS, r_zero_solo
    assert r_zero_solo["worst_symbol"] == "AAA" and r_zero_solo["value"] == 0.0, r_zero_solo
    assert abs(r_zero_solo["room"] - abs(mc)) < 1e-9, r_zero_solo
    # a clean book (no negative marks at all) is entirely unaffected
    r_clean = concentration(pos, 100.0, mc)
    assert r_clean["state"] == PASS, r_clean
    assert r_clean["worst_symbol"] == "AAA" and abs(r_clean["value"] - 0.10) < 1e-9, r_clean
    assert abs(r_clean["room"] - 0.05) < 1e-9, r_clean

    # --- criterion 3: P&L concentration (informational) -----------------------
    pc = m["pnl_concentration"]
    def _o(sym, day, usd):
        return {"event": "outcome", "symbol": sym, "at": f"2026-08-{day:02d}T00:00:00Z",
                "outcome": {"realized_usd": usd}}
    # four names, no single one above 40% of $100 realized -> PASS
    good = [_o("A", 1, 30.0), _o("B", 2, 30.0), _o("C", 3, 25.0), _o("D", 4, 15.0)]
    r = pnl_concentration(good, "2026-08-09", pc["window_days"],
                          pc["max_single_share"], pc["min_distinct_names"])
    assert r["state"] == PASS, r
    assert abs(r["value"] - 0.30) < 1e-9 and r["distinct_names"] == 4, r
    # one name carrying 70% of the result -> FAIL
    hot = [_o("A", 1, 70.0), _o("B", 2, 10.0), _o("C", 3, 10.0), _o("D", 4, 10.0)]
    assert pnl_concentration(hot, "2026-08-09", 90, 0.40, 4)["state"] == FAIL
    # too few distinct names -> FAIL even when no single share is too big
    thin = [_o("A", 1, 30.0), _o("B", 2, 30.0), _o("A", 3, 40.0)]
    assert pnl_concentration(thin, "2026-08-09", 90, 0.40, 4)["state"] == FAIL
    # a LOSS-making window has no share-of-profit to test -> INSUFFICIENT, not PASS
    down = [_o("A", 1, -30.0), _o("B", 2, 10.0)]
    assert pnl_concentration(down, "2026-08-09", 90, 0.40, 4)["state"] == INSUFFICIENT
    # records without realized_usd (every record before 2026-08-09) -> INSUFFICIENT
    legacy = [{"event": "outcome", "symbol": "A", "at": "2026-08-01T00:00:00Z",
               "outcome": {"pnl_pct": 0.011}}]
    r = pnl_concentration(legacy, "2026-08-09", 90, 0.40, 4)
    assert r["state"] == INSUFFICIENT and "realized_usd" in r["reason"], r
    # nothing closed at all -> INSUFFICIENT
    assert pnl_concentration([], "2026-08-09", 90, 0.40, 4)["state"] == INSUFFICIENT
    # outside the window is excluded
    old = [_o("A", 1, 100.0)]
    assert pnl_concentration(old, "2026-12-01", 90, 0.40, 4)["state"] == INSUFFICIENT

    # --- non-finite discipline (same trap as drawdown/concentration) -----------
    # a NaN realized_usd must not silently propagate into the sum: every
    # comparison against NaN is False, so a NaN could make the FAIL branch
    # unreachable and read as a confident PASS.
    nan_rows = [_o("A", 1, float("nan")), _o("B", 2, 10.0),
                _o("C", 3, 10.0), _o("D", 4, 10.0)]
    r_nan = pnl_concentration(nan_rows, "2026-08-09", 90, 0.40, 4)
    assert r_nan["state"] == INSUFFICIENT, r_nan
    assert "A" in r_nan["reason"], r_nan
    # a +inf realized_usd is likewise rejected, not coerced into "biggest"
    inf_rows = [_o("A", 1, float("inf")), _o("B", 2, 10.0),
                _o("C", 3, 10.0), _o("D", 4, 10.0)]
    r_inf = pnl_concentration(inf_rows, "2026-08-09", 90, 0.40, 4)
    assert r_inf["state"] == INSUFFICIENT, r_inf
    assert "A" in r_inf["reason"], r_inf
    # a record with an unparseable/missing `at` cannot be placed in-window ->
    # excluded, must not silently count as in-window
    bad_at = [{"event": "outcome", "symbol": "A", "at": "not-a-date",
               "outcome": {"realized_usd": 30.0}},
              {"event": "outcome", "symbol": "B",
               "outcome": {"realized_usd": 30.0}}]  # missing `at` entirely
    r_bad_at = pnl_concentration(bad_at, "2026-08-09", 90, 0.40, 4)
    assert r_bad_at["state"] == INSUFFICIENT, r_bad_at
    # a clean set (the "good" case above) is entirely unaffected by the
    # non-finite / bad-timestamp handling
    r_clean = pnl_concentration(good, "2026-08-09", pc["window_days"],
                                pc["max_single_share"], pc["min_distinct_names"])
    assert r_clean["state"] == PASS, r_clean
    assert abs(r_clean["value"] - 0.30) < 1e-9 and r_clean["distinct_names"] == 4, r_clean

    # --- six review-verified boundary cases (now permanent assertions) -----------
    # 1. -inf realized_usd -> INSUFFICIENT, naming the offending symbol.
    # (NaN and +inf are already covered above; -inf was not tested)
    neg_inf_rows = [_o("A", 1, float("-inf")), _o("B", 2, 10.0),
                    _o("C", 3, 10.0), _o("D", 4, 10.0)]
    r_neg_inf = pnl_concentration(neg_inf_rows, "2026-08-09", 90, 0.40, 4)
    assert r_neg_inf["state"] == INSUFFICIENT, r_neg_inf
    assert "A" in r_neg_inf["reason"], r_neg_inf

    # 2. Total realized P&L exactly 0.0 -> INSUFFICIENT.
    # (Negative total already covered by the "down" test; exactly zero was untested)
    zero_total = [_o("A", 1, 50.0), _o("B", 2, -50.0)]
    r_zero = pnl_concentration(zero_total, "2026-08-09", 90, 0.40, 4)
    assert r_zero["state"] == INSUFFICIENT, r_zero
    assert "profit" in r_zero["reason"], r_zero

    # 3. max_single_share boundary: exactly 40.0% of total -> PASS.
    # Construct: A=$40, B=$30, C=$20, D=$10 (total=$100, A=40%)
    exactly_40pct = [_o("A", 1, 40.0), _o("B", 2, 30.0), _o("C", 3, 20.0), _o("D", 4, 10.0)]
    r_40 = pnl_concentration(exactly_40pct, "2026-08-09", 90, 0.40, 4)
    assert r_40["state"] == PASS, r_40
    assert abs(r_40["value"] - 0.40) < 1e-9, r_40
    # 40.0009% -> FAIL. Construct: A=$40.0009, B=$30, C=$20, D=$9.9991 (total=$100)
    over_40pct = [_o("A", 1, 40.0009), _o("B", 2, 30.0), _o("C", 3, 20.0), _o("D", 4, 9.9991)]
    r_over = pnl_concentration(over_40pct, "2026-08-09", 90, 0.40, 4)
    assert r_over["state"] == FAIL, r_over
    assert r_over["value"] > 0.40, r_over

    # 4. min_distinct_names boundary: exactly 4 distinct names passes;
    # exactly 3 fails. Share test is comfortably passing in both cases.
    four_names = [_o("A", 1, 30.0), _o("B", 2, 30.0), _o("C", 3, 25.0), _o("D", 4, 15.0)]
    r_four = pnl_concentration(four_names, "2026-08-09", 90, 0.40, 4)
    assert r_four["state"] == PASS, r_four
    assert r_four["distinct_names"] == 4, r_four
    # three distinct names with no concentration issue still fails on name count
    three_names = [_o("A", 1, 30.0), _o("B", 2, 30.0), _o("C", 3, 40.0)]
    r_three = pnl_concentration(three_names, "2026-08-09", 90, 0.40, 4)
    assert r_three["state"] == FAIL, r_three
    assert r_three["distinct_names"] == 3, r_three

    # 5. Per-trade, not per-symbol aggregation. One symbol closes multiple times
    # with per-symbol aggregate exceeding 40%, but no individual round-trip does.
    # A: three trades of $15 each (total $45, aggregate is 45% of $100, exceeds 40%).
    # But each individual A trade is 15%, and the max single trade is $15 (15% of $100).
    # With B=$10, C=$10, D=$10, E=$10, F=$15 (total=$100, distinct=6, max trade=15%):
    multi_trade = [_o("A", 1, 15.0), _o("A", 2, 15.0), _o("A", 3, 15.0),
                   _o("B", 4, 10.0), _o("C", 5, 10.0), _o("D", 6, 10.0),
                   _o("E", 7, 10.0), _o("F", 8, 15.0)]
    r_multi = pnl_concentration(multi_trade, "2026-08-09", 90, 0.40, 4)
    assert r_multi["state"] == PASS, r_multi
    assert abs(r_multi["value"] - 0.15) < 1e-9, r_multi
    assert r_multi["distinct_names"] == 6, r_multi

    # 6. Records with event != "outcome" are ignored, not counted in totals
    # or distinct-name set. A "decision" event with realized_usd=$100 is present
    # but excluded; the test passes because only the outcome events are counted.
    non_outcome = [
        _o("A", 1, 30.0),
        _o("B", 2, 30.0),
        _o("C", 3, 25.0),
        {"event": "decision", "symbol": "D", "at": "2026-08-04T00:00:00Z",
         "outcome": {"realized_usd": 100.0}},  # ignored: not an outcome event
        _o("D", 5, 15.0)
    ]
    r_non_outcome = pnl_concentration(non_outcome, "2026-08-09", 90, 0.40, 4)
    assert r_non_outcome["state"] == PASS, r_non_outcome
    assert r_non_outcome["distinct_names"] == 4, r_non_outcome
    assert abs(r_non_outcome["value"] - 0.30) < 1e-9, r_non_outcome

    # --- criterion 4: relative return (informational) -------------------------
    # book +10%, SPY +5% over the window -> PASS
    eq = [100.0] * 58 + [100.0, 110.0]
    spy = [50.0] * 58 + [50.0, 52.5]
    r = relative_return(eq, spy, 60)
    assert r["state"] == PASS, r
    assert abs(r["value"] - 0.10) < 1e-9 and abs(r["benchmark_return"] - 0.05) < 1e-9, r
    assert abs(r["room"] - 0.05) < 1e-9, r
    # book +2%, SPY +5% -> FAIL (lagging the benchmark)
    assert relative_return([100.0] * 59 + [102.0], spy, 60)["state"] == FAIL
    # book DOWN beats a worse SPY on relative terms, but the >= 0 floor still fails it
    r = relative_return([100.0] * 59 + [98.0], [50.0] * 59 + [45.0], 60)
    assert r["state"] == FAIL and "floor" in r["reason"], r
    # too little history -> INSUFFICIENT (22 rows is the real 2026-08-09 state)
    r = relative_return([100.0] * 22, [50.0] * 22, 60)
    assert r["state"] == INSUFFICIENT and "22" in r["reason"], r
    # mismatched series lengths are a data error, not a pass
    assert relative_return([100.0] * 60, [50.0] * 59, 60)["state"] == INSUFFICIENT
    # a non-positive starting value makes the return undefined
    assert relative_return([0.0] * 60, [50.0] * 60, 60)["state"] == INSUFFICIENT

    # --- non-finite discipline (same trap as drawdown/concentration/pnl) -------
    nan = float("nan")
    pos_inf = float("inf")
    neg_inf = float("-inf")
    # non-finite at the equity window's START endpoint
    r_nan_e0 = relative_return([nan] + [100.0] * 59, spy, 60)
    assert r_nan_e0["state"] == INSUFFICIENT, r_nan_e0
    assert "non-finite" in r_nan_e0["reason"], r_nan_e0
    # non-finite at the equity window's END endpoint (most recent)
    r_nan_e1 = relative_return([100.0] * 59 + [pos_inf], spy, 60)
    assert r_nan_e1["state"] == INSUFFICIENT, r_nan_e1
    assert "non-finite" in r_nan_e1["reason"], r_nan_e1
    # non-finite at an INTERIOR point of equity (not an endpoint) -- the return
    # only reads the two endpoints, but a corrupt series in between still means
    # the series cannot be trusted and must not silently read as clean
    r_nan_e_mid = relative_return(
        [100.0] * 29 + [neg_inf] + [100.0] * 29 + [110.0], spy, 60
    )
    assert r_nan_e_mid["state"] == INSUFFICIENT, r_nan_e_mid
    assert "non-finite" in r_nan_e_mid["reason"], r_nan_e_mid
    # non-finite at the benchmark window's START endpoint
    r_nan_b0 = relative_return(eq, [nan] + [50.0] * 59, 60)
    assert r_nan_b0["state"] == INSUFFICIENT, r_nan_b0
    assert "non-finite" in r_nan_b0["reason"], r_nan_b0
    # non-finite at the benchmark window's END endpoint (most recent)
    r_nan_b1 = relative_return(eq, [50.0] * 59 + [nan], 60)
    assert r_nan_b1["state"] == INSUFFICIENT, r_nan_b1
    assert "non-finite" in r_nan_b1["reason"], r_nan_b1
    # non-finite at an INTERIOR point of benchmark
    r_nan_b_mid = relative_return(
        eq, [50.0] * 29 + [pos_inf] + [50.0] * 29 + [52.5], 60
    )
    assert r_nan_b_mid["state"] == INSUFFICIENT, r_nan_b_mid
    assert "non-finite" in r_nan_b_mid["reason"], r_nan_b_mid
    # a zero starting value in the BENCHMARK is undefined, same as equity
    r_zero_b0 = relative_return(eq, [0.0] * 59 + [52.5], 60)
    assert r_zero_b0["state"] == INSUFFICIENT, r_zero_b0
    assert "non-positive" in r_zero_b0["reason"], r_zero_b0
    # both series entirely flat -- zero return on both sides. Book return (0.0)
    # is not < 0 so the floor is satisfied, and 0.0 >= 0.0 (benchmark) is also
    # satisfied -> PASS, matching neither, doesn't beat, the benchmark either.
    r_flat = relative_return([100.0] * 60, [50.0] * 60, 60)
    assert r_flat["state"] == PASS, r_flat
    assert r_flat["value"] == 0.0 and r_flat["benchmark_return"] == 0.0, r_flat
    assert r_flat["room"] == 0.0, r_flat
    # a clean series is entirely unaffected by the non-finite handling
    r_clean_rr = relative_return(eq, spy, 60)
    assert r_clean_rr["state"] == PASS, r_clean_rr
    assert abs(r_clean_rr["value"] - 0.10) < 1e-9, r_clean_rr
    assert abs(r_clean_rr["benchmark_return"] - 0.05) < 1e-9, r_clean_rr

    # --- status(): the aggregate ----------------------------------------------
    TCFG = {"drawdown": {"max_pct": 0.15},
            "concentration": {"max_position_pct": 0.15},
            "pnl_concentration": {"window_days": 90, "max_single_share": 0.40,
                                  "min_distinct_names": 4},
            "relative_return": {"window_days": 60, "benchmark": "SPY"}}

    healthy = status(equity=[100.0, 101.0], benchmark=[50.0, 50.0],
                     positions={"AAA": {"value": 10.0}}, account_value=100.0,
                     outcomes=[], asof="2026-08-09", cfg=TCFG)
    assert healthy["criteria"]["drawdown"]["state"] == PASS
    assert healthy["criteria"]["concentration"]["state"] == PASS
    # informational criteria are immature today and must NOT block trading
    assert healthy["criteria"]["pnl_concentration"]["state"] == INSUFFICIENT
    assert healthy["criteria"]["relative_return"]["state"] == INSUFFICIENT
    assert healthy["informational_fail"] == []
    assert healthy["blocking_fail"] == [] and healthy["blocking_unmeasurable"] == []
    assert healthy["tradeable"] is True and healthy["degraded"] is False, healthy

    # a blocking FAIL stops trading
    breached = status(equity=[100.0, 80.0], benchmark=[50.0, 50.0],
                      positions={}, account_value=100.0, outcomes=[],
                      asof="2026-08-09", cfg=TCFG)
    assert breached["blocking_fail"] == ["drawdown"], breached
    assert breached["tradeable"] is False

    # a blocking criterion that cannot be MEASURED means degraded mode, not a pass
    dark = status(equity=[100.0, 99.0], benchmark=[50.0, 50.0],
                  positions={"AAA": {"value": None}}, account_value=100.0,
                  outcomes=[], asof="2026-08-09", cfg=TCFG)
    assert dark["blocking_unmeasurable"] == ["concentration"], dark
    assert dark["tradeable"] is False and dark["degraded"] is True, dark

    # --- additional coverage beyond the brief -----------------------------
    # both blocking criteria FAIL at once
    both_fail = status(
        equity=[100.0, 80.0], benchmark=[50.0, 50.0],
        positions={"AAA": {"value": 20.0}}, account_value=100.0,
        outcomes=[], asof="2026-08-09", cfg=TCFG)
    assert both_fail["criteria"]["drawdown"]["state"] == FAIL, both_fail
    assert both_fail["criteria"]["concentration"]["state"] == FAIL, both_fail
    assert set(both_fail["blocking_fail"]) == {"drawdown", "concentration"}, both_fail
    assert both_fail["blocking_unmeasurable"] == [], both_fail
    assert both_fail["tradeable"] is False, both_fail
    assert both_fail["degraded"] is False, both_fail

    # a blocking FAIL alongside an informational FAIL -- tradeable reflects
    # ONLY the blocking one, and the informational FAIL still surfaces
    hot = [_o("A", 1, 70.0), _o("B", 2, 10.0), _o("C", 3, 10.0), _o("D", 4, 10.0)]
    blocking_and_info_fail = status(
        equity=[100.0, 80.0], benchmark=[50.0, 50.0],
        positions={}, account_value=100.0, outcomes=hot, asof="2026-08-09", cfg=TCFG)
    assert blocking_and_info_fail["criteria"]["drawdown"]["state"] == FAIL, blocking_and_info_fail
    assert blocking_and_info_fail["criteria"]["pnl_concentration"]["state"] == FAIL, blocking_and_info_fail
    assert blocking_and_info_fail["blocking_fail"] == ["drawdown"], blocking_and_info_fail
    assert blocking_and_info_fail["informational_fail"] == ["pnl_concentration"], blocking_and_info_fail
    assert blocking_and_info_fail["tradeable"] is False, blocking_and_info_fail
    assert blocking_and_info_fail["degraded"] is False, blocking_and_info_fail

    # both informational criteria FAIL while both blocking ones PASS --
    # tradeable must stay True; informational failure is visible but non-gating
    thin = [_o("A", 1, 30.0), _o("B", 2, 30.0), _o("A", 3, 40.0)]
    lagging_eq = [100.0] * 59 + [102.0]
    lagging_bm = [50.0] * 58 + [50.0, 60.0]
    info_only_fail = status(
        equity=lagging_eq, benchmark=lagging_bm,
        positions={"AAA": {"value": 10.0}}, account_value=100.0,
        outcomes=thin, asof="2026-08-09", cfg=TCFG)
    assert info_only_fail["criteria"]["drawdown"]["state"] == PASS, info_only_fail
    assert info_only_fail["criteria"]["concentration"]["state"] == PASS, info_only_fail
    assert info_only_fail["criteria"]["pnl_concentration"]["state"] == FAIL, info_only_fail
    assert info_only_fail["criteria"]["relative_return"]["state"] == FAIL, info_only_fail
    assert set(info_only_fail["informational_fail"]) == {"pnl_concentration", "relative_return"}, info_only_fail
    assert info_only_fail["blocking_fail"] == [] and info_only_fail["blocking_unmeasurable"] == [], info_only_fail
    assert info_only_fail["tradeable"] is True, info_only_fail
    assert info_only_fail["degraded"] is False, info_only_fail

    # every criterion simultaneously INSUFFICIENT_DATA
    all_dark = status(
        equity=[100.0], benchmark=[50.0],
        positions={"AAA": {"value": None}}, account_value=100.0,
        outcomes=[], asof="2026-08-09", cfg=TCFG)
    assert all_dark["criteria"]["drawdown"]["state"] == INSUFFICIENT, all_dark
    assert all_dark["criteria"]["concentration"]["state"] == INSUFFICIENT, all_dark
    assert all_dark["criteria"]["pnl_concentration"]["state"] == INSUFFICIENT, all_dark
    assert all_dark["criteria"]["relative_return"]["state"] == INSUFFICIENT, all_dark
    assert set(all_dark["blocking_unmeasurable"]) == {"drawdown", "concentration"}, all_dark
    assert all_dark["blocking_fail"] == [], all_dark
    assert all_dark["informational_fail"] == [], all_dark
    assert all_dark["tradeable"] is False, all_dark
    assert all_dark["degraded"] is True, all_dark

    # --- four permanently-verified combinations (review findings 2026-08-09) ----
    # Case 1: concentration FAIL alone (drawdown PASS) -> blocking_fail=["concentration"],
    # tradeable=False, degraded=False.
    # 10% drawdown against 15% limit = PASS; 20% position against 15% limit = FAIL
    conc_fail_alone = status(
        equity=[100.0, 90.0], benchmark=[50.0, 50.0],
        positions={"AAA": {"value": 20.0}}, account_value=100.0,
        outcomes=[], asof="2026-08-09", cfg=TCFG)
    assert conc_fail_alone["blocking_fail"] == ["concentration"], conc_fail_alone
    assert conc_fail_alone["blocking_unmeasurable"] == [], conc_fail_alone
    assert conc_fail_alone["tradeable"] is False, conc_fail_alone
    assert conc_fail_alone["degraded"] is False, conc_fail_alone

    # Case 2: drawdown INSUFFICIENT_DATA alone (concentration PASS) ->
    # blocking_unmeasurable=["drawdown"], tradeable=False, degraded=True.
    # Only 1 equity point = INSUFFICIENT; positions 10% = PASS
    dd_insufficient_alone = status(
        equity=[100.0], benchmark=[50.0],
        positions={"AAA": {"value": 10.0}}, account_value=100.0,
        outcomes=[], asof="2026-08-09", cfg=TCFG)
    assert dd_insufficient_alone["blocking_fail"] == [], dd_insufficient_alone
    assert dd_insufficient_alone["blocking_unmeasurable"] == ["drawdown"], dd_insufficient_alone
    assert dd_insufficient_alone["tradeable"] is False, dd_insufficient_alone
    assert dd_insufficient_alone["degraded"] is True, dd_insufficient_alone

    # Case 3: one blocking criterion FAIL, the other INSUFFICIENT_DATA ->
    # both blocking lists populated, tradeable=False, degraded=True.
    # Drawdown INSUFFICIENT (only 1 point); concentration FAIL (25% > 15%)
    mixed_blocking = status(
        equity=[100.0], benchmark=[50.0],
        positions={"AAA": {"value": 25.0}}, account_value=100.0,
        outcomes=[], asof="2026-08-09", cfg=TCFG)
    assert mixed_blocking["blocking_fail"] == ["concentration"], mixed_blocking
    assert mixed_blocking["blocking_unmeasurable"] == ["drawdown"], mixed_blocking
    assert mixed_blocking["tradeable"] is False, mixed_blocking
    assert mixed_blocking["degraded"] is True, mixed_blocking

    # Case 4: all four criteria PASS -> all lists empty, tradeable=True,
    # degraded=False. Requires: drawdown < 15%, concentration < 15%, 4+ distinct
    # closed trades each < 40% of total with positive sum, and book return >=
    # benchmark return >= 0 over the window.
    # Equity: 60 points, starting at 100, ending at 110 (+10% return, no DD)
    # Benchmark: 60 points, starting at 50, ending at 52.5 (+5% return, lower)
    # Positions: AAA at 10% of equity (< 15%) = $10 of $100
    # Outcomes: 4 trades over last 90 days, realistic profit distribution,
    # no single trade > 40% of $120 total, >= 4 distinct names
    full_eq = [100.0] * 58 + [100.0, 110.0]  # 60 points: PASS drawdown
    full_bm = [50.0] * 58 + [50.0, 52.5]     # 60 points: +10% vs +5%
    full_pos = {"AAA": {"value": 10.0}}      # 10% of $100: PASS concentration
    full_outcomes = [
        _o("A", 1, 35.0),   # 35/120 = 29.2% of profit
        _o("B", 2, 35.0),   # 35/120 = 29.2%
        _o("C", 3, 30.0),   # 30/120 = 25.0%
        _o("D", 4, 20.0)    # 20/120 = 16.7%
        # Total: $120, max single: 35 (29.2% < 40%), 4 distinct names
    ]
    all_pass = status(
        equity=full_eq, benchmark=full_bm,
        positions=full_pos, account_value=100.0,
        outcomes=full_outcomes, asof="2026-08-09", cfg=TCFG)
    assert all_pass["blocking_fail"] == [], all_pass
    assert all_pass["blocking_unmeasurable"] == [], all_pass
    assert all_pass["informational_fail"] == [], all_pass
    assert all_pass["tradeable"] is True, all_pass
    assert all_pass["degraded"] is False, all_pass

    print("selftest OK: mandate -- 4 criteria three-state, blocking vs "
          "informational, unmeasurable never passes")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
