"""Transaction-local joins to registered research outputs; never worker identity."""

import hashlib
import json

from pydantic import TypeAdapter

from .agent_provenance_harness_models import HarnessID, SourceSequence
from .agent_provenance_models import GeneratedResearchOutputReference, RegisteredResearchPins
from .external_agents import ExternalAgentStore
from .generated_outputs import GeneratedOutputStore
from .store import Conflict, Forbidden, Store, StoreError

FAMILY = "generated_public_research_output_v1"
PIN_FIELDS = (
    "model_id",
    "run_id",
    "session_id",
    "harness_instance_id",
    "context_id",
    "context_digest",
    "skill_digest",
    "config_digest",
    "task_id",
    "operation_id",
)


def _registered(conn, actor, project_id, output_id):
    if not conn.in_transaction:
        raise RuntimeError("Generated provenance requires the caller's transaction.")
    Store._project(conn, actor, project_id)
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='generated_public_outputs'"
    ).fetchone():
        raise Forbidden("Research output ledger is not installed.")
    row = conn.execute(
        "SELECT * FROM generated_public_outputs WHERE id=? AND project_id=?",
        (output_id, project_id),
    ).fetchone()
    if row is None:
        raise Forbidden("Registered research output is outside the project or family.")
    if (
        hashlib.sha256(row["content"]).hexdigest() != row["sha256"]
        or len(row["content"]) != row["size"]
    ):
        raise Conflict("Registered research output integrity differs.")
    metadata = json.loads(row["metadata_json"])
    try:
        RegisteredResearchPins.model_validate(metadata["pins"])
        GeneratedResearchOutputReference.model_validate(
            {
                "family": FAMILY,
                "output_id": row["id"],
                "disclosure_id": "projection_only",
                "sha256": row["sha256"],
                "output_kind": metadata["output_kind"],
                "writer_actor_id": row["actor_id"],
                "writer_credential_id": row["credential_id"],
                "writer_org_id": metadata["org_id"],
            }
        )
        # Use the existing source metadata constraints rather than forwarding event prose.
        TypeAdapter(HarnessID).validate_python(metadata["source_event_id"])
        TypeAdapter(SourceSequence).validate_python(metadata["source_sequence"])
    except (ValueError, KeyError, TypeError):
        raise Conflict(
            "Registered research metadata is not a supported content-free projection."
        ) from None
    if (
        metadata["id"] != row["id"]
        or metadata["artifact_id"] != row["id"]
        or metadata["sha256"] != row["sha256"]
        or metadata["actor_id"] != row["actor_id"]
        or metadata["credential_id"] != row["credential_id"]
        or metadata["org_id"] != actor["org_id"]
    ):
        raise Conflict("Registered research output attribution differs.")
    return row, metadata


def _disclosure(conn, actor, output_id, disclosure_id):
    if actor["role"] == "human":
        return None
    ExternalAgentStore._scope(conn, actor, "context:read")
    if disclosure_id is None:
        rows = conn.execute(
            "SELECT * FROM generated_output_disclosures WHERE output_id=? AND revoked=0",
            (output_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM generated_output_disclosures WHERE id=? AND output_id=?",
            (disclosure_id, output_id),
        ).fetchall()
    for row in rows:
        bounds = json.loads(row["bounds_json"])
        if (
            row["revoked"]
            or bounds.get("disclosure_approved") is not True
            or bounds["credential_id"] != actor["credential_id"]
            or bounds["destination"] != actor.get("agent_destination")
        ):
            continue
        try:
            issuer = ExternalAgentStore._live_principal(
                conn, {"id": row["issued_by"], "org_id": actor["org_id"]}
            )
            if issuer["role"] != "human":
                continue
            GeneratedOutputStore._expiry(bounds["expires_at"])
        except StoreError:
            continue
        return row["id"]
    raise Forbidden("Current human recipient research output disclosure is required.")


def generated_output_metadata(conn, actor, project_id, output_id, disclosure_id=None):
    """Same-snapshot closed metadata, never bytes or untrusted event prose."""
    row, metadata = _registered(conn, actor, project_id, output_id)
    recipient_disclosure_id = _disclosure(conn, actor, output_id, disclosure_id)
    return {
        "family": FAMILY,
        "visibility": "authorized_metadata",
        "output_id": row["id"],
        "artifact_id": row["id"],
        "recipient_disclosure_id": recipient_disclosure_id,
        "sha256": row["sha256"],
        "size": row["size"],
        "content_digest": "sha256:" + row["sha256"],
        "output_kind": metadata["output_kind"],
        "writer_actor_id": row["actor_id"],
        "writer_credential_id": row["credential_id"],
        "writer_org_id": metadata["org_id"],
        "pins": {key: metadata["pins"][key] for key in PIN_FIELDS},
        "source_event_id": metadata["source_event_id"],
        "source_sequence": metadata["source_sequence"],
        "scientific_validity": "not_assessed",
        "execution_established": False,
        "attribution": "registered_self_reported_generated_output",
        "custody_state": "human_admitted_public",
        "created_at": row["created_at"],
    }


def validate_generated_output_record(conn, actor, project_id, record):
    reference = record.get("generated_output")
    if reference is None:
        return
    if actor["kind"] != "agent" or record.get("harness") is None:
        raise Forbidden(
            "Research output provenance requires its real enrolled author and harness pins."
        )
    ExternalAgentStore._scope(conn, actor, "provenance:append")
    references = record["references"]
    if any(
        references.get(key) is not None
        for key in (
            "artifact_id",
            "output_manifest_id",
            "provider_job_uid",
            "attempt_id",
            "grant_id",
            "consumption_id",
            "permission_id",
        )
    ):
        raise Forbidden("Research output and worker/execution reference families cannot be mixed.")
    row, metadata = _registered(conn, actor, project_id, reference["output_id"])
    _disclosure(conn, actor, row["id"], reference["disclosure_id"])
    if (
        actor["id"] != row["actor_id"]
        or actor["credential_id"] != row["credential_id"]
        or reference["writer_actor_id"] != row["actor_id"]
        or reference["writer_credential_id"] != row["credential_id"]
        or reference["writer_org_id"] != actor["org_id"]
    ):
        raise Forbidden("Research output provenance cannot impersonate its registered writer.")
    if (
        reference["sha256"] != row["sha256"]
        or reference["output_kind"] != metadata["output_kind"]
        or record["content_digest"] != "sha256:" + row["sha256"]
    ):
        raise Conflict("Research output hash/kind differs from actual registration.")
    harness = record["harness"]
    pins = {key: harness.get(key) for key in PIN_FIELDS}
    pins["model_id"] = record["model_id"]
    pins["operation_id"] = references["operation_id"]
    if (
        pins != metadata["pins"]
        or record["kind"] != "attempt"
        or harness["public_event"] != "generated_output"
        or harness["source_event_id"] != metadata["source_event_id"]
        or harness["source_sequence"] != metadata["source_sequence"]
        or references["task_id"] != pins["task_id"]
        or references["context_id"] != pins["context_id"]
    ):
        raise Conflict("Research output public source or immutable runtime pins differ.")
    # Append/replay remains fenced by current human admission, unlike retained history reads.
    grant = conn.execute(
        "SELECT * FROM generated_output_grants WHERE id=?", (row["grant_id"],)
    ).fetchone()
    bounds = json.loads(grant["bounds_json"])
    issuer = ExternalAgentStore._live_principal(
        conn, {"id": grant["issued_by"], "org_id": actor["org_id"]}
    )
    if (
        issuer["role"] != "human"
        or grant["revoked"]
        or grant["revision"] != metadata["expected_revision"]
    ):
        raise Conflict("Research output human admission revoked or changed.")
    GeneratedOutputStore._expiry(bounds["expires_at"])
    if (
        bounds["pins"] != pins
        or bounds["credential_id"] != actor["credential_id"]
        or bounds["destination"] != actor.get("agent_destination")
        or bounds["output_kind"] != reference["output_kind"]
    ):
        raise Forbidden("Research output admission is outside the writer's pinned scope.")


def project_generated_output(conn, actor, project_id, output_id):
    try:
        return generated_output_metadata(conn, actor, project_id, output_id)
    except StoreError:
        return {
            "family": FAMILY,
            "visibility": "withheld_current_disclosure",
            "completeness": "restricted_output_metadata",
            "scientific_validity": "not_assessed",
        }
