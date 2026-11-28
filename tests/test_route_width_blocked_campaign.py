"""A blocked campaign does not hold Salad route slots with grants nobody leased (ADR 0208 addendum).

Under a recorded route width every grant-holding, non-terminal Salad attempt holds a slot for
every campaign. A blocked campaign starts nothing, so its never-leased grants are given back
through the paused-campaign accounting (ADR 0167): hold settled at zero, the attempt
``failed_transient``/``not_started``. A leased grant stays. The executor takes no lease for a
campaign that may not run it, and an ``unknown`` attempt whose route end is not confirmed keeps
its slot (fail closed)."""

from __future__ import annotations

import sqlite3

import pytest
from campaign_runner_harness import World, admit
from salad_campaign_fixture import (  # noqa: F401
    SaladWorld,
    salad_provider_reset,
    salad_spec,
    salad_stage,
)
from test_campaign_salad_executor import Harness, item
from test_paused_campaign_slot_release import RELEASED, _holder
from test_paused_campaign_slot_release import _spec as _worker_spec
from test_salad_route_width_granted_admitted import _lease_and_package, _own_snapshot

from tessera_control.campaign_provider_salad import (
    SaladRouteProvider,
    register_provider_instance,
    route_end_confirmed,
)
from tessera_control.campaign_slot_reclaim import (
    BLOCKED_RELEASE_DETAIL,
    PAUSED_RELEASE_DETAIL,
    unleased_release_reason,
)
from tessera_control.store import canonical_digest

SLOT_FREED = "campaign.slot_freed"
BLOCKED_RELEASED = "execution.blocked_unleased_grant_released"


def _stage(env, key: str) -> dict:
    snapshot_id, digest = _own_snapshot(env, key)
    stage = salad_stage(env, key, hold=10)
    stage["inputs"][0].update(snapshot_id=snapshot_id, digest=digest)
    return stage


def _world(tmp_path, width: int = 2) -> SaladWorld:
    """Campaign A with two parallel Salad stages; the project allows four grants, so only the
    route width narrows admission."""
    world = SaladWorld(tmp_path, stages=lambda env: [_stage(env, "g1"), _stage(env, "g2")])
    env = world.env
    limits = {
        "expected_revision": 1,
        "currency": "USD",
        "monetary_limit_cents": 1000,
        "max_concurrency": 4,
    }
    status, _ = env.execution.configure_project_execution(
        env.owner, env.project["id"], "wide", canonical_digest(limits), limits
    )
    assert status == 200
    register_provider_instance(world.profiles, guardian_reader=world.guardian, route_width=width)
    world.start()
    return world


def _second_campaign(world: SaladWorld) -> dict:
    spec = salad_spec(world.env, [_stage(world.env, "h1")])
    return world.env.approve(world.env.submit(spec=spec))


def _rows(world: SaladWorld, campaign_id: str) -> dict[str, dict]:
    rows = world.env.query(
        "SELECT a.*, sa.phase FROM campaign_stage_attempts a "
        "LEFT JOIN campaign_salad_attempts sa ON sa.attempt_id=a.id WHERE a.campaign_id=? "
        "ORDER BY a.stage_key, a.attempt",
        campaign_id,
    )
    return {row["stage_key"]: dict(row) for row in rows}


def _assess_all(world: SaladWorld, assessed: set[tuple[str, str]]) -> None:
    """The owner assesses each stage's study source, in every campaign, as it appears."""
    for row in world.env.query(
        "SELECT campaign_id, stage_key FROM campaign_salad_stages WHERE source_id IS NOT NULL"
    ):
        pair = (row["campaign_id"], row["stage_key"])
        if pair in assessed:
            continue
        current = world.campaign
        world.campaign = {"id": row["campaign_id"]}
        try:
            world.assess_source(row["stage_key"])
        finally:
            world.campaign = current
        assessed.add(pair)


def _tick(world: SaladWorld, count: int, assessed: set) -> None:
    for _ in range(count):
        world.tick()
        _assess_all(world, assessed)


def _block(world: SaladWorld, campaign_id: str) -> None:
    """Blocked as a late overrun leaves it: only a person moves it on (no automatic unblock)."""
    world.env.execute(
        "UPDATE campaigns SET status='blocked', stop_reason='budget_overrun: test' WHERE id=?",
        campaign_id,
    )


def _free(world: SaladWorld, width: int = 2) -> int | None:
    return SaladRouteProvider(route_width=width).free_slots(world.runner, None, None)


def _a_full_and_b_waiting(tmp_path) -> tuple[SaladWorld, dict, set]:
    world = _world(tmp_path)
    assessed: set = set()
    _tick(world, 20, assessed)
    a = _rows(world, world.campaign_id)
    assert {k: (a[k]["status"], a[k]["phase"]) for k in a} == {
        "g1": ("admitted", "awaiting_lease"),
        "g2": ("admitted", "awaiting_lease"),
    }, world.dump()
    b = _second_campaign(world)
    _tick(world, 20, assessed)
    assert _rows(world, b["id"])["h1"]["grant_id"] is None, world.dump()
    assert any(e.get("stage_key") == "h1" for e in world.events("campaign.stage_waiting_slot"))
    assert _free(world) == 0
    return world, b, assessed


# -- (1) a blocked campaign gives back its unleased grants ----------------------------------------


def test_a_blocked_campaigns_unleased_grants_free_the_width_for_another_campaign(tmp_path):
    world, b, assessed = _a_full_and_b_waiting(tmp_path)
    grants = {k: r["grant_id"] for k, r in _rows(world, world.campaign_id).items()}
    _block(world, world.campaign_id)

    world.tick()  # one pass: A (created first) gives both slots back

    assert _free(world) == 2

    a = _rows(world, world.campaign_id)
    for key in ("g1", "g2"):
        assert a[key]["status"] == "failed_transient", world.dump()
        assert a[key]["failure_class"] == "not_started"
        assert a[key]["failure_detail"] == BLOCKED_RELEASE_DETAIL
        assert a[key]["settled_cents"] == 0 and a[key]["settlement_basis"] == "unclaimed_zero"
        assert a[key]["phase"] == "unclaimed"  # never listed to the executor again
        grant = world.row("SELECT * FROM execution_grants WHERE id=?", grants[key])
        assert grant["status"] == "reconciled" and grant["settlement_cents"] == 0
    assert world.status() == "blocked"
    # B's waiting stage gave its input release back while it waited (ADR 0208); it re-takes it
    # and is granted through the unchanged admission path within a few passes.
    world.tick_until(lambda: _rows(world, b["id"])["h1"]["grant_id"] is not None, limit=4)
    freed = [e for e in world.events(SLOT_FREED) if e["campaign_id"] == world.campaign_id]
    assert sorted(e["stage_key"] for e in freed) == ["g1", "g2"]
    assert {e["reason"] for e in freed} == {"blocked_unleased_grant"}
    assert {e["trigger"] for e in freed} == {"blocked_campaign_release"}
    released = world.events(BLOCKED_RELEASED)
    assert sorted(e["grant_id"] for e in released) == sorted(grants.values())
    assert all(e["consumed_scientific_retry"] is False for e in released)
    assert not any(
        w["campaign_id"] == world.campaign_id for w in world.env.store.list_salad_work(world.worker)
    )
    # Idempotent: later passes write nothing more.
    _tick(world, 3, assessed)
    assert len([e for e in world.events(SLOT_FREED) if e["campaign_id"] == world.campaign_id]) == 2


def test_a_leased_grant_of_a_blocked_campaign_keeps_its_slot(tmp_path):
    world, b, assessed = _a_full_and_b_waiting(tmp_path)
    _lease_and_package(world, "g1")
    _block(world, world.campaign_id)
    _tick(world, 3, assessed)
    a = _rows(world, world.campaign_id)
    # g1 holds a lease: it may run, so it is left for the evidence paths and keeps its slot.
    assert a["g1"]["status"] == "admitted" and a["g1"]["phase"] == "leased", world.dump()
    grant = world.row("SELECT status FROM execution_grants WHERE id=?", a["g1"]["grant_id"])
    assert grant["status"] == "active"
    # g2 never had a lease: given back, so B takes the one slot left.
    assert a["g2"]["status"] == "failed_transient"
    world.tick_until(lambda: _rows(world, b["id"])["h1"]["grant_id"] is not None, limit=4)
    assert _free(world) == 0  # g1's slot and B's
    assert _rows(world, world.campaign_id)["g1"]["phase"] == "leased"


def test_shared_release_condition():
    assert unleased_release_reason("blocked", False) == (
        "blocked_unleased_grant",
        BLOCKED_RELEASE_DETAIL,
    )
    assert unleased_release_reason("paused_delegation_lapsed", False)[1] == PAUSED_RELEASE_DETAIL
    assert unleased_release_reason("running", True)[1] == PAUSED_RELEASE_DETAIL
    for status in ("running", "stopping", "cancel_requested", "submitted", "succeeded"):
        assert unleased_release_reason(status, False) is None
    assert unleased_release_reason("stopping", True) is None


# -- (2) the executor takes no lease for a campaign that may not run it ---------------------------


@pytest.mark.parametrize(
    ("over", "detail"),
    [
        ({"campaign_status": "blocked"}, "blocked"),
        ({"campaign_status": "paused_delegation_lapsed"}, "paused_delegation_lapsed"),
        ({"campaign_status": "cancel_requested"}, "cancel_requested"),
        ({"campaign_status": "failed"}, "failed"),
        ({"campaign_status": "running", "steering_paused": 1}, "paused"),
    ],
)
def test_the_executor_skips_leasing_for_a_campaign_that_may_not_run(tmp_path, over, detail):
    h = Harness(tmp_path)
    h.control.items = [item("awaiting_lease", route_id=None, operation_id=None, **over)]
    log = h.executor.run_once()
    assert "acquire" not in h.names() and "package" not in h.names()
    assert [(a["action"], a["detail"]) for a in log] == [("lease_held", detail)]
    # Skipped, not failed: nothing is reported against the attempt, and it stays skipped.
    assert "record_rejection" not in h.names()
    h.executor.run_once()
    assert "acquire" not in h.names()


@pytest.mark.parametrize(
    "over", [{}, {"campaign_status": "running"}, {"campaign_status": "stopping"}]
)
def test_the_executor_still_leases_for_a_running_or_stopping_campaign(tmp_path, over):
    h = Harness(tmp_path)
    h.control.items = [
        item("awaiting_lease", route_id=None, operation_id=None, steering_paused=0, **over)
    ]
    log = h.executor.run_once()
    assert ("acquire", "grant_1") in h.control.calls
    assert [a["action"] for a in log] == ["packaged"]


def test_control_lists_the_steering_pause_with_the_work(tmp_path):
    world = _world(tmp_path)
    _tick(world, 20, set())
    work = world.env.store.list_salad_work(world.worker)
    assert work and not any(w["steering_paused"] for w in work)
    project = world.row("SELECT org_id, project_id FROM campaigns WHERE id=?", world.campaign_id)
    world.env.execute(
        """INSERT INTO campaign_steering (campaign_id, org_id, project_id, paused,
        paused_by_role, paused_reason, paused_at, pause_count, updated_at)
        VALUES (?,?,?,1,'agent','test',?,1,?)""",
        world.campaign_id,
        project["org_id"],
        project["project_id"],
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:00:00+00:00",
    )
    work = world.env.store.list_salad_work(world.worker)
    assert work and all(w["steering_paused"] for w in work)


# -- (3) an unknown attempt whose route end is not confirmed keeps its slot -----------------------


def _sql(world: SaladWorld, sql: str, *args) -> None:
    connection = sqlite3.connect(world.env.state / "tessera.sqlite3", timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(sql, args)
        connection.commit()
    finally:
        connection.close()


def _unknown_ghost(world: SaladWorld, route_id: str | None) -> None:
    """Another campaign's ``unknown`` Salad attempt that holds a grant."""
    _sql(
        world,
        "INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status, "
        "hold_cents, grant_id, revision, created_at, updated_at, provider_id) "
        "VALUES ('ghost', 'ghost_campaign', 'x', 1, 'unknown', 0, 'ghost_grant', 1, "
        "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'salad')",
    )
    _sql(
        world,
        "INSERT INTO campaign_salad_attempts (attempt_id, phase, route_id, revision, updated_at) "
        "VALUES ('ghost', 'unknown', ?, 1, '2026-01-01T00:00:00Z')",
        route_id,
    )


def _route_state(world: SaladWorld, route_id: str, revision: int, **state) -> None:
    payload = {"id": route_id, "revision": revision, **state}
    with world.env.store.runner_connection() as connection:
        world.env.store._event(
            connection,
            {"id": world.human["id"], "org_id": world.env.project["org_id"]},
            world.env.project["id"],
            None,
            "remote_execution.state",
            revision,
            payload,
        )


def test_an_unknown_attempt_with_an_unconfirmed_route_keeps_its_slot(tmp_path):
    world = SaladWorld(tmp_path)
    _unknown_ghost(world, "route_ghost")
    assert _free(world, 1) == 0  # no route record at all: not confirmed
    _route_state(
        world, "route_ghost", 1, termination_confirmed=False, provider_status="running", receipts=[]
    )
    assert _free(world, 1) == 0 and _free(world, 2) == 1
    # Only a provider readback of the stopped route frees it.
    _route_state(
        world,
        "route_ghost",
        2,
        termination_confirmed=True,
        provider_status="stopped",
        receipts=[{"provenance": "guardian"}],
    )
    assert _free(world, 1) == 0
    _route_state(
        world,
        "route_ghost",
        3,
        termination_confirmed=True,
        provider_status="stopped",
        receipts=[{"provenance": "provider"}],
    )
    assert _free(world, 1) == 1


def test_an_unknown_attempt_with_no_route_frees_its_slot(tmp_path):
    world = SaladWorld(tmp_path)
    _unknown_ghost(world, None)
    assert _free(world, 1) == 1


def test_a_reallocated_route_needs_the_providers_confirmed_stop():
    state = {
        "reallocated": True,
        "termination_confirmed": True,
        "provider_status": "stopped",
        "receipts": [{"provenance": "provider"}],
    }
    # The route flag alone is not enough: ADR 0149's provider read of the stopped group is.
    assert route_end_confirmed(state) is False
    assert route_end_confirmed(None) is False
    assert route_end_confirmed({"termination_confirmed": True}) is False


# -- (4) the paused behaviour is unchanged ------------------------------------------------------


@pytest.mark.parametrize("how", ["steering", "lapsed"])
def test_paused_release_is_unchanged(tmp_path, how):
    world = World(tmp_path)
    holder, attempt = _holder(world, how)
    fresh = world.campaign(_worker_spec(world))
    admit(world, fresh["id"])
    (closed,) = world.attempts(holder)
    assert closed["status"] == "failed_transient" and closed["failure_class"] == "not_started"
    assert closed["failure_detail"] == PAUSED_RELEASE_DETAIL
    (event,) = [
        e["payload"] for e in world.events_of(SLOT_FREED) if e["payload"]["campaign_id"] == holder
    ]
    assert event["reason"] == "paused_unleased_grant"
    assert event["trigger"] == "paused_campaign_release"
    assert [e["payload"]["grant_id"] for e in world.events_of(RELEASED)] == [attempt["grant_id"]]
    assert not world.events_of(BLOCKED_RELEASED)
