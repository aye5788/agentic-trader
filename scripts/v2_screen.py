"""moomoo V2 (`get_stock_screen`, ProtoID 3252) liquidity screen — JSON to `--out`.

⛔ THIS SCRIPT RUNS UNDER `v2env`, NOT THE SYSTEM INTERPRETER, AND THAT IS THE
WHOLE REASON IT IS A SEPARATE PROCESS. `moomoo-api 10.9.6908` declares
`protobuf >= 3.20.0` with no upper bound, but its V2 decoder calls
`FieldDescriptor.label`, which protobuf 6/7 REMOVED. The box (system python3 and
`.venv` alike) runs protobuf 7.35.1, where every V2 call fails with:

    'google._upb._message.FieldDescriptor' object has no attribute 'label'

V1 and the other ~165 SDK methods are unaffected — they use explicit field
access, not the generic reflection walker — so ONLY the V2 screen needs the old
protobuf. Downgrading protobuf system-wide is not an option: that SDK install is
shared with the sibling repo `moomoo-vol-desk`. Hence a pinned, isolated
interpreter and a process boundary carrying plain JSON.

Build it with deploy/setup_v2env.sh (requirements in deploy/v2env-requirements.txt).
The caller is src/adapters/moomoo/research.py::screen_by_turnover().

⛔ THE TURNOVER FIELD IS MISNAMED AND THIS SCRIPT DOES NOT "FIX" IT.
`CumulativeProperty.AVG_TURNOVER` with `days=N` returns the **N-day CUMULATIVE
dollar turnover**, not a daily average — verified live 2026-09-06 against
single-session snapshots (NVDA $569.44B/20 = $28.5B/day vs a $31.4B session;
AAPL 0.98x; F 0.99x; KO 0.84x). This script emits the RAW server value under the
honest name `turnover_20d_cum`. Converting to a daily average is the caller's
job and happens in exactly one place — universe_maint.to_avg_daily(). Do not
divide here as well, or the division happens twice and every liquidity floor
silently becomes 20x stricter.

Data-only. Places no orders, reads no account, spends NO history quota:
`get_stock_screen` is a server-side screen, entirely separate from the 100-
distinct-symbol `request_history_kline` meter.
"""
import argparse
import json
import sys
import time

from moomoo import RET_OK, OpenQuoteContext, StockScreenRequest
from moomoo.quote.stock_screen_const import (BasicProperty, CumulativeProperty,
                                             ScrMarket, ScrSortDir, SimpleField,
                                             SimpleProperty)

HOST, PORT = "127.0.0.1", 11111
PAGE = 200                 # V2 hard maximum rows per page
PACE_S = 3.5               # documented V2 limit is 10 requests / 30s

# Property ids as they come back in the reply (numeric, never names).
P_CODE, P_NAME, P_CAP = 1101, 1102, 2301


def _build(min_mktcap, days, page_from, page_count):
    r = StockScreenRequest()
    r.add_simple_field(field=SimpleField.MARKET, values=[ScrMarket.US])
    # ⛔ MARKET CAP IS A FLOOR, NOT THE RANKING AXIS. The V1 funnel this replaced
    # sorted by market cap and truncated at 400, which made the effective floor
    # ~$57B and hid 1,084 names that were liquid enough to trade.
    r.add_simple_property(name=SimpleProperty.MARKET_CAP, lower=float(min_mktcap))
    r.add_retrieve_basic(name=BasicProperty.CODE)
    r.add_retrieve_basic(name=BasicProperty.NAME)
    r.add_retrieve_simple(name=SimpleProperty.MARKET_CAP)
    r.add_retrieve_cumulative(name=CumulativeProperty.AVG_TURNOVER, days=int(days))
    r.set_sort(direction=ScrSortDir.DESC, property_type="cumulative",
               property_params={"name": int(CumulativeProperty.AVG_TURNOVER),
                                "days": int(days)})
    r.page_from, r.page_count = int(page_from), int(page_count)
    return r


def _flat(item, days):
    """One reply item -> a flat row. Property ids only; `days` disambiguates the
    cumulative field, whose key carries the period alongside the name."""
    out = {"symbol": None, "name": None, "market_cap": None,
           "turnover_20d_cum": None}
    for res in item.get("results", []):
        prop = res.get("property") or {}
        val = res.get("sval", res.get("dval", res.get("ival")))
        name = prop.get("name")
        if name == P_CODE:
            out["symbol"] = val
        elif name == P_NAME:
            out["name"] = val
        elif name == P_CAP:
            out["market_cap"] = val
        elif name == int(CumulativeProperty.AVG_TURNOVER) and prop.get("days") == days:
            out["turnover_20d_cum"] = val
    return out


def _emit(path, payload) -> None:
    """⛔ THE RESULT GOES TO A FILE, NEVER TO STDOUT. The moomoo SDK writes its
    own connection logging to stdout ("New connect ready: conn=..."), which lands
    in the middle of the document and makes it unparseable — the first version of
    this script failed exactly that way. A file keeps the boundary deterministic
    no matter how chatty the SDK becomes."""
    with open(path, "w") as f:
        json.dump(payload, f)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-mktcap", type=float, required=True)
    ap.add_argument("--rows", type=int, default=PAGE,
                    help="how many ranked rows to retrieve (paged at 200)")
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--out", required=True, help="path to write the JSON result")
    args = ap.parse_args()

    rows, pages, all_count, last_page = [], 0, None, False
    try:
        q = OpenQuoteContext(host=HOST, port=PORT)
    except Exception as e:  # noqa: BLE001 — OpenD down is a normal, reportable state
        _emit(args.out, {"ok": False, "error": f"{type(e).__name__}: {e}"})
        return 1
    try:
        while len(rows) < args.rows and not last_page:
            want = min(PAGE, args.rows - len(rows))
            ret, data = q.get_stock_screen(
                _build(args.min_mktcap, args.days, len(rows), want))
            pages += 1
            if ret != RET_OK:
                _emit(args.out, {"ok": False, "error": f"get_stock_screen: {data}",
                                 "pages": pages})
                return 1
            last_page, all_count, items = data
            if not items:
                break
            rows.extend(_flat(i, args.days) for i in items)
            if not last_page and len(rows) < args.rows:
                time.sleep(PACE_S)
    finally:
        q.close()

    _emit(args.out, {"ok": True, "all_count": all_count, "returned_rows": len(rows),
                     "requested_rows": args.rows, "pages": pages,
                     "last_page": bool(last_page), "days": args.days,
                     "min_mktcap": args.min_mktcap, "rows": rows})
    return 0


if __name__ == "__main__":
    sys.exit(main())
