"""ADR 0149: provider-confirmed termination of a reallocated Salad route (fakes only).

A reallocated route's spend ends only on the provider's own read of the stopped group, recorded
on the route after the stop; the guardian's say-so, a synthetic readback or a worker report never
confirms it. Confirmed, the attempt leaves through the existing settlement path; unconfirmed, it
stays held, visible, and the campaign's input release stays active.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset  # noqa: F401
from test_campaign_salad_executor import (
    OPERATION,
    RECEIPT,
    FakeControl,
    Harness,
    item,
    route_state,
)

from tessera_control.agent_stage_status import stage_status
from tessera_control.campaign_providers import get_provider
from tessera_control.salad_termination import (
    UNCONFIRMED_CODE,
    UNCONFIRMED_EVENT,
    UNCONFIRMED_NEW_ALLOCATION_CODE,
    provider_termination,
)
from tessera_control.store import Conflict

T0 = datetime(2026, 11, 20, tzinfo=UTC)


def _at(seconds: int) -> str:
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _effect(action: str, seconds: int) -> dict:
    return dict(id=uuid.uuid4().hex, action=action, requested_at=_at(seconds))


def _read(effect: dict, seconds: int, *, stopped=True, allocation=None, provenance="provider"):
    """A recorded readback of one route effect (the shape ``RemoteExecutionStore.record``
    appends): stopped with termination confirmed, or still running on an allocation."""
    return dict(
        effect_id=effect["id"],
        provenance=provenance,
        provider_id="provider-group-1",
        allocation_id=None if stopped else allocation,
        state="stopped" if stopped else ("unknown" if allocation != "alloc-a" else "running"),
        termination_confirmed=stopped,
        observed_at=_at(seconds),
        proxy_reason="observed" if stopped or allocation == "alloc-a" else "reallocation_uncertain",
    )


def reallocated(reads=(), *, stop=True, **over) -> dict:
    """A reallocated route: create, start, (stop), then one observe per given readback spec."""
    effects = [_effect("create", 0), _effect("start", 10)]
    if stop:
        effects.append(_effect("stop", 100))
    receipts = []
    for index, spec in enumerate(reads):
        observe = _effect("observe", 110 + 10 * index)
        effects.append(observe)
        receipts.append(_read(observe, 111 + 10 * index, **spec))
    value = dict(
        provider_id="provider-group-1",
        allocation_id="alloc-a",
        reallocated=True,
        desired="stop" if stop else "run",
        state="unknown",
        provider_status="stopped",
        termination_confirmed=False,
        effects=effects,
        receipts=receipts,
    )
    value.update(over)
    return value


# -- the reading -------------------------------------------------------------------------------


def test_a_provider_read_of_the_stopped_group_after_the_stop_confirms():
    reading = provider_termination(reallocated([dict(stopped=True)]))
    assert reading.confirmed and reading.reason == "provider_read_exact_group_stopped"
    assert reading.observed_at == _at(111) and reading.new_allocation_unconfirmed is False


def test_a_provider_that_says_still_running_never_confirms():
    reading = provider_termination(reallocated([dict(stopped=False, allocation="alloc-a")] * 5))
    assert reading.status == "unconfirmed" and reading.code == UNCONFIRMED_CODE


def test_old_instance_gone_but_a_new_allocation_running_is_unconfirmed():
    reading = provider_termination(reallocated([dict(stopped=False, allocation="alloc-b")]))
    assert reading.status == "unconfirmed" and reading.new_allocation_unconfirmed is True
    assert reading.code == UNCONFIRMED_NEW_ALLOCATION_CODE


def test_a_later_adverse_read_undoes_a_confirmation():
    reading = provider_termination(
        reallocated([dict(stopped=True), dict(stopped=False, allocation="alloc-b")])
    )
    assert reading.status == "unconfirmed" and reading.new_allocation_unconfirmed


@pytest.mark.parametrize(
    "route",
    [
        # Guardian-only: no provider read was recorded after the stop.
        reallocated([]),
        # A synthetic readback is never provider observation.
        reallocated([dict(stopped=True, provenance="synthetic")]),
        # No stop was ever requested on the route.
        reallocated([dict(stopped=True)], stop=False),
        # A different provider identity.
        reallocated([dict(stopped=True)], provider_id="provider-group-2"),
    ],
)
def test_nothing_but_the_provider_read_after_the_stop_confirms(route):
    assert provider_termination(route).status == "unconfirmed"


def test_a_read_from_before_the_stop_does_not_count():
    route = reallocated([])
    early = _effect("observe", 50)
    route["effects"].insert(2, early)
    route["receipts"].append(_read(early, 51))
    assert provider_termination(route).reason == "no_provider_read_after_stop"


def test_a_route_that_was_never_reallocated_keeps_the_existing_rule():
    route = reallocated([dict(stopped=True)], reallocated=False)
    assert provider_termination(route).status == "not_applicable"


# -- the executor polls, bounded ----------------------------------------------------------------


class PollingControl(FakeControl):
    """The route answers ``reads`` one per observe; ``refuse`` raises on observe."""

    def __init__(self, reads, refuse=None):
        super().__init__()
        self.reads, self.refuse, self.observes = list(reads), refuse, 0

    def ran(self):
        """What the live driver leaves: the route reallocated, stopped, not yet confirmed."""
        self.state = dict(route_state(), **reallocated([]))

    def observe_termination(self, route_id, profile):
        self.observes += 1
        self._c("observe_termination", route_id, profile.name)
        if self.refuse is not None:
            raise self.refuse
        spec = self.reads.pop(0) if self.reads else dict(stopped=False, allocation="alloc-b")
        effect = _effect("observe", 200 + self.observes)
        self.state["effects"].append(effect)
        self.state["receipts"].append(_read(effect, 201 + self.observes, **spec))


def _harness(tmp_path, control, *, seconds=600):
    receipt = dict(RECEIPT, status="inconclusive", stop_confirmed=False)

    def runner(config_path, receipt_dir, timeout):
        control.ran()
        return receipt

    h = Harness(tmp_path, control, receipt=receipt, runner=runner)
    clock = {"now": 0.0}
    h.executor.config = h.executor.config.model_copy(
        update={"termination_confirm_seconds": seconds, "termination_poll_seconds": 15}
    )
    h.executor.clock = lambda: clock["now"]

    def sleep(seconds):
        h.sleeps.append(seconds)
        clock["now"] += seconds

    h.executor.sleep = sleep
    return h


def test_confirmation_after_n_polls_reports_exactly_one_exit(tmp_path):
    running = dict(stopped=False, allocation="alloc-b")
    control = PollingControl([running, running, dict(stopped=True)])
    h = _harness(tmp_path, control)
    control.items = [item("route_started")]
    h.executor.run_once()
    assert control.observes == 3
    exits = [c for c in control.calls if c[0] == "report_exited"]
    assert exits == [("report_exited", "lease_1", 3, OPERATION, "provider-group-1", _at(204))]
    assert "record_outcome" in h.names()
    assert any(entry["action"] == "termination_confirmed" for entry in h.executor.log)


def test_a_provider_that_keeps_running_is_given_up_at_the_bound(tmp_path):
    control = PollingControl([])
    h = _harness(tmp_path, control, seconds=60)
    control.items = [item("route_started")]
    h.executor.run_once()
    assert control.observes == 4 and sum(h.sleeps) == 60  # bounded, never past the deadline
    assert "report_exited" not in h.names() and "record_outcome" in h.names()
    noted = [e for e in h.executor.log if e["action"] == "termination_unconfirmed"]
    assert noted and noted[0]["detail"] == UNCONFIRMED_NEW_ALLOCATION_CODE


def test_a_refused_observe_ends_the_wait_at_once(tmp_path):
    control = PollingControl([], refuse=Conflict("Remote observation budget exhausted"))
    h = _harness(tmp_path, control)
    control.items = [item("route_started")]
    h.executor.run_once()
    assert control.observes == 1 and h.sleeps == []
    assert "report_exited" not in h.names() and "record_outcome" in h.names()


def test_a_zero_bound_records_the_outcome_without_polling(tmp_path):
    control = PollingControl([dict(stopped=True)])
    h = _harness(tmp_path, control, seconds=0)
    control.items = [item("route_started")]
    h.executor.run_once()
    assert control.observes == 0 and "report_exited" not in h.names()


# -- control: settlement, visibility, release ---------------------------------------------------


def _route_event(world: SaladWorld, state: dict, *, project_id=None, org_id=None) -> None:
    campaign = world.row("SELECT project_id, org_id FROM campaigns WHERE id=?", world.campaign_id)
    with world.env.store.runner_connection() as connection:
        world.env.store._event(
            connection,
            {"id": world.human["id"], "org_id": org_id or campaign["org_id"]},
            project_id or campaign["project_id"],
            None,
            "remote_execution.state",
            state["revision"],
            state,
        )


def _ran_and_reallocated(world: SaladWorld, reads) -> None:
    """Executor double: the route ran, its allocation changed, it was stopped and read."""
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))
    world.claim("create")
    world.claim("start")
    state = world.route_state()
    now = datetime.now(UTC)
    stop = dict(id=uuid.uuid4().hex, action="stop", requested_at=now.isoformat())
    state.update(reallocated([]), receipts=[])
    state["effects"] = [stop]
    for index, spec in enumerate(reads):
        observe = dict(
            id=uuid.uuid4().hex,
            action="observe",
            requested_at=(now + timedelta(seconds=index + 1)).isoformat(),
        )
        state["effects"].append(observe)
        read = _read(observe, 0, **spec)
        read["observed_at"] = (now + timedelta(seconds=index + 1, milliseconds=1)).isoformat()
        state["receipts"].append(read)
    state["revision"] += 1
    _route_event(world, state)


def _record_outcome(world: SaladWorld) -> None:
    row = world.salad()
    world.env.store.record_salad_outcome(
        world.worker,
        row["attempt_id"],
        world.key("outcome"),
        dict(
            route_id=row["route_id"],
            live_receipt=dict(RECEIPT, status="inconclusive", stop_confirmed=False),
            provider_id="provider-group-1",
        ),
    )


def _held(world):
    return world.row(
        "SELECT held_cents, settled_cents FROM campaigns WHERE id=?", world.campaign_id
    )


def _release(world):
    return world.row(
        """SELECT r.* FROM snapshot_releases r JOIN campaign_stage_attempts a
        ON a.release_id = r.id WHERE a.campaign_id=?""",
        world.campaign_id,
    )


def _second_release_refused(world, release) -> bool:
    """Whether another campaign could take its own active release of the same snapshot for the
    same worker (the unique active-release index is what refuses it)."""
    try:
        world.env.execute(
            """INSERT INTO snapshot_releases (id, snapshot_id, worker_principal_id,
            actor_principal_id, digest, classification, rationale, expires_at, created_at,
            status, revision) VALUES (?,?,?,?,?,?,?,?,?,'active',1)""",
            "release_other",
            release["snapshot_id"],
            release["worker_principal_id"],
            release["actor_principal_id"],
            release["digest"],
            release["classification"],
            "another campaign",
            release["expires_at"],
            release["created_at"],
        )
    except sqlite3.IntegrityError:
        return True
    return False


def test_confirmed_termination_releases_the_hold_and_the_input_release(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    _ran_and_reallocated(world, [dict(stopped=False, allocation="alloc-b"), dict(stopped=True)])
    world.observe_exit()  # what the executor reports once the provider read confirmed
    _record_outcome(world)
    assert _held(world)["held_cents"] == 20
    world.tick_until_phase("awaiting_charge")
    # Not blocked: the existing settlement path waits for the charge, as for any failed run.
    assert world.status() == "running" and world.attempt()["salad_verdict"] == "failed"
    assert world.events(UNCONFIRMED_EVENT) == []
    release = _release(world)
    assert release is not None and release["status"] == "active"
    world.record_charge(5)
    world.tick_until(lambda: world.status() == "failed")
    assert dict(_held(world)) == {"held_cents": 0, "settled_cents": 5}
    assert world.attempt()["failure_class"] == "salad_not_passed"
    # The campaign finished, so its input release went through the existing revoke.
    assert _release(world)["status"] == "revoked"
    assert _second_release_refused(world, release) is False
    receipt = json.loads(world.row("SELECT receipt_json FROM campaign_receipts")["receipt_json"])
    termination = receipt["stages"][0]["attempts"][0]["salad_termination"]
    assert termination["status"] == "confirmed" and termination["hold_released"] is True
    # Replay: more ticks and a repeated confirmed read change nothing.
    world.tick(3)
    assert dict(_held(world)) == {"held_cents": 0, "settled_cents": 5}
    assert world.row("SELECT count(*) AS n FROM execution_observations")["n"] == 1


def test_unconfirmed_termination_keeps_the_hold_and_the_release_and_is_visible(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    _ran_and_reallocated(world, [dict(stopped=False, allocation="alloc-b")])
    _record_outcome(world)
    world.tick_until(lambda: world.status() == "blocked")
    world.tick(3)
    salad = world.salad()
    assert salad["phase"] == "unknown"
    assert salad["waiting_reason"] == UNCONFIRMED_NEW_ALLOCATION_CODE
    assert world.attempt()["status"] == "unknown"
    assert _held(world)["held_cents"] == 20 and world.attempt()["settled_cents"] is None
    events = world.events(UNCONFIRMED_EVENT)
    assert len(events) == 1 and events[0]["hold_released"] is False
    assert events[0]["new_allocation_unconfirmed"] is True
    campaign = dict(world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id))
    with world.env.store.runner_connection() as connection:
        status = stage_status(connection, campaign)
    latest = status["stages"][0]["latest_attempt"]
    assert latest["waiting_reason"] == UNCONFIRMED_NEW_ALLOCATION_CODE
    assert latest["status"] == "unknown" and latest["settled_cents"] is None
    release = _release(world)
    assert release["status"] == "active" and _second_release_refused(world, release) is True
    with world.env.store.runner_connection() as connection:
        fields = get_provider("salad").receipt_fields(connection, dict(world.attempt()))
    assert fields["salad_termination"]["status"] == "unconfirmed"
    assert fields["salad_termination"]["hold_released"] is False


def test_a_guardian_or_worker_claim_without_a_provider_read_does_not_confirm(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    _ran_and_reallocated(world, [])  # no provider read after the stop
    world.observe_exit()  # a worker report alone
    row = world.salad()
    world.env.store.record_salad_outcome(
        world.worker,
        row["attempt_id"],
        world.key("outcome"),
        dict(
            route_id=row["route_id"],
            live_receipt=dict(RECEIPT, status="inconclusive", stop_confirmed=False),
            guardian_final=dict(
                status="reconciled",
                reason="provider_confirmed_exact_group_stopped",
                orphan=False,
                allocation_uncertain=True,
                escalation_pending=False,
                budget_fenced=False,
                rate_unknown=False,
                observed_at=_at(0),
            ),
        ),
    )
    world.tick_until(lambda: world.status() == "blocked")
    assert world.salad()["waiting_reason"] == UNCONFIRMED_CODE
    assert _held(world)["held_cents"] == 20


def test_a_confirmed_route_state_in_another_tenant_is_never_read(tmp_path):
    world = SaladWorld(tmp_path)
    world.to_route_started()
    _ran_and_reallocated(world, [dict(stopped=False, allocation="alloc-b")])
    world.observe_exit()
    # A forged, confirmed state for the same route id under another project and org.
    forged = world.route_state()
    stop = next(e for e in forged["effects"] if e["action"] == "stop")
    observe = dict(id=uuid.uuid4().hex, action="observe", requested_at=stop["requested_at"])
    forged["effects"].append(observe)
    confirmed = _read(observe, 0, stopped=True)
    confirmed["observed_at"] = datetime.now(UTC).isoformat()
    forged["receipts"].append(confirmed)
    forged["revision"] += 1
    assert provider_termination(forged).confirmed
    stamp = datetime.now(UTC).isoformat()
    world.env.execute(
        "INSERT INTO organizations (id, name, created_at) VALUES (?,?,?)",
        "org_other",
        "other",
        stamp,
    )
    world.env.execute(
        "INSERT INTO projects (id, org_id, name, created_at) VALUES (?,?,?,?)",
        "project_other",
        "org_other",
        "other",
        stamp,
    )
    _route_event(world, forged, project_id="project_other", org_id="org_other")
    _record_outcome(world)
    world.tick_until(lambda: world.status() == "blocked")
    assert world.salad()["waiting_reason"] == UNCONFIRMED_NEW_ALLOCATION_CODE
    assert _held(world)["held_cents"] == 20
