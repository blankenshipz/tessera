"""A Salad interruption AFTER custody began is a bounded, retryable infrastructure outcome.

Fakes only (the Salad campaign world and its executor double). The provider moved the instance
after custody work started and its own read confirmed the stop: the attempt ends
``failed_transient / salad_interrupted``, spends one ``infra_retries`` and settles at the
guardian's accrued estimate. Without the provider's confirmation it stays unknown and held.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from provider_trust_fixture import DERIVED, install_trust
from salad_campaign_fixture import SaladWorld, salad_provider_reset, salad_stage  # noqa: F401
from test_campaign_salad_executor import RECEIPT
from test_salad_termination_confirm import _held, _ran_and_reallocated

from tessera_control.campaign_infra_retry import SALAD_INTERRUPTED, infra_attempts
from tessera_control.campaign_models import FAILURE_CLASSES

FINAL = dict(
    status="stopped",
    reason="allocation_changed",
    orphan=False,
    allocation_uncertain=True,  # what an allocation change sets; tolerated only for this cause
    escalation_pending=False,
    budget_fenced=False,
    rate_unknown=False,
    published_hourly_rate_usd="0.5",
    estimated_exposure_cents="3.5",
    actual_billed_cost_cents=None,
    observed_at="2026-11-19T00:00:00Z",
)
CUSTODY_BEGAN = dict(
    RECEIPT,
    status="inconclusive",
    reason="provider_stopped_custody_unsealed",
    stop_confirmed=False,
    guardian_stop_cause="allocation_changed",
    custody_work_begun=True,
    provider_stop_confirmed=True,
)


def stage(env, *, infra=1):
    return salad_stage(env, retry={"max_attempts": 1, "infra_retries": infra})


def world_for(tmp_path, *, infra=1) -> SaladWorld:
    return SaladWorld(tmp_path, stages=lambda env: [stage(env, infra=infra)])


def interrupted(world, *, confirmed=True, receipt=None, final=FINAL):
    world.to_route_started()
    reads = [dict(stopped=True)] if confirmed else [dict(stopped=False, allocation="alloc-b")]
    _ran_and_reallocated(world, reads)
    if confirmed:
        world.observe_exit()
    row = world.salad()
    world.env.store.record_salad_outcome(
        world.worker,
        row["attempt_id"],
        world.key("outcome"),
        dict(
            route_id=row["route_id"],
            live_receipt=receipt or CUSTODY_BEGAN,
            provider_id="provider-group-1",
            guardian_final=final,
        ),
    )


def test_the_class_is_known_and_counts_as_an_infrastructure_attempt():
    assert SALAD_INTERRUPTED in FAILURE_CLASSES
    assert infra_attempts([{"failure_class": SALAD_INTERRUPTED}, {"failure_class": None}]) == 1


def test_confirmed_post_custody_interruption_is_transient_spends_one_retry_and_settles_at_estimate(
    tmp_path,
):
    world = world_for(tmp_path)
    interrupted(world)
    world.tick_until(lambda: len(world.env.query("SELECT 1 FROM campaign_stage_attempts")) == 2)
    first = world.env.query(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND attempt=1",
        world.campaign_id,
    )[0]
    assert (first["status"], first["failure_class"]) == ("failed_transient", SALAD_INTERRUPTED)
    assert first["salad_verdict"] == "failed"  # never ok: its outputs are not accepted
    assert first["failure_detail"].startswith(f"salad:{SALAD_INTERRUPTED}")
    assert (first["settled_cents"], first["settlement_basis"]) == (4, "guardian_bounded_hold")
    assert infra_attempts(world.env.query("SELECT * FROM campaign_stage_attempts")) == 1
    second = world.attempt()
    assert second["attempt"] == 2 and second["grant_id"] != first["grant_id"]
    assert second["settled_cents"] is None  # its own hold, never the old claim
    held = _held(world)
    assert held["settled_cents"] == 4 and held["held_cents"] == second["hold_cents"]


def test_without_the_providers_confirmation_the_attempt_stays_unknown_and_held(tmp_path):
    world = world_for(tmp_path)
    interrupted(world, confirmed=False)
    world.tick_until(lambda: world.status() == "blocked")
    attempt = world.attempt()
    assert attempt["status"] == "unknown" and attempt["settled_cents"] is None
    assert attempt["failure_class"] != SALAD_INTERRUPTED
    assert _held(world)["held_cents"] == 20
    assert len(world.env.query("SELECT 1 FROM campaign_stage_attempts")) == 1


def test_retries_exhausted_fails_the_stage_with_salad_interrupted(tmp_path):
    world = world_for(tmp_path, infra=0)
    interrupted(world)
    world.tick_until(lambda: world.status() == "failed")
    attempt = world.attempt()
    assert (attempt["status"], attempt["failure_class"]) == ("failed_permanent", SALAD_INTERRUPTED)
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", world.campaign_id)
    assert stop["stop_reason"] == f"stage_failed:g1:{SALAD_INTERRUPTED}"
    assert "salad_not_passed" not in stop["stop_reason"]
    assert len(world.env.query("SELECT 1 FROM campaign_stage_attempts")) == 1


@pytest.mark.parametrize(
    "receipt",
    [
        dict(CUSTODY_BEGAN, custody_work_begun=None),  # unknown custody is never retryable
        dict(CUSTODY_BEGAN, guardian_stop_cause="deadline"),  # not an allocation change
    ],
)
def test_anything_but_a_confirmed_post_custody_allocation_change_keeps_todays_class(
    tmp_path, receipt
):
    world = world_for(tmp_path)
    interrupted(world, receipt=receipt)
    world.tick(12)  # it waits for a charge, as any failed run does: no automatic retry
    attempt = world.attempt()
    assert attempt["failure_class"] != SALAD_INTERRUPTED
    assert SALAD_INTERRUPTED not in str(attempt["failure_detail"])
    assert len(world.env.query("SELECT 1 FROM campaign_stage_attempts")) == 1


def test_an_estimate_above_the_hold_never_settles_an_interrupted_attempt_automatically(tmp_path):
    world = world_for(tmp_path)
    interrupted(world, final=dict(FINAL, estimated_exposure_cents="999"))
    world.tick(12)
    assert world.attempt()["settled_cents"] is None
    assert json.loads(world.row("SELECT guardian_final_json FROM campaign_salad_attempts")[0])


# -- the salvaged checkpoint ----------------------------------------------------------------

CHECKPOINT = b'{"step": 19000}'


def checkpoint_world(tmp_path, *, declared=True, infra=1, max_bytes=4096) -> SaladWorld:
    from salad_campaign_fixture import salad_spec_v2, salad_stage_v2

    world = SaladWorld(tmp_path)
    # A declared checkpoint needs the diagnostic output beside it (the runtime uploads it last).
    outputs = (
        [
            {"name": "resume_state", "path": "checkpoint.bin"},
            {"name": "diagnostic", "path": "runtime-diagnostic.log"},
        ]
        if declared
        else [{"name": "result", "path": "output.json"}]
    )
    stage = salad_stage_v2(
        world.env,
        "g1",
        outputs=[dict(o, max_bytes=max_bytes) for o in outputs],
        retry={"max_attempts": 1, "infra_retries": infra},
    )
    # The contract declares the same files the stage does (its output_paths must match).
    stage["provider_config"]["workload"]["outputs"] = [
        {"name": o["path"], "max_bytes": max_bytes} for o in outputs
    ]
    world.spec = salad_spec_v2(world.env, [stage])
    # A checkpoint is recorded private_derived data: the provider must be trusted to receive it.
    install_trust({"salad": (DERIVED, [world.env.worker["id"]])})
    return world


def interrupted_with_manifest(world, content=CHECKPOINT, path="checkpoint.bin"):
    """The route was interrupted after custody holding the reserved checkpoint file."""
    world.to_route_started()
    _ran_and_reallocated(world, [dict(stopped=True)])
    # The diagnostic never arrived: the executor's partial manifest lists what reached custody.
    world.record_custody()
    manifest = world.upload_manifest(content, path, placed=True, partial=True)
    world.observe_exit()
    row = world.salad()
    world.env.store.record_salad_outcome(
        world.worker,
        row["attempt_id"],
        world.key("outcome"),
        dict(
            route_id=row["route_id"],
            live_receipt=CUSTODY_BEGAN,
            manifest_id=manifest["id"],
            provider_id="provider-group-1",
            guardian_final=FINAL,
        ),
    )


def next_inputs(world):
    from types import SimpleNamespace

    campaign = dict(world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id))
    return world.runner.bound_inputs(SimpleNamespace(campaign=campaign), "g1")


def test_a_salvaged_checkpoint_is_recorded_and_bound_by_digest_for_the_retry(tmp_path):
    import hashlib

    world = checkpoint_world(tmp_path)
    interrupted_with_manifest(world)
    world.tick_until(lambda: len(world.env.query("SELECT 1 FROM campaign_stage_attempts")) == 2)
    events = world.events("campaign.interrupted_checkpointed")
    digest = hashlib.sha256(CHECKPOINT).hexdigest()
    assert len(events) == 1
    assert events[0]["outcome"] == "interrupted_checkpointed" and events[0]["attempt"] == 1
    assert (events[0]["digest"], events[0]["bytes"]) == (digest, len(CHECKPOINT))
    bound = next_inputs(world)
    assert [(i.name, i.digest, i.bytes) for i in bound] == [
        ("resume_state", digest, len(CHECKPOINT))
    ]
    assert bound[0].source_stage == "g1" and bound[0].data_class == "private_derived"
    first = world.env.query("SELECT * FROM campaign_stage_attempts WHERE attempt=1")[0]
    assert first["salad_verdict"] == "failed"  # the interrupted attempt's outputs stay unaccepted
    world.tick(3)
    assert len(world.events("campaign.interrupted_checkpointed")) == 1  # once


def test_without_a_salvaged_checkpoint_the_retry_starts_clean(tmp_path):
    world = world_for(tmp_path)  # nothing reached custody
    interrupted(world)
    world.tick_until(lambda: len(world.env.query("SELECT 1 FROM campaign_stage_attempts")) == 2)
    assert world.events("campaign.interrupted_checkpointed") == []
    assert next_inputs(world) == ()


def test_a_stage_without_a_declared_checkpoint_never_binds_an_output_as_one(tmp_path):
    world = checkpoint_world(tmp_path, declared=False)
    interrupted_with_manifest(world, path="output.json")
    world.tick_until(lambda: len(world.env.query("SELECT 1 FROM campaign_stage_attempts")) == 2)
    assert world.events("campaign.interrupted_checkpointed") == []
    assert next_inputs(world) == ()


def test_a_checkpoint_over_its_declared_size_is_not_salvaged(tmp_path):
    from types import SimpleNamespace

    from tessera_control.campaign_checkpoint import salvaged_checkpoint

    world = checkpoint_world(tmp_path)
    interrupted_with_manifest(world)
    world.tick_until(lambda: len(world.env.query("SELECT 1 FROM campaign_stage_attempts")) == 2)
    campaign = dict(world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id))
    attempt = dict(world.env.query("SELECT * FROM campaign_stage_attempts WHERE attempt=1")[0])

    def read(max_bytes, name="resume_state", path="checkpoint.bin"):
        declared = SimpleNamespace(
            outputs=[SimpleNamespace(name=name, path=path, max_bytes=max_bytes)]
        )
        with world.runner.store.runner_connection() as connection:
            return salvaged_checkpoint(connection, campaign, declared, attempt)

    assert read(len(CHECKPOINT)) is not None
    assert read(len(CHECKPOINT) - 1) is None  # over the declared cap
    assert read(4096, path="other.bin") is None  # not the declared file
    assert read(4096, path="output.json") is None  # no declared checkpoint output
    other = dict(campaign, project_id="project_other")  # another tenant never reads it
    with world.runner.store.runner_connection() as connection:
        declared = SimpleNamespace(
            outputs=[SimpleNamespace(name="checkpoint", path="output.json", max_bytes=9)]
        )
        assert salvaged_checkpoint(connection, other, declared, attempt) is None


def test_the_receipt_lists_the_checkpoint_by_digest(tmp_path):
    import hashlib

    from tessera_control.campaign_receipts import _bound_section

    world = checkpoint_world(tmp_path)
    interrupted_with_manifest(world)
    world.tick_until(lambda: len(world.env.query("SELECT 1 FROM campaign_stage_attempts")) == 2)
    campaign = dict(world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id))
    with world.runner.store.runner_connection() as connection:
        section = _bound_section(connection, campaign, "g1")
    (entry,) = section["late_bound_inputs"]
    assert entry["digest"] == hashlib.sha256(CHECKPOINT).hexdigest()
    assert (entry["name"], entry["source"], entry["attempt"]) == (
        "resume_state",
        "interrupted_checkpointed",
        1,
    )


def test_a_later_failure_of_another_kind_stops_the_resume_from_an_older_checkpoint(tmp_path):
    world = checkpoint_world(tmp_path)
    interrupted_with_manifest(world)
    world.tick_until(lambda: len(world.env.query("SELECT 1 FROM campaign_stage_attempts")) == 2)
    assert [i.name for i in next_inputs(world)] == ["resume_state"]  # attempt 2 resumes
    # Attempt 2 then fails for the workload's own reasons: attempt 3 must start clean.
    world.env.execute(
        "UPDATE campaign_stage_attempts SET status='failed_transient', "
        "failure_class='salad_not_passed' WHERE attempt=2"
    )
    assert next_inputs(world) == ()
    # The earlier checkpoint stays on the record for the receipt.
    assert len(world.events("campaign.interrupted_checkpointed")) == 1


def test_the_checkpoint_reaches_the_retrys_composed_snapshot_and_its_input_list(tmp_path):
    """The real late-bind path: the retry's input snapshot is composed with the checkpoint, and
    the retry's recorded inputs and the receipt list it by digest."""
    import hashlib

    world = checkpoint_world(tmp_path)
    interrupted_with_manifest(world)
    world.tick_until(
        lambda: world.attempt()["attempt"] == 2 and world.attempt()["trust_policy_digest"]
    )
    digest = hashlib.sha256(CHECKPOINT).hexdigest()
    stage = world.row("SELECT * FROM campaign_stages WHERE stage_key='g1'")
    snapshot = world.row(
        "SELECT manifest_json FROM source_snapshots WHERE id=?", stage["input_snapshot_id"]
    )
    files = {e["relative_path"]: e for e in json.loads(snapshot["manifest_json"])["entries"]}
    entry = files["resume_state/checkpoint.bin"]
    assert (entry["sha256"], entry["size"]) == (digest, len(CHECKPOINT))
    assert any(path.startswith("code/") for path in files)  # the base inputs are still there
    composed = world.events("campaign.checkpoint_snapshot_composed")
    assert len(composed) == 1 and composed[0]["snapshot_id"] == stage["input_snapshot_id"]
    inputs = world.env.query(
        "SELECT name, sha256 FROM campaign_stage_io WHERE attempt_id=? AND direction='input'",
        world.attempt()["id"],
    )
    assert ("resume_state/checkpoint.bin", digest) in [(r["name"], r["sha256"]) for r in inputs]
    first = world.env.query("SELECT id FROM campaign_stage_attempts WHERE attempt=1")[0]
    first_inputs = world.env.query(
        "SELECT name FROM campaign_stage_io WHERE attempt_id=? AND direction='input'", first["id"]
    )
    assert "resume_state/checkpoint.bin" not in [r["name"] for r in first_inputs]


def test_a_later_attempt_after_another_kind_of_failure_gets_the_base_snapshot_again(tmp_path):
    """Predecessor-only resume on the real path: attempt 2 resumed from attempt 1's checkpoint;
    once attempt 2 failed for another reason, attempt 3's snapshot has no checkpoint."""
    world = checkpoint_world(tmp_path)
    interrupted_with_manifest(world)
    world.tick_until(
        lambda: world.attempt()["attempt"] == 2 and world.attempt()["trust_policy_digest"]
    )
    base = world.env.query("SELECT digest FROM source_snapshots WHERE id=?", world.env.snapshot_id)
    stage = world.row("SELECT * FROM campaign_stages WHERE stage_key='g1'")
    assert stage["input_snapshot_digest"] != base[0]["digest"]  # attempt 2: with the checkpoint
    world.env.execute(
        "UPDATE campaign_stage_attempts SET status='failed_transient', "
        "failure_class='salad_not_passed' WHERE attempt=2"
    )
    ctx = world.runner._load(world.campaign_id)
    spec_stage = ctx.spec.stage("g1")
    third = {"attempt": 3, "id": "attempt-3-stand-in"}
    restored = world.runner._compose_for_retry(ctx, dict(stage), spec_stage, third)
    assert restored["input_snapshot_digest"] == base[0]["digest"]
    assert restored["input_snapshot_id"] == world.env.snapshot_id
    assert world.runner._input_entries(ctx, spec_stage, restored["input_snapshot_id"]) is not None
    names = [e.name for e in world.runner._input_entries(ctx, spec_stage, world.env.snapshot_id)]
    assert not any(name.startswith("resume_state/") for name in names)


# -- the partial manifest of an interrupted attempt ------------------------------------------


def test_the_interrupted_attempts_manifest_lists_only_what_reached_custody(tmp_path):
    from tessera_control.artifact_manifests import manifest_is_partial

    world = checkpoint_world(tmp_path)
    interrupted_with_manifest(world)
    row = world.salad()
    manifest = world.row("SELECT * FROM artifact_manifests WHERE id=?", row["manifest_id"])
    listed = json.loads(manifest["artifacts_json"])
    assert [item["relative_path"] for item in listed] == ["checkpoint.bin"]
    assert listed[0]["sha256"] == hashlib.sha256(CHECKPOINT).hexdigest()
    assert manifest_is_partial(listed, ["checkpoint.bin", "runtime-diagnostic.log"])
    assert not manifest_is_partial(listed, ["checkpoint.bin"])


def test_a_manifest_missing_declared_files_is_refused_without_the_partial_mark(tmp_path):
    from tessera_control.store import StoreError

    world = checkpoint_world(tmp_path)
    world.to_route_started()
    _ran_and_reallocated(world, [dict(stopped=True)])
    with pytest.raises(Exception) as refused:
        world.upload_manifest(CHECKPOINT, "checkpoint.bin", placed=True)
    assert "declared output paths" in str(refused.value) or isinstance(refused.value, StoreError)


def test_a_partial_manifest_never_lists_an_undeclared_file(tmp_path):
    world = checkpoint_world(tmp_path)
    world.to_route_started()
    _ran_and_reallocated(world, [dict(stopped=True)])
    with pytest.raises(Exception, match="not declared by the approved plan"):
        world.upload_manifest(b"{}", "extra.json", placed=True, partial=True)


def test_a_partial_manifest_never_verifies_as_a_pass(tmp_path):
    """Even a passed live receipt cannot turn a manifest that lacks declared files into ok."""
    world = checkpoint_world(tmp_path)
    world.to_route_started()
    _ran_and_reallocated(world, [dict(stopped=True)])
    world.record_custody()
    manifest = world.upload_manifest(CHECKPOINT, "checkpoint.bin", placed=True, partial=True)
    world.observe_exit()
    row = world.salad()
    passed = dict(
        status="passed",
        reason="verified_journey",
        stop_confirmed=True,
        outputs=[
            dict(
                name="checkpoint.bin",
                sha256=hashlib.sha256(CHECKPOINT).hexdigest(),
                bytes=len(CHECKPOINT),
            )
        ],
    )
    world.env.store.record_salad_outcome(
        world.worker,
        row["attempt_id"],
        world.key("outcome"),
        dict(
            route_id=row["route_id"],
            live_receipt=passed,
            manifest_id=manifest["id"],
            provider_id="provider-group-1",
            guardian_final=FINAL,
        ),
    )
    world.tick_until(lambda: world.attempt()["status"] != "executing")
    assert world.attempt()["salad_verdict"] != "ok"


def _guardian_manifest(files, *, complete, declared_sorted=True):
    from tessera_control.store import canonical_digest

    binding = dict(tenant="t", project="p", operation="o", epoch=1, artifact="a", kind="output")
    inventory = dict(
        binding=binding, files=files, complete=complete, writers_closed=True, quarantine=True
    )
    inventory["digest"] = canonical_digest(inventory)
    manifest = dict(
        inventories=[inventory], writers_closed=True, quarantine=True, complete=complete
    )
    manifest["digest"] = canonical_digest(manifest)
    return binding, manifest


def _entry(name, data):
    return dict(name=name, sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))


def test_the_import_verifier_accepts_a_partial_manifest_only_when_asked():
    from tessera_control.salad_output_import import OutputImportError, verify_manifest

    declared = {"checkpoint.bin": 4096, "runtime-diagnostic.log": 4096}
    binding, manifest = _guardian_manifest([_entry("checkpoint.bin", CHECKPOINT)], complete=False)
    kwargs = dict(binding=binding, declared=declared, max_total=8192)
    with pytest.raises(OutputImportError):
        verify_manifest(manifest, **kwargs)
    files = verify_manifest(manifest, partial=True, **kwargs)
    assert [f["name"] for f in files] == ["checkpoint.bin"]


@pytest.mark.parametrize(
    "files",
    [
        [dict(name="other.bin", sha256="0" * 64, bytes=0)],  # not declared
        [_entry("checkpoint.bin", b"x" * 5000)],  # over its cap
        [_entry("checkpoint.bin", b"a"), _entry("checkpoint.bin", b"a")],  # repeated
    ],
)
def test_a_partial_manifest_still_checks_every_listed_file(files):
    from tessera_control.salad_output_import import OutputImportError, verify_manifest

    binding, manifest = _guardian_manifest(files, complete=False)
    with pytest.raises(OutputImportError):
        verify_manifest(
            manifest,
            partial=True,
            binding=binding,
            declared={"checkpoint.bin": 4096, "runtime-diagnostic.log": 4096},
            max_total=8192,
        )
