"""Provider-disabled fake-agent path across authenticated local control boundaries.

The only upstream-shaped endpoint is the existing loopback OpenCode fixture.  The
Kubernetes adapter is also injected, so this test neither contacts a cluster nor
claims an OpenCode/provider/model execution or scientific outcome.
"""

from __future__ import annotations

import sqlite3
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from test_kubernetes_execution import _cluster, _FakeKubernetes
from test_opencode_live_adapter import fixture as opencode_fixture

from tessera_control.api import create_app
from tessera_control.snapshots import SnapshotStore
from tessera_control.store import Store, initialize_state
from tessera_control.workspace import capture_workspace, inventory_digest
from tessera_worker.control_channel import ControlChannelError
from tessera_worker.kubernetes_contracts import cluster_identity_digest
from tessera_worker.kubernetes_dispatch_service import (
    KubernetesDispatchWorkerConfig,
    KubernetesDispatchWorkerService,
)
from tessera_worker.kubernetes_execution import KubernetesExecutionPackage, KubernetesOutputBinding
from tessera_worker.opencode_delivery_service import (
    OpenCodeDeliveryWorkerConfig,
    OpenCodeDeliveryWorkerService,
)
from tessera_worker.opencode_http import OpenCodeHarnessInstancePin, OpenCodeHttpClient
from tessera_worker.opencode_live_adapter import LiveOpenCodeHarnessAdapter

WORKER_TOKEN = "fixture-worker-" + "x" * 48


class _WorkerChannel:
    """Authenticated local API channel with bounded lost-response injection."""

    def __init__(
        self,
        client: TestClient,
        *,
        lose_claim_reply: bool = False,
        lose_report_reply: bool = False,
    ) -> None:
        self.client = client
        self.lose_claim_reply = lose_claim_reply
        self.lose_report_reply = lose_report_reply
        self.reports: list[tuple[dict[str, Any], str]] = []

    def post(self, path: str, body: dict[str, Any], key: str) -> dict[str, Any]:
        response = self.client.post(
            path,
            json=body,
            headers={"Authorization": f"Bearer {WORKER_TOKEN}", "Idempotency-Key": key},
        )
        if response.status_code >= 400:
            raise ControlChannelError("fixture control rejection", response.status_code)
        if path.endswith("/claim") and self.lose_claim_reply:
            self.lose_claim_reply = False
            raise ConnectionError("fixture lost dispatch claim response after control commit")
        if path == "/worker/observations":
            self.reports.append((body, key))
            if self.lose_report_reply:
                self.lose_report_reply = False
                raise ConnectionError("fixture lost dispatch report response after control commit")
        return response.json()


def _instance_pin() -> OpenCodeHarnessInstancePin:
    return OpenCodeHarnessInstancePin(
        harness_instance_id="fake-agent-harness",
        server_identity="fake-agent-server",
        server_incarnation="fake-agent-incarnation",
        build_sha256="a" * 64,
        config_sha256="b" * 64,
        workspace_scope="fake-agent-workspace",
    )


def test_provider_disabled_fake_agent_delivery_and_kubernetes_dispatch_recover_end_to_end(
    tmp_path: Path,
) -> None:
    state, source = tmp_path / "state", tmp_path / "source"
    source.mkdir()
    (source / "input.txt").write_text("provider-disabled deterministic fixture\n")
    initialize_state(state)
    owner_token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    worker_headers = {"Authorization": f"Bearer {WORKER_TOKEN}"}
    sequence = 0

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
            nonlocal sequence
            sequence += 1
            response = client.request(
                method,
                path,
                json=body,
                headers=(worker_headers if worker else owner_headers)
                | {"Idempotency-Key": key or f"provider-disabled-e2e-{sequence}"},
            )
            expected_codes = (expected,) if isinstance(expected, int) else expected
            assert response.status_code in expected_codes, response.text
            return response.json()

        project = call("POST", "/projects", {"name": "Provider-disabled E2E"}, 201)
        owner = Store(state).authenticate(owner_token)
        capture = capture_workspace(source)
        snapshot = SnapshotStore(state).adopt_snapshot(
            owner,
            project["id"],
            "provider-disabled-input",
            inventory_digest(capture.inventory),
            capture,
        )[1]
        credential = call(
            "POST",
            "/credentials",
            {
                "kind": "worker",
                "audience": "worker",
                "token": WORKER_TOKEN,
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
                "approved_build_identifier": "provider-disabled-fake-agent",
                "approved_build_digest": "c" * 64,
                "trust_class": "test",
                "region": "loopback",
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
                "rationale": "Explicit deterministic fixture-only input release.",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
            },
            201,
        )
        task = call(
            "POST",
            f"/projects/{project['id']}/tasks",
            {
                "title": "Provider-disabled fake-agent context",
                "purpose": "Deliver bounded context then dispatch a deterministic fake Job.",
                "budget_cents": 0,
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
                "budget_cents": 0,
                "max_duration_seconds": 60,
                "execution_spec": {
                    "argv": ["/hello", "--write", "/outputs/result.txt"],
                    "input_snapshot_digest": snapshot["digest"],
                    "pinned_skill_digests": [],
                    "output_paths": ["result.txt"],
                    "profile": "kubernetes-opencode",
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
        assert approved["status"] == "approved" and approved["execution_granted"] is False
        call(
            "PUT",
            f"/projects/{project['id']}/execution-limits",
            {
                "expected_revision": 0,
                "currency": "USD",
                "monetary_limit_cents": 0,
                "max_concurrency": 1,
            },
            201,
        )
        grant = call(
            "POST",
            f"/projects/{project['id']}/execution-grants",
            {
                "task_id": task["id"],
                "expected_task_revision": approved["revision"],
                "expected_plan_digest": approved["current_plan_digest"],
                "worker_principal_id": credential["principal_id"],
                "snapshot_id": snapshot["id"],
                "pinned_image": "fixture-hello@sha256:" + "d" * 64,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                "max_exposure_cents": 0,
                "currency": "USD",
                "acknowledges_unmeasured_infrastructure": True,
            },
            201,
        )
        experiment = call(
            "POST",
            f"/projects/{project['id']}/experiments",
            {
                "goal": "Exercise one deterministic provider-disabled control path.",
                "success_criterion": "Only durable operational fixture records are retained.",
                "protocol": "No provider, model, cluster, artifact, or scientific claim.",
                "variants": [
                    {
                        "kind": "baseline",
                        "name": "deterministic fake workload",
                        "method_digest": "e" * 64,
                        "input_digest": snapshot["digest"],
                        "metadata": {"fixture": True},
                        "execution_task_id": task["id"],
                        "execution_plan_digest": approved["current_plan_digest"],
                    },
                    {
                        "kind": "intervention",
                        "name": "unexecuted deterministic comparison",
                        "method_digest": "f" * 64,
                        "input_digest": snapshot["digest"],
                        "metadata": {"fixture": True},
                        "execution_task_id": task["id"],
                        "execution_plan_digest": approved["current_plan_digest"],
                    },
                ],
            },
            201,
        )
        baseline = next(
            variant for variant in experiment["design"]["variants"] if variant["kind"] == "baseline"
        )
        admitted = call(
            "POST",
            f"/experiment-variants/{baseline['id']}/admitted-attempts",
            {
                "expected_variant_revision": baseline["revision"],
                "expected_design_revision": experiment["current_design_revision"],
                "grant_id": grant["id"],
            },
            201,
        )
        call("POST", "/worker/heartbeat", {}, 200, worker=True)
        lease = call("POST", "/worker/leases", {"grant_id": grant["id"]}, 201, worker=True)
        package = call(
            "POST",
            f"/worker/leases/{lease['id']}/package",
            {
                "expected_epoch": lease["epoch"],
                "execution_adapter": "kubernetes-job",
                "admitted_attempt_id": admitted["id"],
            },
            201,
            worker=True,
        )
        assert package["package"]["provider_enabled"] is False
        exported = client.get(f"/experiments/{experiment['id']}", headers=owner_headers).json()
        admitted = next(
            item for item in exported["admitted_attempts"] if item["id"] == admitted["id"]
        )

        thread = call(
            "POST",
            "/interaction/threads",
            {"project_id": project["id"], "task_id": task["id"], "title": "Fake agent context"},
            201,
        )
        harness_binding = call(
            "POST",
            "/worker/operation-harness-session-bindings",
            {
                "operation_id": package["package"]["operation_id"],
                "harness_instance_id": "fake-agent-harness",
                "upstream_session_id": "fake-agent-session",
                "adapter_profile": "opencode-v1.18.30-context-only",
                "provider_disabled": True,
            },
            201,
            worker=True,
        )
        queued = call(
            "POST",
            "/interaction/deliveries",
            {
                "thread_id": thread["id"],
                "harness_session_binding_id": harness_binding["id"],
                "context_revision": 1,
                "intent": "direction",
                "body": "Use this bounded provider-disabled fixture context only.",
            },
            201,
        )["delivery"]
        credential_file = tmp_path / "fixture-worker.credential"
        credential_file.write_text(WORKER_TOKEN)
        credential_file.chmod(0o600)
        fake_agent_root = tmp_path / "fake-agent-worker"
        fake_agent_root.mkdir(mode=0o700)
        with opencode_fixture() as upstream:
            adapter = LiveOpenCodeHarnessAdapter(
                OpenCodeHttpClient(
                    upstream.endpoint,
                    upstream.authorization,
                    upstream.pin,
                    _instance_pin(),
                ),
                tmp_path / "fake-agent-journal",
                authority_is_current=lambda *_: True,
                context_only_delivery=True,
                provider_disabled=True,
            )
            delivery = OpenCodeDeliveryWorkerService(
                OpenCodeDeliveryWorkerConfig(
                    "http://127.0.0.1:8765",
                    queued["delivery_id"],
                    credential_file,
                    fake_agent_root,
                ),
                adapter,
                channel=_WorkerChannel(client),
            ).run(lease["epoch"])
            assert delivery["state"] == "acknowledged"
            assert len(upstream.messages) == 1
            assert upstream.sessions == [] and upstream.provider_calls == 0
            adapter.close()

        cluster = _cluster()
        target_digest = cluster_identity_digest(cluster)
        dispatch = call(
            "POST",
            f"/experiment-admitted-attempts/{admitted['id']}/dispatch",
            {
                "expected_revision": admitted["revision"],
                "target_digest": target_digest,
                "profile_version": cluster.profile,
            },
            201,
        )
        execution_package = KubernetesExecutionPackage.from_admitted_package(
            package,
            KubernetesOutputBinding("outputs", "/outputs", "pvc-fixture", "f" * 64),
        )
        fake = _FakeKubernetes(execution_package, cluster)
        worker_root = tmp_path / "kubernetes-worker"
        worker_root.mkdir(mode=0o700)

        def service(channel: _WorkerChannel) -> KubernetesDispatchWorkerService:
            return KubernetesDispatchWorkerService(
                KubernetesDispatchWorkerConfig(
                    "http://127.0.0.1:8765", dispatch["id"], credential_file, worker_root
                ),
                cluster,
                execution_package.output,
                fake.adapter(worker_root / "kubernetes.sqlite3"),
                channel=channel,
            )

        try:
            service(_WorkerChannel(client, lose_claim_reply=True)).run(max_cycles=1)
        except ConnectionError as error:
            assert "lost dispatch claim" in str(error)
        else:
            raise AssertionError("lost claim response must not permit a Kubernetes create")
        assert fake.creates == 0
        lost_report = _WorkerChannel(client, lose_report_reply=True)
        assert service(lost_report).run(max_cycles=1)["status"] == "running"
        assert fake.creates == 1
        # Complete the exact Job manifest that the supervisor submitted.  Its
        # active deadline is narrowed to the remaining authority before create;
        # replacing it with a fixture-built manifest would be a conflicting
        # provider observation and must remain unknown rather than successful.
        completed_job = fake.jobs[execution_package.binding.job_name]
        completed_job["status"] = {"succeeded": 1}
        template = deepcopy(completed_job["spec"]["template"])
        template["metadata"].update(
            {
                "namespace": cluster.namespace,
                "uid": "pod-fixture",
                "ownerReferences": [{"kind": "Job", "uid": "job-fixture", "controller": True}],
            }
        )
        fake.pods = [
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": template["metadata"],
                "spec": template["spec"],
                "status": {"phase": "Succeeded"},
            }
        ]
        recovered = _WorkerChannel(client)
        assert service(recovered).run()["status"] == "succeeded"
        assert fake.creates == 1
        assert [body["observed_state"] for body, _key in lost_report.reports] == ["running"]
        assert [body["observed_state"] for body, _key in recovered.reports] == ["running", "exited"]
        assert recovered.reports[0][1] == lost_report.reports[0][1]

        exported = client.get(f"/experiments/{experiment['id']}", headers=owner_headers).json()
        exported_dispatch = next(
            item for item in exported["kubernetes_dispatches"] if item["id"] == dispatch["id"]
        )
        assert exported_dispatch["operation_id"] == package["operation"]["operation_id"]
        assert exported_dispatch["admitted_attempt_id"] == admitted["id"]
        messages = client.get(
            f"/interaction/threads/{thread['id']}/messages", headers=owner_headers
        ).json()
        assert messages["messages"][0]["delivery"]["state"] == "acknowledged"
        with sqlite3.connect(state / "tessera.sqlite3") as connection:
            delivery_states = connection.execute(
                "SELECT state FROM interaction_message_delivery_states "
                "WHERE delivery_id=? ORDER BY sequence",
                (queued["delivery_id"],),
            ).fetchall()
            events = {
                row[0]
                for row in connection.execute(
                    "SELECT event_type FROM events WHERE project_id=?", (project["id"],)
                )
            }
        assert [row[0] for row in delivery_states] == ["queued", "delivered", "acknowledged"]
        assert {
            "task.approved",
            "interaction_message_delivery_queued",
            "interaction_message_delivery_delivered",
            "interaction_message_delivery_acknowledged",
            "experiment.kubernetes_dispatch_approved",
            "experiment.kubernetes_dispatch_claimed",
        } <= events
