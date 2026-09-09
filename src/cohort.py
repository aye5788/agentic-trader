"""THE ELIGIBILITY COHORT — what the agent may consider, and what it may buy.

⛔ READ THIS BEFORE CHANGING ANYTHING HERE. Three different questions used to be
answered by ONE file, `config/universe.csv`, and conflating them is what this
module exists to end:

  ELIGIBILITY   is this security safe and liquid enough to be considered at all?
                A fact about the INSTRUMENT and its VENUE, established weekly by
                the moomoo V2 liquidity screen. -> `cohort.json`, written by
                scripts/universe_refresh.py.
  SCOREABILITY  does the local price panel carry enough history for
                `momentum.compute()` to produce a number? A fact about OUR DATA,
                established daily by scripts/fetch_prices.py's repair pass.
                -> `history_state.json`.
  SELECTION     which of the eligible, scored names should this book hold? A
                JUDGEMENT, and it belongs to the agent. Nothing here makes it.

`config/universe.csv` answered all three at once by being a hand-maintained list
that was simultaneously the ranking pool, the price-panel column set and the
order-gate whitelist. That is why a name admitted by the Friday screen was
instantly buyable while the signal silently dropped it for want of history
(OPSLOG 2026-09-06), and why the agent's hunting ground could only ever be 150
names somebody typed.

⛔ THE TWO NON-SCOREABLE STATES ARE RESEARCH LEADS, NOT CANDIDATES. A name that
passed the liquidity screen but cannot be scored is visible — the agent may look
at it, read its news, watch it season — and is NOT BUYABLE. Presenting it beside
a scored candidate would invite exactly the substitution this book must never
make: prose judgement standing in for the deterministic momentum calculation.

⛔ AND NONE OF THIS EVER BLOCKS A SELL. Eligibility is an ENTRY question. A held
name that leaves the cohort stays sellable, stays monitored, and keeps its stop —
stops here are software (scripts/market_monitor.py IS the stop), so a gate that
could refuse an exit would strip an open position of its only protection. Every
consumer of this module must preserve that; see `governance.vet_plan` and
`scripts/hooks/pretooluse_order_gate.py`, both of which return on the sell side
before any of this is consulted.

PURE AND STDLIB-ONLY, DELIBERATELY. `src/governance.py` imports this, and
governance is in `agentic-monitor`'s import closure — which runs under system
/usr/bin/python3 (3.10), not the .venv. No pandas, no numpy, no clock: `today`
is injected by every caller that needs it.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

SCHEMA_VERSION = 1

# Default artifact locations, relative to the repo root. Overridable via
# [universe] in config/strategy.toml so a test never touches the live files.
COHORT_FILE = "research_store/universe/cohort.json"
HISTORY_STATE_FILE = "research_store/universe/history_state.json"

# How stale an eligibility artifact may be before it stops authorising buys.
# The screen is WEEKLY (Friday), so a 10-day ceiling tolerates exactly one
# missed run and refuses the second — long enough that a single failed Friday
# does not stop the book trading, short enough that a screen which has quietly
# died cannot keep authorising entries for a month.
DEFAULT_MAX_AGE_DAYS = 10

# ---------------------------------------------------------------------------
# The composed state model. These five strings are the whole vocabulary and
# every surface (brief, universe(), the order gate, the dashboard) uses them.
# ---------------------------------------------------------------------------
SCOREABLE = "scoreable"                     # eligible AND rankable -> BUYABLE
PENDING_HISTORY = "eligible_pending_history"  # eligible, not yet rankable -> research only
UNSCOREABLE = "eligible_unscoreable"        # eligible, rankable FAILED -> research only
# ⛔ VENUE UNVERIFIED OR DISALLOWED -> OBSERVE AND SELL, NEVER BUY.
# `universe_maint.build_ranked_screen` deliberately exempts INCUMBENTS from the
# venue allowlist, so a name already in the universe keeps its rank even when
# its exchange metadata is missing or off-allowlist — dropping it there would
# silently de-list a held name on nothing but a metadata gap. That exemption is
# correct for KEEPING and catastrophic for BUYING: without this status such an
# incumbent flows straight into the cohort, becomes scoreable the moment its
# history is complete, and is authorised for a NEW ENTRY on a venue nobody
# verified. "Keep observing it" and "open a new position in it" are different
# permissions and this is where they separate.
OBSERVE_ONLY = "observe_only"
BUYABLE_STATUSES = (SCOREABLE,)

# History states, written by scripts/fetch_prices.py. `unknown` is what a cohort
# member with no record at all resolves to — a name the price path has not seen
# yet, which is pending, never scoreable.
HIST_COMPLETE = "complete"              # the momentum window is satisfied
HIST_REPAIRING = "repairing"            # history requested, still short
HIST_SEASONING = "seasoning"            # listed too recently to have the window
HIST_DEFERRED = "deferred_quota"        # past this window's distinct-symbol budget
HIST_FAILED = "failed_provider"         # the provider returned nothing usable
HIST_UNKNOWN = "unknown"                # no record — the price path has not seen it

_PENDING_HIST = (HIST_REPAIRING, HIST_SEASONING, HIST_DEFERRED, HIST_UNKNOWN)


class CohortInvalid(Exception):
    """The eligibility artifact cannot be trusted to authorise a buy.

    ⛔ RAISED, NEVER SWALLOWED INTO AN EMPTY SET. An empty cohort and an
    unreadable one are the same value to a naive reader and they mean opposite
    things: "nothing is eligible" is a decision, "I could not read the file" is
    an outage. `src/health.py` already records what that conflation costs (a
    corrupt overrides.json silently reverted every stop in the book). Callers
    must turn this into a REFUSAL for buys, and must not let it touch sells.
    """


# --------------------------------------------------------------------------- #
# loading + validation
# --------------------------------------------------------------------------- #
_REQUIRED_EVIDENCE = ("ticker", "rank", "avg_daily_turnover_usd",
                      "market_cap", "venue", "status", "membership")


def _finite_positive(v) -> bool:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return v == v and v not in (float("inf"), float("-inf")) and v > 0


def validate(doc, *, min_turnover_usd=None, allowed_venues=None) -> list:
    """Everything wrong with `doc`, as a list of strings. Empty list = usable.

    Structural first, then EVIDENCE. The evidence checks are the point: this
    artifact replaces a hand-curated list as the buy boundary, so "a file exists
    and parses" is nowhere near enough. Every name must carry the liquidity and
    venue evidence that made it eligible, and that evidence must still clear the
    configured floors — otherwise a screen run under a different policy, or a
    half-written file, could authorise entries nobody sanctioned.

    Pure. Takes no clock; freshness is `freshness()`, deliberately separate so a
    caller can report "valid but stale" rather than one undifferentiated failure.
    """
    problems = []
    if not isinstance(doc, dict):
        return [f"cohort artifact is {type(doc).__name__}, not an object"]

    ver = doc.get("schema_version")
    if ver != SCHEMA_VERSION:
        problems.append(
            f"schema_version {ver!r}, expected {SCHEMA_VERSION} — a cohort "
            f"written by different code must not be read under these rules")

    if not str(doc.get("as_of") or "").strip():
        problems.append("no as_of — an undated cohort cannot be checked for staleness")

    prov = doc.get("provenance")
    if not isinstance(prov, dict) or not prov:
        problems.append("no provenance — cannot tell which screen produced this")
    elif not str(prov.get("screen_version") or "").strip():
        problems.append("provenance carries no screen_version")

    cov = doc.get("coverage")
    if not isinstance(cov, dict):
        problems.append("no coverage block — cannot tell whether the screen was complete")
    else:
        # ⛔ COVERAGE IS EVIDENCE THE WRITER SUPPLIES, NEVER INFERRED HERE. A
        # truncated screen is well-formed and a well-formed short list is
        # indistinguishable from a complete one by inspection alone — the same
        # reasoning that made `universe_maint.classify` refuse on unexplained
        # absence rather than on a length check.
        if cov.get("complete") is not True:
            problems.append(
                "coverage.complete is not True — the screen could not prove it "
                "saw the whole market it claims to rank")
        probs = cov.get("problems")
        if probs:
            problems.append("screen reported integrity problems: "
                            + "; ".join(str(p) for p in list(probs)[:5]))

    names = doc.get("names")
    if not isinstance(names, list) or not names:
        problems.append("no names — an empty cohort authorises nothing and is "
                        "reported as such, never treated as 'everything'")
        return problems

    seen = set()
    for i, row in enumerate(names):
        if not isinstance(row, dict):
            problems.append(f"names[{i}] is {type(row).__name__}, not a record")
            continue
        missing = [k for k in _REQUIRED_EVIDENCE if k not in row]
        if missing:
            problems.append(f"names[{i}] missing evidence: {', '.join(missing)}")
            continue
        t = str(row["ticker"] or "").strip().upper()
        if not t:
            problems.append(f"names[{i}] has a blank ticker")
            continue
        if t in seen:
            problems.append(f"duplicate ticker in cohort: {t}")
            continue
        seen.add(t)
        if not _finite_positive(row.get("avg_daily_turnover_usd")):
            problems.append(f"{t}: unusable turnover evidence "
                            f"({row.get('avg_daily_turnover_usd')!r})")
        elif (min_turnover_usd is not None
              and float(row["avg_daily_turnover_usd"]) < float(min_turnover_usd)):
            problems.append(
                f"{t}: turnover ${float(row['avg_daily_turnover_usd']):,.0f}/day is "
                f"below the ${float(min_turnover_usd):,.0f} floor this cohort "
                f"claims to enforce")
        if not _finite_positive(row.get("market_cap")):
            problems.append(f"{t}: unusable market-cap evidence ({row.get('market_cap')!r})")
        # ⛔ AN OFF-ALLOWLIST VENUE IS NOT, BY ITSELF, A REASON TO REJECT THE
        # ARTIFACT. Such a row is legitimate: `build_ranked_screen` exempts
        # INCUMBENTS from the venue filter so a metadata gap cannot silently
        # de-list a name we may be holding. Rejecting the whole cohort for one
        # of them would turn a missing exchange string into a box-wide buy
        # freeze. It is handled by CLASSIFICATION instead — `compose()` gives it
        # OBSERVE_ONLY, so it stays visible and sellable and can never be
        # bought.
        #
        # What IS fatal is a row that CLAIMS entry eligibility it does not have.
        # `compose()` re-derives that from `venue` and ignores the stored flag,
        # so a forged flag is already inert; failing here as well means a
        # tampered or wrongly-generated artifact is refused at LOAD rather than
        # quietly reclassified, which is the difference between noticing and not.
        if (allowed_venues is not None and row.get("entry_eligible") is True
                and row.get("venue") not in set(allowed_venues)):
            problems.append(
                f"{t}: claims entry_eligible=true but its venue "
                f"{row.get('venue')!r} is not an approved US venue — the "
                f"artifact asserts a permission its own evidence denies")
    return problems


def load(path, *, min_turnover_usd=None, allowed_venues=None) -> dict:
    """Read and validate the eligibility artifact, or raise `CohortInvalid`.

    ABSENT and MALFORMED both raise, and they carry different messages. Absent
    is a real state (the screen has never run on this box); malformed is a
    defect. Neither may become "an empty cohort", because a caller that reads an
    empty cohort as a clean answer would authorise nothing while reporting
    normally — or, worse, a caller with an `if not cohort:` fallback would drop
    straight back to the legacy list without saying so.
    """
    p = Path(path)
    if not p.exists():
        raise CohortInvalid(f"no eligibility cohort at {p} — the weekly screen "
                            f"has not produced one on this box")
    try:
        doc = json.loads(p.read_text())
    except Exception as e:                                    # noqa: BLE001
        raise CohortInvalid(f"cohort at {p} is present but unreadable "
                            f"({type(e).__name__}: {e})") from e
    problems = validate(doc, min_turnover_usd=min_turnover_usd,
                        allowed_venues=allowed_venues)
    if problems:
        raise CohortInvalid(f"cohort at {p} is invalid: " + "; ".join(problems[:5]))
    return doc


def load_history_state(path) -> dict:
    """{ticker: state} from the daily price path. Missing/unreadable -> {}.

    ⛔ FAILS TOWARD PENDING, NOT TOWARD BUYABLE, AND THAT IS WHY {} IS SAFE
    HERE while an unreadable COHORT raises. An absent history record resolves to
    `unknown`, which composes to `eligible_pending_history` — visible, not
    buyable. So the degraded mode of a missing history file is "nothing is
    scoreable", which refuses entries rather than authorising them. The cohort
    file has the opposite polarity (it is the thing that says yes), so it must
    raise instead.
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        doc = json.loads(p.read_text())
    except Exception:                                         # noqa: BLE001
        return {}
    states = doc.get("states") if isinstance(doc, dict) else None
    if not isinstance(states, dict):
        return {}
    return {str(k).strip().upper(): str(v).strip()
            for k, v in states.items() if str(k).strip()}


# --------------------------------------------------------------------------- #
# freshness
# --------------------------------------------------------------------------- #
def freshness(doc, today: dt.date, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> dict:
    """-> {"as_of", "age_days", "max_age_days", "stale", "reason"}. Pure.

    An UNPARSEABLE as_of is stale, not fresh. Every "is this old?" check in this
    repo has at some point returned "no" because it could not read the date at
    all (OPSLOG 2026-08-31: `_staleness()` returned None on a snapshot that was
    a day old and parsed perfectly, and the session traded on it).
    """
    raw = str((doc or {}).get("as_of") or "").strip()
    try:
        as_of = dt.date.fromisoformat(raw[:10])
    except ValueError:
        return {"as_of": raw or None, "age_days": None,
                "max_age_days": int(max_age_days), "stale": True,
                "reason": f"as_of {raw!r} is not a date — treating as stale"}
    age = (today - as_of).days
    stale = age > int(max_age_days)
    return {"as_of": str(as_of), "age_days": age,
            "max_age_days": int(max_age_days), "stale": stale,
            "reason": (f"cohort is {age}d old, past the {max_age_days}d ceiling"
                       if stale else "")}


# --------------------------------------------------------------------------- #
# composition — eligibility x scoreability
# --------------------------------------------------------------------------- #
def entry_eligible(row, allowed_venues) -> bool:
    """May a NEW BUY name this row's symbol, on venue evidence alone? Pure.

    ⛔ RE-DERIVED AT READ TIME FROM THE `venue` FIELD, NEVER TRUSTED FROM A
    STORED FLAG. An artifact that reached disk without passing the producer's
    validation — hand-edited, written by an older build, restored from a
    backup, or produced by a code path someone added later — must not be able to
    assert its own buy-authorisation. So there is deliberately no
    `row["entry_eligible"]` read here: the venue string is the evidence and this
    function is the judgement, and it runs on every read.

    `allowed_venues=None` means the caller declined to enforce, which is only
    ever right for a pure structural inspection. Every authorisation path passes
    the real allowlist; `active_view()` does it for you.
    """
    if allowed_venues is None:
        return True
    return row.get("venue") in set(allowed_venues)


def compose(doc, history_states: dict, allowed_venues=None) -> dict:
    """Fold the weekly eligibility artifact and the daily history state into the
    one tri-state view every surface reads.

    Returns::

        {"as_of", "by_ticker": {T: {...}}, "scoreable": [...],
         "pending_history": [...], "unscoreable": [...],
         "counts": {...}, "ranks": {T: int}}

    Order within each list is COHORT RANK, i.e. by liquidity, not alphabetical —
    the same order the screen produced, so a reader can see the shape of the
    cohort rather than the alphabet. Pure; no clock, no I/O.
    """
    states = {str(k).upper(): v for k, v in (history_states or {}).items()}
    by_ticker, ranks = {}, {}
    scoreable, pending, unscoreable, observe = [], [], [], []
    for row in (doc or {}).get("names") or []:
        t = str(row.get("ticker") or "").strip().upper()
        if not t:
            continue
        hist = states.get(t, HIST_UNKNOWN)
        # ⛔ VENUE IS TESTED FIRST, AND IT IS TERMINAL FOR ENTRY. A name whose
        # exchange we cannot verify is observe-and-sell REGARDLESS of how
        # complete its history is — a full 252-session window is evidence about
        # our panel, not about where the thing trades. Ordering this after the
        # history branch would let `hist == complete` reach SCOREABLE first,
        # which is exactly the defect: an incumbent kept by the screen's venue
        # exemption becoming buyable the day its backfill finished.
        if not entry_eligible(row, allowed_venues):
            status = OBSERVE_ONLY
            observe.append(t)
        elif hist == HIST_COMPLETE:
            status = SCOREABLE
            scoreable.append(t)
        elif hist == HIST_FAILED:
            status = UNSCOREABLE
            unscoreable.append(t)
        else:
            # Anything not positively complete is PENDING, including a state
            # string this version does not recognise. An unknown state must
            # never fall through to "scoreable" — that is the branch that would
            # make a name buyable on a typo.
            status = PENDING_HISTORY
            pending.append(t)
        rank = row.get("rank")
        ranks[t] = int(rank) if isinstance(rank, int) and not isinstance(rank, bool) else None
        by_ticker[t] = {
            "ticker": t,
            "status": status,
            "history_state": hist,
            "buyable": status in BUYABLE_STATUSES,
            # Entry permission and observation permission, stated separately
            # rather than left to be inferred from `status`. Everything in the
            # cohort is observable and sellable; only `entry_eligible` names may
            # ever be bought, and only then if they are also scoreable.
            "entry_eligible": entry_eligible(row, allowed_venues),
            "rank": ranks[t],
            "avg_daily_turnover_usd": row.get("avg_daily_turnover_usd"),
            "market_cap": row.get("market_cap"),
            "venue": row.get("venue"),
            "membership": row.get("membership"),
        }
    return {
        "as_of": (doc or {}).get("as_of"),
        "by_ticker": by_ticker,
        "scoreable": scoreable,
        "pending_history": pending,
        "unscoreable": unscoreable,
        "observe_only": observe,
        "ranks": ranks,
        "counts": {"eligible": len(by_ticker), "scoreable": len(scoreable),
                   "pending_history": len(pending), "unscoreable": len(unscoreable),
                   "observe_only": len(observe)},
    }


def buyable(view: dict) -> set:
    """The symbols a BUY may name, from a composed view. Buys only — ever.

    Deliberately a plain set with no fallback and no "or everything" branch: the
    ONLY way to get a permissive answer out of this function is to hand it a
    view that genuinely contains scoreable names.
    """
    return {t for t, rec in (view or {}).get("by_ticker", {}).items() if rec["buyable"]}


def explain(view: dict, symbol: str) -> str:
    """Why `symbol` is or is not buyable, in the words the agent will read.

    A refusal has to say which of the two questions failed — eligibility or
    scoreability — because they have completely different remedies and one of
    them ("wait, it is seasoning") is not a remedy at all.
    """
    t = str(symbol or "").strip().upper()
    rec = (view or {}).get("by_ticker", {}).get(t)
    if rec is None:
        return (f"{t} is not in the eligibility cohort as of "
                f"{(view or {}).get('as_of')}: it did not clear the weekly "
                f"liquidity/venue screen, so there is no evidence it is "
                f"tradeable at this book's standard. It is not buyable. "
                f"(A SELL is never refused for this reason.)")
    if rec["buyable"]:
        return (f"{t} is eligible (cohort rank {rec['rank']}, "
                f"${(rec['avg_daily_turnover_usd'] or 0):,.0f}/day on "
                f"{rec['venue']}) and scoreable.")
    if rec["status"] == OBSERVE_ONLY:
        return (f"{t} is OBSERVE-ONLY: its venue is {rec['venue']!r}, which is "
                f"not an approved US exchange (approved: NYSE / NASDAQ / AMEX), "
                f"so there is no verified venue evidence for a NEW position. It "
                f"is kept in the cohort because it is an incumbent — you can "
                f"watch it, and if you HOLD it you can sell it and it keeps its "
                f"stop — but it is not buyable.")
    if rec["status"] == UNSCOREABLE:
        return (f"{t} is ELIGIBLE but NOT SCOREABLE: the price panel cannot "
                f"satisfy the momentum window and the repair failed for a "
                f"provider/data reason ({rec['history_state']}). It is a "
                f"research lead, not a candidate — the deterministic signal has "
                f"no number for it, so it is not buyable.")
    return (f"{t} is ELIGIBLE but its history is PENDING "
            f"({rec['history_state']}): the panel cannot yet satisfy the "
            f"momentum window. You may research it; it is not buyable until it "
            f"is scoreable, because buying it would mean selecting on prose "
            f"where every other holding was selected on the signal.")


# --------------------------------------------------------------------------- #
# config plumbing
# --------------------------------------------------------------------------- #
def allowed_venues() -> tuple:
    """The approved US venues, from the ONE place that defines them.

    Imported from `universe_maint` rather than restated, so the discovery filter
    and the read-time authorisation check cannot drift apart — a second copy of
    this tuple is a second definition, and the one that matters would be
    whichever a given reader happened to find. Falls back to the literal only if
    the import fails, and that fallback is deliberately the same three venues:
    an empty or permissive fallback here would authorise everything.
    """
    try:
        import universe_maint                                # noqa: PLC0415
        return tuple(universe_maint.ALLOWED_VENUES)
    except Exception:                                        # noqa: BLE001
        return ("US_NYSE", "US_NASDAQ", "US_AMEX")


def mode(cfg) -> str:
    """"fixed_list" (the legacy CSV) or "cohort" (this artifact). THE switch.

    ⛔ THIS ONE KEY IS THE ENTIRE ACTIVATION POINT of the cohort migration, and
    it is deliberately the only one. `[universe] mode` in config/strategy.toml
    (or the git-ignored config/strategy.local.toml, which wins) decides whether
    the order gate, the ranking pool and the price panel read the curated CSV or
    the persisted cohort. Rollback is setting it back — no code change, no
    redeploy, no data migration. Anything unrecognised reads as "fixed_list",
    because the failure direction of a typo must be TODAY'S BEHAVIOUR, never an
    accidental widening of what may be bought.
    """
    m = str(((cfg or {}).get("universe") or {}).get("mode") or "").strip().lower()
    return "cohort" if m == "cohort" else "fixed_list"


def paths(cfg, repo: Path) -> tuple:
    """(cohort_file, history_state_file) as absolute paths."""
    u = (cfg or {}).get("universe") or {}
    return (repo / str(u.get("cohort_file") or COHORT_FILE),
            repo / str(u.get("history_state_file") or HISTORY_STATE_FILE))


def max_age_days(cfg) -> int:
    u = (cfg or {}).get("universe") or {}
    try:
        return int(u.get("cohort_max_age_days", DEFAULT_MAX_AGE_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_AGE_DAYS


def active_view(cfg, repo: Path, today: dt.date) -> dict:
    """THE composed cohort for this box right now, or raise `CohortInvalid`.

    Freshness is enforced HERE rather than left to each caller, because "valid
    but three weeks old" is precisely the shape of artifact that keeps
    authorising buys after the job that maintains it has died — and every
    consumer would otherwise have to remember to check.
    """
    cfile, hfile = paths(cfg, repo)
    umc = (cfg or {}).get("universe_maintenance") or {}
    floor = umc.get("add_dvol_floor_usd")
    venues = allowed_venues()
    doc = load(cfile, min_turnover_usd=float(floor) if floor else None,
               allowed_venues=venues)
    fresh = freshness(doc, today, max_age_days(cfg))
    if fresh["stale"]:
        raise CohortInvalid(f"cohort at {cfile} is STALE: {fresh['reason']} — a "
                            f"stale eligibility artifact may not authorise new "
                            f"buys (exits are unaffected)")
    # ⛔ THE ALLOWLIST IS APPLIED HERE, AT READ TIME, ON EVERY AUTHORISATION.
    # Not because the weekly producer is untrusted in particular, but because a
    # permission that is only ever checked by the thing that WRITES the file is
    # a permission that any other writer — an older build, a restored backup, a
    # hand edit, a future code path — silently grants itself. Same reasoning as
    # the PreToolUse gate existing at all rather than trusting the agent to call
    # check_order().
    view = compose(doc, load_history_state(hfile), allowed_venues=venues)
    view["freshness"] = fresh
    view["provenance"] = doc.get("provenance")
    view["coverage"] = doc.get("coverage")
    view["allowed_venues"] = list(venues)
    return view


# --------------------------------------------------------------------------- #
# building the artifact (pure — scripts/universe_refresh.py does the I/O)
# --------------------------------------------------------------------------- #
def build(ranked, turnovers, caps, venues, *, as_of, generated_at,
          incumbents=(), provenance=None, coverage=None,
          rank_max=None, allowed=None) -> dict:
    """Assemble the eligibility artifact from a completed screen. Pure.

    `ranked` is the screen's own descending-turnover order (from
    `universe_maint.build_ranked_screen`), `turnovers` is $/day AFTER the single
    cumulative-to-daily division, `caps`/`venues` are the market-cap and
    exchange evidence. Rank is POSITIONAL in `ranked`, so it means exactly what
    the membership rules already mean by rank.

    ⛔ EVERY NAME CARRIES ITS OWN EVIDENCE, not just its ticker. That is the
    difference between this and `config/universe.csv`: a curated list asserts
    "these are liquid enough" and cannot be re-checked, while a row here states
    the turnover, the cap and the venue that made it eligible, so `validate()`
    can re-test the claim against the configured floors at read time. A file
    that only listed tickers would be a CSV with a longer name.

    ⛔ A NAME WITH NO USABLE EVIDENCE IS OMITTED, never written with a null. A
    null would parse, pass a length check, and then fail `validate()` at the
    worst possible moment — the read that authorises a buy.
    """
    names, rank = [], 0
    allowed = allowed_venues() if allowed is None else tuple(allowed)
    incumbents = {str(i).strip().upper() for i in (incumbents or ())}
    for t in ranked:
        t = str(t).strip().upper()
        rank += 1
        if rank_max is not None and rank > int(rank_max):
            break
        adtv, cap = turnovers.get(t), (caps or {}).get(t)
        if not (_finite_positive(adtv) and _finite_positive(cap)):
            continue
        venue = (venues or {}).get(t)
        names.append({
            "ticker": t,
            "rank": rank,
            "avg_daily_turnover_usd": float(adtv),
            "market_cap": float(cap),
            "venue": venue,
            "status": "eligible",
            "membership": "incumbent" if t in incumbents else "new",
            # ⛔ STAMPED FOR THE READER'S BENEFIT, NOT AS THE AUTHORISATION.
            # `compose()` re-derives this from `venue` on every read and ignores
            # what is stored here, so a wrong or forged value cannot authorise
            # anything. It is written so a human inspecting the file can see at
            # a glance which rows are observe-only, and so `validate()` can
            # refuse an artifact whose claim contradicts its own evidence.
            "entry_eligible": entry_eligible({"venue": venue}, allowed),
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": str(as_of),
        "generated_at": str(generated_at),
        "provenance": dict(provenance or {}),
        "coverage": dict(coverage or {}),
        "names": names,
    }
