"""Automatic reconciliation of attempts that never started (ADR 0046).

Fakes only: the control app runs in-process and a fake worker adapter stands in for Docker.
The orphan is a launch the worker itself reports as refused. A host reboot is not a
trigger: control cannot tell an unreported start from no start.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from campaign_runner_harness import ScriptedAdapter, World, dump, stage_spec

from tessera_control.campaign_autoreconcile import MAX_AUTO_REQUEUES
from tessera_control.execution_models import ReportExecutionObservation
from tessera_worker.cpu import CpuLaunchError

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]
PAST = "2000-01-01T00:00:00Z"
POOLS = "network_address_pool_exhausted"


def _world(tmp_path, **stage_extra) -> tuple[World, dict]:
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC, **stage_extra)]
    return world, world.campaign(spec)


def _latest(world, cid):
    return world.attempts(cid)[-1]


def _admit(world, cid):
    world.tick_until(
        lambda: any(a["status"] == "admitted" for a in world.attempts(cid)), worker=False
    )


def _cut_reports(world):
    original = world.channel.post

    def cut(path, body, key):
        if "/worker/observations" in path or "receipt" in path or "manifest" in path:
            raise ConnectionError("synthetic report loss")
        return original(path, body, key)

    world.channel.post = cut
    return original


def _lapse(world, grant_id):
    world.env.execute("UPDATE execution_grants SET expires_at=? WHERE id=?", PAST, grant_id)
    world.env.execute(
        "UPDATE execution_leases SET expires_at=?, hard_expires_at=? WHERE grant_id=?",
        PAST,
        PAST,
        grant_id,
    )


def _report_launch_error(world, error=POOLS):
    lease = world.row("SELECT * FROM execution_leases ORDER BY issued_at DESC")
    operation = world.row(
        "SELECT operation_id FROM external_operations WHERE lease_id=?", lease["id"]
    )
    world.channel.post(
        "/worker/observations",
        {
            "lease_id": lease["id"],
            "expected_epoch": lease["epoch"],
            "operation_id": operation["operation_id"],
            "observed_state": "unknown",
            "observed_at": datetime.now(UTC).isoformat(),
            "launch_error": error,
        },
        f"launch-error-{error}-{datetime.now(UTC).timestamp()}",
    )


def _refuse_launches(monkeypatch):
    def refuse(self, operation):
        raise CpuLaunchError("refused", POOLS)

    monkeypatch.setattr(ScriptedAdapter, "prepare", refuse)


def _refused_launch(world, cid, *, report=True) -> dict:
    """The worker leases the grant, Docker refuses the launch, the grant then lapses. With
    ``report=False`` the worker's report is lost (it can be sent later)."""
    _admit(world, cid)
    original = None if report else _cut_reports(world)
    world.poller.run(max_cycles=1)
    if original is not None:
        world.channel.post = original
    attempt = _latest(world, cid)
    operation = world.row(
        "SELECT provider_id FROM external_operations WHERE grant_id=?", attempt["grant_id"]
    )
    assert operation["provider_id"] is None
    _lapse(world, attempt["grant_id"])
    return attempt


def _started_but_unreported(world, cid, starts) -> dict:
    """The adapter really creates and starts the container (it runs and writes outputs) but
    every report is lost: control sees a lease and an operation and nothing else."""
    _admit(world, cid)
    original = ScriptedAdapter.start

    def counted(self, operation):
        starts.append(operation.operation_id)
        return original(self, operation)

    ScriptedAdapter.start = counted
    try:
        _cut_reports(world)
        world.poller.run(max_cycles=1)
    finally:
        ScriptedAdapter.start = original
    attempt = _latest(world, cid)
    _lapse(world, attempt["grant_id"])
    return attempt


def test_a_refused_launch_is_reconciled_without_a_human(tmp_path, monkeypatch):
    world, campaign = _world(tmp_path)
    cid = campaign["id"]
    _refuse_launches(monkeypatch)
    first = _refused_launch(world, cid)
    monkeypatch.undo()
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    one, two = world.attempts(cid)
    assert (one["status"], one["failure_class"]) == ("failed_transient", "not_started")
    assert (
        f"launch_error:{POOLS}" in one["failure_detail"]
        and "no evidence" in (one["failure_detail"])
    )
    assert (one["settled_cents"], one["settlement_basis"]) == (0, "unclaimed_zero")
    assert one["exit_code"] is None and one["run_receipt_digest"] is None
    assert one["manifest_id"] is None and one["measurements_json"] is None
    assert two["status"] == "succeeded" and two["grant_id"] != first["grant_id"]
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", first["grant_id"])
    assert grant["status"] == "reconciled" and grant["settlement_cents"] == 0
    ledger = world.row("SELECT held_cents, settled_cents FROM campaigns WHERE id=?", cid)
    assert (ledger["held_cents"], ledger["settled_cents"]) == (0, 0)
    (event,) = world.events_of("campaign.attempt_not_started")
    assert event["payload"]["reason"] == f"launch_error:{POOLS}"
    assert event["payload"]["automatic"] is True and event["payload"]["is_evidence"] is False
    assert event["payload"]["consumed_scientific_retry"] is False
    (granted,) = world.events_of("execution.not_started_grant_reconciled")
    assert granted["payload"]["grant_id"] == first["grant_id"]
    assert granted["payload"]["provider_termination_inferred"] is False
    receipt = json.loads(world.row("SELECT receipt_json FROM campaign_receipts")["receipt_json"])
    entries = {a["attempt"]: a for stage in receipt["stages"] for a in stage["attempts"]}
    assert entries[1]["failure_class"] == "not_started"
    assert entries[1]["not_started"]["is_evidence"] is False
    assert entries[1]["evidence_kind"] is None and entries[1]["exit_code"] is None
    settlement = world.row(
        "SELECT receipt_json FROM execution_frame_settlements s JOIN execution_frames f "
        "ON f.frame_id=s.frame_id WHERE f.campaign_attempt_id=?",
        one["id"],
    )
    assert POOLS in settlement["receipt_json"]


def test_control_refuses_lease_renewal_once_a_launch_error_is_recorded(tmp_path, monkeypatch):
    world, campaign = _world(tmp_path)
    cid = campaign["id"]
    _admit(world, cid)
    _refuse_launches(monkeypatch)
    world.poller.run(max_cycles=1)  # the worker leases, is refused and reports the error
    lease = world.row("SELECT * FROM execution_leases")
    renewed_at = lease["expires_at"]
    renew = {"lease_id": lease["id"], "expected_epoch": lease["epoch"]}
    with pytest.raises(ConnectionError, match="409.*launch error"):
        world.channel.post("/worker/leases/renew", renew, "renew-after")
    assert world.row("SELECT expires_at FROM execution_leases")["expires_at"] == renewed_at


def test_lease_renewal_is_unaffected_without_a_launch_error(tmp_path):
    world, campaign = _world(tmp_path)
    _admit(world, campaign["id"])
    world.poller.run(max_cycles=1)
    lease = world.row("SELECT * FROM execution_leases")
    # The run finished and its grant is resolved or lapsing; a renewal is judged on its own
    # rules, never refused for a launch error that was not reported.
    try:
        world.channel.post(
            "/worker/leases/renew",
            {"lease_id": lease["id"], "expected_epoch": lease["epoch"]},
            "renew-plain",
        )
    except ConnectionError as error:
        assert "launch error" not in str(error)


def test_a_started_but_unreported_attempt_is_not_reconciled_as_not_started(tmp_path):
    """A container that really ran, with every report lost, is an unknown effect: control
    cannot tell it from a launch that never happened, so it keeps the human path."""
    world, campaign = _world(tmp_path)
    cid = campaign["id"]
    starts: list[str] = []
    _started_but_unreported(world, cid, starts)
    assert len(starts) == 1  # the container was started
    for _ in range(4):
        world.runner.tick()
    assert world.status(cid) == "blocked", dump(world, cid)
    assert _latest(world, cid)["status"] == "unknown"
    assert _latest(world, cid)["failure_class"] != "not_started"
    assert world.row("SELECT status FROM execution_grants")["status"] == "active"
    assert not world.events_of("campaign.attempt_not_started")
    assert not world.events_of("campaign.autoreconcile_stopped")


def test_an_already_blocked_campaign_is_unblocked_when_the_launch_error_arrives(
    tmp_path, monkeypatch
):
    world, campaign = _world(tmp_path)
    cid = campaign["id"]
    _refuse_launches(monkeypatch)
    _refused_launch(world, cid, report=False)
    world.runner.tick()
    assert world.status(cid) == "blocked" and world.stages(cid) == {"s1": "blocked_unknown"}
    # The worker's report arrives late; the next loop reconciles the blocked campaign.
    _report_launch_error(world)
    monkeypatch.undo()
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert [a["failure_class"] for a in world.attempts(cid)] == ["not_started", None]


def test_a_cancelled_campaign_is_not_held_open_by_a_refused_launch(tmp_path, monkeypatch):
    world, campaign = _world(tmp_path)
    cid = campaign["id"]
    _refuse_launches(monkeypatch)
    _refused_launch(world, cid, report=False)
    world.runner.tick()
    assert world.status(cid) == "blocked"
    world.cancel(campaign)
    _report_launch_error(world)
    for _ in range(12):
        world.runner.tick()
        if world.status(cid) == "cancelled":
            break
    assert world.status(cid) == "cancelled", dump(world, cid)
    (only,) = world.attempts(cid)  # nothing was reserved again
    assert only["failure_class"] == "not_started" and only["settled_cents"] == 0
    assert world.row("SELECT held_cents FROM campaigns WHERE id=?", cid)["held_cents"] == 0


def test_a_not_started_launch_failure_does_not_consume_a_scientific_retry(tmp_path, monkeypatch):
    world, campaign = _world(tmp_path, retry={"max_attempts": 1, "transient_exit_codes": [7]})
    cid = campaign["id"]
    _refuse_launches(monkeypatch)
    _refused_launch(world, cid)
    monkeypatch.undo()
    world.tick_all(cid)
    # max_attempts is 1 and the first attempt never started: the second still runs.
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert world.row("SELECT attempts_used FROM campaign_stages")["attempts_used"] == 2


def test_an_attempt_that_held_money_is_not_auto_reconciled(tmp_path, monkeypatch):
    world, campaign = _world(tmp_path)
    cid = campaign["id"]
    _refuse_launches(monkeypatch)
    attempt = _refused_launch(world, cid)
    world.env.execute("UPDATE campaign_stage_attempts SET hold_cents=5 WHERE id=?", attempt["id"])
    for _ in range(3):
        world.runner.tick()
    assert world.status(cid) == "blocked", dump(world, cid)
    assert _latest(world, cid)["status"] == "unknown"
    assert world.row("SELECT status FROM execution_grants")["status"] == "active"
    assert not world.events_of("campaign.attempt_not_started")


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE external_operations SET provider_id='container-1'",
        "UPDATE campaign_stage_attempts SET exit_code=0",
        "UPDATE campaign_stage_attempts SET run_receipt_digest=lower(hex(zeroblob(32)))",
        "UPDATE execution_grants SET max_exposure_cents=3",
        "UPDATE execution_leases SET hard_expires_at='2999-01-01T00:00:00Z'",
    ],
)
def test_any_sign_of_a_started_container_keeps_the_human_path(tmp_path, monkeypatch, sql):
    world, campaign = _world(tmp_path)
    cid = campaign["id"]
    _refuse_launches(monkeypatch)
    _refused_launch(world, cid)
    world.env.execute(sql)
    for _ in range(3):
        world.runner.tick()
    assert world.status(cid) == "blocked", dump(world, cid)
    assert _latest(world, cid)["failure_class"] != "not_started"
    assert not world.events_of("campaign.attempt_not_started")


def test_a_worker_observation_of_a_container_blocks_auto_reconcile(tmp_path, monkeypatch):
    world, campaign = _world(tmp_path)
    cid = campaign["id"]
    _refuse_launches(monkeypatch)
    attempt = _refused_launch(world, cid)
    lease = world.row("SELECT * FROM execution_leases")
    world.env.execute(
        """INSERT INTO execution_observations (id, grant_id, lease_id, worker_principal_id, epoch,
        desired_state, observed_state, observed_at, provider_id, termination_confirmed, recorded_at)
        VALUES ('observation_x', ?, ?, ?, ?, 'admitted', 'unknown', ?, 'container-1', 0, ?)""",
        attempt["grant_id"],
        lease["id"],
        lease["worker_principal_id"],
        lease["epoch"],
        PAST,
        PAST,
    )
    for _ in range(3):
        world.runner.tick()
    assert world.status(cid) == "blocked"
    assert _latest(world, cid)["status"] == "unknown"


def test_identical_launch_failures_are_bounded_and_blocked_with_a_reason(tmp_path, monkeypatch):
    world, campaign = _world(tmp_path)
    cid = campaign["id"]
    _refuse_launches(monkeypatch)
    for _ in range(MAX_AUTO_REQUEUES + 1):
        _refused_launch(world, cid)
        for _ in range(10):
            world.runner.tick()
    attempts = world.attempts(cid)
    assert [a["failure_class"] for a in attempts[:MAX_AUTO_REQUEUES]] == [
        "not_started"
    ] * MAX_AUTO_REQUEUES
    last = attempts[-1]
    assert last["status"] == "unknown" and len(attempts) == MAX_AUTO_REQUEUES + 1
    assert world.status(cid) == "blocked", dump(world, cid)
    assert "repeated" in last["failure_detail"]
    assert "a person must reconcile" in last["failure_detail"]
    (stopped,) = world.events_of("campaign.autoreconcile_stopped")
    assert stopped["payload"]["not_started_reason"] == f"launch_error:{POOLS}"
    (blocked,) = world.events_of("campaign.blocked")
    assert "repeated" in blocked["payload"]["reason"]
    count = len(world.attempts(cid))
    for _ in range(5):
        world.runner.tick()
    assert len(world.attempts(cid)) == count


def test_not_started_attempts_are_never_gate_evidence(tmp_path, monkeypatch):
    world, campaign = _world(tmp_path)
    cid = campaign["id"]
    _refuse_launches(monkeypatch)
    _refused_launch(world, cid)
    monkeypatch.undo()
    world.tick_all(cid)
    one, two = world.attempts(cid)
    assert one["failure_class"] == "not_started"
    for column in (
        "exit_code",
        "run_receipt_digest",
        "manifest_id",
        "manifest_digest",
        "measurements_json",
        "salad_verdict",
        "salad_evidence_digest",
    ):
        assert one[column] is None, column
    measured = world.row(
        "SELECT count(*) AS n FROM campaign_stage_attempts WHERE measurements_json IS NOT NULL"
    )
    assert measured["n"] == 1 and two["measurements_json"] is not None


def test_a_launch_error_needs_an_unknown_observation_with_no_container():
    base = {
        "lease_id": "lease_1",
        "expected_epoch": 1,
        "observed_at": "2026-01-01T00:00:00+00:00",
        "launch_error": "launch_failed",
    }
    ReportExecutionObservation(**base, observed_state="unknown")
    for extra in (
        {"observed_state": "running"},
        {"observed_state": "unknown", "provider_id": "container-1"},
        {"observed_state": "exited", "termination_confirmed": True},
    ):
        with pytest.raises(ValueError):
            ReportExecutionObservation(**base, **extra)
    with pytest.raises(ValueError):
        ReportExecutionObservation(
            **{**base, "launch_error": "free text"}, observed_state="unknown"
        )
