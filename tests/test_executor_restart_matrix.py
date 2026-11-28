"""ADR 0212 addendum: an executor killed at any phase settles without a person; no route runs twice.

Two halves, fakes only (no subprocess, no network, no live Salad):

* the executor: a fresh ``SaladCampaignExecutor`` over the same work directory stands in for the
  restarted process, after a kill at each phase; it never re-runs a route and, for a live check
  gone with no receipt, reports the guardian's terminal final once (after it reads terminal on
  two reads a settle interval apart);
* control (``SaladWorld``: real stores and runner): a reported lost route resolves at once as
  ``infra_salad_executor_lost`` (retried, settled at the guardian's estimate rounded up), also
  for an attempt already left ``unknown`` as silent; every condition short of the rule keeps
  today's path (fail closed).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset, salad_stage  # noqa: F401
from test_campaign_salad_executor import RECEIPT, FakeControl, config_for, item

from tessera_control import campaign_report, campaign_runner
from tessera_control import campaign_salad_executor as module
from tessera_control.campaign_infra_retry import (
    EXECUTOR_LOST_REASON,
    INFRA_SALAD_EXECUTOR_LOST,
    INFRA_SALAD_PROVISIONING_OVERRUN,
    PLACEMENT_RETRY_CLASSES,
    is_infra_class,
)
from tessera_control.campaign_models import FAILURE_CLASSES
from tessera_control.campaign_salad import SILENT_EXECUTOR_GRACE_SECONDS
from tessera_control.campaign_salad_commands import ROUTE_LOST_EVENT, SILENT_EXECUTOR
from tessera_control.campaign_salad_executor import (
    ROUTE_LOST_MARKER,
    ROUTE_LOST_SETTLE_SECONDS,
    SaladCampaignExecutor,
    StoreControl,
)
from tessera_control.campaign_salad_route_lost import (
    ROUTE_LOST_HELD_EVENT,
    ROUTE_LOST_RESOLVED_EVENT,
)
from tessera_control.salad_exit_watch import FAILURE_CLASS as EXIT_FAILURE_CLASS
from tessera_control.salad_guardian import RECONCILE_OPERATOR, RECONCILE_PROVIDER
from tessera_control.store import Conflict, StoreError

from test_campaign_salad_executor import PROFILE, WORKER  # isort: skip

STAMP = datetime.now(UTC)
FINAL = dict(
    status="stopped",
    reason="workload_exited",
    orphan=False,
    allocation_uncertain=False,
    escalation_pending=False,
    budget_fenced=False,
    rate_unknown=False,
    published_hourly_rate_usd="0.5",
    estimated_exposure_cents="3.2",
    actual_billed_cost_cents=None,
    observed_at=STAMP.isoformat(),
)


# -- the executor half ----------------------------------------------------------------------


class LostControl(FakeControl):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.lost = []
        self.lost_error = None

    def record_route_lost(self, attempt_id, route_id, guardian_final):
        self._c("record_route_lost", attempt_id, route_id)
        if self.lost_error:
            raise self.lost_error
        self.lost.append((attempt_id, route_id, dict(guardian_final)))

    def record_refusal(self, attempt_id, code, step):
        self._c("record_refusal", attempt_id, code, step)


class Process:
    """One executor process over a shared work directory and control; ``restart`` replaces it
    as a supervisor's restart would (in-memory state gone, the disk kept)."""

    def __init__(self, tmp_path, control=None):
        self.tmp_path = tmp_path
        self.control = control or LostControl()
        self.config = config_for(tmp_path)
        self.now = [1000.0]
        self.runs = []
        self.receipt = dict(RECEIPT)
        self.executor = self._build()

    def _build(self):
        def runner(config_path, receipt_dir, timeout):
            self.runs.append(str(receipt_dir))
            return self.receipt

        def importer(route_id, destination, profile):
            (Path(destination) / "output.json").write_bytes(b"{}")
            return [dict(name="output.json", sha256=hashlib.sha256(b"{}").hexdigest(), bytes=2)]

        return SaladCampaignExecutor(
            self.config,
            PROFILE,
            self.control,
            worker_principal_id=WORKER,
            route_runner=runner,
            importer=importer,
            sleep=lambda _s: None,
            clock=lambda: self.now[0],
        )

    def restart(self):
        self.executor = self._build()

    def advance(self, seconds):
        self.now[0] += seconds

    def names(self):
        return [call[0] for call in self.control.calls]

    def route_dir(self, route_id="route_1"):
        return self.tmp_path / "work" / route_id

    def launched_dir(self, route_id="route_1"):
        """The directory a launched route leaves behind (``_prepare_route``'s layout)."""
        directory = self.route_dir(route_id)
        directory.mkdir(mode=0o700)
        (directory / "receipts").mkdir(mode=0o700)
        (directory / "outputs").mkdir(mode=0o700)
        return directory

    def write_receipt(self, route_id="route_1"):
        target = self.route_dir(route_id) / "receipts" / module.RECEIPT_NAME
        target.write_text(json.dumps(RECEIPT))
        target.chmod(0o600)


def test_a_lost_route_is_reported_once_with_the_guardians_settled_final(tmp_path):
    p = Process(tmp_path)
    p.launched_dir()
    p.control.final = dict(FINAL)
    p.control.items = [item("executing")]
    p.executor.run_once()  # first terminal read: settling, nothing reported
    assert p.control.lost == []
    p.advance(ROUTE_LOST_SETTLE_SECONDS + 1)
    p.executor.run_once()
    assert p.control.lost == [("att_1", "route_1", FINAL)]
    assert p.executor.health()["routes_lost_reported_total"] == 1
    marker = json.loads((p.route_dir() / ROUTE_LOST_MARKER).read_text())
    assert marker == {"guardian_final": FINAL, "reported": True}
    # Never again: later passes, and a later restart, read the marker.
    p.advance(600)
    p.executor.run_once()
    p.restart()
    p.executor.run_once()
    assert len(p.control.lost) == 1
    assert p.runs == [] and "mark_executing" not in p.names()


def test_a_guardian_still_running_holds_and_restarts_the_settle(tmp_path):
    p = Process(tmp_path)
    p.launched_dir()
    p.control.items = [item("executing")]
    p.control.final = dict(FINAL, status="armed", reason="armed")
    notes = p.executor.run_once()
    assert ("route_lost_held", "guardian_armed") in [(n["action"], n["detail"]) for n in notes]
    p.advance(ROUTE_LOST_SETTLE_SECONDS + 1)
    p.control.final = dict(FINAL)
    p.executor.run_once()  # the first terminal read starts the settle now
    assert p.control.lost == []
    p.advance(ROUTE_LOST_SETTLE_SECONDS + 1)
    p.executor.run_once()
    assert len(p.control.lost) == 1


def test_a_receipt_written_while_settling_is_finished_and_never_reported_lost(tmp_path):
    p = Process(tmp_path)
    p.launched_dir()
    p.control.final = dict(FINAL)
    p.control.items = [item("executing")]
    p.executor.run_once()
    p.write_receipt()
    p.advance(ROUTE_LOST_SETTLE_SECONDS + 1)
    p.executor.run_once()
    assert p.control.lost == [] and p.names().count("record_outcome") == 1
    assert p.runs == []


@pytest.mark.parametrize(
    "final, reported",
    [
        pytest.param(None, False, id="no_guardian"),
        pytest.param(dict(FINAL, status="unknown", reason="running"), False, id="not_terminal"),
        pytest.param(dict(FINAL, status="reconciled", reason=RECONCILE_PROVIDER), True, id="rec"),
    ],
)
def test_only_a_terminal_guardian_final_is_reported(tmp_path, final, reported):
    p = Process(tmp_path)
    p.launched_dir()
    p.control.final = final
    p.control.items = [item("executing")]
    p.executor.run_once()
    p.advance(ROUTE_LOST_SETTLE_SECONDS + 1)
    p.executor.run_once()
    assert bool(p.control.lost) is reported


def test_no_route_directory_means_nothing_to_speak_for(tmp_path):
    p = Process(tmp_path)
    p.control.final = dict(FINAL)
    p.control.items = [item("executing")]
    p.executor.run_once()
    p.advance(ROUTE_LOST_SETTLE_SECONDS + 1)
    p.executor.run_once()
    assert p.control.lost == [] and "guardian_final" not in p.names()


def test_a_failed_report_replays_the_marked_final_and_is_bounded(tmp_path):
    p = Process(tmp_path)
    p.launched_dir()
    p.control.final = dict(FINAL)
    p.control.items = [item("executing")]
    p.executor.run_once()
    p.advance(ROUTE_LOST_SETTLE_SECONDS + 1)
    p.control.lost_error = Conflict("busy")
    for _ in range(module.ROUTE_LOST_MAX_FAILURES + 3):
        p.executor.run_once()
    assert p.names().count("record_route_lost") == module.ROUTE_LOST_MAX_FAILURES
    # The final read once is replayed as is, even when the guardian later answers otherwise.
    p.control.final = dict(FINAL, estimated_exposure_cents="9")
    p.control.lost_error = None
    p.restart()
    p.executor.run_once()
    assert p.control.lost == [("att_1", "route_1", FINAL)]


def test_a_silent_unknown_attempt_with_no_receipt_is_reported_lost_too(tmp_path):
    p = Process(tmp_path)
    p.launched_dir()
    p.control.final = dict(FINAL)
    p.control.items = [item("unknown")]
    p.executor.run_once()
    p.advance(ROUTE_LOST_SETTLE_SECONDS + 1)
    p.executor.run_once()
    assert len(p.control.lost) == 1
    assert "renew" not in p.names() and p.runs == []


# Where the process died, what the attempt looks like to the restarted executor, the calls the
# restarted process must make, and how many routes it may run. Control's own next phase stands in
# for the runner between passes. Across both processes no route ever runs twice.
KILL_MATRIX = [
    # Killed before the lease: the restarted process leases and packages, nothing runs.
    pytest.param("awaiting_lease", "none", ["acquire", "package"], 0, id="awaiting_lease"),
    # Killed holding a lease before the route: kept alive only.
    pytest.param("leased", "none", ["renew"], 0, id="leased"),
    pytest.param("awaiting_review", "none", ["renew"], 0, id="awaiting_review"),
    pytest.param("route_proposed", "none", ["renew"], 0, id="route_proposed"),
    # Killed after the route started but before it was marked executing: runs exactly once.
    pytest.param("route_started", "none", ["mark_executing", "record_outcome"], 1, id="started"),
    # Killed after ``mark_executing`` but before the child was laid out: control's own
    # unclaimed unwind ends it (``resumed_without_launch_effect``), nothing runs.
    pytest.param("executing", "unlaunched", ["record_no_receipt"], 0, id="marked_no_dir"),
    # Killed mid-route: no receipt, the guardian stopped the group; reported lost once.
    pytest.param("executing", "lost", ["record_route_lost"], 0, id="mid_route"),
    # Killed after the receipt but before the outcome: finished from the receipt on disk.
    pytest.param("executing", "receipt", ["record_outcome"], 0, id="receipt_on_disk"),
]
# Control's next listing after the executor's step (what the runner and store would show).
NEXT_PHASE = {"awaiting_lease": "leased", "route_started": "executing"}
ONCE = ("acquire", "mark_executing", "record_outcome", "record_route_lost", "record_no_receipt")


@pytest.mark.parametrize("phase, disk, expected, runs", KILL_MATRIX)
def test_a_kill_at_each_phase_is_finished_by_the_restarted_process(
    tmp_path, phase, disk, expected, runs
):
    control = LostControl()
    control.final = dict(FINAL)
    control.record_no_receipt = lambda attempt, route: control._c("record_no_receipt", attempt)
    p = Process(tmp_path, control)
    if disk == "unlaunched":
        control.state = dict(control.state, provider_id=None)
    if disk in ("lost", "receipt"):
        p.launched_dir()
    if disk == "receipt":
        p.write_receipt()
    route = None if phase in ("awaiting_lease", "leased", "awaiting_review") else "route_1"
    control.items = [item(phase, route_id=route)]
    p.restart()  # the process that died is gone; this one starts with the disk as it was
    current = phase
    for _ in range(4):
        p.executor.run_once()
        p.advance(ROUTE_LOST_SETTLE_SECONDS + 1)
        if "record_outcome" in p.names():
            control.items = []  # control has the outcome: no longer the executor's work
        elif current in NEXT_PHASE:
            current = NEXT_PHASE[current]
            control.items = [item(current, route_id=route)]
    names = p.names()
    for name in expected:
        assert name in names, (name, names)
    for name in ONCE:
        assert names.count(name) <= 1, (name, names)
    assert len(p.runs) == runs
    if disk != "lost":
        assert "record_route_lost" not in names


# -- the control half -----------------------------------------------------------------------


def _world(tmp_path, *, infra: int = 1) -> SaladWorld:
    stage = lambda env: salad_stage(env, retry={"max_attempts": 1, "infra_retries": infra})  # noqa: E731
    world = SaladWorld(tmp_path, stages=lambda env: [stage(env)])
    world.to_route_started()
    return world


def _executing(world: SaladWorld):
    """The executor marked the route executing and the live check created and started it."""
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))
    world.claim("create")
    world.claim("start")
    return world.salad()


def _report_lost(world: SaladWorld, row, final: dict | None = None) -> None:
    StoreControl(world.env.state, world.worker).record_route_lost(
        row["attempt_id"], row["route_id"], FINAL if final is None else final
    )


def _attempts(world: SaladWorld) -> list:
    return world.env.query("SELECT * FROM campaign_stage_attempts ORDER BY attempt")


def _first(world: SaladWorld):
    return world.row("SELECT * FROM campaign_stage_attempts WHERE attempt=1")


def _retried_at_estimate(world: SaladWorld):
    world.tick_until(lambda: len(_attempts(world)) >= 2)
    first = _first(world)
    assert (first["status"], first["failure_class"]) == (
        "failed_transient",
        INFRA_SALAD_EXECUTOR_LOST,
    )
    assert EXECUTOR_LOST_REASON in first["failure_detail"]
    assert first["salad_verdict"] == "failed" and first["manifest_id"] is None
    # The guardian's estimate (3.2 cents) rounded up; provisional basis.
    assert (first["settled_cents"], first["settlement_basis"]) == (4, "guardian_bounded_hold")
    assert _attempts(world)[1]["attempt"] == 2
    return first


def test_the_class_is_infrastructure_known_to_the_runner_the_report_and_the_retry():
    assert INFRA_SALAD_EXECUTOR_LOST in FAILURE_CLASSES
    assert INFRA_SALAD_EXECUTOR_LOST in campaign_runner._FAILURE_CLASSES
    assert INFRA_SALAD_EXECUTOR_LOST in campaign_report._FAILURES
    assert INFRA_SALAD_EXECUTOR_LOST in PLACEMENT_RETRY_CLASSES
    assert is_infra_class(INFRA_SALAD_EXECUTOR_LOST)


@pytest.mark.parametrize(
    "final",
    [
        pytest.param(FINAL, id="stopped"),
        pytest.param(
            dict(FINAL, status="reconciled", reason=RECONCILE_PROVIDER, allocation_uncertain=True),
            id="provider_reconciled",
        ),
    ],
)
def test_a_reported_lost_route_resolves_at_once_and_retries(tmp_path, final):
    world = _world(tmp_path)
    row = _executing(world)
    _report_lost(world, row, final)
    assert world.events(ROUTE_LOST_EVENT)[-1]["guardian_status"] == final["status"]
    # No route window is waited out: the resolution comes within a few passes.
    _retried_at_estimate(world)
    assert world.events(ROUTE_LOST_RESOLVED_EVENT)[-1]["from_phase"] == "executing"
    assert world.status() == "running" and world.events("campaign.blocked") == []
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", _first(world)["grant_id"])
    assert grant["status"] == "reconciled" and grant["settlement_cents"] == 4


def test_with_no_infra_retry_left_it_fails_under_its_own_class(tmp_path):
    world = _world(tmp_path, infra=0)
    _report_lost(world, _executing(world))
    world.tick_until(lambda: world.status() == "failed")
    first = _first(world)
    assert (first["status"], first["failure_class"]) == (
        "failed_permanent",
        INFRA_SALAD_EXECUTOR_LOST,
    )
    assert (first["settled_cents"], first["settlement_basis"]) == (4, "guardian_bounded_hold")
    assert len(_attempts(world)) == 1


def _provider_read(world: SaladWorld, *, state: str, observed: datetime) -> None:
    route = world.route_state()
    route["receipts"] = [
        *route.get("receipts", []),
        {
            "provenance": "provider",
            "state": state,
            "provider_id": "provider-group-1",
            "allocation_id": "allocation-2" if state == "running" else None,
            "observed_at": observed.isoformat(),
        },
    ]
    route["revision"] += 1
    world._append("remote_execution.state", route, route["revision"])


@pytest.mark.parametrize(
    "final, code",
    [
        pytest.param(
            dict(FINAL, status="reconciled", reason=RECONCILE_OPERATOR),
            "guardian_not_stopped",
            id="operator_attestation",
        ),
        pytest.param(dict(FINAL, orphan=True), "guardian_final_not_clean", id="orphan"),
        pytest.param(dict(FINAL, budget_fenced=True), "guardian_final_not_clean", id="fenced"),
        pytest.param(dict(FINAL, allocation_uncertain=True), "allocation_uncertain", id="unc"),
        pytest.param(dict(FINAL, actual_billed_cost_cents=5), "billed_cost_recorded", id="billed"),
        pytest.param(
            dict(FINAL, estimated_exposure_cents=None), "no_cost_estimate", id="no_estimate"
        ),
        pytest.param(
            dict(FINAL, estimated_exposure_cents="9999"), "estimate_above_hold", id="above_hold"
        ),
        pytest.param(dict(FINAL, retained_before_move=True), "output_retained", id="retained"),
    ],
)
def test_a_final_short_of_the_rule_keeps_todays_wait(tmp_path, final, code):
    world = _world(tmp_path)
    row = _executing(world)
    _report_lost(world, row, final)
    world.tick(4)
    attempt = _first(world)
    assert world.phase() == "executing" and attempt["status"] == "running"
    assert attempt["settled_cents"] is None and len(_attempts(world)) == 1
    assert world.events(ROUTE_LOST_RESOLVED_EVENT) == []
    held = world.events(ROUTE_LOST_HELD_EVENT)
    assert [event["refusal"] for event in held] == [code]  # once per code, never per pass


@pytest.mark.parametrize("evidence", ["uploaded_output", "later_live_readback"])
def test_an_uploaded_output_or_a_later_live_readback_keeps_todays_wait(tmp_path, evidence):
    world = _world(tmp_path)
    row = _executing(world)
    if evidence == "uploaded_output":
        world.upload_manifest()
    else:
        _provider_read(world, state="running", observed=STAMP + timedelta(minutes=1))
    _report_lost(world, row)
    world.tick(3)
    assert world.phase() == "executing" and _first(world)["status"] == "running"
    assert world.events(ROUTE_LOST_HELD_EVENT)[-1]["refusal"] == (
        "output_collected" if evidence == "uploaded_output" else "provider_lists_live_instance"
    )


def test_without_the_report_todays_silent_path_is_unchanged(tmp_path):
    world = _world(tmp_path)
    _executing(world)
    world.tick(4)
    assert world.phase() == "executing" and _first(world)["status"] == "running"
    assert world.events(ROUTE_LOST_HELD_EVENT) == []


def _silent(world: SaladWorld) -> None:
    """Past the route window plus grace with no renewal: the runner calls the executor silent."""
    window = world.route_state()["proposal"]["selection"]["resources"]["max_seconds"]
    start = datetime.fromisoformat(world.salad()["updated_at"])
    start = start if start.tzinfo else start.replace(tzinfo=UTC)
    later = start + timedelta(seconds=window + SILENT_EXECUTOR_GRACE_SECONDS + 1)
    world.runner.now = lambda: later
    world.tick_until(lambda: world.status() == "blocked")
    salad = world.salad()
    assert (salad["phase"], salad["waiting_reason"]) == ("unknown", SILENT_EXECUTOR)


def test_an_attempt_already_called_silent_resolves_once_the_loss_is_reported(tmp_path):
    world = _world(tmp_path)
    row = _executing(world)
    _silent(world)
    world.tick(2)
    assert world.status() == "blocked" and _first(world)["status"] == "unknown"
    # The restarted executor lists it (late-importable), finds no receipt and reports the loss.
    _report_lost(world, row)
    _retried_at_estimate(world)
    assert world.events(ROUTE_LOST_RESOLVED_EVENT)[-1]["from_phase"] == "unknown"
    # The stage left ``blocked_unknown`` and its retry was admitted without a person. (That retry
    # then meets this test's own clock, moved a route window past the approval.)
    assert world.events("campaign.unblocked") or world.status() != "blocked"


def test_a_silent_attempt_short_of_the_rule_stays_blocked_for_a_person(tmp_path):
    world = _world(tmp_path)
    row = _executing(world)
    _silent(world)
    _report_lost(world, row, dict(FINAL, orphan=True))
    world.tick(4)
    assert world.status() == "blocked" and _first(world)["status"] == "unknown"
    assert world.salad()["waiting_reason"] == SILENT_EXECUTOR
    assert world.events(ROUTE_LOST_HELD_EVENT)[-1]["refusal"] == "guardian_final_not_clean"


def test_the_report_is_refused_outside_its_shape(tmp_path):
    world = _world(tmp_path)
    row = world.salad()
    control = StoreControl(world.env.state, world.worker)
    with pytest.raises(Conflict, match="no executing route"):
        control.record_route_lost(row["attempt_id"], row["route_id"], FINAL)
    _executing(world)
    with pytest.raises(Conflict, match="did not start"):
        control.record_route_lost(row["attempt_id"], "route_other", FINAL)
    with pytest.raises(Conflict, match="has not stopped"):
        control.record_route_lost(
            row["attempt_id"], row["route_id"], dict(FINAL, status="armed", reason="armed")
        )
    with pytest.raises(StoreError):
        control.record_route_lost(row["attempt_id"], row["route_id"], {"status": "stopped"})
    world.env.store.record_salad_outcome(
        world.worker,
        row["attempt_id"],
        world.key("outcome"),
        dict(route_id=row["route_id"], live_receipt=RECEIPT, provider_id="provider-group-1"),
    )
    with pytest.raises(Conflict):
        control.record_route_lost(row["attempt_id"], row["route_id"], FINAL)


def test_a_stored_terminal_final_is_never_overwritten(tmp_path):
    world = _world(tmp_path)
    row = _executing(world)
    world.env.execute(
        "UPDATE campaign_salad_attempts SET guardian_final_json=? WHERE attempt_id=?",
        json.dumps(dict(FINAL, estimated_exposure_cents="2.0")),
        row["attempt_id"],
    )
    _report_lost(world, row, dict(FINAL, estimated_exposure_cents="3.9"))
    stored = json.loads(world.salad()["guardian_final_json"])
    assert stored["estimated_exposure_cents"] == "2.0"


# -- review r2 F1: the class a lost route ends with, and its resolved event, are one ------------

ROUTE_DEADLINE = {"running_seconds": 900, "limit": "route_deadline"}
EXITED_ONE = {"outcome": "read", "events": ["instance_exited"], "exit_codes": [1], "machines": 1}
EXITED_THREE = {"outcome": "read", "events": ["instance_exited"], "exit_codes": [], "machines": 3}


@pytest.mark.parametrize(
    "extra, status, failure_class",
    [
        # The guardian stopped the group at the route deadline after the run got going: with
        # the executor dead nothing collected it, so it is the executor's death, retried.
        pytest.param(
            {"reason": "route_deadline", "route_deadline": ROUTE_DEADLINE},
            "failed_transient",
            INFRA_SALAD_EXECUTOR_LOST,
            id="route_deadline",
        ),
        # A run that never got going keeps the provisioning-overrun class.
        pytest.param(
            {
                "reason": "route_deadline",
                "route_deadline": dict(
                    ROUTE_DEADLINE, running_seconds=30, custody_work_begun=False
                ),
            },
            "failed_transient",
            INFRA_SALAD_PROVISIONING_OVERRUN,
            id="provisioning_overrun",
        ),
        pytest.param(
            {
                "reason": "allocation_timeout",
                "allocation_timeout": {"elapsed_seconds": 600, "deadline_seconds": 600},
            },
            "failed_transient",
            "infra_allocation_timeout",
            id="allocation_timeout",
        ),
        pytest.param(
            {"start_failure": {"machines": 3}, "start_events": EXITED_THREE},
            "failed_permanent",
            "start_failure_suspected",
            id="start_failure",
        ),
        pytest.param(
            {"start_events": EXITED_ONE},
            "failed_permanent",
            EXIT_FAILURE_CLASS,
            id="nonzero_exit",
        ),
    ],
)
def test_a_lost_route_ends_with_the_class_its_resolved_event_names(
    tmp_path, extra, status, failure_class
):
    world = _world(tmp_path)
    _report_lost(world, _executing(world), dict(FINAL, **extra))
    world.tick_until(lambda: _first(world)["status"] in ("failed_transient", "failed_permanent"))
    first = _first(world)
    assert (first["status"], first["failure_class"]) == (status, failure_class)
    assert first["salad_verdict"] == "failed" and first["manifest_id"] is None
    (resolved,) = world.events(ROUTE_LOST_RESOLVED_EVENT)
    assert resolved["failure_class"] == failure_class == first["failure_class"]
    assert resolved["reason"] == EXECUTOR_LOST_REASON
    if failure_class == INFRA_SALAD_EXECUTOR_LOST:
        # Spend unchanged: the guardian's estimate rounded up, then a retry.
        _retried_at_estimate(world)
        assert "route deadline after 900s" in _first(world)["failure_detail"]


def test_a_route_deadline_lost_route_with_no_infra_retry_fails_under_the_lost_class(tmp_path):
    world = _world(tmp_path, infra=0)
    final = dict(FINAL, reason="route_deadline", route_deadline=ROUTE_DEADLINE)
    _report_lost(world, _executing(world), final)
    world.tick_until(lambda: world.status() == "failed")
    first = _first(world)
    assert (first["status"], first["failure_class"]) == (
        "failed_permanent",
        INFRA_SALAD_EXECUTOR_LOST,
    )
    assert world.events(ROUTE_LOST_RESOLVED_EVENT)[-1]["failure_class"] == (
        INFRA_SALAD_EXECUTOR_LOST
    )
    assert (first["settled_cents"], first["settlement_basis"]) == (4, "guardian_bounded_hold")
