"""Actual authenticated API/SQLite/materialization with temporary synthetic principals."""

import base64
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_artifact_disposition_authority import _other_tenant_headers
from test_dataset_snapshots import DATA, DECLARATION
from test_execution import _grant_payload, _setup, _task, _worker

from tessera_control.api import create_app
from tessera_control.dataset_snapshots import (
    DatasetSnapshotStore,
    build_dataset_capture,
    load_dataset_snapshot,
    manifest_bytes,
    verify_dataset_bytes,
)
from tessera_control.store import _connect
from tessera_control.workspace import inventory_digest
from tessera_worker.inputs import materialize_snapshot


class SafeFixture(dict):
    def __repr__(self):
        return "<temporary dataset API fixture; credentials redacted>"


def command(client, actor, path, body, key="command", expected=201):
    response = client.post(path, headers={**actor, "Idempotency-Key": key}, json=body)
    assert response.status_code == expected, response.text
    return response.json()


def upload_body(data=DATA, declaration=DECLARATION):
    sha = hashlib.sha256(data).hexdigest()
    observed = verify_dataset_bytes(data, declaration, expected_sha256=sha, expected_size=len(data))
    digest = hashlib.sha256(manifest_bytes(observed)).hexdigest()
    capture = build_dataset_capture(
        data,
        declaration,
        expected_sha256=sha,
        expected_size=len(data),
        expected_manifest_digest=digest,
    )
    return {
        "declaration": declaration,
        "content_base64": base64.b64encode(data).decode(),
        "expected_data_sha256": sha,
        "expected_data_size": len(data),
        "expected_manifest_digest": digest,
        "expected_input_snapshot_digest": inventory_digest(capture.inventory),
    }


@pytest.fixture
def dataset(tmp_path):
    state, execution, human, worker, project, _ = _setup(tmp_path)
    client = TestClient(create_app(state))
    owner = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    actor = command(
        client,
        owner,
        "/credentials",
        {
            "token": "dataset-agent-" + "a" * 32,
            "kind": "agent",
            "audience": "control",
            "project_id": project["id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
        key="subject",
    )
    return SafeFixture(
        state=state,
        client=client,
        human=human,
        owner=owner,
        worker=worker,
        project=project,
        execution=execution,
        subject=actor,
        agent={"Authorization": "Bearer dataset-agent-" + "a" * 32},
        worker_headers={"Authorization": "Bearer worker-token"},
        body=upload_body(),
        path=f"/projects/{project['id']}/tabular-dataset-snapshots",
    )


def adopt(r, key="adopt"):
    return command(r["client"], r["owner"], r["path"], r["body"], key)


def release(r, snapshot):
    return command(
        r["client"],
        r["owner"],
        f"/snapshots/{snapshot['id']}/releases",
        {
            "expected_digest": snapshot["digest"],
            "worker_principal_id": r["worker"]["id"],
            "classification": "public",
            "permits_worker_transfer": True,
            "rationale": "Release actual synthetic dataset for bounded test workload",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=20)).isoformat(),
        },
        key="release-data",
    )


def lease(r, snapshot):
    task = _task(r["state"], r["human"], r["project"]["id"], snapshot["digest"], "Dataset")
    body = _grant_payload(task, r["worker"], snapshot)
    body["expires_at"] = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    grant = command(
        r["client"],
        r["owner"],
        f"/projects/{r['project']['id']}/execution-grants",
        body,
        key="dataset-grant",
    )
    acquired = command(
        r["client"],
        r["worker_headers"],
        "/worker/leases",
        {"grant_id": grant["id"]},
        key="dataset-lease",
    )
    return grant, acquired


def test_normal_api_human_adopt_release_worker_lease_deliver_same_verified_bytes(dataset, tmp_path):
    r = dataset
    snapshot = adopt(r)
    assert snapshot == adopt(r)
    assert snapshot["transfer_authorized"] is False
    read = r["client"].get(f"/tabular-dataset-snapshots/{snapshot['id']}", headers=r["agent"])
    assert read.status_code == 200 and read.json() == snapshot
    assert snapshot["dataset_manifest"]["license_verification"] == "declared-not-legally-verified"
    assert (
        snapshot["dataset_manifest"]["source_verification"] == "declared-not-independently-verified"
    )
    released = release(r, snapshot)
    grant, acquired = lease(r, snapshot)
    assert (
        grant["snapshot_release_id"] == released["id"]
        and grant["snapshot_digest"] == snapshot["digest"]
    )
    path = f"/worker/leases/{acquired['id']}/inputs"
    bundle = command(
        r["client"],
        r["worker_headers"],
        path,
        {"expected_epoch": acquired["epoch"]},
        key="dataset-inputs",
    )
    assert (
        command(
            r["client"],
            r["worker_headers"],
            path,
            {"expected_epoch": acquired["epoch"]},
            key="dataset-inputs",
        )
        == bundle
    )
    staged = materialize_snapshot(
        bundle,
        tmp_path,
        expected_snapshot_digest=grant["snapshot_digest"],
        expected_lease_id=acquired["id"],
        expected_epoch=acquired["epoch"],
        expected_worker_id=r["worker"]["id"],
    )
    observed, content = load_dataset_snapshot(
        staged.directory, expected_manifest_digest=snapshot["dataset_manifest_digest"]
    )
    assert content == DATA and observed.model_dump(mode="json") == snapshot["dataset_manifest"]
    c = _connect(r["state"] / "tessera.sqlite3")
    try:
        assert (
            c.execute(
                "SELECT count(*) FROM source_snapshots WHERE id=?", (snapshot["id"],)
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute(
                "SELECT count(*) FROM snapshot_files WHERE snapshot_id=?", (snapshot["id"],)
            ).fetchone()[0]
            == 2
        )
        assert (
            c.execute(
                "SELECT count(*) FROM events WHERE event_type='workspace.snapshot_adopted' "
                "AND json_extract(payload_json,'$.snapshot_id')=?",
                (snapshot["id"],),
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute(
                "SELECT count(*) FROM events WHERE event_type='worker.inputs_release_authorized'"
            ).fetchone()[0]
            == 1
        )
        assert (
            "content_base64"
            not in c.execute(
                "SELECT response_json FROM idempotency WHERE path=?", (path,)
            ).fetchone()[0]
        )
        assert c.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 55
    finally:
        c.close()


@pytest.mark.parametrize(
    "change,expected",
    [
        ("base64", 400),
        ("noncanonical-base64", 400),
        ("oversize-target", 422),
        ("digest", 409),
        ("size", 409),
        ("manifest", 409),
        ("input-digest", 409),
        ("server-path", 422),
        ("verified", 422),
        ("license", 422),
        ("invalid-schema-bytes", 409),
    ],
)
def test_upload_rejects_untrusted_truth_targets_or_paths_atomically(dataset, change, expected):
    r = dataset
    body = json.loads(json.dumps(r["body"]))
    if change == "base64":
        body["content_base64"] = "!!!!"
    elif change == "noncanonical-base64":
        body["content_base64"] = "Zh=="
    elif change == "oversize-target":
        body["expected_data_size"] = 4 * 1024 * 1024 + 1
    elif change == "digest":
        body["expected_data_sha256"] = "0" * 64
    elif change == "size":
        body["expected_data_size"] += 1
    elif change == "manifest":
        body["expected_manifest_digest"] = "0" * 64
    elif change == "input-digest":
        body["expected_input_snapshot_digest"] = "0" * 64
    elif change == "server-path":
        body["server_path"] = "/private/not-authorized.csv"
    elif change == "verified":
        body["verified"] = True
    elif change == "license":
        body["declaration"]["source"].pop("license")
    else:
        data = DATA.replace(b"a,1,2", b"a,nan,2")
        body.update(
            content_base64=base64.b64encode(data).decode(),
            expected_data_sha256=hashlib.sha256(data).hexdigest(),
            expected_data_size=len(data),
        )
    command(r["client"], r["owner"], r["path"], body, expected=expected)
    c = _connect(r["state"] / "tessera.sqlite3")
    try:
        assert (
            c.execute(
                "SELECT count(*) FROM source_snapshots WHERE "
                "json_extract(manifest_json,'$.root')='tessera-dataset-snapshot-v1'"
            ).fetchone()[0]
            == 0
        )
    finally:
        c.close()


def test_dataset_upload_human_only_and_worker_audience_not_control(dataset):
    r = dataset
    command(r["client"], r["agent"], r["path"], r["body"], expected=403)
    command(r["client"], r["worker_headers"], r["path"], r["body"], expected=401)
    assert (
        r["client"]
        .post(r["path"], json=r["body"], headers={"Idempotency-Key": "unauth"})
        .status_code
        == 401
    )


def test_current_project_and_tenant_scope_and_nonprofile_read(dataset):
    r = dataset
    snapshot = adopt(r)
    outsider = _other_tenant_headers(r["state"])
    assert (
        r["client"]
        .get(f"/tabular-dataset-snapshots/{snapshot['id']}", headers=outsider)
        .status_code
        == 404
    )
    command(r["client"], outsider, r["path"], r["body"], key="foreign-adopt", expected=404)
    other = command(r["client"], r["owner"], "/projects", {"name": "Other"}, key="other-project")
    command(
        r["client"],
        r["owner"],
        "/credentials",
        {
            "token": "other-project-agent-" + "b" * 32,
            "kind": "agent",
            "audience": "control",
            "project_id": other["id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
        key="other-agent",
    )
    assert (
        r["client"]
        .get(
            f"/tabular-dataset-snapshots/{snapshot['id']}",
            headers={"Authorization": "Bearer other-project-agent-" + "b" * 32},
        )
        .status_code
        == 404
    )
    c = _connect(r["state"] / "tessera.sqlite3")
    try:
        ordinary = c.execute(
            "SELECT id FROM source_snapshots WHERE id!=?", (snapshot["id"],)
        ).fetchone()[0]
    finally:
        c.close()
    assert (
        r["client"].get(f"/tabular-dataset-snapshots/{ordinary}", headers=r["owner"]).status_code
        == 409
    )


def test_upload_idempotency_conflict_and_event_failure_roll_back_bytes(dataset, monkeypatch):
    r = dataset
    snapshot = adopt(r)
    body = upload_body(DATA.replace(b"a,1,2", b"a,1,9"))
    command(r["client"], r["owner"], r["path"], body, key="adopt", expected=409)

    def fail(*args):
        raise sqlite3.OperationalError("synthetic dataset event failure")

    with monkeypatch.context() as patch:
        patch.setattr(DatasetSnapshotStore, "_event", staticmethod(fail))
        with pytest.raises(sqlite3.OperationalError):
            command(r["client"], r["owner"], r["path"], body, key="event-failure")
    c = _connect(r["state"] / "tessera.sqlite3")
    try:
        assert (
            c.execute(
                "SELECT count(*) FROM source_snapshots WHERE "
                "json_extract(manifest_json,'$.root')='tessera-dataset-snapshot-v1'"
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute(
                "SELECT count(*) FROM snapshot_files WHERE snapshot_id=?", (snapshot["id"],)
            ).fetchone()[0]
            == 2
        )
    finally:
        c.close()


@pytest.mark.parametrize(
    "fence",
    [
        "release",
        "release-expiry",
        "lease-expiry",
        "epoch",
        "worker",
        "wrong-worker",
        "grant",
        "bytes",
    ],
)
def test_dataset_delivery_current_release_lease_grant_and_byte_fences(dataset, fence):
    r = dataset
    snapshot = adopt(r)
    released = release(r, snapshot)
    grant, acquired = lease(r, snapshot)
    path = f"/worker/leases/{acquired['id']}/inputs"
    command(
        r["client"], r["worker_headers"], path, {"expected_epoch": acquired["epoch"]}, key="inputs"
    )
    expected = 409
    headers = r["worker_headers"]
    epoch = acquired["epoch"]
    c = _connect(r["state"] / "tessera.sqlite3")
    try:
        if fence == "release":
            c.execute("UPDATE snapshot_releases SET status='revoked' WHERE id=?", (released["id"],))
            expected = 403
        elif fence == "release-expiry":
            c.execute(
                "UPDATE snapshot_releases SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
                (released["id"],),
            )
            expected = 403
        elif fence == "lease-expiry":
            c.execute(
                "UPDATE execution_leases SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
                (acquired["id"],),
            )
        elif fence == "epoch":
            epoch += 1
        elif fence == "worker":
            c.execute(
                "UPDATE credentials SET revoked_at='2026-01-01' WHERE id=?",
                (r["worker"]["credential_id"],),
            )
            expected = 401
        elif fence == "grant":
            c.execute(
                "UPDATE execution_grants SET status='cancel_requested' WHERE id=?", (grant["id"],)
            )
        elif fence == "bytes":
            c.execute(
                "UPDATE snapshot_files SET content=x'00' WHERE snapshot_id=? "
                "AND relative_path='dataset.csv'",
                (snapshot["id"],),
            )
        c.commit()
    finally:
        c.close()
    if fence == "wrong-worker":
        _worker(r["state"], r["human"], r["project"]["id"], "other-worker")
        headers = {"Authorization": "Bearer other-worker"}
        expected = 404
    command(r["client"], headers, path, {"expected_epoch": epoch}, key="inputs", expected=expected)
    command(
        r["client"],
        r["owner"],
        path,
        {"expected_epoch": acquired["epoch"]},
        key="human-not-worker",
        expected=401,
    )


def test_retained_bytes_tamper_fences_get_and_adoption_replay(dataset):
    r = dataset
    snapshot = adopt(r)
    c = _connect(r["state"] / "tessera.sqlite3")
    try:
        c.execute(
            "UPDATE snapshot_files SET content=x'00' WHERE snapshot_id=? "
            "AND relative_path='dataset.csv'",
            (snapshot["id"],),
        )
        c.commit()
    finally:
        c.close()
    assert (
        r["client"]
        .get(f"/tabular-dataset-snapshots/{snapshot['id']}", headers=r["owner"])
        .status_code
        == 409
    )
    command(r["client"], r["owner"], r["path"], r["body"], key="adopt", expected=409)


def test_no_release_from_adoption_classification_or_changed_snapshot_bypass(dataset):
    r = dataset
    snapshot = adopt(r)
    task = _task(r["state"], r["human"], r["project"]["id"], snapshot["digest"], "Dataset")
    body = _grant_payload(task, r["worker"], snapshot)
    command(
        r["client"],
        r["owner"],
        f"/projects/{r['project']['id']}/execution-grants",
        body,
        key="not-released",
        expected=403,
    )
    command(
        r["client"],
        r["owner"],
        f"/snapshots/{snapshot['id']}/releases",
        {
            "expected_digest": snapshot["digest"],
            "worker_principal_id": r["worker"]["id"],
            "classification": "private",
            "permits_worker_transfer": True,
            "rationale": "invalid classification",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=20)).isoformat(),
        },
        key="private",
        expected=422,
    )
    second = command(
        r["client"],
        r["owner"],
        r["path"],
        upload_body(DATA.replace(b"a,1,2", b"a,1,9")),
        key="second-data",
    )
    release(r, second)
    command(
        r["client"],
        r["owner"],
        f"/projects/{r['project']['id']}/execution-grants",
        {**body, "snapshot_id": second["id"]},
        key="changed-input-target",
        expected=409,
    )
