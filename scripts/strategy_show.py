"""Print the codified strategy and prove it wires into the Research Store.

    python scripts/strategy_show.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import strategy  # noqa: E402
import research_store as rs  # noqa: E402
from research_store import ResearchProduct, Thesis  # noqa: E402


def main() -> None:
    cfg = strategy.load()
    print(f"Strategy: {cfg['meta']['name']}  "
          f"({cfg['meta']['edge']} / {cfg['meta']['horizon']} / {cfg['meta']['instrument']})\n")
    for section in ("risk", "trade_management", "signal", "universe", "regime", "proof"):
        print(f"[{section}]")
        for k, v in cfg[section].items():
            print(f"  {k:24} = {v}")
        print()

    # Prove the [risk] table IS the store's mandate: validate a product against it.
    mandate = strategy.risk_mandate(cfg)
    print(f"risk_mandate() -> {mandate}")
    good = ResearchProduct(as_of="2026-07-07T00:00:00+00:00", theses=[
        Thesis(symbol="AAPL", rank=1, verdict="buy", entry_zone=[310, 312],
               stop=305, targets=[325], target_weight=0.08, confidence=0.7)])
    over = ResearchProduct(as_of="2026-07-07T00:00:00+00:00", theses=[
        Thesis(symbol="AAPL", rank=1, verdict="buy", entry_zone=[310, 312],
               stop=305, targets=[325], target_weight=0.20, confidence=0.7)])
    print(f"  valid product  -> violations: {rs.validate_product(good, mandate) or 'none'}")
    print(f"  over-cap (0.20) -> violations: {rs.validate_product(over, mandate)}")


if __name__ == "__main__":
    main()
