"""Command-line entry point for local Tessera control-plane state and serving."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from .api import create_app
from .recovery import backup_state, restore_state
from .store import StoreError, initialize_state, migrate_state
from .workspace import WorkspaceError
from .workspace_cli import add_workspace_commands, run_workspace


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="tessera")
    subcommands = command.add_subparsers(dest="command", required=True)
    add_workspace_commands(subcommands)
    initialize = subcommands.add_parser("init")
    initialize.add_argument("--state-dir", required=True, type=Path)
    migrate = subcommands.add_parser("migrate")
    migrate.add_argument("--state-dir", required=True, type=Path)
    backup = subcommands.add_parser("backup")
    backup.add_argument("--state-dir", required=True, type=Path)
    backup.add_argument("--output", required=True, type=Path)
    restore = subcommands.add_parser("restore")
    restore.add_argument("--backup", required=True, type=Path)
    restore.add_argument("--state-dir", required=True, type=Path)
    serve = subcommands.add_parser("serve")
    serve.add_argument("--state-dir", required=True, type=Path)
    serve.add_argument("--port", required=True, type=int, choices=range(1, 65_536))
    return command


def main() -> int:
    arguments = parser().parse_args()
    if arguments.command == "workspace":
        try:
            return run_workspace(arguments)
        except (StoreError, WorkspaceError) as error:
            print(str(error), file=sys.stderr)
            return 1
    if arguments.command == "init":
        database_path, header_path = initialize_state(arguments.state_dir)
        print(database_path)
        print(header_path)
        return 0
    if arguments.command == "migrate":
        print(migrate_state(arguments.state_dir))
        return 0
    if arguments.command == "backup":
        print(backup_state(arguments.state_dir, arguments.output)["backup_id"])
        return 0
    if arguments.command == "restore":
        database_path, header_path = restore_state(arguments.backup, arguments.state_dir)
        print(database_path)
        print(header_path)
        return 0
    if arguments.command == "serve":
        # No host option is intentional: this local mode must never bind publicly.
        uvicorn.run(create_app(arguments.state_dir), host="127.0.0.1", port=arguments.port)
        return 0
    raise AssertionError("unreachable")
