"""``tessera dev smoke-credentials``: owner-run, short-lived $0 local-docker smoke credentials.

``issue`` writes the two tokens to owner-only files (never printed) and a token-free grant
description; ``revoke`` ends the grant at once; ``list`` shows grants. It runs against a state
directory with the local owner's authority and needs no running service.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import smoke_credentials as grants

FILES = ("agent.token", "approver.token", "smoke-grant.json")


def add_dev_commands(subcommands: argparse._SubParsersAction) -> None:
    from .dev_cli import dev_actions

    groups = dev_actions(subcommands)
    smoke = groups.add_parser(
        "smoke-credentials", help="short-lived $0 local-docker agent and approver credentials"
    )
    actions = smoke.add_subparsers(dest="smoke_action", required=True)
    issue = actions.add_parser("issue", help="mint a grant and write its token files (0600)")
    issue.add_argument("--project-id", required=True)
    issue.add_argument("--destination", required=True)
    issue.add_argument("--out-dir", type=Path, required=True)
    issue.add_argument("--ttl-minutes", type=int, default=30)
    revoke = actions.add_parser("revoke", help="revoke both credentials of a grant")
    revoke.add_argument("--grant-id", required=True)
    actions.add_parser("list", help="list grants (never tokens)")
    for sub in actions.choices.values():
        sub.add_argument("--state-dir", required=True, type=Path)


def _write_new(path: Path, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def _prepare_out_dir(out_dir: Path) -> Path:
    out_dir = out_dir.expanduser()
    if out_dir.is_symlink():
        raise ValueError("The output directory must not be a symlink.")
    if out_dir.exists():
        if not out_dir.is_dir() or out_dir.stat().st_mode & 0o077:
            raise ValueError("The output directory must be a private (0700) directory.")
        if any((out_dir / name).exists() or (out_dir / name).is_symlink() for name in FILES):
            raise ValueError("The output directory already holds smoke credential files.")
    else:
        if not out_dir.parent.is_dir():
            raise ValueError("The output directory's parent does not exist.")
        out_dir.mkdir(mode=0o700)
        os.chmod(out_dir, 0o700)
    return out_dir


def run(arguments: argparse.Namespace) -> int:
    from .store import StoreError

    try:
        if arguments.smoke_action == "list":
            print(json.dumps({"grants": grants.list_grants(arguments.state_dir)}, sort_keys=True))
            return 0
        if arguments.smoke_action == "revoke":
            print(
                json.dumps(grants.revoke(arguments.state_dir, arguments.grant_id), sort_keys=True)
            )
            return 0
        created = not arguments.out_dir.expanduser().exists()
        out_dir = _prepare_out_dir(arguments.out_dir)
        try:
            issued = grants.issue(
                arguments.state_dir,
                arguments.project_id,
                arguments.destination,
                ttl_minutes=arguments.ttl_minutes,
            )
        except Exception:
            if created:
                out_dir.rmdir()
            raise
        description = {
            "schema": grants.SCHEMA,
            "grant_id": issued["grant_id"],
            "project_id": issued["project_id"],
            "destination": issued["destination"],
            "expires_at": issued["expires_at"],
            "agent_credential_id": issued["agent_credential_id"],
            "approver_credential_id": issued["approver_credential_id"],
            "providers": issued["providers"],
            "max_budget_cents": 0,
            "control_url": None,
        }
        written: list[Path] = []
        try:
            for name, text in (
                ("agent.token", issued["agent_token"] + "\n"),
                ("approver.token", issued["approver_token"] + "\n"),
                ("smoke-grant.json", json.dumps(description, indent=2, sort_keys=True) + "\n"),
            ):
                _write_new(out_dir / name, text)
                written.append(out_dir / name)
        except OSError:
            # Never leave live credentials nobody can use: end the grant and remove the files.
            grants.revoke(arguments.state_dir, issued["grant_id"])
            for path in written:
                path.unlink(missing_ok=True)
            raise
    except (StoreError, ValueError, OSError) as error:
        print(f"smoke-credentials failed: {error}", file=sys.stderr)
        return 1
    print(issued["grant_id"])
    for path in written:
        print(path)
    return 0
