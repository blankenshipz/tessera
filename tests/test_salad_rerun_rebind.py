"""A re-run of an adapted Salad stage rebinds its reviewed workload contract; no network.

An adapted stage's contract declares a ``stage`` input by the late-bind key. A re-run (ADR 0069,
0209) pins that input as an ``artifact`` / ``artifact_folder``; the stage keeps its contract, which
must then be rebound to the key of the pins so the runner's composed snapshot digest is bound at
start. A contract that does not carry exactly the recorded key fails closed (a ``StepFailed`` with
a recorded reason, never a retry loop).
"""

from __future__ import annotations

import copy
import json

import pytest
from campaign_runner_harness import ApiChannel, ScriptedAdapter
from salad_campaign_fixture import (  # noqa: F401
    IMAGE,
    SaladWorld,
    salad_provider_reset,
    salad_spec_v2,
)
from test_execution import _worker
from test_salad_late_bound_inputs import (  # noqa: F401
    HOST,
    UP,
    _Host,
    _patched,
    _stage_row,
    _to_salad_stage,
    _trust,
)
from test_salad_stage_adapter import cpu_world, stage

from tessera_control import campaign_provider_worker as worker
from tessera_control.campaign_models import normalize, parse_spec, stage_on_candidate
from tessera_control.campaign_providers import register_provider, unregister_provider
from tessera_control.campaign_rerun import build_retry_spec
from tessera_control.store import Conflict
from tessera_control.campaign_salad import LATE_BOUND_EVENT, stage_workload
from tessera_control.campaign_salad_adapter import (
    expand_spec,
    is_late_bound,
    late_bind_key,
    pinned_bind_key,
)
from tessera_control.store import canonical_digest
from tessera_worker.grant_poller import DevelopmentGrantPoller, GrantPollerConfig
from tessera_worker.service import DevelopmentWorkerService


@pytest.fixture
def host():
    yield
    unregister_provider(HOST)


ACTIONS = ["campaign:submit", "campaign:read", "campaign:cancel", "artifact:read", "campaign:logs"]
FILES = {"result": "data.json", "extra": "extra.json"}
BODIES = {"data.json": '{"rows": 3}', "extra.json": '{"rows": 4}'}


def _poller(world, tmp_path, token):
    channel = ApiChannel(world.env.state)
    channel.headers = {"Authorization": f"Bearer {token}"}
    credential = tmp_path / "host.credential"
    credential.write_text(token)
    credential.chmod(0o600)

    def service(config):
        adapter = ScriptedAdapter(0, dict(BODIES))
        adapter.development_output_root = (
            config.development_output_root / "development-only-cpu-outputs" / "workspaces"
        )

        def finish(_):
            adapter.state = "exited"

        return DevelopmentWorkerService(config, channel=channel, adapter=adapter, sleep=finish)

    return DevelopmentGrantPoller(
        GrantPollerConfig(
            "http://127.0.0.1:8765",
            credential,
            tmp_path / "host-root",
            tmp_path / "host-out",
            (IMAGE,),
        ),
        channel=channel,
        service_factory=service,
        sleep=lambda _: None,
    )


def _dag(world, names):
    env = world.env
    upstream = stage(
        env,
        key=UP,
        provider=HOST,
        alternate_providers=None,
        outputs=[{"name": n, "path": FILES[n], "max_bytes": 1024} for n in FILES],
    )
    dependent = stage(
        env,
        provider=HOST,
        inputs=[{"kind": "stage", "name": "up", "stage": UP, "outputs": list(names)}],
    )
    return salad_spec_v2(env, [upstream, dependent])


def _world(tmp_path, names):
    world = cpu_world(tmp_path)
    env = world.env
    host_worker = _worker(env.state, env.owner, env.project["id"], "host-token")
    register_provider(_Host(worker._descriptor(HOST, host_worker["id"], None, 1)))
    _trust(world, host_worker)
    world.spec = _dag(world, names)
    world.start()
    poller = _poller(world, tmp_path, "host-token")
    for _ in range(80):
        world.tick()
        poller.run(max_cycles=1)
        done = world.row(
            "SELECT status FROM campaign_stages WHERE campaign_id=? AND stage_key=?",
            world.campaign_id,
            UP,
        )
        if done["status"] == "succeeded":
            return world
    raise AssertionError("upstream did not succeed\n" + world.dump())


def _fail_dependent_then_rerun(world):
    """The dependent stage fails at its first start; a person-approved re-run pins the upstream
    outputs (one output: an ``artifact``; several: one ``artifact_folder``)."""
    with pytest.MonkeyPatch.context() as patch:
        _patched(patch, {"digest": "e" * 64})
        _to_salad_stage(world)
        world.tick_until(lambda: _stage_row(world)["status"] == "failed")
        world.tick_until(lambda: world.status() == "failed")
    env, source = world.env, world.campaign_id
    actor = env.agent(ACTIONS, "steerer")
    env.execute("UPDATE campaigns SET submitted_by_principal_id=? WHERE id=?", actor["id"], source)
    body = {"stage_keys": ["g1"]}
    status, new = env.store.rerun_campaign(actor, source, "rerun-1", canonical_digest(body), body)
    assert status == 201 and new["status"] == "submitted"
    return new


def _contracts(spec):
    (dependent,) = [s for s in spec["stages"] if s["key"] == "g1"]
    configs = [dependent.get("provider_config")]
    configs += [i["provider_config"] for i in dependent.get("alternate_providers") or []]
    return [c for c in configs if isinstance(c, dict) and c.get("adapted")]


@pytest.mark.parametrize(
    ("names", "kind"), [(["result"], "artifact"), (["result", "extra"], "artifact_folder")]
)
def test_a_rerun_adapted_salad_stage_binds_and_proposes_its_decision(tmp_path, host, names, kind):
    world = _world(tmp_path, names)
    old = copy.deepcopy(world.env.store.get_campaign(world.env.owner, world.campaign_id)["spec"])
    (old_contract,) = _contracts(old)
    old_key = old_contract["workload"]["input"]["snapshot_digest"]
    new = _fail_dependent_then_rerun(world)

    (pin,) = [i for i in new["spec"]["stages"][0]["inputs"] if i["name"] == "up"]
    assert pin["kind"] == kind  # the original ``stage`` input is gone
    (contract,) = _contracts(new["spec"])
    declared = contract["workload"]["input"]
    normal = normalize(parse_spec(new["spec"])).stages[0]
    assert pinned_bind_key(normal) == declared["snapshot_digest"] != old_key
    assert (
        f"input:{declared['snapshot_digest']}:{declared['max_bytes']}"
        in (contract["workload"]["argv_template"])
    )
    assert is_late_bound(normal, expand_workload(contract))

    world.env.approve(new)
    world.campaign = world.env.store.get_campaign(world.env.owner, new["id"])
    world.tick_until(
        lambda: (
            world.row(
                "SELECT 1 FROM campaign_salad_stages WHERE campaign_id=? AND source_id IS NOT NULL",
                new["id"],
            )
            is not None
        )
    )
    world.assess_source()
    world.tick_until_phase("awaiting_lease")
    digest = _stage_row(world)["input_snapshot_digest"]
    assert digest and digest not in (old_key, declared["snapshot_digest"])
    (bound,) = [e for e in world.events(LATE_BOUND_EVENT) if e["snapshot_digest"] == digest]
    assert bound["inputs"] == []  # pins are checked by the runner's own custody resolution

    world.lease_and_package()
    world.tick_until_phase("awaiting_review")  # propose_decision ran: no ValidationError loop
    world.resolve_decisions()
    world.tick_until_phase("route_started")
    selection = world.route_state()["proposal"]["selection"]
    assert selection["manifests"]["data"] == digest
    work = selection["salad"]["execution"]["workload"]
    assert work["input"]["snapshot_digest"] == digest
    assert f"input:{digest}:{declared['max_bytes']}" in work["argv_template"]


def expand_workload(contract):
    from types import SimpleNamespace

    from tessera_control.campaign_models import SaladStageSpec

    return SimpleNamespace(input=SaladStageSpec.model_validate(contract).workload.input)


def test_a_contract_that_is_not_the_recorded_key_is_a_409_at_submit(tmp_path, host):
    world = _world(tmp_path, ["result"])
    new = _fail_dependent_then_rerun(world)
    # A hand-submitted copy of the re-run whose reviewed contract declares another digest than
    # the key of its pins: nothing may bind it.
    spec = copy.deepcopy(new["spec"])
    for contract in _contracts(spec):
        work = contract["workload"]
        old = f"input:{work['input']['snapshot_digest']}:{work['input']['max_bytes']}"
        work["input"]["snapshot_digest"] = "d" * 64
        work["argv_template"] = [
            f"input:{'d' * 64}:{work['input']['max_bytes']}" if t == old else t
            for t in work["argv_template"]
        ]
    # The submit-time recheck (a fresh input plan) refuses it before any hold.
    with pytest.raises(Conflict, match="adapted Salad contract is stale"):
        world.env.submit(spec=spec)


# -- pure -------------------------------------------------------------------------------------


def _source(world, names):
    spec = expand_spec(parse_spec(_dag(world, names)), world.profiles, tenant=None)
    campaign_spec = json.loads(spec.model_dump_json(exclude_none=True))
    rows = [
        {"stage_key": UP, "name": n, "sha256": f"{i:064x}", "custody_released": True}
        for i, n in enumerate(names, start=1)
    ]
    campaign = {
        "id": "c1",
        "status": "failed",
        "settled_cents": 0,
        "spec": campaign_spec,
        "stages": [
            {"stage_key": UP, "status": "succeeded"},
            {"stage_key": "g1", "status": "failed"},
        ],
    }
    return campaign, {"outputs": rows}


@pytest.mark.parametrize("names", [["result"], ["result", "extra"]])
def test_only_the_exact_original_key_is_rebound(tmp_path, host, names):
    world = cpu_world(tmp_path)
    register_provider(_Host(worker._descriptor(HOST, world.worker["id"], None, 1)))
    campaign, outputs = _source(world, names)
    original = normalize(parse_spec(campaign["spec"])).stage("g1")
    key = late_bind_key(original)
    assert key and all(
        c["workload"]["input"]["snapshot_digest"] == key for c in _contracts(campaign["spec"])
    )

    plan = build_retry_spec(campaign, outputs, ["g1"])
    contracts = _contracts(plan["spec"])
    assert contracts
    normal = normalize(parse_spec(plan["spec"])).stages[0]
    for contract in contracts:
        assert contract["workload"]["input"]["snapshot_digest"] == pinned_bind_key(normal)

    # A contract that declared some other digest is carried over untouched (never guessed).
    other = copy.deepcopy(campaign)
    for contract in _contracts(other["spec"]):
        contract["workload"]["input"]["snapshot_digest"] = "c" * 64
    kept = build_retry_spec(other, outputs, ["g1"])
    assert {c["workload"]["input"]["snapshot_digest"] for c in _contracts(kept["spec"])} == {
        "c" * 64
    }


def test_a_changed_pin_is_not_the_recorded_key(tmp_path, host):
    world = cpu_world(tmp_path)
    register_provider(_Host(worker._descriptor(HOST, world.worker["id"], None, 1)))
    campaign, outputs = _source(world, ["result"])
    plan = build_retry_spec(campaign, outputs, ["g1"])
    stage_dict = copy.deepcopy(plan["spec"])
    (dependent,) = [s for s in stage_dict["stages"] if s["key"] == "g1"]
    (pin,) = [i for i in dependent["inputs"] if i["name"] == "up"]
    pin["sha256"] = "f" * 64  # another artifact than the one the contract was rebound to
    normal = stage_on_candidate(normalize(parse_spec(stage_dict)).stages[0], "salad")
    (contract,) = _contracts(stage_dict)[:1]
    assert contract["workload"]["input"]["snapshot_digest"] != pinned_bind_key(normal)
    assert not is_late_bound(normal, expand_workload(contract))
    # Not late-bound, the reviewed contract is returned as it is, never bound to a snapshot.
    bound = stage_workload({"input_snapshot_digest": "a" * 64}, normal)
    assert bound.input.snapshot_digest == contract["workload"]["input"]["snapshot_digest"]


# -- a selection that cannot form: closed before the route, never after it starts ---------------


def _refuse_selection(monkeypatch):
    from tessera_control import campaign_salad

    def refused(*_args, **_kwargs):
        raise ValueError("the profile no longer admits the reviewed window")

    monkeypatch.setattr(campaign_salad, "derive_selection", refused)


@pytest.mark.usefixtures("salad_provider_reset")
def test_a_selection_that_cannot_form_before_the_route_fails_the_stage_closed(
    tmp_path, monkeypatch
):
    from tessera_control import campaign_salad

    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    # e.g. the operator lowered the profile's window after review: the route is proposed with a
    # selection that can no longer be derived (plan review derived it fine).
    raised = []
    proposing = []
    driver_type = type(world.driver)
    real = driver_type._propose_route
    derive = campaign_salad.derive_selection

    def spy(self, *args, **kwargs):
        proposing.append(1)
        try:
            return real(self, *args, **kwargs)
        except Exception as error:
            raised.append(error)
            raise
        finally:
            proposing.pop()

    def refused(*args, **kwargs):
        if proposing:
            raise ValueError("the profile no longer admits the reviewed window")
        return derive(*args, **kwargs)

    monkeypatch.setattr(driver_type, "_propose_route", spy)
    monkeypatch.setattr(campaign_salad, "derive_selection", refused)
    world.resolve_decisions()
    world.tick_until(lambda: world.status() == "failed", limit=20)
    assert [type(e).__name__ for e in raised] == ["StepFailed"]  # once, not a retry loop
    assert raised[0].error_class == "step_refused"
    reason = world.row("SELECT stop_reason FROM campaigns WHERE id=?", world.campaign_id)
    assert reason["stop_reason"] == "stage_failed:g1:step_refused"
    assert world.row("SELECT 1 FROM campaign_salad_attempts WHERE route_id IS NOT NULL") is None


@pytest.mark.usefixtures("salad_provider_reset")
def test_a_replay_after_the_route_started_never_fails_the_running_attempt(tmp_path, monkeypatch):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    salad = world.salad()
    attempt = world.attempt()
    assert attempt["status"] == "running" and attempt["operation_id"]
    # The replay ``_approve_and_start`` makes once ``start_route`` is recorded, now with a
    # selection that could no longer be derived.
    world.env.execute(
        "UPDATE campaign_salad_attempts SET phase='route_proposed' WHERE attempt_id=?",
        salad["attempt_id"],
    )
    calls = []
    driver_type = type(world.driver)
    real = driver_type._approve_and_start

    def spy(self, *args, **kwargs):
        calls.append(1)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(driver_type, "_approve_and_start", spy)
    _refuse_selection(monkeypatch)
    world.tick_until_phase("route_started", limit=5)
    assert calls == [1]
    assert world.attempt()["status"] == "running"
    assert world.status() == "running"
    assert world.salad()["route_id"] == salad["route_id"]


# -- mixed re-pin: two stage inputs, one upstream re-run ----------------------------------------

UP2 = "prep2"


class _Hosts(_Host):
    """A CPU host with a slot for both upstream stages."""

    def free_slots(self, rt, ctx, stage):
        return 1 if stage.key in (UP, UP2) else 0


def _mixed_dag(world):
    env = world.env
    outputs = [{"name": n, "path": FILES[n], "max_bytes": 1024} for n in FILES]
    upstream = stage(env, key=UP, provider=HOST, alternate_providers=None, outputs=outputs)
    second = stage(env, key=UP2, provider=HOST, alternate_providers=None, outputs=outputs)
    dependent = stage(
        env,
        provider=HOST,
        inputs=[
            {"kind": "stage", "name": "up", "stage": UP, "outputs": ["result"]},
            {"kind": "stage", "name": "up2", "stage": UP2, "outputs": ["extra"]},
        ],
    )
    return salad_spec_v2(env, [upstream, second, dependent])


def _run_hosts(world, poller, keys):
    for _ in range(120):
        world.tick()
        poller.run(max_cycles=1)
        if all(_stage_row(world, key)["status"] == "succeeded" for key in keys):
            return
    raise AssertionError("upstream did not succeed\n" + world.dump())


def _mixed_world(tmp_path):
    world = cpu_world(tmp_path)
    env = world.env
    host_worker = _worker(env.state, env.owner, env.project["id"], "host-token")
    register_provider(_Hosts(worker._descriptor(HOST, host_worker["id"], None, 1)))
    _trust(world, host_worker)
    world.spec = _mixed_dag(world)
    world.start()
    poller = _poller(world, tmp_path, "host-token")
    _run_hosts(world, poller, [UP, UP2])
    return world, poller


def test_a_mixed_rerun_rebinds_to_the_full_key_and_binds_the_composed_snapshot(tmp_path, host):
    world, poller = _mixed_world(tmp_path)
    old = copy.deepcopy(world.env.store.get_campaign(world.env.owner, world.campaign_id)["spec"])
    (old_contract,) = _contracts(old)
    old_key = old_contract["workload"]["input"]["snapshot_digest"]
    assert old_key == late_bind_key(normalize(parse_spec(old)).stage("g1"))

    with pytest.MonkeyPatch.context() as patch:
        _patched(patch, {"digest": "e" * 64})
        _to_salad_stage(world)
        world.tick_until(lambda: _stage_row(world)["status"] == "failed")
        world.tick_until(lambda: world.status() == "failed")
    env, source = world.env, world.campaign_id
    actor = env.agent(ACTIONS, "steerer")
    env.execute("UPDATE campaigns SET submitted_by_principal_id=? WHERE id=?", actor["id"], source)
    body = {"stage_keys": [UP2]}  # only the second upstream runs again
    status, new = env.store.rerun_campaign(actor, source, "rerun-1", canonical_digest(body), body)
    assert status == 201, new

    (dependent,) = [s for s in new["spec"]["stages"] if s["key"] == "g1"]
    kinds = {i["name"]: i["kind"] for i in dependent["inputs"]}
    assert kinds == {"up": "artifact", "up2": "stage"}
    (contract,) = _contracts(new["spec"])
    declared = contract["workload"]["input"]
    normal = normalize(parse_spec(new["spec"])).stage("g1")
    # The full key: the pin's campaign/stage/output/sha256 and the kept stage input, not the
    # late-bind key that records the pin only as ``[name, "artifact", "", []]``.
    assert declared["snapshot_digest"] == pinned_bind_key(normal)
    assert declared["snapshot_digest"] not in (old_key, late_bind_key(normal))
    assert (
        f"input:{declared['snapshot_digest']}:{declared['max_bytes']}"
        in (contract["workload"]["argv_template"])
    )
    assert is_late_bound(normal, expand_workload(contract))

    world.env.approve(new)
    world.campaign = world.env.store.get_campaign(world.env.owner, new["id"])
    _run_hosts(world, poller, [UP2])
    world.tick_until(
        lambda: (
            world.row(
                "SELECT 1 FROM campaign_salad_stages WHERE campaign_id=? AND source_id IS NOT NULL",
                new["id"],
            )
            is not None
        )
    )
    world.assess_source()
    world.tick_until_phase("awaiting_lease")
    digest = _stage_row(world)["input_snapshot_digest"]
    assert digest and digest not in (old_key, declared["snapshot_digest"])
    (bound,) = [e for e in world.events(LATE_BOUND_EVENT) if e["snapshot_digest"] == digest]
    assert [item["name"] for item in bound["inputs"]] == ["up2"]  # the kept stage input

    world.lease_and_package()
    world.tick_until_phase("awaiting_review")  # propose_decision: no ValidationError loop
    world.resolve_decisions()
    world.tick_until_phase("route_started")
    selection = world.route_state()["proposal"]["selection"]
    assert selection["manifests"]["data"] == digest
    work = selection["salad"]["execution"]["workload"]
    assert work["input"]["snapshot_digest"] == digest
    assert f"input:{digest}:{declared['max_bytes']}" in work["argv_template"]


def _mixed_source(world):
    spec = expand_spec(parse_spec(_mixed_dag(world)), world.profiles, tenant=None)
    campaign = {
        "id": "c1",
        "status": "failed",
        "settled_cents": 0,
        "spec": json.loads(spec.model_dump_json(exclude_none=True)),
        "stages": [
            {"stage_key": UP, "status": "succeeded"},
            {"stage_key": UP2, "status": "succeeded"},
            {"stage_key": "g1", "status": "failed"},
        ],
    }
    rows = [
        {"stage_key": UP, "name": "result", "sha256": "1" * 64, "custody_released": True},
        {"stage_key": UP2, "name": "extra", "sha256": "2" * 64, "custody_released": True},
    ]
    return campaign, {"outputs": rows}


def test_a_mixed_rerun_rebinds_only_the_recorded_key_and_the_full_key_is_stable(tmp_path, host):
    world = cpu_world(tmp_path)
    register_provider(_Hosts(worker._descriptor(HOST, world.worker["id"], None, 1)))
    campaign, outputs = _mixed_source(world)
    plan = build_retry_spec(campaign, outputs, [UP2])
    normal = normalize(parse_spec(plan["spec"])).stage("g1")
    key = pinned_bind_key(normal)
    assert key and key != late_bind_key(normal)
    assert {c["workload"]["input"]["snapshot_digest"] for c in _contracts(plan["spec"])} == {key}

    # Another pinned sha256 is another key (the late-bind key would not tell them apart).
    moved = copy.deepcopy(plan["spec"])
    (dependent,) = [s for s in moved["stages"] if s["key"] == "g1"]
    (pin,) = [i for i in dependent["inputs"] if i["name"] == "up"]
    pin["sha256"] = "f" * 64
    other = normalize(parse_spec(moved)).stage("g1")
    assert late_bind_key(other) == late_bind_key(normal)
    assert pinned_bind_key(other) != key
    for contract in _contracts(plan["spec"]):
        assert not is_late_bound(stage_on_candidate(other, "salad"), expand_workload(contract))

    # A contract that is not the source's recorded key is carried untouched and fails closed.
    odd = copy.deepcopy(campaign)
    for contract in _contracts(odd["spec"]):
        work = contract["workload"]
        token = f"input:{work['input']['snapshot_digest']}:{work['input']['max_bytes']}"
        work["input"]["snapshot_digest"] = "c" * 64
        work["argv_template"] = [
            f"input:{'c' * 64}:{work['input']['max_bytes']}" if t == token else t
            for t in work["argv_template"]
        ]
    kept = build_retry_spec(odd, outputs, [UP2])
    assert {c["workload"]["input"]["snapshot_digest"] for c in _contracts(kept["spec"])} == {
        "c" * 64
    }
    kept_stage = normalize(parse_spec(kept["spec"])).stage("g1")
    for contract in _contracts(kept["spec"]):
        assert not is_late_bound(kept_stage, expand_workload(contract))

    # A re-run of that re-run that pins the second upstream too rebinds the full key it recorded.
    again = {
        "id": "c2",
        "status": "failed",
        "settled_cents": 0,
        "spec": plan["spec"],
        "stages": [
            {"stage_key": UP2, "status": "succeeded"},
            {"stage_key": "g1", "status": "failed"},
        ],
    }
    rows = [{"stage_key": UP2, "name": "extra", "sha256": "3" * 64, "custody_released": True}]
    final = build_retry_spec(again, {"outputs": rows}, ["g1"])
    pinned = normalize(parse_spec(final["spec"])).stage("g1")
    assert {i.kind for i in pinned.inputs if i.name in ("up", "up2")} == {"artifact"}
    assert late_bind_key(pinned) is None
    assert {c["workload"]["input"]["snapshot_digest"] for c in _contracts(final["spec"])} == {
        pinned_bind_key(pinned)
    }
