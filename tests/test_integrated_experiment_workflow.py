"""Synthetic, API-shaped goal-to-decision integration evidence.

This test deliberately uses injected OpenCode and Kubernetes effects.  It does
not contact a provider, model, or cluster, and it does not manufacture control
authority with SQL.
"""

from __future__ import annotations

import base64
import hashlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tessera_control.api import create_app
from tessera_control.experiments import ExperimentStore
from tessera_control.recovery import backup_state, restore_state
from tessera_control.snapshots import SnapshotStore
from tessera_control.store import Store, canonical_digest, initialize_state
from tessera_control.workspace import capture_workspace, inventory_digest
from tessera_worker.kubernetes_provider import (
    ClusterIdentity,
    JobBinding,
    KubernetesContractError,
    KubernetesJobJournal,
    parse_job,
    reconcile_cancellation,
    reconcile_create,
)
from tessera_worker.opencode_harness import (
    BoundedPlanMessage,
    OpenCodeEvent,
    OpenCodeHarnessAdapter,
    TesseraBinding,
)


class FakeOpenCode:
    """Provider-disabled in-process OpenCode shape with no network surface."""

    def __init__(self) -> None:
        self.sessions: list[dict[str, object]] = []
        self.messages: list[tuple[str, dict[str, object]]] = []
        self.stream: list[OpenCodeEvent] = []

    def version(self) -> str:
        return "1.2.3"

    def create_session(self, request: dict[str, object]) -> str:
        self.sessions.append(request)
        return "provider-disabled-session"

    def send_message(self, session_id: str, request: dict[str, object]) -> None:
        self.messages.append((session_id, request))

    def abort(self, _session_id: str) -> bool:
        return True

    def events(self, after_cursor: int | None) -> list[OpenCodeEvent]:
        return [
            event for event in self.stream if after_cursor is None or event.cursor > after_cursor
        ]


class FakeKubernetes:
    """Injected Job/Pod callbacks; it neither reads kubeconfig nor runs commands."""

    def __init__(self, binding: JobBinding, cluster: ClusterIdentity) -> None:
        self.binding, self.cluster = binding, cluster
        self.jobs: dict[str, dict[str, object]] = {}
        self.pods: list[dict[str, object]] = []
        self.create_calls = 0

    def create_lost_reply(self, manifest: dict[str, object]) -> None:
        self.create_calls += 1
        job = deepcopy(manifest)
        job["metadata"]["uid"] = f"job-{self.binding.operation_id}"  # type: ignore[index]
        job["status"] = {"active": 1}
        self.jobs[self.binding.job_name] = job
        template = deepcopy(manifest["spec"]["template"])  # type: ignore[index]
        template["metadata"].update(  # type: ignore[index,union-attr]
            {
                "namespace": self.cluster.namespace,
                "uid": f"pod-{self.binding.operation_id}",
                "ownerReferences": [
                    {"kind": "Job", "uid": job["metadata"]["uid"], "controller": True}  # type: ignore[index]
                ],
            }
        )
        self.pods = [
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": template["metadata"],  # type: ignore[index]
                "spec": template["spec"],  # type: ignore[index]
                "status": {"phase": "Running"},
            }
        ]
        # Returning None simulates a lost create reply after the side effect.
        return None

    def inspect(self, name: str) -> dict[str, object] | None:
        value = self.jobs.get(name)
        return deepcopy(value) if value is not None else None

    def complete(self, field: str) -> None:
        self.jobs[self.binding.job_name]["status"] = {field: 1}
        self.pods[0]["status"] = {"phase": "Succeeded" if field == "succeeded" else "Failed"}

    def delete(self, name: str, _uid: str) -> None:
        self.jobs.pop(name, None)
        self.pods.clear()

    def observe_pods(self, _job_uid: str) -> list[dict[str, object]]:
        return deepcopy(self.pods)


def _cluster() -> ClusterIdentity:
    return ClusterIdentity(
        kubeconfig="/private/synthetic/kubeconfig",
        kubeconfig_sha256="a" * 64,
        context="synthetic-context",
        profile="development-minikube-opencode-v1",
        server="https://127.0.0.1:8443",
        server_sha256="b" * 64,
        ca_sha256="c" * 64,
        namespace="synthetic",
        cluster_uid="synthetic-cluster",
        namespace_uid="synthetic-namespace",
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def test_synthetic_integrated_baseline_and_intervention_workflow(tmp_path: Path) -> None:
    state, source = tmp_path / "state", tmp_path / "source"
    source.mkdir()
    (source / "input.txt").write_text("public synthetic fixture")
    initialize_state(state)
    owner_token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    worker_token = "synthetic-worker-" + "x" * 48
    worker_headers = {"Authorization": f"Bearer {worker_token}"}
    counter = 0

    with TestClient(create_app(state)) as client:

        def call(
            method: str,
            path: str,
            body: dict[str, Any],
            expected: int | tuple[int, ...] = (200, 201),
            *,
            worker: bool = False,
            key: str | None = None,
        ) -> dict[str, Any]:
            nonlocal counter
            counter += 1
            response = client.request(
                method,
                path,
                json=body,
                headers=(worker_headers if worker else owner_headers)
                | {"Idempotency-Key": key or f"integrated-{counter}"},
            )
            expected_codes = (expected,) if isinstance(expected, int) else expected
            assert response.status_code in expected_codes, response.text
            return response.json()

        project = call("POST", "/projects", {"name": "Synthetic integrated workflow"}, 201)
        owner = Store(state).authenticate(owner_token)
        capture = capture_workspace(source)
        snapshot = SnapshotStore(state).adopt_snapshot(
            owner,
            project["id"],
            "synthetic-input",
            inventory_digest(capture.inventory),
            capture,
        )[1]
        credential = call(
            "POST",
            "/credentials",
            {
                "kind": "worker",
                "audience": "worker",
                "token": worker_token,
                "project_id": project["id"],
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
            201,
        )
        call(
            "POST",
            f"/projects/{project['id']}/workers",
            {
                "worker_principal_id": credential["principal_id"],
                "approved_build_identifier": "synthetic-integrated-worker",
                "approved_build_digest": "c" * 64,
                "trust_class": "test",
                "region": "local",
            },
            201,
        )
        call(
            "POST",
            f"/snapshots/{snapshot['id']}/releases",
            {
                "expected_digest": snapshot["digest"],
                "worker_principal_id": credential["principal_id"],
                "classification": "public",
                "permits_worker_transfer": True,
                "rationale": "Synthetic public input only.",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
            },
            201,
        )
        task = call(
            "POST",
            f"/projects/{project['id']}/tasks",
            {
                "title": "Synthetic baseline and intervention",
                "purpose": "Exercise the bounded integration ledger only.",
                "budget_cents": 240,
                "max_duration_seconds": 60,
            },
            201,
        )
        plan = call(
            "PUT",
            f"/tasks/{task['id']}/plan",
            {
                "expected_revision": task["revision"],
                "title": task["title"],
                "purpose": task["purpose"],
                "budget_cents": task["budget_cents"],
                "max_duration_seconds": task["max_duration_seconds"],
                "execution_spec": {
                    "argv": ["python", "-c", "print('synthetic')"],
                    "input_snapshot_digest": snapshot["digest"],
                    "pinned_skill_digests": [],
                    "output_paths": ["result.json"],
                    "profile": "local-cpu",
                    "network_enabled": False,
                    "max_output_bytes": 1024,
                },
            },
        )
        approved = call(
            "POST",
            f"/tasks/{task['id']}/approve",
            {
                "expected_revision": plan["revision"],
                "expected_plan_digest": plan["current_plan_digest"],
            },
        )
        call(
            "PUT",
            f"/projects/{project['id']}/execution-limits",
            {
                "expected_revision": 0,
                "currency": "USD",
                "monetary_limit_cents": 240,
                "max_concurrency": 4,
            },
            201,
        )

        def approved_task(label: str) -> tuple[dict[str, Any], dict[str, Any]]:
            fresh_task = call(
                "POST",
                f"/projects/{project['id']}/tasks",
                {
                    "title": f"Synthetic {label}",
                    "purpose": "Exercise the bounded integration ledger only.",
                    "budget_cents": 60,
                    "max_duration_seconds": 60,
                },
                201,
            )
            fresh_plan = call(
                "PUT",
                f"/tasks/{fresh_task['id']}/plan",
                {
                    "expected_revision": fresh_task["revision"],
                    "title": fresh_task["title"],
                    "purpose": fresh_task["purpose"],
                    "budget_cents": fresh_task["budget_cents"],
                    "max_duration_seconds": fresh_task["max_duration_seconds"],
                    "execution_spec": {
                        "argv": ["python", "-c", "print('synthetic')"],
                        "input_snapshot_digest": snapshot["digest"],
                        "pinned_skill_digests": [],
                        "output_paths": ["result.json"],
                        "profile": "local-cpu",
                        "network_enabled": False,
                        "max_output_bytes": 1024,
                    },
                },
            )
            return fresh_task, call(
                "POST",
                f"/tasks/{fresh_task['id']}/approve",
                {
                    "expected_revision": fresh_plan["revision"],
                    "expected_plan_digest": fresh_plan["current_plan_digest"],
                },
            )

        def design_for(
            active_task: dict[str, Any],
            active_plan: dict[str, Any],
            expected_revision: int | None = None,
        ) -> dict[str, Any]:
            body: dict[str, Any] = {
                "goal": "Compare one synthetic baseline with one intervention.",
                "success_criterion": "A human reviews quarantined synthetic evidence.",
                "protocol": "No live execution; retain all terminal labels.",
                "variants": [
                    {
                        "kind": "baseline",
                        "name": "baseline",
                        "method_digest": "d" * 64,
                        "input_digest": "e" * 64,
                        "execution_task_id": active_task["id"],
                        "execution_plan_digest": active_plan["current_plan_digest"],
                        "metadata": {"synthetic": True},
                    },
                    {
                        "kind": "intervention",
                        "name": "intervention",
                        "method_digest": "f" * 64,
                        "input_digest": "0" * 64,
                        "execution_task_id": active_task["id"],
                        "execution_plan_digest": active_plan["current_plan_digest"],
                        "metadata": {"synthetic": True},
                    },
                ],
            }
            if expected_revision is not None:
                body["expected_revision"] = expected_revision
            return body

        experiment = call(
            "POST", f"/projects/{project['id']}/experiments", design_for(task, approved), 201
        )
        variants = {variant["kind"]: variant for variant in experiment["design"]["variants"]}

        def admitted_package(
            variant: dict[str, Any],
            label: str,
            active_task: dict[str, Any],
            active_plan: dict[str, Any],
        ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
            grant = call(
                "POST",
                f"/projects/{project['id']}/execution-grants",
                {
                    "task_id": active_task["id"],
                    "expected_task_revision": active_plan["revision"],
                    "expected_plan_digest": active_plan["current_plan_digest"],
                    "worker_principal_id": credential["principal_id"],
                    "snapshot_id": snapshot["id"],
                    "pinned_image": "python@sha256:" + "1" * 64,
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                    "max_exposure_cents": 60,
                    "currency": "USD",
                    "acknowledges_unmeasured_infrastructure": True,
                },
                201,
            )
            lease = call("POST", "/worker/leases", {"grant_id": grant["id"]}, 201, worker=True)
            admitted = call(
                "POST",
                f"/experiment-variants/{variant['id']}/admitted-attempts",
                {
                    "expected_variant_revision": variant["revision"],
                    "expected_design_revision": experiment["current_design_revision"],
                    "grant_id": grant["id"],
                },
                201,
            )
            package_body = {"expected_epoch": lease["epoch"], "admitted_attempt_id": admitted["id"]}
            package = call(
                "POST",
                f"/worker/leases/{lease['id']}/package",
                package_body,
                201,
                worker=True,
                key=f"package-{label}",
            )
            replay = call(
                "POST",
                f"/worker/leases/{lease['id']}/package",
                package_body,
                201,
                worker=True,
                key=f"package-{label}",
            )
            assert replay == package
            return grant, lease, package

        def kube(
            package: dict[str, Any], attempt_id: str, label: str
        ) -> tuple[FakeKubernetes, KubernetesJobJournal]:
            binding = JobBinding(
                operation_id=package["operation"]["operation_id"],
                attempt_id=attempt_id,
                lease_id=package["lease"]["id"],
                epoch=package["lease"]["epoch"],
                package_sha256=package["package_digest"],
                image=package["package"]["pinned_image"],
                command=("opencode", "serve"),
                active_deadline_seconds=60,
            )
            fake, journal = (
                FakeKubernetes(binding, _cluster()),
                KubernetesJobJournal(tmp_path / f"kubernetes-{label}.sqlite3"),
            )
            first = reconcile_create(
                journal, binding, fake.cluster, fake.inspect, fake.create_lost_reply
            )
            assert first.state == "running" and fake.create_calls == 1
            # Reconciliation after the lost response only inspects the canonical name.
            assert (
                reconcile_create(
                    journal, binding, fake.cluster, fake.inspect, fake.create_lost_reply
                )
                == first
            )
            assert fake.create_calls == 1
            return fake, journal

        baseline_grant, baseline_lease, baseline_package = admitted_package(
            variants["baseline"], "success", task, approved
        )
        baseline_attempt = baseline_package["admitted_attempt_id"]
        baseline_kube, _ = kube(baseline_package, baseline_attempt, "success")
        baseline_kube.complete("succeeded")
        assert (
            parse_job(
                baseline_kube.inspect(baseline_kube.binding.job_name),
                baseline_kube.binding,
                baseline_kube.cluster,
            ).state
            == "succeeded"
        )

        fake_opencode = FakeOpenCode()
        prompts: list[str] = []
        harness_binding = TesseraBinding(
            owner["org_id"],
            project["id"],
            task["id"],
            baseline_package["operation"]["operation_id"],
            baseline_lease["epoch"],
            "synthetic-harness",
            baseline_attempt,
            baseline_grant["id"],
            baseline_attempt,
        )
        harness = OpenCodeHarnessAdapter(
            fake_opencode,
            pinned_version="1.2.3",
            authority_is_current=lambda binding, _context: binding.attempt_id == baseline_attempt,
            decision_callback=lambda request: prompts.append(request.prompt_kind),
        )
        session = harness.start(harness_binding, BoundedPlanMessage("plan-1", "Synthetic context."))
        fake_opencode.stream = [
            OpenCodeEvent(
                owner["org_id"],
                project["id"],
                task["id"],
                baseline_package["operation"]["operation_id"],
                baseline_lease["epoch"],
                "synthetic-harness",
                session.session_id or "",
                0,
                "provider-disabled",
                "provider-auth",
                {"provider": "disabled"},
            )
        ]
        assert harness.poll(baseline_package["operation"]["operation_id"])[0].acceptance == "none"
        assert prompts == ["provider-auth"] and len(fake_opencode.sessions) == 1

        def receipt_and_observation(
            lease: dict[str, Any],
            package: dict[str, Any],
            when: str,
            state: str = "exited",
            confirmed: bool = True,
        ) -> None:
            operation_id = package["operation"]["operation_id"]
            receipt = {
                "lease_id": lease["id"],
                "expected_epoch": lease["epoch"],
                "provider_kind": "fake",
                "provider_version": "synthetic-kubernetes-v1",
                "provider_id": f"fake-{operation_id}",
                "receipt_payload": {"synthetic": "kubernetes", "operation_id": operation_id},
                "receipt_digest": canonical_digest(
                    {"synthetic": "kubernetes", "operation_id": operation_id}
                ),
                "observed_state": state,
                "observed_at": when,
                "termination_confirmed": confirmed,
            }
            call(
                "POST",
                f"/worker/external-operations/{operation_id}/receipt",
                receipt,
                201,
                worker=True,
            )
            call(
                "POST",
                "/worker/observations",
                {
                    "lease_id": lease["id"],
                    "expected_epoch": lease["epoch"],
                    "operation_id": operation_id,
                    "observed_state": state,
                    "observed_at": when,
                    "provider_id": receipt["provider_id"],
                    "termination_confirmed": confirmed,
                },
                201,
                worker=True,
            )

        baseline_time = _now()
        receipt_and_observation(baseline_lease, baseline_package, baseline_time)
        baseline_current = client.get(
            f"/experiments/{experiment['id']}", headers=owner_headers
        ).json()["admitted_attempts"]
        baseline_current = next(item for item in baseline_current if item["id"] == baseline_attempt)
        baseline_resolved = call(
            "POST",
            f"/experiment-admitted-attempts/{baseline_attempt}/resolve",
            {
                "expected_revision": baseline_current["revision"],
                "outcome": "succeeded",
                "reason": None,
            },
        )

        artifact_bytes = b'{"duration_seconds": 1}'
        artifact = call(
            "POST",
            f"/worker/leases/{baseline_lease['id']}/artifacts",
            {
                "expected_epoch": baseline_lease["epoch"],
                "operation_id": baseline_package["operation"]["operation_id"],
                "relative_path": "result.json",
                "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
                "content_base64": base64.b64encode(artifact_bytes).decode(),
            },
            201,
            worker=True,
        )
        placement = call(
            "POST",
            f"/worker/leases/{baseline_lease['id']}/project-artifacts",
            {
                "expected_epoch": baseline_lease["epoch"],
                "operation_id": baseline_package["operation"]["operation_id"],
                "artifacts": [
                    {"id": artifact["id"], "sha256": artifact["sha256"], "size": artifact["size"]}
                ],
            },
            201,
            worker=True,
        )
        assert placement["placements"][0]["artifact_id"] == artifact["id"]
        manifest = call(
            "POST",
            f"/worker/leases/{baseline_lease['id']}/artifact-manifest",
            {
                "expected_epoch": baseline_lease["epoch"],
                "operation_id": baseline_package["operation"]["operation_id"],
                "artifacts": [
                    {
                        "id": artifact["id"],
                        "relative_path": "result.json",
                        "sha256": artifact["sha256"],
                        "size": artifact["size"],
                    }
                ],
            },
            201,
            worker=True,
        )
        assert manifest["status"] == "quarantined" and manifest["classification"] == "unclassified"
        linked = call(
            "POST",
            f"/experiment-attempts/{baseline_attempt}/artifacts",
            {"expected_revision": baseline_resolved["revision"], "artifact_ids": [artifact["id"]]},
        )
        measurement = call(
            "POST",
            f"/experiment-attempts/{baseline_attempt}/measurements",
            {
                "expected_revision": linked["revision"],
                "name": "synthetic duration",
                "value": {"seconds": 1},
                "artifact_id": artifact["id"],
            },
            201,
        )

        call(
            "POST",
            f"/execution-grants/{baseline_grant['id']}/reconcile",
            {
                "expected_revision": baseline_grant["revision"],
                "settlement_cents": 0,
                "provider_terminated": True,
            },
        )
        failed_task, failed_plan = approved_task("failed baseline")
        experiment = call(
            "POST",
            f"/experiments/{experiment['id']}/designs",
            design_for(failed_task, failed_plan, experiment["current_design_revision"]),
            201,
        )
        variants = {variant["kind"]: variant for variant in experiment["design"]["variants"]}
        failed_grant, failed_lease, failed_package = admitted_package(
            variants["baseline"], "failed", failed_task, failed_plan
        )
        failed_attempt = failed_package["admitted_attempt_id"]
        failed_kube, _ = kube(failed_package, failed_attempt, "failed")
        failed_kube.complete("failed")
        failed_time = _now()
        receipt_and_observation(failed_lease, failed_package, failed_time)
        operation_id = failed_package["operation"]["operation_id"]
        contradiction = {
            "lease_id": failed_lease["id"],
            "expected_epoch": failed_lease["epoch"],
            "provider_kind": "fake",
            "provider_version": "synthetic-kubernetes-v1",
            "provider_id": f"fake-{operation_id}",
            "receipt_payload": {"synthetic": "kubernetes", "operation_id": operation_id},
            "receipt_digest": canonical_digest(
                {"synthetic": "kubernetes", "operation_id": operation_id}
            ),
            "observed_state": "unknown",
            "observed_at": failed_time,
            "termination_confirmed": False,
        }
        call(
            "POST",
            f"/worker/external-operations/{operation_id}/receipt",
            contradiction,
            201,
            worker=True,
        )
        failed_current = next(
            item
            for item in client.get(
                f"/experiments/{experiment['id']}", headers=owner_headers
            ).json()["admitted_attempts"]
            if item["id"] == failed_attempt
        )
        assert failed_current["operational_observed_state"] == "unknown"
        call(
            "POST",
            f"/experiment-admitted-attempts/{failed_attempt}/resolve",
            {
                "expected_revision": failed_current["revision"],
                "outcome": "succeeded",
                "reason": None,
            },
            409,
        )
        call(
            "POST",
            f"/experiment-admitted-attempts/{failed_attempt}/resolve",
            {
                "expected_revision": failed_current["revision"],
                "outcome": "failed",
                "reason": "Contradictory worker/provider observations remain unresolved.",
            },
        )

        cancelled_task, cancelled_plan = approved_task("cancelled intervention")
        experiment = call(
            "POST",
            f"/experiments/{experiment['id']}/designs",
            design_for(cancelled_task, cancelled_plan, experiment["current_design_revision"]),
            201,
        )
        variants = {variant["kind"]: variant for variant in experiment["design"]["variants"]}
        cancelled_grant, cancelled_lease, cancelled_package = admitted_package(
            variants["intervention"], "cancelled", cancelled_task, cancelled_plan
        )
        cancelled_attempt = cancelled_package["admitted_attempt_id"]
        cancelled_kube, cancelled_journal = kube(cancelled_package, cancelled_attempt, "cancelled")
        revoked = call(
            "POST",
            f"/execution-grants/{cancelled_grant['id']}/revoke",
            {"expected_revision": cancelled_grant["revision"]},
        )
        cancellation = reconcile_cancellation(
            cancelled_journal,
            cancelled_kube.binding,
            cancelled_kube.cluster,
            cancelled_kube.inspect,
            cancelled_kube.delete,
            cancelled_kube.observe_pods,
        )
        assert cancellation.termination_confirmed is True
        with pytest.raises(KubernetesContractError, match="cancel"):
            reconcile_create(
                cancelled_journal,
                cancelled_kube.binding,
                cancelled_kube.cluster,
                cancelled_kube.inspect,
                cancelled_kube.create_lost_reply,
            )
        call(
            "POST",
            f"/worker/leases/{cancelled_lease['id']}/package",
            {"expected_epoch": cancelled_lease["epoch"], "admitted_attempt_id": cancelled_attempt},
            409,
            worker=True,
        )
        receipt_and_observation(cancelled_lease, cancelled_package, _now())
        cancelled_current = next(
            item
            for item in client.get(
                f"/experiments/{experiment['id']}", headers=owner_headers
            ).json()["admitted_attempts"]
            if item["id"] == cancelled_attempt
        )
        assert revoked["status"] == "cancel_requested"
        call(
            "POST",
            f"/experiment-admitted-attempts/{cancelled_attempt}/resolve",
            {
                "expected_revision": cancelled_current["revision"],
                "outcome": "cancelled",
                "reason": "Human recorded the confirmed synthetic cancellation.",
            },
        )

        unknown_task, unknown_plan = approved_task("unknown intervention")
        experiment = call(
            "POST",
            f"/experiments/{experiment['id']}/designs",
            design_for(unknown_task, unknown_plan, experiment["current_design_revision"]),
            201,
        )
        variants = {variant["kind"]: variant for variant in experiment["design"]["variants"]}
        _unknown_grant, unknown_lease, unknown_package = admitted_package(
            variants["intervention"], "unknown", unknown_task, unknown_plan
        )
        unknown_attempt = unknown_package["admitted_attempt_id"]
        unknown_kube, _ = kube(unknown_package, unknown_attempt, "unknown")
        call(
            "POST",
            "/worker/observations",
            {
                "lease_id": unknown_lease["id"],
                "expected_epoch": unknown_lease["epoch"],
                "operation_id": unknown_package["operation"]["operation_id"],
                "observed_state": "unknown",
                "observed_at": _now(),
                "provider_id": f"fake-{unknown_package['operation']['operation_id']}",
                "termination_confirmed": False,
            },
            201,
            worker=True,
        )
        unknown_current = next(
            item
            for item in client.get(
                f"/experiments/{experiment['id']}", headers=owner_headers
            ).json()["admitted_attempts"]
            if item["id"] == unknown_attempt
        )
        call(
            "POST",
            f"/experiment-admitted-attempts/{unknown_attempt}/resolve",
            {
                "expected_revision": unknown_current["revision"],
                "outcome": "unknown",
                "reason": "The synthetic Job/Pod observation remained unknown.",
            },
        )
        assert unknown_kube.create_calls == 1

        conclusion = call(
            "POST",
            f"/experiments/{experiment['id']}/conclusions",
            {
                "statement": (
                    "Synthetic baseline evidence was reviewed; no scientific claim is made."
                ),
                "evidence": [
                    {
                        "attempt_id": baseline_attempt,
                        "artifact_ids": [artifact["id"]],
                        "measurement_ids": [measurement["id"]],
                    }
                ],
            },
            201,
        )
        reviewed = call(
            "POST",
            f"/conclusions/{conclusion['id']}/review",
            {"expected_revision": conclusion["revision"]},
        )
        call(
            "PUT",
            f"/conclusions/{reviewed['id']}",
            {
                "expected_revision": reviewed["revision"],
                "statement": "Mutation must fail after review.",
                "evidence": [
                    {
                        "attempt_id": baseline_attempt,
                        "artifact_ids": [artifact["id"]],
                        "measurement_ids": [measurement["id"]],
                    }
                ],
            },
            409,
        )
        exported = client.get(
            f"/experiments/{experiment['id']}/export?max_bytes=1000000", headers=owner_headers
        )
        assert exported.status_code == 200, exported.text
        exported_body = exported.json()
        assert len(exported.content) <= 1_000_000
        reopened = client.get(f"/experiments/{experiment['id']}", headers=owner_headers).json()
        admitted = reopened["admitted_attempts"]
        assert {item["id"] for item in admitted} == {
            baseline_attempt,
            failed_attempt,
            cancelled_attempt,
            unknown_attempt,
        }
        operations = {item["operation_id"]: item for item in reopened["operations"]}
        assert all(item["operation_id"] in operations for item in admitted)
        baseline_operation_id = baseline_package["operation"]["operation_id"]
        assert operations[baseline_operation_id]["experiment_attempt_id"] == baseline_attempt
        assert artifact["id"] in {item["artifact_id"] for item in reopened["artifact_links"]}
        assert measurement["id"] in {item["id"] for item in reopened["measurements"]}
        assert exported_body["admitted_attempts"] == admitted
        assert exported_body["conclusions"][0]["status"] == "reviewed"
        assert exported_body["conclusions"][0]["scientific_validity"] == "not_assessed"
        assert exported_body["artifacts"][0]["custody_state"] == "active"

    backup, restored = tmp_path / "backup", tmp_path / "restored"
    backup_state(state, backup)
    restore_state(backup, restored)
    held_token = (restored / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    held_owner = Store(restored).authenticate(held_token)
    recovered = ExperimentStore(restored).get_experiment(held_owner, experiment["id"])
    assert recovered["admitted_attempts"] == admitted
    assert recovered["conclusions"][0]["id"] == reviewed["id"]
