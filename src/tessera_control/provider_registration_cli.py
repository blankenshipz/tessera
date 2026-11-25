"""``tessera providers``: validated, offline edits of the operator providers file (ADR 0081)."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from . import provider_registration as registration

MAX_ENTRY_FILE_BYTES = registration.MAX_ENTRY_BYTES


def add_parser(subcommands: argparse._SubParsersAction) -> None:
    command = subcommands.add_parser(
        "providers",
        help="validated add/update/list of compute providers and profiles (operator file)",
    )
    actions = command.add_subparsers(dest="providers_action", required=True)
    for name in ("list", "audit"):
        item = actions.add_parser(name)
        item.add_argument("--file", type=Path, required=True, help="absolute providers file")
    gpu = actions.add_parser(
        "gpu-classes",
        help="read-only: the Salad GPU classes the operator's profiles allow, with recorded data",
    )
    gpu.add_argument("--file", type=Path, required=True, help="absolute providers file")
    gpu.add_argument("--state-dir", type=Path, help="control state dir (for recorded receipts)")
    gpu.add_argument("--days", type=int, default=14, help="receipt window in days (1-90)")
    gpu.add_argument("--format", choices=("json", "text"), default="json")
    for name in ("add", "update"):
        item = actions.add_parser(name)
        item.add_argument("--file", type=Path, required=True, help="absolute providers file")
        item.add_argument("--section", required=True)
        item.add_argument("--key", help="entry key (update; add reads it from the entry)")
        source = item.add_mutually_exclusive_group(required=True)
        source.add_argument("--entry-file", type=Path)
        source.add_argument("--entry-json")
        item.add_argument("--expected-digest")
        item.add_argument("--dry-run", action="store_true")
        item.add_argument(
            "--provider-trust", type=Path, help="read-only trust file for the readout"
        )


def _entry(arguments: argparse.Namespace) -> dict:
    if arguments.entry_json is not None:
        raw = arguments.entry_json
    else:
        data = Path(arguments.entry_file).read_bytes()[: MAX_ENTRY_FILE_BYTES + 1]
        if len(data) > MAX_ENTRY_FILE_BYTES:
            raise registration.RegistrationRefused("The entry file is too large.")
        raw = data.decode()
    try:
        value = json.loads(raw)
    except ValueError:
        raise registration.RegistrationRefused("The entry is not valid JSON.") from None
    if not isinstance(value, dict):
        raise registration.RegistrationRefused("The entry must be a JSON object.")
    return value


def _gpu_classes(arguments: argparse.Namespace, path: Path) -> int:
    from . import salad_gpu_classes as gpu
    from .store import _database_path

    try:
        if type(arguments.days) is not int or not 1 <= arguments.days <= gpu.MAX_WINDOW_DAYS:
            raise gpu.GpuClassListingError(f"The window must be 1 to {gpu.MAX_WINDOW_DAYS} days.")
        database = None
        if arguments.state_dir is not None:
            database = _database_path(Path(arguments.state_dir))
        listing = gpu.list_gpu_classes(
            gpu.load_profiles_from_file(path), database, days=arguments.days
        )
    except gpu.GpuClassListingError as error:
        print(json.dumps({"refused": str(error)[:500]}), file=sys.stderr)
        return 2
    print(gpu.render_text(listing) if arguments.format == "text" else json.dumps(listing, indent=2))
    return 0


def run(arguments: argparse.Namespace) -> int:
    path = Path(arguments.file)
    try:
        if not path.is_absolute():
            raise registration.RegistrationRefused("The providers file path must be absolute.")
        if arguments.providers_action == "gpu-classes":
            return _gpu_classes(arguments, path)
        if arguments.providers_action == "audit":
            print(json.dumps({"records": registration.read_audit(path)}, indent=2))
            return 0
        if arguments.providers_action == "list":
            document, digest, exists = registration.read_document(path)
            body = {
                "digest": digest,
                "exists": exists,
                "sections": registration.known_section_names(document, True),
                "entries": registration.list_document(document, True),
            }
            print(json.dumps(body, indent=2))
            return 0
        policy = None
        if arguments.provider_trust is not None:
            from .provider_trust import load_policy_file

            policy = load_policy_file(arguments.provider_trust)
        result = registration.apply_change(
            path,
            arguments.providers_action,
            arguments.section,
            arguments.key,
            _entry(arguments),
            actor="cli:" + getpass.getuser(),
            expected_digest=arguments.expected_digest,
            policy=policy,
            dry_run=arguments.dry_run,
            allow_module=True,  # the file's owner runs this; serve imports the same modules
        )
        print(json.dumps(result, indent=2))
        return 0
    except (registration.RegistrationRefused, ValueError, OSError) as error:
        print(json.dumps({"refused": str(error)[:500]}), file=sys.stderr)
        return 2
