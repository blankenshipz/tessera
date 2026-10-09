# ruff: noqa: E501, E701, E702, F841, I001
# fmt: off
"""Run one opt-in, retained Docker Desktop lab-control experiment dispatch.

This provider-disabled fixture renders the shipped single-active lab profile
into one new namespace, uses normal authenticated human/worker routes, and
creates one real deterministic Job. It never uses a current context, registry,
provider credential, or Secret.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import TimeoutError
import hashlib
import importlib.util
import json
import secrets
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

CONTEXT = "docker-desktop"
LABEL = "tessera.io/lab-experiment-fixture"
TIMEOUT_SECONDS = 120
FIXTURE_HOST = "localhost"
ROOT = Path(__file__).resolve().parents[1]


class LabExperimentError(RuntimeError):
    pass


def _load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    if spec is None or spec.loader is None:
        raise LabExperimentError(f"fixture helper {filename} could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _kubectl(kubeconfig: Path, namespace: str | None = None) -> list[str]:
    command = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", CONTEXT]
    if namespace is not None:
        command.extend(["--namespace", namespace])
    return command


def _run(command: list[str], *, input_bytes: bytes | None = None, timeout: int = TIMEOUT_SECONDS) -> str:
    try:
        result = subprocess.run(command, input=input_bytes, capture_output=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LabExperimentError(f"fixture command unavailable or timed out: {command[0]}") from error
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()[-1:]
        raise LabExperimentError(f"fixture command failed ({command[0]}): {' '.join(detail)}")
    return result.stdout.decode("utf-8", "replace")


class _Api:
    """Small explicit port-forward API client; its Host is never ambient."""

    def __init__(self, origin: str, token: str) -> None:
        self.origin, self.token = origin, token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, *, key: str | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        headers = {"Authorization": f"Bearer {self.token}", "Host": FIXTURE_HOST}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if key is not None:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(self.origin + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=10) as response:
                value = json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise LabExperimentError(f"control request {method} {path} returned {error.code}") from error
        except (OSError, TimeoutError, json.JSONDecodeError) as error:
            raise LabExperimentError(f"control request {method} {path} failed") from error
        if not isinstance(value, dict):
            raise LabExperimentError(f"control request {method} {path} returned malformed JSON")
        return value


class _HostControlChannel:
    """Keep the worker transport's literal loopback fence and explicit Host."""

    def __init__(self, origin: str, token: str) -> None:
        from tessera_worker.control_channel import HttpControlChannel

        self._channel = HttpControlChannel(origin, token)

    def post(self, path: str, body: dict[str, Any], key: str, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        from tessera_worker.control_channel import _header_value

        channel = self._channel
        request_timeout = channel.timeout_seconds if timeout_seconds is None else timeout_seconds
        if not isinstance(request_timeout, (int, float)) or not 0 < request_timeout <= channel.timeout_seconds:
            raise ValueError("Request timeout must be positive and no longer than channel timeout.")
        request = urllib.request.Request(
            f"{channel.origin}{channel._path(path)}", data=channel._body(body),
            headers={"Accept": "application/json", "Authorization": f"Bearer {channel.bearer_token}", "Content-Type": "application/json", "Host": FIXTURE_HOST, "Idempotency-Key": _header_value(key, "Idempotency key")}, method="POST",
        )
        future = channel._submit(request, float(request_timeout))
        try:
            return future.result(timeout=float(request_timeout))
        except TimeoutError:
            from tessera_worker.control_channel import ControlChannelError

            raise ControlChannelError("control response deadline exceeded") from None


def _key(nonce: str, action: str) -> str:
    return f"lab-experiment-e2e-{nonce}-{action}"


def _materialize_fixture_reproduction_bundle(bundle: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Verify one export in a new fixture workspace without treating it as a result.

    This is deliberately fixture-local rather than a product import path.  It
    proves only that the immutable bundle's released output bytes can be
    reconstructed under a fresh directory and that the deliberately absent or
    restricted material remains visible to the operator.
    """
    outputs = bundle.get("outputs")
    missing = bundle.get("missing_materials")
    if not isinstance(outputs, list) or not isinstance(missing, list):
        raise LabExperimentError("reproduction bundle lacks bounded output or material records")
    required_material = {
        "input_bytes": "not_exported",
        "skill_material": "restricted",
        "environment_image": "missing",
    }
    reasons = {
        item.get("kind"): item.get("reason")
        for item in missing
        if isinstance(item, dict) and isinstance(item.get("kind"), str)
    }
    if any(
        not isinstance(reasons.get(kind), str) or not reasons[kind].startswith(prefix)
        for kind, prefix in required_material.items()
    ):
        raise LabExperimentError("reproduction bundle does not disclose required missing material")
    workspace.mkdir(mode=0o700)
    root = workspace.resolve()
    written: list[dict[str, Any]] = []
    for item in outputs:
        if not isinstance(item, dict):
            raise LabExperimentError("reproduction bundle output record is invalid")
        relative_path = item.get("relative_path")
        encoded = item.get("content_base64")
        sha256 = item.get("sha256")
        size = item.get("size")
        relative = Path(relative_path) if isinstance(relative_path, str) else None
        if (
            relative is None
            or relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
            or not isinstance(encoded, str)
            or not isinstance(sha256, str)
            or type(size) is not int
        ):
            raise LabExperimentError("reproduction bundle output path or metadata is invalid")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise LabExperimentError("reproduction bundle output encoding is invalid") from error
        target = workspace / relative
        try:
            target.resolve().relative_to(root)
        except ValueError as error:
            raise LabExperimentError("reproduction bundle output escapes the fresh workspace") from error
        if len(content) != size or hashlib.sha256(content).hexdigest() != sha256:
            raise LabExperimentError("reproduction bundle output integrity check failed")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o600)
        written.append({"relative_path": relative_path, "sha256": sha256, "size": size})
    if not written:
        raise LabExperimentError("reproduction bundle has no released outputs")
    return {"outputs": written, "missing_material_kinds": sorted(required_material)}


def _owner_token(kubeconfig: Path, namespace: str) -> str:
    header = _run(_kubectl(kubeconfig, namespace) + ["exec", "deploy/control", "--", "cat", "/state/control/owner.header"]).strip()
    if not header.startswith("Authorization: Bearer ") or not header.removeprefix("Authorization: Bearer "):
        raise LabExperimentError("fixture control owner credential is malformed")
    return header.removeprefix("Authorization: Bearer ")


def _adopt_snapshot(kubeconfig: Path, namespace: str, project_id: str, nonce: str) -> dict[str, Any]:
    shell = "set -eu; rm -rf /tmp/lab-e2e-input; mkdir -p /tmp/lab-e2e-input; printf 'lab control deterministic input\\n' > /tmp/lab-e2e-input/input.txt"
    _run(_kubectl(kubeconfig, namespace) + ["exec", "deploy/control", "--", "sh", "-ec", shell])
    inventory = json.loads(_run(_kubectl(kubeconfig, namespace) + ["exec", "deploy/control", "--", "tessera", "workspace", "inventory", "--root", "/tmp/lab-e2e-input"]))
    digest = inventory.get("digest")
    if not isinstance(digest, str):
        raise LabExperimentError("fixture workspace inventory has no digest")
    adopted = json.loads(_run(_kubectl(kubeconfig, namespace) + ["exec", "deploy/control", "--", "tessera", "workspace", "adopt", "--root", "/tmp/lab-e2e-input", "--state-dir", "/state/control", "--project-id", project_id, "--expected-digest", digest, "--idempotency-key", _key(nonce, "snapshot-adopt")]))
    if adopted.get("digest") != digest:
        raise LabExperimentError("fixture workspace adoption returned a conflicting digest")
    return adopted


def _cluster(live: Any, namespace: str, contracts: Any, docker: Any):
    namespace_uid = docker._uid(live._get("namespace", namespace, None), "Namespace", namespace, None)
    policy = live._get("networkpolicy", "default-deny", namespace)
    policy_uid = docker._uid(policy, "NetworkPolicy", "default-deny", namespace)
    policy_spec = policy.get("spec")
    if not isinstance(policy_spec, dict) or policy_spec.get("podSelector") != {} or sorted(policy_spec.get("policyTypes", [])) != ["Egress", "Ingress"]:
        raise LabExperimentError("shipped lab default-deny policy does not provide fixture network-none evidence")
    live._create(docker._pvc(namespace), namespace)
    pvc = live._get("pvc", "outputs", namespace)
    target = live.target
    cluster = contracts.KubernetesClusterConfig(kubeconfig=str(target.kubeconfig), kubeconfig_sha256=target.kubeconfig_sha256, context=target.context, profile="development-minikube-opencode-v1", server=target.server, server_sha256=hashlib.sha256(target.server.encode()).hexdigest(), ca_sha256=target.ca_sha256, namespace=namespace, cluster_uid=docker._uid(live._get("namespace", "kube-system", None), "Namespace", "kube-system", None), namespace_uid=namespace_uid)
    return cluster, docker._uid(pvc, "PersistentVolumeClaim", "outputs", namespace), docker._pvc_spec_sha256(pvc)


def _restart_control(restart: Any, kubeconfig: Path, namespace: str, before: dict[str, Any], forward: subprocess.Popen[bytes], port: int):
    _run(_kubectl(kubeconfig, namespace) + ["delete", "pod", str(before["metadata"]["name"]), "--wait=false"])
    _run(_kubectl(kubeconfig, namespace) + ["wait", "--for=condition=available", "deployment/control", "--timeout=90s"], timeout=100)
    after = restart._pod(kubeconfig, namespace)
    if after.get("metadata", {}).get("uid") == before.get("metadata", {}).get("uid"):
        raise LabExperimentError("control restart did not change the fixture Pod identity")
    restart._stop_port_forward(forward)
    forward = restart._start_port_forward(kubeconfig, namespace, port)
    restart._wait_ready(port)
    return forward, after


def _main(args: argparse.Namespace) -> dict[str, Any]:
    restart = _load_script("lab_restart_helper", "check_kubernetes_control_plane_restart.py")
    authenticated = _load_script("lab_dispatch_helper", "check_authenticated_kubernetes_dispatch_e2e.py")
    from tessera_worker import docker_desktop_kubernetes as docker
    from tessera_worker import kubernetes_contracts as contracts
    from tessera_worker.kubernetes_dispatch_service import KubernetesDispatchWorkerConfig, KubernetesDispatchWorkerService
    from tessera_worker.kubernetes_execution import KubernetesOutputBinding

    kubeconfig = args.kubeconfig.resolve(strict=True)
    if _run(_kubectl(kubeconfig) + ["get", "namespace", args.namespace, "--ignore-not-found", "-o", "name"], timeout=20).strip():
        raise LabExperimentError("fixture namespace already exists")
    nonce = uuid.uuid4().hex[:12]
    restart._require_containerd_image_store(); restart._require_docker_desktop_kind_cluster()
    control_image = restart._load_image(ROOT, nonce)
    workload_image = docker.build_and_load_exact_image(ROOT, nonce)
    _run(_kubectl(kubeconfig) + ["apply", "-f", "-"], input_bytes=restart._manifest(args.namespace, nonce, control_image))
    namespace_uid = restart._namespace_uid(kubeconfig, args.namespace, nonce)
    _run(_kubectl(kubeconfig) + ["label", "namespace", args.namespace, f"{LABEL}={nonce}"], timeout=20)
    _run(_kubectl(kubeconfig, args.namespace) + ["wait", "--for=condition=available", "deployment/control", "--timeout=90s"], timeout=100)
    target = docker.target_from_kubeconfig(kubeconfig)
    journal = Path("/private/tmp") / f"tessera-lab-experiment-{nonce}.sqlite3"
    live = docker.DockerDesktopKubernetesAdapter(target, journal)
    cluster, pvc_uid, pvc_spec = _cluster(live, args.namespace, contracts, docker)
    port = authenticated._port()
    forward = restart._start_port_forward(kubeconfig, args.namespace, port)
    before_pod = restart._pod(kubeconfig, args.namespace)
    try:
        before_health = restart._wait_ready(port)
        owner = _Api(f"http://127.0.0.1:{port}", _owner_token(kubeconfig, args.namespace))
        project = owner.request("POST", "/projects", {"name": "Lab persistent-control experiment"}, key=_key(nonce, "project"))
        snapshot = _adopt_snapshot(kubeconfig, args.namespace, project["id"], nonce)
        worker_token = secrets.token_urlsafe(48)
        credential = owner.request("POST", "/credentials", {"token": worker_token, "kind": "worker", "audience": "worker", "project_id": project["id"], "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}, key=_key(nonce, "worker-credential"))
        owner.request("POST", f"/projects/{project['id']}/workers", {"worker_principal_id": credential["principal_id"], "approved_build_identifier": "lab-persistent-control-e2e", "approved_build_digest": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "trust_class": "development", "region": "docker-desktop"}, key=_key(nonce, "worker-register"))
        owner.request("POST", f"/snapshots/{snapshot['id']}/releases", {"expected_digest": snapshot["digest"], "worker_principal_id": credential["principal_id"], "classification": "public", "permits_worker_transfer": True, "rationale": "Explicit deterministic lab fixture-only input release.", "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat()}, key=_key(nonce, "snapshot-release"))
        task = owner.request("POST", f"/projects/{project['id']}/tasks", {"title": "Lab deterministic Kubernetes Job", "purpose": "Provider-disabled persistent-control dispatch fixture.", "budget_cents": 0, "max_duration_seconds": args.timeout}, key=_key(nonce, "task"))
        task = owner.request("PUT", f"/tasks/{task['id']}/plan", {"expected_revision": task["revision"], "title": task["title"], "purpose": task["purpose"], "budget_cents": 0, "max_duration_seconds": args.timeout, "execution_spec": {"argv": ["/hello", "--write", "/outputs/result.txt"], "input_snapshot_digest": snapshot["digest"], "pinned_skill_digests": [], "output_paths": ["result.txt"], "profile": "kubernetes-opencode", "network_enabled": False, "max_output_bytes": 1024}}, key=_key(nonce, "plan"))
        approved = owner.request("POST", f"/tasks/{task['id']}/approve", {"expected_revision": task["revision"], "expected_plan_digest": task["current_plan_digest"]}, key=_key(nonce, "approve"))
        if approved.get("execution_granted") is not False: raise LabExperimentError("task approval was incorrectly treated as execution admission")
        owner.request("PUT", f"/projects/{project['id']}/execution-limits", {"expected_revision": 0, "currency": "USD", "monetary_limit_cents": 0, "max_concurrency": 1}, key=_key(nonce, "limits"))
        grant = owner.request("POST", f"/projects/{project['id']}/execution-grants", {"task_id": task["id"], "expected_task_revision": approved["revision"], "expected_plan_digest": approved["current_plan_digest"], "worker_principal_id": credential["principal_id"], "snapshot_id": snapshot["id"], "pinned_image": workload_image.reference, "expires_at": (datetime.now(UTC) + timedelta(minutes=20)).isoformat(), "max_exposure_cents": 0, "currency": "USD", "acknowledges_unmeasured_infrastructure": True}, key=_key(nonce, "grant"))
        variants = [{"kind": kind, "name": f"hello {kind}", "method_digest": hashlib.sha256(f"hello-{kind}".encode()).hexdigest(), "input_digest": snapshot["digest"], "metadata": {"fixture": True}, "execution_task_id": task["id"], "execution_plan_digest": approved["current_plan_digest"]} for kind in ("baseline", "intervention")]
        experiment = owner.request("POST", f"/projects/{project['id']}/experiments", {"goal": "Verify one persistent-control deterministic dispatch.", "success_criterion": "The retained collector reads the fixed output bytes.", "protocol": "Provider-disabled deterministic fixture.", "variants": variants}, key=_key(nonce, "experiment"))
        baseline = next(item for item in experiment["design"]["variants"] if item["kind"] == "baseline")
        admitted = owner.request("POST", f"/experiment-variants/{baseline['id']}/admitted-attempts", {"expected_variant_revision": baseline["revision"], "expected_design_revision": experiment["current_design_revision"], "grant_id": grant["id"]}, key=_key(nonce, "admit"))
        worker = _Api(f"http://127.0.0.1:{port}", worker_token)
        worker.request("POST", "/worker/heartbeat", {}, key=_key(nonce, "heartbeat")); lease = worker.request("POST", "/worker/leases", {"grant_id": grant["id"]}, key=_key(nonce, "lease"))
        package = worker.request("POST", f"/worker/leases/{lease['id']}/package", {"expected_epoch": lease["epoch"], "execution_adapter": "kubernetes-job", "admitted_attempt_id": admitted["id"]}, key=_key(nonce, "package"))
        admitted = next(item for item in owner.request("GET", f"/experiments/{experiment['id']}")["admitted_attempts"] if item["id"] == admitted["id"])
        dispatch = owner.request("POST", f"/experiment-admitted-attempts/{admitted['id']}/dispatch", {"expected_revision": admitted["revision"], "target_digest": contracts.cluster_identity_digest(cluster), "profile_version": "development-minikube-opencode-v1"}, key=_key(nonce, "dispatch"))
        forward, after_pod = _restart_control(restart, kubeconfig, args.namespace, before_pod, forward, port)
        if before_health.get("backend_identity") != restart._wait_ready(port).get("backend_identity"): raise LabExperimentError("persisted control backend identity changed after restart")
        with tempfile.TemporaryDirectory(prefix="tessera-lab-experiment-worker-", dir="/private/tmp") as temporary:
            worker_root = Path(temporary) / "worker"; worker_root.mkdir(mode=0o700)
            credential_file = Path(temporary) / "worker.credential"; credential_file.write_text(worker_token); credential_file.chmod(0o600)
            class _ObservedLiveAdapter(authenticated._LiveAdapter):
                def __init__(self, *adapter_args):
                    super().__init__(*adapter_args)
                    self.submit_error: str | None = None
                    self.submit_status: str | None = None

                def submit(self, package, *, timeout_seconds: float | None = None):
                    try:
                        result = super().submit(package, timeout_seconds=timeout_seconds)
                        self.submit_status = result.observed_state
                        return result
                    except Exception as error:
                        self.submit_error = f"{type(error).__name__}: {error}"
                        raise

            adapter = _ObservedLiveAdapter(live, cluster, journal)
            def service(): return KubernetesDispatchWorkerService(KubernetesDispatchWorkerConfig(f"http://127.0.0.1:{port}", dispatch["id"], credential_file, worker_root), cluster, KubernetesOutputBinding("outputs", "/outputs", pvc_uid, pvc_spec), adapter, channel=_HostControlChannel(f"http://127.0.0.1:{port}", worker_token), poll_seconds=0.5)
            worker_result = service().run()
            if worker_result.get("status") != "succeeded" or adapter.package is None: raise LabExperimentError(f"worker did not observe the real deterministic Job as succeeded: {json.dumps(worker_result, sort_keys=True)}; submit_status={adapter.submit_status}; submit_error={adapter.submit_error}")
            binding = adapter.package.binding; job_uid = docker._uid(live._get("job", binding.job_name, args.namespace), "Job", binding.job_name, args.namespace)
            service().run(max_cycles=1)
            if docker._uid(live._get("job", binding.job_name, args.namespace), "Job", binding.job_name, args.namespace) != job_uid: raise LabExperimentError("worker restart did not retain the original Job identity")
            jobs = json.loads(live._kubectl("get", "jobs", "-l", f"tessera.dev/operation={binding.operation_id}", "-o", "json", namespace=args.namespace).stdout).get("items", [])
            if len(jobs) != 1: raise LabExperimentError("worker reconciliation created a duplicate deterministic Job")
            authenticated._wait_for_bound_pvc(live, args.namespace, pvc_uid, pvc_spec, min(args.timeout, 60)); pods = live._owned_pods(binding, cluster, job_uid)
            if not pods or any(pod.state != "succeeded" for pod in pods): raise LabExperimentError("real Kubernetes Job lacks successful owned Pod evidence")
            collector = f"tessera-collector-{binding.operation_id}"; live._require_collector_authorization(args.namespace); live._create(docker._collector_manifest(args.namespace, collector, workload_image.reference), args.namespace)
            live._kubectl("wait", "--for=jsonpath={.status.phase}=Succeeded", f"pod/{collector}", "--timeout=60s", namespace=args.namespace, timeout=70)
            collector_uid, _image_id = docker._verify_collector(live._get("pod", collector, args.namespace), args.namespace, collector, workload_image, pvc_name="outputs")
            output = live._kubectl("logs", f"pod/{collector}", "--limit-bytes=1024", namespace=args.namespace).stdout.encode()
            if output != docker.RESULT_BYTES: raise LabExperimentError("collector output conflicts with the deterministic fixture result")
            output_sha256 = hashlib.sha256(output).hexdigest()
            upload = worker.request("POST", f"/worker/leases/{lease['id']}/artifact-uploads", {"expected_epoch": lease["epoch"], "operation_id": binding.operation_id, "relative_path": "result.txt", "sha256": output_sha256, "size": len(output)}, key=_key(nonce, "upload-begin"))
            split = max(1, len(output) // 2)
            worker.request("POST", f"/worker/leases/{lease['id']}/artifact-uploads/{upload['id']}/chunks", {"offset": 0, "content_base64": base64.b64encode(output[:split]).decode("ascii")}, key=_key(nonce, "upload-first-chunk"))
            forward, transfer_restart_pod = _restart_control(restart, kubeconfig, args.namespace, after_pod, forward, port)
            if after_pod.get("metadata", {}).get("uid") == transfer_restart_pod.get("metadata", {}).get("uid"):
                raise LabExperimentError("interrupted transfer restart did not change the control Pod identity")
            resumed = worker.request("POST", f"/worker/leases/{lease['id']}/artifact-uploads/{upload['id']}/status", {}, key=_key(nonce, "upload-status-after-restart"))
            if resumed.get("received_bytes") != split:
                raise LabExperimentError("interrupted transfer progress was not retained across control restart")
            worker.request("POST", f"/worker/leases/{lease['id']}/artifact-uploads/{upload['id']}/chunks", {"offset": split, "content_base64": base64.b64encode(output[split:]).decode("ascii")}, key=_key(nonce, "upload-second-chunk"))
            artifact = worker.request("POST", f"/worker/leases/{lease['id']}/artifact-uploads/{upload['id']}/complete", {}, key=_key(nonce, "upload-complete"))
            current = next(item for item in owner.request("GET", f"/experiments/{experiment['id']}")["admitted_attempts"] if item["id"] == admitted["id"])
            linked = owner.request("POST", f"/experiment-attempts/{admitted['id']}/artifacts", {"expected_revision": current["revision"], "artifact_ids": [artifact["id"]]}, key=_key(nonce, "link-artifact"))
            measurement = owner.request("POST", f"/experiment-attempts/{admitted['id']}/measurements", {"expected_revision": linked["revision"], "name": "deterministic output digest", "value": {"bytes": len(output), "sha256": output_sha256}, "artifact_id": artifact["id"]}, key=_key(nonce, "measurement"))
        manifest = worker.request("POST", f"/worker/leases/{lease['id']}/artifact-manifest", {"expected_epoch": lease["epoch"], "operation_id": binding.operation_id, "artifacts": [{"id": artifact["id"], "relative_path": "result.txt", "sha256": artifact["sha256"], "size": artifact["size"]}]}, key=_key(nonce, "manifest"))
        if manifest.get("status") != "quarantined" or manifest.get("classification") != "unclassified": raise LabExperimentError("output manifest was not retained under quarantine")
        disposition = owner.request("POST", f"/artifact-manifests/{manifest['id']}/dispositions", {"expected_manifest_digest": manifest["manifest_digest"], "expected_revision": 0, "disposition": "retain_quarantined", "rationale": "Retain deterministic fixture output under quarantine."}, key=_key(nonce, "retain-quarantined"))
        release = owner.request("POST", f"/artifact-manifests/{manifest['id']}/custody-releases", {"expected_manifest_digest": manifest["manifest_digest"], "expected_revision": 0, "action": "release", "rationale": "Allow fixture-owner retrieval of retained private output."}, key=_key(nonce, "custody-release"))
        released = owner.request("GET", f"/artifact-manifests/{manifest['id']}/released")
        if released.get("manifest", {}).get("status") != "quarantined" or released.get("manifest", {}).get("classification") != "unclassified": raise LabExperimentError("custody release changed output quarantine")
        request = urllib.request.Request(f"http://127.0.0.1:{port}/artifact-manifests/{manifest['id']}/artifacts/{artifact['id']}/content", headers={"Authorization": f"Bearer {owner.token}", "Host": FIXTURE_HOST}, method="GET")
        try:
            with owner.opener.open(request, timeout=10) as response:
                retrieved_bytes = response.read()
                retrieved_sha256 = response.headers.get("X-Tessera-Artifact-Sha256")
        except (OSError, urllib.error.HTTPError) as error:
            raise LabExperimentError("authorized fixture retrieval failed") from error
        if retrieved_bytes != output or retrieved_sha256 != output_sha256: raise LabExperimentError("authorized retrieval bytes or digest conflict with collected output")
        bundle = owner.request("POST", f"/artifact-manifests/{manifest['id']}/reproduction-bundles", {"max_bytes": 1024 * 1024}, key=_key(nonce, "reproduction-bundle"))
        with tempfile.TemporaryDirectory(prefix="tessera-lab-reproduction-", dir="/private/tmp") as temporary:
            reproduced = _materialize_fixture_reproduction_bundle(bundle["bundle"], Path(temporary) / "fresh-workspace")
        final = owner.request("GET", f"/experiments/{experiment['id']}"); exported_dispatch = next(item for item in final["kubernetes_dispatches"] if item["id"] == dispatch["id"])
        if exported_dispatch["operation_id"] != binding.operation_id or measurement["attempt_id"] != admitted["id"]: raise LabExperimentError("output measurement or dispatch linkage conflicts with the operation")
        return {"state": "succeeded", "context": CONTEXT, "namespace": args.namespace, "namespace_uid": namespace_uid, "control_pod_uid_before": before_pod["metadata"]["uid"], "control_pod_uid_after": after_pod["metadata"]["uid"], "control_pod_uid_after_transfer_restart": transfer_restart_pod["metadata"]["uid"], "operation_id": binding.operation_id, "job": binding.job_name, "job_uid": job_uid, "job_count": len(jobs), "collector": collector, "collector_uid": collector_uid, "output_sha256": output_sha256, "artifact_id": artifact["id"], "manifest_id": manifest["id"], "manifest_digest": manifest["manifest_digest"], "custody_release_id": release["id"], "disposition_id": disposition["id"], "reproduction_bundle_id": bundle["id"], "reproduction_bundle_digest": bundle["bundle_digest"], "fresh_workspace_outputs": reproduced["outputs"], "fresh_workspace_missing_material_kinds": reproduced["missing_material_kinds"], "measurement_id": measurement["id"], "admitted_attempt_id": admitted["id"], "kubernetes_dispatch_id": dispatch["id"], "baseline_and_intervention_registered": True, "approval_execution_granted": approved["execution_granted"], "retained": "fixture namespace, lab control PVC, output PVC, Job, Pods, images, and adapter journal", "limitations": ["provider-disabled deterministic fixture only", "the fixture executes only the baseline; the intervention remains registered but unexecuted", "custody release is private human retrieval and preserves quarantined/unclassified output", "the fresh workspace verifies released fixture output only; input, skill, and environment material remain explicit missing/restricted records", "output linkage does not accept an artifact or scientific result", "no Secret, provider, registry, namespace deletion, prune, or current-context fallback was used"]}
    finally:
        restart._stop_port_forward(forward)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--run", action="store_true"); parser.add_argument("--kubeconfig", type=Path); parser.add_argument("--context", choices=[CONTEXT], default=CONTEXT); parser.add_argument("--namespace"); parser.add_argument("--timeout", type=int, default=TIMEOUT_SECONDS); args = parser.parse_args()
    if not args.run:
        print(json.dumps({"fixture_verdict": "skipped", "state": "not_started", "message": "--run required"}, sort_keys=True)); return 1
    if args.kubeconfig is None or args.namespace is None or not args.namespace.startswith("tessera-lab-experiment-e2e-"):
        print(json.dumps({"fixture_verdict": "failed", "state": "not_started", "message": "--kubeconfig and fresh tessera-lab-experiment-e2e-* namespace are required"}, sort_keys=True)); return 1
    if not 1 <= args.timeout <= 180:
        print(json.dumps({"fixture_verdict": "failed", "state": "not_started", "message": "--timeout must be between 1 and 180 seconds"}, sort_keys=True)); return 1
    try: result = _main(args)
    except (LabExperimentError, RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({"fixture_verdict": "failed", "state": "failed", "message": str(error)}, sort_keys=True)); return 1
    print(json.dumps({"fixture_verdict": "passed", **result}, sort_keys=True)); return 0


if __name__ == "__main__": raise SystemExit(main())
