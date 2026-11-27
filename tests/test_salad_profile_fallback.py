"""ADR 0151 with the strict rule of ADR 0169: a Salad stage never moves to another approved
profile after a run that may have started or spent; it is held for an explicit retry decision.

Fakes only: the executor, the provider readbacks and the human are test doubles from the Salad
campaign fixture (no provider, no network, no money). A route "ran unstable" by appending the
route state the control plane records for a reallocation; nothing here proves provider behavior.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from salad_campaign_fixture import (  # noqa: F401
    GPU,
    SaladWorld,
    profile_data,
    salad_provider_reset,
    salad_stage,
)
from test_salad_termination_confirm import _read, _record_outcome, _route_event, reallocated

from tessera_control import campaign_preapproval
from tessera_control import campaign_salad_fallback as fallback
from tessera_control.campaign_provider_salad import register_provider_instance
from tessera_control.campaign_receipts import build_receipt
from tessera_control.salad_campaign_profiles import SaladCampaignProfiles

GPU2 = "550e8400-e29b-41d4-a716-446655440002"
GPU3 = "550e8400-e29b-41d4-a716-446655440003"
GPU9 = "550e8400-e29b-41d4-a716-446655440009"
ALTERNATES = ["gpu-alt1", "gpu-alt2"]


def _profiles(env, **alt_over):
    """The stage's own profile, two alternates on other GPU classes, and one the spec never
    lists (``gpu-other``): it must never be used."""
    return SaladCampaignProfiles.model_validate(
        dict(
            schema="tessera.salad.campaign-profiles.v1",
            profiles=[
                profile_data(env),
                profile_data(env, name="gpu-alt1", gpu_class=GPU2, **alt_over.get("gpu-alt1", {})),
                profile_data(env, name="gpu-alt2", gpu_class=GPU3, **alt_over.get("gpu-alt2", {})),
                profile_data(env, name="gpu-other", gpu_class=GPU9),
            ],
        )
    )


def _stage(env, *, alternates=ALTERNATES, attempts=1):
    stage = salad_stage(env, attempts=attempts)
    if alternates:
        stage["salad"] = {**stage["salad"], "alternate_profiles": list(alternates)}
        # Pre-start moves only (ADR 0169): one per listed alternate.
        stage["retry"] = {**stage["retry"], "max_class_fallbacks": len(alternates)}
    return stage


def _world(tmp_path, *, alternates=ALTERNATES, attempts=1, **alt_over) -> SaladWorld:
    world = SaladWorld(
        tmp_path, stages=lambda env: [_stage(env, alternates=alternates, attempts=attempts)]
    )
    world.profiles = _profiles(world.env, **alt_over)
    register_provider_instance(world.profiles, guardian_reader=world.guardian)
    return world


def _ran_unstable(world: SaladWorld, *, within: bool) -> None:
    """Executor double: the route started, its allocation changed (``within`` the 900 s window
    of first running, or only as a reallocation with no readable start), it was stopped and the
    provider read the exact group stopped after the stop."""
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))
    world.claim("create")
    world.claim("start")
    state = world.route_state()
    now = datetime.now(UTC)
    state.update(reallocated([]), receipts=[])
    stop = dict(id=uuid.uuid4().hex, action="stop", requested_at=now.isoformat())
    effects, receipts = [], []
    if within:
        start = dict(
            id=uuid.uuid4().hex,
            action="start",
            requested_at=(now - timedelta(seconds=40)).isoformat(),
        )
        effects.append(start)
        for seconds, allocation in ((30, "alloc-a"), (20, "alloc-b")):
            read = _read(start, 0, stopped=False, allocation=allocation)
            read.update(
                observed_at=(now - timedelta(seconds=seconds)).isoformat(),
                state="running" if allocation == "alloc-a" else "unknown",
            )
            receipts.append(read)
    effects.append(stop)
    observe = dict(
        id=uuid.uuid4().hex,
        action="observe",
        requested_at=(now + timedelta(seconds=1)).isoformat(),
    )
    effects.append(observe)
    read = _read(observe, 0, stopped=True)
    read["observed_at"] = (now + timedelta(seconds=1, milliseconds=1)).isoformat()
    receipts.append(read)
    state.update(effects=effects, receipts=receipts)
    state["revision"] += 1
    _route_event(world, state)
    world.observe_exit()  # what the executor reports once the provider read confirmed
    _record_outcome(world)


def _first_attempt_started(world: SaladWorld) -> None:
    world.to_route_started()


def _next_attempt_started(world: SaladWorld, number: int) -> None:
    """Settle the ended attempt with its charge, then drive attempt ``number`` to route start."""
    world.tick_until_phase("awaiting_charge")
    world.record_charge(5)
    world.tick_until(
        lambda: (world.attempt() or {})["attempt"] == number and world.phase() == "awaiting_lease"
    )
    world.lease_and_package()
    world.tick_until_phase("awaiting_review")
    world.resolve_decisions()
    world.tick_until_phase("route_started")


def _stage_profile(world: SaladWorld) -> str:
    return world.row(
        "SELECT profile FROM campaign_salad_stages WHERE campaign_id=?", world.campaign_id
    )["profile"]


def _route_class(world: SaladWorld) -> str:
    return world.route_state()["proposal"]["selection"]["resources"]["gpu_class"]


def _receipt(world: SaladWorld) -> dict:
    campaign = world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id)
    with world.env.store.runner_connection() as connection:
        receipt, _ = build_receipt(connection, campaign, "running")
    return receipt


def _held(world: SaladWorld) -> int:
    return world.row("SELECT held_cents FROM campaigns WHERE id=?", world.campaign_id)["held_cents"]


# -- after a possible start: never a switch, an explicit retry decision ----------


def _ended_and_charged(world: SaladWorld, cents: int = 5) -> None:
    world.tick_until_phase("awaiting_charge")
    world.record_charge(cents)
    world.tick_until(lambda: world.status() == "blocked")


@pytest.mark.parametrize(
    ("within", "reason"),
    [
        (False, fallback.TRIGGER_TERMINATION_CONFIRMED),
        (True, fallback.TRIGGER_ALLOCATION_UNSTABLE),
    ],
)
def test_an_unstable_run_never_moves_to_another_profile(tmp_path, within, reason):
    world = _world(tmp_path)
    _first_attempt_started(world)
    assert _stage_profile(world) == "gpu-main" and _route_class(world) == GPU
    _ran_unstable(world, within=within)
    _ended_and_charged(world)

    # No switch, no new attempt, no hold: the stage is held with ONE stop event.
    assert world.events(fallback.SWITCH_EVENT) == []
    [stop] = world.events(fallback.STOP_EVENT)
    assert stop["outcome"] == fallback.POST_START_REFUSED and stop["attempt"] == 1
    assert stop["reason"] == reason and stop["to_profile"] is None
    assert stop["started"] is True and stop["spent"] is True
    assert stop["from_profile"] == "gpu-main" and stop["tried"] == ["gpu-main"]
    assert stop["attempt_id"] == world.attempt()["id"]
    stage = world.row("SELECT status FROM campaign_stages WHERE campaign_id=?", world.campaign_id)
    assert stage["status"] == "blocked_unknown"
    # The attempt keeps its own outcome and settles on its recorded charge (as any failed run).
    assert world.attempt()["status"] == "failed_permanent"
    assert world.attempt()["attempt"] == 1 and world.attempt()["settled_cents"] == 5
    assert _held(world) == 0 and _stage_profile(world) == "gpu-main"
    assert world.events("campaign.blocked")[-1]["reason"] == fallback.POST_START_REFUSED
    world.tick(3)  # replay: nothing more is recorded or started
    assert len(world.events(fallback.STOP_EVENT)) == 1 and world.attempt()["attempt"] == 1
    assert world.events(fallback.SWITCH_EVENT) == []

    receipt = _receipt(world)
    entry = receipt["stages"][0]
    assert entry["salad_profiles"]["switches"] == []
    stopped = entry["salad_profiles"]["stopped"]
    assert stopped["outcome"] == "post_start_switch_refused" and stopped["started"] is True
    assert [a["salad_profile"] for a in entry["attempts"]] == ["gpu-main"]


def test_the_refused_move_is_an_explicit_decision_in_the_stuck_view(tmp_path):
    from tessera_control import campaign_stuck

    world = _world(tmp_path)
    _first_attempt_started(world)
    _ran_unstable(world, within=False)
    _ended_and_charged(world)
    with world.env.store.runner_connection() as connection:
        findings = campaign_stuck.detect(
            connection,
            world.env.project["org_id"],
            world.env.project["id"],
            campaign_stuck.get_settings(
                connection, world.env.project["org_id"], world.env.project["id"]
            ),
            datetime.now(UTC),
        )
    [finding] = [
        f for f in findings if f["code"] == "attempt_outcome_unknown" and f["stage_key"] == "g1"
    ]
    decision = finding["evidence"]["salad_profile_decision"]
    assert decision == {"outcome": "post_start_switch_refused", "attempt": 1, "started": True}
    assert finding["evidence"]["stage_status"] == "blocked_unknown"


def test_an_unlisted_profile_is_unreachable():
    from tessera_control.campaign_models import NormalStage  # noqa: F401  (shape reference)

    class Stage:
        key = "g1"
        provider_config = {"profile": "gpu-main", "alternate_profiles": ["gpu-alt1"]}

    assert fallback.order(Stage()) == ("gpu-main", "gpu-alt1")
    with pytest.raises(KeyError):
        fallback.on_profile(Stage(), "gpu-other")


# -- what never switches ----------


def test_a_single_profile_stage_keeps_the_existing_behavior(tmp_path):
    world = _world(tmp_path, alternates=None)
    _first_attempt_started(world)
    assert "alternate_profiles" not in json.dumps(
        json.loads(
            world.row("SELECT spec_json FROM campaigns WHERE id=?", world.campaign_id)["spec_json"]
        )
    )
    _ran_unstable(world, within=True)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(5)
    world.tick_until(lambda: world.status() == "failed")
    assert world.attempt()["attempt"] == 1
    # The provider's own confirmed move is infrastructure; no infra retry is enabled here.
    assert world.attempt()["failure_class"] == "infra_salad_reallocated"
    assert world.events(fallback.SWITCH_EVENT) == [] and world.events(fallback.STOP_EVENT) == []
    receipt = json.loads(world.row("SELECT receipt_json FROM campaign_receipts")["receipt_json"])
    stage = receipt["stages"][0]
    assert "salad_profiles" not in stage
    assert all("salad_profile" not in attempt for attempt in stage["attempts"])


def test_no_switch_while_custody_has_begun_on_the_current_attempt(tmp_path):
    world = _world(tmp_path)
    _first_attempt_started(world)
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))
    state = world.route_state()
    state.update(reallocated([dict(stopped=False, allocation="alloc-b")]))
    state["revision"] += 1
    _route_event(world, state)  # reallocated while executing, no outcome yet
    world.tick(4)
    assert world.events(fallback.SWITCH_EVENT) == []
    assert _stage_profile(world) == "gpu-main" and world.attempt()["attempt"] == 1
    assert world.phase() in ("route_started", "executing")


def test_a_failed_run_on_a_stable_route_does_not_switch(tmp_path):
    world = _world(tmp_path)
    _first_attempt_started(world)
    world.run_to_outcome(passed=False)
    world.tick_until_phase("awaiting_charge")
    world.record_charge(5)
    world.tick_until(lambda: world.status() == "failed")
    assert world.events(fallback.SWITCH_EVENT) == [] and world.attempt()["attempt"] == 1
    section = json.loads(world.row("SELECT receipt_json FROM campaign_receipts")["receipt_json"])[
        "stages"
    ][0]["salad_profiles"]
    assert section["switches"] == [] and section["stopped"] is None


# -- coverage of every listed profile ----------


def test_an_alternate_over_the_run_cap_is_refused_at_submit(tmp_path):
    world = _world(tmp_path, **{"gpu-alt1": {"max_run_cents": 10, "max_task_cents": 10}})
    with pytest.raises(Exception) as error:
        world.start()
    assert "gpu-alt1" in str(error.value)


def test_a_preapproval_must_bind_the_alternate(monkeypatch):
    """Fakes: a stage that resolves its reviews under a pre-approval falls back without a person
    only when the recorded entry names the placed candidate and binds the profile's plan digest."""

    class Spec:
        @staticmethod
        def stage(key):
            return type("S", (), {"provider": "salad"})()

    class Ctx:
        spec = Spec()

    alternate = type(
        "A",
        (),
        {"provider": "salad", "provider_config": {"profile": "gpu-alt1"}, "key": "g1"},
    )()
    monkeypatch.setattr(campaign_preapproval, "covered_provider", lambda c, p, own: "salad")
    monkeypatch.setattr(campaign_preapproval, "recorded_profiles", lambda c, p: None)
    assert fallback._preapproval_covers(None, Ctx(), "g1", {}, alternate) is False
    monkeypatch.setattr(
        campaign_preapproval,
        "recorded_profiles",
        lambda c, p: [{"profile": "gpu-main", "plan_digest": "a" * 64}],
    )
    assert fallback._preapproval_covers(None, Ctx(), "g1", {}, alternate) is False
    monkeypatch.setattr(
        campaign_preapproval,
        "recorded_profiles",
        lambda c, p: [
            {"profile": "gpu-main", "plan_digest": "a" * 64},
            {"profile": "gpu-alt1", "plan_digest": "b" * 64},
        ],
    )
    assert fallback._preapproval_covers(None, Ctx(), "g1", {}, alternate) is True
    monkeypatch.setattr(campaign_preapproval, "covered_provider", lambda c, p, own: "salad-cpu")
    assert fallback._preapproval_covers(None, Ctx(), "g1", {}, alternate) is False
