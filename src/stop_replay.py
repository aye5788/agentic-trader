"""Stop-aware single-position replay — pure, no I/O, no clock.

Given a long position's entry, stop, first target, and the daily OHLC bars that
followed, decide how it would have exited and return realized-R (P&L in units of
the entry-to-stop risk). Used by the adaptive stop-multiple learner to turn price
history into pseudo-outcomes. Conservative same-bar rule: if a bar's low touches
the stop AND its high touches the target, assume the STOP hit first (worst case).

Spec: docs/superpowers/specs/2026-07-23-adaptive-input-layer-design.md §6.1
Known simplification (v1): models stop / first-target / time-horizon exits only,
not the 21-day MA exit — replay is a prior, not truth (spec §13 replay-bias).
"""
from __future__ import annotations


def replay_position(entry_price, stop, target, highs, lows, closes, max_days):
    entry_price = float(entry_price)
    stop = float(stop)
    risk = entry_price - stop
    n = min(int(max_days), len(closes))

    def r_of(px):
        return (float(px) - entry_price) / risk if risk else 0.0

    for i in range(n):
        lo = float(lows[i])
        hi = float(highs[i])
        if lo <= stop:                                  # conservative: stop before target
            return {"stopped": True, "hit_target": False,
                    "exit_price": stop, "exit_day": i + 1, "realized_r": r_of(stop)}
        if target is not None and hi >= float(target):
            tgt = float(target)
            return {"stopped": False, "hit_target": True,
                    "exit_price": tgt, "exit_day": i + 1, "realized_r": r_of(tgt)}
    exit_px = float(closes[n - 1]) if n > 0 else entry_price
    return {"stopped": False, "hit_target": False,
            "exit_price": exit_px, "exit_day": n, "realized_r": r_of(exit_px)}


def _selftest() -> None:
    # 1. Stop hit on day 2 (low 94 <= stop 95) -> realized_r == -1 exactly.
    r = replay_position(100.0, 95.0, 120.0,
                        highs=[102, 101, 130], lows=[98, 94, 90], closes=[101, 96, 100],
                        max_days=10)
    assert r["stopped"] is True and r["hit_target"] is False, r
    assert r["exit_price"] == 95.0 and r["exit_day"] == 2, r
    assert r["realized_r"] == -1.0, r          # (95-100)/(100-95)

    # 2. Target hit on day 3 (high 121 >= target 120), stop never touched.
    r = replay_position(100.0, 95.0, 120.0,
                        highs=[102, 108, 121], lows=[98, 101, 110], closes=[101, 107, 119],
                        max_days=10)
    assert r["hit_target"] is True and r["stopped"] is False, r
    assert r["exit_price"] == 120.0 and r["exit_day"] == 3, r
    assert r["realized_r"] == 4.0, r           # (120-100)/5

    # 3. Neither: horizon exit at last close.
    r = replay_position(100.0, 95.0, 120.0,
                        highs=[102, 103, 104], lows=[98, 99, 100], closes=[101, 102, 103],
                        max_days=3)
    assert r["stopped"] is False and r["hit_target"] is False, r
    assert r["exit_price"] == 103.0 and r["exit_day"] == 3, r
    assert r["realized_r"] == 0.6, r           # (103-100)/5

    # 4. Same bar touches both -> conservative STOP first.
    r = replay_position(100.0, 95.0, 120.0,
                        highs=[125], lows=[94], closes=[100], max_days=5)
    assert r["stopped"] is True and r["hit_target"] is False, r

    # 5. max_days shorter than the path caps the hold.
    r = replay_position(100.0, 95.0, 200.0,
                        highs=[101, 102, 103, 104], lows=[99, 98, 97, 96],
                        closes=[100, 101, 102, 103], max_days=2)
    assert r["exit_day"] == 2 and r["exit_price"] == 101.0, r

    print("selftest OK: replay_position stop/target/horizon/same-bar/max_days")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
