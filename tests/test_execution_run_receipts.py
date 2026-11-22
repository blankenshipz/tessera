"""Observed-provider run receipts stay disjoint from fake-provider evidence."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_artifact_disposition_authority import _other_tenant_headers
from test_result_acceptances import _checkpoint_with_receipt, _request
from test_result_checkpoints import _checkpoint_request, _reconciled_manifest

from tessera_control import execution_run_receipts
from tessera_control.api import create_app
from tessera_control.artifact_manifests import ArtifactManifestStore
from tessera_control.artifacts import ArtifactStore
from tessera_control.execution_run_receipt_api import router as run_receipt_router
from tessera_control.execution_run_receipts import ExecutionRunReceiptStore, argv_digest
from tessera_control.result_acceptances import ResultAcceptanceStore
from tessera_control.result_checkpoints import ResultCheckpointStore
from tessera_control.store import (
    Conflict,
    Forbidden,
    NotFound,
    StoreError,
    _connect,
    canonical_digest,
    utc_now,
)

DB = "tessera.sqlite3"


def _owner_token(state: Path) -> str:
    return (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()


def _subdir(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


def _insert_operation(state: Path, owner, grant, lease, checkpoint, adapter: str) -> None:
    now = utc_now()
    with closing(_connect(state / DB)) as connection:
        connection.execute(
            """INSERT INTO external_operations
            (operation_id, org_id, project_id, task_id, grant_id, lease_id,
            worker_principal_id, epoch, package_digest, adapter_version, profile_version,
            docker_lookup_key, request_digest, retry_class, desired_state, observed_state,
            provider_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'local-cpu-v1', ?, ?,
            'manual_reconciliation', 'admitted', 'exited', NULL, ?, ?)""",
            (
                checkpoint["operation_id"],
                owner["org_id"],
                grant["project_id"],
                grant["task_id"],
                grant["id"],
                lease["id"],
                lease["worker_principal_id"],
                lease["epoch"],
                "a" * 64,
                adapter,
                f"lookup-{checkpoint['operation_id']}",
                "b" * 64,
                now,
                now,
            ),
        )


def _set_grant_status(state: Path, grant_id: str, status: str) -> None:
    with closing(_connect(state / DB)) as connection:
        connection.execute("UPDATE execution_grants SET status=? WHERE id=?", (status, grant_id))


# Control-recorded facts a matching receipt must cite, keyed by lease id.
_EXPECTED: dict[str, dict[str, str]] = {}


_MISSING = object()


def _run_body(lease, payload_overrides=None, **overrides):
    started = datetime.now(UTC)
    expected = _EXPECTED.get(
        lease["id"],
        {
            "operation_id": "0" * 32,
            "plan_digest": "p" * 64,
            "command_digest": "c" * 64,
            "output_digest": "d" * 64,
        },
    )
    payload = {
        "exit": 3,
        "note": "synthetic run observation",
        "operation_id": expected["operation_id"],
        "plan_digest": expected["plan_digest"],
    }
    for name, value in (payload_overrides or {}).items():
        if value is _MISSING:
            payload.pop(name, None)
        else:
            payload[name] = value
    body = {
        "lease_id": lease["id"],
        "expected_epoch": lease["epoch"],
        "provider_id": "container-abc",
        "command_digest": expected["command_digest"],
        "exit_code": 3,
        "started_at": (started + timedelta(seconds=0)).isoformat(),
        "finished_at": (started + timedelta(seconds=2)).isoformat(),
        "output_digest": expected["output_digest"],
        "receipt_payload": payload,
        "receipt_digest": canonical_digest(payload),
    }
    return body | overrides


def _report(state, worker, operation_id, body, key="run"):
    store = ExecutionRunReceiptStore(state)
    return store.report_run_receipt(worker, operation_id, key, canonical_digest(body), body)


def _observed_fixture(tmp_path: Path, adapter: str = "docker-local-cpu-v1"):
    """A reconciled checkpoint whose operation has no receipt yet.

    Receipts are only recordable while the grant is unresolved, so the grant is
    held 'active' for the report and restored to 'reconciled' afterwards, which
    models the real order (receipt before reconciliation) for the fixture.
    """
    state, owner, worker, grant, lease, manifest, first = _reconciled_manifest(tmp_path)
    body = _checkpoint_request(grant, lease, manifest)
    _, checkpoint = ResultCheckpointStore(state).commit(
        owner, grant["id"], "checkpoint", canonical_digest(body), body
    )
    _insert_operation(state, owner, grant, lease, checkpoint, adapter)
    with closing(_connect(state / DB)) as connection:
        grant_row = connection.execute(
            "SELECT * FROM execution_grants WHERE id=?", (grant["id"],)
        ).fetchone()
        plan, _ = ArtifactStore._plan(connection, grant_row)
    assert manifest["operation_id"] == checkpoint["operation_id"]
    _EXPECTED[lease["id"]] = {
        "operation_id": checkpoint["operation_id"],
        "plan_digest": grant_row["plan_digest"],
        "command_digest": argv_digest(plan["execution_spec"]["argv"]),
        "output_digest": manifest["manifest_digest"],
    }
    return state, owner, worker, grant, lease, checkpoint


def _report_unresolved(state, worker, grant, checkpoint, body, key="run"):
    _set_grant_status(state, grant["id"], "active")
    try:
        return _report(state, worker, checkpoint["operation_id"], body, key)
    finally:
        _set_grant_status(state, grant["id"], "reconciled")


def test_run_receipt_derives_kind_duration_and_fixed_labels(tmp_path: Path) -> None:
    state, owner, worker, grant, lease, checkpoint = _observed_fixture(tmp_path)
    op = checkpoint["operation_id"]
    body = _run_body(lease)
    status, receipt = _report_unresolved(state, worker, grant, checkpoint, body)
    assert status == 201
    assert receipt["provider_kind"] == "local_docker_cpu"
    assert receipt["adapter_version"] == "docker-local-cpu-v1"
    assert receipt["duration_ms"] == 2000
    assert receipt["exit_code"] == 3
    assert receipt["receipt_evidence"] == "observed_provider_execution"
    assert receipt["provider_success"] == "not_established"
    assert receipt["scientific_validity"] == "not_assessed"
    with closing(_connect(state / DB)) as connection:
        operation = connection.execute(
            "SELECT provider_id, observed_state FROM external_operations WHERE operation_id=?",
            (op,),
        ).fetchone()
        # The worker's exited observation owns observed_state; the receipt only binds identity.
        assert tuple(operation) == ("container-abc", "exited")
        assert (
            connection.execute(
                "SELECT count(*) FROM events WHERE event_type=?",
                ("worker.execution_run_receipt_recorded",),
            ).fetchone()[0]
            == 1
        )
    # Exact replay and a new key with identical content are idempotent.
    assert _report_unresolved(state, worker, grant, checkpoint, body)[1] == receipt
    status, again = _report_unresolved(state, worker, grant, checkpoint, body, key="other")
    assert status == 200 and again == receipt
    changed = _run_body(
        lease, exit_code=0, started_at=body["started_at"], finished_at=body["finished_at"]
    )
    with pytest.raises(Conflict):
        _report_unresolved(state, worker, grant, checkpoint, changed, key="third")
    assert (
        ExecutionRunReceiptStore(state).get_run_receipt(owner, op)["receipt_digest"]
        == (body["receipt_digest"])
    )


def test_client_cannot_choose_provider_kind_or_adapter(tmp_path: Path) -> None:
    state, _, worker, grant, lease, checkpoint = _observed_fixture(tmp_path)
    for extra in (
        {"provider_kind": "fake"},
        {"provider_kind": "remote_salad"},
        {"adapter_version": "x"},
        {"receipt_evidence": "observed_provider_execution"},
    ):
        with pytest.raises(StoreError):
            _report_unresolved(state, worker, grant, checkpoint, _run_body(lease) | extra)


def test_fake_and_run_receipts_are_mutually_exclusive(tmp_path: Path) -> None:
    state, owner, worker, grant, lease, manifest, _f, checkpoint, _d = _checkpoint_with_receipt(
        tmp_path
    )
    # Fake receipt present: run receipt is refused (adapter here is unmapped too,
    # so use a mapped adapter to prove the exclusion itself).
    with closing(_connect(state / DB)) as connection:
        connection.execute(
            "UPDATE external_operations SET adapter_version='docker-local-cpu-v1', provider_id=NULL"
        )
    with pytest.raises(Conflict, match="fake provider evidence"):
        _report_unresolved(state, worker, grant, checkpoint, _run_body(lease))
    # The trigger also aborts a raw run-receipt insert.
    with closing(_connect(state / DB)) as connection, pytest.raises(sqlite3.IntegrityError):
        _raw_run_insert(connection, checkpoint["operation_id"], grant, lease, owner)

    state2, owner2, worker2, grant2, lease2, checkpoint2 = _observed_fixture(_subdir(tmp_path, "b"))
    _report_unresolved(state2, worker2, grant2, checkpoint2, _run_body(lease2))
    now = utc_now()
    with closing(_connect(state2 / DB)) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """INSERT INTO external_operation_receipts
            (operation_id, provider_kind, provider_version, provider_id, receipt_payload_json,
            receipt_digest, observed_state, observed_at, termination_confirmed, created_at,
            updated_at) VALUES (?, 'fake', 'v', 'p', '{}', ?, 'exited', ?, 1, ?, ?)""",
            (checkpoint2["operation_id"], "e" * 64, now, now, now),
        )


def _raw_run_insert(connection, operation_id, grant, lease, owner, **overrides):
    row = {
        "operation_id": operation_id,
        "org_id": owner["org_id"],
        "project_id": grant["project_id"],
        "task_id": grant["task_id"],
        "grant_id": grant["id"],
        "lease_id": lease["id"],
        "epoch": lease["epoch"],
        "worker_principal_id": lease["worker_principal_id"],
        "provider_kind": "local_docker_cpu",
        "receipt_evidence": "observed_provider_execution",
        "adapter_version": "docker-local-cpu-v1",
        "provider_id": "raw-provider",
        "command_digest": "c" * 64,
        "exit_code": 0,
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z",
        "duration_ms": 1000,
        "output_digest": "d" * 64,
        "receipt_payload_json": "{}",
        "receipt_digest": "e" * 64,
        "provider_success": "not_established",
        "scientific_validity": "not_assessed",
        "created_at": utc_now(),
    } | overrides
    columns = ", ".join(row)
    marks = ", ".join(f":{name}" for name in row)
    connection.execute(f"INSERT INTO execution_run_receipts ({columns}) VALUES ({marks})", row)


def test_run_receipt_fences_and_validation(tmp_path: Path) -> None:
    state, owner, worker, grant, lease, checkpoint = _observed_fixture(tmp_path)
    op = checkpoint["operation_id"]
    good = _run_body(lease)
    for bad, error in (
        (_run_body(lease, expected_epoch=lease["epoch"] + 1), Conflict),
        (_run_body(lease, lease_id="other-lease"), Conflict),
        (_run_body(lease, receipt_digest="0" * 64), Conflict),
        (_run_body(lease, command_digest="xyz"), StoreError),
        (_run_body(lease, exit_code=2**31), StoreError),
        (_run_body(lease, started_at="2026-01-01T00:00:00"), StoreError),
        (
            _run_body(
                lease,
                started_at=good["finished_at"],
                finished_at=good["started_at"],
            ),
            Conflict,
        ),
        (
            _run_body(
                lease,
                started_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
                finished_at=datetime.now(UTC).isoformat(),
            ),
            Conflict,
        ),
        (
            _run_body(
                lease,
                started_at=datetime.now(UTC).isoformat(),
                finished_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            ),
            Conflict,
        ),
    ):
        with pytest.raises(error):
            _report_unresolved(state, worker, grant, checkpoint, bad)
    # Resolved grant: no receipt authority.
    with pytest.raises(Conflict):
        _report(state, worker, op, good)
    # Wrong worker sees nothing.
    with pytest.raises(Forbidden):
        _report(state, owner, op, good)
    with pytest.raises(NotFound):
        _report(state, worker, "0" * 32, good)
    with closing(_connect(state / DB)) as connection:
        assert connection.execute("SELECT count(*) FROM execution_run_receipts").fetchone()[0] == 0


def test_unmapped_adapter_is_rejected(tmp_path: Path) -> None:
    state, _, worker, grant, lease, checkpoint = _observed_fixture(
        tmp_path, adapter="development-local-cpu-v1"
    )
    with pytest.raises(Conflict, match="No observed-provider receipt mapping"):
        _report_unresolved(state, worker, grant, checkpoint, _run_body(lease))


def test_run_receipt_rows_are_immutable_and_checks_reject_fake_labels(tmp_path: Path) -> None:
    state, owner, worker, grant, lease, checkpoint = _observed_fixture(tmp_path)
    op = checkpoint["operation_id"]
    _report_unresolved(state, worker, grant, checkpoint, _run_body(lease))
    with closing(_connect(state / DB)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE execution_run_receipts SET exit_code=0")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM execution_run_receipts")
    # CHECKs on a fresh operation id (trigger/PK order cannot mask the CHECK).
    for overrides in (
        {"provider_kind": "fake"},
        {"receipt_evidence": "fake_provider_development_only"},
    ):
        with (
            closing(_connect(state / DB)) as connection,
            pytest.raises(sqlite3.IntegrityError, match="CHECK"),
        ):
            _raw_run_insert(connection, op + "x", grant, lease, owner, **overrides)


def test_cross_org_human_cannot_read_run_receipt(tmp_path: Path) -> None:
    state, owner, worker, grant, lease, checkpoint = _observed_fixture(tmp_path)
    op = checkpoint["operation_id"]
    _report_unresolved(state, worker, grant, checkpoint, _run_body(lease))
    app = create_app(state)
    app.include_router(run_receipt_router)  # the coordinator wires this in api.py
    client = TestClient(app)
    ok = client.get(
        f"/external-operations/{op}/run-receipt",
        headers={"Authorization": f"Bearer {_owner_token(state)}"},
    )
    assert ok.status_code == 200
    assert ok.json()["receipt_evidence"] == "observed_provider_execution"
    other = client.get(
        f"/external-operations/{op}/run-receipt", headers=_other_tenant_headers(state)
    )
    assert other.status_code == 404


def test_acceptance_over_run_receipt_uses_observed_ledger(tmp_path: Path) -> None:
    state, owner, worker, grant, lease, checkpoint = _observed_fixture(tmp_path)
    _, receipt = _report_unresolved(state, worker, grant, checkpoint, _run_body(lease))
    store = ResultAcceptanceStore(state)
    body = _request(checkpoint, receipt["receipt_digest"])
    wrong = _request(checkpoint, "f" * 64)
    with pytest.raises(Conflict):
        store.record(owner, checkpoint["id"], "wrong", canonical_digest(wrong), wrong)
    status, record = store.record(owner, checkpoint["id"], "a", canonical_digest(body), body)
    assert status == 201
    assert record["evidence_label"] == "provider_observed_unassessed"
    assert record["receipt_evidence"] == "observed_provider_execution"
    assert record["provider_kind"] == "local_docker_cpu"
    assert record["exit_code"] == 3
    assert record["provider_success"] == "not_established"
    assert record["scientific_validity"] == "not_assessed"
    retract = _request(checkpoint, receipt["receipt_digest"], action="retract", revision=1)
    status, second = store.record(owner, checkpoint["id"], "b", canonical_digest(retract), retract)
    assert second["revision"] == 2 and second["exit_code"] == 3
    listed = store.list_records(owner, checkpoint["id"], 0, 10)
    assert [row["revision"] for row in listed] == [1, 2]
    assert all(row["evidence_label"] == "provider_observed_unassessed" for row in listed)
    with closing(_connect(state / DB)) as connection:
        assert (
            connection.execute("SELECT count(*) FROM execution_result_acceptances").fetchone()[0]
            == 0
        )


def test_acceptance_over_fake_receipt_stays_fake_labelled(tmp_path: Path) -> None:
    state, owner, _w, _g, _l, _m, _f, checkpoint, digest = _checkpoint_with_receipt(tmp_path)
    store = ResultAcceptanceStore(state)
    body = _request(checkpoint, digest)
    _, record = store.record(owner, checkpoint["id"], "a", canonical_digest(body), body)
    assert record["evidence_label"] == "development_synthetic_only"
    assert record["receipt_evidence"] == "fake_provider_development_only"
    assert "provider_kind" not in record
    with closing(_connect(state / DB)) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM execution_observed_result_acceptances"
            ).fetchone()[0]
            == 0
        )


def test_run_receipt_never_asserts_termination_for_the_operation(tmp_path: Path) -> None:
    state, owner, worker, grant, lease, checkpoint = _observed_fixture(tmp_path)
    op = checkpoint["operation_id"]
    with closing(_connect(state / DB)) as connection:
        connection.execute(
            "UPDATE external_operations SET observed_state='running' WHERE operation_id=?", (op,)
        )
    status, _ = _report_unresolved(state, worker, grant, checkpoint, _run_body(lease))
    assert status == 201
    with closing(_connect(state / DB)) as connection:
        assert (
            connection.execute(
                "SELECT observed_state FROM external_operations WHERE operation_id=?", (op,)
            ).fetchone()[0]
            == "running"
        )


def _assert_refused(state, worker, grant, checkpoint, body, key="neg") -> None:
    with pytest.raises(Conflict) as refused:
        _report_unresolved(state, worker, grant, checkpoint, body, key)
    assert str(refused.value) == "Run receipt does not match control's recorded operation."
    with closing(_connect(state / DB)) as connection:
        assert connection.execute("SELECT count(*) FROM execution_run_receipts").fetchone()[0] == 0


def test_run_receipt_must_cite_the_operation_and_plan_control_recorded(tmp_path: Path) -> None:
    state, _, worker, grant, lease, checkpoint = _observed_fixture(tmp_path)
    for name, value in (
        ("operation_id", "f" * 32),
        ("plan_digest", "f" * 64),
        ("operation_id", 7),
        ("plan_digest", None),
        ("operation_id", _MISSING),
        ("plan_digest", _MISSING),
    ):
        body = _run_body(lease, payload_overrides={name: value})
        _assert_refused(state, worker, grant, checkpoint, body)
    both = _run_body(lease, payload_overrides={"operation_id": _MISSING, "plan_digest": _MISSING})
    _assert_refused(state, worker, grant, checkpoint, both)


def test_run_receipt_command_and_output_digests_must_equal_control_computed_values(
    tmp_path: Path,
) -> None:
    state, _, worker, grant, lease, checkpoint = _observed_fixture(tmp_path)
    for override in ({"command_digest": "9" * 64}, {"output_digest": "9" * 64}):
        # A worker-signed digest of its own (wrong) claim still passes the self-digest check.
        _assert_refused(state, worker, grant, checkpoint, _run_body(lease, **override))
    expected = _EXPECTED[lease["id"]]
    # The worker's local seal digest is not control's manifest digest.
    sealed = _run_body(
        lease, payload_overrides={"sealed_manifest_digest": expected["output_digest"]},
        output_digest="8" * 64,
    )  # fmt: skip
    _assert_refused(state, worker, grant, checkpoint, sealed)


def test_run_receipt_is_refused_without_exactly_one_matching_control_manifest(
    tmp_path: Path,
) -> None:
    for index, (sql, params) in enumerate(
        (
            ("DELETE FROM artifact_manifests", ()),
            ("UPDATE artifact_manifests SET operation_id=?", ("e" * 32,)),
            ("UPDATE artifact_manifests SET project_id='project_other'", ()),
            ("UPDATE artifact_manifests SET epoch=epoch+1", ()),
            ("UPDATE artifact_manifests SET lease_id='lease_other'", ()),
            ("UPDATE artifact_manifests SET task_id='task_other'", ()),
            ("UPDATE artifact_manifests SET grant_id='grant_other'", ()),
            ("UPDATE artifact_manifests SET worker_principal_id='principal_other'", ()),
            ("UPDATE artifact_manifests SET plan_digest=?", ("f" * 64,)),
        )
    ):
        state, _, worker, grant, lease, checkpoint = _observed_fixture(
            _subdir(tmp_path, f"m{index}")
        )
        _mutate_manifest_unchecked(state, sql, params)
        _assert_refused(state, worker, grant, checkpoint, _run_body(lease))


def _mutate_manifest_unchecked(state: Path, sql: str, params: tuple) -> None:
    with closing(_connect(state / DB)) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='artifact_manifests'"
        ).fetchall():
            connection.execute(f"DROP TRIGGER {name}")
        connection.execute(sql, params)
        connection.commit()


def test_run_receipt_replay_must_match_stored_receipt_and_control_records(
    tmp_path: Path,
) -> None:
    state, _, worker, grant, lease, checkpoint = _observed_fixture(tmp_path)
    body = _run_body(lease)
    assert _report_unresolved(state, worker, grant, checkpoint, body)[0] == 201
    assert _report_unresolved(state, worker, grant, checkpoint, body, "again")[0] == 200
    changed = _run_body(lease, output_digest="9" * 64)
    with pytest.raises(Conflict) as refused:
        _report_unresolved(state, worker, grant, checkpoint, changed, "changed")
    assert "9" * 64 not in str(refused.value)
    # An exact replay is also re-verified: it is refused once control's record disagrees.
    _mutate_manifest_unchecked(
        state, "UPDATE artifact_manifests SET manifest_digest=?", ("7" * 64,)
    )
    with pytest.raises(Conflict):
        _report_unresolved(state, worker, grant, checkpoint, body, "after-tamper")


def test_manifest_schema_makes_a_second_manifest_per_lease_or_operation_impossible(
    tmp_path: Path,
) -> None:
    """lease_id is UNIQUE and each operation owns exactly one lease (external_operations
    has UNIQUE operation_id and UNIQUE lease_id), so a re-upload cannot add a second row."""
    state, *_ = _observed_fixture(tmp_path)
    with closing(_connect(state / DB)) as connection:
        row = connection.execute("SELECT * FROM artifact_manifests").fetchone()
        columns = [c[0] for c in connection.execute("SELECT * FROM artifact_manifests").description]
        clone = dict(zip(columns, tuple(row), strict=True))
        clone["id"] = "artifact_manifest_second"
        names = ",".join(clone)
        marks = ",".join(f":{name}" for name in clone)
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE.*lease_id"):
            connection.execute(f"INSERT INTO artifact_manifests ({names}) VALUES ({marks})", clone)
        assert connection.execute("SELECT count(*) FROM artifact_manifests").fetchone()[0] == 1
        # external_operations: one operation per lease and per operation id.
        index_cols = {
            tuple(i[2] for i in connection.execute(f"PRAGMA index_info({idx[1]})"))
            for idx in connection.execute("PRAGMA index_list(external_operations)")
            if idx[2]
        }
        assert ("lease_id",) in index_cols and ("operation_id",) in index_cols


def test_run_receipt_is_refused_if_more_than_one_manifest_matches_the_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, _, worker, grant, lease, checkpoint = _observed_fixture(tmp_path)
    real = execution_run_receipts._manifests_for_operation

    def two_rows(connection, operation_id, org_id):
        rows = real(connection, operation_id, org_id)
        assert len(rows) == 1  # the database itself never holds a second one
        return rows + rows

    monkeypatch.setattr(execution_run_receipts, "_manifests_for_operation", two_rows)
    # The single real manifest would have matched; the duplicate alone causes the refusal.
    _assert_refused(state, worker, grant, checkpoint, _run_body(lease))
    monkeypatch.setattr(execution_run_receipts, "_manifests_for_operation", real)
    assert _report_unresolved(state, worker, grant, checkpoint, _run_body(lease))[0] == 201


def test_manifest_re_upload_after_a_worker_restart_is_idempotent_and_receipt_still_accepted(
    tmp_path: Path,
) -> None:
    state, _, worker, grant, lease, checkpoint = _observed_fixture(tmp_path)
    with closing(_connect(state / DB)) as connection:
        before = connection.execute("SELECT id, manifest_digest FROM artifact_manifests").fetchall()
        stored = connection.execute("SELECT artifacts_json FROM artifact_manifests").fetchone()[0]
    assert len(before) == 1
    artifacts = json.loads(stored)
    body = {
        "expected_epoch": lease["epoch"],
        "operation_id": checkpoint["operation_id"],
        "artifacts": artifacts,
    }
    # A restarted worker replays the manifest with a fresh idempotency key.
    _set_grant_status(state, grant["id"], "active")
    try:
        status, replayed = ArtifactManifestStore(state).create_manifest(
            worker, lease["id"], "after-restart", canonical_digest(body), body
        )
    finally:
        _set_grant_status(state, grant["id"], "reconciled")
    assert status == 200
    assert replayed["manifest_digest"] == _EXPECTED[lease["id"]]["output_digest"]
    with closing(_connect(state / DB)) as connection:
        after = connection.execute("SELECT id, manifest_digest FROM artifact_manifests").fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]
    assert _report_unresolved(state, worker, grant, checkpoint, _run_body(lease))[0] == 201


def test_run_receipt_trust_fields_are_recorded_at_lease_time_not_recomputed(
    tmp_path: Path, trust_policy
) -> None:
    from tessera_control.execution_run_receipts import NOT_RECORDED, _lease_trust_fields

    state, owner, worker, grant, lease, checkpoint = _observed_fixture(tmp_path)
    with closing(_connect(state / DB)) as connection:
        (event,) = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload_json FROM events WHERE event_type='execution.lease_acquired' "
                "AND json_extract(payload_json,'$.lease_id')=?",
                (lease["id"],),
            )
        ]
        recorded = {
            key: event[key]
            for key in ("trust_provider_id", "trust_policy_digest", "admission_basis")
        }
        assert recorded["trust_policy_digest"]  # the setting in force at lease time
        # A setting installed after the lease must not leak into the receipt.
        trust_policy({"salad": (["public", "private_derived"], [worker["id"]])})
        org_id = connection.execute("SELECT org_id FROM principals LIMIT 1").fetchone()[0]
        assert _lease_trust_fields(connection, org_id, lease["id"]) == recorded
        # A lease without the record (older than v65) is reported, not computed.
        assert _lease_trust_fields(connection, org_id, "lease_missing") == dict.fromkeys(
            recorded, NOT_RECORDED
        )
    _report_unresolved(state, worker, grant, checkpoint, _run_body(lease))
    receipt = ExecutionRunReceiptStore(state).get_run_receipt(owner, checkpoint["operation_id"])
    assert {k: receipt[k] for k in recorded} == recorded
