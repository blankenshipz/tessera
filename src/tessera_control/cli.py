"""Command-line entry point for local Tessera control-plane state and serving."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import uvicorn

from .api import create_app
from .deployment import serving_config
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


def _add_astra_setup_command(subcommands: argparse._SubParsersAction) -> None:
    """Register the deliberately separate Astra operator setup command family."""
    command = subcommands.add_parser("astra-setup", help="operator-only retained Kubernetes setup")
    actions = command.add_subparsers(dest="astra_setup_action", required=True)
    actions.add_parser("profiles", help="list setup profiles without host access")
    for name in ("validate", "discover", "plan"):
        action = actions.add_parser(name)
        action.add_argument("--target", required=True, choices=["docker-desktop-kubernetes-v1"])
        action.add_argument("--kubeconfig", required=True, type=Path)
        action.add_argument("--context", required=True, choices=["docker-desktop"])
        action.add_argument("--namespace", required=True)
        action.add_argument("--image", required=True)
        action.add_argument("--runtime-identity", required=True)
        if name in ("discover", "plan"):
            action.add_argument("--output", required=True, type=Path)
    apply = actions.add_parser("apply", help="apply only an exact approved setup plan")
    apply.add_argument("--plan", required=True, type=Path)
    apply.add_argument("--approved-plan", required=True)
    apply.add_argument("--journal", required=True, type=Path)
    status = actions.add_parser("status", help="read selected setup resource status")
    status.add_argument("--plan", required=True, type=Path)
    status.add_argument("--journal", type=Path)
    attach = actions.add_parser(
        "attach", help="report that execution attachment remains unavailable"
    )
    attach.add_argument("--plan", required=True, type=Path)
    export = actions.add_parser(
        "export-live-v2", help="export provider-v2 prerequisites; never run them"
    )
    export.add_argument("--plan", required=True, type=Path)
    export.add_argument("--output", required=True, type=Path)


def _add_astra_installation_command(subcommands: argparse._SubParsersAction) -> None:
    """Register a pure, offline Astra installation plan renderer."""
    command = subcommands.add_parser(
        "astra-installation", help="render an offline, non-applying Astra installation plan"
    )
    actions = command.add_subparsers(dest="astra_installation_action", required=True)
    plan = actions.add_parser("plan", help="render a deterministic offline plan")
    plan.add_argument("--mode", required=True, choices=["create", "observe", "adopt", "adapt"])
    plan.add_argument("--installation-name", required=True)
    plan.add_argument("--profile", required=True)
    plan.add_argument("--kubeconfig-reference-sha256", required=True)
    plan.add_argument("--context", required=True)
    plan.add_argument("--server", required=True)
    plan.add_argument("--ca-sha256", required=True)
    plan.add_argument("--cluster-uid", required=True)
    plan.add_argument("--namespace", required=True)
    plan.add_argument("--namespace-uid", required=True)
    plan.add_argument("--desired-spec-sha256")
    plan.add_argument("--approval-expires-at", required=True)
    plan.add_argument(
        "--credential-reference",
        action="append",
        default=[],
        metavar="KIND:REFERENCE",
        help="sanitized credential-lease or external-reference handle; never a value",
    )
    plan.add_argument(
        "--inspection-snapshot",
        type=Path,
        help="bounded offline-unverified inspection JSON; never queried or treated as live",
    )
    plan.add_argument("--observed-uid")
    plan.add_argument("--observed-spec-sha256")
    plan.add_argument("--observed-at")
    inspect = actions.add_parser("inspect-live", help="run bounded read-only live inspection")
    inspect.add_argument("--kubeconfig", required=True, type=Path)
    inspect.add_argument("--context", required=True)
    inspect.add_argument("--installation-name", required=True)
    inspect.add_argument("--profile", required=True)
    inspect.add_argument("--namespace", required=True)
    inspect.add_argument("--installation-resource", required=True)
    inspect.add_argument("--resource", action="append", default=[])
    revalidate = actions.add_parser(
        "revalidate-live", help="re-read and fence a plan; never applies or enables mutation"
    )
    revalidate.add_argument("--plan", required=True, type=Path)
    revalidate.add_argument("--kubeconfig", required=True, type=Path)
    revalidate.add_argument("--context", required=True)
    revalidate.add_argument("--installation-resource", required=True)
    revalidate.add_argument("--resource", action="append", default=[])


def _add_astra_namespace_command(subcommands: argparse._SubParsersAction) -> None:
    """Namespace-only operator path; it never installs a control plane."""
    command = subcommands.add_parser(
        "astra-namespace",
        help="inspect/use or explicitly create one dedicated Namespace; never install Astra",
        description="Namespace-only operator path; never install Astra or a control plane here.",
    )
    actions = command.add_subparsers(dest="astra_namespace_action", required=True)
    inspect = actions.add_parser("inspect-existing", help="read one existing namespace; no writes")
    inspect.add_argument("--kubeconfig", required=True, type=Path)
    inspect.add_argument("--context", required=True)
    inspect.add_argument("--namespace", required=True)
    for name in ("plan-use-existing", "plan-create"):
        action = actions.add_parser(name)
        action.add_argument("--kubeconfig", required=True, type=Path)
        action.add_argument("--context", required=True)
        action.add_argument("--namespace", required=True)
        action.add_argument("--approval-expires-at", required=True)
        if name == "plan-create":
            action.add_argument("--installation", required=True)
            action.add_argument("--operation", required=True)
    for name in ("create", "status", "reconcile"):
        action = actions.add_parser(name)
        action.add_argument("--plan", required=True, type=Path)
        action.add_argument("--kubeconfig", required=True, type=Path)
        action.add_argument("--context", required=True)
        action.add_argument("--receipt", type=Path)
        if name == "create":
            action.add_argument(
                "--approved-plan", required=True, help="exact human-approved plan digest"
            )
            action.add_argument(
                "--receipt-output",
                type=Path,
                help="new private file for the UID/resourceVersion creation receipt",
            )


def parser() -> argparse.ArgumentParser:
    from .agent_sandbox_cli import add_agent_sandbox_commands
    from .connection_tunnel import add_connection_tunnel_command

    command = argparse.ArgumentParser(prog="tessera")
    subcommands = command.add_subparsers(dest="command", required=True)
    add_agent_sandbox_commands(subcommands)
    add_connection_tunnel_command(subcommands)
    add_workspace_commands(subcommands)
    _add_minikube_provisioning_command(subcommands)
    _add_astra_setup_command(subcommands)
    _add_astra_installation_command(subcommands)
    _add_astra_namespace_command(subcommands)
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
    serve.add_argument("--bind-host", default="127.0.0.1")
    serve.add_argument("--trusted-host", action="append")
    serve.add_argument("--public-origin")
    serve.add_argument("--runtime-owner-config", type=Path)
    serve.add_argument("--runtime-owner-sha256")
    serve.add_argument("--agent-sandbox-config", type=Path)
    serve.add_argument("--agent-sandbox-sha256")
    return command


def _run_minikube_provisioning(arguments: argparse.Namespace) -> int:
    # Import here so every other control CLI path remains independent of Minikube.
    from tessera_provisioner.minikube import (
        MinikubePlan,
        MinikubeProvisioner,
        MinikubeProvisioningError,
        prepare_result_journal,
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
    journal = None
    try:
        # Custody is a precondition for --apply: never mutate infrastructure if
        # the mandatory operation record cannot be safely published afterward.
        if arguments.journal is not None:
            journal = prepare_result_journal(arguments.journal)
        provisioner = MinikubeProvisioner()
        if arguments.status:
            result = provisioner.status(plan)
        else:
            result = provisioner.apply(plan, arguments.approved_plan)
        if journal is not None:
            record_result(journal, result)
    except MinikubeProvisioningError as error:
        result = {"state": "failed", "plan_digest": plan.digest, "message": str(error)}
        if journal is not None:
            try:
                record_result(journal, result)
            except MinikubeProvisioningError:
                pass
        print(json.dumps(result, sort_keys=True), file=sys.stderr)
        return 1
    finally:
        if journal is not None:
            journal.close()
    print(json.dumps(result, sort_keys=True))
    return 0


def _write_operator_json(path: Path, value: object) -> None:
    """Publish a non-secret plan/discovery only once, never following a symlink."""
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise ValueError("operator output must be a new absolute non-symlink path")
    parent_stat = path.parent.stat(follow_symlinks=False)
    if (
        stat.S_ISLNK(parent_stat.st_mode)
        or not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != os.getuid()
        or parent_stat.st_mode & 0o077
    ):
        raise ValueError("operator output parent must be an existing private directory")
    with path.open("x", encoding="utf-8") as output:
        output.write(json.dumps(value, sort_keys=True, separators=(",", ":")))
        output.write("\n")
    path.chmod(0o600)


def _run_astra_setup(arguments: argparse.Namespace) -> int:
    # This isolated import ensures ordinary local control paths never need kubectl.
    from tessera_provisioner.astra_setup import (
        AstraDockerDesktopSetup,
        AstraSetupError,
        SetupJournal,
        SetupPlan,
        SetupProfile,
        plan_from_json,
        plan_json,
        profile_catalog,
    )

    try:
        if arguments.astra_setup_action == "profiles":
            print(json.dumps({"profiles": profile_catalog()}, sort_keys=True))
            return 0
        if arguments.astra_setup_action in {"validate", "discover", "plan"}:
            profile = SetupProfile.docker_desktop(
                arguments.namespace, arguments.image, arguments.runtime_identity
            )
            operator = AstraDockerDesktopSetup(arguments.kubeconfig, arguments.context)
            if arguments.astra_setup_action == "validate":
                print(json.dumps(operator.validate(profile), sort_keys=True))
                return 0
            discovery = operator.discover(profile)
            if arguments.astra_setup_action == "discover":
                _write_operator_json(arguments.output, discovery.payload())
                print(
                    json.dumps(
                        {"state": "discovered", "output": str(arguments.output)}, sort_keys=True
                    )
                )
                return 0
            plan = SetupPlan.create(profile, discovery)
            _write_operator_json(arguments.output, json.loads(plan_json(plan)))
            print(
                json.dumps(
                    {
                        "state": "planned",
                        "plan_digest": plan.digest,
                        "output": str(arguments.output),
                    },
                    sort_keys=True,
                )
            )
            return 0
        plan = plan_from_json(arguments.plan)
        operator = AstraDockerDesktopSetup(Path(plan.discovery.kubeconfig), plan.discovery.context)
        if arguments.astra_setup_action == "apply":
            result = operator.apply(plan, arguments.approved_plan, SetupJournal(arguments.journal))
            print(json.dumps(result, sort_keys=True))
            return 0
        if arguments.astra_setup_action == "status":
            journal = SetupJournal(arguments.journal) if arguments.journal is not None else None
            result = operator.status(plan, journal)
            if arguments.journal is not None:
                journal.record({"event": "status", **result})
            print(json.dumps(result, sort_keys=True))
            return 0
        if arguments.astra_setup_action == "attach":
            print(
                json.dumps(
                    {
                        "state": "not_attached",
                        "plan_digest": plan.digest,
                        "message": (
                            "Setup does not attach an execution target, enroll a worker, "
                            "grant authority, or dispatch work."
                        ),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if arguments.astra_setup_action == "export-live-v2":
            export = {
                "version": "astra-live-v2-export-v1",
                "setup_plan_digest": plan.digest,
                "target": {
                    "context": plan.discovery.context,
                    "server": plan.discovery.server,
                    "ca_sha256": plan.discovery.ca_sha256,
                    "cluster_uid": plan.discovery.cluster_uid,
                    "namespace": plan.profile.namespace,
                },
                "image": plan.profile.image,
                "runtime_identity": plan.profile.runtime_identity,
                "state": "provider_effects_not_authorized",
                "required_confirmation": (
                    "external v2 runner preflight and one-use provider confirmation"
                ),
                "limitations": [
                    (
                        "export does not create a Secret, deployment, grant, worker, Job, "
                        "provider call, or OAuth effect"
                    ),
                    "the existing external v2 runner owns provider preflight and confirmation",
                ],
            }
            _write_operator_json(arguments.output, export)
            print(
                json.dumps({"state": "exported", "output": str(arguments.output)}, sort_keys=True)
            )
            return 0
        raise AssertionError("unreachable Astra setup action")
    except (AstraSetupError, ValueError, OSError) as error:
        print(
            json.dumps({"state": "failed", "message": str(error)}, sort_keys=True), file=sys.stderr
        )
        return 1


def _credential_reference(value: str):
    from tessera_provisioner.astra_installation import AstraInstallationError, CredentialReference

    kind, separator, reference = value.partition(":")
    if not separator or ":" in reference:
        raise AstraInstallationError("credential references must use KIND:REFERENCE.")
    return CredentialReference(kind, reference)  # type: ignore[arg-type]


def _run_astra_installation(arguments: argparse.Namespace) -> int:
    """Render only caller-supplied offline facts; no host or Kubernetes access occurs."""
    from tessera_provisioner.astra_installation import (
        AstraInstallationError,
        InstallationIdentity,
        InstallationInspection,
        InstallationPlan,
        ObservedInstallation,
        TargetIdentity,
        inspection_from_json,
        plan_json,
    )

    try:
        if arguments.astra_installation_action in {"inspect-live", "revalidate-live"}:
            from datetime import UTC, datetime

            from tessera_provisioner.astra_live_inspection import (
                LiveAstraInspector,
                revalidate_plan,
            )

            if arguments.astra_installation_action == "revalidate-live":
                from tessera_provisioner.astra_installation import plan_from_json

                plan = plan_from_json(arguments.plan)
                identity, namespace = plan.inspection.installation, plan.inspection.target.namespace
            else:
                identity = InstallationIdentity(arguments.installation_name, arguments.profile)
                namespace = arguments.namespace
                plan = None
            evidence = LiveAstraInspector(arguments.kubeconfig, arguments.context).inspect(
                identity, namespace, arguments.installation_resource, arguments.resource
            )
            if plan is not None:
                revalidate_plan(plan, evidence, datetime.now(UTC))
                evidence = {
                    **evidence,
                    "plan_digest": plan.digest,
                    "state": "live_inspection_revalidated_not_apply_ready",
                }
            print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
            return 0
        identity = InstallationIdentity(arguments.installation_name, arguments.profile)
        target = TargetIdentity(
            kubeconfig_reference_sha256=arguments.kubeconfig_reference_sha256,
            context=arguments.context,
            server=arguments.server,
            ca_sha256=arguments.ca_sha256,
            cluster_uid=arguments.cluster_uid,
            namespace=arguments.namespace,
            namespace_uid=arguments.namespace_uid,
        )
        observation_inputs = (
            arguments.observed_uid,
            arguments.observed_spec_sha256,
            arguments.observed_at,
        )
        if arguments.inspection_snapshot is not None:
            if any(value is not None for value in observation_inputs):
                raise AstraInstallationError(
                    "an offline inspection snapshot cannot be merged with observed inputs."
                )
            inspection = inspection_from_json(arguments.inspection_snapshot)
            if inspection.installation != identity or inspection.target != target:
                raise AstraInstallationError(
                    "offline inspection snapshot target or installation identity does not match "
                    "explicit inputs."
                )
            inspection = replace(inspection, source="offline-snapshot")
        else:
            if any(value is not None for value in observation_inputs) and not all(
                value is not None for value in observation_inputs
            ):
                raise AstraInstallationError(
                    "observed UID, spec digest, and timestamp must be supplied together."
                )
            observation = (
                ObservedInstallation(
                    identity,
                    target,
                    arguments.observed_uid,
                    arguments.observed_spec_sha256,
                    arguments.observed_at,
                )
                if all(value is not None for value in observation_inputs)
                else None
            )
            inspection = InstallationInspection(identity, target, observation)
        references = tuple(_credential_reference(value) for value in arguments.credential_reference)
        plan = InstallationPlan.create(
            mode=arguments.mode,
            inspection=inspection,
            desired_spec_sha256=arguments.desired_spec_sha256,
            credential_references=references,
            approval_expires_at=arguments.approval_expires_at,
        )
        print(plan_json(plan))
        return 0
    except (AstraInstallationError, OSError, ValueError, RuntimeError) as error:
        print(
            json.dumps(
                {
                    "state": "failed",
                    "message": str(error),
                    "verification": (
                        "live_inspection_not_apply_readiness"
                        if arguments.astra_installation_action
                        in {"inspect-live", "revalidate-live"}
                        else "offline_unverified"
                    ),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


def _run_astra_namespace(arguments: argparse.Namespace) -> int:
    """Run the small explicit Namespace lifecycle without any installer behavior."""
    from datetime import UTC, datetime

    from tessera_provisioner.astra_live_inspection import LiveInspectionError
    from tessera_provisioner.astra_namespace import (
        NamespaceLifecycleError,
        NamespaceOperator,
        NamespacePlan,
        plan_from_json,
        receipt_from_json,
    )

    try:
        if arguments.astra_namespace_action in {
            "inspect-existing",
            "plan-use-existing",
            "plan-create",
        }:
            operator = NamespaceOperator(arguments.kubeconfig, arguments.context)
            inspection = operator.inspect(arguments.namespace)
            if arguments.astra_namespace_action == "inspect-existing":
                print(json.dumps(inspection.payload(), sort_keys=True, separators=(",", ":")))
                return 0
            mode = (
                "use-existing"
                if arguments.astra_namespace_action == "plan-use-existing"
                else "create-dedicated"
            )
            plan = NamespacePlan.create(
                mode=mode,
                inspection=inspection,
                approval_expires_at=arguments.approval_expires_at,
                installation=getattr(arguments, "installation", None),
                operation=getattr(arguments, "operation", None),
            )
            print(
                json.dumps(
                    {**plan.payload(), "digest": plan.digest}, sort_keys=True, separators=(",", ":")
                )
            )
            return 0
        plan = plan_from_json(arguments.plan)
        operator = NamespaceOperator(arguments.kubeconfig, arguments.context)
        receipt = receipt_from_json(arguments.receipt) if arguments.receipt is not None else None
        if arguments.astra_namespace_action == "create":
            result = operator.create(plan, arguments.approved_plan, datetime.now(UTC))
            payload = result.payload() if hasattr(result, "payload") else result
            if arguments.receipt_output is not None:
                if not hasattr(result, "payload"):
                    raise NamespaceLifecycleError(
                        "no receipt can be recorded until reconciliation resolves "
                        "the create response"
                    )
                _write_operator_json(arguments.receipt_output, payload)
        else:
            # Reconcile is intentionally status-only.  It never retries a lost create
            # response or adopts a same-named object without the original receipt.
            payload = operator.status(plan, receipt)
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0
    except (NamespaceLifecycleError, LiveInspectionError, OSError, ValueError) as error:
        print(
            json.dumps({"state": "failed", "message": str(error)}, sort_keys=True), file=sys.stderr
        )
        return 1


def main() -> int:
    arguments = parser().parse_args()
    if arguments.command == "agent-sandbox":
        from .agent_sandbox_cli import run_agent_sandbox

        return run_agent_sandbox(arguments)
    if arguments.command == "connection-tunnel":
        from .connection_tunnel import run_connection_tunnel

        return run_connection_tunnel(arguments)
    if arguments.command == "minikube-provision":
        return _run_minikube_provisioning(arguments)
    if arguments.command == "astra-setup":
        return _run_astra_setup(arguments)
    if arguments.command == "astra-installation":
        return _run_astra_installation(arguments)
    if arguments.command == "astra-namespace":
        return _run_astra_namespace(arguments)
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
        owner = None
        try:
            deployment = serving_config(
                arguments.bind_host, arguments.trusted_host, arguments.public_origin
            )
            sandbox_backend = None
            if arguments.agent_sandbox_config is not None or arguments.agent_sandbox_sha256:
                from .agent_sandbox_configuration import load_agent_sandbox_backend

                sandbox_backend = load_agent_sandbox_backend(
                    arguments.agent_sandbox_config, arguments.agent_sandbox_sha256
                )
            if arguments.runtime_owner_config is not None or arguments.runtime_owner_sha256:
                from .runtime_composition import load_runtime_owner

                owner = load_runtime_owner(
                    arguments.runtime_owner_config,
                    arguments.runtime_owner_sha256,
                    state_dir=arguments.state_dir,
                )
                adapter = owner.control_adapter()
            else:
                adapter = None
        except ValueError as error:
            print(f"serve configuration error: {error}", file=sys.stderr)
            return 2
        except Exception:
            if owner is not None:
                try:
                    owner.close()
                except Exception:
                    pass
            print("serve runtime owner configuration blocked", file=sys.stderr)
            return 78
        cleanup_failed = False

        def close_owner():
            nonlocal cleanup_failed
            if owner is not None:
                try:
                    owner.close()
                except Exception:
                    cleanup_failed = True
                    print("serve runtime owner shutdown blocked", file=sys.stderr)
            return not cleanup_failed

        try:
            app = create_app(
                arguments.state_dir,
                deployment,
                runtime_binding_adapter=adapter,
                agent_sandbox_backend=sandbox_backend,
            )
            if owner is not None:
                previous_lifespan = app.router.lifespan_context

                @asynccontextmanager
                async def owner_lifespan(application):
                    # Uvicorn re-raises SIGTERM before run() returns: release the
                    # owner during ASGI shutdown, retaining the app's lifespan.
                    primary_failed = False
                    try:
                        async with previous_lifespan(application) as state:
                            yield state
                    except BaseException:
                        primary_failed = True
                        raise
                    finally:
                        if not close_owner() and not primary_failed:
                            raise RuntimeError("runtime owner shutdown failed") from None

                app.router.lifespan_context = owner_lifespan
            uvicorn.run(
                app,
                host=arguments.bind_host,
                port=arguments.port,
            )
        finally:
            # Also cover app construction, startup/bind failures and runners that
            # never entered lifespan. RuntimeOwnerComposition.close is one-shot.
            close_owner()
        return 78 if cleanup_failed else 0
    raise AssertionError("unreachable")
