"""Explicit HTTPS Kubernetes callbacks for the opt-in constant-probe pilot.

No ambient kube context, exec plugin, proxy, Secret, image pull, retry, provisioning
or teardown. KubernetesExecutionAdapter owns intent/identity/reconciliation.
"""

from __future__ import annotations

import hashlib
import json
import ssl
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPSHandler, Request, build_opener

from .comparison_pilot import PilotError, digest
from .control_channel import ControlChannelError, HttpControlChannel, _RejectRedirects
from .kubernetes_contracts import (
    KubernetesClusterConfig,
    KubernetesConnectivityEvidence,
    KubernetesNetworkPolicyEvidence,
    canonical_json,
    cluster_identity_digest,
)
from .kubernetes_execution import KubernetesExecutionAdapter, KubernetesOutputBinding
from .pilot_isolation import DISPATCHER, OUTPUT, PROFILE, PilotIsolation
from .service import read_private_credential

_OUTPUT_STORAGE_LABELS = {
    "app.kubernetes.io/managed-by": "tessera",
    "tessera.io/output-role": "dedicated-pilot-output",
}


def validate_output_storage_evidence(value, cluster, output, project_id, max_output_bytes):
    """Validate retained output quota evidence before any credential is read.

    A PVC request or free-space observation is not a write quota.  The only
    admissible state records real byte *and* inode exhaustion for the selected
    PVC profile.  Docker Desktop's ordinary local-path storage must therefore
    be represented as ``operator_attested_unknown`` and cannot start this pilot.
    """
    if not isinstance(value, dict):
        raise PilotError("output storage evidence is malformed")
    common = {
        "version",
        "target_digest",
        "project_id",
        "pvc_name",
        "pvc_uid",
        "pvc_spec_sha256",
        "pvc_labels",
        "state",
    }
    if not common.issubset(value) or value.get("version") != 1:
        raise PilotError("output storage evidence has an unsupported contract version")
    if (
        value["target_digest"] != cluster_identity_digest(cluster)
        or value["project_id"] != project_id
        or value["pvc_name"] != output.pvc_name
        or value["pvc_uid"] != output.pvc_uid
        or value["pvc_spec_sha256"] != output.pvc_spec_sha256
        or value["pvc_labels"] != (_OUTPUT_STORAGE_LABELS | {"tessera.io/project-id": project_id})
    ):
        raise PilotError("output storage evidence is not bound to the selected PVC")
    if value["state"] == "operator_attested_unknown":
        if set(value) != common | {"operator_attestation", "expires_at"}:
            raise PilotError("unknown output storage evidence fields are malformed")
        if not isinstance(value["operator_attestation"], str) or not value["operator_attestation"]:
            raise PilotError("unknown output storage evidence requires an operator attestation")
        raise PilotError(
            "output storage enforcement is operator-attested unknown; live execution is blocked"
        )
    required = common | {"max_bytes", "max_inodes", "byte_probe", "inode_probe", "expires_at"}
    if value["state"] != "filesystem_quota_enforced_v1" or set(value) != required:
        raise PilotError("hard output storage enforcement evidence is required")
    if (
        type(value["max_bytes"]) is not int
        or value["max_bytes"] < max_output_bytes
        or type(value["max_inodes"]) is not int
        or value["max_inodes"] < 1
    ):
        raise PilotError("output storage bounds do not cover the approved output")
    for kind, key, observed in (
        ("byte", "byte_probe", "written_bytes"),
        ("inode", "inode_probe", "created_files"),
    ):
        probe = value[key]
        limit = value["max_bytes"] if kind == "byte" else value["max_inodes"]
        if (
            not isinstance(probe, dict)
            or probe.get("classification") != "enforced"
            or probe.get("limit") != limit
            or type(probe.get(observed)) is not int
            or probe[observed] > limit
            or probe.get("failure_errno") not in {28, 122}
        ):
            raise PilotError(f"output {kind} quota evidence is not an observed enforced bound")
    try:
        expires_at = datetime.fromisoformat(value["expires_at"])
    except (TypeError, ValueError) as error:
        raise PilotError("output storage evidence expiry is malformed") from error
    if expires_at.tzinfo is None or expires_at <= datetime.now(UTC):
        raise PilotError("output storage evidence is expired")
    return value


def bounded_file(path: Path, limit: int) -> bytes:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise PilotError("runtime reference must name an absolute regular file")
    with path.open("rb") as source:
        content = source.read(limit + 1)
    if len(content) > limit:
        raise PilotError("runtime reference exceeds its size limit")
    return content


def load_target(value):
    cluster = dict(value["cluster"])
    if cluster.get("network_none_evidence") is None:
        return KubernetesClusterConfig(**cluster), KubernetesOutputBinding(**value["output"])
    evidence = dict(cluster["network_none_evidence"])
    evidence["observed_at"] = datetime.fromisoformat(evidence["observed_at"])
    for field in ("allowed_connectivity", "denied_connectivity"):
        probe = dict(evidence[field])
        probe["observed_at"] = datetime.fromisoformat(probe["observed_at"])
        evidence[field] = KubernetesConnectivityEvidence(**probe)
    cluster["network_none_evidence"] = KubernetesNetworkPolicyEvidence(**evidence)
    return KubernetesClusterConfig(**cluster), KubernetesOutputBinding(**value["output"])


class RemoteKubernetes:
    def __init__(
        self,
        cluster,
        output,
        ca_file: Path,
        credential_file: Path,
        journal,
        scope,
        cost_expires_at,
        output_storage,
        *,
        isolation,
    ):
        from urllib.request import ProxyHandler

        self.cluster, self.output = cluster, output
        self.credential_file, self.journal = credential_file, journal
        if set(scope) != {"org_id", "project_id", "service_account", "expires_at"}:
            raise PilotError("compute credential requires explicit tenant/project/namespace scope")
        self.scope = scope
        self.isolation = PilotIsolation(
            org_id=scope["org_id"], project_id=scope["project_id"], **isolation
        )
        if (
            cluster.namespace != self.isolation.namespace
            or scope["service_account"] != DISPATCHER
            or output.pvc_name != OUTPUT
            or output.mount_path != "/outputs"
        ):
            raise PilotError("pilot requires the dedicated project execution target")
        self.cost_expires_at = cost_expires_at
        self.output_storage = output_storage
        self.credential_expires_at = datetime.fromisoformat(scope["expires_at"])
        self.credential_fingerprint = hashlib.sha256(
            read_private_credential(credential_file).encode()
        ).digest()
        ca = bounded_file(ca_file, 1024 * 1024)
        if hashlib.sha256(ca).hexdigest() != cluster.ca_sha256:
            raise PilotError("target CA file digest mismatch")
        if hashlib.sha256(bounded_file(Path(cluster.kubeconfig), 1024 * 1024)).hexdigest() != (
            cluster.kubeconfig_sha256
        ):
            raise PilotError("explicit target kubeconfig digest mismatch")
        if hashlib.sha256(cluster.server.encode()).hexdigest() != cluster.server_sha256:
            raise PilotError("target server digest mismatch")
        context = ssl.create_default_context(cadata=ca.decode("ascii"))
        self.opener = build_opener(
            ProxyHandler({}), _RejectRedirects(), HTTPSHandler(context=context)
        )
        self.sequence = 0
        self.jobs_path = f"/apis/batch/v1/namespaces/{cluster.namespace}/jobs"
        self.pods_path = f"/api/v1/namespaces/{cluster.namespace}/pods"

    def request(self, method, path, body=None, *, log_limit=None, admission_probe=False):
        if admission_probe and (method != "POST" or path != self.jobs_path + "?dryRun=All"):
            raise PilotError("admission probes must be server-side Job dry runs")
        self.sequence += 1
        identity = {"sequence": self.sequence, "method": method, "path": path}
        self.journal.event("remote_request_intent", identity | {"request_digest": digest(body)})
        token = read_private_credential(self.credential_file)
        if hashlib.sha256(token.encode()).digest() != self.credential_fingerprint:
            raise PilotError("compute credential reference changed; no implicit account rotation")
        # Use the existing single-flight, absolute caller-deadline transport. A
        # timed-out thread may finish remotely; it cannot initiate another call.
        channel = HttpControlChannel(self.cluster.server, token, timeout_seconds=5)
        request = Request(
            channel.origin + path,
            data=None if body is None else canonical_json(body),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method=method,
        )

        def perform(req, timeout):
            return self.opener.open(req, timeout=timeout)

        channel._open = perform
        if admission_probe:

            def probe(req, timeout):
                try:
                    with perform(req, timeout) as response:
                        content = response.read(65537)
                    if len(content) > 65536:
                        raise PilotError("oversized dry-run reply")
                    return json.loads(content)
                except HTTPError as error:
                    # Quota, RBAC, schema and unrelated webhook rejections do
                    # not establish that this admission policy denied the Job.
                    content = error.read(65537)
                    error.close()
                    if error.code in (403, 422) and len(content) <= 65536:
                        message = json.loads(content).get("message", "")
                        if (
                            self.isolation.namespace in message
                            and PROFILE + ": constraint " in message
                        ):
                            return {"admission_denied": True, "policy": self.isolation.namespace}
                    raise PilotError("dry run rejected without selected-policy evidence") from None

            channel._perform = probe
        if log_limit is not None:

            def read_log(req, timeout):
                try:
                    with perform(req, timeout) as response:
                        content = response.read(log_limit + 1)
                    if len(content) > log_limit:
                        raise PilotError("remote stdout exceeds approved output limit")
                    return {"content": content}
                except Exception:
                    raise PilotError("remote stdout unavailable or exceeds limit") from None

            channel._perform = read_log
        try:
            response = channel._submit(request, 5).result(timeout=5)
        except (ControlChannelError, HTTPError) as error:
            self.journal.event("remote_reply_unavailable", identity | {"automatic_retry": False})
            if method == "GET" and getattr(error, "status", None) == 404:
                return None
            raise PilotError("remote request rejected; no retry") from None
        except Exception:
            self.journal.event("remote_reply_unknown", identity | {"automatic_retry": False})
            raise PilotError("remote reply unknown; no retry") from None
        response_digest = (
            hashlib.sha256(response["content"]).hexdigest()
            if log_limit is not None
            else digest(response)
        )
        self.journal.event("remote_response", identity | {"response_digest": response_digest})
        return response

    def verify_target(self, *, submission=False, authority_probe=False):
        if submission or authority_probe:
            if datetime.now(UTC) >= self.cost_expires_at:
                raise PilotError("target cost evidence expired")
            if datetime.now(UTC) >= self.credential_expires_at:
                raise PilotError("compute credential authorization expired")
            if submission:
                try:
                    storage_expires_at = datetime.fromisoformat(self.output_storage["expires_at"])
                except (KeyError, TypeError, ValueError):
                    raise PilotError("output storage attestation expiry is malformed") from None
                if storage_expires_at.tzinfo is None or storage_expires_at <= datetime.now(UTC):
                    raise PilotError("output storage attestation expired")
            subject = self.request(
                "POST",
                "/apis/authentication.k8s.io/v1/selfsubjectreviews",
                {"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview", "spec": {}},
            )
            expected_subject = (
                f"system:serviceaccount:{self.cluster.namespace}:{self.scope['service_account']}"
            )
            if subject["status"]["userInfo"]["username"] != expected_subject:
                raise PilotError("compute credential is not the selected namespace service account")
            self.journal.event(
                "compute_credential_scope",
                {
                    "org_id": self.scope["org_id"],
                    "project_id": self.scope["project_id"],
                    "subject": expected_subject,
                    "expires_at": self.scope["expires_at"],
                },
            )
        for name, uid in (
            ("kube-system", self.cluster.cluster_uid),
            (self.cluster.namespace, self.cluster.namespace_uid),
        ):
            value = self.request("GET", f"/api/v1/namespaces/{name}")
            if not value or value["metadata"]["uid"] != uid:
                raise PilotError("remote namespace/cluster identity changed")
        if submission or authority_probe:
            self.isolation.verify(self.request, value)
        if submission:
            evidence = self.cluster.network_none_evidence
            if evidence is None or not evidence.is_fresh_for(
                self.cluster.cluster_uid, self.cluster.namespace_uid
            ):
                raise PilotError("fresh selected-target network isolation evidence required")
            policy = self.request(
                "GET",
                f"/apis/networking.k8s.io/v1/namespaces/{self.cluster.namespace}"
                f"/networkpolicies/{evidence.policy_name}",
            )
            pvc = self.request(
                "GET",
                f"/api/v1/namespaces/{self.cluster.namespace}"
                f"/persistentvolumeclaims/{self.output.pvc_name}",
            )
            if (
                policy["metadata"]["uid"] != evidence.policy_uid
                or digest(policy["spec"]) != evidence.policy_spec_sha256
                or pvc["metadata"]["uid"] != self.output.pvc_uid
                or digest(pvc["spec"]) != self.output.pvc_spec_sha256
                or any(
                    pvc.get("metadata", {}).get("labels", {}).get(key) != expected
                    for key, expected in self.output_storage["pvc_labels"].items()
                )
            ):
                raise PilotError(
                    "selected target policy/PVC identity or dedicated ownership changed"
                )

    def inspect(self, name):
        observed = self.request("GET", self.jobs_path + "/" + quote(name, safe=""))
        if (
            observed is None
            and self.has_event("remote_submit_intent", name)
            and not self.has_event("remote_job_seen", name)
        ):
            # A 404 cannot disprove a POST still being processed by the server.
            # This persists across restart and cannot turn lost submit into a
            # green cancellation/absence receipt.
            raise PilotError("unobserved submit intent requires manual reconciliation")
        return observed

    def has_event(self, kind, name):
        return self.journal.contains(kind, name)

    def create(self, manifest):
        name = manifest["metadata"]["name"]
        self.journal.event("remote_submit_intent", {"name": name})
        self.verify_target(submission=True)
        if (
            self.has_event("remote_cancel_intent", name)
            or datetime.now(UTC) >= self.submission_deadline
        ):
            raise PilotError("submission fenced after preflight delay or cancellation")
        if manifest["spec"]["template"]["spec"]["containers"][0]["image"] != self.isolation.image:
            raise PilotError("admitted image differs from the isolated target policy")
        return self.request("POST", self.jobs_path, manifest)

    def delete(self, name, uid):
        self.verify_target()
        return self.request(
            "DELETE",
            self.jobs_path + "/" + quote(name, safe=""),
            {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "propagationPolicy": "Foreground",
                "preconditions": {"uid": uid},
            },
        )

    def pods(self, uid):
        response = self.request("GET", self.pods_path + "?limit=100")
        if response.get("metadata", {}).get("continue"):
            raise PilotError("pod listing exceeds pilot bound; termination remains unknown")
        return [
            pod
            for pod in response["items"]
            if any(
                owner.get("uid") == uid
                and owner.get("controller") is True
                and owner.get("kind") == "Job"
                for owner in pod.get("metadata", {}).get("ownerReferences", [])
            )
        ]

    def collect(self, adapter, package, limit):
        self.verify_target()
        status = adapter.reconcile(package)
        if status.observed_state != "succeeded" or not status.termination_confirmed:
            raise PilotError("fresh terminal evidence required before log collection")
        pods = self.pods(status.job.job_uid)
        if len(pods) != 1:
            raise PilotError("pilot requires exactly one owned Pod; no replacement execution")
        pod = pods[0]
        uid, name = pod["metadata"]["uid"], pod["metadata"]["name"]
        statuses = pod["status"].get("containerStatuses", [])
        if len(statuses) != 1 or statuses[0].get("restartCount") != 0:
            raise PilotError("container restarted or identity unavailable")
        # Do not use limitBytes: server truncation could disguise oversized output.
        response = self.request(
            "GET",
            self.pods_path + f"/{quote(name, safe='')}/log?container=opencode",
            log_limit=limit,
        )
        after = self.request("GET", self.pods_path + f"/{quote(name, safe='')}")
        if after["metadata"]["uid"] != uid:
            raise PilotError("Pod replaced during output collection")
        self.journal.event(
            "stdout_custody",
            {
                "operation_id": package.operation_id,
                "pod_uid": uid,
                "job_uid": status.job.job_uid,
                "provider_id": status.provider_id,
                "sha256": hashlib.sha256(response["content"]).hexdigest(),
            },
        )
        return response["content"], status.provider_id


class PilotRemoteExecutionAdapter(KubernetesExecutionAdapter):
    """Keep transport callbacks fenced when a deadline leaves an in-flight call.

    All authority, intent and observation validation remains in the parent.
    These additional transport fences cannot authorize work or clear uncertainty.
    """

    def __init__(self, journal_path, remote):
        self.remote = remote
        super().__init__(
            journal_path,
            remote.cluster,
            inspect_job=remote.inspect,
            create_job=remote.create,
            delete_job=remote.delete,
            observe_pods=remote.pods,
        )

    def submit(self, package, *, timeout_seconds=None):
        if any(
            package.package[field] != self.remote.scope[field] for field in ("org_id", "project_id")
        ):
            raise PilotError("compute credential tenant/project differs from admitted package")
        self.remote.submission_deadline = min(
            package.lease_expires_at,
            package.hard_expires_at,
            self.remote.credential_expires_at,
            self.remote.cost_expires_at,
        )
        result = super().submit(package, timeout_seconds=timeout_seconds)
        self._record_verified_job(package, result)
        return result

    def reconcile(self, package, *, timeout_seconds=None):
        result = super().reconcile(package, timeout_seconds=timeout_seconds)
        self._record_verified_job(package, result)
        return result

    def _record_verified_job(self, package, result):
        # Only the existing adapter's validated Job identity can discharge the
        # transport's "never observed" uncertainty, not an arbitrary GET body.
        if result.job is not None:
            self.remote.journal.event(
                "remote_job_seen",
                {"name": package.binding.job_name, "job_uid": result.job.job_uid},
            )

    def cancel(self, package, *, timeout_seconds=None):
        self.remote.journal.event("remote_cancel_intent", {"name": package.binding.job_name})
        return super().cancel(package, timeout_seconds=timeout_seconds)
