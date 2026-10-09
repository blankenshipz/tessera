"""Atomic content-free provenance and permission-fenced notebook metadata."""

import hashlib
import json
import re
import sqlite3
from typing import Any

from .agent_provenance_models import AgentProvenanceRecord, ProvenanceReferences
from .store import Conflict, Forbidden, Store, utc_now

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")


def create_agent_provenance_schema(conn: sqlite3.Connection) -> None:
    from .agent_provenance_outputs import create_output_provenance_schema

    create_output_provenance_schema(conn)
    # Do not use executescript: sqlite3 implicitly commits an active transaction.
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_provenance (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        credential_id TEXT NOT NULL,
        record_id TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        envelope_json TEXT NOT NULL,
        UNIQUE(org_id, project_id, record_id))""")
    conn.execute("""CREATE INDEX IF NOT EXISTS agent_provenance_project_sequence
        ON agent_provenance(org_id, project_id, sequence)""")
    for action in ("UPDATE", "DELETE"):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS agent_provenance_no_{action.lower()}
            BEFORE {action} ON agent_provenance BEGIN
            SELECT RAISE(ABORT, 'Agent provenance is append-only'); END""")


def _identifier(value: str | None) -> None:
    if value is not None and not _IDENTIFIER.fullmatch(value):
        raise ValueError("Provenance requires opaque identifiers, not content or URLs.")


def _body(record: AgentProvenanceRecord | dict[str, Any]) -> dict[str, Any]:
    body = AgentProvenanceRecord.model_validate(record).model_dump(mode="json")
    # Additive optional output links must not change an older outbox's record digest.
    for field in ("output_manifest_id", "provider_job_uid"):
        if body["references"].get(field) is None:
            body["references"].pop(field, None)
    for field in ("record_id", "correlation_id", "causation_id", "model_id", "tool_id"):
        _identifier(body[field])
    for value in body["references"].values():
        _identifier(value)
    for value in body["remote"].values():
        _identifier(value)
    if body["content_digest"] is not None and not _DIGEST.fullmatch(body["content_digest"]):
        raise ValueError("Only SHA-256 content digests are accepted.")
    return body


def _transaction(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise RuntimeError("Provenance requires a caller-owned transaction.")


def _record(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result.pop("request_digest", None)
    result.update(json.loads(result.pop("envelope_json")))
    result["model_attribution"] = "self_reported" if result["model_id"] else "unknown"
    return result


def record_agent_provenance(
    conn: sqlite3.Connection,
    actor: dict[str, Any],
    project_id: str,
    record: AgentProvenanceRecord | dict[str, Any],
) -> dict[str, Any]:
    """Append using the already-authenticated command identity and transaction."""
    _transaction(conn)
    live = Store._live_principal(
        conn, actor, audience=actor.get("audience", "control"), external_allowed=True
    )
    Store._project(conn, live, project_id)
    body = _body(record)
    if body.get("generated_output") is not None and body.get("harness") is None:
        raise Forbidden("Research output provenance requires immutable public harness pins.")
    if body.get("guard_phase") is not None and body.get("harness") is None:
        raise Forbidden("Guard provenance requires approved public harness/context metadata.")
    if body.get("harness") is not None:
        from .agent_provenance_harness import validate_public_harness_record

        live["agent_destination"] = actor.get("agent_destination")
        validate_public_harness_record(conn, live, project_id, body)
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    existing = conn.execute(
        "SELECT * FROM agent_provenance WHERE org_id=? AND project_id=? AND record_id=?",
        (live["org_id"], project_id, body["record_id"]),
    ).fetchone()
    if existing:
        if (
            existing["request_digest"] != digest
            or existing["actor_principal_id"] != live["id"]
            or existing["credential_id"] != live["credential_id"]
        ):
            raise Conflict("Provenance record identity was reused with different content or actor.")
        return _record(existing)
    cursor = conn.execute(
        """INSERT INTO agent_provenance
        (org_id,project_id,actor_principal_id,credential_id,record_id,request_digest,
        recorded_at,envelope_json) VALUES (?,?,?,?,?,?,?,?)""",
        (
            live["org_id"],
            project_id,
            live["id"],
            live["credential_id"],
            body["record_id"],
            digest,
            utc_now(),
            encoded,
        ),
    )
    return _record(
        conn.execute(
            "SELECT * FROM agent_provenance WHERE sequence=?", (cursor.lastrowid,)
        ).fetchone()
    )


def _event(conn, live, project_id, row: sqlite3.Row) -> dict[str, Any]:
    from .agent_provenance_output_projection import event_output_metadata

    result = dict(row)
    payload = json.loads(result.pop("payload_json"))
    # Never forward arbitrary ordinary event payloads (they may contain prose).
    references = {}
    for key in ProvenanceReferences.model_fields:
        if key in {"output_manifest_id", "provider_job_uid"}:
            continue
        value = payload.get(key)
        if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
            references[key] = value
    result["references"] = references
    result["payload_projection"] = "opaque_references_only"
    if result["event_type"] == "generated_output.registered":
        from .agent_provenance_generated import project_generated_output

        result[
            "references"
        ] = {}  # Do not mistake the research artifact alias for a worker artifact.
        result["output"] = project_generated_output(conn, live, project_id, payload.get("id"))
        return result
    output = event_output_metadata(conn, live, project_id, result["event_type"], payload)
    if output is not None:
        result["output"] = output
        if output.get("output_manifest_id"):
            references["output_manifest_id"] = output["output_manifest_id"]
        if output.get("visibility") == "authorized_metadata":
            for field in ("operation_id", "attempt_id", "permission_id", "provider_job_uid"):
                if output.get(field):
                    references[field] = output[field]
    return result


def project_agent_notebook(
    conn: sqlite3.Connection,
    actor: dict[str, Any],
    project_id: str,
    *,
    after_sequence: int = 0,
    after_event_cursor: int = 0,
    limit: int = 100,
    max_bytes: int = 1048576,
) -> dict[str, Any]:
    """Project metadata snapshot; no scientific-validity or full-history claims."""
    _transaction(conn)
    if not (0 <= after_sequence and 0 <= after_event_cursor and 1 <= limit <= 1000):
        raise ValueError("Invalid notebook cursor or page limit.")
    if not 1024 <= max_bytes <= 16 * 1048576:
        raise ValueError("Invalid notebook byte cap.")
    live = Store._live_principal(conn, actor)
    if live["kind"] not in {"local_owner", "human_session"}:
        raise Forbidden("Notebook projection requires separately authenticated human access.")
    Store._project(conn, live, project_id)
    return _project_notebook_metadata(
        conn,
        live,
        project_id,
        after_sequence=after_sequence,
        after_event_cursor=after_event_cursor,
        limit=limit,
        max_bytes=max_bytes,
    )


def project_external_agent_notebook(
    conn: sqlite3.Connection,
    actor: dict[str, Any],
    project_id: str,
    *,
    after_sequence: int = 0,
    after_event_cursor: int = 0,
    limit: int = 100,
    max_bytes: int = 1048576,
) -> dict[str, Any]:
    """Read project metadata only under explicit notebook:read enrollment."""
    _transaction(conn)
    live = Store._live_principal(conn, actor, external_allowed=True)
    live["agent_destination"] = actor.get("agent_destination")
    if live["kind"] != "agent" or live["project_id"] != project_id:
        raise Forbidden("A project-bound external-agent credential is required.")
    Store._project(conn, live, project_id)
    scope = conn.execute(
        "SELECT actions_json,destination FROM external_agent_scopes WHERE credential_id=?",
        (live["credential_id"],),
    ).fetchone()
    if scope is None or "notebook:read" not in json.loads(scope["actions_json"]):
        raise Forbidden("Notebook access is outside explicit external-agent scope.")
    if actor.get("agent_destination") != scope["destination"]:
        raise Forbidden("External-agent destination does not match enrollment.")
    return _project_notebook_metadata(
        conn,
        live,
        project_id,
        after_sequence=after_sequence,
        after_event_cursor=after_event_cursor,
        limit=limit,
        max_bytes=max_bytes,
    )


def _project_notebook_metadata(
    conn: sqlite3.Connection,
    live: dict[str, Any],
    project_id: str,
    *,
    after_sequence: int,
    after_event_cursor: int,
    limit: int,
    max_bytes: int,
) -> dict[str, Any]:
    if not (0 <= after_sequence and 0 <= after_event_cursor and 1 <= limit <= 1000):
        raise ValueError("Invalid notebook cursor or page limit.")
    if not 1024 <= max_bytes <= 16 * 1048576:
        raise ValueError("Invalid notebook byte cap.")
    params = (live["org_id"], project_id)
    high = conn.execute(
        "SELECT COALESCE(MAX(sequence),0) FROM agent_provenance WHERE org_id=? AND project_id=?",
        params,
    ).fetchone()[0]
    event_high = conn.execute(
        "SELECT COALESCE(MAX(cursor),0) FROM events WHERE org_id=? AND project_id=?", params
    ).fetchone()[0]
    provenance = [
        _record(row)
        for row in conn.execute(
            "SELECT * FROM agent_provenance WHERE org_id=? AND project_id=? AND sequence>? "
            "ORDER BY sequence LIMIT ?",
            (*params, after_sequence, limit + 1),
        )
    ]
    from .agent_provenance_output_projection import project_record_output

    provenance = [project_record_output(conn, live, project_id, record) for record in provenance]
    from .agent_provenance_harness_projection import attach_public_harness_accounting

    provenance = attach_public_harness_accounting(conn, project_id, provenance)
    from .agent_provenance_guard import guard_observation_projection

    provenance = [
        guard_observation_projection(conn, live, project_id, record) for record in provenance
    ]
    events = [
        _event(conn, live, project_id, row)
        for row in conn.execute(
            "SELECT cursor,event_type,resource_revision,actor_principal_id,created_at,"
            "correlation_id,causation_id,payload_json FROM events "
            "WHERE org_id=? AND project_id=? AND cursor>? ORDER BY cursor LIMIT ?",
            (*params, after_event_cursor, limit + 1),
        )
    ]
    more = len(provenance) > limit or len(events) > limit
    provenance, events = provenance[:limit], events[:limit]
    restricted = any(
        item.get("output", {}).get("visibility") == "withheld_current_disclosure"
        or item.get("output", {}).get("completeness") == "restricted_output_metadata"
        or item.get("guard_observation", {}).get("visibility") == "withheld_current_disclosure"
        for item in provenance + events
    )
    result = {
        "version": "agent-notebook-metadata-v1",
        "project_id": project_id,
        "provenance": provenance,
        "control_events": events,
        "watermark": {"sequence": high, "event_cursor": event_high},
        "next": {
            "after_sequence": provenance[-1]["sequence"] if provenance else after_sequence,
            "after_event_cursor": events[-1]["cursor"] if events else after_event_cursor,
        },
        "has_more": more,
        "completeness": "partial_page"
        if more or after_sequence or after_event_cursor
        else "permission_filtered_recorded_project_metadata_snapshot"
        if restricted
        else "complete_recorded_project_metadata_snapshot",
        "output_coverage": "registered_output_event_and_reference_joins_only;"
        "unregistered_history_unknown",
        "coverage": "historical_uninstrumented_and_off_platform_work_unknown",
        "harness_coverage": "approved_public_metadata_sources_only;unseen_sources_unknown;"
        "raw_transcript_and_hidden_reasoning_not_captured",
        "content": "redacted_metadata_only",
        "scientific_validity": "not_assessed",
        "execution_verification": "not_established_by_provenance",
    }
    if len(json.dumps(result, sort_keys=True).encode()) > max_bytes:
        raise Conflict("Notebook projection exceeds the requested byte cap.")
    return result
