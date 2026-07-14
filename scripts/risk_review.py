"""RISK REVIEW — deterministic core for the intraday risk-management overlay.

Builds per-position facts for the two scheduled agentic reviews, validates every
proposed change against a hard ONE-WAY (risk-reducing only) invariant, and writes
stricter-only geometry overrides + deferred intents the monitor enforces. No LLM,
no order placement here — that is prompts/risk_review.md's job. See
docs/superpowers/specs/2026-07-14-intraday-risk-management-design.md.

    .venv/bin/python scripts/risk_review.py --selftest
"""
import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

OVERRIDES = REPO / "research_store" / "monitor" / "overrides.json"
INTENTS = REPO / "research_store" / "monitor" / "deferred_intents.json"
FACTS = REPO / "research_store" / "rh" / "risk_review_facts.json"
DECISIONS = REPO / "research_store" / "rh" / "risk_review_decisions.json"

_ACTION_KINDS = {"hold", "tighten_stop", "lower_tp", "trim", "exit", "watch"}


def validate_geometry(current: dict, proposed: dict, *, entry_low=None) -> tuple[dict, list[str]]:
    """Keep only strictly risk-reducing edits. Stop may move UP (>=) but must stay
    STRICTLY BELOW entry (the [risk] mandate's stop-below-entry rule — passing
    entry_low re-guards it without a full re-validate). Targets may only move IN
    (each <= the same-index current target). Anything else is dropped with a
    reason. Missing fields in `proposed` are simply not changed."""
    accepted, rejections = {}, []
    cur_stop = current.get("stop")
    if "stop" in proposed and proposed["stop"] is not None:
        ps = float(proposed["stop"])
        if cur_stop is not None and ps < cur_stop:
            rejections.append(f"stop {ps} < current {cur_stop} — loosening rejected")
        elif entry_low is not None and ps >= entry_low:
            rejections.append(f"stop {ps} >= entry_low {entry_low} — must stay below entry")
        else:
            accepted["stop"] = ps
    cur_t = current.get("targets") or []
    if "targets" in proposed and proposed["targets"] is not None:
        pt = proposed["targets"]
        if len(pt) == len(cur_t) and all(p <= c for p, c in zip(pt, cur_t)):
            accepted["targets"] = [float(x) for x in pt]
        else:
            rejections.append(f"targets {pt} not all <= current {cur_t} — extending rejected")
    return accepted, rejections


def validate_action(action: dict) -> tuple[bool, str | None]:
    """An action may only ever de-risk. Reject any non-de-risk kind (e.g. an
    attempted entry) and any nonsensical trim fraction."""
    kind = action.get("kind")
    if kind not in _ACTION_KINDS:
        return False, f"action kind {kind!r} is not a de-risk action"
    if kind == "trim":
        f = action.get("fraction")
        if not (isinstance(f, (int, float)) and 0.0 < f < 1.0):
            return False, f"trim fraction {f!r} must be in (0,1)"
    return True, None


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _atomic_write(path: Path, text: str) -> None:
    """Write via temp-file + os.replace so the always-on 15s monitor never reads a
    half-written overrides.json. A torn read would make the monitor drop ALL
    overrides for that tick (not just the one being written) — exactly the wrong
    moment during a fast breakdown. os.replace is atomic on POSIX."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def read_overrides(path: Path = OVERRIDES) -> dict:
    """Active geometry overrides, pruning any past their `expires` date."""
    ov = _read_json(path, {})
    today = date.today().isoformat()
    live = {s: o for s, o in ov.items() if str(o.get("expires", "9999")) >= today}
    if live != ov:
        _atomic_write(path, json.dumps(live, indent=2))
    return live


def write_override(sym: str, accepted: dict, reason: str, expires: str,
                   *, path: Path = OVERRIDES) -> None:
    """Merge one name's ALREADY-VALIDATED geometry (from validate_geometry) into
    the overrides file. Caller must pass only accepted (stricter) fields."""
    ov = _read_json(path, {})
    ov[sym] = {**accepted, "reason": reason, "expires": expires,
               "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _atomic_write(path, json.dumps(ov, indent=2))


def read_intents(path: Path = INTENTS) -> list:
    ints = _read_json(path, [])
    today = date.today().isoformat()
    return [i for i in ints if str(i.get("expires", "9999")) >= today]


def append_intent(intent: dict, *, path: Path = INTENTS) -> None:
    ints = _read_json(path, [])
    ints.append({**intent, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    _atomic_write(path, json.dumps(ints, indent=2))


def _selftest() -> None:
    # --- one-way geometry: stop may only move UP, targets only pull IN ---
    acc, rej = validate_geometry({"stop": 100.0, "targets": [120.0, 140.0]},
                                 {"stop": 105.0, "targets": [118.0, 135.0]})
    assert acc == {"stop": 105.0, "targets": [118.0, 135.0]}, acc
    assert rej == [], rej

    acc, rej = validate_geometry({"stop": 100.0, "targets": [120.0, 140.0]},
                                 {"stop": 95.0, "targets": [125.0, 140.0]})
    assert "stop" not in acc, acc            # loosening the stop is dropped
    assert "targets" not in acc, acc         # raising a target is dropped
    assert len(rej) == 2, rej

    acc, rej = validate_geometry({"stop": 100.0, "targets": [120.0, 140.0]},
                                 {"stop": 100.0})    # equal stop = no-op, allowed but harmless
    assert acc.get("stop") == 100.0

    # a stop may rise but must stay STRICTLY BELOW entry (the [risk] mandate's
    # stop-below-entry rule — see Self-Review; folded in here, not deferred)
    acc, rej = validate_geometry({"stop": 100.0, "targets": [120.0]},
                                 {"stop": 118.0}, entry_low=115.0)
    assert "stop" not in acc and len(rej) == 1, (acc, rej)

    # --- actions: only the de-risk kinds; never an entry; trim fraction sane ---
    assert validate_action({"symbol": "X", "kind": "exit"}) == (True, None)
    ok, why = validate_action({"symbol": "X", "kind": "buy"})
    assert ok is False and "buy" in why
    ok, why = validate_action({"symbol": "X", "kind": "trim", "fraction": 1.5})
    assert ok is False
    assert validate_action({"symbol": "X", "kind": "trim", "fraction": 0.5}) == (True, None)

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "overrides.json"
        write_override("NVDA", {"stop": 105.0}, "trail up", "2026-07-18", path=p)
        got = read_overrides(path=p)
        assert got["NVDA"]["stop"] == 105.0 and got["NVDA"]["reason"] == "trail up", got
        # an already-expired override is pruned on read
        write_override("OLD", {"stop": 1.0}, "stale", "2000-01-01", path=p)
        assert "OLD" not in read_overrides(path=p)
        ip = Path(d) / "intents.json"
        append_intent({"symbol": "AMD", "note": "watch 21d reclaim", "expires": "2026-07-18"}, path=ip)
        assert read_intents(path=ip)[0]["symbol"] == "AMD"
    print("selftest OK: one-way geometry + action validation")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        return


if __name__ == "__main__":
    main()
