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
from datetime import date, datetime, timedelta, timezone
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
        ps = proposed["stop"]
        if not isinstance(ps, (int, float)):
            rejections.append(f"stop {ps!r} is not numeric — rejected")
        else:
            ps = float(ps)
            if cur_stop is not None and ps < cur_stop:
                rejections.append(f"stop {ps} < current {cur_stop} — loosening rejected")
            elif entry_low is not None and ps >= entry_low:
                rejections.append(f"stop {ps} >= entry_low {entry_low} — must stay below entry")
            else:
                accepted["stop"] = ps
    cur_t = current.get("targets") or []
    if "targets" in proposed and proposed["targets"] is not None:
        pt = proposed["targets"]
        if not isinstance(pt, (list, tuple)) or not all(isinstance(x, (int, float)) for x in pt):
            rejections.append(f"targets {pt!r} contains non-numeric elements — rejected")
        elif len(pt) == len(cur_t) and all(p <= c for p, c in zip(pt, cur_t)):
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
    if not isinstance(ov, dict):
        ov = {}
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
    if not isinstance(ints, list):
        ints = []
    today = date.today().isoformat()
    return [i for i in ints if str(i.get("expires", "9999")) >= today]


def append_intent(intent: dict, *, path: Path = INTENTS) -> None:
    ints = _read_json(path, [])
    ints.append({**intent, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    _atomic_write(path, json.dumps(ints, indent=2))


def build_facts(valued, theses, cfg, *, highs, spy_ret, cur_sigma, entry_sigma,
                vix, regime_on, now_iso) -> dict:
    """Per-name risk readout. Flags are attention hints for the agent, not
    triggers — the agent judges every held name each pass. `spy_ret` = recent
    return minus SPY's (spec §5 #1); `cur_sigma` = current trailing daily sigma
    (spec §5 #5), compared against `entry_sigma` for vol expansion."""
    by_sym = {t.symbol: t for t in theses}
    out = []
    for sym, p in (valued.get("positions") or {}).items():
        th = by_sym.get(sym)
        if th is None or th.stop is None:
            continue
        mark = p.get("mark") or 0.0
        flags = []
        dist_to_stop = (mark - th.stop) / mark if mark else None
        if dist_to_stop is not None and dist_to_stop <= 0.03:
            flags.append("near_stop")
        high = highs.get(sym)
        giveback = (high - mark) / high if (high and mark) else None
        if giveback is not None and giveback >= cfg["giveback_flag_pct"]:
            flags.append("giveback")
        es = entry_sigma.get(sym)
        cs = cur_sigma.get(sym)
        if es and cs and cs > cfg["vol_expansion_mult"] * es:
            flags.append("vol_expansion")
        rb = (th.review_by or "")[:10]
        if rb and rb <= _days_ahead_iso(now_iso, cfg["earnings_window_days"]):
            flags.append("earnings_soon")
        out.append({
            "symbol": sym, "mark": round(float(mark), 4), "stop": th.stop,
            "targets": th.targets, "pnl_pct": p.get("pnl"),
            "dist_to_stop_pct": round(dist_to_stop, 4) if dist_to_stop is not None else None,
            "giveback_from_high_pct": round(giveback, 4) if giveback is not None else None,
            "rel_return_vs_spy": spy_ret.get(sym), "rank": th.rank,
            "review_by": th.review_by, "flags": flags,
        })
    return {"generated": now_iso,
            "backdrop": {"vix": vix, "vix_ceiling": 28.0, "regime_on": regime_on},
            "positions": out}


def _days_ahead_iso(now_iso: str, days: int) -> str:
    from datetime import timedelta
    base = datetime.fromisoformat(now_iso).date()
    return (base + timedelta(days=days)).isoformat()


def apply_decisions(decisions, current_geom, *, armed,
                    overrides_path=OVERRIDES, intents_path=INTENTS):
    """Validate each agent decision (one-way invariant) and, only when armed,
    persist geometry overrides / watch-notes. Returns (applied, orders, rejected):
      - applied = the validated de-risk geometry/watch actions this pass DECIDED
                  on. These are persisted to overrides_path/intents_path only when
                  armed; on an unarmed (alert-only) pass, applied is populated the
                  same way but nothing is written to disk. Consumers that need to
                  know whether an action actually took effect must check the
                  journal event's `armed` flag, not just the presence of entries
                  in `applied`;
      - orders  = trim/exit the prompt must still PLACE via the RH MCP — recorded
                  here as INTENTS only, never as confirmed fills (record_fills.py
                  logs the actual fill; the letter narrates from that, not from here);
      - rejected = decisions that failed the one-way / action-kind guard.
    Keeping orders separate stops the journal (and the weekly letter) from ever
    claiming a sale that has not actually filled."""
    applied, orders, rejected = [], [], []
    for dcn in decisions:
        sym = dcn.get("symbol")
        try:
            ok, why = validate_action(dcn)
            if not ok:
                rejected.append({"symbol": sym, "reason": why})
                continue
            kind = dcn["kind"]
            if kind in ("tighten_stop", "lower_tp"):
                cg = current_geom.get(sym, {})
                acc, rej = validate_geometry(cg, {k: dcn.get(k) for k in ("stop", "targets")},
                                             entry_low=cg.get("entry_low"))
                if not acc:
                    rejected.append({"symbol": sym, "reason": "; ".join(rej) or "no stricter change"})
                    continue
                if armed:
                    write_override(sym, acc, dcn.get("reason", ""),
                                   dcn.get("expires", date.today().isoformat()),
                                   path=overrides_path)
                applied.append({"symbol": sym, "kind": kind, "geometry": acc})
            elif kind == "watch":
                if armed:
                    append_intent({"symbol": sym, "note": dcn.get("note", dcn.get("reason", "")),
                                   "expires": dcn.get("expires", date.today().isoformat())},
                                  path=intents_path)
                applied.append({"symbol": sym, "kind": "watch"})
            elif kind in ("trim", "exit"):  # an ORDER — the prompt places it via MCP
                orders.append({"symbol": sym, "kind": kind, "fraction": dcn.get("fraction"),
                               "reason": dcn.get("reason", "")})
            # kind == "hold" is a no-op: nothing to persist, nothing to place.
        except Exception as e:
            # One malformed decision (e.g. non-numeric stop) must never abort the
            # whole --apply batch and drop other VALID de-risk decisions alongside it.
            rejected.append({"symbol": sym, "reason": str(e)})
    return applied, orders, rejected


def _held(prod):
    """The held names the review tends (weighted, with a stop)."""
    return [t for t in prod.theses if t.target_weight > 0 and t.stop]


def _gather_highs(prod) -> dict:
    """Highest HIGH since ENTRY per held name -> {symbol: high}.

    Reads the cached highs panel (research_store/prices/highs.parquet), same as
    _gather_rel_strength reads closes — no API call at all. This replaced a Schwab
    per-name `get_price_history` loop; the panel already holds 10y of daily highs
    and is refreshed by scripts/fetch_prices.py, so the request was redundant.

    Bounding to bars at/after the thesis entry (`as_of`) keeps give-back honest — a
    pre-entry spike we never held through must not count as our high-water mark.

    The panel excludes the unsettled current session, so TODAY's intraday high is
    folded in from a live moomoo snapshot (one unmetered call). Without it, a name
    that peaked and reversed this morning would understate its give-back — the exact
    thing this function exists to measure. Best-effort: if the snapshot fails, the
    settled-panel high still stands.
    """
    try:
        import pandas as pd
        panel = pd.read_parquet(REPO / "research_store" / "prices" / "highs.parquet")
    except Exception:
        return {}

    held = list(_held(prod))
    out = {}
    for t in held:
        if t.symbol not in panel.columns:
            continue
        s = panel[t.symbol].dropna()
        if t.as_of:
            try:
                s = s[s.index >= pd.Timestamp(t.as_of[:10])]
            except Exception:
                pass
        if len(s):
            out[t.symbol] = float(s.max())

    try:                                    # today's session high, if reachable
        from adapters.moomoo import prices as mmp
        live = mmp.live_quotes([t.symbol for t in held])
        for sym, q in live.items():
            h = q.get("high")
            if isinstance(h, (int, float)) and h > 0:
                out[sym] = max(out.get(sym, 0.0), float(h))
    except Exception:
        pass
    return out


def _gather_rel_strength(prod) -> dict:
    """Recent (~1mo) return MINUS SPY's, per held name, from cached closes
    (research_store/prices/closes.parquet). Spec §5 #1 — the most on-strategy
    signal; positive = still leading the market. Best-effort: any failure omits
    the value and the agent simply sees no RS number for that name."""
    try:
        import pandas as pd
        closes = pd.read_parquet(REPO / "research_store" / "prices" / "closes.parquet")
        WIN = 21  # ~1 trading month
        if "SPY" not in closes.columns:
            return {}
        spy = closes["SPY"].dropna()
        if len(spy) <= WIN:
            return {}
        spy_ret = float(spy.iloc[-1] / spy.iloc[-1 - WIN] - 1.0)
        out = {}
        for t in _held(prod):
            if t.symbol not in closes.columns:
                continue
            s = closes[t.symbol].dropna()
            if len(s) > WIN:
                out[t.symbol] = round(float(s.iloc[-1] / s.iloc[-1 - WIN] - 1.0) - spy_ret, 4)
        return out
    except Exception:
        return {}


def _gather_cur_sigma(prod) -> dict:
    """Current trailing daily sigma (~3mo) per held name from cached closes —
    compared in build_facts against the entry sigma to flag vol expansion
    (spec §5 #5). Best-effort."""
    try:
        import pandas as pd
        closes = pd.read_parquet(REPO / "research_store" / "prices" / "closes.parquet")
        out = {}
        for t in _held(prod):
            if t.symbol not in closes.columns:
                continue
            s = closes[t.symbol].dropna()
            if len(s) > 64:
                out[t.symbol] = round(float(s.pct_change().iloc[-64:].std()), 5)
        return out
    except Exception:
        return {}


def _gather_entry_sigma(prod) -> dict:
    return {t.symbol: (t.signals or {}).get("sigma") for t in prod.theses
            if (t.signals or {}).get("sigma")}


def _gather_vix() -> float | None:
    """Latest VIX. Sourced from FRED (`VIXCLS`), which already served the macro
    regime indicators — so this needed no new plumbing when the Schwab `$VIX` quote
    went away. FRED publishes the settled daily close, not an intraday tick; for a
    twice-daily de-risk review that is the right granularity anyway."""
    try:
        from adapters.fred import indicators
        v = indicators.get_vix()
        val = (v or {}).get("value")
        return float(val) if isinstance(val, (int, float)) else None
    except Exception:
        return None


def _q_last(q: dict):
    blk = (q or {}).get("quote", q) or {}
    for k in ("lastPrice", "mark", "closePrice"):
        if isinstance(blk.get(k), (int, float)):
            return float(blk[k])
    return None


def _selftest() -> None:
    # Expiry fixtures MUST be relative to today. `read_overrides`/`read_intents`
    # prune against the real `date.today()`, so a hardcoded future date rots into
    # the past and the fixture is pruned before the assertion ever runs. That is
    # exactly what happened: every `expires` below was pinned to "2026-07-18",
    # which went stale on 2026-07-19 and left this selftest failing with
    # KeyError 'NVDA' — on an ARMED job that places real orders.
    # The subtler damage was to the armed=False case at the end: pruning made
    # `read_overrides(...) == {}` true whether or not the file was wrongly
    # written, so the ships-safe property silently stopped being tested. It now
    # asserts the file does not exist, which pruning cannot fake.
    LIVE = (date.today() + timedelta(days=7)).isoformat()   # always in the future
    DEAD = "2000-01-01"                                     # always in the past

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
        write_override("NVDA", {"stop": 105.0}, "trail up", LIVE, path=p)
        got = read_overrides(path=p)
        assert got["NVDA"]["stop"] == 105.0 and got["NVDA"]["reason"] == "trail up", got
        # an already-expired override is pruned on read
        write_override("OLD", {"stop": 1.0}, "stale", DEAD, path=p)
        assert "OLD" not in read_overrides(path=p)
        # ...and pruning must not take the live one with it
        assert "NVDA" in read_overrides(path=p)
        ip = Path(d) / "intents.json"
        append_intent({"symbol": "AMD", "note": "watch 21d reclaim", "expires": LIVE}, path=ip)
        assert read_intents(path=ip)[0]["symbol"] == "AMD"
        # an expired intent is pruned on read, same as an override
        append_intent({"symbol": "OLD", "note": "stale", "expires": DEAD}, path=ip)
        assert [i["symbol"] for i in read_intents(path=ip)] == ["AMD"]

    # --- per-position fact-builder ---
    from research_store.models import Thesis
    valued = {"positions": {"BE": {"qty": 1.0, "avg_cost": 200.0, "mark": 210.0,
                                   "value": 210.0, "pnl": 0.05}}}
    theses = [Thesis(symbol="BE", rank=3, verdict="buy", stop=190.0,
                     targets=[230.0, 260.0], target_weight=0.07,
                     review_by="2026-07-16 (next earnings)")]
    cfg = {"ma_break_days": 21, "giveback_flag_pct": 0.10,
           "vol_expansion_mult": 1.75, "earnings_window_days": 5}
    facts = build_facts(valued, theses, cfg, highs={"BE": 250.0},
                        spy_ret={"BE": -0.04}, cur_sigma={"BE": 0.06},
                        entry_sigma={"BE": 0.03}, vix=22.0,
                        regime_on=True, now_iso="2026-07-14T16:00:00+00:00")
    pos = facts["positions"][0]
    assert pos["symbol"] == "BE"
    assert round(pos["giveback_from_high_pct"], 2) == 0.16     # (250-210)/250
    assert "giveback" in pos["flags"]                          # 0.16 > 0.10
    assert "vol_expansion" in pos["flags"]                     # 0.06 > 1.75*0.03
    assert pos["rel_return_vs_spy"] == -0.04                   # lagging SPY
    assert facts["backdrop"]["vix"] == 22.0

    with tempfile.TemporaryDirectory() as d:
        ovp, dp = Path(d) / "ov.json", Path(d) / "dec.json"
        dp.write_text(json.dumps([
            {"symbol": "NVDA", "kind": "tighten_stop", "reason": "trail",
             "stop": 108.0, "expires": LIVE},
            {"symbol": "AMD", "kind": "buy", "reason": "sneaky entry"},   # must be rejected
        ]))
        applied, orders, rejected = apply_decisions(
            json.loads(dp.read_text()),
            current_geom={"NVDA": {"stop": 100.0, "targets": [120.0], "entry_low": 130.0}},
            armed=True, overrides_path=ovp, intents_path=Path(d) / "i.json")
        assert "NVDA" in [a["symbol"] for a in applied], applied
        assert any(r["symbol"] == "AMD" for r in rejected), rejected      # buy rejected
        assert read_overrides(path=ovp)["NVDA"]["stop"] == 108.0
        # a trim/exit is an INTENT (orders), never counted as an applied change
        applied2, orders2, _ = apply_decisions(
            [{"symbol": "TSLA", "kind": "exit", "reason": "thesis broke"}],
            current_geom={}, armed=True,
            overrides_path=Path(d) / "o2.json", intents_path=Path(d) / "i2.json")
        assert orders2 and orders2[0]["symbol"] == "TSLA" and not applied2, (orders2, applied2)

        # a malformed decision (non-numeric stop) must be rejected, not raised, and
        # must not knock a valid decision in the SAME batch off the applied list
        applied3, orders3, rejected3 = apply_decisions(
            [{"symbol": "GME", "kind": "tighten_stop", "reason": "garbage in",
              "stop": "garbage", "expires": LIVE},
             {"symbol": "NVDA", "kind": "tighten_stop", "reason": "trail",
              "stop": 109.0, "expires": LIVE}],
            current_geom={"NVDA": {"stop": 100.0, "targets": [120.0], "entry_low": 130.0}},
            armed=True, overrides_path=Path(d) / "o3.json", intents_path=Path(d) / "i3.json")
        assert any(r["symbol"] == "GME" for r in rejected3), rejected3
        assert "NVDA" in [a["symbol"] for a in applied3], applied3

        # armed=False must write NO override file — the top ships-safe property.
        # Assert on the FILE, not on read_overrides(): a read prunes expired
        # entries and returns {} either way, which is how a decayed fixture date
        # turned this check vacuous once already. The file's absence cannot be
        # faked by pruning.
        o4 = Path(d) / "o4.json"
        applied4, _, _ = apply_decisions(
            [{"symbol": "AMD", "kind": "tighten_stop", "reason": "trail",
              "stop": 51.0, "expires": LIVE}],
            current_geom={"AMD": {"stop": 50.0, "targets": [70.0], "entry_low": 60.0}},
            armed=False, overrides_path=o4, intents_path=Path(d) / "i4.json")
        assert "AMD" in [a["symbol"] for a in applied4], applied4    # validated...
        assert not o4.exists(), f"armed=False wrote {o4}"            # ...but never persisted
        assert read_overrides(path=o4) == {}

    print("selftest OK: one-way geometry + action validation")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--facts", action="store_true", help="gather + write risk_review_facts.json")
    ap.add_argument("--apply", action="store_true", help="apply risk_review_decisions.json")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        return

    import strategy as strat            # noqa: E402
    import governance as gov            # noqa: E402
    import marks                        # noqa: E402
    from research_store import read_current, store   # noqa: E402
    cfg = strat.load()
    rc = cfg["risk_review"]
    armed = gov.live_approved(cfg) and not rc.get("alert_only", True)

    if args.facts:
        valued = marks.load()
        prod = read_current()
        if valued is None or prod is None:
            sys.exit("no snapshot/product yet")
        # NOTE: highs/spy_ret/entry_sigma/vix are filled from adapters here;
        # each is best-effort — a failed fetch leaves its flag off (see build_facts).
        facts = build_facts(valued, prod.theses, rc, highs=_gather_highs(prod),
                            spy_ret=_gather_rel_strength(prod), cur_sigma=_gather_cur_sigma(prod),
                            entry_sigma=_gather_entry_sigma(prod),
                            vix=_gather_vix(), regime_on=((prod.regime or {}).get("status") == "on"),
                            now_iso=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        FACTS.parent.mkdir(parents=True, exist_ok=True)
        FACTS.write_text(json.dumps(facts, indent=2))
        print(f"facts -> {FACTS} ({len(facts['positions'])} positions, armed={armed})")
        return

    if args.apply:
        decisions = _read_json(DECISIONS, [])
        prod = read_current()
        current_geom = {t.symbol: {"stop": t.stop, "targets": t.targets,
                                   "entry_low": (t.entry_zone or [None])[0]}
                        for t in (prod.theses if prod else [])}
        applied, orders, rejected = apply_decisions(decisions, current_geom, armed=armed)
        store.append_journal({"event": "risk_review",
                              "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "armed": armed, "applied": applied,
                              "orders_intended": orders, "rejected": rejected})
        # Phone push ONLY when something de-risking actually happened — a routine
        # "0 actions" twice a day is exactly the as-designed noise Aaron doesn't
        # want (see docs/OPSLOG.md / the no-skip-noise rule).
        acted = applied + orders
        if acted:
            from notify import push
            push(f"Risk review: {len(acted)} de-risk action(s)" + ("" if armed else " (alert-only)"),
                 "\n".join(f"{a['symbol']} {a['kind']}" for a in acted),
                 tags="shield")
        print(f"applied {len(applied)}, orders {len(orders)}, rejected {len(rejected)} (armed={armed})")
        return


if __name__ == "__main__":
    main()
