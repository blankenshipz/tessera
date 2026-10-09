"""Stale local-pilot authority remains auditable and fails closed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from test_execution import _grant_payload, _setup

from tessera_control.api import create_app
from tessera_control.store import _connect, canonical_digest


def _owner_headers(state: Path, key: str) -> dict[str, str]:
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}


def _grant(tmp_path: Path) -> tuple[Path, TestClient, dict[str, object], dict[str, object]]:
    state, execution, owner, worker, project, task = _setup(tmp_path)
    with _connect(execution.database_path) as connection:
        snapshot_id = connection.execute("SELECT snapshot_id FROM snapshot_releases").fetchone()[0]
    payload = _grant_payload(task, worker, {"id": snapshot_id})
    payload["max_exposure_cents"] = 100
    grant = execution.issue_execution_grant(
        owner, project["id"], "stale-authority-grant", canonical_digest(payload), payload
    )[1]
    return state, TestClient(create_app(state)), project, grant


def test_stale_worker_retirement_revokes_local_identity_without_clearing_authority(
    tmp_path: Path,
) -> None:
    state, client, project, grant = _grant(tmp_path)
    fresh = client.post(
        "/workers/registered-worker-token/retire-stale",
        json={"expected_revision": 1},
        headers=_owner_headers(state, "fresh-retirement-must-fail"),
    )
    assert fresh.status_code == 409
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE worker_registry SET last_seen_at=? WHERE id='registered-worker-token'",
            ((datetime.now(UTC) - timedelta(minutes=2)).isoformat(),),
        )
    before = client.get("/operator/health", headers=_owner_headers(state, "ignored")).json()
    assert before["workers"]["stale_count"] == 1
    assert before["resource_budgets"][0]["status"] == "reserved_unattempted_authority"
    retired = client.post(
        "/workers/registered-worker-token/retire-stale",
        json={"expected_revision": 1},
        headers=_owner_headers(state, "retire-stale-worker"),
    )
    assert retired.status_code == 200
    assert retired.json()["lifecycle_state"] == "retired"
    after = client.get("/operator/health", headers=_owner_headers(state, "ignored")).json()
    assert after["workers"]["stale_count"] == 0
    # Retirement fences identity only. The expired/unattempted reconciliation
    # command remains the separate, audited budget-release decision.
    budget = after["resource_budgets"][0]
    assert budget["authority_admission"] == "blocked"
    assert budget["remaining_cents"] == 0
    assert budget["unattempted_reserved_cents"] == 100
    with _connect(state / "tessera.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT status FROM execution_grants WHERE id=?", (grant["id"],)
            ).fetchone()[0]
            == "active"
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM events WHERE event_type='worker.retired'"
            ).fetchone()[0]
            == 1
        )


def test_only_expired_zero_attempt_grant_releases_its_reservation_with_history(
    tmp_path: Path,
) -> None:
    state, client, project, grant = _grant(tmp_path)
    blocked = client.post(
        f"/execution-grants/{grant['id']}/reconcile-expired-unattempted",
        json={"expected_revision": grant["revision"]},
        headers=_owner_headers(state, "unexpired-must-not-clear"),
    )
    assert blocked.status_code == 409
    generic = client.post(
        f"/execution-grants/{grant['id']}/reconcile",
        json={
            "expected_revision": grant["revision"],
            "settlement_cents": 0,
            "provider_terminated": True,
        },
        headers=_owner_headers(state, "generic-must-not-clear-unattempted"),
    )
    assert generic.status_code == 409
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE execution_grants SET expires_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), grant["id"]),
        )
    held = client.get("/operator/health", headers=_owner_headers(state, "ignored")).json()
    budget = held["resource_budgets"][0]
    assert budget["status"] == "reserved_unattempted_authority"
    assert budget["authority_admission"] == "blocked"
    assert {alert["code"] for alert in held["alerts"]} >= {
        "resource_budget_unattempted_authority_reserved"
    }
    reconciled = client.post(
        f"/execution-grants/{grant['id']}/reconcile-expired-unattempted",
        json={"expected_revision": grant["revision"]},
        headers=_owner_headers(state, "reconcile-expired-zero-attempt"),
    )
    assert reconciled.status_code == 200
    assert reconciled.json()["status"] == "reconciled"
    assert reconciled.json()["settlement_cents"] == 0
    clear = client.get("/operator/health", headers=_owner_headers(state, "ignored")).json()
    assert clear["resource_budgets"] == [
        {
            "project_id": project["id"],
            "currency": "USD",
            "monetary_limit_cents": 100,
            "held_cents": 0,
            "settled_cents": 0,
            "unattempted_reserved_cents": 0,
            "attempted_reserved_cents": 0,
            "unattributed_held_cents": 0,
            "remaining_cents": 100,
            "authority_admission": "available",
            "max_concurrency": 1,
            "active_or_cancelling_grants": 0,
            "status": "within_limit",
        }
    ]
    with _connect(state / "tessera.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM execution_grants WHERE id=?", (grant["id"],)
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM execution_leases WHERE grant_id=?", (grant["id"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM external_operations WHERE grant_id=?", (grant["id"],)
            ).fetchone()[0]
            == 0
        )
        event = connection.execute(
            "SELECT payload_json FROM events "
            "WHERE event_type='execution.expired_unattempted_grant_reconciled'"
        ).fetchone()[0]
    assert '"provider_termination_inferred":false' in event


def test_expired_grant_with_a_lease_cannot_use_zero_attempt_clearance(tmp_path: Path) -> None:
    state, client, _project, grant = _grant(tmp_path)
    lease = client.post(
        "/worker/leases",
        json={"grant_id": grant["id"]},
        headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "lease-before-expiry"},
    )
    assert lease.status_code == 201
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE execution_grants SET expires_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), grant["id"]),
        )
    rejected = client.post(
        f"/execution-grants/{grant['id']}/reconcile-expired-unattempted",
        json={"expected_revision": grant["revision"]},
        headers=_owner_headers(state, "leased-authority-must-not-clear"),
    )
    assert rejected.status_code == 409
    health = client.get("/operator/health", headers=_owner_headers(state, "ignored")).json()
    budget = health["resource_budgets"][0]
    assert budget["status"] == "exhausted"
    assert budget["attempted_reserved_cents"] == 100
    assert budget["remaining_cents"] == 0
