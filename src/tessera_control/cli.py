"""Command-line entry point for local Tessera control-plane state and serving."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import uvicorn

from .api import create_app
from .recovery import backup_state, restore_state
from .store import StoreError, initialize_state, migrate_state
from .workspace import WorkspaceError
from .workspace_cli import add_workspace_commands, run_workspace


def _add_minikube_provisioning_command(subcommands: argparse._SubParsersAction) -> None:
    command = subcommands.add_parser("minikube-provision")
    mode = command.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="mutate only after human approval")
    mode.add_argument("--status", action="store_true", help="read explicit tessera-dev status")
    mode.add_argument(
        "--teardown", action="store_true", help="report retained-only teardown policy"
    )
    command.add_argument("--approved-plan", help="exact digest explicitly approved by a human")
    command.add_argument("--journal", type=Path, help="local path for sanitized operation result")


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="tessera")
    subcommands = command.add_subparsers(dest="command", required=True)
    add_workspace_commands(subcommands)
    _add_minikube_provisioning_command(subcommands)
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


def _run_minikube_provisioning(arguments: argparse.Namespace) -> int:
    # Import here so every other control CLI path remains independent of Minikube.
    from tessera_provisioner.minikube import (
        MinikubePlan,
        MinikubeProvisioner,
        MinikubeProvisioningError,
        record_result,
        render_plan,
    )

    plan = MinikubePlan.default()
    if not arguments.apply and not arguments.status and not arguments.teardown:
        print(render_plan(plan))
        return 0
    if arguments.teardown:
        result: dict[str, object] = {
            "state": "retained",
            "plan_digest": plan.digest,
            "message": (
                "Teardown is not authorized or implemented; tessera-dev resources were retained."
            ),
        }
        if arguments.journal is not None:
            record_result(arguments.journal, result)
        print(json.dumps(result, sort_keys=True))
        return 0
    if arguments.apply and (arguments.approved_plan != plan.digest or arguments.journal is None):
        print(
            "--apply requires a human-approved exact --approved-plan digest and --journal.",
            file=sys.stderr,
        )
        return 2
    try:
        provisioner = MinikubeProvisioner()
        if arguments.status:
            result = provisioner.status(plan)
        else:
            result = provisioner.apply(plan, arguments.approved_plan)
        if arguments.journal is not None:
            record_result(arguments.journal, result)
    except MinikubeProvisioningError as error:
        result = {"state": "failed", "plan_digest": plan.digest, "message": str(error)}
        if arguments.journal is not None:
            try:
                record_result(arguments.journal, result)
            except MinikubeProvisioningError:
                pass
        print(json.dumps(result, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


def main() -> int:
    arguments = parser().parse_args()
    if arguments.command == "minikube-provision":
        return _run_minikube_provisioning(arguments)
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
        try:
            print(backup_state(arguments.state_dir, arguments.output)["backup_id"])
        except (StoreError, sqlite3.Error, OSError) as error:
            print(f"backup failed: {error}", file=sys.stderr)
            return 1
        return 0
    if arguments.command == "restore":
        try:
            database_path, header_path = restore_state(arguments.backup, arguments.state_dir)
        except (StoreError, sqlite3.Error, OSError) as error:
            print(f"restore failed: {error}", file=sys.stderr)
            return 1
        print(database_path)
        print(header_path)
        return 0
    if arguments.command == "serve":
        # No host option is intentional: this local mode must never bind publicly.
        uvicorn.run(create_app(arguments.state_dir), host="127.0.0.1", port=arguments.port)
        return 0
    raise AssertionError("unreachable")
