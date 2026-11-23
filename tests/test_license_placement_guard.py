"""Licence-label placement guard (ADR 0113): a provider that lists ``allowed_license_labels`` in
the provider-trust file receives a stage only if every effective label of every input is allowed
(labels are inherited through lineage); fail closed at submit, approval, placement and dispatch.
Fakes only; two worker-grant providers stand in for a restricted and an unrestricted host."""

# ruff: noqa: F811
from __future__ import annotations

import pytest
from campaign_runner_harness import dump
from test_campaign_models_v2 import _spec2
from test_campaign_placement import (  # noqa: F401
    FAR,
    NEAR,
    _placed,
    _receipt,
    _stage,
    _world,
    providers,
)

from tessera_control import license_labels
from tessera_control.provider_trust import install_policy, policy_from_document, record_policy
from tessera_control.store import Conflict, _connect, utc_now

STAGE_REFUSED = "campaign.stage_license_refused"
MESSAGE = "may receive only licence labels"


def _policy(env, far_worker, *, far_labels=None, near_labels=None, mode=None):
    """Install and record (as `serve` does) a trust policy; a label list restricts a provider."""
    entries = {
        NEAR: {
            "data_classes": ["public", "private_derived"],
            "worker_principal_ids": [env.worker["id"]],
        },
        FAR: {
            "data_classes": ["public", "private_derived"],
            "worker_principal_ids": [far_worker["id"]],
        },
    }
    if near_labels is not None:
        entries[NEAR]["allowed_license_labels"] = list(near_labels)
    if far_labels is not None:
        entries[FAR]["allowed_license_labels"] = list(far_labels)
    document = {"schema": "tessera.provider-trust.v1", "providers": entries}
    if mode:
        document["mode"] = mode
    policy = policy_from_document(document, "file")
    install_policy(policy)
    connection = _connect(env.state / "tessera.sqlite3")
    try:
        with connection:
            record_policy(connection, policy, "operator", utc_now())
    finally:
        connection.close()


def _label(env, labels):
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


def _events(world, campaign_id, kind):
    return [
        e["payload"] for e in world.events_of(kind) if e["payload"]["campaign_id"] == campaign_id
    ]


# -- plan time ----------------------------------------------------------------------------------


def test_a_restricted_provider_refuses_a_forbidden_label_at_submit(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    _policy(env, world.far_worker, far_labels=["open"])
    _label(env, ["restricted"])
    with pytest.raises(Conflict, match=MESSAGE) as refused:
        world.campaign(_spec2(_stage(env, "a", provider=FAR, alternates=())))
    assert "restricted" in str(refused.value) and FAR in str(refused.value)


def test_every_allowed_label_is_accepted_and_the_receipt_names_them(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    _policy(env, world.far_worker, far_labels=["open", "attribution"])
    _label(env, ["attribution", "open"])
    campaign = world.campaign(_spec2(_stage(env, "a", provider=FAR, alternates=())))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    stage = _receipt(world, campaign["id"])["stages"][0]
    assert stage["input"]["license_labels"] == ["attribution", "open"]


def test_unlabelled_data_is_refused_unless_the_provider_allows_it(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    _policy(env, world.far_worker, far_labels=["open"])
    with pytest.raises(Conflict, match="unlabelled"):
        world.campaign(_spec2(_stage(env, "a", provider=FAR, alternates=())))
    _policy(env, world.far_worker, far_labels=["open", "unlabelled"])
    campaign = world.campaign(_spec2(_stage(env, "a", provider=FAR, alternates=())))
    assert campaign["id"]


def test_an_unrestricted_provider_is_unaffected_and_receipts_keep_their_shape(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    _policy(env, world.far_worker)  # no allowed_license_labels anywhere
    campaign = world.campaign(_spec2(_stage(env, "a", alternates=())))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    assert "license_labels" not in _receipt(world, campaign["id"])["stages"][0]["input"]


def test_a_restricted_alternate_is_refused_even_when_the_primary_is_fine(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    _policy(env, world.far_worker, far_labels=["open"])
    _label(env, ["restricted"])
    with pytest.raises(Conflict, match=MESSAGE):
        world.campaign(_spec2(_stage(env, "a", provider=NEAR, alternates=(FAR,))))


def test_derived_data_inherits_its_inputs_labels_through_lineage(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    _policy(env, world.far_worker, far_labels=["open"])
    _label(env, ["restricted"])
    # b runs on the restricted provider and reads only a's output: the label is inherited.
    spec = _spec2(
        _stage(env, "a", provider=NEAR, alternates=()),
        _stage(env, "b", provider=FAR, alternates=(), upstream="a"),
    )
    with pytest.raises(Conflict, match=MESSAGE):
        world.campaign(spec)
    # The same lineage on the unrestricted provider is allowed.
    ok = _spec2(
        _stage(env, "a", provider=NEAR, alternates=()),
        _stage(env, "b", provider=NEAR, alternates=(), upstream="a"),
    )
    assert world.campaign(ok)["id"]


def test_the_owner_mode_does_not_bypass_a_label_restriction(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    _policy(env, world.far_worker, far_labels=["open"], mode="all_registered_providers")
    _label(env, ["restricted"])
    with pytest.raises(Conflict, match=MESSAGE):
        world.campaign(_spec2(_stage(env, "a", provider=FAR, alternates=())))


# -- dispatch time --------------------------------------------------------------------------------


def test_a_policy_tightened_after_approval_makes_placement_skip_and_fall_back(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    _policy(env, world.far_worker)  # unrestricted when approved
    campaign = world.campaign(_spec2(_stage(env, "a", provider=FAR, alternates=(NEAR,))))
    _label(env, ["restricted"])
    _policy(env, world.far_worker, far_labels=["open"])
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    (placed,) = _placed(world, campaign["id"])
    assert placed["provider_id"] == NEAR
    (skip,) = placed["skipped"]
    assert skip["provider_id"] == FAR and skip["reason"] == "license_label"
    assert "restricted" in skip["detail"]


def test_a_single_provider_stage_fails_closed_when_the_restriction_arrives_later(
    tmp_path, providers
):
    world = _world(tmp_path, providers)
    env = world.env
    _policy(env, world.far_worker)
    campaign = world.campaign(_spec2(_stage(env, "a", provider=FAR, alternates=())))
    _label(env, ["restricted"])
    _policy(env, world.far_worker, far_labels=["open"])
    world.tick_all(campaign["id"])
    assert world.stages(campaign["id"])["a"] == "failed"
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", campaign["id"])
    assert stop["stop_reason"] == "stage_failed:a:input_not_admitted"
    assert not world.attempts(campaign["id"])  # nothing held or sent
    (event,) = _events(world, campaign["id"], STAGE_REFUSED)
    assert event["provider"] == FAR and event["labels"] == ["restricted"]
    assert MESSAGE in event["reason"]


# -- unlisted providers, a busy database, the receipt as dispatched --


def _policy_without(env, far_worker):
    """A trust file that lists only the near provider; the far one is registered but unlisted."""
    document = {
        "schema": "tessera.provider-trust.v1",
        "mode": "all_registered_providers",
        "providers": {
            NEAR: {
                "data_classes": ["public", "private_derived"],
                "worker_principal_ids": [env.worker["id"]],
            }
        },
    }
    policy = policy_from_document(document, "file")
    install_policy(policy)
    connection = _connect(env.state / "tessera.sqlite3")
    try:
        with connection:
            record_policy(connection, policy, "operator", utc_now())
    finally:
        connection.close()


def test_a_provider_missing_from_the_trust_file_is_refused_labelled_data(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    _policy_without(env, world.far_worker)
    _label(env, ["open"])
    with pytest.raises(Conflict, match=MESSAGE) as refused:
        world.campaign(_spec2(_stage(env, "a", provider=FAR, alternates=())))
    assert "open" in str(refused.value) and FAR in str(refused.value)


def test_a_busy_database_is_not_turned_into_a_permanent_refusal(tmp_path, providers, monkeypatch):
    import sqlite3

    world = _world(tmp_path, providers)
    env = world.env
    _policy(env, world.far_worker, far_labels=["open", "unlabelled"])

    def busy(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(license_labels, "stage_labels", busy)
    with pytest.raises(sqlite3.OperationalError):
        world.campaign(_spec2(_stage(env, "a", provider=FAR, alternates=())))

    def broken(*args, **kwargs):
        raise ValueError("unreadable lineage")

    monkeypatch.setattr(license_labels, "stage_labels", broken)
    with pytest.raises(Conflict, match="could not be determined"):
        world.campaign(_spec2(_stage(env, "a", provider=FAR, alternates=())))


def test_the_receipt_records_the_labels_checked_at_dispatch(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    _policy(env, world.far_worker, far_labels=["open", "later", "unlabelled"])
    _label(env, ["open"])
    campaign = world.campaign(_spec2(_stage(env, "a", provider=FAR, alternates=())))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    _label(env, ["later"])  # added after the stage was dispatched
    stage = _receipt(world, campaign["id"])["stages"][0]
    assert stage["input"]["license_labels"] == ["open"]
    assert stage["input"]["license_labels_checked"] == "dispatch"


def test_unlisted_unlabelled_receipts_keep_their_shape(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    _policy_without(env, world.far_worker)
    campaign = world.campaign(_spec2(_stage(env, "a", provider=FAR, alternates=())))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    stage_input = _receipt(world, campaign["id"])["stages"][0]["input"]
    assert "license_labels" not in stage_input
    assert not _events(world, campaign["id"], "campaign.stage_license_checked")
