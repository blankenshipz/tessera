"""Synthetic HTTP authority lifecycle; this test never submits a provider job."""

from datetime import UTC, datetime, timedelta
from itertools import count

from fastapi.testclient import TestClient

from tessera_control.api import create_app
from tessera_control.snapshots import SnapshotStore
from tessera_control.store import initialize_state
from tessera_control.workspace import capture_workspace, inventory_digest


def test_admission_cancellation_and_retained_exposure_through_http(tmp_path):
    state, source = tmp_path / "state", tmp_path / "source"
    source.mkdir()
    (source / "input.txt").write_text("Synthetic public fixture")
    initialize_state(state)
    owner = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    client = TestClient(create_app(state))
    sequence = count()

    def call(method, path, body, expected, headers=None, key=None):
        response = client.request(
            method,
            path,
            json=body,
            headers=(headers or owner) | {"Idempotency-Key": key or f"http-{next(sequence)}"},
        )
        assert response.status_code == expected, response.text
        return response.json()

    project = call("POST", "/projects", {"name": "Synthetic authority"}, 201)
    capture = capture_workspace(source)
    snapshots = SnapshotStore(state)
    actor = snapshots.authenticate(owner["Authorization"].removeprefix("Bearer "))
    _, snapshot = snapshots.adopt_snapshot(
        actor, project["id"], "input", inventory_digest(capture.inventory), capture
    )
    worker_token = "synthetic-worker-" + "x" * 48
    worker = {"Authorization": f"Bearer {worker_token}"}
    credential = call(
        "POST",
        "/credentials",
        {
            "kind": "worker",
            "audience": "worker",
            "token": worker_token,
            "project_id": project["id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
        201,
    )
    task = call(
        "POST",
        f"/projects/{project['id']}/tasks",
        {
            "title": "Record a bounded CPU intent",
            "purpose": "Synthetic fixture only",
            "budget_cents": 10,
            "max_duration_seconds": 120,
        },
        201,
    )
    plan_body = {
        "expected_revision": task["revision"],
        "title": task["title"],
        "purpose": task["purpose"],
        "budget_cents": 10,
        "max_duration_seconds": 120,
        "execution_spec": {
            "argv": ["python", "-c", "print('fixture')"],
            "input_snapshot_digest": snapshot["digest"],
            "pinned_skill_digests": [],
            "output_paths": [],
            "profile": "local-cpu",
            "network_enabled": False,
            "max_output_bytes": 1024,
        },
    }
    plan = call("PUT", f"/tasks/{task['id']}/plan", plan_body, 200)
    approved = call(
        "POST",
        f"/tasks/{task['id']}/approve",
        {
            "expected_revision": plan["revision"],
            "expected_plan_digest": plan["plan_digest"],
        },
        200,
    )
    call(
        "PUT",
        f"/projects/{project['id']}/execution-limits",
        {
            "expected_revision": 0,
            "currency": "USD",
            "monetary_limit_cents": 10,
            "max_concurrency": 1,
        },
        201,
    )
    grant_body = {
        "task_id": task["id"],
        "expected_task_revision": approved["revision"],
        "expected_plan_digest": approved["plan_digest"],
        "worker_principal_id": credential["principal_id"],
        "snapshot_id": snapshot["id"],
        "pinned_image": "python@sha256:" + "a" * 64,
        "currency": "USD",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        "max_exposure_cents": 10,
        "acknowledges_unmeasured_infrastructure": True,
    }
    call("POST", f"/projects/{project['id']}/execution-grants", grant_body, 403)
    call(
        "POST",
        f"/snapshots/{snapshot['id']}/releases",
        {
            "expected_digest": snapshot["digest"],
            "worker_principal_id": credential["principal_id"],
            "classification": "public",
            "permits_worker_transfer": True,
            "rationale": "Explicit synthetic public-data review",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
        },
        201,
    )
    grant = call("POST", f"/projects/{project['id']}/execution-grants", grant_body, 201)
    assert grant["execution_dispatched"] is False
    lease = call("POST", "/worker/leases", {"grant_id": grant["id"]}, 201, worker, "lease-once")
    assert (
        call("POST", "/worker/leases", {"grant_id": grant["id"]}, 201, worker, "lease-once")
        == lease
    )
    call("POST", "/worker/leases", {"grant_id": grant["id"]}, 401)
    call(
        "PUT",
        f"/tasks/{task['id']}/plan",
        plan_body | {"expected_revision": approved["revision"]},
        409,
    )
    revoked = call("POST", f"/execution-grants/{grant['id']}/revoke", {"expected_revision": 1}, 200)
    assert revoked["status"] == "cancel_requested"
    call("POST", "/worker/leases", {"grant_id": grant["id"]}, 409, worker, "lease-once")
    call(
        "POST",
        "/worker/leases/renew",
        {"lease_id": lease["id"], "expected_epoch": lease["epoch"]},
        409,
        worker,
    )
    settlement = {
        "expected_revision": revoked["revision"],
        "settlement_cents": 15,
        "provider_terminated": True,
    }
    call("POST", f"/execution-grants/{grant['id']}/reconcile", settlement, 409)
    for observed_state, confirmed in (("unknown", False), ("exited", True)):
        call(
            "POST",
            "/worker/observations",
            {
                "lease_id": lease["id"],
                "expected_epoch": lease["epoch"],
                "observed_state": observed_state,
                "termination_confirmed": confirmed,
                "observed_at": datetime.now(UTC).isoformat(),
                "provider_id": "synthetic-no-job-was-run",
            },
            201,
            worker,
        )
        if not confirmed:
            call("POST", f"/execution-grants/{grant['id']}/reconcile", settlement, 409)
    reconciled = call("POST", f"/execution-grants/{grant['id']}/reconcile", settlement, 200)
    assert reconciled["status"] == "reconciled"
    limits = client.get(f"/projects/{project['id']}/execution-limits", headers=owner).json()
    assert limits["held_cents"] == 0 and limits["settled_cents"] == 15
    assert limits["settled_cents"] > limits["monetary_limit_cents"]
    assert client.get(f"/tasks/{task['id']}", headers=owner).json()["status"] == "approved"
