from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tessera_control.snapshots import SnapshotStore
from tessera_control.store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    _connect,
    _token_hash,
    canonical_digest,
    initialize_state,
)
from tessera_control.workspace import CapturedWorkspace, capture_workspace, inventory_digest


def _owner_token(state_dir: Path) -> str:
    header = (state_dir / "owner.header").read_text().strip()
    return header.split(": ", 1)[1].removeprefix("Bearer ")


def _setup(tmp_path: Path) -> tuple[Path, SnapshotStore, dict[str, object], dict[str, object]]:
    state = tmp_path / "state"
    initialize_state(state)
    store = SnapshotStore(state)
    owner = store.authenticate(_owner_token(state))
    status, project = store.create_project(
        owner, "project", canonical_digest({"name": "Snapshots"}), {"name": "Snapshots"}
    )
    assert status == 201
    return state, store, owner, project


def _capture(
    tmp_path: Path, name: str = "input.txt", content: bytes = b"before"
) -> CapturedWorkspace:
    root = tmp_path / "workspace"
    root.mkdir(parents=True)
    (root / name).write_bytes(content)
    return capture_workspace(root.absolute())


def _agent(
    state: Path, owner: dict[str, object], project_id: str, token: str = "agent-token"
) -> dict[str, object]:
    created = datetime.now(UTC)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            """INSERT INTO principals (id, org_id, role, token_hash, revoked_at, created_at)
            VALUES ('snapshot-agent', 'org_local', 'agent', ?, NULL, ?)""",
            (_token_hash(token), created.isoformat().replace("+00:00", "Z")),
        )
        connection.execute(
            """INSERT INTO credentials
            (id, principal_id, kind, audience, project_id, parent_id, expires_at,
             revoked_at, created_at, created_by_principal_id)
            VALUES ('snapshot-agent-credential', 'snapshot-agent', 'agent', 'control', ?, ?, ?,
            NULL, ?, ?)""",
            (
                project_id,
                owner["credential_id"],
                (created + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                created.isoformat().replace("+00:00", "Z"),
                owner["id"],
            ),
        )
    return Store(state).authenticate(token)


def test_adoption_retains_captured_bytes_after_source_edit_and_restart(tmp_path: Path) -> None:
    state, store, owner, project = _setup(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "input.txt"
    source.write_bytes(b"before")
    capture = capture_workspace(root.absolute())
    digest = inventory_digest(capture.inventory)
    status, snapshot = store.adopt_snapshot(owner, project["id"], "adopt", digest, capture)
    assert status == 201
    source.write_bytes(b"after")
    restarted = SnapshotStore(state)
    response = restarted.get_snapshot(owner, snapshot["id"])
    assert response["digest"] == digest
    assert response["manifest"] == snapshot["manifest"]
    assert "content" not in response
    verified = restarted.verify_snapshot(owner, snapshot["id"])
    assert verified["integrity"] == "verified"
    with _connect(state / "tessera.sqlite3") as connection:
        retained = connection.execute(
            "SELECT content FROM snapshot_files WHERE snapshot_id = ?", (snapshot["id"],)
        ).fetchone()[0]
    assert retained == b"before"


def test_digest_mismatch_and_event_failure_leave_no_snapshot_or_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, store, owner, project = _setup(tmp_path)
    capture = _capture(tmp_path)
    with pytest.raises(Conflict, match="changed"):
        store.adopt_snapshot(owner, project["id"], "wrong", "0" * 64, capture)
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM source_snapshots").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM snapshot_files").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM events WHERE event_type = 'workspace.snapshot_adopted'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM idempotency WHERE path LIKE '%snapshots'"
            ).fetchone()[0]
            == 0
        )

    def fail_event(*_: object) -> None:
        raise sqlite3.OperationalError("injected snapshot event failure")

    digest = inventory_digest(capture.inventory)
    with monkeypatch.context() as patch:
        patch.setattr(Store, "_event", staticmethod(fail_event))
        with pytest.raises(sqlite3.OperationalError, match="injected snapshot"):
            store.adopt_snapshot(owner, project["id"], "event-fail", digest, capture)
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM source_snapshots").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM snapshot_files").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM idempotency WHERE path LIKE '%snapshots'"
            ).fetchone()[0]
            == 0
        )
    assert store.adopt_snapshot(owner, project["id"], "event-fail", digest, capture)[0] == 201


def test_snapshot_replay_is_single_event_and_tampering_denies_verification(tmp_path: Path) -> None:
    state, store, owner, project = _setup(tmp_path)
    capture = _capture(tmp_path)
    digest = inventory_digest(capture.inventory)
    first = store.adopt_snapshot(owner, project["id"], "same", digest, capture)
    replay = store.adopt_snapshot(owner, project["id"], "same", digest, capture)
    assert first == replay
    snapshot_id = first[1]["id"]
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM source_snapshots").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM events WHERE event_type = 'workspace.snapshot_adopted'"
            ).fetchone()[0]
            == 1
        )
        connection.execute(
            "UPDATE snapshot_files SET content = ? WHERE snapshot_id = ?",
            (b"tampered", snapshot_id),
        )
    with pytest.raises(Conflict, match="integrity"):
        store.verify_snapshot(owner, snapshot_id)

    second_capture = _capture(tmp_path / "second", content=b"second")
    _, second = store.adopt_snapshot(
        owner, project["id"], "second", inventory_digest(second_capture.inventory), second_capture
    )
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute("DELETE FROM snapshot_files WHERE snapshot_id = ?", (second["id"],))
    with pytest.raises(Conflict, match="file set"):
        store.verify_snapshot(owner, second["id"])


def test_snapshot_reads_live_auth_scope_and_human_adoption(tmp_path: Path) -> None:
    state, store, owner, first_project = _setup(tmp_path)
    status, second_project = store.create_project(
        owner, "second-project", canonical_digest({"name": "Other"}), {"name": "Other"}
    )
    assert status == 201
    capture = _capture(tmp_path)
    digest = inventory_digest(capture.inventory)
    _, snapshot = store.adopt_snapshot(owner, second_project["id"], "foreign", digest, capture)
    agent = _agent(state, owner, first_project["id"])
    with pytest.raises(NotFound):
        store.get_snapshot(agent, snapshot["id"])
    with pytest.raises(NotFound):
        store.list_snapshots(agent, second_project["id"])
    with pytest.raises(NotFound):
        store.verify_snapshot(agent, snapshot["id"])
    with pytest.raises(Forbidden, match="human"):
        store.adopt_snapshot(agent, first_project["id"], "agent-adopt", digest, capture)

    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE principals SET revoked_at = '2020-01-01T00:00:00Z' WHERE id = ?", (owner["id"],)
        )
    for read in (
        lambda: store.get_snapshot(owner, snapshot["id"]),
        lambda: store.list_snapshots(owner, second_project["id"]),
        lambda: store.verify_snapshot(owner, snapshot["id"]),
    ):
        with pytest.raises(Forbidden):
            read()


@pytest.mark.parametrize("corrupt", ["{", "[]", "{}", '"' + "x" * (1024 * 1024) + '"'])
def test_corrupt_manifest_returns_integrity_conflict(tmp_path: Path, corrupt: str) -> None:
    from fastapi.testclient import TestClient

    from tessera_control.api import create_app

    state, store, owner, project = _setup(tmp_path)
    capture = _capture(tmp_path)
    _, snapshot = store.adopt_snapshot(
        owner, project["id"], "adopt", inventory_digest(capture.inventory), capture
    )
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE source_snapshots SET manifest_json = ? WHERE id = ?", (corrupt, snapshot["id"])
        )
    for read in (store.get_snapshot, store.verify_snapshot):
        with pytest.raises(Conflict, match="manifest integrity"):
            read(owner, snapshot["id"])
    with TestClient(create_app(state)) as client:
        result = client.get(
            f"/snapshots/{snapshot['id']}",
            headers={"Authorization": f"Bearer {_owner_token(state)}"},
        )
    assert result.status_code == 409
    assert result.json() == {"detail": "Snapshot manifest integrity check failed."}
