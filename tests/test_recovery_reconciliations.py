"""Default fake/API recovery closure tests for lost original worker identity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from test_execution import _grant_payload, _setup
from test_goal_to_decision import _admitted_operation_with_confirmed_sources

from tessera_control.api import create_app
from tessera_control.migrations import migrate_v43_to_v44
from tessera_control.store import _connect, _token_hash, canonical_digest, utc_now


def _owner_headers(state: Path, key: str | None = None) -> dict[str, str]:
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    headers = {"Authorization": f"Bearer {token}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _operation(state: Path) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    """Create only durable intent; this test helper never submits a provider job."""
    _state, execution, owner, worker, project, task = _setup(state.parent)
    with _connect(execution.database_path) as connection:
        snapshot_id = connection.execute("SELECT snapshot_id FROM snapshot_releases").fetchone()[0]
    grant_payload = _grant_payload(task, worker, {"id": snapshot_id})
    grant = execution.issue_execution_grant(
        owner, project["id"], "recovery-grant", canonical_digest(grant_payload), grant_payload
    )[1]
    lease = execution.acquire_execution_lease(
        worker, grant["id"], "recovery-lease", "recovery-lease"
    )[1]
    operation = {
        "operation_id": "a" * 32,
        "org_id": owner["org_id"],
        "project_id": project["id"],
        "task_id": task["id"],
        "grant_id": grant["id"],
        "lease_id": lease["id"],
        "worker_principal_id": worker["id"],
        "epoch": lease["epoch"],
        "package_digest": "b" * 64,
        "adapter_version": "fake-recovery-v1",
        "profile_version": "fake-recovery-profile-v1",
        "docker_lookup_key": "recovery-lookup-a",
        "request_digest": "c" * 64,
        "retry_class": "manual_reconciliation",
        "desired_state": "admitted",
        "observed_state": "unknown",
        "provider_id": "fake-recovery-job",
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    with _connect(execution.database_path) as connection:
        connection.execute(
            """INSERT INTO external_operations
            (operation_id,org_id,project_id,task_id,grant_id,lease_id,worker_principal_id,epoch,
             package_digest,adapter_version,profile_version,docker_lookup_key,request_digest,
             retry_class,desired_state,observed_state,provider_id,created_at,updated_at)
            VALUES (:operation_id,:org_id,:project_id,:task_id,:grant_id,:lease_id,
            :worker_principal_id,:epoch,:package_digest,:adapter_version,:profile_version,
            :docker_lookup_key,:request_digest,:retry_class,:desired_state,:observed_state,
            :provider_id,:created_at,:updated_at)""",
            operation,
        )
    return grant, lease, operation


def _retire(client: TestClient, state: Path) -> None:
    response = client.post(
        "/workers/registered-worker-token/retire",
        json={"expected_revision": 1},
        headers=_owner_headers(state, "retire-original-worker"),
    )
    assert response.status_code == 200


def _evidence(
    operation: dict[str, object],
    state: str,
    terminal: bool = False,
    *,
    observed_at: str | None = None,
    provider_id: str = "fake-recovery-job",
) -> dict[str, object]:
    payload = {"operation_id": operation["operation_id"], "state": state}
    return {
        "observed_state": state,
        "observed_at": observed_at or datetime.now(UTC).isoformat(),
        "provider_id": provider_id,
        "evidence_reference": "fake-provider-lookup-receipt",
        "evidence_digest": canonical_digest(payload),
        "termination_confirmed": terminal,
    }


def test_revoked_worker_terminal_evidence_closes_original_operation_idempotently(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    grant, lease, operation = _operation(state)
    client = TestClient(create_app(state))
    _retire(client, state)

    listed = client.get(
        f"/execution-grants/{grant['id']}/recovery-reconciliations", headers=_owner_headers(state)
    )
    assert listed.status_code == 200
    recovery = listed.json()["recovery_reconciliations"][0]
    assert recovery["state"] == "identity_lost"
    assert recovery["operation_id"] == operation["operation_id"]
    assert recovery["identity_loss_reason"] == "worker_retired"

    # Revocation rejects the old credential before it can renew or report.
    assert (
        client.post(
            "/worker/leases/renew",
            json={"lease_id": lease["id"], "expected_epoch": lease["epoch"]},
            headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "old-renew"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/worker/observations",
            json={
                "lease_id": lease["id"],
                "expected_epoch": lease["epoch"],
                "operation_id": operation["operation_id"],
                "observed_state": "exited",
                "observed_at": datetime.now(UTC).isoformat(),
                "provider_id": "fake-recovery-job",
                "termination_confirmed": True,
            },
            headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "old-report"},
        ).status_code
        == 401
    )

    body = {
        "expected_grant_revision": grant["revision"],
        "expected_recovery_revision": recovery["revision"],
        "operation_id": operation["operation_id"],
        **_evidence(operation, "exited", True),
    }
    first = client.post(
        f"/execution-grants/{grant['id']}/recovery-reconciliations",
        json=body,
        headers=_owner_headers(state, "terminal-recovery"),
    )
    replay = client.post(
        f"/execution-grants/{grant['id']}/recovery-reconciliations",
        json=body,
        headers=_owner_headers(state, "terminal-recovery"),
    )
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    result = first.json()["recovery"]
    assert result["state"] == "terminal_confirmed"
    assert result["evidence"][0]["evidence_reference"] == "fake-provider-lookup-receipt"
    assert "observer_principal_id" not in str(result)
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM external_operations").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM execution_leases").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM execution_recovery_reconciliations"
            ).fetchone()[0]
            == 1
        )

    reconciled = client.post(
        f"/execution-grants/{grant['id']}/reconcile",
        json={
            "expected_revision": grant["revision"],
            "settlement_cents": 0,
            "provider_terminated": True,
        },
        headers=_owner_headers(state, "settle-after-recovery"),
    )
    assert reconciled.status_code == 200


def test_unknown_provider_evidence_stays_unresolved_and_cancellation_is_not_terminal(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    grant, _lease, operation = _operation(state)
    client = TestClient(create_app(state))
    _retire(client, state)
    recovery = client.get(
        f"/execution-grants/{grant['id']}/recovery-reconciliations", headers=_owner_headers(state)
    ).json()["recovery_reconciliations"][0]
    cancellation = client.post(
        f"/execution-grants/{grant['id']}/recovery-reconciliations",
        json={
            "expected_grant_revision": grant["revision"],
            "expected_recovery_revision": recovery["revision"],
            "operation_id": operation["operation_id"],
            "request_cancellation": True,
        },
        headers=_owner_headers(state, "request-recovery-cancellation"),
    )
    assert cancellation.status_code == 201
    assert cancellation.json()["recovery"]["state"] == "cancel_requested"
    assert cancellation.json()["grant"]["status"] == "cancel_requested"
    response = client.post(
        f"/execution-grants/{grant['id']}/recovery-reconciliations",
        json={
            "expected_grant_revision": cancellation.json()["grant"]["revision"],
            "expected_recovery_revision": cancellation.json()["recovery"]["revision"],
            "operation_id": operation["operation_id"],
            **_evidence(operation, "unknown"),
        },
        headers=_owner_headers(state, "unknown-recovery"),
    )
    assert response.status_code == 201
    assert response.json()["recovery"]["state"] == "unresolved"
    assert response.json()["recovery"]["cancellation_requested"] is True
    assert response.json()["grant"]["status"] == "cancel_requested"
    assert (
        client.post(
            f"/execution-grants/{grant['id']}/reconcile",
            json={"expected_revision": 2, "settlement_cents": 0, "provider_terminated": True},
            headers=_owner_headers(state, "must-not-settle-unknown"),
        ).status_code
        == 409
    )


def test_recovery_evidence_requires_known_provider_and_current_reconciliation(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    grant, lease, operation = _operation(state)
    client = TestClient(create_app(state))
    _retire(client, state)
    recovery = client.get(
        f"/execution-grants/{grant['id']}/recovery-reconciliations", headers=_owner_headers(state)
    ).json()["recovery_reconciliations"][0]
    wrong_provider = client.post(
        f"/execution-grants/{grant['id']}/recovery-reconciliations",
        json={
            "expected_grant_revision": grant["revision"],
            "expected_recovery_revision": recovery["revision"],
            "operation_id": operation["operation_id"],
            **_evidence(operation, "exited", True, provider_id="wrong-provider"),
        },
        headers=_owner_headers(state, "wrong-recovery-provider"),
    )
    assert wrong_provider.status_code == 409
    assert "provider identity" in wrong_provider.json()["detail"]

    observed_at = datetime.now(UTC)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            """INSERT INTO execution_observations
            (id,grant_id,lease_id,worker_principal_id,epoch,operation_id,desired_state,
             observed_state,observed_at,provider_id,termination_confirmed,recorded_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "recovery-current-observation",
                grant["id"],
                lease["id"],
                operation["worker_principal_id"],
                lease["epoch"],
                operation["operation_id"],
                "admitted",
                "running",
                observed_at.isoformat(),
                operation["provider_id"],
                0,
                utc_now(),
            ),
        )
    backdated = client.post(
        f"/execution-grants/{grant['id']}/recovery-reconciliations",
        json={
            "expected_grant_revision": grant["revision"],
            "expected_recovery_revision": recovery["revision"],
            "operation_id": operation["operation_id"],
            **_evidence(operation, "running", observed_at=operation["created_at"]),
        },
        headers=_owner_headers(state, "backdated-recovery-evidence"),
    )
    assert backdated.status_code == 409
    assert "predates the current" in backdated.json()["detail"]


def test_equal_time_recovery_evidence_is_unresolved_and_later_correction_reopens(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    grant, lease, operation = _operation(state)
    observed_at = datetime.now(UTC)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            """INSERT INTO execution_observations
            (id,grant_id,lease_id,worker_principal_id,epoch,operation_id,desired_state,
             observed_state,observed_at,provider_id,termination_confirmed,recorded_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "recovery-equal-time-exit",
                grant["id"],
                lease["id"],
                operation["worker_principal_id"],
                lease["epoch"],
                operation["operation_id"],
                "admitted",
                "exited",
                observed_at.isoformat(),
                operation["provider_id"],
                1,
                utc_now(),
            ),
        )
    client = TestClient(create_app(state))
    _retire(client, state)
    recovery = client.get(
        f"/execution-grants/{grant['id']}/recovery-reconciliations", headers=_owner_headers(state)
    ).json()["recovery_reconciliations"][0]
    first = client.post(
        f"/execution-grants/{grant['id']}/recovery-reconciliations",
        json={
            "expected_grant_revision": grant["revision"],
            "expected_recovery_revision": recovery["revision"],
            "operation_id": operation["operation_id"],
            **_evidence(operation, "unknown", observed_at=observed_at.isoformat()),
        },
        headers=_owner_headers(state, "equal-time-recovery-evidence"),
    )
    assert first.status_code == 201
    assert first.json()["recovery"]["state"] == "unresolved"
    assert first.json()["grant"]["recovery_reconciliation"]["state"] == "unresolved"

    correction_at = (observed_at + timedelta(milliseconds=1)).isoformat()
    correction = client.post(
        f"/execution-grants/{grant['id']}/recovery-reconciliations",
        json={
            "expected_grant_revision": grant["revision"],
            "expected_recovery_revision": first.json()["recovery"]["revision"],
            "operation_id": operation["operation_id"],
            **_evidence(operation, "running", observed_at=correction_at),
        },
        headers=_owner_headers(state, "later-recovery-correction"),
    )
    assert correction.status_code == 201
    assert correction.json()["recovery"]["state"] == "independently_observed"
    with _connect(state / "tessera.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT observed_state FROM external_operations WHERE operation_id=?",
                (operation["operation_id"],),
            ).fetchone()[0]
            == "running"
        )


def test_recovery_reconciliation_projects_the_bound_admitted_attempt(tmp_path: Path) -> None:
    (
        state,
        _execution,
        records,
        owner,
        _worker,
        grant,
        _lease,
        admitted,
        operation_id,
        receipt,
        _worker_exit,
    ) = _admitted_operation_with_confirmed_sources(tmp_path)
    client = TestClient(create_app(state))
    _retire(client, state)
    recovery = client.get(
        f"/execution-grants/{grant['id']}/recovery-reconciliations", headers=_owner_headers(state)
    ).json()["recovery_reconciliations"][0]
    response = client.post(
        f"/execution-grants/{grant['id']}/recovery-reconciliations",
        json={
            "expected_grant_revision": grant["revision"],
            "expected_recovery_revision": recovery["revision"],
            "operation_id": operation_id,
            "observed_state": "unknown",
            "observed_at": receipt["observed_at"],
            "provider_id": receipt["provider_id"],
            "evidence_reference": "same-time-recovery-correction",
            "evidence_digest": canonical_digest({"operation_id": operation_id, "state": "unknown"}),
            "termination_confirmed": False,
        },
        headers=_owner_headers(state, "project-recovery-to-admission"),
    )
    assert response.status_code == 201
    assert response.json()["recovery"]["state"] == "unresolved"
    attempt = records.get_experiment(owner, admitted["experiment_id"])["admitted_attempts"][0]
    assert attempt["operational_state"] == attempt["operational_observed_state"] == "unknown"
    assert attempt["operational_observation_cursor"].startswith("reconciliation:")
    assert attempt["state_history"][-1]["state"] == "unknown"


def test_lost_journal_wrong_identity_and_foreign_tenant_are_fenced(tmp_path: Path) -> None:
    state = tmp_path / "state"
    grant, _lease, operation = _operation(state)
    client = TestClient(create_app(state))
    _retire(client, state)
    base = {
        "expected_grant_revision": grant["revision"],
        "expected_recovery_revision": 0,
        "operation_id": "d" * 32,
    }
    wrong = client.post(
        f"/execution-grants/{grant['id']}/recovery-reconciliations",
        json=base,
        headers=_owner_headers(state, "wrong-operation"),
    )
    assert wrong.status_code == 409

    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "INSERT INTO organizations (id,name,created_at) VALUES ('org_foreign','Foreign',?)",
            (utc_now(),),
        )
        connection.execute(
            """INSERT INTO principals (id,org_id,role,token_hash,revoked_at,created_at)
            VALUES ('foreign-owner','org_foreign','human',?,NULL,?)""",
            (_token_hash("foreign-owner-token"), utc_now()),
        )
        connection.execute(
            """INSERT INTO credentials
            (id,principal_id,kind,audience,project_id,parent_id,expires_at,revoked_at,created_at,
             created_by_principal_id)
            VALUES ('foreign-owner-credential','foreign-owner','local_owner','control',NULL,NULL,
            NULL,NULL,?,'foreign-owner')""",
            (utc_now(),),
        )
    foreign = client.get(
        f"/execution-grants/{grant['id']}/recovery-reconciliations",
        headers={"Authorization": "Bearer foreign-owner-token"},
    )
    assert foreign.status_code == 404

    # Removing the only journal row never manufactures an operation/recovery row.
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute("DELETE FROM execution_recovery_reconciliations")
        connection.execute(
            "DELETE FROM external_operations WHERE operation_id=?", (operation["operation_id"],)
        )
    lost = client.post(
        f"/execution-grants/{grant['id']}/recovery-reconciliations",
        json={
            "expected_grant_revision": grant["revision"],
            "expected_recovery_revision": 0,
            "operation_id": operation["operation_id"],
        },
        headers=_owner_headers(state, "lost-journal"),
    )
    assert lost.status_code == 409
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM external_operations").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM execution_recovery_reconciliations"
            ).fetchone()[0]
            == 0
        )


def test_v43_to_v44_adds_empty_recovery_schema_without_inventing_loss(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _grant, _lease, operation = _operation(state)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TABLE execution_recovery_observations")
        connection.execute("DROP TABLE execution_recovery_reconciliations")
        connection.execute("UPDATE schema_meta SET schema_version=43")
        migrate_v43_to_v44(connection)
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 44
        assert (
            connection.execute(
                "SELECT count(*) FROM execution_recovery_reconciliations"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT operation_id FROM external_operations WHERE operation_id=?",
                (operation["operation_id"],),
            ).fetchone()[0]
            == operation["operation_id"]
        )
