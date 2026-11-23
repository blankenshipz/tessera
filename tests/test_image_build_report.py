"""ADR 0123: control records one image-build result per operation, validated by the plan."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from test_image_build_worker import BASE, DIGEST, EGRESS, POLICY, raw
from test_sandbox_egress_control import _world

from tessera_control.api import create_app
from tessera_control.image_build_models import FAILURE_CODES, ReportImageBuild
from tessera_control.image_build_report import (
    image_build_failure_for_operation,
    image_build_for_operation,
)
from tessera_control.store import _connect
from tessera_worker.image_build import builder_argv, parse_build_result


def _setup(tmp_path, policy=POLICY):
    spec = {"argv": builder_argv(policy), "egress": EGRESS, "build": policy}
    state, _, worker, _, _, lease, op = _world(tmp_path / "w", spec)
    return state, lease, op["package"]["operation_id"], TestClient(create_app(state))


def _row(**changes):
    row = parse_build_result(raw(), POLICY)
    return row | changes


def _post(client, operation, lease, key="k1", token="worker-token", **body):
    payload = {"lease_id": lease["id"], "expected_epoch": lease["epoch"]} | body
    return client.post(
        f"/worker/operations/{operation}/image-build",
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
    )


def test_result_is_recorded_once_with_no_secret(tmp_path):
    state, lease, operation, client = _setup(tmp_path)
    response = _post(client, operation, lease, result=_row())
    assert response.status_code == 201, response.text
    with _connect(state / "tessera.sqlite3") as connection:
        event = image_build_for_operation(connection, operation)
        assert image_build_failure_for_operation(connection, operation) is None
        stored = connection.execute(
            "SELECT payload_json FROM events WHERE event_type='execution.image_built'"
        ).fetchone()[0]
    assert event == {
        "operation_id": operation,
        "image": f"ghcr.io/org/img@sha256:{DIGEST}",
        "digest": DIGEST,
        "size_bytes": 12345,
        "pushed": True,
        "base_images": [BASE],
        "via_control": False,
    }
    for secret in ("auth", "password", "token", "worker-token"):
        assert secret not in stored
    assert _post(client, operation, lease, key="k2", result=_row()).status_code == 409
    # The same key replays the stored answer instead of recording a second event.
    assert _post(client, operation, lease, result=_row()).status_code == 201


def test_failure_code_is_recorded_and_blocks_a_later_success(tmp_path):
    state, lease, operation, client = _setup(tmp_path)
    assert _post(client, operation, lease, failed="result_missing").status_code == 201
    with _connect(state / "tessera.sqlite3") as connection:
        assert image_build_failure_for_operation(connection, operation) == {
            "operation_id": operation,
            "failed": "result_missing",
            "via_control": False,
        }
        assert image_build_for_operation(connection, operation) is None
    assert _post(client, operation, lease, key="k2", result=_row()).status_code == 409


@pytest.mark.parametrize(
    "change",
    [
        {"image": f"evil.example/org/img@sha256:{DIGEST}"},
        {"image": f"ghcr.io/org/other@sha256:{DIGEST}"},
        {"image": f"ghcr.io/org/img@sha256:{'d' * 64}"},
        {"pushed": False},
        {"base_images": ["x/y@sha256:" + "e" * 64]},
        {"size_bytes": 10**9 + 1},
    ],
)
def test_row_contradicting_the_plan_is_409_and_not_recorded(tmp_path, change):
    state, lease, operation, client = _setup(tmp_path)
    response = _post(client, operation, lease, result=_row(**change))
    assert response.status_code in (409, 422)
    with _connect(state / "tessera.sqlite3") as connection:
        assert image_build_for_operation(connection, operation) is None


def test_auth_lease_epoch_and_operation_are_checked(tmp_path):
    state, lease, operation, client = _setup(tmp_path)
    assert _post(client, operation, lease, token="wrong", result=_row()).status_code in (401, 403)
    assert _post(client, "f" * 32, lease, result=_row()).status_code == 404
    assert (
        client.post(
            f"/worker/operations/{operation}/image-build",
            json={"lease_id": "other", "expected_epoch": lease["epoch"], "result": _row()},
            headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "k9"},
        ).status_code
        == 409
    )
    assert (
        _post(client, operation, lease, key="k8", result=_row(), expected_epoch=99).status_code
        == 409
    )
    no_auth = client.post(
        f"/worker/operations/{operation}/image-build", json={}, headers={"Idempotency-Key": "k"}
    )
    assert no_auth.status_code in (401, 403)


def test_operation_without_a_build_plan_is_refused(tmp_path):
    state, _, worker, _, _, lease, op = _world(tmp_path / "plain", {})
    client = TestClient(create_app(state))
    response = _post(client, op["package"]["operation_id"], lease, result=_row())
    assert response.status_code == 409


def test_report_body_is_strict():
    ok = {"lease_id": "l", "expected_epoch": 1, "result": _row()}
    assert ReportImageBuild.model_validate(ok)
    for bad in (
        ok | {"failed": "result_missing"},
        {"lease_id": "l", "expected_epoch": 1},
        {"lease_id": "l", "expected_epoch": 1, "failed": "free text, not a code"},
        ok | {"extra": 1},
        {**ok, "result": _row() | {"extra": 1}},
    ):
        with pytest.raises(ValueError):
            ReportImageBuild.model_validate(bad)
    assert all(
        ReportImageBuild.model_validate({"lease_id": "l", "expected_epoch": 1, "failed": c})
        for c in FAILURE_CODES
    )
    json.dumps(sorted(FAILURE_CODES))
