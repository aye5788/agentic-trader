#!/usr/bin/env python3
"""Retro-adjust the daily OHLC panel for stock splits.

    /usr/bin/python3 scripts/adjust_splits.py [--dry] [--selftest]

⚠️ SYSTEM python3 (3.10), not .venv — it imports the moomoo adapter.

WHAT BREAKS WITHOUT THIS
    The panel stores RAW closes, so a split lands in it as a genuine one-day
    return: MNST's 2:1 on 2026-08-11 reads as -50.2%. Momentum's R, sigma and
    trend are all computed off these returns (src/momentum.py), so one split
    poisons a name's score for a full 252-day lookback — and sigma is what sets
    stop distance and target geometry, so it reaches real orders, not just the
    ranking. 21 split-shaped moves are present across the current 10-year panel.

    A prior fix (0e5c67b) stopped a split triggering a phantom SALE in the
    monitor. That guarded the STOP path only; the panel itself stayed raw, so
    the signal kept eating splits as returns. This is the other half.

HOW IT DECIDES
    Detection is FREE and local: a split-shaped move is large and does NOT
    revert the next session. Confirmation costs one get_rehab call per
    candidate, which is why we do not sweep 168 names daily — get_rehab is
    per-symbol.

    An adjustment is only ever applied on POSITIVE evidence from get_rehab. An
    empty result means "no data OR unreachable" (the adapter cannot tell them
    apart), and is treated as "do nothing" — never as proof a name did not
    split.

IDEMPOTENCY IS THE WHOLE GAME
    Applying a split twice quarters the history instead of halving it, silently,
    with no error and no way to tell from the panel afterwards. Every applied
    (ticker, ex_date) is therefore recorded in splits_applied.json and never
    re-applied. That ledger is the ONLY thing standing between a re-run and a
    corrupted ten-year panel, so it is written BEFORE the panel — a crash
    between the two costs one unadjusted split (visible, fixable) rather than a
    double-applied one (invisible, not).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

OUT_DIR = REPO / "research_store" / "prices"
APPLIED = OUT_DIR / "splits_applied.json"
PANELS = {"open": "opens.parquet", "high": "highs.parquet",
          "low": "lows.parquet", "close": "closes.parquet"}

# A move this large that does NOT revert is structurally a split, not a trade.
# Deliberately loose: these are only CANDIDATES, and every one is confirmed
# against get_rehab before anything is touched. A false candidate costs one API
# call; a missed one costs a poisoned year of signal.
DROP_THRESHOLD = -0.35          # 1.55:1 or wider
RISE_THRESHOLD = 0.60           # reverse splits
REVERT_TOLERANCE = 0.20         # next-day move smaller than this = no bounce


def split_candidates(closes: pd.DataFrame,
                     drop=DROP_THRESHOLD, rise=RISE_THRESHOLD,
                     revert=REVERT_TOLERANCE) -> list:
    """Split-SHAPED moves in the panel. PURE — no network, no I/O.

    A crash bounces or keeps falling; a split is a one-off re-basing that the
    next session simply continues from. Requiring the next day to be quiet is
    what separates them, and it is why a -38% earnings collapse does not become
    a candidate.
    """
    out = []
    if closes is None or closes.empty:
        return out
    rets = closes.pct_change()
    for ticker in closes.columns:
        s = rets[ticker].dropna()
        for when, move in s[(s <= drop) | (s >= rise)].items():
            later = s.loc[s.index > when]
            if len(later) and abs(later.iloc[0]) >= revert:
                continue                       # it bounced -> a real move
            out.append({"ticker": str(ticker),
                        "date": when.date().isoformat(),
                        "move": float(move),
                        "implied_ratio": round(1.0 / (1.0 + float(move)), 4)})
    return sorted(out, key=lambda c: (c["ticker"], c["date"]))


def apply_split(panel: pd.DataFrame, ticker: str, ex_date: str,
                ratio: float) -> pd.DataFrame:
    """Re-base `ticker`'s prices BEFORE ex_date onto the post-split basis. PURE.

    Rows strictly BEFORE ex_date are multiplied by `ratio` (0.5 for a 2:1), so
    the split-day return becomes the real trading move instead of -50%. Rows on
    and after ex_date are already post-split and must not be touched.

    Returns a COPY — never mutates the caller's frame, so a failure partway
    through a multi-panel adjustment cannot leave one panel adjusted and another
    not.
    """
    if panel is None or panel.empty or ticker not in panel.columns:
        return panel
    out = panel.copy()
    cut = pd.Timestamp(ex_date)
    mask = out.index < cut
    if not mask.any():
        return out                              # no history before the split
    out.loc[mask, ticker] = out.loc[mask, ticker] * float(ratio)
    return out


def _load_applied() -> dict:
    try:
        return json.loads(APPLIED.read_text())
    except Exception:                           # noqa: BLE001 — absent or torn
        return {}


def already_applied(applied: dict, ticker: str, ex_date: str) -> bool:
    """Has this exact split already been folded in?

    ⛔ The guard against silently quartering a ten-year panel. There is no way
    to detect a double-applied split from the panel afterwards -- the numbers
    are simply wrong, consistently, with no discontinuity to find.
    """
    return str(ex_date) in (applied.get(str(ticker)) or [])


def _record(applied: dict, ticker: str, ex_date: str) -> dict:
    out = {k: list(v) for k, v in applied.items()}
    out.setdefault(str(ticker), [])
    if str(ex_date) not in out[str(ticker)]:
        out[str(ticker)].append(str(ex_date))
        out[str(ticker)].sort()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="report, write nothing")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        return

    closes_path = OUT_DIR / PANELS["close"]
    if not closes_path.exists():
        sys.exit(f"no panel at {closes_path} — run scripts/fetch_prices.py first")

    closes = pd.read_parquet(closes_path)
    applied = _load_applied()
    cands = [c for c in split_candidates(closes)
             if not already_applied(applied, c["ticker"], c["date"])]
    print(f"panel {closes.shape[0]}x{closes.shape[1]} | "
          f"{len(cands)} unadjusted split-shaped move(s)")
    if not cands:
        return

    from adapters.moomoo import prices as mmp       # noqa: PLC0415
    from adapters.moomoo.client import quote_ctx    # noqa: PLC0415

    ctx = quote_ctx()
    confirmed = []
    try:
        for c in cands:
            hist = mmp.splits(c["ticker"], ctx=ctx)
            hit = next((h for h in hist if h["ex_date"] == c["date"]), None)
            if not hit:
                # No positive evidence. Could be a real crash, could be an
                # unreachable gateway -- we cannot tell, so we do nothing.
                print(f"  {c['ticker']:6} {c['date']} {c['move']:+7.1%} "
                      f"-> NOT a confirmed split, left alone")
                continue
            confirmed.append({**c, "ratio": hit["ratio"]})
            print(f"  {c['ticker']:6} {c['date']} {c['move']:+7.1%} "
                  f"-> CONFIRMED split ratio {hit['ratio']:.4f}")
    finally:
        try:
            ctx.close()
        except Exception:                          # noqa: BLE001
            pass

    if not confirmed or args.dry:
        print(f"{len(confirmed)} confirmed; {'DRY RUN — nothing written' if args.dry else 'nothing to write'}")
        return

    # LEDGER FIRST, PANELS SECOND. A crash between the two leaves a split
    # recorded-but-unapplied: visible as a stale -50% return, and fixable by
    # hand. The reverse order risks applying twice on the next run, which
    # quarters the history with nothing to detect it by.
    for c in confirmed:
        applied = _record(applied, c["ticker"], c["date"])
    APPLIED.parent.mkdir(parents=True, exist_ok=True)
    APPLIED.write_text(json.dumps(applied, indent=2, sort_keys=True))

    for field, name in PANELS.items():
        path = OUT_DIR / name
        if not path.exists():
            continue
        panel = pd.read_parquet(path)
        for c in confirmed:
            panel = apply_split(panel, c["ticker"], c["date"], c["ratio"])
        panel.to_parquet(path)
    print(f"adjusted {len(confirmed)} split(s) across {len(PANELS)} panels; "
          f"ledger -> {APPLIED}")


def _selftest() -> None:
    idx = pd.date_range("2026-08-01", periods=6, freq="D")

    # a 2:1 split: price halves and the next session is quiet
    sp = pd.DataFrame({"MNST": [100.0, 101.0, 102.0, 51.0, 51.5, 52.0]}, index=idx)
    c = split_candidates(sp)
    assert len(c) == 1, c
    assert c[0]["ticker"] == "MNST" and c[0]["date"] == "2026-08-04", c
    assert abs(c[0]["implied_ratio"] - 2.0) < 0.05, c

    # a real CRASH that keeps falling is NOT a candidate -- the next session
    # moves, a split's does not
    cr = pd.DataFrame({"XYZ": [100.0, 101.0, 102.0, 55.0, 40.0, 38.0]}, index=idx)
    assert split_candidates(cr) == [], split_candidates(cr)
    # ...and one that BOUNCES is not either
    bo = pd.DataFrame({"XYZ": [100.0, 101.0, 102.0, 55.0, 75.0, 74.0]}, index=idx)
    assert split_candidates(bo) == []

    # an ordinary bad day is far below the threshold
    ok = pd.DataFrame({"XYZ": [100.0, 101.0, 102.0, 88.0, 88.5, 89.0]}, index=idx)
    assert split_candidates(ok) == []

    # applying it re-bases ONLY the pre-split rows
    adj = apply_split(sp, "MNST", "2026-08-04", 0.5)
    assert list(adj["MNST"][:3]) == [50.0, 50.5, 51.0], list(adj["MNST"])
    assert list(adj["MNST"][3:]) == [51.0, 51.5, 52.0], list(adj["MNST"])
    # ...and the -50% return is gone
    assert abs(adj["MNST"].pct_change().iloc[3]) < 0.02, adj["MNST"].pct_change()
    assert split_candidates(adj) == [], "an adjusted panel has no candidates left"

    # PURE: the caller's frame is untouched
    assert sp["MNST"].iloc[0] == 100.0, "apply_split mutated its input"

    # unknown ticker / no prior history are no-ops, not errors
    assert apply_split(sp, "NOPE", "2026-08-04", 0.5)["MNST"].iloc[0] == 100.0
    assert apply_split(sp, "MNST", "2026-08-01", 0.5)["MNST"].iloc[0] == 100.0

    # ⛔ DOUBLE APPLICATION quarters the panel, silently and undetectably --
    # this ledger is the only thing preventing it
    assert already_applied({"MNST": ["2026-08-04"]}, "MNST", "2026-08-04") is True
    assert already_applied({"MNST": ["2026-08-04"]}, "MNST", "2026-08-05") is False
    assert already_applied({}, "MNST", "2026-08-04") is False
    rec = _record({}, "MNST", "2026-08-04")
    assert already_applied(rec, "MNST", "2026-08-04") is True
    assert _record(rec, "MNST", "2026-08-04") == rec, "recording twice must be a no-op"
    # and the proof that it matters: applying twice really does quarter it
    twice = apply_split(adj, "MNST", "2026-08-04", 0.5)
    assert twice["MNST"].iloc[0] == 25.0, twice["MNST"].iloc[0]

    # empty / absent inputs
    assert split_candidates(pd.DataFrame()) == []
    assert split_candidates(None) == []
    print("adjust_splits: OK — splits detected, crashes ignored, pre-split rows "
          "re-based, double application blocked by the ledger")


if __name__ == "__main__":
    main()
