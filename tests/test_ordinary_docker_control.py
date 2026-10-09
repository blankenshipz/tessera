"""Real temporary control transactions; Docker/container evidence is synthetic."""

import base64
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_agent_authority import (
    admit_payload,
    assert_empty_runtime_v55,
    enroll,
    post,
    strip_runtime_v55,
)
from test_execution import _grant_payload, _owner, _snapshot_release, _worker
from test_goal_to_decision import _design

from tessera_control.api import create_app
from tessera_control.recovery import backup_state, restore_state
from tessera_control.store import Store, canonical_digest, initialize_state, migrate_state
from tessera_worker.ordinary_docker_contract import DockerLease, DockerPackage, DockerTarget


@pytest.fixture
def docker(tmp_path, request):
    state = tmp_path / "state"
    initialize_state(state)
    store = Store(state)
    human = _owner(state)
    owner = {"Authorization": (state / "owner.header").read_text().split(": ", 1)[1].strip()}
    project = store.create_project(human, "project", "project", {"name": "Docker"})[1]["id"]
    worker = _worker(state, human, project, "worker-token")
    auth = {"Authorization": "Bearer worker-token"}
    snapshot, _ = _snapshot_release(state, human, project, worker["id"])
    client = TestClient(create_app(state))
    image = "example/guard@sha256:" + "a" * 64
    resources = {
        "cpu_millicores": 100,
        "memory_bytes": 67108864,
        "pids_limit": 16,
        "scratch_bytes": 1048576,
    }
    target = post(
        client,
        owner,
        f"/projects/{project}/docker-targets",
        {
            "worker_principal_id": worker["id"],
            "target": {"daemon_id": "test-daemon", "endpoint_digest": "b" * 64, "image": image},
            "resources": resources,
            "max_output_bytes": 1024,
            "network": "none",
            "placement": "independent_execution_host",
            "isolation_approved": True,
            "expires_at": (datetime.now(UTC) + timedelta(hours=3)).isoformat(),
        },
    )
    task = post(
        client,
        owner,
        f"/projects/{project}/tasks",
        {"title": "Docker", "purpose": "bounded", "budget_cents": 100, "max_duration_seconds": 300},
    )
    plan = {
        "expected_revision": task["revision"],
        "title": "Docker",
        "purpose": "bounded",
        "budget_cents": 100,
        "max_duration_seconds": 300,
        "execution_spec": {
            "profile": "ordinary-docker",
            "argv": ["python", "experiment.py"],
            "input_snapshot_digest": snapshot["digest"],
            "pinned_skill_digests": [],
            "output_paths": ["result.txt"],
            "network_enabled": False,
            "max_output_bytes": 1024,
            "docker": resources
            | {"target_id": target["id"], "target_digest": target["target_digest"]},
        },
    }
    task = store.revise_task_plan(human, task["id"], "plan", canonical_digest(plan), plan)[1]
    task = store.approve_task(
        human, task["id"], "approve", "approve", task["revision"], task["current_plan_digest"]
    )[1]
    limits = client.put(
        f"/projects/{project}/execution-limits",
        headers=owner | {"Idempotency-Key": "limits"},
        json={
            "expected_revision": 0,
            "currency": "USD",
            "monetary_limit_cents": 100,
            "max_concurrency": 1,
        },
    )
    assert limits.status_code == 201, limits.text
    payload = _grant_payload(task, worker, {"id": snapshot["id"]})
    payload.update(pinned_image=image, max_exposure_cents=100)
    grant = post(client, owner, f"/projects/{project}/execution-grants", payload)
    lease = post(client, auth, "/worker/leases", {"grant_id": grant["id"]})
    grant = client.get(f"/execution-grants/{grant['id']}", headers=owner).json()
    experiment = post(client, owner, f"/projects/{project}/experiments", _design(grant=grant))
    variant = next(v for v in experiment["design"]["variants"] if v["kind"] == "baseline")
    agent, credential = enroll(client, owner, project)
    bounds = {
        "subject_credential_id": credential["credential_id"],
        "destination": "openai:personal",
        "model": "model-1",
        "purpose": "Docker",
        "actions": ["attempt:admit", "attempt:dispatch", "attempt:status", "execution:cancel"],
        "bindings": [
            {
                "variant_id": variant["id"],
                "expected_variant_revision": variant["revision"],
                "expected_design_revision": experiment["current_design_revision"],
                "execution_grant_id": grant["id"],
                "expected_execution_grant_revision": grant["revision"],
                "expected_execution_epoch": grant["epoch"],
                "task_id": task["id"],
                "expected_task_revision": task["revision"],
                "plan_digest": task["current_plan_digest"],
                "target_digest": target["target_digest"],
            }
        ],
        "budget_cents": 100,
        "max_duration_seconds": 300,
        "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        "delegation_depth": 0,
    }
    authority = post(
        client, owner, f"/projects/{project}/agent-authority-grants", {"bounds": bounds}
    )
    consumed = post(
        client, agent, f"/agent-authority-grants/{authority['id']}/consume", admit_payload(bounds)
    )
    attempt = consumed["result"]
    package = post(
        client,
        auth,
        f"/worker/docker/leases/{lease['id']}/package",
        {"expected_epoch": lease["epoch"], "admitted_attempt_id": attempt["id"]},
        key="package",
    )
    exported = client.get(f"/experiments/{experiment['id']}", headers=owner).json()
    attempt = next(item for item in exported["admitted_attempts"] if item["id"] == attempt["id"])
    if getattr(request, "param", True):
        post(
            client,
            agent,
            f"/agent-authority-grants/{authority['id']}/consume",
            {
                "expected_revision": 1,
                "expected_epoch": 1,
                "model": "model-1",
                "command": {
                    "action": "attempt:dispatch",
                    "attempt_id": attempt["id"],
                    "expected_revision": attempt["revision"],
                    "target_digest": target["target_digest"],
                    "profile_version": "ordinary-docker-v1",
                },
            },
            key="dispatch",
        )
    pins = {
        "operation_id": package["package"]["operation_id"],
        "admitted_attempt_id": attempt["id"],
        "lease_id": lease["id"],
        "expected_epoch": lease["epoch"],
        "package_digest": package["package_digest"],
        "target_digest": target["target_digest"],
    }
    return (
        state,
        client,
        owner,
        auth,
        agent,
        target,
        grant,
        lease,
        attempt,
        package,
        pins,
        authority,
    )


def test_native_package_claim_current_report_reconcile(docker):
    state, client, owner, worker, agent, target, grant, lease, attempt, package, pins, authority = (
        docker
    )
    native = DockerPackage.model_validate_json(json.dumps(package["package"]))
    native.envelope(DockerTarget.model_validate(target["bounds"]["target"]))
    assert native.package_digest == package["package_digest"]
    assert "kubernetes_workload" not in package["package"]
    pending = client.get(
        "/worker/docker/dispatches", headers=worker, params={"target_digest": pins["target_digest"]}
    )
    assert pending.status_code == 200 and pending.json()["dispatches"][0]["fence"] == pins
    claim = post(client, worker, "/worker/docker/claim", pins, key="claim", expected=200)
    DockerLease.model_validate_json(json.dumps(claim["lease"]))
    assert post(client, worker, "/worker/docker/claim", pins, key="claim", expected=200) == claim
    assert (
        client.post("/worker/docker/current-authority", headers=worker, json=pins).status_code
        == 200
    )
    report = pins | {
        "container_id": "c" * 64,
        "state": "succeeded",
        "termination_confirmed": True,
        "stop_requested": False,
        "exit_code": 0,
    }
    result = post(client, worker, "/worker/docker/report", report, key="report", expected=200)
    assert (
        post(client, worker, "/worker/docker/report", report, key="report", expected=200) == result
    )
    assert result["human_resolution_required"] and result["scientific_validity"] == "not_assessed"
    content = b"synthetic Docker output"
    artifact = post(
        client,
        worker,
        f"/worker/leases/{lease['id']}/artifacts",
        {
            "expected_epoch": pins["expected_epoch"],
            "operation_id": pins["operation_id"],
            "relative_path": "result.txt",
            "sha256": hashlib.sha256(content).hexdigest(),
            "content_base64": base64.b64encode(content).decode(),
        },
    )
    manifest = post(
        client,
        worker,
        f"/worker/leases/{lease['id']}/artifact-manifest",
        {
            "expected_epoch": pins["expected_epoch"],
            "operation_id": pins["operation_id"],
            "artifacts": [
                {key: artifact[key] for key in ("id", "relative_path", "sha256", "size")}
            ],
        },
    )
    assert manifest["operation_id"] == pins["operation_id"] and manifest["status"] == "quarantined"
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM kubernetes_dispatches").fetchone()[0] == 0
        assert db.execute("SELECT provider_id FROM external_operations").fetchone()[0] == "c" * 64
        assert db.execute("SELECT held_cents FROM execution_project_limits").fetchone()[0] == 100
    post(
        client,
        owner,
        f"/execution-grants/{grant['id']}/reconcile",
        {
            "expected_revision": grant["revision"],
            "settlement_cents": 0,
            "provider_terminated": True,
        },
        expected=200,
    )


@pytest.mark.parametrize("fence", ["epoch", "digest", "target", "attempt", "revoked", "expired"])
def test_current_authority_and_claim_fail_closed(docker, fence):
    state, client, owner, worker, agent, target, grant, lease, attempt, package, pins, authority = (
        docker
    )
    pins = dict(pins)
    if fence == "epoch":
        pins["expected_epoch"] += 1
    if fence == "digest":
        pins["package_digest"] = "d" * 64
    if fence == "target":
        pins["target_digest"] = "d" * 64
    if fence == "attempt":
        pins["admitted_attempt_id"] = "other"
    if fence == "revoked":
        post(
            client,
            owner,
            f"/docker-targets/{target['id']}/revoke",
            {"expected_revision": 1},
            expected=200,
        )
    if fence == "expired":
        with sqlite3.connect(state / "tessera.sqlite3") as db:
            db.execute("UPDATE execution_leases SET expires_at='2000-01-01T00:00:00Z'")
    assert (
        client.post("/worker/docker/current-authority", headers=worker, json=pins).status_code
        == 409
    )
    post(client, worker, "/worker/docker/claim", pins, expected=409)


def test_unknown_does_not_release_and_cancelled_report_after_revocation(docker):
    state, client, owner, worker, agent, target, grant, lease, attempt, package, pins, authority = (
        docker
    )
    post(client, worker, "/worker/docker/claim", pins, expected=200)
    unknown = pins | {
        "state": "unknown",
        "container_id": None,
        "termination_confirmed": False,
        "stop_requested": False,
        "exit_code": None,
    }
    post(client, worker, "/worker/docker/report", unknown, expected=200)
    post(
        client,
        owner,
        f"/execution-grants/{grant['id']}/reconcile",
        {
            "expected_revision": grant["revision"],
            "settlement_cents": 0,
            "provider_terminated": True,
        },
        expected=409,
    )
    cancelled = post(
        client,
        owner,
        f"/execution-grants/{grant['id']}/revoke",
        {"expected_revision": grant["revision"]},
        expected=200,
    )
    assert (
        client.post("/worker/docker/current-authority", headers=worker, json=pins).status_code
        == 409
    )
    terminal = pins | {
        "state": "cancelled",
        "container_id": "c" * 64,
        "termination_confirmed": True,
        "stop_requested": True,
        "exit_code": 137,
    }
    post(client, worker, "/worker/docker/report", terminal, expected=200)
    post(
        client,
        owner,
        f"/execution-grants/{grant['id']}/reconcile",
        {
            "expected_revision": cancelled["revision"],
            "settlement_cents": 0,
            "provider_terminated": True,
        },
        expected=200,
    )


@pytest.mark.parametrize("held", [False, True])
def test_explicit53_to54_empty_enrollment(tmp_path, held):
    state = tmp_path / "state"
    initialize_state(state)
    if held:
        backup = tmp_path / "backup"
        backup_state(state, backup)
        restored = tmp_path / "held"
        restore_state(backup, restored)
        state = restored
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        strip_runtime_v55(db)
        db.execute("DROP TABLE ordinary_docker_dispatches")
        db.execute("DROP TABLE ordinary_docker_targets")
        db.execute("UPDATE schema_meta SET schema_version=53")
        tables = (
            "control_recovery",
            "principals",
            "credentials",
            "events",
            "idempotency",
            "generated_public_outputs",
        )
        before = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
        health = db.execute("SELECT * FROM operator_backup_health").fetchall()
    assert TestClient(create_app(state)).get("/health/ready").status_code == 503
    # Preserve proof of the historical 53->54 step before upgrading to current55.
    from tessera_control.migrations import migrate_v53_to_v54

    with sqlite3.connect(state / "tessera.sqlite3") as db:
        migrate_v53_to_v54(db)
        assert db.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 54
        assert db.execute("SELECT count(*) FROM ordinary_docker_targets").fetchone()[0] == 0
        assert (
            db.execute("SELECT 1 FROM sqlite_master WHERE name='runtime_enrollments'").fetchone()
            is None
        )
    assert TestClient(create_app(state)).get("/health/ready").status_code == 503
    assert migrate_state(state) == 55
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM ordinary_docker_targets").fetchone()[0] == 0
        assert_empty_runtime_v55(db)
        assert all(
            db.execute(f"SELECT * FROM {table}").fetchall() == before[table] for table in tables
        )
        if held:
            assert db.execute("SELECT * FROM operator_backup_health").fetchall() == health


@pytest.mark.parametrize(
    "change", ["hash", "attempt", "epoch", "container", "confirmation", "job_uid"]
)
def test_native_report_fences(docker, change):
    state, client, owner, worker, agent, target, grant, lease, attempt, package, pins, authority = (
        docker
    )
    post(client, worker, "/worker/docker/claim", pins, expected=200)
    body = pins | {
        "container_id": "c" * 64,
        "state": "succeeded",
        "termination_confirmed": True,
        "stop_requested": False,
        "exit_code": 0,
    }
    if change == "hash":
        body["package_digest"] = "f" * 64
    if change == "attempt":
        body["admitted_attempt_id"] = "other"
    if change == "epoch":
        body["expected_epoch"] += 1
    if change == "container":
        body["container_id"] = None
    if change == "confirmation":
        body["termination_confirmed"] = False
    if change == "job_uid":
        body["job_uid"] = "forbidden"
    post(
        client, worker, "/worker/docker/report", body, expected=422 if change == "job_uid" else 409
    )
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM execution_observations").fetchone()[0] == 0
        assert db.execute("SELECT held_cents FROM execution_project_limits").fetchone()[0] == 100


def test_agent_cannot_enroll_target_or_use_worker_channel(docker):
    state, client, owner, worker, agent, target, grant, lease, attempt, package, pins, authority = (
        docker
    )
    post(
        client,
        agent,
        f"/projects/{package['package']['project_id']}/docker-targets",
        target["bounds"],
        expected=403,
    )
    assert (
        client.post("/worker/docker/current-authority", headers=agent, json=pins).status_code == 401
    )


def test_running_restart_keeps_original_package_container_and_claim(docker):
    state, client, owner, worker, agent, target, grant, lease, attempt, package, pins, authority = (
        docker
    )
    original = post(client, worker, "/worker/docker/claim", pins, key="first-claim", expected=200)
    post(
        client,
        worker,
        "/worker/docker/report",
        pins
        | {
            "container_id": "c" * 64,
            "state": "running",
            "termination_confirmed": False,
            "stop_requested": False,
            "exit_code": None,
        },
        expected=200,
    )
    client = TestClient(create_app(state))
    pending = client.get(
        "/worker/docker/dispatches", headers=worker, params={"target_digest": pins["target_digest"]}
    ).json()
    assert (
        pending["dispatches"][0]["state"] == "running" and pending["dispatches"][0]["fence"] == pins
    )
    assert (
        post(client, worker, "/worker/docker/claim", pins, key="restart-claim", expected=200)
        == original
    )
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute(
            "SELECT state,container_id FROM ordinary_docker_dispatches"
        ).fetchone() == ("running", "c" * 64)
        assert db.execute("SELECT count(*) FROM external_operations").fetchone()[0] == 1


@pytest.mark.parametrize("docker", [False], indirect=True)
def test_native_never_queued_human_recovery_releases_once(docker):
    state, client, owner, worker, agent, target, grant, lease, attempt, package, pins, authority = (
        docker
    )
    cancelled = post(
        client,
        owner,
        f"/execution-grants/{grant['id']}/revoke",
        {"expected_revision": grant["revision"]},
        expected=200,
    )
    path = f"/execution-grants/{grant['id']}/reconcile-expired-undispatched"
    body = {"expected_revision": cancelled["revision"]}
    response = post(client, owner, path, body, key="native-no-start", expected=200)
    assert post(client, owner, path, body, key="native-no-start", expected=200) == response
    assert (
        client.post("/worker/docker/current-authority", headers=worker, json=pins).status_code
        == 409
    )
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT held_cents FROM execution_project_limits").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM ordinary_docker_dispatches").fetchone()[0] == 0


def test_native_ever_queued_never_claimed_is_not_absence_proof(docker):
    state, client, owner, worker, agent, target, grant, lease, attempt, package, pins, authority = (
        docker
    )
    cancelled = post(
        client,
        owner,
        f"/execution-grants/{grant['id']}/revoke",
        {"expected_revision": grant["revision"]},
        expected=200,
    )
    post(
        client,
        owner,
        f"/execution-grants/{grant['id']}/reconcile-expired-undispatched",
        {"expected_revision": cancelled["revision"]},
        expected=409,
    )


@pytest.mark.parametrize(
    "fence", ["lease_expired", "target_expired", "grant_expired", "target_revoked", "grant_revoked"]
)
def test_original_worker_stop_only_readback_and_durable_ack(docker, fence):
    state, client, owner, worker, agent, target, grant, lease, attempt, package, pins, authority = (
        docker
    )
    post(client, worker, "/worker/docker/claim", pins, expected=200)
    report = pins | {
        "container_id": "c" * 64,
        "state": "running",
        "termination_confirmed": False,
        "stop_requested": False,
        "exit_code": None,
    }
    first = post(
        client, worker, "/worker/docker/report", report, key="durable-running", expected=200
    )
    query = pins | {"expected_container_id": "c" * 64, "delivery_key": "durable-running"}
    before = client.post("/worker/docker/readback", headers=worker, json=query)
    assert before.status_code == 200 and before.json()["can_execute"] is True
    if fence == "lease_expired":
        with sqlite3.connect(state / "tessera.sqlite3") as db:
            db.execute("UPDATE execution_leases SET expires_at='2000-01-01T00:00:00Z'")
    elif fence == "target_expired":
        with sqlite3.connect(state / "tessera.sqlite3") as db:
            bounds = json.loads(
                db.execute("SELECT bounds_json FROM ordinary_docker_targets").fetchone()[0]
            )
            bounds["expires_at"] = "2000-01-01T00:00:00Z"
            db.execute("UPDATE ordinary_docker_targets SET bounds_json=?", (json.dumps(bounds),))
    elif fence == "grant_expired":
        with sqlite3.connect(state / "tessera.sqlite3") as db:
            db.execute("UPDATE execution_grants SET expires_at='2000-01-01T00:00:00Z'")
    elif fence == "target_revoked":
        post(
            client,
            owner,
            f"/docker-targets/{target['id']}/revoke",
            {"expected_revision": 1},
            expected=200,
        )
    else:
        post(
            client,
            owner,
            f"/execution-grants/{grant['id']}/revoke",
            {"expected_revision": grant["revision"]},
            expected=200,
        )
    client = TestClient(create_app(state))
    result = client.post("/worker/docker/readback", headers=worker, json=query)
    assert result.status_code == 200, result.text
    read = result.json()
    assert read["authorization"] == "stop_only" and read["can_execute"] is False
    assert read["effective_action"] == "stop" and read["container_id"] == "c" * 64
    assert read["desired_state"] == ("cancel_requested" if fence == "grant_revoked" else "admitted")
    assert read["delivery"]["response"] == first
    assert read["delivery"]["payload_digest"] == canonical_digest(report)
    assert read["latest_delivery"] == read["delivery"] and read["latest_report"] == report
    assert (
        client.post("/worker/docker/current-authority", headers=worker, json=pins).status_code
        == 409
    )
    post(client, worker, "/worker/docker/claim", pins, expected=409)
    post(
        client,
        worker,
        "/worker/leases/renew",
        {"lease_id": lease["id"], "expected_epoch": lease["epoch"]},
        expected=409,
    )
    cancelled = report | {
        "state": "cancelled",
        "termination_confirmed": True,
        "stop_requested": True,
        "exit_code": 137,
    }
    final = post(
        client, worker, "/worker/docker/report", cancelled, key="durable-cancelled", expected=200
    )
    latest = client.post("/worker/docker/readback", headers=worker, json=query).json()
    assert latest["delivery"]["response"] == first
    assert latest["latest_delivery"]["response"] == final
    assert latest["dispatch_state"] == "cancelled" and latest["can_execute"] is False
    assert (
        post(
            client,
            worker,
            "/worker/docker/report",
            cancelled,
            key="durable-cancelled",
            expected=200,
        )
        == final
    )
    missing = client.post(
        "/worker/docker/readback", headers=worker, json=pins | {"delivery_key": "not-delivered"}
    ).json()
    assert missing["delivery"] is None
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM execution_observations").fetchone()[0] == 2


@pytest.mark.parametrize(
    "mismatch",
    ["container", "epoch", "actor", "other_worker", "credential_expired", "tenant", "receipt"],
)
def test_readback_never_bypasses_identity_or_returns_wrong_receipt(docker, mismatch):
    state, client, owner, worker, agent, target, grant, lease, attempt, package, pins, authority = (
        docker
    )
    post(client, worker, "/worker/docker/claim", pins, expected=200)
    report = pins | {
        "container_id": "c" * 64,
        "state": "running",
        "termination_confirmed": False,
        "stop_requested": False,
        "exit_code": None,
    }
    post(client, worker, "/worker/docker/report", report, key="readback-report", expected=200)
    query = pins | {"expected_container_id": "c" * 64, "delivery_key": "readback-report"}
    auth = worker
    expected = 409
    if mismatch == "container":
        query["expected_container_id"] = "d" * 64
    if mismatch == "epoch":
        query["expected_epoch"] += 1
    if mismatch == "actor":
        auth = agent
        expected = 401
    if mismatch == "other_worker":
        _worker(state, _owner(state), package["package"]["project_id"], "other-worker-token")
        auth = {"Authorization": "Bearer other-worker-token"}
        expected = 404
    if mismatch == "credential_expired":
        with sqlite3.connect(state / "tessera.sqlite3") as db:
            db.execute(
                "UPDATE credentials SET expires_at='2000-01-01T00:00:00Z' WHERE kind='worker'"
            )
        expected = 401
    if mismatch == "tenant":
        with sqlite3.connect(state / "tessera.sqlite3") as db:
            db.execute("INSERT INTO organizations VALUES ('other','other','now')")
            db.execute("UPDATE external_operations SET org_id='other'")
    if mismatch == "receipt":
        with sqlite3.connect(state / "tessera.sqlite3") as db:
            fake = dict(report)
            fake["operation_id"] = "f" * 32
            db.execute(
                "UPDATE idempotency SET response_json=? WHERE idempotency_key='readback-report'",
                (
                    json.dumps(
                        fake
                        | {"scientific_validity": "not_assessed", "human_resolution_required": True}
                    ),
                ),
            )
    response = client.post("/worker/docker/readback", headers=auth, json=query)
    assert response.status_code == expected, response.text
