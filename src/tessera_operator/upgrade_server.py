"""Upgrade-only local human operator MCP; no bootstrap or authority issuance."""

import argparse

from mcp.server.fastmcp import FastMCP

from . import upgrade


def create_server() -> FastMCP:
    server = FastMCP("Tessera occupied-target upgrade operator", log_level="CRITICAL")
    upgrade.register(server)
    return server


def main(argv=None) -> None:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
