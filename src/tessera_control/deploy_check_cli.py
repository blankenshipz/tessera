"""``tessera deploy-check``: one checked command for what a deploy still needs (ADR 0098)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import deploy_check
from .deploy_check import Finding

AUTOMATIC_FIXES = ("drop_removed_keys", "refresh_source_pin", "migrate")


def add_parser(subcommands: argparse._SubParsersAction) -> None:
    command = subcommands.add_parser(
        "deploy-check",
        help="report what a deploy still needs; offers receipted fixes with --apply",
        description=(
            "Read-only unless --apply names a fix. Checks the control database schema, the backup "
            "size bound, executor and live-check config keys a release removed, the providers file "
            "and the executor's profiles, and (with --smoke) runs a synthetic networkless CPU "
            "container. Nothing here reads a secret, contacts a provider or spends anything."
        ),
    )
    command.add_argument("--state-dir", type=Path, help="control state directory (schema, backup)")
    command.add_argument(
        "--executor-config", type=Path, action="append", default=[], help="absolute; repeatable"
    )
    command.add_argument(
        "--live-config", type=Path, action="append", default=[], help="absolute; repeatable"
    )
    command.add_argument("--providers-file", type=Path, help="absolute operator providers file")
    command.add_argument(
        "--guardian-status-file",
        type=Path,
        help="absolute path to a saved Salad guardian status reply; checks it carries frame_record",
    )
    command.add_argument(
        "--max-backup-bytes",
        type=int,
        help="the backup size bound you will pass to tessera backup (default 1 GiB)",
    )
    command.add_argument(
        "--smoke", action="store_true", help="run the synthetic CPU container smoke"
    )
    command.add_argument(
        "--apply",
        action="append",
        default=[],
        choices=AUTOMATIC_FIXES,
        help="apply one named fix and write a receipt; repeatable",
    )
    command.add_argument(
        "--backup-confirmed",
        action="store_true",
        help="with --apply migrate: you have taken a backup of the state directory",
    )
    command.add_argument(
        "--source-commit",
        help="with --apply refresh_source_pin: the 40-hex commit of the installed release",
    )
    command.add_argument(
        "--receipt-dir", type=Path, help="existing absolute directory for fix receipts (--apply)"
    )
    command.add_argument("--report-file", type=Path, help="also write the JSON report here (0600)")
    command.add_argument("--format", choices=("text", "json"), default="text")


def gather(
    *,
    state_dir: Path | None,
    executor_configs: list[Path],
    live_configs: list[Path],
    providers_file: Path | None,
    max_backup_bytes: int | None,
    smoke: bool,
    guardian_status_file: Path | None = None,
) -> list[Finding]:
    """Every check for the areas given, in a stable order (shared with ``deploy-upgrade``)."""
    from . import deploy_config, deploy_smoke, deploy_state

    findings: list[Finding] = []
    if state_dir is not None:
        findings.append(deploy_state.check_schema(state_dir))
        findings.append(deploy_state.check_backup_cap(state_dir, max_backup_bytes))
    for index, path in enumerate([*executor_configs, *live_configs], start=1):
        findings.extend(deploy_config.check_config(path, label=str(index)))
    if providers_file is not None:
        findings.extend(deploy_state.check_providers(providers_file, None, guardian_status_file))
    for path in executor_configs:
        findings.extend(deploy_state.check_executor_profiles(path))
    findings.append(deploy_smoke.run_smoke() if smoke else deploy_smoke.smoke_skipped())
    return findings


def _gather(arguments: argparse.Namespace) -> list[Finding]:
    return gather(
        state_dir=arguments.state_dir,
        executor_configs=arguments.executor_config,
        live_configs=arguments.live_config,
        providers_file=arguments.providers_file,
        max_backup_bytes=arguments.max_backup_bytes,
        smoke=arguments.smoke,
        guardian_status_file=arguments.guardian_status_file,
    )


def _apply(arguments: argparse.Namespace) -> list[dict[str, Any]]:
    """Run the named automatic fixes; each writes its own receipt. Order is fixed so a config
    is repaired before anything that would read it."""
    applied: list[dict[str, Any]] = []
    wanted = set(arguments.apply)
    if not wanted:
        return applied
    if arguments.receipt_dir is None:
        raise ValueError("--apply needs --receipt-dir (an existing absolute directory).")
    if "drop_removed_keys" in wanted:
        from . import deploy_config

        for path in [*arguments.executor_config, *arguments.live_config]:
            receipt = deploy_config.apply_drop_removed_keys(path, arguments.receipt_dir)
            if receipt is not None:
                applied.append(receipt)
    if "refresh_source_pin" in wanted:
        from . import deploy_config

        for path in arguments.executor_config:
            receipt = deploy_config.apply_refresh_source_pin(
                path, arguments.receipt_dir, arguments.source_commit
            )
            if receipt is not None:
                applied.append(receipt)
    if "migrate" in wanted:
        if arguments.state_dir is None:
            raise ValueError("--apply migrate needs --state-dir.")
        from . import deploy_state

        receipt = deploy_state.apply_migrate(
            arguments.state_dir, arguments.receipt_dir, backup_confirmed=arguments.backup_confirmed
        )
        if receipt is not None:
            applied.append(receipt)
    return applied


def _require_commit_at_checkout(commit: str) -> None:
    """Refuse a ``--source-commit`` that is not the checkout's head, when a checkout is at hand.

    Only the pin's digest is measured from the install; the commit is operator-asserted. When the
    working directory is a repository checkout (``tests/`` and ``src/``) whose head git can name,
    the commit is held to it as ``deploy-upgrade`` holds the plan's ``source_revision``. Without
    a checkout nothing can be compared and nothing is refused."""
    from pathlib import Path

    from .deploy_upgrade import checkout_head

    root = Path.cwd()
    if not (root / "tests").is_dir() or not (root / "src").is_dir():
        return
    head = checkout_head(root)
    if head is not None and head != commit:
        raise ValueError(
            "--source-commit is not the commit checked out in the working directory "
            f"({head}); run from a checkout at that commit or pass its commit"
        )


def run(arguments: argparse.Namespace) -> int:
    try:
        # Everything that can refuse is checked before any fix changes anything.
        if arguments.report_file is not None:
            deploy_check.require_report_path(arguments.report_file)
        if arguments.apply:
            if arguments.receipt_dir is None:
                raise ValueError("--apply needs --receipt-dir (an existing absolute directory).")
            deploy_check.require_writable_directory(arguments.receipt_dir, "receipt directory")
        if "refresh_source_pin" in arguments.apply:
            from .salad_live_check import installed_source_pin

            if arguments.source_commit is None:
                raise ValueError("--apply refresh_source_pin needs --source-commit.")
            installed_source_pin(arguments.source_commit)  # refuses before any fix
            _require_commit_at_checkout(arguments.source_commit)  # likewise
        applied = _apply(arguments)
        report = deploy_check.build_report(_gather(arguments), applied)
        if arguments.report_file is not None:
            deploy_check.write_private_json(arguments.report_file, report)
    except (ValueError, OSError) as error:
        print(f"deploy-check: {error}", file=sys.stderr)
        return 2
    if arguments.format == "json":
        print(json.dumps(report, sort_keys=True, indent=2))
    else:
        print(deploy_check.render_text(report))
        for entry in applied:
            print(f"applied {entry['fix']}: receipt {entry['receipt_sha256'][:16]}")
    return deploy_check.exit_code(report)
