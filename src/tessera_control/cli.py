"""Command-line entry point for local Tessera control-plane state and serving."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from .api import create_app
from .store import initialize_state


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="tessera")
    subcommands = command.add_subparsers(dest="command", required=True)
    initialize = subcommands.add_parser("init")
    initialize.add_argument("--state-dir", required=True, type=Path)
    serve = subcommands.add_parser("serve")
    serve.add_argument("--state-dir", required=True, type=Path)
    serve.add_argument("--port", required=True, type=int, choices=range(1, 65_536))
    return command


def main() -> int:
    arguments = parser().parse_args()
    if arguments.command == "init":
        database_path, header_path = initialize_state(arguments.state_dir)
        print(database_path)
        print(header_path)
        return 0
    if arguments.command == "serve":
        # No host option is intentional: this local mode must never bind publicly.
        uvicorn.run(create_app(arguments.state_dir), host="127.0.0.1", port=arguments.port)
        return 0
    raise AssertionError("unreachable")
