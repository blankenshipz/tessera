"""Explicit local adoption commands; source paths never enter the HTTP write API."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict
from pathlib import Path

from .snapshots import MAX_SNAPSHOT_BYTES, MAX_SNAPSHOT_FILES, SnapshotStore
from .store import StoreError
from .workspace import (
    _open_root_directory,
    _read_bytes_at,
    capture_workspace,
    inventory_digest,
    inventory_workspace,
)


def add_workspace_commands(commands: argparse._SubParsersAction) -> None:
    workspace = commands.add_parser("workspace", help="Review and preserve local inputs")
    actions = workspace.add_subparsers(dest="workspace_command", required=True)
    inventory = actions.add_parser("inventory", help="Read-only inventory; prints no file contents")
    inventory.add_argument("--root", required=True, type=Path)
    adopt = actions.add_parser("adopt", help="Preserve reviewed files in private local state")
    adopt.add_argument("--root", required=True, type=Path)
    adopt.add_argument("--state-dir", required=True, type=Path)
    adopt.add_argument("--project-id", required=True)
    adopt.add_argument("--expected-digest", required=True)
    adopt.add_argument("--idempotency-key", required=True)
    verify = actions.add_parser("verify", help="Verify retained bytes without exporting them")
    verify.add_argument("--state-dir", required=True, type=Path)
    verify.add_argument("--snapshot-id", required=True)


def _owner(store: SnapshotStore, state: Path):
    fd = _open_root_directory(state.absolute(), label="control state")
    try:
        header = _read_bytes_at(fd, "owner.header", 1024, "owner.header")
    finally:
        os.close(fd)
    match = re.fullmatch(rb"Authorization: Bearer ([A-Za-z0-9_-]{32,256})\r?\n?", header)
    if match is None:
        raise StoreError("Invalid owner credential header.")
    return store.authenticate(match[1].decode("ascii"))


def run_workspace(arguments: argparse.Namespace) -> int:
    action = arguments.workspace_command
    if action == "inventory":
        inventory = inventory_workspace(
            str(arguments.root),
            max_files=MAX_SNAPSHOT_FILES,
            max_file_bytes=MAX_SNAPSHOT_BYTES,
            max_total_bytes=MAX_SNAPSHOT_BYTES,
        )
        result = {
            "inventory": asdict(inventory),
            "digest": inventory_digest(inventory),
            "transfer_authorized": False,
        }
    else:
        store = SnapshotStore(arguments.state_dir)
        actor = _owner(store, arguments.state_dir)
        if action == "verify":
            result = store.verify_snapshot(actor, arguments.snapshot_id)
        else:
            if not arguments.idempotency_key.strip() or len(arguments.idempotency_key) > 128:
                raise StoreError("A valid idempotency key is required.")
            # Do not recursively import the control service's private database or auth files.
            if arguments.state_dir.absolute().is_relative_to(arguments.root.absolute()):
                raise StoreError("The selected source root must not contain control-plane state.")
            # Validate project scope before reading any selected source file.
            store.get_project(actor, arguments.project_id)
            capture = capture_workspace(str(arguments.root))
            _, result = store.adopt_snapshot(
                actor,
                arguments.project_id,
                arguments.idempotency_key,
                arguments.expected_digest,
                capture,
            )
    print(json.dumps(result, sort_keys=True))
    return 0
