# ruff: noqa: E501
# fmt: off
"""Opt-in Docker Desktop proof of PVC-backed control-plane restart readiness.

This fixture never uses a current context or a pre-existing namespace.  It is
not a production deployment, provider test, or Kubernetes target enrollment.
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

CONTEXT = "docker-desktop"
LABEL = "tessera.io/control-restart-fixture"
TIMEOUT_SECONDS = 120
MAX_IMAGE_ARCHIVE_BYTES = 512 * 1024 * 1024
DOCKER_DESKTOP_KIND_NODE = "desktop-control-plane"
PROFILE_PATH = Path(__file__).resolve().parents[1] / "deploy/kubernetes/lab-control-plane.yaml"
FIXTURE_TRUSTED_HOST = "localhost"


class FixtureError(RuntimeError):
    pass


def _run(command: list[str], *, input_bytes: bytes | None = None, timeout: int = TIMEOUT_SECONDS) -> str:
    try:
        result = subprocess.run(
            command,
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FixtureError(f"fixture command unavailable or timed out: {command[0]}") from error
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace").strip().splitlines()[-1:]
        raise FixtureError(f"fixture command failed ({command[0]}): {' '.join(message)}")
    return result.stdout.decode("utf-8", "replace")


def _kubectl(kubeconfig: Path, namespace: str | None = None) -> list[str]:
    command = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", CONTEXT]
    if namespace is not None:
        command.extend(["--namespace", namespace])
    return command


def _profile_documents() -> list[dict[str, object]]:
    """Load the shipped profile before rendering its isolated fixture variant."""
    try:
        documents = list(yaml.safe_load_all(PROFILE_PATH.read_text()))
    except (OSError, yaml.YAMLError) as error:
        raise FixtureError("shipped Kubernetes lab profile could not be read") from error
    if not all(isinstance(document, dict) for document in documents):
        raise FixtureError("shipped Kubernetes lab profile has an invalid document")
    required = {
        ("Namespace", "tessera-lab"),
        ("ConfigMap", "tessera-control-remote"),
        ("PersistentVolumeClaim", "tessera-control-state"),
        ("Deployment", "tessera-control"),
        ("Service", "tessera-control"),
        ("NetworkPolicy", "default-deny"),
        ("NetworkPolicy", "allow-labelled-control-clients"),
        ("NetworkPolicy", "allow-labelled-control-client-egress"),
        ("NetworkPolicy", "allow-tls-control-client-dns"),
    }
    identities = {
        (
            document.get("kind"),
            document.get("metadata", {}).get("name")
            if isinstance(document.get("metadata"), dict)
            else None,
        )
        for document in documents
    }
    if not required <= identities:
        raise FixtureError("shipped Kubernetes lab profile is missing a required resource")
    return documents


def _resource(documents: list[dict[str, object]], kind: str, name: str) -> dict[str, object]:
    for document in documents:
        metadata = document.get("metadata")
        if document.get("kind") == kind and isinstance(metadata, dict) and metadata.get("name") == name:
            return document
    raise FixtureError(f"shipped Kubernetes lab profile is missing {kind}/{name}")


def _set_probe_host(container: dict[str, object], host: str) -> None:
    for probe_name in ("readinessProbe", "livenessProbe"):
        probe = container.get(probe_name)
        http_get = probe.get("httpGet") if isinstance(probe, dict) else None
        headers = http_get.get("httpHeaders") if isinstance(http_get, dict) else None
        if not isinstance(headers, list):
            raise FixtureError(f"shipped {probe_name} has no Host header")
        for header in headers:
            if isinstance(header, dict) and header.get("name") == "Host":
                header["value"] = host
                break
        else:
            raise FixtureError(f"shipped {probe_name} has no Host header")


def _manifest(namespace: str, nonce: str, image: str) -> bytes:
    """Render the shipped profile with only fixture ownership and image changes."""
    labels = {LABEL: nonce, "app.kubernetes.io/name": "tessera-control-restart"}
    documents = copy.deepcopy(_profile_documents())
    namespace_document = _resource(documents, "Namespace", "tessera-lab")
    namespace_document["metadata"] = {"name": namespace, "labels": labels}
    for document in documents:
        if document is not namespace_document:
            metadata = document.get("metadata")
            if not isinstance(metadata, dict):
                raise FixtureError("shipped Kubernetes lab profile has invalid metadata")
            metadata["namespace"] = namespace
            metadata["labels"] = {**metadata.get("labels", {}), **labels}

    config = _resource(documents, "ConfigMap", "tessera-control-remote")
    config["data"] = {
        "TESSERA_TRUSTED_HOST": FIXTURE_TRUSTED_HOST,
        "TESSERA_PUBLIC_ORIGIN": f"https://{FIXTURE_TRUSTED_HOST}",
    }
    pvc = _resource(documents, "PersistentVolumeClaim", "tessera-control-state")
    pvc["metadata"]["name"] = "state"  # type: ignore[index]
    pvc["spec"]["resources"]["requests"]["storage"] = "64Mi"  # type: ignore[index]
    deployment = _resource(documents, "Deployment", "tessera-control")
    deployment["metadata"]["name"] = "control"  # type: ignore[index]
    pod_metadata = deployment["spec"]["template"].get("metadata")  # type: ignore[index]
    if not isinstance(pod_metadata, dict):
        raise FixtureError("shipped Kubernetes lab profile has no Pod metadata")
    # Keep the shipped Deployment selector labels intact. The fixture ownership
    # label is the only additional Pod label; applying the resource's renamed
    # app label here would make the immutable selector no longer match.
    pod_metadata["labels"] = {**pod_metadata.get("labels", {}), LABEL: nonce}
    pod_spec = deployment["spec"]["template"]["spec"]  # type: ignore[index]
    for container in [*pod_spec["initContainers"], *pod_spec["containers"]]:  # type: ignore[index]
        container["image"] = image
        container["imagePullPolicy"] = "Never"
    control = next(
        (container for container in pod_spec["containers"] if container.get("name") == "control"),  # type: ignore[index]
        None,
    )
    if not isinstance(control, dict):
        raise FixtureError("shipped Kubernetes lab profile has no control container")
    _set_probe_host(control, FIXTURE_TRUSTED_HOST)
    state_claim = next(
        (
            volume.get("persistentVolumeClaim")
            for volume in pod_spec["volumes"]  # type: ignore[index]
            if volume.get("name") == "state"
        ),
        None,
    )
    if not isinstance(state_claim, dict):
        raise FixtureError("shipped Kubernetes lab profile has no state PVC volume")
    state_claim["claimName"] = "state"
    service = _resource(documents, "Service", "tessera-control")
    service["metadata"]["name"] = "control"  # type: ignore[index]
    return yaml.safe_dump_all(documents, sort_keys=False).encode("utf-8")


def _load_image(root: Path, nonce: str, *, prebuilt_image_id: str | None = None) -> str:
    # Docker Desktop's kind node has an isolated k8s.io containerd namespace.
    # Load the one bounded local archive into that known node, rather than
    # assuming a host Docker image is visible to kubelet or using a registry.
    reference = f"docker.io/library/tessera-control-restart:{nonce}"
    if prebuilt_image_id is None:
        _run(
            [
                "docker",
                "buildx",
                "build",
                "--load",
                "--platform",
                "linux/amd64",
                "--target",
                "lab",
                "--tag",
                reference,
                str(root),
            ],
            timeout=300,  # Pinned base manifest resolution can outlast the default.
        )
    try:
        images = json.loads(_run(["docker", "image", "inspect", reference]))
        image = images[0]
        if len(images) != 1 or image.get("Os") != "linux" or image.get("Architecture") != "amd64":
            raise ValueError("loaded image is not linux/amd64")
        if prebuilt_image_id is not None and image.get("Id") != prebuilt_image_id:
            raise ValueError("prebuilt image identity changed before node import")
    except (IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise FixtureError(
            "Docker Desktop did not load one linux/amd64 fixture image into its containerd image store"
        ) from error
    with TemporaryDirectory(prefix="tessera-control-restart-image-") as temporary:
        archive = Path(temporary) / "image.tar"
        _run(["docker", "image", "save", "--output", str(archive), reference])
        try:
            archive_size = archive.stat().st_size
            if not 0 < archive_size <= MAX_IMAGE_ARCHIVE_BYTES:
                raise ValueError("fixture image archive exceeds the bounded import size")
            archive_bytes = archive.read_bytes()
        except OSError as error:
            raise FixtureError("Docker Desktop fixture image archive could not be read") from error
        except ValueError as error:
            raise FixtureError(str(error)) from error
        _run(
            [
                "docker",
                "exec",
                "-i",
                DOCKER_DESKTOP_KIND_NODE,
                "ctr",
                "--namespace",
                "k8s.io",
                "images",
                "import",
                "--base-name",
                reference,
                "-",
            ],
            input_bytes=archive_bytes,
        )
        refs = _run(
            [
                "docker",
                "exec",
                DOCKER_DESKTOP_KIND_NODE,
                "ctr",
                "--namespace",
                "k8s.io",
                "images",
                "ls",
                "--quiet",
            ]
        ).splitlines()
        short_reference = reference.removeprefix("docker.io/library/")
        source = next((item for item in refs if item in {reference, short_reference}), None)
        if source is None:
            raise FixtureError("Docker Desktop kind node did not retain the imported fixture image")
        if source != reference:
            _run(
                [
                    "docker",
                    "exec",
                    DOCKER_DESKTOP_KIND_NODE,
                    "ctr",
                    "--namespace",
                    "k8s.io",
                    "images",
                    "tag",
                    source,
                    reference,
                ]
            )
        verified_refs = _run(
            [
                "docker",
                "exec",
                DOCKER_DESKTOP_KIND_NODE,
                "ctr",
                "--namespace",
                "k8s.io",
                "images",
                "ls",
                "--quiet",
            ]
        ).splitlines()
        if reference not in verified_refs:
            raise FixtureError("Docker Desktop kind node did not retain the canonical fixture image reference")
        _run(["docker", "exec", DOCKER_DESKTOP_KIND_NODE, "crictl", "inspecti", reference])
    return reference


def _require_containerd_image_store() -> None:
    driver_status = json.loads(_run(["docker", "info", "--format", "{{json .DriverStatus}}"], timeout=20))
    if ["driver-type", "io.containerd.snapshotter.v1"] not in driver_status:
        raise FixtureError(
            "Docker Desktop is using the legacy image store; its kind cluster cannot use the fixture image. "
            "Enable Use containerd for pulling and storing images, then retry."
        )


def _require_docker_desktop_kind_cluster() -> None:
    """Reject a host whose selected Docker Desktop cluster cannot share this store."""
    status = _run(["docker", "desktop", "kubernetes", "status"], timeout=20)
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    if fields.get("State", "").strip().lower() != "running" or fields.get("Mode", "").strip().lower() != "kind":
        raise FixtureError(
            "Docker Desktop Kubernetes must be running in kind mode for the containerd image-load fixture"
        )


def _pod(kubeconfig: Path, namespace: str) -> dict[str, object]:
    payload = json.loads(
        _run(_kubectl(kubeconfig, namespace) + ["get", "pods", "-l", LABEL, "-o", "json"])
    )
    items = payload.get("items", [])
    if len(items) != 1:
        raise FixtureError("fixture Deployment did not have exactly one labelled Pod")
    return items[0]


def _namespace_uid(kubeconfig: Path, namespace: str, nonce: str) -> str:
    payload = json.loads(_run(_kubectl(kubeconfig) + ["get", "namespace", namespace, "-o", "json"], timeout=20))
    metadata = payload.get("metadata", {})
    labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
    uid = metadata.get("uid") if isinstance(metadata, dict) else None
    if labels.get(LABEL) != nonce or not isinstance(uid, str) or not uid:
        raise FixtureError("fixture namespace ownership identity is unavailable")
    return uid


def _request(
    port: int,
    path: str,
    headers: dict[str, str] | None = None,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    request_headers = dict(headers or {})
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _owner_authorization(header: str) -> str:
    try:
        name, value = header.split(": ", 1)
    except ValueError as error:
        raise FixtureError("fixture owner header is malformed") from error
    if name != "Authorization" or not value.startswith("Bearer ") or not value.removeprefix("Bearer "):
        raise FixtureError("fixture owner header is malformed")
    return value


def _start_port_forward(kubeconfig: Path, namespace: str, port: int) -> subprocess.Popen[bytes]:
    try:
        return subprocess.Popen(
            _kubectl(kubeconfig, namespace) + ["port-forward", "service/control", f"{port}:8765"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        raise FixtureError("fixture port-forward could not start") from error


def _stop_port_forward(forward: subprocess.Popen[bytes]) -> None:
    if forward.poll() is not None:
        return
    forward.terminate()
    try:
        forward.wait(timeout=10)
    except subprocess.TimeoutExpired:
        forward.kill()
        forward.wait(timeout=10)


def _wait_ready(port: int) -> dict[str, object]:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            status, payload = _request(port, "/health/ready", {"Host": FIXTURE_TRUSTED_HOST})
            if status == 200:
                live_status, _ = _request(port, "/health/live", {"Host": FIXTURE_TRUSTED_HOST})
                if live_status == 200:
                    return payload
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(1)
    raise FixtureError("fixture control plane health probes never both returned success")


def _project_created_event(port: int, project: dict[str, object], headers: dict[str, str]) -> dict[str, object]:
    project_id = project.get("id")
    if not isinstance(project_id, str) or not project_id:
        raise FixtureError("fixture project response has no identity")
    status, payload = _request(port, f"/projects/{project_id}/labbook", headers)
    events = payload.get("events")
    matching = (
        [event for event in events if isinstance(event, dict) and event.get("event_type") == "project.created"]
        if isinstance(events, list)
        else []
    )
    if status != 200 or len(matching) != 1 or not isinstance(matching[0].get("cursor"), int):
        raise FixtureError("fixture project creation event identity is unavailable")
    return matching[0]


def _availability_diagnostics(kubeconfig: Path, namespace: str) -> dict[str, object]:
    """Return bounded status/events only; never collect container logs or state."""
    diagnostics: dict[str, object] = {}
    try:
        pod = _pod(kubeconfig, namespace)
        diagnostics["pod_status"] = pod.get("status", {})
        pod_name = str(pod.get("metadata", {}).get("name", ""))
        if pod_name:
            events = json.loads(
                _run(
                    _kubectl(kubeconfig, namespace)
                    + [
                        "get",
                        "events",
                        "--field-selector",
                        f"involvedObject.kind=Pod,involvedObject.name={pod_name}",
                        "-o",
                        "json",
                    ],
                    timeout=20,
                )
            )
            diagnostics["pod_events"] = [
                {
                    key: item.get(key)
                    for key in ("type", "reason", "message", "count", "lastTimestamp")
                }
                for item in events.get("items", [])[-10:]
            ]
    except (FixtureError, ValueError, KeyError) as error:
        diagnostics["pod_diagnostic"] = str(error)
    try:
        deployment = json.loads(
            _run(_kubectl(kubeconfig, namespace) + ["get", "deployment", "control", "-o", "json"], timeout=20)
        )
        diagnostics["deployment_status"] = deployment.get("status", {})
    except (FixtureError, ValueError) as error:
        diagnostics["deployment_diagnostic"] = str(error)
    return diagnostics


def _reported_image_never_pull(diagnostics: dict[str, object]) -> bool:
    events = diagnostics.get("pod_events", [])
    return isinstance(events, list) and any(
        isinstance(event, dict) and event.get("reason") == "ErrImageNeverPull"
        for event in events
    )


def _cleanup(kubeconfig: Path, namespace: str, nonce: str, expected_uid: str) -> str:
    try:
        namespace_payload = json.loads(_run(_kubectl(kubeconfig) + ["get", "namespace", namespace, "-o", "json"], timeout=20))
        metadata = namespace_payload.get("metadata", {})
        labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
        actual_uid = metadata.get("uid") if isinstance(metadata, dict) else None
        if labels.get(LABEL) != nonce or actual_uid != expected_uid:
            return "retained_cleanup_unverified"
        _run(_kubectl(kubeconfig) + ["delete", "namespace", namespace, "--wait=false"], timeout=20)
        _run(
            _kubectl(kubeconfig)
            + ["wait", "--for=delete", f"namespace/{namespace}", "--timeout=90s"],
            timeout=100,
        )
    except FixtureError:
        return "retained_cleanup_failed"
    return "cleaned"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--context", choices=[CONTEXT], default=CONTEXT)
    parser.add_argument("--namespace")
    parser.add_argument("--retain", action="store_true", help="retain fixture namespace after observation")
    arguments = parser.parse_args()
    if not arguments.run:
        print(json.dumps({"fixture_verdict": "skipped", "state": "not_started", "message": "--run required"}))
        return 1
    if arguments.kubeconfig is None or arguments.namespace is None:
        print(json.dumps({"fixture_verdict": "failed", "state": "not_started", "message": "--kubeconfig and --namespace are required"}))
        return 1
    if not arguments.namespace.startswith("tessera-control-e2e-"):
        print(json.dumps({"fixture_verdict": "failed", "state": "not_started", "message": "namespace must start tessera-control-e2e-"}))
        return 1
    try:
        kubeconfig = arguments.kubeconfig.resolve(strict=True)
        existing = _run(
            _kubectl(kubeconfig) + ["get", "namespace", arguments.namespace, "--ignore-not-found", "-o", "name"],
            timeout=20,
        )
        if existing.strip():
            print(json.dumps({"fixture_verdict": "failed", "state": "not_started", "message": "fixture namespace already exists"}))
            return 1
    except (FixtureError, OSError, ValueError) as error:
        print(json.dumps({"fixture_verdict": "failed", "state": "not_started", "message": str(error)}))
        return 1

    nonce = uuid.uuid4().hex[:12]
    cleanup = "not_created"
    evidence: dict[str, object] = {"namespace": arguments.namespace, "run": nonce}
    try:
        _require_containerd_image_store()
        _require_docker_desktop_kind_cluster()
        image = _load_image(Path(__file__).resolve().parents[1], nonce)
        evidence["image"] = image
        cleanup = "retained_after_apply_attempt"
        _run(_kubectl(kubeconfig) + ["apply", "-f", "-"], input_bytes=_manifest(arguments.namespace, nonce, image))
        namespace_uid = _namespace_uid(kubeconfig, arguments.namespace, nonce)
        evidence["namespace_uid"] = namespace_uid
        try:
            _run(
                _kubectl(kubeconfig, arguments.namespace)
                + ["wait", "--for=condition=available", "deployment/control", "--timeout=90s"],
                timeout=100,
            )
        except FixtureError as error:
            evidence.update(_availability_diagnostics(kubeconfig, arguments.namespace))
            if _reported_image_never_pull(evidence):
                raise FixtureError(
                    "Docker Desktop kind could not see the explicitly loaded containerd fixture image; "
                    "the fixture namespace is retained. Confirm Docker Desktop's containerd image store is enabled "
                    "and inspect the reported Pod events; do not retry with a registry push or system-container exec."
                ) from error
            raise
        before_pod = _pod(kubeconfig, arguments.namespace)
        port = 18765
        forward = _start_port_forward(kubeconfig, arguments.namespace, port)
        try:
            before = _wait_ready(port)
            evidence["health_before"] = {"live": 200, "ready": 200}
            token = _owner_authorization(
                _run(
                    _kubectl(kubeconfig, arguments.namespace)
                    + ["exec", "deploy/control", "--", "cat", "/state/control/owner.header"]
                ).strip()
            )
            status, project = _request(
                port,
                "/projects",
                {
                    "Host": FIXTURE_TRUSTED_HOST,
                    "Authorization": token,
                    "Idempotency-Key": "restart-fixture",
                },
                method="POST",
                payload={"name": "Kubernetes control restart fixture"},
            )
            if status != 201:
                raise FixtureError("fixture project creation was not admitted")
            request_headers = {
                "Host": FIXTURE_TRUSTED_HOST,
                "Authorization": token,
                "Idempotency-Key": "restart-fixture",
            }
            event_before = _project_created_event(port, project, request_headers)
            _run(_kubectl(kubeconfig, arguments.namespace) + ["delete", "pod", str(before_pod["metadata"]["name"]), "--wait=false"])
            _run(_kubectl(kubeconfig, arguments.namespace) + ["wait", "--for=condition=available", "deployment/control", "--timeout=90s"], timeout=100)
            after_pod = _pod(kubeconfig, arguments.namespace)
            _stop_port_forward(forward)
            forward = _start_port_forward(kubeconfig, arguments.namespace, port)
            after = _wait_ready(port)
            evidence["health_after"] = {"live": 200, "ready": 200}
            status, projects = _request(
                port, "/projects", {"Host": FIXTURE_TRUSTED_HOST, "Authorization": token}
            )
            if status != 200 or project not in projects.get("projects", []):
                raise FixtureError("fixture project was not retained after restart")
            replay_status, replay = _request(
                port,
                "/projects",
                request_headers,
                method="POST",
                payload={"name": "Kubernetes control restart fixture"},
            )
            event_after = _project_created_event(port, project, request_headers)
            if replay_status != 201 or replay != project or event_after != event_before:
                raise FixtureError("fixture idempotent command or event identity changed after restart")
            if before.get("backend_identity") != after.get("backend_identity"):
                raise FixtureError("persisted backend identity changed after Pod restart")
            if before_pod["metadata"]["uid"] == after_pod["metadata"]["uid"]:
                raise FixtureError("fixture Pod identity did not change")
            evidence.update(
                {
                    "image": image,
                    "pvc": "state",
                    "service": "control",
                    "pod_uid_before": before_pod["metadata"]["uid"],
                    "pod_uid_after": after_pod["metadata"]["uid"],
                    "backend_identity": before["backend_identity"],
                    "backend_identity_before": before["backend_identity"],
                    "backend_identity_after": after["backend_identity"],
                    "project_id": project["id"],
                    "project_created_event_cursor": event_before["cursor"],
                }
            )
        finally:
            _stop_port_forward(forward)
        cleanup = (
            "retained_by_request"
            if arguments.retain
            else _cleanup(kubeconfig, arguments.namespace, nonce, namespace_uid)
        )
        print(json.dumps({"fixture_verdict": "passed", "state": "succeeded", "cleanup": cleanup, **evidence}, sort_keys=True))
        return 0
    except (FixtureError, OSError, ValueError, KeyError) as error:
        print(json.dumps({"fixture_verdict": "failed", "state": "failed", "cleanup": cleanup, "message": str(error), **evidence}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
