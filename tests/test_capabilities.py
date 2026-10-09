"""Offline policy/intent/audit tests: no downloaded bytes or installed packages."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_input_delivery import _delivery

from tessera_control.api import create_app
from tessera_control.capabilities import CapabilityStore
from tessera_control.capability_models import CapabilityDeclaration
from tessera_control.store import Conflict, Forbidden, Store, canonical_digest


def declaration(kind="model"):
    action = {
        "id": "artifact-1",
        "kind": kind,
        "name": "fixture",
        "version": "1.0",
        "url": "https://artifacts.example.org/approved/fixture",
        "sha256": "b" * 64,
        "max_bytes": 1024,
        "max_seconds": 10,
        "filesystem_root": "/workspace/artifacts",
    }
    policy = {
        "environment_id": "sandbox-1",
        "image_digest": "sha256:" + "a" * 64,
        "network": [{"host": "artifacts.example.org", "port": 443}],
        "filesystem_roots": ["/workspace/artifacts"],
        "max_bytes": 1024,
        "max_seconds": 10,
        "actions": [action],
    }
    if kind == "package":
        action.update(
            manager="pip",
            channel="https://artifacts.example.org/approved",
            lockfile_sha256="c" * 64,
        )
        policy["package_channels"] = [{"manager": "pip", "url": action["channel"]}]
    else:
        policy[kind + "_registries"] = ["https://artifacts.example.org/approved"]
    return CapabilityDeclaration.model_validate(policy).model_dump(mode="json")


def setup_policy(tmp_path, kind="model", *, approve=True, declared=None):
    state, _, owner, worker, grant, lease = _delivery(tmp_path)
    store = CapabilityStore(state)
    request = {
        "expected_plan_revision": grant["task_revision"] - 1,
        "expected_plan_digest": grant["plan_digest"],
        "declaration": declared or declaration(kind),
    }
    status, policy = store.request(owner, grant["task_id"], "request", request)
    assert status == 201
    decision = {
        "expected_revision": 1,
        "declaration_digest": policy["declaration_digest"],
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    }
    if approve:
        status, policy = store.approve(owner, policy["id"], "approve", decision)
        assert status == 200
    binding = {
        "execution_lease_id": lease["id"],
        "expected_epoch": lease["epoch"],
        "environment_id": request["declaration"]["environment_id"],
        "image_digest": request["declaration"]["image_digest"],
        "action": request["declaration"]["actions"][0],
        "credential_access_lease_id": None,
    }
    return state, store, owner, worker, grant, lease, policy, binding, decision


def receipt(binding):
    action = binding["action"]
    packages = (
        [
            {
                "name": action["name"],
                "version": action["version"],
                "artifact_sha256": action["sha256"],
            }
        ]
        if action["kind"] == "package"
        else []
    )
    return {
        "actual": action,
        "bytes_downloaded": 100,
        "elapsed_ms": 5,
        "outcome": "installed" if packages else "downloaded",
        "downloaded_sha256": action["sha256"],
        "installed_packages": packages,
        "result_sha256": canonical_digest(packages) if packages else action["sha256"],
    }


def events(store):
    with sqlite3.connect(store.database_path) as connection:
        return [
            (name, json.loads(body))
            for name, body in connection.execute(
                "SELECT event_type,payload_json FROM events "
                "WHERE event_type LIKE 'capability.%' ORDER BY cursor"
            )
        ]


@pytest.mark.parametrize("kind", ["model", "data", "package"])
def test_approved_intent_replay_receipt_and_complete_audit(tmp_path, kind):
    state, store, owner, worker, _, _, policy, binding, decision = setup_policy(tmp_path, kind)
    assert policy["execution_enabled"] is False
    assert store.approve(owner, policy["id"], "approve", decision) == (200, policy)
    first = store.admit(worker, policy["id"], "admit", binding)
    assert first[0] == 201 and first[1]["execution_enabled"] is False
    assert store.admit(worker, policy["id"], "admit", binding) == first
    assert store.admit(worker, policy["id"], "another-key", binding)[0] == 409
    operation = first[1]["id"]
    assert store.authorize(worker, operation)[0] == 200
    report = receipt(binding)
    status, recorded = store.receipt(worker, operation, "receipt", report)
    assert status == 201 and recorded["assessment"] == "reported"
    assert recorded["evidence"] == "worker_reported_not_independently_verified"
    assert store.receipt(worker, operation, "receipt", report) == (status, recorded)
    assert store.receipt(worker, operation, "new-receipt-key", report) == (200, recorded)
    with pytest.raises(Conflict):
        store.receipt(worker, operation, "changed", report | {"bytes_downloaded": 50})
    assert store.authorize(worker, operation)[0] == 403
    with pytest.raises(Conflict):
        store.admit(worker, policy["id"], "admit", binding)
    audit = events(store)
    names = [name for name, _ in audit]
    for name in ("requested", "approved", "operation_admitted", "effect_reported"):
        assert names.count("capability." + name) == 1
    assert "capability.operation_denied" in names and "capability.use_denied" in names
    assert audit[0][1]["declaration"] == policy["declaration"]
    assert audit[1][1]["approved_by"] == owner["id"]
    assert (
        next(body for name, body in audit if name == "capability.effect_reported")["receipt"]
        == report
    )
    restarted = CapabilityStore(state).get(owner, policy["id"])
    assert restarted["operations"][0]["receipt"] == recorded


@pytest.mark.parametrize(
    "field,value",
    [
        ("environment_id", "another-environment"),
        ("image_digest", "sha256:" + "c" * 64),
        ("expected_epoch", 99),
        ("credential_access_lease_id", "ambient-key"),
    ],
)
def test_binding_violations_are_audited(tmp_path, field, value):
    _, store, _, worker, _, _, policy, binding, _ = setup_policy(tmp_path)
    assert store.admit(worker, policy["id"], "violation", binding | {field: value})[0] == 403
    assert events(store)[-1][0] == "capability.operation_denied"


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "https://other.example.org/fixture"},
        {"url": "https://artifacts.example.org:444/approved/fixture"},
        {"sha256": "d" * 64},
        {"version": "2.0"},
        {"filesystem_root": "/workspace/packages"},
        {"max_bytes": 1025},
        {"max_seconds": 11},
        {"manager": "uv"},
        {"channel": "https://other.example.org/simple"},
        {"lockfile_sha256": "d" * 64},
    ],
)
def test_action_cannot_widen_any_approved_field(tmp_path, changes):
    _, store, _, worker, _, _, policy, binding, _ = setup_policy(tmp_path, "package")
    body = binding | {"action": binding["action"] | changes}
    assert store.admit(worker, policy["id"], "violation", body)[0] == 403


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"downloaded_sha256": "d" * 64}, "download_digest_mismatch"),
        ({"result_sha256": "d" * 64}, "result_digest_mismatch"),
        ({"bytes_downloaded": 1025}, "budget_exceeded"),
        ({"elapsed_ms": 10001}, "budget_exceeded"),
        ({"outcome": "failed"}, "incomplete_effect"),
    ],
)
def test_bad_effect_is_retained_quarantined_and_revokes_policy(tmp_path, changes, reason):
    _, store, owner, worker, _, _, policy, binding, _ = setup_policy(tmp_path)
    _, op = store.admit(worker, policy["id"], "admit", binding)
    report = receipt(binding) | changes
    _, result = store.receipt(worker, op["id"], "receipt", report)
    assert result["receipt"] == report
    assert result["assessment"] == "quarantined" and reason in result["reasons"]
    assert store.get(owner, policy["id"])["status"] == "revoked"
    assert events(store)[-1][0] == "capability.automatically_revoked"


def test_actual_prohibited_host_and_control_root_remain_auditable(tmp_path):
    _, store, _, worker, _, _, policy, binding, _ = setup_policy(tmp_path)
    _, op = store.admit(worker, policy["id"], "admit", binding)
    report = receipt(binding)
    report["actual"] = binding["action"] | {
        "url": "https://169.254.169.254/metadata",
        "filesystem_root": "/control-pvc",
    }
    _, result = store.receipt(worker, op["id"], "receipt", report)
    assert "action_or_digest_mismatch" in result["reasons"]
    assert result["receipt"]["actual"] == report["actual"]


def test_package_actual_versions_and_inventory_digest_are_checked(tmp_path):
    _, store, _, worker, _, _, policy, binding, _ = setup_policy(tmp_path, "package")
    _, op = store.admit(worker, policy["id"], "admit", binding)
    report = receipt(binding)
    report["installed_packages"][0]["version"] = "unexpected-version"
    _, result = store.receipt(worker, op["id"], "receipt", report)
    assert set(result["reasons"]) == {"installed_inventory_mismatch", "inventory_digest_mismatch"}


def test_event_failure_rolls_back_intent_and_idempotency(tmp_path, monkeypatch):
    _, store, _, worker, _, _, policy, binding, _ = setup_policy(tmp_path)

    def fail(*args):
        raise sqlite3.OperationalError("injected audit failure")

    with monkeypatch.context() as patch:
        patch.setattr(Store, "_event", staticmethod(fail))
        with pytest.raises(sqlite3.OperationalError):
            store.admit(worker, policy["id"], "admit", binding)
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM capability_operations").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM idempotency WHERE path LIKE '/worker/capabilities/%'"
            ).fetchone()[0]
            == 0
        )
    assert store.admit(worker, policy["id"], "admit", binding)[0] == 201


def test_api_separates_audiences_strict_models_and_human_approval(tmp_path):
    state, store, owner, worker, grant, _, policy, binding, decision = setup_policy(
        tmp_path, approve=False
    )
    client = TestClient(create_app(state))
    owner_headers = {
        "Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1],
        "Idempotency-Key": "http",
    }
    worker_headers = {"Authorization": "Bearer worker-token", "Idempotency-Key": "http"}
    path = f"/worker/capabilities/{policy['id']}/operations"
    assert client.post(path, headers=owner_headers, json=binding).status_code == 401
    assert (
        client.post(path, headers=worker_headers, json=binding | {"allow_host": True}).status_code
        == 422
    )
    assert client.post(path, headers=worker_headers, json=binding).status_code == 403
    approval = f"/capabilities/{policy['id']}/approve"
    assert client.post(approval, headers=worker_headers, json=decision).status_code == 401
    # A control-audience agent can propose, but cannot approve or revoke.
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE credentials SET kind='agent',audience='control' WHERE principal_id=?",
            (worker["id"],),
        )
    agent = Store(state).authenticate("worker-token")
    request = {
        "expected_plan_revision": grant["task_revision"] - 1,
        "expected_plan_digest": grant["plan_digest"],
        "declaration": declaration(),
    }
    assert store.request(agent, grant["task_id"], "agent-request", request)[0] == 201
    assert client.post(approval, headers=worker_headers, json=decision).status_code == 403
    with pytest.raises(Forbidden):
        store.revoke(agent, policy["id"], "agent-revoke", {"expected_revision": 1})
    assert client.post(approval, headers=owner_headers, json=decision).status_code == 200
    assert (
        client.get(f"/capabilities/{policy['id']}", headers=owner_headers).json()[
            "execution_enabled"
        ]
        is False
    )
    assert owner["id"] != worker["id"]


def test_concurrent_admission_reserves_budget_only_once(tmp_path):
    _, store, _, worker, _, _, policy, binding, _ = setup_policy(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(store.admit, worker, policy["id"], key, binding) for key in ("one", "two")
        ]
        assert sorted(future.result()[0] for future in futures) == [201, 409]
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM capability_operations").fetchone()[0] == 1


def test_http_worker_authorize_receipt_and_revoke(tmp_path):
    state, _, _, _, _, _, policy, binding, _ = setup_policy(tmp_path)
    client = TestClient(create_app(state))
    headers = {"Authorization": "Bearer worker-token", "Idempotency-Key": "http-admit"}
    response = client.post(
        f"/worker/capabilities/{policy['id']}/operations", headers=headers, json=binding
    )
    assert response.status_code == 201
    op = response.json()["id"]
    response = client.post(f"/worker/capability-operations/{op}/authorize", headers=headers)
    assert response.status_code == 200 and response.json()["execution_enabled"] is False
    response = client.post(
        f"/worker/capability-operations/{op}/receipt", headers=headers, json=receipt(binding)
    )
    assert response.status_code == 201 and response.json()["assessment"] == "reported"
    owner_headers = {
        "Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1],
        "Idempotency-Key": "http-revoke",
    }
    response = client.post(
        f"/capabilities/{policy['id']}/revoke", headers=owner_headers, json={"expected_revision": 2}
    )
    assert response.status_code == 200 and response.json()["status"] == "revoked"
