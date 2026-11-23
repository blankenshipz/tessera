# ruff: noqa: F811
"""A licence label inherited through a completed upstream stage keeps a heavy dependent stage
off Salad at placement, and local says why (ADR 0141 candidate, ADR 0107 heavy guard).

The upstream stage ``up`` reads a labelled snapshot and runs to success on the trusted local
CPU host (a worker-grant provider driven by a fake poller and scripted adapter), so ``g1`` is
placed from a real stage output. Fakes only: no Salad call, no network. Labels are generic
stand-ins for an operator's non-commercial and restricted-derivation sources.
"""

from __future__ import annotations

import json

import pytest
from campaign_runner_harness import ApiChannel, ScriptedAdapter
from salad_campaign_fixture import SaladWorld, salad_provider_reset, salad_spec_v2  # noqa: F401
from test_heavy_placement_data_class import (  # noqa: F401
    NON_COMMERCIAL,
    RESTRICTED,
    _label,
    _never_salad,
    _placed,
    _receipt,
    _trust,
)
from test_heavy_stage_remote_candidate import GIB, _configure, _reset, _stored  # noqa: F401
from test_salad_stage_adapter import NEAR, cpu_world, near, request, stage  # noqa: F401

from tessera_control import campaign_heavy_guard as guard
from tessera_worker.grant_poller import DevelopmentGrantPoller, GrantPollerConfig
from tessera_worker.service import DevelopmentWorkerService

OPEN_ONLY = ["open"]
INPUT = [{"kind": "stage", "name": "up", "stage": "up", "outputs": ["output"]}]


class _Near:
    """Drives the local host's worker: claim, run (scripted), report, for the near provider."""

    def __init__(self, world, near):
        env = world.env
        self.tmp = world.env.state.parent
        credential = self.tmp / "near.credential"
        credential.write_text("near-token")
        credential.chmod(0o600)
        self.channel = ApiChannel(env.state)
        self.channel.headers = {"Authorization": "Bearer near-token"}
        self.runs: list[str] = []
        self.poller = DevelopmentGrantPoller(
            GrantPollerConfig(
                "http://127.0.0.1:8765",
                credential,
                self.tmp / "near-root",
                self.tmp / "near-out",
                (stage(env)["image"],),
            ),
            channel=self.channel,
            service_factory=self._service,
            sleep=lambda _: None,
        )

    def _service(self, config):
        adapter = ScriptedAdapter(0, {"output.json": json.dumps({"score": 3})})
        adapter.development_output_root = (
            config.development_output_root / "development-only-cpu-outputs" / "workspaces"
        )
        self.runs.append(config.grant_id)

        def finish(_):
            adapter.state = "exited"

        return DevelopmentWorkerService(config, channel=self.channel, adapter=adapter, sleep=finish)


def _stage_status(world, key):
    row = world.row(
        "SELECT status FROM campaign_stages WHERE campaign_id=? AND stage_key=?",
        world.campaign_id,
        key,
    )
    return None if row is None else row["status"]


def _explicit(env):
    """A Salad candidate the author wrote out (no adapter request: that cannot read a stage)."""
    from salad_campaign_fixture import salad_stage

    from tessera_control.campaign_models import SALAD_RUNTIME

    base = salad_stage(env)["salad"]
    egress = {"kind": "provider_broad", "fetches": []}  # derived data may declare no fetch hosts
    workload = {**base["workload"], "egress": egress}
    config = {**base, "workload": workload}
    return {"provider": "salad", "runtime": SALAD_RUNTIME, "provider_config": config}


def _contract_argv(env):
    from salad_campaign_fixture import salad_stage

    return salad_stage(env)["argv"]


def _lineage_world(tmp_path, near, label, *, listed):
    """``up`` (local, labelled input) then heavy ``g1`` reading its output; Salad forbidden it."""
    if not listed:
        _configure()
    if listed:  # an explicit (non-adapter) Salad candidate needs the GPU profile
        world = SaladWorld(tmp_path)
        near(world, 1)
    else:
        world = cpu_world(tmp_path, near, slots=1)
    classes = ("public", "private_derived")
    if listed:  # approved while unrestricted (a listed forbidden label is refused at submit)
        _trust(world, near, salad_classes=classes)

        def tighten():
            _label(world, [label])
            _trust(world, near, salad_classes=classes, salad_labels=OPEN_ONLY)

        world.tighten = tighten
    else:
        world.tighten = lambda: None
        _trust(world, near, salad_classes=classes, salad_labels=OPEN_ONLY)
        _label(world, [label])
    env = world.env
    upstream = stage(env, key="up", alternate_providers=None)
    heavy = stage(
        env,
        key="g1",
        alternate_providers=[_explicit(env)] if listed else None,
        memory_bytes=2 * GIB,
        inputs=INPUT,
        **({"argv": _contract_argv(env)} if listed else {}),
    )
    world.spec = salad_spec_v2(env, [upstream, heavy])
    return world


def _run_upstream(world, near_driver):
    world.start()
    world.tighten()
    stages = {s["key"]: s for s in _stored(world)["stages"]}
    world.tick_until(
        lambda: (
            _stage_status(world, "up") == "succeeded"
            or near_driver.poller.run(max_cycles=1)
            or _stage_status(world, "up") == "succeeded"
        ),
        limit=120,
    )
    return stages


@pytest.mark.parametrize("label", [RESTRICTED, NON_COMMERCIAL])
def test_a_heavy_stage_with_a_candidate_setting_keeps_off_salad_for_an_inherited_label(
    tmp_path, near, label
):
    world = _lineage_world(tmp_path, near, label, listed=False)
    driver = _Near(world, near)
    stages = _run_upstream(world, driver)
    assert not stages["g1"].get("alternate_providers")
    record = stages["g1"]["heavy_remote_candidate"]
    assert record["added"] is False, record
    _assert_local_and_explained(world, driver, label)


@pytest.mark.parametrize("label", [RESTRICTED, NON_COMMERCIAL])
def test_a_listed_salad_is_skipped_for_an_inherited_label_and_local_says_why(tmp_path, near, label):
    world = _lineage_world(tmp_path, near, label, listed=True)
    driver = _Near(world, near)
    stages = _run_upstream(world, driver)
    assert stages["g1"]["alternate_providers"][0]["provider"] == "salad"
    _assert_local_and_explained(world, driver, label)
    (placed,) = _placed(world)
    (skip,) = [s for s in placed["skipped"] if s["provider_id"] == "salad"]
    assert skip["reason"] == "license_label" and label in skip["detail"]
    assert placed["heavy_guard"]["reasons"] == ["remote_skipped"]


def _assert_local_and_explained(world, driver, label):
    def landed():
        driver.poller.run(max_cycles=1)
        attempt = world.attempt("g1")
        return attempt is not None

    world.tick_until(landed, limit=120)
    assert world.attempt("g1")["provider_id"] == NEAR
    assert "heavy_local_unexplained" not in world.dump()
    (event,) = world.events(guard.EVENT)
    assert event["reasons"] and event["reasons"] not in (["not_listed"], ["remote_not_tried"])
    for placed in _placed(world):
        assert placed["heavy_guard"]["reasons"] == event["reasons"]
    receipt_stage = {s["stage_key"]: s for s in _receipt(world)["stages"]}["g1"]
    assert label in receipt_stage["input"]["license_labels"]
    assert "heavy_local_unexplained" not in json.dumps(receipt_stage)
    (entry,) = receipt_stage["attempts"]
    assert entry["placement"]["heavy_guard"]["reasons"] == event["reasons"]
    _never_salad(world)
