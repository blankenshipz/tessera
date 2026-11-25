"""Use-restriction labels through lineage (ADR 0113, amended 2026-11-23). Fakes only.

A labelled source must not reach a provider whose trust entry does not name the label, even
when the operator listed that provider for ``private_derived`` data without a label list; the
labels of a stage's inputs follow derived data within a campaign and across campaigns (a
recorded output keeps what its stage read); unlabelled data is unaffected. Labels here are
generic stand-ins (``non-commercial``, ``restricted-source``) for an operator's vocabulary.
"""

# ruff: noqa: F811
from __future__ import annotations

import copy
import dataclasses
import sqlite3

import pytest
from campaign_runner_harness import dump
from test_campaign_models_v2 import _spec2
from test_campaign_placement import FAR, NEAR, _stage, _world, providers  # noqa: F401
from test_license_labels import (  # noqa: F401
    NOW,
    add,
    connect,
    env,
    labels_of,
    policy,
    snap,
    spec_of,
    stage,
    up,
)
from test_license_placement_guard import MESSAGE, _label, _policy

from tessera_control import license_labels as ll
from tessera_control import migrations
from tessera_control.campaign_models import NormalInput
from tessera_control.migrations import LATEST_SCHEMA_VERSION, MIGRATIONS
from tessera_control.store import Conflict, initialize_state, migrate_state

NON_COMMERCIAL = "non-commercial"
RESTRICTED = "restricted-source"
TABLE = "campaign_output_license_labels"

# -- the provider rule: a label is admitted only where a trust entry names it ----------------


def _third_party(provider_id, principal):
    """Re-register a fake worker-grant provider as third-party capacity (a Salad stand-in)."""
    from tessera_control import campaign_provider_worker as worker
    from tessera_control.campaign_providers import replace_provider

    descriptor = dataclasses.replace(worker._descriptor(provider_id, principal), third_party=True)
    replace_provider(worker.WorkerGrantProvider(descriptor))


@pytest.mark.parametrize("label", [NON_COMMERCIAL, RESTRICTED, "typo-label"])
def test_a_third_party_provider_without_a_label_list_refuses_labelled_data(providers, label):
    providers("remote", "w1")
    _third_party("remote", "w1")
    rule = policy()  # listed for private_derived, no allowed_license_labels
    assert rule.allowed_license_labels("remote") == {ll.UNLABELLED}
    message = ll.license_refusal("remote", {label}, rule)
    assert message and label in message and "'remote'" in message
    assert ll.license_refusal("remote", {label, ll.UNLABELLED}, rule)
    # Unlabelled data is unaffected; an unresolved lineage is refused like a label.
    assert ll.license_refusal("remote", {ll.UNLABELLED}, rule) is None
    assert ll.license_refusal("remote", {ll.UNLABELLED, ll.UNKNOWN_LINEAGE}, rule)
    # Naming the label admits it.
    assert ll.license_refusal("remote", {label}, policy([label])) is None
    # An unregistered id is not known to be operator-owned: the same refusal.
    assert ll.license_refusal("unregistered", {label}, policy(provider="unregistered"))


def test_an_operator_owned_provider_listed_without_a_list_is_unrestricted_as_before(providers):
    providers("own-host", "w1")  # a worker-grant provider: third_party false
    rule = policy(provider="own-host")
    assert rule.allowed_license_labels("own-host") is None
    assert ll.license_refusal("own-host", {NON_COMMERCIAL, ll.UNLABELLED}, rule) is None


# -- lineage within one campaign ---------------------------------------------------------------


def test_a_long_pipeline_in_one_campaign_resolves_instead_of_reading_as_unknown(env):
    length = ll.MAX_DEPTH + 4
    stages = [stage("s0", [snap(env, "a", env.snapshot_id)])]
    stages += [stage(f"s{i}", [up("up", f"s{i - 1}")]) for i in range(1, length + 1)]
    spec = spec_of(env, stages)
    assert labels_of(env, spec, f"s{length}") == {"up": {ll.UNLABELLED}}
    add(env, env.snapshot_id, [NON_COMMERCIAL])
    assert labels_of(env, spec, f"s{length}") == {"up": {NON_COMMERCIAL}}


def test_a_public_artifact_input_is_unlabelled_not_unknown_lineage(env):
    base = spec_of(env, [stage("s1", [snap(env, "a", env.snapshot_id)])])
    public = NormalInput(
        "public_artifact",
        "pub",
        public_files=(("w.bin", "https://example.org/w.bin", "6" * 64, 10),),
    )
    spec = dataclasses.replace(
        base,
        stages=(dataclasses.replace(base.stage("s1"), inputs=(*base.stage("s1").inputs, public)),),
    )
    assert labels_of(env, spec, "s1")["pub"] == {ll.UNLABELLED}


# -- lineage across campaigns: a recorded output keeps its labels ------------------------------


def _artifact_consumer(env, campaign_id, source_stage):
    item = NormalInput(
        "artifact", "art", campaign_id=campaign_id, stage=source_stage, output="o", sha256="5" * 64
    )
    base = spec_of(env, [stage("c1", [snap(env, "a", env.snapshot_id)])])
    return dataclasses.replace(
        base, stages=(dataclasses.replace(base.stage("c1"), inputs=(item,)),)
    )


def _campaign(env, campaign_id):
    return {"id": campaign_id, "org_id": env.owner["org_id"], "project_id": env.project["id"]}


def test_a_recorded_output_keeps_its_labels_where_recomputation_stops(env, monkeypatch):
    add(env, env.snapshot_id, [RESTRICTED])
    source = env.submit()["id"]  # s1 reads the snapshot, s2 reads s1
    consumer = _artifact_consumer(env, source, "s2")
    assert labels_of(env, consumer, "c1")["art"] == {RESTRICTED}
    monkeypatch.setattr(ll, "MAX_DEPTH", 0)  # a chain longer than the recompute limit
    assert labels_of(env, consumer, "c1")["art"] == {ll.UNLABELLED, ll.UNKNOWN_LINEAGE}
    with connect(env) as db:
        assert ll.record_output_labels(db, _campaign(env, source), "s2", NOW) == [RESTRICTED]
    assert labels_of(env, consumer, "c1")["art"] == {RESTRICTED}


def test_recorded_output_labels_are_add_only_and_scoped(env):
    source = env.submit()["id"]
    with connect(env) as db:
        assert ll.record_output_labels(db, _campaign(env, source), "s2", NOW) == [ll.UNLABELLED]
    add(env, env.snapshot_id, [NON_COMMERCIAL])
    with connect(env) as db:
        merged = ll.record_output_labels(db, _campaign(env, source), "s2", NOW)
        assert merged == [NON_COMMERCIAL, ll.UNLABELLED]
        with pytest.raises(sqlite3.DatabaseError, match="labels"):
            db.execute(f"UPDATE {TABLE} SET labels_json='[]'")
        with pytest.raises(sqlite3.DatabaseError, match="labels"):
            db.execute(f"DELETE FROM {TABLE}")
        other = {"org_id": "org_other", "project_id": "project_other"}
        assert ll.recorded_output_labels(db, source, "s2", **other) is None


def test_the_migration_adds_the_table_and_fresh_state_matches(tmp_path):
    version = migrations.OUTPUT_LICENSE_SCHEMA_VERSION
    assert version == LATEST_SCHEMA_VERSION
    assert MIGRATIONS[version - 1].__name__ == "migrate_output_license_schema"
    names = f"SELECT type,name,sql FROM sqlite_master WHERE name LIKE '{TABLE}%' ORDER BY name"
    fresh, state = tmp_path / "fresh", tmp_path / "old"
    initialize_state(fresh)
    initialize_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute(f"DROP TABLE {TABLE}")
        db.execute(f"UPDATE schema_meta SET schema_version={version - 1}")
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    with (
        sqlite3.connect(state / "tessera.sqlite3") as db,
        sqlite3.connect(fresh / "tessera.sqlite3") as ref,
    ):
        assert db.execute(names).fetchall() == ref.execute(names).fetchall() != []


# -- end to end: derived data does not reach a provider listed without the label --------------


def _derived_on_far(env):
    """Stage a on the near provider reads the snapshot; b on far reads only a's output."""
    first = _stage(env, "a", provider=NEAR, alternates=())
    second = _stage(env, "b", provider=FAR, alternates=())
    second["inputs"] = [{"kind": "stage", "name": "up", "stage": "a", "outputs": ["result"]}]
    return _spec2(first, second)


@pytest.mark.parametrize("label", [NON_COMMERCIAL, RESTRICTED])
def test_derived_data_of_a_labelled_source_never_reaches_a_provider_listed_without_it(
    tmp_path, providers, label
):
    world = _world(tmp_path, providers)
    env = world.env
    _third_party(FAR, world.far_worker["id"])
    _policy(env, world.far_worker)  # far: listed for private_derived, no label list
    _label(env, [label])
    with pytest.raises(Conflict, match=MESSAGE) as refused:
        world.campaign(_derived_on_far(env))
    assert FAR in str(refused.value) and label in str(refused.value)
    # The operator names the label for far: admitted.
    _policy(env, world.far_worker, far_labels=[label])
    assert world.campaign(_derived_on_far(env))["id"]


def test_unlabelled_derived_data_still_reaches_a_provider_listed_without_a_list(
    tmp_path, providers
):
    world = _world(tmp_path, providers)
    _third_party(FAR, world.far_worker["id"])
    _policy(world.env, world.far_worker)
    campaign = world.campaign(_derived_on_far(world.env))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])


def test_a_later_campaign_reading_a_recorded_output_inherits_its_label(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    _third_party(FAR, world.far_worker["id"])
    _policy(env, world.far_worker)
    _label(env, [NON_COMMERCIAL])
    source = world.campaign(_spec2(_stage(env, "a", provider=NEAR, alternates=())))["id"]
    world.tick_all(source)
    assert world.status(source) == "succeeded", dump(world, source)
    recorded = world.row(f"SELECT labels_json FROM {TABLE} WHERE campaign_id=?", source)
    assert recorded is not None and NON_COMMERCIAL in recorded["labels_json"]
    output = world.row(
        "SELECT * FROM campaign_stage_io WHERE campaign_id=? AND direction='output'", source
    )
    consumer = copy.deepcopy(_spec2(_stage(env, "c", provider=FAR, alternates=())))
    consumer["stages"][0]["inputs"] = [
        {
            "kind": "artifact",
            "name": "art",
            "campaign_id": source,
            "stage": "a",
            "output": output["name"],
            "sha256": output["sha256"],
        }
    ]
    with pytest.raises(Conflict, match=MESSAGE) as refused:
        world.campaign(consumer)
    assert FAR in str(refused.value) and NON_COMMERCIAL in str(refused.value)
