"""Separate original-worker preparation evidence and original-owner held release.

This is worker-attested recovery, not independent provider proof. The historical
Job create-intent remains true in the evidence. No provider ID or terminal state
is inferred and no existing zero-create proof model is widened.
"""

import json
from datetime import timedelta

from .execution import MAX_OBSERVATION_FUTURE_SKEW, _only_unconfirmed_worker_unknown
from .execution_models import (
    AttestKubernetesPreparationNotSubmitted,
    ReconcileAttestedPreparationNotSubmitted,
)
from .store import Conflict, Forbidden, _parse_utc, canonical_digest, utc_now

ATTESTATION_EVENT = "execution.kubernetes_preparation_not_submitted_attested"
RECONCILIATION_EVENT = "execution.attested_preparation_not_submitted_reconciled"
TRUST_BOUNDARY = "original_worker_attested_exclusive_journals_and_target_not_independent"


def _binding(store, connection, actor, grant_id, body):
    grant = store._grant(connection, actor, grant_id)
    if grant["status"] != "cancel_requested" or grant["revision"] != body["expected_revision"]:
        raise Conflict("Original cancelled grant revision is no longer current.")
    # The attestation and its human consumption both require current original
    # worker credential authority. Expired identity must not become an owner-only
    # create-intent override through this new path.
    worker = store._live_principal(
        connection,
        {"id": grant["worker_principal_id"], "org_id": actor["org_id"]},
        audience="worker",
    )
    if worker["kind"] != "worker" or worker["project_id"] != grant["project_id"]:
        raise Forbidden("Current original worker identity is required.")
    registry = connection.execute(
        """SELECT lifecycle_state FROM worker_registry
        WHERE worker_principal_id=? AND org_id=? AND project_id=?""",
        (worker["id"], actor["org_id"], grant["project_id"]),
    ).fetchone()
    if registry is None or registry["lifecycle_state"] in {"retired", "quarantined"}:
        raise Forbidden("Original worker registry identity is no longer eligible.")
    operations = connection.execute(
        "SELECT * FROM external_operations WHERE grant_id=?", (grant_id,)
    ).fetchall()
    dispatches = connection.execute(
        "SELECT * FROM kubernetes_dispatches WHERE grant_id=?", (grant_id,)
    ).fetchall()
    leases = connection.execute(
        "SELECT * FROM execution_leases WHERE grant_id=?", (grant_id,)
    ).fetchall()
    if len(operations) != 1 or len(dispatches) != 1 or len(leases) != 1:
        raise Conflict("Preparation evidence requires one exact original operation/dispatch/lease.")
    operation, dispatch, lease = operations[0], dispatches[0], leases[0]
    if (
        operation["operation_id"] != body["operation_id"]
        or operation["lease_id"] != body["lease_id"]
        or operation["epoch"] != body["epoch"]
        or operation["package_digest"] != body["package_digest"]
        or operation["worker_principal_id"] != worker["id"]
        or operation["desired_state"] != "cancel_requested"
        or operation["provider_id"] is not None
        or operation["adapter_version"] != "kubernetes-job-v1"
        or operation["profile_version"] != "development-minikube-opencode-v1"
        or dispatch["operation_id"] != body["operation_id"]
        or dispatch["lease_id"] != body["lease_id"]
        or dispatch["epoch"] != body["epoch"]
        or dispatch["package_digest"] != body["package_digest"]
        or dispatch["target_digest"] != body["target_digest"]
        or dispatch["worker_principal_id"] != worker["id"]
        or dispatch["claimed_by_principal_id"] != worker["id"]
        or dispatch["state"] != "cancel_requested"
        or lease["id"] != body["lease_id"]
        or lease["epoch"] != body["epoch"]
        or lease["worker_principal_id"] != worker["id"]
        or body["job_name"] != f"tessera-op-{body['operation_id']}"
        or not _only_unconfirmed_worker_unknown(connection, body["operation_id"])
    ):
        raise Conflict("Preparation evidence conflicts with original dispatch or observations.")
    return grant


def _fresh(store, body, seconds):
    now, observed = store._now(), _parse_utc(body["observed_at"])
    if not now - timedelta(seconds=seconds) <= observed <= now + MAX_OBSERVATION_FUTURE_SKEW:
        raise Conflict("Preparation recovery readbacks are not fresh.")


def attest(store, principal, grant_id, key, digest, payload):
    body = store._body(AttestKubernetesPreparationNotSubmitted, payload)
    path = f"/worker/execution-grants/{grant_id}/attest-preparation-not-submitted"

    def validate(connection, actor):
        grant = _binding(store, connection, actor, grant_id, body)
        if actor["kind"] != "worker" or actor["id"] != grant["worker_principal_id"]:
            raise Forbidden("Only the current original worker may attest preparation evidence.")
        _fresh(store, body, 30)
        original = connection.execute(
            """SELECT payload_json FROM events WHERE org_id=? AND event_type=?
            AND json_extract(payload_json,'$.grant_id')=? ORDER BY cursor LIMIT 1""",
            (actor["org_id"], ATTESTATION_EVENT, grant_id),
        ).fetchone()
        if original is not None:
            prior = json.loads(original["payload_json"])["attestation"]
            immutable_fields = (
                "operation_id",
                "lease_id",
                "epoch",
                "package_digest",
                "target_digest",
                "job_name",
                "namespace_uid",
                "journal_binding_digest",
                "journal_manifest_digest",
                "preparation_manifest_digest",
                "preparation_pod_uid",
                "preparation_journal_pod_uid",
                "preparation_gate",
            )
            if any(prior.get(field) != body[field] for field in immutable_fields):
                raise Conflict("Original preparation attestation identity cannot be replaced.")
        return grant

    def command(connection, actor):
        grant = validate(connection, actor)
        attestation_digest = canonical_digest(body)
        store._event(
            connection,
            actor,
            grant["project_id"],
            grant["task_id"],
            ATTESTATION_EVENT,
            grant["revision"],
            {
                "grant_id": grant_id,
                "operation_id": body["operation_id"],
                "worker_principal_id": actor["id"],
                "proof_kind": body["proof_kind"],
                "attestation_digest": attestation_digest,
                "attestation": body,
                "preparation_pod_name": "tessera-output-prepare-" + body["operation_id"],
                "execution_object_selector": "tessera.dev/operation=" + body["operation_id"],
                "helper_object_selector": "tessera.dev/output-operation=" + body["operation_id"],
                "historical_create_intent": True,
                "trust_boundary": TRUST_BOUNDARY,
                "provider_termination_inferred": False,
                "operational_only": True,
            },
        )
        return 201, {
            "grant_id": grant_id,
            "operation_id": body["operation_id"],
            "attestation_digest": attestation_digest,
            "authority_released": False,
        }

    return store._mutate(
        principal,
        key,
        "POST",
        path,
        digest,
        command,
        audience="worker",
        replay_guard=lambda c, a, _: validate(c, a),
    )


def reconcile(store, principal, grant_id, key, digest, payload):
    body = store._body(ReconcileAttestedPreparationNotSubmitted, payload)
    path = f"/execution-grants/{grant_id}/reconcile-attested-preparation-not-submitted"

    def owner(connection, actor):
        store._require_human(actor)
        grant = store._grant(connection, actor, grant_id)
        if actor["id"] != grant["created_by_principal_id"]:
            raise Forbidden(
                "Only the original human grant owner may reconcile preparation evidence."
            )
        return grant

    def command(connection, actor):
        owner(connection, actor)
        event = connection.execute(
            """SELECT * FROM events WHERE org_id=? AND event_type=?
            AND json_extract(payload_json,'$.grant_id')=? ORDER BY cursor DESC LIMIT 1""",
            (actor["org_id"], ATTESTATION_EVENT, grant_id),
        ).fetchone()
        if event is None:
            raise Conflict("Original-worker preparation attestation is missing.")
        report = json.loads(event["payload_json"])
        attestation = store._body(AttestKubernetesPreparationNotSubmitted, report["attestation"])
        grant = _binding(store, connection, actor, grant_id, attestation)
        if (
            grant["revision"] != body["expected_revision"]
            or report["operation_id"] != body["operation_id"]
            or attestation["operation_id"] != body["operation_id"]
            or report["attestation_digest"] != body["attestation_digest"]
            or report["attestation_digest"] != canonical_digest(attestation)
            or report["worker_principal_id"] != grant["worker_principal_id"]
            or event["actor_principal_id"] != grant["worker_principal_id"]
            or event["project_id"] != grant["project_id"]
            or event["resource_revision"] != grant["revision"]
            or report["trust_boundary"] != TRUST_BOUNDARY
        ):
            raise Conflict("Original-worker preparation attestation is mismatched.")
        _fresh(store, attestation, 60)
        store._limit(connection, actor, grant["project_id"])
        now, revision = utc_now(), grant["revision"] + 1
        changed = connection.execute(
            """UPDATE execution_grants SET status='reconciled',revision=?,updated_at=?,
            reconciled_at=?,reconciled_by_principal_id=?,settlement_cents=0
            WHERE id=? AND revision=? AND status='cancel_requested'""",
            (revision, now, now, actor["id"], grant_id, grant["revision"]),
        ).rowcount
        released = connection.execute(
            """UPDATE execution_project_limits SET held_cents=held_cents-?,updated_at=?
            WHERE project_id=? AND held_cents>=?""",
            (grant["max_exposure_cents"], now, grant["project_id"], grant["max_exposure_cents"]),
        ).rowcount
        if changed != 1 or released != 1:
            raise Conflict("Preparation accounting fence or reservation changed.")
        updated = dict(grant)
        updated.update(
            status="reconciled",
            revision=revision,
            updated_at=now,
            reconciled_at=now,
            reconciled_by_principal_id=actor["id"],
            settlement_cents=0,
        )
        store._event(
            connection,
            actor,
            grant["project_id"],
            grant["task_id"],
            RECONCILIATION_EVENT,
            revision,
            {
                "grant_id": grant_id,
                "operation_id": body["operation_id"],
                "attestation_digest": body["attestation_digest"],
                "proof_kind": "pre_job_preparation",
                "historical_create_intent": True,
                "settlement_cents": 0,
                "provider_termination_inferred": False,
                "trust_boundary": TRUST_BOUNDARY,
                "operational_only": True,
            },
        )
        return 200, store._grant_response(connection, updated)

    # Successful reconciliation is a stable idempotent receipt; reauth and
    # original-owner eligibility still apply, without pretending readbacks renewed.
    return store._mutate(
        principal, key, "POST", path, digest, command, replay_guard=lambda c, a, _: owner(c, a)
    )
