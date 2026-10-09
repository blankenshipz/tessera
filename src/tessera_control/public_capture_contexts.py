"""Actual separate human input/policy readback; never a pin-echo admission shim."""

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

from .agent_provenance_harness_models import HarnessID, SeparatedPublicHarnessDisclosure
from .external_agents import ExternalAgentStore
from .store import Conflict, Forbidden, Store, StoreError

SEPARATION_FIELDS = ("input_context_id", "input_body_digest", "capture_policy_context_id")


class ResolvedCaptureContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    context_id: HarnessID
    org_id: HarnessID
    project_id: HarnessID
    credential_id: HarnessID
    destination_digest: HarnessID
    issuer_principal_id: HarnessID
    issuer_role: str
    disclosure_approved: bool
    classification: str
    body: str = Field(max_length=4000, repr=False)


class SeparatedCaptureResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    input_context: ResolvedCaptureContext
    capture_policy: ResolvedCaptureContext


def validate_separated_resolution(binding, resolution):
    try:
        return _validate_separated_resolution(binding, resolution)
    except StoreError:
        raise
    except (ValueError, TypeError, KeyError):
        raise Forbidden("Unsupported actual curated input/public policy readback.") from None


def _validate_separated_resolution(binding, resolution):
    """Verify actual readback bodies; return ephemeral resolution for input delivery."""
    result = SeparatedCaptureResolution.model_validate(resolution).model_dump(mode="json")
    for name, id_field in (
        ("input_context", "input_context_id"),
        ("capture_policy", "capture_policy_context_id"),
    ):
        context = result[name]
        if (
            context["context_id"] != binding[id_field]
            or context["issuer_role"] != "human"
            or not context["disclosure_approved"]
            or context["classification"] not in {"public", "project"}
            or any(
                context[key] != binding[key]
                for key in ("org_id", "project_id", "credential_id", "destination_digest")
            )
        ):
            raise Forbidden("Input/policy readback is outside the human-approved capture scope.")
    if (
        binding["context_id"] != binding["input_context_id"]
        or binding["input_context_id"] == binding["capture_policy_context_id"]
    ):
        raise Forbidden("Capture policy is not a substitute for curated input.")
    try:
        input_json = json.loads(result["input_context"]["body"])
    except ValueError:
        input_json = None
    if isinstance(input_json, dict) and input_json.get("schema_version") in {
        "tessera-public-harness-disclosure-v1",
        "tessera-separated-public-harness-disclosure-v1",
    }:
        raise Forbidden("Public capture policy JSON cannot serve as curated research input.")
    digest = hashlib.sha256(result["input_context"]["body"].encode("utf-8")).hexdigest()
    if digest != binding["input_body_digest"] or digest != binding["context_digest"]:
        raise Conflict("Actual curated input readback differs from the approved body digest.")
    policy = SeparatedPublicHarnessDisclosure.model_validate_json(
        result["capture_policy"]["body"]
    ).model_dump(mode="json")
    for key, value in policy.items():
        if key == "schema_version":
            continue
        if key in binding and binding[key] != value:
            raise Conflict("Separate human policy differs from the immutable capture/input pins.")
    return result


def resolve_separated_capture_contexts(conn, principal, binding):
    """Authenticated transaction-local backend resolver. Returns bodies only to caller.

    Ordinary immutable body contexts only; artifact contexts have a different custody
    boundary and are not silently substituted here. No tokens or state credentials
    are read; callers supply the existing authenticated principal reference.
    """
    if not conn.in_transaction:
        raise RuntimeError("Capture readback requires a caller-owned transaction.")
    live = ExternalAgentStore._live_principal(conn, principal)
    live["agent_destination"] = principal.get("agent_destination")
    scope = ExternalAgentStore._scope(conn, live, "context:read")
    ExternalAgentStore._scope(conn, live, "provenance:append")
    Store._project(conn, live, binding["project_id"])
    if any(
        live[key] != binding[pin]
        for key, pin in (
            ("id", "actor_principal_id"),
            ("org_id", "org_id"),
            ("credential_id", "credential_id"),
        )
    ):
        raise Forbidden("Capture resolver requires its actual enrolled actor.")
    expected_destination = "sha256:" + hashlib.sha256(scope["destination"].encode()).hexdigest()
    if expected_destination != binding["destination_digest"]:
        raise Forbidden("Capture resolver destination differs from enrollment.")
    result = {}
    for name, field in (
        ("input_context", "input_context_id"),
        ("capture_policy", "capture_policy_context_id"),
    ):
        row = conn.execute(
            "SELECT * FROM external_agent_contexts WHERE id=? AND project_id=? "
            "AND credential_id=? AND destination=?",
            (binding[field], binding["project_id"], live["credential_id"], scope["destination"]),
        ).fetchone()
        if row is None:
            raise Forbidden("Actual separate input/policy context is outside the enrolled scope.")
        issuer = ExternalAgentStore._live_principal(
            conn, {"id": row["created_by"], "org_id": live["org_id"]}
        )
        payload = json.loads(row["payload_json"])
        if "artifact_id" in payload or not isinstance(payload.get("body"), str):
            raise Forbidden("Separated capture requires immutable ordinary body contexts.")
        result[name] = {
            "context_id": row["id"],
            "org_id": issuer["org_id"],
            "project_id": row["project_id"],
            "credential_id": row["credential_id"],
            "destination_digest": expected_destination,
            "issuer_principal_id": issuer["id"],
            "issuer_role": issuer["role"],
            "disclosure_approved": payload["disclosure_approved"],
            "classification": payload["classification"],
            "body": payload["body"],
        }
    return validate_separated_resolution(binding, result)
