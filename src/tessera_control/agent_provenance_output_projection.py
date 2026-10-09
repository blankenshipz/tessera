"""Resolve event IDs through actual output ledger rows, never event content."""

from .agent_provenance_outputs import output_manifest_metadata


def event_output_metadata(conn, actor, project_id, event_type, payload):
    manifest_id = payload.get("output_manifest_id")
    if event_type == "worker.artifact_manifest_quarantined":
        manifest_id = payload.get("id")
    elif event_type in {
        "human.artifact_disposition_recorded",
        "human.artifact_custody_released",
        "human.artifact_custody_withdrawn",
        "human.artifact_custody_revoked",
        "human.reproduction_bundle_exported",
    }:
        manifest_id = payload.get("manifest_id")
    if isinstance(manifest_id, str):
        row = conn.execute(
            "SELECT id FROM artifact_manifests WHERE id=? AND project_id=?",
            (manifest_id, project_id),
        ).fetchone()
    elif event_type == "worker.artifact_quarantined" and isinstance(payload.get("id"), str):
        row = conn.execute(
            "SELECT m.id FROM artifact_manifests m JOIN artifacts a ON a.lease_id=m.lease_id "
            "AND a.project_id=m.project_id WHERE a.id=? AND m.project_id=?",
            (payload["id"], project_id),
        ).fetchone()
    elif isinstance(payload.get("operation_id"), str):
        row = conn.execute(
            "SELECT id FROM artifact_manifests WHERE operation_id=? AND project_id=?",
            (payload["operation_id"], project_id),
        ).fetchone()
    else:
        return None
    if row is None:
        return {"visibility": "missing_or_restricted_output", "scientific_validity": "not_assessed"}
    return output_manifest_metadata(conn, actor, project_id, row["id"])


def project_record_output(conn, actor, project_id, record):
    if record.get("generated_output") is not None:
        from .agent_provenance_generated import project_generated_output

        output = project_generated_output(
            conn, actor, project_id, record["generated_output"]["output_id"]
        )
        record["output"] = output
        if output["visibility"] != "authorized_metadata":
            record.pop("generated_output", None)
            record["content_digest"] = None
        return record
    references = record["references"]
    manifest_id = references.get("output_manifest_id")
    if manifest_id is None and references.get("artifact_id"):
        row = conn.execute(
            "SELECT m.id FROM artifact_manifests m JOIN artifacts a ON a.lease_id=m.lease_id "
            "AND a.project_id=m.project_id WHERE a.id=? AND m.project_id=?",
            (references["artifact_id"], project_id),
        ).fetchone()
        manifest_id = row["id"] if row else None
    if manifest_id:
        row = conn.execute(
            "SELECT id FROM artifact_manifests WHERE id=? AND project_id=?",
            (manifest_id, project_id),
        ).fetchone()
        output = (
            output_manifest_metadata(conn, actor, project_id, manifest_id)
            if row
            else {
                "visibility": "missing_or_restricted_output",
                "scientific_validity": "not_assessed",
            }
        )
        record["output"] = output
        if output["visibility"] != "authorized_metadata" and actor["role"] != "human":
            record["content_digest"] = None
            references["artifact_id"] = None
            references["provider_job_uid"] = None
    elif references.get("artifact_id") and actor["role"] != "human":
        record["content_digest"] = None
        references["artifact_id"] = None
        record["output"] = {
            "visibility": "withheld_current_disclosure",
            "completeness": "restricted_output_metadata",
        }
    return record
