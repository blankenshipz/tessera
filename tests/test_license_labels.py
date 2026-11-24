"""Licence labels on snapshots: add-only storage, lineage inheritance, the provider rule.

No dataset or vendor names: labels are an operator vocabulary (``cc-by``, ``non-commercial``).
"""

from __future__ import annotations

import dataclasses
import sqlite3
import uuid

import pytest
from campaign_fixture import Env

from tessera_control import license_labels as ll
from tessera_control.campaign_models import NormalInput, normalize, parse_spec
from tessera_control.migrations import LATEST_SCHEMA_VERSION, LICENSE_SCHEMA_VERSION
from tessera_control.provider_trust import policy_from_document
from tessera_control.store import NotFound, _connect, migrate_state

NOW = "2026-11-23T00:00:00Z"


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def connect(env):
    return _connect(env.state / "tessera.sqlite3")


def extra_snapshot(env) -> str:
    """A second snapshot of the same project (a copy of the fixture row with a new id)."""
    ident = "snapshot_" + uuid.uuid4().hex
    with connect(env) as db:
        db.execute(
            "INSERT INTO source_snapshots SELECT ?, project_id, ?, manifest_json, created_at, "
            "actor_principal_id FROM source_snapshots WHERE id=?",
            (ident, uuid.uuid4().hex * 2, env.snapshot_id),
        )
    return ident


def principal(env, **over):
    return {"id": env.owner["id"], "org_id": env.owner["org_id"], **over}


def add(env, snapshot_id, labels):
    with connect(env) as db:
        return ll.add_snapshot_labels(db, principal(env), snapshot_id, labels, NOW)


def stage(key, inputs, **over):
    value = {
        "key": key,
        "backend": "docker-local-cpu-v1",
        "image": "python@sha256:" + "a" * 64,
        "argv": ["python", "-c", "print(1)"],
        "output_paths": ["data.json"],
        "max_output_bytes": 1024,
        "max_duration_seconds": 60,
        "max_exposure_cents": 6,
        "inputs": inputs,
    }
    value.update(over)
    return value


def snap(env, name, snapshot_id, digest=None):
    return {
        "kind": "snapshot",
        "name": name,
        "snapshot_id": snapshot_id,
        "digest": digest or env.snapshot_digest,
        "data_class": "public",
    }


def up(name, key):
    return {"kind": "stage", "name": name, "stage": key, "paths": ["data.json"]}


def spec_of(env, stages):
    return normalize(
        parse_spec(
            {
                "version": 1,
                "name": "labels",
                "budget_cap_cents": 10,
                "deadline_seconds": 3600,
                "worker_principal_id": env.worker["id"],
                "stages": stages,
            }
        )
    )


def pub(name):
    return NormalInput(
        kind="public_artifact",
        name=name,
        public_files=(("w", "https://models.example.com/w.bin", "c" * 64, 10),),
    )


def spec_with_public(env, items):
    """A v1 spec cannot spell a public_artifact, so swap the normalized inputs in directly."""
    spec = spec_of(env, [stage("s1", [snap(env, "code", env.snapshot_id)])])
    stage_one = dataclasses.replace(spec.stages[0], inputs=tuple(items))
    return dataclasses.replace(spec, stages=(stage_one,))


def labels_of(env, spec, key):
    with connect(env) as db:
        return ll.effective_labels(
            db, spec, key, org_id=env.owner["org_id"], project_id=env.project["id"]
        )


# -- storage -------------------------------------------------------------------------------


def test_label_validation():
    assert ll.validate_labels(["cc-by", "cc-by", "a1_b"]) == ["a1_b", "cc-by"]
    for bad in (["Upper"], ["1x"], ["x" * 33], [""], ["has space"], [3], []):
        with pytest.raises(ValueError):
            ll.validate_labels(bad)
    for reserved in (ll.UNLABELLED, ll.UNKNOWN_LINEAGE):
        with pytest.raises(ValueError, match="reserved"):
            ll.validate_labels([reserved])
    with pytest.raises(ValueError, match="at most"):
        ll.validate_labels([f"l{i}" for i in range(ll.MAX_LABELS + 1)])


def test_an_unlabelled_snapshot_reads_as_unlabelled_and_a_labelled_one_does_not(env):
    with connect(env) as db:
        assert ll.snapshot_labels(db, env.snapshot_id) == {ll.UNLABELLED}
    result = add(env, env.snapshot_id, ["non-commercial"])
    assert result == {"labels": ["non-commercial"], "added": ["non-commercial"], "revision": 1}
    with connect(env) as db:
        assert ll.snapshot_labels(db, env.snapshot_id) == {"non-commercial"}


def test_adding_is_idempotent_and_add_only(env):
    add(env, env.snapshot_id, ["cc-by"])
    again = add(env, env.snapshot_id, ["cc-by"])
    assert again == {"labels": ["cc-by"], "added": [], "revision": 1}
    more = add(env, env.snapshot_id, ["research-only", "cc-by"])
    assert more == {"labels": ["cc-by", "research-only"], "added": ["research-only"], "revision": 2}
    assert add(env, env.snapshot_id, ["a"])["labels"] == ["a", "cc-by", "research-only"]


def test_the_cap_applies_to_the_merged_set(env):
    add(env, env.snapshot_id, [f"l{i}" for i in range(ll.MAX_LABELS)])
    with pytest.raises(ValueError, match="at most"):
        add(env, env.snapshot_id, ["one-more"])


def test_a_label_can_never_be_removed_or_the_row_deleted_or_moved(env):
    add(env, env.snapshot_id, ["cc-by", "non-commercial"])
    with connect(env) as db:
        with pytest.raises(sqlite3.DatabaseError, match="only be added"):
            db.execute(
                "UPDATE snapshot_license_labels SET labels_json='[\"cc-by\"]' WHERE snapshot_id=?",
                (env.snapshot_id,),
            )
        with pytest.raises(sqlite3.DatabaseError, match="only be added"):
            db.execute("UPDATE snapshot_license_labels SET labels_json='[]'")
        with pytest.raises(sqlite3.DatabaseError, match="only be added"):
            db.execute("UPDATE snapshot_license_labels SET project_id='other'")
        with pytest.raises(sqlite3.DatabaseError, match="permanent"):
            db.execute("DELETE FROM snapshot_license_labels")
        # A superset is the one allowed change.
        db.execute(
            'UPDATE snapshot_license_labels SET labels_json=\'["cc-by","non-commercial","z"]\''
        )


def test_another_organizations_snapshot_reads_as_not_found(env):
    with connect(env) as db, pytest.raises(NotFound):
        ll.add_snapshot_labels(
            db, {"id": "p", "org_id": "org_other"}, env.snapshot_id, ["cc-by"], NOW
        )
    with connect(env) as db, pytest.raises(NotFound):
        ll.add_snapshot_labels(db, principal(env), "snapshot_missing", ["cc-by"], NOW)


def test_a_project_bound_principal_cannot_label_another_projects_snapshot(env):
    with connect(env) as db, pytest.raises(NotFound):
        ll.add_snapshot_labels(
            db, principal(env, project_id="project_other"), env.snapshot_id, ["cc-by"], NOW
        )
    with connect(env) as db:
        done = ll.add_snapshot_labels(
            db, principal(env, project_id=env.project["id"]), env.snapshot_id, ["cc-by"], NOW
        )
    assert done["labels"] == ["cc-by"]


# -- lineage -------------------------------------------------------------------------------


def test_a_snapshot_input_carries_its_labels_and_a_stage_input_inherits_them(env):
    add(env, env.snapshot_id, ["cc-by"])
    spec = spec_of(
        env,
        [
            stage("s1", [snap(env, "code", env.snapshot_id)]),
            stage("s2", [up("up", "s1")]),
        ],
    )
    assert labels_of(env, spec, "s1") == {"code": {"cc-by"}}
    assert labels_of(env, spec, "s2") == {"up": {"cc-by"}}


def test_labels_propagate_through_two_hops_and_unite_over_inputs(env):
    other = extra_snapshot(env)
    add(env, env.snapshot_id, ["cc-by"])
    add(env, other, ["non-commercial"])
    spec = spec_of(
        env,
        [
            stage("s1", [snap(env, "a", env.snapshot_id)]),
            stage("s2", [snap(env, "b", other, "e" * 64), up("one", "s1")]),
            stage("s3", [up("two", "s2")]),
        ],
    )
    assert labels_of(env, spec, "s2") == {"b": {"non-commercial"}, "one": {"cc-by"}}
    with connect(env) as db:
        kw = {"org_id": env.owner["org_id"], "project_id": env.project["id"]}
        assert ll.stage_labels(db, spec, "s3", **kw) == {"cc-by", "non-commercial"}
        assert ll.stage_labels(db, spec, "s1", **kw) == {"cc-by"}


def test_unlabelled_data_stays_unlabelled_through_lineage(env):
    spec = spec_of(
        env,
        [stage("s1", [snap(env, "a", env.snapshot_id)]), stage("s2", [up("up", "s1")])],
    )
    assert labels_of(env, spec, "s2") == {"up": {ll.UNLABELLED}}


def _artifact(spec, stage_key, campaign_id, stage):
    """The stage with an extra artifact input naming a stage of another campaign."""
    item = NormalInput(
        "artifact",
        "art",
        campaign_id=campaign_id,
        stage=stage,
        output="data",
        sha256="5" * 64,
    )
    stages = tuple(
        dataclasses.replace(s, inputs=(*s.inputs, item)) if s.key == stage_key else s
        for s in spec.stages
    )
    return dataclasses.replace(spec, stages=stages)


def test_an_artifact_input_carries_the_labels_of_its_source_stage(env):
    add(env, env.snapshot_id, ["research-only"])
    source = env.submit()["id"]  # the fixture spec: s1 reads the snapshot, s2 reads s1 too
    consumer = _artifact(
        spec_of(env, [stage("c1", [snap(env, "a", env.snapshot_id)])]), "c1", source, "s2"
    )
    assert labels_of(env, consumer, "c1")["art"] == {"research-only"}


def test_an_unresolvable_artifact_carries_the_unknown_lineage_marker(env):
    base = spec_of(env, [stage("c1", [snap(env, "a", env.snapshot_id)])])
    for campaign, stage_key in (("campaign_missing", "s1"), (env.submit()["id"], "no_such_stage")):
        consumer = _artifact(base, "c1", campaign, stage_key)
        assert labels_of(env, consumer, "c1")["art"] == {ll.UNLABELLED, ll.UNKNOWN_LINEAGE}


def test_an_unknown_upstream_stage_and_a_cycle_fail_closed(env):
    base = spec_of(
        env,
        [stage("a", [snap(env, "x", env.snapshot_id)]), stage("b", [up("up", "a")])],
    )
    broken = dataclasses.replace(
        base,
        stages=(
            dataclasses.replace(
                base.stage("a"), inputs=(NormalInput("stage", "loop", stage="b", paths=("d",)),)
            ),
            base.stage("b"),
        ),
    )
    assert labels_of(env, broken, "b") == {"up": {ll.UNLABELLED, ll.UNKNOWN_LINEAGE}}
    ghost = dataclasses.replace(
        base,
        stages=(
            base.stage("a"),
            dataclasses.replace(
                base.stage("b"), inputs=(NormalInput("stage", "g", stage="zzz", paths=("d",)),)
            ),
        ),
    )
    assert labels_of(env, ghost, "b") == {"g": {ll.UNLABELLED, ll.UNKNOWN_LINEAGE}}


def test_a_chain_deeper_than_the_limit_reads_as_unknown_lineage(env):
    add(env, env.snapshot_id, ["non-commercial"])
    depth = ll.MAX_DEPTH + 3
    stages = [stage("s0", [snap(env, "a", env.snapshot_id)])]
    stages += [stage(f"s{i}", [up("up", f"s{i - 1}")]) for i in range(1, depth + 1)]
    spec = spec_of(env, stages)
    deep = labels_of(env, spec, f"s{depth}")["up"]
    assert ll.UNKNOWN_LINEAGE in deep  # the bottom is out of reach: never silently clean
    near = labels_of(env, spec, "s3")["up"]
    assert near == {"non-commercial"}


# -- the provider rule ---------------------------------------------------------------------


def policy(allowed=None, provider="remote"):
    entry = {"data_classes": ["public", "private_derived"], "worker_principal_ids": ["w1"]}
    if allowed is not None:
        entry["allowed_license_labels"] = allowed
    return policy_from_document(
        {"schema": "tessera.provider-trust.v1", "providers": {provider: entry}}, "file"
    )


def test_a_provider_without_a_restriction_receives_anything():
    assert ll.license_refusal("remote", {"non-commercial", ll.UNLABELLED}, policy()) is None
    # A provider the trust file does not list may receive only unlabelled data (fail closed).
    assert ll.license_refusal("unlisted", {ll.UNLABELLED}, policy()) is None
    assert ll.license_refusal("unlisted", {"x"}, policy()) is not None
    assert ll.license_refusal("remote", {"x"}, None) is None


def test_a_restricted_provider_receives_only_allowed_labels():
    rule = policy(["cc-by", "public-domain"])
    assert ll.license_refusal("remote", {"cc-by"}, rule) is None
    assert ll.license_refusal("remote", {"cc-by", "public-domain"}, rule) is None
    message = ll.license_refusal("remote", {"cc-by", "non-commercial"}, rule)
    assert message and "'remote'" in message and "['non-commercial']" in message


def test_unlabelled_and_unknown_lineage_data_is_refused_unless_allowed():
    rule = policy(["cc-by"])
    assert "unlabelled" in ll.license_refusal("remote", {ll.UNLABELLED}, rule)
    assert ll.license_refusal("remote", {ll.UNLABELLED, ll.UNKNOWN_LINEAGE}, rule)
    assert ll.license_refusal("remote", {ll.UNLABELLED}, policy(["cc-by", ll.UNLABELLED])) is None
    # An unresolved lineage is never cleared by allowing unlabelled data.
    assert ll.license_refusal(
        "remote", {ll.UNLABELLED, ll.UNKNOWN_LINEAGE}, policy(["cc-by", ll.UNLABELLED])
    )


def test_a_pinned_public_input_is_unlabelled_not_unknown_lineage_and_never_hides_a_label(env):
    # A pinned public artifact is fetched by the provider and carries no operator label.
    spec = spec_with_public(env, [pub("weights")])
    assert labels_of(env, spec, "s1") == {"weights": {ll.UNLABELLED}}
    rule = policy([ll.UNLABELLED])
    assert (
        ll.license_refusal("remote", set().union(*labels_of(env, spec, "s1").values()), rule)
        is None
    )
    # Beside a labelled snapshot it adds nothing: the restricted label is still refused.
    add(env, env.snapshot_id, ["non-commercial"])
    mixed = spec_with_public(
        env,
        [
            spec_of(env, [stage("s1", [snap(env, "code", env.snapshot_id)])]).stages[0].inputs[0],
            pub("weights"),
        ],
    )
    carried = set().union(*labels_of(env, mixed, "s1").values())
    assert "non-commercial" in carried and ll.UNKNOWN_LINEAGE not in carried
    assert ll.license_refusal("remote", carried, rule) is not None
    # Downstream of a labelled snapshot the label still propagates past a public input.
    assert ll.license_refusal("remote", carried, policy(["cc-by", ll.UNLABELLED]))


# -- migration -----------------------------------------------------------------------------


def test_migration_adds_the_table_and_its_triggers_to_a_schema_85_database(env):
    path = env.state / "tessera.sqlite3"
    assert LICENSE_SCHEMA_VERSION == 85 <= LATEST_SCHEMA_VERSION
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE snapshot_license_labels")  # drops its triggers too
        db.execute("UPDATE schema_meta SET schema_version=?", (LICENSE_SCHEMA_VERSION - 1,))
    assert migrate_state(env.state) == LATEST_SCHEMA_VERSION
    assert migrate_state(env.state) == LATEST_SCHEMA_VERSION  # idempotent
    add(env, env.snapshot_id, ["cc-by"])
    with connect(env) as db:
        names = {
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_schema WHERE tbl_name=?", ("snapshot_license_labels",)
            )
        }
        assert {"snapshot_license_labels_no_delete", "snapshot_license_labels_add_only"} <= names
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("DELETE FROM snapshot_license_labels")
