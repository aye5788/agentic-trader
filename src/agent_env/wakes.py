"""Agent-registered wakes — the agent asking to be woken when something happens.

Fixed premarket/open/close sessions mean the agent can only act at three moments
it did not choose. A wake lets it say "if MU trades below 640, wake me" and then
stop watching, which is the difference between a schedule and attention.

⚠️ A WAKE IS NOT A STOP, and this distinction is the whole safety story. A stop
is enforced by scripts/market_monitor.py, which places the order itself and needs
no agent. A wake only STARTS A SESSION; the agent then decides, and may decide to
do nothing. Never register a wake in place of a stop -- if the intent is "get me
out at X", that is set_levels, not this. The tool docstring says so too.

Bounded on purpose:
  - `budget`  how many times this wake may ever fire, so a flapping condition
              cannot spawn unbounded sessions (each costs tokens and holds the
              trading lock).
  - `ttl_days` wakes expire. A condition that mattered last month is noise now,
              and an unbounded registry silently becomes one.
  - `MAX_ACTIVE` a hard ceiling on registrations, so a loop cannot fill the file.

Pure given (now): every function takes the clock, so expiry and firing are
selftestable without waiting.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

WAKES_FILE = "wakes.json"
MAX_ACTIVE = 20
DEFAULT_TTL_DAYS = 5
DEFAULT_BUDGET = 3
DIRECTIONS = ("above", "below")


def _now(now=None) -> datetime:
    return now or datetime.now(timezone.utc)


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}                       # torn/corrupt -> behave as empty, never raise


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)                   # atomic: a reader never sees a half file


def _key(symbol: str, direction: str, level: float) -> str:
    return f"{symbol}:{direction}:{level:g}"


def register(path: Path, symbol: str, direction: str, level: float,
             reason: str, budget: int = DEFAULT_BUDGET,
             ttl_days: int = DEFAULT_TTL_DAYS, now=None) -> dict:
    """Ask to be woken when `symbol` trades above/below `level`."""
    symbol = str(symbol).strip().upper()
    direction = str(direction).strip().lower()
    reason = str(reason).strip()
    if not symbol:
        return {"error": "symbol is required"}
    if direction not in DIRECTIONS:
        return {"error": f"direction must be one of {list(DIRECTIONS)}"}
    try:
        level = float(level)
    except (TypeError, ValueError):
        return {"error": "level must be a number"}
    if level <= 0 or level != level:            # non-positive or NaN
        return {"error": "level must be a positive number"}
    if not reason:
        return {"error": "a reason is required — an unexplained wake cannot be "
                         "judged when it fires days later"}
    budget = max(1, int(budget))
    ttl_days = max(1, int(ttl_days))

    data = _load(path)
    live = {k: w for k, w in data.items() if not _expired(w, _now(now))}
    key = _key(symbol, direction, level)
    if key not in live and len(live) >= MAX_ACTIVE:
        return {"error": f"{len(live)} wakes already active (cap {MAX_ACTIVE}). "
                         f"Deregister something before adding more."}
    n = _now(now)
    live[key] = {"symbol": symbol, "direction": direction, "level": level,
                 "reason": reason, "budget": budget, "fired": 0,
                 "registered_at": n.isoformat(timespec="seconds"),
                 "expires_at": (n + timedelta(days=ttl_days)).isoformat(
                     timespec="seconds")}
    _save(path, live)
    return {"registered": live[key], "key": key, "active": len(live)}


def _expired(w: dict, now: datetime) -> bool:
    exp = w.get("expires_at")
    if not exp:
        return False
    try:
        return datetime.fromisoformat(exp) <= now
    except ValueError:
        return False


def status(path: Path, now=None) -> dict:
    """Active wakes, with expired ones separated rather than silently dropped."""
    n = _now(now)
    data = _load(path)
    active, expired = {}, {}
    for k, w in data.items():
        (expired if _expired(w, n) else active)[k] = w
    return {"active": active, "expired": sorted(expired),
            "cap": MAX_ACTIVE,
            "note": "A wake starts a SESSION. It is not a stop and places no "
                    "order — use set_levels for that."}


def deregister(path: Path, key: str) -> dict:
    data = _load(path)
    if key not in data:
        return {"error": f"no wake registered under {key!r}",
                "active": sorted(data)}
    removed = data.pop(key)
    _save(path, data)
    return {"deregistered": removed, "active": len(data)}


def due(path: Path, prices: dict, now=None) -> list:
    """Which wakes the given prices satisfy. PURE — no I/O beyond the read.

    Used by whatever polls prices to decide a session is warranted. Budget and
    expiry are enforced HERE so a flapping condition cannot fire forever.
    """
    n = _now(now)
    out = []
    for key, w in _load(path).items():
        if _expired(w, n) or w.get("fired", 0) >= w.get("budget", DEFAULT_BUDGET):
            continue
        px = prices.get(w["symbol"])
        if px is None:
            continue
        hit = (px >= w["level"]) if w["direction"] == "above" else (px <= w["level"])
        if hit:
            out.append({"key": key, **w, "price": px})
    return out


def mark_fired(path: Path, key: str, now=None) -> dict:
    """Count a firing against the wake's budget; retire it when spent."""
    data = _load(path)
    w = data.get(key)
    if not w:
        return {"error": f"no wake under {key!r}"}
    w["fired"] = int(w.get("fired", 0)) + 1
    w["last_fired_at"] = _now(now).isoformat(timespec="seconds")
    spent = w["fired"] >= w.get("budget", DEFAULT_BUDGET)
    if spent:
        data.pop(key)
    _save(path, data)
    return {"key": key, "fired": w["fired"], "retired": spent}


def _selftest() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / WAKES_FILE
        t0 = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

        # validation: every field that could make a wake unjudgeable later
        assert "error" in register(p, "", "below", 1.0, "r", now=t0)
        assert "error" in register(p, "MU", "sideways", 1.0, "r", now=t0)
        assert "error" in register(p, "MU", "below", 0, "r", now=t0)
        assert "error" in register(p, "MU", "below", float("nan"), "r", now=t0)
        assert "error" in register(p, "MU", "below", 640, "", now=t0)

        r = register(p, "mu", "below", 640, "re-enter if it retests", now=t0)
        key = r["key"]
        assert r["registered"]["symbol"] == "MU"
        assert status(p, now=t0)["active"][key]["budget"] == DEFAULT_BUDGET

        # not due above the level; due below it
        assert due(p, {"MU": 700.0}, now=t0) == []
        assert due(p, {}, now=t0) == []                    # missing price -> not due
        assert len(due(p, {"MU": 639.0}, now=t0)) == 1

        # direction 'above' is the mirror
        register(p, "STX", "above", 300, "breakout confirm", now=t0)
        assert len(due(p, {"STX": 301.0}, now=t0)) == 1
        assert due(p, {"STX": 299.0}, now=t0) == []

        # budget retires the wake rather than firing forever
        for i in range(DEFAULT_BUDGET):
            out = mark_fired(p, key, now=t0)
        assert out["retired"] is True, out
        assert key not in status(p, now=t0)["active"]
        assert due(p, {"MU": 1.0}, now=t0) == [], "a spent wake must not re-fire"

        # expiry: visible as expired, and never due
        t_late = t0 + timedelta(days=DEFAULT_TTL_DAYS + 1)
        st = status(p, now=t_late)
        assert st["active"] == {} and st["expired"], st
        assert due(p, {"STX": 999.0}, now=t_late) == []

        # cap: expired ones do not consume the cap
        p2 = Path(d) / "w2.json"
        for i in range(MAX_ACTIVE):
            assert "error" not in register(p2, f"S{i}", "below", 10 + i, "x", now=t0)
        over = register(p2, "ZZZ", "below", 5, "x", now=t0)
        assert "error" in over and "cap" in over["error"], over
        assert "error" not in register(p2, "ZZZ", "below", 5, "x", now=t_late)

        # deregister
        assert "error" in deregister(p2, "nope")
        k2 = _key("ZZZ", "below", 5)
        assert "deregistered" in deregister(p2, k2)

        # corrupt file behaves as empty, never raises
        p3 = Path(d) / "w3.json"
        p3.write_text("{ not json")
        assert status(p3, now=t0)["active"] == {}
        assert due(p3, {"MU": 1.0}, now=t0) == []

    print("wakes: OK — bounded by budget, ttl and cap; a spent wake cannot re-fire")


if __name__ == "__main__":
    _selftest()
