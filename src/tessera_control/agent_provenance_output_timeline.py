"""Closed lifecycle metadata; timestamps and renewal acks are not causal explanations."""


def output_control_timeline(conn, actor, manifest, operation):
    grant = conn.execute(
        "SELECT status,revision,epoch,created_at,updated_at,expires_at FROM execution_grants "
        "WHERE id=? AND project_id=?",
        (manifest["grant_id"], manifest["project_id"]),
    ).fetchone()
    lease = conn.execute(
        "SELECT issued_at,expires_at,hard_expires_at,renewed_at,epoch FROM execution_leases "
        "WHERE id=? AND grant_id=?",
        (manifest["lease_id"], manifest["grant_id"]),
    ).fetchone()
    renewal = conn.execute(
        "SELECT COUNT(*) AS count,MIN(created_at) AS first_ack_at,MAX(created_at) AS last_ack_at "
        "FROM events WHERE org_id=? AND project_id=? AND event_type='execution.lease_renewed' "
        "AND json_extract(payload_json,'$.lease_id')=?",
        (actor["org_id"], manifest["project_id"], manifest["lease_id"]),
    ).fetchone()
    cancellation = conn.execute(
        "SELECT cursor,event_type,actor_principal_id,created_at FROM events "
        "WHERE org_id=? AND project_id=? AND event_type='execution.grant_cancel_requested' "
        "AND json_extract(payload_json,'$.grant_id')=? ORDER BY cursor LIMIT 1",
        (actor["org_id"], manifest["project_id"], manifest["grant_id"]),
    ).fetchone()
    return {
        "manifest_recorded_at": manifest["created_at"],
        "lease_id": manifest["lease_id"],
        "manifest_epoch": manifest["epoch"],
        "grant": dict(grant) if grant else None,
        "lease": dict(lease) if lease else None,
        "operation_created_at": operation["created_at"] if operation else None,
        "operation_updated_at": operation["updated_at"] if operation else None,
        "renewal_acknowledgments": dict(renewal),
        "first_recorded_cancellation_request": dict(cancellation) if cancellation else None,
        "cancellation_reason": "not_recorded_in_closed_metadata",
        "late_cause": "not_inferred_from_late_flag_or_current_fence",
        "coverage": "central_recorded_lifecycle_metadata_only",
    }
