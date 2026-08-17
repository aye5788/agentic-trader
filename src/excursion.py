"""How far a position ran, and how much of that run is actually protected.

positions() showed qty/cost/mark/stop and nothing about the PATH. So the agent
could not see what a human reads off the same book in seconds: that a position
up 48% would keep only a fraction of that gain if its stop fired, or that a
position showing a profit would close red because the stop never followed the
price up.

No threshold lives here. These are facts the agent reads; what to do about them
is its decision.
"""
from __future__ import annotations


def facts(cost: float, mark: float, stop, highs) -> dict:
    """Excursion facts for one position. Pure. None where undefined."""
    out = {"peak_pct": None, "giveback_pct": None, "gain_protected_pct": None}
    try:
        cost = float(cost); mark = float(mark)
    except (TypeError, ValueError):
        return out
    if cost <= 0:
        return out
    prices = [float(h) for h in (highs or []) if isinstance(h, (int, float))]
    if prices:
        peak = max(max(prices), mark)          # today's mark can exceed the panel
        out["peak_pct"] = peak / cost - 1.0
        out["giveback_pct"] = out["peak_pct"] - (mark / cost - 1.0)
    gain = mark - cost
    if stop is not None and gain > 0:
        # NEGATIVE when the stop sits below cost: the position shows a profit
        # and would close at a loss. Stated, not implied.
        out["gain_protected_pct"] = (float(stop) - cost) / gain
    return out


def entry_date(events, symbol: str):
    """Date of the earliest buy in the CURRENT holding period, or None.

    A sell between buys means the earliest buy belongs to a closed lot, so the
    entry is ambiguous -- return None. A confident wrong peak is worse than an
    absent one.
    """
    first_buy = None
    for e in events or []:
        if e.get("event") != "execution":
            continue
        day = str(e.get("ts") or "")[:10]
        for f in e.get("fills") or []:
            if f.get("symbol") != symbol or f.get("status") != "filled":
                continue
            if f.get("side") == "buy" and first_buy is None:
                first_buy = day
            elif f.get("side") == "sell" and first_buy is not None:
                return None                    # ambiguous: lot closed and reopened
    return first_buy


def _selftest() -> None:
    # ran to 60, now 50, cost 40, stop 44
    f = facts(cost=40.0, mark=50.0, stop=44.0, highs=[42.0, 60.0, 50.0])
    assert round(f["peak_pct"], 4) == 0.5, f            # 60/40 - 1
    assert round(f["giveback_pct"], 4) == 0.25, f       # 0.50 - 0.25
    assert round(f["gain_protected_pct"], 4) == 0.4, f  # (44-40)/(50-40)

    # STOP BELOW COST: showing a profit, would close at a LOSS. This is the
    # AMD/TER case and it must read as negative, not as zero or absent.
    f = facts(cost=100.0, mark=110.0, stop=95.0, highs=[112.0])
    assert f["gain_protected_pct"] < 0, f
    assert round(f["gain_protected_pct"], 4) == -0.5, f  # (95-100)/(110-100)

    # no gain -> protected share is undefined, never a divide-by-zero
    f = facts(cost=100.0, mark=100.0, stop=90.0, highs=[100.0])
    assert f["gain_protected_pct"] is None, f

    # no price history -> null, never a fabricated peak
    f = facts(cost=100.0, mark=110.0, stop=95.0, highs=[])
    assert f["peak_pct"] is None and f["giveback_pct"] is None, f

    # entry date = earliest still-open buy
    ev = [{"event": "execution", "ts": "2026-08-03T14:00:00+00:00",
           "fills": [{"symbol": "AAA", "side": "buy", "status": "filled"}]},
          {"event": "execution", "ts": "2026-08-07T14:00:00+00:00",
           "fills": [{"symbol": "AAA", "side": "buy", "status": "filled"}]}]
    assert entry_date(ev, "AAA") == "2026-08-03", entry_date(ev, "AAA")
    assert entry_date(ev, "ZZZ") is None

    # AMBIGUOUS: a full exit between buys means the earliest buy is NOT this
    # lot's entry. Report nothing rather than a confident wrong peak.
    ev2 = ev[:1] + [{"event": "execution", "ts": "2026-08-05T14:00:00+00:00",
                     "fills": [{"symbol": "AAA", "side": "sell", "status": "filled"}]}] + ev[1:]
    assert entry_date(ev2, "AAA") is None, "a sell between buys makes entry ambiguous"

    print("selftest OK: excursion -- peak/giveback/protected-gain, negative when "
          "the stop sits below cost, null rather than guessed")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
