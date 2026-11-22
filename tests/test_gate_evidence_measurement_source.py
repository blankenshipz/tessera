"""Measurement provenance is server-set: never inferred from, or writable via, the value."""

from __future__ import annotations

import json
import uuid

import pytest
from gate_evidence_fixture import GateWorld

from tessera_control.store import StoreError, canonical_digest, migrate_state

TABLES = ("research_attempt_measurements", "research_admitted_attempt_measurements")


def _count(world: GateWorld) -> int:
    with world.db() as connection:
        return connection.execute(
            "SELECT count(*) FROM research_admitted_attempt_measurements"
        ).fetchone()[0]


def _payload(world: GateWorld, attempt: str, value: dict) -> dict:
    return {
        "expected_revision": world.revision(attempt),
        "name": "rmse",
        "value": value,
    }


def test_human_measurement_is_human_entered(tmp_path):
    world = GateWorld(tmp_path)
    attempt = world.admitted_attempt()
    row = world.human_measurement(attempt, 1.5)
    assert row["source"] == "human_entered"
    with world.db() as connection:
        stored = connection.execute(
            "SELECT source FROM research_admitted_attempt_measurements WHERE id=?", (row["id"],)
        ).fetchone()
        event = connection.execute(
            "SELECT payload_json FROM events WHERE event_type='experiment.measurement_recorded'"
        ).fetchone()
    assert stored["source"] == "human_entered"
    assert json.loads(event["payload_json"])["source"] == "human_entered"


@pytest.mark.parametrize(
    "key", ["derivation", "artifact_sha256", "json_pointer", "source", "measurement_source"]
)
def test_reserved_keys_rejected_by_store_and_http(tmp_path, key):
    world = GateWorld(tmp_path)
    attempt = world.admitted_attempt()
    payload = _payload(world, attempt, {"number": 1.0, key: "x"})
    with pytest.raises(StoreError):
        world.experiments.record_measurement(
            world.owner, attempt, uuid.uuid4().hex, canonical_digest(payload), payload
        )
    response = world.client.post(
        f"/experiment-attempts/{attempt}/measurements",
        json=payload,
        headers=world.headers | {"Idempotency-Key": uuid.uuid4().hex},
    )
    assert 400 <= response.status_code < 500, response.text
    assert _count(world) == 0


def test_nested_reserved_key_is_allowed(tmp_path):
    world = GateWorld(tmp_path)
    attempt = world.admitted_attempt()
    payload = _payload(world, attempt, {"number": 1.0, "detail": {"derivation": 1}})
    body = world.post(f"/experiment-attempts/{attempt}/measurements", payload)
    assert body["source"] == "human_entered"
    assert body["value"]["detail"] == {"derivation": 1}


def test_migration_backfills_source_from_events_only(tmp_path):
    world = GateWorld(tmp_path)
    attempt = world.admitted_attempt()
    derived = world.human_measurement(attempt, 1.0, "derived_one")
    plain = world.human_measurement(attempt, 2.0, "plain")
    lookalike = world.human_measurement(attempt, 3.0, "lookalike")
    with world.db() as connection:
        connection.execute(
            "UPDATE research_admitted_attempt_measurements SET value_json=? WHERE id=?",
            (json.dumps({"number": 3.0, "derivation": "retained-json-number-v1"}), lookalike["id"]),
        )
        # Synthetic derived event for one row, cloned from its recorded event.
        connection.execute(
            """INSERT INTO events (org_id,project_id,task_id,event_type,resource_revision,
            actor_principal_id,created_at,correlation_id,causation_id,payload_json)
            SELECT org_id,project_id,task_id,'experiment.measurement_derived',resource_revision,
            actor_principal_id,created_at,correlation_id,causation_id,?
            FROM events WHERE event_type='experiment.measurement_recorded' LIMIT 1""",
            (json.dumps({"measurement_id": derived["id"], "attempt_id": attempt}),),
        )
        for table in TABLES:
            connection.execute(f"ALTER TABLE {table} DROP COLUMN source")
        connection.execute("UPDATE schema_meta SET schema_version=58 WHERE singleton=1")
    world.client.close()
    assert migrate_state(world.state) >= 58
    with world.db() as connection:
        sources = {
            row["id"]: row["source"]
            for row in connection.execute(
                "SELECT id,source FROM research_admitted_attempt_measurements"
            )
        }
    assert sources == {
        derived["id"]: "derived",
        plain["id"]: "human_entered",
        lookalike["id"]: "human_entered",
    }
