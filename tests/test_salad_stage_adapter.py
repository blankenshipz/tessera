"""Salad stage adapter (ADR 0076): a local-docker CPU stage lists Salad by placement alone.

Fakes only: the Salad executor, guardian and human are the ``SaladWorld`` test doubles and no
Salad call is made. The CPU host is a worker-grant provider whose capacity a test fixes.
"""

from __future__ import annotations

import base64
import copy
import json

import pytest
from salad_campaign_fixture import (  # noqa: F401
    IMAGE,
    SaladWorld,
    program_template,
    salad_provider_reset,
    salad_spec_v2,
)
from test_execution import _worker

from tessera_control import campaign_provider_worker as worker
from tessera_control.campaign_models import SALAD_RUNTIME, SaladStageSpec, parse_spec, spec_digest
from tessera_control.campaign_providers import register_provider, unregister_provider
from tessera_control.campaign_salad_adapter import expand_spec
from tessera_control.store import Conflict, canonical_digest

NEAR = "near-cpu"
RATE = dict(vcpu_hour_usd="0.04", memory_gb_hour_usd="0.01")
ARGV = ["python", "/inputs/main.py", "--out=/outputs/output.json", "/inputs"]


class _Capacity(worker.WorkerGrantProvider):
    slots = 0

    def free_slots(self, rt, ctx, stage):
        return type(self).slots


@pytest.fixture
def near():
    def make(world: SaladWorld, slots: int) -> None:
        env = world.env
        principal = _worker(env.state, env.owner, env.project["id"], "near-token")
        _Capacity.slots = slots
        register_provider(_Capacity(worker._descriptor(NEAR, principal["id"], None, 1)))
        make.principal = principal
        return principal

    yield make
    unregister_provider(NEAR)


def adapter_section(**over):
    return {"program": program_template(), "max_input_bytes": 1 << 20, **over}


def cpu_world(tmp_path, near=None, slots=0, adapter=True, **profile_over):
    over = dict(
        name="cpu-main",
        gpu_class=None,
        cpu_rate=RATE,
        cpu_millicores=2000,
        memory_mb=2048,
        capability_environment_id="salad-cpu-campaign",
        max_run_cents=50,
        max_task_cents=100,
    )
    if adapter:
        over["stage_adapter"] = adapter_section()
    over.update(profile_over)
    world = SaladWorld(tmp_path, **over)
    if near is not None:
        near(world, slots)
    return world


def request(profile="cpu-main", **over):
    return {
        "provider": "salad",
        "runtime": SALAD_RUNTIME,
        "provider_config": {"adapt": {"profile": profile}},
        **over,
    }


def stage(env, **over):
    value = {
        "key": "g1",
        "provider": NEAR,
        "runtime": "docker-cpu-v1",
        "provider_config": {},
        "image": IMAGE,
        "argv": list(ARGV),
        "max_duration_seconds": 600,
        "max_exposure_cents": 20,
        "grant_ttl_seconds": 1200,
        "retry": {"max_attempts": 1},
        "inputs": [
            {
                "kind": "snapshot",
                "name": "code",
                "snapshot_id": env.snapshot_id,
                "digest": env.snapshot_digest,
                "data_class": "public",
            }
        ],
        "outputs": [{"name": "output", "path": "output.json", "max_bytes": 4096}],
        "alternate_providers": [request()],
    }
    value.update(over)
    return value


def spec_of(world, **over):
    return salad_spec_v2(world.env, [stage(world.env, **over)])


def expanded(world, **over):
    spec = parse_spec(spec_of(world, **over))
    return expand_spec(spec, world.profiles)


def candidate(spec):
    (item,) = spec.stages[0].alternate_providers
    return item


def refusal(world, match, **over):
    with pytest.raises(Conflict, match=match):
        expanded(world, **over)


# -- expansion ---------------------------------------------------------------------------------


def test_the_request_expands_to_the_explicit_candidate_a_person_approves(tmp_path, near):
    world = cpu_world(tmp_path, near)
    spec = expanded(world)
    item = candidate(spec)
    config = SaladStageSpec.model_validate(item.provider_config)
    assert config.profile == "cpu-main" and config.gpu is False and config.adapted is True
    assert config.worker_principal_id == world.worker["id"]
    work = config.workload
    assert work.runtime == "gpu_runtime_v1" and work.workload_seconds == 600
    assert [(o.name, o.max_bytes) for o in work.outputs] == [("output.json", 4096)]
    assert work.custody_host == "custody.example-tailnet.ts.net" and work.env == {}
    assert work.input.snapshot_digest == world.env.snapshot_digest
    assert work.input.max_bytes == 1 << 20
    # The manifest is the stage's own argv as the one image entry; nothing is fetched.
    token = [t for t in work.argv_template if t.startswith("b64:")]
    raw = base64.urlsafe_b64decode("".join(t[4:] for t in token) + "==")
    assert json.loads(raw)["fetches"] == [
        {"kind": "entry", "source": "image", "path": "stage_argv.py", "argv": ARGV}
    ]
    assert work.egress.fetches == [] and work.egress.redirect_hosts == []
    assert work.capability_network() == []
    # 600 s at $0.10/h is 1.67 cents, held as 2; the stage's own cap is untouched.
    assert item.max_exposure_cents == 2 and spec.stages[0].max_exposure_cents == 20
    assert spec.stages[0].argv == ARGV and spec.stages[0].image == IMAGE


def test_expansion_is_deterministic_and_a_spec_without_a_request_is_unchanged(tmp_path, near):
    world = cpu_world(tmp_path, near)
    assert spec_digest(expanded(world)) == spec_digest(expanded(world))
    plain = parse_spec(spec_of(world, alternate_providers=None))
    assert expand_spec(plain, world.profiles) is plain
    assert "adapted" not in json.dumps(plain.model_dump(mode="json"))
    # An explicit candidate without the flag serializes exactly as before.
    config = SaladStageSpec.model_validate(candidate(expanded(world)).provider_config)
    assert "adapted" in config.model_dump(mode="json")
    assert "adapted" not in config.model_copy(update={"adapted": False}).model_dump(mode="json")


def test_an_explicit_estimate_is_kept(tmp_path, near):
    world = cpu_world(tmp_path, near)
    spec = expanded(world, alternate_providers=[request(max_exposure_cents=5)])
    assert candidate(spec).max_exposure_cents == 5


# -- refusals, each naming the limit ----------------------------------------------------------


@pytest.mark.parametrize(
    ("over", "match"),
    [
        ({"max_duration_seconds": 3601}, "exceeds the Salad limit of 3600"),
        ({"max_duration_seconds": 59}, "below the Salad runtime minimum of 60"),
        ({"image": "python@sha256:" + "9" * 64}, "allowed_image_digests"),
        (
            {"retry": {"max_attempts": 2, "transient_exit_codes": [137]}},
            "transient_exit_codes cannot be carried",
        ),
        (
            {"outputs": [{"name": "output", "path": "out/dir/output.json", "max_bytes": 4096}]},
            "not a flat Salad output name",
        ),
        (
            {"outputs": [{"name": "output", "path": "output.json", "max_bytes": 17 << 20}]},
            "in total|per-file limit|exceed",
        ),
        ({"max_exposure_cents": 1}, "below the Salad estimate"),
        ({"alternate_providers": [request("nope")]}, "unknown profile 'nope'"),
        ({"argv": ["python", "x" * 3900, *["y" * 3900] * 5]}, "too large"),
    ],
)
def test_a_stage_over_a_salad_limit_is_refused_with_the_limit_named(tmp_path, near, over, match):
    world = cpu_world(tmp_path, near)
    refusal(world, match, **over)


def test_more_than_one_input_needs_the_platform_to_size_it(tmp_path, near):
    world = cpu_world(tmp_path, near)
    first = stage(world.env)["inputs"][0]
    refusal(world, "cannot be sized here", inputs=[first, {**first, "name": "second"}])


def test_an_input_from_another_stage_is_refused(tmp_path, near):
    world = cpu_world(tmp_path, near)
    inputs = [{"kind": "stage", "name": "up", "stage": "upstream", "outputs": ["output"]}]
    spec = salad_spec_v2(
        world.env,
        [
            stage(world.env, key="upstream", alternate_providers=None),
            stage(world.env, inputs=inputs),
        ],
    )
    with pytest.raises(Conflict, match="another stage's output"):
        expand_spec(parse_spec(spec), world.profiles)


def test_a_profile_that_is_not_opened_to_adapted_stages_is_refused(tmp_path, near):
    refusal(cpu_world(tmp_path, near, adapter=False), "stage_adapter section")


def test_a_gpu_profile_cannot_adapt(tmp_path, near):
    world = SaladWorld(tmp_path)
    near(world, 0)
    with pytest.raises(Conflict, match="not a CPU-only profile"):
        expand_spec(
            parse_spec(spec_of(world, alternate_providers=[request("gpu-main")])), world.profiles
        )


def test_no_profiles_at_all_is_refused(tmp_path, near):
    world = cpu_world(tmp_path, near)
    with pytest.raises(Conflict, match="no Salad profile is configured"):
        expand_spec(parse_spec(spec_of(world)), None)


def test_the_adapter_is_only_for_a_salad_candidate(tmp_path, near):
    world = cpu_world(tmp_path, near)
    with pytest.raises(ValueError, match="only a salad candidate"):
        parse_spec(
            spec_of(
                world,
                alternate_providers=[request(provider="local-docker", runtime="docker-cpu-v1")],
            )
        )


def test_a_gpu_profile_cannot_carry_a_stage_adapter_section(tmp_path):
    with pytest.raises(ValueError, match="CPU-only"):
        SaladWorld(tmp_path, stage_adapter=adapter_section())


# -- submit, approval and placement -----------------------------------------------------------


def test_submit_digests_and_stores_the_expanded_spec_and_validate_shows_it(tmp_path, near):
    world = cpu_world(tmp_path, near)
    env = world.env
    body = {"spec": spec_of(world)}
    checked = env.store.validate_campaign_spec(env.owner, env.project["id"], body)
    assert checked["valid"] is True
    shown = checked["expanded_spec"]["stages"][0]["alternate_providers"][0]
    assert shown["provider_config"]["adapted"] is True and shown["max_exposure_cents"] == 2
    submitted = env.submit(spec=spec_of(world))
    assert submitted["spec_digest"] == checked["spec_digest"]
    stored = json.loads(env.query("SELECT spec_json FROM campaigns")[0]["spec_json"])
    assert stored["stages"][0]["alternate_providers"][0]["provider_config"]["adapted"] is True
    assert "adapt" not in stored["stages"][0]["alternate_providers"][0]["provider_config"]
    assert submitted["spec_digest"] == canonical_digest(stored)


def test_a_busy_cpu_host_runs_the_unchanged_stage_on_salad(tmp_path, near):
    world = cpu_world(tmp_path, near, slots=0)
    world.spec = spec_of(world)
    world.to_route_started()
    (placed,) = world.events("campaign.stage_placed")
    assert placed["candidates"] == [NEAR, "salad"] and placed["provider_id"] == "salad"
    assert placed["skipped"][0]["reason"] == "no_free_slot" and placed["hold_cents"] == 2
    assert world.phase() == "route_started"
    # The reviewed plan carries the contract's argv, and the route is the CPU one.
    plans = [
        json.loads(row["execution_spec_json"])
        for row in world.env.query(
            "SELECT execution_spec_json FROM task_revisions WHERE execution_spec_json IS NOT NULL"
        )
    ]
    argv = next(p["argv"] for p in plans if p["argv"][0] == "/tessera/bin/run")
    assert argv[0] == "/tessera/bin/run" and argv[1] == "v1"


def test_an_idle_cpu_host_keeps_the_stage_off_salad(tmp_path, near):
    world = cpu_world(tmp_path, near, slots=1)
    world.spec = spec_of(world)
    world.start()
    world.tick_until(lambda: world.attempt() is not None)
    assert world.attempt()["provider_id"] == NEAR


def test_a_request_that_reaches_the_runner_unexpanded_is_never_valid_salad_config():
    with pytest.raises(ValueError):
        SaladStageSpec.model_validate({"adapt": {"profile": "cpu-main"}})


def test_the_expansion_does_not_mutate_its_input(tmp_path, near):
    world = cpu_world(tmp_path, near)
    spec = parse_spec(spec_of(world))
    before = copy.deepcopy(spec.model_dump(mode="json"))
    expand_spec(spec, world.profiles)
    assert spec.model_dump(mode="json") == before


def test_an_adapter_request_in_the_primary_slot_is_a_clean_refusal(tmp_path, near):
    world = cpu_world(tmp_path, near)
    env = world.env
    own = {"provider": "salad", "runtime": SALAD_RUNTIME, "alternate_providers": None}
    own["provider_config"] = {"adapt": {"profile": "cpu-main"}}
    body = {"spec": spec_of(world, **own)}
    with pytest.raises(Conflict, match="only for an alternate_providers candidate"):
        env.store.validate_campaign_spec(env.owner, env.project["id"], body)
    with pytest.raises(Conflict, match="only for an alternate_providers candidate"):
        env.store.submit_campaign(env.owner, env.project["id"], "k", canonical_digest(body), body)


def _private(world):
    from campaign_fixture import record_snapshot_private

    record_snapshot_private(world.env.state, world.env.snapshot_id, "derived")
    (source,) = stage(world.env)["inputs"]
    return [{**source, "data_class": "private_derived"}]


def test_a_private_input_is_refused_unless_salad_is_trusted_for_it(tmp_path, near):
    from provider_trust_fixture import DERIVED, install_trust

    world = cpu_world(tmp_path, near, slots=0)
    near_worker = near.principal
    install_trust({NEAR: (DERIVED, [near_worker["id"]])})
    body = {"spec": spec_of(world, inputs=_private(world))}
    env = world.env
    with pytest.raises(Conflict, match="not trusted to receive"):
        env.store.submit_campaign(env.owner, env.project["id"], "k", canonical_digest(body), body)


def test_a_private_input_runs_on_salad_when_trust_admits_it_and_the_placement_is_recorded(
    tmp_path, near
):
    from provider_trust_fixture import DERIVED, install_trust

    world = cpu_world(tmp_path, near, slots=0)
    install_trust(
        {
            NEAR: (DERIVED, [near.principal["id"]]),
            "salad": (DERIVED, [world.worker["id"]]),
        }
    )
    world.spec = spec_of(world, inputs=_private(world))
    world.to_route_started()
    (placed,) = world.events("campaign.stage_placed")
    assert placed["provider_id"] == "salad" and placed["skipped"][0]["reason"] == "no_free_slot"
    stored = json.loads(world.row("SELECT spec_json FROM campaigns")["spec_json"])
    config = stored["stages"][0]["alternate_providers"][0]["provider_config"]
    assert config["adapted"] is True
    assert config["workload"]["egress"]["fetches"] == []
