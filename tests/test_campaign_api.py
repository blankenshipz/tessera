"""Campaign HTTP routes against the real app and store (ADR 0038); synthetic data only."""

from __future__ import annotations

import pytest
from campaign_fixture import declare_snapshot_public
from fastapi.testclient import TestClient
from test_artifact_disposition_authority import _other_tenant_headers
from test_execution import _setup

from tessera_control.api import create_app
from tessera_control.store import _connect

IMAGE = "python@sha256:" + "a" * 64


def _spec(snapshot, worker_id, **stage_extra):
    stage = {
        "key": "s1",
        "backend": "docker-local-cpu-v1",
        "image": IMAGE,
        "argv": ["python", "-c", "print(1)"],
        "output_paths": ["data.json"],
        "max_output_bytes": 4096,
        "max_duration_seconds": 60,
        "inputs": [
            {
                "kind": "snapshot",
                "name": "code",
                "snapshot_id": snapshot[0],
                "digest": snapshot[1],
                "data_class": "public",
            }
        ],
        **stage_extra,
    }
    return {
        "version": 1,
        "name": "synthetic campaign",
        "budget_cap_cents": 4,
        "deadline_seconds": 600,
        "worker_principal_id": worker_id,
        "stages": [stage],
    }


@pytest.fixture
def world(tmp_path):
    state, execution, owner, worker, project, _ = _setup(tmp_path)
    with _connect(execution.database_path) as connection:
        snapshot = connection.execute(
            "SELECT r.snapshot_id, s.digest FROM snapshot_releases r "
            "JOIN source_snapshots s ON s.id = r.snapshot_id"
        ).fetchone()
    declare_snapshot_public(state, snapshot[0])
    client = TestClient(create_app(state))
    headers = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    return client, headers, project["id"], _spec(tuple(snapshot), worker["id"]), state


def _post(client, path, headers, body, key="k1"):
    return client.post(path, json=body, headers={**headers, "Idempotency-Key": key})


def test_submit_replay_conflict_and_validation(world):
    client, headers, project, spec, _ = world
    path = f"/projects/{project}/campaigns"
    first = _post(client, path, headers, {"spec": spec})
    assert first.status_code in (200, 201), first.text
    campaign = first.json()
    assert campaign["status"] == "submitted"
    replay = _post(client, path, headers, {"spec": spec})
    assert replay.status_code == first.status_code and replay.json() == campaign
    changed = {**spec, "name": "different"}
    assert _post(client, path, headers, {"spec": changed}).status_code == 409
    assert client.post(path, json={"spec": spec}, headers=headers).status_code == 400
    assert _post(client, path, headers, {"spec": {**spec, "stages": []}}, "k2").status_code == 422
    assert _post(client, path, headers, {"spec": spec, "extra": 1}, "k3").status_code == 422
    assert client.post(path, json={"spec": spec}).status_code == 401


def test_salad_stage_is_refused_at_submit_without_profiles(world):
    from types import SimpleNamespace

    from salad_campaign_fixture import salad_stage

    client, headers, project, spec, _ = world
    source = spec["stages"][0]["inputs"][0]
    stub = SimpleNamespace(
        snapshot_id=source["snapshot_id"],
        snapshot_digest=source["digest"],
        worker={"id": spec["worker_principal_id"]},
    )
    response = _post(
        client,
        f"/projects/{project}/campaigns",
        headers,
        {"spec": {**spec, "budget_cap_cents": 50, "stages": [salad_stage(stub)]}},
    )
    assert response.status_code == 409
    assert "not enabled" in response.json()["detail"]
    # A Salad stage that carries no salad block is invalid outright, not merely refused.
    bare = {**spec["stages"][0], "backend": "salad-gpu-v1"}
    response = _post(
        client,
        f"/projects/{project}/campaigns",
        headers,
        {"spec": {**spec, "stages": [bare]}},
        "k9",
    )
    assert response.status_code == 422


def test_get_list_and_not_found(world):
    client, headers, project, spec, state = world
    campaign = _post(client, f"/projects/{project}/campaigns", headers, {"spec": spec}).json()
    got = client.get(f"/campaigns/{campaign['id']}", headers=headers)
    assert got.status_code == 200 and got.json()["id"] == campaign["id"]
    listed = client.get(f"/projects/{project}/campaigns?offset=0&limit=10", headers=headers).json()
    assert [c["id"] for c in listed["campaigns"]] == [campaign["id"]]
    assert (
        client.get(f"/projects/{project}/campaigns?limit=201", headers=headers).status_code == 422
    )
    assert client.get("/campaigns/missing", headers=headers).status_code == 404
    other = _other_tenant_headers(state)
    assert client.get(f"/campaigns/{campaign['id']}", headers=other).status_code == 404
    assert client.get(f"/campaigns/{campaign['id']}/receipt", headers=other).status_code == 404


def test_approve_is_exact_and_idempotent(world):
    client, headers, project, spec, _ = world
    campaign = _post(client, f"/projects/{project}/campaigns", headers, {"spec": spec}).json()
    path = f"/campaigns/{campaign['id']}/approve"
    body = {"expected_revision": campaign["revision"], "expected_spec_digest": "0" * 64}
    assert _post(client, path, headers, body, "a1").status_code in (403, 409)
    body["expected_spec_digest"] = campaign["spec_digest"]
    approved = _post(client, path, headers, body, "a2")
    assert approved.status_code in (200, 201), approved.text
    assert _post(client, path, headers, body, "a2").json() == approved.json()
    assert client.get(f"/campaigns/{campaign['id']}", headers=headers).json()["status"] == "running"
    assert client.post(path, json=body, headers={**headers}).status_code == 400


def test_cancel_is_reduce_only_and_receipt_waits_for_terminal(world):
    client, headers, project, spec, _ = world
    campaign = _post(client, f"/projects/{project}/campaigns", headers, {"spec": spec}).json()
    approve = {
        "expected_revision": campaign["revision"],
        "expected_spec_digest": campaign["spec_digest"],
    }
    approved = _post(client, f"/campaigns/{campaign['id']}/approve", headers, approve, "a1").json()
    assert client.get(f"/campaigns/{campaign['id']}/receipt", headers=headers).status_code == 404
    cancel = {"expected_revision": approved["revision"], "reason": "synthetic cancel"}
    response = _post(client, f"/campaigns/{campaign['id']}/cancel", headers, cancel, "c1")
    assert response.status_code in (200, 201), response.text
    assert response.json()["status"] in ("cancel_requested", "cancelled")
    bad = {"expected_revision": 1, "reason": ""}
    assert (
        _post(client, f"/campaigns/{campaign['id']}/cancel", headers, bad, "c2").status_code == 422
    )
