# ruff: noqa: F811
"""A heavy stage that reads a prep stage's output gets the operator's Salad candidate (ADR 0141
amended by ADR 0140's late-bound inputs), and runs there with the bound digest pinned.

Fakes only: the upstream stage runs on a worker-grant CPU host through the development worker,
the Salad executor, guardian and human are ``SaladWorld`` test doubles, and no Salad call is made.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from salad_campaign_fixture import salad_provider_reset, salad_spec_v2  # noqa: F401
from test_execution import _worker
from test_salad_late_bound_inputs import (
    HOST,
    OUTPUT,
    UP,
    _executor_upload,
    _Host,
    _poller,
    _stage_row,
    _to_salad_stage,
    _trust,
    host,  # noqa: F401
)
from test_salad_stage_adapter import adapter_section, cpu_world, stage

from tessera_control import campaign_heavy_guard as guard
from tessera_control import campaign_heavy_remote as heavy_remote
from tessera_control import campaign_placement_preference as preference
from tessera_control import campaign_provider_worker as worker
from tessera_control import late_bound_inputs, program_budget
from tessera_control.campaign_models import HEAVY_REMOTE_ADDED, SaladStageSpec, parse_spec
from tessera_control.campaign_providers import register_provider
from tessera_control.campaign_receipts import build_receipt
from tessera_control.campaign_salad import LATE_BOUND_EVENT
from tessera_control.campaign_salad_adapter import late_bind_key, upstream_outputs
from tessera_control.store import canonical_digest

GIB = 1024**3
SETTING = {"provider": "salad", "max_exposure_cents": 10}


@pytest.fixture(autouse=True)
def _reset():
    preference.configure(None, ())
    program_budget.reset()
    yield
    preference.configure(None, ())
    program_budget.reset()


def _configure(**over):
    preference.configure({"heavy_remote_candidate": {**SETTING, **over}}, [HOST, "salad"])


def _dag(world, *, upstream_over=None, **dependent_over):
    env = world.env
    upstream = stage(
        env,
        key=UP,
        provider=HOST,
        alternate_providers=None,
        outputs=[{"name": "result", "path": "data.json", "max_bytes": 1024}],
        **(upstream_over or {}),
    )
    dependent = stage(
        env,
        provider=HOST,
        alternate_providers=None,  # local only: the rule must add Salad
        memory_bytes=2 * GIB,  # heavy by the declared-memory signal
        inputs=[{"kind": "stage", "name": "up", "stage": UP, "outputs": ["result"]}],
        **dependent_over,
    )
    return salad_spec_v2(env, [upstream, dependent])


def _setup(tmp_path, **profile_over):
    world = cpu_world(tmp_path, **profile_over)
    env = world.env
    host_worker = _worker(env.state, env.owner, env.project["id"], "host-token")
    register_provider(_Host(worker._descriptor(HOST, host_worker["id"], None, 1)))
    _trust(world, host_worker)
    return world


def _stored(world) -> dict:
    row = world.env.query("SELECT spec_json FROM campaigns")[0]
    return json.loads(row["spec_json"])


def _dependent(world) -> dict:
    return next(s for s in _stored(world)["stages"] if s["key"] == "g1")


def _run_upstream(world, tmp_path):
    poller = _poller(world, tmp_path, "host-token")
    for _ in range(80):
        world.tick()
        poller.run(max_cycles=1)
        if _stage_row(world, UP)["status"] == "succeeded":
            return
    raise AssertionError("upstream did not succeed\n" + world.dump())


# -- submit -----------------------------------------------------------------------------------


def test_a_heavy_chained_stage_gets_salad_first_sized_by_the_declared_bound(tmp_path, host):
    _configure()
    world = _setup(tmp_path)
    world.spec = _dag(world)
    world.env.submit(spec=world.spec)
    stage_json = _dependent(world)
    first = stage_json["alternate_providers"][0]
    assert first["provider"] == "salad" and first["provider_config"]["adapted"] is True
    record = stage_json["heavy_remote_candidate"]
    assert record == {
        "added": True,
        "reason": HEAVY_REMOTE_ADDED,
        "provider": "salad",
        "signals": ["declared_memory"],
        "profile": "cpu-main",
        "max_exposure_cents": first["max_exposure_cents"],
    }
    # Within every cap: operator, stage, campaign budget.
    assert 0 < first["max_exposure_cents"] <= min(10, stage_json["max_exposure_cents"], 50)
    # The contract declares the input by its late-bind key and the upstream's declared bound.
    work = SaladStageSpec.model_validate(first["provider_config"]).workload
    spec = parse_spec(world.spec)
    key = late_bind_key(spec.stages[1], upstream_outputs(spec))
    assert work.input.snapshot_digest == key and work.input.max_bytes == 1024
    # The light upstream stage is untouched; the digest covers the added candidate.
    upstream = next(s for s in _stored(world)["stages"] if s["key"] == UP)
    assert "heavy_remote_candidate" not in upstream and "alternate_providers" not in upstream
    (campaign,) = world.env.query("SELECT spec_digest FROM campaigns")
    assert campaign["spec_digest"] == canonical_digest(_stored(world))


def test_a_declared_bound_over_the_profile_input_cap_is_not_added(tmp_path, host):
    _configure()
    world = _setup(tmp_path, stage_adapter=adapter_section(max_input_bytes=100))
    world.spec = _dag(world)
    world.env.submit(spec=world.spec)
    stage_json = _dependent(world)
    record = stage_json["heavy_remote_candidate"]
    assert record["added"] is False and record["reason"] == "profile_unfit"
    assert "max_input_bytes 100" in record["detail"]
    assert "alternate_providers" not in stage_json  # nothing invented, no cap raised


# -- end to end -------------------------------------------------------------------------------


def test_after_the_upstream_succeeds_salad_runs_with_the_bound_digest(tmp_path, host):
    _configure()
    world = _setup(tmp_path)
    world.spec = _dag(world)
    world.start()
    assert _dependent(world)["heavy_remote_candidate"]["added"] is True
    _run_upstream(world, tmp_path)
    _to_salad_stage(world)
    world.tick_until_phase("awaiting_lease")
    (placed,) = [p for p in world.events("campaign.stage_placed") if p["stage_key"] == "g1"]
    assert placed["provider_id"] == "salad" and placed["candidates"][0] == "salad"
    assert not [e for e in world.events(guard.EVENT) if e.get("stage_key") == "g1"]

    # The runner bound the upstream output; the driver pinned the composed snapshot's digest.
    digest = _stage_row(world)["input_snapshot_digest"]
    (bound,) = world.events(LATE_BOUND_EVENT)
    assert bound["snapshot_digest"] == digest and bound["max_bytes"] == 1024
    (item,) = bound["inputs"]
    upstream_io = world.row(
        "SELECT sha256, size_bytes FROM campaign_stage_io WHERE campaign_id=? AND stage_key=? "
        "AND direction='output'",
        world.campaign_id,
        UP,
    )
    assert (item["digest"], item["bytes"]) == (upstream_io["sha256"], len(OUTPUT))

    world.lease_and_package()
    world.tick_until_phase("awaiting_review")
    world.resolve_decisions()
    world.tick_until_phase("route_started")
    selection = world.route_state()["proposal"]["selection"]
    assert selection["manifests"]["data"] == digest
    work = selection["salad"]["execution"]["workload"]
    assert work["input"]["snapshot_digest"] == digest and work["input"]["max_bytes"] == 1024
    assert f"input:{digest}:1024" in work["argv_template"]

    _executor_upload(world, tmp_path)
    world.run_to_outcome()
    world.tick_until_phase("awaiting_charge")
    with world.env.store.runner_connection() as connection:
        campaign = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (world.campaign_id,)
        ).fetchone()
        receipt, _ = build_receipt(connection, campaign, "succeeded")
    entry = next(s for s in receipt["stages"] if s["stage_key"] == "g1")
    assert entry["heavy_remote_candidate"]["reason"] == HEAVY_REMOTE_ADDED
    assert entry["attempts"][0]["late_bound_inputs"]["snapshot_digest"] == digest


def test_an_upstream_that_has_not_succeeded_never_reaches_salad(tmp_path, host, monkeypatch):
    _configure()
    world = _setup(tmp_path)
    world.spec = _dag(world)
    world.start()
    _run_upstream(world, tmp_path)

    def refuse(*args, **kwargs):
        raise late_bound_inputs.LateBindRefused("upstream_not_succeeded", "test double")

    monkeypatch.setattr(late_bound_inputs, "resolve_stage_inputs", refuse)
    _to_salad_stage(world)
    world.tick_until(lambda: _stage_row(world)["status"] == "failed")
    (refused,) = world.events(late_bound_inputs.REFUSED_EVENT)
    assert refused["code"] == "upstream_not_succeeded"
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", world.campaign_id)
    assert stop["stop_reason"] == "stage_failed:g1:late_bind_refused:upstream_not_succeeded"
    assert world.events(LATE_BOUND_EVENT) == []
    assert world.row("SELECT 1 FROM campaign_salad_attempts") is None  # nothing reached Salad


# -- inputs that are not served ---------------------------------------------------------------


def test_an_input_neither_pinned_nor_a_stage_output_of_this_campaign_is_not_served():
    spec = SimpleNamespace(stages=[SimpleNamespace(key=UP), SimpleNamespace(key="g1")])
    pinned = [SimpleNamespace(kind=k, name=k) for k in ("snapshot", "artifact", "public_artifact")]
    chained = SimpleNamespace(kind="stage", name="up", stage=UP)
    assert heavy_remote._unpinned_input(spec, "g1", [*pinned, chained]) is None
    foreign = SimpleNamespace(kind="stage", name="up", stage="elsewhere")
    assert "no other stage" in heavy_remote._unpinned_input(spec, "g1", [foreign])
    itself = SimpleNamespace(kind="stage", name="up", stage="g1")
    assert "no other stage" in heavy_remote._unpinned_input(spec, "g1", [itself])
    unresolved = SimpleNamespace(kind="receipt_output", name="prior")
    assert "not pinned" in heavy_remote._unpinned_input(spec, "g1", [unresolved])
