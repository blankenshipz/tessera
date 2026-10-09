"""Same-snapshot output ledger metadata, not byte access or execution attestation."""

import hashlib
import json
import re

from .agent_provenance_models import ProviderJobIdentity
from .store import Conflict, Forbidden, Store, utc_now

_SHA = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")


def create_output_provenance_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_output_provider_bindings (
        output_manifest_id TEXT PRIMARY KEY REFERENCES artifact_manifests(id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        operation_id TEXT NOT NULL REFERENCES external_operations(operation_id),
        provider_id TEXT NOT NULL, job_uid TEXT NOT NULL,
        identity_digest TEXT NOT NULL, actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        credential_id TEXT NOT NULL, recorded_at TEXT NOT NULL)""")
    for action in ("UPDATE", "DELETE"):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS output_provider_binding_no_{action.lower()}
            BEFORE {action} ON agent_output_provider_bindings BEGIN
            SELECT RAISE(ABORT, 'Output provider bindings are append-only'); END""")


def _manifest(conn, actor, project_id, manifest_id):
    Store._project(conn, actor, project_id)
    row = conn.execute(
        "SELECT m.* FROM artifact_manifests m JOIN projects p ON p.id=m.project_id "
        "WHERE m.id=? AND m.project_id=? AND p.org_id=?",
        (manifest_id, project_id, actor["org_id"]),
    ).fetchone()
    if row is None:
        raise Forbidden("Output manifest is outside the authorized project.")
    return row


def _entries(manifest):
    if len(manifest["artifacts_json"]) > 1048576:
        raise Conflict("Output manifest metadata exceeds the projection cap.")
    entries = json.loads(manifest["artifacts_json"])
    if not isinstance(entries, list) or len(entries) > 128:
        raise Conflict("Output manifest exceeds the projection record limit.")
    return entries


def disclosed_artifacts(conn, actor, manifest):
    """Recheck disclosure/custody/retention using metadata only; never read bytes."""
    if actor["role"] == "human":
        return {entry["id"] for entry in _entries(manifest)}
    release = conn.execute(
        "SELECT id,state,manifest_digest FROM artifact_custody_releases "
        "WHERE manifest_id=? ORDER BY revision DESC LIMIT 1",
        (manifest["id"],),
    ).fetchone()
    disposition = conn.execute(
        "SELECT disposition FROM artifact_dispositions WHERE manifest_id=? "
        "ORDER BY revision DESC LIMIT 1",
        (manifest["id"],),
    ).fetchone()
    if (
        release is None
        or release["state"] != "released"
        or release["manifest_digest"] != manifest["manifest_digest"]
        or disposition is None
        or disposition["disposition"] != "retain_quarantined"
    ):
        return set()
    visible = set()
    contexts = conn.execute(
        "SELECT payload_json FROM external_agent_contexts "
        "WHERE project_id=? AND credential_id=? AND destination=? "
        "AND json_extract(payload_json,'$.manifest_id')=? LIMIT 129",
        (
            manifest["project_id"],
            actor["credential_id"],
            actor.get("agent_destination"),
            manifest["id"],
        ),
    ).fetchall()
    if len(contexts) > 128:
        raise Conflict("Output disclosure projection exceeds the context record limit.")
    entries = {entry["id"]: entry for entry in _entries(manifest)}
    for context in contexts:
        body = json.loads(context["payload_json"])
        if body.get("disclosure_approved") is not True or body.get("classification") not in {
            "public",
            "project",
        }:
            continue
        artifact_id = body.get("artifact_id")
        entry = entries.get(artifact_id)
        if (
            entry is None
            or body.get("custody_release_id") != release["id"]
            or body.get("expected_manifest_digest") != manifest["manifest_digest"]
            or body.get("sha256") != entry["sha256"]
            or body.get("size") != entry["size"]
            or type(body.get("max_bytes")) is not int
            or body["max_bytes"] < entry["size"]
        ):
            continue
        retained = conn.execute(
            "SELECT 1 FROM artifacts a JOIN project_artifact_retention r ON r.artifact_id=a.id "
            "AND r.project_id=a.project_id JOIN project_artifact_placements p "
            "ON p.artifact_id=a.id "
            "AND p.project_id=a.project_id WHERE a.id=? AND a.project_id=? "
            "AND a.sha256=? AND a.size=? AND r.state='active' "
            "AND a.status='quarantined' AND a.classification='unclassified' "
            "AND p.sha256=a.sha256 AND p.size=a.size",
            (artifact_id, manifest["project_id"], entry["sha256"], entry["size"]),
        ).fetchone()
        if retained:
            visible.add(artifact_id)
    return visible


def _provider_identity(proof):
    identity = ProviderJobIdentity.model_validate(proof).model_dump()
    encoded = json.dumps(
        identity | {"version": "kubernetes-provider-v1"}, sort_keys=True, separators=(",", ":")
    ).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return identity, "kubernetes-v1:" + digest, digest


def record_output_provider_binding(conn, actor, project_id, manifest_id, provider_job):
    """Caller-owned transaction; bind opaque UID only to an already observed provider hash."""
    if not conn.in_transaction:
        raise RuntimeError("Output binding requires a caller-owned transaction.")
    live = Store._live_principal(
        conn, actor, audience=actor.get("audience", "control"), external_allowed=True
    )
    live["agent_destination"] = actor.get("agent_destination")
    manifest = _manifest(conn, live, project_id, manifest_id)
    if live["kind"] == "worker":
        if live["id"] != manifest["worker_principal_id"]:
            raise Forbidden("Only the original output worker may bind its provider identity.")
    elif live["kind"] == "agent":
        scope = conn.execute(
            "SELECT * FROM external_agent_scopes WHERE credential_id=?", (live["credential_id"],)
        ).fetchone()
        if (
            scope is None
            or "provenance:append" not in json.loads(scope["actions_json"])
            or scope["destination"] != live["agent_destination"]
            or not disclosed_artifacts(conn, live, manifest)
        ):
            raise Forbidden("Output provider attribution requires scoped current disclosure.")
    elif live["role"] != "human":
        raise Forbidden("Output provider attribution requires an authorized actor.")
    identity, provider_id, digest = _provider_identity(provider_job)
    operation = conn.execute(
        "SELECT provider_id FROM external_operations WHERE operation_id=? AND org_id=? "
        "AND project_id=? AND lease_id=? AND grant_id=? AND task_id=?",
        (
            manifest["operation_id"],
            live["org_id"],
            project_id,
            manifest["lease_id"],
            manifest["grant_id"],
            manifest["task_id"],
        ),
    ).fetchone()
    if operation is None or operation["provider_id"] != provider_id:
        raise Conflict("Provider UID proof does not match the recorded output operation identity.")
    existing = conn.execute(
        "SELECT * FROM agent_output_provider_bindings WHERE output_manifest_id=?", (manifest_id,)
    ).fetchone()
    if existing:
        if existing["identity_digest"] != digest:
            raise Conflict("Output provider identity cannot be replaced.")
        return dict(existing)
    conn.execute(
        """INSERT INTO agent_output_provider_bindings
        (output_manifest_id,org_id,project_id,operation_id,provider_id,job_uid,identity_digest,
        actor_principal_id,credential_id,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            manifest_id,
            live["org_id"],
            project_id,
            manifest["operation_id"],
            provider_id,
            identity["job_uid"],
            digest,
            live["id"],
            live["credential_id"],
            utc_now(),
        ),
    )
    return dict(
        conn.execute(
            "SELECT * FROM agent_output_provider_bindings WHERE output_manifest_id=?",
            (manifest_id,),
        ).fetchone()
    )


def output_manifest_metadata(conn, actor, project_id, manifest_id):
    """Internal projection for an already live, project-authorized notebook actor."""
    manifest = _manifest(conn, actor, project_id, manifest_id)
    visible = disclosed_artifacts(conn, actor, manifest)
    if actor["role"] != "human" and not visible:
        return {
            "output_manifest_id": manifest_id,
            "visibility": "withheld_current_disclosure",
            "completeness": "restricted_output_metadata",
            "scientific_validity": "not_assessed",
        }
    artifacts = []
    for entry in _entries(manifest):
        if entry["id"] not in visible:
            continue
        row = conn.execute(
            "SELECT id,sha256,size,late FROM artifacts WHERE id=? AND project_id=?",
            (entry["id"], project_id),
        ).fetchone()
        if (
            row is None
            or not _ID.fullmatch(row["id"])
            or not _SHA.fullmatch(row["sha256"])
            or row["sha256"] != entry["sha256"]
            or row["size"] != entry["size"]
        ):
            raise Conflict(
                "Output manifest artifact metadata does not match its registered identity."
            )
        artifacts.append(
            {
                "artifact_id": row["id"],
                "sha256": row["sha256"],
                "size": row["size"],
                "late": bool(row["late"]),
            }
        )
    operation = conn.execute(
        "SELECT * FROM external_operations WHERE operation_id=? AND project_id=? AND org_id=?",
        (manifest["operation_id"], project_id, actor["org_id"]),
    ).fetchone()
    attempt = conn.execute(
        "SELECT a.id,a.state,a.outcome FROM research_experiment_admitted_attempts a "
        "JOIN research_experiments e ON e.id=a.experiment_id "
        "WHERE a.operation_id=? AND e.project_id=?",
        (manifest["operation_id"], project_id),
    ).fetchone()
    binding = conn.execute(
        "SELECT job_uid FROM agent_output_provider_bindings "
        "WHERE output_manifest_id=? AND org_id=? AND project_id=?",
        (manifest_id, actor["org_id"], project_id),
    ).fetchone()
    disposition = conn.execute(
        "SELECT disposition FROM artifact_dispositions WHERE manifest_id=? "
        "ORDER BY revision DESC LIMIT 1",
        (manifest_id,),
    ).fetchone()
    release = conn.execute(
        "SELECT state FROM artifact_custody_releases WHERE manifest_id=? "
        "ORDER BY revision DESC LIMIT 1",
        (manifest_id,),
    ).fetchone()
    outcome = attempt["outcome"] if attempt and attempt["outcome"] else "unknown"
    if outcome not in {"succeeded", "failed", "cancelled", "unknown"}:
        outcome = "unknown"
    from .agent_provenance_output_timeline import output_control_timeline

    return {
        "output_manifest_id": manifest_id,
        "manifest_digest": manifest["manifest_digest"],
        "operation_id": manifest["operation_id"],
        "attempt_id": attempt["id"] if attempt else None,
        "task_id": manifest["task_id"],
        "permission_id": manifest["grant_id"],
        "provider_job_uid": binding["job_uid"] if binding else None,
        "provider_identity_status": "recorded_uid_hash_match" if binding else "job_uid_unknown",
        "late": bool(manifest["late"]),
        "collection_outcome": "late" if manifest["late"] else "on_time",
        "status": "quarantined",
        "artifacts": artifacts,
        "disposition": disposition["disposition"] if disposition else "unknown",
        "custody_state": release["state"] if release else "unknown",
        "control_outcome": outcome,
        "timeline": output_control_timeline(conn, actor, manifest, operation),
        "desired_state": operation["desired_state"] if operation else "unknown",
        "observed_state": operation["observed_state"] if operation else "unknown",
        "visibility": "authorized_metadata",
        "state_semantics": "current_snapshot_ledger_join_not_event_time_state",
        "content": "hashes_and_identifiers_only",
        "completeness": "recorded_output_metadata"
        if len(artifacts) == len(_entries(manifest))
        else "restricted_output_metadata",
        "scientific_validity": "not_assessed",
        "execution_verification": "not_established_by_output_metadata",
    }
