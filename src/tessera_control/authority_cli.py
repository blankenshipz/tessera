"""`tessera authority issue-reasoning`: a person's reasoning-only grant over the control API.

A thin client of ``POST /projects/{id}/agent-authority-grants`` with ``bounds.kind`` set to
``reasoning`` (ADR 0155). The control plane decides everything: it accepts the decision only from
a person's session or from the owner credential carrying an automation label, refuses it with
``model_spend_not_configured`` until the project has a usable model-spend limit for the provider,
caps the expiry at 24 hours and at the subject credential's own expiry, and requires the subject
to hold ``authority:consume`` and ``model_spend:record``. This command checks nothing the API does
not also check, and never reads, prints or stores a credential value: its output is the grant's
identifiers and the fields the reasoning runtime profile names it by.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

MAX_EXPIRES_IN = 86_400
DEFAULT_PURPOSE = "reasoning model calls"
# The fields the reasoning runtime profile names its grant by (the runtime binding fence).
RUNTIME_PROFILE_FIELDS = ("grant_id", "revision", "epoch", "subject_credential_id", "destination")


def add_authority_commands(subcommands: argparse._SubParsersAction) -> None:
    command = subcommands.add_parser(
        "authority", help="agent authority decisions over the control API (a person's act)"
    )
    actions = command.add_subparsers(dest="authority_action", required=True)
    issue = actions.add_parser(
        "issue-reasoning",
        help="issue a reasoning-only grant: model calls only, own budget, expiry <= 24h",
        description=(
            "Issue a reasoning-only agent authority grant (ADR 0155). It pays for model calls "
            "to one provider and model and authorizes nothing else. Refused with "
            "model_spend_not_configured until the project's model-spend limits exist."
        ),
    )
    issue.add_argument("--project", "--project-id", dest="project_id", required=True)
    issue.add_argument(
        "--subject-credential-id",
        required=True,
        help="the agent credential the grant is for (needs authority:consume, model_spend:record)",
    )
    issue.add_argument(
        "--destination",
        required=True,
        help="the agent's identity destination (X-Agent-Destination), not the model host",
    )
    issue.add_argument("--model", required=True, help="bare model id, without the provider")
    issue.add_argument("--provider", required=True, help="provider id, for example anthropic")
    issue.add_argument("--budget-cents", type=int, required=True, help="at least 1")
    expiry = issue.add_mutually_exclusive_group(required=True)
    expiry.add_argument(
        "--expires-in",
        type=int,
        metavar="SECONDS",
        help=f"grant lifetime from now, 1-{MAX_EXPIRES_IN}; must end before the subject's expiry",
    )
    expiry.add_argument(
        "--expires-at",
        metavar="ISO8601",
        help="exact expiry with an offset (at most 24h ahead); use it with --idempotency-key "
        "to retry a request whose answer was lost, since --expires-in is relative to now",
    )
    issue.add_argument(
        "--max-duration-seconds",
        type=int,
        help="longest single run the grant pays for, 1-86400 (default: 86400; the grant's "
        "expiry still ends every run)",
    )
    issue.add_argument("--purpose", default=DEFAULT_PURPOSE)
    issue.add_argument("--owner-header", type=Path, required=True)
    issue.add_argument("--control-url", default="http://127.0.0.1:8765")
    issue.add_argument(
        "--idempotency-key",
        help="reuse only to retry the identical request (default: a fresh random key)",
    )
    issue.add_argument(
        "--automation-label",
        help="marks this decision as automation (X-Tessera-Automation); never authority "
        "(default: $TESSERA_AUTOMATION_LABEL or tessera-cli:authority-issue-reasoning)",
    )
    issue.add_argument(
        "--delegation-ref",
        help="the owner delegation this runs under (X-Tessera-Delegation; default: "
        "$TESSERA_DELEGATION_REF); a label, never authority",
    )


def reasoning_body(arguments: argparse.Namespace, now: datetime | None = None) -> dict:
    """The issue request body; validated by the same model the control API uses."""
    from .agent_authority_models import IssueAgentAuthority

    now = now or datetime.now(UTC)
    if arguments.expires_at is not None:
        expires_at = datetime.fromisoformat(arguments.expires_at.replace("Z", "+00:00"))
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("expires-at")
    else:
        if not 1 <= arguments.expires_in <= MAX_EXPIRES_IN:
            raise ValueError("expires-in")
        expires_at = now + timedelta(seconds=arguments.expires_in)
    # Not derived from the clock, so a retry with the same key and --expires-at is identical.
    duration = arguments.max_duration_seconds
    if duration is None:
        duration = MAX_EXPIRES_IN
    bounds = {
        "kind": "reasoning",
        "subject_credential_id": arguments.subject_credential_id,
        "destination": arguments.destination,
        "model": arguments.model,
        "purpose": arguments.purpose,
        "provider_id": arguments.provider,
        "actions": ["model:call"],
        "budget_cents": arguments.budget_cents,
        "max_duration_seconds": duration,
        "expires_at": expires_at.isoformat(),
    }
    return IssueAgentAuthority.model_validate({"bounds": bounds, "request_id": None}).model_dump(
        mode="json"
    )


def grant_summary(grant: dict) -> dict:
    """What the operator needs, and nothing else (no token, no header, no raw body)."""
    bounds = grant.get("bounds") or {}
    runtime = {
        "grant_id": grant.get("id"),
        "revision": grant.get("revision"),
        "epoch": grant.get("epoch"),
        "subject_credential_id": grant.get("subject_credential_id")
        or bounds.get("subject_credential_id"),
        "destination": bounds.get("destination"),
    }
    return {
        "grant_id": grant.get("id"),
        "revision": grant.get("revision"),
        "epoch": grant.get("epoch"),
        "status": grant.get("status"),
        "project_id": grant.get("project_id"),
        "kind": bounds.get("kind"),
        "provider_id": bounds.get("provider_id"),
        "model": bounds.get("model"),
        "budget_cents": bounds.get("budget_cents"),
        "remaining_budget_cents": grant.get("remaining_budget_cents"),
        "max_duration_seconds": bounds.get("max_duration_seconds"),
        "expires_at": bounds.get("expires_at"),
        "runtime_profile": {field: runtime[field] for field in RUNTIME_PROFILE_FIELDS},
    }


def _transport_class():
    from .agent_research_review import LocalTransport, ReviewError

    class AuthorityTransport(LocalTransport):
        """Relays the control's bounded refusal body (a stable code); carries the identity."""

        def __call__(self, method, path, body=None, key=None):
            import urllib.error
            import urllib.request

            request = urllib.request.Request(
                self.origin + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers=self.request_headers(key),
                method=method,
            )
            try:
                with self.opener.open(request, timeout=30) as response:
                    return response.status, json.loads(response.read(1024 * 1024))
            except urllib.error.HTTPError as error:
                try:
                    return error.code, json.loads(error.read(8192))
                except ValueError:
                    return error.code, None
            except Exception:
                raise ReviewError("Control service unreachable or refused") from None

    return AuthorityTransport


def _refusal(status: int, response) -> dict:
    detail = response.get("detail") if isinstance(response, dict) else None
    result: dict = {"status": status}
    if isinstance(detail, str):
        result["detail"] = detail[:300]
    return result


def run_authority(arguments: argparse.Namespace, transport_factory=None) -> int:
    from .agent_research_review import (
        LocalTransport,
        ReviewError,
        control_origin,
        read_owner_header,
    )
    from .owner_identity import IdentityError, resolve_identity

    try:
        identity = resolve_identity(arguments, "authority-" + arguments.authority_action)
    except IdentityError as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2
    try:
        body = reasoning_body(arguments)
    except ValueError:
        print(
            json.dumps(
                {
                    "error": "Invalid reasoning grant arguments: provider, model, budget-cents "
                    f">= 1, expires-in 1-{MAX_EXPIRES_IN} or expires-at with an offset, and "
                    "max-duration-seconds 1-86400"
                }
            ),
            file=sys.stderr,
        )
        return 2
    key = arguments.idempotency_key
    if not key:
        key = "issue-reasoning-" + uuid.uuid4().hex
        print(f"idempotency-key: {key}", file=sys.stderr)
    # A retry with the same key must send the identical body: print the exact expiry to reuse.
    print(f"expires-at: {body['bounds']['expires_at']}", file=sys.stderr)
    try:
        origin = control_origin(arguments.control_url)
        transport = (transport_factory or _transport_class())(
            origin, read_owner_header(arguments.owner_header)
        )
        if isinstance(transport, LocalTransport):
            transport.identity = identity
        project = quote(arguments.project_id, safe="")
        status, response = transport(
            "POST", f"/projects/{project}/agent-authority-grants", body, key
        )
    except ReviewError as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 1
    if not (200 <= status < 300 and isinstance(response, dict)):
        print(json.dumps(_refusal(status, response), sort_keys=True))
        return 1
    print(json.dumps(grant_summary(response), indent=2, sort_keys=True))
    return 0
