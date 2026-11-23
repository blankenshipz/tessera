"""A Salad stage that reads another stage's output (late-bound input), end to end; no network.

The upstream stage runs on a worker-grant CPU host through the development worker (a scripted
adapter writes its output); the dependent stage is adapted to Salad (ADR 0076) and placed there
because the host has no free slot for it. The Salad executor, guardian and human are the
``SaladWorld`` test doubles: no Salad call is made.
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest
from campaign_runner_harness import ApiChannel, ScriptedAdapter
from salad_campaign_fixture import (  # noqa: F401
    IMAGE,
    SaladWorld,
    salad_provider_reset,
    salad_spec_v2,
)
from test_execution import _worker
from test_salad_stage_adapter import cpu_world, stage

from tessera_control import campaign_provider_worker as worker
from tessera_control import late_bound_inputs
from tessera_control.campaign_models import SaladStageSpec, parse_spec
from tessera_control.campaign_providers import register_provider, unregister_provider
from tessera_control.campaign_receipts import build_receipt
from tessera_control.campaign_salad import (
    LATE_BIND_REFUSED_EVENT,
    LATE_BOUND_EVENT,
    stage_workload,
)
from tessera_control.campaign_salad_adapter import (
    bind_workload,
    expand_spec,
    input_plan,
    late_bind_key,
    upstream_outputs,
)
from tessera_control.provider_trust import install_policy, policy_from_document
from tessera_control.store import Conflict
from tessera_worker.grant_poller import DevelopmentGrantPoller, GrantPollerConfig
from tessera_worker.service import DevelopmentWorkerService

HOST = "cpu-host"
UP = "prep"
OUTPUT = b'{"rows": 3}'


class _Host(worker.WorkerGrantProvider):
    """A CPU host with a slot for the upstream stage only, so the dependent stage's Salad
    candidate is the one placed."""

    def free_slots(self, rt, ctx, stage):
        return 1 if stage.key == UP else 0


@pytest.fixture
def host():
    yield
    unregister_provider(HOST)


def _trust(world, host_worker, salad_labels=None):
    salad = {"data_classes": ["public", "private_derived"], "worker_principal_ids": []}
    salad["worker_principal_ids"].append(world.worker["id"])
    if salad_labels is not None:
        salad["allowed_license_labels"] = salad_labels
    document = {
        "schema": "tessera.provider-trust.v1",
        "providers": {
            HOST: {
                "data_classes": ["public", "private_derived"],
                "worker_principal_ids": [host_worker["id"]],
            },
            "salad": salad,
        },
    }
    install_policy(policy_from_document(document, "file"))


def _poller(world, tmp_path, token):
    channel = ApiChannel(world.env.state)
    channel.headers = {"Authorization": f"Bearer {token}"}
    credential = tmp_path / "host.credential"
    credential.write_text(token)
    credential.chmod(0o600)

    def service(config):
        adapter = ScriptedAdapter(0, {"data.json": OUTPUT.decode()})
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


def _dag(world):
    env = world.env
    upstream = stage(
        env,
        key=UP,
        provider=HOST,
        alternate_providers=None,
        outputs=[{"name": "result", "path": "data.json", "max_bytes": 1024}],
    )
    dependent = stage(
        env,
        provider=HOST,
        inputs=[{"kind": "stage", "name": "up", "stage": UP, "outputs": ["result"]}],
    )
    return salad_spec_v2(env, [upstream, dependent])


def _world(tmp_path, host, salad_labels=None):
    world = cpu_world(tmp_path)
    env = world.env
    host_worker = _worker(env.state, env.owner, env.project["id"], "host-token")
    register_provider(_Host(worker._descriptor(HOST, host_worker["id"], None, 1)))
    _trust(world, host_worker, salad_labels)
    world.spec = _dag(world)
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
            break
    else:
        raise AssertionError("upstream did not succeed\n" + world.dump())
    return world


def _stage_row(world, key="g1"):
    return world.row(
        "SELECT * FROM campaign_stages WHERE campaign_id=? AND stage_key=?", world.campaign_id, key
    )


def _to_salad_stage(world):
    world.tick_until(lambda: world.row("SELECT 1 FROM campaign_salad_stages") is not None)
    world.tick_until(
        lambda: (
            world.row("SELECT source_id FROM campaign_salad_stages WHERE source_id IS NOT NULL")
            is not None
        )
    )
    world.assess_source()


# -- end to end ------------------------------------------------------------------------------


def test_a_salad_stage_consumes_a_host_produced_output_bound_at_start(tmp_path, host):
    world = _world(tmp_path, host)
    _to_salad_stage(world)
    world.tick_until_phase("awaiting_lease")
    (placed,) = [p for p in world.events("campaign.stage_placed") if p["stage_key"] == "g1"]
    assert placed["provider_id"] == "salad"

    # The runner composed the upstream output; the driver bound it and recorded what it checked.
    row = _stage_row(world)
    digest = row["input_snapshot_digest"]
    (bound,) = world.events(LATE_BOUND_EVENT)
    assert bound["snapshot_digest"] == digest
    (item,) = bound["inputs"]
    assert item["source_stage"] == UP and item["output"] == "result" and item["name"] == "up"
    upstream_io = world.row(
        "SELECT sha256, size_bytes FROM campaign_stage_io WHERE campaign_id=? AND stage_key=? "
        "AND direction='output'",
        world.campaign_id,
        UP,
    )
    assert (item["digest"], item["bytes"]) == (upstream_io["sha256"], upstream_io["size_bytes"])
    assert item["bytes"] == len(OUTPUT) and bound["snapshot_bytes"] <= bound["max_bytes"] == 1024

    world.lease_and_package()
    world.tick_until_phase("awaiting_review")
    world.resolve_decisions()
    world.tick_until_phase("route_started")

    # The route names the composed snapshot, and its contract and argv carry the same digest.
    selection = world.route_state()["proposal"]["selection"]
    assert selection["manifests"]["data"] == digest
    work = selection["salad"]["execution"]["workload"]
    assert work["input"]["snapshot_digest"] == digest and work["input"]["max_bytes"] == 1024
    assert f"input:{digest}:1024" in work["argv_template"]
    plans = [
        json.loads(r["execution_spec_json"])
        for r in world.env.query(
            "SELECT execution_spec_json FROM task_revisions WHERE execution_spec_json IS NOT NULL"
        )
    ]
    salad_plan = next(p for p in plans if p["argv"][0] == "/tessera/bin/run")
    assert salad_plan["input_snapshot_digest"] == digest
    assert f"input:{digest}:1024" in salad_plan["argv"]

    _executor_upload(world, tmp_path)
    world.run_to_outcome()
    world.tick_until_phase("awaiting_charge")
    with world.env.store.runner_connection() as connection:
        campaign = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (world.campaign_id,)
        ).fetchone()
        receipt, _ = build_receipt(connection, campaign, "succeeded")
    entry = next(s for s in receipt["stages"] if s["stage_key"] == "g1")
    lineage = entry["attempts"][0]["late_bound_inputs"]
    assert lineage["snapshot_digest"] == digest
    assert lineage["inputs"] == [
        {
            "name": "up",
            "source_stage": UP,
            "output": "result",
            "digest": item["digest"],
            "bytes": len(OUTPUT),
            "data_class": item["data_class"],
        }
    ]


def _executor_upload(world, tmp_path):
    """Upload the Salad outputs through the executor's own control port (project placement
    first, then the manifest): the project already has a storage binding from the host stage."""
    import hashlib

    from tessera_control.campaign_salad_executor import StoreControl

    control = StoreControl(world.env.state, world.worker)

    def upload(content=b'{"score": 1}'):
        row = world.salad()
        directory = tmp_path / "salad-out"
        directory.mkdir(exist_ok=True)
        (directory / "output.json").write_bytes(content)
        files = [{"name": "output.json", "sha256": hashlib.sha256(content).hexdigest()}]
        return control.upload_outputs(
            row["lease_id"], row["epoch"], row["operation_id"], directory, files
        )

    world.upload_manifest = upload


def _patched(monkeypatch, change):
    real = late_bound_inputs.resolve_stage_inputs

    def fake(*args, **kwargs):
        return tuple(dataclasses.replace(r, **change) for r in real(*args, **kwargs))

    monkeypatch.setattr(late_bound_inputs, "resolve_stage_inputs", fake)


def _refused(world):
    _to_salad_stage(world)
    world.tick_until(lambda: _stage_row(world)["status"] == "failed")
    # The runner binds and refuses first (ADR 0140); the Salad driver's own refusal event is the
    # second line of defence for what the runner admitted.
    (refused,) = world.events(LATE_BIND_REFUSED_EVENT) + world.events(
        late_bound_inputs.REFUSED_EVENT
    )
    assert world.events(LATE_BOUND_EVENT) == []
    assert world.row("SELECT 1 FROM campaign_salad_attempts") is None  # nothing reached Salad
    return refused["code"]


def test_a_snapshot_that_differs_from_the_resolved_digest_is_refused(tmp_path, host, monkeypatch):
    # The resolved digest no longer matches the bytes the runner composes: the runner's staging
    # step refuses first (fail closed), so nothing is bound, proposed or sent to Salad.
    world = _world(tmp_path, host)
    _patched(monkeypatch, {"digest": "e" * 64})
    _to_salad_stage(world)
    world.tick_until(lambda: _stage_row(world)["status"] == "failed")
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", world.campaign_id)
    assert stop["stop_reason"] == "stage_failed:g1:step_refused"
    assert world.events(LATE_BOUND_EVENT) == []
    assert world.row("SELECT 1 FROM campaign_salad_attempts") is None  # nothing reached Salad


def test_a_resolved_output_whose_licence_salad_may_not_receive_is_refused(
    tmp_path, host, monkeypatch
):
    # Salad may receive only unlabelled data; the resolved upstream output turns out to carry a
    # non-commercial label (as a restricted dataset's derived rows would).
    world = _world(tmp_path, host, salad_labels=["unlabelled"])
    _patched(monkeypatch, {"license_labels": ("non-commercial",)})
    assert _refused(world) == "license_refused"


def test_a_resolver_refusal_is_the_closed_code(tmp_path, host, monkeypatch):
    world = _world(tmp_path, host)

    def refuse(*args, **kwargs):
        raise late_bound_inputs.LateBindRefused("upstream_not_settled", "test double")

    monkeypatch.setattr(late_bound_inputs, "resolve_stage_inputs", refuse)
    code = _refused(world)
    assert code == "upstream_not_settled" and code in late_bound_inputs.REFUSAL_CODES


# -- submit-time placement (pure) -------------------------------------------------------------


def test_the_contract_declares_the_late_bind_key_and_the_declared_bound(tmp_path, host):
    world = cpu_world(tmp_path)
    register_provider(_Host(worker._descriptor(HOST, world.worker["id"], None, 1)))
    spec = parse_spec(_dag(world))
    (item,) = expand_spec(spec, world.profiles, tenant=None).stages[1].alternate_providers
    work = SaladStageSpec.model_validate(item.provider_config).workload
    key = late_bind_key(spec.stages[1], upstream_outputs(spec))
    assert work.input.snapshot_digest == key and work.input.max_bytes == 1024
    assert f"input:{key}:1024" in work.argv_template
    # The normal form (resolved paths) gives the same key, so the driver recognises it.
    normal = SimpleNamespace(
        inputs=[SimpleNamespace(kind="stage", name="up", stage=UP, paths=("data.json",))]
    )
    assert late_bind_key(normal) == key
    bound = bind_workload(work, "a" * 64)
    assert bound.input.snapshot_digest == "a" * 64 and bound.input.max_bytes == 1024
    assert f"input:{'a' * 64}:1024" in bound.argv_template and key not in bound.argv_template
    # Until the runner composes the input, the reviewed contract is unchanged.
    config = SimpleNamespace(provider_config=item.provider_config, **vars(normal))
    assert stage_workload({"input_snapshot_digest": None}, config) == work
    assert stage_workload({"input_snapshot_digest": "a" * 64}, config) == bound


def _profile(max_input_bytes=1 << 20):
    return SimpleNamespace(
        name="cpu-main",
        disk_mb=1,
        stage_adapter=SimpleNamespace(max_input_bytes=max_input_bytes),
    )


def _late(name="up"):
    return SimpleNamespace(kind="stage", name=name, stage=UP, outputs=["result"], key="g1")


def test_an_unbounded_upstream_output_is_refused():
    item = _late()
    holder = SimpleNamespace(key="g1", inputs=[item])
    with pytest.raises(Conflict, match="no declared max_bytes"):
        input_plan(holder, _profile(), None, lambda source: None)
    with pytest.raises(Conflict, match="no declared max_bytes"):
        input_plan(holder, _profile(), None, None)


def test_the_declared_bound_is_held_to_the_profile_caps():
    holder = SimpleNamespace(key="g1", inputs=[_late()])
    with pytest.raises(Conflict, match="max_input_bytes 100"):
        input_plan(holder, _profile(100), None, lambda source: [("data.json", 1024)])
    with pytest.raises(Conflict, match="disk_mb"):
        input_plan(holder, _profile(None), None, lambda source: [("data.json", 2 << 20)])
    plan = input_plan(holder, _profile(), None, lambda source: [("data.json", 1024)])
    assert plan[1] == 1024 and len(plan[0]) == 64


def test_a_known_input_beside_a_late_one_needs_the_recorded_sizes():
    snapshot = SimpleNamespace(kind="snapshot", name="code", digest="b" * 64)
    holder = SimpleNamespace(key="g1", inputs=[snapshot, _late()])
    with pytest.raises(Conflict, match="cannot be sized here"):
        input_plan(holder, _profile(), None, lambda source: [("data.json", 10)])
    plan = input_plan(
        holder, _profile(), lambda source: [("main.py", "c" * 64, 5)], lambda s: [("data.json", 10)]
    )
    assert plan[1] == 15


def test_a_refused_salad_alternate_shows_on_the_receipt(tmp_path, host, monkeypatch):
    """The runner binds the stage (local resolver call passes); the Salad driver's own resolution
    is then refused. It records the runner's refusal event beside its own, in one step with the
    failure, and the receipt shows ``input_not_pinned`` after the bound entry (fail closed)."""
    import inspect

    real = late_bound_inputs.resolve_stage_inputs

    def refuse_from_the_salad_driver(*args, **kwargs):
        if any(f.filename.endswith("campaign_salad.py") for f in inspect.stack()):
            raise late_bound_inputs.LateBindRefused(
                "license_refused",
                "private free text /secret/path",
                input_name="up",
                source_stage=UP,
            )
        return real(*args, **kwargs)

    world = _world(tmp_path, host)
    monkeypatch.setattr(late_bound_inputs, "resolve_stage_inputs", refuse_from_the_salad_driver)
    _to_salad_stage(world)
    world.tick_until(lambda: _stage_row(world)["status"] == "failed")
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", world.campaign_id)
    assert stop["stop_reason"] == "stage_failed:g1:license_refused"
    assert world.row("SELECT 1 FROM campaign_salad_attempts") is None  # nothing reached Salad
    assert world.events(LATE_BOUND_EVENT) == []
    (salad_event,) = world.events(LATE_BIND_REFUSED_EVENT)
    (event,) = world.events(late_bound_inputs.REFUSED_EVENT)
    assert salad_event["code"] == event["code"] == "license_refused"
    assert event["input"] == "up" and event["source_stage"] == UP and event["provider"] == "salad"
    assert "reason" not in event
    with world.env.store.runner_connection() as connection:
        campaign = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (world.campaign_id,)
        ).fetchone()
        receipt, _ = build_receipt(connection, campaign, "failed")
    entry = next(s for s in receipt["stages"] if s["stage_key"] == "g1")
    assert entry["input"][late_bound_inputs.LAUNCH_INPUTS_KEY][-1] == {
        "status": "input_not_pinned",
        "code": "license_refused",
        "input": "up",
        "source_stage": UP,
    }
    assert "secret" not in json.dumps(receipt)
