"""Route width counts attempts after ``issue_grant`` and ``admit_attempt`` (ADR 0208).

``issue_grant`` moves an attempt to ``granted`` and ``admit_attempt`` to ``admitted``; it keeps
that status through every pre-route phase. The width must count those attempts, or every
attempt waiting for its route looks like a free slot and a wide campaign holds more grants
than the executor can route. Each stage here has its own snapshot, so three stages run in
parallel without revoking one another's release."""

from __future__ import annotations

import sqlite3

import pytest
from campaign_fixture import declare_snapshot_public
from salad_campaign_fixture import (  # noqa: F401
    SaladWorld,
    salad_provider_reset,
    salad_stage,
)

from tessera_control.campaign_provider_salad import (
    ROUTE_SLOT_PHASES,
    SaladRouteProvider,
    register_provider_instance,
)
from tessera_control.campaign_runner import ACTIVE_ATTEMPT, TERMINAL_ATTEMPT
from tessera_control.execution import ExecutionStore
from tessera_control.runtime_packages import RuntimePackageStore
from tessera_control.snapshots import SnapshotStore
from tessera_control.store import canonical_digest
from tessera_control.workspace import capture_workspace, inventory_digest

KEYS = ("g1", "g2", "g3")


def _own_snapshot(env, key: str) -> tuple[str, str]:
    root = env.state.parent / f"workspace-{key}"
    root.mkdir()
    (root / "input.txt").write_bytes(f"input for {key}".encode())
    capture = capture_workspace(root.absolute())
    digest = inventory_digest(capture.inventory)
    _, snapshot = SnapshotStore(env.state).adopt_snapshot(
        env.owner, env.project["id"], f"snapshot-{key}", digest, capture
    )
    declare_snapshot_public(env.state, snapshot["id"])
    return snapshot["id"], snapshot["digest"]


def _stages(env) -> list[dict]:
    stages = []
    for key in KEYS:
        snapshot_id, digest = _own_snapshot(env, key)
        stage = salad_stage(env, key, hold=10)
        stage["inputs"][0].update(snapshot_id=snapshot_id, digest=digest)
        stages.append(stage)
    return stages


def _world(tmp_path, width: int | None) -> SaladWorld:
    """Three parallel Salad stages; the project allows four grants, so only the route width
    (when set) narrows admission."""
    world = SaladWorld(tmp_path, stages=_stages)
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


def _attempts(world: SaladWorld) -> dict[str, dict]:
    rows = world.env.query(
        "SELECT a.stage_key, a.status, a.grant_id, sa.phase FROM campaign_stage_attempts a "
        "LEFT JOIN campaign_salad_attempts sa ON sa.attempt_id=a.id WHERE a.campaign_id=? "
        "ORDER BY a.stage_key, a.attempt",
        world.campaign_id,
    )
    return {row["stage_key"]: dict(row) for row in rows}


def _granted(world: SaladWorld) -> set[str]:
    return {key for key, row in _attempts(world).items() if row["grant_id"] is not None}


def _tick(world: SaladWorld, count: int, assessed: set[str]) -> None:
    """Run the runner; the owner assesses each stage's study source as it appears."""
    for _ in range(count):
        world.tick()
        for key in KEYS:
            found = world.row(
                "SELECT 1 FROM campaign_salad_stages WHERE campaign_id=? AND stage_key=? "
                "AND source_id IS NOT NULL",
                world.campaign_id,
                key,
            )
            if found is not None and key not in assessed:
                world.assess_source(key)
                assessed.add(key)


def _lease_and_package(world: SaladWorld, key: str) -> None:
    """The executor leases and packages ``key``'s grant (the fixture's double, for one stage)."""
    attempt_id = world.salad(key)["attempt_id"]
    (item,) = [
        w
        for w in world.env.store.list_salad_work(world.worker)
        if w["attempt_id"] == attempt_id and w["phase"] == "awaiting_lease"
    ]
    _, lease = ExecutionStore(world.env.state).acquire_execution_lease(
        world.worker, item["grant_id"], world.key("lease"), "lease"
    )
    body = {
        "expected_epoch": lease["epoch"],
        "admitted_attempt_id": item["admitted_attempt_id"],
        "execution_adapter": "kubernetes-job",
    }
    _, response = RuntimePackageStore(world.env.state).execution_package(
        world.worker,
        lease["id"],
        world.key("package"),
        canonical_digest(body),
        lease["epoch"],
        admitted_attempt_id=item["admitted_attempt_id"],
        execution_adapter="kubernetes-job",
    )
    world.env.store.record_salad_package(
        world.worker,
        item["attempt_id"],
        world.key("record-package"),
        dict(
            operation_id=response["package"]["operation_id"],
            lease_id=lease["id"],
            epoch=lease["epoch"],
            package_digest=response["package_digest"],
        ),
    )
    world.lease = lease


def test_third_stage_waits_while_two_granted_or_admitted_attempts_hold_the_width(tmp_path):
    world = _world(tmp_path, 2)
    assessed: set[str] = set()
    _tick(world, 20, assessed)
    rows = _attempts(world)
    # The runner's real path: issue_grant then admit_attempt for two stages ...
    assert _granted(world) == {"g1", "g2"}, world.dump()
    assert {rows[k]["status"] for k in ("g1", "g2")} == {"admitted"}
    assert {rows[k]["phase"] for k in ("g1", "g2")} == {"awaiting_lease"}
    # ... and the third holds no grant: no lease clock, no lease-window burn.
    assert rows["g3"]["status"] == "reserved" and rows["g3"]["grant_id"] is None
    assert [e["stage_key"] for e in world.events("campaign.stage_waiting_slot")] == ["g3"]
    assert world.row("SELECT count(*) AS n FROM execution_grants")["n"] == 2

    # g1 leases and starts its route; it holds the slot the whole way.
    _lease_and_package(world, "g1")
    _tick(world, 4, assessed)
    world.resolve_decisions()
    world.tick_until(lambda: world.phase("g1") == "route_started")
    assert _attempts(world)["g1"]["status"] == "running"
    _tick(world, 4, assessed)
    assert _granted(world) == {"g1", "g2"}

    # g1's route ends and its attempt settles (terminal): the waiter takes the slot.
    world.run_to_outcome()
    world.tick_until(lambda: world.phase("g1") == "awaiting_charge")
    world.record_charge(5, "g1")
    world.tick_until(lambda: _attempts(world)["g1"]["status"] == "succeeded")
    world.tick_until(lambda: "g3" in _granted(world))
    assert SaladRouteProvider(route_width=2).free_slots(world.runner, None, None) == 0


def test_no_recorded_width_grants_all_three(tmp_path):
    world = _world(tmp_path, None)
    _tick(world, 20, set())
    assert _granted(world) == set(KEYS), world.dump()
    assert not world.events("campaign.stage_waiting_slot")


def _sql(world: SaladWorld, sql: str, *args) -> None:
    connection = sqlite3.connect(world.env.state / "tessera.sqlite3", timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(sql, args)
        connection.commit()
    finally:
        connection.close()


def _ghost(world: SaladWorld, status: str, phase: str | None) -> None:
    """Another campaign's grant-holding attempt (no Salad row when ``phase`` is None)."""
    _sql(
        world,
        "INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status, "
        "hold_cents, grant_id, revision, created_at, updated_at, provider_id) "
        "VALUES ('ghost', 'ghost_campaign', 'x', 1, ?, 0, 'ghost_grant', 1, "
        "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'salad')",
        status,
    )
    if phase is not None:
        _sql(
            world,
            "INSERT INTO campaign_salad_attempts (attempt_id, phase, revision, updated_at) "
            "VALUES ('ghost', ?, 1, '2026-01-01T00:00:00Z')",
            phase,
        )


@pytest.mark.parametrize("status", sorted(ACTIVE_ATTEMPT))
@pytest.mark.parametrize("phase", [None, *ROUTE_SLOT_PHASES])
def test_every_non_terminal_status_in_a_slot_phase_holds_the_slot(tmp_path, status, phase):
    world = SaladWorld(tmp_path)
    _ghost(world, status, phase)
    assert SaladRouteProvider(route_width=1).free_slots(world.runner, None, None) == 0
    assert SaladRouteProvider(route_width=2).free_slots(world.runner, None, None) == 1


@pytest.mark.parametrize("status", sorted(TERMINAL_ATTEMPT))
def test_a_terminal_attempt_frees_its_slot_even_in_a_slot_phase(tmp_path, status):
    world = SaladWorld(tmp_path)
    _ghost(world, status, "awaiting_lease")
    assert SaladRouteProvider(route_width=1).free_slots(world.runner, None, None) == 1


def test_unset_width_stays_unbounded_with_granted_attempts(tmp_path):
    world = SaladWorld(tmp_path)
    _ghost(world, "granted", "awaiting_lease")
    assert SaladRouteProvider().free_slots(world.runner, None, None) is None
    assert SaladRouteProvider().slot_free(world.runner, None, None) is True
