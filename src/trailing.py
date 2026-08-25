"""Peak-anchored trailing stop — the exit that bounds GIVEBACK. Pure, no I/O.

⛔ WHY THIS EXISTS. Until 2026-08-25 this book had three exits and a hole:

    stop      bounds loss from ENTRY   enforced every poll by market_monitor
    target    captures a large move    5.5 sigma; first-hit 10.7% within 10 days
    rotation  exits on RANK decay      weekly; ~54% of all exits
    ---       bounds loss from PEAK    DID NOT EXIST

Nothing watched a winner giving itself back. The measured cost, from the live
book on the day this was written (peak return -> current return):

    INTC  +24.9% -> -0.4%    gave back 101.7% of its peak
    STX   +26.6% -> +2.0%    gave back  92.5%
    AMD   +13.0% -> +2.0%    gave back  84.5%
    DELL  +28.2% -> +11.3%   gave back  60.0%
    SNDK  +52.7% -> +22.7%   gave back  57.0%

INTC round-tripped an entire +24.9% gain to nothing and NO mechanism could fire:
its rank kept it in the book so rotation never touched it, its target sat 5.4
sigma away, and its stop — measured from ENTRY and never moved — only triggers
after the whole gain is already gone. That is not a missed judgement call. There
was no instrument that could have expressed it.

⛔ AND THE KNOWLEDGE WAS ALREADY THERE. prompts/charter.md hands every 15:15
session `peak_pct` and `giveback_pct` and says outright: "A stop that has not
moved since entry is not neutral -- it silently converts a winner into a loser."
Then, like every other fact in that document, it adds "these are facts, not
instructions". So the system measured the problem, named it, showed it to the
agent before every close, and gave it nothing that could act. Reading a number
and having a mechanism are different things; this is the mechanism.

⛔ WHY A RATCHET IS NOT "THE MONITOR INVENTING A MARKET OPINION". That objection
was raised against this design and withdrawn, because it cannot distinguish this
from the fixed stop the monitor ALREADY enforces without argument. Both are a
level the process sells at. The only difference is that this one has a rule for
moving UP. If an automatic stop at entry is legitimate risk control, an automatic
stop that follows the position is the same control applied to the same position
later in its life. The alternative on offer -- the agent tightening by hand via
set_levels -- was available every single session while INTC round-tripped, with
the charter text above in front of it, and it did not happen.

THE RULE, and every number in it is config, never literal here:

    activate when   peak_return >= activation_sigma * daily_sigma
    trail_stop   =  entry * (1 + (1 - giveback_fraction) * peak_return)
    ratchet         trail_stop never decreases, ever
    effective    =  max(thesis stop, agent stop, trail stop)

`effective_stop` taking the MAXIMUM is what makes this un-loosenable: a lower
agent override cannot pull the level back down, and an explicit `widen=True`
(which legitimately widens a STATIC stop) cannot unwind banked profit either.
That asymmetry is deliberate. A stop may be widened on a reasoned view of the
chart; a trail exists precisely to remove the discretion to keep holding a
winner that has started giving itself back.

⛔ THIS MODULE IS PURE AND MUST STAY PURE. It reads no file, no clock and no
quote feed. The monitor owns the durable state and the polling; this owns only
the arithmetic, so the rules can be exercised without a market, a broker or a
running service. The monitor is the stop and sits on a ~0.1s budget -- nothing
here may grow an import, touch parquet, or take a lock.
"""
from __future__ import annotations


def _fin(x) -> bool:
    """Finite and usable as a price/ratio? Pure."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return False
    return f == f and f not in (float("inf"), float("-inf"))


def validate_trail_state(state) -> tuple[bool, str]:
    """Is this stored trail row usable? -> (ok, reason). NEVER raises.

    ⛔ A MALFORMED ROW MUST DEGRADE TO THE ORDINARY STOP, NOT TO NO STOP. The
    caller's contract is: if this returns False, ignore the trail entirely and
    keep enforcing the static stop, then say so out loud. A trail that silently
    evaluates against a corrupt peak would either never fire or fire instantly,
    and both are worse than not having one.
    """
    if not isinstance(state, dict):
        return False, f"trail state is {type(state).__name__}, not an object"
    for key in ("entry_price", "peak_price"):
        if not _fin(state.get(key)):
            return False, f"{key} is missing or not a finite number"
        if float(state[key]) <= 0:
            return False, f"{key} must be positive, got {state[key]}"
    if float(state["peak_price"]) < float(state["entry_price"]):
        # Legal: a position underwater since entry simply has not activated.
        pass
    ts = state.get("trail_stop")
    if ts is not None and (not _fin(ts) or float(ts) <= 0):
        return False, f"trail_stop present but unusable: {ts!r}"
    return True, ""


def update_peak(state: dict, observed_high) -> dict:
    """Advance the stored peak. Pure — returns a NEW dict, never mutates.

    ⛔ THE PEAK ONLY EVER RISES. It is the high-water mark of the position's
    life, not of the current session: a peak that reset daily would let a winner
    give everything back over a week without one poll seeing a breach.

    `observed_high` should be max(quote.high, quote.last) at the call site. The
    monitor polls every ~15s and a high printed between two polls is invisible to
    `last` alone -- so a trail fed only on `last` measures a peak the position
    never actually gave back from, and under-protects exactly the fast movers
    this is for. A non-finite observation is IGNORED rather than allowed to
    corrupt the high-water mark.
    """
    out = dict(state or {})
    if not _fin(observed_high) or float(observed_high) <= 0:
        return out
    prior = out.get("peak_price")
    out["peak_price"] = (float(observed_high) if not _fin(prior)
                         else max(float(prior), float(observed_high)))
    return out


def compute_trail_stop(state: dict, sigma, activation_sigma: float,
                       giveback_fraction: float):
    """The trail level implied by the peak so far -> (level|None, activated).

    None means NOT ACTIVE and the caller must fall through to the static stop.
    Two distinct reasons produce it, and neither is an error:
      - the position has not yet run far enough to activate;
      - sigma is unavailable, so "far enough" has no meaning here.

    ⛔ ACTIVATION IS SCALED IN THE NAME'S OWN VOLATILITY, NOT IN PERCENT. A 6%
    run means something entirely different in KO (sigma 1.18%/day) than in SNDK
    (7.38%/day) -- a flat percentage trigger would trail a defensive name on
    ordinary noise while never activating on the volatile one that actually
    needs it. The same reasoning already governs the static stop.

    ⛔ IT ALSO MUST NOT ACTIVATE AT BREAKEVEN. With the documented defaults the
    level locks (1 - giveback_fraction) of a run that is by construction at least
    `activation_sigma` sigmas -- so activation banks a real gain rather than
    converting the position into a free option that any pullback closes.
    """
    if not _fin(sigma) or float(sigma) <= 0:
        return None, False
    entry, peak = state.get("entry_price"), state.get("peak_price")
    if not (_fin(entry) and _fin(peak)) or float(entry) <= 0:
        return None, False
    peak_return = float(peak) / float(entry) - 1.0
    if peak_return < float(activation_sigma) * float(sigma):
        return None, False
    level = float(entry) * (1.0 + (1.0 - float(giveback_fraction)) * peak_return)
    # THE RATCHET. Monotone by construction, and re-asserted here so a shrunken
    # peak (a corrupted or hand-edited state file) can never walk the level back
    # down. A trail that can fall is not a trail.
    prior = state.get("trail_stop")
    if _fin(prior):
        level = max(level, float(prior))
    return level, True


def effective_stop(base_stop, agent_stop, trail_stop):
    """The level actually enforced = the STRICTEST of the three. -> (level|None, source).

    ⛔ MAX, NOT "MOST RECENT" AND NOT "MOST SPECIFIC". Whichever sits highest
    protects the most, and profit protection must not be reversible by a later,
    looser instruction. src/levels.py already resolves thesis-vs-agent for the
    STATIC pair (and refuses a loosening that is not an explicit reasoned widen);
    this composes that resolved static level with the dynamic floor. An explicit
    widen can still widen the static stop -- it cannot unwind the trail, because
    the trail is not a view about the chart, it is banked gain.
    """
    best, source = None, None
    for level, name in ((base_stop, "thesis"), (agent_stop, "agent"),
                        (trail_stop, "trail")):
        if not _fin(level) or float(level) <= 0:
            continue
        if best is None or float(level) > best:
            best, source = float(level), name
    return best, source


def trail_trigger(symbol: str, price, state: dict, sigma, cfg: dict,
                  base_stop=None, agent_stop=None):
    """Would the trail sell `symbol` at `price` right now? -> dict or None. Pure.

    Returns None when there is nothing to say. A returned dict is a DESCRIPTION
    of a breach, never an order: the monitor decides whether to act on it, and
    while `[monitor.trail].enabled` is false it journals the description and
    does nothing -- which is the shadow mode this ships in.

    ⛔ IT REPORTS ONLY BREACHES THE TRAIL ITSELF CAUSED. If the static stop is at
    or above the trail then the ordinary stop path already covers this name, and
    emitting a second trigger for the same event would double-fire the exit
    executor against one position -- the 2026-08-19 failure shape. `source ==
    "trail"` is the whole gate.

    The exit is ALWAYS the full position (`fraction: 1.0`). A trail says the run
    is over, which is not a scale-out; it also keeps this path clear of the
    fractional sub-$1 minimum that makes a half-trim unplaceable on a small
    position and leaves the monitor re-firing at something it cannot reduce.
    """
    ok, why = validate_trail_state(state)
    if not ok:
        return {"symbol": symbol, "reason": "trail_state_invalid", "detail": why}
    level, activated = compute_trail_stop(
        state, sigma,
        float(cfg.get("activation_sigma", 2.5)),
        float(cfg.get("giveback_fraction", 0.35)))
    if not activated or not _fin(price):
        return None
    eff, source = effective_stop(base_stop, agent_stop, level)
    if source != "trail":
        return None                     # the static stop already governs this name
    if float(price) > eff:
        return None
    entry = float(state["entry_price"])
    return {"symbol": symbol, "reason": "trail", "fraction": 1.0,
            "price": float(price), "level": eff,
            "peak_price": float(state["peak_price"]),
            "entry_price": entry,
            "locked_gain_pct": round((eff / entry - 1.0) * 100.0, 4),
            "peak_gain_pct": round((float(state["peak_price"]) / entry - 1.0) * 100.0, 4)}
