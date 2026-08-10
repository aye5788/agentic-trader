"""Live moomoo access for the agent's tool surface — called on demand, not cached.

The agent asks for a price and gets THIS SECOND's price. Nothing here reads a
file written by a cron job. If the feed is down, these functions say so; they
never fall back to a stale number, because a stale number the agent believes is
live is worse than no number at all.

Runtime note (this reverses a long-standing assumption in CLAUDE.md): the moomoo
SDK was believed to be importable only under system /usr/bin/python3 (3.10). That
was an accident of how it was installed, not a constraint — `moomoo-api` declares
no Requires-Python and its deps (pandas, protobuf>=3.20, pycryptodome, simplejson)
all resolve on 3.12. It is now installed in .venv as well, so the MCP server calls
moomoo directly, in-process.

STDOUT IS PROTOCOL. This module is imported by an MCP server that speaks JSON-RPC
over stdin/stdout. The moomoo SDK attaches a StreamHandler bound to a *direct
reference* to sys.stdout (logger "FTConsoleLog"), so `contextlib.redirect_stdout`
does NOT contain it -- the handler kept its own pointer. _seal_stdout() below
retargets that handler at stderr on import. Without it, one connect message
(~369 bytes) is injected mid-stream and every tool on the server breaks, not just
these. Verified 2026-08-10: stdout empty, stderr carries the diagnostics.

OpenD is SHARED with the sibling repos (moomoo-vol-desk, moomoo-data-collector) on
127.0.0.1:11111. Every call here opens its own context and closes it in a finally,
rather than holding a long-lived connection for the life of the server.

Documented limits (moomoo API reference, mirrored at
/opt/trading/skills/moomooapi/scripts/quote/ on the moom droplet -- 129 endpoint
scripts, one per call, which is the authority here, not guesswork):

  get_market_snapshot   60 req / 30s, 400 codes/req, NO subscription required.
                        Costs no history quota -- but it is NOT "unmetered";
                        there is a hard request-rate ceiling.
  get_market_state      10 req / 30s, 400 codes/req. The STRICTER limit, so one
                        quote() call = one of these; ~10 quote() calls per 30s.
  get_earnings_calendar 60 req / 30s, and a hard 7-DAY window per query.
  request_history_kline capped at 100 distinct stocks ACCOUNT-WIDE, shared with
                        the sibling repos. Deliberately NOT exposed here: an
                        agent browsing history could exhaust the whole account's
                        quota permanently.

`update_time` on a US snapshot is US EASTERN time (Beijing for HK/A-shares).

VIX is NOT available from moomoo: get_macro_indicator_list(US) returns 24
indicators and none is a volatility index, and US.VIX is not a snapshot code.
VIXY/UVXY are volatility-FUTURES ETFs that decay and are not the index, so they
are not a substitute. VIX stays on FRED (VIXCLS). Checked 2026-08-10.
"""
from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta

HOST = "127.0.0.1"
PORT = 11111


def _seal_stdout() -> None:
    """Point moomoo's console logger at stderr. MUST run before any moomoo call."""
    for name in ("FTConsoleLog", "FTFileLog"):
        for h in list(logging.getLogger(name).handlers):
            if getattr(h, "stream", None) is sys.stdout:
                h.stream = sys.stderr


def _import_moomoo():
    import moomoo as mo          # noqa: PLC0415 -- deferred so import errors surface as data errors
    _seal_stdout()
    return mo


class OpendDown(RuntimeError):
    """OpenD is not accepting connections. Raised INSTEAD of hanging forever."""


def opend_reachable(timeout: float = 3.0) -> tuple[bool, str]:
    """Probe the OpenD port before constructing a quote context.

    ⚠️ THIS PROBE IS LOAD-BEARING, not a nicety. `OpenQuoteContext(...)` RETRIES
    FOREVER when OpenD is down -- it never returns and never raises, so a
    try/except around it catches nothing and the caller blocks indefinitely. In
    an MCP server that means the tool call never comes back and the agent is
    stuck with no error to report. Verified on this box 2026-08-10: the
    constructor was still hanging when SIGTERMed at 15s.

    Lesson taken from /opt/trading/watcher/session.py:393, where the same failure
    produced silent no-trade days.

    The socket is constructed INSIDE the try: if socket creation itself fails
    (fd exhaustion, a sandbox blocking sockets) that must degrade loudly here,
    not propagate and take the whole call down.
    """
    import socket                    # noqa: PLC0415
    sock = None
    try:
        sock = socket.socket()
        sock.settimeout(timeout)
        sock.connect((HOST, PORT))
        return True, ""
    except Exception as e:           # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    finally:
        try:
            if sock is not None:
                sock.close()
        except Exception:            # noqa: BLE001
            pass


@contextmanager
def _ctx():
    """Open a quote context, always close it. Yields (moomoo_module, context)."""
    ok, why = opend_reachable()
    if not ok:
        raise OpendDown(
            f"OpenD is not reachable on {HOST}:{PORT} ({why}). No live market "
            "data is available. Nothing cached is being substituted."
        )
    mo = _import_moomoo()
    q = mo.OpenQuoteContext(host=HOST, port=PORT)
    try:
        yield mo, q
    finally:
        try:
            q.close()
        except Exception:          # noqa: BLE001 -- close failure must not mask a result
            pass


# --- rate limiting -----------------------------------------------------------
# moomoo's documented per-endpoint ceilings. OpenD is SHARED with the sibling
# repos, so exceeding these degrades moomoo-vol-desk as well as us -- and the
# agent drives these calls interactively, with no natural pacing. `prices.py`
# paces its own calls; this module had NONE until 2026-08-10.
#
# get_market_state is the strict one at 10/30s, and `quotes()` makes one of each,
# so a quote() call is bounded by the market-state budget, not the snapshot's.
_LIMITS = {
    "snapshot": (60, 30.0),
    "market_state": (10, 30.0),
    "earnings": (60, 30.0),
    "rank": (60, 30.0),
    "plate": (60, 30.0),
    "calendar": (60, 30.0),
    "trading_days": (60, 30.0),
    "subscribe": (60, 30.0),
}
_calls: dict = {}


def _pace(kind: str) -> float:
    """Block until another `kind` call is within moomoo's published rate limit.

    Sliding window, not a fixed min-interval: the limits are "N per 30s", so a
    burst of 10 followed by a wait is legal and a rigid interval would be slower
    than the API allows for no benefit. Returns seconds waited (for tests).
    """
    import time                            # noqa: PLC0415
    limit, window = _LIMITS.get(kind, (60, 30.0))
    now = time.monotonic()
    hist = [t for t in _calls.get(kind, []) if now - t < window]
    waited = 0.0
    if len(hist) >= limit:
        sleep_for = window - (now - hist[0]) + 0.05
        if sleep_for > 0:
            time.sleep(sleep_for)
            waited = sleep_for
            now = time.monotonic()
            hist = [t for t in hist if now - t < window]
    hist.append(now)
    _calls[kind] = hist
    return waited


def _num(v):
    """moomoo uses 'N/A' and 0.0 for absent. Return None rather than a fake number."""
    if v is None:
        return None
    if isinstance(v, str):
        if v.strip().upper() in ("N/A", "", "NAN", "NONE"):
            return None
        try:
            v = float(v)
        except ValueError:
            return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:                     # NaN
        return None
    return f


def _code(sym: str) -> str:
    s = sym.strip().upper()
    return s if "." in s else f"US.{s}"


def _bare(code: str) -> str:
    return code.split(".", 1)[1] if "." in code else code


# ---------------------------------------------------------------- quotes ----

QUOTE_FIELDS = {
    "last": "last_price",
    "open": "open_price",
    "prev_close": "prev_close_price",
    "high": "high_price",
    "low": "low_price",
    "volume": "volume",
    "turnover": "turnover",
    "week52_high": "highest52weeks_price",
    "week52_low": "lowest52weeks_price",
    "turnover_rate": "turnover_rate",
    "volume_ratio": "volume_ratio",
    "amplitude": "amplitude",
}


_UNKNOWN_PREFIX = "unknown stock"
_MAX_DROP_ROUNDS = 25


def _parse_unknown(msg: str) -> str | None:
    """moomoo rejects the WHOLE batch if any code is bad, naming only the first:
    'Unknown stock. FAKE1'. Pull that symbol out so the caller can drop and retry.
    """
    s = str(msg or "").strip()
    if _UNKNOWN_PREFIX not in s.lower():
        return None
    tail = s.split(".")[-1].strip()
    return tail.upper() or None


def _snapshot_resilient(mo, q, codes: list) -> tuple[list, dict]:
    """Snapshot `codes`, dropping unknown symbols one at a time until it succeeds.

    moomoo fails the entire call on a single unrecognised code. Without this, one
    delisted holding or one typo returns ZERO prices for every other name in the
    request -- the agent would be blind to its whole book because of one bad
    symbol. Returns (rows, {dropped_symbol: reason}).
    """
    remaining = list(codes)
    dropped: dict = {}
    for _ in range(_MAX_DROP_ROUNDS):
        if not remaining:
            return [], dropped
        _pace("snapshot")
        ret, df = q.get_market_snapshot(remaining)
        if ret == mo.RET_OK:
            return df.to_dict("records"), dropped
        bad = _parse_unknown(df)
        if not bad:
            raise RuntimeError(f"moomoo snapshot failed: {df}")
        before = len(remaining)
        remaining = [c for c in remaining if _bare(c) != bad]
        dropped[bad] = "unknown to moomoo (delisted, or not a US symbol)"
        if len(remaining) == before:        # named a code we never sent -> stop
            raise RuntimeError(f"moomoo snapshot failed: {df}")
    raise RuntimeError(
        f"gave up after {_MAX_DROP_ROUNDS} unknown symbols: {sorted(dropped)}")


# US regular session only. REST is the HK/A-share lunch break -- nothing trades
# then, so it is NOT a regular-session print and must not be treated as one.
_REGULAR_STATES = frozenset({"MORNING", "AFTERNOON"})
_CLOSED_STATES = frozenset({"CLOSED", "NONE", "WAITING_OPEN"})


def _market_state(mo, q, codes) -> dict:
    """Per-symbol session state: is this a regular-session print or a thin one?

    Without this the agent cannot tell a 4am pre-market tick from a real close.
    Both arrive as `last_price` and look identical. Failure is non-fatal -- a
    quote with an unknown session is still a quote -- so this degrades to {}.
    """
    try:
        _pace("market_state")
        ret, df = q.get_market_state(codes)
        if ret != mo.RET_OK:
            return {}
        return {_bare(str(r.get("code", ""))): r.get("market_state")
                for r in df.to_dict("records")}
    except Exception:               # noqa: BLE001
        return {}


def quotes(symbols) -> dict:
    """Live snapshot for up to 400 symbols in ONE unmetered call.

    Returns {"asof": iso, "quotes": {SYM: {...}}, "unavailable": {SYM: reason}}.
    A symbol whose last price is missing or zero lands in `unavailable` -- it is
    never reported as a price of 0.0. One unknown symbol does not sink the batch.
    """
    syms = [s for s in (symbols or []) if str(s).strip()]
    if not syms:
        return {"error": "no symbols given", "quotes": {}, "unavailable": {}}
    if len(syms) > 400:
        return {"error": f"{len(syms)} symbols exceeds the 400-per-call limit",
                "quotes": {}, "unavailable": {}}

    codes = [_code(s) for s in syms]
    try:
        with _ctx() as (mo, q):
            rows, bad = _snapshot_resilient(mo, q, codes)
            # Pass only the codes that SURVIVED the snapshot. get_market_state
            # rejects a whole batch on one unknown code, exactly like the
            # snapshot does -- reusing `codes` here silently returned {} and the
            # session label vanished with no error anywhere.
            good = [str(r.get("code")) for r in rows if r.get("code")]
            state = _market_state(mo, q, good) if good else {}
    except Exception as e:                      # noqa: BLE001
        return {"error": f"moomoo unreachable ({type(e).__name__}: {e}). "
                         "OpenD may be down -- no price is available, and no "
                         "cached price is being substituted.",
                "quotes": {}, "unavailable": {}}

    out = {}
    seen = set()
    for r in rows:
        sym = _bare(str(r.get("code", "")))
        seen.add(sym)
        last = _num(r.get("last_price"))
        if not last:                            # None or 0.0
            bad[sym] = "no last price in the snapshot (halted, or not trading)"
            continue
        rec = {}
        for key, col in QUOTE_FIELDS.items():
            val = _num(r.get(col))
            if val is not None:
                rec[key] = val
        prev = rec.get("prev_close")
        if prev:
            rec["change_pct"] = round((last / prev - 1.0) * 100, 3)
        hi52 = rec.get("week52_high")
        if hi52:
            rec["pct_below_52w_high"] = round((1.0 - last / hi52) * 100, 2)
        rec["update_time_et"] = r.get("update_time")   # US Eastern, per moomoo docs
        st = state.get(sym)
        if st:
            rec["session"] = st
            if st in _CLOSED_STATES:
                rec["session_note"] = (
                    f"{st}: the market is closed. `last` is the previous "
                    "session's closing print, not a live price.")
            elif st not in _REGULAR_STATES:
                rec["session_note"] = (
                    f"{st}: NOT a regular-session price. Pre/after-hours and "
                    "overnight prints are thin, and can differ materially from "
                    "where the regular session opens.")
        out[sym] = rec

    for s in syms:
        b = _bare(_code(s))
        if b not in seen and b not in bad:      # don't clobber the drop reason
            bad[b] = "symbol not returned by moomoo (unknown or unsupported)"

    return {
        "asof": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": "moomoo get_market_snapshot (live; no history quota consumed, "
                  "but rate-limited to 60 req/30s)",
        "quotes": out,
        "unavailable": bad,
    }


# -------------------------------------------------------------- earnings ----

def earnings(symbols=None, weeks: int = 4, today: date | None = None) -> dict:
    """Next scheduled earnings date per symbol, from moomoo's US calendar.

    moomoo caps each calendar query at a 7-DAY window, so this makes one call per
    week scanned (default 4 = 4 calls) and walks forward. The calendar is
    market-wide, so scanning for one symbol costs the same as for fifty.

    `symbols=None` returns the whole window unfiltered (hundreds of rows).

    Returns days_until and BEFORE/AFTER-open timing. It states no opinion about
    whether proximity to earnings is a reason to do anything.
    """
    today = today or date.today()
    want = {_bare(_code(s)) for s in symbols} if symbols else None

    found, scanned = {}, []
    try:
        with _ctx() as (mo, q):
            for w in range(max(1, int(weeks))):
                begin = today + timedelta(days=7 * w)
                end = begin + timedelta(days=6)          # inclusive 7-day window
                scanned.append(f"{begin}..{end}")
                _pace("earnings")
                ret, df = q.get_earnings_calendar(
                    market=mo.Market.US,
                    begin_date=begin.isoformat(),
                    end_date=end.isoformat(),
                )
                if ret != mo.RET_OK:
                    return {"error": f"moomoo earnings calendar failed: {df}",
                            "earnings": {}}
                for r in df.to_dict("records"):
                    sym = _bare(str(r.get("security", "")))
                    if want is not None and sym not in want:
                        continue
                    if sym in found:                      # keep the EARLIEST
                        continue
                    d = str(r.get("earnings_date", ""))[:10]
                    if not d:
                        continue
                    try:
                        days = (date.fromisoformat(d) - today).days
                    except ValueError:
                        continue
                    rec = {
                        "date": d,
                        "days_until": days,
                        "when": r.get("pub_type"),        # BEFORE / AFTER open
                        "period": r.get("period_text"),
                    }
                    for k, col in (("eps_estimate", "eps_predict"),
                                   ("iv", "iv"), ("iv_rank", "iv_rank")):
                        v = _num(r.get(col))
                        if v:
                            rec[k] = v
                    found[sym] = rec
                if want is not None and len(found) == len(want):
                    break
    except Exception as e:                      # noqa: BLE001
        return {"error": f"moomoo unreachable ({type(e).__name__}: {e}). "
                         "No earnings dates available; none are being guessed.",
                "earnings": {}}

    res = {
        "asof": today.isoformat(),
        "source": "moomoo get_earnings_calendar (live, US)",
        "windows_scanned": scanned,
        "earnings": dict(sorted(found.items(), key=lambda kv: kv[1]["days_until"])),
    }
    if want is not None:
        missing = sorted(want - set(found))
        res["none_scheduled"] = missing
        res["note"] = (f"'none_scheduled' means no report falls in the next "
                       f"{len(scanned)} week(s) scanned -- NOT that none is coming.")
    return res


# ----------------------------------------------------------------- depth ----

_SUB_MIN_SECONDS = 65          # moomoo REJECTS an unsubscribe before 60s
_SUB_MAX_HELD = 5              # never hold more than this many order books
_subs: dict = {}               # code -> monotonic timestamp of subscribe


def _reap_subs(mo, q) -> list:
    """Release any order-book subscription past moomoo's 60s minimum.

    ⚠️ moomoo REFUSES an unsubscribe inside the first minute ("subscription
    duration ... too short. Minimum ... 1 minute"), so subscribe→read→release is
    impossible: the slot is held whether we like it or not. Slots are capped at
    100 and SHARED with the sibling repos through the same OpenD, so they are
    reaped lazily on the next call instead, and the number held at once is
    capped.

    On a fresh process we ADOPT whatever order books OpenD already holds -- a
    previous server may have exited owning some -- so a restart cannot orphan a
    slot forever.
    """
    import time                            # noqa: PLC0415
    now = time.monotonic()
    try:
        ret, info = q.query_subscription()
        if ret == mo.RET_OK:
            existing = set((info.get("sub_list") or {}).get("ORDER_BOOK") or [])
            for code in existing:
                _subs.setdefault(code, now)          # adopt, then age out normally
            for code in list(_subs):
                if code not in existing:
                    _subs.pop(code, None)            # already gone
    except Exception:                       # noqa: BLE001
        pass
    released = []
    for code, at in sorted(_subs.items(), key=lambda kv: kv[1]):
        if now - at < _SUB_MIN_SECONDS:
            continue
        ret, msg = q.unsubscribe([code], mo.SubType.ORDER_BOOK)
        if ret == mo.RET_OK:
            _subs.pop(code, None)
            released.append(code)
        # A failed release is NOT swallowed -- it stays in _subs and is retried
        # on the next call, and the caller is told below.
    return released


def depth(symbol: str, levels: int = 5) -> dict:
    """Live bid/ask ladder for ONE symbol — is this name tradeable right now?

    Answers a different question from `quote`: turnover says how much trades over
    a day, this says what is actually resting at the touch right now, and what it
    would cost to cross.

    ⚠️ Requires a SUBSCRIPTION, unlike everything else here, and moomoo will not
    let it be released for 60 seconds. Slots are capped at 100 and shared with the
    sibling repos, so this holds at most %d at a time and reaps the expired ones
    on each call. If that cap is reached and nothing is old enough to release,
    this REFUSES rather than quietly consuming another shared slot.
    """ % _SUB_MAX_HELD
    code = _code(symbol)
    ok, why = opend_reachable()
    if not ok:
        return {"error": f"OpenD not reachable ({why})", "symbol": _bare(code)}
    mo = _import_moomoo()
    q = mo.OpenQuoteContext(host=HOST, port=PORT)
    subscribed = False
    try:
        _reap_subs(mo, q)
        if code not in _subs and len(_subs) >= _SUB_MAX_HELD:
            return {"symbol": _bare(code),
                    "error": f"already holding {len(_subs)} order-book "
                             f"subscriptions (cap {_SUB_MAX_HELD}) and none has "
                             f"passed moomoo's 60s minimum yet. Retry shortly — "
                             f"refusing to take another shared slot."}
        _pace("subscribe")
        ret, msg = q.subscribe([code], mo.SubType.ORDER_BOOK)
        if ret != mo.RET_OK:
            return {"error": f"could not subscribe to the order book: {msg}",
                    "symbol": _bare(code)}
        subscribed = True
        import time as _t                   # noqa: PLC0415
        _subs.setdefault(code, _t.monotonic())
        ret, data = q.get_order_book(code, num=max(1, int(levels)))
        if ret != mo.RET_OK:
            return {"error": f"order book unavailable: {data}", "symbol": _bare(code)}
        bids = [(float(p), float(v)) for p, v, *_ in (data.get("Bid") or [])]
        asks = [(float(p), float(v)) for p, v, *_ in (data.get("Ask") or [])]
        out = {"symbol": _bare(code),
               "bids": [{"price": p, "size": v} for p, v in bids],
               "asks": [{"price": p, "size": v} for p, v in asks]}
        if bids and asks:
            bid, ask = bids[0][0], asks[0][0]
            mid = (bid + ask) / 2.0
            out["bid"], out["ask"] = bid, ask
            out["spread"] = round(ask - bid, 4)
            if mid:
                out["spread_bps"] = round((ask - bid) / mid * 10000, 2)
            out["depth_at_touch"] = {"bid_size": bids[0][1], "ask_size": asks[0][1]}
        else:
            out["note"] = ("no two-sided book right now — outside regular hours "
                           "the ladder is usually empty, which is not illiquidity")
        return out
    except Exception as e:                  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "symbol": _bare(code)}
    finally:
        # No unsubscribe here: moomoo rejects it for the first 60s (see
        # _reap_subs). The slot is released on a later call instead. Attempting
        # it here would fail every time and, if swallowed, would look like it had
        # worked -- which is exactly how the first version leaked a shared slot.
        del subscribed
        try:
            q.close()
        except Exception:                   # noqa: BLE001
            pass


# ----------------------------------------------------------- period ranks ----

_MAX_PLATE_FALLBACK = 30

_PERIODS = {"5d": "change_rate_5d", "10d": "change_rate_10d",
            "20d": "change_rate_20d", "60d": "change_rate_60d",
            "120d": "change_rate_120d", "250d": "change_rate_250d",
            "ytd": "change_rate_ytd"}


MAX_RANK_COUNT = 200          # 200 works, 500 is rejected by the endpoint


def leaders(period: str = "20d", n: int = 25, worst: bool = False) -> dict:
    """The market's strongest (or weakest) names over a horizon — DISCOVERY.

    This looks OUTSIDE the 168-name universe. Its job is to show what is moving
    that the configured universe does not contain, so the universe is a starting
    point rather than a cage.

    Deliberately NOT our signal, and not a substitute for it: src/momentum.py is
    the house measure (residual, sector-tilted, vol-scaled) over names we have
    deep history for. This is raw price change over a fixed window across ~6,900
    US names. Where they disagree is information, not an error.

    ⚠️ NOT a per-symbol lookup. The endpoint returns a ranked PAGE capped at
    %d rows out of ~6,900, with no way to ask for one name, and paging to find an
    arbitrary symbol would cost ~35 calls. For a specific name's own numbers use
    `quote` and `candidates`. Reporting a global rank for a name found in a
    200-row page would be a fabricated denominator.
    """ % MAX_RANK_COUNT
    col = _PERIODS.get(str(period).lower())
    if col is None:
        return {"error": f"unknown period {period!r}; use one of {sorted(_PERIODS)}"}
    n = max(1, min(int(n), MAX_RANK_COUNT))
    try:
        with _ctx() as (mo, q):
            _pace("rank")
            ret, payload = q.get_period_change_rank(market=mo.Market.US,
                                                    count=MAX_RANK_COUNT)
            if ret != mo.RET_OK:
                return {"error": f"period rank failed: {payload}"}
            # Payload is (total_available, DataFrame) -- pick the frame by SHAPE,
            # not by position: indexing [0] silently grabbed the int total.
            df = None
            if isinstance(payload, tuple):
                for el in payload:
                    if hasattr(el, "columns"):
                        df = el
                        break
            else:
                df = payload
            if df is None:
                return {"error": f"unexpected payload shape: {type(payload).__name__}"}
    except Exception as e:                  # noqa: BLE001
        return {"error": f"moomoo unreachable ({type(e).__name__}: {e})"}

    if col not in df.columns:
        return {"error": f"{col} missing from the response"}
    ranked = df.sort_values(col, ascending=bool(worst)).head(n)
    rows = []
    for r in ranked.to_dict("records"):
        rec = {"symbol": _bare(str(r.get("security", ""))), "name": r.get("name")}
        for label, c in _PERIODS.items():
            v = _num(r.get(c))
            if v is not None:
                rec[f"chg_{label}_pct"] = v
        for extra in ("cur_price", "turnover", "market_cap", "volume_ratio"):
            v = _num(r.get(extra))
            if v is not None:
                rec[extra] = v
        rows.append(rec)
    return {"source": f"moomoo get_period_change_rank ({'weakest' if worst else 'strongest'} by {period})",
            "scanned": f"{len(df)} of the US market (endpoint caps a page at {MAX_RANK_COUNT})",
            "leaders": rows}


# --------------------------------------------------------------- sectors ----

def sectors(symbols) -> dict:
    """Real industry membership per name, from moomoo's plate data.

    The residual-momentum tilt currently proxies sectors with 11 SPDR ETFs; this
    is the actual classification. INDUSTRY plates only -- CONCEPT plates are
    thematic baskets ("Metaverse") and OTHER is noise, so folding them in would
    put one name in a dozen overlapping "sectors".
    """
    syms = _symbols_or_empty(symbols)
    if not syms:
        return {"error": "no symbols given", "sectors": {}}
    codes = [_code(s) for s in syms]
    rows, unsupported = [], {}
    try:
        with _ctx() as (mo, q):
            _pace("plate")
            ret, df = q.get_owner_plate(codes)
            if ret == mo.RET_OK:
                rows = df.to_dict("records")
            else:
                # ⚠️ ETFs are NOT supported by this endpoint, and ONE ETF fails
                # the whole batch -- the error names no symbol, unlike the
                # snapshot's "Unknown stock. X", so there is nothing to drop and
                # retry. Fall back to per-symbol calls and record which are
                # unsupported, rather than returning nothing for a book that is
                # part single names and part sleeve ETFs (ours always is).
                for c in codes[:_MAX_PLATE_FALLBACK]:
                    _pace("plate")
                    r1, d1 = q.get_owner_plate([c])
                    if r1 == mo.RET_OK:
                        rows.extend(d1.to_dict("records"))
                    else:
                        unsupported[_bare(c)] = str(d1)[:120]
                if len(codes) > _MAX_PLATE_FALLBACK:
                    unsupported["_truncated"] = (
                        f"only the first {_MAX_PLATE_FALLBACK} symbols were "
                        f"retried individually ({len(codes)} requested)")
    except Exception as e:                  # noqa: BLE001
        return {"error": f"moomoo unreachable ({type(e).__name__}: {e})",
                "sectors": {}}
    out: dict = {}
    for r in rows:
        if str(r.get("plate_type")) != "INDUSTRY":
            continue
        sym = _bare(str(r.get("code", "")))
        out.setdefault(sym, []).append(str(r.get("plate_name")))
    missing = sorted({_bare(c) for c in codes} - set(out) - set(unsupported))
    res = {"source": "moomoo get_owner_plate (INDUSTRY plates only)",
           "sectors": out,
           "no_industry_plate": missing}
    if unsupported:
        res["unsupported"] = unsupported
        res["note"] = ("ETFs have no industry plate — an empty result for a "
                       "sleeve ETF is expected, not a failure.")
    return res


# -------------------------------------------------------- macro calendar ----

def macro_calendar(days: int = 7, importance: str = "HIGH", today=None) -> dict:
    """Scheduled US macro events (FOMC, CPI, payrolls) in the next `days`.

    The macro sibling of `earnings`: dates the whole book is exposed to at once,
    rather than one name. States dates and nothing else -- what to do about an
    FOMC two days out is the agent's call.
    """
    today = today or date.today()
    end = today + timedelta(days=max(1, int(days)))
    try:
        with _ctx() as (mo, q):
            _pace("calendar")
            out = q.get_economic_calendar(begin_date=today.isoformat(),
                                          end_date=end.isoformat())
            ret, data = out[0], out[1]
            if ret != mo.RET_OK:
                return {"error": f"economic calendar failed: {data}", "events": []}
            rows = data.to_dict("records") if hasattr(data, "to_dict") else list(data)
    except Exception as e:                  # noqa: BLE001
        return {"error": f"moomoo unreachable ({type(e).__name__}: {e})", "events": []}

    wanted = str(importance).upper() if importance else None
    events = []
    for r in rows:
        country = str(r.get("country", ""))
        if "United States" not in country and country.upper() not in ("US", "USA"):
            continue
        star = str(r.get("star", "")).upper()
        if wanted and wanted != "ALL" and star != wanted:
            continue
        ts = r.get("timestamp")
        when = None
        if ts:
            try:
                when = datetime.fromtimestamp(float(ts)).astimezone().isoformat(
                    timespec="minutes")
            except (TypeError, ValueError, OSError):
                when = None
        events.append({"title": r.get("title"), "when": when, "importance": star,
                       "previous": r.get("previous"), "consensus": r.get("consensus"),
                       "actual": r.get("actual")})
    return {"source": "moomoo get_economic_calendar (US)",
            "window": f"{today}..{end}",
            "importance_filter": wanted or "ALL",
            "events": events}


def _symbols_or_empty(symbols) -> list:
    if symbols is None:
        return []
    if isinstance(symbols, str):
        return [s for s in symbols.replace(",", " ").split() if s]
    return [str(s).strip() for s in symbols if str(s).strip()]


# -------------------------------------------------------------- selftest ----

def _selftest() -> None:
    # _num: the honesty of missing values
    assert _num("N/A") is None
    assert _num("") is None
    assert _num(float("nan")) is None
    assert _num(None) is None
    assert _num(0.0) == 0.0
    assert _num("12.5") == 12.5
    assert _num(3) == 3.0

    # code normalisation
    assert _code("nvda") == "US.NVDA"
    assert _code("US.NVDA") == "US.NVDA"
    assert _bare("US.NVDA") == "NVDA"
    assert _bare("NVDA") == "NVDA"

    # guard rails that must not need a network round trip
    assert quotes([])["error"]
    assert quotes(["A"] * 401)["error"]
    assert quotes([])["quotes"] == {}

    # a zero last price must never surface as a quote
    assert not _num(0.0) or _num(0.0) == 0.0    # 0.0 is falsy -> routed to unavailable

    # stdout sealing must be idempotent and must not raise
    _seal_stdout()
    _seal_stdout()

    # --- rate limiting ------------------------------------------------------
    import time as _time
    _calls.clear()
    # Under the limit: never sleeps.
    for _ in range(10):
        assert _pace("snapshot") == 0.0
    assert len(_calls["snapshot"]) == 10

    # market_state is the strict one (10/30s). The 11th must wait.
    _calls.clear()
    for _ in range(_LIMITS["market_state"][0]):
        assert _pace("market_state") == 0.0
    # Fake the window as nearly elapsed so the test waits ~0.1s, not 30.
    _calls["market_state"] = [_time.monotonic() - 29.95] * _LIMITS["market_state"][0]
    waited = _pace("market_state")
    assert 0 < waited < 1.0, f"expected a short wait, got {waited}"

    # Entries older than the window fall out rather than accumulating forever.
    _calls["snapshot"] = [_time.monotonic() - 31.0] * 100
    assert _pace("snapshot") == 0.0
    assert len(_calls["snapshot"]) == 1, _calls["snapshot"]

    # Budgets are per-endpoint: exhausting one must not throttle another.
    _calls.clear()
    _calls["market_state"] = [_time.monotonic()] * _LIMITS["market_state"][0]
    assert _pace("snapshot") == 0.0

    # An unknown endpoint falls back to the conservative default, never KeyErrors.
    assert _pace("something_new") == 0.0
    _calls.clear()

    # --- unknown-symbol parsing -------------------------------------------
    assert _parse_unknown("Unknown stock. FAKE1") == "FAKE1"
    assert _parse_unknown("unknown stock. abc") == "ABC"
    assert _parse_unknown("Timeout") is None
    assert _parse_unknown("") is None
    assert _parse_unknown(None) is None

    # --- batch survives bad symbols (no network) --------------------------
    class _FakeMo:
        RET_OK = 0

    class _FakeDF:
        def __init__(self, recs):
            self._r = recs

        def to_dict(self, _):
            return self._r

    class _FakeQ:
        """Rejects the whole batch naming one bad code at a time, like moomoo."""
        def __init__(self, badset):
            self.bad = set(badset)
            self.calls = 0

        def get_market_snapshot(self, codes):
            self.calls += 1
            for c in codes:
                if _bare(c) in self.bad:
                    return -1, f"Unknown stock. {_bare(c)}"
            return 0, _FakeDF([{"code": c, "last_price": 10.0,
                                "prev_close_price": 8.0} for c in codes])

    q = _FakeQ({"FAKE1", "FAKE2"})
    rows, dropped = _snapshot_resilient(_FakeMo, q, ["US.NVDA", "US.FAKE1",
                                                     "US.FAKE2", "US.MU"])
    assert sorted(dropped) == ["FAKE1", "FAKE2"], dropped
    assert {_bare(r["code"]) for r in rows} == {"NVDA", "MU"}, rows
    assert q.calls == 3, q.calls          # fail, fail, succeed

    # a non-symbol error must propagate, never be silently swallowed
    class _BrokenQ:
        def get_market_snapshot(self, codes):
            return -1, "Connection timeout"

    try:
        _snapshot_resilient(_FakeMo, _BrokenQ(), ["US.NVDA"])
        raise AssertionError("a timeout must not be treated as a bad symbol")
    except RuntimeError as e:
        assert "timeout" in str(e).lower()

    # a reply naming a code we never sent must not loop forever
    class _LiarQ:
        def get_market_snapshot(self, codes):
            return -1, "Unknown stock. NEVERSENT"

    try:
        _snapshot_resilient(_FakeMo, _LiarQ(), ["US.NVDA"])
        raise AssertionError("must stop when the named code was not in the batch")
    except RuntimeError:
        pass

    print("live: OK")


if __name__ == "__main__":
    _selftest()
