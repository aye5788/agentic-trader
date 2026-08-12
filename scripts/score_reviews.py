#!/usr/bin/env python3
"""SCORE the agent AND its reviewer against what the market actually did.

    .venv/bin/python scripts/score_reviews.py [--horizon 5] [--selftest]

WHY THIS EXISTS
    A reviewer whose verdicts are never checked is decoration. So is an agent
    whose reasoning is never priced. Both write persuasive prose; only one of
    them can be right about any given trade, and the tape settles it.

    This does NOT ask either model to grade itself, and it does not ask me to
    grade them — I am the same model as the agent and found its reasoning
    persuasive this morning for exactly that reason. It reads the price panel.

HOW A DECISION IS SCORED
    Every decision the agent records has a direction. An `exit` or `trim` says
    "this will do worse than the alternative"; a `hold` or `add` says the
    opposite. `horizon` trading days later, the panel says what happened.

      exit/trim  -> RIGHT if the name fell, WRONG if it rose
      hold/add   -> RIGHT if the name rose, WRONG if it fell

    The reviewer is scored on the SAME event, from its stance: an AFFIRM is a
    bet that the agent was right, a DISSENT that it was wrong. So a dissent that
    proves correct scores against the agent and for the reviewer, and vice
    versa. Neither can win by being vague.

WHAT IT DELIBERATELY REFUSES TO SCORE
    Portfolio-level findings, and decisions on symbols the panel cannot price.
    Coverage is reported honestly: a scorecard that quietly drops what it cannot
    measure reads as a full record and is not one. `unscoreable` is a first-class
    number here, not an omission.

    ⚠️ It is a MEASUREMENT, not a verdict on skill. Five days of price action on
    a handful of trades is noise; the number is worth reading as a running tally
    over months, and worth ignoring over a week. It exists so that a systematic
    bias — an agent that only ever reduces, a reviewer that only ever affirms —
    becomes visible as a count instead of an argument.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

JOURNAL = REPO / "research_store" / "journal.jsonl"
PANEL = REPO / "research_store" / "prices" / "closes.parquet"
OUT = REPO / "research_store" / "reviews" / "scorecard.json"

REDUCE = {"exit", "trim", "sell", "derisk", "derisk_concentration", "reduce"}
INCREASE = {"hold", "add", "buy", "open", "increase", "keep"}


def direction(action: str) -> str | None:
    """Which way did this decision bet? -> 'reduce' | 'increase' | None."""
    a = str(action or "").strip().lower()
    if a in REDUCE:
        return "reduce"
    if a in INCREASE:
        return "increase"
    return None


def forward_return(closes: pd.DataFrame, symbol: str, when: str,
                   horizon: int) -> float | None:
    """Return from `when` to `horizon` TRADING rows later. None if unmeasurable.

    Trading rows, not calendar days: the panel has no weekend rows, so indexing
    by position is what "5 sessions later" actually means. Returns None rather
    than a partial figure when the horizon has not elapsed — a decision scored
    early is scored on noise, and would let a bad call look good for a day.
    """
    if closes is None or closes.empty or symbol not in closes.columns:
        return None
    s = closes[symbol].dropna()
    if s.empty:
        return None
    try:
        cut = pd.Timestamp(str(when)[:10])
    except Exception:                       # noqa: BLE001
        return None
    at = s.index.searchsorted(cut)
    if at >= len(s):
        return None
    end = at + horizon
    if end >= len(s):
        return None                          # horizon has not elapsed yet
    a, b = float(s.iloc[at]), float(s.iloc[end])
    return None if a <= 0 else (b / a) - 1.0


def score_decision(action: str, ret: float | None, dead_band: float = 0.01) -> str:
    """-> 'agent_right' | 'agent_wrong' | 'tie' | 'unscoreable'.

    `dead_band` exists because a 0.3% move does not vindicate anybody. Without
    it, noise gets recorded as skill in whichever direction it happened to fall,
    and a tally built from that is worse than no tally.
    """
    d = direction(action)
    if d is None or ret is None:
        return "unscoreable"
    if abs(ret) < dead_band:
        return "tie"
    rose = ret > 0
    if d == "reduce":
        return "agent_wrong" if rose else "agent_right"
    return "agent_right" if rose else "agent_wrong"


def score_reviewer(stance: str, agent_outcome: str) -> str:
    """The reviewer bet on the agent (AFFIRM) or against it (DISSENT)."""
    st = str(stance or "").upper()
    if agent_outcome not in ("agent_right", "agent_wrong") or st not in ("AFFIRM", "DISSENT"):
        return "unscoreable"
    agent_won = agent_outcome == "agent_right"
    if st == "AFFIRM":
        return "reviewer_right" if agent_won else "reviewer_wrong"
    return "reviewer_wrong" if agent_won else "reviewer_right"


def _load(path: Path) -> list:
    rows = []
    try:
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:               # noqa: BLE001
                continue
    except Exception:                       # noqa: BLE001
        pass
    return rows


def build(rows: list, closes: pd.DataFrame, horizon: int) -> dict:
    """The scorecard. Pure: no I/O, so it can be tested without a live panel."""
    stance_by_day = {}
    for e in rows:
        if e.get("event") == "codex_review":
            stance_by_day[str(e.get("ts", ""))[:10]] = e.get("stance")

    tally = {"agent_right": 0, "agent_wrong": 0, "tie": 0, "unscoreable": 0}
    rev = {"reviewer_right": 0, "reviewer_wrong": 0, "unscoreable": 0}
    bias = {"reduce": 0, "increase": 0, "other": 0}
    detail = []

    for e in rows:
        if e.get("event") != "agent_decision":
            continue
        sym, act = e.get("symbol"), e.get("action")
        day = str(e.get("ts", ""))[:10]

        d = direction(act)
        bias["reduce" if d == "reduce" else "increase" if d == "increase" else "other"] += 1

        # PORTFOLIO is a finding, not a position — it has no price to settle it
        ret = None if sym == "PORTFOLIO" else forward_return(closes, sym, day, horizon)
        outcome = score_decision(act, ret)
        tally[outcome] += 1

        r = score_reviewer(stance_by_day.get(day), outcome)
        rev[r] += 1

        detail.append({"day": day, "symbol": sym, "action": act,
                       "forward_return": None if ret is None else round(ret, 4),
                       "outcome": outcome, "reviewer": r,
                       "stance": stance_by_day.get(day)})

    scored = tally["agent_right"] + tally["agent_wrong"]
    rscored = rev["reviewer_right"] + rev["reviewer_wrong"]
    total_dir = bias["reduce"] + bias["increase"]
    return {
        "horizon_sessions": horizon,
        "agent": {**tally,
                  "scored": scored,
                  "hit_rate": round(tally["agent_right"] / scored, 3) if scored else None},
        "reviewer": {**rev, "scored": rscored,
                     "hit_rate": round(rev["reviewer_right"] / rscored, 3) if rscored else None},
        # ⚠️ THE BIAS COUNT IS THE POINT. An agent that only ever reduces is
        # visible here in one line, whatever its prose says, and no amount of
        # well-written caution moves the number.
        "direction": {**bias,
                      "reduce_share": round(bias["reduce"] / total_dir, 3) if total_dir else None},
        "decisions": detail,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=5,
                    help="trading sessions to wait before scoring a decision")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
        return

    closes = pd.read_parquet(PANEL) if PANEL.exists() else pd.DataFrame()
    card = build(_load(JOURNAL), closes, a.horizon)
    card["as_of"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(card, indent=2))

    ag, rv, dr = card["agent"], card["reviewer"], card["direction"]
    print(f"scorecard @ {a.horizon} sessions -> {OUT}")
    print(f"  agent    : {ag['agent_right']} right / {ag['agent_wrong']} wrong "
          f"({ag['scored']} scored, {ag['tie']} tie, {ag['unscoreable']} unscoreable)"
          + (f"  hit {ag['hit_rate']:.0%}" if ag["hit_rate"] is not None else ""))
    print(f"  reviewer : {rv['reviewer_right']} right / {rv['reviewer_wrong']} wrong"
          + (f"  hit {rv['hit_rate']:.0%}" if rv["hit_rate"] is not None else ""))
    print(f"  direction: {dr['reduce']} reduce / {dr['increase']} increase"
          + (f"  ({dr['reduce_share']:.0%} of decisions cut exposure)"
             if dr["reduce_share"] is not None else ""))
    total = dr["reduce"] + dr["increase"]
    if dr["reduce_share"] is not None and dr["reduce_share"] >= 0.8 and total:
        print(f"  ⚠️ {dr['reduce_share']:.0%} of {total} decisions reduced exposure — "
              f"check this is the tape, not a standing disposition.")


def _selftest() -> None:
    assert direction("exit") == "reduce" and direction("trim") == "reduce"
    assert direction("hold") == "increase" and direction("buy") == "increase"
    assert direction("derisk_concentration") == "reduce"
    assert direction("noted") is None

    # a sell is right when the name FALLS, wrong when it rises
    assert score_decision("exit", -0.08) == "agent_right"
    assert score_decision("exit", +0.08) == "agent_wrong"
    # a hold is the mirror
    assert score_decision("hold", +0.08) == "agent_right"
    assert score_decision("hold", -0.08) == "agent_wrong"
    # noise is NOT skill in either direction
    assert score_decision("exit", 0.003) == "tie"
    assert score_decision("hold", -0.004) == "tie"
    assert score_decision("exit", None) == "unscoreable"
    assert score_decision("pondered", 0.5) == "unscoreable"

    # the reviewer is scored on the same event, from its own stance
    assert score_reviewer("AFFIRM", "agent_right") == "reviewer_right"
    assert score_reviewer("AFFIRM", "agent_wrong") == "reviewer_wrong"
    assert score_reviewer("DISSENT", "agent_wrong") == "reviewer_right"
    assert score_reviewer("DISSENT", "agent_right") == "reviewer_wrong"
    assert score_reviewer("SPLIT", "agent_right") == "unscoreable"
    assert score_reviewer("AFFIRM", "tie") == "unscoreable"

    idx = pd.date_range("2026-08-03", periods=12, freq="B")
    # index 0..4 = 100, index 5+ = 110, so exactly FIVE sessions on is +10%.
    # (The first draft used [100]*6, which leaves session 5 still at 100 and
    # made the fixture, not the code, wrong.)
    closes = pd.DataFrame({"MU": [100.0] * 5 + [110.0] * 7}, index=idx)
    assert abs(forward_return(closes, "MU", "2026-08-03", 5) - 0.10) < 1e-9
    # horizon not yet elapsed -> None, never a partial score
    assert forward_return(closes, "MU", idx[-1].date().isoformat(), 5) is None
    assert forward_return(closes, "NOPE", "2026-08-03", 5) is None
    assert forward_return(pd.DataFrame(), "MU", "2026-08-03", 5) is None

    # end to end: a trim into a name that then RALLIED is scored against the
    # agent, and an AFFIRM on it is scored against the reviewer
    rows = [
        {"event": "agent_decision", "ts": "2026-08-03T14:00:00+00:00",
         "symbol": "MU", "action": "trim"},
        {"event": "agent_decision", "ts": "2026-08-03T14:00:00+00:00",
         "symbol": "PORTFOLIO", "action": "derisk_concentration"},
        {"event": "codex_review", "ts": "2026-08-03T15:00:00+00:00", "stance": "AFFIRM"},
    ]
    card = build(rows, closes, 5)
    assert card["agent"]["agent_wrong"] == 1, card["agent"]
    assert card["reviewer"]["reviewer_wrong"] == 1, card["reviewer"]
    # PORTFOLIO has no price to settle it and must NOT be quietly dropped
    assert card["agent"]["unscoreable"] == 1, card["agent"]
    # ...but it still counts toward the direction tally: it was a decision to cut
    assert card["direction"]["reduce"] == 2, card["direction"]
    assert card["direction"]["reduce_share"] == 1.0, card["direction"]

    print("score_reviews: OK -- both parties scored off the PANEL, noise is a "
          "tie not skill, unscoreable is counted not hidden")


if __name__ == "__main__":
    main()
