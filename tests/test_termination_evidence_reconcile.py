"""Definitive termination evidence reaches control after the worker stopped driving a grant
(ADR 0160). In-process control, the real poller and service, fake Docker and fake guardian
replies only: no Docker, no provider and no network."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from campaign_runner_harness import World, lapse, stage_spec, started_but_unreported
from test_campaign_salad_executor import FakeControl, Harness, item

from tessera_control import campaign_exited_resolve as cer
from tessera_control.campaign_salad_executor import GROUP_STOP_MARKER, group_stop_hold_reason
from tessera_control.execution import ExecutionStore
from tessera_control.salad_guardian import RECONCILE_OPERATOR, RECONCILE_PROVIDER
from tessera_control.store import Conflict, StoreError, canonical_digest
from tessera_worker.control_channel import ControlChannelError
from tessera_worker.termination_evidence import (
    MARKER,
    TerminationEvidenceSweep,
    read_identity,
)

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]


class FakeDocker:
    """``docker inspect`` replies from a table keyed by container id and name."""

    def __init__(self) -> None:
        self.containers: dict[str, dict] = {}
        self.unavailable = False
        self.calls: list[str] = []

    def add(self, identity, container_id, status, exit_code=None, *, running=False, labels=None):
        record = {
            "Id": container_id,
            "Config": {
                "Labels": labels
                or {
                    "tessera.operation": identity.operation_id,
                    "tessera.plan": identity.plan_digest,
                }
            },
            "State": {"Status": status, "Running": running, "ExitCode": exit_code},
        }
        self.containers[container_id] = record
        self.containers[identity.container_name] = record

    def __call__(self, arguments):
        assert list(arguments[:3]) == ["inspect", "--type", "container"]
        reference = arguments[3]
        self.calls.append(reference)
        if self.unavailable:
            return SimpleNamespace(returncode=1, stdout="", stderr="Cannot connect to daemon")
        record = self.containers.get(reference)
        if record is None:
            return SimpleNamespace(
                returncode=1, stdout="", stderr=f"Error: No such container: {reference}"
            )
        return SimpleNamespace(returncode=0, stdout=json.dumps([record]), stderr="")


class StatusChannel:
    """The harness channel, raising control's refusals with their HTTP status as the real
    channel does (so a refusal is ``permanent``, not a transport failure)."""

    def __init__(self, channel) -> None:
        self.channel, self.posts = channel, []

    def post(self, path, body, key):
        self.posts.append((path, body))
        try:
            return self.channel.post(path, body, key)
        except ConnectionError as error:
            found = re.search(r"rejected command: (\d+)", str(error))
            raise ControlChannelError(
                "control refused", int(found.group(1)) if found else None
            ) from None


def _world(tmp_path, **extra):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC, **extra)]
    return world, world.campaign(spec)["id"]


def _admit(world, cid):
    world.tick_until(
        lambda: any(a["status"] == "admitted" for a in world.attempts(cid)), worker=False
    )


def _gave_up_while_running(world, cid):
    """The container ran and control accepted its ``running`` observation (with the container
    id); the lease renewal then failed, the service gave up (``result.json`` unknown) and no
    exit was ever reported."""
    _admit(world, cid)
    original = world.channel.post

    def cut(path, body, key):
        if "/worker/leases/renew" in path:
            raise ConnectionError("synthetic renewal loss")
        return original(path, body, key)

    world.channel.post = cut
    world.poller.run(max_cycles=1)
    world.channel.post = original
    attempt = world.attempts(cid)[-1]
    grant_dir = world.tmp / "worker-root" / "grants" / attempt["grant_id"]
    result = json.loads((grant_dir / "result.json").read_text())
    assert (result["state"], result["reason"]) == ("unknown", "heartbeat_or_renewal_failure")
    return attempt, grant_dir


def _sweep(world, docker, channel=None):
    sweep = TerminationEvidenceSweep(
        channel or world.channel, run=docker, clock=lambda: 0.0, poll_seconds=1.0
    )
    world.poller.termination = sweep
    return sweep


def _observations(world, grant_id):
    return [
        (r["observed_state"], r["termination_confirmed"], r["provider_id"])
        for r in world.env.query(
            "SELECT * FROM execution_observations WHERE grant_id=? ORDER BY rowid", grant_id
        )
    ]


def _grant(world, grant_id):
    return world.row("SELECT * FROM execution_grants WHERE id=?", grant_id)


def _human_reconcile(world, grant_id):
    grant = _grant(world, grant_id)
    body = {
        "expected_revision": grant["revision"],
        "settlement_cents": 0,
        "provider_terminated": True,
    }
    return ExecutionStore(world.env.state).reconcile_execution_grant(
        world.env.human(), grant_id, "reconcile-" + grant_id[-8:], canonical_digest(body), body
    )


def _identity(grant_dir):
    identity = read_identity(grant_dir / "workspace" / "runtime")
    assert identity is not None
    return identity


def _later(world, minutes):
    world.runner.now = lambda: datetime.now(UTC) + timedelta(minutes=minutes)


# -- (a) the exact recorded container is gone after the lease ended ---------------------------


def test_an_absent_recorded_container_is_delivered_and_reconcile_accepts_it(tmp_path):
    world, cid = _world(tmp_path)
    attempt, grant_dir = _gave_up_while_running(world, cid)
    identity = _identity(grant_dir)
    recorded = f"container-{identity.operation_id}"
    assert ("running", 0, recorded) in _observations(world, attempt["grant_id"])
    # Before any evidence every supported route refuses, as observed in the field.
    with pytest.raises(Conflict, match="requires confirmed termination"):
        _human_reconcile(world, attempt["grant_id"])
    lapse(world, attempt["grant_id"])
    docker = FakeDocker()  # no container by id or by name
    _sweep(world, docker)
    world.poller.run(max_cycles=1)
    assert docker.calls == [recorded, identity.container_name]
    marker = json.loads((grant_dir / MARKER).read_text())
    assert (marker["status"], marker["reason"], marker["provider_id"]) == (
        "reported",
        "container_absent",
        recorded,
    )
    assert _observations(world, attempt["grant_id"])[-1] == ("exited", 1, recorded)
    (event,) = [
        e
        for e in world.events_of("execution.observation_reported")
        if e["payload"].get("termination_evidence")
    ]
    assert event["payload"]["termination_evidence"] == "container_absent"
    assert event["payload"]["grant_id"] == attempt["grant_id"]
    assert "exit_code" not in event["payload"]
    # The existing route now accepts it; nothing else was changed by the report.
    assert _grant(world, attempt["grant_id"])["status"] == "active"
    status, grant = _human_reconcile(world, attempt["grant_id"])
    assert status == 200 and grant["status"] == "reconciled"
    # Delivered once: later cycles neither inspect nor post again.
    docker.calls.clear()
    world.poller.run(max_cycles=1)
    assert docker.calls == []


def test_the_campaign_settles_an_absent_container_and_the_retry_runs(tmp_path):
    world, cid = _world(tmp_path, retry={"max_attempts": 2})
    first, _ = _gave_up_while_running(world, cid)
    lapse(world, first["grant_id"])
    _sweep(world, FakeDocker())
    world.poller.run(max_cycles=1)
    _later(world, 10)  # past the output grace (ADR 0064)
    world.runner.tick()
    closed = world.row("SELECT * FROM campaign_stage_attempts WHERE id=?", first["id"])
    assert (closed["status"], closed["failure_class"]) == (
        "failed_transient",
        cer.EXIT_CONFIRMED_UNRESOLVED,
    )
    assert (closed["settled_cents"], closed["settlement_basis"]) == (0, "local_zero")
    assert _grant(world, first["grant_id"])["status"] == "reconciled"
    world.runner.now = lambda: datetime.now(UTC)
    world.tick_all(cid)
    assert world.status(cid) == "succeeded"
    assert [a["status"] for a in world.attempts(cid)] == ["failed_transient", "succeeded"]


def test_absence_before_the_lease_ended_is_refused_and_retried_later(tmp_path):
    world, cid = _world(tmp_path)
    attempt, grant_dir = _gave_up_while_running(world, cid)
    channel = StatusChannel(world.channel)
    sweep = _sweep(world, FakeDocker(), channel)
    world.poller.run(max_cycles=1)  # the lease is still live at control
    marker = json.loads((grant_dir / MARKER).read_text())
    assert (marker["status"], marker["reason"], marker["control_status"]) == (
        "held",
        "control_refused",
        409,
    )
    assert marker["final"] is False and _grant(world, attempt["grant_id"])["status"] == "active"
    assert _observations(world, attempt["grant_id"])[-1][0] == "running"
    lapse(world, attempt["grant_id"])
    sweep._backoff.clear()  # the backoff has passed
    world.poller.run(max_cycles=1)
    assert json.loads((grant_dir / MARKER).read_text())["status"] == "reported"
    assert _observations(world, attempt["grant_id"])[-1][:2] == ("exited", 1)


# -- (b) the container exited with a known code -----------------------------------------------


def test_an_exited_container_reports_its_exit_code_and_the_retry_applies(tmp_path):
    world, cid = _world(tmp_path, retry={"max_attempts": 2})
    first, grant_dir = _gave_up_while_running(world, cid)
    identity = _identity(grant_dir)
    recorded = f"container-{identity.operation_id}"
    docker = FakeDocker()
    docker.add(identity, recorded, "exited", 137)  # SIGKILL
    _sweep(world, docker)
    world.poller.run(max_cycles=1)  # the grant is still live: an exit needs no expiry
    marker = json.loads((grant_dir / MARKER).read_text())
    assert (marker["status"], marker["reason"], marker["exit_code"]) == (
        "reported",
        "container_exited",
        137,
    )
    (event,) = [
        e for e in world.events_of("execution.observation_reported") if "exit_code" in e["payload"]
    ]
    assert event["payload"]["exit_code"] == 137 and event["payload"]["termination_confirmed"]
    assert "termination_evidence" not in event["payload"]
    lapse(world, first["grant_id"])
    _later(world, 10)
    world.runner.tick()
    closed = world.row("SELECT * FROM campaign_stage_attempts WHERE id=?", first["id"])
    assert closed["status"] == "failed_transient"
    assert closed["failure_detail"].startswith("exit_code:137")
    assert _grant(world, first["grant_id"])["status"] == "reconciled"
    world.runner.now = lambda: datetime.now(UTC)
    world.tick_all(cid)
    assert world.status(cid) == "succeeded"


# -- doubt holds --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "reason", "final"),
    [
        ("running", "container_may_run", False),
        ("created", "container_may_run", False),
        ("restarting", "container_may_run", False),
        ("unavailable", "docker_unavailable", False),
        ("other_labels", "container_identity_mismatch", True),
        ("other_id_by_name", "container_identity_conflict", True),
    ],
)
def test_any_doubt_holds_the_grant_with_a_reason(tmp_path, setup, reason, final):
    world, cid = _world(tmp_path)
    attempt, grant_dir = _gave_up_while_running(world, cid)
    lapse(world, attempt["grant_id"])
    identity = _identity(grant_dir)
    recorded = f"container-{identity.operation_id}"
    docker = FakeDocker()
    if setup == "running":
        docker.add(identity, recorded, "running", 0, running=True)
    elif setup == "created":
        docker.add(identity, recorded, "created", 0)
    elif setup == "restarting":
        docker.add(identity, recorded, "exited", 1)
        docker.containers[recorded]["State"]["Restarting"] = True
    elif setup == "unavailable":
        docker.unavailable = True
    elif setup == "other_labels":
        docker.add(identity, recorded, "exited", 0, labels={"tessera.operation": "x"})
    else:
        docker.add(identity, "container-someone-else", "exited", 0)
        docker.containers.pop("container-someone-else")
    channel = StatusChannel(world.channel)
    _sweep(world, docker, channel)
    world.poller.run(max_cycles=1)
    marker = json.loads((grant_dir / MARKER).read_text())
    assert (marker["status"], marker["reason"], marker["final"]) == ("held", reason, final)
    assert channel.posts == []  # nothing reached control
    assert _observations(world, attempt["grant_id"])[-1][:2] == ("running", 0)
    with pytest.raises(Conflict, match="requires confirmed termination"):
        _human_reconcile(world, attempt["grant_id"])


def _unreported_and_given_up(world, cid):
    """Started, every report lost (control never recorded the container), lease lapsed; the
    poller then gave up on its undelivered observation as in the field (``result.json``)."""
    attempt = started_but_unreported(world, cid, [])
    del world.channel.post  # the harness cut stays installed otherwise
    grant_dir = world.tmp / "worker-root" / "grants" / attempt["grant_id"]
    if not (grant_dir / "result.json").exists():
        (grant_dir / "result.json").write_text(
            json.dumps({"state": "unknown", "reason": "observation_report_failure"})
        )
    return attempt, grant_dir


# -- (d) an absent container whose identity was never recorded ----------------------------------


def test_absence_without_a_recorded_identity_never_counts(tmp_path):
    world, cid = _world(tmp_path)
    attempt, grant_dir = _unreported_and_given_up(world, cid)
    assert _observations(world, attempt["grant_id"]) == []
    channel = StatusChannel(world.channel)
    docker = FakeDocker()
    _sweep(world, docker, channel)
    world.poller.run(max_cycles=1)
    marker = json.loads((grant_dir / MARKER).read_text())
    assert (marker["status"], marker["reason"], marker["final"]) == (
        "held",
        "container_identity_unrecorded",
        True,
    )
    assert channel.posts == [] and _grant(world, attempt["grant_id"])["status"] == "active"
    # A final hold is not re-checked.
    docker.calls.clear()
    world.poller.run(max_cycles=1)
    assert docker.calls == []


def test_control_refuses_absence_for_an_identity_it_never_recorded(tmp_path):
    """Even a worker that claims it: control matches the identity itself (fail closed)."""
    world, cid = _world(tmp_path)
    attempt, grant_dir = _unreported_and_given_up(world, cid)
    identity = _identity(grant_dir)
    body = {
        "lease_id": identity.lease_id,
        "expected_epoch": identity.epoch,
        "operation_id": identity.operation_id,
        "observed_state": "exited",
        "observed_at": datetime.now(UTC).isoformat(),
        "provider_id": f"container-{identity.operation_id}",
        "termination_confirmed": True,
        "termination_evidence": "container_absent",
    }
    with pytest.raises(ConnectionError, match="409"):
        world.channel.post("/worker/observations", body, "absent-unrecorded")
    assert _observations(world, attempt["grant_id"]) == []


def test_control_refuses_absence_naming_another_container(tmp_path):
    world, cid = _world(tmp_path)
    attempt, grant_dir = _gave_up_while_running(world, cid)
    lapse(world, attempt["grant_id"])
    identity = _identity(grant_dir)
    body = {
        "lease_id": identity.lease_id,
        "expected_epoch": identity.epoch,
        "operation_id": identity.operation_id,
        "observed_state": "exited",
        "observed_at": datetime.now(UTC).isoformat(),
        "provider_id": "container-other",
        "termination_confirmed": True,
        "termination_evidence": "container_absent",
    }
    with pytest.raises(ConnectionError, match="409"):
        world.channel.post("/worker/observations", body, "absent-other")
    # Malformed evidence (an exit code, or no confirmation) is refused before the store.
    for change in ({"exit_code": 137}, {"termination_confirmed": False}):
        with pytest.raises(ConnectionError, match="422"):
            world.channel.post("/worker/observations", body | {"provider_id": "x"} | change, "bad")
    store = ExecutionStore(world.env.state)
    worker = world.env.worker
    payload = body | {"provider_id": f"container-{identity.operation_id}", "exit_code": 1}
    with pytest.raises(StoreError):
        store.report_execution_observation(
            worker, identity.lease_id, "bad-store", canonical_digest(payload), payload
        )


def test_absence_from_another_worker_is_not_found(tmp_path):
    """Only the worker that held the lease may report for it (tenant and principal scoped)."""
    world, cid = _world(tmp_path)
    attempt, grant_dir = _gave_up_while_running(world, cid)
    lapse(world, attempt["grant_id"])
    identity = _identity(grant_dir)
    other = dict(world.env.worker, id="principal_other")
    payload = {
        "lease_id": identity.lease_id,
        "expected_epoch": identity.epoch,
        "operation_id": identity.operation_id,
        "observed_state": "exited",
        "observed_at": datetime.now(UTC).isoformat(),
        "provider_id": f"container-{identity.operation_id}",
        "termination_confirmed": True,
        "termination_evidence": "container_absent",
    }
    with pytest.raises(StoreError):
        ExecutionStore(world.env.state).report_execution_observation(
            other, identity.lease_id, "other-worker", canonical_digest(payload), payload
        )
    assert _observations(world, attempt["grant_id"])[-1][0] == "running"


# -- (c) the guardian's confirmed exact-group stop (Salad) ------------------------------------

GROUP = str(uuid.uuid4())


class GuardianControl(FakeControl):
    def __init__(self, stop):
        super().__init__()
        self.stop = stop
        self.state = dict(self.state, provider_id=GROUP)
        self.reported = []
        self.report_error = None

    def guardian_stop(self, route_id):
        self._c("guardian_stop", route_id)
        return self.stop

    def report_group_stopped(self, lease_id, epoch, operation_id, provider_id, observed_at):
        self._c("report_group_stopped", lease_id, epoch, operation_id, provider_id)
        if self.report_error:
            raise self.report_error
        self.reported.append(observed_at)


def _executing(tmp_path, stop):
    h = Harness(tmp_path, GuardianControl(stop))
    h.control.items = [item("executing")]
    (tmp_path / "work" / "route_1").mkdir(parents=True, mode=0o700)  # it was launched here
    return h


@pytest.mark.parametrize(
    "stop",
    [
        {"status": "reconciled", "reason": RECONCILE_PROVIDER, "provider_id": GROUP},
        {"status": "stopped", "reason": "exact_terminal_and_sealed", "provider_id": GROUP},
        # The guardian keeps groups in their canonical hex spelling.
        {"status": "reconciled", "reason": RECONCILE_PROVIDER, "provider_id": uuid.UUID(GROUP).hex},
    ],
)
def test_a_guardian_confirmed_group_stop_is_reported_once(tmp_path, stop):
    h = _executing(tmp_path, stop)
    actions = h.executor.run_once()
    assert ("group_stop_reported", stop["status"]) in [(a["action"], a["detail"]) for a in actions]
    assert [c for c in h.control.calls if c[0] == "report_group_stopped"] == [
        ("report_group_stopped", "lease_1", 3, item("executing")["operation_id"], GROUP)
    ]
    assert "record_outcome" not in h.names() and h.runs == []  # no outcome is invented
    marker = json.loads((tmp_path / "work" / "route_1" / GROUP_STOP_MARKER).read_text())
    assert marker["reported"] is True and marker["provider_id"] == GROUP
    h.executor.run_once()
    assert h.names().count("report_group_stopped") == 1


@pytest.mark.parametrize(
    ("stop", "reason"),
    [
        (None, "guardian_status_unavailable"),
        ({"status": "unknown", "reason": "allocation_changed", "provider_id": GROUP}, None),
        ({"status": "armed", "reason": "armed", "provider_id": GROUP}, None),
        ({"status": "reconciled", "reason": RECONCILE_OPERATOR, "provider_id": GROUP}, None),
        ({"status": "stopped", "reason": "x", "provider_id": str(uuid.uuid4())}, None),
        ({"status": "stopped", "reason": "x"}, "guardian_group_unrecorded"),
    ],
)
def test_anything_but_the_exact_group_stopped_holds(tmp_path, stop, reason):
    h = _executing(tmp_path, stop)
    actions = h.executor.run_once()
    held = [a["detail"] for a in actions if a["action"] == "group_stop_held"]
    assert len(held) == 1 and (reason is None or held[0] == reason)
    assert held[0] == group_stop_hold_reason(stop, GROUP)
    assert "report_group_stopped" not in h.names()


def test_a_route_without_a_recorded_group_holds(tmp_path):
    h = _executing(tmp_path, {"status": "stopped", "reason": "x", "provider_id": GROUP})
    h.control.state = dict(h.control.state, provider_id=None)
    actions = h.executor.run_once()
    assert ("group_stop_held", "group_identity_unrecorded") in [
        (a["action"], a["detail"]) for a in actions
    ]
    assert "guardian_stop" not in h.names() and "report_group_stopped" not in h.names()


def test_a_failed_group_stop_report_replays_the_same_request_and_is_bounded(tmp_path):
    stop = {"status": "reconciled", "reason": RECONCILE_PROVIDER, "provider_id": GROUP}
    h = _executing(tmp_path, stop)
    h.control.report_error = RuntimeError("control down")
    for _ in range(7):
        h.executor.run_once()
    assert h.names().count("report_group_stopped") == 5  # bounded, then given up
    assert h.names().count("guardian_stop") == 1  # the time is fixed once, so retries replay
    marker = json.loads((tmp_path / "work" / "route_1" / GROUP_STOP_MARKER).read_text())
    assert marker["reported"] is False


def test_control_accepts_group_stop_only_for_the_routes_recorded_group(tmp_path):
    """Control-side match of ``provider_group_stopped`` against the route's recorded group."""
    world, cid = _world(tmp_path)
    attempt, grant_dir = _gave_up_while_running(world, cid)
    identity = _identity(grant_dir)
    store = ExecutionStore(world.env.state)
    worker = world.env.worker

    def report(group, key):
        payload = {
            "lease_id": identity.lease_id,
            "expected_epoch": identity.epoch,
            "operation_id": identity.operation_id,
            "observed_state": "exited",
            "observed_at": datetime.now(UTC).isoformat(),
            "provider_id": group,
            "termination_confirmed": True,
            "termination_evidence": "provider_group_stopped",
        }
        return store.report_execution_observation(
            worker, identity.lease_id, key, canonical_digest(payload), payload
        )

    recorded = f"container-{identity.operation_id}"
    with pytest.raises(Conflict, match="provider group"):
        report(recorded, "no-route")  # no route recorded for this operation
    grant = _grant(world, attempt["grant_id"])
    for route, group in (("route_a", recorded), ("route_a", None)):
        world.env.execute(
            """INSERT INTO events (org_id, project_id, task_id, event_type, resource_revision,
            actor_principal_id, created_at, correlation_id, causation_id, payload_json)
            VALUES (?, ?, ?, 'remote_execution.state', 1, ?, ?, NULL, NULL, ?)""",
            worker["org_id"],
            grant["project_id"],
            grant["task_id"],
            worker["id"],
            datetime.now(UTC).isoformat(),
            json.dumps(
                {
                    "id": route,
                    "provider_id": group,
                    "proposal": {"operation_id": identity.operation_id},
                }
            ),
        )
    with pytest.raises(Conflict, match="provider group"):
        report(recorded, "newest-route-state-has-none")  # only the newest state per route counts
    world.env.execute(
        """UPDATE events SET payload_json=json_set(payload_json,'$.provider_id',?)
        WHERE event_type='remote_execution.state'""",
        recorded,
    )
    with pytest.raises(Conflict):  # the operation's recorded identity refuses it first
        report("group-other", "other-group")
    status, _ = report(recorded, "matching")
    assert status == 201
    assert _observations(world, attempt["grant_id"])[-1] == ("exited", 1, recorded)
