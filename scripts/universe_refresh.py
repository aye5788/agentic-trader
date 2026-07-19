"""Universe maintenance — quarterly liquidity refresh. Data-only, offline.
See docs/superpowers/specs/2026-07-19-universe-maintenance-design.md."""
import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import universe_maint as um  # noqa: E402


def _selftest() -> None:
    # rank_pond: descending by turnover, drops non-positive
    r = um.rank_pond({"A": 30.0, "B": 100.0, "C": 0.0, "D": None})
    assert r == ["B", "A"], r

    # propose_membership: seeds protected, fills banded, adds fill open slots
    params = {"target_size": 3, "keep_rank_max": 3, "add_rank_max": 3,
              "add_dvol_floor_usd": 10.0}
    current = [
        {"ticker": "SEED1", "source": "seed", "sector": "X", "exchange": "NASDAQ", "flag": "", "as_of": "2026-01-01"},
        {"ticker": "FILL_OK", "source": "fill_dvol", "sector": "", "exchange": "NYSE", "flag": "", "as_of": "2026-01-01"},
        {"ticker": "FILL_GONE", "source": "fill_dvol", "sector": "", "exchange": "NYSE", "flag": "", "as_of": "2026-01-01"},
    ]
    turn = {"SEED1": 5.0, "FILL_OK": 90.0, "NEW": 80.0, "FILL_GONE": 1.0}
    ranked = ["FILL_OK", "NEW", "SEED1", "FILL_GONE"]  # ranks 1,2,3,4
    p = um.propose_membership(ranked, turn, current, set(), params)
    assert "FILL_GONE" in p["drop_fills"], p        # rank 4 <= keep_max 4? no: strictly beyond band? see rule
    assert "NEW" in p["add"], p                      # rank 2, above floor, open slot
    assert "SEED1" in p["keep"], p                   # seed always kept despite rank 3 / low $vol
    assert len(p["result"]) == 3, p                  # target size respected
    print("universe_maint selftest OK: rank_pond + propose_membership")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        return


if __name__ == "__main__":
    main()
