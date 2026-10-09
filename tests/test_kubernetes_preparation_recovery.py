"""Synthetic original-journal proofs; no live provider or outcome fabrication."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest
from test_kubernetes_execution import _admitted_package, _cluster

from tessera_worker.kubernetes_contracts import (
    KubernetesJobJournal,
    KubernetesUnresolvedError,
    canonical_json,
    job_manifest,
)
from tessera_worker.kubernetes_execution import KubernetesExecutionAdapter
from tessera_worker.kubernetes_output_collector import KubernetesOutputCollector
from tessera_worker.kubernetes_preparation_recovery import attest_preparation_failure


def fixture(tmp_path):
    _, _, package = _admitted_package(tmp_path)
    package = replace(
        package,
        output=replace(package.output, operation_scoped=True),
        binding=replace(package.binding, output_operation_scoped=True),
    )
    cluster = _cluster()
    journal = tmp_path / "jobs.sqlite3"
    adapter = KubernetesExecutionAdapter(
        journal,
        cluster,
        inspect_job=lambda _: None,
        create_job=lambda _: pytest.fail("no workload submission"),
        delete_job=lambda *_: pytest.fail("no Job cleanup"),
        observe_pods=lambda _: [],
    )
    adapter._bind(package)
    adapter._journal._bind(package.binding, job_manifest(package.binding, cluster))
    with sqlite3.connect(journal) as db:
        db.execute(
            "UPDATE kubernetes_jobs SET create_intent=1 WHERE operation_id=?",
            (package.operation_id,),
        )

    class Client:
        def __init__(self):
            self.cluster = cluster
            self.pod = None
            self.workloads = []

        def verify(self, output):
            assert output == package.output

        def get(self, kind, name):
            if kind == "namespace":
                return {"metadata": {"name": name, "uid": cluster.namespace_uid}}
            if kind == "job":
                return None
            assert kind == "pod"
            return self.pod

        def path(self, kind):
            return "/" + kind

        def request(self, method, path):
            assert method == "GET" and "labelSelector=" in path
            return {
                "kind": "JobList" if path.startswith("/job?") else "PodList",
                "metadata": {},
                "items": self.workloads,
            }

    client = Client()
    collector = KubernetesOutputCollector(
        client, package.output, "new/tessera@sha256:" + "d" * 64, tmp_path / "collectors.sqlite3"
    )
    manifest = collector.manifest("prepare", package, image="old/tessera@sha256:" + "e" * 64)
    with sqlite3.connect(collector.journal) as db:
        db.execute(
            "INSERT INTO collectors VALUES (?, ?, 'prepared-owned', NULL)",
            (manifest["metadata"]["name"], canonical_json(manifest).decode()),
        )
    client.pod = json.loads(json.dumps(manifest))
    client.pod["metadata"]["uid"] = "prepared-owned"
    client.pod["status"] = {"phase": "Succeeded"}
    del client.pod["spec"]["containers"][0]["volumeMounts"][0]["readOnly"]
    del client.pod["spec"]["volumes"][0]["persistentVolumeClaim"]["readOnly"]
    return package, collector, journal, adapter


def test_historical_proof_preserves_true_intent_unknown_outcome_and_null_provider(tmp_path):
    package, collector, journal, _ = fixture(tmp_path)
    proof = attest_preparation_failure(package, collector, journal, exclusive_stopped_worker=True)
    assert proof["proof_kind"] == "pre_job_preparation"
    assert proof["create_intent"] is True
    assert proof["job_request_not_reached"] is True
    assert proof["job_uid"] is None and "provider_id" not in proof
    assert "termination_confirmed" not in proof
    assert proof["preparation_journal_pod_uid"] == proof["preparation_pod_uid"] == "prepared-owned"
    assert proof["worker_attests_original_exclusive_stopped"] is True
    with sqlite3.connect(journal) as db:
        assert db.execute(
            "SELECT create_intent,cancellation_requested,job_uid FROM kubernetes_jobs"
        ).fetchone() == (1, 1, None)
        assert db.execute(
            "SELECT observed_state,job_uid,cancellation_requested "
            "FROM kubernetes_execution_bindings"
        ).fetchone() == ("unknown", None, 1)
    from pydantic import ValidationError

    from tessera_control.execution_models import (
        AttestKubernetesNeverSubmitted,
        AttestKubernetesPreparationNotSubmitted,
    )

    assert (
        AttestKubernetesPreparationNotSubmitted.model_validate(
            proof | {"expected_revision": 1}
        ).proof_kind
        == "pre_job_preparation"
    )

    with pytest.raises(ValidationError):
        AttestKubernetesNeverSubmitted.model_validate(proof | {"expected_revision": 1})


@pytest.mark.parametrize(
    "change",
    [
        "receipt",
        "foreign_spec",
        "job_uid",
        "missing_job",
        "workload_exists",
        "missing_preparation_uid",
        "replacement_preparation_uid",
    ],
)
def test_historical_proof_rejects_incomplete_conflicting_or_started_evidence(tmp_path, change):
    package, collector, journal, _ = fixture(tmp_path)
    if change == "receipt":
        with sqlite3.connect(collector.journal) as db:
            db.execute("UPDATE collectors SET result='{}'")
    elif change == "foreign_spec":
        collector.client.pod["spec"]["containers"][0]["image"] = "foreign@sha256:" + "f" * 64
    elif change == "job_uid":
        with sqlite3.connect(journal) as db:
            db.execute("UPDATE kubernetes_jobs SET job_uid='started-job'")
    elif change == "missing_job":
        with sqlite3.connect(journal) as db:
            db.execute("DELETE FROM kubernetes_jobs")
    elif change == "missing_preparation_uid":
        with sqlite3.connect(collector.journal) as db:
            db.execute("UPDATE collectors SET uid=NULL")
        collector.client.get = lambda *_: pytest.fail(
            "missing original UID must fail before current lookup"
        )
    elif change == "replacement_preparation_uid":
        collector.client.pod["metadata"]["uid"] = "replacement"
    else:
        collector.client.workloads = [{"metadata": {"uid": "foreign-workload"}}]
    with pytest.raises((ValueError, KubernetesUnresolvedError)):
        attest_preparation_failure(package, collector, journal, exclusive_stopped_worker=True)
    if change in {"foreign_spec", "workload_exists"}:
        with sqlite3.connect(journal) as db:
            assert db.execute("SELECT cancellation_requested FROM kubernetes_jobs").fetchone() == (
                1,
            )
    if change == "missing_preparation_uid":
        with sqlite3.connect(collector.journal) as db:
            assert db.execute("SELECT uid FROM collectors").fetchone() == (None,)
        with sqlite3.connect(journal) as db:
            assert db.execute(
                "SELECT create_intent,cancellation_requested,job_uid FROM kubernetes_jobs"
            ).fetchone() == (1, 0, None)


def test_generator_requires_explicit_stopped_exclusive_assertion(tmp_path):
    package, collector, journal, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match="explicitly stopped and exclusive"):
        attest_preparation_failure(package, collector, journal)
    with pytest.raises(ValueError, match="explicitly stopped and exclusive"):
        attest_preparation_failure(package, collector, journal, exclusive_stopped_worker=False)


def test_zero_create_cli_proof_uses_existing_contract_without_helper_uid_backfill(tmp_path):
    from tessera_control.execution_models import AttestKubernetesNeverSubmitted
    from tessera_worker.kubernetes_preparation_recovery import attest_bound_job_intent

    package, collector, path, adapter = fixture(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE kubernetes_jobs SET create_intent=0,cancellation_requested=1")
        db.execute("UPDATE kubernetes_execution_bindings SET cancellation_requested=1")
    with sqlite3.connect(collector.journal) as db:
        db.execute("UPDATE collectors SET uid=NULL")
    proof = attest_bound_job_intent(
        package, collector, adapter._journal, exclusive_stopped_worker=True
    )
    assert proof["proof_kind"] == "bound_job_intent" and proof["create_intent"] is False
    assert "termination_confirmed" not in proof and "provider_id" not in proof
    AttestKubernetesNeverSubmitted.model_validate(proof | {"expected_revision": 1})
    with sqlite3.connect(collector.journal) as db:
        assert db.execute("SELECT uid FROM collectors").fetchone() == (None,)
    with pytest.raises(ValueError):
        attest_bound_job_intent(package, collector, adapter._journal)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE kubernetes_jobs SET create_intent=1")
    with pytest.raises(KubernetesUnresolvedError):
        attest_bound_job_intent(package, collector, adapter._journal, exclusive_stopped_worker=True)


def test_future_failed_prepare_never_writes_job_submission_intent(tmp_path):
    _, _, package = _admitted_package(tmp_path)
    cluster, journal = _cluster(), tmp_path / "jobs.sqlite3"

    def failed_prepare(_):
        raise ValueError("preparation gate rejected")

    adapter = KubernetesExecutionAdapter(
        journal,
        cluster,
        inspect_job=lambda _: None,
        create_job=lambda _: pytest.fail("no Job submission"),
        delete_job=lambda *_: pytest.fail("no deletion"),
        observe_pods=lambda _: [],
        prepare_output=failed_prepare,
    )
    with pytest.raises(ValueError, match="preparation gate"):
        adapter.submit(package)
    assert adapter.cancel(package).termination_confirmed is False
    proof = KubernetesJobJournal(journal).attest_never_submitted(
        package.binding,
        cluster,
        inspect_namespace=lambda name: {"metadata": {"name": name, "uid": cluster.namespace_uid}},
        inspect_job=lambda _: None,
        list_operation_jobs=lambda _: [],
        list_operation_pods=lambda _: [],
    )
    assert proof["create_intent"] is False and proof["job_uid"] is None
