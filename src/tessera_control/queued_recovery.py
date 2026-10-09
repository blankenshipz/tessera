"""Original-worker never-claimed proof and separately authenticated human release.

Control proves claim absence from its transactional history; external absence is
attributed worker evidence, not independent provider proof. No target is changed.
"""

import json
from datetime import timedelta

from .execution import MAX_OBSERVATION_FUTURE_SKEW
from .execution_models import AttestKubernetesNeverClaimed, ReconcileAttestedNeverSubmitted
from .store import Conflict, Forbidden, _parse_utc, canonical_digest, utc_now

ATTESTATION = "execution.kubernetes_never_claimed_attested"
RECONCILIATION = "execution.attested_never_claimed_reconciled"


def _binding(store, db, actor, grant_id, body, *, historical_worker=None):
    grant = store._grant(db, actor, grant_id)
    if (
        grant["status"] != "cancel_requested"
        or grant["revision"] != body["expected_revision"]
        or grant["epoch"] != body["grant_epoch"]
    ):
        raise Conflict("Cancelled grant revision/epoch is no longer current.")
    worker = historical_worker or store._live_principal(
        db, {"id": grant["worker_principal_id"], "org_id": actor["org_id"]}, audience="worker"
    )
    if worker["kind"] != "worker" or worker["project_id"] != grant["project_id"]:
        raise Forbidden("Current original worker identity is required.")
    registry = db.execute(
        "SELECT lifecycle_state FROM worker_registry WHERE worker_principal_id=? AND org_id=?",
        (worker["id"], actor["org_id"]),
    ).fetchone()
    if registry is None or registry["lifecycle_state"] in {"retired", "quarantined"}:
        raise Forbidden("Original worker is not eligible for recovery evidence.")
    rows = [
        db.execute(f"SELECT * FROM {table} WHERE grant_id=?", (grant_id,)).fetchall()
        for table in (
            "external_operations",
            "kubernetes_dispatches",
            "execution_leases",
            "research_experiment_admitted_attempts",
        )
    ]
    if any(len(row) != 1 for row in rows):
        raise Conflict("One original operation/dispatch/lease/admission is required.")
    operation, dispatch, lease, attempt = [row[0] for row in rows]
    task_epoch = db.execute(
        "SELECT epoch FROM execution_task_epochs WHERE task_id=?", (grant["task_id"],)
    ).fetchone()
    package = db.execute(
        "SELECT * FROM execution_runtime_packages WHERE operation_id=?", (body["operation_id"],)
    ).fetchone()
    if (
        task_epoch is None
        or task_epoch["epoch"] != grant["epoch"]
        or body["epoch"] != grant["epoch"] - 1
        or dispatch["id"] != body["dispatch_id"]
        or dispatch["target_digest"] != body["target_digest"]
        or dispatch["state"] != "cancel_requested"
        or dispatch["claimed_at"] is not None
        or dispatch["claimed_by_principal_id"] is not None
        or operation["desired_state"] != "cancel_requested"
        or operation["provider_id"] is not None
        or operation["adapter_version"] != "kubernetes-job-v1"
        or operation["profile_version"] != "development-minikube-opencode-v1"
        or dispatch["profile_version"] != operation["profile_version"]
        or package is None
        or package["package_digest"] != body["package_digest"]
        or attempt["id"] != dispatch["admitted_attempt_id"]
        or attempt["task_id"] != grant["task_id"]
        or attempt["plan_digest"] != grant["plan_digest"]
        or attempt["outcome"] is not None
        or body["job_name"] != "tessera-op-" + body["operation_id"]
    ):
        raise Conflict("Never-claimed proof conflicts with the original bound history.")
    for row in (operation, dispatch, attempt):
        if (
            row["operation_id"] != body["operation_id"]
            or row["package_digest"] != body["package_digest"]
        ):
            raise Conflict("Operation/package binding changed.")
    for row in (operation, dispatch):
        if (
            row["lease_id"] != body["lease_id"]
            or row["epoch"] != body["epoch"]
            or row["worker_principal_id"] != worker["id"]
        ):
            raise Conflict("Original worker/lease binding changed.")
    if (
        lease["id"] != body["lease_id"]
        or lease["epoch"] != body["epoch"]
        or lease["worker_principal_id"] != worker["id"]
    ):
        raise Conflict("Original lease changed.")
    if (body["target_relation"] == "exact") != (
        body["actual_target_digest"] == body["target_digest"]
    ):
        raise Conflict("Target relation is inconsistent.")
    # Null claim columns alone are insufficient: historical claims/reclaims and
    # remote route effects must also be absent. Unknown observations are NOT proof.
    if (
        db.execute("SELECT 1 FROM execution_observations WHERE grant_id=?", (grant_id,)).fetchone()
        or db.execute("SELECT 1 FROM artifacts WHERE lease_id=?", (body["lease_id"],)).fetchone()
        or db.execute(
            "SELECT 1 FROM events WHERE org_id=? AND "
            "event_type IN ('experiment.kubernetes_dispatch_claimed',"
            "'remote_execution.state','execution.external_operation_receipt') AND "
            "(json_extract(payload_json,'$.operation_id')=? OR "
            "json_extract(payload_json,'$.proposal.operation_id')=?)",
            (actor["org_id"], body["operation_id"], body["operation_id"]),
        ).fetchone()
    ):
        raise Conflict("Claim or possible external effects prevent never-claimed recovery.")
    if db.execute(
        "SELECT 1 FROM external_operation_receipts WHERE operation_id=?", (body["operation_id"],)
    ).fetchone():
        raise Conflict("An external receipt prevents no-start proof.")
    from .external_operations import current_operation_reconciliation

    if current_operation_reconciliation(db, body["operation_id"]) is not None:
        raise Conflict("Existing operation recovery prevents no-start proof.")
    return grant, attempt


def _fresh(store, body, seconds):
    now = store._now()
    if (
        not now - timedelta(seconds=seconds)
        <= _parse_utc(body["observed_at"])
        <= now + MAX_OBSERVATION_FUTURE_SKEW
    ):
        raise Conflict("Never-claimed provider readbacks are stale.")


def attest(store, principal, grant_id, key, digest, payload):
    body = store._body(AttestKubernetesNeverClaimed, payload)

    def validate(db, actor):
        grant, _ = _binding(store, db, actor, grant_id, body)
        if actor["kind"] != "worker" or actor["id"] != grant["worker_principal_id"]:
            raise Forbidden("Only the original worker may attest.")
        _fresh(store, body, 30)
        prior = db.execute(
            "SELECT payload_json FROM events WHERE org_id=? AND event_type=? AND "
            "json_extract(payload_json,'$.grant_id')=? ORDER BY cursor LIMIT 1",
            (actor["org_id"], ATTESTATION, grant_id),
        ).fetchone()
        if prior is not None:
            original = json.loads(prior["payload_json"])["attestation"]
            if any(
                original[field] != value for field, value in body.items() if field != "observed_at"
            ):
                raise Conflict("Original never-claimed proof identity cannot be replaced.")
        return grant

    def command(db, actor):
        grant = validate(db, actor)
        proof_digest = canonical_digest(body)
        store._event(
            db,
            actor,
            grant["project_id"],
            grant["task_id"],
            ATTESTATION,
            grant["revision"],
            {
                "grant_id": grant_id,
                "operation_id": body["operation_id"],
                "attestation": body,
                "attestation_digest": proof_digest,
                "trust_boundary": "original_worker_attested_exclusive_journals_and_actual_target",
                "provider_termination_inferred": False,
                "operational_only": True,
            },
        )
        return 201, {
            "grant_id": grant_id,
            "operation_id": body["operation_id"],
            "attestation_digest": proof_digest,
            "authority_released": False,
        }

    return store._mutate(
        principal,
        key,
        "POST",
        f"/worker/execution-grants/{grant_id}/attest-never-claimed",
        digest,
        command,
        audience="worker",
        replay_guard=lambda db, a, _: validate(db, a),
    )


def reconcile(store, principal, grant_id, key, digest, payload):
    body = store._body(ReconcileAttestedNeverSubmitted, payload)

    def command(db, actor):
        store._require_human(actor)
        grant = store._grant(db, actor, grant_id)
        if actor["id"] != grant["created_by_principal_id"]:
            raise Forbidden("Only the original human grant issuer may release the reservation.")
        event = db.execute(
            "SELECT * FROM events WHERE org_id=? AND event_type=? AND "
            "json_extract(payload_json,'$.grant_id')=? ORDER BY cursor DESC LIMIT 1",
            (actor["org_id"], ATTESTATION, grant_id),
        ).fetchone()
        if event is None:
            raise Conflict("Original worker never-claimed attestation is missing.")
        report = json.loads(event["payload_json"])
        proof = report["attestation"]
        if (
            event["actor_principal_id"] != grant["worker_principal_id"]
            or report["attestation_digest"] != body["attestation_digest"]
            or proof["operation_id"] != body["operation_id"]
            or proof["expected_revision"] != body["expected_revision"]
        ):
            raise Conflict("Never-claimed attestation binding changed.")
        grant, attempt = _binding(store, db, actor, grant_id, proof)
        _fresh(store, proof, 60)
        store._limit(db, actor, grant["project_id"])
        now, revision = utc_now(), grant["revision"] + 1
        changed = db.execute(
            "UPDATE execution_grants SET status='reconciled',revision=?,updated_at=?,"
            "reconciled_at=?,reconciled_by_principal_id=?,settlement_cents=0 "
            "WHERE id=? AND status='cancel_requested' AND revision=?",
            (revision, now, now, actor["id"], grant_id, grant["revision"]),
        ).rowcount
        released = db.execute(
            "UPDATE execution_project_limits SET held_cents=held_cents-?,updated_at=? "
            "WHERE project_id=? AND held_cents>=?",
            (grant["max_exposure_cents"], now, grant["project_id"], grant["max_exposure_cents"]),
        ).rowcount
        if changed != 1 or released != 1:
            raise Conflict("Reservation or revision accounting fence changed.")
        from .experiments import _append_admitted_attempt_state

        _append_admitted_attempt_state(
            db,
            dict(attempt),
            "interrupted",
            actor["id"],
            reason="not_executed: cancelled never-claimed dispatch",
        )
        store._event(
            db,
            actor,
            grant["project_id"],
            grant["task_id"],
            RECONCILIATION,
            revision,
            {
                "grant_id": grant_id,
                "operation_id": body["operation_id"],
                "dispatch_id": proof["dispatch_id"],
                "admitted_attempt_id": attempt["id"],
                "attestation_digest": body["attestation_digest"],
                "outcome": "not_executed",
                "settlement_cents": 0,
                "provider_termination_inferred": False,
                "operational_only": True,
            },
        )
        updated = dict(grant) | {
            "status": "reconciled",
            "revision": revision,
            "updated_at": now,
            "reconciled_at": now,
            "reconciled_by_principal_id": actor["id"],
            "settlement_cents": 0,
        }
        return 200, store._grant_response(db, updated)

    return store._mutate(
        principal,
        key,
        "POST",
        f"/execution-grants/{grant_id}/reconcile-attested-never-claimed",
        digest,
        command,
    )
