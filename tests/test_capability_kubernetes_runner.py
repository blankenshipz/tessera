"""Explicit synthetic raw API/kernel fixtures. No Kubernetes/client/socket effects."""

import copy
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_capability_activation import NOW, candidate  # noqa: F401

from tessera_control.store import canonical_digest
from tessera_worker.capability_conformance import main
from tessera_worker.capability_effects import BackendBlocked
from tessera_worker.capability_inspection import (
    RawEvidenceInspector,
    negative_jobs,
    object_digests,
    probe_requests,
)
from tessera_worker.capability_kubernetes import (
    FakeKubernetesCapabilityClient,
    KubernetesCapabilityRunner,
    RunnerJournal,
)
from tessera_worker.capability_runner_contract import PackageVerifier, ResourceBounds, RunnerPlan


def bind_plan(manifest):
    bounds = ResourceBounds(
        **manifest.quota.model_dump(exclude={"mechanism"}),
        pids=32,
        seconds=manifest.timeout_seconds,
    )
    plan = RunnerPlan(activation=manifest, project_bounds=bounds, package_bounds=bounds, pids=32)
    pvc_spec = {
        "resources": {"requests": {"storage": str(manifest.quota.output_bytes)}},
        "accessModes": ["ReadWriteOncePod"],
        "volumeMode": "Filesystem",
        "volumeName": "fixture-volume",
        "storageClassName": "fixture-project-quota",
    }
    updated = manifest.model_copy(
        update=object_digests(plan) | {"pvc_spec_digest": canonical_digest(pvc_spec)}
    )
    plan = plan.model_copy(update={"activation": updated})
    assert object_digests(plan) == object_digests(plan.model_copy(update={"activation": manifest}))
    return plan, pvc_spec


def raw_fixture(plan, pvc_spec):
    m = plan.activation
    objects = copy.deepcopy(plan.objects())
    for o in objects:
        o["metadata"].update(uid="fixture-" + o["kind"], resourceVersion="1", generation=1)
        if o["kind"] == "Namespace":
            o["metadata"]["uid"] = m.namespace_uid
            o["status"] = {"phase": "Active"}
        if o["kind"] == "ServiceAccount":
            o["metadata"]["uid"] = m.service_account_uid
        if o["kind"] == "ValidatingAdmissionPolicy":
            o["status"] = {"observedGeneration": 1, "typeChecking": {"expressionWarnings": []}}
        if o["kind"] == "ResourceQuota":
            used = {k: "0" for k in o["spec"]["hard"]}
            used.update(
                persistentvolumeclaims="1", **{"requests.storage": str(m.quota.output_bytes)}
            )
            o["status"] = {"hard": copy.deepcopy(o["spec"]["hard"]), "used": used}
    probes = {
        key: {
            "request": request,
            "response": RawEvidenceInspector.expected_response(
                plan, key, policy_uid="fixture-NetworkPolicy"
            ),
            "nonce": m.nonce,
            "subject": plan.digest,
            "observed_at": NOW.isoformat(),
        }
        for key, request in probe_requests(plan).items()
    }
    return {
        "plan_digest": plan.digest,
        "nonce": m.nonce,
        "cluster_digest": m.cluster_digest,
        "observed_at": NOW.isoformat(),
        "objects": objects,
        "identity": {
            "apiVersion": "authentication.k8s.io/v1",
            "kind": "SelfSubjectReview",
            "status": {"userInfo": {"username": m.dispatcher_identity}},
        },
        "pvc": {
            "kind": "PersistentVolumeClaim",
            "metadata": plan.metadata(m.pvc) | {"uid": m.pvc_uid},
            "spec": pvc_spec,
            "status": {"phase": "Bound"},
        },
        "pv": {
            "kind": "PersistentVolume",
            "metadata": {"name": "fixture-volume"},
            "spec": {
                "claimRef": {"namespace": m.namespace, "name": m.pvc, "uid": m.pvc_uid},
                "csi": {"driver": "fixture.invalid", "volumeHandle": "fixture-only"},
                "persistentVolumeReclaimPolicy": "Retain",
            },
        },
        "probes": probes,
        "events": {"kind": "EventList", "items": []},
        "rbac_inventory": [o for o in objects if o["kind"] in {"Role", "RoleBinding"}],
    }


@pytest.fixture
def runner(candidate, tmp_path):  # noqa: F811 - imported pytest fixture
    h = candidate
    plan, pvc_spec = bind_plan(h.manifest)
    verifier = PackageVerifier(
        authority=lambda _: True,
        identity=("org", "project", "worker"),
        approved_plan_digest=plan.digest,
        now=lambda: NOW,
    )
    verified = verifier.verify(h.package)
    heartbeat = {
        "identity": plan.activation.watchdog.identity,
        "certificate_sha256": plan.activation.watchdog.certificate_sha256,
        "nonce": plan.activation.nonce,
        "job_uid": "fake-job-uid",
        "sequence": 1,
        "observed_at": NOW.isoformat(),
        "deadline": (NOW + timedelta(seconds=3)).isoformat(),
    }
    client = FakeKubernetesCapabilityClient(
        snapshot=raw_fixture(plan, pvc_spec), heartbeat=heartbeat
    )
    journal = RunnerJournal(tmp_path / "runner")
    engine = KubernetesCapabilityRunner(
        client=client, journal=journal, verifier=verifier, claim=lambda *_: True, now=lambda: NOW
    )
    yield SimpleNamespace(**locals())
    journal.close()


def start(h):
    return h.engine.submit(h.plan, h.verified)


def test_exact_quota_manifest_and_ordered_create(runner):
    h = runner
    assert start(h) == "pending"
    assert h.client.calls == ["collect", "create", "arm", "heartbeat", "unsuspend"]
    pod = h.plan.job()["spec"]["template"]["spec"]
    assert pod["containers"][0]["resources"]["limits"] == h.plan.resources()
    assert "pids" not in pod["containers"][0]["resources"]["limits"]  # no invented K8s resource
    assert "enforcement-pids" in h.client.snapshot["probes"]
    receipts = h.journal.receipts(h.plan.activation.operation_id)
    assert [r["state"] for r in receipts] == [
        "inspecting",
        "claim_intent",
        "create_intent",
        "suspended",
        "start_intent",
        "pending",
    ]
    assert receipts[-1]["source"] == "fake-kubernetes"
    for previous, current in zip(receipts, receipts[1:], strict=False):
        assert current["previous_digest"] == canonical_digest(previous)
    with pytest.raises(BackendBlocked):
        start(h)
    assert h.client.calls.count("create") == 1


@pytest.mark.parametrize("index", range(18))
def test_all_forbidden_manifests_rejected(runner, index):
    job = list(negative_jobs(runner.plan).values())[index]
    with pytest.raises(BackendBlocked):
        runner.plan.validate_job(job)
    with pytest.raises(BackendBlocked):
        runner.client.create_suspended(runner.plan, job)
    assert not runner.client.jobs


@pytest.mark.parametrize(
    "kind",
    [
        "Namespace",
        "ServiceAccount",
        "ResourceQuota",
        "LimitRange",
        "NetworkPolicy",
        "Role",
        "RoleBinding",
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
    ],
)
@pytest.mark.parametrize("fault", ["missing", "weak"])
def test_missing_or_weak_objects_block_before_create(runner, kind, fault):
    objects = runner.client.snapshot["objects"]
    obj = next(o for o in objects if o["kind"] == kind)
    if fault == "missing":
        objects.remove(obj)
    else:
        obj["metadata"]["labels"]["escape"] = "true"
    assert start(runner) == "blocked"
    assert runner.client.calls == ["collect"]


@pytest.mark.parametrize(
    "probe",
    [
        "namespace",
        "image-command",
        "service-account",
        "rbac-secrets",
        "rbac-token-issuance",
        "rbac-exec",
        "rbac-control-pvc",
        "rbac-escalation",
        "rbac-cross-namespace",
        "admission-positive",
        "admission-hostpath",
        "admission-privileged",
        "admission-hostnetwork",
        "admission-hostpid-ipc",
        "admission-extra-containers",
        "admission-image",
        "admission-command",
        "admission-control-pvc",
        "admission-secret",
        "admission-ambient-token",
        "admission-policy-mode",
        "output-pvc",
        "resource-quota",
        "output-byte-exhaustion",
        "output-inode-exhaustion",
        "ephemeral-exhaustion",
        "network-policy",
        "network-allowed",
        "network-denied-public",
        "network-denied-private",
        "network-denied-metadata",
        "network-denied-control",
        "network-denied-dns-bypass",
        "network-denied-proxy",
        "dns-pins",
        "tls-san-issuer",
        "tls-wrong-ip",
        "tls-wrong-certificate",
        "redirect",
        "ambient-credentials",
        "readonly-nonroot",
        "archive-escape",
        "lockfile-closure",
        "watchdog-health",
        "watchdog-broker-loss",
        "watchdog-revoke",
        "enforcement-cpu",
        "enforcement-memory",
        "enforcement-pids",
        "enforcement-time",
        "rbac-extra-pvc",
        "rbac-extra-resource",
        "admission-projected",
        "admission-extra-pvc",
        "admission-args",
        "admission-service-account",
        "admission-init",
        "admission-ephemeral",
        "admission-writable-root",
        "admission-resources",
    ],
)
@pytest.mark.parametrize("fault", ["missing", "claimed", "stale", "wrong-request"])
def test_every_raw_probe_is_required_and_bound(runner, probe, fault):
    probes = runner.client.snapshot["probes"]
    if fault == "missing":
        del probes[probe]
    elif fault == "claimed":
        probes[probe]["response"] = {"passed": True, "signature": "signed-only"}
    elif fault == "stale":
        probes[probe]["observed_at"] = (NOW - timedelta(seconds=6)).isoformat()
    else:
        probes[probe]["request"] = {"claim": "same control"}
    assert start(runner) == "blocked"
    assert runner.client.calls == ["collect"]
    with pytest.raises(BackendBlocked):
        start(runner)


@pytest.mark.parametrize(
    "fault",
    [
        "quota-exceeded",
        "quota-stale",
        "policy-warning",
        "rbac-extra",
        "pvc-control",
        "pvc-uid",
        "pv-hostpath",
        "pv-claim",
        "identity",
        "event",
        "nonce",
        "future",
        "package",
    ],
)
def test_contradictory_raw_evidence(runner, fault):
    s = runner.client.snapshot
    if fault.startswith("quota"):
        q = next(o for o in s["objects"] if o["kind"] == "ResourceQuota")
        q["status"]["used" if fault == "quota-exceeded" else "hard"]["pods"] = "2"
    elif fault == "policy-warning":
        next(o for o in s["objects"] if o["kind"] == "ValidatingAdmissionPolicy")["status"][
            "typeChecking"
        ]["expressionWarnings"] = ["warning"]
    elif fault == "rbac-extra":
        s["rbac_inventory"].append({"kind": "RoleBinding", "subjects": ["workload"]})
    elif fault == "pvc-control":
        s["pvc"]["metadata"]["namespace"] = "control"
    elif fault == "pvc-uid":
        s["pvc"]["metadata"]["uid"] = "other"
    elif fault == "pv-hostpath":
        s["pv"]["spec"]["hostPath"] = {"path": "/control"}
    elif fault == "pv-claim":
        s["pv"]["spec"]["claimRef"]["namespace"] = "control"
    elif fault == "identity":
        s["identity"]["status"]["userInfo"]["username"] = "workload"
    elif fault == "event":
        s["events"]["items"] = [{"type": "Warning", "reason": "FailedMount"}]
    elif fault == "nonce":
        s["nonce"] = "0" * 64
    elif fault == "future":
        s["observed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    else:
        s["plan_digest"] = "0" * 64
    assert start(runner) == "blocked"


@pytest.mark.parametrize("field", list(ResourceBounds.model_fields))
@pytest.mark.parametrize("scope", ["project_bounds", "package_bounds"])
def test_quota_cannot_exceed_either_bound(runner, field, scope):
    bounds = getattr(runner.plan, scope)
    weakened = bounds.model_copy(update={field: getattr(bounds, field) - 1})
    runner.plan = runner.plan.model_copy(update={scope: weakened})
    runner.verifier.approved_plan_digest = runner.plan.digest
    with pytest.raises(BackendBlocked):
        start(runner)
    assert runner.client.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("image", "fixture.invalid/runner@sha256:" + "0" * 64),
        ("command", ("/bin/sh",)),
        ("package_digest", "0" * 64),
        ("project_id", "other"),
        ("namespace", "control"),
        ("operation_id", "other"),
    ],
)
def test_binding_mismatch_before_effect(runner, field, value):
    runner.plan = runner.plan.model_copy(
        update={"activation": runner.plan.activation.model_copy(update={field: value})}
    )
    runner.verifier.approved_plan_digest = runner.plan.digest
    with pytest.raises(BackendBlocked):
        start(runner)
    assert not runner.client.calls


@pytest.mark.parametrize("phase", ["create", "arm", "heartbeat", "unsuspend"])
def test_lost_reply_unknown_and_no_automatic_retry(runner, phase):
    runner.client.fail_at = phase
    assert start(runner) == "unknown"
    with pytest.raises(BackendBlocked):
        start(runner)
    before = list(runner.client.calls)
    assert runner.engine.reconcile(runner.plan) == "unknown"
    assert runner.client.calls == before + ["observe"]


def absence(h):
    return {
        "observed_at": NOW.isoformat(),
        "nonce": h.plan.activation.nonce,
        "namespace_uid": h.plan.activation.namespace_uid,
        "job_uid": "fake-job-uid",
        "job": {"kind": "Status", "code": 404, "reason": "NotFound"},
        "pods": {"kind": "PodList", "items": []},
    }


def test_cancel_is_not_termination_and_cleanup_preserves_foreign_resources(runner):
    h = runner
    assert start(h) == "pending"
    h.client.jobs["foreign"] = {"retained": True}
    assert h.engine.cancel(h.plan) == "cancel_requested"
    assert h.engine.reconcile(h.plan) == "unknown"
    assert h.engine.cancel(h.plan) == "unknown"
    assert h.client.calls.count("delete") == 1
    h.client.observation = absence(h)
    assert h.engine.reconcile(h.plan) == "terminated"
    assert h.client.jobs == {"foreign": {"retained": True}}
    assert (
        h.journal.receipts(h.plan.activation.operation_id)[-1]["custody"]
        == "quarantined_unreleased"
    )
    with pytest.raises(BackendBlocked):
        start(h)


@pytest.mark.parametrize("field", ["nonce", "namespace_uid", "job_uid", "observed_at", "pods"])
def test_cancellation_requires_fresh_exact_absence(runner, field):
    h = runner
    start(h)
    h.engine.cancel(h.plan)
    h.client.observation = absence(h)
    h.client.observation[field] = "wrong"
    assert h.engine.reconcile(h.plan) == "unknown"


def test_unknown_on_restart_retains_nonce(runner):
    h = runner
    start(h)
    root = h.journal.root
    h.journal.close()
    h.journal = RunnerJournal(root)
    try:
        engine = KubernetesCapabilityRunner(
            client=h.client,
            journal=h.journal,
            verifier=h.verifier,
            claim=lambda *_: True,
            now=lambda: NOW,
        )
        assert engine.monitor(h.plan, h.verified) == "unknown"
        with pytest.raises(BackendBlocked):
            engine.submit(h.plan, h.verified)
    finally:
        h.journal.close()


def running(h):
    job = copy.deepcopy(h.client.jobs[h.plan.name])
    pod = copy.deepcopy(job["spec"]["template"])
    pod["metadata"].update(
        namespace=h.plan.activation.namespace,
        uid="pod-uid",
        ownerReferences=[
            {"kind": "Job", "name": h.plan.name, "uid": "fake-job-uid", "controller": True}
        ],
    )
    pod["status"] = {
        "containerStatuses": [
            {
                "name": "installer",
                "ready": True,
                "restartCount": 0,
                "imageID": h.plan.activation.image,
                "state": {"running": {}},
            }
        ]
    }
    return {"observed_at": NOW.isoformat(), "job": job, "pods": {"kind": "PodList", "items": [pod]}}


@pytest.mark.parametrize(
    "fault",
    [None, "image", "owner", "nonce", "not-ready", "heartbeat-replay", "heartbeat-identity"],
)
def test_monitor_readiness_and_heartbeat(runner, fault):
    h = runner
    start(h)
    h.client.heartbeat_response["sequence"] = 2
    h.client.observation = running(h)
    pod = h.client.observation["pods"]["items"][0]
    if fault == "image":
        pod["status"]["containerStatuses"][0]["imageID"] = "other"
    elif fault == "owner":
        pod["metadata"]["ownerReferences"][0]["uid"] = "other"
    elif fault == "nonce":
        pod["metadata"]["annotations"]["tessera.io/nonce"] = "other"
    elif fault == "not-ready":
        pod["status"]["containerStatuses"][0]["ready"] = False
    elif fault == "heartbeat-replay":
        h.client.heartbeat_response["sequence"] = 1
    elif fault == "heartbeat-identity":
        h.client.heartbeat_response["identity"] = "workload"
    assert h.engine.monitor(h.plan, h.verified) == ("running" if fault is None else "unknown")


def test_raw_claims_or_unverified_package_never_enable_live(runner):
    h = runner
    with pytest.raises(BackendBlocked):
        h.engine.submit(h.plan, h.verified.package)
    h.verifier.authority = lambda _: False
    with pytest.raises(BackendBlocked):
        start(h)
    with pytest.raises(BackendBlocked):
        KubernetesCapabilityRunner(
            client=object(), journal=h.journal, verifier=h.verifier, claim=lambda *_: True
        )
    assert h.client.calls == []


def test_command_disabled_and_live_blocks_before_reading_config(capsys):
    assert main([]) == 0
    assert "disabled" in capsys.readouterr().out
    assert main(["--live", "--config", "/does/not/exist-secret"]) == 2
    assert "secret" not in capsys.readouterr().out
    assert main(["--schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["additionalProperties"] is False


def test_secret_upstream_error_not_in_receipts(runner):
    h = runner
    from dataclasses import replace

    h.verified = h.verifier.verify(
        replace(h.verified.package, signature="SECRET-FIXTURE-SIGNATURE")
    )
    h.client.fail_at = "create"
    start(h)
    receipts = json.dumps(h.journal.receipts(h.plan.activation.operation_id))
    assert "upstream" not in receipts and "signature" not in receipts
    assert h.verified.package.signature not in receipts


@pytest.mark.parametrize("index", range(19))
def test_pod_admission_and_ephemeral_update_evidence_required(runner, index):
    probes = runner.client.snapshot["probes"]
    key = [key for key in probes if key.startswith("pod-admission-")][index]
    del probes[key]
    assert start(runner) == "blocked"
    assert runner.client.calls == ["collect"]


@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("contradictory", [False, True])
def test_normal_terminal_requires_both_job_and_pod(runner, exit_code, contradictory):
    h = runner
    start(h)
    h.client.heartbeat_response["sequence"] = 2
    observation = running(h)
    observation["pods"]["items"][0]["status"]["containerStatuses"][0].update(
        ready=False, state={"terminated": {"exitCode": exit_code, "finishedAt": NOW.isoformat()}}
    )
    observation["job"]["status"] = {
        "active": int(contradictory),
        "succeeded": int(exit_code == 0),
        "failed": int(exit_code != 0),
        "conditions": [{"type": "Complete" if exit_code == 0 else "Failed", "status": "True"}],
    }
    h.client.observation = observation
    assert h.engine.monitor(h.plan, h.verified) == ("unknown" if contradictory else "terminated")
    assert "installed" not in json.dumps(h.journal.receipts(h.plan.activation.operation_id))


def test_lost_global_claim_is_unknown_without_create(runner):
    def lost(*_):
        raise OSError("secret")

    runner.engine.claim = lost
    assert start(runner) == "unknown"
    assert runner.client.calls == ["collect"]


def test_cleanup_checks_uid_and_full_nonce_not_truncated_label(runner):
    h = runner
    start(h)
    h.client.jobs[h.plan.name]["metadata"]["annotations"]["tessera.io/nonce"] = (
        h.plan.activation.nonce[:32] + "0" * 32
    )
    assert h.engine.cancel(h.plan) == "unknown"
    assert h.plan.name in h.client.jobs


def test_typed_raw_values_cannot_coerce_boolean_to_integer(runner):
    runner.client.snapshot["probes"]["watchdog-revoke"]["response"]["sequence"] = True
    assert start(runner) == "blocked"
