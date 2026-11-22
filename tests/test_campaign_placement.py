"""Capacity-aware placement on approved provider candidates (ADR 0060). Fakes only.

Two unbilled worker-grant providers stand in for the project host's CPU slot (``near-cpu``)
and another registered CPU provider (``far-cpu``), each with its own enrolled worker
principal and fake poller. No network, no provider, no money.
"""

from __future__ import annotations

import json

import pytest
from campaign_runner_harness import IMAGE, ApiChannel, World, dump
from test_campaign_models_v2 import _spec2
from test_execution import _worker

from tessera_control import campaign_provider_worker as worker
from tessera_control.campaign_providers import register_provider, unregister_provider
from tessera_control.store import Conflict, NotFound, StoreError, canonical_digest
from tessera_worker.grant_poller import DevelopmentGrantPoller, GrantPollerConfig

NEAR, FAR, GHOST = "near-cpu", "far-cpu", "ghost-cpu"
PLACED = "campaign.stage_placed"
WAITING = "campaign.stage_waiting_placement"


class _TokenChannel(ApiChannel):
    def __init__(self, state, token):
        super().__init__(state)
        self.headers = {"Authorization": f"Bearer {token}"}


def _register(provider_id: str, principal: str, slots: int = 1) -> None:
    register_provider(
        worker.WorkerGrantProvider(worker._descriptor(provider_id, principal, None, slots))
    )


@pytest.fixture
def providers():
    made: list[str] = []

    def make(provider_id: str, principal: str, slots: int = 1) -> None:
        _register(provider_id, principal, slots)
        made.append(provider_id)

    yield make
    for provider_id in made:
        unregister_provider(provider_id)


def _trust(env, far_worker, near=None, far=None, ghost_worker=None):
    from provider_trust_fixture import DERIVED, install_trust

    table = {
        NEAR: (DERIVED if near is None else near, [env.worker["id"]]),
        FAR: (DERIVED if far is None else far, [far_worker["id"]]),
    }
    if ghost_worker is not None:  # a principal binds one provider
        table[GHOST] = (DERIVED, [ghost_worker["id"]])
    policy = install_trust(table)
    # Stored as `tessera serve` stores it, so an approval can name the setting it was checked
    # against and a later narrowing intersects with it rather than with "unknown".
    from tessera_control.provider_trust import record_policy
    from tessera_control.store import _connect, utc_now

    connection = _connect(env.state / "tessera.sqlite3")
    try:
        with connection:
            record_policy(connection, policy, "operator", utc_now())
    finally:
        connection.close()


def _world(tmp_path, providers, *, ghost=False):
    """near-cpu on the fixture's worker, far-cpu on a second worker with its own poller."""
    world = World(tmp_path)
    env = world.env
    far_worker = _worker(env.state, env.owner, env.project["id"], "far-token")
    providers(NEAR, env.worker["id"])
    providers(FAR, far_worker["id"])
    ghost_worker = None
    if ghost:
        ghost_worker = _worker(env.state, env.owner, env.project["id"], "ghost-token")
        _register(GHOST, ghost_worker["id"])
    _trust(env, far_worker, ghost_worker=ghost_worker)
    env.execute("UPDATE execution_project_limits SET max_concurrency=4")
    credential = world.tmp / "far.credential"
    credential.write_text("far-token")
    credential.chmod(0o600)
    far_channel = _TokenChannel(env.state, "far-token")

    def far_service(config):
        own, world.channel = world.channel, far_channel
        try:
            return world._service(config)
        finally:
            world.channel = own

    far_poller = DevelopmentGrantPoller(
        GrantPollerConfig(
            "http://127.0.0.1:8765",
            credential,
            world.tmp / "far-root",
            world.tmp / "far-out",
            (IMAGE,),
        ),
        channel=far_channel,
        service_factory=far_service,
        sleep=lambda _: None,
    )
    near_poller = world.poller

    class _Both:
        def run(self, max_cycles=1):
            near_poller.run(max_cycles=max_cycles)
            far_poller.run(max_cycles=max_cycles)

    world.poller = _Both()
    world.far_worker = far_worker
    return world


def _snapshot(env) -> dict:
    return {
        "kind": "snapshot",
        "name": "code",
        "snapshot_id": env.snapshot_id,
        "digest": env.snapshot_digest,
        "data_class": "public",
    }


def _stage(env, key, provider=NEAR, alternates=(FAR,), upstream=None, exposure=0, **extra):
    inputs = [_snapshot(env)]
    if upstream:
        inputs.append({"kind": "stage", "name": "up", "stage": upstream, "outputs": ["result"]})
    stage = {
        "key": key,
        "provider": provider,
        "runtime": "docker-cpu-v1",
        "image": IMAGE,
        "argv": ["python", "-c", "print(1)"],
        "outputs": [{"name": "result", "path": "data.json", "kind": "metrics", "max_bytes": 1024}],
        "max_duration_seconds": 60,
        "max_exposure_cents": exposure,
        "inputs": inputs,
        **extra,
    }
    if alternates:
        stage["alternate_providers"] = [
            item if isinstance(item, dict) else {"provider": item, "runtime": "docker-cpu-v1"}
            for item in alternates
        ]
    return stage


def _placed(world, campaign_id):
    return [
        e["payload"] for e in world.events_of(PLACED) if e["payload"]["campaign_id"] == campaign_id
    ]


def _receipt(world, campaign_id):
    row = world.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", campaign_id)
    return json.loads(row["receipt_json"])


def _grants(world):
    return world.env.query("SELECT worker_principal_id FROM execution_grants ORDER BY created_at")


# -- 1. spec form ---------------------------------------------------------------------------


def test_single_provider_specs_keep_their_digest_and_never_record_a_placement(tmp_path, providers):
    from tessera_control.campaign_models import parse_spec, spec_digest

    world = _world(tmp_path, providers)
    env = world.env
    single = _spec2(_stage(env, "a", alternates=()))
    assert "alternate_providers" not in parse_spec(single).model_dump(mode="json")["stages"][0]
    # The dump (and so the digest) is the one this spec had before alternates existed.
    explicit = {**single, "stages": [{**single["stages"][0], "alternate_providers": None}]}
    assert spec_digest(parse_spec(explicit)) == spec_digest(parse_spec(single))
    campaign = world.campaign(single)
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    assert not _placed(world, campaign["id"])
    entry = _receipt(world, campaign["id"])["stages"][0]["attempts"][0]
    assert "placement" not in entry and entry["provider_id"] == NEAR


def test_candidates_are_validated_like_the_stages_own_provider(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env

    def submit(spec):
        body = {"spec": spec}
        digest = canonical_digest(body)
        return env.store.submit_campaign(
            env.owner, env.project["id"], "k-" + digest[:12], digest, body
        )

    with pytest.raises(StoreError, match="Unknown compute provider: 'nowhere-cpu'"):
        submit(_spec2(_stage(env, "a", alternates=("nowhere-cpu",))))
    with pytest.raises(StoreError, match="distinct"):
        submit(_spec2(_stage(env, "a", alternates=(NEAR,))))
    with pytest.raises(StoreError, match="forbidden"):
        submit(_spec2(_stage(env, "a"), forbid_providers=[FAR]))
    over = {"provider": FAR, "runtime": "docker-cpu-v1", "max_exposure_cents": 5}
    with pytest.raises(StoreError, match="estimate exceeds"):
        submit(_spec2(_stage(env, "a", alternates=(over,), exposure=4)))
    # A candidate whose worker may not receive the stage's derived input is refused at submit.
    _trust(env, world.far_worker, far=["public"])
    with pytest.raises(Conflict, match="far-cpu"):
        submit(_spec2(_stage(env, "a", alternates=()), _stage(env, "b", upstream="a")))


# -- 2. order, concurrency, receipts ------------------------------------------------------------


def test_placement_takes_the_first_candidate_that_passes(tmp_path, providers):
    world = _world(tmp_path, providers)
    campaign = world.campaign(_spec2(_stage(world.env, "a")))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    (placed,) = _placed(world, campaign["id"])
    assert placed["provider_id"] == NEAR and placed["candidates"] == [NEAR, FAR]
    assert placed["skipped"] == [] and placed["not_evaluated"] == [FAR]
    assert [a["provider_id"] for a in world.attempts(campaign["id"])] == [NEAR]


def test_two_independent_stages_run_at_once_on_two_providers(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    world.scenarios = [(0, {"data.json": '{"score": 1}'}), (0, {"data.json": '{"score": 2}'})]
    campaign = world.campaign(_spec2(_stage(env, "a"), _stage(env, "b")))
    # Without any worker running, both stages get a grant: one per provider's single slot.
    world.tick_until(lambda: len(_grants(world)) == 2, worker=False)
    assert {g["worker_principal_id"] for g in _grants(world)} == {
        env.worker["id"],
        world.far_worker["id"],
    }
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    by_stage = {p["stage_key"]: p for p in _placed(world, campaign["id"])}
    assert by_stage["a"]["provider_id"] == NEAR
    assert by_stage["b"]["provider_id"] == FAR
    (skip,) = by_stage["b"]["skipped"]
    assert skip["provider_id"] == NEAR and skip["reason"] == "no_free_slot"
    assert {a["stage_key"]: a["provider_id"] for a in world.attempts(campaign["id"])} == {
        "a": NEAR,
        "b": FAR,
    }
    receipt = _receipt(world, campaign["id"])
    stages = {s["stage_key"]: s for s in receipt["stages"]}
    assert stages["b"]["provider_id"] == FAR and stages["b"]["runtime"] == "docker-cpu-v1"
    entry = stages["b"]["attempts"][0]
    assert entry["provider_id"] == FAR
    assert entry["worker_principal_id"] == world.far_worker["id"]
    assert entry["placement"]["provider_id"] == FAR
    assert entry["placement"]["candidates"] == [NEAR, FAR]
    assert entry["placement"]["skipped"][0]["reason"] == "no_free_slot"
    assert entry["placement"]["budget_refusal"] is False
    assert entry["evidence_kind"] == "run_receipt" and entry["evidence_digest"]


def test_a_stage_waits_when_every_candidate_is_busy_and_never_degrades(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    campaign = world.campaign(_spec2(_stage(env, "a"), _stage(env, "b"), _stage(env, "c")))
    world.tick_until(lambda: len(_grants(world)) == 2, worker=False)
    for _ in range(4):
        world.runner.tick()
    # Both slots are taken: c has no attempt, no hold and no grant, and says why, once.
    assert world.stages(campaign["id"])["c"] == "pending"
    assert [a["stage_key"] for a in world.attempts(campaign["id"])] == ["a", "b"]
    waits = [e["payload"] for e in world.events_of(WAITING) if e["payload"]["stage_key"] == "c"]
    assert len(waits) == 1
    assert {s["reason"] for s in waits[0]["skipped"]} == {"no_free_slot"}
    view = env.store.get_campaign(env.owner, campaign["id"])["placement"]
    c_view = next(item for item in view if item["stage_key"] == "c")
    assert c_view["placements"] == [] and c_view["waiting"]["skipped"][0]["reason"] == (
        "no_free_slot"
    )
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    assert world.stages(campaign["id"]) == {"a": "succeeded", "b": "succeeded", "c": "succeeded"}


# -- 3. refusals ------------------------------------------------------------------------------


def test_an_untrusted_candidate_is_skipped_with_its_reason(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    world.scenarios = [(0, {"data.json": '{"score": 1}'}), (0, {"data.json": '{"score": 2}'})]
    campaign = world.campaign(_spec2(_stage(env, "a"), _stage(env, "b", upstream="a")))
    # After approval the operator narrows near-cpu to public data: a's public input may still
    # go there, b's derived input may not.
    _trust(env, world.far_worker, near=["public"])
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    placed = {p["stage_key"]: p for p in _placed(world, campaign["id"])}
    assert placed["b"]["provider_id"] == FAR
    (skip,) = placed["b"]["skipped"]
    assert skip["reason"] == "untrusted" and "private_derived" in skip["detail"]


def test_when_no_candidate_is_trusted_the_stage_fails_as_a_single_provider_stage_would(
    tmp_path, providers
):
    world = _world(tmp_path, providers)
    env = world.env
    campaign = world.campaign(_spec2(_stage(env, "a"), _stage(env, "b", upstream="a")))
    _trust(env, world.far_worker, near=["public"], far=["public"])
    world.tick_all(campaign["id"])
    assert world.stages(campaign["id"])["b"] == "failed"
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", campaign["id"])
    assert stop["stop_reason"] == "stage_failed:b:input_not_admitted"
    assert [a["stage_key"] for a in world.attempts(campaign["id"])] == ["a"]  # nothing sent
    assert not [p for p in _placed(world, campaign["id"]) if p["stage_key"] == "b"]


def test_an_unregistered_candidate_is_skipped_even_when_it_is_the_stages_own(tmp_path, providers):
    world = _world(tmp_path, providers, ghost=True)
    env = world.env
    campaign = world.campaign(_spec2(_stage(env, "a", provider=GHOST, alternates=(NEAR,))))
    unregister_provider(GHOST)  # the operator restarted without it
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    (placed,) = _placed(world, campaign["id"])
    assert placed["provider_id"] == NEAR
    assert placed["skipped"] == [
        {"provider_id": GHOST, "reason": "unregistered", "detail": "not in the provider registry"}
    ]
    # Only the approved candidates were ever reachable.
    assert {a["provider_id"] for a in world.attempts(campaign["id"])} == {NEAR}


def test_a_candidate_whose_estimate_does_not_fit_the_remaining_budget_is_skipped(
    tmp_path, providers
):
    world = _world(tmp_path, providers)
    env = world.env
    cheap = {"provider": FAR, "runtime": "docker-cpu-v1", "max_exposure_cents": 10}
    spec = _spec2(
        _stage(env, "a", alternates=(), exposure=30),
        _stage(env, "b", alternates=(cheap,), exposure=40),
        budget_cap_cents=60,
    )
    env.execute("UPDATE execution_project_limits SET monetary_limit_cents=1000")
    campaign = world.campaign(spec)
    world.tick_until(lambda: len(world.attempts(campaign["id"])) == 2, worker=False)
    placed = {p["stage_key"]: p for p in _placed(world, campaign["id"])}
    # a holds 30 of 60: b's own 40 does not fit while it is held, far-cpu's 10 does.
    assert placed["b"]["provider_id"] == FAR and placed["b"]["hold_cents"] == 10
    assert placed["b"]["skipped"][0]["reason"] == "budget_in_flight"
    holds = {a["stage_key"]: a["hold_cents"] for a in world.attempts(campaign["id"])}
    assert holds == {"a": 30, "b": 10}
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])


def test_a_budget_no_candidate_can_fit_is_a_recorded_budget_refusal(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    cheaper = {"provider": FAR, "runtime": "docker-cpu-v1", "max_exposure_cents": 40}
    campaign = world.campaign(
        _spec2(_stage(env, "a", alternates=(cheaper,), exposure=50), budget_cap_cents=60)
    )
    body = {"expected_revision": 2, "budget_cap_cents": 30, "reason": "tighten"}
    revision = world.row("SELECT revision FROM campaigns WHERE id=?", campaign["id"])["revision"]
    body["expected_revision"] = revision
    status, _ = env.store.reduce_campaign_budget(
        env.human(), campaign["id"], "k-reduce", canonical_digest(body), body
    )
    assert status == 200
    for _ in range(4):
        world.runner.tick()
    (attempt,) = world.attempts(campaign["id"])
    assert attempt["status"] == "refused_budget" and attempt["failure_class"] == "budget_refused"
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", campaign["id"])
    assert stop["stop_reason"] == "budget_refused:a"
    (placed,) = _placed(world, campaign["id"])
    assert placed["budget_refusal"] is True and placed["provider_id"] == NEAR
    assert [s["reason"] for s in placed["skipped"]] == ["over_campaign_budget"]
    assert not _grants(world)


# -- 4. steering and status ---------------------------------------------------------------------


def _steer(world, campaign, actor=None, key="k-steer", **fields):
    env = world.env
    revision = world.row("SELECT revision FROM campaigns WHERE id=?", campaign["id"])["revision"]
    body = {"expected_revision": revision, "reason": "use the spare provider", **fields}
    return env.store.steer_campaign_placement(
        actor or env.human(), campaign["id"], key, canonical_digest(body), body
    )


def test_steering_pins_or_excludes_only_approved_candidates(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    campaign = world.campaign(_spec2(_stage(env, "a"), _stage(env, "b", alternates=())))
    with pytest.raises(Conflict, match="approved candidates"):
        _steer(world, campaign, stage_key="a", pin="nowhere-cpu")
    with pytest.raises(Conflict, match="no candidate"):
        _steer(world, campaign, stage_key="a", exclude=[NEAR, FAR])
    with pytest.raises(Conflict, match="one provider"):
        _steer(world, campaign, stage_key="b", pin=NEAR)
    with pytest.raises(NotFound):
        _steer(world, campaign, stage_key="zz", pin=NEAR)
    status, steered = _steer(world, campaign, stage_key="a", exclude=[NEAR])
    assert status == 200 and steered["allowed"] == [FAR]
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    placed = {p["stage_key"]: p for p in _placed(world, campaign["id"])}
    assert placed["a"]["provider_id"] == FAR
    assert placed["a"]["skipped"] == [
        {"provider_id": NEAR, "reason": "excluded_by_steering", "detail": ""}
    ]
    assert placed["a"]["steering"] == {"pin": None, "exclude": [NEAR]}
    view = env.store.get_campaign(env.owner, campaign["id"])["placement"]
    assert [item["stage_key"] for item in view] == ["a"]  # single-provider stages: nothing
    assert view[0]["candidates"] == [NEAR, FAR]
    assert view[0]["steering"] == {"pin": None, "exclude": [NEAR]}
    assert view[0]["placements"][0]["provider_id"] == FAR and view[0]["waiting"] is None
    receipt = _receipt(world, campaign["id"])
    a = next(s for s in receipt["stages"] if s["stage_key"] == "a")
    assert a["attempts"][0]["placement"]["steering"] == {"pin": None, "exclude": [NEAR]}


def test_an_agent_steers_only_its_own_campaign(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    agent = env.agent(["campaign:submit", "campaign:read", "campaign:cancel"], name="mine")
    other = env.agent(["campaign:submit", "campaign:read", "campaign:cancel"], name="other")
    campaign = env.approve(env.submit(actor=agent, spec=_spec2(_stage(env, "a"))))
    from tessera_control.store import Forbidden

    with pytest.raises(Forbidden):
        _steer(world, campaign, actor=other, stage_key="a", pin=FAR)
    status, steered = _steer(world, campaign, actor=agent, stage_key="a", pin=FAR)
    assert status == 200 and steered["allowed"] == [FAR]


def test_steering_route_and_status_field_over_http(tmp_path, providers):
    from fastapi.testclient import TestClient

    from tessera_control.api import create_app

    world = _world(tmp_path, providers)
    env = world.env
    campaign = world.campaign(_spec2(_stage(env, "a")))
    human = env.human()
    token = "synthetic-human-token-" + "x" * 30
    assert human["id"]
    client = TestClient(create_app(env.state))
    headers = {"Authorization": f"Bearer {token}"}
    revision = world.row("SELECT revision FROM campaigns WHERE id=?", campaign["id"])["revision"]
    response = client.post(
        f"/campaigns/{campaign['id']}/placement-steering",
        json={"expected_revision": revision, "stage_key": "a", "pin": FAR, "reason": "spread"},
        headers={**headers, "Idempotency-Key": "k-http-steer"},
    )
    assert response.status_code == 200, response.text
    world.tick_all(campaign["id"])
    read = client.get(f"/campaigns/{campaign['id']}", headers=headers)
    assert read.status_code == 200
    (stage,) = read.json()["placement"]
    assert stage["steering"] == {"pin": FAR, "exclude": []}
    assert stage["placements"][0]["provider_id"] == FAR
    assert stage["placements"][0]["skipped"][0]["reason"] == "not_pinned"


def test_mcp_tools_expose_steering_and_the_status_field(tmp_path):
    pytest.importorskip("mcp")
    import asyncio

    import httpx

    from tessera_mcp.client import ControlClient
    from tessera_mcp.config import Config
    from tessera_mcp.server import CAMPAIGN_TOOL_SCOPES, create_server

    seen: list = []
    reference = tmp_path / "token"
    reference.write_text("synthetic-agent-token")
    reference.chmod(0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content))
        return httpx.Response(200, json={"placement": [{"stage_key": "a"}]})

    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )
    assert CAMPAIGN_TOOL_SCOPES["tessera_steer_campaign_placement"] == "campaign:cancel"
    read_only = create_server(
        client, campaign_tools=True, campaign_actions=frozenset({"campaign:read"})
    )
    names = {t.name for t in asyncio.run(read_only.list_tools())}
    assert "tessera_steer_campaign_placement" not in names and "tessera_get_campaign" in names
    server = create_server(client, campaign_tools=True)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    steer = tools["tessera_steer_campaign_placement"]
    assert steer.annotations.readOnlyHint is False and steer.annotations.destructiveHint is False
    assert "placement" in tools["tessera_get_campaign"].description
    asyncio.run(
        server.call_tool(
            "tessera_steer_campaign_placement",
            {
                "campaign_id": "campaign_1",
                "expected_revision": 3,
                "stage_key": "a",
                "exclude": [NEAR],
                "reason": "spread",
                "idempotency_key": "k-mcp",
            },
        )
    )
    method, path, content = seen[-1]
    assert (method, path) == ("POST", "/campaigns/campaign_1/placement-steering")
    assert json.loads(content) == {
        "expected_revision": 3,
        "stage_key": "a",
        "pin": None,
        "exclude": [NEAR],
        "reason": "spread",
    }
