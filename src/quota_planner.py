"""WHICH names get a metered history request this run, and which wait.

⛔ THE CONSTRAINT IS A BROKER FACT. moomoo meters `request_history_kline` at
**100 DISTINCT stocks per rolling window, account-wide**. It was hit for real on
2026-07-29 at exactly `stock: 100/100` with 98 of 168 universe names still
unfetched. Screening (`get_stock_screen`, `get_stock_filter`,
`get_stock_basicinfo`, `get_market_snapshot`) is entirely UNMETERED against that
cap, which is why discovery can rank the whole market weekly for zero quota
while giving 200 names a 252-session window cannot be done in one run at all.

⛔ SO THE COHORT CAN BE BROAD AND THE SCOREABLE SET CANNOT GROW AS FAST. That is
not a defect to engineer around; it is the shape of the system, and pretending
otherwise is how you get a "candidate" the signal has no number for. A name is a
research lead until the panel can score it (see src/cohort.py).

# --------------------------------------------------------------------------- #
# THE SOURCE OF TRUTH FOR REMAINING CAPACITY, in strict order of preference
# --------------------------------------------------------------------------- #

  1. BROKER TELEMETRY  `get_history_kl_quota()` -> (used, remain, detail_list),
     wrapped as `adapters.moomoo.prices.history_quota()`. `remain` IS the
     remaining distinct-symbol capacity, straight from the server, and
     `detail_list` names the symbols currently counted so "which repeats are
     free" is the server's answer rather than our inference. UNMETERED. This is
     the source of truth whenever it answers.

  2. OPERATOR-CONFIRMED BOOTSTRAP/RESET  `research_store/universe/
     history_quota_reset.json`, written by a human who has established that the
     broker window was empty at a stated moment. Requests recorded before that
     moment stop counting; requests after it count normally.

  3. CONFIRMED BROKER WINDOW  `[history_acquisition] rolling_window_days`, set
     by an operator who has DOCUMENTED the real duration. Unset by default,
     because it is not known.

  4. UNKNOWN -> ZERO NEW CAPACITY. If none of the above answers, the honest
     remaining capacity is none. Already-charged symbols may still be retried
     (a repeat costs no distinct-symbol unit); no new distinct symbol may be.

⛔ WHAT WAS WRONG BEFORE, AND WHY IT IS NOT A SMALL POINT (2026-09-09, second
audit). This module used `repeat_credit_days = 7` as the effective CAPACITY
window: a request older than 7 days dropped out of `charged`, which made
`remaining_new_distinct` RISE, which let the planner schedule new distinct
symbols on day 8 — while moomoo may still have been counting the originals. The
window's true length is undocumented; 7 was a number we chose.

And the docstring claimed the opposite of the truth. It said a shorter assumed
window is conservative because it "only schedules fewer names". That is false
under capacity subtraction: a SHORTER window means FEWER symbols counted as
charged, which means MORE remaining capacity, which schedules MORE new names.
The claim has been removed from this module, `config/strategy.toml` and the
docs. An assumed duration cannot make capacity safe in either direction — only
asking the broker, or a human confirming a reset, can.

WHAT THIS MODULE GUARANTEES
  1. New distinct symbols never exceed the remaining capacity established by
     the source of truth above — never by an expiry timer.
  2. An ABSOLUTE per-run request ceiling, enforced independently, so a wrong or
     malformed state cannot authorise an unbounded number of calls.
  3. A deterministic order, so two runs on the same inputs plan the same thing
     and a deferral is reproducible rather than incidental.
  4. Every deferred name carries a REASON.
  5. Exhaustion defers; it never truncates, replaces or corrupts the cohort.
  6. Symbols are recorded as charged only AFTER a request was actually
     attempted, and every attempted symbol is recorded including failures — the
     meter charges a SYMBOL, not a successful response.

⚠️ SCOPE OF "ACCOUNT-WIDE", STATED ACCURATELY. The meter covers the whole
brokerage account. Telemetry (source 1) genuinely reports that account-wide
state, so under telemetry the accounting is account-wide correct. The local
ledger (sources 2-3) records only THIS repository's requests, so it is a lower
bound; per the operator the former sibling `moomoo-vol-desk` process is
inactive and this repository is the primary consumer, and
`~/moomoo-data-collector` is not a material consumer unless it starts calling
`request_history_kline`. Do not describe the fallback path as account-wide safe.

Pure: no I/O and no clock in the planning functions (`today` is injected). The
ledger/reset helpers at the bottom are the only file access and are kept
separate; telemetry is fetched by the caller and passed in.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

# The broker's meter. Overridable from [history_acquisition] in
# config/strategy.toml; this is the fallback and the documented figure.
DEFAULT_QUOTA = 100

# ⛔ THERE IS NO DEFAULT WINDOW DURATION, AND THAT IS DELIBERATE. moomoo does not
# document how long the rolling window is, and this repo's own readings only
# bracket it (100/100 on 2026-07-29, 5/100 on 2026-09-06). `None` means UNKNOWN:
# nothing expires out of the ledger, so no capacity is ever reclaimed by the
# passage of time. An operator who has genuinely established the duration sets
# `[history_acquisition] rolling_window_days` — an accurately named setting, not
# a "credit" timer that quietly doubled as a capacity model.
DEFAULT_ROLLING_WINDOW_DAYS = None

LEDGER_FILE = "research_store/universe/history_quota.json"
RESET_FILE = "research_store/universe/history_quota_reset.json"

# Which authority established the capacity figure, for the run log, the tests
# and the operator. Never cosmetic: source is what a reader needs to know before
# trusting a number that authorises spend.
SRC_TELEMETRY = "broker_telemetry"     # the server answered; authoritative
SRC_WINDOW = "configured_window"       # operator-documented rolling_window_days
SRC_RESET = "operator_reset"           # operator-confirmed bootstrap/reset
SRC_LEDGER = "local_ledger"            # local evidence of requests, no expiry
SRC_UNKNOWN = "unknown"                # -> zero new capacity

# Priority tiers, best first. The numbers are ordering only; they are never
# thresholds and nothing compares them against config.
TIER_HELD = 0        # a name this book OWNS
TIER_NEAR = 1        # already admitted and closest to becoming scoreable
TIER_COHORT = 2      # by eligibility rank — the most liquid names first
TIER_OTHER = 3       # anything else (not in the cohort, not held)

_TIER_REASON = {
    TIER_HELD: "held position — the book must be able to rank what it owns",
    TIER_NEAR: "closest to scoreable — fewest missing sessions in the window",
    TIER_COHORT: "cohort rank — the most liquid eligible names first",
    TIER_OTHER: "outside the cohort and not held",
}


def priority_key(ticker: str, *, held: set, missing_rows: dict,
                 cohort_ranks: dict) -> tuple:
    """The deterministic sort key for one candidate. Pure, total, tie-free.

    ⛔ IT ENDS IN THE TICKER, AND THAT IS LOAD-BEARING. Without a total order,
    two names with identical evidence sort by whatever `set` iteration happened
    to produce, so the same inputs defer a DIFFERENT name on the next run and
    neither ever gets repaired — a starvation bug that looks like flapping. The
    previous implementation took `repair[:quota]` off a `sorted(set(...))`,
    which was stable but expressed no priority at all: it was alphabetical, so
    a held position could be deferred behind three names beginning with 'A'.

    Order of the tiers, and why each earns its place:
      HELD    a position whose momentum cannot be computed is a position the
              book cannot reason about, rotate out of, or rank against its
              peers. This is the only tier justified by RISK rather than by
              opportunity, so it comes first.
      NEAR    a name needing few sessions converts one quota unit into a
              scoreable candidate now; a name needing the full window may still
              fail. Cheapest evidence first.
      COHORT  among the rest, the most liquid eligible names — the portion of
              the cohort the book is most likely to actually be able to trade.
    """
    t = str(ticker).strip().upper()
    if t in (held or set()):
        return (TIER_HELD, 0, 0, t)
    miss = missing_rows.get(t) if missing_rows else None
    rank = cohort_ranks.get(t) if cohort_ranks else None
    if isinstance(miss, int) and not isinstance(miss, bool) and miss >= 0:
        # NEAR is only meaningful for a name we already have a partial column
        # for. A name with nothing at all carries no `missing_rows` entry and
        # falls through to its cohort rank.
        return (TIER_NEAR, miss, rank if isinstance(rank, int) else 10 ** 6, t)
    if isinstance(rank, int) and not isinstance(rank, bool):
        return (TIER_COHORT, rank, 0, t)
    return (TIER_OTHER, 0, 0, t)


def tier_of(ticker: str, *, held: set, missing_rows: dict, cohort_ranks: dict) -> int:
    return priority_key(ticker, held=held, missing_rows=missing_rows,
                        cohort_ranks=cohort_ranks)[0]


def remaining_new_distinct(quota: int, charged, capacity_unknown: bool = False) -> int:
    """How many NEW distinct symbols this window still has room for. Pure.

        remaining = max(0, quota - len(charged_in_window))

    ⛔ `capacity_unknown` COLLAPSES IT TO ZERO. That is the fail-conservative
    branch for a ledger that is PRESENT BUT UNREADABLE: we know requests were
    made and cannot tell which, so the only honest remaining capacity is none.
    An ABSENT ledger is a different state — a genuine first run on this box, and
    nothing has been charged — so it yields full capacity. "Broken" and "empty"
    are not the same value; conflating them is what `src/health.py` exists to
    catch elsewhere in this repo, and here it would silently authorise a full
    100-symbol spend on top of one already made.
    """
    if capacity_unknown:
        return 0
    return max(0, int(quota) - len({str(c).strip().upper() for c in (charged or ())}))


def plan(candidates, *, held=(), missing_rows=None, cohort_ranks=None,
         quota: int = DEFAULT_QUOTA, charged=(), remaining_new=None,
         capacity_unknown: bool = False) -> dict:
    """Split `candidates` into what to request now and what waits. Pure.

    `charged` is the set of symbols already inside moomoo's rolling window: a
    repeat of one costs no distinct-symbol unit, AND its COUNT is what has
    already been spent — see `capacity_state()`, which resolves it from
    the source of truth rather than from an assumed expiry.

    Returns::

        {"request": [...],        # issue history requests for these, in order
         "deferred": [...],       # over budget; retried when capacity frees up
         "new_symbols": [...],    # subset of request that spends the meter
         "repeat_symbols": [...], # subset of request that is free
         "reasons": {T: str},     # why each name is where it is
         "budget": {...}}         # the accounting, for the run log and tests

    ⛔ TWO CEILINGS, AND THEY BOUND DIFFERENT THINGS.

      1. `len(new_symbols) <= remaining_new` — the ROLLING, SHARED,
         ACROSS-RUNS distinct-symbol meter. `remaining_new` DEFAULTS to
         `remaining_new_distinct(quota, charged)`, deliberately: the safe
         computation is what a caller gets by saying nothing, so no caller can
         fall back into the per-run counting that was the original defect.
      2. `len(request) <= quota` — an ABSOLUTE per-run request count, which
         holds even when `charged` is wrong, the ledger is missing, or every
         candidate is a free repeat. Without it a ledger claiming 500 free
         repeats would issue 500 calls in one run.

    A repeat is scheduled without touching ceiling 1, and an UNCHARGED name past
    it is deferred with a reason. It stays deferred across runs until capacity
    frees up — it is never promoted to a new request in the meantime, because
    doing that is exactly the overspend this function exists to prevent.
    """
    quota = max(0, int(quota))
    held = {str(h).strip().upper() for h in (held or ())}
    charged = {str(c).strip().upper() for c in (charged or ())}
    missing_rows = missing_rows or {}
    cohort_ranks = cohort_ranks or {}
    if remaining_new is None:
        remaining_new = remaining_new_distinct(quota, charged, capacity_unknown)
    remaining_new = max(0, int(remaining_new))

    ordered = sorted({str(c).strip().upper() for c in candidates if str(c).strip()},
                     key=lambda t: priority_key(t, held=held,
                                                missing_rows=missing_rows,
                                                cohort_ranks=cohort_ranks))

    request, deferred, new_syms, repeat_syms = [], [], [], []
    reasons = {}
    for t in ordered:
        is_repeat = t in charged
        # Ceiling 2: the absolute per-run request count, independent of `charged`.
        if len(request) >= quota:
            deferred.append(t)
            reasons[t] = (f"deferred: {len(request)} request(s) already planned, "
                          f"at the {quota}-request ceiling for this run")
            continue
        # Ceiling 1: the rolling, shared, across-runs distinct-symbol meter.
        if not is_repeat and len(new_syms) >= remaining_new:
            deferred.append(t)
            reasons[t] = (
                f"deferred: no new distinct-symbol capacity left in this rolling "
                f"window ({len(charged)} symbol(s) already charged against the "
                f"{quota}-symbol quota, {remaining_new} remaining, "
                f"{len(new_syms)} planned). It is NOT retried as a new request "
                f"until capacity frees up."
                if not capacity_unknown else
                f"deferred: history-quota ledger is present but unreadable, so "
                f"remaining capacity is UNKNOWN and treated as zero. Only "
                f"already-charged symbols may be requested until the ledger is "
                f"readable again.")
            continue
        request.append(t)
        (repeat_syms if is_repeat else new_syms).append(t)
        tier = tier_of(t, held=held, missing_rows=missing_rows,
                       cohort_ranks=cohort_ranks)
        reasons[t] = _TIER_REASON[tier] + (
            " (repeat — already inside the rolling window, costs no new quota unit)"
            if is_repeat else "")

    return {
        "request": request,
        "deferred": deferred,
        "new_symbols": new_syms,
        "repeat_symbols": repeat_syms,
        "reasons": reasons,
        "budget": {"quota": quota,
                   "already_charged": len(charged),
                   "remaining_new_distinct": remaining_new,
                   "capacity_unknown": bool(capacity_unknown),
                   "requested": len(request),
                   "new_symbols": len(new_syms),
                   "repeat_symbols": len(repeat_syms),
                   "deferred": len(deferred),
                   "exhausted": bool(deferred)},
    }


# --------------------------------------------------------------------------- #
# the rolling-window ledger (the only I/O here)
# --------------------------------------------------------------------------- #
def load_ledger_state(path) -> dict:
    """-> {"requests": {T: iso}, "present": bool, "readable": bool}.

    ⛔ ABSENT AND UNREADABLE ARE DIFFERENT STATES AND MUST NOT COLLAPSE TO ONE
    EMPTY DICT. That collapse was the original defect's second half: an
    unreadable ledger returned `{}`, which reads as "nothing has been charged",
    which hands back FULL remaining capacity — the most permissive possible
    answer produced by a failure. Absent means this box has never requested
    history and full capacity is correct; present-but-unreadable means requests
    were made and we cannot tell which, and the only honest capacity is zero
    (see `remaining_new_distinct`). Same "broken is not empty" rule
    `src/health.py` enforces for the artifacts whose emptiness costs money.
    """
    p = Path(path)
    if not p.exists():
        return {"requests": {}, "present": False, "readable": True}
    try:
        doc = json.loads(p.read_text())
    except Exception:                                         # noqa: BLE001
        return {"requests": {}, "present": True, "readable": False}
    reqs = doc.get("requests") if isinstance(doc, dict) else None
    if not isinstance(reqs, dict):
        # A file that parses as JSON but carries no `requests` map is
        # structurally wrong, not empty — same conclusion.
        return {"requests": {}, "present": True, "readable": False}
    out = {}
    for k, v in reqs.items():
        k = str(k).strip().upper()
        if k:
            out[k] = str(v)
    return {"requests": out, "present": True, "readable": True}


def load_ledger(path) -> dict:
    """{TICKER: iso-datetime} of past history requests. Convenience wrapper.

    ⚠️ THIS ALONE CANNOT TELL YOU WHETHER CAPACITY IS KNOWN — an unreadable
    ledger and a first-ever run both yield `{}` here. Anything computing a
    BUDGET must use `load_ledger_state()`/`capacity_state()` instead; this is for
    callers that only want to look up timestamps.
    """
    return load_ledger_state(path)["requests"]


def charged_symbols(ledger: dict, today: dt.date,
                    rolling_window_days=None) -> set:
    """Symbols the LOCAL LEDGER says are still counted by the broker. Pure.

    ⛔ WITH `rolling_window_days=None` NOTHING EVER EXPIRES. That is the default
    and it is the whole correction: the previous version dropped entries older
    than an assumed 7 days, which RAISED remaining capacity on day 8 while the
    broker may still have been counting them. A duration nobody has confirmed
    cannot be allowed to hand back spend.

    An operator who has DOCUMENTED the real window passes it here (via
    `[history_acquisition] rolling_window_days`) and entries older than it stop
    counting — which is correct, because then it is knowledge rather than a
    guess.

    An unparseable timestamp COUNTS rather than expiring: we know a request was
    recorded and cannot tell when, and the conservative reading of "when?" is
    "recently enough to still count".
    """
    out = set()
    cutoff = (None if rolling_window_days in (None, 0, "")
              else today - dt.timedelta(days=int(rolling_window_days)))
    for t, when in (ledger or {}).items():
        if cutoff is None:
            out.add(t)
            continue
        try:
            d = dt.date.fromisoformat(str(when)[:10])
        except (ValueError, TypeError):
            out.add(t)          # unknown date -> still counted (conservative)
            continue
        if d >= cutoff:
            out.add(t)
    return out


def load_reset_state(path) -> dict:
    """The operator-confirmed bootstrap/reset marker. -> {...}. Never raises.

    -> {"present", "readable", "effective_from": date|None, "reason": str,
        "confirmed_by": str}

    ⛔ THIS IS THE ONLY WAY CAPACITY COMES BACK WITHOUT THE BROKER SAYING SO.
    A human writes it, having established that the window was empty at
    `effective_from`; requests recorded before that instant stop counting and
    requests after it count normally. It is therefore self-limiting — it grants
    one fresh window, not a standing exemption — and it is auditable, because
    the file records who confirmed it, when, and why.

    Malformed or undated => NOT honoured. A reset that cannot say *from when* is
    not evidence of anything, and treating it as one would be exactly the
    unverified-duration failure in a different costume.
    """
    p = Path(path)
    if not p.exists():
        return {"present": False, "readable": True, "effective_from": None,
                "reason": "", "confirmed_by": ""}
    try:
        doc = json.loads(p.read_text())
        raw = str(doc.get("effective_from") or "").strip()
        eff = dt.date.fromisoformat(raw[:10])
    except Exception:                                         # noqa: BLE001
        return {"present": True, "readable": False, "effective_from": None,
                "reason": "", "confirmed_by": ""}
    if not str(doc.get("confirmed_by") or "").strip():
        # An unattributed reset is not an operator confirmation.
        return {"present": True, "readable": False, "effective_from": None,
                "reason": "", "confirmed_by": ""}
    return {"present": True, "readable": True, "effective_from": eff,
            "reason": str(doc.get("reason") or ""),
            "confirmed_by": str(doc.get("confirmed_by"))}


def capacity_state(*, telemetry=None, ledger_state=None, reset_state=None,
                   today: dt.date, quota: int = DEFAULT_QUOTA,
                   rolling_window_days=None) -> dict:
    """THE remaining distinct-symbol capacity, and which authority established it.

    -> {"charged": set, "already_charged": int, "remaining_new": int,
        "capacity_unknown": bool, "source": str, "note": str, "retention": dict}

    ⛔ `retention` IS THE PERMISSION TO FORGET, AND IT COMES FROM THE SAME
    AUTHORITY THAT SET THE CAPACITY. Pass it straight to `record(**retention)`.
    It is `{}` — keep everything — for every branch that inferred capacity from
    local evidence or could not establish it at all, so a caller cannot prune
    under UNKNOWN even by accident. Only telemetry-with-detail (`keep_only`), an
    operator reset and a documented window (`prune_before`) return anything.
    Deciding capacity and deciding retention in two places is what let a 90-day
    prune quietly reclaim capacity the no-expiry policy forbade.

    Resolution order, strictly (see this module's header):

      1. BROKER TELEMETRY — `remain` is the answer, full stop. No local
         arithmetic, no assumed window. `detail_list` supplies the free-repeat
         set; if details were not supplied, NO symbol is treated as a free
         repeat (we decline to guess which ones the server is counting).
      2. OPERATOR RESET — ledger entries before `effective_from` stop counting.
      3. CONFIGURED WINDOW — an operator-documented duration expires entries.
      4. LOCAL LEDGER, NO EXPIRY — every recorded request still counts.
      5. UNKNOWN — zero new capacity.

    ⛔ AN ABSENT LEDGER IS **NOT** EVIDENCE THE BROKER QUOTA IS UNUSED. It is
    evidence that THIS BOX has no record — which is exactly the state of a fresh
    clone, a restored droplet, or a deleted file, none of which tell you
    anything about what the account has already spent. It therefore resolves to
    UNKNOWN (zero new capacity) unless telemetry answers or an operator has
    confirmed a reset. The previous version treated it as "first run, full
    capacity assumed", which is the most permissive possible reading of missing
    information.
    """
    quota = max(0, int(quota))
    ledger_state = ledger_state or {"requests": {}, "present": False, "readable": True}
    reset_state = reset_state or {"present": False, "readable": True,
                                  "effective_from": None}

    # ---- 1. broker telemetry: authoritative -------------------------------
    if telemetry and telemetry.get("ok"):
        remain = telemetry.get("remain")
        if isinstance(remain, int) and remain >= 0:
            charged = set(telemetry.get("charged") or ())
            if not telemetry.get("detail_available"):
                charged = set()
            return {"charged": charged, "already_charged": telemetry.get("used"),
                    "remaining_new": min(remain, quota), "capacity_unknown": False,
                    "source": SRC_TELEMETRY,
                    # The server named what it is counting, so the ledger may be
                    # narrowed to exactly that. Not time-based, and it cannot
                    # over-grant: it can only ever agree with the broker. With
                    # NO detail there is no retention — we will not guess which
                    # symbols to forget.
                    "retention": ({"keep_only": charged}
                                  if telemetry.get("detail_available") else {}),
                    "note": (f"broker telemetry: {telemetry.get('used')} used, "
                             f"{remain} distinct-symbol slot(s) remaining"
                             + ("" if telemetry.get("detail_available") else
                                " (no per-symbol detail — no symbol treated as a "
                                "free repeat)"))}

    led_present, led_readable = ledger_state.get("present"), ledger_state.get("readable")
    requests = ledger_state.get("requests") or {}

    # ---- ledger present but unreadable: capacity unknown -------------------
    if led_present and not led_readable:
        return {"charged": set(), "already_charged": None, "remaining_new": 0,
                "capacity_unknown": True, "source": SRC_UNKNOWN, "retention": {},
                "note": ("history-quota ledger is present but UNREADABLE and "
                         "broker telemetry did not answer — remaining capacity "
                         "is UNKNOWN and treated as zero")}

    # ---- 2. operator-confirmed reset ---------------------------------------
    if reset_state.get("present") and reset_state.get("readable") \
            and reset_state.get("effective_from"):
        eff = reset_state["effective_from"]
        since = {}
        for t, when in requests.items():
            try:
                if dt.date.fromisoformat(str(when)[:10]) >= eff:
                    since[t] = when
            except (ValueError, TypeError):
                since[t] = when          # undated -> counts (conservative)
        charged = set(since)
        return {"charged": charged, "already_charged": len(charged),
                "remaining_new": max(0, quota - len(charged)),
                "capacity_unknown": False, "source": SRC_RESET,
                # A human established the window was empty at `eff`, so entries
                # before it are genuinely superseded — an authority's act, not a
                # clock's.
                "retention": {"prune_before": eff},
                "note": (f"operator-confirmed reset effective {eff} "
                         f"(by {reset_state.get('confirmed_by')!r}): "
                         f"{len(charged)} request(s) since, "
                         f"{max(0, quota - len(charged))} of {quota} remaining")}

    # ---- 3. operator-documented rolling window -----------------------------
    if rolling_window_days not in (None, 0, ""):
        charged = charged_symbols(requests, today, rolling_window_days)
        return {"charged": charged, "already_charged": len(charged),
                "remaining_new": max(0, quota - len(charged)),
                "capacity_unknown": False, "source": SRC_WINDOW,
                # An operator DOCUMENTED the duration, so entries older than it
                # really have expired. This is the only branch where a date does
                # the forgetting, and it exists because someone confirmed it.
                "retention": {"prune_before":
                              today - dt.timedelta(days=int(rolling_window_days))},
                "note": (f"configured rolling window {rolling_window_days}d: "
                         f"{len(charged)} charged, "
                         f"{max(0, quota - len(charged))} of {quota} remaining")}

    # ---- 4. local ledger, NO expiry ----------------------------------------
    if led_present:
        charged = charged_symbols(requests, today, None)
        rem = max(0, quota - len(charged))
        return {"charged": charged, "already_charged": len(charged),
                "remaining_new": rem, "capacity_unknown": False,
                "source": SRC_LEDGER,
                # ⛔ NOTHING MAY BE FORGOTTEN HERE. Capacity is being inferred
                # from local evidence with no confirmed window, so discarding an
                # entry would hand back a distinct-symbol slot on nothing but
                # the passage of time — which is exactly the 90-day prune this
                # branch's own "no expiry" promise was being contradicted by.
                "retention": {},
                "note": (f"local ledger, NO expiry (broker window duration "
                         f"unconfirmed): {len(charged)} symbol(s) recorded, "
                         f"{rem} of {quota} remaining. Capacity returns only via "
                         f"broker telemetry or an operator-confirmed reset.")}

    # ---- 5. no ledger at all: UNKNOWN, not "unused" -------------------------
    return {"charged": set(), "already_charged": None, "remaining_new": 0,
            "capacity_unknown": True, "source": SRC_UNKNOWN, "retention": {},
            "note": ("no history-quota ledger and no broker telemetry — an "
                     "absent local record is NOT evidence the account's quota "
                     "is unused, so remaining capacity is treated as ZERO. "
                     "Establish it with broker telemetry (bring OpenD up) or "
                     "write an operator-confirmed reset: see "
                     "docs/OPERATOR_MANUAL.md 'history quota'.")}


def record(path, symbols, when: dt.date, *, prune_before=None,
           keep_only=None) -> dict:
    """Stamp `symbols` as requested on `when` and persist. Returns the ledger.

    ⛔ IT PRUNES NOTHING BY DEFAULT, AND THAT IS THE POINT. This function used
    to drop every entry older than 90 days "far beyond any plausible rolling
    window" — but the window is deliberately UNKNOWN, so there is no such thing
    as beyond it. That made a quiet back door out of the no-expiry policy:

        1. the ledger holds 100 symbols charged on day 0;
        2. day 91 arrives, telemetry is down, no reset, no documented window;
        3. a REPEAT is still allowed (it costs no distinct-symbol unit) and
           calls record();
        4. record() prunes the other 99 while writing that one repeat;
        5. the next capacity read counts 1 charged symbol, not 100, and grants
           99 slots of NEW distinct-symbol capacity;
        6. history requests go out with no telemetry, no confirmed reset and no
           documented window behind them.

    Retention is now an ACT OF AN AUTHORITY, never of a clock. Pass one of:

      `prune_before=<date>`  entries stamped before this are superseded. Only
                             legitimate from an operator-confirmed reset's
                             `effective_from`, or from a DOCUMENTED
                             `rolling_window_days`.
      `keep_only=<set>`      retain only these symbols. Only legitimate from
                             broker telemetry that supplied per-symbol detail —
                             the server itself saying what it is counting.

    Neither is inferred here. `capacity_state()` returns the permitted retention
    for whichever authority answered, so the SAME resolution that decides
    capacity decides what may be forgotten; a caller cannot accidentally prune
    under UNKNOWN because the branch that returns UNKNOWN returns no retention.

    ⛔ AN UNPARSEABLE TIMESTAMP IS KEPT, NEVER DROPPED. It used to be discarded
    by `except ValueError: continue` — while `charged_symbols()` deliberately
    COUNTS such an entry as still charged. So the two halves disagreed, and the
    disagreement leaked capacity: the entry counted against the budget until the
    next write, then vanished. Unknown-when is treated as still-counting on both
    sides now.

    ⚠️ GROWTH, STATED HONESTLY. With no authority the file keeps every symbol
    ever requested. It is a dict keyed by ticker, so it is bounded by the number
    of DISTINCT symbols this box has ever asked for — a few thousand at the very
    most, kilobytes. Unbounded-in-time is not unbounded-in-size, and an
    over-large ledger only ever makes the planner MORE conservative.

    ⛔ CALL IT ONLY AFTER A REQUEST WAS ACTUALLY ATTEMPTED, and for every name
    attempted INCLUDING the failures: the meter charges a distinct SYMBOL, not a
    successful response. Recording planned-but-unsent names would spend capacity
    that was never used; recording only the successes would make tomorrow treat
    a failed name as new and under-schedule around it for ever.

    ⛔ AN UNREADABLE LEDGER IS PRESERVED, NOT CLOBBERED. Overwriting it would
    discard the record of everything already charged and hand back full capacity
    on the next run — turning a corrupt file into a licence to overspend. It is
    moved aside to `<path>.corrupt` first, so the evidence survives and the
    fresh file starts honest. (In practice this is unreachable from the normal
    path: an unreadable ledger yields zero capacity and no free repeats, so
    nothing is scheduled and nothing is recorded. It is defence for the callers
    that do not go through `capacity_state()`.)
    """
    p = Path(path)
    state = load_ledger_state(p)
    if state["present"] and not state["readable"]:
        try:
            p.replace(p.with_suffix(p.suffix + ".corrupt"))
        except OSError:
            pass
    led = dict(state["requests"])

    # Retention runs BEFORE this run's stamps are added, so a symbol requested
    # now is never removed by the same call that recorded it.
    if keep_only is not None:
        keep = {str(k).strip().upper() for k in keep_only}
        led = {t: v for t, v in led.items() if t in keep}
    if prune_before is not None:
        kept = {}
        for t, v in led.items():
            try:
                if dt.date.fromisoformat(str(v)[:10]) >= prune_before:
                    kept[t] = v
            except (ValueError, TypeError):
                kept[t] = v      # unknown WHEN -> still counting; never dropped
        led = kept

    for s in symbols or ():
        t = str(s).strip().upper()
        if t:
            led[t] = str(when)

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(
        {"note": "request_history_kline requests, per symbol. LOCAL EVIDENCE of "
                 "requests this repo made — NOT a statement about the broker's "
                 "remaining quota. NOTHING here expires on a timer and nothing "
                 "is pruned by age: entries are removed only by broker "
                 "telemetry naming what it counts, by an operator-confirmed "
                 "reset (history_quota_reset.json), or by a DOCUMENTED "
                 "[history_acquisition] rolling_window_days. "
                 "See src/quota_planner.py.",
         "updated": str(when), "requests": led}, indent=2, sort_keys=True))
    return led
