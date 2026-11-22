"""Disposable durable filesystem claims; synthetic transport, no native authority."""

import copy
import hashlib
import json
import sqlite3
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tessera_operator import schema_supervised as s


class Scripted(s.DisposableProfile):
    def __init__(self):
        self.db = "a" * 64
        self.calls = []
        self.denied = False
        self.lost = False
        self.drift = False
        self.checks = []

    def observe(self, plan, stage):
        return {"rv": "changed" if self.drift else "10", "sha256": self.db}

    def source_digest(self):
        return "b" * 64

    def compile(self, plan, stage, observed, previous):
        # Scripted CAS compilation demonstrates request cannot supply a command.
        command = {
            "method": "PATCH",
            "uri": "/fixed/deployment",
            "body": [
                {"op": "test", "path": "/metadata/uid", "value": plan["target"]["deployment_uid"]},
                {"op": "test", "path": "/metadata/resourceVersion", "value": observed["rv"]},
                {"op": "replace", "path": "/spec/replicas", "value": 0},
            ],
        }
        return dict(
            target=plan["target"],
            db={"incarnation": plan["db_incarnation"], "sha256": self.db},
            image="test@sha256:" + "c" * 64,
            script_sha256="d" * 64,
            command=command,
            evidence_sha256=s.digest(observed),
        )

    def check_bound(self, binding):
        self.checks.append(s.digest(binding))
        if self.denied:
            raise PermissionError("synthetic transport denial")

    def dispatch(self, binding):
        self.calls.append(copy.deepcopy(binding))
        self.db = s.digest([self.db, binding["phase"]])
        if self.lost:
            raise TimeoutError("synthetic lost acknowledgement")
        return {
            "action_sha256": s.digest(binding),
            "post_db_sha256": self.db,
            "incarnation": binding["db"]["incarnation"],
            "phase": binding["phase"],
        }

    def validate_result(self, binding, result):
        assert result["action_sha256"] == s.digest(binding)
        assert result["incarnation"] == binding["db"]["incarnation"]


def operation(tmp_path):
    profile = Scripted()
    now = [100]
    op = s.SupervisedOperation(tmp_path, profile, clock=lambda: now[0])
    op.create(
        dict(
            operation="operation-89",
            deadline=200,
            target={"deployment_uid": "dep", "pvc_uid": "pvc", "pv_uid": "pv"},
            db_incarnation="inc",
            db_sha256=profile.db,
        )
    )
    return op, profile, now


def test_exact_packet_once_and_reopen(tmp_path):
    op, profile, _ = operation(tmp_path)
    packet = op.prepare("scale_zero")
    result = op.apply_disposable("scale_zero")
    assert profile.checks == [packet["action_sha256"]] * 2
    assert len(profile.calls) == 1
    reopened = s.SupervisedOperation(tmp_path, profile, clock=lambda: 100)
    assert reopened.reconcile("scale_zero")["result"] == result
    with pytest.raises(s.SupervisedDenied):
        reopened.apply_disposable("scale_zero")
    assert len(profile.calls) == 1
    assert reopened.prepare("network_policy")["binding"]["db"]["sha256"] == result["post_db_sha256"]


@pytest.mark.parametrize("failure", ["expiry", "drift", "db", "denial", "target"])
def test_stale_or_denied_never_dispatches(tmp_path, failure):
    op, profile, now = operation(tmp_path)
    op.prepare("scale_zero")
    if failure == "expiry":
        now[0] = 200
    elif failure == "drift":
        profile.drift = True
    elif failure == "db":
        profile.db = "f" * 64
    elif failure == "target":
        original = profile.compile

        def changed(*args):
            return original(*args) | {"target": {"deployment_uid": "replacement"}}

        profile.compile = changed
    else:
        profile.denied = True
    with pytest.raises((s.SupervisedDenied, PermissionError)):
        op.apply_disposable("scale_zero")
    assert not profile.calls and not (tmp_path / "scale_zero.claim.json").exists()


def test_migration_lost_ack_no_resubmit_or_56_recovery(tmp_path):
    op, profile, now = operation(tmp_path)
    for phase in s.STAGES[:4]:
        op.prepare(phase)
        op.apply_disposable(phase)
    op.prepare("migrate")
    profile.lost = True
    with pytest.raises(TimeoutError):
        op.apply_disposable("migrate")
    now[0] = 300
    seen = op.reconcile("migrate")
    assert seen["state"] == "unknown_ack" and seen["resubmit"] is False
    assert seen["recovery"] == "compatible58 forward-only; admission held"
    for phase in ("migrate", "validate", "scale_zero"):
        with pytest.raises(s.SupervisedDenied):
            op.apply_disposable(phase)
    assert len(profile.calls) == 5


def test_default_native_apply_denies_even_with_prepared_plan(tmp_path):
    op, profile, _ = operation(tmp_path)
    op.prepare("scale_zero")
    with pytest.raises(s.SupervisedDenied, match="native exact-effect"):
        op.apply("scale_zero")
    assert not profile.calls and not hasattr(s, "register")


def test_phase_and_prepare_duplicates_and_lock(tmp_path):
    op, _, _ = operation(tmp_path)
    with pytest.raises(s.SupervisedDenied):
        op.prepare("migrate")
    op.prepare("scale_zero")
    with pytest.raises(FileExistsError):
        op.prepare("scale_zero")
    with op.locked(), pytest.raises(s.SupervisedDenied, match="owned"):
        s.SupervisedOperation(tmp_path, Scripted()).prepare("scale_zero")


def test_non_disposable_profile_cannot_use_test_apply(tmp_path):
    op, _, _ = operation(tmp_path)
    op.profile = object()
    with pytest.raises(s.SupervisedDenied, match="disposable"):
        op.apply_disposable("scale_zero")


def test_retained_plan_tamper_denied(tmp_path):
    op, _, _ = operation(tmp_path)
    (tmp_path / "plan.json").write_text('{"value": {}, "sha256": "not-matching"}')
    with pytest.raises(s.SupervisedDenied, match="plan drift"):
        op.prepare("scale_zero")


def concrete(tmp_path):
    """Real temp DB/receipts, concrete compiler+transport, scripted Kubernetes."""
    from maintenance_custody_fixture import export_phase
    from maintenance_hold_fixture import fixture_hold

    from tessera_operator.maintenance_handoff import MaintenanceHandoff

    exports = {}
    state = tmp_path / "state" / "control"
    state.mkdir(parents=True)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.executescript(
            "CREATE TABLE schema_meta(schema_version INTEGER);"
            "INSERT INTO schema_meta VALUES(56);"
            "CREATE TABLE control_recovery(incarnation TEXT);"
            "INSERT INTO control_recovery VALUES('real-disposable');"
        )
    observed_db = s.read_db(state.parent, "control/tessera.sqlite3")
    old, new = "test@sha256:" + "a" * 64, "test@sha256:" + "b" * 64
    plan = dict(
        operation="concrete-89",
        deadline=200,
        old_image=old,
        new_image=new,
        target={"deployment_uid": "dep", "pvc_uid": "pvc", "pv_uid": "pv"},
        db_incarnation=observed_db["incarnation"],
        db_sha256=observed_db["sha256"],
    )
    deployment = {
        "metadata": {
            "name": "tessera-control",
            "uid": "dep",
            "resourceVersion": "1",
            "generation": 1,
        },
        "spec": {
            "replicas": 1,
            "template": {"spec": {"containers": [{"name": "control", "image": old}]}},
        },
        "status": {"replicas": 1, "observedGeneration": 1},
    }
    objects = {
        "deployment": deployment,
        "pvc": {"metadata": {"uid": "pvc"}, "spec": {"volumeName": "pv"}},
        "pv": {"metadata": {"uid": "pv"}, "spec": {"claimRef": {"uid": "pvc"}}},
        "pods": {"items": []},
    }
    calls, remote = [], {}
    failure = {"lost": False, "denied": False}

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if "get" in argv:
            kind = argv[argv.index("get") + 1]
            value = objects[kind]
        elif "patch" in argv:
            if failure["denied"]:
                return SimpleNamespace(returncode=1, stdout=b"", stderr=b"synthetic denial")
            patch = json.loads(argv[argv.index("--patch") + 1])
            assert [p["path"] for p in patch[:3]] == [
                "/metadata/uid",
                "/metadata/resourceVersion",
                "/spec",
            ]
            assert patch[0]["value"] == "dep" and patch[2]["value"] == deployment["spec"]
            deployment["spec"]["replicas"] = patch[3]["value"]
            deployment["spec"]["template"]["spec"]["containers"][0]["image"] = patch[4]["value"]
            deployment["metadata"]["resourceVersion"] = "2"
            deployment["status"]["replicas"] = patch[3]["value"]
            value = deployment
            if failure["lost"]:
                raise TimeoutError("scripted lost CAS ack")
        elif "create" in argv:
            value = json.loads(kwargs["input"])
            kind = "pod" if value["kind"] == "Pod" else "networkpolicy"
            value["metadata"]["uid"] = "maintenance" if kind == "pod" else "network"
            value["metadata"]["resourceVersion"] = "123"
            value["status"] = {
                "containerStatuses": [
                    {
                        "name": n,
                        "imageID": i,
                        "containerID": "containerd://" + n,
                        "restartCount": 0,
                        "state": {"running": {}},
                    }
                    for n, i in (("old", old), ("new", new))
                ]
            }
            objects[kind] = value
            if kind == "pod":
                objects["pods"]["items"] = [value]
        else:
            assert "exec" in argv and argv[-6:-1] == [
                "--",
                "/app/.venv/bin/python",
                "-I",
                "-B",
                "-c",
            ]
            code = argv[-1]
            if "save(" in code:
                phase = (
                    "backup"
                    if "'backup.claim.json'" in code
                    else "migrate"
                    if "'migrate.claim.json'" in code
                    else "validate"
                )
                assert "mode=ro&immutable=1" in code and "open('x')" in code
                assert "--state-dir" in code and "/state/control" in code
                if phase in {"migrate", "validate"}:
                    with sqlite3.connect(state / "tessera.sqlite3") as db:
                        db.execute("UPDATE schema_meta SET schema_version=58")
                    # Finish the scripted write/read before immutable observation;
                    # read_db deliberately rejects even atime changes mid-probe.
                    (state / "tessera.sqlite3").read_bytes()
                    observed_db.update(s.read_db(state.parent, "control/tessera.sqlite3"))
                value = {
                    "operation": plan["operation"],
                    "phase": phase,
                    "db": dict(observed_db),
                    "target": plan["target"],
                    "input_db_sha256": (
                        plan["db_sha256"]
                        if phase in {"backup", "migrate"}
                        else observed_db["sha256"]
                    ),
                }
                if phase in {"backup", "validate"}:
                    value, exports[phase] = export_phase(
                        tmp_path / ("export-" + phase),
                        plan,
                        phase,
                        state / "tessera.sqlite3",
                        value["input_db_sha256"],
                    )
                remote[phase] = value
                if failure["lost"]:
                    raise TimeoutError("scripted lost exec ack")
            elif ".result.json" in code:
                value = copy.deepcopy(remote["migrate"])
                if failure.get("wrong_readback"):
                    value["operation"] = "unrelated-operation"
            else:
                value = observed_db
        return SimpleNamespace(returncode=0, stdout=json.dumps(value).encode(), stderr=b"")

    def spawn(argv, body):
        result = run(argv, input=body)
        code = (
            "import os;os.write(1,"
            + repr(result.stdout)
            + ");os.write(2,"
            + repr(result.stderr)
            + ");raise SystemExit("
            + str(result.returncode)
            + ")"
        )
        return subprocess.Popen(
            [__import__("sys").executable, "-I", "-B", "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    profile = s.KubernetesSupervisedProfile(
        transport=s.BoundedKubernetes(spawn=spawn), clock=lambda: 100
    )
    op = s.SupervisedOperation(tmp_path / "journal", profile, clock=lambda: 100)
    op.create(plan)

    def pin(phase, custody_override=None):
        if phase == "backup":
            profile.hold, _, _, enforcement = fixture_hold(tmp_path / "quota", plan)
            profile.hold.prepare("hold", plan)
            profile.hold.apply("hold", plan)
            enforcement()
        if phase == "activate":
            effective_plan = op.plan() | {
                "phase_receipts": {
                    stage: op.read(stage + ".result.json")
                    for stage in ("backup", "migrate", "validate")
                }
            }
            pod = objects["pod"]
            for status in pod["status"]["containerStatuses"]:
                status["state"] = {
                    "terminated": {
                        "containerID": status["containerID"],
                        "finishedAt": "2026-10-24T00:00:01Z",
                        "exitCode": 0,
                    }
                }
            pins = dict(
                uid=pod["metadata"]["uid"],
                spec_sha256=s.digest(pod["spec"]),
                containers={
                    c["name"]: {k: c[k] for k in ("containerID", "imageID", "restartCount")}
                    for c in pod["status"]["containerStatuses"]
                },
            )

            class HandoffTransport:
                def get(self, name):
                    assert name == plan["operation"]
                    return {"items": [copy.deepcopy(pod)]}

                def delete(self, name, body):
                    assert body["preconditions"] == {"uid": "maintenance", "resourceVersion": "123"}
                    assert body["gracePeriodSeconds"] == 30
                    return 202

            profile.handoff = MaintenanceHandoff(
                tmp_path / "handoff", transport=HandoffTransport(), clock=lambda: 100
            )
            profile.handoff.apply(effective_plan, pins, exports)
            profile.hold.handoff = profile.handoff
            # Synthetic owner records, actual local export destination identities.
            from tessera_operator.bundle_export import BundleExport, directory_identity
            from tessera_operator.maintenance_composition import OwnerMaintenance
            from tessera_operator.maintenance_witness import read_json

            local_exports = {
                phase: BundleExport(entry["journal"], entry["destination"], clock=lambda: 100)
                for phase, entry in exports.items()
            }
            # This receipt is generated from the actual materialized bundles and
            # is independently revalidated by MaintenanceHold at release.
            materialized_receipt = OwnerMaintenance(
                None, profile.hold, profile.handoff, structured_api=None
            ).custody_readback(effective_plan, local_exports)
            custody_path = tmp_path / "quota" / "custody.json"
            custody_path.write_text(
                json.dumps(
                    dict(
                        operation=plan["operation"],
                        target=plan["target"],
                        kind="custody",
                        observed_at=90,
                        valid_until=200,
                        failure_domain="maintenance-pod-and-pvc",
                        source_pvc_uid=plan["target"]["pvc_uid"],
                        custodian_subject="fixture-custodian",
                        topology_receipt_sha256="synthetic-topology",
                        retention_until=200,
                        retention_policy="retain_through_recovery_no_auto_cleanup",
                        materialized_receipt=materialized_receipt,
                        release_journal=dict(
                            destination=directory_identity(profile.hold.directory),
                            reserved_bytes=4 * 262144,
                            capacity_receipt_sha256="synthetic-reservation-not-live",
                        ),
                        destinations={
                            p: read_json(Path(e["journal"]) / "claim.json")[0]["destination"]
                            for p, e in exports.items()
                        },
                    )
                )
            )
            profile.hold.prerequisites["custody"] = dict(
                path=str(custody_path),
                sha256=hashlib.sha256(custody_path.read_bytes()).hexdigest(),
                owner="fixture-custodian",
            )
            profile.hold.prepare("release", effective_plan)
            profile.hold.apply("release", effective_plan)

        def write(name, value):
            path = tmp_path / (phase + "-" + name + ".json")
            path.write_text(json.dumps(value, sort_keys=True))
            return dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())

        witnesses = {}
        for kind in ("admission", "host_writers", "guardian", "liability"):
            value = dict(
                target=plan["target"],
                operation=plan["operation"],
                kind=kind,
                observed_at=90,
                valid_until=200,
                state="held",
                unaccounted_writers=[],
                guardian_uid="synthetic-guardian",
                lease_until=200,
                exposure_cents=100,
            )
            witnesses[kind] = write(kind, value) | {
                "owner": "synthetic-independent-fixture",
                "valid_until": 200,
            }
        evidence = dict(
            target=plan["target"],
            operation=plan["operation"],
            phase=phase,
            observed_at=90,
            valid_until=200,
            witnesses=witnesses,
            exposure_cents=100,
            db=dict(observed_db),
            allowed_pod_uids=["maintenance"],
        )
        if "pod" in objects:
            evidence.update(
                maintenance_pod_uid="maintenance",
                maintenance_spec_sha256=s.digest(objects["pod"]["spec"]),
                container_image_ids={"old": old, "new": new},
            )
        if phase in {"migrate", "validate", "activate"}:
            source = remote["validate" if phase == "activate" else "backup"]["custody"]
            source = copy.deepcopy(source)
            if custody_override:
                source = custody_override(source)
            manifest, restore = (
                write("manifest", source["manifest"]),
                write("restore", source["restore_receipt"]),
            )
            evidence["backup_custody"] = dict(
                custodian="synthetic-only",
                manifest_path=manifest["path"],
                manifest_sha256=manifest["sha256"],
                restore_receipt_path=restore["path"],
                restore_receipt_sha256=restore["sha256"],
            )
        ref = write("phase", evidence)
        op.pin_evidence(phase, ref)
        return evidence, ref

    return op, profile, calls, failure, pin


def test_concrete_all_phases_render_and_execute_scripted_transport(tmp_path):
    op, _, calls, _, pin = concrete(tmp_path)
    for phase in s.STAGES:
        pin(phase)
        if phase == "activate":
            # Preserve the positive complete-journey acceptance below, but do
            # not manufacture a native completion reader to make it pass.
            before = len(calls)
            try:
                packet = op.prepare(phase)
            except s.SupervisedDenied as exc:
                assert "native completion unavailable" in str(exc)
                assert all("get" in argv for argv, _ in calls[before:])
                assert not (op.directory / "activate.claim.json").exists()
                assert not (op.directory / "activate.result.json").exists()
                pytest.xfail("successful activation unmet: native release completion reader absent")
        else:
            packet = op.prepare(phase)
        if phase == "maintenance_pod":
            body = json.loads(packet["binding"]["command"]["stdin"])
            assert body["spec"]["restartPolicy"] == "Never"
            assert body["spec"]["automountServiceAccountToken"] is False
        op.apply(phase)
    assert op.reconcile("migrate")["recovery"].startswith("compatible58")
    assert (
        sum(
            "patch" in argv or "create" in argv or ("exec" in argv and "save(" in argv[-1])
            for argv, _ in calls
        )
        == 7
    )


@pytest.mark.parametrize("phase", ["scale_zero", "migrate"])
def test_concrete_lost_ack_readback_never_resubmits(tmp_path, phase):
    op, _, calls, failure, pin = concrete(tmp_path)
    for stage in s.STAGES:
        pin(stage)
        op.prepare(stage)
        if stage == phase:
            failure["lost"] = True
            with pytest.raises(TimeoutError):
                op.apply(stage)
            break
        op.apply(stage)
    prior = len(calls)
    read = op.reconcile_remote(phase)
    assert read
    assert len(calls) == prior + 1
    with pytest.raises(s.SupervisedDenied):
        op.apply(phase)
    assert len(calls) == prior + 1


def test_concrete_evidence_tamper_denies_before_transport(tmp_path):
    op, _, calls, _, pin = concrete(tmp_path)
    _, ref = pin("scale_zero")
    from pathlib import Path

    Path(ref["path"]).write_text("{}")
    with pytest.raises(s.SupervisedDenied, match="evidence changed"):
        op.prepare("scale_zero")
    assert not calls


def test_concrete_transport_denial_keeps_claim_and_never_retries(tmp_path):
    op, _, calls, failure, pin = concrete(tmp_path)
    pin("scale_zero")
    op.prepare("scale_zero")
    failure["denied"] = True
    with pytest.raises(s.SupervisedDenied, match="denied/uncertain"):
        op.apply("scale_zero")
    count = len(calls)
    assert op.reconcile("scale_zero")["state"] == "unknown_ack"
    assert op.read("scale_zero.failure.json")["transport"]["reason"] == "transport/response"
    with pytest.raises(s.SupervisedDenied):
        op.apply("scale_zero")
    assert len(calls) == count


def test_concrete_deadline_denies_without_more_io(tmp_path):
    op, profile, calls, _, pin = concrete(tmp_path)
    pin("scale_zero")
    op.prepare("scale_zero")
    count = len(calls)
    op.clock = profile.clock = lambda: 200
    with pytest.raises(s.SupervisedDenied, match="expired"):
        op.apply("scale_zero")
    assert len(calls) == count


def test_real_db_rendered_script_guard_refuses_drift(tmp_path):
    op, _, _, _, pin = concrete(tmp_path)
    evidence, _ = pin("scale_zero")
    code = s.phase_script(
        "migrate", "guard-only", evidence["db"] | {"incarnation": "wrong"}, time.time() + 60
    )
    code = code.replace("'/state'", repr(str(tmp_path / "state")))
    before = (tmp_path / "state/control/tessera.sqlite3").read_bytes()
    result = subprocess.run(
        [__import__("sys").executable, "-I", "-B", "-c", code], capture_output=True
    )
    assert result.returncode != 0
    assert (tmp_path / "state/control/tessera.sqlite3").read_bytes() == before
    assert not (tmp_path / "scratch").exists()


@pytest.mark.parametrize(
    "stage,wrong",
    [
        ("migrate", "manifest"),
        ("validate", "restore"),
        ("activate", "swapped_phase"),
        ("migrate", "incarnation"),
        ("validate", "input_db_sha256"),
        ("activate", "post_db_sha256"),
    ],
)
def test_wrong_valid_custody_hashes_fail_actual_compiler_gate(tmp_path, stage, wrong):
    op, _, calls, _, pin = concrete(tmp_path)
    for phase in s.STAGES:
        if phase == stage:
            break
        pin(phase)
        op.prepare(phase)
        op.apply(phase)

    def swapped(source):
        if wrong == "manifest":
            source["manifest"]["backup_id"] = "another-valid-backup"
            source["restore_receipt"]["manifest_sha256"] = hashlib.sha256(
                json.dumps(source["manifest"], sort_keys=True).encode()
            ).hexdigest()
        elif wrong == "restore":
            source["restore_receipt"]["held_db"]["incarnation"] = "another-held-restore"
        elif wrong == "swapped_phase":
            source = op.read("backup.result.json")["remote"]["custody"]
        else:
            source["restore_receipt"]["source_incarnation" if wrong == "incarnation" else wrong] = (
                "unrelated-proof-value"
            )
        return source

    evidence, _ = pin(stage, swapped)
    before = len(calls)
    if stage == "activate":
        with pytest.raises(s.SupervisedDenied, match="native completion unavailable"):
            op.prepare(stage)
        assert not (op.directory / "activate.claim.json").exists()
        assert not (op.directory / "activate.result.json").exists()
        # The earlier native-completion gate is intentionally closed. Preserve
        # lineage rejection coverage by exercising the actual custody compiler
        # directly, without mocking or bypassing activation admission.
        effective = op.plan() | {
            "phase_receipts": {
                phase: op.read(phase + ".result.json")
                for phase in s.STAGES[: s.STAGES.index(stage)]
            }
        }
        with pytest.raises(s.SupervisedDenied, match="retained phase lineage"):
            s.KubernetesSupervisedProfile.check_custody(effective, stage, evidence)
        assert all("get" in argv for argv, _ in calls[before:])
        return
    with pytest.raises(s.SupervisedDenied, match="retained phase lineage"):
        op.prepare(stage)
    assert not (op.directory / f"{stage}.claim.json").exists()
    assert all(
        "get" in argv or ("exec" in argv and "save(" not in argv[-1]) for argv, _ in calls[before:]
    )


@pytest.mark.parametrize("fd", [1, 2])
def test_real_child_flood_is_stream_bounded_and_reaped(fd):
    transport = s.BoundedKubernetes(limit=4096, timeout=2)
    code = f"import os\nwhile True: os.write({fd},b'x'*8192)"
    with pytest.raises(s.SupervisedDenied):
        transport.call({"argv": [__import__("sys").executable, "-I", "-B", "-c", code]})
    assert transport.last_failure["reason"] == "stream cap"
    assert transport.last_failure["stdout_bytes"] <= 4096
    assert transport.last_failure["stderr_bytes"] <= 4096
    assert transport.last_failure["exit_code"] is not None


def test_real_child_deadline_is_bounded_and_reaped():
    transport = s.BoundedKubernetes(timeout=0.1)
    start = time.monotonic()
    with pytest.raises(s.SupervisedDenied):
        transport.call(
            {"argv": [__import__("sys").executable, "-I", "-B", "-c", "import time;time.sleep(30)"]}
        )
    assert transport.last_failure["reason"] == "deadline"
    assert time.monotonic() - start < 3
    assert transport.last_failure["exit_code"] is not None


def test_wrong_operation_existing_remote_receipt_cannot_reconcile(tmp_path):
    op, _, calls, failure, pin = concrete(tmp_path)
    for stage in s.STAGES:
        pin(stage)
        op.prepare(stage)
        if stage == "migrate":
            failure["lost"] = True
            with pytest.raises(TimeoutError):
                op.apply(stage)
            break
        op.apply(stage)
    failure["wrong_readback"] = True
    count = len(calls)
    with pytest.raises(s.SupervisedDenied, match="remote phase identity"):
        op.reconcile_remote("migrate")
    assert len(calls) == count + 1 and "exec" in calls[-1][0]
    assert "save(" not in calls[-1][0][-1]


def test_generated_backup_script_retains_real_manifest_and_held_proof(tmp_path):
    """Real SQLite/files/children; synthetic CLI, not application backup evidence."""
    import sys

    op, _, _, _, _ = concrete(tmp_path)
    state, scratch = tmp_path / "state", tmp_path / "scratch"
    scratch.mkdir()
    with sqlite3.connect(state / "control/tessera.sqlite3") as db:
        db.executescript(
            "ALTER TABLE control_recovery ADD COLUMN state TEXT DEFAULT 'active';"
            "ALTER TABLE control_recovery ADD COLUMN source_backup_digest TEXT;"
        )
    stub = tmp_path / "synthetic_cli.py"
    stub.write_text("""import sys,sqlite3,json,hashlib,shutil
from pathlib import Path
a=sys.argv[1:]
def arg(k): return Path(a[a.index(k)+1])
if a[0]=='backup':
    root=arg('--output');root.mkdir()
    c=sqlite3.connect(arg('--state-dir')/'tessera.sqlite3');d=sqlite3.connect(root/'snapshot.sqlite3')
    c.backup(d);c.close();d.close()
    sha=hashlib.sha256((root/'snapshot.sqlite3').read_bytes()).hexdigest()
    (root/'manifest.json').write_text(json.dumps({'backup_id':'synthetic-only','sha256':sha,'schema_version':56},sort_keys=True))
else:
    root=arg('--state-dir');root.mkdir();backup=arg('--backup')
    shutil.copyfile(backup/'snapshot.sqlite3',root/'tessera.sqlite3')
    m=json.loads((backup/'manifest.json').read_text())
    with sqlite3.connect(root/'tessera.sqlite3') as c:
        c.execute("UPDATE control_recovery SET incarnation='held-synthetic',"
                  "state='held_restore',source_backup_digest=?",(m['sha256'],))
    (root/'tessera.sqlite3').read_bytes()
""")
    before = s.read_db(state, "control/tessera.sqlite3")
    code = s.phase_script(
        "backup", op.plan()["operation"], before, time.time() + 30, op.plan()["target"]
    )
    code = code.replace(
        "[sys.executable,'-I','-B','-m','tessera_control',*args]",
        f"[sys.executable,'-I','-B',{str(stub)!r},*args]",
    )
    code = code.replace("/state", str(state)).replace("/scratch", str(scratch))
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code], capture_output=True, timeout=5, check=False
    )
    assert result.returncode == 0, result.stderr.decode()[:1000]
    custody = json.loads(result.stdout)["custody"]
    assert (
        custody["manifest_sha256"]
        == hashlib.sha256((scratch / "pre/manifest.json").read_bytes()).hexdigest()
    )
    assert (
        custody["restore_receipt_sha256"]
        == hashlib.sha256((scratch / "backup.restore-proof.json").read_bytes()).hexdigest()
    )
    assert custody["restore_receipt"] == json.loads(
        (scratch / "backup.restore-proof.json").read_text()
    )
    assert custody["restore_receipt"]["held_db"]["incarnation"] == "held-synthetic"
    assert custody["restore_receipt"]["input_db_sha256"] == before["sha256"]
