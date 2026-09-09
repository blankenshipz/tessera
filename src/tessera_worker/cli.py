"""CLI for the explicitly opt-in native development worker."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .service import DevelopmentWorkerConfig, DevelopmentWorkerError, DevelopmentWorkerService


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="tessera-worker")
    commands = command.add_subparsers(dest="command", required=True)
    run = commands.add_parser("development-run")
    run.add_argument("--control-url", required=True)
    run.add_argument("--grant-id", required=True)
    run.add_argument("--credential-file", type=Path, required=True)
    run.add_argument("--workspace-root", type=Path, required=True)
    run.add_argument("--development-output-root", type=Path, required=True)
    run.add_argument("--allowed-image", required=True)
    run.add_argument("--poll-seconds", type=float, default=1.0)
    run.add_argument("--max-ticks", type=int, default=90_000)
    return command


def main() -> int:
    arguments = parser().parse_args()
    try:
        config = DevelopmentWorkerConfig(
            control_url=arguments.control_url,
            grant_id=arguments.grant_id,
            credential_file=arguments.credential_file,
            workspace_root=arguments.workspace_root,
            development_output_root=arguments.development_output_root,
            allowed_image=arguments.allowed_image,
            poll_seconds=arguments.poll_seconds,
            max_ticks=arguments.max_ticks,
        )
        print(json.dumps(DevelopmentWorkerService(config).run(), sort_keys=True))
        return 0
    except (DevelopmentWorkerError, ValueError, OSError) as error:
        # Never include parsed arguments or credential contents in diagnostics.
        print(f"development worker failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
