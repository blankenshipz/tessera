"""Collect human maintenance evidence without using or renewing a worker credential."""

from datetime import UTC, datetime

from tessera_control.execution_models import (
    HumanMaintenanceNeverClaimedProof,
    IssueNeverClaimedMaintenance,
)
from tessera_control.store import canonical_digest

from .kubernetes_contracts import cluster_identity_digest
from .kubernetes_queued_recovery import FIELDS, attest_never_claimed


def validate_authorization(envelope, cluster=None):
    if set(envelope) != {"authorization", "authorization_digest"}:
        raise ValueError("A complete control-issued maintenance authorization is required")
    auth = envelope["authorization"]
    if canonical_digest(auth) != envelope["authorization_digest"]:
        raise ValueError("Maintenance authorization digest changed")
    IssueNeverClaimedMaintenance.model_validate(
        {field: auth[field] for field in IssueNeverClaimedMaintenance.model_fields}
    )
    if (
        auth["purpose"] != "stop-only-never-claimed"
        or any(
            auth[field] is not False
            for field in (
                "execute_allowed",
                "renew_allowed",
                "dispatch_allowed",
                "provider_permission_issued",
            )
        )
        or auth["maintenance_actor_principal_id"] != auth["human_issuer_principal_id"]
    ):
        raise ValueError("Maintenance cannot carry execution/provider credential authority")
    now = datetime.now(UTC)
    issued = datetime.fromisoformat(auth["issued_at"].replace("Z", "+00:00"))
    expiry = datetime.fromisoformat(auth["expires_at"].replace("Z", "+00:00"))
    original_expiry = datetime.fromisoformat(
        auth["original_worker_expired_at"].replace("Z", "+00:00")
    )
    if not original_expiry <= issued <= now < expiry or (expiry - issued).total_seconds() > 300:
        raise ValueError("Maintenance authorization is not current/bounded to expired identity")
    if cluster is not None and (
        cluster_identity_digest(cluster) != auth["actual_target_digest"]
        or cluster.namespace != auth["namespace"]
        or cluster.namespace_uid != auth["namespace_uid"]
        or cluster.cluster_uid != auth["cluster_uid"]
    ):
        raise ValueError("Actual provider target differs from authorized maintenance identity")
    return auth


def collect_maintenance_proof(
    workspace, envelope, cluster, *, exclusive_stopped_worker=False, **readers
):
    if exclusive_stopped_worker is not True:
        raise ValueError("Original worker must be stopped with exclusive retained custody")
    auth = validate_authorization(envelope, cluster)
    proof = attest_never_claimed(
        workspace,
        {field: auth[field] for field in FIELDS},
        cluster,
        exclusive_stopped_worker=exclusive_stopped_worker,
        **readers,
    )
    validate_authorization(
        envelope, cluster
    )  # Slow/denied reads cannot outlive maintenance custody.
    extra = {
        field: auth[field]
        for field in (
            "maintenance_actor_principal_id",
            "maintenance_actor_credential_id",
            "human_issuer_principal_id",
            "original_worker_principal_id",
            "original_worker_credential_id",
            "custody_digest",
            "provider_read_authorization_reference",
            "human_attests_original_worker_stopped_exclusive_custody",
        )
    }
    proof = HumanMaintenanceNeverClaimedProof.model_validate(
        proof
        | extra
        | {
            "maintenance_authorization_id": auth["id"],
            "maintenance_authorization_digest": envelope["authorization_digest"],
        }
    ).model_dump(mode="json")
    return {"proof": proof, "proof_digest": canonical_digest(proof)}
