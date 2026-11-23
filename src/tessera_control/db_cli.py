"""``tessera db``: offline maintenance of the control database (the service must be stopped)."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def _nonnegative(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a nonnegative integer") from None
    if number < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return number


def add_db_commands(subcommands: argparse._SubParsersAction) -> None:
    database = subcommands.add_parser("db", help="offline control database maintenance")
    actions = database.add_subparsers(dest="db_action", required=True)
    compact = actions.add_parser(
        "compact",
        help="rewrite the stopped control database without free pages, after a verified backup",
    )
    compact.add_argument("--state-dir", required=True, type=Path)
    compact.add_argument(
        "--backup-root",
        type=Path,
        help="where the pre-compaction backup goes (default: the retention policy's backup_root)",
    )
    compact.add_argument(
        "--min-free-bytes",
        type=_nonnegative,
        help="free space the state directory filesystem must have (default 8 GiB)",
    )
    compact.add_argument(
        "--dry-run", action="store_true", help="run the checks and print the plan; write nothing"
    )


def run_db(arguments: argparse.Namespace) -> int:
    from .db_compact import compact_state
    from .store import StoreError

    try:
        result = compact_state(
            arguments.state_dir,
            backup_root=arguments.backup_root,
            min_free_bytes=arguments.min_free_bytes,
            dry_run=arguments.dry_run,
        )
    except (StoreError, sqlite3.Error, OSError) as error:
        print(f"db compact failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, indent=1))
    if result.get("receipt_written") is False:
        print("db compact: compaction completed; receipt not written", file=sys.stderr)
    return 0
