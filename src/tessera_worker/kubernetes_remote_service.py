"""Opt-in, namespaced Kubernetes dispatch process with explicit target identity.

The operator supplies target.json and a projected worker token separately. A
NetworkPolicy declaration alone is never treated as connectivity proof.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from io import BytesIO
from pathlib import Path

import yaml

from .control_channel import ControlChannelError, HttpControlChannel
from .kubernetes_contracts import (
    KubernetesClusterConfig,
    KubernetesConnectivityEvidence,
    KubernetesNetworkPolicyEvidence,
    canonical_json,
)
from .kubernetes_dispatch_service import (
    KubernetesDispatchWorkerConfig,
    KubernetesDispatchWorkerService,
)
from .kubernetes_execution import KubernetesExecutionAdapter, KubernetesOutputBinding
from .kubernetes_pod_listing import list_owned_pods
from .service import DevelopmentWorkerError, read_private_credential


class RemoteTargetError(RuntimeError):
    def __init__(self, message, *, diagnostic_code="target_validation_failed", http_status=None):
        super().__init__(message)
        self.diagnostic_code = diagnostic_code
        self.http_status = http_status


def _run_with_idle_recovery(service: KubernetesDispatchWorkerService, max_cycles: int) -> dict:
    """Retry only pre-execution control outages, retaining the same durable journal.

    A descriptor means provider work may be owned locally: the supervisor, not this
    idle admission loop, must enforce its lease/outage and cancellation bounds.
    HTTP denials and malformed/protocol responses are not transport failures.
    """
    for attempt in range(4):
        try:
            return service.run(max_cycles=max_cycles or None)
        except ControlChannelError as error:
            if (
                max_cycles
                or error.status not in {None, 502, 503, 504}
                or service.claims.descriptor() is not None
                or attempt == 3
            ):
                raise
            time.sleep(min(2**attempt, 4))
    raise AssertionError("unreachable")


def _private_json(path: Path) -> dict:
    if (
        not path.is_absolute()
        or not path.is_file()
        or (path.is_symlink() and path.parent != Path("/run/secrets/tessera"))
    ):
        raise RemoteTargetError("an absolute regular target file is required")
    if path.stat().st_size > 16384:
        raise RemoteTargetError("target file exceeds 16 KiB")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise RemoteTargetError("target must be an object")
    return value


def _network(value: dict) -> KubernetesNetworkPolicyEvidence:
    def probe(item: dict) -> KubernetesConnectivityEvidence:
        return KubernetesConnectivityEvidence(
            item["source_pod_uid"],
            item["destination_pod_uid"],
            item["outcome"],
            item["timeout_seconds"],
            datetime.fromisoformat(item["observed_at"]),
        )

    return KubernetesNetworkPolicyEvidence(
        value["capability"],
        value["policy_name"],
        value["policy_uid"],
        value["policy_spec_sha256"],
        value["cluster_uid"],
        value["namespace_uid"],
        datetime.fromisoformat(value["observed_at"]),
        probe(value["allowed_connectivity"]),
        probe(value["denied_connectivity"]),
    )


class NamespacedKubernetesClient:
    """Small fixed-path HTTPS client; no ambient kubeconfig, proxy or redirects."""

    def __init__(self, cluster: KubernetesClusterConfig, ca: Path, token: Path):
        self.cluster = cluster
        parsed = urllib.parse.urlsplit(cluster.server)
        if (
            parsed.scheme != "https"
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise RemoteTargetError("Kubernetes server must be an HTTPS origin")
        if not ca.is_absolute() or not token.is_absolute():
            raise RemoteTargetError("absolute projected CA and token paths are required")
        if hashlib.sha256(ca.read_bytes()).hexdigest() != cluster.ca_sha256:
            raise RemoteTargetError("Kubernetes CA does not match target binding")
        if hashlib.sha256(cluster.server.encode()).hexdigest() != cluster.server_sha256:
            raise RemoteTargetError("Kubernetes server does not match target binding")
        self.ca, self.token = ca, token
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(ca))),
            _NoRedirect(),
        )

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        max_bytes: int = 2_000_000,
        timeout_seconds: float = 5.0,
    ) -> dict | None:
        if not path.startswith("/api/") and not path.startswith("/apis/"):
            raise RemoteTargetError("Kubernetes path outside fixed API")
        token = self.token.read_bytes()
        if not token or len(token) > 8192 or any(ch in token for ch in (b"\n", b"\r")):
            raise RemoteTargetError(
                "invalid projected Kubernetes token", diagnostic_code="projected_token_invalid"
            )
        payload = canonical_json(body) if body is not None else None
        request = urllib.request.Request(
            self.cluster.server.rstrip("/") + path,
            data=payload,
            method=method,
            headers={
                "Authorization": "Bearer " + token.decode("ascii"),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        deadline = time.monotonic() + min(5.0, timeout_seconds)
        try:
            with self.opener.open(request, timeout=min(5.0, timeout_seconds)) as response:
                data = self._read_response(response, max_bytes, deadline)
            if len(data) > max_bytes:
                raise RemoteTargetError("Kubernetes response exceeds bound")
            return json.loads(data) if data else None
        except urllib.error.HTTPError as error:
            if method == "GET" and error.code == 404:
                error.close()
                return None
            code = "kubernetes_http_rejected"
            try:
                # HTTPError wraps the HTTPResponse in fp. Read that response in
                # single buffered chunks, resetting its socket to the remaining
                # *original* budget, never a new diagnostics timeout.
                response = error.fp
                status_body = self._read_response(response, 8192, deadline, diagnostics=True)
                status = json.loads(status_body) if len(status_body) <= 8192 else None
                if (
                    isinstance(status, dict)
                    and status.get("kind") == "Status"
                    and status.get("reason") == "Forbidden"
                    and isinstance(status.get("message"), str)
                    and "exceeded quota:" in status["message"]
                ):
                    code = "kubernetes_quota_exceeded"
            except (OSError, ValueError):
                pass
            finally:
                error.close()
            raise RemoteTargetError(
                f"Kubernetes {method} rejected (HTTP {error.code})",
                diagnostic_code=code,
                http_status=error.code,
            ) from None

        except ssl.SSLError:
            raise RemoteTargetError(
                "Kubernetes TLS request unavailable", diagnostic_code="kubernetes_tls_error"
            ) from None
        except (OSError, ValueError, UnicodeError) as error:
            reason = getattr(error, "reason", None)
            code = (
                "kubernetes_tls_error"
                if isinstance(reason, ssl.SSLError)
                else "kubernetes_transport_timeout"
                if isinstance(error, TimeoutError) or isinstance(reason, TimeoutError)
                else "kubernetes_transport_or_decode_error"
            )
            raise RemoteTargetError(
                "Kubernetes request unavailable or malformed", diagnostic_code=code
            ) from None

    @staticmethod
    def _read_response(response, max_bytes, deadline, *, diagnostics=False):
        socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if diagnostics and (
            not callable(getattr(response, "read1", None))
            or (socket is None and not isinstance(response, BytesIO))
        ):
            # Never attempt optional diagnostics on an unknown blocking stream.
            # In-memory buffers require no I/O timeout; real HTTPResponses must
            # expose the socket whose timeout we reduce on every buffered read.
            raise ValueError("bounded HTTP diagnostics unavailable")
        chunks, size = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if socket is not None:
                socket.settimeout(remaining)
            reader = getattr(response, "read1", response.read)
            chunk = reader(min(65536, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                break
        if time.monotonic() >= deadline:
            raise TimeoutError
        return b"".join(chunks)

    def path(self, kind: str, name: str | None = None) -> str:
        namespace = urllib.parse.quote(self.cluster.namespace, safe="")
        base = {
            "job": f"/apis/batch/v1/namespaces/{namespace}/jobs",
            "pod": f"/api/v1/namespaces/{namespace}/pods",
            "pvc": f"/api/v1/namespaces/{namespace}/persistentvolumeclaims",
            "configmap": f"/api/v1/namespaces/{namespace}/configmaps",
            "networkpolicy": f"/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies",
            "namespace": "/api/v1/namespaces",
        }[kind]
        return base + ("/" + urllib.parse.quote(name, safe="") if name else "")

    def get(self, kind: str, name: str) -> dict | None:
        return self.request("GET", self.path(kind, name))

    def dataset_client(self, output):
        client = self

        class DatasetClient:
            def authorize(self, verb, name=None, admission=False):
                client.verify(output, admission=admission)
                review = client.request(
                    "POST",
                    "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
                    {
                        "apiVersion": "authorization.k8s.io/v1",
                        "kind": "SelfSubjectAccessReview",
                        "spec": {
                            "resourceAttributes": {
                                "namespace": client.cluster.namespace,
                                "verb": verb,
                                "group": "",
                                "resource": "configmaps",
                                **({"name": name} if name else {}),
                            }
                        },
                    },
                )
                if (review or {}).get("status", {}).get("allowed") is not True:
                    raise RemoteTargetError(
                        "Scoped ConfigMap RBAC denied", diagnostic_code="dataset_rbac_denied"
                    )

            def dataset_read(self, name):
                self.authorize("get", name)
                return client.request("GET", client.path("configmap", name), max_bytes=1024 * 1024)

            def dataset_create(self, manifest):
                if (
                    manifest.get("kind") != "ConfigMap"
                    or manifest.get("immutable") is not True
                    or manifest.get("metadata", {}).get("namespace") != client.cluster.namespace
                ):
                    raise RemoteTargetError("Dataset resource outside bound namespace/type")
                self.authorize("create", admission=True)
                value = client.request(
                    "POST", client.path("configmap"), manifest, max_bytes=1024 * 1024
                )
                client.verify(output, admission=True)
                return value

            def dataset_delete(self, name, uid):
                self.authorize("delete", name)
                return client.request(
                    "DELETE",
                    client.path("configmap", name),
                    {
                        "apiVersion": "v1",
                        "kind": "DeleteOptions",
                        "preconditions": {"uid": uid},
                    },
                )

        return DatasetClient()

    def verify(self, output: KubernetesOutputBinding, *, admission: bool = False) -> None:
        if hashlib.sha256(Path(self.cluster.kubeconfig).read_bytes()).hexdigest() != (
            self.cluster.kubeconfig_sha256
        ):
            raise RemoteTargetError(
                "explicit kubeconfig identity changed",
                diagnostic_code="kubeconfig_identity_changed",
            )
        cluster_ns = self.get("namespace", "kube-system")
        workload_ns = self.get("namespace", self.cluster.namespace)
        if (cluster_ns or {}).get("metadata", {}).get("uid") != self.cluster.cluster_uid or (
            workload_ns or {}
        ).get("metadata", {}).get("uid") != self.cluster.namespace_uid:
            raise RemoteTargetError(
                "cluster or workload namespace identity changed",
                diagnostic_code="namespace_identity_changed",
            )
        pvc = self.get("pvc", output.pvc_name)
        if (
            (pvc or {}).get("metadata", {}).get("uid") != output.pvc_uid
            or (pvc or {}).get("status", {}).get("phase") != "Bound"
            or hashlib.sha256(canonical_json((pvc or {}).get("spec"))).hexdigest()
            != output.pvc_spec_sha256
        ):
            raise RemoteTargetError(
                "output PVC binding changed or is unbound", diagnostic_code="pvc_identity_changed"
            )
        evidence = self.cluster.network_none_evidence
        if evidence is None or not evidence.is_bound_to(
            self.cluster.cluster_uid, self.cluster.namespace_uid
        ):
            raise RemoteTargetError(
                "bound witnessed network enforcement is required",
                diagnostic_code="network_evidence_invalid",
            )
        if admission and not evidence.is_fresh_for(
            self.cluster.cluster_uid, self.cluster.namespace_uid
        ):
            raise RemoteTargetError(
                "fresh witnessed network enforcement is required for admission",
                diagnostic_code="network_evidence_stale",
            )
        policy = self.get("networkpolicy", evidence.policy_name)
        if (policy or {}).get("metadata", {}).get("uid") != evidence.policy_uid or hashlib.sha256(
            canonical_json((policy or {}).get("spec"))
        ).hexdigest() != evidence.policy_spec_sha256:
            raise RemoteTargetError(
                "witnessed network policy identity changed",
                diagnostic_code="network_policy_identity_changed",
            )

    def adapter(
        self, journal: Path, output: KubernetesOutputBinding, collector=None
    ) -> KubernetesExecutionAdapter:
        def inspect(name: str):
            self.verify(output)
            return self.get("job", name)

        def create(manifest: dict):
            self.verify(output, admission=True)
            return self.request("POST", self.path("job"), manifest)

        def delete(name: str, uid: str):
            self.verify(output)
            return self.request(
                "DELETE",
                self.path("job", name),
                {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}},
            )

        def pods(uid: str):
            self.verify(output)
            try:
                owned = list_owned_pods(self, uid)
            except ValueError as error:
                raise RemoteTargetError(str(error)) from None
            self.verify(output)
            return owned

        return KubernetesExecutionAdapter(
            journal,
            self.cluster,
            inspect_job=inspect,
            create_job=create,
            delete_job=delete,
            observe_pods=pods,
            prepare_output=collector.prepare if collector else None,
            collect_output=collector.collect if collector else None,
            release_output=collector.release_collection if collector else None,
            dataset_client=self.dataset_client(output),
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def configure(target_file: Path, ca: Path, token: Path, workspace: Path):
    data = _private_json(target_file)
    binding = data["cluster"]
    evidence = _network(data["network_evidence"])
    kubeconfig = Path(binding["kubeconfig"])
    if (
        not kubeconfig.is_absolute()
        or (kubeconfig.is_symlink() and kubeconfig.parent != Path("/run/secrets/tessera"))
        or hashlib.sha256(kubeconfig.read_bytes()).hexdigest() != binding["kubeconfig_sha256"]
    ):
        raise RemoteTargetError("explicit kubeconfig identity changed")
    if kubeconfig.stat().st_size > 65536:
        raise RemoteTargetError("explicit kubeconfig exceeds 64 KiB")
    try:
        config = yaml.safe_load(kubeconfig.read_bytes())
    except yaml.YAMLError:
        raise RemoteTargetError("explicit kubeconfig is malformed") from None
    if not isinstance(config, dict):
        raise RemoteTargetError("explicit kubeconfig is malformed")
    contexts = [
        item for item in config.get("contexts", []) if item.get("name") == binding["context"]
    ]
    if len(contexts) != 1:
        raise RemoteTargetError("explicit kubeconfig context is unavailable")
    clusters = [
        item
        for item in config.get("clusters", [])
        if item.get("name") == contexts[0]["context"]["cluster"]
    ]
    if len(clusters) != 1 or clusters[0]["cluster"].get("server") != binding["server"]:
        raise RemoteTargetError("explicit kubeconfig server differs from target")
    ca_data = clusters[0]["cluster"].get("certificate-authority-data")
    if (
        not isinstance(ca_data, str)
        or hashlib.sha256(base64.b64decode(ca_data, validate=True)).hexdigest()
        != binding["ca_sha256"]
    ):
        raise RemoteTargetError("explicit kubeconfig CA differs from target")
    cluster = KubernetesClusterConfig(**binding, network_none_evidence=evidence)
    output = KubernetesOutputBinding(**{**data["output"], "operation_scoped": True})
    client = NamespacedKubernetesClient(cluster, ca, token)
    client.verify(output)
    from .kubernetes_output_collector import KubernetesOutputCollector

    collector = KubernetesOutputCollector(
        client, output, data.get("collector_image"), workspace / "output-pods.sqlite3"
    )
    return cluster, output, client.adapter(workspace / "kubernetes-jobs.sqlite3", output, collector)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-file", type=Path, required=True)
    parser.add_argument("--ca-file", type=Path, required=True)
    parser.add_argument("--kubernetes-token-file", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--control-ca-file", type=Path, required=True)
    parser.add_argument("--control-url", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--max-cycles", type=int, default=0)
    args = parser.parse_args(argv)
    if args.max_cycles < 0 or not args.workspace.is_absolute():
        parser.error("max cycles must be nonnegative and workspace absolute")
    try:
        args.workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        cluster, output, adapter = configure(
            args.target_file, args.ca_file, args.kubernetes_token_file, args.workspace
        )
        control_context = ssl.create_default_context(cafile=str(args.control_ca_file))
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=control_context),
            _NoRedirect(),
        )
        try:
            credential = read_private_credential(args.credential_file)
        except DevelopmentWorkerError:
            channel = None  # Existing journal still permits local recovery/cancellation.
        else:
            channel = HttpControlChannel(
                args.control_url,
                credential,
                opener=lambda request, timeout: opener.open(request, timeout=timeout),
            )
        service = KubernetesDispatchWorkerService(
            KubernetesDispatchWorkerConfig(
                args.control_url, None, args.credential_file, args.workspace
            ),
            cluster,
            output,
            adapter,
            channel=channel,
        )
        while True:
            result = _run_with_idle_recovery(service, args.max_cycles)
            print(
                json.dumps(
                    {
                        "status": (
                            "output_delivery_pending"
                            if result.get("output_delivery") == "pending"
                            else result.get("status", "observed")
                        ),
                        "dispatch_id": result.get("dispatch_id"),
                        **(
                            {"failure_evidence": result["failure_evidence"]}
                            if result.get("failure_evidence")
                            else {}
                        ),
                        **(
                            {"no_start_attestation_required": True}
                            if result.get("no_start_attestation_required")
                            else {}
                        ),
                    }
                ),
                flush=True,
            )
            if args.max_cycles:
                break
            if (
                result.get("status") in {"idle", "report_draining"}
                or result.get("output_delivery") == "pending"
                or result.get("no_start_attestation_required")
                or result.get("status") == "cancel_requested"
                and result.get("reconciliation_required")
            ):
                time.sleep(5)
    except (OSError, ValueError, KeyError, TypeError, RemoteTargetError) as error:
        print(f"remote dispatch unavailable: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
