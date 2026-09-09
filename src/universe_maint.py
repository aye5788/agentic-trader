"""Pure universe-maintenance logic: read/write the CSV, rank by dollar-volume,
propose membership under the seed-protection + band rules. No I/O beyond the CSV
helpers; no moomoo import (that lives in adapters/moomoo)."""
import csv
import re

FIELDS = ["ticker", "source", "sector", "exchange", "flag", "as_of"]

_DAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
         "friday": 4, "saturday": 5, "sunday": 6}
DEFAULT_SCREEN_DAY = "friday"


def screen_due(cfg: dict, today) -> bool:
    """Is the universe rescreen due on `today`? -> bool. Pure; takes no clock.

    ⛔ THE CADENCE LIVES IN CONFIG, NOT IN A CRON LITERAL. It was quarterly —
    `0 19 1-7 1,4,7,10 *` plus a `[ "$(date +%u)" -eq 7 ]` guard in the bash
    wrapper — so the schedule was spelled in two places, in two languages, and
    the real cadence (first Sunday of Jan/Apr/Jul/Oct) was not written anywhere
    a reader would look. It also never once fired. Now: WEEKLY on
    `[universe_maintenance] screen_day`, and this predicate is the authority.

    An unrecognised day falls back to Friday rather than raising or refusing —
    a screen that silently stops running is the failure mode this whole module
    exists to avoid, and it is exactly what quarterly-and-never-fired looked
    like.
    """
    want = str((cfg.get("universe_maintenance") or {}).get(
        "screen_day", DEFAULT_SCREEN_DAY)).strip().lower()
    return today.weekday() == _DAYS.get(want, _DAYS[DEFAULT_SCREEN_DAY])


def read_universe(path) -> list:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def write_universe(path, rows) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def rank_pond(turnovers: dict) -> list:
    """Tickers by descending dollar-volume; drops None/<=0."""
    valid = {t: v for t, v in turnovers.items() if v and v > 0}
    return sorted(valid, key=lambda t: valid[t], reverse=True)


# --------------------------------------------------------------------------
#  V2 liquidity screen (2026-09-06) — the weekly discovery ranking
# --------------------------------------------------------------------------

# ⛔ DO NOT "CORRECT" THIS TO 1. moomoo's V2 field is named
# `CumulativeProperty.AVG_TURNOVER`, and the name is WRONG: with `days=N` the
# server returns the N-day CUMULATIVE dollar turnover, not a daily average.
# Verified live 2026-09-06 against single-session snapshots — NVDA
# $569.44B/20 = $28.5B/day against a $31.4B session (0.91x), AAPL 0.98x,
# F 0.99x, KO 0.84x; undivided it would read 20x too liquid.
#
# The whole liquidity policy rides on this number: `add_dvol_floor_usd` is
# $50M/DAY, so an undivided comparison would admit names at $2.5M/day. Divide
# in ONE place — here — and never again downstream.
V2_TURNOVER_DAYS = 20

# Venue allowlist for DISCOVERY. moomoo's `Market.US` includes the OTC pink
# sheets, which is what filled 100 of the old V1 screen's 400 slots with foreign
# ADR lines that could not even return a turnover quote. Values are moomoo's own
# `ExchType` strings from get_stock_basicinfo.
#
# ⚠️ This is a VENUE test, never an instrument test — see
# adapters.moomoo.research.exchange_types() for why the map must span the ETF
# roster too (moomoo files REITs there).
ALLOWED_VENUES = ("US_NYSE", "US_NASDAQ", "US_AMEX")


def to_avg_daily(cumulative, days: int = V2_TURNOVER_DAYS):
    """N-day cumulative dollar turnover -> average dollars per day.

    Returns None for anything that is not a usable positive number, so a
    malformed row cannot be silently scored as zero (which would quietly fail
    the add floor instead of raising an integrity problem)."""
    try:
        v = float(cumulative)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")) or v <= 0:
        return None
    return v / float(days)


def build_ranked_screen(rows, exchanges: dict, keep_rank_max: int,
                        days: int = V2_TURNOVER_DAYS, allowed=ALLOWED_VENUES,
                        incumbents=()) -> tuple:
    """V2 screen rows -> (ranked, avg_daily_turnover, report). Pure.

    `rows` arrive in the server's own descending-turnover order. This function
    verifies that order rather than trusting it, applies the venue allowlist,
    and converts cumulative turnover to the daily average the membership rules
    are written in.

    ⛔ INTEGRITY PROBLEMS ARE RETURNED, NOT SWALLOWED. Every entry in
    report["problems"] must become NO_CHANGE at the caller. This function must
    never fall back to another ranking source: a screen that quietly ranks on
    something else is a different strategy wearing the same name.

    ⛔ THE VENUE ALLOWLIST IS A DISCOVERY CONSTRAINT AND MUST NOT EVICT
    INCUMBENTS. An incumbent whose venue is unknown or disallowed keeps its
    rank, because dropping it from `ranked` would push it past keep_rank_max and
    silently de-list a name already in the whitelist on nothing more than a
    metadata gap. New names still need an allowed venue to be discoverable.
    """
    problems, excluded_venue, unknown_venue = [], [], []
    incumbents = set(incumbents)
    allowed = set(allowed)

    seen, ordered = set(), []
    prev = None
    for i, row in enumerate(rows):
        sym = (row.get("symbol") or "").strip()
        if not sym:
            problems.append(f"row {i} has no symbol")
            continue
        if sym in seen:
            problems.append(f"duplicate symbol in screen result: {sym}")
            continue
        seen.add(sym)
        cum = row.get("turnover_20d_cum")
        adtv = to_avg_daily(cum, days)
        if adtv is None:
            problems.append(f"{sym}: unusable {days}d turnover ({cum!r})")
            continue
        cap = row.get("market_cap")
        try:
            capf = float(cap)
        except (TypeError, ValueError):
            capf = None
        if capf is None or capf != capf or capf <= 0:
            # It passed a server-side market-cap FLOOR, so a null cap is
            # incoherent rather than merely absent.
            problems.append(f"{sym}: passed the cap floor but reports cap {cap!r}")
            continue
        # The server was asked to sort descending; prove it did.
        if prev is not None and cum > prev:
            problems.append(
                f"screen not sorted descending at {sym}: {cum} > {prev} — "
                f"server-side ordering cannot be trusted for ranking")
        prev = cum
        ordered.append((sym, adtv, capf))

    keep = []
    for sym, adtv, capf in ordered:
        venue = exchanges.get(sym)
        if sym in incumbents:
            keep.append((sym, adtv))
            continue
        if venue is None:
            unknown_venue.append(sym)
            continue
        if venue not in allowed:
            excluded_venue.append(sym)
            continue
        keep.append((sym, adtv))

    if len(keep) < keep_rank_max:
        problems.append(
            f"only {len(keep)} ranked name(s) on allowed venues, fewer than "
            f"keep_rank_max={keep_rank_max} — cannot establish the rank "
            f"boundary membership decisions depend on")

    ranked = [s for s, _ in keep]
    turnovers = {s: v for s, v in keep}
    # ⛔ THE EVIDENCE IS CARRIED OUT, NOT DISCARDED (2026-09-09). Market cap and
    # venue were validated here and then dropped, so the only thing downstream
    # ever saw was a ticker and a number -- which is exactly what made
    # `config/universe.csv` unauditable: a list of names asserting "these are
    # liquid enough" with no way to re-test the claim. The eligibility cohort
    # (src/cohort.py) re-validates every row against the configured floors at
    # READ time, and it can only do that if the row carries what made it
    # eligible. Additive keys: every existing reader of this report is untouched.
    caps = {s: c for s, _, c in ordered if s in turnovers}
    report = {"problems": problems, "excluded_venue": excluded_venue,
              "unknown_venue": unknown_venue, "ranked_count": len(ranked),
              "market_caps": caps,
              "venues": {s: exchanges.get(s) for s in ranked}}
    return ranked, turnovers, report


def propose_membership(ranked, turnovers, current_rows, seed_flags, params,
                       is_equity=None) -> dict:
    """Seeds always kept; fills kept while $-vol rank <= keep_rank_max; open slots
    filled from best non-incumbents ranked <= add_rank_max and >= the $-vol floor.
    flagged_seeds = seeds the weekly watch marked stale (surfaced, NOT dropped).

    ⛔ ADDS ARE EQUITIES ONLY (2026-08-20). `is_equity(ticker) -> bool` is
    injected so this stays pure and testable; `scripts/universe_refresh.py`
    passes a moomoo-backed one (a fund has no `total_market_val`). Default is
    the name-shape + denylist backstop.

    This is a FILTER on the add path, not merely a HOLD reason in classify().
    A fund reaching `add` would be written into config/universe.csv, which is
    also the order-gate whitelist — so a screen that merely flagged it would
    still have made it buyable the moment a human approved an otherwise routine
    proposal. Rejected candidates are reported in `rejected_non_equity` rather
    than dropped silently: the screen must be able to say what it refused.
    """
    is_equity = is_equity or _looks_like_common_stock
    target = params["target_size"]
    keep_max = params["keep_rank_max"]
    add_max = params["add_rank_max"]
    floor = params["add_dvol_floor_usd"]

    rank_of = {t: i + 1 for i, t in enumerate(ranked)}
    big = len(ranked) + 10_000
    by_ticker = {r["ticker"]: r for r in current_rows}
    seeds = [r["ticker"] for r in current_rows if r["source"] == "seed"]
    fills = [r["ticker"] for r in current_rows if r["source"] != "seed"]

    # ⛔ SEEDS ARE PROTECTED FROM THE RANK, NOT FROM THE EQUITY TEST.
    # `is_equity` was applied only to ADDS, so a fund already sitting in
    # config/universe.csv as a seed stayed there for ever -- and that file IS
    # the order-gate whitelist, so it stayed buyable. Seed protection exists so
    # a conviction name is not dropped for a bad dollar-volume week; it was
    # never meant to exempt an instrument from being an equity at all
    # (reviewer, 2026-08-20). Retained-but-non-equity names are SURFACED, never
    # silently dropped: removing a human-chosen seed is the operator's call.
    non_equity_kept = [t for t in seeds if not is_equity(t)]
    kept = list(seeds)  # 1. seeds always kept
    kept_fills = [t for t in fills if rank_of.get(t, big) <= keep_max]
    dropped_fills = [t for t in fills if rank_of.get(t, big) > keep_max]
    kept += kept_fills

    have = set(kept)  # 3. fill open slots
    open_slots = max(0, target - len(kept))
    adds, rejected = [], []
    for t in ranked:
        if len(adds) >= open_slots:
            break
        if t in have or rank_of[t] > add_max:
            if rank_of[t] > add_max:
                break
            continue
        if turnovers.get(t, 0) < floor:
            continue
        if not is_equity(t):            # funds/leveraged/odd instruments
            rejected.append(t)
            continue
        adds.append(t)

    result = [by_ticker[t] for t in kept]
    result += [{"ticker": t, "source": "screen", "sector": "",
                "exchange": "", "flag": "", "as_of": ""} for t in adds]
    return {
        "keep": sorted(kept),
        "drop_fills": sorted(dropped_fills),
        "add": adds,
        "rejected_non_equity": rejected,
        # Existing members that would not pass the add-time equity test. Not
        # dropped automatically — surfaced so a human decides.
        "non_equity_kept": sorted(non_equity_kept),
        "flagged_seeds": sorted(set(seeds) & set(seed_flags)),
        "result": result[:target],
    }


_LEVERAGED = {"SOXL", "SOXS", "TQQQ", "SQQQ", "SPXL", "SPXU", "TNA", "TZA",
              "UVXY", "SVXY", "UPRO", "SDOW", "UDOW", "LABU", "LABD"}

# ⛔ FUNDS ARE NOT CANDIDATES (2026-08-20). The weekly screen builds ADDs from a
# pond that is either moomoo's market-cap screen — documented as UNFILTERED, so
# preferred shares, SPAC units and funds all rank (docs/DATA_SOURCES.md §5e) —
# or, on failure, config/pit_pool.csv. The regex below passes "SPY", "GLD" and
# "XLK" happily, so before this the Friday screen could ADD a fund straight back
# into config/universe.csv, which is also the order-gate whitelist. That would
# have silently undone the sleeve deletion by a different route.
#
# This denylist is a BACKSTOP, not the mechanism. The real filter is a positive
# test: moomoo serves NO `total_market_val` for a fund (the documented cause of
# the capital-flow ETF null, OPSLOG 2026-07-28), so an add with no market cap is
# rejected in propose_membership via the injected `is_equity` predicate. A
# denylist alone can only ever exclude the funds someone remembered to list.
_FUNDS = {"SPY", "IWM", "EFA", "EEM", "GLD", "TLT", "AGG",
          "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB",
          "XLRE", "XLC", "QQQ", "DIA", "VTI", "VOO", "IVV", "SLV", "HYG",
          "LQD", "IEF", "SHY", "TIP", "VXX", "USO", "UNG"}

NON_EQUITY = _LEVERAGED | _FUNDS


def _looks_like_common_stock(ticker: str) -> bool:
    """Name-shape sanity: 1-5 uppercase letters, and not a known fund/leveraged
    product. NOT sufficient on its own — see NON_EQUITY above."""
    return bool(re.fullmatch(r"[A-Z]{1,5}", ticker)) and ticker not in NON_EQUITY


def classify(proposal, pond_count, params, coverage=None) -> dict:
    """Decide APPLY vs. change nothing. -> {"decision", "reasons", "report"}.

    ⛔ THERE IS NO LONGER A "WAIT FOR A HUMAN" OUTCOME, AND THAT IS THE POINT.
    The two outcomes are AUTO_APPLY and NO_CHANGE, and NO_CHANGE is reachable
    ONLY from a data-integrity failure, which self-clears the moment the feed is
    healthy again. Nothing here can enter a state that a person has to come and
    reset.

    Why this changed (2026-08-27): the previous version returned HOLD from five
    conditions, three of which regenerated themselves every week, so AUTO_APPLY
    was in practice unreachable and the screen emailed a proposal that could
    never become a universe. config/universe.csv went unchanged from 2026-07-08
    to 2026-08-27 while every scheduled job reported healthy.

      - `auto_apply_max_changes` (a churn ceiling) was justified in code as
        "possible data glitch". It is a PROXY, and a bad one: after weeks of a
        frozen pool a large diff is expected drift, not corruption. The direct
        test now exists — see `coverage` below — so the proxy is gone rather
        than merely widened. A wider threshold would have been the same defect
        with a bigger number.
      - `flagged_seeds` blocked the whole screen because ONE conviction name had
        drifted down the ranks. That is a report, not a fault: propose_membership
        surfaces stale seeds and never drops them. It no longer blocks the other
        149 names from rotating.
      - `non_equity_kept` blocked on incumbents that the equity predicate could
        not VERIFY, which is not the same claim as "is a fund" — see the caller.
        It is likewise reported, not blocking.

    WHAT STILL REFUSES, and why each is about a mechanism rather than a taste:
      - `coverage["v2_problems"]`: the liquidity screen contradicted itself —
        duplicate rows, unusable turnover, an order that is not descending, or
        too few venue-clean names to reach keep_rank_max. See
        build_ranked_screen().
      - `coverage["missing"]`: the feed did not account for every pond name.
        Ranking an incomplete panel silently de-lists whatever is absent.
      - short pond: fewer usable names than the target the screen must fill.
      - an add that fails the name-shape/denylist backstop.
    Each of those is a fact about the DATA, and each is true again next run only
    if the data is still broken.
    """
    reasons = []
    # ⛔ V2 SCREEN INTEGRITY. build_ranked_screen() reports duplicates, unusable
    # turnover values, a server sort that is not actually descending, and too few
    # venue-clean rows to establish the keep boundary. Each is direct evidence
    # the ranking cannot be trusted, so each refuses — and refusing means the
    # last-known-good universe stands, never a fall back to another discovery
    # mechanism (that would run a different strategy under the same name).
    for p in list((coverage or {}).get("v2_problems") or []):
        reasons.append(f"V2 screen integrity: {p}")
    missing = list((coverage or {}).get("missing") or [])
    if missing:
        head = ", ".join(missing[:8]) + ("…" if len(missing) > 8 else "")
        reasons.append(
            f"feed incomplete: {len(missing)} pond name(s) neither returned nor "
            f"reported unquotable ({head}) — ranking an incomplete panel would "
            f"silently drop tradable names")
    if pond_count < params["target_size"]:
        reasons.append(f"pond only {pond_count} names (< target {params['target_size']}) — data may be broken")
    bad = [t for t in proposal["add"] if not _looks_like_common_stock(t)]
    if bad:
        reasons.append("add(s) failed sanity: " + ", ".join(bad))
    # Reported, never blocking. Acting on either is a portfolio judgement that
    # belongs to a session, not a condition that freezes pool maintenance.
    report = {}
    if proposal["flagged_seeds"]:
        report["flagged_seeds"] = list(proposal["flagged_seeds"])
    if proposal.get("non_equity_kept"):
        report["unverified_incumbents"] = list(proposal["non_equity_kept"])
    return {"decision": "NO_CHANGE" if reasons else "AUTO_APPLY",
            "reasons": reasons, "report": report}


def update_seed_watch(watch: dict, seed_ranks: dict, max_history: int) -> dict:
    """Append this week's momentum rank per seed; retain last max_history weeks."""
    for t, rank in seed_ranks.items():
        hist = watch.setdefault(t, [])
        hist.append(rank)
        watch[t] = hist[-max_history:]
    return watch


def flag_stale_seeds(watch: dict, params) -> list:
    """A seed is stale if its momentum rank has been worse than stale_seed_rank_floor
    for stale_seed_weeks consecutive weeks.

    ⛔ A MISSING READING IS NOT A BAD READING. Only NUMERIC entries count. The
    accrual in scripts/slow_loop.py used to convert an INELIGIBLE seed — one the
    momentum filter did not rank at all — into the literal rank 9999, which is
    above any sane floor, so a name that was merely not trending accrued
    staleness every single week. In a cross-sectional momentum book most of the
    universe is untrending at any moment, so this flagged blue chips (MSFT,
    NFLX, ORCL, T, CMCSA, NKE were all flagged on 2026-08-21) and, because a
    flagged seed then forced HOLD, it froze the entire screen.

    Staleness must mean "was ranked, and ranked badly" — which is what
    [universe_maintenance] in config/strategy.toml has always claimed it means.
    A seed with no reading has produced no evidence either way.
    """
    floor = params["stale_seed_rank_floor"]
    weeks = params["stale_seed_weeks"]
    flagged = []
    for t, hist in watch.items():
        vals = [r for r in (hist or [])
                if isinstance(r, (int, float)) and not isinstance(r, bool)]
        if len(vals) >= weeks and all(r > floor for r in vals[-weeks:]):
            flagged.append(t)
    return sorted(flagged)
