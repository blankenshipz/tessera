from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tessera_control.snapshot_releases import (
    SnapshotReleaseStore,
    authorize_snapshot,
)
from tessera_control.store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    StoreError,
    _connect,
    canonical_digest,
    initialize_state,
)
from tessera_control.workspace import capture_workspace, inventory_digest


def _owner_token(state: Path) -> str:
    return (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()


def _setup(
    tmp_path: Path,
) -> tuple[Path, SnapshotReleaseStore, dict[str, Any], dict[str, Any], dict[str, Any]]:
    state, root = tmp_path / "state", tmp_path / "workspace"
    root.mkdir(parents=True)
    (root / "input.txt").write_bytes(b"synthetic input")
    initialize_state(state)
    store = SnapshotReleaseStore(state)
    owner = store.authenticate(_owner_token(state))
    project_body = {"name": "Released synthetic input"}
    _, project = store.create_project(
        owner, "project", canonical_digest(project_body), project_body
    )
    capture = capture_workspace(root.absolute())
    _, snapshot = store.adopt_snapshot(
        owner, project["id"], "snapshot", inventory_digest(capture.inventory), capture
    )
    return state, store, owner, project, snapshot


def _worker(
    store: SnapshotReleaseStore,
    owner: dict[str, Any],
    project_id: str,
    key: str,
    *,
    expires_at: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    expiry = expires_at or datetime.now(UTC) + timedelta(hours=2)
    token = (key.replace("-", "a") + "x" * 43)[:43]
    body = {
        "kind": "worker",
        "audience": "worker",
        "project_id": project_id,
        "expires_at": expiry.isoformat().replace("+00:00", "Z"),
    }
    _, issued = store.issue_credential(owner, key, canonical_digest(body), body, token)
    return store.authenticate_worker(token), issued


def _release_body(
    snapshot: dict[str, Any], worker: dict[str, Any], **overrides: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "expected_digest": snapshot["digest"],
        "worker_principal_id": worker["id"],
        "classification": "public",
        "permits_worker_transfer": True,
        "rationale": "Human review permits this bounded synthetic worker transfer.",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    }
    body.update(overrides)
    return body


def _release(
    store: SnapshotReleaseStore,
    owner: dict[str, Any],
    snapshot: dict[str, Any],
    body: dict[str, Any],
    key: str,
) -> tuple[int, dict[str, Any]]:
    return store.release_snapshot(owner, snapshot["id"], key, canonical_digest(body), body)


def _authorization(
    state: Path,
    actor: dict[str, Any],
    snapshot: dict[str, Any],
    worker: dict[str, Any],
) -> dict[str, Any]:
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute("BEGIN")
        return authorize_snapshot(
            connection, actor, snapshot["id"], worker["id"], snapshot["digest"]
        )


def test_release_requires_explicit_public_transfer_and_human_review(tmp_path: Path) -> None:
    _, store, owner, project, snapshot = _setup(tmp_path)
    worker, _ = _worker(store, owner, project["id"], "worker")
    for invalid in (
        {"classification": "unclassified"},
        {"permits_worker_transfer": False},
        {"missing": "classification"},
        {"missing": "permits_worker_transfer"},
    ):
        body = _release_body(snapshot, worker)
        body.update(invalid)
        missing = body.pop("missing", None)
        if missing is not None:
            body.pop(missing)
        with pytest.raises(StoreError, match="Invalid snapshot release"):
            _release(store, owner, snapshot, body, f"invalid-{next(iter(invalid))}")

    agent_body = {
        "kind": "agent",
        "audience": "control",
        "project_id": project["id"],
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    agent_token = "z" * 43
    _, _ = store.issue_credential(
        owner, "agent", canonical_digest(agent_body), agent_body, agent_token
    )
    agent = store.authenticate(agent_token)
    with pytest.raises(Forbidden, match="human"):
        _release(store, agent, snapshot, _release_body(snapshot, worker), "agent-release")


def test_release_checks_digest_worker_scope_and_identity_expiry(tmp_path: Path) -> None:
    _, store, owner, project, snapshot = _setup(tmp_path)
    worker, _ = _worker(store, owner, project["id"], "worker")
    wrong_digest = _release_body(snapshot, worker, expected_digest="0" * 64)
    with pytest.raises(Conflict, match="digest"):
        _release(store, owner, snapshot, wrong_digest, "wrong-digest")

    other_body = {"name": "Other project"}
    _, other_project = store.create_project(
        owner, "other-project", canonical_digest(other_body), other_body
    )
    foreign_worker, _ = _worker(store, owner, other_project["id"], "foreign-worker")
    with pytest.raises(NotFound):
        _release(
            store,
            owner,
            snapshot,
            _release_body(snapshot, foreign_worker),
            "foreign-worker-release",
        )

    short_worker, _ = _worker(
        store,
        owner,
        project["id"],
        "short-worker",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    with pytest.raises(StoreError, match="outlive"):
        _release(
            store,
            owner,
            snapshot,
            _release_body(
                snapshot,
                short_worker,
                expires_at=(datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
            ),
            "outlive-worker",
        )


def test_release_replay_is_single_event_and_event_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, store, owner, project, snapshot = _setup(tmp_path)
    worker, _ = _worker(store, owner, project["id"], "worker")
    body = _release_body(snapshot, worker)
    first = _release(store, owner, snapshot, body, "same")
    assert first == _release(store, owner, snapshot, body, "same")
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM snapshot_releases").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM events WHERE event_type='workspace.snapshot_released'"
            ).fetchone()[0]
            == 1
        )

    second_worker, _ = _worker(store, owner, project["id"], "second-worker")
    failing = _release_body(snapshot, second_worker)

    def fail_event(*_: object) -> None:
        raise sqlite3.OperationalError("injected release event failure")

    with monkeypatch.context() as patch:
        patch.setattr(Store, "_event", staticmethod(fail_event))
        with pytest.raises(sqlite3.OperationalError, match="injected release"):
            _release(store, owner, snapshot, failing, "event-failure")
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM snapshot_releases").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM idempotency WHERE path LIKE '%/releases'"
            ).fetchone()[0]
            == 1
        )


def test_authorization_rechecks_retained_bytes_issuer_worker_revocation_and_expiry(
    tmp_path: Path,
) -> None:
    state, store, owner, project, snapshot = _setup(tmp_path)
    worker, worker_credential = _worker(store, owner, project["id"], "worker")
    _, release = _release(store, owner, snapshot, _release_body(snapshot, worker), "release")
    assert _authorization(state, owner, snapshot, worker)["status"] == "active"

    revoke_body = {"expected_revision": 1}
    status, revoked = store.revoke_snapshot_release(
        owner,
        release["id"],
        "revoke",
        canonical_digest(revoke_body),
        revoke_body["expected_revision"],
    )
    assert status == 200 and revoked["status"] == "revoked"
    with pytest.raises(Forbidden, match="no live release"):
        _authorization(state, owner, snapshot, worker)

    worker, worker_credential = _worker(store, owner, project["id"], "revoked-worker")
    _release(store, owner, snapshot, _release_body(snapshot, worker), "revoked-worker-release")

    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE credentials SET revoked_at=? WHERE id=?",
            ("2020-01-01T00:00:00Z", worker_credential["credential_id"]),
        )
    with pytest.raises(Forbidden):
        _authorization(state, owner, snapshot, worker)

    expiry_worker, expiry_credential = _worker(store, owner, project["id"], "expiry-worker")
    _release(store, owner, snapshot, _release_body(snapshot, expiry_worker), "expiry-release")
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE credentials SET expires_at=? WHERE id=?",
            ("2020-01-01T00:00:00Z", expiry_credential["credential_id"]),
        )
    with pytest.raises(Forbidden):
        _authorization(state, owner, snapshot, expiry_worker)

    intact_worker, _ = _worker(store, owner, project["id"], "intact-worker")
    _release(store, owner, snapshot, _release_body(snapshot, intact_worker), "intact-release")
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE snapshot_files SET content=? WHERE snapshot_id=?",
            (b"corrupt", snapshot["id"]),
        )
    with pytest.raises(Conflict, match="integrity"):
        _authorization(state, owner, snapshot, intact_worker)

    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE principals SET revoked_at=? WHERE id=?",
            ("2020-01-01T00:00:00Z", owner["id"]),
        )
    with pytest.raises(Forbidden):
        _authorization(state, owner, snapshot, intact_worker)
