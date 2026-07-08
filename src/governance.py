"""Governance guardrails (docs/DESIGN.md Layer 5) — the last gate before a live
order. The Research Store [risk] mandate validates the research PRODUCT on write;
this validates EXECUTION at trade time — the checks that must hold even when the
book itself is fine:

  - kill-switch : a file (research_store/HALT) whose mere presence stops trading
  - drawdown    : halt if the account is down > max_drawdown from its tracked peak
  - order caps  : reject any single order over max_order_pct of account value
  - whitelist   : only ever trade names in the configured universe
  - live switch : [proof] live_approved gates PLACEMENT entirely (proof gate §9)

Pure functions + a tiny JSON state file for the drawdown peak. No broker I/O —
the fast loop feeds in account value + plan and acts on the verdict. Params live
in [governance] / [proof] in config/strategy.toml (the source of truth).
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATE = REPO / "research_store" / "governance" / "state.json"


def kill_switch_active(cfg) -> bool:
    return (REPO / cfg["governance"]["kill_switch_file"]).exists()


def live_approved(cfg) -> bool:
    """The master switch. Fast loop may PLACE orders only when this is true."""
    return bool(cfg.get("proof", {}).get("live_approved", False))


def _load_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {"peak_value": 0.0}


def update_peak(account_value: float) -> dict:
    st = _load_state()
    st["peak_value"] = max(float(st.get("peak_value", 0.0)), float(account_value))
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=2))
    return st


def drawdown_halt(account_value: float, cfg) -> tuple[bool, float]:
    """(halted?, current_drawdown). Updates the peak as a side effect."""
    peak = update_peak(account_value)["peak_value"] or float(account_value)
    dd = 0.0 if peak <= 0 else (float(account_value) / peak - 1.0)
    return dd < -abs(cfg["governance"]["max_drawdown"]), dd


def whitelist(cfg) -> set[str]:
    def col(path):
        return {ln.split(",")[0].strip()
                for ln in (REPO / path).read_text().splitlines()[1:] if ln.strip()}
    return col(cfg["universe"]["source"]) | col(cfg["etf_sleeve"]["source"])


def preflight(account_value: float, cfg) -> list[str]:
    """Global halts checked before ANY order. Returns halt reasons ([] = clear)."""
    halts = []
    if kill_switch_active(cfg):
        halts.append(f"KILL-SWITCH active ({cfg['governance']['kill_switch_file']} present)")
    halted, dd = drawdown_halt(account_value, cfg)
    if halted:
        halts.append(f"DRAWDOWN halt: {dd:.1%} from peak "
                     f"(limit {cfg['governance']['max_drawdown']:.0%})")
    return halts


def vet_plan(plan: list[dict], account_value: float, cfg) -> tuple[list[dict], list[dict]]:
    """Split a plan into (approved, blocked). Each blocked order carries a reason."""
    g = cfg["governance"]
    wl = whitelist(cfg) if g.get("require_whitelist") else None
    max_order = g["max_order_pct"] * float(account_value)
    approved, blocked = [], []
    for o in plan:
        why = None
        if wl is not None and o["symbol"] not in wl:
            why = "not in whitelist universe"
        elif o["side"] == "buy" and o["amount"] > max_order + 1e-9:
            why = f"${o['amount']:.2f} exceeds max order ${max_order:.2f} ({g['max_order_pct']:.0%})"
        (blocked if why else approved).append({**o, "blocked": why} if why else o)
    return approved, blocked
