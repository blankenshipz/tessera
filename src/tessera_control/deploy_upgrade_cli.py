"""``tessera deploy-upgrade``: the checked upgrade command (ADR 0099)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import deploy_check


def add_parser(subcommands: argparse._SubParsersAction) -> None:
    command = subcommands.add_parser(
        "deploy-upgrade",
        help="preflight, capped backup, migrate, ordered restart, health, smoke, receipt",
        description=(
            "Reads an operator plan (tessera.deploy-upgrade-plan.v1). By default it only reports "
            "what it would do after running the preflight; pass --execute to stop services, back "
            "up, migrate, restart in order, wait for health, run the smoke and write a receipt."
        ),
    )
    command.add_argument("--plan", type=Path, required=True, help="absolute, owner-only plan file")
    command.add_argument(
        "--execute", action="store_true", help="make the changes (default is a dry run)"
    )
    command.add_argument("--report-file", type=Path, help="also write the receipt JSON here")
    command.add_argument("--format", choices=("text", "json"), default="text")


def _text(document: dict) -> str:
    lines = [f"{document['mode']}: {document['result']}"]
    for step in document["steps"]:
        mark = "ok  " if step["ok"] else "FAIL"
        lines.append(f"[{mark}] {step['step']}: {step['detail']} ({step['seconds']}s)")
    for planned in document.get("planned", []):
        lines.append(f"  would: {planned}")
    if document.get("failed_step"):
        lines.append(f"failed at: {document['failed_step']}")
        if document.get("backup"):
            lines.append(f"backup to restore from: {document['backup']['name']}")
    lines.append(f"duration: {document['duration_seconds']}s")
    return "\n".join(lines)


def run(arguments: argparse.Namespace) -> int:
    from . import deploy_upgrade
    from .deploy_upgrade_plan import PlanError, load_plan

    try:
        plan = load_plan(arguments.plan)
    except PlanError as error:
        print(f"deploy-upgrade: {error}", file=sys.stderr)
        return 2
    try:
        document = deploy_upgrade.run_upgrade(plan, execute=arguments.execute)
        if arguments.execute:
            deploy_upgrade.write_receipt(plan, document)
        if arguments.report_file is not None:
            if not arguments.report_file.is_absolute():
                raise ValueError("--report-file must be an absolute path.")
            deploy_check.write_private_json(arguments.report_file, document)
    except (ValueError, OSError) as error:
        print(f"deploy-upgrade: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(document, sort_keys=True, indent=2)
        if arguments.format == "json"
        else _text(document)
    )
    return 0 if document["result"] == "ok" else 1
