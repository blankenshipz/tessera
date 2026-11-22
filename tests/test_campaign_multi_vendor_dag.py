"""End-to-end multi-vendor DAG: two providers, artifact chaining, reservations; no network.

``local-docker`` runs the first and last stage, a second vendor (a fake plugin loaded
through the operator file) runs the middle stage, and a later campaign pins the final
output by artifact input.
"""

from __future__ import annotations

import json

import pytest
from campaign_runner_harness import IMAGE, ApiChannel, World, dump
from test_campaign_models_v2 import _spec2
from test_execution import _worker

from tessera_control import campaign_provider_worker as worker
from tessera_control import campaign_providers as cp
from tessera_control.campaign_providers import get_provider, unregister_provider
from tessera_worker.grant_poller import DevelopmentGrantPoller, GrantPollerConfig

VENDOR = "fake-vendor"


@pytest.fixture
def second_vendor(tmp_path, monkeypatch):
    """Register a second vendor through the plugin loader, as an operator file would."""
    import sys

    (tmp_path / "fake_vendor_plugin.py").write_text(
        "from tessera_control import campaign_provider_worker as worker\n"
        "from tessera_control.campaign_providers import register_provider\n"
        "def register_from_config(section):\n"
        "    register_provider(worker.WorkerGrantProvider(\n"
        f"        worker._descriptor({VENDOR!r}, section['principal'])))\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    yield lambda local, vendor: cp.configure_providers(
        {
            "local_docker": {"worker_principal_id": local},
            "plugins": {"fake_vendor_plugin": {"principal": vendor}},
        }
    )
    unregister_provider(VENDOR)
    cp._PLUGIN_MODULES.pop(VENDOR, None)
    sys.modules.pop("fake_vendor_plugin", None)
    worker.register_from_config(None)


def _stage(key: str, provider: str, upstream: str | None, snapshot: dict) -> dict:
    inputs = [snapshot]
    if upstream:
        inputs.append({"kind": "stage", "name": "up", "stage": upstream, "outputs": ["result"]})
    return {
        "key": key,
        "provider": provider,
        "runtime": "docker-cpu-v1",
        "image": IMAGE,
        "argv": ["python", "-c", "print(1)"],
        "outputs": [{"name": "result", "path": "data.json", "kind": "metrics", "max_bytes": 1024}],
        "max_duration_seconds": 60,
        "inputs": inputs,
    }


def _snapshot(env) -> dict:
    return {
        "kind": "snapshot",
        "name": "code",
        "snapshot_id": env.snapshot_id,
        "digest": env.snapshot_digest,
        "data_class": "public",
    }


def _ledger(world, campaign_id):
    return world.row("SELECT held_cents, settled_cents FROM campaigns WHERE id=?", campaign_id)


def _chain(env) -> dict:
    snap = _snapshot(env)
    return _spec2(
        _stage("a", "local-docker", None, snap),
        _stage("b", VENDOR, "a", snap),
        _stage("c", "local-docker", "b", snap),
    )


class _TokenChannel(ApiChannel):
    def __init__(self, state, token):
        super().__init__(state)
        self.headers = {"Authorization": f"Bearer {token}"}


def _service_on(world, config, channel):
    """The harness's fake worker service, but speaking as another worker's credential."""
    own, world.channel = world.channel, channel
    try:
        return world._service(config)
    finally:
        world.channel = own


def _setup(world, second_vendor):
    """Bind each vendor to its own worker principal (a principal binds one provider)."""
    env = world.env
    other = _worker(env.state, env.owner, env.project["id"], "vendor-token")
    second_vendor(env.worker["id"], other["id"])
    from provider_trust_fixture import DERIVED, install_trust

    install_trust({"local-docker": (DERIVED, [env.worker["id"]]), VENDOR: (DERIVED, [other["id"]])})
    credential = world.tmp / "vendor.credential"
    credential.write_text("vendor-token")
    credential.chmod(0o600)
    vendor_channel = _TokenChannel(env.state, "vendor-token")
    vendor_poller = DevelopmentGrantPoller(
        GrantPollerConfig(
            "http://127.0.0.1:8765",
            credential,
            world.tmp / "vendor-root",
            world.tmp / "vendor-out",
            (IMAGE,),
        ),
        channel=vendor_channel,
        service_factory=lambda config: _service_on(world, config, vendor_channel),
        sleep=lambda _: None,
    )
    # One poll cycle drives both vendors' workers.
    local_poller = world.poller

    class _Both:
        def run(self, max_cycles=1):
            local_poller.run(max_cycles=max_cycles)
            vendor_poller.run(max_cycles=max_cycles)

    world.poller = _Both()
    return env


def test_three_stages_across_two_vendors_chain_artifacts_and_release_reservations(
    tmp_path, second_vendor
):
    world = World(tmp_path)
    env = _setup(world, second_vendor)
    world.scenarios = [
        (0, {"data.json": '{"score": 1}'}),
        (0, {"data.json": '{"score": 2}'}),
        (0, {"data.json": '{"score": 3}'}),
    ]
    campaign = world.campaign(_chain(env))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])

    attempts = world.attempts(campaign["id"])
    assert [a["provider_id"] for a in attempts] == ["local-docker", VENDOR, "local-docker"]
    assert get_provider(VENDOR).descriptor.provider_id == VENDOR
    assert cp.plugin_of(VENDOR) == "fake_vendor_plugin"
    # Every attempt settled at zero and nothing is still reserved.
    assert all(a["settled_cents"] == 0 for a in attempts)
    assert dict(_ledger(world, campaign["id"])) == {"held_cents": 0, "settled_cents": 0}

    # Data edges: b read a's output and c read b's, each recorded in the stage I/O index.
    rows = env.query(
        "SELECT stage_key, direction, name, sha256, snapshot_id FROM campaign_stage_io "
        "WHERE campaign_id=? "
        "ORDER BY stage_key, direction, name",
        campaign["id"],
    )
    outputs = {r["stage_key"]: r["sha256"] for r in rows if r["direction"] == "output"}
    ups = {
        r["stage_key"]: r["sha256"]
        for r in rows
        if r["direction"] == "input" and r["snapshot_id"] is None
    }
    assert len(set(outputs.values())) == 3
    assert ups == {"b": outputs["a"], "c": outputs["b"]}

    receipt = json.loads(
        world.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", campaign["id"])[
            "receipt_json"
        ]
    )
    assert [s["attempts"][0]["provider_id"] for s in receipt["stages"]] == [
        "local-docker",
        VENDOR,
        "local-docker",
    ]
    assert all(s["attempts"][0]["settlement_basis"] == "local_zero" for s in receipt["stages"])

    # A second campaign pins the last stage's output by artifact input.
    final = next(r for r in rows if r["stage_key"] == "c" and r["direction"] == "output")
    artifact = {
        "kind": "artifact",
        "name": "prior",
        "campaign_id": campaign["id"],
        "stage": "c",
        "output": final["name"],
        "sha256": final["sha256"],
    }
    follow = _stage("d", VENDOR, None, _snapshot(env))
    follow["inputs"].append(artifact)
    world.scenarios = [(0, {"data.json": '{"score": 4}'})]
    second = world.campaign(_spec2(follow))
    world.tick_all(second["id"])
    assert world.status(second["id"]) == "succeeded", dump(world, second["id"])
    pinned = env.query(
        "SELECT sha256 FROM campaign_stage_io WHERE campaign_id=? AND name='prior'", second["id"]
    )
    assert [r["sha256"] for r in pinned] == [final["sha256"]]
    assert dict(_ledger(world, second["id"])) == {"held_cents": 0, "settled_cents": 0}


def test_a_failing_middle_stage_releases_its_reservation_and_skips_downstream(
    tmp_path, second_vendor
):
    world = World(tmp_path)
    env = _setup(world, second_vendor)
    world.scenarios = [(0, {"data.json": '{"score": 1}'}), (9, {"data.json": '{"score": 2}'})]
    campaign = world.campaign(_chain(env))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) != "succeeded", dump(world, campaign["id"])
    stages = world.stages(campaign["id"])
    assert stages == {"a": "succeeded", "b": "failed", "c": "skipped"}, stages
    attempts = world.attempts(campaign["id"])
    assert {a["provider_id"] for a in attempts if a["stage_key"] == "b"} == {VENDOR}
    assert all(a["settled_cents"] is not None for a in attempts)
    assert dict(_ledger(world, campaign["id"]))["held_cents"] == 0
