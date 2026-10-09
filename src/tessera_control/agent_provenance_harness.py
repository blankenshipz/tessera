"""Validate approved content-free public capture within the existing transaction."""

import json

from .agent_provenance_harness_models import PublicHarnessDisclosure
from .store import Conflict, Forbidden

_KINDS = {
    "message": "message",
    "tool": "tool",
    "permission": "permission",
    "delegation": "delegation",
    "generated_output": "attempt",
    "guard_phase": "model",
}
_PINS = (
    "session_id",
    "harness_instance_id",
    "task_id",
    "context_id",
    "context_digest",
    "skill_digest",
    "config_digest",
)


def validate_public_harness_record(conn, actor, project_id, record):
    """No raw contents, new authority, schemas or second transaction are admitted."""
    harness = record.get("harness")
    if harness is None:
        return
    if actor["kind"] != "agent":
        raise Forbidden("Public harness reports require the real enrolled agent actor.")
    scope = conn.execute(
        "SELECT * FROM external_agent_scopes WHERE credential_id=?", (actor["credential_id"],)
    ).fetchone()
    if (
        scope is None
        or "provenance:append" not in json.loads(scope["actions_json"])
        or scope["destination"] != actor.get("agent_destination")
    ):
        raise Forbidden("Public harness capture is outside enrolled append/destination scope.")
    references = record["references"]
    if (
        references.get("context_id") != harness["context_id"]
        or references.get("task_id") != harness["task_id"]
    ):
        raise Forbidden(
            "Public harness capture requires its exact context and central task references."
        )
    task = conn.execute(
        "SELECT 1 FROM tasks WHERE id=? AND project_id=?", (harness["task_id"], project_id)
    ).fetchone()
    context = conn.execute(
        "SELECT c.payload_json,p.role FROM external_agent_contexts c "
        "JOIN principals p ON p.id=c.created_by WHERE c.id=? AND c.project_id=? "
        "AND c.credential_id=? AND c.destination=? AND p.org_id=?",
        (
            harness["context_id"],
            project_id,
            actor["credential_id"],
            scope["destination"],
            actor["org_id"],
        ),
    ).fetchone()
    taskless_research = harness["task_id"] is None and record.get("generated_output") is not None
    if (task is None and not taskless_research) or context is None or context["role"] != "human":
        raise Forbidden("An exact human-issued project/task capture context is required.")
    payload = json.loads(context["payload_json"])
    if payload.get("disclosure_approved") is not True or payload.get("classification") not in {
        "public",
        "project",
    }:
        raise Forbidden("Public harness capture context has no explicit disclosure approval.")
    if harness.get("input_context_id") is not None:
        from .agent_provenance_harness_models import SeparatedPublicHarnessDisclosure
        from .public_capture_contexts import resolve_separated_capture_contexts

        binding = {
            **harness,
            "org_id": actor["org_id"],
            "project_id": project_id,
            "actor_principal_id": actor["id"],
            "credential_id": actor["credential_id"],
            "model_id": record["model_id"],
            "destination_digest": record["remote"]["destination_digest"],
        }
        resolution = resolve_separated_capture_contexts(conn, actor, binding)
        policy = SeparatedPublicHarnessDisclosure.model_validate_json(
            resolution["capture_policy"]["body"]
        ).model_dump()
    else:
        try:
            policy = PublicHarnessDisclosure.model_validate_json(
                payload.get("body", "")
            ).model_dump()
        except (ValueError, TypeError) as error:
            raise Forbidden("Context is not a typed public harness disclosure policy.") from error
    for field in ("run_id", "task_id", "context_digest", "skill_digest", "config_digest"):
        if harness[field] != policy[field]:
            raise Conflict("Public harness metadata differs from immutable human-approved pins.")
    if (
        harness["public_event"] not in policy["allowed_public_events"]
        or record["kind"] != _KINDS[harness["public_event"]]
    ):
        raise Forbidden("Public event category is outside its approved capture boundary.")
    if harness["public_event"] == "message" and not record["remote"].get("message_id"):
        raise Forbidden("Public message metadata requires its opaque upstream message reference.")
    if harness["public_event"] == "tool" and not record.get("tool_id"):
        raise Forbidden("Public tool metadata requires its opaque tool identity.")
    if (harness["public_event"] == "guard_phase") != (record.get("guard_phase") is not None):
        raise Forbidden(
            "Guard observations require the explicit approved public metadata category."
        )
    if record.get("guard_phase") is not None:
        from .agent_provenance_guard import validate_guard_phase_record

        validate_guard_phase_record(conn, actor, project_id, record, policy)
    if record.get("generated_output") is not None:
        from .agent_provenance_generated import validate_generated_output_record

        validate_generated_output_record(conn, actor, project_id, record)
    elif harness["public_event"] == "generated_output" or record["content_digest"] is not None:
        _validate_generated_output(conn, actor, project_id, record, harness)
    parameters = (actor["org_id"], project_id, actor["credential_id"], harness["run_id"])
    where = "org_id=? AND project_id=? AND credential_id=? AND "
    where += "json_extract(envelope_json,'$.harness.run_id')=?"
    previous = conn.execute(
        f"SELECT envelope_json FROM agent_provenance WHERE {where} ORDER BY sequence LIMIT 1",
        parameters,
    ).fetchone()
    if previous:
        first = json.loads(previous["envelope_json"])["harness"]
        separation = ("input_context_id", "input_body_digest", "capture_policy_context_id")
        if any(first.get(field) != harness.get(field) for field in (*_PINS, *separation)):
            raise Conflict("A public capture run cannot replace its session/instance/context pins.")
    duplicate = conn.execute(
        f"SELECT record_id FROM agent_provenance WHERE {where} AND "
        "(json_extract(envelope_json,'$.harness.source_event_id')=? OR "
        "json_extract(envelope_json,'$.harness.source_sequence')=?) LIMIT 1",
        (*parameters, harness["source_event_id"], harness["source_sequence"]),
    ).fetchone()
    if duplicate and duplicate["record_id"] != record["record_id"]:
        raise Conflict("A public source event/sequence cannot be rekeyed as a new record.")


def _validate_generated_output(conn, actor, project_id, record, harness):
    from .agent_provenance_outputs import _manifest, disclosed_artifacts

    references = record["references"]
    if not references.get("output_manifest_id") or not references.get("artifact_id"):
        raise Forbidden("Generated output metadata requires a registered manifest and artifact.")
    manifest = _manifest(conn, actor, project_id, references["output_manifest_id"])
    if (
        manifest["task_id"] != harness["task_id"]
        or references.get("operation_id") != manifest["operation_id"]
        or references["artifact_id"] not in disclosed_artifacts(conn, actor, manifest)
    ):
        raise Forbidden(
            "Generated output is outside its task/operation/current disclosure boundary."
        )
    artifact = conn.execute(
        "SELECT sha256 FROM artifacts WHERE id=? AND project_id=?",
        (references["artifact_id"], project_id),
    ).fetchone()
    if artifact is None or record["content_digest"] != "sha256:" + artifact["sha256"]:
        raise Conflict("Generated output digest differs from the registered artifact.")
