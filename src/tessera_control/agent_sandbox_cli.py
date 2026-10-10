"""Authenticated lifecycle commands; credentials are private file references."""

import json
import sys
from pathlib import Path

from tessera_worker.control_channel import ControlChannelError, HttpControlChannel
from tessera_worker.service import DevelopmentWorkerError, read_private_credential


def add_agent_sandbox_commands(subcommands):
    parser = subcommands.add_parser("agent-sandbox", help="authenticated direct Sandbox lifecycle")
    actions = parser.add_subparsers(dest="sandbox_action", required=True)
    for name in ("create", "run", "release", "status", "stop", "collect", "deliver", "cleanup"):
        command = actions.add_parser(name)
        command.add_argument("--control-url", required=True)
        command.add_argument("--credential-file", required=True, type=Path)
        command.add_argument("--idempotency-key", required=True)
        command.add_argument("--lease-id" if name == "create" else "--operation-id", required=True)
        if name == "cleanup":
            command.add_argument("--manifest-id", required=True)
            command.add_argument("--manifest-digest", required=True)
        else:
            command.add_argument("--epoch", type=int, required=True)
        if name == "deliver":
            command.add_argument("--operator-config", required=True, type=Path)
            command.add_argument("--operator-sha256", required=True)
            command.add_argument("--package-file", required=True, type=Path)
            command.add_argument("--custody-root", required=True, type=Path)


def run_agent_sandbox(arguments):
    name = arguments.sandbox_action
    try:
        channel = HttpControlChannel(
            arguments.control_url, read_private_credential(arguments.credential_file)
        )
        if name == "deliver":
            from tessera_worker.agent_sandbox_outputs import deliver_stopped_sandbox

            from .agent_sandbox_configuration import load_agent_sandbox_backend, private_bytes

            backend = load_agent_sandbox_backend(
                arguments.operator_config, arguments.operator_sha256
            )
            package = json.loads(private_bytes(arguments.package_file, 1024 * 1024))
            if (
                package["operation_id"] != arguments.operation_id
                or package["epoch"] != arguments.epoch
                or not backend.operator_config.collector_image
                or package["sandbox"]["target"]
                != backend.operator_config.target.model_dump(mode="json")
            ):
                raise ValueError("exact package and approved collector image required")
            result = deliver_stopped_sandbox(
                package,
                backend,
                channel,
                image=backend.operator_config.collector_image,
                custody_root=arguments.custody_root,
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if name == "create":
            path = f"/worker/agent-sandbox/leases/{arguments.lease_id}"
        else:
            prefix = "/worker" if name in {"run", "release"} else ""
            action = {"status": "observe", "collect": "collection-handoff"}.get(name, name)
            path = f"{prefix}/agent-sandboxes/{arguments.operation_id}/{action}"
        body = (
            {"manifest_id": arguments.manifest_id, "manifest_digest": arguments.manifest_digest}
            if name == "cleanup"
            else {"expected_epoch": arguments.epoch}
        )
        result = channel.post(path, body, arguments.idempotency_key)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("phase") not in {"uncertain", "cleanup_uncertain"} else 1
    except (ControlChannelError, DevelopmentWorkerError, ValueError, RuntimeError, OSError):
        print(
            "Sandbox command unavailable or rejected; inspect retained operation state.",
            file=sys.stderr,
        )
        return 1
