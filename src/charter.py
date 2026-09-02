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
        "⚠️ THESE ARE MEASURED AND REPORTED. NOTHING ACTS ON THEM. Nothing "
        "flattens the book on a breach and nothing blocks an order because a "
        "criterion failed -- verified, not assumed: the function that would turn "
        "a breach into an action has no caller, and the order gate never reads "
        "mandate status. You are the enforcement. Do not carry a position because "
        "you believe the machine would stop you; it will not.",
        "",
        "Two criteria are the ones that MATTER MOST, and you police them:",
        "",
        f"- **Drawdown** — no more than {_pct(dd)} below the all-time high-water "
        f"mark, measured close-to-close. Never intraday: an intraday measure "
        f"fires on noise.",
        f"- **Concentration** — no single position above {_pct(conc)} of equity, "
        f"at any mark. ⚠️ The order gate caps a single ORDER's notional, never "
        f"the resulting POSITION: two adds under the cap, or one entry that "
        f"rallies, produces a breach with every order passing cleanly. This is "
        f"the number you police, and it is PER NAME.",
        f"",
        f"  ⛔ It is not a sector or theme measure and must not be read as one. "
        f"This line used to add that the criterion was \"blind to sector — "
        f"several names in one industry read as diversified\", which invited "
        f"exactly the reading that THEME CONCENTRATION below rules out: a large "
        f"share of equity in one theme is NOT a reason to act, because no theme "
        f"limit exists anywhere in your mandate. Sessions took the hint, found "
        f"no threshold to apply, invented one (52.4%, then ~35%, then ~40%), and "
        f"wrote it into rule-outs the order gate ENFORCES. Read THEME "
        f"CONCENTRATION for the four specific mechanisms that ARE reasons.",
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
        # Day trading, options and the account-type fact are stated once each in
        # "WHAT YOU CAN AND CANNOT TRADE" and "WHAT IS TRUE ABOUT THIS SYSTEM".
        # Repeating them here read as three separate rules about the same thing.
        # CHANGED 2026-08-18: the account moved from cash to LIMITED MARGIN, so
        # the T+1 clause this line carried for months is simply no longer true.
        "- **Settlement** — same session. This is a limited-margin account: "
        "sale proceeds are spendable immediately, not at T+1. Size buys against "
        "`buying_power` — it is the figure the broker checks an order against.",
        f"- **Liquidity floor** — names below "
        f"${float(gov_cfg.get('min_dollar_volume_20d', 0)):,.0f} of 20-day dollar "
        f"volume are flagged advisory, never blocked.",
        "- **Observe the data feed's rate limits.** Paced in code; you will not "
        "normally meet them.",
    ])


def render_gate(gov_cfg: dict) -> str:
    # ⛔ THIS LIST SAYS "It refuses:" AND IS READ AS COMPLETE. Every refusal in
    # scripts/hooks/pretooluse_order_gate.py belongs here. The drawdown halt and
    # the rule-out limb were both missing until 2026-08-18 -- an agent could be
    # refused for a reason the charter never named, and the drawdown halt is the
    # one refusal it cannot diagnose from its own tools.
    #
    # ⛔ AND IT WAS STILL INCOMPLETE. Found 2026-08-23 by walking decide()'s
    # branches against this list: `live_approved` and the stop-out COOLDOWN both
    # refuse a live BUY and neither was named, and three malformed-order denials
    # (unreadable order record, side neither buy nor sell, a buy the gate cannot
    # size) were absent too. The list is now ordered to match decide()'s own
    # evaluation order, so the next reader can walk the two side by side. If you
    # add a branch to decide(), add it here in the same position.
    #
    # ⛔ NO LITERAL for the cooldown duration. [monitor] cooldown_days is not in
    # the governance table this function is handed, and a number typed here would
    # be both undeclared and drift-prone. The gate's own refusal text names the
    # actual date, so the charter says "until the date it names" and stays true.
    order_pct = gov_cfg.get("max_order_pct")
    max_dd = gov_cfg.get("max_drawdown")
    # ⛔ SPLIT BY SIDE, because the previous single list was WRONG in three ways
    # and an agent read all of them (found by an independent Codex audit of the
    # rendered charter, 2026-08-18, each verified against the code):
    #   - "A SELL is refused by nothing but the kill switch" -- false. SHADOW
    #     refuses sells too; the gate's own text says "including sells". An
    #     agent believing it can always exit in shadow mode is the dangerous one.
    #   - the per-order cap was listed as applying to "any order" -- it is
    #     BUYS ONLY (governance.py: capping a sell would strand a position the
    #     system is trying to exit).
    #   - "any order in an account other than the one Agentic account" -- the
    #     hook performs NO account check; that protection is at the broker.
    return "\n".join([
        "Before any order reaches the broker it passes a gate that runs in the "
        "harness, not in your judgment. This is every refusal it can produce, in "
        "the order it applies them.",
        "",
        "**Refused whatever the side, buy or sell:**",
        "",
        "- an order record the gate cannot read, or one whose side is neither "
        "buy nor sell — it cannot tell whether the order adds risk, so it "
        "refuses rather than guessing",
        "- any order while the **kill switch** is set — exits must then be "
        "placed by hand",
        "- **every** order while shadow mode is set, sells included",
        "",
        "**Refused for a BUY only.** An exit is never blocked by these: the stop "
        "here is software, so blocking a sell would remove a position's only "
        "protection.",
        "",
        "- a buy while this box is not armed to open positions at all — the "
        "`live_approved` master switch is off. Exits stay available, for the "
        "same reason the drawdown halt leaves them available.",
        "- a buy while entries are halted",
        f"- a buy while the book is more than **{_pct(max_dd)}** below its "
        f"tracked equity peak",
        "- a buy in a name the monitor stopped out recently, while its recorded "
        "COOLDOWN is still running — the refusal names the date it runs to. A "
        "name can stop out and still be top-ranked; the cooldown is why the "
        "rebuy is refused today rather than never.",
        "- a buy in a name carrying an active `rule_out`, until `revisit()` "
        "clears it",
        "- a buy the gate cannot SIZE — no dollar amount, and no quantity × "
        "limit price to multiply out. It must not fetch a quote on the critical "
        "path, so it refuses rather than guessing: **place buys as a dollar "
        "amount.** The same limb refuses a buy whose amount, or the account "
        "value the cap is computed from, is missing or not a finite number.",
        "- a buy for a symbol outside the configured universe",
        f"- a buy whose notional exceeds **{_pct(order_pct)} of equity**",
        "",
        "One more, and it is not about your order: the gate FAILS CLOSED. If it "
        "cannot reach a verdict at all — an unreadable config, a bug in it — it "
        "denies and names the exception. Report that one; do not retry it.",
        "",
        "The account you may trade in is enforced at the broker, not by this "
        "gate.",
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
        ("PRICE", ("quote", "depth", "terrain", "history")),
        ("EVENTS", ("earnings", "macro_calendar", "macro", "news")),
        ("DECIDE", ("check_order", "set_levels", "clear_levels",
                    "record_decision", "announce")),
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

    Derived, not stated: the universe is re-cut weekly and a literal here
    would be wrong within months. Counting the file is the only figure that
    cannot drift from what candidates()/universe() will actually show.
    """
    repo = repo or REPO
    # ONE universe. This looped over ("universe", "etf_sleeve") until
    # 2026-08-20 and told the agent its hunting ground was "150 single names and
    # 18 ETFs" — four days after the sleeve was retired and three after its
    # positions were sold. The charter is the agent's whole standing account of
    # the game, so a retired allocation named here is not cosmetic.
    counts = []
    src = strat_cfg.get("universe", {}).get("source")
    if src and (repo / src).is_file():
        rows = [ln for ln in (repo / src).read_text().splitlines()[1:] if ln.strip()]
        counts.append(f"{len(rows)} single names")
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
    book_w = port.get("book_weight")
    tilt = sig.get("residual_tilt")
    parts = ["The house currently believes: **cross-sectional momentum**, "
             "long-only, rebalanced weekly."]
    if book_hold is not None:
        parts.append(f"Top {book_hold} single names. Equities only.")
    if book_w is not None:
        parts.append(f"Capital allocates {_pct(book_w)} to the single-name book.")
    if tilt is not None:
        parts.append(f"The signal is sector-residualised at a tilt of {tilt} — "
                     f"momentum measured against sector peers rather than raw. "
                     f"The sector series that makes that subtraction possible is "
                     f"read-only market data, not something you can hold.")
    # ⛔ DEFINE THE SIGNAL, do not just name it. "cross-sectional momentum"
    # appeared once and was never unpacked, so the agent could not tell what it
    # ranks on, what makes a name eligible, or what releases a holding -- and
    # filled the gap with generic portfolio management (2026-08-18).
    band = port.get("book_band")
    how = ["\n\n**What that means, concretely.** Each name is scored by "
           "percentile-ranking two views across the eligible universe and "
           "averaging them: risk-adjusted 12-month return, and trend (the close "
           "divided by its 200-day mean)."]
    how.append("A name is **eligible only if its 12-month return is positive** — "
               "that is the absolute filter, and it is what takes this book to "
               "cash when a trend breaks, since it has no short leg.")
    if book_hold is not None:
        how.append(f"The book holds the top {book_hold}.")
    if band is not None:
        how.append(f"A name you already hold is retained until it falls below "
                   f"rank {band} — that band exists so a name slipping one place "
                   f"does not churn out and straight back in.")
    parts.append(" ".join(how))
    parts.append(
        "\n\n**Weekly means the ROTATION, not the risk.** The full re-rank and "
        "rotation happens once a week, on Sunday. Weekday sessions are not "
        "rebalances — they manage the book you already hold. That does not "
        "restrict exits: a stop, a target, a trim, a thesis that broke, or a "
        "same-day close are available to you on any session.")
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
        # The post-fill arming window (SIZING AND STOPS). Strict key access on
        # purpose: scripts/market_monitor.py reads cfg["monitor"]["poll_secs"]
        # the same way, so the charter cannot quote an interval the monitor
        # does not run -- and a missing key raises here rather than rendering
        # "up to 0 seconds".
        "__MONITOR_POLL_SECS__": str(int(strat_cfg["monitor"]["poll_secs"])),
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
                           "max_drawdown": 0.25,
                           "min_dollar_volume_20d": 50_000_000.0},
            "portfolio": {"book_hold": 14, "book_weight": 1.0},
            "signal": {"residual_tilt": 0.75},
            "monitor": {"poll_secs": 15}}
    TOOLS = ["mcp__agentic-trader__brief", "mcp__agentic-trader__quote",
             "mcp__agentic-trader__check_order", "mcp__agentic-trader__performance",
             "mcp__agentic-trader__wake_register", "mcp__agentic-trader__ping"]

    out = render(MCFG, SCFG, TOOLS)

    # ⛔ THE CHARTER MUST NOT NAME A SLEEVE. It rendered "150 single names and
    # 18 ETFs" as the agent's hunting ground for four days after the sleeve was
    # retired and three after its positions were sold. The charter is the whole
    # standing account of the game the agent is playing, so this is not cosmetic:
    # a retired allocation named here reads as a live one.
    baseline = render_baseline(SCFG)
    assert "Top 14 single names. Equities only." in baseline, baseline
    assert "Capital allocates 100% to the single-name book" in baseline, baseline
    assert "sleeve" not in baseline.lower(), baseline
    assert "ETF" not in baseline, baseline
    # a stale [etf_sleeve] table left in a local override must change NOTHING
    stale = render_baseline(dict(SCFG, etf_sleeve={"enabled": True},
                                 portfolio=dict(SCFG["portfolio"],
                                                sleeve_hold=4, sleeve_weight=0.3)))
    assert stale == baseline, "a stale sleeve config resurrected the sleeve"

    # every number is INTERPOLATED -- change the config, the charter changes
    assert "20%" in out and "15%" in out, out[:400]
    alt = render(dict(MCFG, drawdown={"max_pct": 0.33}), SCFG, TOOLS)
    assert "33%" in alt and "20%" not in alt.split("Drawdown")[1][:80], alt[:400]

    # announce fires BELOW the blocking limit, derived from it -- never a 2nd number
    assert "12%" in out, "announce trigger must be 80% of the 15% concentration cap"

    # ⚠️ AN ANNOUNCEMENT IS NOT A VETO. The charter said these classes were pushed
    # "so a human can veto the unusual" -- false in the way that matters: a push
    # does not block, the loops are headless, and nobody is at the other end. An
    # agent that believed it had been vetted by a human would defer to a review
    # that never happened. State what is true instead: it is seen as it happens,
    # and the kill switch is the intervention. The tool must also be NAMED, or the
    # first class (wholesale abandonment, undetectable from any order) has no way
    # to be announced at all -- the gap this text used to paper over.
    assert "can veto" not in out, "an announcement must never be described as a veto"
    assert "A push is a notification, not a veto." in out
    assert "announce, then proceed" in out
    assert "`announce()`" in out, "the announce tool must be named where it is needed"
    assert "announce" in render_tools(TOOLS + ["mcp__agentic-trader__announce"]).split("DECIDE")[1][:120]

    # no placeholder survives
    for ph in ("__MANDATE__", "__TERMS__", "__GATE__", "__TOOLS__",
               "__BASELINE__", "__ANNOUNCE_PCT__", "__ANNOUNCE_FRACTION__",
               "__UNIVERSE__", "__MONITOR_POLL_SECS__"):
        assert ph not in out, ph
    # the arming window is the monitor's real interval, interpolated
    assert "up to 15\nseconds later" in out or "up to 15 seconds later" in out, \
        "monitor poll interval did not reach the charter"
    alt_poll = render(MCFG, dict(SCFG, monitor={"poll_secs": 7}), TOOLS)
    assert "up to 7\nseconds later" in alt_poll or "up to 7 seconds later" in alt_poll

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
    #     any price, so 15:15 cannot be a place to react to it
    #   - 15:15 opens nothing: forty-five minutes before losing control of a
    #     position for 17.5 hours is not when a new one gets opened
    #
    # ⛔ THE TIMES AND THE COUNT MUST MATCH deploy/agentic-session@{open,close}
    # .timer. Until 2026-08-18 this charter described THREE sessions -- 10:00,
    # 12:00 and 15:45 -- and asserted all three here. Only two have ever
    # existed, at 10:35 and 15:15. Nothing caught it because the assertion
    # tested the charter against ITSELF. An agent told a midday session is
    # coming can defer a decision to a session that never runs, so the phantom
    # is asserted ABSENT below, not merely unmentioned.
    for must in ("10:35 — THE BOOK",
                 "15:15 — IS EVERYTHING STILL TRUE",
                 "Two run each weekday",
                 "Gap risk is priced HERE, at entry",
                 "This session does not open positions",
                 "Write to tomorrow"):
        assert must in out, f"session definition lost: {must}"
    for phantom in ("12:00", "15:45", "10:00 — THE BOOK", "Three run each weekday"):
        assert phantom not in out, f"a session that does not run reappeared: {phantom}"
    # the 15:15 handoff is what makes research_log a HANDOFF and not an archive
    assert "10:35 session" in out.split("Write to tomorrow")[1][:400]

    # ⚠️ PRESERVE THE ACTION, NOT A RESULT. A charter saying only "reduce, close
    # or hold" reads as binary and quietly discards the partial. So the ACTION is
    # pinned here as first-class. What is deliberately NOT pinned -- and was
    # removed from the charter on 2026-08-23 -- is the standing empirical claim
    # that trims had the best measured record: a result written into standing
    # policy cannot be revised by later evidence and cannot go stale, which is
    # exactly the job the institutional-evidence layer now does under mechanical
    # validation and staleness. Do not put a measured result back in here.
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

    # Publishing one broker page as the whole book silently deletes holdings on
    # later pages. The charter must demand evidence of cursor exhaustion, not a
    # position-count guess or comparison with the previous snapshot.
    for must in ("continue until a response has no `next`",
                 "every later cursor must match the prior page's",
                 "Completeness is evidence you supply",
                 "pagination exhaustion is unproven"):
        assert must in out, f"pagination-completeness instruction lost: {must}"

    # ⚠️ CLAIMS THAT WERE FALSE AND ARE NOW PINNED. Three independent audits found
    # the charter asserting safety properties the code does not provide -- the most
    # dangerous class of error here, because the agent cannot check and will rely.
    assert "a level is not enforced until the tool says it is" in out.lower()
    assert "enforcement.stop.enforced: true" in out
    assert "could not be given an enforced stop" in out
    # ⚠️ THE CHARTER MUST NOT GRANT A PERMISSION THE GATE REFUSES. Three passages
    # told the agent it could trade off-universe ("you may act on one", "if you
    # take one", and an announce-first class for it) while require_whitelist=true
    # makes vet_plan deny every such buy. A fictional permission is worse than a
    # stated limit: the agent plans around it and is refused with no explanation
    # it was given in advance.
    assert "cannot currently BUY what it" in out
    assert "reachable only if the" in out
    assert "in either direction" in out, "level adjustment must not read one-way"
    # ⚠️ THIS ASSERTION WAS INVERTED AGAIN ON 2026-08-17 (levels-mechanism),
    # deliberately, back to what it pinned before the FIRST inversion the same
    # day. That earlier inversion pinned "never loosened" because set_levels,
    # merge_levels and write_levels had no `widen` parameter -- the charter's
    # old promise that a stop COULD be loosened when marked deliberately was a
    # fiction the agent's tools could not back up, demonstrated live on MRK:
    # the agent reasoned a stop from measured terrain, the deterministic
    # formula stop out-raised it, and apply_overrides()'s stricter-only rule
    # made the agent's number unreachable with no route to override it. Those
    # three functions now accept `widen` end to end (decide.py, server.py),
    # so "never loosened" is a limit that no longer describes the mechanism --
    # pinning it would be exactly the old fiction, just facing the other way.
    # Pin the CAPABILITY, or this can flip back unnoticed a third time.
    assert "loosened, but only when you mark it deliberately" in out, \
        "the charter must state the widen mechanism now that it exists"
    assert "widen=True" in out, "the charter must name the actual parameter"
    assert "never loosened" not in out, \
        "the charter must not re-promise the OLD limit the tools now contradict"
    assert "NOTHING ACTS ON THEM" in out, "the mandate is advisory; do not reclaim it"
    assert "You are the enforcement" in out
    assert "never\nthe resulting POSITION" in out or "never the resulting POSITION" in out.replace("\n"," ")
    assert "2026-07-23" in out, "the agent must know its record starts here"
    # ⚠️ Pinned as an INSTRUCTION, not a verdict. An earlier draft added "the
    # record is unflattering", "closes have on average lost money", "it does not
    # yet show an edge". All true, none of it changing a single decision -- and a
    # trader told its evidence is worthless has been handed a rigorous-sounding
    # reason to do nothing. Facts belong here only where they change an action.
    assert "unflattering" not in out
    assert "does not yet show" not in out
    assert "Pace is a decision" in out, "the deployment brake was removed"
    # Every position carries BOTH levels, and the agent is told the check exists.
    assert "BOTH a stop and a take-profit target" in out
    assert "Neither is\noptional" in out or "Neither is optional" in out.replace("\n", " ")
    assert "This is checked, continuously" in out
    assert "Do not treat the check as the safety" in out
    # ⚠️ The single largest realised loss in this book came from a mechanism the
    # charter did not describe: 11 of 18 closes fired in ONE minute on 2026-07-27
    # when the regime gate flipped, mean -7.65%. An agent cannot reason about its
    # biggest risk if nobody tells it the risk exists.
    #
    # That mechanism was REMOVED (regime_filter, 2026-08-12; the execution pass
    # that acted on the empty book, 2026-08-14), so this no longer pins the
    # warning -- it pins the CORRECTION. These assertions used to require the
    # text "LIQUIDATE THE BOOK WITHOUT ASKING YOU", which kept a false hazard
    # in front of the agent and would have failed the moment anyone told it the
    # truth. A test that pins a claim outlives the claim; when the system
    # changes, the test has to move with it or it defends the stale version.
    assert "Nothing liquidates your book but you" in out
    assert "2026-07-27" in out, "the evidence for the regime risk was dropped"
    # ⚠️ The EVIDENCE stays even though the hazard is gone: "nothing liquidates
    # your book but you" is a claim the agent should be able to check, and the
    # reason it became true is what makes it checkable.
    assert "eleven positions closed in a single minute" in out
    assert "observation about the market, not a rule that acts" in out, \
        "the regime must be framed as a fact, never as an authority"
    assert "judgment is\n  yours" in out or "judgment is yours" in out.replace("\n  ", " ")

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
    # ...but it is no longer a blank cheque: deviating on SELECTION and on RISK
    # carry different burdens. "the only thing it costs you is a recorded
    # reason" was the sentence that licensed rank-34-38 buys on 2026-08-18.
    assert "Deviating on RISK" in out and "Deviating on SELECTION" in out
    assert "the only thing it costs you is a recorded" not in out

    # ⛔ DEFINE, DO NOT NAME. Each of these was named and never defined, and the
    # agent filled the gap with generic portfolio management (2026-08-18).
    assert "A swing trade here opens" in out, "the trade itself must be defined"
    assert "It lasts days to weeks" in out
    assert "percentile-ranking two views" in out, "the signal must be defined"
    assert "eligible only if its 12-month return is positive" in out
    assert "A **thesis** is the reason you opened a position" in out

    # ⛔ EVERY BOUNDARY STATES ITS EXCEPTION, or the agent invents a prohibition.
    assert "You MAY close a position the same day you opened it" in out
    assert "not a minimum" in out or "not a promise to hold" in out
    assert "does not forbid selling\nthe remainder" in out or \
           "It does not forbid selling" in out
    assert "This is about anchoring, and nothing more" in out
    assert "except when you are\n  executing the weekly rotation" in out or \
           "executing the weekly rotation" in out
    assert "Weekly means the ROTATION, not the risk" in out

    # ⛔ CONCENTRATION IS CONDITIONS, NOT A DISPOSITION. Stated flatly either way
    # ("always fine" / "always a risk") produces the wrong behaviour; the agent
    # needs the cases that distinguish them.
    assert "These are NOT reasons to reduce a theme" in out
    assert "These ARE reasons, and each names a specific mechanism" in out
    assert "It fell hard today" in out, "the one-day-drawdown non-reason was lost"
    assert "Clustered stop risk" in out
    assert "leaves the denominator unchanged" in out, \
        "the arithmetic a session got wrong on 2026-08-18 must be stated"

    # the two guards that became judgement when fast_loop was deleted, and were
    # named NOWHERE in the charter until 2026-08-18
    assert "compare the live price with the thesis entry zone" in out
    assert "rebuying a name that recently stopped out" in out

    # ⛔ WHAT REFUSES A SELL MUST BE EXACTLY TRUE. This asserted "refused by
    # nothing but the kill switch" until 2026-08-18, which was FALSE -- shadow
    # mode refuses sells too, and an agent believing it can always exit is the
    # dangerous direction of that error. The gate is now split by side; these
    # assertions prove both halves survive and that the false claim is gone.
    assert "Refused whatever the side, buy or sell" in out
    assert "shadow mode is set, sells included" in out
    assert "Refused for a BUY only" in out
    assert "refused by nothing but the kill switch" not in out, \
        "the false sell-protection claim came back"
    # the per-order cap and the whitelist are BUY-side; a sell is never capped
    for buy_only in ("a buy whose notional exceeds", "a buy for a symbol outside",
                     "a buy while entries are halted"):
        assert buy_only in out, buy_only
    # the gate does not check the account; saying it does invents a protection
    assert "enforced at the broker, not by this gate" in out

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
    # REAL config that each applicable clause actually made it in -- renaming a
    # live-sleeve key must break this test, while a retired sleeve must be named
    # as retired rather than rendered as a zero-holding allocation.
    real_port = strategy.load().get("portfolio", {})
    assert str(real_port["book_hold"]) in real, "book_hold clause vanished"
    assert _pct(real_port["book_weight"]) in real, "book_weight clause vanished"
    # ⛔ AND THE REAL CHARTER MUST NOT MENTION A SLEEVE OR AN ETF AT ALL.
    # Deleted 2026-08-20. Previously this branch asserted the retired sleeve was
    # *named as retired* — which still put "ETF sleeve" in front of the agent
    # every session. There is no sleeve; the correct rendering is silence.
    assert "sleeve" not in real.lower(), "the deleted sleeve is still in the charter"
    assert "Capital splits" not in real, "a two-engine capital split is still rendered"
    for gone in ("sleeve_hold", "sleeve_weight"):
        assert gone not in real_port, f"{gone} is still in [portfolio]"
    assert str(strategy.load()["signal"]["residual_tilt"]) in real, "tilt vanished"
    assert (f"up to {int(strategy.load()['monitor']['poll_secs'])}\nseconds later" in real
            or f"up to {int(strategy.load()['monitor']['poll_secs'])} seconds later" in real), \
        "monitor poll interval vanished from the real render"

    print("charter: OK — every number derived, nothing dropped, unknown placeholder raises")


if __name__ == "__main__":
    _selftest()
