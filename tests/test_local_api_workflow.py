"""Ordinary CLI/API/worker path with fake Kubernetes effects and synthetic bytes.

No direct store mutations, hand-posted execution observations, provider receipts,
cluster clients, or real credentials. This is integration evidence, not live work.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_kubernetes_execution import _cluster, _FakeKubernetes

from tessera_control.api import create_app
from tessera_control.cli import main
from tessera_worker.control_channel import ControlChannelError
from tessera_worker.kubernetes_contracts import cluster_identity_digest
from tessera_worker.kubernetes_dispatch_service import (
    KubernetesDispatchWorkerConfig,
    KubernetesDispatchWorkerService,
)
from tessera_worker.kubernetes_execution import KubernetesExecutionPackage, KubernetesOutputBinding


class Journey:
    def __init__(self, root, monkeypatch, capsys):
        self.root, self.state = root, root / "state"
        self.monkeypatch, self.capsys = monkeypatch, capsys
        self.cli("init", "--state-dir", self.state)
        self.client = TestClient(create_app(self.state))
        self.owner = (self.state / "owner.header").read_text().strip().split(": ", 1)[1]
        self.worker = "Bearer synthetic-worker-" + "x" * 48
        self.sequence = 0
        self.project = self.call("POST", "/projects", {"name": "Synthetic API comparison"})
        source = root / "source"
        source.mkdir()
        (source / "input.txt").write_text("public synthetic input\n")
        inventory = self.cli("workspace", "inventory", "--root", source)
        self.snapshot = self.cli(
            "workspace",
            "adopt",
            "--root",
            source,
            "--state-dir",
            self.state,
            "--project-id",
            self.project["id"],
            "--expected-digest",
            inventory["digest"],
            "--idempotency-key",
            "adopt-public-input",
        )
        self.credential = self.call(
            "POST",
            "/credentials",
            {
                "kind": "worker",
                "audience": "worker",
                "token": self.worker.removeprefix("Bearer "),
                "project_id": self.project["id"],
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
        self.call(
            "POST",
            f"/projects/{self.project['id']}/workers",
            {
                "worker_principal_id": self.credential["principal_id"],
                "approved_build_identifier": "synthetic-api-worker",
                "approved_build_digest": "a" * 64,
                "trust_class": "test",
                "region": "local",
            },
        )
        self.call(
            "POST",
            f"/snapshots/{self.snapshot['id']}/releases",
            {
                "expected_digest": self.snapshot["digest"],
                "worker_principal_id": self.credential["principal_id"],
                "classification": "public",
                "permits_worker_transfer": True,
                "rationale": "Synthetic input only.",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
            },
        )
        self.call(
            "PUT",
            f"/projects/{self.project['id']}/execution-limits",
            {
                "expected_revision": 0,
                "currency": "USD",
                "monetary_limit_cents": 20,
                "max_concurrency": 2,
            },
        )
        self.tasks = {}
        variants = []
        for kind, multiplier in (("baseline", 1), ("intervention", 2)):
            task = self.call(
                "POST",
                f"/projects/{self.project['id']}/tasks",
                {
                    "title": kind,
                    "purpose": "Compare synthetic fixture outputs, not scientific truth.",
                    "budget_cents": 10,
                    "max_duration_seconds": 300,
                },
            )
            task = self.call(
                "PUT",
                f"/tasks/{task['id']}/plan",
                {
                    "expected_revision": task["revision"],
                    "title": task["title"],
                    "purpose": task["purpose"],
                    "budget_cents": 10,
                    "max_duration_seconds": 300,
                    "execution_spec": {
                        "argv": ["python", "-c", f"print({multiplier})"],
                        "input_snapshot_digest": self.snapshot["digest"],
                        "pinned_skill_digests": [],
                        "output_paths": ["result.txt"],
                        "profile": "kubernetes-opencode",
                        "network_enabled": False,
                        "max_output_bytes": 1024,
                    },
                },
            )
            self.tasks[kind] = task
            variants.append(
                {
                    "kind": kind,
                    "name": kind,
                    "method_digest": task["current_plan_digest"],
                    "input_digest": self.snapshot["digest"],
                    "metadata": {"synthetic": True},
                    "execution_task_id": task["id"],
                    "execution_plan_digest": task["current_plan_digest"],
                }
            )
        self.experiment = self.call(
            "POST",
            f"/projects/{self.project['id']}/experiments",
            {
                "goal": "Compare one registered baseline and intervention through ordinary APIs.",
                "success_criterion": "Retain both outcomes and review synthetic evidence.",
                "protocol": "Injected Kubernetes effects; no code or provider execution.",
                "variants": variants,
            },
        )
        self.cluster = _cluster()
        self.worker_root = root / "worker"
        self.worker_root.mkdir(mode=0o700)
        self.credential_file = root / "worker.credential"
        self.credential_file.write_text(self.worker.removeprefix("Bearer "))
        self.credential_file.chmod(0o600)

    def cli(self, *args):
        self.monkeypatch.setattr(sys, "argv", ["tessera", *map(str, args)])
        assert main() == 0
        captured = self.capsys.readouterr()
        assert not captured.err
        return json.loads(captured.out) if args[0] != "init" else None

    def call(self, method, path, body=None, *, worker=False, key=None, expected=(200, 201)):
        self.sequence += 1
        response = self.client.request(
            method,
            path,
            json=body,
            headers={
                "Authorization": self.worker if worker else self.owner,
                "Idempotency-Key": key or f"journey-{self.sequence}",
            },
        )
        assert response.status_code in expected, response.text
        return response.json()

    def export(self):
        return self.call("GET", f"/experiments/{self.experiment['id']}/export")

    def attempt(self, attempt_id):
        return next(item for item in self.export()["admitted_attempts"] if item["id"] == attempt_id)

    def admit(self, kind):
        task = self.tasks[kind]
        grant_body = {
            "task_id": task["id"],
            "expected_task_revision": task["revision"],
            "expected_plan_digest": task["current_plan_digest"],
            "worker_principal_id": self.credential["principal_id"],
            "snapshot_id": self.snapshot["id"],
            "pinned_image": "python@sha256:" + "b" * 64,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            "max_exposure_cents": 5,
            "currency": "USD",
            "acknowledges_unmeasured_infrastructure": True,
        }
        grant_path = f"/projects/{self.project['id']}/execution-grants"
        self.call("POST", grant_path, grant_body, expected=(409,))
        approval = {
            "expected_revision": task["revision"],
            "expected_plan_digest": task["current_plan_digest"],
        }
        self.call("POST", f"/tasks/{task['id']}/approve", approval, worker=True, expected=(401,))
        approved = self.call("POST", f"/tasks/{task['id']}/approve", approval)
        assert approved["execution_granted"] is False
        if kind == "baseline":
            assert self.export()["operations"] == []
        grant_body["expected_task_revision"] = approved["revision"]
        grant = self.call("POST", grant_path, grant_body)
        variant = next(
            item for item in self.experiment["design"]["variants"] if item["kind"] == kind
        )
        path = f"/experiment-variants/{variant['id']}/admitted-attempts"
        body = {
            "expected_variant_revision": variant["revision"],
            "expected_design_revision": self.experiment["current_design_revision"],
            "grant_id": grant["id"],
        }
        self.call("POST", path, body | {"expected_design_revision": 999}, expected=(409,))
        admitted = self.call("POST", path, body, key=f"admit-{kind}")
        assert self.call("POST", path, body, key=f"admit-{kind}") == admitted
        self.call("POST", "/worker/heartbeat", {}, worker=True)
        lease = self.call("POST", "/worker/leases", {"grant_id": grant["id"]}, worker=True)
        delivery = self.call(
            "POST",
            f"/worker/leases/{lease['id']}/package",
            {
                "expected_epoch": lease["epoch"],
                "execution_adapter": "kubernetes-job",
                "admitted_attempt_id": admitted["id"],
            },
            worker=True,
        )
        assert delivery["package"]["provider_enabled"] is False
        current = self.attempt(admitted["id"])
        dispatch = self.call(
            "POST",
            f"/experiment-admitted-attempts/{admitted['id']}/dispatch",
            {
                "expected_revision": current["revision"],
                "target_digest": cluster_identity_digest(self.cluster),
                "profile_version": self.cluster.profile,
            },
        )
        package = KubernetesExecutionPackage.from_admitted_package(
            delivery,
            KubernetesOutputBinding("outputs", "/outputs", "synthetic-pvc", "c" * 64),
        )
        return grant, lease, admitted["id"], dispatch, _FakeKubernetes(package, self.cluster)

    def run(self, fake):
        journey = self

        class Channel:
            def post(self, path, body, key):
                response = journey.client.post(
                    path,
                    json=body,
                    headers={
                        "Authorization": journey.worker,
                        "Idempotency-Key": key,
                    },
                )
                if response.status_code >= 400:
                    raise ControlChannelError(response.text, response.status_code)
                return response.json()

        return KubernetesDispatchWorkerService(
            KubernetesDispatchWorkerConfig(
                "http://127.0.0.1:8765",
                credential_file=self.credential_file,
                workspace_root=self.worker_root,
            ),
            self.cluster,
            fake.package.output,
            fake.adapter(self.worker_root / "kubernetes.sqlite3"),
            channel=Channel(),
        ).run(max_cycles=1)

    def resolve(self, attempt_id, outcome, *, expected=(200, 201)):
        return self.call(
            "POST",
            f"/experiment-admitted-attempts/{attempt_id}/resolve",
            {
                "expected_revision": self.attempt(attempt_id)["revision"],
                "outcome": outcome,
                "reason": None if outcome == "succeeded" else "Synthetic adverse outcome retained.",
            },
            expected=expected,
        )


def complete(fake, state):
    """Mutate only the injected external resource, never the control ledger."""
    job = fake.jobs[fake.package.binding.job_name]
    job["status"] = {state: 1}
    pod = fake.pod("Succeeded" if state == "succeeded" else "Failed")
    template = job["spec"]["template"]
    pod["metadata"].update(deepcopy(template["metadata"]))
    pod["spec"] = deepcopy(template["spec"])
    fake.pods = [pod]


def retain_evidence(journey, lease, attempt_id, fake, content):
    binding = {"expected_epoch": lease["epoch"], "operation_id": fake.package.operation_id}
    path = f"/worker/leases/{lease['id']}"
    artifact = journey.call(
        "POST",
        path + "/artifacts",
        binding
        | {
            "relative_path": "result.txt",
            "sha256": hashlib.sha256(content).hexdigest(),
            "content_base64": base64.b64encode(content).decode(),
        },
        worker=True,
    )
    # Neither a fabricated operation nor the old CPU-derived identity can place
    # or manifest output for an operation-journaled Kubernetes lease.
    legacy_id = hashlib.sha256(f"tessera-cpu-v1:{lease['id']}".encode()).hexdigest()[:32]
    for wrong_id in ("0" * 32, legacy_id):
        for endpoint, keys in (
            ("/project-artifacts", ("id", "sha256", "size")),
            ("/artifact-manifest", ("id", "relative_path", "sha256", "size")),
        ):
            journey.call(
                "POST",
                path + endpoint,
                binding
                | {
                    "operation_id": wrong_id,
                    "artifacts": [{key: artifact[key] for key in keys}],
                },
                worker=True,
                expected=(409,),
            )
    journey.call(
        "POST",
        path + "/project-artifacts",
        binding
        | {
            "artifacts": [{key: artifact[key] for key in ("id", "sha256", "size")}],
        },
        worker=True,
    )
    manifest = journey.call(
        "POST",
        path + "/artifact-manifest",
        binding
        | {
            "artifacts": [
                {key: artifact[key] for key in ("id", "relative_path", "sha256", "size")}
            ],
        },
        worker=True,
    )
    assert manifest["status"] == "quarantined" and manifest["classification"] == "unclassified"
    linked = journey.call(
        "POST",
        f"/experiment-attempts/{attempt_id}/artifacts",
        {
            "expected_revision": journey.attempt(attempt_id)["revision"],
            "artifact_ids": [artifact["id"]],
        },
    )
    measurement = journey.call(
        "POST",
        f"/experiment-attempts/{attempt_id}/measurements",
        {
            "expected_revision": linked["revision"],
            "name": "Synthetic output",
            "value": {"text": content.decode()},
            "artifact_id": artifact["id"],
        },
    )
    content_path = f"/artifact-manifests/{manifest['id']}/artifacts/{artifact['id']}/content"
    journey.call("GET", content_path, expected=(403,))
    journey.call(
        "POST",
        f"/artifact-manifests/{manifest['id']}/dispositions",
        {
            "expected_manifest_digest": manifest["manifest_digest"],
            "expected_revision": 0,
            "disposition": "retain_quarantined",
            "rationale": "Retain synthetic evidence for review.",
        },
    )
    journey.call(
        "POST",
        f"/artifact-manifests/{manifest['id']}/custody-releases",
        {
            "expected_manifest_digest": manifest["manifest_digest"],
            "expected_revision": 0,
            "action": "release",
            "rationale": "Private human inspection of synthetic bytes.",
        },
    )
    return {
        "attempt_id": attempt_id,
        "artifact_ids": [artifact["id"]],
        "measurement_ids": [measurement["id"]],
    }, content_path


@pytest.mark.parametrize("outcome", ("succeeded", "failed", "cancelled", "unknown"))
def test_registered_comparison_through_normal_worker_and_durable_evidence(
    tmp_path: Path,
    monkeypatch,
    capsys,
    outcome: str,
):
    journey = Journey(tmp_path, monkeypatch, capsys)
    citations, contents, ids = [], [], []
    for kind, result in (("baseline", "succeeded"), ("intervention", outcome)):
        grant, lease, attempt_id, dispatch, fake = journey.admit(kind)
        ids.append(attempt_id)
        first = journey.run(fake)
        assert first["dispatch_id"] == dispatch["id"] and fake.creates == 1
        journey.resolve(attempt_id, "succeeded", expected=(409,))
        if result in {"succeeded", "failed"}:
            complete(fake, result)
            finished = journey.run(fake)
            assert finished["status"] == result
            assert journey.attempt(attempt_id)["operational_termination_confirmed"] is True
        else:
            journey.call(
                "POST",
                f"/execution-grants/{grant['id']}/revoke",
                {"expected_revision": grant["revision"]},
            )
            journey.resolve(attempt_id, "cancelled", expected=(409,))
            if result == "unknown":

                def unavailable_delete(_name, _uid):
                    raise ConnectionError("synthetic lost cancellation")

                fake.delete = unavailable_delete
            stopped = journey.run(fake)
            assert stopped["status"] == "cancel_requested"
            assert stopped["cancellation"]["termination_confirmed"] is (result == "cancelled")
            if result == "unknown":
                journey.resolve(attempt_id, "cancelled", expected=(409,))
                current_grant = journey.call("GET", f"/execution-grants/{grant['id']}")
                assert current_grant["cancellation"]["state"] in {"unknown", "unresolved"}
                journey.call(
                    "POST",
                    f"/execution-grants/{grant['id']}/reconcile",
                    {
                        "expected_revision": current_grant["revision"],
                        "settlement_cents": 0,
                        "provider_terminated": True,
                    },
                    expected=(409,),
                )
        resolved = journey.resolve(attempt_id, result)
        assert resolved["human_outcome"]["outcome"] == result
        assert fake.creates == 1
        if result == "succeeded":
            content = b"1\n" if kind == "baseline" else b"2\n"
            citation, path = retain_evidence(journey, lease, attempt_id, fake, content)
            citations.append(citation)
            contents.append((path, content))
        if result == "unknown":
            # Later confirmed cancellation improves operational knowledge, not the
            # already-recorded human unknown label, and never submits again.
            fake.delete = _FakeKubernetes.delete.__get__(fake)
            assert journey.run(fake)["cancellation"]["termination_confirmed"] is True
            assert journey.attempt(attempt_id)["human_outcome"]["outcome"] == "unknown"
            assert fake.creates == 1
    conclusion = journey.call(
        "POST",
        f"/experiments/{journey.experiment['id']}/conclusions",
        {
            "statement": "Synthetic comparison and adverse outcomes reviewed; no scientific claim.",
            "design_revision": 1,
            "evidence": citations,
        },
    )
    journey.call(
        "POST",
        f"/conclusions/{conclusion['id']}/review",
        {"expected_revision": conclusion["revision"], "design_revision": 1},
    )
    before = journey.export()
    journey.client.close()
    journey.client = TestClient(create_app(journey.state))
    assert journey.export() == before
    assert {item["id"] for item in before["admitted_attempts"]} == set(ids)
    assert len(before["operations"]) == len(before["kubernetes_dispatches"]) == 2
    assert before["conclusions"][0]["scientific_validity"] == "not_assessed"
    assert before["conclusions"][0]["status"] == "reviewed"
    for path, content in contents:
        response = journey.client.get(path, headers={"Authorization": journey.owner})
        assert response.status_code == 200 and response.content == content
    journey.client.close()


def test_queue_replay_retains_idle_or_exact_dispatch_and_rechecks_authority(
    tmp_path, monkeypatch, capsys
):
    journey = Journey(tmp_path, monkeypatch, capsys)
    body = {
        "target_digest": cluster_identity_digest(journey.cluster),
        "profile_version": journey.cluster.profile,
    }
    idle = journey.call(
        "POST", "/worker/kubernetes-dispatches/next/claim", body, worker=True, key="lost-idle"
    )
    assert idle["availability"] == "idle"
    grant, _lease, _attempt, dispatch, fake = journey.admit("baseline")
    claimed = journey.call(
        "POST", "/worker/kubernetes-dispatches/next/claim", body, worker=True, key="lost-claim"
    )
    assert journey.run(fake)["dispatch_id"] == dispatch["id"]
    assert (
        journey.call(
            "POST", "/worker/kubernetes-dispatches/next/claim", body, worker=True, key="lost-idle"
        )
        == idle
    )
    assert fake.creates == 1
    complete(fake, "succeeded")
    assert journey.run(fake)["status"] == "succeeded"
    _grant, _lease, _attempt, next_dispatch, next_fake = journey.admit("intervention")
    assert journey.run(next_fake)["dispatch_id"] == next_dispatch["id"]
    assert (
        journey.call(
            "POST", "/worker/kubernetes-dispatches/next/claim", body, worker=True, key="lost-claim"
        )
        == claimed
    )
    journey.call(
        "POST", f"/execution-grants/{grant['id']}/revoke", {"expected_revision": grant["revision"]}
    )
    journey.call(
        "POST",
        "/worker/kubernetes-dispatches/next/claim",
        body,
        worker=True,
        key="lost-claim",
        expected=(409,),
    )
    assert fake.creates == next_fake.creates == 1
    journey.client.close()
