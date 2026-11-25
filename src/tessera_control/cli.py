"""Command-line entry point for local Tessera control-plane state and serving."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import uvicorn

from . import maintenance_lock
from .api import create_app
from .deployment import serving_config
from .recovery import backup_state, restore_state
from .store import StoreError, initialize_state, migrate_state, require_sqlite_runtime
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


def _add_campaign_commands(subcommands: argparse._SubParsersAction) -> None:
    """Campaign client commands (ADR 0038): JSON out; approval stays a human, labelled act."""
    command = subcommands.add_parser(
        "campaign", help="submit, approve, read or cancel campaigns over the control API"
    )
    actions = command.add_subparsers(dest="campaign_action", required=True)

    def common(
        sub: argparse.ArgumentParser,
        *,
        mutating: bool,
        label: bool = False,
        optional_key: bool = False,
    ) -> None:
        sub.add_argument("--owner-header", type=Path, required=True)
        sub.add_argument("--control-url", default="http://127.0.0.1:8765")
        if mutating:
            sub.add_argument(
                "--idempotency-key",
                required=not optional_key,
                help="reuse only to retry the identical request; a batch must use one per call"
                + (" (default: a fresh random key)" if optional_key else ""),
            )
        if label:
            sub.add_argument(
                "--automation-label",
                help="marks this decision as automation (X-Tessera-Automation); never authority "
                "(default: $TESSERA_AUTOMATION_LABEL or tessera-cli:campaign-<action>)",
            )
        sub.add_argument(
            "--delegation-ref",
            help="the owner delegation this runs under (X-Tessera-Delegation; default: "
            "$TESSERA_DELEGATION_REF); a label, never authority",
        )

    submit = actions.add_parser("submit", help="submit a campaign spec; grants nothing")
    submit.add_argument("--project-id", required=True)
    submit.add_argument("--spec", type=Path, required=True, help="campaign spec JSON file")
    common(submit, mutating=True)
    approve = actions.add_parser("approve", help="human approval of one exact spec digest")
    approve.add_argument("--campaign-id", required=True)
    approve.add_argument(
        "--expected-revision",
        type=int,
        help="default: the campaign's current revision, read just before approving",
    )
    approve.add_argument("--expected-spec-digest", required=True)
    common(approve, mutating=True, label=True, optional_key=True)
    get = actions.add_parser("get")
    get.add_argument("--campaign-id", required=True)
    common(get, mutating=False)
    listing = actions.add_parser("list")
    listing.add_argument("--project-id", required=True)
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument("--limit", type=int, default=50)
    common(listing, mutating=False)
    cancel = actions.add_parser("cancel", help="reduce-only: stop further spend and reconcile")
    cancel.add_argument("--campaign-id", required=True)
    cancel.add_argument("--expected-revision", type=int, required=True)
    cancel.add_argument("--reason", required=True)
    common(cancel, mutating=True)
    digests = actions.add_parser(
        "preapproval-digests",
        help="offline, read-only: the review digests an approve body would pre-approve",
    )
    digests.add_argument("--state-dir", required=True, type=Path)
    digests.add_argument("--salad-campaign-profiles", required=True, type=Path)
    digests.add_argument("--campaign-id", required=True)
    digests.add_argument("--stage-key")
    report = actions.add_parser(
        "report",
        help="plain-words report of one campaign or a time window (no internal ids unless asked)",
    )
    report.add_argument("--campaign-id", help="one campaign; or give --project-id with a window")
    report.add_argument("--project-id", help="every campaign created in --since .. --until")
    report.add_argument("--since", help="ISO-8601 UTC start (inclusive)")
    report.add_argument("--until", help="ISO-8601 UTC end (exclusive)")
    report.add_argument("--limit", type=int, default=100)
    report.add_argument("--detail", action="store_true", help="include ids and digests")
    report.add_argument("--format", choices=("text", "json"), default="text")
    common(report, mutating=False)
    receipt = actions.add_parser("receipt", help="terminal campaign receipt")
    receipt.add_argument("--campaign-id", required=True)
    common(receipt, mutating=False)
    _add_standing_commands(actions, common)


def _add_standing_commands(actions, common) -> None:
    """`campaign standing ...` (ADR 0042): human-created, capped, revocable templates."""
    standing = actions.add_parser("standing", help="standing approvals for scheduled starts")
    verbs = standing.add_subparsers(dest="standing_action", required=True)
    create = verbs.add_parser("create", help="create a standing approval (human decision)")
    create.add_argument("--project-id", required=True)
    create.add_argument("--spec", type=Path, required=True)
    create.add_argument("--expected-spec-digest", required=True)
    create.add_argument("--per-start-budget-cents", type=int, required=True)
    create.add_argument("--max-starts", type=int, required=True)
    create.add_argument("--max-total-budget-cents", type=int, required=True)
    create.add_argument("--not-after", required=True)
    create.add_argument("--not-before")
    create.add_argument("--min-interval-seconds", type=int)
    create.add_argument("--max-concurrent", type=int, default=1)
    create.add_argument("--trigger", choices=("schedule", "follow_on"), default="schedule")
    create.add_argument("--follow-on")
    create.add_argument("--retry-max", type=int, default=0)
    create.add_argument("--retry-on", choices=("failed", "failed_or_stopped"), default="failed")
    common(create, mutating=True, label=True)
    for verb in ("list", "show"):
        sub = verbs.add_parser(verb)
        sub.add_argument("--project-id", required=True)
        if verb == "show":
            sub.add_argument("--id", required=True)
        else:
            sub.add_argument("--offset", type=int, default=0)
            sub.add_argument("--limit", type=int, default=50)
        common(sub, mutating=False)
    revoke = verbs.add_parser("revoke", help="stop all new starts from a standing approval")
    revoke.add_argument("--project-id", required=True)
    revoke.add_argument("--id", required=True)
    revoke.add_argument("--expected-revision", type=int, required=True)
    revoke.add_argument("--reason", required=True)
    revoke.add_argument("--cancel-running", action="store_true")
    revoke.add_argument("--automation-label", help="X-Tessera-Automation label (owner credential)")
    common(revoke, mutating=True)


_READ_ONLY_CAMPAIGN_ACTIONS = frozenset({"get", "list", "report", "receipt"})
_READ_ONLY_STANDING_ACTIONS = frozenset({"list", "show"})


def _standing_call(arguments, project: str, key):
    """Build the (method, path, body, key) call for a `campaign standing` verb."""
    from urllib.parse import quote

    from .agent_research_review import ReviewError
    from .campaign_standing import CreateStandingApproval, RevokeStandingApproval

    action = arguments.standing_action
    base = f"/projects/{project}/standing-approvals"
    if action == "create":
        try:
            spec = json.loads(arguments.spec.read_text(encoding="utf-8"))
            body = CreateStandingApproval(
                spec=spec,
                expected_spec_digest=arguments.expected_spec_digest,
                per_start_budget_cap_cents=arguments.per_start_budget_cents,
                max_starts=arguments.max_starts,
                max_total_budget_cents=arguments.max_total_budget_cents,
                min_interval_seconds=arguments.min_interval_seconds,
                max_concurrent=arguments.max_concurrent,
                not_before=arguments.not_before,
                not_after=arguments.not_after,
                trigger=arguments.trigger,
                follow_on_standing_approval_id=arguments.follow_on,
                retry_max=arguments.retry_max,
                retry_on=arguments.retry_on,
            ).model_dump(mode="json")
        except (OSError, ValueError) as error:
            raise ReviewError(f"Invalid standing approval: {type(error).__name__}") from None
        return ("POST", base, body, key)
    if action == "list":
        query = f"?offset={int(arguments.offset)}&limit={int(arguments.limit)}"
        return ("GET", base + query, None, None)
    standing = quote(arguments.id, safe="")
    if action == "show":
        return ("GET", f"{base}/{standing}", None, None)
    body = RevokeStandingApproval(
        expected_revision=arguments.expected_revision, reason=arguments.reason
    ).model_dump(mode="json")
    return ("POST", f"{base}/{standing}/revoke", body, key)


def _cancel_running(transport, response: dict, key: str) -> list[dict]:
    """Ordinary, separate cancels for the campaigns a revoke listed."""
    from urllib.parse import quote

    outcomes = []
    for campaign_id in response.get("running_campaign_ids", []):
        path = f"/campaigns/{quote(campaign_id, safe='')}"
        status, current = transport("GET", path, None, None)
        if not 200 <= status < 300 or not isinstance(current, dict):
            outcomes.append({"campaign_id": campaign_id, "cancel_status": status})
            continue
        body = {"expected_revision": current["revision"], "reason": "standing approval revoked"}
        status, _ = transport("POST", path + "/cancel", body, f"{key}-cancel-{campaign_id}"[:200])
        outcomes.append({"campaign_id": campaign_id, "cancel_status": status})
    return outcomes


def run_campaign(arguments: argparse.Namespace, transport_factory=None) -> int:
    from urllib.parse import quote

    from .agent_research_review import (
        LocalTransport,
        ReviewError,
        control_origin,
        read_owner_header,
    )
    from .campaign_models import (
        ApproveCampaign,
        CancelCampaign,
        SubmitCampaign,
        approve_payload,
    )

    if arguments.campaign_action == "preapproval-digests":
        from .campaign_preapproval import preview_for_campaign

        try:
            found = preview_for_campaign(
                arguments.state_dir,
                arguments.salad_campaign_profiles,
                arguments.campaign_id,
                arguments.stage_key,
            )
        except (OSError, ValueError) as error:
            print(json.dumps({"error": str(error)[:200]}))
            return 1
        print(json.dumps({"preapprovals": found}, indent=2, sort_keys=True))
        return 0

    from .owner_identity import IdentityError, resolve_identity

    # Only decision requests carry (and validate) the identity; a read grants nothing, so a bad
    # label in the environment must not block `get`, `list`, `report`, `receipt` or standing reads.
    read_only = arguments.campaign_action in _READ_ONLY_CAMPAIGN_ACTIONS or (
        arguments.campaign_action == "standing"
        and arguments.standing_action in _READ_ONLY_STANDING_ACTIONS
    )
    try:
        identity = (
            None
            if read_only
            else resolve_identity(arguments, "campaign-" + str(arguments.campaign_action))
        )
    except IdentityError as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2

    class CampaignTransport(LocalTransport):
        """Like the proposal transport, but relays control denial bodies; carries the identity."""

        def __call__(self, method, path, body=None, key=None):
            import urllib.error
            import urllib.request

            headers = self.request_headers(key)
            data = json.dumps(body).encode() if body is not None else None
            request = urllib.request.Request(
                self.origin + path, data=data, headers=headers, method=method
            )
            try:
                with self.opener.open(request, timeout=30) as response:
                    return response.status, json.loads(response.read(8 * 1024 * 1024))
            except urllib.error.HTTPError as error:
                try:
                    return error.code, json.loads(error.read(65536))
                except ValueError:
                    return error.code, None
            except Exception:
                raise ReviewError("Control service unreachable or refused") from None

    try:
        origin = control_origin(arguments.control_url)
        transport = (transport_factory or CampaignTransport)(
            origin, read_owner_header(arguments.owner_header)
        )
        if isinstance(transport, LocalTransport):
            transport.identity = identity
        action = arguments.campaign_action
        key = getattr(arguments, "idempotency_key", None)
        project = quote(getattr(arguments, "project_id", "") or "", safe="")
        campaign = quote(getattr(arguments, "campaign_id", "") or "", safe="")
        if action == "standing":
            call = _standing_call(arguments, project, key)
        elif action == "submit":
            try:
                spec = json.loads(arguments.spec.read_text(encoding="utf-8"))
                body = SubmitCampaign(spec=spec).model_dump(mode="json")
            except (OSError, ValueError) as error:
                raise ReviewError(f"Invalid campaign spec: {type(error).__name__}") from None
            call = ("POST", f"/projects/{project}/campaigns", body, key)
        elif action == "approve":
            if not key:
                # A fresh key per call, so a batch can never replay one campaign's approval.
                key = "approve-" + uuid.uuid4().hex
                print(f"idempotency-key: {key}", file=sys.stderr)
            revision = arguments.expected_revision
            if revision is None:
                read_status, current = transport("GET", f"/campaigns/{campaign}", None, None)
                if not (200 <= read_status < 300 and isinstance(current, dict)):
                    raise ReviewError("Could not read the campaign's current revision")
                revision = current["revision"]
            body = ApproveCampaign(
                expected_revision=revision,
                expected_spec_digest=arguments.expected_spec_digest,
            )
            body = approve_payload(body)
            call = ("POST", f"/campaigns/{campaign}/approve", body, key)
        elif action == "cancel":
            body = CancelCampaign(
                expected_revision=arguments.expected_revision, reason=arguments.reason
            ).model_dump(mode="json")
            call = ("POST", f"/campaigns/{campaign}/cancel", body, key)
        elif action == "get":
            call = ("GET", f"/campaigns/{campaign}", None, None)
        elif action == "receipt":
            call = ("GET", f"/campaigns/{campaign}/receipt", None, None)
        elif action == "report":
            flag = "true" if arguments.detail else "false"
            if arguments.campaign_id and not (arguments.since or arguments.until):
                call = ("GET", f"/campaigns/{campaign}/report?detail={flag}", None, None)
            elif arguments.project_id and arguments.since and arguments.until and not campaign:
                since = quote(arguments.since, safe="")
                until = quote(arguments.until, safe="")
                query = f"since={since}&until={until}&limit={int(arguments.limit)}&detail={flag}"
                call = ("GET", f"/projects/{project}/campaign-report?{query}", None, None)
            else:
                raise ValueError("report needs --campaign-id, or --project-id with --since/--until")
        else:
            query = f"?offset={int(arguments.offset)}&limit={int(arguments.limit)}"
            call = ("GET", f"/projects/{project}/campaigns{query}", None, None)
        status, response = transport(*call)
        if (
            action == "standing"
            and arguments.standing_action == "revoke"
            and arguments.cancel_running
            and 200 <= status < 300
            and isinstance(response, dict)
        ):
            response = {**response, "cancelled": _cancel_running(transport, response, key)}
    except ReviewError as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 1
    except ValueError as error:
        print(json.dumps({"error": f"Invalid arguments: {type(error).__name__}"}), file=sys.stderr)
        return 2
    ok = 200 <= status < 300
    if ok and action == "report" and arguments.format == "text" and isinstance(response, dict):
        print(response.get("text", ""))
        return 0
    print(json.dumps(response if ok else {"status": status, "body": response}, sort_keys=True))
    return 0 if ok else 1


def parser() -> argparse.ArgumentParser:
    from .agent_sandbox_cli import add_agent_sandbox_commands
    from .connection_tunnel import add_connection_tunnel_command

    command = argparse.ArgumentParser(prog="tessera")
    subcommands = command.add_subparsers(dest="command", required=True)
    add_agent_sandbox_commands(subcommands)
    add_connection_tunnel_command(subcommands)
    from .agent_research_review import add_agent_proposal_commands

    add_agent_proposal_commands(subcommands)
    _add_campaign_commands(subcommands)
    from .authority_cli import add_authority_commands

    add_authority_commands(subcommands)
    from .provider_registration_cli import add_parser as add_providers_command

    add_providers_command(subcommands)
    from .deploy_check_cli import add_parser as add_deploy_check_command

    add_deploy_check_command(subcommands)
    from tessera_agent.reasoning_install_cli import add_parser as add_reasoning_command

    add_reasoning_command(subcommands)
    from .deploy_upgrade_cli import add_parser as add_deploy_upgrade_command

    add_deploy_upgrade_command(subcommands)
    from .deploy_upgrade_cli import add_rollback_parser

    add_rollback_parser(subcommands)
    from .focused_tests_cli import add_dev_commands

    add_dev_commands(subcommands)
    from .provider_trust_cli import add_provider_trust_commands

    add_provider_trust_commands(subcommands)
    from .smoke_credentials_cli import add_dev_commands

    add_dev_commands(subcommands)
    from .db_cli import add_db_commands

    add_db_commands(subcommands)
    from .credential_reenroll import add_parser as add_credentials_command

    add_credentials_command(subcommands)
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
    for recovery_command in (backup, restore):
        recovery_command.add_argument(
            "--max-backup-bytes",
            type=int,
            default=None,
            help="operator size bound for the backup (default 1 GiB, at most 64 GiB); "
            "a restore needs a bound that covers the backup",
        )
    retention = subcommands.add_parser(
        "retention", help="storage retention: list (dry run) or apply the policy"
    )
    retention.add_argument("action", choices=["plan", "apply", "verify"])
    retention.add_argument("--state-dir", required=True, type=Path)
    retention.add_argument(
        "--plan-digest",
        help="apply only the plan with this digest (as printed by `retention plan`)",
    )
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
    serve.add_argument(
        "--campaign-tick-seconds",
        type=float,
        default=2.0,
        help="campaign runner tick interval, 0.2-60 (ADR 0038)",
    )
    serve.add_argument(
        "--campaign-stuck-scan-seconds",
        type=float,
        default=300.0,
        help="how often to scan campaigns for stuck conditions, 0 disables (default 300)",
    )
    serve.add_argument(
        "--campaign-unclaimed-autoreconcile-limit",
        type=int,
        choices=range(0, 11),
        default=2,
        metavar="0-10",
        help=(
            "automatic zero reconciles per campaign of unknown attempts whose provider proves "
            "no create was claimed, 0 disables (default 2; ADR 0157)"
        ),
    )
    serve.add_argument(
        "--salad-credential-directory",
        type=Path,
        help=(
            "absolute path of the Salad credential store (the private directory the guardian "
            "resolves management credentials from). It is read for expiry metadata only, never "
            "a value, so the health route and campaign admission can report an expired or "
            "revoked credential up front; without it that state reads as unknown"
        ),
    )
    serve.add_argument(
        "--salad-campaign-profiles",
        type=Path,
        help=(
            "alias: owner-only Salad campaign profile file, registered as the salad "
            "provider's profiles; without it Salad stages are refused"
        ),
    )
    serve.add_argument(
        "--salad-guardian-read",
        type=Path,
        help=(
            "owner-only config (tessera.salad.guardian-read.v1) naming the guardian's pinned "
            "status endpoint: control reads each Salad frame's guardian record itself before "
            "a bounded-hold settlement (ADR 0041); without it no Salad stage settles on the "
            "guardian's verdict and the hold waits for a human charge"
        ),
    )
    serve.add_argument(
        "--campaign-providers",
        type=Path,
        help=(
            "owner-only campaign provider file (tessera.campaign-providers.v1): local-docker "
            "and self-hosted worker principals and Salad profiles (ADR 0040)"
        ),
    )
    serve.add_argument(
        "--provider-trust",
        type=Path,
        help=(
            "owner-only provider data-class trust file (tessera.provider-trust.v1); without "
            "it every provider receives public data only (ADR 0040)"
        ),
    )
    serve.add_argument(
        "--program-budget",
        type=Path,
        help=(
            "owner-only program budget file (tessera.program-budget.v1): caps on the money "
            "committed across campaigns, counted at every admission and start (ADR 0045)"
        ),
    )
    serve.add_argument(
        "--agent-submissions",
        action="store_true",
        help=(
            "let a credential with campaign:submit_cleared consume a human's approved "
            "campaign submission (default: off; needs --agent-submission-kill-switch) (ADR 0143)"
        ),
    )
    serve.add_argument(
        "--agent-submission-kill-switch",
        type=Path,
        help=(
            "marker file: while it exists (or its state is ambiguous) every approved-submission "
            "consume is refused"
        ),
    )
    serve.add_argument(
        "--accept-automation-gate-go",
        action="store_true",
        help=(
            "count a gate `go` recorded by the owner credential under a self-declared "
            "automation label as the go of an unlock (default: only a person's session); "
            "the decision keeps its class on the receipt (ADR 0051)"
        ),
    )
    serve.add_argument(
        "--no-campaign-runner",
        action="store_true",
        help="serve the campaign routes without the in-process runner",
    )
    serve.add_argument(
        "--no-campaign-scheduler",
        action="store_true",
        help="run the campaign runner without starting campaigns from standing approvals",
    )
    serve.add_argument(
        "--review-single-owner",
        action="store_true",
        help=(
            "single-owner deployment: let one person resolve review findings of jobs they "
            "submitted through another credential (recorded as single_owner_opt_in)"
        ),
    )
    serve.add_argument(
        "--code-review-git-remotes",
        type=Path,
        help=(
            "JSON file mapping repository names to local bare repositories for code review "
            "submit (env fallback TESSERA_CODE_REVIEW_GIT_REMOTES); default off"
        ),
    )
    serve.add_argument(
        "--monitor-enrollments",
        type=Path,
        help=(
            "owner-only execution-monitor enrollment file (tessera.monitor-enrollments.v1): "
            "opt in to the pinned mutual-TLS monitor receiver listener; without it no "
            "listener starts (ADR 0127)"
        ),
    )
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


def main(argv: list[str] | None = None) -> int:
    words = sys.argv[1:] if argv is None else argv
    if words[:1] == ["credentials"]:
        # The installed ``tessera`` calls main() with no argument; the guard must hold for it too.
        arguments, extra = parser().parse_known_args(words)
        if extra:  # an unknown argument may carry a credential: never echo it
            print(
                "Credential arguments invalid; use --help (no secret arguments).", file=sys.stderr
            )
            return 2
    else:
        arguments = parser().parse_args(argv)
    if arguments.command == "credentials":
        from .credential_reenroll import run as run_credentials

        return run_credentials(arguments)
    if arguments.command == "agent-sandbox":
        from .agent_sandbox_cli import run_agent_sandbox

        return run_agent_sandbox(arguments)
    if arguments.command == "connection-tunnel":
        from .connection_tunnel import run_connection_tunnel

        return run_connection_tunnel(arguments)
    if arguments.command == "agent-proposal":
        from .agent_research_review import run_agent_proposal

        return run_agent_proposal(arguments)
    if arguments.command == "campaign":
        return run_campaign(arguments)
    if arguments.command == "authority":
        from .authority_cli import run_authority

        return run_authority(arguments)
    if arguments.command == "providers":
        from .provider_registration_cli import run as run_providers

        return run_providers(arguments)
    if arguments.command == "deploy-rollback":
        from .deploy_upgrade_cli import run_rollback

        return run_rollback(arguments)
    if arguments.command == "deploy-upgrade":
        from .deploy_upgrade_cli import run as run_deploy_upgrade

        return run_deploy_upgrade(arguments)
    if arguments.command == "reasoning":
        from tessera_agent.reasoning_install_cli import run as run_reasoning

        return run_reasoning(arguments)
    if arguments.command == "dev":
        if arguments.dev_action == "smoke-credentials":
            from .smoke_credentials_cli import run as run_smoke_credentials

            return run_smoke_credentials(arguments)
        from .focused_tests_cli import run_dev

        return run_dev(arguments)
    if arguments.command == "deploy-check":
        from .deploy_check_cli import run as run_deploy_check

        return run_deploy_check(arguments)
    if arguments.command == "admin":
        from .provider_trust_cli import run_provider_trust

        return run_provider_trust(arguments)
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
        try:
            database_path, header_path = initialize_state(arguments.state_dir)
        except (StoreError, ValueError) as error:
            print(f"init failed: {error}", file=sys.stderr)
            return 1
        except (OSError, sqlite3.Error) as error:
            # An unwritable parent, a path the filesystem rejects, a full disk or a database that
            # cannot be opened: a short message naming only the kind of failure, never the path
            # and never a traceback. initialize_state leaves a failed bootstrap in place for
            # diagnosis (it never deletes a path another process may have filled), so say so,
            # because a retry would otherwise only report that the directory already exists.
            reason = (
                error.strerror
                if isinstance(error, OSError) and error.strerror
                else type(error).__name__
            )
            message = f"init failed: the state directory could not be created ({reason})."
            try:
                left_behind = Path(arguments.state_dir).expanduser().exists()
            except OSError:  # the same path problem that made init fail
                left_behind = False
            if left_behind:
                message += (
                    " A partly created state directory was left in place;"
                    " remove it before retrying."
                )
            print(message, file=sys.stderr)
            return 1
        print(database_path)
        print(header_path)
        return 0
    if arguments.command == "migrate":
        try:
            print(migrate_state(arguments.state_dir))
        except StoreError as error:
            print(f"migrate failed: {error}", file=sys.stderr)
            return 1
        return 0
    if arguments.command == "db":
        from .db_cli import run_db

        return run_db(arguments)
    if arguments.command == "retention":
        from . import storage_retention

        try:
            if arguments.action == "verify":
                result = storage_retention.verify(arguments.state_dir)
            elif arguments.action == "plan":
                result = storage_retention.run(
                    arguments.state_dir, mode="dry_run", trigger="manual"
                )
            else:
                result = storage_retention.run(
                    arguments.state_dir,
                    mode="apply",
                    trigger="manual",
                    expect_plan_digest=arguments.plan_digest,
                )
        except (storage_retention.RetentionError, OSError) as error:
            print(f"retention failed: {error}", file=sys.stderr)
            return 1
        print(json.dumps(result, sort_keys=True, indent=1))
        return 0 if result["status"] == "complete" else 1
    if arguments.command == "backup":
        try:
            print(
                backup_state(
                    arguments.state_dir, arguments.output, max_bytes=arguments.max_backup_bytes
                )["backup_id"]
            )
        except (StoreError, sqlite3.Error, OSError) as error:
            print(f"backup failed: {error}", file=sys.stderr)
            return 1
        return 0
    if arguments.command == "restore":
        try:
            database_path, header_path = restore_state(
                arguments.backup, arguments.state_dir, max_bytes=arguments.max_backup_bytes
            )
        except (StoreError, sqlite3.Error, OSError) as error:
            print(f"restore failed: {error}", file=sys.stderr)
            return 1
        print(database_path)
        print(header_path)
        return 0
    if arguments.command == "serve":
        try:
            require_sqlite_runtime()
        except StoreError as error:
            print(f"serve failed: {error}", file=sys.stderr)
            return 1
        owner = None
        monitor_service = None
        if not 0.2 <= arguments.campaign_tick_seconds <= 60:
            print("serve configuration error: --campaign-tick-seconds is 0.2-60", file=sys.stderr)
            return 2
        remotes_file = arguments.code_review_git_remotes
        if remotes_file is None and os.environ.get("TESSERA_CODE_REVIEW_GIT_REMOTES"):
            remotes_file = Path(os.environ["TESSERA_CODE_REVIEW_GIT_REMOTES"])
        if remotes_file is not None:
            from .code_review_git import load_git_remotes

            try:
                load_git_remotes(remotes_file)
            except (ValueError, OSError):
                print(
                    "serve configuration error: code review git remotes file is unreadable "
                    "or malformed",
                    file=sys.stderr,
                )
                return 2
        runner_interval = None if arguments.no_campaign_runner else arguments.campaign_tick_seconds
        if arguments.agent_submissions and arguments.agent_submission_kill_switch is None:
            print(
                "serve configuration error: --agent-submissions needs "
                "--agent-submission-kill-switch",
                file=sys.stderr,
            )
            return 2
        try:
            deployment = serving_config(
                arguments.bind_host, arguments.trusted_host, arguments.public_origin
            )
            from .provider_trust import builtin_policy, load_policy_file

            # A symlink, foreign owner, group/world-writable or malformed file is a
            # TrustPolicyRefused (a ValueError): serve exits 2 below.
            trust_policy = (
                load_policy_file(arguments.provider_trust)
                if arguments.provider_trust is not None
                else builtin_policy()
            )
            from .campaign_providers import configure_providers, load_providers_file

            provider_config = (
                load_providers_file(arguments.campaign_providers)
                if arguments.campaign_providers is not None
                else {}
            )
            if arguments.salad_campaign_profiles is not None:
                if provider_config.get("salad_profiles") is not None:
                    raise ValueError(
                        "salad profiles come from either --campaign-providers or "
                        "--salad-campaign-profiles, not both"
                    )
                from .salad_campaign_profiles import load_profiles

                provider_config["salad_profiles"] = load_profiles(
                    arguments.salad_campaign_profiles
                ).model_dump(mode="json", by_alias=True)
            if arguments.salad_guardian_read is not None:
                if provider_config.get("salad_guardian_read") is not None:
                    raise ValueError(
                        "the guardian read config comes from either --campaign-providers or "
                        "--salad-guardian-read, not both"
                    )
                if not arguments.salad_guardian_read.is_absolute():
                    raise ValueError("--salad-guardian-read must be an absolute path")
                provider_config["salad_guardian_read"] = str(arguments.salad_guardian_read)
            if arguments.salad_credential_directory is not None:
                if provider_config.get("salad_credential_directory") is not None:
                    raise ValueError(
                        "the salad credential directory comes from either --campaign-providers "
                        "or --salad-credential-directory, not both"
                    )
                if not arguments.salad_credential_directory.is_absolute():
                    raise ValueError("--salad-credential-directory must be an absolute path")
                provider_config["salad_credential_directory"] = str(
                    arguments.salad_credential_directory
                )
            configure_providers(provider_config or None)
            from . import program_budget
            from .campaign_gates import configure_go_authority

            configure_go_authority(
                accept_automation_labelled=bool(arguments.accept_automation_gate_go)
            )
            program_budget.configure(
                program_budget.load_program_budget_file(arguments.program_budget)
                if arguments.program_budget is not None
                else None
            )
            sandbox_backend = None
            if arguments.agent_sandbox_config is not None or arguments.agent_sandbox_sha256:
                from .agent_sandbox_configuration import load_agent_sandbox_backend

                sandbox_backend = load_agent_sandbox_backend(
                    arguments.agent_sandbox_config, arguments.agent_sandbox_sha256
                )
            if arguments.monitor_enrollments is not None:
                from .monitor_service import MonitorService, load_enrollments

                # Refuse a malformed file before any effect; start() re-validates it.
                load_enrollments(arguments.monitor_enrollments)
                monitor_service = MonitorService(arguments.monitor_enrollments, arguments.state_dir)
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
            from .provider_trust import install_policy, record_at_start
            from .store import _database_path

            install_policy(trust_policy)
            app = create_app(
                arguments.state_dir,
                deployment,
                runtime_binding_adapter=adapter,
                agent_sandbox_backend=sandbox_backend,
                campaign_runner_interval=runner_interval,
                campaign_scheduler=not arguments.no_campaign_scheduler,
                campaign_stuck_scan_seconds=max(0.0, arguments.campaign_stuck_scan_seconds),
                campaign_unclaimed_autoreconcile_limit=(
                    arguments.campaign_unclaimed_autoreconcile_limit
                ),
                review_single_owner=arguments.review_single_owner,
                storage_retention=True,
                code_review_git_remotes=remotes_file,
            )
            # The state is initialized and current (create_app checked it): audit the load.
            record_at_start(_database_path(arguments.state_dir), trust_policy)
            app.state.provider_trust_path = arguments.provider_trust
            from .campaign_submissions import AgentSubmissionConfig

            app.state.agent_submission = AgentSubmissionConfig(
                enabled=bool(arguments.agent_submissions),
                kill_switch_path=(
                    None
                    if arguments.agent_submission_kill_switch is None
                    else str(arguments.agent_submission_kill_switch)
                ),
            )
            app.state.campaign_providers_path = arguments.campaign_providers
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
            if monitor_service is not None:
                monitored_lifespan = app.router.lifespan_context

                @asynccontextmanager
                async def monitor_lifespan(application):
                    # The opt-in listener runs only while the app is serving.
                    async with monitored_lifespan(application) as state:
                        try:
                            monitor_service.start()
                        except Exception as error:
                            print(f"serve monitor listener blocked: {error}", file=sys.stderr)
                            raise
                        try:
                            yield state
                        finally:
                            monitor_service.stop()

                app.router.lifespan_context = monitor_lifespan
            try:
                service_lock = maintenance_lock.shared(arguments.state_dir)
                service_lock.__enter__()
            except (StoreError, OSError) as error:
                print(f"serve failed: {error}", file=sys.stderr)
                return 1
            try:
                uvicorn.run(
                    app,
                    host=arguments.bind_host,
                    port=arguments.port,
                )
            finally:
                service_lock.__exit__(None, None, None)
        finally:
            # Also cover app construction, startup/bind failures and runners that
            # never entered lifespan. RuntimeOwnerComposition.close is one-shot.
            if monitor_service is not None:
                monitor_service.stop()
            close_owner()
        return 78 if cleanup_failed else 0
    raise AssertionError("unreachable")
