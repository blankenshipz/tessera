"""One candidate list for placement and infrastructure reroute (ADR 0060 section 5a, ADR 0058).

``retry.reroute_providers`` targets are placement candidates with ADR 0058's limits: used only
for an infrastructure retry (or to stay where one moved the stage), unbilled worker-grant
providers of the stage's runtime, placed by the same checks and recorded the same way. Also the
A -> B -> A case: a stage returning to a provider prepares it again. Fakes only.
"""

from __future__ import annotations

import json

import pytest
from campaign_runner_harness import dump
from test_campaign_models_v2 import _spec2
from test_campaign_placement import FAR, GHOST, NEAR, _placed, _stage, _world

from tessera_control import campaign_provider_worker as worker
from tessera_control.campaign_providers import register_provider, unregister_provider
from tessera_control.store import Conflict, canonical_digest

OK = '{"score": 1}'
KILLED = 137


class _Near(worker.WorkerGrantProvider):
    slots = 1
    prepare_result = "ready"
    prepares = 0

    def free_slots(self, rt, ctx, stage):
        return type(self).slots

    def prepare(self, rt, ctx, key):
        type(self).prepares += 1
        return type(self).prepare_result


class _Far(worker.WorkerGrantProvider):
    slots = 1

    def free_slots(self, rt, ctx, stage):
        return type(self).slots


@pytest.fixture
def providers():
    made: list[str] = []
    _Near.slots, _Near.prepare_result, _Near.prepares, _Far.slots = 1, "ready", 0, 1

    def make(provider_id: str, principal: str, slots: int = 1) -> None:
        cls = _Near if provider_id == NEAR else _Far
        register_provider(cls(worker._descriptor(provider_id, principal, None, slots)))
        made.append(provider_id)

    yield make
    for provider_id in made:
        unregister_provider(provider_id)


def _reroute_stage(env):
    return _stage(
        env,
        "a",
        alternates=(),
        retry={"max_attempts": 1, "infra_retries": 1, "reroute_providers": [FAR]},
    )


def _granted(world, cid, number):
    rows = [a for a in world.attempts(cid) if a["attempt"] == number]
    return bool(rows) and rows[0]["grant_id"] is not None


def test_an_infrastructure_retry_is_placed_on_the_reroute_target_and_recorded(tmp_path, providers):
    world = _world(tmp_path, providers)
    world.scenarios = [(KILLED, {"data.json": OK}), (0, {"data.json": OK})]
    campaign = world.campaign(_spec2(_reroute_stage(world.env)))
    cid = campaign["id"]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    first, second = world.attempts(cid)
    assert (first["provider_id"], first["failure_class"]) == (NEAR, "infra_container_killed")
    assert (second["provider_id"], second["status"]) == (FAR, "succeeded")
    placed = {p["attempt"]: p for p in _placed(world, cid)}
    # First attempt: the reroute target is a candidate but not for a first attempt.
    # First attempt: no alternates and no reroute due, so the single-provider path (ADR 0058).
    assert 1 not in placed
    # The infrastructure retry: untried candidates first, so the reroute target, via placement.
    assert placed[2]["provider_id"] == FAR and placed[2]["candidates"] == [FAR, NEAR]
    assert placed[2]["skipped"] == []
    (retry,) = world.events_of("campaign.stage_infra_retry")
    assert retry["payload"]["provider"] == FAR and retry["payload"]["rerouted"] is True
    receipt = json.loads(world.row("SELECT receipt_json FROM campaign_receipts")["receipt_json"])
    entries = {a["attempt"]: a for s in receipt["stages"] for a in s["attempts"]}
    assert entries[2]["placement"]["provider_id"] == FAR
    assert entries[2]["worker_principal_id"] == world.far_worker["id"]
    assert entries[1]["infrastructure_retry"]["reason"]


def test_a_first_attempt_keeps_the_single_provider_path_and_never_reroutes(tmp_path, providers):
    """With reroute targets but no alternates, a first attempt behaves exactly as before ADR
    0060: reserved on the stage's own provider, waiting there for a slot at grant time."""
    world = _world(tmp_path, providers)
    _Near.slots = 0  # the stage's own provider is busy
    campaign = world.campaign(_spec2(_reroute_stage(world.env)))
    cid = campaign["id"]
    world.tick_until(lambda: bool(world.events_of("campaign.stage_waiting_slot")), worker=False)
    for _ in range(3):
        world.runner.tick()
    (attempt,) = world.attempts(cid)
    assert attempt["provider_id"] == NEAR and attempt["grant_id"] is None
    # The free reroute target is not a way around the busy provider, and nothing was placed.
    assert not _placed(world, cid) and not world.events_of("campaign.stage_waiting_placement")
    assert not world.env.query(
        "SELECT 1 FROM execution_grants WHERE worker_principal_id=?", world.far_worker["id"]
    )
    view = world.env.store.get_campaign(world.env.owner, cid)["placement"]
    assert view[0]["candidates"] == [NEAR, FAR] and view[0]["infra_retry_only"] == [FAR]
    _Near.slots = 1
    world.tick_until(lambda: world.attempts(cid)[0]["grant_id"] is not None, worker=False)


def test_candidate_order_keeps_reroute_targets_for_infrastructure_retries():
    from tessera_control.campaign_models import normalize, parse_spec_json
    from tessera_control.campaign_placement import candidate_order, reroute_due

    stage = {
        "key": "a",
        "provider": NEAR,
        "runtime": "docker-cpu-v1",
        "image": "python@sha256:" + "a" * 64,
        "argv": ["python", "-c", "print(1)"],
        "outputs": [{"name": "result", "path": "data.json", "max_bytes": 1024}],
        "max_duration_seconds": 60,
        "inputs": [
            {
                "kind": "snapshot",
                "name": "code",
                "snapshot_id": "snap",
                "digest": "b" * 64,
                "data_class": "public",
            }
        ],
        "alternate_providers": [{"provider": FAR, "runtime": "docker-cpu-v1"}],
        "retry": {"max_attempts": 2, "infra_retries": 1, "reroute_providers": ["spare-cpu"]},
    }
    base = normalize(parse_spec_json(json.dumps(_spec2(stage)))).stage("a")
    assert base.candidate_ids == (NEAR, FAR, "spare-cpu")

    def attempt(provider, failure):
        return {"provider_id": provider, "failure_class": failure}

    order, excluded = candidate_order(base, [])
    assert order == (NEAR, FAR, "spare-cpu") and excluded["spare-cpu"].reason == "infra_retry_only"
    # A task failure on far-cpu: stay there first; the reroute target is still not due.
    tried = [attempt(FAR, "t2_declared_exit_code")]
    assert not reroute_due(base, tried)
    assert candidate_order(base, tried)[0] == (FAR, NEAR, "spare-cpu")
    # An infrastructure failure on near-cpu: untried first, the reroute target now allowed.
    killed = [attempt(NEAR, "infra_container_killed")]
    assert reroute_due(base, killed)
    assert candidate_order(base, killed) == ((FAR, "spare-cpu", NEAR), {})


def test_a_reroute_target_that_stopped_qualifying_is_skipped_with_adr_0058s_reason(
    tmp_path, providers
):
    world = _world(tmp_path, providers)
    world.scenarios = [(KILLED, {"data.json": OK}), (0, {"data.json": OK})]
    campaign = world.campaign(_spec2(_reroute_stage(world.env)))
    cid = campaign["id"]
    world.tick_until(lambda: _granted(world, cid, 1), worker=False)
    # The far worker's credential is revoked after approval: check_reroute refuses it now.
    world.env.execute(
        "UPDATE credentials SET revoked_at='2000-01-01T00:00:00Z' WHERE principal_id=?",
        world.far_worker["id"],
    )
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    placed = {p["attempt"]: p for p in _placed(world, cid)}
    assert placed[2]["provider_id"] == NEAR  # back to the stage's own provider, as ADR 0058
    (skip,) = placed[2]["skipped"]
    assert skip["provider_id"] == FAR and skip["reason"] == "reroute_refused"


def test_steering_cannot_pin_a_reroute_target_or_leave_only_one(tmp_path, providers):
    world = _world(tmp_path, providers)
    env = world.env
    campaign = world.campaign(_spec2(_reroute_stage(env)))

    def steer(**fields):
        revision = world.row("SELECT revision FROM campaigns WHERE id=?", campaign["id"])
        body = {"expected_revision": revision["revision"], "stage_key": "a", "reason": "r"}
        body.update(fields)
        return env.store.steer_campaign_placement(
            env.human(),
            campaign["id"],
            "k-" + canonical_digest(body)[:10],
            canonical_digest(body),
            body,
        )

    with pytest.raises(Conflict, match="reroute target"):
        steer(pin=FAR)
    with pytest.raises(Conflict, match="no candidate"):
        steer(exclude=[NEAR])
    status, steered = steer(exclude=[FAR])  # turning the reroute off is allowed
    assert status == 200 and steered["allowed"] == [NEAR]


def test_a_stage_returning_to_a_provider_prepares_it_again(tmp_path, providers):
    """A -> B -> A: an earlier attempt on A is not taken as proof A is still prepared."""
    world = _world(tmp_path, providers)
    env = world.env
    world.scenarios = [
        (3, {"data.json": OK}),
        (3, {"data.json": OK}),
        (0, {"data.json": OK}),
    ]
    stage = _stage(env, "a", retry={"max_attempts": 3, "transient_exit_codes": [3]})
    campaign = world.campaign(_spec2(stage))
    cid = campaign["id"]
    # Attempt 1 on near-cpu; once it holds its grant, near-cpu is busy so attempt 2 moves.
    world.tick_until(lambda: _granted(world, cid, 1), worker=False)
    _Near.slots = 0
    world.tick_until(lambda: _granted(world, cid, 2))
    assert world.attempts(cid)[1]["provider_id"] == FAR
    # far-cpu is busy now and near-cpu free, but near-cpu's preparation no longer stands.
    _Far.slots, _Near.slots, _Near.prepare_result = 0, 1, "waiting"
    before = _Near.prepares
    world.tick_until(lambda: world.attempts(cid)[1]["status"] == "failed_transient")
    for _ in range(4):
        world.runner.tick()
        world.poller.run(max_cycles=1)
    placed = {p["attempt"]: p for p in _placed(world, cid)}
    assert placed[3]["provider_id"] == NEAR
    assert [a["attempt"] for a in world.attempts(cid)] == [1, 2], dump(world, cid)
    assert _Near.prepares > before  # asked again, not assumed
    held = world.row("SELECT held_cents FROM campaigns WHERE id=?", cid)["held_cents"]
    assert held == 0
    _Near.prepare_result = "ready"
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert [a["provider_id"] for a in world.attempts(cid)] == [NEAR, FAR, NEAR]


@pytest.fixture
def ghost_cleanup():
    yield
    unregister_provider(GHOST)  # the shared helper registers it without a teardown


def _both_lists(env):
    return _stage(
        env,
        "a",
        retry={"max_attempts": 1, "infra_retries": 1, "reroute_providers": [GHOST]},
    )


def test_a_stage_may_name_alternates_and_a_reroute_list(tmp_path, providers, ghost_cleanup):
    world = _world(tmp_path, providers, ghost=True)
    env = world.env
    body = {"spec": _spec2(_both_lists(env))}
    digest = canonical_digest(body)
    status, result = env.store.submit_campaign(env.owner, env.project["id"], "k-both", digest, body)
    assert status == 201 and result["id"]  # one candidate list (ADR 0060 section 5a)
    # Either one alone is still admitted.
    for key, stage in (("k-alt", _stage(env, "b")), ("k-rer", _reroute_stage(env))):
        alone = {"spec": _spec2(stage)}
        status, _ = env.store.submit_campaign(
            env.owner, env.project["id"], key, canonical_digest(alone), alone
        )
        assert status == 201


def test_alternates_and_a_reroute_target_share_one_candidate_list_end_to_end(
    tmp_path, providers, ghost_cleanup
):
    world = _world(tmp_path, providers, ghost=True)
    world.scenarios = [(KILLED, {"data.json": OK}), (0, {"data.json": OK})]
    campaign = world.campaign(_spec2(_both_lists(world.env)))
    cid = campaign["id"]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    first, second = world.attempts(cid)
    assert (first["provider_id"], first["failure_class"]) == (NEAR, "infra_container_killed")
    # The retry is placed by the same mechanism: the untried alternate comes before the
    # reroute target, which is only ever an infrastructure retry's candidate.
    assert (second["provider_id"], second["status"]) == (FAR, "succeeded")
    placed = {p["attempt"]: p for p in _placed(world, cid)}
    assert placed[1]["provider_id"] == NEAR  # a first attempt: the reroute target is not eligible
    assert placed[2]["provider_id"] == FAR and placed[2]["candidates"][0] == FAR
    assert GHOST in placed[2]["candidates"]
    assert GHOST not in {a["provider_id"] for a in world.attempts(cid)}


def test_the_reroute_target_is_used_once_the_alternates_are_exhausted(
    tmp_path, providers, ghost_cleanup
):
    world = _world(tmp_path, providers, ghost=True)
    world.scenarios = [(KILLED, {"data.json": OK}), (KILLED, {"data.json": OK})]
    stage = _stage(
        world.env,
        "a",
        retry={"max_attempts": 1, "infra_retries": 2, "reroute_providers": [GHOST]},
    )
    cid = world.campaign(_spec2(stage))["id"]
    # Own provider, then the alternate, each killed by the infrastructure; the third attempt has
    # no untried approved alternate left, so the reroute target is placed.
    world.tick_until(lambda: len(world.attempts(cid)) == 3, limit=400)
    assert [a["provider_id"] for a in world.attempts(cid)] == [NEAR, FAR, GHOST]
    placed = {p["attempt"]: p for p in _placed(world, cid)}
    assert placed[3]["provider_id"] == GHOST
    assert placed[3]["candidates"][0] == GHOST  # untried first
