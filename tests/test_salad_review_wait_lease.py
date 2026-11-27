"""A Salad attempt survives a long review wait.

* Worker authority that lapsed before the route started (the lease's hard expiry passed while
  the plan review waited, or control refuses the worker as disconnected) requeues the stage as
  ``not_started`` with a fresh grant: nothing was sent, so it is never ``failed_permanent``.
* A refusal that is not about worker authority stays a permanent step refusal.
* The executor heartbeats its registration every pass, independent of leases: an idle pass,
  a pass whose only attempt is fenced, and a pass whose work listing failed all heartbeat.

Real stores and runner (``SaladWorld``) on synthetic state; no provider, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from salad_campaign_fixture import SaladWorld, salad_provider_reset  # noqa: F401
from test_campaign_salad_executor import FakeControl, Harness, item

from tessera_control.campaign_autoreconcile import MAX_AUTO_REQUEUES, NOT_STARTED, repeat_count
from tessera_control.campaign_salad_lease_lapse import (
    LEASE_LAPSED_BEFORE_ROUTE,
    REQUEUE_EVENT,
    WORKER_UNAVAILABLE_BEFORE_ROUTE,
)
from tessera_control.campaign_steps import StepFailed
from tessera_control.store import Conflict


def _past(minutes: int = 5) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


def _lapse_lease(world: SaladWorld) -> None:
    """The lease's hard expiry (counted from packaging) passed during the review wait."""
    world.env.execute(
        "UPDATE execution_leases SET expires_at=?, hard_expires_at=? WHERE id=?",
        _past(),
        _past(),
        world.salad()["lease_id"],
    )


def _stale_worker(world: SaladWorld) -> None:
    world.env.execute("UPDATE worker_registry SET last_seen_at=?", _past(80))


def _first(world: SaladWorld):
    return world.row(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND stage_key='g1' "
        "AND attempt=1",
        world.campaign_id,
    )


def _stage(world: SaladWorld):
    return world.row(
        "SELECT * FROM campaign_stages WHERE campaign_id=? AND stage_key='g1'", world.campaign_id
    )


def _requeued(world: SaladWorld) -> bool:
    first = _first(world)
    return first["status"] in {"failed_transient", "failed_permanent"}


def test_a_lease_that_lapsed_during_the_review_wait_requeues_not_started(tmp_path):
    # The field case: packaged, the review waited past the lease's hard expiry, the executor
    # fenced the attempt and its registration went stale; then the review was approved.
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    _lapse_lease(world)
    _stale_worker(world)
    world.resolve_decisions()

    world.tick_until(lambda: _requeued(world))

    first = _first(world)
    assert first["status"] == "failed_transient"
    assert first["failure_class"] == NOT_STARTED
    assert f"({LEASE_LAPSED_BEFORE_ROUTE})" in first["failure_detail"]
    # Nothing was created at the provider: settled at zero, never a start.
    settlement = world.row(
        "SELECT settled_cents FROM campaign_stage_attempts WHERE id=?", first["id"]
    )
    assert settlement["settled_cents"] in (0, None)
    assert world.row(
        "SELECT 1 FROM campaign_step_intents WHERE campaign_id=? AND step='start_route' "
        "AND status='done'",
        world.campaign_id,
    ) is None
    # The stage (one scientific attempt) is not failed; a fresh attempt is placed and the
    # campaign keeps running.
    world.tick_until(lambda: world.attempt()["attempt"] == 2)
    assert _stage(world)["status"] not in {"failed", "skipped"}
    assert world.status() == "running"
    assert [e for e in world.events(REQUEUE_EVENT) if e.get("reason") == LEASE_LAPSED_BEFORE_ROUTE]


def test_a_disconnected_worker_refusal_before_start_requeues_not_started(tmp_path):
    # The lease is still current, but control refuses the stale worker lease authority
    # (``require_ready_worker``) on a pre-route step: transient, not a permanent refusal.
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    _stale_worker(world)
    world.resolve_decisions()

    world.tick_until(lambda: _requeued(world))

    first = _first(world)
    assert first["status"] == "failed_transient", world.dump()
    assert first["failure_class"] == NOT_STARTED
    assert f"({WORKER_UNAVAILABLE_BEFORE_ROUTE})" in first["failure_detail"]
    assert _stage(world)["status"] not in {"failed", "skipped"}


def test_a_genuine_pre_route_refusal_stays_permanent(tmp_path, monkeypatch):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    world.resolve_decisions()
    world.tick_until_phase("route_proposed")
    driver_type = type(world.driver)

    def refused(self, *args, **kwargs):
        raise StepFailed("salad", "step_refused", "salad_image_not_allowlisted")

    monkeypatch.setattr(driver_type, "_approve_and_start", refused)

    world.tick_until(lambda: _requeued(world))

    first = _first(world)
    assert first["status"] == "failed_permanent"
    assert first["failure_class"] == "step_refused"
    assert not world.events(REQUEUE_EVENT)


def test_a_lease_lapse_requeue_counts_toward_the_identical_repeat_bound(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    _lapse_lease(world)
    world.resolve_decisions()
    world.tick_until(lambda: _requeued(world))
    first = dict(_first(world))
    assert repeat_count([first], LEASE_LAPSED_BEFORE_ROUTE) == 1
    assert repeat_count([first] * MAX_AUTO_REQUEUES, LEASE_LAPSED_BEFORE_ROUTE) == MAX_AUTO_REQUEUES


# -- executor liveness ------------------------------------------------------------------------


def test_an_idle_pass_heartbeats_the_registration(tmp_path):
    h = Harness(tmp_path)
    h.control.items = []
    h.executor.run_once()
    h.executor.run_once()
    assert h.names() == ["heartbeat", "heartbeat"]


def test_a_fenced_attempt_waiting_for_review_does_not_stop_the_heartbeat(tmp_path):
    h = Harness(tmp_path)
    h.control.items = [item("awaiting_review")]
    h.control.renew_error = Conflict("Execution lease is no longer current.")
    actions = h.executor.run_once()
    assert [a["action"] for a in actions] == ["lease_renewal_refused"]
    h.control.calls.clear()
    # The attempt is fenced (never renewed again), but the executor stays seen as alive.
    for _ in range(3):
        h.executor.run_once()
    assert h.names() == ["heartbeat"] * 3


def test_a_failed_work_listing_still_heartbeats(tmp_path):
    class NoWork(FakeControl):
        def work(self):
            raise RuntimeError("control busy")

    h = Harness(tmp_path, NoWork())
    actions = h.executor.run_once()
    assert [a["action"] for a in actions] == ["work_unavailable"]
    assert h.names() == ["heartbeat"]


def test_a_renewing_pass_sends_one_heartbeat(tmp_path):
    h = Harness(tmp_path)
    h.control.items = [item("awaiting_review")]
    h.executor.run_once()
    assert h.names() == ["heartbeat", "renew"]
