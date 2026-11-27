"""Salad campaign executor: fakes only (no subprocess, no network, no live Salad)."""

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from test_salad_campaign_profiles import (
    DATA,
    IMAGE,
    OPERATION,
    contract_data,
    profile_data,
)

from tessera_control import campaign_salad_executor as module
from tessera_control.campaign_salad_executor import (
    CONFIG_SCHEMA,
    ExecutorConfig,
    RouteTimeout,
    SaladCampaignExecutor,
    load_config,
    subprocess_route_runner,
)
from tessera_control.salad_campaign_profiles import SaladCampaignProfile, derive_selection
from tessera_control.salad_live_check import installed_source_pin
from tessera_control.salad_workload_contract import WorkloadContractV1
from tessera_control.store import Conflict

WORKER = "principal_w"
PLAN = {"budget_cents": 100, "max_duration_seconds": 600}
OBSERVED = "2026-11-19T00:00:00Z"
OUTPUT = b'{"result": 1}'


def custody():
    return dict(
        host="custody.example-tailnet.ts.net",
        port=443,
        token_file="/x/t",
        ca_file="/x/c",
        certificate_sha256="c" * 64,
    )


def config_for(tmp_path):
    work = tmp_path / "work"
    work.mkdir(mode=0o700, parents=True)
    return ExecutorConfig.model_validate(
        {
            "schema": CONFIG_SCHEMA,
            "state_dir": "/x/state",
            "profiles_file": "/x/profiles.json",
            "profile": "gpu-main",
            "worker_credential_file": "/x/w",
            "credential_directory": "/x/cred",
            "guardian_socket": "/x/g.sock",
            "guardian_uid": 1000,
            "custody": custody(),
            "work_directory": str(work),
            "source_revision": "a" * 40 + "+sha256:" + "b" * 64,
            "poll_seconds": 2,
        }
    )


PROFILE = SaladCampaignProfile.model_validate(profile_data())
CONTRACT = WorkloadContractV1.model_validate(contract_data())


def selection(profile=PROFILE):
    return derive_selection(
        profile,
        CONTRACT,
        operation_id=OPERATION,
        epoch=3,
        snapshot_digest=DATA,
        image=IMAGE,
        max_cost_cents=50,
        plan_max_seconds=PLAN["max_duration_seconds"],
    )


def route_state(**over):
    value = dict(
        org_id="org_1",
        project_id="project_1",
        task_id="task_1",
        proposal_digest="p" * 64,
        approved_by="human_1",
        desired="start",
        effects=[],
        provider_id="group-1",
        termination_confirmed=True,
        reallocated=False,
        receipts=[
            {"provenance": "synthetic", "observed_at": "2026-11-18T00:00:00Z"},
            {"provenance": "provider", "observed_at": OBSERVED},
        ],
        proposal=dict(
            operation_id=OPERATION, epoch=3, selection=selection().model_dump(mode="json")
        ),
    )
    value.update(over)
    return value


class FakeControl:
    def __init__(self, *, pending=("grant_1",), plan=None):
        self.pending, self.plan_value = set(pending), plan or dict(PLAN)
        self.items = []
        self.state = route_state()
        self.calls = []
        self.renew_error = None
        self.uploaded = None
        self.final = None
        self.final_error = None
        self.heartbeat_error = None
        # None: the registry never refuses; False: the worker's last heartbeat is stale and
        # lease authority is refused (as the store's ``require_ready_worker`` does) until one.
        self.worker_fresh = None
        self.package_error = None
        self.pending_error = None
        self.rejections = []
        self.rejection_error = None
        self.acquire_error = None

    def record_rejection(self, attempt_id, step, error):
        self.rejections.append((attempt_id, step, type(error).__name__))
        if self.rejection_error:
            raise self.rejection_error

    def _c(self, name, *args):
        self.calls.append((name, *args))

    def work(self):
        return list(self.items)

    def pending_grants(self):
        if self.pending_error:
            raise self.pending_error
        return set(self.pending)

    def plan(self, grant_id):
        return self.plan_value

    def acquire_lease(self, grant_id, key):
        self._c("acquire", grant_id)
        if self.acquire_error:
            raise self.acquire_error
        if self.worker_fresh is False:
            raise Conflict("Worker is disconnected and cannot receive lease authority.")
        return dict(id="lease_1", epoch=3)

    def package(self, lease_id, epoch, admitted_attempt_id, key):
        self._c("package", lease_id, epoch, admitted_attempt_id)
        if self.package_error:
            raise self.package_error
        return dict(package=dict(operation_id=OPERATION), package_digest="d" * 64)

    def record_package(self, attempt_id, payload):
        self._c("record_package", attempt_id, payload)

    def operation(self, operation_id):
        return dict(lease_id="lease_1", epoch=3, grant_id="grant_1")

    def heartbeat(self):
        self._c("heartbeat")
        if self.heartbeat_error:
            raise self.heartbeat_error
        if self.worker_fresh is False:
            self.worker_fresh = True

    def renew(self, lease_id, epoch):
        self._c("renew", lease_id, epoch)
        if self.renew_error:
            raise self.renew_error

    def route(self, route_id):
        return self.state

    def mark_executing(self, attempt_id):
        self._c("mark_executing", attempt_id)

    def upload_outputs(self, lease_id, epoch, operation_id, directory, files):
        self._c("upload", lease_id, epoch, operation_id, [f["name"] for f in files])
        self.uploaded = {f["name"]: (Path(directory) / f["name"]).read_bytes() for f in files}
        return dict(id="manifest_1")

    def report_exited(self, lease_id, epoch, operation_id, provider_id, observed_at):
        self._c("report_exited", lease_id, epoch, operation_id, provider_id, observed_at)

    def record_outcome(self, attempt_id, payload):
        self._c("record_outcome", attempt_id, payload)

    def guardian_final(self, route_id):
        self._c("guardian_final", route_id)
        if self.final_error:
            raise self.final_error
        return self.final


def item(phase, **over):
    value = dict(
        attempt_id="att_1",
        phase=phase,
        route_id="route_1",
        operation_id=OPERATION,
        grant_id="grant_1",
        admitted_attempt_id="adm_1",
        profile="gpu-main",
    )
    value.update(over)
    return value


RECEIPT = {
    "schema": "tessera.salad.live-check.v1",
    "status": "passed",
    "reason": "verified_journey",
    "stop_confirmed": True,
    "financial": {"estimated_exposure_cents": "3.5", "actual_billed_cost_cents": None},
}


class Harness:
    def __init__(self, tmp_path, control=None, *, receipt=RECEIPT, profile=PROFILE, runner=None):
        self.control = control or FakeControl()
        self.runs = []
        self.imports = []
        self.import_profiles = []
        self.config = config_for(tmp_path)
        self.receipt = receipt

        def default_runner(config_path, receipt_dir, timeout):
            mode = stat.S_IMODE(os.stat(config_path).st_mode)
            document = json.loads(Path(config_path).read_text())
            self.runs.append(
                dict(mode=mode, timeout=timeout, document=document, receipts=Path(receipt_dir))
            )
            assert not list(Path(receipt_dir).iterdir())
            return self.receipt

        def importer(route_id, destination, profile):
            self.import_profiles.append(profile.name)
            self.imports.append(route_id)
            (Path(destination) / "output.json").write_bytes(OUTPUT)
            return [
                dict(
                    name="output.json", sha256=hashlib.sha256(OUTPUT).hexdigest(), bytes=len(OUTPUT)
                )
            ]

        self.sleeps: list[float] = []
        self.executor = SaladCampaignExecutor(
            self.config,
            profile,
            self.control,
            worker_principal_id=WORKER,
            route_runner=runner or default_runner,
            importer=importer,
            sleep=self.sleeps.append,
        )

    def names(self):
        return [c[0] for c in self.control.calls]


def test_lease_package_and_record_for_a_pending_grant(tmp_path):
    h = Harness(tmp_path)
    h.control.items = [item("awaiting_lease", route_id=None, operation_id=None)]
    actions = h.executor.run_once()
    assert [a["action"] for a in actions] == ["packaged"]
    assert h.control.calls == [
        ("heartbeat",),
        ("acquire", "grant_1"),
        ("package", "lease_1", 3, "adm_1"),
        (
            "record_package",
            "att_1",
            dict(operation_id=OPERATION, lease_id="lease_1", epoch=3, package_digest="d" * 64),
        ),
    ]


def test_a_stale_worker_heartbeats_once_before_its_first_lease(tmp_path):
    h = Harness(tmp_path)
    h.control.worker_fresh = False
    h.control.items = [
        item("awaiting_lease", route_id=None, operation_id=None),
        item("awaiting_lease", attempt_id="att_2", route_id=None, operation_id=None),
    ]
    assert [a["action"] for a in h.executor.run_once()] == ["packaged", "packaged"]
    # One heartbeat per pass, sent before lease authority is asked for.
    assert h.names() == ["heartbeat", "acquire", "package", "record_package"] + [
        "acquire",
        "package",
        "record_package",
    ]


def test_a_heartbeat_failure_is_noted_and_does_not_end_the_pass(tmp_path):
    h = Harness(tmp_path)
    h.control.heartbeat_error = RuntimeError("registry /private/path down")
    h.control.items = [item("awaiting_lease", route_id=None, operation_id=None)]
    assert [(a["action"], a["detail"]) for a in h.executor.run_once()] == [
        ("heartbeat_failed", "RuntimeError"),
        ("packaged", OPERATION),
    ]
    # A stale worker is still refused by the store; the refusal is noted, nothing faked.
    h = Harness(tmp_path / "b")
    h.control.worker_fresh = False
    h.control.heartbeat_error = RuntimeError("down")
    h.control.items = [item("awaiting_lease", route_id=None, operation_id=None)]
    assert [(a["action"], a["detail"]) for a in h.executor.run_once()] == [
        ("heartbeat_failed", "RuntimeError"),
        ("error", "Conflict"),
    ]
    assert h.names() == ["heartbeat", "acquire"]
    # The next pass heartbeats again and leases.
    h.control.heartbeat_error = None
    assert [a["action"] for a in h.executor.run_once()] == ["packaged"]


def test_a_leased_attempt_keeps_its_keepalive_beside_a_new_lease(tmp_path):
    h = Harness(tmp_path)
    h.control.items = [
        item("leased"),
        item("awaiting_lease", attempt_id="att_2", route_id=None, operation_id=None),
    ]
    h.executor.run_once()
    assert h.names() == ["heartbeat", "renew", "heartbeat", "acquire", "package", "record_package"]


def test_no_lease_when_grant_is_not_pending_or_plan_budget_exceeds_profile(tmp_path):
    h = Harness(tmp_path, FakeControl(pending=()))
    h.control.items = [item("awaiting_lease")]
    assert [a["action"] for a in h.executor.run_once()] == ["not_pending"]
    h = Harness(
        tmp_path / "b",
        FakeControl(plan=dict(budget_cents=PROFILE.max_task_cents + 1, max_duration_seconds=600)),
    )
    h.control.items = [item("awaiting_lease")]
    assert [a["detail"] for a in h.executor.run_once()] == ["plan_budget_above_profile"]
    # No lease or package call; only the pass's own registry heartbeat (liveness is not leases).
    assert h.control.calls == [("heartbeat",)]


def test_happy_path_runs_imports_uploads_observes_and_records(tmp_path):
    h = Harness(tmp_path)
    h.control.final = dict(FINAL)
    h.control.items = [item("route_started")]
    actions = h.executor.run_once()
    assert [a["action"] for a in actions] == ["outcome_recorded"]
    names = h.names()
    # Lease kept alive, then marked executing strictly before the run, then the outcome.
    assert names == [
        "heartbeat",
        "renew",
        "mark_executing",
        "upload",
        "report_exited",
        "guardian_final",
        "record_outcome",
    ]
    run = h.runs[0]
    assert run["mode"] == 0o600
    assert run["timeout"] == PROFILE.route_max_seconds + module.SUBPROCESS_GRACE_SECONDS
    document = run["document"]
    assert document["schema"] == "tessera.salad.live-config.v1"
    assert document["approved_max_cost_cents"] == PLAN["budget_cents"]
    assert document["max_seconds"] == PROFILE.route_max_seconds
    assert document["route_id"] == "route_1" and document["proposal_digest"] == "p" * 64
    assert "credential" not in json.dumps(document["policy"]).replace(
        "credential-ref:salad-management", ""
    )
    # The per-route config is gone; the empty receipt directory and outputs are kept.
    route_dir = tmp_path / "work" / "route_1"
    assert not (route_dir / "live-config.json").exists()
    assert run["receipts"].is_dir() and (route_dir / "outputs" / "output.json").exists()
    assert stat.S_IMODE(os.stat(route_dir).st_mode) == 0o700
    assert h.control.uploaded == {"output.json": OUTPUT}
    calls = dict((c[0], c) for c in h.control.calls)
    assert calls["report_exited"][1:] == ("lease_1", 3, OPERATION, "group-1", OBSERVED)
    outcome = calls["record_outcome"][2]
    assert outcome == dict(
        route_id="route_1",
        live_receipt=RECEIPT,
        manifest_id="manifest_1",
        provider_id="group-1",
        estimated_exposure=RECEIPT["financial"],
        guardian_final=FINAL,
    )


def test_selection_mismatch_never_runs(tmp_path):
    other = SaladCampaignProfile.model_validate(profile_data(memory_mb=2048))
    h = Harness(tmp_path, profile=other)
    h.control.items = [item("route_started")]
    assert [a["action"] for a in h.executor.run_once()] == ["refused_selection_mismatch"]
    assert h.runs == [] and "mark_executing" not in h.names()
    assert not (tmp_path / "work" / "route_1").exists()


def test_a_public_literal_over_non_public_admission_evidence_never_runs(tmp_path):
    h = Harness(tmp_path)
    h.control.state = route_state(
        input_admission={"admission_basis": "provider_trust_policy", "provider_id": "salad"}
    )
    h.control.items = [item("route_started")]
    assert [a["action"] for a in h.executor.run_once()] == ["refused_selection_mismatch"]
    assert h.runs == []
    ok = Harness(tmp_path / "ok")
    ok.control.state = route_state(input_admission={"admission_basis": "public_only"})
    ok.control.items = [item("route_started")]
    assert "refused_selection_mismatch" not in [a["action"] for a in ok.executor.run_once()]


@pytest.mark.parametrize(
    "state",
    [
        dict(approved_by=None),
        dict(desired="stop"),
        dict(org_id="org_other"),
        dict(effects=[{"action": "create"}]),
        dict(effects=[{"action": "start"}]),
    ],
)
def test_unapproved_stopped_foreign_or_launched_routes_never_run(tmp_path, state):
    h = Harness(tmp_path)
    h.control.state = route_state(**state)
    h.control.items = [item("route_started")]
    h.executor.run_once()
    assert h.runs == [] and "mark_executing" not in h.names()


def test_timeout_records_no_outcome_and_never_reruns(tmp_path):
    def runner(config_path, receipt_dir, timeout):
        raise RouteTimeout

    h = Harness(tmp_path, runner=runner)
    h.control.items = [item("route_started")]
    assert [a["action"] for a in h.executor.run_once()] == ["route_timeout"]
    assert "record_outcome" not in h.names()
    assert not (tmp_path / "work" / "route_1" / "live-config.json").exists()
    h.control.items = [item("route_started")]
    assert [a["action"] for a in h.executor.run_once()] == ["refused_route_already_attempted"]
    assert h.names().count("mark_executing") == 1


def test_a_second_pass_never_runs_a_route_that_already_ran(tmp_path):
    h = Harness(tmp_path)
    h.control.items = [item("route_started")]
    h.executor.run_once()
    h.executor.run_once()
    assert len(h.runs) == 1 and h.names().count("record_outcome") == 1


def test_unconfirmed_stop_records_the_receipt_without_import_or_observation(tmp_path):
    receipt = dict(RECEIPT, status="inconclusive", stop_confirmed=False)
    h = Harness(tmp_path, receipt=receipt)
    h.control.items = [item("route_started")]
    h.executor.run_once()
    assert h.imports == [] and "upload" not in h.names()
    outcome = h.control.calls[-1][2]
    assert outcome["manifest_id"] is None and outcome["live_receipt"] == receipt


@pytest.mark.parametrize(
    "state",
    [dict(termination_confirmed=False), dict(reallocated=True), dict(receipts=[])],
)
def test_no_exit_observation_without_provider_confirmed_termination(tmp_path, state):
    h = Harness(tmp_path)
    h.control.state = route_state(**state)
    h.control.items = [item("route_started")]
    h.executor.run_once()
    assert "report_exited" not in h.names()
    assert "record_outcome" in h.names()


def test_corrupt_import_is_not_uploaded(tmp_path):
    h = Harness(tmp_path)
    original = h.executor.importer

    def lying(route_id, destination):
        files = original(route_id, destination)
        files[0]["sha256"] = "0" * 64
        return files

    h.executor.importer = lying
    h.control.items = [item("route_started")]
    h.executor.run_once()
    assert "upload" not in h.names()
    assert h.control.calls[-1][2]["manifest_id"] is None


def test_renewal_refusal_stops_keepalive_and_blocks_the_run(tmp_path):
    h = Harness(tmp_path)
    h.control.renew_error = Conflict("Execution lease is no longer current.")
    h.control.items = [item("route_started")]
    actions = [a["action"] for a in h.executor.run_once()]
    assert actions == ["lease_renewal_refused", "fenced_no_run"]
    assert h.runs == []
    h.executor.run_once()
    assert h.names().count("renew") == 1
    h.control.items = [item("leased")]
    h.executor.run_once()
    assert h.names().count("renew") == 1


def test_leased_attempt_is_kept_alive_each_pass(tmp_path):
    h = Harness(tmp_path)
    h.control.items = [item("leased")]
    h.executor.run_once()
    h.executor.run_once()
    assert h.names() == ["heartbeat", "renew"] * 2


@pytest.mark.parametrize(
    "phase",
    ["leased", "awaiting_review", "reviewed", "capability_ready", "route_proposed"],
)
def test_every_phase_holding_the_lease_before_the_route_is_kept_alive(tmp_path, phase):
    h = Harness(tmp_path)
    h.control.items = [item(phase, route_id=None)]
    assert h.executor.run_once() == []
    assert h.control.calls == [("heartbeat",), ("renew", "lease_1", 3)]
    assert h.runs == []


def test_a_route_started_attempt_is_kept_alive_before_it_runs(tmp_path):
    h = Harness(tmp_path)
    h.control.items = [item("route_started")]
    h.executor.run_once()
    assert h.names()[:2] == ["heartbeat", "renew"]
    assert len(h.runs) == 1


@pytest.mark.parametrize("phase", ["awaiting_review", "route_proposed"])
def test_a_refused_renewal_during_review_fences_once_and_never_runs(tmp_path, phase):
    h = Harness(tmp_path)
    h.control.renew_error = Conflict("Remote execution lease/epoch is fenced.")
    h.control.items = [item(phase, route_id=None)]
    assert [(a["action"], a["detail"]) for a in h.executor.run_once()] == [
        ("lease_renewal_refused", "Conflict")
    ]
    h.executor.run_once()
    assert h.names().count("renew") == 1
    # The fence holds when the runner moves the attempt on to a started route.
    h.control.items = [item("route_started")]
    assert [a["action"] for a in h.executor.run_once()] == ["fenced_no_run"]
    assert h.names().count("renew") == 1 and h.runs == []


def test_a_lease_held_through_a_multi_pass_review_wait_is_renewed_every_pass(tmp_path):
    h = Harness(tmp_path)
    h.control.items = [item("awaiting_lease", route_id=None, operation_id=None)]
    assert [a["action"] for a in h.executor.run_once()] == ["packaged"]
    phases = ["leased"] + ["awaiting_review"] * 4 + ["reviewed", "capability_ready"]
    phases += ["route_proposed"] * 2
    for phase in phases:
        h.control.items = [item(phase, route_id=None)]
        h.executor.run_once()
    renewals = [c for c in h.control.calls if c[0] == "renew"]
    assert renewals == [("renew", "lease_1", 3)] * len(phases)
    assert h.executor.fenced == {}
    h.control.items = [item("route_started")]
    h.executor.run_once()
    assert len(h.runs) == 1


def test_executing_attempt_after_restart_finishes_only_from_a_receipt_on_disk(tmp_path):
    h = Harness(tmp_path)
    h.control.items = [item("executing")]
    h.executor.run_once()
    assert "record_outcome" not in h.names() and h.runs == []
    receipts = tmp_path / "work" / "route_1" / "receipts"
    receipts.mkdir(parents=True, mode=0o700)
    (tmp_path / "work" / "route_1" / "outputs").mkdir(mode=0o700)
    target = receipts / module.RECEIPT_NAME
    target.write_text(json.dumps(RECEIPT))
    target.chmod(0o600)
    h.executor.run_once()
    assert h.runs == [] and h.names().count("record_outcome") == 1


def test_a_profile_for_another_worker_is_refused(tmp_path):
    with pytest.raises(ValueError):
        SaladCampaignExecutor(
            config_for(tmp_path),
            PROFILE,
            FakeControl(),
            worker_principal_id="someone_else",
            importer=lambda *a: [],
        )


def test_subprocess_runner_uses_isolated_unchanged_live_driver_and_bounds_it(tmp_path, monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(command=command, **kwargs)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    with pytest.raises(RouteTimeout):
        subprocess_route_runner(tmp_path / "c.json", tmp_path / "r", 77)
    command = seen["command"]
    assert command[1:4] == ["-I", "-m", "tessera_control.salad_live_check"]
    assert "--run" in command and seen["timeout"] == 77 and seen["check"] is False


def test_config_is_closed_and_private(tmp_path):
    tmp_path.chmod(0o700)
    for name in ("state", "cred", "work"):
        (tmp_path / name).mkdir(mode=0o700)
    for name in ("w", "t", "ca", "profiles"):
        (tmp_path / name).write_text("x")
        (tmp_path / name).chmod(0o600)
    document = {
        "schema": CONFIG_SCHEMA,
        "state_dir": str(tmp_path / "state"),
        "profiles_file": str(tmp_path / "profiles"),
        "profile": "gpu-main",
        "worker_credential_file": str(tmp_path / "w"),
        "credential_directory": str(tmp_path / "cred"),
        "guardian_socket": "/run/guardian.sock",
        "guardian_uid": 1000,
        "custody": dict(custody(), token_file=str(tmp_path / "t"), ca_file=str(tmp_path / "ca")),
        "work_directory": str(tmp_path / "work"),
        "source_revision": installed_source_pin("a" * 40),
        "poll_seconds": 5,
    }

    def write(value, mode=0o600):
        path = tmp_path / "config.json"
        path.write_text(json.dumps(value))
        path.chmod(mode)
        return path

    assert load_config(write(document)).profile == "gpu-main"
    for bad in (
        dict(document, extra=1),
        dict(document, poll_seconds=1),
        dict(document, poll_seconds=31),
        dict(document, schema="other"),
        dict(document, work_directory=str(tmp_path / "missing")),
        dict(document, custody=dict(document["custody"], port=80)),
    ):
        with pytest.raises(ValueError):
            load_config(write(bad))
    with pytest.raises(ValueError):
        load_config(write(document, mode=0o644))


FINAL = dict(
    status="stopped",
    reason="deadline",
    orphan=False,
    allocation_uncertain=False,
    escalation_pending=False,
    budget_fenced=False,
    rate_unknown=False,
    published_hourly_rate_usd="0.5",
    estimated_exposure_cents="3.5",
    actual_billed_cost_cents=None,
    observed_at="2026-11-19T00:00:00Z",
)


def test_the_guardian_final_is_read_after_the_exit_and_passed_on_the_outcome(tmp_path):
    h = Harness(tmp_path)
    h.control.final = dict(FINAL)
    h.control.items = [item("route_started")]
    h.executor.run_once()
    names = h.names()
    assert (
        names.index("report_exited") < names.index("guardian_final") < names.index("record_outcome")
    )
    calls = dict((c[0], c) for c in h.control.calls)
    assert calls["record_outcome"][2]["guardian_final"] == FINAL


def test_an_unreadable_guardian_status_still_records_the_outcome(tmp_path):
    h = Harness(tmp_path)
    h.control.final_error = RuntimeError("custody unavailable")
    h.control.items = [item("route_started")]
    actions = h.executor.run_once()
    # The failure is visible (error class only), the outcome is still recorded.
    assert [(a["action"], a["detail"]) for a in actions][:1] == [
        ("guardian_final_unavailable", "RuntimeError")
    ]
    assert actions[-1]["action"] == "outcome_recorded"
    calls = dict((c[0], c) for c in h.control.calls)
    assert calls["record_outcome"][2]["guardian_final"] is None
    assert h.sleeps == [2, 2, 2]  # bounded re-reads before giving up


def test_the_guardian_is_re_read_until_it_has_stopped(tmp_path):
    h = Harness(tmp_path)
    armed = dict(FINAL, status="armed")
    reads = [armed, armed, dict(FINAL)]
    h.control.guardian_final = lambda route_id: reads.pop(0)
    h.control.items = [item("route_started")]
    h.executor.run_once()
    assert h.sleeps == [2, 2] and reads == []
    calls = dict((c[0], c) for c in h.control.calls)
    assert calls["record_outcome"][2]["guardian_final"] == FINAL


def test_no_configured_guardian_skips_the_re_reads(tmp_path):
    h = Harness(tmp_path)
    h.control.guardian_final = lambda route_id: None
    h.control.items = [item("route_started")]
    actions = h.executor.run_once()
    assert h.sleeps == []
    assert ("guardian_final_unavailable", "unprojectable") in [
        (a["action"], a["detail"]) for a in actions
    ]


def test_a_final_that_never_stops_is_logged(tmp_path):
    h = Harness(tmp_path)
    armed = dict(FINAL, status="armed")
    h.control.guardian_final = lambda route_id: armed
    h.control.items = [item("route_started")]
    actions = h.executor.run_once()
    assert h.sleeps == [2, 2, 2]
    assert ("guardian_not_stopped", "armed") in [(a["action"], a["detail"]) for a in actions]


def test_build_executor_wires_the_guardian_status_through_the_custody_client(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    built = {}

    class FakeClient:
        def __init__(self, **kwargs):
            built["kwargs"] = kwargs

        def status(self, state):
            built["state"] = state
            return dict(FINAL, estimated_exposure_cents=3.5)

    class FakeProfiles:
        def get(self, name):
            return h.executor.profile

    class FakeStore:
        def __init__(self, state_dir):
            pass

        def authenticate_worker(self, token):
            return {"id": WORKER}

    import tessera_control.salad_live_check as live
    import tessera_control.salad_server as server
    import tessera_control.store as store

    monkeypatch.setattr(server, "SaladSafetyHTTPSClient", FakeClient)
    monkeypatch.setattr(store, "Store", FakeStore)
    monkeypatch.setattr(live, "_private_bytes", lambda path, cap: b"token")
    monkeypatch.setattr(module, "load_profiles", lambda path: FakeProfiles())
    monkeypatch.setattr(module, "trusted_importer", lambda *a: None)
    captured = {}
    monkeypatch.setattr(
        module,
        "StoreControl",
        lambda state_dir, worker, status, *rest: captured.setdefault("status", status),
    )
    config = h.config.model_copy(update={"custody": h.config.custody})
    module.build_executor(config)
    projected = project = captured["status"]({"reservation": "r"})
    assert built["kwargs"] == config.custody.client_fields()
    assert built["state"] == {"reservation": "r"} and projected["status"] == "stopped"
    assert module.project_guardian_final(project)["estimated_exposure_cents"] == "3.5"


def test_the_projection_accepts_finite_json_floats_only():
    raw = dict(FINAL, estimated_exposure_cents=3.5, published_hourly_rate_usd=0.5)
    projected = module.project_guardian_final(raw)
    assert (projected["estimated_exposure_cents"], projected["published_hourly_rate_usd"]) == (
        "3.5",
        "0.5",
    )
    for bad in (float("nan"), float("inf"), True):
        assert (
            module.project_guardian_final(dict(raw, estimated_exposure_cents=bad))[
                "estimated_exposure_cents"
            ]
            is None
        )


def test_the_projection_keeps_only_recorded_keys_and_is_unclean_when_a_flag_is_missing():
    raw = dict(
        FINAL,
        extra="x",
        manifest={"secret": 1},
        cost="unknown",
        total_cap_cents=5,
        estimated_exposure_cents=4,
    )
    del raw["observed_at"], raw["orphan"]
    projected = module.project_guardian_final(raw)
    assert set(projected) == set(FINAL)
    assert projected["orphan"] is True  # missing is never read as clean
    assert projected["estimated_exposure_cents"] == "4"
    assert module.project_guardian_final(dict(raw, status="exotic")) is None
    assert module.project_guardian_final(None) is None


def test_main_once_writes_health_and_bad_config_exits_2(tmp_path, monkeypatch, capsys):
    h = Harness(tmp_path)
    h.control.items = []
    (tmp_path / "state").mkdir(mode=0o700)
    config = h.config.model_copy(update={"state_dir": tmp_path / "state"})
    monkeypatch.setattr(module, "load_config", lambda path: config)
    monkeypatch.setattr(module, "build_executor", lambda config: h.executor)
    health = tmp_path / "health.json"
    assert module.main(["--config", "x", "--once", "--health-file", str(health)]) == 0
    module.maintenance_lock._release_process_locks()
    document = json.loads(health.read_text())
    assert document["schema"] == "tessera.service-health.v1" and document["loops"] == 1
    assert stat.S_IMODE(os.stat(health).st_mode) == 0o600
    monkeypatch.undo()
    assert module.main(["--config", str(tmp_path / "missing.json"), "--once"]) == 2
    assert "executor_setup_unavailable" in capsys.readouterr().out


def test_main_max_cycles_bounds_the_loop(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    h.control.items = []
    (tmp_path / "state").mkdir(mode=0o700)
    fast = h.config.model_copy(update={"poll_seconds": 0.01, "state_dir": tmp_path / "state"})
    monkeypatch.setattr(module, "load_config", lambda path: fast)
    monkeypatch.setattr(module, "build_executor", lambda config: h.executor)
    health = tmp_path / "health.json"
    args = ["--config", "x", "--max-cycles", "2", "--health-file", str(health)]
    assert module.main(args) == 0
    module.maintenance_lock._release_process_locks()
    assert json.loads(health.read_text())["loops"] == 2


# -- several profiles in one executor (ADR 0070) -----------------------------------------------


def _cpu_profile():
    from test_salad_cpu_profile import cpu_profile_data

    return SaladCampaignProfile.model_validate(cpu_profile_data())


def test_one_executor_runs_a_cpu_profile_beside_a_gpu_profile(tmp_path):
    cpu = _cpu_profile()
    h = Harness(tmp_path, profile=[PROFILE, cpu])
    assert set(h.executor.profiles) == {"gpu-main", "cpu-main"}
    h.control.final = dict(FINAL)
    h.control.state = route_state(
        proposal=dict(
            operation_id=OPERATION, epoch=3, selection=selection(cpu).model_dump(mode="json")
        )
    )
    h.control.items = [item("route_started", profile="cpu-main")]
    assert [a["action"] for a in h.executor.run_once()] == ["outcome_recorded"]
    # The route ran with the CPU profile's policy, and custody was imported under it.
    assert h.runs[0]["timeout"] == cpu.route_max_seconds + module.SUBPROCESS_GRACE_SECONDS
    assert h.import_profiles == ["cpu-main"]
    # A GPU selection presented under the CPU stage's profile never runs.
    h2 = Harness(tmp_path / "second", profile=[PROFILE, cpu])
    h2.control.items = [item("route_started", profile="cpu-main")]
    assert [a["action"] for a in h2.executor.run_once()] == ["refused_selection_mismatch"]
    assert h2.runs == []


def test_work_for_a_profile_the_executor_does_not_serve_is_refused(tmp_path):
    h = Harness(tmp_path, profile=[PROFILE, _cpu_profile()])
    h.control.items = [item("awaiting_lease", route_id=None, operation_id=None, profile="other")]
    assert [(a["action"], a["detail"]) for a in h.executor.run_once()] == [
        ("refused_profile", "different_profile")
    ]
    assert h.control.calls == [("heartbeat",)]
    h.control.items = [item("awaiting_lease", route_id=None, operation_id=None, profile="cpu-main")]
    assert [a["action"] for a in h.executor.run_once()] == ["packaged"]


def test_the_config_names_profile_or_profiles_and_build_loads_each(tmp_path, monkeypatch):
    one = config_for(tmp_path)
    base = one.model_dump(mode="json", by_alias=True)
    assert one.profile_names == ("gpu-main",)
    many = ExecutorConfig.model_validate(
        {**base, "profile": None, "profiles": ["gpu-main", "cpu-main"]}
    )
    assert many.profile_names == ("gpu-main", "cpu-main")
    for bad in (
        {**base, "profiles": ["cpu-main"]},
        {**base, "profile": None},
        {**base, "profile": None, "profiles": ["cpu-main", "cpu-main"]},
        {**base, "profile": None, "profiles": []},
    ):
        with pytest.raises(ValueError):
            ExecutorConfig.model_validate(bad)
    with pytest.raises(ValueError, match="distinct"):
        SaladCampaignExecutor(
            many, [PROFILE, PROFILE], FakeControl(), worker_principal_id=WORKER, importer=None
        )

    cpu = _cpu_profile()
    loaded = {"gpu-main": PROFILE, "cpu-main": cpu}

    class FakeProfiles:
        def get(self, name):
            return loaded.get(name)

    class FakeStore:
        def __init__(self, state_dir):
            pass

        def authenticate_worker(self, token):
            return {"id": WORKER}

    import tessera_control.salad_live_check as live
    import tessera_control.store as store

    monkeypatch.setattr(store, "Store", FakeStore)
    monkeypatch.setattr(live, "_private_bytes", lambda path, cap: b"token")
    monkeypatch.setattr(module, "load_profiles", lambda path: FakeProfiles())
    monkeypatch.setattr(module, "StoreControl", lambda *a: FakeControl())
    built = module.build_executor(many)
    assert list(built.profiles) == ["gpu-main", "cpu-main"]
    unknown = ExecutorConfig.model_validate(
        {**base, "profile": None, "profiles": ["gpu-main", "x"]}
    )
    with pytest.raises(ValueError, match="Unknown"):
        module.build_executor(unknown)


@pytest.mark.parametrize(
    ("host", "workload_host", "accepted"),
    [
        # no workload name: ``host`` is both, as before
        ("custody.example-tailnet.ts.net", None, True),
        ("custody-admin.internal", None, False),
        # split: connect to the admin endpoint, advertise the profile's workload name
        ("custody-admin.internal", "custody.example-tailnet.ts.net", True),
        ("custody-admin.internal", "other.example-tailnet.ts.net", False),
        # a matching admin host does not excuse a different advertised name
        ("custody.example-tailnet.ts.net", "other.example-tailnet.ts.net", False),
    ],
)
def test_the_profile_custody_host_is_compared_with_the_workload_name(
    tmp_path, monkeypatch, host, workload_host, accepted
):
    h = Harness(tmp_path)
    built = {}

    class FakeClient:
        def __init__(self, **kwargs):
            built["kwargs"] = kwargs

        def status(self, state):
            return dict(FINAL)

    class FakeProfiles:
        def get(self, name):
            return h.executor.profile

    class FakeStore:
        def __init__(self, state_dir):
            pass

        def authenticate_worker(self, token):
            return {"id": WORKER}

    import tessera_control.salad_live_check as live
    import tessera_control.salad_server as server
    import tessera_control.store as store

    monkeypatch.setattr(server, "SaladSafetyHTTPSClient", FakeClient)
    monkeypatch.setattr(store, "Store", FakeStore)
    monkeypatch.setattr(live, "_private_bytes", lambda path, cap: b"token")
    monkeypatch.setattr(module, "load_profiles", lambda path: FakeProfiles())
    monkeypatch.setattr(module, "trusted_importer", lambda *a: None)
    monkeypatch.setattr(module, "StoreControl", lambda state_dir, worker, status, *rest: status)
    fields = {"host": host, **({"workload_host": workload_host} if workload_host else {})}
    custody_config = h.config.custody.model_copy(update=fields)
    config = h.config.model_copy(update={"custody": custody_config})
    if not accepted:
        with pytest.raises(ValueError, match="Custody host differs from the profile"):
            module.build_executor(config)
        return
    status = module.build_executor(config)
    assert status is not None
    # The custody client connects to and pins the admin ``host`` only; the workload name is
    # compared with the profile and never reaches the client (built when status is first asked).
    status.control({})
    assert built["kwargs"]["host"] == host
    assert "workload_host" not in built["kwargs"]


def test_the_workload_name_never_reaches_the_custody_client_or_live_check_config():
    config = module.CustodyConfig(**custody(), workload_host="custody.example-tailnet.ts.net")
    assert "workload_host" not in config.client_fields()
    assert config.client_fields() == module.CustodyConfig(**custody()).client_fields()
    assert config.workload_name() == "custody.example-tailnet.ts.net"
    assert module.CustodyConfig(**custody()).workload_name() == custody()["host"]
    with pytest.raises(ValueError):
        module.CustodyConfig(**custody(), workload_host="Not A Host")


def _awaiting():
    return [item("awaiting_lease", route_id=None, operation_id=None)]


@pytest.mark.parametrize("step", ["grant_poll", "lease", "package"])
def test_a_control_refusal_is_recorded_with_its_step_and_attempt(tmp_path, step):
    h = Harness(tmp_path)
    h.control.items = _awaiting()
    if step == "grant_poll":
        h.control.pending_error = Conflict("secret-token host.example.invalid")
    elif step == "lease":
        h.control.acquire_error = Conflict("secret-token host.example.invalid")
    else:
        h.control.package_error = Conflict("secret-token host.example.invalid")
    actions = h.executor.run_once()
    assert [(a["action"], a["detail"]) for a in actions][-1] == ("error", "Conflict")
    assert h.control.rejections == [("att_1", step, "Conflict")]
    assert "secret-token" not in json.dumps(actions)


@pytest.mark.parametrize("step", ["lease", "package"])
def test_a_non_store_exception_is_recorded_with_its_step(tmp_path, step):
    h = Harness(tmp_path)
    h.control.items = _awaiting()
    boom = OSError("secret-token host.example.invalid")
    if step == "lease":
        h.control.acquire_error = boom
    else:
        h.control.package_error = boom
    actions = h.executor.run_once()
    assert [(a["action"], a["detail"]) for a in actions][-1] == ("error", "OSError")
    assert h.control.rejections == [("att_1", step, "OSError")]
    assert "secret-token" not in json.dumps(actions)


def test_a_failing_rejection_recorder_never_breaks_the_pass(tmp_path):
    h = Harness(tmp_path)
    h.control.acquire_error = Conflict("refused")
    h.control.rejection_error = RuntimeError("secret-token store down")
    h.control.items = _awaiting() + [
        item("awaiting_lease", attempt_id="att_2", route_id=None, operation_id=None)
    ]
    actions = h.executor.run_once()
    assert [(a["action"], a["attempt_id"], a["detail"]) for a in actions] == [
        ("error", "att_1", "Conflict"),
        ("rejection_unrecorded", "att_1", "RuntimeError"),
        ("error", "att_2", "Conflict"),
        ("rejection_unrecorded", "att_2", "RuntimeError"),
    ]
    assert "secret-token" not in json.dumps(actions)


def test_a_control_without_record_rejection_behaves_as_before(tmp_path):
    class Plain(FakeControl):
        record_rejection = None

    h = Harness(tmp_path, Plain())
    h.control.acquire_error = Conflict("refused")
    h.control.items = _awaiting()
    actions = h.executor.run_once()
    assert [(a["action"], a["detail"]) for a in actions] == [("error", "Conflict")]


def _fake_live_check(monkeypatch, stdout, stderr=b"", receipt_dir=None):
    def fake_run(command, **kwargs):
        if receipt_dir is not None:
            target = Path(receipt_dir) / module.RECEIPT_NAME
            target.write_text(json.dumps(RECEIPT))
            target.chmod(0o600)
        return subprocess.CompletedProcess(command, 2, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(module.subprocess, "run", fake_run)


def _no_receipt_detail(tmp_path):
    receipts = tmp_path / "r"
    receipts.mkdir(mode=0o700, exist_ok=True)
    with pytest.raises(module.RouteExitedWithoutReceipt) as raised:
        subprocess_route_runner(tmp_path / "c.json", receipts, 5)
    return raised.value.detail


def test_runner_reports_only_status_and_reason_when_no_receipt(tmp_path, monkeypatch, caplog):
    out = (
        b"noise /opt/x/secret token=abc\n"
        b'{"status": "inconclusive", "reason": "source_pin_malformed", "path": "/p/q"}\n'
    )
    _fake_live_check(monkeypatch, out, stderr=b"Traceback secret")
    with caplog.at_level("WARNING"):
        detail = _no_receipt_detail(tmp_path)
    assert detail == "inconclusive:source_pin_malformed"
    assert "source_pin_malformed" in caplog.text
    assert "secret" not in caplog.text and "/p/q" not in caplog.text


@pytest.mark.parametrize(
    "stdout",
    [
        b"",
        b"not json at all /opt/x\n",
        b'{"status": "x", "reason": "has spaces and /paths"}\n',
        b'{"status": "inconclusive", "reason": "' + b"a" * 5000 + b'"}\n',
        b"x" * 500000,
        b"[1, 2]\n",
    ],
)
def test_runner_uses_fixed_code_for_garbage_or_oversized_output(tmp_path, monkeypatch, stdout):
    _fake_live_check(monkeypatch, stdout)
    assert _no_receipt_detail(tmp_path) == "live_check_exited_without_receipt"


def test_runner_with_receipt_is_unchanged(tmp_path, monkeypatch):
    receipts = tmp_path / "r"
    receipts.mkdir(mode=0o700)
    _fake_live_check(monkeypatch, b'{"status": "passed"}\n', receipt_dir=receipts)
    assert subprocess_route_runner(tmp_path / "c.json", receipts, 5) == RECEIPT


def test_executor_records_the_reason_when_the_live_check_leaves_no_receipt(tmp_path):
    def runner(config_path, receipt_dir, timeout):
        raise module.RouteExitedWithoutReceipt("inconclusive:source_pin_malformed")

    h = Harness(tmp_path, runner=runner)
    h.control.items = [item("route_started")]
    notes = h.executor.run_once()
    # The reason is noted, then the terminal no-receipt report goes to control (this fake has
    # no ``record_no_receipt``; its failure is recorded, never fatal).
    assert [(n["action"], n["detail"]) for n in notes] == [
        ("no_receipt", "inconclusive:source_pin_malformed"),
        ("no_receipt_report_failed", "AttributeError"),
    ]
    assert "record_outcome" not in h.names()
