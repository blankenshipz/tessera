"""Unblocking after a resolved blocker does not manufacture unknowns (q-2705 F5).

Field shape (9:11 PM ET Nov 26): a campaign blocked longer than a route window by one unknown
attempt; the blocker is then recovered at 0 (auto-reconciled as not started), the runner
unblocks, and the first running pass must not call a healthy long-running Salad route silent
from the downtime it is recovering from, nor re-block. Synthetic state only (no provider, no
network): the blocker is a hold-0 CPU attempt whose launch error the worker reported.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from salad_campaign_fixture import SaladWorld, cpu_stage, salad_stage

from tessera_control.campaign_salad_commands import SILENT_EXECUTOR


def _stages(env):
    return [salad_stage(env, "g1"), cpu_stage(env, "z1", hold=0)]


def _at(world: SaladWorld, moment: datetime) -> None:
    world.runner.now = lambda: moment


def _unknowns(world: SaladWorld) -> list:
    return world.env.query(
        "SELECT stage_key, attempt FROM campaign_stage_attempts WHERE campaign_id=? "
        "AND status='unknown'",
        world.campaign_id,
    )


def _blocked_by_z1_with_g1_executing(world: SaladWorld) -> dict:
    """z1 admitted first, then g1 driven to a running route; then z1 goes unknown."""
    world.env.execute("UPDATE execution_project_limits SET max_concurrency=4")
    gate = {"closed": {"g1"}}
    original = world.runner._advance_stage

    def gated(ctx, key):
        return False if key in gate["closed"] else original(ctx, key)

    world.runner._advance_stage = gated
    world.start()
    world.tick_until(lambda: (world.attempt("z1") or {"status": None})["status"] == "admitted")
    gate["closed"] = set()
    world.tick_until(
        lambda: (
            world.row("SELECT source_id FROM campaign_salad_stages WHERE source_id IS NOT NULL")
            is not None
        )
    )
    world.assess_source()
    world.tick_until_phase("awaiting_lease")
    world.lease_and_package()
    world.tick_until_phase("awaiting_review")
    world.resolve_decisions()
    world.tick_until_phase("route_started")
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))
    world.claim("create")
    world.claim("start")
    # Keep the recovered blocker's stage from re-reserving so the test watches g1 only.
    gate["closed"] = {"z1"}

    blocker = world.attempt("z1")
    world.runner._set_attempt(
        blocker["id"],
        status="unknown",
        failure_class="observed_unknown",
        failure_detail="worker silent",
    )
    world.runner._stage_unknown(world.runner._load(world.campaign_id), "z1")
    assert world.status() == "blocked"
    return blocker


def test_unblock_after_a_resolved_blocker_keeps_a_healthy_route_running(tmp_path):
    world = SaladWorld(tmp_path, stages=_stages)
    blocker = _blocked_by_z1_with_g1_executing(world)
    window = world.route_state()["proposal"]["selection"]["resources"]["max_seconds"]
    blocks_before = len(world.events("campaign.blocked"))

    # The block outlasts the route window plus grace (the field block was ~2 h).
    unblock_at = datetime.now(UTC) + timedelta(seconds=window + 3 * 3600)
    _at(world, unblock_at)
    world.tick(2)
    assert world.status() == "blocked"  # nothing resolves the blocker on its own
    assert world.attempt("g1")["status"] == "running"

    # Resolve the blocker: the worker's launch error proves it never started (ADR 0046).
    world._append(
        "execution.observation_reported",
        {"grant_id": blocker["grant_id"], "launch_error": "image_pull_failed"},
    )
    world.tick_until(lambda: world.status() == "running", limit=4)
    resolved = world.row("SELECT * FROM campaign_stage_attempts WHERE id=?", blocker["id"])
    assert resolved["status"] == "failed_transient"
    assert resolved["failure_class"] == "not_started" and resolved["settled_cents"] == 0
    (unblocked,) = world.events("campaign.unblocked")
    assert unblocked["at"]

    # The healthy route is still inside its window counted from the unblock.
    _at(world, unblock_at + timedelta(seconds=window // 2))
    world.tick(4)

    assert _unknowns(world) == []
    assert world.attempt("g1")["status"] == "running"
    assert world.salad()["phase"] == "executing"
    assert world.status() == "running"
    assert len(world.events("campaign.blocked")) == blocks_before
    assert world.events("campaign.salad_import_awaited") == []


def test_silent_clock_after_unblock_still_expires_for_a_truly_silent_route(tmp_path):
    """The clock restarts at the unblock; it is not disabled. Past window plus grace from the
    unblock, a claimed route whose executor stays silent goes unknown as before."""
    world = SaladWorld(tmp_path, stages=_stages)
    blocker = _blocked_by_z1_with_g1_executing(world)
    window = world.route_state()["proposal"]["selection"]["resources"]["max_seconds"]
    unblock_at = datetime.now(UTC) + timedelta(seconds=window + 3 * 3600)
    _at(world, unblock_at)
    world._append(
        "execution.observation_reported",
        {"grant_id": blocker["grant_id"], "launch_error": "image_pull_failed"},
    )
    world.tick_until(lambda: world.status() == "running", limit=4)

    _at(world, unblock_at + timedelta(seconds=window + 3600))
    world.tick(2)

    g1 = world.attempt("g1")
    assert g1["status"] == "unknown" and g1["failure_detail"] == SILENT_EXECUTOR
    assert world.status() == "blocked"
