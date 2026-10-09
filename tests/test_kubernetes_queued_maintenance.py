"""Synthetic provider reads, actual typed control transactions; no live effects."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from test_kubernetes_dispatch import _owner_headers
from test_kubernetes_queued_recovery import _attest, _fixture, _proof

from tessera_control.store import canonical_digest, initialize_state
from tessera_worker.kubernetes_maintenance_recovery import collect_maintenance_proof
from tessera_worker.kubernetes_remote_service import RemoteTargetError


def _setup(tmp_path, *, expire=True, claim=False):
    client, state, cluster, package, root, adapter, grant, worker, binding = _fixture(
        tmp_path, claim=claim
    )
    proof = _proof(root, binding, cluster)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        credential = db.execute(
            "SELECT id FROM credentials WHERE principal_id=?", (grant["worker_principal_id"],)
        ).fetchone()[0]
        if expire:
            db.execute(
                "UPDATE credentials SET expires_at=? WHERE id=?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), credential),
            )
    body = {
        field: proof[field]
        for field in (
            "expected_revision",
            "grant_epoch",
            "dispatch_id",
            "operation_id",
            "lease_id",
            "epoch",
            "package_digest",
            "target_digest",
            "actual_target_digest",
            "target_relation",
            "namespace_uid",
            "job_name",
        )
    } | {
        "original_worker_principal_id": grant["worker_principal_id"],
        "original_worker_credential_id": credential,
        "namespace": cluster.namespace,
        "cluster_uid": cluster.cluster_uid,
        "custody_reference": "synthetic-stopped-deployment-uid",
        "custody_digest": "c" * 64,
        "provider_read_authorization_reference": "synthetic-rolebinding-uid",
        "human_attests_original_worker_stopped_exclusive_custody": True,
        "human_attests_existing_provider_read_authorization": True,
        "purpose": "stop-only-never-claimed",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=4)).isoformat(),
    }
    return client, state, cluster, package, root, adapter, grant, worker, binding, body


def _issue(client, state, grant, body, key="maintenance"):
    return client.post(
        f"/execution-grants/{grant['id']}/never-claimed-maintenance",
        json=body,
        headers=_owner_headers(state) | {"Idempotency-Key": key},
    )


def _collect(root, envelope, cluster, **overrides):
    return collect_maintenance_proof(
        root,
        envelope,
        cluster,
        exclusive_stopped_worker=True,
        **(
            {
                "inspect_namespace": lambda name: {
                    "metadata": {"name": name, "uid": cluster.namespace_uid}
                },
                "inspect_job": lambda _: None,
                "list_operation_jobs": lambda _: [],
                "list_operation_pods": lambda _: [],
                "list_helper_pods": lambda _: [],
            }
            | overrides
        ),
    )


def _release(client, state, grant, payload, key="release-maintenance"):
    return client.post(
        f"/execution-grants/{grant['id']}/reconcile-maintenance-never-claimed",
        json=payload,
        headers=_owner_headers(state) | {"Idempotency-Key": key},
    )


def test_expired_worker_stays_401_human_actual_actor_release_replay_and_no_new_authority(tmp_path):
    client, state, cluster, package, root, adapter, grant, worker, binding, body = _setup(tmp_path)
    assert _attest(client, grant, worker, _proof(root, binding, cluster)).status_code == 401
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        before = {
            table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "credentials",
                "execution_leases",
                "external_operations",
                "execution_runtime_packages",
                "kubernetes_dispatches",
            )
        }
    issued = _issue(client, state, grant, body)
    assert issued.status_code == 201, issued.text
    assert _issue(client, state, grant, body).json() == issued.json()
    assert _issue(client, state, grant, body, "second-key").status_code == 409
    envelope = issued.json()
    auth = envelope["authorization"]
    assert all(
        auth[field] is False
        for field in (
            "execute_allowed",
            "renew_allowed",
            "dispatch_allowed",
            "provider_permission_issued",
        )
    )
    assert (
        client.post(
            f"/worker/kubernetes-dispatches/{binding['dispatch_id']}/claim",
            json={"target_digest": binding["target_digest"], "profile_version": cluster.profile},
            headers={"Authorization": "Bearer " + auth["id"], "Idempotency-Key": "not-a-bearer"},
        ).status_code
        == 401
    )
    payload = _collect(root, envelope, cluster)
    released = _release(client, state, grant, payload)
    assert released.status_code == 200, released.text
    assert released.json()["status"] == "reconciled"
    assert released.json()["cancellation"]["state"] != "confirmed_termination"
    assert _release(client, state, grant, payload).json() == released.json()
    assert _release(client, state, grant, payload, "second-release").status_code == 409
    assert (
        _attest(client, grant, worker, _proof(root, binding, cluster), "still-expired").status_code
        == 401
    )
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        for table, count in before.items():
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == count
        events = db.execute(
            "SELECT actor_principal_id,payload_json FROM events WHERE event_type IN "
            "('execution.human_maintenance_never_claimed_attested','execution.human_maintenance_never_claimed_reconciled')"
        ).fetchall()
        assert len(events) == 2
        for actual_actor, value in events:
            evidence = json.loads(value)
            assert (
                actual_actor
                == auth["human_issuer_principal_id"]
                == auth["maintenance_actor_principal_id"]
            )
            assert (
                evidence["maintenance_actor_credential_id"]
                == auth["maintenance_actor_credential_id"]
            )
            assert (
                evidence["original_worker_credential_id"] == body["original_worker_credential_id"]
            )
            assert evidence["provider_termination_inferred"] is False
            assert evidence["outcome"] == "not_executed"
        assert db.execute(
            "SELECT held_cents,settled_cents FROM execution_project_limits"
        ).fetchone() == (0, 0)
    with pytest.raises(sqlite3.IntegrityError, match="fenced"):
        adapter._bind(package)


@pytest.mark.parametrize(
    "field,value",
    [
        ("original_worker_principal_id", "foreign-worker"),
        ("original_worker_credential_id", "foreign-credential"),
        ("expected_revision", 99),
        ("grant_epoch", 99),
        ("epoch", 99),
        ("package_digest", "a" * 64),
        ("target_digest", "a" * 64),
        ("human_attests_original_worker_stopped_exclusive_custody", False),
        ("human_attests_existing_provider_read_authorization", False),
        ("purpose", "execute"),
    ],
)
def test_maintenance_identity_and_scope_denials(tmp_path, field, value):
    client, state, _, _, _, _, grant, _, _, body = _setup(tmp_path)
    denied = _issue(client, state, grant, body | {field: value})
    assert denied.status_code in (409, 422), denied.text


def test_active_original_worker_and_tenant_and_worker_issuer_denied(tmp_path):
    client, state, _, _, _, _, grant, worker, _, body = _setup(tmp_path, expire=False)
    assert _issue(client, state, grant, body).status_code == 409
    other = tmp_path / "other"
    initialize_state(other)
    assert _issue(client, other, grant, body).status_code in (401, 403, 404)
    response = client.post(
        f"/execution-grants/{grant['id']}/never-claimed-maintenance",
        json=body,
        headers=worker | {"Idempotency-Key": "worker-self-maintenance"},
    )
    assert response.status_code in (401, 403)


@pytest.mark.parametrize(
    "field,value",
    [
        ("proof_digest", "a" * 64),
        ("maintenance_authorization_digest", "a" * 64),
        ("maintenance_actor_principal_id", "foreign-human"),
        ("maintenance_actor_credential_id", "foreign-session"),
        ("human_issuer_principal_id", "foreign-human"),
        ("original_worker_credential_id", "foreign-credential"),
        ("custody_digest", "f" * 64),
        ("actual_target_digest", "f" * 64),
        ("provider_read_authorization_reference", "foreign-role"),
    ],
)
def test_proof_digest_actor_custody_and_target_tampering_held(tmp_path, field, value):
    client, state, cluster, _, root, _, grant, _, _, body = _setup(tmp_path)
    issued = _issue(client, state, grant, body)
    assert issued.status_code == 201, issued.text
    payload = _collect(root, issued.json(), cluster)
    if field == "proof_digest":
        payload[field] = value
    else:
        payload["proof"][field] = value
        payload["proof_digest"] = canonical_digest(payload["proof"])
    assert _release(client, state, grant, payload).status_code == 409
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT status FROM execution_grants").fetchone()[0] == "cancel_requested"


def test_provider403_never_becomes_absence_or_maintenance_permission(tmp_path):
    client, state, cluster, _, root, _, grant, _, _, body = _setup(tmp_path)
    issued = _issue(client, state, grant, body)
    assert issued.status_code == 201

    def denied(_):
        raise RemoteTargetError("Kubernetes GET rejected", http_status=403)

    with pytest.raises(RemoteTargetError) as failure:
        _collect(root, issued.json(), cluster, list_operation_jobs=denied)
    assert failure.value.http_status == 403
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT status FROM execution_grants").fetchone()[0] == "cancel_requested"
        assert (
            db.execute(
                "SELECT count(*) FROM events WHERE "
                "event_type='execution.human_maintenance_never_claimed_attested'"
            ).fetchone()[0]
            == 0
        )


def test_maintenance_expiry_and_proof_before_authorization_denied(tmp_path):
    client, state, cluster, _, root, _, grant, _, _, body = _setup(tmp_path)
    expired = body | {"expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
    assert _issue(client, state, grant, expired, "expired").status_code == 409
    long = body | {"expires_at": (datetime.now(UTC) + timedelta(minutes=6)).isoformat()}
    assert _issue(client, state, grant, long, "long").status_code == 409
    issued = _issue(client, state, grant, body)
    payload = _collect(root, issued.json(), cluster)
    old = payload["proof"] | {
        "observed_at": (datetime.now(UTC) - timedelta(seconds=10))
        .isoformat()
        .replace("+00:00", "Z")
    }
    assert (
        _release(
            client, state, grant, {"proof": old, "proof_digest": canonical_digest(old)}, "oldproof"
        ).status_code
        == 409
    )
    client.app.state.execution._now = lambda: datetime.now(UTC) + timedelta(minutes=6)
    assert _release(client, state, grant, payload).status_code == 409
    assert _issue(client, state, grant, body).status_code == 409


def test_competing_human_release_race_once_and_atomicity(tmp_path):
    client, state, cluster, _, root, _, grant, _, _, body = _setup(tmp_path)
    issued = _issue(client, state, grant, body)
    payload = _collect(root, issued.json(), cluster)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute(
            "CREATE TRIGGER fail_release BEFORE UPDATE ON execution_project_limits "
            "BEGIN SELECT RAISE(ABORT, 'synthetic accounting failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        _release(client, state, grant, payload)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT status FROM execution_grants").fetchone()[0] == "cancel_requested"
        assert (
            db.execute(
                "SELECT count(*) FROM events WHERE "
                "event_type='execution.human_maintenance_never_claimed_attested'"
            ).fetchone()[0]
            == 0
        )
        db.execute("DROP TRIGGER fail_release")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda key: _release(client, state, grant, payload, key).status_code,
                ("race-a", "race-b"),
            )
        )
    assert sorted(results) == [200, 409]


def test_unknown_evidence_after_authorization_blocks_human_release(tmp_path):
    client, state, cluster, package, root, _, grant, worker, _, body = _setup(tmp_path)
    issued = _issue(client, state, grant, body)
    payload = _collect(root, issued.json(), cluster)
    # Synthetic corruption models contradictory late provenance; it must never be discarded.
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute("UPDATE external_operations SET provider_id='possibly-submitted-job'")
    assert _release(client, state, grant, payload).status_code == 409
    assert _attest(client, grant, worker, payload["proof"]).status_code == 401


def test_foreign_local_target_and_missing_custody_denied_before_reads(tmp_path):
    client, state, cluster, _, root, _, grant, _, _, body = _setup(tmp_path)
    issued = _issue(client, state, grant, body)
    envelope = issued.json()
    auth = envelope["authorization"] | {"actual_target_digest": "f" * 64}
    modified = {"authorization": auth, "authorization_digest": canonical_digest(auth)}

    def must_not_read(_):
        pytest.fail("foreign-target read must not occur")

    with pytest.raises(ValueError, match="target"):
        _collect(root, modified, cluster, inspect_namespace=must_not_read)
    with pytest.raises(ValueError, match="stopped"):
        collect_maintenance_proof(root, envelope, cluster, exclusive_stopped_worker=False)


def test_claim_history_cannot_be_maintained_after_expiry(tmp_path):
    client, state, _, _, _, _, grant, _, _, body = _setup(tmp_path, claim=True)
    assert _issue(client, state, grant, body).status_code == 409


def test_maintenance_human_export_joins_actual_actor_not_expired_worker(tmp_path):
    client, state, cluster, _, root, _, grant, _, _, body = _setup(tmp_path)
    issued = _issue(client, state, grant, body)
    payload = _collect(root, issued.json(), cluster)
    assert _release(client, state, grant, payload).status_code == 200
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        experiment_id = db.execute(
            "SELECT experiment_id FROM research_experiment_admitted_attempts WHERE grant_id=?",
            (grant["id"],),
        ).fetchone()[0]
    exported = client.get(f"/experiments/{experiment_id}/export", headers=_owner_headers(state))
    assert exported.status_code == 200
    notebook = exported.json()["agent_notebook"]
    events = {event["event_type"]: event for event in notebook["control_events"]}
    for kind in (
        "execution.never_claimed_maintenance_issued",
        "execution.human_maintenance_never_claimed_attested",
        "execution.human_maintenance_never_claimed_reconciled",
    ):
        assert events[kind]["actor_principal_id"] == grant["created_by_principal_id"]
        assert events[kind]["references"]["operation_id"] == body["operation_id"]
        assert events[kind]["payload_projection"] == "opaque_references_only"
        assert "attestation" not in events[kind]
    assert notebook["execution_verification"] == "not_established_by_provenance"


def test_same_tenant_nonissuer_and_foreign_credential_consumption_denied(tmp_path):
    from tessera_control.store import Store, _token_hash

    client, state, cluster, _, root, _, grant, _, _, body = _setup(tmp_path)
    token = "synthetic-other-human"
    store = Store(state)
    owner = store.authenticate(_owner_headers(state)["Authorization"].removeprefix("Bearer "))
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute(
            "INSERT INTO principals (id,org_id,role,token_hash,revoked_at,created_at) "
            "VALUES ('other-human',?,'human',?,NULL,?)",
            (owner["org_id"], _token_hash(token), now),
        )
        db.execute(
            "INSERT INTO credentials "
            "(id,principal_id,kind,audience,project_id,parent_id,expires_at,revoked_at,"
            "created_at,created_by_principal_id) "
            "VALUES ('other-session','other-human','human_session','control',?,?,?,NULL,?,?)",
            (
                grant["project_id"],
                owner["credential_id"],
                (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
                now,
                owner["id"],
            ),
        )
    path = f"/execution-grants/{grant['id']}"
    headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "foreign-human"}
    assert (
        client.post(path + "/never-claimed-maintenance", json=body, headers=headers).status_code
        == 403
    )
    issued = _issue(client, state, grant, body)
    payload = _collect(root, issued.json(), cluster)
    assert (
        client.post(
            path + "/reconcile-maintenance-never-claimed", json=payload, headers=headers
        ).status_code
        == 403
    )


def test_concurrent_custody_authorization_issues_only_one_stop_only_record(tmp_path):
    client, state, _, _, _, _, grant, _, _, body = _setup(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda key: _issue(client, state, grant, body, key).status_code,
                ("issue-a", "issue-b"),
            )
        )
    assert sorted(results) == [201, 409]
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert (
            db.execute(
                "SELECT count(*) FROM events WHERE "
                "event_type='execution.never_claimed_maintenance_issued'"
            ).fetchone()[0]
            == 1
        )
