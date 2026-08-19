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
    out = {"peak_pct": None, "giveback_pct": None}
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
    # ⛔ gain_protected_pct REMOVED 2026-08-19. It was (stop - cost) / (mark -
    # cost): the denominator goes to zero as a position sits near its entry, so
    # the ratio explodes and dominates any cross-position comparison. On
    # 2026-08-18 a session was told to trim "the worst gain-protection" and the
    # metric ranked TER ~9x worse than AMD when AMD was worse by every quantity
    # describing real money -- five times the loss against cost and more than
    # twice the decline from the mark. It nearly took the wrong position.
    #
    # Not repaired with an epsilon, cap or floor: those hide the singularity
    # behind an arbitrary threshold. Replacing the denominator with cost just
    # reproduces trade_pnl_at_stop_pct_cost, which already exists.
    #
    # Its one useful signal -- "shows a profit, would close at a loss" -- is now
    # the BOOLEAN profitable_now_but_loss_at_stop in state.holdings(), computed
    # from the EFFECTIVE stop. A boolean cannot invert a comparison. Magnitude
    # comes from trade_pnl_at_stop_* and mark_to_stop_*.
    #
    # This file returns path facts only.
    return out


def entry_date(events, symbol: str):
    """Date of the first buy in the CURRENT (still-open) holding period.

    Walks this symbol's filled fills in chronological order, accumulating
    SIGNED QUANTITY: buy +qty, sell -qty. A lot is CLOSED only when the
    running quantity returns to (approximately) zero -- a partial sell/trim
    does NOT close it and does NOT move the entry date, because it never
    actually exited the position. The entry date is the date of the first
    buy after the most recent close; if the position has never closed, it is
    the first buy overall.

    Returns None only when there is no current lot to report:
      - nothing was ever bought,
      - everything bought has since been sold back out and nothing was
        bought after, or
      - the only sells seen have no buy to attribute them to -- these are
        SKIPPED, not fatal, so a stray/duplicate sell from stale or
        incomplete history does not poison a later, unambiguous buy (a real
        case in the live journal: an old lot's fills don't fully net to zero,
        because the journal doesn't reach back far enough to explain them,
        but a clean new buy opens weeks later with nothing after it -- that
        current lot is perfectly readable and must not be nulled by history
        that predates it).
    Also returns None when a fill's quantity cannot be derived at all (no
    `quantity`, and no `amount`/`avg_price` pair to compute one from) --
    that is the one case where the data itself, not just its net effect, is
    unusable.

    A confident wrong peak is worse than an absent one -- but so is nulling a
    position whose history is perfectly clear, which is the defect this
    replaced: the old rule nulled on ANY sell after a buy, so a partial trim
    (common -- rebalancing to a target weight) looked identical to a genuine
    exit-and-reopen.
    """
    fills = []
    for e in events or []:
        if e.get("event") != "execution":
            continue
        day = str(e.get("ts") or "")[:10]
        for f in e.get("fills") or []:
            if f.get("symbol") != symbol or f.get("status") != "filled":
                continue
            side = f.get("side")
            if side not in ("buy", "sell"):
                continue                       # not a side this tracks; ignore, don't guess
            qty = f.get("quantity")
            try:
                qty = float(qty)
            except (TypeError, ValueError):
                # Older fills carry no share `quantity`, only a dollar
                # `amount` and an `avg_price` -- derive qty from those. This
                # is an APPROXIMATION (rounding survives the round trip
                # through amount/avg_price), which is exactly why closure
                # below is judged with a tolerance, not equality to zero.
                try:
                    qty = float(f.get("amount")) / float(f.get("avg_price"))
                except (TypeError, ValueError, ZeroDivisionError):
                    return None                # can't derive a quantity -- can't interpret this
            fills.append((day, side, qty))

    running = 0.0
    peak = 0.0          # largest running quantity this lot has reached
    entry = None
    for day, side, qty in fills:
        if side == "buy":
            if entry is None:
                entry = day                    # first buy of a fresh (or reopened) lot
                running = 0.0
                peak = 0.0
            running += qty
            peak = max(peak, running)
        else:                                  # sell
            if entry is None:
                # No open lot to attribute this sell to -- either nothing has
                # been bought yet, or a prior lot already closed. SKIP rather
                # than abort: this fill alone doesn't make the REST of the
                # history uninterpretable, and a later buy still starts an
                # unambiguous new lot (the live STX case: an old lot's fills
                # don't net cleanly, but a clean new buy follows weeks later
                # with nothing after it).
                continue
            running -= qty
            # Tolerance is a FRACTION of the largest position size this lot
            # reached, not an absolute share count -- pure arithmetic hygiene
            # for the amount/avg_price approximation above (float
            # accumulation, not exact), never a trading threshold.
            tol = max(peak, qty, 1e-9) * 1e-6
            if running <= tol:
                running = 0.0
                entry = None                   # lot closed; no current holding period yet
    return entry


def _selftest() -> None:
    # ran to 60, now 50, cost 40, stop 44
    f = facts(cost=40.0, mark=50.0, stop=44.0, highs=[42.0, 60.0, 50.0])
    assert round(f["peak_pct"], 4) == 0.5, f            # 60/40 - 1
    assert round(f["giveback_pct"], 4) == 0.25, f       # 0.50 - 0.25
    # ⛔ THE RATIO IS GONE AND MUST NOT COME BACK. It is not merely absent: a
    # future edit that reintroduces it would restore a field that ranked two
    # positions backwards on live money.
    assert "gain_protected_pct" not in f, f
    for probe in (facts(100.0, 110.0, 95.0, [112.0]),
                  facts(100.0, 100.0, 90.0, [100.0]),
                  facts(100.0, 90.0, 80.0, [110.0])):
        assert "gain_protected_pct" not in probe, probe
    # this file returns PATH facts only
    assert set(f) == {"peak_pct", "giveback_pct"}, f

    # no price history -> null, never a fabricated peak
    f = facts(cost=100.0, mark=110.0, stop=95.0, highs=[])
    assert f["peak_pct"] is None and f["giveback_pct"] is None, f

    # entry date = earliest still-open buy (two buys, still-open lot)
    def _fill(day, side, symbol="AAA", **kw):
        f = {"symbol": symbol, "side": side, "status": "filled", **kw}
        return {"event": "execution", "ts": f"{day}T14:00:00+00:00", "fills": [f]}

    ev = [_fill("2026-08-03", "buy", quantity=1.0),
          _fill("2026-08-07", "buy", quantity=1.0)]
    assert entry_date(ev, "AAA") == "2026-08-03", entry_date(ev, "AAA")
    assert entry_date(ev, "ZZZ") is None

    # GENUINE FULL EXIT then RE-ENTRY: sell zeroes the running quantity, so
    # the lot that opened on day1 is closed. The later buy starts a NEW lot --
    # entry must be the LATER buy, not the original one.
    ev_reentry = [_fill("2026-08-03", "buy", quantity=1.0),
                  _fill("2026-08-05", "sell", quantity=1.0),
                  _fill("2026-08-07", "buy", quantity=1.0)]
    assert entry_date(ev_reentry, "AAA") == "2026-08-07", entry_date(ev_reentry, "AAA")

    # PARTIAL TRIM between buys (the live TER shape): sell does not zero the
    # running quantity, so the lot never closed. Entry stays the ORIGINAL buy.
    ev_trim = [_fill("2026-08-07", "buy", symbol="TER", quantity=0.01283),
               _fill("2026-08-12", "sell", symbol="TER", quantity=0.006414),  # trim, not a close
               _fill("2026-08-13", "buy", symbol="TER", quantity=0.005)]     # rebalance back up
    assert entry_date(ev_trim, "TER") == "2026-08-07", entry_date(ev_trim, "TER")
    assert entry_date(ev_trim, "ZZZ") is None, "no fills for this symbol"

    # TRAILING sell that does NOT zero the position (the live SNDK shape:
    # buy, buy, sell, nothing after). Must NOT be None -- the position is
    # still open and its entry is the earliest buy of the still-open lot.
    ev_trail = [_fill("2026-08-01", "buy", symbol="SNDK", quantity=1.0),
                _fill("2026-08-10", "buy", symbol="SNDK", quantity=0.5),
                _fill("2026-08-14", "sell", symbol="SNDK", quantity=0.2)]
    assert entry_date(ev_trail, "SNDK") == "2026-08-01", entry_date(ev_trail, "SNDK")

    # TRAILING sell that DOES zero the position, with no buy after: there is
    # no current lot, so there is no current entry date. Documented as None.
    ev_closed = [_fill("2026-08-01", "buy", symbol="XYZ", quantity=1.0),
                 _fill("2026-08-14", "sell", symbol="XYZ", quantity=1.0)]
    assert entry_date(ev_closed, "XYZ") is None, "fully closed, no re-buy -> no current lot"

    # amount/avg_price fills (no `quantity`) -- older journal shape. A partial
    # sell derived this way must not close (or null) the lot.
    ev_amt = [_fill("2026-08-01", "buy", symbol="OLD", amount=1000.0, avg_price=100.0),
              _fill("2026-08-10", "sell", symbol="OLD", amount=500.0, avg_price=100.0)]
    assert entry_date(ev_amt, "OLD") == "2026-08-01", entry_date(ev_amt, "OLD")

    # a sell with no preceding buy at all, and NOTHING follows it -- no open
    # lot exists, so there is nothing to report.
    ev_orphan = [_fill("2026-08-01", "sell", symbol="ORP", quantity=1.0)]
    assert entry_date(ev_orphan, "ORP") is None

    # the live STX shape: messy old fills that never quite net to zero (an
    # orphan sell with no matching buy in this history, most likely because
    # the journal doesn't reach back far enough to explain it), followed by a
    # clean new buy with nothing after it. The stray sell must be skipped,
    # not treated as poisoning everything downstream -- the current lot is
    # unambiguous and must resolve to the later, clean buy.
    ev_stx = [_fill("2026-07-13", "buy", symbol="STX", amount=1.27, avg_price=857.2399),
              _fill("2026-07-16", "sell", symbol="STX", amount=1.6, avg_price=739.96),
              _fill("2026-07-17", "sell", symbol="STX", amount=3.12, avg_price=711.57),
              _fill("2026-08-03", "buy", symbol="STX", amount=4.76, avg_price=798.5499)]
    assert entry_date(ev_stx, "STX") == "2026-08-03", entry_date(ev_stx, "STX")

    # a fill whose quantity cannot be derived at all (no quantity, no
    # amount/avg_price) -- genuinely uninterpretable, not a guess.
    ev_bad = [_fill("2026-08-01", "buy", symbol="BAD")]
    assert entry_date(ev_bad, "BAD") is None

    # tolerance is relative to position size, not exact-zero: a sell whose
    # amount/avg_price-derived quantity overshoots the true holding by float
    # noise must still register as a CLOSE, not leave a phantom open lot.
    ev_noise = [_fill("2026-08-01", "buy", symbol="NOI", quantity=1.0),
                _fill("2026-08-10", "sell", symbol="NOI",
                      amount=100.0, avg_price=99.99999999999999)]
    assert entry_date(ev_noise, "NOI") is None, "float-noise-sized sell should close, not linger"

    print("selftest OK: excursion -- peak/giveback path facts only, null rather "
          "than guessed; the unstable gain_protected_pct ratio is gone and "
          "asserted absent; entry_date tracks running quantity so a partial "
          "trim does not null a live holding period")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
