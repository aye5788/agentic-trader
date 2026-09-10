#!/usr/bin/env python3
"""WATCH A LIVE SESSION — a read-only renderer for the session's stream-json.

scripts/session.py spawns `claude -p --output-format stream-json` with stdout
pointed AT A FILE (logs/session_stream.<mode>.jsonl), because a piped stdout is
invisible until the child exits. That file is therefore already a live,
event-by-event transcript: this script just tails it and prints it in a form a
human can follow while the session is still running.

⛔ STRICTLY READ-ONLY, AND DELIBERATELY DEPENDENCY-FREE. It opens the log for
reading and nothing else: no repo imports, no config, no network, no broker, no
writes. It cannot influence the session it is watching, and if it crashes the
session does not notice. Stdlib only so it runs under any of the box's three
interpreters.

    scripts/watch_session.py open              # wait for + follow the next open session
    scripts/watch_session.py close --replay    # re-read the last close session from the top
    scripts/watch_session.py open --quiet      # tool calls + orders only, no prose

The file is opened with "w" by session.py, so a NEW session TRUNCATES it in
place. Truncation is detected (size < offset) and treated as a new run rather
than as an error — that is exactly the moment the session we are waiting for
began.
"""
import argparse, json, os, sys, time
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Tools whose invocation is the whole point of watching. An order is not just
# another tool call and must not scroll past looking like one.
LOUD = {"mcp__robinhood-trading__place_equity_order": "🔴 ORDER PLACED",
        "mcp__robinhood-trading__cancel_equity_order": "🔴 ORDER CANCELLED",
        "mcp__agentic-trader__set_levels":            "🟠 LEVELS SET",
        "mcp__agentic-trader__clear_levels":          "🟠 LEVELS CLEARED",
        "mcp__agentic-trader__record_decision":       "🟡 DECISION RECORDED",
        "mcp__agentic-trader__rule_out":              "🟡 RULED OUT",
        "mcp__robinhood-trading__review_equity_order": "🔵 order review"}


def clock():
    return datetime.now().strftime("%H:%M:%S")


def short(name):
    """mcp__agentic-trader__brief -> at:brief ; Bash -> Bash"""
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1].replace("agentic-trader", "at").replace("robinhood-trading", "rh")
        return f"{server}:{parts[-1]}"
    return name


def flat(value, limit):
    """One-line, length-bounded rendering of arbitrary JSON."""
    if isinstance(value, (dict, list)):
        try:
            value = json.dumps(value, separators=(",", ":"))
        except Exception:
            value = repr(value)
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def emit(line):
    print(line, flush=True)


def render(event, quiet):
    kind = event.get("type")

    if kind == "system" and event.get("subtype") == "init":
        emit(f"\n{'=' * 72}")
        emit(f"{clock()}  ▶  SESSION STARTED   model={event.get('model', '?')}")
        emit(f"{'=' * 72}")
        return

    if kind == "assistant":
        for block in event.get("message", {}).get("content", []) or []:
            btype = block.get("type")
            if btype == "text" and not quiet:
                text = (block.get("text") or "").strip()
                for para in [p for p in text.split("\n") if p.strip()]:
                    emit(f"{clock()}  │ {para}")
            elif btype == "tool_use":
                name = block.get("name", "?")
                args = flat(block.get("input", {}), 240)
                banner = LOUD.get(name)
                if banner:
                    emit("")
                    emit(f"{clock()}  {banner}  {short(name)}")
                    emit(f"{' ' * 10} {args}")
                    emit("")
                else:
                    emit(f"{clock()}  →  {short(name)}  {args}")
            elif btype == "thinking" and not quiet:
                emit(f"{clock()}  ·  (thinking)")
        return

    if kind == "user":
        for block in event.get("message", {}).get("content", []) or []:
            if block.get("type") != "tool_result":
                continue
            body = block.get("content")
            if isinstance(body, list):
                body = " ".join(p.get("text", "") for p in body if isinstance(p, dict))
            mark = "✗ ERROR" if block.get("is_error") else "←"
            emit(f"{clock()}  {mark}  {flat(body, 200 if not block.get('is_error') else 400)}")
        return

    if kind == "result":
        cost = event.get("total_cost_usd")
        secs = (event.get("duration_ms") or 0) / 1000.0
        emit("")
        emit(f"{'=' * 72}")
        emit(f"{clock()}  ■  SESSION ENDED   {event.get('subtype', '?')}   "
             f"turns={event.get('num_turns', '?')}  "
             f"{secs / 60:.1f}min  " + (f"${cost:.2f}" if isinstance(cost, (int, float)) else ""))
        text = (event.get("result") or "").strip()
        if text:
            emit("")
            for para in [p for p in text.split("\n") if p.strip()]:
                emit(f"  {para}")
        emit(f"{'=' * 72}")


def follow(path, replay, quiet, idle_notice):
    """Tail `path` forever. Handles: not-there-yet, truncation, partial lines."""
    offset = 0
    started = False
    buffer = ""
    last_note = 0.0

    if replay and os.path.exists(path):
        started = True
    elif os.path.exists(path):
        offset = os.path.getsize(path)   # skip the PREVIOUS session's transcript

    emit(f"{clock()}  watching {os.path.relpath(path, REPO)}"
         + ("  (replaying from the top)" if replay else "  (waiting for the next session)"))

    while True:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = None

        if size is None:
            pass                                   # file not created yet
        else:
            if size < offset:                      # session.py opened it with "w"
                emit(f"\n{clock()}  ↻  file truncated — a new session is starting")
                offset, buffer = 0, ""
            if size > offset:
                started = True
                with open(path, "r", errors="replace") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                    offset = fh.tell()
                buffer += chunk
                *lines, buffer = buffer.split("\n")  # keep any partial last line
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        render(json.loads(line), quiet)
                    except json.JSONDecodeError:
                        emit(f"{clock()}  ?  {flat(line, 160)}")

        now = time.time()
        if not started and idle_notice and now - last_note > idle_notice:
            emit(f"{clock()}  … still waiting")
            last_note = now
        time.sleep(0.4)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", default="open",
                    choices=["open", "close", "premarket", "wake", "drill"])
    ap.add_argument("--replay", action="store_true",
                    help="print the existing transcript from the top, then keep following")
    ap.add_argument("--quiet", action="store_true",
                    help="tool calls and results only — suppress the agent's prose")
    ap.add_argument("--idle-notice", type=float, default=120.0,
                    help="seconds between 'still waiting' lines (0 = silent)")
    args = ap.parse_args()

    path = os.path.join(REPO, "logs", f"session_stream.{args.mode}.jsonl")
    try:
        follow(path, args.replay, args.quiet, args.idle_notice)
    except KeyboardInterrupt:
        emit(f"\n{clock()}  stopped watching (the session is unaffected)")


if __name__ == "__main__":
    main()
