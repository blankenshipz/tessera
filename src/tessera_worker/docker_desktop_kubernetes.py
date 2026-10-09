# ruff: noqa: E501
"""Narrow real Kubernetes conformance adapter for one Docker Desktop Job.

This is deliberately not a control-plane dispatcher.  It accepts only an
explicit ``docker-desktop`` kubeconfig/context/server target and creates a
unique namespace owned by the caller.  It never reads a current context, prunes,
or deletes a namespace itself. The separate authenticated dispatch fixture uses
the normal control outbox and this module's explicitly bound Kubernetes transport.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import re
import shlex
import socket
import subprocess
import tarfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .kubernetes_contracts import (
    KubernetesClusterConfig,
    KubernetesJobBinding,
    KubernetesJobJournal,
    canonical_json,
    parse_pod,
)

PROFILE = "development-kubernetes-conformance-v1"
CONTEXT = "docker-desktop"
RESULT_PATH = "/outputs/result.txt"
_OUTPUT_PVC_LABELS = {
    "app.kubernetes.io/managed-by": "tessera",
    "tessera.io/output-role": "dedicated-pilot-output",
}
RESULT_BYTES = b"hello tessera kubernetes e2e\n"
_MAX_OUTPUT = 64 * 1024
_COMMAND_TIMEOUT = 120


class LiveKubernetesError(RuntimeError):
    """A live conformance action cannot safely continue."""


class _KubectlNotFound(LiveKubernetesError):
    pass


@dataclass(frozen=True)
class ExactImage:
    reference: str
    manifest_digest: str
    config_digest: str
    archive_sha256: str


@dataclass(frozen=True)
class DockerDesktopTarget:
    kubeconfig: Path
    context: str
    server: str
    ca_sha256: str
    kubeconfig_sha256: str


@dataclass(frozen=True)
class LiveKubernetesEvidence:
    namespace: str
    namespace_uid: str
    pvc: str
    pvc_uid: str
    pvc_spec_sha256: str
    job: str
    job_uid: str
    pod_uids: tuple[str, ...]
    collector: str
    collector_uid: str
    collector_image_id: str
    image: ExactImage
    output_sha256: str
    output_bytes: bytes
    job_state: str


def target_from_kubeconfig(kubeconfig: Path) -> DockerDesktopTarget:
    """Discover server/CA only through the caller's explicit kubeconfig/context."""
    if not Path(kubeconfig).is_absolute():
        raise LiveKubernetesError("Docker Desktop kubeconfig must be an explicit absolute path")
    path = Path(kubeconfig).resolve(strict=True)
    raw = _run(
        (
            "kubectl",
            "--kubeconfig",
            str(path),
            "--context",
            CONTEXT,
            "config",
            "view",
            "--raw",
            "--minify",
            "-o",
            "json",
        )
    )
    try:
        config = json.loads(raw.stdout)
        contexts = config["contexts"]
        clusters = config["clusters"]
        if len(contexts) != 1 or contexts[0]["name"] != CONTEXT:
            raise ValueError("selected context does not match docker-desktop")
        context = contexts[0]["context"]
        cluster_name = context["cluster"]
        cluster = next(item["cluster"] for item in clusters if item["name"] == cluster_name)
        server = cluster["server"]
        ca = base64.b64decode(cluster["certificate-authority-data"], validate=True)
    except (
        IndexError,
        KeyError,
        StopIteration,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        raise LiveKubernetesError(
            "explicit docker-desktop kubeconfig identity is malformed"
        ) from error
    if not isinstance(server, str) or not server.startswith("https://") or not ca:
        raise LiveKubernetesError("explicit docker-desktop server or CA is unavailable")
    return DockerDesktopTarget(
        path,
        CONTEXT,
        server,
        hashlib.sha256(ca).hexdigest(),
        _sha256_file(path),
    )


def build_and_load_exact_image(
    repository_root: Path, nonce: str, *, prebuilt_image_id: str | None = None
) -> ExactImage:
    """Build amd64 OCI bytes, verify their manifest, then import that archive only.

    Docker Desktop Kubernetes is Kind-based here.  The node container is named
    explicitly and receives the exact OCI archive via ``ctr``; no registry,
    pull, ambient image, provider, or credential is involved.
    """
    _require_docker_desktop_node()
    tag = f"tessera-e2e-hello:{nonce}"
    node_repository = "docker.io/library/tessera-e2e-hello"
    node_tag = f"{node_repository}:{nonce}"
    with TemporaryDirectory(prefix="tessera-kubernetes-image-") as temporary:
        archive = Path(temporary) / "image.oci.tar"
        docker_archive = Path(temporary) / "image.docker.tar"
        if prebuilt_image_id is None:
            _run(
                (
                    "docker",
                    "build",
                    "--platform",
                    "linux/amd64",
                    "--build-arg",
                    f"TESSERA_E2E_NONCE={nonce}",
                    "--tag",
                    tag,
                    "--file",
                    str(repository_root / "examples" / "kubernetes-hello" / "Dockerfile"),
                    str(repository_root / "examples" / "kubernetes-hello"),
                )
            )
        else:
            inspected = json.loads(_run(("docker", "image", "inspect", tag)).stdout)
            if (
                len(inspected) != 1
                or inspected[0].get("Id") != prebuilt_image_id
                or inspected[0].get("Os") != "linux"
                or inspected[0].get("Architecture") != "amd64"
                or inspected[0].get("Config", {}).get("Labels", {}).get("io.tessera.e2e-nonce")
                != nonce
            ):
                raise LiveKubernetesError("prebuilt workload image identity changed before import")
        _run(("docker", "save", "--output", str(docker_archive), tag))
        _docker_archive_to_oci(docker_archive, archive, tag)
        image = _verify_oci_archive(archive, tag, node_repository)
        # Docker Desktop's containerd image store can report a local Docker
        # image ID that differs from the config blob emitted by ``docker save``.
        # The archive's config digest is verified above and imported below, so
        # host-cache presentation must not reject that exact archive.
        _run(
            (
                "docker",
                "exec",
                "-i",
                "desktop-control-plane",
                "ctr",
                "--namespace",
                "k8s.io",
                "images",
                "import",
                "--base-name",
                tag,
                "-",
            ),
            input=archive.read_bytes(),
        )
        refs = _run(
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
        source = next(
            (item for item in refs if item == tag or item.startswith(f"{tag}@")),
            None,
        )
        if source is None:
            raise LiveKubernetesError("Docker Desktop node did not retain the imported image tag")
        _run(
            (
                "docker",
                "exec",
                "desktop-control-plane",
                "ctr",
                "--namespace",
                "k8s.io",
                "images",
                "tag",
                source,
                node_tag,
            )
        )
        _run(
            (
                "docker",
                "exec",
                "desktop-control-plane",
                "ctr",
                "--namespace",
                "k8s.io",
                "images",
                "tag",
                source,
                image.reference,
            )
        )
        verified_refs = _run(
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
        if node_tag not in verified_refs or image.reference not in verified_refs:
            raise LiveKubernetesError(
                "Docker Desktop node did not retain the canonical tag and exact image reference"
            )
        _run(("docker", "exec", "desktop-control-plane", "crictl", "inspecti", image.reference))
        return image


def _docker_archive_to_oci(source_path: Path, destination: Path, tag: str) -> None:
    """Convert one locally built Docker-save image to a verified OCI layout.

    Docker Desktop's Docker build driver intentionally has no OCI exporter. This
    conversion avoids a registry/pull and preserves the local image config and
    uncompressed filesystem layers byte-for-byte for containerd import.
    """
    try:
        with tarfile.open(source_path) as source:
            entries = json.load(source.extractfile("manifest.json"))
            if not isinstance(entries, list) or len(entries) != 1:
                raise ValueError("Docker archive must contain exactly one image")
            entry = entries[0]
            config_name = entry["Config"]
            layer_names = entry["Layers"]
            if not isinstance(config_name, str) or not isinstance(layer_names, list):
                raise ValueError("Docker archive manifest is malformed")
            config = source.extractfile(config_name).read()
            layers = [
                (name, _uncompressed_layer(source.extractfile(name).read())) for name in layer_names
            ]
    except (
        KeyError,
        AttributeError,
        OSError,
        tarfile.TarError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        raise LiveKubernetesError(
            "locally built Docker image cannot be converted to OCI"
        ) from error
    config_json = json.loads(config)
    if config_json.get("architecture") != "amd64" or config_json.get("os") != "linux":
        raise LiveKubernetesError("locally built Docker image is not linux/amd64")
    config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
    layer_descriptors = []
    blobs: list[tuple[str, bytes]] = [(config_digest, config)]
    for name, layer in layers:
        if not isinstance(name, str):
            raise LiveKubernetesError("locally built Docker layer name is malformed")
        digest = "sha256:" + hashlib.sha256(layer).hexdigest()
        blobs.append((digest, layer))
        layer_descriptors.append(
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar",
                "digest": digest,
                "size": len(layer),
            }
        )
    manifest = canonical_json(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": config_digest,
                "size": len(config),
            },
            "layers": layer_descriptors,
        }
    )
    manifest_digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    index = canonical_json(
        {
            "schemaVersion": 2,
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": manifest_digest,
                    "size": len(manifest),
                    "platform": {"architecture": "amd64", "os": "linux"},
                    "annotations": {"org.opencontainers.image.ref.name": tag},
                }
            ],
        }
    )
    try:
        with tarfile.open(destination, "w") as destination_archive:
            _tar_bytes(destination_archive, "oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
            _tar_bytes(destination_archive, "index.json", index)
            for digest, blob in [*blobs, (manifest_digest, manifest)]:
                _tar_bytes(destination_archive, f"blobs/sha256/{digest[7:]}", blob)
    except (OSError, tarfile.TarError) as error:
        raise LiveKubernetesError("locally built OCI archive could not be written") from error


def _uncompressed_layer(value: bytes) -> bytes:
    """Return the OCI layer's required uncompressed tar bytes.

    Docker Desktop's containerd image store may serialize ``docker save`` layer
    blobs as gzip streams rather than legacy ``layer.tar`` files. OCI's
    ``application/vnd.oci.image.layer.v1.tar`` descriptor is explicitly an
    uncompressed tar, and its digest must therefore be over the decoded bytes.
    """
    try:
        decoded = gzip.decompress(value) if value.startswith(b"\x1f\x8b") else value
        with tarfile.open(fileobj=io.BytesIO(decoded), mode="r:"):
            pass
    except (EOFError, OSError, tarfile.TarError) as error:
        raise LiveKubernetesError("Docker image layer is not a valid uncompressed tar") from error
    return decoded


def _tar_bytes(archive: tarfile.TarFile, name: str, value: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(value)
    archive.addfile(info, io.BytesIO(value))


def _verify_oci_archive(archive: Path, tag: str, repository: str | None = None) -> ExactImage:
    """Validate the OCI index, manifest/config digests, and linux/amd64 config."""
    archive_bytes = archive.read_bytes()
    try:
        with tarfile.open(archive) as source:
            index = json.load(source.extractfile("index.json"))
            descriptors = index["manifests"]
            if not isinstance(descriptors, list) or len(descriptors) != 1:
                raise ValueError("OCI index must contain exactly one manifest")
            descriptor = descriptors[0]
            if descriptor.get("annotations", {}).get("org.opencontainers.image.ref.name") != tag:
                raise ValueError("OCI archive reference annotation conflicts")
            manifest_digest = descriptor["digest"]
            manifest_bytes = source.extractfile(
                f"blobs/sha256/{manifest_digest.split(':', 1)[1]}"
            ).read()
            if "sha256:" + hashlib.sha256(manifest_bytes).hexdigest() != manifest_digest:
                raise ValueError("OCI manifest digest conflicts")
            manifest = json.loads(manifest_bytes)
            config_digest = manifest["config"]["digest"]
            config_bytes = source.extractfile(
                f"blobs/sha256/{config_digest.split(':', 1)[1]}"
            ).read()
            if "sha256:" + hashlib.sha256(config_bytes).hexdigest() != config_digest:
                raise ValueError("OCI config digest conflicts")
            config = json.loads(config_bytes)
    except (
        KeyError,
        AttributeError,
        OSError,
        tarfile.TarError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        raise LiveKubernetesError("locally built OCI archive cannot be digest-verified") from error
    if config.get("architecture") != "amd64" or config.get("os") != "linux":
        raise LiveKubernetesError("locally built OCI image is not linux/amd64")
    if not isinstance(manifest_digest, str) or not isinstance(config_digest, str):
        raise LiveKubernetesError("locally built OCI image digest is malformed")
    return ExactImage(
        f"{repository or tag}@{manifest_digest}",
        manifest_digest,
        config_digest,
        hashlib.sha256(archive_bytes).hexdigest(),
    )


class DockerDesktopKubernetesAdapter:
    """Create and reconcile one retained, isolated Job/PVC conformance namespace."""

    def __init__(self, target: DockerDesktopTarget, journal_path: Path) -> None:
        if target.context != CONTEXT:
            raise LiveKubernetesError("only the explicit docker-desktop context is permitted")
        self.target = target
        self.journal_path = Path(journal_path)

    def run(
        self, image: ExactImage, nonce: str, namespace: str | None = None
    ) -> LiveKubernetesEvidence:
        """Run one deterministic Job and a read-only post-exit collector; retain all objects."""
        if namespace is None:
            namespace = f"tessera-e2e-{nonce}"
            self._create(
                {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}},
                None,
            )
        elif not re.fullmatch(r"[a-z]([-a-z0-9]{0,61}[a-z0-9])?", namespace):
            raise LiveKubernetesError("explicit conformance namespace is malformed")
        namespace_resource = self._get("namespace", namespace, None)
        namespace_uid = _uid(namespace_resource, "Namespace", namespace, None)
        self._create(_deny_all_policy(namespace), namespace)
        policy = self._get("networkpolicy", "deny-all", namespace)
        policy_spec = _mapping(policy.get("spec"), "network policy spec")
        expected_policy = _deny_all_policy(namespace)["spec"]
        if canonical_json(policy_spec) != canonical_json(expected_policy):
            raise LiveKubernetesError(
                "created NetworkPolicy does not enforce the fixed deny-all policy"
            )
        self._create(_pvc(namespace), namespace)
        pvc = self._get("pvc", "outputs", namespace)
        pvc_uid = _uid(pvc, "PersistentVolumeClaim", "outputs", namespace)
        pvc_spec_sha256 = _pvc_spec_sha256(pvc)
        cluster = KubernetesClusterConfig(
            kubeconfig=str(self.target.kubeconfig),
            kubeconfig_sha256=self.target.kubeconfig_sha256,
            context=self.target.context,
            profile=PROFILE,
            server=self.target.server,
            server_sha256=hashlib.sha256(self.target.server.encode()).hexdigest(),
            ca_sha256=self.target.ca_sha256,
            namespace=namespace,
            cluster_uid=_uid(
                self._get("namespace", "kube-system", None), "Namespace", "kube-system", None
            ),
            namespace_uid=namespace_uid,
        )
        operation_id = hashlib.sha256(f"tessera-e2e:{nonce}".encode()).hexdigest()[:32]
        binding = KubernetesJobBinding(
            operation_id=operation_id,
            attempt_id=f"e2e-attempt-{nonce}",
            lease_id=f"e2e-lease-{nonce}",
            epoch=1,
            package_sha256=hashlib.sha256(RESULT_BYTES).hexdigest(),
            image=image.reference,
            command=("/hello", "--write", RESULT_PATH),
            active_deadline_seconds=60,
            network_mode="cluster",
            output_pvc="outputs",
            output_mount_path="/outputs",
            output_pvc_uid=pvc_uid,
            output_pvc_spec_sha256=pvc_spec_sha256,
        )
        journal = KubernetesJobJournal(self.journal_path)
        job = journal.reconcile_create(
            binding,
            cluster,
            lambda name: self._maybe_get("job", name, namespace),
            lambda manifest: self._create(manifest, namespace),
        )
        self._kubectl(
            "wait",
            "--for=condition=complete",
            f"job/{binding.job_name}",
            "--timeout=75s",
            namespace=namespace,
            timeout=90,
        )
        observed = self._get("job", binding.job_name, namespace)
        job = journal.reconcile_observation(binding, cluster, observed)
        if job.state != "succeeded":
            raise LiveKubernetesError("workload Job did not reach succeeded")
        _verify_bound_pvc(
            self._get("pvc", "outputs", namespace), namespace, pvc_uid, pvc_spec_sha256
        )
        pods = self._owned_pods(binding, cluster, job.job_uid)
        if not pods or any(pod.state != "succeeded" for pod in pods):
            raise LiveKubernetesError("workload owned Pods did not reach succeeded")
        self._require_collector_authorization(namespace)
        collector = f"tessera-collector-{operation_id}"
        self._create(_collector_manifest(namespace, collector, image.reference), namespace)
        self._kubectl(
            "wait",
            "--for=jsonpath={.status.phase}=Succeeded",
            f"pod/{collector}",
            "--timeout=60s",
            namespace=namespace,
            timeout=70,
        )
        collector_resource = self._get("pod", collector, namespace)
        collector_uid, image_id = _verify_collector(
            collector_resource, namespace, collector, image, pvc_name="outputs"
        )
        _verify_bound_pvc(
            self._get("pvc", "outputs", namespace), namespace, pvc_uid, pvc_spec_sha256
        )
        output = self._kubectl(
            "logs", f"pod/{collector}", "--limit-bytes=1024", namespace=namespace
        ).stdout.encode()
        if output != RESULT_BYTES:
            raise LiveKubernetesError(
                "post-exit collector bytes conflict with the exact expected result"
            )
        return LiveKubernetesEvidence(
            namespace,
            namespace_uid,
            "outputs",
            pvc_uid,
            pvc_spec_sha256,
            binding.job_name,
            job.job_uid,
            tuple(pod.pod_uid for pod in pods),
            collector,
            collector_uid,
            image_id,
            image,
            hashlib.sha256(output).hexdigest(),
            output,
            job.state,
        )

    def cancel(self, binding: KubernetesJobBinding, cluster: KubernetesClusterConfig) -> object:
        """Use the durable cancel fence; this never deletes a namespace or selector set."""
        journal = KubernetesJobJournal(self.journal_path)

        def delete(name: str, uid: str) -> None:
            self._delete_job_uid_precondition(name, uid, cluster.namespace)

        return journal.request_cancel(
            binding,
            cluster,
            lambda name: self._maybe_get("job", name, cluster.namespace),
            delete,
            lambda job_uid: self._owned_pod_resources(job_uid, cluster.namespace),
        )

    def _delete_job_uid_precondition(self, name: str, uid: str, namespace: str) -> None:
        """Delete one Job through the API's UID precondition, never check-then-delete.

        ``kubectl delete`` does not expose DeleteOptions.preconditions. A short
        loopback proxy retains kubectl's explicit kubeconfig/context auth while
        allowing the API request body to carry the immutable UID. A replacement
        between inspection and this request therefore receives HTTP 409 rather
        than being deleted by name.
        """
        self._delete_uid_precondition(
            f"/apis/batch/v1/namespaces/{quote(namespace)}/jobs/{quote(name)}", uid
        )

    def delete_namespace_uid_precondition(self, name: str, uid: str) -> None:
        """Remove only the exact owned fixture namespace, never a replacement."""
        if not re.fullmatch(r"tessera-e2e-[a-z0-9-]{1,48}", name):
            raise LiveKubernetesError("namespace is not an authenticated E2E fixture")
        self._delete_uid_precondition(f"/api/v1/namespaces/{quote(name)}", uid)

    def _delete_uid_precondition(self, path: str, uid: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", uid):
            raise LiveKubernetesError("Kubernetes UID precondition is malformed")
        self._require_unchanged_kubeconfig()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        command = (
            "kubectl",
            "--kubeconfig",
            str(self.target.kubeconfig),
            "--context",
            self.target.context,
            "proxy",
            "--address=127.0.0.1",
            f"--port={port}",
            "--accept-hosts=^127\\.0\\.0\\.1$",
            "--disable-filter=true",
        )
        proxy = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
        )
        url = f"http://127.0.0.1:{port}{path}"
        payload = canonical_json(
            {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": uid},
                "propagationPolicy": "Background",
            }
        )
        deadline = time.monotonic() + 15
        try:
            while True:
                try:
                    with urlopen(  # noqa: S310 -- fixed loopback proxy URL
                        Request(
                            url,
                            data=payload,
                            method="DELETE",
                            headers={"Content-Type": "application/json"},
                        ),
                        timeout=3,
                    ):
                        return
                except HTTPError as error:
                    if error.code == 409:
                        raise LiveKubernetesError(
                            "Kubernetes UID precondition rejected deletion"
                        ) from error
                    raise LiveKubernetesError(
                        "Kubernetes UID-precondition delete was rejected"
                    ) from error
                except URLError as error:
                    if time.monotonic() >= deadline or proxy.poll() is not None:
                        raise LiveKubernetesError(
                            "Kubernetes UID-precondition proxy did not become available"
                        ) from error
                    time.sleep(0.1)
        finally:
            proxy.terminate()
            try:
                proxy.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proxy.kill()
                proxy.wait(timeout=3)

    def _owned_pods(
        self, binding: KubernetesJobBinding, cluster: KubernetesClusterConfig, job_uid: str
    ):
        return [
            parse_pod(item, binding, cluster, job_uid)
            for item in self._owned_pod_resources(job_uid, cluster.namespace)
        ]

    def _owned_pod_resources(self, job_uid: str, namespace: str) -> list[Mapping[str, object]]:
        listing = self._kubectl(
            "get",
            "pods",
            "--selector",
            f"batch.kubernetes.io/controller-uid={job_uid}",
            "-o",
            "json",
            namespace=namespace,
        )
        items = _mapping(json.loads(listing.stdout), "Pod list").get("items")
        if not isinstance(items, list):
            raise LiveKubernetesError("owned Pod list is malformed")
        return [_mapping(item, "owned Pod") for item in items]

    def _require_collector_authorization(self, namespace: str) -> None:
        for resource, verb in (("pods", "create"), ("pods", "get"), ("pods/log", "get")):
            answer = self._kubectl("auth", "can-i", verb, resource, namespace=namespace).stdout
            if answer != "yes\n":
                raise LiveKubernetesError(
                    "collector is not explicitly authorized by Kubernetes RBAC"
                )

    def dataset_client(self, cluster, output):
        live = self

        class DatasetClient:
            def authorize(self, verb, admission=False):
                live._require_unchanged_kubeconfig()
                if (
                    live._get("namespace", "kube-system", None).get("metadata", {}).get("uid")
                    != cluster.cluster_uid
                    or live._get("namespace", cluster.namespace, None)
                    .get("metadata", {})
                    .get("uid")
                    != cluster.namespace_uid
                ):
                    raise LiveKubernetesError("Dataset cluster/namespace UID changed")
                pvc = live._get("pvc", output.pvc_name, cluster.namespace)
                if (
                    _uid(pvc, "PersistentVolumeClaim", output.pvc_name, cluster.namespace)
                    != output.pvc_uid
                    or hashlib.sha256(canonical_json(pvc.get("spec"))).hexdigest()
                    != output.pvc_spec_sha256
                    or pvc.get("status", {}).get("phase") != "Bound"
                ):
                    raise LiveKubernetesError("Dataset output PVC identity/spec/binding changed")
                evidence = cluster.network_none_evidence
                if evidence is None or not evidence.is_bound_to(
                    cluster.cluster_uid, cluster.namespace_uid
                ):
                    raise LiveKubernetesError("Dataset network evidence is missing")
                if admission and not evidence.is_fresh_for(
                    cluster.cluster_uid, cluster.namespace_uid
                ):
                    raise LiveKubernetesError("Dataset network admission evidence is stale")
                policy = live._get("networkpolicy", evidence.policy_name, cluster.namespace)
                if (
                    policy.get("metadata", {}).get("uid") != evidence.policy_uid
                    or hashlib.sha256(canonical_json(policy.get("spec"))).hexdigest()
                    != evidence.policy_spec_sha256
                ):
                    raise LiveKubernetesError("Dataset network policy identity changed")
                if (
                    live._kubectl(
                        "auth", "can-i", verb, "configmaps", namespace=cluster.namespace
                    ).stdout
                    != "yes\n"
                ):
                    raise LiveKubernetesError("Scoped ConfigMap RBAC denied")

            def dataset_read(self, name):
                self.authorize("get")
                return live._dataset_resource(name, cluster.namespace)

            def dataset_create(self, manifest):
                if (
                    manifest.get("kind") != "ConfigMap"
                    or manifest.get("immutable") is not True
                    or manifest.get("metadata", {}).get("namespace") != cluster.namespace
                ):
                    raise LiveKubernetesError("Dataset resource outside bound namespace/type")
                self.authorize("create", admission=True)
                value = live._dataset_resource(
                    manifest["metadata"]["name"], cluster.namespace, manifest=manifest
                )
                self.authorize("get", admission=True)
                return value

            def dataset_delete(self, name, uid):
                self.authorize("delete")
                live._delete_uid_precondition(
                    f"/api/v1/namespaces/{quote(cluster.namespace)}/configmaps/{quote(name)}", uid
                )

        return DatasetClient()

    def _dataset_resource(self, name, namespace, *, manifest=None):
        """Only immutable ConfigMap create/read may use the larger JSON bound.

        Complete outputs remain in a unique private receipt, never in exception
        messages or shared logs. Generic conformance commands retain 64KiB.
        """
        self._require_unchanged_kubeconfig()
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?", name):
            raise LiveKubernetesError("Dataset ConfigMap name is malformed")
        data = None if manifest is None else canonical_json(manifest).decode()
        if data is not None and len(data.encode()) > 1024 * 1024:
            raise LiveKubernetesError("Dataset ConfigMap request exceeds 1MiB")
        args = (
            ("get", "configmap", name, "-o", "json")
            if manifest is None
            else ("create", "-f", "-", "-o", "json")
        )
        argv = (
            "kubectl",
            "--kubeconfig",
            str(self.target.kubeconfig),
            "--context",
            self.target.context,
            "--server",
            self.target.server,
            "--request-timeout=15s",
            "--namespace",
            namespace,
            *args,
        )
        receipt = Path(mkdtemp(prefix="dataset-command-", dir=self.journal_path.parent))
        receipt.chmod(0o700)
        for filename in ("stdout", "stderr", "result.json"):
            path = receipt / filename
            path.touch(mode=0o600)
        metadata = {
            "argv": argv,
            "action": args[0],
            "resource": "configmap",
            "name": name,
            "namespace": namespace,
            "stdout_limit": 1024 * 1024,
            "stderr_limit": _MAX_OUTPUT,
            "exit": None,
        }
        try:
            with (receipt / "stdout").open("w") as stdout, (receipt / "stderr").open("w") as stderr:
                completed = subprocess.run(
                    argv,
                    input=data,
                    text=True,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=30,
                    check=False,
                )
            metadata["exit"] = completed.returncode
        except (subprocess.TimeoutExpired, OSError) as error:
            metadata["failure"] = type(error).__name__
            raise LiveKubernetesError(
                f"Dataset command failed; private receipt: {receipt}"
            ) from None
        finally:
            (receipt / "result.json").write_text(json.dumps(metadata, sort_keys=True))
        if (receipt / "stdout").stat().st_size > 1024 * 1024 or (
            receipt / "stderr"
        ).stat().st_size > _MAX_OUTPUT:
            raise LiveKubernetesError(
                f"Dataset command output exceeds bound; private receipt: {receipt}"
            )
        stderr = (receipt / "stderr").read_text(errors="replace")
        if completed.returncode:
            if manifest is None and "Error from server (NotFound)" in stderr:
                return None
            raise LiveKubernetesError(f"Dataset command rejected; private receipt: {receipt}")
        try:
            value = _mapping(json.loads((receipt / "stdout").read_text()), "dataset response")
            _uid(value, "ConfigMap", name, namespace)
            if value.get("immutable") is not True or not isinstance(
                value.get("binaryData"), Mapping
            ):
                raise LiveKubernetesError("Dataset response is not an immutable binary ConfigMap")
        except (ValueError, LiveKubernetesError):
            raise LiveKubernetesError(
                f"Dataset command returned invalid resource; private receipt: {receipt}"
            ) from None
        return value

    def _create(
        self, manifest: Mapping[str, object], namespace: str | None
    ) -> Mapping[str, object]:
        completed = self._kubectl(
            "create", "-f", "-", "-o", "json", namespace=namespace, input=json.dumps(manifest)
        )
        return _mapping(json.loads(completed.stdout), "created resource")

    def _get(self, kind: str, name: str, namespace: str | None) -> Mapping[str, object]:
        try:
            return self._maybe_get(kind, name, namespace, required=True)
        except _KubectlNotFound as error:
            raise LiveKubernetesError(f"required {kind}/{name} was not found") from error

    def _maybe_get(
        self, kind: str, name: str, namespace: str | None, required: bool = False
    ) -> Mapping[str, object] | None:
        try:
            completed = self._kubectl("get", kind, name, "-o", "json", namespace=namespace)
        except _KubectlNotFound:
            if required:
                raise
            return None
        return _mapping(json.loads(completed.stdout), f"{kind} response")

    def _kubectl(
        self, *args: str, namespace: str | None, timeout: int = 30, input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        self._require_unchanged_kubeconfig()
        argv = [
            "kubectl",
            "--kubeconfig",
            str(self.target.kubeconfig),
            "--context",
            self.target.context,
            "--server",
            self.target.server,
            "--request-timeout=15s",
        ]
        if namespace is not None:
            argv.extend(("--namespace", namespace))
        return _run(tuple(argv) + args, timeout=timeout, input=input)

    def _require_unchanged_kubeconfig(self) -> None:
        if _sha256_file(self.target.kubeconfig) != self.target.kubeconfig_sha256:
            raise LiveKubernetesError("explicit Docker Desktop kubeconfig changed after attachment")


def _deny_all_policy(namespace: str) -> dict[str, object]:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "deny-all", "namespace": namespace},
        "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/managed-by": "tessera"}},
            "policyTypes": ["Ingress", "Egress"],
        },
    }


def _pvc(namespace: str) -> dict[str, object]:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": "outputs", "namespace": namespace, "labels": _OUTPUT_PVC_LABELS},
        "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "64Mi"}}},
    }


def _collector_manifest(namespace: str, name: str, image: str) -> dict[str, object]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/managed-by": "tessera"},
        },
        "spec": {
            "activeDeadlineSeconds": 60,
            "automountServiceAccountToken": False,
            "containers": [
                {
                    "name": "collector",
                    "image": image,
                    "imagePullPolicy": "Never",
                    "command": ["/hello", "--read", RESULT_PATH],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "privileged": False,
                        "readOnlyRootFilesystem": True,
                        "runAsNonRoot": True,
                        "runAsUser": 65532,
                    },
                    "volumeMounts": [
                        {"name": "outputs", "mountPath": "/outputs", "readOnly": True}
                    ],
                }
            ],
            "enableServiceLinks": False,
            "hostIPC": False,
            "hostNetwork": False,
            "hostPID": False,
            "restartPolicy": "Never",
            "securityContext": {
                "runAsGroup": 65532,
                "runAsNonRoot": True,
                "runAsUser": 65532,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "volumes": [
                {
                    "name": "outputs",
                    "persistentVolumeClaim": {"claimName": "outputs", "readOnly": True},
                }
            ],
        },
    }


def _verify_collector(
    resource: Mapping[str, object],
    namespace: str,
    name: str,
    image: ExactImage,
    *,
    pvc_name: str,
) -> tuple[str, str]:
    uid = _uid(resource, "Pod", name, namespace)
    spec = _mapping(resource.get("spec"), "collector spec")
    containers = spec.get("containers")
    if (
        not isinstance(containers, list)
        or len(containers) != 1
        or not isinstance(containers[0], Mapping)
    ):
        raise LiveKubernetesError("collector Pod container shape conflicts")
    container = containers[0]
    if (
        container.get("image") != image.reference
        or container.get("imagePullPolicy") != "Never"
        or container.get("command") != ["/hello", "--read", RESULT_PATH]
        or container.get("securityContext")
        != {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "privileged": False,
            "readOnlyRootFilesystem": True,
            "runAsNonRoot": True,
            "runAsUser": 65532,
        }
        or container.get("volumeMounts")
        != [{"name": "outputs", "mountPath": "/outputs", "readOnly": True}]
    ):
        raise LiveKubernetesError(
            "collector Pod image, read-only mount, or security fields conflict"
        )
    if (
        spec.get("automountServiceAccountToken") is not False
        or spec.get("enableServiceLinks") is not False
        # Kubernetes may omit explicitly false host-namespace fields from a
        # returned Pod. Their API defaults are false; a present non-false value
        # is unsafe and remains rejected.
        or any(
            spec.get(field, False) is not False for field in ("hostIPC", "hostNetwork", "hostPID")
        )
        or spec.get("restartPolicy") != "Never"
        or spec.get("securityContext")
        != {
            "runAsGroup": 65532,
            "runAsNonRoot": True,
            "runAsUser": 65532,
            "seccompProfile": {"type": "RuntimeDefault"},
        }
        or spec.get("volumes")
        != [
            {
                "name": "outputs",
                "persistentVolumeClaim": {"claimName": pvc_name, "readOnly": True},
            }
        ]
    ):
        raise LiveKubernetesError("collector Pod PVC binding or security fields conflict")
    statuses = _mapping(resource.get("status"), "collector status").get("containerStatuses")
    if not isinstance(statuses, list) or len(statuses) != 1 or not isinstance(statuses[0], Mapping):
        raise LiveKubernetesError("collector image status is unavailable")
    image_id = statuses[0].get("imageID")
    if (
        not isinstance(image_id, str)
        or not image_id
        or (image.manifest_digest not in image_id and image.config_digest not in image_id)
    ):
        raise LiveKubernetesError("collector image ID does not match the verified local image")
    return uid, image_id


def _verify_bound_pvc(
    resource: Mapping[str, object], namespace: str, expected_uid: str, expected_spec_sha256: str
) -> None:
    if _uid(resource, "PersistentVolumeClaim", "outputs", namespace) != expected_uid:
        raise LiveKubernetesError("PVC UID changed after workload scheduling")
    if _pvc_spec_sha256(resource) != expected_spec_sha256:
        raise LiveKubernetesError("PVC spec changed after workload scheduling")
    labels = _mapping(resource.get("metadata"), "PVC metadata").get("labels")
    if not isinstance(labels, Mapping) or any(
        labels.get(key) != expected for key, expected in _OUTPUT_PVC_LABELS.items()
    ):
        raise LiveKubernetesError("PVC is not the dedicated Tessera output claim")
    if _mapping(resource.get("status"), "PVC status").get("phase") != "Bound":
        raise LiveKubernetesError("PVC did not bind after workload scheduling")


def _pvc_spec_sha256(resource: Mapping[str, object]) -> str:
    """Hash request identity while excluding Kubernetes' late-bound PV name."""
    spec = dict(_mapping(resource.get("spec"), "PVC spec"))
    spec.pop("volumeName", None)
    return hashlib.sha256(canonical_json(spec)).hexdigest()


def _require_docker_desktop_node() -> None:
    version = _run(
        ("docker", "version", "--format", "{{.Server.Arch}}/{{.Server.Os}}")
    ).stdout.strip()
    if version != "amd64/linux":
        raise LiveKubernetesError("Docker daemon is not the required linux/amd64 node architecture")
    node = _run(
        (
            "docker",
            "inspect",
            "--format",
            "{{.State.Running}} {{.Config.Image}}",
            "desktop-control-plane",
        )
    ).stdout.strip()
    if not node.startswith("true kindest/node:"):
        raise LiveKubernetesError("named Docker Desktop Kubernetes node is unavailable")


def _run(
    argv: Sequence[str], *, timeout: int = _COMMAND_TIMEOUT, input: str | bytes | None = None
) -> subprocess.CompletedProcess[str | bytes]:
    if not 1 <= timeout <= _COMMAND_TIMEOUT:
        raise ValueError("live command timeout must be between one and 120 seconds")
    completed = subprocess.run(
        tuple(argv),
        input=input,
        text=not isinstance(input, bytes),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    for value in (completed.stdout, completed.stderr):
        if len(value.encode() if isinstance(value, str) else value) > _MAX_OUTPUT:
            raise LiveKubernetesError("live command output exceeds the bounded adapter limit")
    if completed.returncode:
        stdout = _command_output(completed.stdout)
        stderr = _command_output(completed.stderr)
        details = f"{stdout}\n{stderr}".lower()
        if "notfound" in details or "not found" in details:
            raise _KubectlNotFound("Kubernetes resource was not found")
        diagnostic = stderr.strip() or stdout.strip() or "no diagnostic output"
        raise LiveKubernetesError(f"live command failed ({shlex.join(tuple(argv))}): {diagnostic}")
    return completed


def _command_output(value: str | bytes) -> str:
    return value if isinstance(value, str) else value.decode("utf-8", errors="replace")


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LiveKubernetesError(f"{field} is malformed")
    return value


def _uid(resource: Mapping[str, object], kind: str, name: str, namespace: str | None) -> str:
    metadata = _mapping(resource.get("metadata"), f"{kind} metadata")
    uid = metadata.get("uid")
    if (
        resource.get("kind") != kind
        or metadata.get("name") != name
        or metadata.get("namespace") != namespace
        or not isinstance(uid, str)
        or not uid
    ):
        raise LiveKubernetesError(f"{kind} identity conflicts with the explicit target")
    return uid


def _sha256_file(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def new_nonce() -> str:
    """Return a DNS-safe unique token; it is used only in retained test names."""
    return uuid.uuid4().hex[:16]


__all__ = [
    "CONTEXT",
    "PROFILE",
    "RESULT_BYTES",
    "RESULT_PATH",
    "DockerDesktopKubernetesAdapter",
    "DockerDesktopTarget",
    "ExactImage",
    "LiveKubernetesError",
    "LiveKubernetesEvidence",
    "build_and_load_exact_image",
    "new_nonce",
    "target_from_kubeconfig",
]
