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


def classify(proposal, pond_count, params) -> dict:
    """Decide auto-apply vs. hold-for-human. HOLD on any anomaly/judgment case."""
    reasons = []
    changes = len(proposal["add"]) + len(proposal["drop_fills"])
    if changes > params["auto_apply_max_changes"]:
        reasons.append(f"{changes} changes > {params['auto_apply_max_changes']} (possible data glitch)")
    if proposal["flagged_seeds"]:
        reasons.append("stale seed(s) up for decision: " + ", ".join(proposal["flagged_seeds"]))
    if pond_count < params["target_size"]:
        reasons.append(f"pond only {pond_count} names (< target {params['target_size']}) — data may be broken")
    bad = [t for t in proposal["add"] if not _looks_like_common_stock(t)]
    if bad:
        reasons.append("add(s) failed sanity: " + ", ".join(bad))
    # A fund already IN the universe is a live whitelist entry, so it is a
    # decision for a human, not something to auto-apply around.
    stuck = proposal.get("non_equity_kept") or []
    if stuck:
        reasons.append("existing member(s) are not common stock and remain in "
                       "the whitelist: " + ", ".join(stuck))
    return {"decision": "HOLD" if reasons else "AUTO_APPLY", "reasons": reasons}


def update_seed_watch(watch: dict, seed_ranks: dict, max_history: int) -> dict:
    """Append this week's momentum rank per seed; retain last max_history weeks."""
    for t, rank in seed_ranks.items():
        hist = watch.setdefault(t, [])
        hist.append(rank)
        watch[t] = hist[-max_history:]
    return watch


def flag_stale_seeds(watch: dict, params) -> list:
    """A seed is stale if its momentum rank has been worse than stale_seed_rank_floor
    for stale_seed_weeks consecutive weeks."""
    floor = params["stale_seed_rank_floor"]
    weeks = params["stale_seed_weeks"]
    flagged = [t for t, hist in watch.items()
               if len(hist) >= weeks and all(r > floor for r in hist[-weeks:])]
    return sorted(flagged)
