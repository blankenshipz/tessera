"""Credential-access leases retain only opaque metadata and worker handles."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_input_delivery import _delivery

from tessera_control.api import create_app
from tessera_control.credential_leases import CredentialAccessLeaseStore
from tessera_control.store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    _token_hash,
    canonical_digest,
    migrate_state,
)


def _payload(lease: dict[str, object], **changes: object) -> dict[str, object]:
    return {
        "execution_lease_id": lease["id"],
        "expected_epoch": lease["epoch"],
        "secret_ref": "fake/test/provider-key",
        "audience": "provider-neutral-tool",
        "purpose": "read-a-test-resource",
        "scope": ["resource:example", "action:read"],
        "expires_at": (datetime.now(UTC) + timedelta(seconds=10)).isoformat(),
    } | changes


def _create(tmp_path):
    state, _, owner, worker, grant, lease = _delivery(tmp_path)
    store = CredentialAccessLeaseStore(state)
    payload = _payload(lease)
    status, access = store.create(owner, grant["id"], "access", canonical_digest(payload), payload)
    assert status == 201
    return state, store, owner, worker, grant, lease, access, payload


def test_metadata_only_human_creation_replay_and_worker_handle(tmp_path):
    state, store, owner, worker, grant, lease, access, payload = _create(tmp_path)
    assert access["grant_id"] == grant["id"]
    assert access["execution_lease_id"] == lease["id"]
    assert access["worker_principal_id"] == worker["id"]
    assert access["epoch"] == lease["epoch"]
    assert access["scope"] == payload["scope"] and access["metadata_only"] is True
    assert store.create(owner, grant["id"], "access", canonical_digest(payload), payload) == (
        201,
        access,
    )
    with pytest.raises(Conflict):
        store.create(
            owner,
            grant["id"],
            "access",
            canonical_digest(payload | {"purpose": "different"}),
            payload | {"purpose": "different"},
        )
    with pytest.raises(Conflict, match="within its execution lease"):
        too_late = payload | {"expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
        store.create(owner, grant["id"], "too-late", canonical_digest(too_late), too_late)

    app = create_app(state)
    app.state.fake_secret_backend.put(str(payload["secret_ref"]), "raw-secret-never-persisted")
    client = TestClient(app)
    response = client.post(
        f"/worker/credential-access-leases/{access['id']}/access",
        headers={"Authorization": "Bearer worker-token"},
    )
    assert response.status_code == 200
    handle = response.json()
    assert set(handle) == {"credential_access_lease_id", "handle", "expires_at"}
    assert "raw-secret" not in response.text and "secret_ref" not in response.text
    assert app.state.fake_secret_backend.resolve(handle["handle"]) == "raw-secret-never-persisted"
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        persisted = "\n".join(
            row[0] for row in connection.execute("SELECT payload_json FROM events")
        )
        replies = "\n".join(
            row[0] for row in connection.execute("SELECT response_json FROM idempotency")
        )
    assert "raw-secret-never-persisted" not in persisted + replies
    assert "raw-secret-never-persisted" not in (state / "tessera.sqlite3").read_bytes().decode(
        "latin1"
    )
    assert app.state.fake_secret_backend.access_log[-1]["action"] == "resolved"


def test_expired_handle_does_not_revoke_overlapping_handles_or_lease(tmp_path, monkeypatch):
    state, store, _, worker, _, _, access, _ = _create(tmp_path)
    backend = create_app(state).state.fake_secret_backend
    backend.put("fake/test/provider-key", "raw-value")
    current = store.issue_handle(worker, access["id"], backend)["handle"]

    monkeypatch.setattr("tessera_control.credential_leases.HANDLE_SECONDS", -1)
    stale = store.issue_handle(worker, access["id"], backend)["handle"]
    with pytest.raises(Forbidden, match="no longer current"):
        backend.resolve(stale)

    # Expiring the stale handle is not an authoritative lease revocation.
    assert backend.resolve(current) == "raw-value"
    monkeypatch.setattr("tessera_control.credential_leases.HANDLE_SECONDS", 15)
    retried = store.issue_handle(worker, access["id"], backend)["handle"]
    assert backend.resolve(retried) == "raw-value"


def test_explicit_lease_revoke_invalidates_all_handles(tmp_path):
    state, store, _, worker, _, _, access, _ = _create(tmp_path)
    app = create_app(state)
    backend = app.state.fake_secret_backend
    backend.put("fake/test/provider-key", "raw-value")
    first = store.issue_handle(worker, access["id"], backend)["handle"]
    second = store.issue_handle(worker, access["id"], backend)["handle"]

    owner_token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    response = TestClient(app).post(
        f"/credential-access-leases/{access['id']}/revoke",
        headers={"Authorization": f"Bearer {owner_token}", "Idempotency-Key": "revoke-handles"},
        json={"expected_revision": access["revision"]},
    )
    assert response.status_code == 200
    for handle in (first, second):
        with pytest.raises(Forbidden):
            backend.resolve(handle)
    with pytest.raises(Conflict, match="revoked"):
        store.issue_handle(worker, access["id"], backend)


def test_access_fences_cross_tenant_parent_epoch_expiry_retirement_and_revocation(tmp_path):
    state, store, owner, worker, grant, lease, access, _ = _create(tmp_path)
    app = create_app(state)
    backend = app.state.fake_secret_backend
    backend.put("fake/test/provider-key", "raw-value")
    handle = store.issue_handle(worker, access["id"], backend)["handle"]
    assert backend.resolve(handle) == "raw-value"
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("UPDATE execution_grants SET epoch=epoch+1 WHERE id=?", (grant["id"],))
    with pytest.raises(Forbidden):
        backend.resolve(handle)
    with pytest.raises(Conflict):
        store.renew(
            owner,
            access["id"],
            "renew-fenced",
            "renew-fenced",
            {"expected_revision": access["revision"], "expires_at": access["expires_at"]},
        )

    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE execution_grants SET epoch=? WHERE id=?", (lease["epoch"], grant["id"])
        )
        connection.execute(
            "UPDATE execution_leases SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
            (lease["id"],),
        )
    with pytest.raises(Conflict):
        store.issue_handle(worker, access["id"], backend)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE execution_leases SET expires_at=? WHERE id=?",
            (lease["expires_at"], lease["id"]),
        )
        connection.execute(
            "UPDATE credentials SET revoked_at=? WHERE principal_id=?",
            ("2020-01-01Z", worker["id"]),
        )
    with pytest.raises(Forbidden):
        store.issue_handle(worker, access["id"], backend)


def test_access_lease_expiry_and_restart_do_not_persist_handles_or_secrets(tmp_path):
    state, store, _, worker, _, _, access, _ = _create(tmp_path)
    first_app = create_app(state)
    first_app.state.fake_secret_backend.put("fake/test/provider-key", "raw-value")
    old_handle = store.issue_handle(worker, access["id"], first_app.state.fake_secret_backend)[
        "handle"
    ]

    # A process restart loses the in-memory handle and value, but the durable
    # metadata lease remains usable after a trusted backend re-supplies its value.
    restarted_app = create_app(state)
    with pytest.raises(Forbidden):
        restarted_app.state.fake_secret_backend.resolve(old_handle)
    restarted_app.state.fake_secret_backend.put("fake/test/provider-key", "raw-value")
    new_handle = store.issue_handle(worker, access["id"], restarted_app.state.fake_secret_backend)[
        "handle"
    ]
    assert restarted_app.state.fake_secret_backend.resolve(new_handle) == "raw-value"

    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE credential_access_leases SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
            (access["id"],),
        )
    with pytest.raises(Conflict, match="expired"):
        store.issue_handle(worker, access["id"], restarted_app.state.fake_secret_backend)


def test_only_humans_create_list_renew_and_revoke(tmp_path):
    state, store, owner, worker, grant, lease, access, payload = _create(tmp_path)
    with pytest.raises(Forbidden):
        store.create(worker, grant["id"], "worker-create", canonical_digest(payload), payload)
    with pytest.raises(Forbidden):
        store.list(worker, grant["project_id"], 0, 10)
    revoked = store.revoke(
        owner,
        access["id"],
        "revoke",
        canonical_digest({"expected_revision": access["revision"]}),
        {"expected_revision": access["revision"]},
    )
    assert revoked[1]["revoked_at"] is not None
    assert (
        store.revoke(
            owner,
            access["id"],
            "revoke-again",
            canonical_digest({"expected_revision": access["revision"]}),
            {"expected_revision": access["revision"]},
        )[0]
        == 200
    )
    with pytest.raises(Conflict):
        store.renew(
            owner,
            access["id"],
            "renew-revoked",
            "renew-revoked",
            {"expected_revision": revoked[1]["revision"], "expires_at": lease["expires_at"]},
        )


def test_foreign_worker_cannot_access_a_tenant_scoped_lease(tmp_path):
    state, store, _, _, _, _, access, _ = _create(tmp_path)
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("INSERT INTO organizations VALUES ('org_foreign', 'Foreign', ?)", (now,))
        connection.execute(
            "INSERT INTO projects VALUES ('project_foreign', 'org_foreign', 'Foreign', ?)", (now,)
        )
        connection.execute(
            "INSERT INTO principals VALUES ('foreign_owner', 'org_foreign', 'human', ?, NULL, ?)",
            (_token_hash("foreign-owner-token"), now),
        )
        connection.execute(
            """INSERT INTO credentials VALUES ('foreign_owner_credential', 'foreign_owner',
            'local_owner', 'control', NULL, NULL, NULL, NULL, ?, 'foreign_owner')""",
            (now,),
        )
        connection.execute(
            "INSERT INTO principals VALUES ('foreign_worker', 'org_foreign', 'agent', ?, NULL, ?)",
            (_token_hash("foreign-worker-token"), now),
        )
        connection.execute(
            """INSERT INTO credentials VALUES ('foreign_worker_credential', 'foreign_worker',
            'worker', 'worker', 'project_foreign', 'foreign_owner_credential', ?, NULL, ?,
            'foreign_owner')""",
            (expiry, now),
        )
    foreign = Store(state).authenticate_worker("foreign-worker-token")
    with pytest.raises(NotFound):
        store.issue_handle(foreign, access["id"], create_app(state).state.fake_secret_backend)


def test_v20_migration_creates_no_credential_access_leases_and_rolls_back(tmp_path, monkeypatch):
    from tessera_control.migrations import LATEST_SCHEMA_VERSION, MIGRATIONS
    from tessera_control.store import initialize_state

    state = tmp_path / "state"
    initialize_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TABLE credential_access_leases")
        from schema_fixtures import drop_empty_experiment_schema

        drop_empty_experiment_schema(connection)
        from migration_fixture_helpers import strip_empty_runtime_v55

        strip_empty_runtime_v55(connection)
        connection.execute("UPDATE schema_meta SET schema_version=20")
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert (
            connection.execute("SELECT count(*) FROM credential_access_leases").fetchone()[0] == 0
        )

    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TABLE credential_access_leases")
        strip_empty_runtime_v55(connection)
        connection.execute("UPDATE schema_meta SET schema_version=20")

    def fail(connection):
        from tessera_control.credential_leases import create_credential_access_lease_schema

        create_credential_access_lease_schema(connection)
        raise RuntimeError("injected credential lease migration fault")

    with monkeypatch.context() as patch:
        patch.setitem(MIGRATIONS, 20, fail)
        with pytest.raises(RuntimeError, match="injected credential lease"):
            migrate_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 20
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='credential_access_leases'"
            ).fetchone()
            is None
        )
