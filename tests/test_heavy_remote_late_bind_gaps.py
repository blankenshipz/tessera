# ruff: noqa: F811
"""A heavy stage that got the operator's Salad candidate at submit (ADR 0141) and reads another
stage's output binds that output at stage start exactly as an explicit Salad request does
(ADR 0140): a missing, unsettled or differing upstream output fails the stage closed before
anything reaches Salad, with the closed code on the refusal event and the receipt.

Fakes only (``SaladWorld`` doubles, development worker on a CPU host); no Salad call is made.
These use the real resolver against altered upstream records unless the test says otherwise.
"""

from __future__ import annotations

import dataclasses
import inspect
import json

from salad_campaign_fixture import salad_provider_reset  # noqa: F401
from test_chained_heavy_salad_candidate import (
    _configure,
    _dag,
    _dependent,
    _reset,  # noqa: F401
    _run_upstream,
    _setup,
)
from test_salad_late_bound_inputs import UP, _stage_row, _to_salad_stage, host  # noqa: F401

from tessera_control import campaign_salad_adapter as adapter
from tessera_control import late_bound_inputs
from tessera_control.campaign_receipts import build_receipt
from tessera_control.campaign_salad import (
    LATE_BIND_MISMATCH,
    LATE_BIND_REFUSED_EVENT,
    LATE_BOUND_EVENT,
)


def _started(tmp_path):
    _configure()
    world = _setup(tmp_path)
    world.spec = _dag(world)
    world.start()
    assert _dependent(world)["heavy_remote_candidate"]["added"] is True
    _run_upstream(world, tmp_path)
    return world


def _alter(world, sql, *params):
    with world.env.store.runner_connection() as connection:
        changed = connection.execute(sql, (*params, world.campaign_id, UP)).rowcount
    assert changed == 1


def _failed_closed(world):
    """Drive the dependent stage to its end; it must fail with nothing sent to Salad."""
    _to_salad_stage(world)
    world.tick_until(lambda: _stage_row(world)["status"] == "failed")
    assert world.events(LATE_BOUND_EVENT) == []
    assert world.row("SELECT 1 FROM campaign_salad_attempts") is None  # nothing reached Salad
    return world.row("SELECT stop_reason FROM campaigns WHERE id=?", world.campaign_id)[
        "stop_reason"
    ]


def _receipt_entry(world):
    with world.env.store.runner_connection() as connection:
        campaign = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (world.campaign_id,)
        ).fetchone()
        receipt, _ = build_receipt(connection, campaign, "failed")
    return next(s for s in receipt["stages"] if s["stage_key"] == "g1"), receipt


def _runner_refusal(world, code):
    (refused,) = world.events(late_bound_inputs.REFUSED_EVENT)
    assert refused["code"] == code and refused["input"] == "up"
    assert refused["source_stage"] == UP and refused["provider"] == "salad"
    entry, _ = _receipt_entry(world)
    assert entry["input"][late_bound_inputs.LAUNCH_INPUTS_KEY][-1] == {
        "status": late_bound_inputs.NOT_PINNED,
        "code": code,
        "input": "up",
        "source_stage": UP,
    }


# -- case 4: digest mismatch at stage start ----------------------------------------------------


def test_a_succeeded_attempt_whose_manifest_digest_differs_is_refused_at_start(tmp_path, host):
    # The recorded output rows are append-only; the attempt's manifest digest is what can drift.
    world = _started(tmp_path)
    _alter(
        world,
        "UPDATE campaign_stage_attempts SET manifest_digest=? WHERE campaign_id=? "
        "AND stage_key=? AND status='succeeded'",
        "f" * 64,
    )
    stop = _failed_closed(world)
    assert stop == "stage_failed:g1:late_bind_refused:digest_mismatch"
    _runner_refusal(world, "digest_mismatch")


def test_a_resolved_digest_the_runner_cannot_compose_is_refused(tmp_path, host, monkeypatch):
    world = _started(tmp_path)
    real = late_bound_inputs.resolve_stage_inputs

    def fake(*args, **kwargs):
        return tuple(dataclasses.replace(r, digest="e" * 64) for r in real(*args, **kwargs))

    monkeypatch.setattr(late_bound_inputs, "resolve_stage_inputs", fake)
    assert _failed_closed(world) == "stage_failed:g1:step_refused"


def test_the_salad_driver_refuses_a_snapshot_that_differs_from_its_own_resolution(
    tmp_path, host, monkeypatch
):
    """The runner binds and composes the real bytes; the driver's own re-resolution at stage
    start then reports another digest, so the composed snapshot no longer matches it."""
    world = _started(tmp_path)
    real = late_bound_inputs.resolve_stage_inputs

    def differ_in_the_driver(*args, **kwargs):
        resolved = real(*args, **kwargs)
        if any(f.filename.endswith("campaign_salad.py") for f in inspect.stack()):
            return tuple(dataclasses.replace(r, digest="e" * 64) for r in resolved)
        return resolved

    monkeypatch.setattr(late_bound_inputs, "resolve_stage_inputs", differ_in_the_driver)
    stop = _failed_closed(world)
    assert stop == f"stage_failed:g1:{LATE_BIND_MISMATCH}"
    (salad_event,) = world.events(LATE_BIND_REFUSED_EVENT)
    (event,) = world.events(late_bound_inputs.REFUSED_EVENT)
    assert salad_event["code"] == event["code"] == LATE_BIND_MISMATCH
    assert event["provider"] == "salad"


# -- case 3: upstream missing, unsettled or not succeeded (real resolver) ----------------------


def test_a_succeeded_upstream_without_its_collected_outputs_is_refused(tmp_path, host):
    world = _started(tmp_path)
    _alter(
        world,
        "UPDATE campaign_stage_attempts SET manifest_id=NULL WHERE campaign_id=? "
        "AND stage_key=? AND status='succeeded'",
    )
    assert _failed_closed(world) == "stage_failed:g1:late_bind_refused:output_missing"
    _runner_refusal(world, "output_missing")


def test_an_unsettled_upstream_attempt_is_refused(tmp_path, host):
    world = _started(tmp_path)
    _alter(
        world,
        "UPDATE campaign_stage_attempts SET settled_cents=NULL WHERE campaign_id=? "
        "AND stage_key=? AND status='succeeded'",
    )
    assert _failed_closed(world) == "stage_failed:g1:late_bind_refused:upstream_not_settled"
    _runner_refusal(world, "upstream_not_settled")


def test_an_upstream_without_a_succeeded_attempt_is_refused(tmp_path, host):
    world = _started(tmp_path)
    _alter(
        world,
        "UPDATE campaign_stage_attempts SET status='failed_permanent' WHERE campaign_id=? "
        "AND stage_key=? AND status='succeeded'",
    )
    assert _failed_closed(world) == "stage_failed:g1:late_bind_refused:upstream_not_succeeded"
    _runner_refusal(world, "upstream_not_succeeded")


# -- submit: an upstream output with no declared bound -----------------------------------------


def test_an_upstream_output_without_a_declared_bound_gets_no_candidate(tmp_path, host, monkeypatch):
    """The spec model requires ``max_bytes`` on every output; should the adapter ever be handed
    an upstream it cannot size, the heavy-remote rule records why and adds nothing."""
    monkeypatch.setattr(adapter, "upstream_outputs", lambda spec: lambda source: None)
    _configure()
    world = _setup(tmp_path)
    world.spec = _dag(world)
    world.env.submit(spec=world.spec)
    stage_json = _dependent(world)
    record = stage_json["heavy_remote_candidate"]
    assert record["added"] is False and record["reason"] == "profile_unfit"
    assert "no declared max_bytes" in record["detail"]
    assert "alternate_providers" not in stage_json
    assert "salad" not in json.dumps(stage_json.get("alternate_providers") or [])
