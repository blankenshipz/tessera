"""Late binding of in-campaign stage-output inputs (ADR 0140); fake worker, no network.

A two-stage chain runs on the local provider: the dependent stage binds the upstream output
at start, pins its digest, and the receipt records the binding. The resolver then fails
closed on a missing, unsettled or failed upstream and on a digest mismatch, and the
post-resolution routing re-check refuses a restricted upstream artifact for a provider the
trust setting does not admit for it (a Salad-like remote provider id; nothing is contacted).
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from campaign_runner_harness import IMAGE, World, dump
from provider_trust_fixture import DERIVED

from tessera_control import campaign_provider_worker as worker
from tessera_control import late_bound_inputs as late_bound
from tessera_control import license_labels
from tessera_control.campaign_models import NormalInput, normalize, parse_spec_json
from tessera_control.late_bound_inputs import LateBindRefused, ResolvedStageInput
from tessera_control.provider_trust import install_policy, policy_from_document, record_policy
from tessera_control.store import _connect, utc_now

REMOTE = "salad"


@pytest.fixture(autouse=True)
def _local_docker_worker():
    yield
    worker.register_from_config(None)


def _world(tmp_path) -> World:
    world = World(tmp_path)
    worker.register_from_config({"local_docker": {"worker_principal_id": world.env.worker["id"]}})
    return world


def _stage(key: str, upstream: str | None, snapshot: dict) -> dict:
    inputs = [snapshot]
    if upstream:
        inputs.append({"kind": "stage", "name": "up", "stage": upstream, "outputs": ["result"]})
    return {
        "key": key,
        "provider": "local-docker",
        "runtime": "docker-cpu-v1",
        "image": IMAGE,
        "argv": ["python", "-c", "print(1)"],
        "outputs": [{"name": "result", "path": "data.json", "kind": "metrics", "max_bytes": 1024}],
        "max_duration_seconds": 60,
        "inputs": inputs,
    }


def _chain(env) -> dict:
    snapshot = {
        "kind": "snapshot",
        "name": "code",
        "snapshot_id": env.snapshot_id,
        "digest": env.snapshot_digest,
        "data_class": "public",
    }
    return {
        "version": 2,
        "name": "c",
        "budget_cap_cents": 10,
        "deadline_seconds": 3600,
        "stages": [_stage("a", None, snapshot), _stage("b", "a", snapshot)],
    }


@pytest.fixture
def ran(tmp_path):
    """A finished a -> b chain: (world, campaign row, normalized spec)."""
    world = _world(tmp_path)
    world.scenarios = [(0, {"data.json": '{"score": 1}'}), (0, {"data.json": '{"score": 2}'})]
    campaign = world.campaign(_chain(world.env))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    row = world.row("SELECT * FROM campaigns WHERE id=?", campaign["id"])
    return world, dict(row), normalize(parse_spec_json(row["spec_json"]))


def _db(world):
    return _connect(world.env.state / "tessera.sqlite3")


def _resolve(world, campaign, stage, **kwargs):
    connection = _db(world)
    try:
        scope = {"org_id": campaign["org_id"], "project_id": campaign["project_id"], **kwargs}
        return late_bound.resolve_stage_inputs(connection, campaign["id"], stage, **scope)
    finally:
        connection.close()


def _upstream_output(world, campaign):
    return world.row(
        "SELECT sha256, size_bytes, manifest_id FROM campaign_stage_io WHERE campaign_id=? "
        "AND stage_key='a' AND direction='output'",
        campaign["id"],
    )


def _refused(world, campaign, stage, code, **kwargs):
    with pytest.raises(LateBindRefused) as caught:
        _resolve(world, campaign, stage, **kwargs)
    assert caught.value.code == code
    return caught.value


def test_dependent_stage_binds_upstream_output_and_receipt_records_the_lineage_edge(ran):
    world, campaign, _spec = ran
    output = _upstream_output(world, campaign)
    expected = {
        "name": "up",
        "source_stage": "a",
        "output": "result",
        "digest": output["sha256"],
        "bytes": output["size_bytes"],
        "data_class": "private_derived",
        "license_labels": ["unlabelled"],
        "path": "data.json",
        "manifest_id": output["manifest_id"],
    }
    receipt = json.loads(
        world.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", campaign["id"])[
            "receipt_json"
        ]
    )
    stages = {s["stage_key"]: s for s in receipt["stages"]}
    (entry,) = stages["b"]["input"][late_bound.LAUNCH_INPUTS_KEY]
    assert entry.pop("bound_at")  # binding event timestamp; see test_late_bound_receipt_fields
    assert [entry] == [expected]
    # A stage without stage-output inputs keeps its receipt shape.
    assert late_bound.LAUNCH_INPUTS_KEY not in stages["a"]["input"]
    events = [e["payload"] for e in world.events_of(late_bound.BOUND_EVENT)]
    assert [e["stage_key"] for e in events] == ["b"]
    assert events[0][late_bound.LAUNCH_INPUTS_KEY] == [expected]
    # The composed input of b was built from exactly the bound digest.
    pinned = world.row(
        "SELECT sha256 FROM campaign_stage_io WHERE campaign_id=? AND stage_key='b' "
        "AND direction='input' AND name='up/data.json'",
        campaign["id"],
    )
    assert pinned["sha256"] == output["sha256"]
    # What a provider reads through the launch inputs: resolved digests only.
    connection = _db(world)
    try:
        bound = late_bound.bound_inputs(connection, campaign, "b")
    finally:
        connection.close()
    assert bound == (ResolvedStageInput.from_json(expected),)
    assert late_bound.launch_payload(bound) == {late_bound.LAUNCH_INPUTS_KEY: [expected]}


def test_resolver_returns_the_recorded_digest_and_keeps_an_earlier_binding(ran):
    world, campaign, spec = ran
    output = _upstream_output(world, campaign)
    (item,) = _resolve(world, campaign, spec.stage("b"))
    assert (item.source_stage, item.output, item.digest, item.bytes) == (
        "a",
        "result",
        output["sha256"],
        output["size_bytes"],
    )
    same = _resolve(world, campaign, spec.stage("b"), expected={("up", "result"): item.digest})
    assert same == (item,)
    error = _refused(
        world, campaign, spec.stage("b"), "digest_mismatch", expected={("up", "result"): "0" * 64}
    )
    assert error.input_name == "up"


def test_resolver_fails_closed_on_missing_upstream_or_output_and_on_another_tenant(ran):
    world, campaign, spec = ran
    stage = spec.stage("b")
    ghost = NormalInput("stage", "up", stage="ghost", paths=("data.json",))
    _refused(world, campaign, dataclasses.replace(stage, inputs=(ghost,)), "upstream_missing")
    undeclared = NormalInput("stage", "up", stage="a", paths=("other.json",))
    _refused(world, campaign, dataclasses.replace(stage, inputs=(undeclared,)), "output_missing")
    with pytest.raises(LateBindRefused) as caught:
        _connection = _db(world)
        try:
            late_bound.resolve_stage_inputs(
                _connection, campaign["id"], stage, org_id="org_other", project_id="prj_other"
            )
        finally:
            _connection.close()
    assert caught.value.code == "campaign_not_found"


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("UPDATE campaign_stages SET status='running' WHERE campaign_id=? AND stage_key='a'",
         "upstream_not_settled"),
        ("UPDATE campaign_stage_attempts SET settled_cents=NULL, settlement_basis=NULL "
         "WHERE campaign_id=? AND stage_key='a'", "upstream_not_settled"),
        ("UPDATE campaign_stages SET status='failed' WHERE campaign_id=? AND stage_key='a'",
         "upstream_not_succeeded"),
        ("UPDATE campaign_stage_attempts SET status='failed_permanent' "
         "WHERE campaign_id=? AND stage_key='a'", "upstream_not_succeeded"),
        ("UPDATE campaign_stage_attempts SET manifest_digest='" + "f" * 64 + "' "
         "WHERE campaign_id=? AND stage_key='a'", "digest_mismatch"),
    ],
)  # fmt: skip
def test_resolver_fails_closed_on_an_unsettled_failed_or_mismatched_upstream(ran, sql, code):
    world, campaign, spec = ran
    world.env.execute(sql, campaign["id"])
    _refused(world, campaign, spec.stage("b"), code)


def _policy(world, remote_classes, remote_labels=None):
    entries = {
        "local-docker": {"data_classes": DERIVED, "worker_principal_ids": [world.env.worker["id"]]},
        REMOTE: {"data_classes": list(remote_classes), "worker_principal_ids": ["wrk_remote"]},
    }
    if remote_labels is not None:
        entries[REMOTE]["allowed_license_labels"] = list(remote_labels)
    policy = policy_from_document(
        {"schema": "tessera.provider-trust.v1", "providers": entries}, "file"
    )
    install_policy(policy)
    connection = _db(world)
    try:
        with connection:
            record_policy(connection, policy, "operator", utc_now())
    finally:
        connection.close()


def _route(world, stage, resolved, provider):
    connection = _db(world)
    try:
        return late_bound.check_resolved_routing(
            connection,
            dataclasses.replace(stage, provider=provider),
            resolved,
            worker="wrk_remote" if provider == REMOTE else world.env.worker["id"],
        )
    finally:
        connection.close()


def test_routing_recheck_refuses_a_derived_upstream_artifact_for_a_public_only_remote(ran):
    world, campaign, spec = ran
    resolved = _resolve(world, campaign, spec.stage("b"))
    _policy(world, ["public"])
    with pytest.raises(LateBindRefused) as caught:
        _route(world, spec.stage("b"), resolved, REMOTE)
    assert caught.value.code == "data_class_refused"
    assert _route(world, spec.stage("b"), resolved, "local-docker") == {"unlabelled"}


def test_routing_recheck_refuses_a_restricted_licence_label_inherited_by_the_upstream_output(ran):
    world, campaign, spec = ran
    connection = _db(world)
    try:
        with connection:
            license_labels.add_snapshot_labels(
                connection,
                {"id": world.env.owner["id"], "org_id": world.env.owner["org_id"]},
                world.env.snapshot_id,
                ["non-commercial"],
                utc_now(),
            )
    finally:
        connection.close()
    (item,) = _resolve(world, campaign, spec.stage("b"))
    assert item.license_labels == ("non-commercial",)
    _policy(world, DERIVED, remote_labels=["unlabelled"])
    with pytest.raises(LateBindRefused) as caught:
        _route(world, spec.stage("b"), (item,), REMOTE)
    assert caught.value.code == "license_refused"
    assert "non-commercial" in caught.value.detail


def test_a_bind_refusal_fails_the_dependent_stage_with_its_closed_code(tmp_path, monkeypatch):
    world = _world(tmp_path)

    def refuse(*_args, **_kwargs):
        raise LateBindRefused("upstream_not_settled", "test refusal", input_name="up")

    monkeypatch.setattr(late_bound, "resolve_stage_inputs", refuse)
    campaign = world.campaign(_chain(world.env))
    world.tick_all(campaign["id"])
    assert world.stages(campaign["id"]) == {"a": "succeeded", "b": "failed"}
    stop = world.row("SELECT stop_reason, held_cents FROM campaigns WHERE id=?", campaign["id"])
    assert stop["stop_reason"] == "stage_failed:b:late_bind_refused:upstream_not_settled"
    assert stop["held_cents"] == 0
    (event,) = [e["payload"] for e in world.events_of(late_bound.REFUSED_EVENT)]
    assert (event["stage_key"], event["code"], event["input"]) == (
        "b",
        "upstream_not_settled",
        "up",
    )
    assert world.events_of(late_bound.BOUND_EVENT) == []
    # Nothing ran for b: no grant was ever issued to it.
    assert all(
        a["grant_id"] is None for a in world.attempts(campaign["id"]) if a["stage_key"] == "b"
    )


def test_refusal_codes_are_closed():
    with pytest.raises(ValueError):
        LateBindRefused("something_else")
    assert {"upstream_missing", "upstream_not_settled", "digest_mismatch"} <= (
        late_bound.REFUSAL_CODES
    )
