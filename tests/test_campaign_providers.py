"""Provider registry and a provider-neutral runner (ADR 0040 section 2); fakes only."""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest
from campaign_runner_harness import IMAGE, World, dump
from test_campaign_models_v2 import _spec2

from tessera_control import campaign_provider_worker as worker
from tessera_control.campaign_delegation import STEP_TABLE
from tessera_control.campaign_providers import (
    OutputLimits,
    ProviderDescriptor,
    get_provider,
    register_provider,
    unregister_provider,
)
from tessera_control.store import StoreError, canonical_digest


class _Fake(worker.WorkerGrantProvider):
    """A third provider that reuses the worker-grant mechanics under its own id."""


def _descriptor(provider_id: str, steps=None, principal: str = "") -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id=provider_id,
        kind="worker_grant",
        runtimes=frozenset({"docker-cpu-v1"}),
        target=None,
        billable=False,
        third_party=False,
        delegable_steps=frozenset(worker.WORKER_STEPS if steps is None else steps),
        output_limits=OutputLimits(8, 1024 * 1024, 1024 * 1024),
        worker_principal_id=principal,
    )


@pytest.fixture
def fake_provider():
    created = []

    def make(provider_id="fake-cpu", **kwargs):
        provider = register_provider(_Fake(_descriptor(provider_id, **kwargs)))
        created.append(provider_id)
        return provider

    yield make
    for provider_id in created:
        unregister_provider(provider_id)


def test_duplicate_provider_id_is_refused(fake_provider):
    fake_provider("dup-cpu")
    with pytest.raises(ValueError, match="already registered"):
        register_provider(_Fake(_descriptor("dup-cpu")))
    with pytest.raises(ValueError, match="already registered"):
        register_provider(_Fake(_descriptor("local-docker")))


def test_a_provider_declaring_an_unknown_step_is_refused():
    assert "invented_step" not in STEP_TABLE
    with pytest.raises(ValueError, match="unknown steps"):
        register_provider(_Fake(_descriptor("bad-cpu", steps={"create_task", "invented_step"})))
    with pytest.raises(KeyError):
        get_provider("bad-cpu")


def test_builtin_local_docker_is_registered_and_self_hosted_comes_from_the_file():
    descriptor = get_provider("local-docker").descriptor
    assert descriptor.kind == "worker_grant" and descriptor.runtimes == {
        "docker-cpu-v1",
        "docker-build-v1",
    }
    try:
        worker.register_from_config(
            {"self_hosted": [{"id": "self-hosted-a", "worker_principal_id": "worker-a"}]}
        )
        assert get_provider("self-hosted-a").descriptor.worker_principal_id == "worker-a"
        with pytest.raises(ValueError):
            worker.register_from_config(
                {"self_hosted": [{"id": "salad", "worker_principal_id": "w"}]}
            )
        with pytest.raises(ValueError):  # closed model
            worker.register_from_config(
                {"self_hosted": [{"id": "x1", "worker_principal_id": "w", "k": 1}]}
            )
    finally:
        unregister_provider("self-hosted-a")
        worker._REGISTERED.discard("self-hosted-a")
        worker.register_from_config(None)


def test_unknown_provider_is_refused_at_submit(tmp_path):
    world = World(tmp_path)
    stage = _spec2()["stages"][0]
    stage["provider"] = "nowhere"
    # A new dict, never an in-place update: the default input is shared test data.
    stage["inputs"] = [
        {
            **stage["inputs"][0],
            "snapshot_id": world.env.snapshot_id,
            "digest": world.env.snapshot_digest,
        }
    ]
    body = {"spec": _spec2(stage)}
    with pytest.raises(StoreError, match="Unknown compute provider"):
        world.env.store.submit_campaign(
            world.env.owner, world.env.project["id"], "k", canonical_digest(body), body
        )


def test_the_runner_core_has_no_vendor_branches():
    source = (Path(__file__).parents[1] / "src/tessera_control/campaign_runner.py").read_text()
    for forbidden in ("SALAD_BACKEND", "LOCAL_CPU_BACKEND", "SALAD_GPU_BACKEND", "self.salad."):
        assert forbidden not in source
    assert not re.search(r"[\"']salad[\"']|[\"']local-docker[\"']", source)
    assert "backend" not in source.lower().replace("provider-billed backend", "")


def _v2_chain(env, provider: str) -> dict:
    snapshot = {
        "kind": "snapshot",
        "name": "code",
        "snapshot_id": env.snapshot_id,
        "digest": env.snapshot_digest,
        "data_class": "public",
    }
    first = {
        "key": "a",
        "provider": provider,
        "runtime": "docker-cpu-v1",
        "image": IMAGE,
        "argv": ["python", "-c", "print(1)"],
        "outputs": [{"name": "result", "path": "data.json", "kind": "metrics", "max_bytes": 1024}],
        "max_duration_seconds": 60,
        "inputs": [snapshot],
    }
    second = {
        **first,
        "key": "b",
        "inputs": [
            snapshot,
            {"kind": "stage", "name": "up", "stage": "a", "outputs": ["result"]},
        ],
    }
    return _spec2(first, second)


def test_a_fake_third_provider_runs_a_two_stage_v2_campaign_without_runner_changes(
    tmp_path, fake_provider
):
    world = World(tmp_path)
    env = world.env
    fake_provider("fake-cpu", principal=env.worker["id"])
    env.trust(provider="fake-cpu")
    # Distinct outputs: identical bytes would repeat a lineage edge across the stages.
    world.scenarios = [(0, {"data.json": '{"score": 3}'}), (0, {"data.json": '{"score": 4}'})]
    campaign = world.campaign(_v2_chain(env, "fake-cpu"))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    attempts = world.attempts(campaign["id"])
    assert [a["provider_id"] for a in attempts] == ["fake-cpu", "fake-cpu"]
    assert {a["admission_basis"] for a in attempts} == {"public_only", "provider_trust_policy"}
    assert all(a["trust_policy_digest"] for a in attempts)
    intents = env.intents(campaign["id"])
    assert {json.loads(i["resources_json"]).get("provider_id") for i in intents} == {"fake-cpu"}
    receipt = json.loads(
        world.row("SELECT * FROM campaign_receipts WHERE campaign_id=?", campaign["id"])[
            "receipt_json"
        ]
    )
    assert receipt["version"] == 2
    first = receipt["stages"][0]["attempts"][0]
    assert first["provider_id"] == "fake-cpu" and first["runtime"] == "docker-cpu-v1"
    assert first["settlement_basis"] == "local_zero" and first["settlement_final"] is True
    assert first["evidence_kind"] == "run_receipt" and first["evidence_digest"]
    assert [i["direction"] for i in first["io"]] == ["input", "output"]


def test_private_derived_stage_input_is_refused_under_the_default_trust(tmp_path, fake_provider):
    world = World(tmp_path)
    env = world.env
    fake_provider("fake-cpu", principal=env.worker["id"])
    env.trust(["public"], provider="fake-cpu")  # bound, but public only
    body = {"spec": _v2_chain(env, "fake-cpu")}
    from tessera_control.store import Conflict

    with pytest.raises(Conflict, match="not trusted to receive private_derived"):
        env.store.submit_campaign(env.owner, env.project["id"], "k", canonical_digest(body), body)


def test_delegation_refuses_a_step_outside_the_providers_set(tmp_path, fake_provider):
    world = World(tmp_path)
    env = world.env
    fake_provider(
        "fake-cpu", steps=worker.WORKER_STEPS - {"create_task"}, principal=env.worker["id"]
    )
    env.trust(provider="fake-cpu")
    campaign = world.campaign(_v2_chain(env, "fake-cpu"))
    for _ in range(3):
        world.runner.tick()
    intents = [i for i in env.intents(campaign["id"]) if i["step"] == "create_task"]
    assert intents and {i["status"] for i in intents} == {"failed"}
    assert world.row("SELECT task_id FROM campaign_stages WHERE stage_key='a'")["task_id"] is None


def test_a_stored_spec_parses_after_its_provider_is_dropped_or_its_limits_shrink(
    tmp_path, fake_provider
):
    """ADR 0040: provider validation is for submit and approve; the registry may change."""
    from tessera_control.campaign_models import parse_spec_json

    world = World(tmp_path)
    env = world.env
    fake_provider("fake-cpu", principal=env.worker["id"])
    env.trust(provider="fake-cpu")
    campaign = world.campaign(_v2_chain(env, "fake-cpu"))
    text = world.row("SELECT spec_json FROM campaigns WHERE id=?", campaign["id"])["spec_json"]
    assert parse_spec_json(text, providers=True)  # the full check passes while it is registered
    # Lowered output limits: still parses structurally, refused by the full check.
    provider = get_provider("fake-cpu")
    wide = provider.descriptor
    provider.descriptor = dataclasses.replace(wide, output_limits=OutputLimits(1, 1, 1))
    with pytest.raises(ValueError, match="outputs exceed"):
        parse_spec_json(text, providers=True)
    assert parse_spec_json(text).stages[0].provider == "fake-cpu"
    provider.descriptor = wide
    unregister_provider("fake-cpu")
    with pytest.raises(ValueError, match="Unknown compute provider"):
        parse_spec_json(text, providers=True)
    assert parse_spec_json(text).stages[0].provider == "fake-cpu"
    # The in-flight campaign reaches a terminal state and a receipt instead of crashing.
    for _ in range(40):
        world.runner.tick()
        if world.status(campaign["id"]) not in {"approved", "running"}:
            break
    assert world.status(campaign["id"]) in {"failed", "cancelled"}, dump(world, campaign["id"])
    receipt = world.row("SELECT * FROM campaign_receipts WHERE campaign_id=?", campaign["id"])
    assert receipt is not None
