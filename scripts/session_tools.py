"""Enumerate the agent's MCP tools by ASKING the server, never from a list.

⚠️ NEVER HARDCODE THE TOOL LIST. Taken from the reference system, which paid for
this: a capability was built and registered while the hardcoded array did not
name it. The tool existed, the session could not see it, and the session
reported it had skipped the work -- indistinguishable from the capability not
existing. Deriving the list makes that failure unrepresentable.

Refuses to return empty: a session launched with no tools would run, trade
nothing, and look like a quiet day.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


def discover(server_cmd: list, server_name: str) -> list:
    from mcp import ClientSession, StdioServerParameters      # noqa: PLC0415
    from mcp.client.stdio import stdio_client                 # noqa: PLC0415

    async def _go():
        params = StdioServerParameters(command=server_cmd[0], args=server_cmd[1:])
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return sorted(t.name for t in (await s.list_tools()).tools)

    names = asyncio.run(_go())
    if not names:
        raise RuntimeError(
            f"MCP tool discovery returned NOTHING for {server_name}. Refusing to "
            f"emit an empty allowlist -- a session with no tools would run, trade "
            f"nothing, and look like a quiet day.")
    return [f"mcp__{server_name}__{n}" for n in names]


def discover_agentic() -> list:
    return discover([str(REPO / ".venv" / "bin" / "python"),
                     str(REPO / "src" / "agent_env" / "server.py")],
                    "agentic-trader")


def _selftest() -> None:
    names = discover_agentic()
    assert names, "discovery returned nothing"
    assert all(n.startswith("mcp__agentic-trader__") for n in names), names
    assert "mcp__agentic-trader__halt_status" in names
    assert "mcp__agentic-trader__check_order" in names
    print(f"session_tools: OK — discovered {len(names)} tools")


def main() -> None:
    if "--print" in sys.argv:
        for name in discover_agentic():
            print(name)
    else:
        _selftest()


if __name__ == "__main__":
    main()
