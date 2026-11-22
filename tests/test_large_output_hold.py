"""An output registered and finished minutes after the exit keeps its attempt open (ADR 0086)."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from test_campaign_exited_resolve import _exit_without_receipt, _world
from test_exited_resolve_uploads import (  # noqa: F401 - fixtures
    CHUNK,
    DATA,
    _assess,
    _later,
    _put,
    _setup,
    _t,
    _tiny,
)

from tessera_control import campaign_exited_resolve as cer
from tessera_control.artifacts import ArtifactStore
from tessera_control.execution import ExecutionStore
from tessera_control.execution_run_receipts import argv_digest
from tessera_control.store import canonical_digest


def _status(world, attempt):
    row = world.row(
        "SELECT status, failure_class FROM campaign_stage_attempts WHERE id=?", attempt["id"]
    )
    grant = world.row("SELECT status FROM execution_grants WHERE id=?", attempt["grant_id"])
    return row["status"], row["failure_class"], grant["status"]


def test_an_expired_grant_does_not_close_the_attempt_before_the_worker_registers_its_upload(
    tmp_path,
):
    # The exit is reported first; the worker then hashes its output and registers the upload.
    world, attempt, reach, lease, upload_id = _setup(tmp_path)
    world.env.execute("DELETE FROM snapshot_chunk_uploads WHERE id=?", upload_id)  # not begun yet
    world.runner.tick()
    assert _status(world, attempt)[1:] == (None, "active")  # inside the grace after the exit
    _later(world, 10)
    world.runner.tick()  # nothing was ever registered: the grace is bounded
    assert _status(world, attempt)[1] == cer.EXIT_CONFIRMED_UNRESOLVED


def _expire_just_after_the_exit(world, attempt):
    """The grant and lease run out a second after control recorded the exit."""
    seen = world.row(
        "SELECT recorded_at FROM execution_observations WHERE grant_id=? "
        "AND observed_state='exited' ORDER BY rowid LIMIT 1",
        attempt["grant_id"],
    )
    recorded = datetime.fromisoformat(seen["recorded_at"].replace("Z", "+00:00"))
    expiry = (recorded + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    world.env.execute(
        "UPDATE execution_grants SET expires_at=? WHERE id=?", expiry, attempt["grant_id"]
    )
    world.env.execute(
        "UPDATE execution_leases SET expires_at=? WHERE grant_id=?", expiry, attempt["grant_id"]
    )


def _exited(world, campaign):
    """The exit is reported while the worker's later posts are still lost; then the link returns."""
    original = world.channel.post
    attempt = _exit_without_receipt(world, campaign["id"])
    world.channel.post = original
    return attempt


def _receipt_body(world, attempt, lease, manifest, *, finished_after_exit=0.0):
    """A run receipt as the worker posts it, built from control's own records."""
    gid = attempt["grant_id"]
    operation = world.row("SELECT * FROM external_operations WHERE grant_id=?", gid)
    with world.runner.store.runner_connection() as connection:
        grant = connection.execute("SELECT * FROM execution_grants WHERE id=?", (gid,)).fetchone()
        plan, _ = ArtifactStore._plan(connection, grant)
    exited = world.row(
        "SELECT recorded_at FROM execution_observations WHERE grant_id=? "
        "AND observed_state='exited' ORDER BY rowid LIMIT 1",
        gid,
    )
    recorded = datetime.fromisoformat(exited["recorded_at"].replace("Z", "+00:00"))
    started = datetime.fromisoformat(lease["issued_at"].replace("Z", "+00:00"))
    finished = recorded + timedelta(seconds=finished_after_exit)
    command = argv_digest(plan["execution_spec"]["argv"])
    provider_id = operation["provider_id"] or "container-" + operation["operation_id"]
    payload = {
        "schema": "tessera.run-receipt.docker-local-cpu.v1",
        "operation_id": operation["operation_id"],
        "plan_digest": grant["plan_digest"],
        "exit_code": 0,
    }
    body = {
        "lease_id": lease["id"],
        "expected_epoch": lease["epoch"],
        "provider_id": provider_id,
        "command_digest": command,
        "exit_code": 0,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "output_digest": manifest["manifest_digest"],
        "receipt_payload": payload,
        "receipt_digest": canonical_digest(payload),
    }
    return operation["operation_id"], body


def _late_output(world, attempt, monkeypatch, payload, *, registered_after=None):
    """The live timeline up to the manifest: exit, expiry a second later, upload 10 min after."""
    reach = world.channel.post
    gid = attempt["grant_id"]
    lease = world.row("SELECT * FROM execution_leases WHERE grant_id=?", gid)
    operation = world.row("SELECT operation_id FROM external_operations WHERE grant_id=?", gid)
    base = f"/worker/leases/{lease['id']}/large-artifact-uploads"
    started = reach(
        base,
        {
            "expected_epoch": lease["epoch"],
            "operation_id": operation["operation_id"],
            "relative_path": "data.json",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        },
        "begin-large",
    )
    upload_id = started["id"]
    if registered_after is not None:  # control saw the upload begin this long after the exit
        exited = world.row(
            "SELECT recorded_at FROM execution_observations WHERE grant_id=? "
            "AND observed_state='exited' ORDER BY rowid LIMIT 1",
            gid,
        )
        began = (
            datetime.fromisoformat(exited["recorded_at"].replace("Z", "+00:00")) + registered_after
        )
        world.env.execute(
            "UPDATE snapshot_chunk_uploads SET created_at=? WHERE id=?",
            began.isoformat().replace("+00:00", "Z"),
            upload_id,
        )
    _expire_just_after_the_exit(world, attempt)
    minutes = 10
    for store in (ArtifactStore, ExecutionStore):  # control's clock moves on with the runner's
        monkeypatch.setattr(
            store, "_now", lambda self: datetime.now(UTC) + timedelta(minutes=minutes)
        )
    _later(world, minutes)
    for seq in range(-(-len(payload) // CHUNK)):
        part = payload[seq * CHUNK : (seq + 1) * CHUNK]
        reach(
            f"{base}/{upload_id}/chunks/{seq}",
            {
                "content_base64": base64.b64encode(part).decode(),
                "sha256": hashlib.sha256(part).hexdigest(),
            },
            f"put-{seq}",
        )
        world.runner.tick()
        assert _status(world, attempt)[1:] == (None, "active")  # held while the bytes arrive
    done = reach(f"{base}/{upload_id}/complete", {}, "done")
    assert done["status"] == "quarantined" and done["late"] is (registered_after is not None)
    manifest = reach(
        f"/worker/leases/{lease['id']}/artifact-manifest",
        {
            "expected_epoch": lease["epoch"],
            "operation_id": operation["operation_id"],
            "artifacts": [
                {
                    "id": upload_id,
                    "relative_path": "data.json",
                    "sha256": done["sha256"],
                    "size": len(payload),
                }
            ],
        },
        "manifest",
    )
    assert manifest["late"] is (registered_after is not None)
    return lease, manifest


PAYLOAD = json.dumps({"score": 3, "pad": "x" * 600}).encode()  # data.json, over the tiny cap


def test_an_output_finishing_minutes_after_the_grant_expired_is_not_late_and_the_attempt_succeeds(
    tmp_path, monkeypatch
):
    """The live timeline, receipt included: it is posted through the worker's own endpoint."""
    world, campaign = _world(tmp_path, retry={"max_attempts": 1})
    attempt = _exited(world, campaign)
    lease, manifest = _late_output(world, attempt, monkeypatch, PAYLOAD)
    operation_id, body = _receipt_body(world, attempt, lease, manifest)
    posted = world.channel.post(
        f"/worker/external-operations/{operation_id}/run-receipt", body, "run-receipt"
    )
    assert posted["exit_code"] == 0
    for _ in range(6):
        world.runner.tick()
    final = world.row("SELECT * FROM campaign_stage_attempts WHERE id=?", attempt["id"])
    assert final["status"] == "succeeded", dict(final)
    assert final["manifest_id"] == manifest["id"] and final["run_receipt_digest"]


def test_a_worker_that_reports_its_exit_and_registers_its_output_late_gets_it_marked_late(
    tmp_path, monkeypatch
):
    """The consistent hostile case, enforced: registration time is control's, not the worker's."""
    world, campaign = _world(tmp_path, retry={"max_attempts": 1})
    attempt = _exited(world, campaign)
    # The upload began three minutes after control recorded the exit: past the 120 s grace.
    _late_output(world, attempt, monkeypatch, PAYLOAD, registered_after=timedelta(minutes=3))


def test_a_receipt_for_a_run_that_finished_after_its_reported_exit_is_refused(
    tmp_path, monkeypatch
):
    """A worker that reports its exit early and keeps computing gets no receipt (ADR 0086)."""
    world, campaign = _world(tmp_path, retry={"max_attempts": 1})
    attempt = _exited(world, campaign)
    lease, manifest = _late_output(world, attempt, monkeypatch, PAYLOAD)
    operation_id, body = _receipt_body(world, attempt, lease, manifest, finished_after_exit=30)
    with pytest.raises(ConnectionError, match=r"409.*Run receipt finished after the recorded exit"):
        world.channel.post(
            f"/worker/external-operations/{operation_id}/run-receipt", body, "run-receipt"
        )
    assert world.row("SELECT count(*) AS n FROM execution_run_receipts")["n"] == 0


def test_a_receipt_is_refused_when_control_saw_the_container_running_after_its_exit(
    tmp_path, monkeypatch
):
    """The bound does not rest on the worker's finish time: a later observation counts."""
    world, campaign = _world(tmp_path, retry={"max_attempts": 1})
    attempt = _exited(world, campaign)
    lease, manifest = _late_output(world, attempt, monkeypatch, PAYLOAD)
    exited = world.row(
        "SELECT * FROM execution_observations WHERE grant_id=? AND observed_state='exited' "
        "ORDER BY rowid LIMIT 1",
        attempt["grant_id"],
    )
    later = datetime.fromisoformat(exited["observed_at"].replace("Z", "+00:00")) + timedelta(
        minutes=1
    )
    world.env.execute(
        "INSERT INTO execution_observations (id, grant_id, lease_id, worker_principal_id, epoch, "
        "desired_state, observed_state, observed_at, provider_id, termination_confirmed, "
        "recorded_at, operation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        "observation_later_running",
        exited["grant_id"],
        exited["lease_id"],
        exited["worker_principal_id"],
        exited["epoch"],
        exited["desired_state"],
        "running",
        later.isoformat().replace("+00:00", "Z"),
        exited["provider_id"],
        0,
        later.isoformat().replace("+00:00", "Z"),
        exited["operation_id"],
    )
    operation_id, body = _receipt_body(world, attempt, lease, manifest)  # an honest finished_at
    with pytest.raises(ConnectionError, match=r"409.*contradicts a later observation"):
        world.channel.post(
            f"/worker/external-operations/{operation_id}/run-receipt", body, "run-receipt"
        )


def _contradict(world, attempt, source):
    """Control's own record of the operation says the container was running at/after its exit."""
    exited = world.row(
        "SELECT * FROM execution_observations WHERE grant_id=? AND observed_state='exited' "
        "ORDER BY rowid LIMIT 1",
        attempt["grant_id"],
    )
    at = exited["observed_at"]
    later = (
        (datetime.fromisoformat(at.replace("Z", "+00:00")) + timedelta(minutes=1))
        .isoformat()
        .replace("+00:00", "Z")
    )
    operation = world.row(
        "SELECT * FROM external_operations WHERE operation_id=?", exited["operation_id"]
    )
    now = later
    if source == "worker_same_time":
        world.env.execute(
            "INSERT INTO execution_observations (id, grant_id, lease_id, worker_principal_id, "
            "epoch, desired_state, observed_state, observed_at, provider_id, "
            "termination_confirmed, recorded_at, operation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            "observation_same_time",
            exited["grant_id"],
            exited["lease_id"],
            exited["worker_principal_id"],
            exited["epoch"],
            exited["desired_state"],
            "running",
            at,  # the same instant as the confirmed exit
            exited["provider_id"],
            0,
            at,
            exited["operation_id"],
        )
    elif source == "provider_receipt":
        world.env.execute(
            "INSERT INTO external_operation_receipts (operation_id, provider_kind, "
            "provider_version, provider_id, receipt_payload_json, receipt_digest, observed_state, "
            "observed_at, termination_confirmed, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            operation["operation_id"],
            "fake",
            "v",
            "provider-observer",
            "{}",
            "a" * 64,
            "running",
            later,
            0,
            now,
            now,
        )
    else:  # a recovery observer's lookup
        world.env.execute(
            "INSERT INTO execution_recovery_reconciliations (id, org_id, project_id, task_id, "
            "grant_id, lease_id, original_worker_principal_id, epoch, operation_id, "
            "identity_loss_reason, state, revision, cancellation_requested, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            "recovery_1",
            operation["org_id"],
            operation["project_id"],
            operation["task_id"],
            operation["grant_id"],
            operation["lease_id"],
            operation["worker_principal_id"],
            operation["epoch"],
            operation["operation_id"],
            "worker_retired",
            "reconciliation_required",
            1,
            0,
            now,
            now,
        )
        world.env.execute(
            "INSERT INTO execution_recovery_observations (id, recovery_id, observer_principal_id, "
            "action, evidence_reference, evidence_digest, provider_id, observed_state, "
            "observed_at, termination_confirmed, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            "recovery_observation_1",
            "recovery_1",
            operation["worker_principal_id"],
            "lookup",
            "evidence",
            "b" * 64,
            "provider-observer",
            "running",
            later,
            0,
            now,
        )


@pytest.mark.parametrize("source", ["worker_same_time", "provider_receipt", "recovery_observer"])
def test_every_source_of_a_contradicting_observation_refuses_the_receipt(
    tmp_path, monkeypatch, source
):
    """The check reads control's whole reconciliation, not only the worker's own reports."""
    world, campaign = _world(tmp_path, retry={"max_attempts": 1})
    attempt = _exited(world, campaign)
    lease, manifest = _late_output(world, attempt, monkeypatch, PAYLOAD)
    _contradict(world, attempt, source)
    operation_id, body = _receipt_body(world, attempt, lease, manifest)
    # A recovery reconciliation fences the original worker before the reconciliation is read, so
    # that source is refused by the fence (a stronger refusal), not by the contradiction check.
    refusal = "fenced" if source == "recovery_observer" else "contradicts a later observation"
    with pytest.raises(ConnectionError, match=rf"409.*{refusal}"):
        world.channel.post(
            f"/worker/external-operations/{operation_id}/run-receipt", body, "run-receipt"
        )
    assert world.row("SELECT count(*) AS n FROM execution_run_receipts")["n"] == 0


def test_an_honest_worker_that_reports_unknown_after_its_exit_is_refused_a_receipt(
    tmp_path, monkeypatch
):
    """Fail-closed by design (ADR 0086): a later unknown observation also refuses the receipt."""
    world, campaign = _world(tmp_path, retry={"max_attempts": 1})
    attempt = _exited(world, campaign)
    lease, manifest = _late_output(world, attempt, monkeypatch, PAYLOAD)
    exited = world.row(
        "SELECT * FROM execution_observations WHERE grant_id=? AND observed_state='exited' "
        "ORDER BY rowid LIMIT 1",
        attempt["grant_id"],
    )
    later = (
        (
            datetime.fromisoformat(exited["observed_at"].replace("Z", "+00:00"))
            + timedelta(minutes=1)
        )
        .isoformat()
        .replace("+00:00", "Z")
    )
    world.env.execute(
        "INSERT INTO execution_observations (id, grant_id, lease_id, worker_principal_id, epoch, "
        "desired_state, observed_state, observed_at, provider_id, termination_confirmed, "
        "recorded_at, operation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        "observation_unknown",
        exited["grant_id"],
        exited["lease_id"],
        exited["worker_principal_id"],
        exited["epoch"],
        exited["desired_state"],
        "unknown",
        later,
        exited["provider_id"],
        0,
        later,
        exited["operation_id"],
    )
    operation_id, body = _receipt_body(world, attempt, lease, manifest)
    with pytest.raises(ConnectionError, match=r"409.*contradicts a later observation"):
        world.channel.post(
            f"/worker/external-operations/{operation_id}/run-receipt", body, "run-receipt"
        )


def test_the_grace_after_an_exit_holds_even_past_the_approval_window(tmp_path):
    """ADR 0086: the 120 s grace is checked before the approval window; it is bounded."""
    world, attempt, reach, lease, upload_id = _setup(tmp_path)
    past_approval = datetime.now(UTC) - timedelta(minutes=5)
    progress = cer.UploadProgress()
    assert _assess(world, attempt["id"], progress, at=_t(1), approval_expires=past_approval) is None
    verdict = _assess(world, attempt["id"], progress, at=_t(3), approval_expires=past_approval)
    assert verdict is not None  # past the grace the approval window ends the hold


class _Late:
    """ArtifactStore._late on a bare in-memory database: the rule needs no campaign tables."""

    EXIT = datetime(2030, 1, 1, tzinfo=UTC)

    @staticmethod
    def stamp(moment):
        return moment.isoformat().replace("+00:00", "Z")

    def check(self, *, now, grant=None, lease=None, observations=None, **late):
        import sqlite3

        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE tasks (id, revision, status, current_plan_digest)")
        db.execute("INSERT INTO tasks VALUES ('t', 1, 'approved', 'p')")
        db.execute(
            "CREATE TABLE execution_observations (lease_id, epoch, observed_state, "
            "termination_confirmed, recorded_at)"
        )
        expiry = self.stamp(self.EXIT + timedelta(seconds=1))
        base_grant = {
            "status": "active",
            "expires_at": expiry,
            "epoch": 1,
            "task_id": "t",
            "task_revision": 1,
            "plan_digest": "p",
        }
        base_lease = {"id": "l", "epoch": 1, "expires_at": expiry}
        rows = (
            [("l", 1, "exited", 1, self.stamp(self.EXIT))] if observations is None else observations
        )
        db.executemany("INSERT INTO execution_observations VALUES (?,?,?,?,?)", rows)
        store = ArtifactStore.__new__(ArtifactStore)
        store._now = lambda: now
        late.setdefault("registered_at", self.stamp(self.EXIT + timedelta(seconds=5)))
        return store._late(db, base_grant | (grant or {}), base_lease | (lease or {}), **late)


@pytest.mark.parametrize(
    ("case", "late"),
    [
        ("minutes_after_expiry", 0),  # a grant no runner is holding: the run's own exit decides
        ("two_hours_and_a_minute", 1),  # past the ceiling after the recorded exit
        ("reconciled_grant", 1),
        ("cancel_requested_grant", 1),
        ("no_exit_recorded", 1),
        ("exit_not_confirmed", 1),
        ("running_again_after_exit", 1),
        ("exit_after_expiry", 1),
        ("epoch_mismatch", 1),
        ("plan_changed", 1),
        ("registered_inside_the_grace", 0),
        ("registered_after_the_grace", 1),  # a worker that kept computing after its exit report
        ("single_request_arriving_after_the_grace", 1),  # no upload to register: arrival counts
        ("manifest_follows_its_artifacts", 0),
    ],
)
def test_the_not_late_rule_covers_every_grant_and_is_bounded(case, late):
    probe = _Late()
    exit_at = probe.EXIT
    now = exit_at + timedelta(minutes=10)
    kwargs: dict = {}
    if case == "two_hours_and_a_minute":
        now = exit_at + timedelta(hours=2, minutes=1)
    elif case == "reconciled_grant":
        kwargs["grant"] = {"status": "reconciled"}
    elif case == "cancel_requested_grant":
        kwargs["grant"] = {"status": "cancel_requested"}
    elif case == "no_exit_recorded":
        kwargs["observations"] = []
    elif case == "exit_not_confirmed":
        kwargs["observations"] = [("l", 1, "exited", 0, probe.stamp(exit_at))]
    elif case == "running_again_after_exit":
        kwargs["observations"] = [
            ("l", 1, "exited", 1, probe.stamp(exit_at)),
            ("l", 1, "running", 0, probe.stamp(exit_at + timedelta(seconds=30))),
        ]
    elif case == "exit_after_expiry":
        kwargs["observations"] = [
            ("l", 1, "exited", 1, probe.stamp(exit_at + timedelta(seconds=5)))
        ]
    elif case == "epoch_mismatch":
        kwargs["grant"] = {"epoch": 2}
    elif case == "plan_changed":
        kwargs["grant"] = {"plan_digest": "other"}
    elif case == "registered_inside_the_grace":
        kwargs["registered_at"] = probe.stamp(exit_at + timedelta(seconds=119))
    elif case == "registered_after_the_grace":
        kwargs["registered_at"] = probe.stamp(exit_at + timedelta(seconds=121))
    elif case == "single_request_arriving_after_the_grace":
        kwargs["registered_at"] = None  # the request arrives at ``now``, minutes after the exit
    elif case == "manifest_follows_its_artifacts":
        kwargs["registered_at"], kwargs["manifest"] = None, True
    assert probe.check(now=now, **kwargs) == late
