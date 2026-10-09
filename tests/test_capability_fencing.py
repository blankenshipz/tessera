"""Policy lifetime, replay, scoped credentials and schema recovery (offline)."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from test_capabilities import declaration, events, receipt, setup_policy
from test_execution import _worker

from tessera_control.capabilities import CapabilityStore
from tessera_control.credential_leases import CredentialAccessLeaseStore
from tessera_control.migrations import LATEST_SCHEMA_VERSION, MIGRATIONS
from tessera_control.recovery import backup_state, restore_state
from tessera_control.store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    canonical_digest,
    initialize_state,
    migrate_state,
)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE capability_policies SET expires_at='2000-01-01T00:00:00Z'",
        "UPDATE capability_policies SET status='revoked'",
        "UPDATE execution_leases SET expires_at='2000-01-01T00:00:00Z'",
        "UPDATE execution_grants SET expires_at='2000-01-01T00:00:00Z'",
        "UPDATE execution_grants SET epoch=epoch+1",
        "UPDATE execution_grants SET status='cancel_requested'",
        "UPDATE worker_registry SET lifecycle_state='retired'",
        "UPDATE tasks SET revision=revision+1",
        "UPDATE snapshot_releases SET status='revoked'",
        "UPDATE capability_operations SET deadline='2000-01-01T00:00:00Z'",
        "UPDATE capability_policies SET declaration_digest='corrupted'",
        "UPDATE execution_grants SET pinned_image='different@sha256:" + "d" * 64 + "'",
    ],
)
def test_every_use_and_admission_replay_rechecks_authority(tmp_path, sql):
    _, store, _, worker, _, _, policy, binding, _ = setup_policy(tmp_path)
    _, op = store.admit(worker, policy["id"], "admit", binding)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(sql)
    assert store.authorize(worker, op["id"])[0] == 403
    with pytest.raises((Conflict, Forbidden)):
        store.admit(worker, policy["id"], "admit", binding)
    # Late reports record effects, never retroactively authorize them.
    _, result = store.receipt(worker, op["id"], "late", receipt(binding))
    assert "stale_authority" in result["reasons"] and result["assessment"] == "quarantined"


def test_human_revoke_cannot_be_undone_by_replayed_approval_or_new_policy(tmp_path):
    _, store, owner, worker, grant, _, policy, binding, decision = setup_policy(tmp_path)
    _, op = store.admit(worker, policy["id"], "admit", binding)
    _, revoked = store.revoke(owner, policy["id"], "revoke", {"expected_revision": 2})
    assert revoked["status"] == "revoked"
    assert events(store)[-1][1]["termination_confirmed"] is False
    with pytest.raises(Conflict):
        store.approve(owner, policy["id"], "approve", decision)
    assert store.authorize(worker, op["id"])[0] == 403
    request = {
        "expected_plan_revision": grant["task_revision"] - 1,
        "expected_plan_digest": grant["plan_digest"],
        "declaration": declaration(),
    }
    _, second = store.request(owner, grant["task_id"], "new-policy", request)
    with pytest.raises(Conflict, match="already had capability authority"):
        store.approve(owner, second["id"], "new-approval", decision)


@pytest.mark.parametrize(
    "change",
    [
        {"declaration_digest": "d" * 64},
        {"expected_revision": 99},
        {"expires_at": "2000-01-01T00:00:00Z"},
        {"expires_at": "2099-01-01T00:00:00Z"},
    ],
)
def test_approval_exact_basis_and_bounded_expiry(tmp_path, change):
    _, store, owner, worker, _, _, policy, binding, decision = setup_policy(tmp_path, approve=False)
    with pytest.raises(Conflict):
        store.approve(owner, policy["id"], "wrong-approval", decision | change)
    assert store.admit(worker, policy["id"], "no-approval", binding)[0] == 403


def test_credential_reference_requires_exact_live_human_access_lease(tmp_path):
    declared = declaration()
    declared["credential_refs"] = ["broker/artifact-read"]
    declared["actions"][0]["credential_ref"] = "broker/artifact-read"
    _, store, owner, worker, grant, lease, policy, binding, _ = setup_policy(
        tmp_path, declared=declared
    )
    assert store.admit(worker, policy["id"], "no-credential", binding)[0] == 403
    access_store = CredentialAccessLeaseStore(store.database_path.parent)
    body = {
        "execution_lease_id": lease["id"],
        "expected_epoch": lease["epoch"],
        "secret_ref": declared["credential_refs"][0],
        "audience": binding["action"]["url"],
        "purpose": "artifact-read",
        "scope": ["sha256:" + "b" * 64, "action:read"],
        "expires_at": (datetime.now(UTC) + timedelta(seconds=10)).isoformat(),
    }
    _, access = access_store.create(owner, grant["id"], "access", canonical_digest(body), body)
    binding["credential_access_lease_id"] = access["id"]
    # Even a valid access lease cannot target another audience/digest.
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("UPDATE credential_access_leases SET audience='wrong-audience'")
    assert store.admit(worker, policy["id"], "wrong-access", binding)[0] == 403
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("UPDATE credential_access_leases SET audience=?", (body["audience"],))
    _, op = store.admit(worker, policy["id"], "admit", binding)
    access_store.revoke(owner, access["id"], "revoke", "revoke", {"expected_revision": 1})
    assert store.authorize(worker, op["id"])[0] == 403
    with pytest.raises(Conflict):
        store.admit(worker, policy["id"], "admit", binding)


def test_worker_identity_tenant_and_revocation_fences(tmp_path):
    state, store, owner, worker, grant, _, policy, binding, _ = setup_policy(tmp_path)
    _, op = store.admit(worker, policy["id"], "admit", binding)
    other = _worker(state, owner, grant["project_id"], "other")
    assert store.admit(other, policy["id"], "other-admit", binding)[0] == 403
    with pytest.raises(Forbidden):
        store.receipt(other, op["id"], "other-receipt", receipt(binding))
    # Move another valid worker into a second tenant with its own project.
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "INSERT INTO organizations VALUES ('foreign-org','Foreign','2026-01-01')"
        )
        connection.execute(
            "INSERT INTO projects VALUES ('foreign-project','foreign-org','Foreign','2026-01-01')"
        )
        created = connection.execute(
            "SELECT created_at FROM credentials WHERE principal_id=?", (other["id"],)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO principals VALUES "
            "('foreign-owner','foreign-org','human','unused-hash',NULL,?)",
            (created,),
        )
        connection.execute(
            "INSERT INTO credentials VALUES "
            "('foreign-owner-credential','foreign-owner','local_owner','control',"
            "NULL,NULL,NULL,NULL,?,'foreign-owner')",
            (created,),
        )
        connection.execute("UPDATE principals SET org_id='foreign-org' WHERE id=?", (other["id"],))
        connection.execute(
            "UPDATE credentials SET project_id='foreign-project',"
            "parent_id='foreign-owner-credential',created_by_principal_id='foreign-owner' "
            "WHERE principal_id=?",
            (other["id"],),
        )
    foreign = Store(state).authenticate_worker("other")
    with pytest.raises(NotFound):
        store.admit(foreign, policy["id"], "foreign-admit", binding)
    with pytest.raises(NotFound):
        store.receipt(foreign, op["id"], "foreign-receipt", receipt(binding))
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE credentials SET revoked_at='2026-01-01' WHERE principal_id=?", (worker["id"],)
        )
    with pytest.raises(Forbidden):
        store.admit(worker, policy["id"], "admit", binding)
    with pytest.raises(Forbidden):
        store.authorize(worker, op["id"])


def test_receipt_event_failure_rolls_back_actual_and_auto_revocation(tmp_path, monkeypatch):
    _, store, owner, worker, _, _, policy, binding, _ = setup_policy(tmp_path)
    _, op = store.admit(worker, policy["id"], "admit", binding)

    def fail(*args):
        raise sqlite3.OperationalError("receipt audit unavailable")

    report = receipt(binding) | {"downloaded_sha256": "d" * 64}
    with monkeypatch.context() as patch:
        patch.setattr(Store, "_event", staticmethod(fail))
        with pytest.raises(sqlite3.OperationalError):
            store.receipt(worker, op["id"], "receipt", report)
    current = store.get(owner, policy["id"])
    assert current["status"] == "approved" and current["operations"][0]["receipt"] is None
    assert store.receipt(worker, op["id"], "receipt", report)[1]["assessment"] == "quarantined"


def test_historical_receipt_replay_rechecks_project_scope(tmp_path):
    _, store, owner, worker, _, _, policy, binding, _ = setup_policy(tmp_path)
    _, op = store.admit(worker, policy["id"], "admit", binding)
    report = receipt(binding)
    store.receipt(worker, op["id"], "receipt", report)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "INSERT INTO projects VALUES ('another-project',?,'Other','2026-01-01')",
            (owner["org_id"],),
        )
        connection.execute(
            "UPDATE credentials SET project_id='another-project' WHERE principal_id=?",
            (worker["id"],),
        )
    with pytest.raises(NotFound):
        store.receipt(worker, op["id"], "receipt", report)


def test_v48_migration_empty_idempotent_and_transactional(tmp_path, monkeypatch):
    state = tmp_path / "state"
    initialize_state(state)

    def downgrade():
        with sqlite3.connect(state / "tessera.sqlite3") as connection:
            connection.execute("DROP TABLE capability_operations")
            connection.execute("DROP TABLE capability_policies")
            from migration_fixture_helpers import strip_empty_runtime_v55

            strip_empty_runtime_v55(connection)
            connection.execute("UPDATE schema_meta SET schema_version=48")

    downgrade()
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM capability_policies").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM capability_operations").fetchone()[0] == 0
    downgrade()
    original = MIGRATIONS[48]

    def fail(connection):
        original(connection)
        raise RuntimeError("injected migration failure")

    with monkeypatch.context() as patch:
        patch.setitem(MIGRATIONS, 48, fail)
        with pytest.raises(RuntimeError):
            migrate_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 48
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='capability_policies'"
            ).fetchone()
            is None
        )
    assert migrate_state(state) == LATEST_SCHEMA_VERSION


def test_backup_preserves_audit_but_restore_cannot_enable_effects(tmp_path):
    state, store, owner, worker, _, _, policy, binding, _ = setup_policy(tmp_path)
    _, op = store.admit(worker, policy["id"], "admit", binding)
    store.receipt(worker, op["id"], "receipt", receipt(binding))
    backup_state(state, tmp_path / "backup")
    restore_state(tmp_path / "backup", tmp_path / "held")
    restored = CapabilityStore(tmp_path / "held")
    token = (tmp_path / "held" / "owner.header").read_text().strip().split("Bearer ", 1)[1]
    held_owner = restored.authenticate(token)
    assert (
        restored.get(held_owner, policy["id"])["operations"]
        == store.get(owner, policy["id"])["operations"]
    )
    with pytest.raises(Forbidden):
        restored.revoke(held_owner, policy["id"], "held-revoke", {"expected_revision": 2})
