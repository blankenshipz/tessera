# ruff: noqa: F811
"""A placement skip for a licence label or a data class carries a structured cause (labels or
class) taken from the refusal itself: on the placement event, the receipt ``heavy_guard``
detail and the heavy-local audit item. Existing reason codes and messages are unchanged.

Fakes only (the ``SaladWorld`` double and a CPU worker-grant host); generic labels.
"""

from __future__ import annotations

from datetime import UTC, datetime

from campaign_fixture import record_snapshot_private
from salad_campaign_fixture import salad_provider_reset  # noqa: F401
from test_heavy_local_audit_api import World as AuditWorld  # noqa: F401
from test_heavy_local_audit_api import world as audit_world  # noqa: F401
from test_heavy_placement_data_class import (
    NEAR,
    NON_COMMERCIAL,
    _explained,
    _label,
    _placed,
    _receipt,
    _trust,
)
from test_heavy_stage_remote_candidate import GIB, _configure, _reset  # noqa: F401
from test_salad_stage_adapter import cpu_world, near, spec_of, stage  # noqa: F401

from tessera_control import campaign_placement as placement
from tessera_control import campaign_stuck
from tessera_control.provider_trust import install_policy, policy_from_document, record_policy
from tessera_control.store import _connect, utc_now


def _tightened_label_world(tmp_path, near, **over):
    _configure()
    world = cpu_world(tmp_path, near, slots=1)
    _trust(world, near)  # unrestricted when approved
    world.spec = spec_of(world, **over)
    world.start()
    _label(world, [NON_COMMERCIAL])
    _trust(world, near, salad_labels=["open"])  # tightened after approval
    return world


def _salad_skip(placed):
    (skip,) = [s for s in placed["skipped"] if s["provider_id"] == "salad"]
    return skip


def test_a_label_skip_names_the_label_on_event_receipt_and_audit(tmp_path, near):
    world = _tightened_label_world(tmp_path, near, memory_bytes=2 * GIB)
    event = _explained(world)
    (placed,) = _placed(world)
    skip = _salad_skip(placed)
    assert skip["reason"] == "license_label" and NON_COMMERCIAL in skip["detail"]  # unchanged
    assert skip["cause"] == "license_label" and skip["labels"] == [NON_COMMERCIAL]
    expected = [
        {
            "provider_id": "salad",
            "reason": "license_label",
            "cause": "license_label",
            "labels": [NON_COMMERCIAL],
        }
    ]
    assert placed["heavy_guard"]["detail"] == expected and event["detail"] == expected
    receipt = _receipt(world)["stages"][0]
    assert receipt["attempts"][0]["placement"]["heavy_guard"]["detail"] == expected
    assert _salad_skip(receipt["attempts"][0]["placement"])["labels"] == [NON_COMMERCIAL]
    with world.env.store.runner_connection() as connection:
        items = campaign_stuck.heavy_local_audit(
            connection,
            world.env.owner["org_id"],
            world.env.project["id"],
            datetime.now(UTC),
        )
    assert items and items[0]["detail"] == expected


def test_a_data_class_skip_names_the_class(tmp_path, near):
    _configure()
    world = cpu_world(tmp_path, near, slots=1)
    record_snapshot_private(world.env.state, world.env.snapshot_id, "derived")
    (source,) = stage(world.env)["inputs"]
    private = [{**source, "data_class": "private_derived"}]
    _trust(world, near, salad_classes=("public", "private_derived"))
    world.spec = spec_of(world, memory_bytes=2 * GIB, inputs=private)
    world.start()
    _trust(world, near, salad_classes=("public",))  # tightened after approval
    world.tick_until(lambda: world.attempt() is not None)
    (placed,) = _placed(world)
    skip = _salad_skip(placed)
    assert skip["reason"] == "untrusted"
    assert skip["cause"] == "data_class" and skip["data_class"] == "private_derived"
    assert "labels" not in skip
    (entry,) = placed["heavy_guard"]["detail"]
    assert entry["cause"] == "data_class" and entry["data_class"] == "private_derived"


def test_a_light_stage_skip_carries_the_cause_on_its_placement_event(tmp_path, near):
    _configure()
    world = cpu_world(tmp_path, near, slots=1)
    _trust(world, near)
    world.spec = spec_of(world)  # light: no heavy signal
    world.start()
    _label(world, [NON_COMMERCIAL])
    # Tightened after approval: the local (first) candidate no longer takes the label.
    policy = policy_from_document(
        {
            "schema": "tessera.provider-trust.v1",
            "providers": {
                NEAR: {
                    "data_classes": ["public", "private_derived"],
                    "worker_principal_ids": [near.principal["id"]],
                    "allowed_license_labels": ["open"],
                },
                "salad": {
                    "data_classes": ["public"],
                    "worker_principal_ids": [world.worker["id"]],
                },
            },
        },
        "file",
    )
    install_policy(policy)
    connection = _connect(world.env.state / "tessera.sqlite3")
    try:
        with connection:
            record_policy(connection, policy, "operator", utc_now())
    finally:
        connection.close()
    # The stage may land on Salad or end with no candidate; either way the event that records
    # the local skip carries the structured cause.
    world.tick_until(lambda: bool(_placed(world)) or bool(world.events(placement.WAITING_EVENT)))
    (placed,) = (_placed(world) or world.events(placement.WAITING_EVENT))[-1:]
    (skip,) = [s for s in placed["skipped"] if s["provider_id"] == NEAR]
    assert skip["reason"] == "license_label"
    assert skip["cause"] == "license_label" and skip["labels"] == [NON_COMMERCIAL]
    assert "heavy_guard" not in placed


def test_skips_without_a_cause_keep_their_key_set():
    plain = placement.Skip("salad", "over_run_cap", "2 > 1")
    assert set(plain.payload()) == {"provider_id", "reason", "detail"}
    caused = placement.Skip("salad", "license_label", "x", {"cause": "license_label", "labels": []})
    assert set(caused.payload()) == {"provider_id", "reason", "detail", "cause", "labels"}


def test_the_audit_item_carries_a_cause_in_its_recorded_detail(audit_world):
    detail = [
        {
            "provider_id": "salad",
            "reason": "license_label",
            "cause": "license_label",
            "labels": [NON_COMMERCIAL],
        }
    ]
    audit_world.sql("DELETE FROM events WHERE event_type='campaign.heavy_stage_local_fallback'")
    audit_world.seed()
    audit_world.sql(
        "UPDATE events SET payload_json=json_set(payload_json,'$.detail',json(?)) "
        "WHERE event_type='campaign.heavy_stage_local_fallback'",
        __import__("json").dumps(detail),
    )
    (item,) = audit_world.get().json()["items"]
    assert item["detail"] == detail
