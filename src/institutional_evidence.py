"""INSTITUTIONAL EVIDENCE — lifecycle-weighted aggregation over measured behaviour. Pure.

WHAT THIS IS
    The aggregation layer above src/behavior_measurement.py. It folds the
    per-decision measurement into ONE DESCRIPTIVE RECORD PER ACTION CLASS and
    decides, on sample-size grounds alone, whether that record is solid enough
    to be shown to a future stateless session.

    It answers exactly one question:

        historically, when the agent took this class of action inside a real
        position, what did the price do over the next 1/5/10/20 sessions

    It does NOT answer "what should the agent do now". There is no score, no
    verdict, no rule, no threshold to trade against and no recommended action.
    A distribution is the whole output.

⛔ WHAT THE STATUS LABELS MEAN, AND WHAT THEY DO NOT
    `insufficient` / `candidate` / `validated` / `stale` describe EVIDENCE
    SUFFICIENCY — how many independent observations stand behind a distribution.
    They are not trading quality. `validated` does NOT mean the action class was
    correct, profitable or preferred; it means there are enough distinct
    positions and distinct symbols behind the numbers that showing them to the
    agent is not showing it noise. A validated distribution centred on a loss is
    exactly as `validated` as one centred on a gain.

⛔ REPEATED DECISIONS ARE NOT INDEPENDENT TRADES — THE LOAD-BEARING RULE
    One position lifecycle can carry many decisions: five HOLDs on one position
    is one position observed five times, not five trades. Every statistic here
    is therefore computed over LIFECYCLE-LEVEL observations. Decisions inside one
    lifecycle are first collapsed to their MEDIAN for that action class and
    horizon, and only those per-lifecycle medians are aggregated. `decision_n` is
    reported beside `lifecycle_n` so the difference is always visible, but it
    never drives a statistic and never drives a status.

⛔ THE LIVE AGENT HAS NO WRITE PATH HERE
    Nothing in this module is callable from an MCP tool. The artifact is written
    by an operator script; the agent only ever receives the rendered block, which
    is text. Evidence is never authored by the party it is shown to.

V1 SCOPE
    Position-management decisions only — every row in the measurement carries a
    `position_id`, so a decision made while flat is structurally absent rather
    than filtered out. BUY/SKIP-while-flat learning is a later, separate step and
    is not approximated here by symbol or by date.

    Agent-facing evidence covers the four exposure-direction groups Step 5
    already isolates. `unclassified` actions stay measurable and stay in the
    operator artifact's coverage, but are never promoted: an action whose effect
    on exposure is ambiguous cannot support a claim about what followed it.

PURITY
    No I/O, no clock, no journal write, no broker call. `as_of` is supplied by
    the caller; every window boundary is derived from it. Same input -> same
    output, including the version hash.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from datetime import date, datetime, timedelta

import behavior_measurement as _bm

EVIDENCE_SCHEMA = 1

SCOPE = "position_management"

#: Rolling window, in CALENDAR days, ending at `as_of`. Statistics are rebuilt
#: from source experience inside it on every run — never incrementally mutated,
#: because an incrementally updated statistic drifts from the data it claims.
WINDOW_DAYS = 180

#: An evidence item whose newest supporting decision is older than this is stale
#: even if the 180-day window still holds enough of it.
STALE_AFTER_DAYS = 45

STATUS_INSUFFICIENT = "insufficient"
STATUS_CANDIDATE = "candidate"
STATUS_VALIDATED = "validated"
STATUS_STALE = "stale"

#: The groups that may become agent-facing. `unclassified` is deliberately absent.
AGENT_GROUPS = (
    _bm.GROUP_INCREASE,
    _bm.GROUP_REDUCE,
    _bm.GROUP_HOLD,
    _bm.GROUP_RISK_LEVEL,
)

#: The horizon whose lifecycle-level coverage gates promotion. One horizon has to
#: carry the gate or the gate is vacuous; 10 sessions is the middle of the four
#: measured and is the only one used this way — every horizon is still reported.
GATE_HORIZON = 10

CANDIDATE_MIN = {"lifecycles": 4, "symbols": 3, "decisions": 0, "gate_lifecycles": 4}
VALIDATED_MIN = {"lifecycles": 8, "symbols": 5, "decisions": 12, "gate_lifecycles": 8}

#: No single symbol may stand behind more than this share of the supporting
#: lifecycles. Above it the item cannot be validated however large N is: eight
#: lifecycles that are really one name repeated is one observation wearing a
#: sample's clothes.
MAX_SYMBOL_SHARE = 0.35

#: At most one item per validated group, and there are four groups.
RETRIEVAL_LIMIT = 4

BLOCK_TARGET_CHARS = 1500
BLOCK_MAX_CHARS = 2000

#: Horizons rendered into the agent block. 1d stays in the artifact and in the
#: operator report; it is the noisiest of the four and the least worth the
#: characters in a capped block.
BLOCK_HORIZONS = (5, 10, 20)

#: Words that would turn a distribution into a verdict. Checked against the
#: rendered block, not merely avoided by intention.
FORBIDDEN_BLOCK_WORDS = (
    "favorable", "favourable", "unfavorable", "unfavourable",
    "good", "bad", "recommended", "recommend", "should", "avoid", "prefer",
    "better", "worse", "right", "wrong", "success", "successful", "optimal",
)

BLOCK_HEADING = "INSTITUTIONAL EXPERIENCE — HISTORICAL EVIDENCE"
BLOCK_WARNING = ("Historical evidence, not a trading rule.\n"
                 "Current facts may differ.")


# --------------------------------------------------------------------------- #
# dates — parsed, never invented
# --------------------------------------------------------------------------- #
def _as_date(value):
    """A `date` from an ISO date or timestamp, or None. Never guesses."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# statistics — over lifecycle-level observations only
# --------------------------------------------------------------------------- #
def _percentile(values, q):
    """Linear-interpolated percentile over a sorted list. Defined at n=1.

    Written out rather than taken from `statistics.quantiles`, which raises below
    n=2 — and n=1 is a real and reportable sample here, not an error.
    """
    vals = sorted(float(v) for v in values)
    n = len(vals)
    if n == 0:
        return None
    if n == 1:
        return vals[0]
    pos = q * (n - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def _numbers(values):
    return [float(v) for v in values
            if isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(float(v))]


def _count(items) -> dict:
    out: dict = {}
    for item in items:
        key = item if isinstance(item, str) else repr(item)
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def _distribution(lifecycle_values) -> dict:
    """mean / median / p25 / p75 over LIFECYCLE-level observations."""
    vals = _numbers(lifecycle_values)
    if not vals:
        return {"lifecycles": 0, "mean": None, "median": None,
                "p25": None, "p75": None}
    return {
        "lifecycles": len(vals),
        "mean": statistics.fmean(vals),
        "median": statistics.median(vals),
        "p25": _percentile(vals, 0.25),
        "p75": _percentile(vals, 0.75),
    }


# --------------------------------------------------------------------------- #
# reason text — carried verbatim, never classified
# --------------------------------------------------------------------------- #
def reason_index(projection) -> dict:
    """{(position_id, ts, action): reason} from a `lifecycle_outcomes.project()`.

    ⛔ TEXT ONLY, AND OPERATOR-ONLY. The reason a decision stated is preserved
    because it is cheap to carry and impossible to reconstruct later — not
    because anything here reads it. Nothing in this module parses it, matches on
    it, counts it, or turns it into a category, and it never enters the agent
    block or the version hash. A taxonomy over these strings is a different
    decision than the one this file implements.
    """
    out: dict = {}
    for lifecycle in (projection or {}).get("lifecycles", []):
        pid = lifecycle.get("position_id")
        for d in lifecycle.get("decisions", []) or []:
            if not isinstance(d, dict):
                continue
            reason = d.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                continue
            key = (pid, d.get("ts"), str(d.get("action") or "").strip().lower())
            out.setdefault(key, reason.strip())
    return out


# --------------------------------------------------------------------------- #
# one evidence item
# --------------------------------------------------------------------------- #
def evidence_id(analytical_group: str) -> str:
    """Stable for the same conceptual scope. Never varies with the statistics.

    Provenance depends on this: `evidence_ids_seen` on a decision must still name
    the same thing months later, when the numbers behind it have all changed.
    """
    return f"{SCOPE}:{analytical_group}"


def _lifecycle_observations(rows, horizon):
    """{position_id: median of that lifecycle's measured returns at `horizon`}.

    THE ANTI-DOUBLE-COUNTING STEP. Five HOLDs on one position contribute one
    number here, not five.
    """
    per_position: dict = {}
    for row in rows:
        val = row.get(f"return_{horizon}d")
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            continue
        if not math.isfinite(float(val)):
            continue
        per_position.setdefault(row.get("position_id"), []).append(float(val))
    return {pid: statistics.median(vals) for pid, vals in per_position.items()}


def _horizon_status_counts(rows, horizon) -> dict:
    """Coverage at one horizon, over DECISIONS. Pending/unavailable stay counted.

    They are excluded from the numbers above — a return that has not happened is
    not zero — but a horizon that is 80% pending must not read as a thin sample;
    it reads as a young one, and only these counts say which.
    """
    out = {_bm.MEASURED: 0, _bm.PENDING: 0, _bm.UNAVAILABLE: 0}
    key = f"{horizon}d"
    for row in rows:
        cell = (row.get("horizons") or {}).get(key) or {}
        status = cell.get("status")
        if status in out:
            out[status] += 1
    return out


def _symbol_of_lifecycle(rows) -> dict:
    """{position_id: symbol}. One lifecycle is one symbol; first row states it."""
    out: dict = {}
    for row in rows:
        pid = row.get("position_id")
        if pid not in out:
            out[pid] = row.get("symbol")
    return out


def _sufficiency(sample, gate_lifecycles, concentration) -> tuple:
    """(status-from-current-window, limitation notes). Sample size only."""
    notes = []

    def meets(mins):
        return (sample["lifecycles"] >= mins["lifecycles"]
                and sample["symbols"] >= mins["symbols"]
                and sample["decisions"] >= mins["decisions"]
                and gate_lifecycles >= mins["gate_lifecycles"])

    if not meets(CANDIDATE_MIN):
        return STATUS_INSUFFICIENT, notes
    if not meets(VALIDATED_MIN):
        return STATUS_CANDIDATE, notes
    if concentration is not None and concentration > MAX_SYMBOL_SHARE:
        # Held at candidate DELIBERATELY. The counts clear every validated
        # threshold; what they do not clear is independence, and a distribution
        # that is mostly one name is a statement about that name.
        notes.append(
            f"one symbol stands behind {concentration * 100:.0f}% of the "
            f"supporting lifecycles (cap {MAX_SYMBOL_SHARE * 100:.0f}%); held at "
            f"candidate on independence, not on sample size")
        return STATUS_CANDIDATE, notes
    return STATUS_VALIDATED, notes


def _previous_status(previous, eid):
    for item in (previous or {}).get("items", []) or []:
        if isinstance(item, dict) and item.get("evidence_id") == eid:
            return item.get("status"), bool(item.get("was_validated"))
    return None, False


def _item(group, rows, *, as_of, window_start, horizons, previous, reasons) -> dict:
    eid = evidence_id(group)
    by_symbol_lifecycle = _symbol_of_lifecycle(rows)
    lifecycle_ids = sorted(str(p) for p in by_symbol_lifecycle)
    symbols = sorted({str(s) for s in by_symbol_lifecycle.values() if s})

    counts: dict = {}
    for sym in by_symbol_lifecycle.values():
        counts[sym] = counts.get(sym, 0) + 1
    concentration = (max(counts.values()) / len(by_symbol_lifecycle)
                     if by_symbol_lifecycle else None)

    sample = {"decisions": len(rows),
              "lifecycles": len(lifecycle_ids),
              "symbols": len(symbols)}

    horizon_blocks: dict = {}
    gate_lifecycles = 0
    for h in horizons:
        block = _distribution(_lifecycle_observations(rows, h).values())
        status_counts = _horizon_status_counts(rows, h)
        block["decisions_measured"] = status_counts[_bm.MEASURED]
        block["decisions_pending"] = status_counts[_bm.PENDING]
        block["decisions_unavailable"] = status_counts[_bm.UNAVAILABLE]
        horizon_blocks[f"{h}d"] = block
        if h == GATE_HORIZON:
            gate_lifecycles = block["lifecycles"]

    # THE SUPPORTING-OBSERVATION SPAN. Distinct from the rolling query window
    # above it: `window_start`/`window_end` are where we LOOKED, these are where
    # the evidence actually IS. The block renders this pair and not the window,
    # so a version computed over the window would move every day the calendar
    # did while the agent read identical text.
    oldest = newest = None
    for row in rows:
        d = _as_date(row.get("ts"))
        if not d:
            continue
        if newest is None or d > newest:
            newest = d
        if oldest is None or d < oldest:
            oldest = d

    status, notes = _sufficiency(sample, gate_lifecycles, concentration)

    prev_status, prev_was_validated = _previous_status(previous, eid)
    was_validated = (status == STATUS_VALIDATED
                     or prev_status == STATUS_VALIDATED
                     or prev_was_validated)

    age_days = (as_of - newest).days if newest else None
    if was_validated and age_days is not None and age_days > STALE_AFTER_DAYS:
        notes.append(f"newest supporting decision is {age_days} days old "
                     f"(stale beyond {STALE_AFTER_DAYS})")
        status = STATUS_STALE
    elif was_validated and status == STATUS_INSUFFICIENT:
        notes.append("previously met the validated thresholds; the current "
                     "rolling window no longer reaches candidate")
        status = STATUS_STALE

    limitations = [
        "descriptive distribution of what the price did afterwards; it is not a "
        "claim that the decision caused it, and there is no counterfactual here",
        "lifecycle-weighted: repeated decisions inside one position contribute "
        "one observation per horizon, not one each",
        "position-management decisions only — decisions made while flat are not "
        "in this corpus at all",
    ] + notes

    excerpts = []
    if reasons:
        for row in rows:
            key = (row.get("position_id"), row.get("ts"),
                   str(row.get("raw_action") or "").strip().lower())
            text = reasons.get(key)
            if text:
                excerpts.append({"ts": row.get("ts"), "symbol": row.get("symbol"),
                                 "raw_action": row.get("raw_action"),
                                 "position_id": row.get("position_id"),
                                 "reason": text})

    return {
        "evidence_id": eid,
        "scope": SCOPE,
        "analytical_group": group,
        "status": status,
        # Sticky, so a later rebuild can tell "never was" from "no longer is".
        # Without it, a validated item that decays to nothing reads as a brand
        # new insufficient one and its staleness is silently lost.
        "was_validated": was_validated,
        "sample": sample,
        "symbol_concentration_max": concentration,
        "raw_actions": _count(r.get("raw_action") for r in rows),
        "symbols": symbols,
        "horizons": horizon_blocks,
        # KEPT, AND OPERATOR/SYSTEM-FACING ONLY: the mechanical query boundary.
        "window_start": window_start.isoformat(),
        "window_end": as_of.isoformat(),
        "oldest_supporting_decision": oldest.isoformat() if oldest else None,
        "newest_supporting_decision": newest.isoformat() if newest else None,
        "newest_supporting_decision_age_days": age_days,
        "limitations": limitations,
        # ⛔ OPERATOR-ONLY, VERBATIM, UNCLASSIFIED. Never rendered to the agent,
        # never hashed into the version, never parsed by anything here.
        "reason_excerpts": excerpts,
    }


# --------------------------------------------------------------------------- #
# version — a content hash over exactly what the agent can see
# --------------------------------------------------------------------------- #
def _version_payload(items) -> list:
    """The canonical agent-visible payload the version hashes.

    EVERYTHING THE BLOCK RENDERS IS IN HERE, AND NOTHING ELSE IS. The rolling
    `window_start`/`window_end` are excluded because the block does not show
    them: they are the mechanical 180-day query boundary and they advance every
    day, so hashing them would be a timestamp wearing a hash — churning the
    version while the agent reads text that has not changed by one character.
    What the block DOES show — the span of the supporting observations — is
    included, so a genuine change in which experiences stand behind the evidence
    always moves the version. Two different agent-visible texts can never share
    a version, and one unchanged text can never have two.
    """
    out = []
    for item in items:
        horizons = {}
        for key, block in sorted(item["horizons"].items()):
            horizons[key] = {
                "lifecycles": block["lifecycles"],
                "mean": None if block["mean"] is None else round(block["mean"], 6),
                "median": None if block["median"] is None else round(block["median"], 6),
                "p25": None if block["p25"] is None else round(block["p25"], 6),
                "p75": None if block["p75"] is None else round(block["p75"], 6),
            }
        out.append({
            "evidence_id": item["evidence_id"],
            "analytical_group": item["analytical_group"],
            "status": item["status"],
            "sample": item["sample"],
            "symbol_concentration_max": (
                None if item["symbol_concentration_max"] is None
                else round(item["symbol_concentration_max"], 6)),
            # BOTH ENDS OF THE SPAN, because both are rendered. The version's
            # contract is that two different agent-visible texts can never share
            # one -- so every value the block prints has to be in here.
            "oldest_supporting_decision": item["oldest_supporting_decision"],
            "newest_supporting_decision": item["newest_supporting_decision"],
            "horizons": horizons,
        })
    return out


def version_of(items) -> str:
    payload = json.dumps(_version_payload(items), sort_keys=True,
                         separators=(",", ":"))
    return "ev1-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# the build
# --------------------------------------------------------------------------- #
def build(measurement, *, as_of, window_days=WINDOW_DAYS, previous=None,
          reasons=None, horizons=None) -> dict:
    """Measurement -> the institutional evidence artifact. Pure; no clock.

    `as_of` is REQUIRED and supplied by the caller: every window boundary and
    every staleness age is derived from it, so this function never has to know
    what day it is and the same input always produces the same artifact.

    `previous` is the last artifact, if any. It carries exactly one fact forward
    — whether an item ever met the validated thresholds — because "no longer
    sufficient" is only distinguishable from "never was" against a prior state.
    """
    asof_date = _as_date(as_of)
    if asof_date is None:
        raise ValueError(f"as_of must be an ISO date; got {as_of!r}")
    window_start = asof_date - timedelta(days=int(window_days))

    horizons = tuple(horizons or measurement.get("horizons") or _bm.HORIZONS)

    rows = list(measurement.get("decisions") or [])
    in_window, out_of_window, undated = [], 0, 0
    for row in rows:
        d = _as_date(row.get("ts"))
        if d is None:
            undated += 1
            continue
        if window_start <= d <= asof_date:
            in_window.append(row)
        else:
            out_of_window += 1

    by_group: dict = {}
    for row in in_window:
        by_group.setdefault(row.get("analytical_group"), []).append(row)

    items = []
    for group in AGENT_GROUPS:
        group_rows = by_group.get(group, [])
        if not group_rows:
            # No experience at all in the window: reported as an empty
            # insufficient item rather than omitted, so the operator can see the
            # difference between "nothing happened" and "the group vanished".
            pass
        items.append(_item(group, group_rows, as_of=asof_date,
                           window_start=window_start, horizons=horizons,
                           previous=previous, reasons=reasons))

    items.sort(key=lambda i: (-i["sample"]["lifecycles"], i["analytical_group"]))

    validated = select(items, limit=RETRIEVAL_LIMIT)

    coverage = {
        "decisions_in_corpus": len(rows),
        "decisions_in_window": len(in_window),
        "decisions_outside_window": out_of_window,
        "decisions_undated": undated,
        # Measurable and operator-visible, never promoted. See module docstring.
        "unclassified_decisions_in_window": len(
            by_group.get(_bm.GROUP_UNCLASSIFIED, [])),
        "measurement_coverage": measurement.get("coverage", {}),
    }

    return {
        "evidence_schema": EVIDENCE_SCHEMA,
        "measurement_schema": measurement.get("measurement_schema"),
        "version": version_of(validated),
        "as_of": asof_date.isoformat(),
        "window": {"days": int(window_days),
                   "start": window_start.isoformat(),
                   "end": asof_date.isoformat()},
        "horizons": [f"{h}d" for h in horizons],
        "gate_horizon": f"{GATE_HORIZON}d",
        "thresholds": {"candidate": dict(CANDIDATE_MIN),
                       "validated": dict(VALIDATED_MIN),
                       "max_symbol_share": MAX_SYMBOL_SHARE,
                       "stale_after_days": STALE_AFTER_DAYS},
        "agent_visible_evidence_ids": [i["evidence_id"] for i in validated],
        "coverage": coverage,
        "items": items,
    }


# --------------------------------------------------------------------------- #
# retrieval — validated only, deterministic, no search of any kind
# --------------------------------------------------------------------------- #
def select(evidence_or_items, limit=RETRIEVAL_LIMIT) -> list:
    """The validated items a session may see, in deterministic order.

    No embedding, no vector store, no relevance model. The agent is managing
    positions, so every validated position-management group is potentially
    relevant and there are at most four of them; ranking them by anything
    cleverer than sample size would be a judgement this layer is not allowed to
    make. Candidate, insufficient and stale items are NOT returned — a weak
    observation shown to the agent is indistinguishable, at the point of use,
    from a strong one.
    """
    items = (evidence_or_items.get("items", [])
             if isinstance(evidence_or_items, dict) else list(evidence_or_items or []))
    validated = [i for i in items if i.get("status") == STATUS_VALIDATED]
    validated.sort(key=lambda i: (-i["sample"]["lifecycles"], i["analytical_group"]))
    return validated[:int(limit)]


# --------------------------------------------------------------------------- #
# the agent-facing block
# --------------------------------------------------------------------------- #
def _pct1(value):
    return "  n/a" if not isinstance(value, (int, float)) else f"{value * 100:+.1f}"


def _horizon_cell(block) -> str:
    if not block or not block.get("lifecycles"):
        # The sample counts and the range are REQUIRED output; when a horizon has
        # no measured lifecycle observation the honest cell says so rather than
        # borrowing a neighbouring horizon's number.
        return "not yet measurable"
    return (f"{_pct1(block['median'])} "
            f"[{_pct1(block['p25'])},{_pct1(block['p75'])}]")


def render_agent_block(evidence_or_items) -> str:
    """The compact read-only section, or "" when there is nothing validated.

    ⛔ ABSENCE IS RENDERED AS ABSENCE. With no validated evidence this returns
    the empty string and the session context gets no section at all — not a
    "no evidence available" line. A stateless agent told there is no evidence has
    been told something, and what it would infer from that is not a fact we have.

    ⛔ NO VERDICT VOCABULARY. The block states counts and distributions and
    stops. `check_block` enforces that against the rendered text, because an
    adjective added here months from now would reach the agent as institutional
    truth without anything else in the system noticing.
    """
    items = select(evidence_or_items) if isinstance(evidence_or_items, dict) \
        else list(evidence_or_items or [])
    if not items:
        return ""

    lines = [BLOCK_HEADING,
             "What the price did after past lifecycle-linked decisions of each "
             "exposure class. Percent move over N sessions, median [p25,p75].",
             ""]
    for item in items:
        s = item["sample"]
        # ⛔ THE OBSERVATION SPAN, NEVER THE ROLLING WINDOW. The window is where
        # the builder looked; the span is where the evidence is, and it is the
        # only one of the two that says anything about the experience itself.
        span = f"{item['oldest_supporting_decision']}..{item['newest_supporting_decision']}"
        lines.append(
            f"{item['analytical_group']} — decisions {s['decisions']}, "
            f"lifecycles {s['lifecycles']}, symbols {s['symbols']}; "
            f"observations {span}")
        cells = "   ".join(
            f"{h}s {_horizon_cell(item['horizons'].get(f'{h}d'))}"
            for h in BLOCK_HORIZONS)
        lines.append(f"  {cells}")
    lines += ["", BLOCK_WARNING]
    return "\n".join(lines) + "\n"


def check_block(text: str) -> list:
    """Ways a rendered block violates its own contract. Empty is good."""
    problems = []
    if len(text) > BLOCK_MAX_CHARS:
        problems.append(f"block is {len(text)} chars, over the {BLOCK_MAX_CHARS} cap")
    lowered = text.lower()
    for word in FORBIDDEN_BLOCK_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            problems.append(f"verdict vocabulary in an evidence block: {word!r}")
    if text and BLOCK_WARNING not in text:
        problems.append("the block does not carry the required warning verbatim")
    return problems


def provenance(evidence, items=None) -> dict | None:
    """{evidence_version, evidence_ids_seen} for a delivered block, else None.

    None means NO BLOCK WAS DELIVERED, and the caller must omit both fields
    entirely rather than write them as null — an explicit null would claim the
    session was offered evidence and saw none, which is a different fact.
    """
    chosen = select(evidence) if items is None else list(items)
    if not chosen:
        return None
    return {"evidence_version": evidence.get("version") if isinstance(evidence, dict)
            else version_of(chosen),
            "evidence_ids_seen": [i["evidence_id"] for i in chosen]}


# --------------------------------------------------------------------------- #
# the operator view — every status, including the ones the agent never sees
# --------------------------------------------------------------------------- #
def render_operator(evidence) -> str:
    w = evidence["window"]
    out = ["INSTITUTIONAL EVIDENCE — OPERATOR VIEW",
           f"as_of {evidence['as_of']}  window {w['start']}..{w['end']} "
           f"({w['days']}d)  version {evidence['version']}",
           ""]
    cov = evidence["coverage"]
    out += [f"decisions in corpus {cov['decisions_in_corpus']}  "
            f"in window {cov['decisions_in_window']}  "
            f"outside {cov['decisions_outside_window']}  "
            f"undated {cov['decisions_undated']}  "
            f"unclassified {cov['unclassified_decisions_in_window']}", ""]

    for item in evidence["items"]:
        s = item["sample"]
        conc = item["symbol_concentration_max"]
        out.append(f"[{item['status'].upper()}] {item['evidence_id']}")
        out.append(f"    decisions {s['decisions']}  lifecycles {s['lifecycles']}  "
                   f"symbols {s['symbols']}  "
                   f"max symbol share "
                   f"{'—' if conc is None else format(conc * 100, '.0f') + '%'}  "
                   f"observations {item['oldest_supporting_decision']}.."
                   f"{item['newest_supporting_decision']}")
        for key in evidence["horizons"]:
            b = item["horizons"].get(key, {})
            out.append(f"      {key:>4}  lifecycles {b.get('lifecycles', 0):>3}  "
                       f"median {_pct1(b.get('median')):>7}  "
                       f"p25 {_pct1(b.get('p25')):>7}  p75 {_pct1(b.get('p75')):>7}  "
                       f"mean {_pct1(b.get('mean')):>7}  "
                       f"(measured {b.get('decisions_measured', 0)} / "
                       f"pending {b.get('decisions_pending', 0)} / "
                       f"unavailable {b.get('decisions_unavailable', 0)} decisions)")
        for note in item["limitations"][3:]:
            out.append(f"      note: {note}")
        out.append("")

    ids = evidence["agent_visible_evidence_ids"]
    out.append(f"agent-visible: {', '.join(ids) if ids else 'NOTHING (no block is rendered)'}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# selftest
# --------------------------------------------------------------------------- #
def _selftest() -> None:
    """Exercise the aggregation on events built by THE REAL WRITERS.

    ⛔ THIS IS NOT LIVE DATA AND PROVES NOTHING ABOUT THE LIVE STREAM. Every
    lifecycle below comes from `position_lifecycle.diff` and every decision from
    `agent_env.decide.decision_entry`, so the SHAPES are production's and a field
    rename upstream breaks this test — but the values are invented, they live
    only in memory, and nothing is written anywhere. What it checks is the
    lifecycle weighting, the sufficiency gate, the concentration cap, staleness,
    the stability of the id against the movement of the version, and the refusal
    to render anything at all when nothing is validated.
    """
    from datetime import timedelta as _td                       # noqa: PLC0415
    import position_lifecycle as lc                             # noqa: PLC0415
    from agent_env.decide import decision_entry                 # noqa: PLC0415

    as_of = "2026-08-21"
    sessions, d = [], date(2026, 1, 2)
    while d <= date(2026, 8, 21):
        if d.weekday() < 5:
            sessions.append(d.isoformat())
        d += _td(days=1)
    # A deterministic zigzag, so the distribution straddles zero and no reading
    # of these numbers as "this action works" can survive.
    def series(seed):
        px, out = 100.0, []
        for i, _s in enumerate(sessions):
            out.append(round(px, 4))
            px *= 1.0 + (0.004 if (i + seed) % 3 else -0.006)
        return out
    syms = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III"]
    panel = {"sessions": sessions,
             "series": {s: series(i) for i, s in enumerate(syms)}}

    def one(symbol, opened_on, closed_on, decisions):
        opened = lc.diff(prior={}, new={symbol: {"qty": "2", "avg_cost": "100"}},
                         open_lifecycles={}, observed_at=f"{opened_on}T20:05:00+00:00",
                         orders=[{"id": f"o-{symbol}-{opened_on}", "symbol": symbol,
                                  "side": "buy", "state": "filled", "quantity": "2",
                                  "average_price": "100",
                                  "last_transaction_at": f"{opened_on}T14:00:00+00:00"}],
                         account_number="T")
        pid = opened[0]["position_id"]
        active = {symbol: {"position_id": pid, "origin": opened[0]["origin"],
                           "opened_on": opened[0]["opened_on"]}}
        closed = lc.diff(prior={symbol: {"qty": "2", "avg_cost": "100"}}, new={},
                         open_lifecycles=active,
                         observed_at=f"{closed_on}T20:05:00+00:00",
                         orders=[{"id": f"o-{symbol}-{closed_on}-x", "symbol": symbol,
                                  "side": "sell", "state": "filled", "quantity": "2",
                                  "average_price": "110",
                                  "last_transaction_at": f"{closed_on}T18:00:00+00:00"}],
                         account_number="T")
        dec = [decision_entry(symbol, a, w, f"{t}T14:40:00+00:00", position_id=pid)
               for t, a, w in decisions]
        return opened + dec + closed, pid

    def build_from(journal, previous=None, asof=as_of):
        return build(_bm.measure(journal, panel, as_of=asof), as_of=asof,
                     previous=previous)

    def pick(ev, group):
        return next(i for i in ev["items"] if i["analytical_group"] == group)

    # ---- nothing at all: every group reported, nothing rendered ------------
    empty = build_from([])
    assert len(empty["items"]) == len(AGENT_GROUPS), empty["items"]
    assert all(i["status"] == STATUS_INSUFFICIENT for i in empty["items"])
    assert select(empty) == []
    assert render_agent_block(empty) == "", "absence must render as absence"
    assert provenance(empty) is None, "no block -> None, never an empty object"

    # ---- three lifecycles: insufficient, and invisible to the agent --------
    thin = []
    for s, o in [("AAA", "2026-07-06"), ("BBB", "2026-07-07"), ("CCC", "2026-07-08")]:
        ev, _ = one(s, o, "2026-08-14", [("2026-07-13", "hold", "leading")])
        thin += ev
    ev_thin = build_from(thin)
    assert pick(ev_thin, _bm.GROUP_HOLD)["status"] == STATUS_INSUFFICIENT
    assert render_agent_block(ev_thin) == ""

    # ---- ten lifecycles, nine symbols: validated, and RENDERED -------------
    plan = [("AAA", "2026-06-15", [("2026-07-08", "hold", "leader"),
                                   ("2026-07-13", "hold", "trend intact"),
                                   ("2026-07-16", "hold", "above the 20d"),
                                   ("2026-07-21", "hold", "above its stop")]),
            ("BBB", "2026-06-16", [("2026-07-09", "hold", "unchanged")]),
            ("CCC", "2026-06-17", [("2026-07-10", "hold", "consolidating")]),
            ("DDD", "2026-06-18", [("2026-07-13", "hold", "earnings past")]),
            ("EEE", "2026-06-19", [("2026-07-14", "hold", "sector bid")]),
            ("FFF", "2026-06-22", [("2026-07-15", "hold", "no breach")]),
            ("GGG", "2026-06-23", [("2026-07-16", "hold", "rank improved")]),
            ("HHH", "2026-06-24", [("2026-07-17", "hold", "inside the band")]),
            ("AAA", "2026-07-06", [("2026-07-20", "hold", "same thesis")]),
            ("III", "2026-07-07", [("2026-07-21", "hold", "quiet")])]
    full, repeat_pid = [], None
    for s, o, decs in plan:
        ev, pid = one(s, o, "2026-08-20", decs)
        if s == "AAA" and o == "2026-06-15":
            repeat_pid = pid
        full += ev
    good = build_from(full)
    item = pick(good, _bm.GROUP_HOLD)
    assert item["status"] == STATUS_VALIDATED, item["status"]

    # ⛔ THE LOAD-BEARING ONE. Four HOLDs on one position are four DECISIONS and
    # exactly ONE lifecycle observation, whose value is their median.
    assert item["sample"] == {"decisions": 13, "lifecycles": 10, "symbols": 9}, item["sample"]
    assert item["horizons"]["10d"]["lifecycles"] == 10, item["horizons"]["10d"]
    rows = [r for r in _bm.measure(full, panel, as_of=as_of)["decisions"]
            if r["analytical_group"] == _bm.GROUP_HOLD]
    obs = _lifecycle_observations(rows, 10)
    mine = [r["return_10d"] for r in rows if r["position_id"] == repeat_pid]
    assert len(mine) == 4 and abs(obs[repeat_pid] - statistics.median(mine)) < 1e-12

    block = render_agent_block(good)
    assert block and check_block(block) == [], check_block(block)
    assert len(block) <= BLOCK_TARGET_CHARS, len(block)
    assert BLOCK_HEADING in block and BLOCK_WARNING in block
    for required in ("decisions 13", "lifecycles 10", "symbols 9"):
        assert required in block, f"sample counts must survive the size cap: {required}"
    # The block shows the OBSERVATION SPAN and never the rolling query window.
    span = (f"observations {item['oldest_supporting_decision']}.."
            f"{item['newest_supporting_decision']}")
    assert span in block, block
    assert item["window_start"] not in block, "the query window must not be rendered"
    assert "window" not in block.lower(), "the query window must not be rendered"
    prov = provenance(good)
    assert prov == {"evidence_version": good["version"],
                    "evidence_ids_seen": ["position_management:hold_exposure"]}, prov

    # ---- concentration: numerically validated, held at candidate ----------
    conc = []
    for i, (s, o) in enumerate([("AAA", "2026-06-15"), ("AAA", "2026-07-06"),
                                ("AAA", "2026-07-13"), ("AAA", "2026-07-20"),
                                ("BBB", "2026-06-16"), ("CCC", "2026-06-17"),
                                ("DDD", "2026-06-18"), ("EEE", "2026-06-19"),
                                ("FFF", "2026-06-22")]):
        day = (date.fromisoformat("2026-07-08") + _td(days=i)).isoformat()
        ev, _ = one(s, o, "2026-08-20", [(day, "trim", "first target"),
                                         (day, "trim", "second tranche")])
        conc += ev
    ce = pick(build_from(conc), _bm.GROUP_REDUCE)
    assert ce["sample"]["lifecycles"] >= VALIDATED_MIN["lifecycles"]
    assert ce["sample"]["decisions"] >= VALIDATED_MIN["decisions"]
    assert ce["symbol_concentration_max"] > MAX_SYMBOL_SHARE, ce["symbol_concentration_max"]
    assert ce["status"] == STATUS_CANDIDATE, "concentration must block promotion"
    assert render_agent_block(build_from(conc)) == "", "candidate is never agent-facing"

    # ---- staleness, both clauses -----------------------------------------
    shifted = []
    for s, o, decs in plan:
        o2 = (date.fromisoformat(o) - _td(days=70)).isoformat()
        d2 = [((date.fromisoformat(t) - _td(days=70)).isoformat(), a, w)
              for t, a, w in decs]
        ev, _ = one(s, o2, "2026-06-11", d2)
        shifted += ev
    old = pick(build_from(shifted), _bm.GROUP_HOLD)
    assert old["newest_supporting_decision"] >= build_from(shifted)["window"]["start"]
    assert old["newest_supporting_decision_age_days"] > STALE_AFTER_DAYS
    assert old["status"] == STATUS_STALE, old["status"]
    assert render_agent_block(build_from(shifted)) == "", "stale is never agent-facing"
    assert old["evidence_id"] == item["evidence_id"], "stale evidence is not deleted"

    decayed = pick(build_from(thin, previous=good), _bm.GROUP_HOLD)
    assert decayed["status"] == STATUS_STALE, "decay below candidate reads stale"
    assert pick(build_from(thin), _bm.GROUP_HOLD)["status"] == STATUS_INSUFFICIENT, \
        "...and without that history it is insufficient, not stale"

    # ---- id stable, version moves with content and NOT with the calendar --
    more, _ = one("III", "2026-07-06", "2026-08-20",
                  [("2026-07-23", "hold", "new position")])
    grown = build_from(full + more)
    assert pick(grown, _bm.GROUP_HOLD)["evidence_id"] == item["evidence_id"]
    assert grown["version"] != good["version"], "new experience must move the version"
    assert build_from(full)["version"] == good["version"], "same input -> same version"
    assert render_agent_block(grown) != block, \
        "a changed observation set must change the rendered text"

    # ---- A: as_of advances, supporting observations identical -------------
    # The 180-day boundary moves, `window_start` moves with it, and the agent
    # reads text that has not changed by one character -- so the version must
    # not move either. This is the churn the span-not-window rule exists to stop.
    later = build_from(full, asof="2026-08-25")
    assert pick(later, _bm.GROUP_HOLD)["window_start"] != item["window_start"], \
        "the rolling window did not actually advance; the check would be vacuous"
    assert render_agent_block(later) == block, "the agent-visible text changed"
    assert later["version"] == good["version"], \
        "the version moved while the delivered text did not"

    # ---- B: the span itself changes -> text changes -> version changes -----
    span_moved = build_from(full + more)
    moved_item = pick(span_moved, _bm.GROUP_HOLD)
    assert moved_item["newest_supporting_decision"] != item["newest_supporting_decision"]
    assert render_agent_block(span_moved) != block, "the delivered text must change"
    assert span_moved["version"] != good["version"], "the version must change with it"

    # ---- the window is a filter, not a suggestion -------------------------
    stale_only = build_from(shifted, asof="2027-06-01")
    assert pick(stale_only, _bm.GROUP_HOLD)["sample"]["decisions"] == 0, \
        "experience outside the 180d window contributes nothing"
    assert stale_only["coverage"]["decisions_outside_window"] == 13

    print("institutional_evidence: OK — lifecycle-weighted, gated, stale-aware, "
          "and silent when it has nothing validated to say")


if __name__ == "__main__":
    _selftest()
