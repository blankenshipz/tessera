"""Authenticated raw API custody and one-use inspection, not live execution authority.

Runtime observations require a supervisor-injected independent witness. There is
deliberately no JSON/signed-claim witness loader and no default passing witness.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from tessera_control.store import canonical_digest

from .capability_activation import canonical, stamp
from .capability_inspection import RBAC_DENIALS, RawEvidenceInspector, probe_requests, raw_object
from .capability_journal import CapabilityJournal
from .capability_kube_client import APIRequest, CollectionError, encoded

RESOURCES = {
    "Namespace": ("v1", "namespaces", False),
    "ServiceAccount": ("v1", "serviceaccounts", True),
    "ResourceQuota": ("v1", "resourcequotas", True),
    "LimitRange": ("v1", "limitranges", True),
    "NetworkPolicy": ("networking.k8s.io/v1", "networkpolicies", True),
    "Role": ("rbac.authorization.k8s.io/v1", "roles", True),
    "RoleBinding": ("rbac.authorization.k8s.io/v1", "rolebindings", True),
    "ValidatingAdmissionPolicy": (
        "admissionregistration.k8s.io/v1",
        "validatingadmissionpolicies",
        False,
    ),
    "ValidatingAdmissionPolicyBinding": (
        "admissionregistration.k8s.io/v1",
        "validatingadmissionpolicybindings",
        False,
    ),
    "PersistentVolumeClaim": ("v1", "persistentvolumeclaims", True),
    "PersistentVolume": ("v1", "persistentvolumes", False),
    "Pod": ("v1", "pods", True),
    "Job": ("batch/v1", "jobs", True),
    "Deployment": ("apps/v1", "deployments", True),
    "Event": ("v1", "events", True),
}


def resource_path(kind, namespace, name=None):
    version, resource, namespaced = RESOURCES[kind]
    path = ("/api/" if version == "v1" else "/apis/") + version
    if namespaced:
        path += "/namespaces/" + namespace
    path += "/" + resource
    return path + ("/" + name if name else "")


def catalogue(plan, binding):
    """Only exact reads, bounded namespace lists, SSAR/SSR and Job/Pod dry-run POSTs.

    Cross-namespace denial reviews contain attributes, not permission to access
    the control namespace. No Secret read, exec, TokenRequest or actual create.
    """
    m = plan.activation
    if (
        binding.plan_digest,
        binding.namespace,
        binding.namespace_uid,
        binding.org_id,
        binding.project_id,
        binding.cluster_digest,
    ) != (plan.digest, m.namespace, m.namespace_uid, m.org_id, m.project_id, m.cluster_digest):
        raise CollectionError("contradictory")
    result = {
        "cluster": APIRequest("GET", "/api/v1/namespaces/kube-system"),
        "identity": APIRequest(
            "POST",
            "/apis/authentication.k8s.io/v1/selfsubjectreviews",
            encoded(
                {"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview", "spec": {}}
            ),
        ),
    }
    for index, obj in enumerate(plan.objects()):
        result[f"object-{index}"] = APIRequest(
            "GET", resource_path(obj["kind"], m.namespace, obj["metadata"]["name"])
        )
    result["pvc"] = APIRequest("GET", resource_path("PersistentVolumeClaim", m.namespace, m.pvc))
    result["pv"] = APIRequest(
        "GET", resource_path("PersistentVolume", m.namespace, binding.pv_name)
    )
    for kind in ("Deployment", "Pod", "Job", "Role", "RoleBinding", "Event"):
        result["list-" + kind] = APIRequest("GET", resource_path(kind, m.namespace) + "?limit=100")
    for key, request in probe_requests(plan).items():
        if "dryRun" in request:
            obj = request["object"]
            result[key] = APIRequest(
                "POST",
                resource_path(obj["kind"], m.namespace) + "?dryRun=All&fieldValidation=Strict",
                encoded(obj),
            )
        elif key in RBAC_DENIALS:
            result[key] = APIRequest(
                "POST", "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", encoded(request)
            )
    result["rbac-positive"] = APIRequest(
        "POST",
        "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
        encoded(
            {
                "apiVersion": "authorization.k8s.io/v1",
                "kind": "SelfSubjectAccessReview",
                "spec": {
                    "resourceAttributes": {
                        "namespace": m.namespace,
                        "group": "batch",
                        "resource": "jobs",
                        "verb": "create",
                    }
                },
            }
        ),
    )
    workload = f"system:serviceaccount:{m.namespace}:{m.service_account}"
    if binding.identity == workload:
        return {key: value for key, value in result.items() if key in {"identity", *RBAC_DENIALS}}
    if binding.identity == m.dispatcher_identity:
        return {
            key: value
            for key, value in result.items()
            if key == "identity" or key.startswith(("rbac-", "admission-", "pod-admission-"))
        }
    if binding.identity == m.watchdog.identity:
        raise CollectionError("contradictory")
    return {
        key: value for key, value in result.items() if value.method == "GET" or key == "identity"
    }


class RuntimeWitness(Protocol):
    """Trusted process/transport integration, NOT an operator evidence document.

    Must execute fresh bounded challenges in independent CNI/kernel/watchdog
    custody, including harmless negative RBAC controls for both principals.
    Return the existing raw probe schema, never a summary boolean. The collector
    accepts only IndependentRuntimeWitnesses or explicitly enabled test fakes;
    the Protocol is documentation, not an arbitrary-object trust bypass.
    """

    def observe(self, plan, requests, *, deadline): ...


class CollectorJournal(CapabilityJournal):
    def __init__(self, root):
        super().__init__(root)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS collector_attempts (
                    nonce TEXT PRIMARY KEY, operation TEXT UNIQUE NOT NULL,
                    package TEXT UNIQUE NOT NULL, plan TEXT UNIQUE NOT NULL);
                CREATE TABLE IF NOT EXISTS collector_evidence (
                    nonce TEXT PRIMARY KEY, payload BLOB NOT NULL,
                    digest TEXT UNIQUE NOT NULL, signature TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS collector_consumed (nonce TEXT PRIMARY KEY);
            """)
            for table in ("collector_attempts", "collector_evidence", "collector_consumed"):
                for action in ("UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action} "
                        f"BEFORE {action} ON {table} "
                        "BEGIN SELECT RAISE(ABORT,'retained collector custody'); END"
                    )

    def begin(self, plan):
        m = plan.activation
        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO collector_attempts VALUES (?,?,?,?)",
                    (m.nonce, m.operation_id, m.package_digest, plan.digest),
                )
        except Exception:
            raise CollectionError("stale") from None

    def consume(self, nonce):
        try:
            with self.connect() as db:
                db.execute("INSERT INTO collector_consumed VALUES (?)", (nonce,))
        except Exception:
            raise CollectionError("stale") from None


@dataclass(frozen=True, repr=False)
class CollectedEvidence:
    payload: bytes
    digest: str
    signature: str
    _issuer: object


class KubernetesEvidenceCollector:
    def __init__(
        self,
        *,
        client,
        dispatcher,
        workload,
        witness,
        journal,
        key,
        resource_pins,
        now=None,
        monotonic=time.monotonic,
        allow_fake_witnesses=False,
    ):
        if type(key) is not bytes or len(key) < 32:
            raise CollectionError("auth-denied")
        self.client, self.dispatcher, self.workload = client, dispatcher, workload
        self.witness, self.journal, self._key = witness, journal, key
        self.pins = json.loads(encoded(resource_pins))
        self.now, self.clock = now or (lambda: datetime.now(UTC)), monotonic
        self._seal = object()
        self.allow_fake_witnesses = allow_fake_witnesses is True
        self.journal.attach()

    def collect(self, plan, requests):
        self.journal.begin(plan)  # includes failed/partial/revoked attempts, survives restart
        m = plan.activation
        self._nonce = m.nonce
        started = self.now()
        deadline = self.clock() + min(5, self.client.binding.max_seconds)
        records, snapshot = {}, None
        category = "unavailable"
        try:
            if requests != probe_requests(plan):
                raise CollectionError("contradictory")
            for c, identity in (
                (self.client, self.client.binding.identity),
                (self.dispatcher, m.dispatcher_identity),
                (self.workload, f"system:serviceaccount:{m.namespace}:{m.service_account}"),
            ):
                catalogue(plan, c.binding)
                if (
                    c.binding.identity != identity
                    or c.binding.cluster_digest != self.client.binding.cluster_digest
                ):
                    raise CollectionError("contradictory")
            if self.client.binding.identity in {
                m.dispatcher_identity,
                self.workload.binding.identity,
                m.watchdog.identity,
            }:
                raise CollectionError("contradictory")
            if not stamp(m.issued_at) <= started < stamp(m.expires_at):
                raise CollectionError("stale")

            def read(c, key, *, label=None):
                if len(records) >= 256:
                    raise CollectionError("unavailable")
                result = c.request(key, deadline=deadline)
                result["observed_at"] = self.now().isoformat()
                result["request_digest"] = canonical_digest(result["request"])
                result["response_digest"] = canonical_digest(result["body"])
                records[label or c.binding.identity + ":" + key] = result
                if len(encoded(records)) > 8 * 1024 * 1024:
                    raise CollectionError("malformed-response")
                return result

            def success(c, key, *, label=None):
                reply = read(c, key, label=label)
                if reply["status"] not in {200, 201}:
                    raise CollectionError("auth-denied")
                return reply["body"]

            for c in (self.client, self.dispatcher, self.workload):
                identity = success(c, "identity")
                if (
                    identity.get("kind") != "SelfSubjectReview"
                    or identity.get("status", {}).get("userInfo", {}).get("username")
                    != c.binding.identity
                ):
                    raise CollectionError("auth-denied")
            cluster = success(self.client, "cluster")
            if (
                cluster.get("kind") != "Namespace"
                or cluster["metadata"]["name"] != "kube-system"
                or cluster["metadata"]["uid"] != self.client.binding.cluster_uid
            ):
                raise CollectionError("contradictory")
            keys = [f"object-{i}" for i in range(len(plan.objects()))] + ["pvc", "pv", "cluster"]
            if set(self.pins) != set(keys):
                raise CollectionError("contradictory")
            objects = {}
            for key in keys:
                obj = cluster if key == "cluster" else success(self.client, key)
                meta = obj["metadata"]
                if not all(
                    type(meta.get(k)) is str and meta[k]
                    for k in ("uid", "resourceVersion", "creationTimestamp")
                ):
                    raise CollectionError("malformed-response")
                if [meta["uid"], meta["resourceVersion"]] != self.pins[key]:
                    raise CollectionError("stale")
                if stamp(meta["creationTimestamp"]) > started:
                    raise CollectionError("stale")
                objects[key] = obj
            lists = {}
            for kind in ("Deployment", "Pod", "Job", "Role", "RoleBinding", "Event"):
                listing = success(self.client, "list-" + kind)
                if (
                    listing.get("kind") != kind + "List"
                    or not listing.get("metadata", {}).get("resourceVersion")
                    or listing["metadata"].get("continue")
                    or listing["metadata"].get("remainingItemCount", 0)
                ):
                    raise CollectionError("malformed-response")
                if kind in {"Deployment", "Pod", "Job", "Event"} and listing["items"] != []:
                    # Before next exact Job: no existing workloads, even if labeled similarly.
                    raise CollectionError("contradictory")
                lists[kind] = listing
            probes = {}
            for key, request in requests.items():
                if "dryRun" in request:
                    reply = read(self.dispatcher, key)
                    response = reply["body"]
                    if key.endswith("-positive"):
                        if reply["status"] != 201 or raw_object(response) != request["object"]:
                            raise CollectionError("contradictory")
                        response = {"code": 201, "object": raw_object(response)}
                    else:
                        if reply["status"] != 403 or response.get("code") != 403:
                            raise CollectionError("contradictory")
                        # Retain native body above. Only API envelope fields are discarded.
                        response = {
                            k: v
                            for k, v in response.items()
                            if k not in {"apiVersion", "metadata", "message"}
                        }
                    probes[key] = {
                        "request": request,
                        "response": response,
                        "nonce": m.nonce,
                        "subject": plan.digest,
                        "observed_at": reply["observed_at"],
                    }
            review = success(self.dispatcher, "rbac-positive")
            if review.get("kind") != "SelfSubjectAccessReview" or review.get("status") != {
                "allowed": True,
                "denied": False,
            }:
                raise CollectionError("contradictory")
            reviews = {}
            for key in RBAC_DENIALS:
                reviews[key] = []
                for c in (self.dispatcher, self.workload):
                    review = success(c, key)
                    if review.get("kind") != "SelfSubjectAccessReview" or review.get("status") != {
                        "allowed": False,
                        "denied": True,
                    }:
                        raise CollectionError("contradictory")
                    reviews[key].append(review)
            if self.witness is None:
                raise CollectionError("unavailable")
            from .capability_witness_fake import FakeRuntimeWitnesses
            from .capability_witness_transport import IndependentRuntimeWitnesses

            if type(self.witness) is IndependentRuntimeWitnesses:
                if any(
                    c.pin.identity == self.client.binding.identity
                    for c in self.witness.channels.values()
                ):
                    raise CollectionError("contradictory")
                if any(
                    self._key in (c._request_key, c._report_key)
                    for c in self.witness.channels.values()
                ):
                    raise CollectionError("auth-denied")
            elif not (self.allow_fake_witnesses and type(self.witness) is FakeRuntimeWitnesses):
                # A Python object claiming to be an observer is not authenticated
                # daemon custody. Fake witnesses require explicit test composition.
                raise CollectionError("auth-denied")
            runtime_requests = {k: v for k, v in requests.items() if k not in probes}
            try:
                runtime = self.witness.observe(plan, runtime_requests, deadline=deadline)
            finally:
                records["witnesses"] = self.witness.records
            if type(self.witness) is IndependentRuntimeWitnesses:
                self.witness.require_api_agreement(list(objects.values()))
            if set(runtime) != set(runtime_requests) or len(encoded(runtime)) > 2 * 1024 * 1024:
                raise CollectionError("malformed-response")
            records["runtime"] = runtime
            for key in RBAC_DENIALS:
                for i, review in enumerate(reviews[key]):
                    if runtime[key]["response"]["subjects"][i]["review"] != {
                        "kind": review["kind"],
                        "status": review["status"],
                    }:
                        raise CollectionError("contradictory")
            probes.update(runtime)
            snapshot = {
                "plan_digest": plan.digest,
                "nonce": m.nonce,
                "cluster_digest": m.cluster_digest,
                "observed_at": self.now().isoformat(),
                "objects": [objects[f"object-{i}"] for i in range(len(plan.objects()))],
                "pvc": objects["pvc"],
                "pv": objects["pv"],
                "identity": {
                    "apiVersion": "authentication.k8s.io/v1",
                    "kind": "SelfSubjectReview",
                    "status": {"userInfo": {"username": m.dispatcher_identity}},
                },
                "events": {"kind": "EventList", "items": lists["Event"]["items"]},
                "rbac_inventory": lists["Role"]["items"] + lists["RoleBinding"]["items"],
                "probes": probes,
            }
            # Bracket observations; resourceVersions are opaque, never numeric clocks.
            for key in keys:
                if success(self.client, key, label="recheck:" + key) != objects[key]:
                    raise CollectionError("stale")
            for kind, listing in lists.items():
                if success(self.client, "list-" + kind, label="recheck-list:" + kind) != listing:
                    raise CollectionError("stale")
            if self.clock() >= deadline:
                raise CollectionError("timeout")
            RawEvidenceInspector(client=None, now=self.now).validate(
                plan, snapshot, not_before=started
            )
            category = "pass"
        except CollectionError as exc:
            category = exc.category
        except Exception:
            category = "contradictory"
        payload = encoded(
            {
                "version": 1,
                "collector": self.client.binding.identity,
                "bindings": [
                    client.binding.model_dump(mode="json")
                    for client in (self.client, self.dispatcher, self.workload)
                ],
                "server": self.client.binding.server,
                "ca_sha256": self.client.binding.ca_sha256,
                "cluster_uid": self.client.binding.cluster_uid,
                "manifest_digest": hashlib.sha256(canonical(m)).hexdigest(),
                "plan_digest": plan.digest,
                "org_id": m.org_id,
                "project_id": m.project_id,
                "package": m.package_digest,
                "operation": m.operation_id,
                "nonce": m.nonce,
                "started_at": started.isoformat(),
                "completed_at": self.now().isoformat(),
                "expires_at": m.expires_at,
                "result": category,
                "witness_source": "fake" if self.allow_fake_witnesses else "independent-daemon",
                "records": records,
                "snapshot": snapshot,
            }
        )
        digest = hashlib.sha256(payload).hexdigest()
        signature = hmac.new(self._key, b"tessera-collector-v1\0" + payload, "sha256").hexdigest()
        with self.journal.connect() as db:
            db.execute(
                "INSERT INTO collector_evidence VALUES (?,?,?,?)",
                (m.nonce, payload, digest, signature),
            )
        if category != "pass":
            raise CollectionError(category)
        return CollectedEvidence(payload, digest, signature, self._seal)

    def unwrap(self, evidence, plan):
        # No imported signed claims. Only the exact in-process collector result is
        # eligible, and even a failed use consumes it durably before validation.
        if not hasattr(self, "_nonce"):
            raise CollectionError("stale")
        self.journal.consume(self._nonce)
        try:
            if type(evidence) is not CollectedEvidence or evidence._issuer is not self._seal:
                raise ValueError
            signature = hmac.new(
                self._key, b"tessera-collector-v1\0" + evidence.payload, "sha256"
            ).hexdigest()
            if (
                not hmac.compare_digest(signature, evidence.signature)
                or hashlib.sha256(evidence.payload).hexdigest() != evidence.digest
            ):
                raise ValueError
            body = json.loads(evidence.payload)
            if body["plan_digest"] != plan.digest or body["result"] != "pass":
                raise ValueError
            if set(body["records"].get("witnesses", {})) != {
                "cni",
                "quota",
                "watchdog",
                "admission",
            }:
                raise ValueError
            if not self.allow_fake_witnesses:
                self.witness.revalidate(plan, body["records"]["witnesses"], now=self.now())
            RawEvidenceInspector(client=None, now=self.now).validate(
                plan, body["snapshot"], not_before=stamp(body["started_at"])
            )
            return body["snapshot"]
        except Exception:
            raise CollectionError("stale") from None
