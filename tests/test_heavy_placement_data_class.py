# ruff: noqa: F811
"""A heavy stage whose data Salad may not receive falls back locally for a stated reason
(ADR 0141 candidate, ADR 0107 heavy guard), and Salad never receives that data.

Fakes only: the Salad executor is a ``SaladWorld`` test double, the CPU host is a worker-grant
provider with free slots, no Salad call is made. Label names are generic stand-ins:
``non-commercial`` and ``restricted-source`` stand for an operator's non-commercial and
restricted-derivation sources.
"""

from __future__ import annotations

from salad_campaign_fixture import salad_provider_reset, salad_spec_v2  # noqa: F401
from test_heavy_stage_remote_candidate import GIB, HEAVY, _configure, _reset, _stored  # noqa: F401
from test_salad_stage_adapter import NEAR, cpu_world, near, spec_of, stage  # noqa: F401

from tessera_control import campaign_heavy_guard as guard
from tessera_control import license_labels
from tessera_control.campaign_models import HEAVY_REMOTE_ADDED
from tessera_control.campaign_receipts import build_receipt
from tessera_control.provider_trust import install_policy, policy_from_document, record_policy
from tessera_control.store import _connect, utc_now

NON_COMMERCIAL = "non-commercial"
RESTRICTED = "restricted-source"


def _trust(world, near, *, salad_classes=("public",), salad_labels=None, near_classes=None):
    """Install and record a trust policy: Salad's classes and label list, local trusted."""
    env = world.env
    entries = {
        NEAR: {
            "data_classes": list(near_classes or ["public", "private_derived"]),
            "worker_principal_ids": [near.principal["id"]],
            "allowed_license_labels": [NON_COMMERCIAL, RESTRICTED, "open", "unlabelled"],
        },
        "salad": {
            "data_classes": list(salad_classes),
            "worker_principal_ids": [world.worker["id"]],
        },
    }
    if salad_labels is not None:
        entries["salad"]["allowed_license_labels"] = list(salad_labels)
    policy = policy_from_document(
        {"schema": "tessera.provider-trust.v1", "providers": entries}, "file"
    )
    install_policy(policy)
    connection = _connect(env.state / "tessera.sqlite3")
    try:
        with connection:
            record_policy(connection, policy, "operator", utc_now())
    finally:
        connection.close()


def _label(world, labels):
    env = world.env
    connection = _connect(env.state / "tessera.sqlite3")
    try:
        with connection:
            license_labels.add_snapshot_labels(
                connection,
                {"id": env.owner["id"], "org_id": env.owner["org_id"]},
                env.snapshot_id,
                labels,
                utc_now(),
            )
    finally:
        connection.close()


def _placed(world):
    return world.events("campaign.stage_placed")


def _receipt(world):
    with world.env.store.runner_connection() as connection:
        row = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (world.campaign_id,)
        ).fetchone()
        receipt, _ = build_receipt(connection, row, "running")
    return receipt


def _never_salad(world):
    assert not world.env.query("SELECT * FROM campaign_salad_attempts")
    assert not world.env.query("SELECT * FROM campaign_salad_stages")


def _explained(world):
    """The stage landed on local and the heavy guard says why (event, placement if recorded)."""
    world.tick_until(lambda: world.attempt() is not None)
    assert world.attempt()["provider_id"] == NEAR
    (event,) = world.events(guard.EVENT)
    assert event["signals"] == ["declared_memory"] and event["reasons"], event
    # An unexplained fallback is not a licence or data-class explanation.
    assert event["reasons"] not in (["not_listed"], ["remote_not_tried"]), event
    for placed in _placed(world):  # a single-candidate stage records no placement event
        assert placed["heavy_guard"]["reasons"] == event["reasons"]
    assert _receipt(world)["stages"][0]
    _never_salad(world)
    return event


def _heavy_world(tmp_path, near, **over):
    world = cpu_world(tmp_path, near, slots=1)
    world.spec = spec_of(world, **HEAVY, **over)
    return world


# -- 1. unrestricted: Salad first ---------------------------------------------------------------


def test_an_unrestricted_heavy_stage_is_added_to_salad_and_placed_there(tmp_path, near):
    _configure()
    world = _heavy_world(tmp_path, near)
    world.start()
    stage_ = _stored(world)["stages"][0]
    assert stage_["alternate_providers"][0]["provider"] == "salad"
    assert stage_["heavy_remote_candidate"]["added"] is True
    assert stage_["heavy_remote_candidate"]["reason"] == HEAVY_REMOTE_ADDED
    world.tick_until(lambda: bool(_placed(world)))
    (placed,) = _placed(world)
    assert placed["provider_id"] == "salad" and placed["candidates"][0] == "salad"
    assert not world.events(guard.EVENT) and "heavy_guard" not in placed


# -- 2. a licence label Salad may not receive ---------------------------------------------------


def test_a_licence_label_salad_forbids_keeps_a_heavy_stage_local_with_a_reason(tmp_path, near):
    _configure()
    world = _heavy_world(tmp_path, near)
    _trust(world, near, salad_labels=["open"])
    _label(world, [NON_COMMERCIAL])
    world.start()
    stage_ = _stored(world)["stages"][0]
    record = stage_.get("heavy_remote_candidate")
    # Never added as a live candidate; the refusal, if recorded, names the licence or refusal.
    assert not stage_.get("alternate_providers")
    assert record is not None and record["added"] is False
    assert record["reason"] == "candidate_refused", record
    event = _explained(world)
    assert event["reasons"] == ["remote_candidate_not_added"], event
    assert event["detail"] == [
        {
            "provider_id": "salad",
            "reason": "candidate_refused",
            "source": "heavy_remote_candidate",
            "cause": "license_label",  # ADR 0141 amendment 2026-11-23c
            "labels": [NON_COMMERCIAL],
        }
    ]
    assert _receipt(world)["stages"][0]["input"]["license_labels"] == [NON_COMMERCIAL]


# -- 3. a data class Salad is not trusted for ---------------------------------------------------


def test_a_data_class_salad_is_not_trusted_for_keeps_a_heavy_stage_local(tmp_path, near):
    from campaign_fixture import record_snapshot_private

    _configure()
    world = cpu_world(tmp_path, near, slots=1)
    record_snapshot_private(world.env.state, world.env.snapshot_id, "derived")
    (source,) = stage(world.env)["inputs"]
    private = [{**source, "data_class": "private_derived"}]
    _trust(world, near, salad_classes=("public",))
    world.spec = spec_of(world, **HEAVY, inputs=private)
    world.start()
    stage_ = _stored(world)["stages"][0]
    assert not stage_.get("alternate_providers")
    record = stage_.get("heavy_remote_candidate")
    assert record is not None and record["added"] is False, record
    event = _explained(world)
    assert record["reason"] == "candidate_refused", record
    assert event["reasons"] == ["remote_candidate_not_added"], event


# -- 4. listed [local, salad]: Salad skipped at placement ---------------------------------------


def test_a_listed_salad_the_trust_forbids_is_skipped_and_local_says_why(tmp_path, near):
    _configure()
    world = cpu_world(tmp_path, near, slots=1)
    _trust(world, near)  # unrestricted when approved
    world.spec = spec_of(world, memory_bytes=2 * GIB)  # the stage lists salad itself
    world.start()
    assert _stored(world)["stages"][0]["alternate_providers"][0]["provider"] == "salad"
    _label(world, [NON_COMMERCIAL])
    _trust(world, near, salad_labels=["open"])  # tightened after approval
    event = _explained(world)
    (placed,) = _placed(world)
    block = placed["heavy_guard"]
    assert event["reasons"] == block["reasons"]
    (skip,) = [s for s in placed["skipped"] if s["provider_id"] == "salad"]
    assert skip["reason"] == "license_label" and NON_COMMERCIAL in skip["detail"]
    assert block["detail"] == [
        {
            "provider_id": "salad",
            "reason": "license_label",
            "cause": "license_label",  # structured cause from the refusal, not the message
            "labels": [NON_COMMERCIAL],
        }
    ]
    assert block["reasons"] == ["remote_skipped"]


# -- 5. a label inherited through lineage -------------------------------------------------------


def test_a_label_inherited_through_lineage_keeps_the_heavy_stage_off_salad(tmp_path, near):
    _configure()
    world = cpu_world(tmp_path, near, slots=1)
    _trust(world, near, salad_labels=["open"])
    _label(world, [RESTRICTED])
    upstream = stage(world.env, key="up", alternate_providers=None, memory_bytes=None)
    heavy = stage(
        world.env,
        key="g1",
        alternate_providers=None,
        memory_bytes=2 * GIB,
        inputs=[{"kind": "stage", "name": "up", "stage": "up", "outputs": ["output"]}],
    )
    world.spec = salad_spec_v2(world.env, [upstream, heavy])
    world.start()
    stages = {s["key"]: s for s in _stored(world)["stages"]}
    assert not stages["g1"].get("alternate_providers")
    assert stages["g1"]["heavy_remote_candidate"]["added"] is False
