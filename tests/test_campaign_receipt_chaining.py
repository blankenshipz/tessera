"""ADR 0080: a stage input that names a value on an earlier campaign's receipt."""

from __future__ import annotations

import json
import sqlite3

import pytest
from campaign_runner_harness import World, dump, stage_spec
from test_campaign_io import _finished
from test_campaign_models_v2 import _spec2

from tessera_control.campaign_models import SubmitCampaign, parse_spec
from tessera_control.store import Conflict, canonical_digest


def _consumer(world: World, row, **ref):
    env = world.env
    reference = {"kind": "receipt_output", "name": "prior", "stage": "s1", "output": row["name"]}
    reference.update(ref)
    stage = {
        "key": "reuse",
        "provider": "local-docker",
        "runtime": "docker-cpu-v1",
        "image": stage_spec(env, "x")["image"],
        "argv": ["python", "-c", "print(1)"],
        "outputs": [{"name": "result", "path": "data.json", "max_bytes": 1024}],
        "max_duration_seconds": 60,
        "inputs": [
            {
                "kind": "snapshot",
                "name": "code",
                "snapshot_id": env.snapshot_id,
                "digest": env.snapshot_digest,
                "data_class": "public",
            },
            reference,
        ],
    }
    spec = _spec2(stage)
    spec["stages"][0]["provider_config"] = {}
    return spec


def _submit(world: World, spec, predecessors=None, key="k"):
    body = {"spec": spec}
    if predecessors is not None:
        body["predecessors"] = predecessors
    env = world.env
    return env.store.submit_campaign(
        env.owner, env.project["id"], key, canonical_digest(body), body
    )[1]


@pytest.fixture
def chained(tmp_path):
    from tessera_control import campaign_provider_worker as worker

    world = World(tmp_path)
    first, row = _finished(world)
    world.env.trust()
    worker.register_from_config({"local_docker": {"worker_principal_id": world.env.worker["id"]}})
    try:
        yield world, first, row
    finally:
        worker.register_from_config(None)


def test_reference_models_need_exactly_one_source():
    base = {"kind": "receipt_output", "name": "n", "stage": "s", "output": "o"}
    from tessera_control.campaign_models import ReceiptOutputSource

    ReceiptOutputSource.model_validate({**base, "campaign_id": "campaign_a"})
    ReceiptOutputSource.model_validate({**base, "predecessor": "weights"})
    for bad in (base, {**base, "campaign_id": "campaign_a", "predecessor": "weights"}):
        with pytest.raises(ValueError):
            ReceiptOutputSource.model_validate(bad)
    assert SubmitCampaign.model_fields["predecessors"].default_factory() == {}


def test_submit_pins_the_receipt_value_and_records_lineage(chained):
    world, first, row = chained
    created = _submit(world, _consumer(world, row, predecessor="weights"), {"weights": first["id"]})
    pinned = created["spec"]["stages"][0]["inputs"][1]
    assert pinned["kind"] == "artifact" and pinned["sha256"] == row["sha256"]
    assert pinned["campaign_id"] == first["id"]
    assert pinned["resolved_from"]["predecessor"] == "weights"
    stored = world.row("SELECT spec_json FROM campaigns WHERE id=?", created["id"])
    assert "receipt_output" not in stored["spec_json"]
    digest = world.row(
        "SELECT receipt_digest FROM campaign_receipts WHERE campaign_id=?", first["id"]
    )["receipt_digest"]
    assert pinned["resolved_from"]["source_receipt_sha256"] == digest
    # The pinned spec is exactly what a hand-written artifact input would digest to,
    # plus the lineage the approver sees.
    parse_spec(created["spec"])


def test_chained_campaign_runs_and_its_receipt_carries_the_lineage(chained):
    world, first, row = chained
    created = _submit(world, _consumer(world, row, campaign_id=first["id"]))
    world.env.approve(created)
    world.tick_all(created["id"])
    assert world.status(created["id"]) == "succeeded", dump(world, created["id"])
    receipt = json.loads(
        world.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", created["id"])[
            "receipt_json"
        ]
    )
    [item] = receipt["chained_inputs"]
    assert item["campaign_id"] == first["id"] and item["sha256"] == row["sha256"]
    assert item["predecessor"] is None
    old = json.loads(
        world.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", first["id"])[
            "receipt_json"
        ]
    )
    assert "chained_inputs" not in old


@pytest.mark.parametrize(
    ("ref", "predecessors", "message"),
    [
        ({"predecessor": "weights"}, {"other": "campaign_x"}, "not in the submit"),
        ({"predecessor": "weights"}, None, "not in the submit"),
        ({"campaign_id": "campaign_missing"}, None, "no such campaign"),
        ({"campaign_id": "$FIRST", "output": "absent"}, None, "lists no succeeded output"),
        ({"campaign_id": "$FIRST", "stage": "nope"}, None, "lists no succeeded output"),
    ],
)
def test_refusals_name_the_input_and_reason(chained, ref, predecessors, message):
    world, first, row = chained
    ref = {k: (first["id"] if v == "$FIRST" else v) for k, v in ref.items()}
    with pytest.raises(Conflict, match=message) as caught:
        _submit(world, _consumer(world, row, **ref), predecessors)
    assert "'reuse'" in str(caught.value) and "'prior'" in str(caught.value)


def test_a_predecessor_that_has_not_succeeded_is_refused(chained):
    world, first, row = chained
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1")]
    pending = world.campaign(spec)  # submitted and approved, not run
    assert world.status(pending["id"]) != "succeeded"
    with pytest.raises(Conflict, match="not succeeded"):
        _submit(world, _consumer(world, row, campaign_id=pending["id"]))


def test_predecessors_that_no_input_uses_are_refused(chained):
    world, first, row = chained
    spec = _consumer(world, row, campaign_id=first["id"])
    spec["stages"][0]["inputs"][1] = {
        "kind": "artifact",
        "name": "prior",
        "campaign_id": first["id"],
        "stage": "s1",
        "output": row["name"],
        "sha256": row["sha256"],
    }
    with pytest.raises(Conflict, match="no input references one"):
        _submit(world, spec, {"weights": first["id"]})


def test_a_spec_without_references_is_unchanged(chained):
    world, first, row = chained
    spec = _consumer(world, row, campaign_id=first["id"])
    spec["stages"][0]["inputs"][1] = {
        "kind": "artifact",
        "name": "prior",
        "campaign_id": first["id"],
        "stage": "s1",
        "output": row["name"],
        "sha256": row["sha256"],
    }
    created = _submit(world, spec)
    assert "resolved_from" not in json.dumps(created["spec"])


# Spec digest of a hand-written artifact spec, computed on the base commit (56dac60), before
# receipt_output and resolved_from existed.
ARTIFACT_SPEC_DIGEST_AT_BASE = "c3c2faa1561369de76cf755847d7c316cfd65a7062f9cee4276dda79828f8d56"


def test_hand_written_artifact_spec_digest_is_unchanged_from_the_base_commit():
    from test_campaign_models_v2 import _stage2

    from tessera_control.campaign_models import spec_digest

    artifact = {
        "kind": "artifact",
        "name": "prior",
        "campaign_id": "campaign_abc",
        "stage": "a",
        "output": "result",
        "sha256": "a" * 64,
    }
    spec = _spec2(_stage2("a"), _stage2("b", artifact))
    assert spec_digest(parse_spec(spec)) == ARTIFACT_SPEC_DIGEST_AT_BASE


def test_submit_body_without_predecessors_dumps_as_before():
    body = SubmitCampaign(spec=_spec2()).model_dump(mode="json")
    assert set(body) == {"spec"}
    with_chain = SubmitCampaign(spec=_spec2(), predecessors={"a": "campaign_1"})
    assert with_chain.model_dump(mode="json")["predecessors"] == {"a": "campaign_1"}


def _pinned(first, row):
    return {
        "kind": "artifact",
        "name": "prior",
        "campaign_id": first["id"],
        "stage": "s1",
        "output": row["name"],
        "sha256": row["sha256"],
    }


def test_a_submitted_resolved_from_is_refused_everywhere(chained):
    world, first, row = chained
    env = world.env
    forged = _pinned(first, row)
    forged["resolved_from"] = {"predecessor": "made_up", "source_receipt_sha256": "f" * 64}
    spec = _consumer(world, row, campaign_id=first["id"])
    spec["stages"][0]["inputs"][1] = forged
    with pytest.raises(Conflict, match="resolved_from is set by submit only"):
        _submit(world, spec)
    body = {"spec": spec}
    with pytest.raises(Conflict, match="resolved_from is set by submit only"):
        env.store.validate_campaign_spec(env.owner, env.project["id"], body)
    standing = {
        "spec": spec,
        "expected_spec_digest": "0" * 64,
        "per_start_budget_cap_cents": 5,
        "max_starts": 1,
        "max_total_budget_cents": 5,
        "not_after": "2099-01-01T00:00:00+00:00",
    }
    with pytest.raises(Conflict, match="resolved_from is set by submit only"):
        env.store.create_standing_approval(
            env.human(), env.project["id"], "k", canonical_digest(standing), standing
        )


def test_resolver_output_with_a_mismatched_receipt_digest_is_refused(chained):
    from types import SimpleNamespace

    from tessera_control.campaign_io import resolve_artifact

    world, first, row = chained
    created = _submit(world, _consumer(world, row, campaign_id=first["id"]))
    pinned = created["spec"]["stages"][0]["inputs"][1]
    project = {"org_id": world.env.owner["org_id"], "project_id": world.env.project["id"]}
    conn = sqlite3.connect(world.env.state / "tessera.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        source = SimpleNamespace(**{k: v for k, v in pinned.items() if k != "resolved_from"})
        source.resolved_from = parse_spec(created["spec"]).stages[0].inputs[1].resolved_from
        resolve_artifact(conn, project, source)  # the real digest resolves
        source.resolved_from = source.resolved_from.model_copy(
            update={"source_receipt_sha256": "f" * 64}
        )
        with pytest.raises(Conflict, match="resolved_from does not match"):
            resolve_artifact(conn, project, source)
    finally:
        conn.close()


def test_validate_resolves_receipt_references_read_only(chained):
    world, first, row = chained
    env = world.env
    spec = _consumer(world, row, predecessor="weights")
    result = env.store.validate_campaign_spec(
        env.owner, env.project["id"], {"spec": spec, "predecessors": {"weights": first["id"]}}
    )
    created = _submit(world, spec, {"weights": first["id"]})
    assert result["valid"] and result["spec_digest"] == created["spec_digest"]
    with pytest.raises(Conflict, match="not in the submit"):
        env.store.validate_campaign_spec(env.owner, env.project["id"], {"spec": spec})
    with pytest.raises(Conflict, match="no such campaign"):
        env.store.validate_campaign_spec(
            env.owner,
            env.project["id"],
            {"spec": _consumer(world, row, campaign_id="campaign_missing")},
        )


def test_standing_approval_refuses_receipt_output_explicitly(chained):
    world, first, row = chained
    env = world.env
    spec = _consumer(world, row, campaign_id=first["id"])
    body = {
        "spec": spec,
        "expected_spec_digest": "0" * 64,
        "per_start_budget_cap_cents": 5,
        "max_starts": 1,
        "max_total_budget_cents": 5,
        "not_after": "2099-01-01T00:00:00+00:00",
    }
    with pytest.raises(Conflict, match="cannot use receipt_output"):
        env.store.create_standing_approval(
            env.human(), env.project["id"], "k", canonical_digest(body), body
        )


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_a_failed_or_cancelled_predecessor_is_refused_with_its_status(chained, status):
    world, first, row = chained
    world.env.execute("UPDATE campaigns SET status=? WHERE id=?", status, first["id"])
    with pytest.raises(Conflict, match=f"is {status}, not succeeded"):
        _submit(world, _consumer(world, row, campaign_id=first["id"]))


def test_a_predecessor_in_another_project_is_refused(chained):
    world, first, row = chained
    world.env.execute(
        "INSERT INTO projects(id, org_id, name, created_at) VALUES "
        "('project_other', ?, 'other', '2026-01-01T00:00:00Z')",
        world.env.owner["org_id"],
    )
    world.env.execute("UPDATE campaigns SET project_id='project_other' WHERE id=?", first["id"])
    with pytest.raises(Conflict, match="no such campaign in this project"):
        _submit(world, _consumer(world, row, campaign_id=first["id"]))
