# ruff: noqa: E501
"""Context-only message delivery control and worker fences; no live harness or provider."""

# fmt: off

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_kubernetes_execution import _admitted_package

from tessera_control.api import create_app
from tessera_control.interaction import InteractionStore
from tessera_control.migrations import LATEST_SCHEMA_VERSION
from tessera_control.recovery import backup_state, restore_state
from tessera_control.store import (
    Conflict,
    Forbidden,
    Store,
    _connect,
    initialize_state,
    migrate_state,
)


def _owner(state: Path) -> dict[str, object]:
    token = (state / "owner.header").read_text().strip().split(": ", 1)[1].removeprefix("Bearer ")
    return Store(state).authenticate(token)


def _delivery_fixture(tmp_path: Path):
    response, worker, package = _admitted_package(tmp_path)
    state = tmp_path / "state"
    owner, interactions = _owner(state), InteractionStore(Store(state))
    project_id, task_id = package.package["project_id"], package.package["task_id"]
    _, thread = interactions.create_thread(owner, "thread", project_id, "Harness", task_id)
    with _connect(state / "tessera.sqlite3") as connection:
        attempt = connection.execute(
            "SELECT revision FROM research_experiment_admitted_attempts WHERE id=?",
            (response["admitted_attempt_id"],),
        ).fetchone()
    assert attempt is not None
    _, binding = interactions.create_harness_session_binding(
        worker,
        "bind",
        operation_id=package.binding.operation_id,
        harness_instance_id="harness-1",
        upstream_session_id="session-1",
        adapter_profile="opencode-v1.18.30-context-only",
        provider_disabled=True,
    )
    payload = {
        "thread_id": thread["id"], "harness_session_binding_id": binding["id"],
        "context_revision": 1,
        "intent": "direction", "body": "Use this exact context only.",
    }
    return state, interactions, owner, worker, payload


def test_human_enqueue_worker_claim_ack_history_and_legacy_chat(tmp_path: Path) -> None:
    _state, interactions, owner, worker, payload = _delivery_fixture(tmp_path)
    _, legacy = interactions.send_message(owner, "legacy", payload["thread_id"], "question", "Legacy chat")
    status, created = interactions.enqueue_harness_delivery(owner, "enqueue", **payload)
    assert status == 201 and created["delivery"]["state"] == "queued"
    delivery = created["delivery"]
    with pytest.raises(Forbidden):
        interactions.enqueue_harness_delivery(worker, "worker-enqueue", **payload)
    status, claim = interactions.claim_harness_delivery(
        worker, "claim", delivery["delivery_id"], expected_epoch=delivery["epoch"]
    )
    assert status == 200 and claim["delivery"]["state"] == "delivered"
    assert interactions.claim_harness_delivery(
        worker, "claim", delivery["delivery_id"], expected_epoch=delivery["epoch"]
    ) == (status, claim)
    with pytest.raises(Conflict, match="context digest"):
        interactions.acknowledge_harness_delivery(
            worker, "bad-digest", delivery["delivery_id"], expected_revision=2,
            context_digest="f" * 64, upstream_message_id="message-1", upstream_readback_digest="a" * 64,
        )
    _, acknowledged = interactions.acknowledge_harness_delivery(
        worker, "ack", delivery["delivery_id"], expected_revision=2,
        context_digest=delivery["context_digest"], upstream_message_id="message-1",
        upstream_readback_digest="b" * 64,
    )
    assert acknowledged["state"] == "acknowledged" and acknowledged["upstream_message_id"] == "message-1"
    messages = interactions.list_messages(owner, payload["thread_id"])
    assert next(item for item in messages if item["id"] == legacy["id"])["delivery_legacy"] is True
    delivered = next(item for item in messages if item["id"] == created["id"])["delivery"]
    assert delivered["state"] == "acknowledged" and delivered["upstream_session_id"] == "session-1"


def test_worker_session_binding_is_immutable_idempotent_and_tenant_scoped(tmp_path: Path) -> None:
    state, interactions, owner, worker, payload = _delivery_fixture(tmp_path)
    binding = interactions.get_harness_session_binding(owner, payload["harness_session_binding_id"])
    exact = {
        "operation_id": binding["operation_id"],
        "harness_instance_id": binding["harness_instance_id"],
        "upstream_session_id": binding["upstream_session_id"],
        "adapter_profile": binding["adapter_profile"],
        "provider_disabled": True,
    }
    assert interactions.create_harness_session_binding(worker, "same-binding", **exact) == (200, binding)
    assert interactions.create_harness_session_binding(worker, "same-binding", **exact) == (200, binding)
    with pytest.raises(Conflict, match="different request"):
        interactions.create_harness_session_binding(
            worker, "same-binding", **(exact | {"upstream_session_id": "other-session"})
        )
    with pytest.raises(Conflict, match="immutable identity"):
        interactions.create_harness_session_binding(
            worker, "different-binding", **(exact | {"upstream_session_id": "other-session"})
        )

    client = TestClient(create_app(state))
    owner_header = (state / "owner.header").read_text().strip().split(": ", 1)[1]
    read = client.get(
        f"/interaction/harness-session-bindings/{binding['id']}",
        headers={"Authorization": owner_header},
    )
    assert read.status_code == 200 and read.json() == binding
    worker_read = client.get(
        f"/interaction/harness-session-bindings/{binding['id']}",
        headers={"Authorization": "Bearer worker-token"},
    )
    assert worker_read.status_code == 401
    worker_create = client.post(
        "/worker/operation-harness-session-bindings",
        headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "worker-api"},
        json=exact,
    )
    assert worker_create.status_code == 200 and worker_create.json() == binding
    with _connect(state / "tessera.sqlite3") as connection, pytest.raises(
        sqlite3.IntegrityError, match="immutable"
    ):
        connection.execute(
            "UPDATE operation_harness_session_bindings SET epoch=epoch+1 WHERE id=?",
            (binding["id"],),
        )


def test_enqueue_derives_destination_and_backup_restore_retains_binding_history(tmp_path: Path) -> None:
    state, interactions, owner, _worker, payload = _delivery_fixture(tmp_path)
    _, queued = interactions.enqueue_harness_delivery(owner, "enqueue", **payload)
    delivery = queued["delivery"]
    assert delivery["harness_session_binding_id"] == payload["harness_session_binding_id"]
    client = TestClient(create_app(state))
    owner_header = (state / "owner.header").read_text().strip().split(": ", 1)[1]
    rejected = client.post(
        "/interaction/deliveries",
        headers={"Authorization": owner_header, "Idempotency-Key": "routing"},
        json=payload | {"worker_principal_id": "attacker"},
    )
    assert rejected.status_code == 422

    backup = tmp_path / "backup"
    assert backup_state(state, backup)["schema_version"] == LATEST_SCHEMA_VERSION
    restored = tmp_path / "restored"
    restore_state(backup, restored)
    restored_binding = InteractionStore(Store(restored)).get_harness_session_binding(
        _owner(restored), payload["harness_session_binding_id"]
    )
    assert restored_binding["operation_id"] == delivery["operation_id"]


def test_v37_delivery_migration_preserves_rows_uniqueness_and_idempotency(tmp_path: Path) -> None:
    state, interactions, owner, worker, payload = _delivery_fixture(tmp_path)
    _, created = interactions.enqueue_harness_delivery(owner, "legacy-enqueue", **payload)
    delivery_id = created["delivery"]["delivery_id"]
    with _connect(state / "tessera.sqlite3") as connection:
        for column in ("lease_id", "grant_id", "operation_id"):
            connection.execute(f"ALTER TABLE interaction_message_deliveries DROP COLUMN {column}")
        connection.execute(
            "UPDATE interaction_message_deliveries SET harness_session_binding_id=NULL WHERE id=?",
            (delivery_id,),
        )
        from migration_fixture_helpers import strip_empty_runtime_v55

        strip_empty_runtime_v55(connection)
        connection.execute("UPDATE schema_meta SET schema_version=37")
        before = dict(
            connection.execute(
                "SELECT * FROM interaction_message_deliveries WHERE id=?", (delivery_id,)
            ).fetchone()
        )
        idempotency_count = connection.execute("SELECT count(*) FROM idempotency").fetchone()[0]

    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    with _connect(state / "tessera.sqlite3") as connection:
        migrated = dict(
            connection.execute(
                "SELECT * FROM interaction_message_deliveries WHERE id=?", (delivery_id,)
            ).fetchone()
        )
        for column in ("operation_id", "grant_id", "lease_id"):
            assert migrated.pop(column) is None
        assert migrated == before
        assert connection.execute("SELECT count(*) FROM idempotency").fetchone()[0] == idempotency_count
        columns = [row[1] for row in connection.execute("PRAGMA table_info(interaction_message_deliveries)")]
        selected = ",".join("?" if column == "id" else column for column in columns)
        with pytest.raises(sqlite3.IntegrityError, match="message_id"):
            connection.execute(
                f"INSERT INTO interaction_message_deliveries SELECT {selected} "
                "FROM interaction_message_deliveries WHERE id=?",
                ("duplicate-delivery", delivery_id),
            )

    with pytest.raises(Conflict, match="authority"):
        interactions.claim_harness_delivery(
            worker, "legacy-claim", delivery_id, expected_epoch=created["delivery"]["epoch"]
        )
    first = interactions.enqueue_harness_delivery(owner, "post-migration-enqueue", **payload)
    assert interactions.enqueue_harness_delivery(owner, "post-migration-enqueue", **payload) == first
    assert migrate_state(state) == LATEST_SCHEMA_VERSION


def test_v39_session_binding_migration_is_empty_and_database_immutable(tmp_path: Path) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TRIGGER operation_harness_session_bindings_no_update")
        connection.execute("DROP TRIGGER operation_harness_session_bindings_no_delete")
        connection.execute("DROP TABLE operation_harness_session_bindings")
        connection.execute(
            "ALTER TABLE interaction_message_deliveries DROP COLUMN harness_session_binding_id"
        )
        from migration_fixture_helpers import strip_empty_runtime_v55

        strip_empty_runtime_v55(connection)
        connection.execute("UPDATE schema_meta SET schema_version=38")

    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute(
            "SELECT count(*) FROM operation_harness_session_bindings"
        ).fetchone()[0] == 0
        assert "harness_session_binding_id" in {
            column[1]
            for column in connection.execute("PRAGMA table_info(interaction_message_deliveries)")
        }
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger' "
                "AND tbl_name='operation_harness_session_bindings'"
            )
        }
    assert triggers == {
        "operation_harness_session_bindings_no_delete",
        "operation_harness_session_bindings_no_update",
    }


def test_attempt_epoch_fences_and_control_only_cancellation(tmp_path: Path) -> None:
    state, interactions, owner, worker, payload = _delivery_fixture(tmp_path)
    _, created = interactions.enqueue_harness_delivery(owner, "enqueue", **payload)
    delivery = created["delivery"]
    with pytest.raises(Conflict, match="claimable"):
        interactions.claim_harness_delivery(
            worker, "wrong-epoch", delivery["delivery_id"], expected_epoch=delivery["epoch"] + 1
        )
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE research_experiment_admitted_attempts SET revision=revision+1 WHERE id=?",
            (delivery["admitted_attempt_id"],),
        )
    with pytest.raises(Conflict, match="authority"):
        interactions.claim_harness_delivery(
            worker, "stale-attempt", delivery["delivery_id"], expected_epoch=delivery["epoch"]
        )
    cancelled = interactions.cancel_harness_delivery(
        owner, "cancel", delivery["delivery_id"], expected_revision=1
    )
    assert cancelled[1]["state"] == "cancelled"
    with pytest.raises(Conflict, match="claimable"):
        interactions.claim_harness_delivery(
            worker, "claim-after-cancel", delivery["delivery_id"], expected_epoch=delivery["epoch"]
        )
    with _connect(state / "tessera.sqlite3") as connection:
        states = connection.execute(
            "SELECT state FROM interaction_message_delivery_states WHERE delivery_id=? ORDER BY sequence",
            (delivery["delivery_id"],),
        ).fetchall()
    assert [row["state"] for row in states] == ["queued", "cancelled"]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE execution_leases SET expires_at='2020-01-01T00:00:00Z'",
        "UPDATE execution_grants SET status='cancel_requested'",
        "UPDATE worker_registry SET lifecycle_state='draining'",
        "UPDATE tasks SET revision=revision+1 WHERE id=(SELECT task_id FROM external_operations LIMIT 1)",
    ],
    ids=["expired-lease", "revoked-grant", "worker-lifecycle", "task-basis"],
)
def test_claim_rechecks_live_authority_before_releasing_context(
    tmp_path: Path, statement: str
) -> None:
    state, interactions, owner, worker, payload = _delivery_fixture(tmp_path)
    _, created = interactions.enqueue_harness_delivery(owner, "enqueue", **payload)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(statement)
    with pytest.raises(Conflict, match="authority|active|ready|basis"):
        interactions.claim_harness_delivery(
            worker,
            "denied-claim",
            created["delivery"]["delivery_id"],
            expected_epoch=created["delivery"]["epoch"],
        )


def test_claim_replay_rechecks_authority_without_returning_context(tmp_path: Path) -> None:
    state, interactions, owner, _worker, payload = _delivery_fixture(tmp_path)
    _, created = interactions.enqueue_harness_delivery(owner, "enqueue", **payload)
    delivery = created["delivery"]
    client = TestClient(create_app(state))
    headers = {"Authorization": "Bearer worker-token", "Idempotency-Key": "claim"}
    claimed = client.post(
        f"/worker/interaction-deliveries/{delivery['delivery_id']}/claim",
        json={"expected_epoch": delivery["epoch"]},
        headers=headers,
    )
    assert claimed.status_code == 200 and claimed.json()["delivery"]["body"] == payload["body"]
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute("UPDATE execution_leases SET expires_at='2020-01-01T00:00:00Z'")
    denied = client.post(
        f"/worker/interaction-deliveries/{delivery['delivery_id']}/claim",
        json={"expected_epoch": delivery["epoch"]},
        headers=headers,
    )
    assert denied.status_code == 409
    assert "delivery" not in denied.json() and payload["body"] not in denied.text


def test_historical_readback_acknowledgement_does_not_reopen_authority(tmp_path: Path) -> None:
    state, interactions, owner, worker, payload = _delivery_fixture(tmp_path)
    _, created = interactions.enqueue_harness_delivery(owner, "enqueue", **payload)
    delivery = created["delivery"]
    _, claimed = interactions.claim_harness_delivery(
        worker, "claim", delivery["delivery_id"], expected_epoch=delivery["epoch"]
    )
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute("UPDATE execution_leases SET expires_at='2020-01-01T00:00:00Z'")
    _, acknowledged = interactions.acknowledge_harness_delivery(
        worker,
        "ack",
        delivery["delivery_id"],
        expected_revision=claimed["delivery"]["revision"],
        context_digest=delivery["context_digest"],
        upstream_message_id="message-1",
        upstream_readback_digest="a" * 64,
    )
    assert acknowledged["state"] == "acknowledged"


def test_worker_routes_require_worker_audience_and_preserve_context_digest(tmp_path: Path) -> None:
    state, _interactions, _owner_actor, _worker, payload = _delivery_fixture(tmp_path)
    owner_header = (state / "owner.header").read_text().strip().split(": ", 1)[1]
    client = TestClient(create_app(state))
    created = client.post(
        "/interaction/deliveries", json=payload,
        headers={"Authorization": owner_header, "Idempotency-Key": "api-enqueue"},
    )
    assert created.status_code == 201
    delivery = created.json()["delivery"]
    denied = client.post(
        f"/worker/interaction-deliveries/{delivery['delivery_id']}/claim",
        json={"expected_epoch": delivery["epoch"]},
        headers={"Authorization": owner_header, "Idempotency-Key": "human-claim"},
    )
    assert denied.status_code == 401
    claimed = client.post(
        f"/worker/interaction-deliveries/{delivery['delivery_id']}/claim",
        json={"expected_epoch": delivery["epoch"]},
        headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "worker-claim"},
    )
    assert claimed.status_code == 200 and claimed.json()["delivery"]["context_digest"] == delivery["context_digest"]
