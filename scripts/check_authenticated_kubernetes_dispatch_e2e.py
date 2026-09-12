"""Run one retained, authenticated Docker Desktop Kubernetes dispatch conformance E2E.

This narrow, explicitly opt-in fixture uses a temporary loopback control service
and normal authenticated human/worker routes. It never uses an ambient Kubernetes
context, provider/model/OpenCode credentials, or a registry pull.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tessera_worker.docker_desktop_kubernetes import (
    CONTEXT,
    RESULT_BYTES,
    DockerDesktopKubernetesAdapter,
    ExactImage,
    LiveKubernetesError,
    _collector_manifest,
    _pvc,
    _pvc_spec_sha256,
    _uid,
    _verify_bound_pvc,
    _verify_collector,
    build_and_load_exact_image,
    new_nonce,
    target_from_kubeconfig,
)
from tessera_worker.kubernetes_contracts import (
    KubernetesClusterConfig,
    KubernetesNetworkPolicyEvidence,
    canonical_json,
    cluster_identity_digest,
)
from tessera_worker.kubernetes_dispatch_service import (
    KubernetesDispatchWorkerConfig,
    KubernetesDispatchWorkerService,
)
from tessera_worker.kubernetes_execution import (
    KubernetesExecutionAdapter,
    KubernetesOutputBinding,
)


class DispatchE2EError(RuntimeError):
    """The bounded dispatch fixture cannot safely continue."""


def _run(argv: tuple[str, ...], *, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=timeout)
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or error.stdout.strip() or "no diagnostic output"
        raise DispatchE2EError(f"local command failed: {detail[:500]}") from error


def _node_image_or_build(root: Path, nonce: str) -> tuple[ExactImage, str]:
    """Use a node-resident exact hello image, or build/load the pinned local source."""
    listed = _run(
        (
            "docker",
            "exec",
            "desktop-control-plane",
            "ctr",
            "--namespace",
            "k8s.io",
            "images",
            "ls",
            "--quiet",
        )
    ).stdout.splitlines()
    prefix = "docker.io/library/tessera-e2e-hello@sha256:"
    references = sorted({value for value in listed if value.startswith(prefix)})
    if not references:
        return build_and_load_exact_image(root, nonce), "built-and-loaded"
    reference = references[0]
    digest = reference.rsplit("@", 1)[1]
    manifest = json.loads(
        _run(
            (
                "docker",
                "exec",
                "desktop-control-plane",
                "ctr",
                "--namespace",
                "k8s.io",
                "content",
                "get",
                digest,
            )
        ).stdout
    )
    config_digest = manifest.get("config", {}).get("digest")
    inspected = json.loads(
        _run(("docker", "exec", "desktop-control-plane", "crictl", "inspecti", reference)).stdout
    )
    spec = inspected.get("info", {}).get("imageSpec", {})
    if (
        manifest.get("schemaVersion") != 2
        or not isinstance(config_digest, str)
        or not config_digest.startswith("sha256:")
        or spec.get("architecture") != "amd64"
        or spec.get("os") != "linux"
        or spec.get("config", {}).get("Entrypoint") != ["/hello"]
        or spec.get("config", {}).get("User") != "65532:65532"
    ):
        raise DispatchE2EError("Docker Desktop node image is not the deterministic hello workload.")
    return ExactImage(
        reference, digest, config_digest, "node-resident-not-rebuilt"
    ), "node-resident"


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class _Api:
    def __init__(self, origin: str, token: str) -> None:
        self.origin, self.token = origin, token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None, *, key: str | None = None
    ) -> dict[str, Any]:
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        headers = {"Authorization": f"Bearer {self.token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if key is not None:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(
            self.origin + path, data=data, headers=headers, method=method
        )
        try:
            with self._opener.open(request, timeout=10) as response:
                value = json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read(1024).decode("utf-8", errors="replace")
            raise DispatchE2EError(
                f"control request {method} {path} returned {error.code}: {detail}"
            ) from error
        except (OSError, TimeoutError, json.JSONDecodeError) as error:
            raise DispatchE2EError(f"control request {method} {path} failed") from error
        if not isinstance(value, dict):
            raise DispatchE2EError(f"control request {method} {path} returned malformed JSON")
        return value


def _wait_for_control(origin: str) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(50):
        try:
            with opener.open(origin + "/health", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise DispatchE2EError("temporary authenticated control service did not become healthy")


def _key(nonce: str, operation: str) -> str:
    return f"authenticated-kubernetes-e2e-{nonce}-{operation}"


def _adopt_snapshot(executable: Path, state: Path, project_id: str, workspace: Path, nonce: str):
    inventory = json.loads(
        _run((str(executable), "workspace", "inventory", "--root", str(workspace))).stdout
    )
    snapshot = json.loads(
        _run(
            (
                str(executable),
                "workspace",
                "adopt",
                "--root",
                str(workspace),
                "--state-dir",
                str(state),
                "--project-id",
                project_id,
                "--expected-digest",
                inventory["digest"],
                "--idempotency-key",
                _key(nonce, "snapshot-adopt"),
            )
        ).stdout
    )
    if snapshot.get("digest") != inventory["digest"]:
        raise DispatchE2EError(
            "authenticated local snapshot adoption returned a conflicting digest"
        )
    return snapshot


def _cluster(live: DockerDesktopKubernetesAdapter, namespace: str):
    live._create({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}}, None)
    namespace_uid = _uid(live._get("namespace", namespace, None), "Namespace", namespace, None)
    policy_spec = {
        "podSelector": {"matchLabels": {"app.kubernetes.io/managed-by": "tessera"}},
        "policyTypes": ["Ingress", "Egress"],
    }
    live._create(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "deny-all", "namespace": namespace},
            "spec": policy_spec,
        },
        namespace,
    )
    policy = live._get("networkpolicy", "deny-all", namespace)
    policy_uid = _uid(policy, "NetworkPolicy", "deny-all", namespace)
    if canonical_json(policy.get("spec")) != canonical_json(policy_spec):
        raise DispatchE2EError("created NetworkPolicy conflicts with the fixed network-none policy")
    live._create(_pvc(namespace), namespace)
    target = live.target
    cluster = KubernetesClusterConfig(
        kubeconfig=str(target.kubeconfig),
        kubeconfig_sha256=target.kubeconfig_sha256,
        context=target.context,
        profile="development-minikube-opencode-v1",
        server=target.server,
        server_sha256=hashlib.sha256(target.server.encode()).hexdigest(),
        ca_sha256=target.ca_sha256,
        namespace=namespace,
        cluster_uid=_uid(
            live._get("namespace", "kube-system", None), "Namespace", "kube-system", None
        ),
        namespace_uid=namespace_uid,
        network_none_evidence=KubernetesNetworkPolicyEvidence(
            "network-policy-enforced-v1",
            "deny-all",
            policy_uid,
            hashlib.sha256(canonical_json(policy_spec)).hexdigest(),
        ),
    )
    pvc = live._get("pvc", "outputs", namespace)
    return cluster, _uid(pvc, "PersistentVolumeClaim", "outputs", namespace), _pvc_spec_sha256(pvc)


class _LiveAdapter:
    """The durable nonblocking adapter with only explicit Kubernetes callbacks."""

    def __init__(self, live: DockerDesktopKubernetesAdapter, cluster, journal: Path) -> None:
        self.live, self.cluster, self.package = live, cluster, None
        self.adapter = KubernetesExecutionAdapter(
            journal,
            cluster,
            inspect_job=lambda name: live._maybe_get("job", name, cluster.namespace),
            create_job=lambda manifest: live._create(manifest, cluster.namespace),
            delete_job=self._delete_job,
            observe_pods=lambda job_uid: live._owned_pod_resources(job_uid, cluster.namespace),
        )

    def _delete_job(self, name: str, uid: str) -> None:
        current = self.live._get("job", name, self.cluster.namespace)
        if _uid(current, "Job", name, self.cluster.namespace) != uid:
            raise DispatchE2EError("Job UID changed before narrowly named deletion")
        self.live._kubectl("delete", "job", name, "--wait=false", namespace=self.cluster.namespace)

    def submit(self, package):
        self.package = package
        return self.adapter.submit(package)

    def artifact_collection_handoff(self, package):
        return self.adapter.artifact_collection_handoff(package)


def _main(args: argparse.Namespace) -> dict[str, Any]:
    nonce, root = new_nonce(), Path(__file__).resolve().parents[1]
    target = target_from_kubeconfig(args.kubeconfig)
    image, image_source = _node_image_or_build(root, nonce)
    journal = args.journal or Path("/tmp") / f"tessera-authenticated-kubernetes-e2e-{nonce}.sqlite3"
    if journal.exists():
        raise DispatchE2EError("refusing to reuse a local dispatch journal")
    namespace = f"tessera-e2e-{nonce}"
    live = DockerDesktopKubernetesAdapter(target, journal)
    cluster, pvc_uid, pvc_spec = _cluster(live, namespace)
    target_digest = cluster_identity_digest(cluster)
    # macOS /tmp is a symlink; workspace custody intentionally rejects a
    # symlinked ancestor, so use the verified physical temporary directory.
    with tempfile.TemporaryDirectory(
        prefix="tessera-authenticated-kubernetes-control-", dir="/private/tmp"
    ) as temporary:
        temporary_root = Path(temporary)
        state, workspace, worker_root = (
            temporary_root / "state",
            temporary_root / "workspace",
            temporary_root / "worker",
        )
        workspace.mkdir(mode=0o700)
        worker_root.mkdir(mode=0o700)
        (workspace / "input.txt").write_bytes(b"immutable authenticated Kubernetes E2E input\n")
        executable = Path(sys.executable).with_name("tessera")
        _run((str(executable), "init", "--state-dir", str(state)))
        owner_header = (state / "owner.header").read_text().strip()
        port, origin = _port(), ""
        origin = f"http://127.0.0.1:{port}"
        server = subprocess.Popen(
            [str(executable), "serve", "--state-dir", str(state), "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_for_control(origin)
            owner = _Api(origin, owner_header.split("Bearer ", 1)[1])
            project = owner.request(
                "POST",
                "/projects",
                {"name": "Authenticated Kubernetes E2E"},
                key=_key(nonce, "project"),
            )
            worker_token = secrets.token_urlsafe(48)
            credential = owner.request(
                "POST",
                "/credentials",
                {
                    "token": worker_token,
                    "kind": "worker",
                    "audience": "worker",
                    "project_id": project["id"],
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
                key=_key(nonce, "worker-credential"),
            )
            registered = owner.request(
                "POST",
                f"/projects/{project['id']}/workers",
                {
                    "worker_principal_id": credential["principal_id"],
                    "approved_build_identifier": "authenticated-kubernetes-e2e",
                    "approved_build_digest": hashlib.sha256(
                        Path(__file__).read_bytes()
                    ).hexdigest(),
                    "trust_class": "development",
                    "region": "docker-desktop",
                },
                key=_key(nonce, "worker-register"),
            )
            snapshot = _adopt_snapshot(executable, state, project["id"], workspace, nonce)
            owner.request(
                "POST",
                f"/snapshots/{snapshot['id']}/releases",
                {
                    "expected_digest": snapshot["digest"],
                    "worker_principal_id": credential["principal_id"],
                    "classification": "public",
                    "permits_worker_transfer": True,
                    "rationale": "Explicit deterministic local E2E input release.",
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
                },
                key=_key(nonce, "snapshot-release"),
            )
            task = owner.request(
                "POST",
                f"/projects/{project['id']}/tasks",
                {
                    "title": "Deterministic Kubernetes hello",
                    "purpose": "Authenticated provider-disabled Kubernetes dispatch conformance.",
                    "budget_cents": 0,
                    "max_duration_seconds": 60,
                },
                key=_key(nonce, "task"),
            )
            task = owner.request(
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
                key=_key(nonce, "task-plan"),
            )
            task = owner.request(
                "POST",
                f"/tasks/{task['id']}/approve",
                {
                    "expected_revision": task["revision"],
                    "expected_plan_digest": task["current_plan_digest"],
                },
                key=_key(nonce, "task-approve"),
            )
            owner.request(
                "PUT",
                f"/projects/{project['id']}/execution-limits",
                {
                    "expected_revision": 0,
                    "currency": "USD",
                    "monetary_limit_cents": 0,
                    "max_concurrency": 1,
                },
                key=_key(nonce, "limits"),
            )
            grant = owner.request(
                "POST",
                f"/projects/{project['id']}/execution-grants",
                {
                    "task_id": task["id"],
                    "expected_task_revision": task["revision"],
                    "expected_plan_digest": task["current_plan_digest"],
                    "worker_principal_id": credential["principal_id"],
                    "snapshot_id": snapshot["id"],
                    "pinned_image": image.reference,
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=20)).isoformat(),
                    "max_exposure_cents": 0,
                    "currency": "USD",
                    "acknowledges_unmeasured_infrastructure": True,
                },
                key=_key(nonce, "grant"),
            )
            variants = []
            for kind in ("baseline", "intervention"):
                variants.append(
                    {
                        "kind": kind,
                        "name": f"hello {kind}",
                        "method_digest": hashlib.sha256(f"hello-{kind}".encode()).hexdigest(),
                        "input_digest": snapshot["digest"],
                        "metadata": {},
                        "execution_task_id": task["id"],
                        "execution_plan_digest": task["current_plan_digest"],
                    }
                )
            experiment = owner.request(
                "POST",
                f"/projects/{project['id']}/experiments",
                {
                    "goal": "Verify one exact authenticated Kubernetes dispatch output.",
                    "success_criterion": "The collector reads the declared hello bytes exactly.",
                    "protocol": "Provider-disabled deterministic conformance fixture.",
                    "variants": variants,
                },
                key=_key(nonce, "experiment"),
            )
            baseline = next(
                item for item in experiment["design"]["variants"] if item["kind"] == "baseline"
            )
            admitted = owner.request(
                "POST",
                f"/experiment-variants/{baseline['id']}/admitted-attempts",
                {
                    "expected_variant_revision": baseline["revision"],
                    "expected_design_revision": experiment["current_design_revision"],
                    "grant_id": grant["id"],
                },
                key=_key(nonce, "attempt-admit"),
            )
            worker = _Api(origin, worker_token)
            worker.request("POST", "/worker/heartbeat", {}, key=_key(nonce, "worker-heartbeat"))
            lease = worker.request(
                "POST", "/worker/leases", {"grant_id": grant["id"]}, key=_key(nonce, "lease")
            )
            package = worker.request(
                "POST",
                f"/worker/leases/{lease['id']}/package",
                {
                    "expected_epoch": lease["epoch"],
                    "execution_adapter": "kubernetes-job",
                    "admitted_attempt_id": admitted["id"],
                },
                key=_key(nonce, "package"),
            )
            # Package binding advances the admitted-attempt revision. Dispatch must
            # use the current exported authority, not the pre-package revision.
            packaged_export = owner.request("GET", f"/experiments/{experiment['id']}")
            admitted = next(
                item
                for item in packaged_export["admitted_attempts"]
                if item["id"] == admitted["id"]
            )
            dispatch = owner.request(
                "POST",
                f"/experiment-admitted-attempts/{admitted['id']}/dispatch",
                {
                    "expected_revision": admitted["revision"],
                    "target_digest": target_digest,
                    "profile_version": "development-minikube-opencode-v1",
                },
                key=_key(nonce, "dispatch"),
            )
            credential_file = temporary_root / "worker.credential"
            credential_file.write_text(worker_token)
            credential_file.chmod(0o600)
            adapter = _LiveAdapter(live, cluster, journal)
            result = KubernetesDispatchWorkerService(
                KubernetesDispatchWorkerConfig(
                    origin, dispatch["id"], credential_file, worker_root
                ),
                cluster,
                KubernetesOutputBinding("outputs", "/outputs", pvc_uid, pvc_spec),
                adapter,
            ).run()
            if result.get("status") != "succeeded" or adapter.package is None:
                raise DispatchE2EError(
                    "worker claim did not observe a successful real Kubernetes Job"
                )
            job = live._get("job", adapter.package.binding.job_name, namespace)
            job_uid = _uid(job, "Job", adapter.package.binding.job_name, namespace)
            _verify_bound_pvc(live._get("pvc", "outputs", namespace), namespace, pvc_uid, pvc_spec)
            pods = live._owned_pods(adapter.package.binding, cluster, job_uid)
            if not pods or any(pod.state != "succeeded" for pod in pods):
                raise DispatchE2EError("real Kubernetes Job lacks successful owned Pod evidence")
            live._require_collector_authorization(namespace)
            collector = f"tessera-collector-{adapter.package.operation_id}"
            live._create(_collector_manifest(namespace, collector, image.reference), namespace)
            live._kubectl(
                "wait",
                "--for=jsonpath={.status.phase}=Succeeded",
                f"pod/{collector}",
                "--timeout=60s",
                namespace=namespace,
                timeout=70,
            )
            collector_resource = live._get("pod", collector, namespace)
            collector_uid, collector_image_id = _verify_collector(
                collector_resource, namespace, collector, image, pvc_name="outputs"
            )
            _verify_bound_pvc(live._get("pvc", "outputs", namespace), namespace, pvc_uid, pvc_spec)
            output = live._kubectl(
                "logs", f"pod/{collector}", "--limit-bytes=1024", namespace=namespace
            ).stdout.encode()
            if output != RESULT_BYTES:
                raise DispatchE2EError(
                    "collector output conflicts with the exact deterministic result"
                )
            exported = owner.request("GET", f"/experiments/{experiment['id']}")
            exported_dispatch = next(
                item for item in exported["kubernetes_dispatches"] if item["id"] == dispatch["id"]
            )
            if (
                exported_dispatch["operation_id"] != package["operation"]["operation_id"]
                or exported_dispatch["admitted_attempt_id"] != admitted["id"]
            ):
                raise DispatchE2EError("experiment export does not retain control dispatch linkage")
            return {
                "state": "succeeded",
                "context": CONTEXT,
                "target_digest": target_digest,
                "namespace": namespace,
                "namespace_uid": cluster.namespace_uid,
                "pvc": "outputs",
                "pvc_uid": pvc_uid,
                "job": adapter.package.binding.job_name,
                "job_uid": job_uid,
                "pod_uids": [pod.pod_uid for pod in pods],
                "collector": collector,
                "collector_uid": collector_uid,
                "collector_image_id": collector_image_id,
                "image": image.reference,
                "image_manifest_digest": image.manifest_digest,
                "image_config_digest": image.config_digest,
                "image_source": image_source,
                "output_utf8": output.decode("utf-8"),
                "output_sha256": hashlib.sha256(output).hexdigest(),
                "experiment_id": experiment["id"],
                "admitted_attempt_id": admitted["id"],
                "operation_id": package["operation"]["operation_id"],
                "kubernetes_dispatch_id": dispatch["id"],
                "package_digest": package["package_digest"],
                "exported_dispatch_operation_id": exported_dispatch["operation_id"],
                "worker_registration_id": registered["id"],
                "journal": str(journal),
                "retained": (
                    "namespace, NetworkPolicy, PVC, Job, workload Pod, collector Pod, "
                    "and local journal"
                ),
                "limitations": [
                    "provider-disabled deterministic fixture only",
                    "no OpenCode, model, provider, or credential delivery invocation",
                    "collection readback does not release an artifact or accept a result",
                    "no namespace deletion, broad deletion, prune, or context switch was issued",
                ],
            }
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="perform the retained live dispatch run")
    parser.add_argument(
        "--kubeconfig", type=Path, required=True, help="explicit absolute kubeconfig"
    )
    parser.add_argument("--context", choices=[CONTEXT], default=CONTEXT)
    parser.add_argument(
        "--journal", type=Path, help="new local journal path outside the repository"
    )
    args = parser.parse_args()
    if not args.run:
        parser.error("--run is required; this command has no ambient live mode")
    try:
        print(json.dumps(_main(args), sort_keys=True))
        return 0
    except (
        DispatchE2EError,
        LiveKubernetesError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
    ) as error:
        print(json.dumps({"state": "failed", "message": str(error)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
