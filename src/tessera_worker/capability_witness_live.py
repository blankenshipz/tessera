"""Docker Desktop Kubernetes adapters composed ONLY in independent witness services.

API admission is implemented here. Node effects use the explicitly pinned native
supervisor channel: no host exec, kubeconfig discovery, report import, downloaded
image or fallback to requested limits. Unsupported native facilities block BEFORE
fixture creation. Other platforms can implement NativeDriver without changing the
collector's evidence or activation boundary.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace

from .capability_activation import stamp
from .capability_collector import resource_path
from .capability_inspection import RawEvidenceInspector, negative_jobs, raw_object
from .capability_kube_client import APIRequest, CollectionError, decode, encoded
from .capability_runner_contract import RunnerPlan
from .capability_witness import Cleanup, Resource, exact, require, safe_payload
from .capability_witness_native import (
    cgroup_observation,
    filesystem_observation,
    network_observation,
    watchdog_observation,
)
from .capability_witness_target import LiveWitnessTarget


class DockerDesktopNodeSensors:
    """NodeSensors implementation for an explicit API + native supervisor pair.

    The service manager must bound this daemon and native supervisor out of
    process. The native driver owns fixture creates/kill/cleanup in its own
    journal; this adapter owns actual admission creates in `journal`. Both burn
    intent before effects and refuse nonce reuse. Credentials stay in services.
    """

    def __init__(
        self,
        *,
        target,
        plan,
        api_transport,
        supervisor,
        journal,
        monotonic=time.monotonic,
        now=None,
    ):
        self.target = LiveWitnessTarget.model_validate_json(target.model_dump_json())
        self.plan = RunnerPlan.model_validate_json(plan.model_dump_json())
        p, m, pin = self.target.profile, self.plan.activation, self.target.pin
        require(
            p.plan_digest == self.plan.digest
            and p.cluster_digest == m.cluster_digest
            and p.image == m.image
            and pin.namespace not in {m.namespace, m.control_namespace}
            and pin.identity
            not in {
                m.dispatcher_identity,
                f"system:serviceaccount:{m.namespace}:{m.service_account}",
            }
        )
        self.api, self.supervisor, self.journal = api_transport, supervisor, journal
        self.clock, self.now = monotonic, now or (lambda: datetime.now(UTC))
        self.calls, self.bytes, self.audit_ids, self.api_records = 0, 0, set(), []
        self.used = False
        with journal.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS live_adapter_attempts (
                    nonce TEXT PRIMARY KEY, challenge TEXT UNIQUE NOT NULL, target TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS live_adapter_api (
                    nonce TEXT NOT NULL, sequence INTEGER NOT NULL, request BLOB NOT NULL,
                    PRIMARY KEY(nonce,sequence));
                CREATE TABLE IF NOT EXISTS live_adapter_replies (
                    nonce TEXT NOT NULL, sequence INTEGER NOT NULL, response BLOB NOT NULL,
                    PRIMARY KEY(nonce,sequence));
            """)
            for table in ("live_adapter_attempts", "live_adapter_api", "live_adapter_replies"):
                for action in ("UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action} BEFORE {action} "
                        f"ON {table} BEGIN SELECT RAISE(ABORT,'retained native evidence'); END"
                    )

    def _api(self, h, method, path, body=None, *, deadline):
        self.calls += 1
        p = self.target.profile
        require(self.calls + self.supervisor.sequence <= p.max_calls and self.clock() < deadline)
        request = {"method": method, "path": path, "body": body or {}}
        data = safe_payload(request)
        with self.journal.connect() as db:
            db.execute(
                "INSERT INTO live_adapter_api VALUES (?,?,?)",
                (h.request.run_nonce, self.calls, data),
            )
        try:
            reply = self.api.send(
                APIRequest(method, path, encoded(body) if body else b""),
                deadline=deadline,
                limit=p.max_bytes - self.bytes,
            )
            self.bytes += len(reply.body)
            require(self.clock() < deadline and self.bytes <= p.max_bytes)
            require(reply.audit_id and reply.audit_id not in self.audit_ids)
            self.audit_ids.add(reply.audit_id)
            value = decode(reply.body)
            safe_payload(value)
            record = {
                "request": request,
                "response": value,
                "status": reply.status,
                "audit_id": reply.audit_id,
                "observed_at": self.now().isoformat(),
            }
            require(len(encoded(self.api_records + [record])) <= p.max_bytes)
            with self.journal.connect() as db:
                db.execute(
                    "INSERT INTO live_adapter_replies VALUES (?,?,?)",
                    (h.request.run_nonce, self.calls, encoded(record)),
                )
            self.api_records.append(record)
            return record
        except Exception:
            raise CollectionError("unavailable") from None

    def _get(self, h, path, *, deadline):
        r = self._api(h, "GET", path, deadline=deadline)
        require(r["status"] == 200)
        return r["response"]

    def _native(self, h, operation, arguments=None, *, deadline):
        require(self.calls + self.supervisor.sequence < self.target.profile.max_calls)
        result = self.supervisor.call(h.request, operation, arguments or {}, deadline=deadline)
        require(self.bytes + self.supervisor.bytes <= self.target.profile.max_bytes)
        return result

    def prepare(self, request, *, deadline):
        require(not self.used)
        self.used = True
        p, m, pin = self.target.profile, self.plan.activation, self.target.pin
        require(
            request.kind == pin.kind
            and request.inputs["plan"] == self.plan.model_dump(mode="json")
            and request.run_nonce == m.nonce
            and request.plan_digest == self.plan.digest
            and request.cluster_digest == p.cluster_digest
            and stamp(request.started_at) <= self.now() < stamp(request.deadline)
            and 0 < deadline - self.clock() <= p.max_seconds
        )
        with self.journal.connect() as db:
            db.execute(
                "INSERT INTO live_adapter_attempts VALUES (?,?,?)",
                (request.run_nonce, request.challenge, p.digest),
            )
        h = SimpleNamespace(
            request=request,
            boot_id=p.boot_id,
            resources=[],
            local=[],
            fixtures={},
            snapshots=0,
            dry=None,
        )
        # All preparation is read-only, including native preflight. No create may
        # occur until BOTH independently authenticated inventories agree.
        identity = self._api(
            h,
            "POST",
            "/apis/authentication.k8s.io/v1/selfsubjectreviews",
            {"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview"},
            deadline=deadline,
        )
        require(
            identity["status"] in {200, 201}
            and identity["response"]["status"]["userInfo"]["username"] == pin.identity
        )
        for name, uid in (
            ("kube-system", self.target.api.binding.cluster_uid),
            (m.namespace, m.namespace_uid),
            (pin.namespace, pin.namespace_uid),
        ):
            obj = self._get(h, "/api/v1/namespaces/" + name, deadline=deadline)
            require(
                obj["kind"] == "Namespace"
                and obj["metadata"]["uid"] == uid
                and obj["metadata"]["name"] == name
                and not obj["metadata"].get("deletionTimestamp")
            )
        node = self._get(h, "/api/v1/nodes/" + p.node_name, deadline=deadline)
        require(
            node["metadata"]["uid"] == p.node_uid
            and node["status"]["nodeInfo"]["bootID"] == p.boot_id
            and node["status"]["nodeInfo"]["operatingSystem"] == "linux"
            and node["status"]["nodeInfo"]["containerRuntimeVersion"].startswith("containerd://")
            and node["status"]["nodeInfo"]["kubeletVersion"] == p.kubernetes_version
        )
        require(self._get(h, "/version", deadline=deadline)["gitVersion"] == p.kubernetes_version)
        sa = self._get(
            h,
            f"/api/v1/namespaces/{m.namespace}/serviceaccounts/{m.service_account}",
            deadline=deadline,
        )
        require(
            sa["metadata"]["uid"] == m.service_account_uid
            and not sa.get("automountServiceAccountToken", True)
        )
        native = self._native(h, "preflight", deadline=deadline)
        exact(
            native,
            "node_uid boot_id image binary_sha256 cri cgroup filesystem cni "
            "fixture_empty supervisor_process",
        )
        require(
            native["node_uid"] == p.node_uid
            and native["boot_id"] == p.boot_id
            and native["image"] == p.image
            and native["binary_sha256"] == p.fixture_binary_sha256
            and native["cri"] == p.cri_driver
            and native["fixture_empty"] is True
            and native["supervisor_process"] == "independent-external-service"
        )
        if pin.kind == "cni":
            require(p.cni_driver == native["cni"] == "native-policy-flow-v1")
            for namespace, accounts in (
                (m.namespace, p.probe_service_accounts),
                (pin.namespace, p.endpoint_service_accounts),
            ):
                for name, uid in accounts.values():
                    require(
                        f"system:serviceaccount:{namespace}:{name}"
                        not in {
                            pin.identity,
                            m.dispatcher_identity,
                            f"system:serviceaccount:{m.namespace}:{m.service_account}",
                        }
                    )
                    account = self._get(
                        h,
                        f"/api/v1/namespaces/{namespace}/serviceaccounts/{name}",
                        deadline=deadline,
                    )
                    require(
                        account["metadata"]["uid"] == uid
                        and account.get("automountServiceAccountToken") is False
                    )
        if pin.kind == "quota":
            require(
                p.storage_driver == native["filesystem"] == "linux-project-quota-v1"
                and native["cgroup"] == "cgroup2fs"
            )
        if pin.kind == "watchdog":
            require(
                m.watchdog.identity == pin.identity
                and m.watchdog.lease_seconds + m.watchdog.heartbeat_seconds
                < deadline - self.clock()
            )
        # prepare must not create: failures before returning a handle have nothing
        # to clean locally. Native create intents live in the external journal.
        require(self._native(h, "prepare", deadline=deadline) == {"ready": True, "resources": []})
        return h

    def _fixture(self, h, operation, arguments, *, deadline):
        result = self._native(h, operation, arguments, deadline=deadline)
        exact(result, "fixture resources")
        fixture = result["fixture"]
        require(type(fixture) is str and fixture and fixture not in h.fixtures)
        resources = [Resource.model_validate_json(encoded(r)) for r in result["resources"]]
        require(
            resources and len(h.resources) + len(resources) <= self.target.profile.max_resources
        )
        for r in resources:
            require(
                r.run_nonce == h.request.run_nonce
                and r.uid not in {x.uid for x in h.resources}
                and (r.namespace, r.namespace_uid)
                in {
                    (self.plan.activation.namespace, self.plan.activation.namespace_uid),
                    (self.target.pin.namespace, self.target.pin.namespace_uid),
                }
                and stamp(h.request.started_at) <= stamp(r.created_at) <= self.now()
            )
            if r.kind == "Pod":
                require(
                    r.node_uid == self.target.pin.node_uid
                    and r.identity is not None
                    and r.identity
                    not in {self.target.pin.identity, self.plan.activation.dispatcher_identity}
                )
            h.resources.append(r)
        h.fixtures[fixture] = resources
        return fixture

    def resources(self, h):
        return tuple(h.resources)

    def native_evidence(self, h):
        return {
            "target_digest": self.target.profile.digest,
            "profile": self.target.profile.model_dump(mode="json"),
            "records": self.supervisor.records,
            "api": self.api_records,
        }

    def ancillary_probes(self, h, requests, *, deadline):
        # Actual TLS/installer/RBAC controls remain mandatory. Never synthesize
        # them from the main probes or return expected_response as an observation.
        result = self._native(h, "ancillary", {"requests": requests}, deadline=deadline)
        require(set(result) == set(requests))
        return result

    def network_fixture(self, h, category, *, deadline):
        return self._fixture(
            h, "network-fixture", {"category": category, "packets": 1}, deadline=deadline
        )

    def network_policy(self, h, *, deadline):
        # A single bounded native stream waits for this exact policy generation.
        # It is not a retry of any packet probe; no convergence event means unknown.
        return self._native(h, "policy-convergence", deadline=deadline)

    def network_exchange(self, h, fixture, *, packets, deadline):
        require(fixture in h.fixtures and packets == 1)
        raw = self._native(
            h, "network-exchange", {"fixture": fixture, "packets": packets}, deadline=deadline
        )
        return network_observation(raw, h.request)

    def quota_fixture(self, h, *, bounds, deadline):
        q = self.plan.activation.quota
        require(
            bounds
            == {
                "allocation_bytes": q.memory_bytes + 1024 * 1024,
                "fork_attempts": self.plan.pids + 1,
                "cpu_milliseconds": 250,
                "output_bytes": q.output_bytes + 1,
                "output_inodes": q.output_inodes + 1,
                "ephemeral_bytes": q.ephemeral_bytes + 1,
            }
        )
        return self._fixture(h, "quota-fixture", {"bounds": bounds}, deadline=deadline)

    def _pod(self, h, fixture):
        pods = [r for r in h.fixtures[fixture] if r.kind == "Pod"]
        require(len(pods) == 1)
        return pods[0]

    def cgroup_snapshot(self, h, fixture, *, deadline):
        h.snapshots += 1
        require(h.snapshots <= 2)
        raw = self._native(
            h, "cgroup", {"fixture": fixture, "snapshot": h.snapshots}, deadline=deadline
        )
        return cgroup_observation(raw, self.plan, self.target.pin, self._pod(h, fixture))

    def pressure(self, h, fixture, *, bounds, deadline):
        return self._native(
            h, "pressure", {"fixture": fixture, "bounds": bounds}, deadline=deadline
        )

    def filesystem_sentinels(self, h, fixture, *, bounds, deadline):
        raw = self._native(
            h, "filesystem", {"fixture": fixture, "bounds": bounds}, deadline=deadline
        )
        exact(raw, "items")
        require(len(raw["items"]) == 3)
        return [filesystem_observation(r, self.plan) for r in raw["items"]]

    def namespace_access(self, h, fixture, *, deadline):
        return self._native(h, "access", {"fixture": fixture}, deadline=deadline)

    def termination(self, h, fixture, *, deadline):
        return self._native(h, "termination", {"fixture": fixture}, deadline=deadline)

    def watchdog_fixture(self, h, trigger, *, deadline):
        require(trigger in {"broker-suspended", "revoked"})
        return self._fixture(h, "watchdog-fixture", {"trigger": trigger}, deadline=deadline)

    def watchdog_challenge(self, h, fixture, *, deadline):
        require(
            self._native(h, "watchdog-arm", {"fixture": fixture}, deadline=deadline)
            == {"challenge": h.request.challenge, "sequence": 1, "armed": True}
        )

    def watchdog_fault(self, h, fixture, trigger, *, deadline):
        require(
            self._native(
                h, "watchdog-fault", {"fixture": fixture, "trigger": trigger}, deadline=deadline
            )
            == {"injected": trigger}
        )

    def watchdog_observe(self, h, fixture, *, deadline):
        raw = self._native(h, "watchdog-observe", {"fixture": fixture}, deadline=deadline)
        return watchdog_observation(
            raw, h.request, self.plan, self.target.pin, self._pod(h, fixture)
        )

    def admission_policy(self, h, *, deadline):
        result = {"policies": [], "bindings": []}
        for obj in self.plan.objects():
            kind = obj["kind"]
            if kind not in {"ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"}:
                continue
            got = self._get(
                h, resource_path(kind, None, obj["metadata"]["name"]), deadline=deadline
            )
            require(raw_object(got) == obj)
            meta = got["metadata"]
            require(meta["uid"] and meta["resourceVersion"] and type(meta["generation"]) is int)
            policy = kind == "ValidatingAdmissionPolicy"
            if policy:
                require(
                    got["spec"]["failurePolicy"] == "Fail"
                    and got["status"]
                    == {
                        "observedGeneration": meta["generation"],
                        "typeChecking": {"expressionWarnings": []},
                    }
                )
            else:
                require(got["spec"]["validationActions"] == ["Deny"])
            result["policies" if policy else "bindings"].append(got)
        return result

    def admission_dry_runs(self, h, *, deadline):
        result = {}
        for kind in ("Job", "Pod"):
            for key, job in {"positive": self.plan.job(), **negative_jobs(self.plan)}.items():
                obj = (
                    job
                    if kind == "Job"
                    else {
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "metadata": self.plan.metadata(self.plan.name),
                        "spec": job["spec"]["template"]["spec"],
                    }
                )
                r = self._api(
                    h,
                    "POST",
                    resource_path(kind, self.plan.activation.namespace)
                    + "?dryRun=All&fieldValidation=Strict",
                    obj,
                    deadline=deadline,
                )
                if key == "positive":
                    require(r["status"] == 201 and raw_object(r["response"]) == obj)
                else:
                    require(
                        r["status"] == 403
                        and r["response"]
                        == RawEvidenceInspector.expected_response(
                            self.plan, key, policy_uid="unused"
                        )
                    )
                result[f"{kind}:{key}"] = {
                    "request": obj,
                    "response": r["response"],
                    "status": r["status"],
                    "audit_id": r["audit_id"],
                    "field_validation": "Strict",
                    "dry_run": "All",
                }
        h.dry = result
        return result

    def admission_disposable(self, h, *, deadline):
        require(h.dry is not None)  # Never execute a dangerous negative control.
        result = []
        m, pin = self.plan.activation, self.target.pin
        for kind in ("Job", "Pod"):
            obj = h.dry[f"{kind}:positive"]["request"]
            path = resource_path(kind, m.namespace)
            absent = self._api(h, "GET", path + "/" + self.plan.name, deadline=deadline)
            require(absent["status"] == 404 and absent["response"].get("reason") == "NotFound")
            r = self._api(h, "POST", path + "?fieldValidation=Strict", obj, deadline=deadline)
            require(r["status"] == 201)
            meta = r["response"]["metadata"]
            # Record successful exact identity before comparing admission output;
            # mutated responses still need owned cleanup, never selector deletion.
            require(
                meta["name"] == self.plan.name
                and meta["namespace"] == m.namespace
                and meta["annotations"]["tessera.io/nonce"] == m.nonce
            )
            resource = Resource(
                kind=kind,
                name=meta["name"],
                uid=meta["uid"],
                namespace=m.namespace,
                namespace_uid=m.namespace_uid,
                run_nonce=m.nonce,
                resource_version=meta["resourceVersion"],
                created_at=meta["creationTimestamp"],
                node_uid=pin.node_uid if kind == "Pod" else None,
                identity=f"system:serviceaccount:{m.namespace}:{m.service_account}"
                if kind == "Pod"
                else None,
            )
            h.resources.append(resource)
            h.local.append(resource)
            require(raw_object(r["response"]) == obj)
            persisted = self._get(h, path + "/" + self.plan.name, deadline=deadline)
            require(persisted == r["response"])
            if kind == "Pod":
                identity = self._native(h, "pod-identity", {"uid": resource.uid}, deadline=deadline)
                require(
                    identity
                    == {
                        "uid": resource.uid,
                        "node_uid": pin.node_uid,
                        "identity": resource.identity,
                    }
                )
            result.append(
                {
                    "request": obj,
                    "response": r["response"],
                    "persisted": persisted,
                    "status": 201,
                    "audit_id": r["audit_id"],
                    "field_validation": "Strict",
                }
            )
        return result

    def cleanup(self, h, *, deadline):
        result, failures = [], False
        for r in reversed(h.local):
            try:
                path = resource_path(r.kind, r.namespace, r.name)
                ns = self._get(h, "/api/v1/namespaces/" + r.namespace, deadline=deadline)
                require(ns["metadata"]["uid"] == r.namespace_uid)
                got = self._get(h, path, deadline=deadline)["metadata"]
                require(
                    (got["uid"], got["resourceVersion"], got["name"], got["namespace"])
                    == (r.uid, r.resource_version, r.name, r.namespace)
                    and got["annotations"]["tessera.io/nonce"] == h.request.run_nonce
                )
                pre = {"uid": r.uid, "resourceVersion": r.resource_version}
                deleted = self._api(
                    h,
                    "DELETE",
                    path,
                    {
                        "apiVersion": "v1",
                        "kind": "DeleteOptions",
                        "preconditions": pre,
                        "propagationPolicy": "Foreground",
                    },
                    deadline=deadline,
                )
                require(deleted["status"] in {200, 202})
                readback = self._api(h, "GET", path, deadline=deadline)
                require(
                    readback["status"] == 404 and readback["response"].get("reason") == "NotFound"
                )
                result.append(
                    Cleanup(
                        resource=r,
                        preconditions=pre,
                        delete_status=deleted["status"],
                        readback={"kind": "Status", "code": 404, "reason": "NotFound"},
                        observed_at=self.now().isoformat(),
                    )
                )
            except Exception:
                failures = True
        try:
            # Native service cleans from ITS durable creates, not client inventory.
            raw = self._native(h, "cleanup", deadline=deadline)
            exact(raw, "resources cleanup")
            owned = [r for r in h.resources if r not in h.local]
            require(raw["resources"] == [r.model_dump(mode="json") for r in owned])
            remote = [Cleanup.model_validate_json(encoded(c)) for c in raw["cleanup"]]
            require({c.resource for c in remote} == set(owned) and len(remote) == len(owned))
            result.extend(remote)
        except Exception:
            failures = True
        if failures:
            raise CollectionError("unavailable")
        return tuple(result)


def compose_live_program(*, target, plan, journal):
    """Explicit daemon composition. Call only in the independently supervised service."""
    from .capability_kube_client import HTTPSKubernetesTransport, load_credentials, private_read
    from .capability_witness_native import NativeSupervisorRPC
    from .capability_witness_probes import (
        AdmissionWitness,
        CNIWitness,
        ExternalWatchdogWitness,
        QuotaWitness,
    )
    from .capability_witness_target import mtls_transport

    ref = target.api
    credentials = load_credentials(
        ref.binding, kubeconfig=ref.kubeconfig, token_file=ref.token_file, ca_file=ref.ca_file
    )
    channel = target.supervisor
    supervisor = NativeSupervisorRPC(
        target=target,
        transport=mtls_transport(channel.credentials, channel.tls),
        request_key=private_read(channel.request_key_file, 128),
        response_key=private_read(channel.response_key_file, 128),
    )
    sensors = DockerDesktopNodeSensors(
        target=target,
        plan=plan,
        api_transport=HTTPSKubernetesTransport(ref.binding, credentials),
        supervisor=supervisor,
        journal=journal,
    )
    return {
        "cni": CNIWitness,
        "quota": QuotaWitness,
        "watchdog": ExternalWatchdogWitness,
        "admission": AdmissionWitness,
    }[target.pin.kind](sensors)
