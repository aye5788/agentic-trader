"""Render the agent's session charter from the constants that ENFORCE it.

THE FAILURE THIS PREVENTS: a charter that states a threshold in prose drifts from
the code that applies it, and the agent is then told a limit it will not actually
meet. CLAUDE.md already paid for this once -- it carried a fixed account balance,
the figure went stale, and agents anchored on it to dismiss real risks. The remedy
was to delete the number and force a live read. Same remedy here: prompts/
charter.md contains PLACEHOLDERS, never literals, and every number is interpolated
from mandate.toml, strategy.toml, or the live tool list at render time.

A literal threshold in prompts/charter.md is a defect. check_charter_no_literals
in src/repo_checks.py enforces that, so this cannot rot quietly.

WHAT THIS DOES NOT COVER: it renders text. It does not verify that the rendered
limits are the ones the gate actually applies at order time -- that binding lives
in scripts/hooks/pretooluse_order_gate.py and is proven by live probe, not here.
It also cannot check that the PROSE around a number is still true; only the
numbers are derived.

Substitution is str.replace, never str.format: the charter is full of braces
(JSON shapes, code) and a format() would choke on them or silently eat one.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "prompts" / "charter.md"

# The announce-first trigger is a FRACTION OF the blocking concentration limit,
# never a second number. At the limit the gate refuses outright; announcing at
# 80% means a human hears about a position getting large before it is stopped,
# and there is exactly one concentration figure in the system.
ANNOUNCE_FRACTION = 0.80


def _pct(x) -> str:
    """0.15 -> '15%'. Trailing zeros trimmed so 0.2 reads '20%', not '20.0%'."""
    v = float(x) * 100.0
    return f"{v:g}%"


def render_mandate(mandate_cfg: dict) -> str:
    dd = mandate_cfg.get("drawdown", {}).get("max_pct")
    conc = mandate_cfg.get("concentration", {}).get("max_position_pct")
    pnl = mandate_cfg.get("pnl_concentration", {})
    rel = mandate_cfg.get("relative_return", {})
    return "\n".join([
        "Two criteria BLOCK. A breach makes the machine act, not advise:",
        "",
        f"- **Drawdown** — no more than {_pct(dd)} below the all-time high-water "
        f"mark, measured close-to-close. Never intraday: an intraday measure "
        f"fires on noise.",
        f"- **Concentration** — no single position above {_pct(conc)} of equity, "
        f"at any mark.",
        "",
        "Two criteria INFORM. They judge whether the approach is working and "
        "never gate an order:",
        "",
        f"- **P&L concentration** — over {pnl.get('window_days')} days, no single "
        f"closed round trip above {_pct(pnl.get('max_single_share'))} of realised "
        f"P&L, and at least {pnl.get('min_distinct_names')} distinct names closed.",
        f"- **Relative return** — versus {rel.get('benchmark')} over "
        f"{rel.get('window_days')} trading days.",
        "",
        "Every criterion is three-state: PASS / FAIL / INSUFFICIENT_DATA. "
        "INSUFFICIENT_DATA is never a pass. `mandate_status()` gives you the live "
        "numbers and the room left on each.",
    ])


def render_terms(gov_cfg: dict) -> str:
    return "\n".join([
        "Standing terms:",
        "",
        # Day trading, options and the cash-account fact are stated once each in
        # "WHAT YOU CAN AND CANNOT TRADE" and "WHAT IS TRUE ABOUT THIS SYSTEM".
        # Repeating them here read as three separate rules about the same thing.
        "- **Settlement** — T+1 on closes. Size buys against `buying_power`, "
        "never `cash`; the difference is real and often most of the balance.",
        f"- **Liquidity floor** — names below "
        f"${float(gov_cfg.get('min_dollar_volume_20d', 0)):,.0f} of 20-day dollar "
        f"volume are flagged advisory, never blocked.",
        "- **Observe the data feed's rate limits.** Paced in code; you will not "
        "normally meet them.",
    ])


def render_gate(gov_cfg: dict) -> str:
    order_pct = gov_cfg.get("max_order_pct")
    return "\n".join([
        "Before any order reaches the broker it passes a gate that runs in the "
        "harness, not in your judgment. It refuses:",
        "",
        "- any order while the **kill switch** is set — including a sell; exits "
        "are placed by hand then",
        "- a **BUY** while entries are halted (exits still place)",
        "- a **BUY** for a symbol outside the configured universe",
        f"- any order whose notional exceeds **{_pct(order_pct)} of equity**",
        "- any order in an account other than the one Agentic account",
        "- **every** order while shadow mode is set",
        "",
        "**A SELL is refused by nothing but the kill switch.** Stops here are "
        "software, so blocking a sell would remove a position's only protection.",
    ])


def render_tools(tool_names) -> str:
    """Grouped by what each tool TOUCHES. Complete, never ranked.

    ⚠️ A PARTIAL LIST IS A RECOMMENDATION. The tools somebody thought to type are
    the tools that get used, and the ones omitted may as well not exist. So the
    caller passes the LIVE list and anything not matching a group below is
    surfaced under "Other" rather than silently dropped.
    """
    groups = (
        ("ORIENT", ("brief", "positions", "account", "performance",
                    "mandate_status", "halt_status")),
        ("SELECT", ("candidates", "universe", "leaders", "sectors")),
        ("PRICE", ("quote", "depth", "terrain")),
        ("EVENTS", ("earnings", "macro_calendar", "macro", "news")),
        ("DECIDE", ("check_order", "set_levels", "record_decision")),
        ("REMEMBER", ("research_log", "rule_out", "revisit", "open_question",
                      "close_question")),
        ("ATTENTION", ("wake_register", "wake_status", "wake_deregister")),
        ("LIVENESS", ("ping",)),
    )
    bare = {n.split("__")[-1] for n in tool_names}
    lines, claimed = [], set()
    for label, members in groups:
        present = [m for m in members if m in bare]
        if present:
            claimed.update(present)
            lines.append(f"- **{label}** — {', '.join(present)}")
    leftover = sorted(bare - claimed)
    if leftover:
        lines.append(f"- **OTHER** — {', '.join(leftover)}")
    lines.append("- **ORDERS (Robinhood — the only execution venue)** — "
                 "place_equity_order, cancel_equity_order, review_equity_order, "
                 "and the account/order reads")
    return "\n".join(lines)


def render_universe(strat_cfg: dict, repo: Path | None = None) -> str:
    """How many names are in the default hunting ground, counted from the CSVs.

    Derived, not stated: the universe is re-cut quarterly and a literal here
    would be wrong within months. Counting the file is the only figure that
    cannot drift from what candidates()/universe() will actually show.
    """
    repo = repo or REPO
    counts = []
    for key, label in (("universe", "single names"), ("etf_sleeve", "ETFs")):
        src = strat_cfg.get(key, {}).get("source")
        if not src:
            continue
        path = repo / src
        if not path.is_file():
            continue
        rows = [ln for ln in path.read_text().splitlines()[1:] if ln.strip()]
        counts.append(f"{len(rows)} {label}")
    if not counts:
        return ("A configured universe is your default hunting ground; "
                "`universe()` shows it in full.")
    return (f"A configured universe of {' and '.join(counts)} is your default "
            f"hunting ground — `candidates()` ranks the top of it and "
            f"`universe()` shows all of it. You may trade outside that list; "
            f"say why when you do, and note that an off-list name has no deep "
            f"price history here, so it cannot be scored or given measured "
            f"stop geometry.")


def render_baseline(strat_cfg: dict) -> str:
    """The house view, derived. Key names come from config/strategy.toml's
    [portfolio]/[signal] tables -- book_hold/sleeve_hold/book_weight, NOT the
    max_holdings/n_names an outsider would guess. A wrong key here fails SILENTLY
    (the clause is simply omitted), which is why the selftest asserts the numbers
    appear rather than only that render() succeeded.
    """
    port = strat_cfg.get("portfolio", {})
    sig = strat_cfg.get("signal", {})
    book_hold = port.get("book_hold")
    sleeve_hold = port.get("sleeve_hold")
    book_w = port.get("book_weight")
    sleeve_w = port.get("sleeve_weight")
    tilt = sig.get("residual_tilt")
    parts = ["The house currently believes: **cross-sectional momentum**, "
             "long-only, rebalanced weekly."]
    if book_hold and sleeve_hold:
        parts.append(f"Top {book_hold} single names plus a {sleeve_hold}-holding "
                     f"ETF sleeve.")
    if book_w is not None and sleeve_w is not None:
        parts.append(f"Capital splits {_pct(book_w)} to the book and "
                     f"{_pct(sleeve_w)} to the sleeve.")
    if tilt is not None:
        parts.append(f"The signal is sector-residualised at a tilt of {tilt} — "
                     f"momentum measured against sector peers rather than raw.")
    parts.append(
        "\n\nThe evidence: a survivorship-corrected point-in-time backtest, "
        "2021–2026, returned materially above the benchmark on both absolute and "
        "risk-adjusted terms, with no position held into a delisting.")
    return " ".join(parts)


def render(mandate_cfg: dict, strat_cfg: dict, tool_names,
           template: str | None = None) -> str:
    """Fill the charter. Raises if any placeholder survives."""
    text = template if template is not None else TEMPLATE.read_text()
    gov_cfg = strat_cfg.get("governance", {})
    conc = mandate_cfg.get("concentration", {}).get("max_position_pct", 0.0)
    subs = {
        "__MANDATE__": render_mandate(mandate_cfg),
        "__TERMS__": render_terms(gov_cfg),
        "__GATE__": render_gate(gov_cfg),
        "__TOOLS__": render_tools(tool_names),
        "__BASELINE__": render_baseline(strat_cfg),
        "__UNIVERSE__": render_universe(strat_cfg),
        "__ANNOUNCE_PCT__": _pct(float(conc) * ANNOUNCE_FRACTION),
        "__ANNOUNCE_FRACTION__": _pct(ANNOUNCE_FRACTION),
    }
    for key, val in subs.items():
        text = text.replace(key, val)
    # Strip the author-facing markers. `<!-- historical -->` exists so
    # check_charter_no_literals can tell a measured past result (which cannot
    # drift) from a live threshold (which can). It is a note to whoever edits the
    # TEMPLATE, and it must never reach the agent: rendered mid-sentence it read
    # "...produced 78% winners that lost money, because the <!-- historical -->
    # losers were larger", corrupting the sentence it was meant to annotate.
    text = re.sub(r"[ \t]*<!--.*?-->", "", text, flags=re.S)
    left = [k for k in subs if k in text]
    if left:
        raise ValueError(f"charter placeholders not substituted: {left}")
    if "__" in text and any(seg.isupper() for seg in text.split("__")[1::2]):
        raise ValueError("charter still contains an UNKNOWN __PLACEHOLDER__")
    return text


def _selftest() -> None:
    MCFG = {"drawdown": {"max_pct": 0.20},
            "concentration": {"max_position_pct": 0.15},
            "pnl_concentration": {"window_days": 90, "max_single_share": 0.40,
                                  "min_distinct_names": 4},
            "relative_return": {"window_days": 60, "benchmark": "SPY"}}
    SCFG = {"governance": {"max_order_pct": 0.15,
                           "min_dollar_volume_20d": 50_000_000.0},
            "portfolio": {"book_hold": 10, "sleeve_hold": 4,
                          "book_weight": 0.70, "sleeve_weight": 0.30},
            "signal": {"residual_tilt": 0.75}}
    TOOLS = ["mcp__agentic-trader__brief", "mcp__agentic-trader__quote",
             "mcp__agentic-trader__check_order", "mcp__agentic-trader__performance",
             "mcp__agentic-trader__wake_register", "mcp__agentic-trader__ping"]

    out = render(MCFG, SCFG, TOOLS)

    # every number is INTERPOLATED -- change the config, the charter changes
    assert "20%" in out and "15%" in out, out[:400]
    alt = render(dict(MCFG, drawdown={"max_pct": 0.33}), SCFG, TOOLS)
    assert "33%" in alt and "20%" not in alt.split("Drawdown")[1][:80], alt[:400]

    # announce fires BELOW the blocking limit, derived from it -- never a 2nd number
    assert "12%" in out, "announce trigger must be 80% of the 15% concentration cap"

    # no placeholder survives
    for ph in ("__MANDATE__", "__TERMS__", "__GATE__", "__TOOLS__",
               "__BASELINE__", "__ANNOUNCE_PCT__", "__ANNOUNCE_FRACTION__",
               "__UNIVERSE__"):
        assert ph not in out, ph

    # ⚠️ THE INACTION-DRIFT GUARD. The objective must read as MAKING MONEY, and
    # sitting out must carry a HIGHER burden of proof than acting, with the
    # admissible evidence enumerated. Observed on a sibling project: given a soft
    # "holding is legitimate if you give a reason", the agent held indefinitely
    # and produced a fresh justification each session. A narrative is always
    # available; a number is not.
    assert "make money trading this book" in out
    assert "HIGHER burden of proof than acting" in out
    assert "you must cite a FACT" in out
    assert "NOT admissible" in out
    assert "If you cannot name the number, you do not have the evidence." in out
    # ...and it must NOT become a churn mandate
    assert "This is not a quota" in out
    assert "Churn for its own sake is worse than stillness" in out

    # ⚠️ THE SESSIONS ARE NOT INTERCHANGEABLE. Each answers a different question,
    # and two properties are load-bearing:
    #   - gap risk is priced at ENTRY, because extended/overnight sessions reject
    #     fractional and dollar-based orders outright (verified against the order
    #     tool's own schema) -- there is NO action available between the bells at
    #     any price, so 15:45 cannot be a place to react to it
    #   - 15:45 opens nothing: fifteen minutes before losing control of a position
    #     for 17.5 hours is not when a new one gets opened
    for must in ("10:00 — THE BOOK", "12:00 — WHAT CHANGED",
                 "15:45 — IS EVERYTHING STILL TRUE",
                 "Gap risk is priced HERE, at entry",
                 "This session does not open positions",
                 "Write to tomorrow"):
        assert must in out, f"session definition lost: {must}"
    # the 15:45 handoff is what makes research_log a HANDOFF and not an archive
    assert "The 10:00 session\nreads these" in out or "10:00 session" in out.split("Write to tomorrow")[1][:400]

    # ⚠️ PRESERVE WHAT WORKS. The de-risk trim is the behaviour with the best
    # measured record in this system (two AMAT trims, +6.53% and +3.38%, both
    # ahead of a known earnings date). A charter saying only "reduce, close or
    # hold" reads as binary and would quietly discard it.
    assert "Trimming is a first-class action" in out
    assert "not a half-measure" in out
    assert "twice in one day" in out, "the no-double-trim discipline was dropped"
    # ⚠️ DISPOSITION-EFFECT GUARD. Every trim example in the record is a GAIN, which
    # anchors the action as profit-taking and would leave losers held. Trimming is
    # about exposure and applies identically to a losing position; the entry price
    # is not information. This is the single most common bias in retail trading
    # writing, which is exactly what a model would have absorbed.
    assert "A trim is about EXPOSURE, not profit" in out
    assert "applies equally to a loser" in out
    assert "Do not treat your entry price as information" in out

    # ⚠️ THE ANTI-CIRCUMVENTION SECTION. Written from an honest assessment of how
    # THIS model actually behaves, not from a threat model -- every item listed
    # happened during construction. It is the section most likely to be trimmed
    # as "negative" or "redundant", so each clause is pinned.
    for must in ("Do not retry a refusal with variation",
                 "Do not split an order",
                 "satisfy the letter of a check while defeating its purpose",
                 "change what a rule computes from",
                 "Do not record a reason that is not the actual reason",
                 "Do not resolve an ambiguity",
                 "Do not treat your own earlier output as verified",
                 "Do not assume when checking is available",
                 "Do not report done when it is partly done"):
        assert must in out, f"anti-circumvention clause lost: {must}"

    # the opening ORIENTS: role, capital, horizon, and what is off-limits
    for must in ("THE JOB", "objective", "horizon", "Options", "Short selling",
                 "Margin or leverage", "WHY YOU ARE HERE RIGHT NOW"):
        assert must in out, f"charter opening lost: {must}"
    # ...and the job is stated BEFORE the machinery
    assert out.index("THE JOB") < out.index("WHAT WILL REFUSE YOU")

    # the venue split is stated, and BEFORE the tool list
    assert "moomoo is DATA" in out
    assert out.index("moomoo is DATA") < out.index("WHAT YOU HAVE")

    # the tool list is COMPLETE: an unknown tool surfaces, never vanishes
    odd = render(MCFG, SCFG, TOOLS + ["mcp__agentic-trader__brand_new_thing"])
    assert "brand_new_thing" in odd, "an unrecognised tool must not be dropped"

    # the deviation allowance is present and unhedged
    assert "belief, not a rule" in out
    assert "not a violation" in out

    # a SELL's protection is stated
    assert "refused by nothing but the kill switch" in out

    # the author-facing marker must NEVER reach the agent -- rendered inline it
    # corrupted the very sentence it annotated
    assert "<!--" not in out and "-->" not in out, "HTML comment leaked into the charter"
    flat = " ".join(out.split())
    assert "78% winners that lost money, because the losers were larger" in flat, \
        "stripping the marker must not damage the sentence around it"

    # a surviving unknown placeholder is an ERROR, not silently shipped
    try:
        render(MCFG, SCFG, TOOLS, template="hello __MYSTERY__ world")
        raise AssertionError("an unknown placeholder must raise")
    except ValueError:
        pass

    # renders against the REAL config and the REAL template
    import strategy                                  # noqa: PLC0415
    import tomllib                                   # noqa: PLC0415
    real_m = tomllib.loads((REPO / "config" / "mandate.toml").read_text())
    real = render(real_m, strategy.load(), TOOLS)
    assert len(real) > 2000, "real charter suspiciously short"
    assert "__" not in real.replace("__pycache__", ""), "placeholder left in real render"

    # ⚠️ THE SILENT-OMISSION GUARD. render_baseline reads config keys by name and
    # simply SKIPS a clause whose key is missing, so a renamed key would leave the
    # agent an incomplete house view with no error anywhere. Assert against the
    # REAL config that each clause actually made it in -- renaming book_hold or
    # sleeve_weight in strategy.toml must break this test, not the charter.
    real_port = strategy.load().get("portfolio", {})
    assert str(real_port["book_hold"]) in real, "book_hold clause vanished"
    assert str(real_port["sleeve_hold"]) in real, "sleeve_hold clause vanished"
    assert _pct(real_port["book_weight"]) in real, "book_weight clause vanished"
    assert str(strategy.load()["signal"]["residual_tilt"]) in real, "tilt vanished"

    print("charter: OK — every number derived, nothing dropped, unknown placeholder raises")


if __name__ == "__main__":
    _selftest()
