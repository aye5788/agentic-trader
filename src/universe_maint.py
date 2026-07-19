"""Pure universe-maintenance logic: read/write the CSV, rank by dollar-volume,
propose membership under the seed-protection + band rules. No I/O beyond the CSV
helpers; no moomoo import (that lives in adapters/moomoo)."""
import csv
import re

FIELDS = ["ticker", "source", "sector", "exchange", "flag", "as_of"]


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


def propose_membership(ranked, turnovers, current_rows, seed_flags, params) -> dict:
    """Seeds always kept; fills kept while $-vol rank <= keep_rank_max; open slots
    filled from best non-incumbents ranked <= add_rank_max and >= the $-vol floor.
    flagged_seeds = seeds the weekly watch marked stale (surfaced, NOT dropped)."""
    target = params["target_size"]
    keep_max = params["keep_rank_max"]
    add_max = params["add_rank_max"]
    floor = params["add_dvol_floor_usd"]

    rank_of = {t: i + 1 for i, t in enumerate(ranked)}
    big = len(ranked) + 10_000
    by_ticker = {r["ticker"]: r for r in current_rows}
    seeds = [r["ticker"] for r in current_rows if r["source"] == "seed"]
    fills = [r["ticker"] for r in current_rows if r["source"] != "seed"]

    kept = list(seeds)  # 1. seeds always kept
    kept_fills = [t for t in fills if rank_of.get(t, big) <= keep_max]
    dropped_fills = [t for t in fills if rank_of.get(t, big) > keep_max]
    kept += kept_fills

    have = set(kept)  # 3. fill open slots
    open_slots = max(0, target - len(kept))
    adds = []
    for t in ranked:
        if len(adds) >= open_slots:
            break
        if t in have or rank_of[t] > add_max:
            if rank_of[t] > add_max:
                break
            continue
        if turnovers.get(t, 0) < floor:
            continue
        adds.append(t)

    result = [by_ticker[t] for t in kept]
    result += [{"ticker": t, "source": "screen", "sector": "",
                "exchange": "", "flag": "", "as_of": ""} for t in adds]
    return {
        "keep": sorted(kept),
        "drop_fills": sorted(dropped_fills),
        "add": adds,
        "flagged_seeds": sorted(set(seeds) & set(seed_flags)),
        "result": result[:target],
    }


_LEVERAGED = {"SOXL", "SOXS", "TQQQ", "SQQQ", "SPXL", "SPXU", "TNA", "TZA",
              "UVXY", "SVXY", "UPRO", "SDOW", "UDOW", "LABU", "LABD"}


def _looks_like_common_stock(ticker: str) -> bool:
    """v1 sanity: 1-5 uppercase letters, not a known leveraged/inverse ETF."""
    return bool(re.fullmatch(r"[A-Z]{1,5}", ticker)) and ticker not in _LEVERAGED


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
    return {"decision": "HOLD" if reasons else "AUTO_APPLY", "reasons": reasons}
