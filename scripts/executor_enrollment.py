"""Owner tool for a stable executor enrollment (one long-lived worker principal).

    executor_enrollment.py enroll --state-dir S --owner-header H --executor-dir NEW \\
        --provider salad|local-docker|self-hosted --region R [--project-name N] [--days 7] \\
        [--existing-project PROJECT_ID]
    executor_enrollment.py renew  --state-dir S --owner-header H --executor-dir DIR
    executor_enrollment.py revoke --state-dir S --owner-header H --executor-dir DIR
    executor_enrollment.py show   --executor-dir DIR

``enroll`` creates a new project for the executor, or with ``--existing-project`` enrolls
it into a project that already exists (the owner only; an unknown project is refused
before any credential is issued; re-running the same command in the same directory
returns the same record). It creates a new 0700 directory with the 0600 worker token, a
request journal and ``executor.json`` (identifiers and times only), then prints the
non-secret record: list its ``worker_principal_id`` under the provider in the provider
trust file and
reload. ``renew`` extends the same principal by up to 24 h, never past ``not_after``
(at most seven days after enrollment). Rotate by enrolling a new directory, editing the
trust file, reloading, then revoking the old directory. Tokens are never printed.
See docs/provider-trust.md (Stable executor).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import salad_live_setup as setup  # noqa: E402

from tessera_control import executor_enrollment as ee  # noqa: E402
from tessera_control.store import Forbidden  # noqa: E402

# Registration labels per provider (descriptive metadata, as the route setups use).
PROVIDER_LABELS = {
    "salad": dict(
        approved_build_identifier="tessera-trusted-salad-server-v1", trust_class="trusted-server"
    ),
    # The development grant poller (tessera-worker development-poll) behind the built-in
    # local-docker provider, or a self-hosted instance of the same code.
    "local-docker": dict(
        approved_build_identifier="tessera-development-poller-v1", trust_class="local-host"
    ),
    "self-hosted": dict(
        approved_build_identifier="tessera-development-poller-v1", trust_class="self-hosted"
    ),
}


def _operation_journal(directory: Path, operation: str) -> setup.Journal:
    """A fresh journal for one renew/revoke under DIR/operations (created 0700)."""
    parent = directory / "operations"
    try:
        os.mkdir(parent, 0o700)
    except FileExistsError:
        pass
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return setup.Journal(parent / f"{stamp}-{operation}")


def _repeated_enrollment(args) -> ee.Executor | None:
    """The record of an identical earlier ``--existing-project`` enrollment, else ``None``.

    Re-running the command in the same directory changes nothing and prints the same
    record; a directory that holds anything else (another provider, project, region or
    number of days, or no record at all) is refused, never overwritten."""
    if not args.existing_project or not args.executor_dir.exists():
        return None
    executor = ee.read(args.executor_dir)
    if not ee.same_enrollment(
        executor,
        provider=args.provider,
        project_id=args.existing_project,
        region=args.region,
        lifetime=timedelta(days=args.days),
    ):
        raise ee.ExecutorError(
            "executor directory already holds a different enrollment "
            "(provider, project, region or days differ, or it is revoked)"
        )
    return executor


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("enroll", "renew", "revoke"):
        action = actions.add_parser(name)
        action.add_argument("--state-dir", type=Path, required=True)
        action.add_argument("--owner-header", type=Path, required=True)
        action.add_argument("--executor-dir", type=Path, required=True)
        if name == "enroll":
            action.add_argument("--provider", required=True, choices=sorted(PROVIDER_LABELS))
            action.add_argument("--region", required=True)
            action.add_argument("--project-name", default=None)
            action.add_argument("--existing-project", default=None, metavar="PROJECT_ID")
            action.add_argument("--days", type=int, default=7, choices=range(1, 8))
    show = actions.add_parser("show")
    show.add_argument("--executor-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "show":
            executor = ee.read(args.executor_dir)
        else:
            owner_token = setup.owner_token_from_header(args.owner_header)
            if args.action == "enroll" and args.existing_project and args.project_name:
                raise ee.ExecutorError("--project-name cannot be used with --existing-project")
            repeated = _repeated_enrollment(args) if args.action == "enroll" else None
            if repeated is not None:
                executor = repeated
            elif args.action == "enroll":
                from tessera_control.salad_live_check import source_digest

                token = secrets.token_urlsafe(48)
                journal = setup.Journal(args.executor_dir)
                journal.write(ee.TOKEN_NAME, token.encode())
                control = setup.Control(args.state_dir, owner_token, journal)
                executor = ee.enroll(
                    control.http,
                    args.executor_dir,
                    token,
                    provider=args.provider,
                    project_name=args.project_name or f"{args.provider} executor",
                    approved_build_digest=source_digest(),
                    region=args.region,
                    existing_project_id=args.existing_project,
                    lifetime=timedelta(days=args.days),
                    **PROVIDER_LABELS[args.provider],
                )
                del token
            else:
                ee.read(args.executor_dir)  # validate the directory before writing under it
                journal = _operation_journal(args.executor_dir, args.action)
                control = setup.Control(args.state_dir, owner_token, journal)
                operation = ee.renew if args.action == "renew" else ee.revoke
                executor = operation(control.http, args.executor_dir)
    except (ee.ExecutorError, setup.SetupError, Forbidden, OSError) as error:
        print(f"executor {args.action} stopped: {error}", file=sys.stderr)
        return 1
    print(json.dumps(executor.public(), sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
