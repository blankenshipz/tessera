"""Opt-in real SDK stdio client against configured real control service; no inference.

Scenario file contains only typed tool arguments and record IDs, never credentials.
Each step opens a new actual subprocess/session, testing disconnect/reconnect.
Use --probe for status/schema acceptance only (not full-workflow acceptance).
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tessera_mcp.config import Config


async def run(scenario):
    config = Config.from_env()
    config.token()  # Check private runtime reference; do not print it.
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "tessera_mcp.server"], env=dict(os.environ)
    )
    receipts = []
    steps = scenario or [{"tool": "tessera_status", "arguments": {}}]
    if not isinstance(steps, list) or not 1 <= len(steps) <= 64:
        raise ValueError("Scenario must contain 1–64 bounded steps")
    previous = {}
    for index, step in enumerate(steps):
        if set(step) - {"tool", "arguments", "denied", "same_as", "identity_field"}:
            raise ValueError("Unknown scenario field")
        with open(os.devnull, "w") as errors:
            async with stdio_client(params, errlog=errors) as (read, write):
                async with ClientSession(read, write) as client:
                    init = await client.initialize()
                    assert init.protocolVersion == "2025-11-25"
                    tools = {tool.name for tool in (await client.list_tools()).tools}
                    assert "tessera_consume_grant" in tools
                    result = await client.call_tool(step["tool"], step.get("arguments", {}))
                    assert bool(result.isError) == step.get("denied", False)
                    if "same_as" in step:
                        field = step.get("identity_field", "id")
                        assert result.structuredContent[field] == previous[step["same_as"]][field]
                    previous[index] = result.structuredContent
                    receipts.append({"step": index, "tool": step["tool"], "passed": True})
    print(
        json.dumps(
            {
                "protocol": "2025-11-25",
                "transport": "stdio",
                "coverage": "scenario" if scenario else "status/schema probe only",
                "receipts": receipts,
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--probe", action="store_true")
    group.add_argument("--scenario", type=Path)
    args = parser.parse_args()
    try:
        scenario = None
        if args.scenario:
            if args.scenario.stat().st_size > 256 * 1024:
                raise ValueError("Scenario exceeds bound")
            scenario = json.loads(args.scenario.read_text())
        asyncio.run(asyncio.wait_for(run(scenario), timeout=180))
    except Exception:
        print("MCP workflow acceptance failed; no sensitive arguments logged", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
