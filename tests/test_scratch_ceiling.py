"""Operator scratch ceiling, plan requests and admission (ADR 0059). Fakes only."""

from __future__ import annotations

import json

import pytest
from campaign_runner_harness import ScriptedAdapter, World, dump, stage_spec

from tessera_control.campaign_models import CampaignSpec, StageSpec
from tessera_control.campaign_provider_worker import ProvidersFile, register_from_config
from tessera_control.campaign_providers import get_provider
from tessera_control.store import Conflict
from tessera_worker import scratch_limits as limits
from tessera_worker.cpu import CpuError, CpuOperation, DockerCpuAdapter
from tessera_worker.grant_poller import GrantPollerConfig, load_poller_config
from tessera_worker.runtime import CpuRuntime
from tessera_worker.service import DevelopmentWorkerError
from tessera_worker.supervisor import SupervisorError

MIB = limits.MIB
GIB = limits.GIB
IMAGE = "python@sha256:" + "a" * 64
METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]
OK = {"data.json": json.dumps({"score": 3})}


def _adapter(**kwargs) -> DockerCpuAdapter:
    return DockerCpuAdapter(frozenset({IMAGE}), **kwargs)


def _host(monkeypatch, total):
    monkeypatch.setattr(limits, "host_memory_bytes", lambda *args, **kwargs: total)


# -- the worker bounds -------------------------------------------------------------------------


def test_defaults_stay_conservative_and_a_request_above_them_is_refused():
    adapter = _adapter()
    assert adapter.max_scratch_bytes == 64 * MIB and adapter.scratch_bytes is None
    assert adapter.scratch_for(None) is None
    assert adapter.scratch_for(32 * MIB) == 32 * MIB
    with pytest.raises(ValueError, match="exceeds the operator ceiling of 67108864"):
        adapter.scratch_for(65 * MIB)


def test_an_operator_can_raise_the_ceiling_a_plan_may_then_ask_within_it(monkeypatch):
    _host(monkeypatch, 64 * GIB)
    adapter = _adapter(memory_bytes=4 * GIB, scratch_bytes=16 * MIB, max_scratch_bytes=2 * GIB)
    assert adapter.scratch_for(988 * MIB) == 988 * MIB
    assert adapter.scratch_for(2 * GIB) == 2 * GIB
    with pytest.raises(ValueError, match="exceeds the operator ceiling"):
        adapter.scratch_for(2 * GIB + 1)
    assert adapter.operation_limits()["scratch_bytes"] == 16 * MIB  # the default is unchanged


def test_scratch_is_memory_backed_so_it_must_fit_the_memory_limit(monkeypatch):
    _host(monkeypatch, 64 * GIB)
    adapter = _adapter(memory_bytes=512 * MIB, max_scratch_bytes=2 * GIB)
    with pytest.raises(ValueError, match="needs more memory"):
        adapter.scratch_for(988 * MIB)
    assert adapter.scratch_for(256 * MIB) == 256 * MIB
    with pytest.raises(ValueError, match="needs more memory"):
        CpuOperation(
            "a" * 32, IMAGE, ("x",), "b" * 64, 10, memory_bytes=512 * MIB, scratch_bytes=988 * MIB
        )
    # A default-sized scratch never trips this, so existing small configurations stand.
    CpuOperation(
        "a" * 32, IMAGE, ("x",), "b" * 64, 10, memory_bytes=64 * MIB, scratch_bytes=16 * MIB
    )


def test_the_host_check_bounds_what_an_operator_can_configure(monkeypatch):
    _host(monkeypatch, 8 * GIB)
    with pytest.raises(ValueError, match="max_scratch_bytes"):
        _adapter(memory_bytes=2 * GIB, max_scratch_bytes=5 * GIB)
    with pytest.raises(ValueError, match="memory_bytes"):
        _adapter(memory_bytes=7 * GIB)
    _adapter(memory_bytes=4 * GIB, max_scratch_bytes=4 * GIB)  # within half and three quarters


def test_the_host_check_is_skipped_for_default_settings_and_an_unreadable_host(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(limits, "host_memory_bytes", lambda *a, **k: 1)  # a tiny "host"
    _adapter(memory_bytes=GIB, scratch_bytes=64 * MIB)  # historical bounds: never checked
    monkeypatch.undo()
    limits.check_host_capacity(8 * GIB, 8 * GIB, meminfo=tmp_path / "missing")  # unreadable
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       16777216 kB\n")
    assert limits.host_memory_bytes(meminfo) == 16 * GIB
    with pytest.raises(ValueError):
        limits.check_host_capacity(15 * GIB, None, meminfo=meminfo)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_scratch_bytes": 64 * GIB + 1},
        {"max_scratch_bytes": 0},
        {"scratch_bytes": 128 * MIB, "max_scratch_bytes": 64 * MIB},
    ],
)
def test_ceiling_settings_are_validated(kwargs):
    with pytest.raises(ValueError):
        _adapter(**kwargs)


def test_a_default_scratch_above_the_historical_ceiling_raises_the_ceiling_to_cover_it(monkeypatch):
    _host(monkeypatch, 64 * GIB)
    adapter = _adapter(memory_bytes=2 * GIB, scratch_bytes=GIB)
    assert adapter.max_scratch_bytes == GIB


def test_the_container_gets_the_granted_scratch_and_a_larger_one_is_refused(monkeypatch):
    _host(monkeypatch, 64 * GIB)
    adapter = _adapter(memory_bytes=4 * GIB, max_scratch_bytes=2 * GIB)
    operation = CpuOperation(
        "a" * 32, IMAGE, ("x",), "b" * 64, 10, memory_bytes=4 * GIB, scratch_bytes=GIB
    )
    arguments = adapter.create_arguments(operation)
    assert any(
        item.startswith("/work:rw,noexec,nosuid,nodev,size=1073741824") for item in arguments
    )
    too_big = CpuOperation(
        "a" * 32, IMAGE, ("x",), "b" * 64, 10, memory_bytes=4 * GIB, scratch_bytes=3 * GIB
    )
    with pytest.raises(CpuError, match="differ from the operator"):
        adapter.create_arguments(too_big)


def test_the_runtime_grants_or_refuses_a_plan_request_at_admission(monkeypatch):
    _host(monkeypatch, 64 * GIB)
    runtime = CpuRuntime.__new__(CpuRuntime)
    runtime.adapter = _adapter(memory_bytes=4 * GIB, max_scratch_bytes=GIB)
    assert runtime._operation_limits(512 * MIB)["scratch_bytes"] == 512 * MIB
    assert runtime._operation_limits(None).get("scratch_bytes") is None
    with pytest.raises(SupervisorError, match="exceeds the operator ceiling"):
        runtime._operation_limits(2 * GIB)
    runtime.adapter = object()  # an adapter that grants nothing refuses a request
    with pytest.raises(SupervisorError, match="does not grant"):
        runtime._operation_limits(32 * MIB)


# -- the poller config -------------------------------------------------------------------------


def _config(tmp_path, **extra) -> GrantPollerConfig:
    credential = tmp_path / "credential"
    credential.write_text("x")
    credential.chmod(0o600)
    return GrantPollerConfig(
        "http://127.0.0.1:8765",
        credential,
        tmp_path / "w",
        tmp_path / "o",
        (IMAGE,),
        **extra,
    )


def test_the_poller_config_carries_and_checks_the_ceiling(tmp_path, monkeypatch):
    _host(monkeypatch, 64 * GIB)
    config = _config(tmp_path, memory_bytes=4 * GIB, max_scratch_bytes=2 * GIB)
    assert config.max_scratch_bytes == 2 * GIB
    for bad in (
        {"max_scratch_bytes": 64 * GIB + 1},
        {"max_scratch_bytes": True},
        {"scratch_bytes": 128 * MIB, "max_scratch_bytes": 64 * MIB},
        {"memory_bytes": 65 * GIB},
    ):
        with pytest.raises(DevelopmentWorkerError):
            _config(tmp_path, **bad)
    _host(monkeypatch, 4 * GIB)
    with pytest.raises(DevelopmentWorkerError, match="max_scratch_bytes"):
        _config(tmp_path, memory_bytes=2 * GIB, max_scratch_bytes=3 * GIB)


def test_the_poller_config_file_accepts_the_ceiling(tmp_path, monkeypatch):
    _host(monkeypatch, 64 * GIB)
    credential = tmp_path / "credential"
    credential.write_text("x")
    credential.chmod(0o600)
    path = tmp_path / "poll.json"
    path.write_text(
        json.dumps(
            {
                "schema": "tessera.worker.development-poll.v1",
                "control_url": "http://127.0.0.1:8765",
                "credential_file": str(credential),
                "workspace_root": str(tmp_path / "w"),
                "development_output_root": str(tmp_path / "o"),
                "allowed_images": [IMAGE],
                "memory_bytes": 4 * GIB,
                "max_scratch_bytes": 2 * GIB,
            }
        )
    )
    path.chmod(0o600)
    assert load_poller_config(path).max_scratch_bytes == 2 * GIB


# -- the spec and admission --------------------------------------------------------------------


def _world(tmp_path, **extra):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC, **extra)]
    return world, spec


def test_a_spec_without_scratch_keeps_its_shape_and_digest(tmp_path):
    world, spec = _world(tmp_path)
    parsed = CampaignSpec.model_validate(spec)
    assert "scratch_bytes" not in parsed.model_dump(mode="json")["stages"][0]
    asked = CampaignSpec.model_validate(spec_with(world, 32 * MIB))
    assert asked.model_dump(mode="json")["stages"][0]["scratch_bytes"] == 32 * MIB


@pytest.mark.parametrize("bad", [0, MIB - 1, 64 * GIB + 1, True, "big"])
def test_a_scratch_request_is_bounded_in_the_spec(tmp_path, bad):
    with pytest.raises(ValueError):
        StageSpec.model_validate(
            stage_spec(_world(tmp_path)[0].env, "s1", metrics=METRIC, scratch_bytes=bad)
        )


def _world_spec(world):
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC)]
    return spec


def spec_with(world, scratch):
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC, scratch_bytes=scratch)]
    return spec


def test_admission_refuses_a_request_above_the_providers_ceiling_with_the_reason(tmp_path):
    world = World(tmp_path)
    with pytest.raises(Conflict) as refused:
        world.env.submit(spec=spec_with(world, 988 * MIB))
    message = str(refused.value)
    assert "scratch_exceeds_operator_ceiling" in message
    assert str(988 * MIB) in message and str(64 * MIB) in message  # asked and granted
    assert "max_scratch_bytes" in message
    assert world.row("SELECT count(*) AS n FROM campaigns")["n"] == 0


def test_a_request_within_the_ceiling_is_admitted_and_reaches_the_plan(tmp_path):
    world = World(tmp_path)
    assert world.env.submit(spec=spec_with(world, 64 * MIB))["id"]
    from tessera_control.campaign_models import normalize, stage_execution_spec

    parsed = CampaignSpec.model_validate(spec_with(world, 32 * MIB))
    plan = stage_execution_spec(parsed, "s1", "d" * 64)
    assert plan["max_scratch_bytes"] == 32 * MIB
    bare = CampaignSpec.model_validate(_world_spec(world))
    assert "max_scratch_bytes" not in stage_execution_spec(bare, "s1", "d" * 64)
    assert normalize(parsed).stage("s1").scratch_bytes == 32 * MIB


def test_the_operator_declares_a_higher_ceiling_per_provider(tmp_path):
    world = World(tmp_path)
    register_from_config(
        {
            "local_docker": {
                "worker_principal_id": world.env.worker["id"],
                "max_scratch_bytes": 2 * GIB,
            }
        }
    )
    try:
        assert get_provider("local-docker").descriptor.max_scratch_bytes == 2 * GIB
        assert world.env.submit(spec=spec_with(world, 988 * MIB))["id"]
        with pytest.raises(Conflict, match="scratch_exceeds_operator_ceiling"):
            world.env.submit(spec=spec_with(world, 3 * GIB))
    finally:
        register_from_config(None)
    assert get_provider("local-docker").descriptor.max_scratch_bytes == 64 * MIB


def test_the_provider_file_bounds_the_declared_ceiling():
    for bad in (MIB - 1, 64 * GIB + 1, True, "2GiB"):
        with pytest.raises(ValueError):
            ProvidersFile.model_validate(
                {"local_docker": {"worker_principal_id": "w", "max_scratch_bytes": bad}}
            )
    parsed = ProvidersFile.model_validate(
        {"self_hosted": [{"id": "lab-box", "worker_principal_id": "w", "max_scratch_bytes": GIB}]}
    )
    assert parsed.self_hosted[0].max_scratch_bytes == GIB


def test_a_provider_that_does_not_grant_scratch_refuses_the_request(tmp_path):
    world = World(tmp_path)
    provider = get_provider("local-docker")
    provider.grants_scratch = False
    try:
        with pytest.raises(Conflict, match="does not grant a scratch request"):
            world.env.submit(spec=spec_with(world, 32 * MIB))
    finally:
        del provider.grants_scratch
    assert world.env.submit(spec=spec_with(world, 32 * MIB))["id"]


def test_a_salad_stage_cannot_ask_for_scratch(tmp_path):
    from salad_campaign_fixture import salad_spec, salad_stage

    env = World(tmp_path).env
    stage = salad_stage(env, "g1")
    with pytest.raises(ValueError, match="scratch_bytes is supported only"):
        CampaignSpec.model_validate(salad_spec(env, [{**stage, "scratch_bytes": 32 * MIB}]))


# -- the receipt -------------------------------------------------------------------------------


def _grant_what_is_asked(monkeypatch):
    """The harness worker stands in for a poller whose ceiling covers the request."""
    monkeypatch.setattr(
        ScriptedAdapter, "scratch_for", lambda self, requested: requested, raising=False
    )
    monkeypatch.setattr(ScriptedAdapter, "operation_limits", lambda self: {}, raising=False)


def test_the_receipt_records_the_scratch_asked_and_the_scratch_the_worker_ran_with(
    tmp_path, monkeypatch
):
    _grant_what_is_asked(monkeypatch)
    world = World(tmp_path)
    cid = world.campaign(spec_with(world, 48 * MIB))["id"]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    receipt = json.loads(world.row("SELECT receipt_json FROM campaign_receipts")["receipt_json"])
    (entry,) = [a for stage in receipt["stages"] for a in stage["attempts"]]
    assert entry["scratch"] == {"requested_bytes": 48 * MIB, "granted_bytes": 48 * MIB}
    run = world.row("SELECT receipt_payload_json FROM execution_run_receipts")
    assert json.loads(run["receipt_payload_json"])["scratch_bytes"] == 48 * MIB


def test_a_stage_that_asks_for_nothing_records_the_default_it_ran_with(tmp_path, monkeypatch):
    _grant_what_is_asked(monkeypatch)
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC)]
    cid = world.campaign(spec)["id"]
    world.tick_all(cid)
    receipt = json.loads(world.row("SELECT receipt_json FROM campaign_receipts")["receipt_json"])
    (entry,) = [a for stage in receipt["stages"] for a in stage["attempts"]]
    assert entry["scratch"]["requested_bytes"] is None
    assert entry["scratch"]["granted_bytes"] == 16 * MIB  # the CpuOperation default
