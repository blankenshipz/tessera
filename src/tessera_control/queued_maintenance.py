"""Human-issued stop-only recovery for an expired original worker identity.

Event-backed, five-minute, non-bearer maintenance authorizations. They never enter
the credential/lease/execution permission system or confer Kubernetes permission.
External journal/provider facts remain explicitly attributed human attestations.
"""

import json
import uuid
from datetime import timedelta

from .execution_models import (
    IssueNeverClaimedMaintenance,
    KubernetesNeverClaimedIdentity,
    ReconcileMaintenanceNeverClaimed,
)
from .queued_recovery import _binding, _fresh
from .store import Conflict, Forbidden, _parse_utc, canonical_digest, utc_now

ISSUED = "execution.never_claimed_maintenance_issued"
ATTESTED = "execution.human_maintenance_never_claimed_attested"
RECONCILED = "execution.human_maintenance_never_claimed_reconciled"
TRUST = "original_human_issuer_attested_exclusive_journals_and_provider_reads_not_independent"


def _identity(store, db, actor, grant_id, body):
    store._require_human(actor)
    grant = store._grant(db, actor, grant_id)
    if actor["id"] != grant["created_by_principal_id"]:
        raise Forbidden("Only the original human grant issuer may perform maintenance recovery.")
    credential = db.execute(
        "SELECT c.*,p.org_id,p.revoked_at AS principal_revoked_at FROM credentials c "
        "JOIN principals p ON p.id=c.principal_id WHERE c.id=? AND p.org_id=?",
        (body["original_worker_credential_id"], actor["org_id"]),
    ).fetchone()
    if (
        credential is None
        or credential["principal_id"] != grant["worker_principal_id"]
        or credential["principal_id"] != body["original_worker_principal_id"]
        or credential["kind"] != "worker"
        or credential["audience"] != "worker"
        or credential["project_id"] != grant["project_id"]
        or credential["revoked_at"] is not None
        or credential["principal_revoked_at"] is not None
        or credential["expires_at"] is None
        or _parse_utc(credential["expires_at"]) > store._now()
    ):
        raise Conflict("Exact expired original worker credential is required; never renew it.")
    lease = db.execute(
        "SELECT issued_at FROM execution_leases WHERE id=?", (body["lease_id"],)
    ).fetchone()
    if lease is None or _parse_utc(credential["created_at"]) > _parse_utc(lease["issued_at"]):
        raise Conflict("Credential was not present at original lease issuance.")
    worker = {
        "id": credential["principal_id"],
        "kind": "worker",
        "project_id": credential["project_id"],
    }
    return _binding(store, db, actor, grant_id, body, historical_worker=worker)


def _expiry(store, body):
    if not store._now() < _parse_utc(body["expires_at"]) <= store._now() + timedelta(minutes=5):
        raise Conflict("Maintenance authorization must expire within five minutes.")


def issue(store, principal, grant_id, key, digest, payload):
    body = store._body(IssueNeverClaimedMaintenance, payload)
    path = f"/execution-grants/{grant_id}/never-claimed-maintenance"

    def validate(db, actor):
        grant, _ = _identity(store, db, actor, grant_id, body)
        _expiry(store, body)
        return grant

    def command(db, actor):
        grant = validate(db, actor)
        prior = db.execute(
            "SELECT payload_json FROM events WHERE org_id=? AND event_type=? AND "
            "json_extract(payload_json,'$.grant_id')=? ORDER BY cursor LIMIT 1",
            (actor["org_id"], ISSUED, grant_id),
        ).fetchone()
        if prior is not None:
            original = json.loads(prior["payload_json"])["authorization"]
            if any(
                original[field] != value for field, value in body.items() if field != "expires_at"
            ):
                raise Conflict("Original maintenance custody/identity cannot be replaced.")
        latest = db.execute(
            "SELECT payload_json FROM events WHERE org_id=? AND event_type=? AND "
            "json_extract(payload_json,'$.grant_id')=? ORDER BY cursor DESC LIMIT 1",
            (actor["org_id"], ISSUED, grant_id),
        ).fetchone()
        if (
            latest is not None
            and _parse_utc(json.loads(latest["payload_json"])["authorization"]["expires_at"])
            > store._now()
        ):
            raise Conflict("One exclusive unexpired maintenance authorization already exists.")
        authorization = body | {
            "id": "maintenance_" + uuid.uuid4().hex,
            "grant_id": grant_id,
            "org_id": actor["org_id"],
            "project_id": grant["project_id"],
            "human_issuer_principal_id": actor["id"],
            "maintenance_actor_principal_id": actor["id"],
            "maintenance_actor_credential_id": actor["credential_id"],
            "original_worker_expired_at": credential_expiry(db, body),
            "issued_at": utc_now(),
            "execute_allowed": False,
            "renew_allowed": False,
            "dispatch_allowed": False,
            "provider_permission_issued": False,
        }
        auth_digest = canonical_digest(authorization)
        store._event(
            db,
            actor,
            grant["project_id"],
            grant["task_id"],
            ISSUED,
            grant["revision"],
            {
                "grant_id": grant_id,
                "operation_id": body["operation_id"],
                "authorization": authorization,
                "authorization_digest": auth_digest,
                "trust_boundary": TRUST,
                "operational_only": True,
            },
        )
        return 201, {"authorization": authorization, "authorization_digest": auth_digest}

    def guard(db, actor, response):
        validate(db, actor)
        auth = response["authorization"]
        if auth["maintenance_actor_credential_id"] != actor["credential_id"]:
            raise Forbidden("Maintenance authorization belongs to its issuing human session.")

    return store._mutate(principal, key, "POST", path, digest, command, replay_guard=guard)


def credential_expiry(db, body):
    return db.execute(
        "SELECT expires_at FROM credentials WHERE id=?", (body["original_worker_credential_id"],)
    ).fetchone()[0]


def reconcile(store, principal, grant_id, key, digest, payload):
    body = store._body(ReconcileMaintenanceNeverClaimed, payload)
    proof = body["proof"]
    path = f"/execution-grants/{grant_id}/reconcile-maintenance-never-claimed"

    def command(db, actor):
        grant, attempt = _identity(store, db, actor, grant_id, proof)
        if canonical_digest(proof) != body["proof_digest"]:
            raise Conflict("Maintenance proof digest does not match the submitted proof.")
        event = db.execute(
            "SELECT * FROM events WHERE org_id=? AND event_type=? AND "
            "json_extract(payload_json,'$.grant_id')=? AND "
            "json_extract(payload_json,'$.authorization.id')=?",
            (actor["org_id"], ISSUED, grant_id, proof["maintenance_authorization_id"]),
        ).fetchone()
        if event is None:
            raise Conflict("Bound human maintenance authorization is missing.")
        record = json.loads(event["payload_json"])
        auth = record["authorization"]
        if (
            record["authorization_digest"] != proof["maintenance_authorization_digest"]
            or canonical_digest(auth) != record["authorization_digest"]
            or event["actor_principal_id"] != actor["id"]
            or auth["maintenance_actor_principal_id"] != actor["id"]
            or auth["human_issuer_principal_id"] != actor["id"]
            or auth["maintenance_actor_credential_id"] != actor["credential_id"]
            or auth["purpose"] != "stop-only-never-claimed"
            or auth["original_worker_expired_at"] != credential_expiry(db, proof)
            or _parse_utc(auth["expires_at"]) <= store._now()
            or _parse_utc(proof["observed_at"]) < _parse_utc(auth["issued_at"])
        ):
            raise Conflict("Maintenance authority is expired, foreign or no longer bound.")
        fields = set(KubernetesNeverClaimedIdentity.model_fields) | {
            "original_worker_principal_id",
            "original_worker_credential_id",
            "custody_digest",
            "provider_read_authorization_reference",
            "maintenance_actor_principal_id",
            "maintenance_actor_credential_id",
            "human_issuer_principal_id",
            "human_attests_original_worker_stopped_exclusive_custody",
        }
        if any(proof[field] != auth[field] for field in fields):
            raise Conflict("Maintenance proof/custody/target identity changed.")
        _fresh(store, proof, 30)
        store._limit(db, actor, grant["project_id"])
        now, revision = utc_now(), grant["revision"] + 1
        if (
            db.execute(
                "UPDATE execution_grants SET status='reconciled',revision=?,updated_at=?,"
                "reconciled_at=?,reconciled_by_principal_id=?,settlement_cents=0 "
                "WHERE id=? AND status='cancel_requested' AND revision=? AND epoch=?",
                (revision, now, now, actor["id"], grant_id, grant["revision"], grant["epoch"]),
            ).rowcount
            != 1
        ):
            raise Conflict("Original maintenance revision fence changed.")
        if (
            db.execute(
                "UPDATE execution_project_limits SET held_cents=held_cents-?,updated_at=? "
                "WHERE project_id=? AND held_cents>=?",
                (
                    grant["max_exposure_cents"],
                    now,
                    grant["project_id"],
                    grant["max_exposure_cents"],
                ),
            ).rowcount
            != 1
        ):
            raise Conflict("Original reservation accounting fence changed.")
        from .experiments import _append_admitted_attempt_state

        _append_admitted_attempt_state(
            db,
            dict(attempt),
            "interrupted",
            actor["id"],
            reason="not_executed: human stop-only recovery of expired never-claimed worker",
        )
        evidence = {
            "grant_id": grant_id,
            "operation_id": proof["operation_id"],
            "dispatch_id": proof["dispatch_id"],
            "admitted_attempt_id": attempt["id"],
            "maintenance_authorization_id": auth["id"],
            "proof_digest": body["proof_digest"],
            "maintenance_actor_principal_id": actor["id"],
            "maintenance_actor_credential_id": actor["credential_id"],
            "human_issuer_principal_id": auth["human_issuer_principal_id"],
            "original_worker_principal_id": proof["original_worker_principal_id"],
            "original_worker_credential_id": proof["original_worker_credential_id"],
            "trust_boundary": TRUST,
            "outcome": "not_executed",
            "settlement_cents": 0,
            "provider_termination_inferred": False,
            "operational_only": True,
        }
        store._event(
            db,
            actor,
            grant["project_id"],
            grant["task_id"],
            ATTESTED,
            revision,
            evidence | {"attestation": proof},
        )
        store._event(
            db, actor, grant["project_id"], grant["task_id"], RECONCILED, revision, evidence
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

    return store._mutate(principal, key, "POST", path, digest, command)
