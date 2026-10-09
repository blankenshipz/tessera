"""Offline authenticated-boundary fixtures. No real API, token, CNI or workload effects."""

import base64
import copy
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_capability_activation import NOW, candidate  # noqa: F401
from test_capability_kubernetes_runner import bind_plan, raw_fixture

from tessera_control.store import canonical_digest
from tessera_worker.capability_activation import ActivationJournal, CollectorActivationGate
from tessera_worker.capability_collector import (
    CollectorJournal,
    KubernetesEvidenceCollector,
    catalogue,
)
from tessera_worker.capability_collector_cli import main
from tessera_worker.capability_effects import BackendBlocked
from tessera_worker.capability_inspection import RBAC_DENIALS, RawEvidenceInspector, probe_requests
from tessera_worker.capability_kube_client import (
    APIResponse,
    AuthenticatedKubernetesClient,
    ClientBinding,
    CollectionError,
    Credentials,
    HTTPSKubernetesTransport,
    decode,
    encoded,
    load_credentials,
)
from tessera_worker.capability_kubernetes import (
    FakeKubernetesCapabilityClient,
    KubernetesCapabilityRunner,
    RunnerJournal,
)
from tessera_worker.capability_runner_contract import PackageVerifier
from tessera_worker.capability_witness_fake import FakeRuntimeWitnesses


class FakeAPI:
    def __init__(self, responses):
        self.responses, self.calls, self.fault = responses, [], None

    def send(self, request, *, deadline, limit):
        self.calls.append(request)
        status, body = copy.deepcopy(self.responses[(request.method, request.path, request.body)])
        if self.fault:
            status, body = self.fault(request, status, body, len(self.calls))
        return APIResponse(status, encoded(body), f"request-{len(self.calls)}")


@pytest.fixture
def collection(candidate, tmp_path):  # noqa: F811
    ca = b"synthetic-ca-not-a-certificate"
    cluster = canonical_digest(
        ["https://127.0.0.1:6443", hashlib.sha256(ca).hexdigest(), "cluster-uid"]
    )
    plan, pvc_spec = bind_plan(
        candidate.manifest.model_copy(
            update={
                "cluster_digest": cluster,
                "issued_at": (NOW - timedelta(seconds=4)).isoformat(),
            }
        )
    )
    snapshot = raw_fixture(plan, pvc_spec)
    m = plan.activation
    objects = {f"object-{i}": obj for i, obj in enumerate(snapshot["objects"])}
    objects |= {
        "pvc": snapshot["pvc"],
        "pv": snapshot["pv"],
        "cluster": {
            "kind": "Namespace",
            "metadata": {"name": "kube-system", "uid": "cluster-uid"},
        },
    }
    for key, obj in objects.items():
        obj["metadata"].setdefault("uid", key + "-uid")
        obj["metadata"].update(
            resourceVersion="123", creationTimestamp=(NOW - timedelta(days=1)).isoformat()
        )
    clients, transports = [], []
    for identity in (
        "system:serviceaccount:" + m.namespace + ":collector",
        m.dispatcher_identity,
        f"system:serviceaccount:{m.namespace}:{m.service_account}",
    ):
        binding = ClientBinding(
            mode="kubeconfig",
            context="fixture",
            server="https://127.0.0.1:6443",
            ca_sha256=hashlib.sha256(ca).hexdigest(),
            cluster_uid="cluster-uid",
            namespace=m.namespace,
            namespace_uid=m.namespace_uid,
            org_id=m.org_id,
            project_id=m.project_id,
            plan_digest=plan.digest,
            identity=identity,
            pv_name="fixture-volume",
            timeout_seconds=5,
            max_seconds=120,
            max_requests=128,
            max_response_bytes=2 * 1024 * 1024,
        )
        responses = {}
        for key, request in catalogue(plan, binding).items():
            status = 200
            if key in objects:
                body = objects[key]
            elif key == "identity":
                body = {
                    "apiVersion": "authentication.k8s.io/v1",
                    "kind": "SelfSubjectReview",
                    "status": {"userInfo": {"username": identity}},
                }
            elif key.startswith("list-"):
                kind = key.removeprefix("list-")
                body = {
                    "kind": kind + "List",
                    "metadata": {"resourceVersion": "123"},
                    "items": [o for o in snapshot["objects"] if o["kind"] == kind],
                }
            elif key == "rbac-positive" or key in RBAC_DENIALS:
                allowed = key == "rbac-positive"
                body = {
                    "kind": "SelfSubjectAccessReview",
                    "status": {"allowed": allowed, "denied": not allowed},
                }
            else:
                if key.endswith("-positive"):
                    status, body = 201, probe_requests(plan)[key]["object"]
                else:
                    status, body = 403, snapshot["probes"][key]["response"]
            responses[(request.method, request.path, request.body)] = copy.deepcopy((status, body))
        transport = FakeAPI(responses)
        clients.append(
            AuthenticatedKubernetesClient(
                binding=binding, transport=transport, plan=plan, monotonic=lambda: 10.0
            )
        )
        transports.append(transport)
    journal = CollectorJournal(tmp_path / "collector")
    witness = FakeRuntimeWitnesses(snapshot, now=lambda: NOW)
    collector = KubernetesEvidenceCollector(
        client=clients[0],
        dispatcher=clients[1],
        workload=clients[2],
        witness=witness,
        journal=journal,
        key=b"c" * 32,
        resource_pins={
            k: [v["metadata"]["uid"], v["metadata"]["resourceVersion"]] for k, v in objects.items()
        },
        now=lambda: NOW,
        monotonic=lambda: 10.0,
        allow_fake_witnesses=True,
    )
    verifier = PackageVerifier(
        authority=lambda _: True,
        identity=(m.org_id, m.project_id, m.worker_id),
        approved_plan_digest=plan.digest,
        now=lambda: NOW,
    )
    yield SimpleNamespace(**locals())
    journal.close()


def collect(h):
    return h.collector.collect(h.plan, probe_requests(h.plan))


def receipt(h):
    with h.journal.connect() as db:
        return json.loads(db.execute("SELECT payload FROM collector_evidence").fetchone()[0])


def test_all_pass_canonical_authenticated_and_one_use(collection):
    h = collection
    evidence = collect(h)
    assert evidence.payload == encoded(json.loads(evidence.payload))
    assert hashlib.sha256(evidence.payload).hexdigest() == evidence.digest
    body = receipt(h)
    assert body["result"] == "pass"
    assert body["cluster_uid"] == "cluster-uid"
    assert body["org_id"] == h.m.org_id
    assert body["records"]["recheck:pv"]["body"]["metadata"]["resourceVersion"] == "123"
    assert all(
        "audit_id" in v and "observed_at" in v
        for k, v in body["records"].items()
        if k not in {"runtime", "witnesses"}
    )
    assert h.collector.unwrap(evidence, h.plan)["nonce"] == h.m.nonce
    with pytest.raises(CollectionError, match="stale"):
        h.collector.unwrap(evidence, h.plan)
    with pytest.raises(CollectionError, match="stale"):
        collect(h)
    assert all(
        r.method == "GET" or "dryRun=All" in r.path or "selfsubject" in r.path
        for t in h.transports
        for r in t.calls
    )


def test_activation_exact_next_manifest_and_revocation(collection, tmp_path):
    h = collection
    journal = ActivationJournal(tmp_path / "collected-activation")
    try:
        gate = CollectorActivationGate(collector=h.collector, verifier=h.verifier, journal=journal)
        verified = h.verifier.verify(h.candidate.package)
        result = gate.authorize(h.plan, verified)
        assert len(result["evidence_digest"]) == 64
        with pytest.raises(BackendBlocked):
            gate.authorize(h.plan, verified)
        changed = h.plan.model_copy(
            update={
                "activation": h.m.model_copy(
                    update={"operation_id": "different", "nonce": "f" * 64}
                )
            }
        )
        with pytest.raises(BackendBlocked):
            gate.authorize(changed, verified)
    finally:
        journal.close()


@pytest.mark.parametrize(
    "field,value",
    [("operation_id", "other"), ("nonce", "f" * 64), ("org_id", "other"), ("project_id", "other")],
)
def test_other_manifest_cannot_use_result(collection, field, value):
    h = collection
    evidence = collect(h)
    changed = h.plan.model_copy(update={"activation": h.m.model_copy(update={field: value})})
    with pytest.raises(CollectionError):
        h.collector.unwrap(evidence, changed)
    with pytest.raises(CollectionError):
        h.collector.unwrap(evidence, h.plan)


@pytest.mark.parametrize("fault", ["signature", "issuer", "payload", "stale"])
def test_imported_tampered_or_stale_result_blocked(collection, fault):
    h = collection
    evidence = collect(h)
    if fault == "signature":
        evidence = replace(evidence, signature="0" * 64)
    elif fault == "issuer":
        evidence = replace(evidence, _issuer=object())
    elif fault == "payload":
        evidence = replace(evidence, payload=evidence.payload + b" ")
    else:
        h.collector.now = lambda: NOW + timedelta(seconds=6)
    with pytest.raises(CollectionError):
        h.collector.unwrap(evidence, h.plan)
    with pytest.raises(CollectionError):
        collect(h)


@pytest.mark.parametrize("index", range(3))
@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_auth_denial_and_unavailable_never_pass(collection, index, status):
    h = collection
    h.transports[index].fault = lambda r, s, b, n: (status, {"kind": "Status"})
    with pytest.raises(CollectionError):
        collect(h)
    assert receipt(h)["result"] in {"auth-denied", "unavailable"}
    with pytest.raises(CollectionError):
        collect(h)


@pytest.mark.parametrize(
    "fault",
    [
        "uid",
        "version",
        "namespace",
        "tenant",
        "cluster",
        "missing",
        "drift",
        "list-drift",
        "pagination",
        "deployment",
        "pod",
        "job",
        "event",
    ],
)
def test_raw_object_drift_partial_and_unexpected_workloads(collection, fault):
    h = collection
    counts = {}

    def mutate(r, s, b, n):
        counts[r.path] = counts.get(r.path, 0) + 1
        if r.path.endswith("/namespaces/" + h.m.namespace):
            if fault == "uid":
                b["metadata"]["uid"] = "wrong"
            if fault == "version":
                b["metadata"]["resourceVersion"] = "122"
            if fault == "namespace":
                b["metadata"]["name"] = "other"
            if fault == "tenant":
                b["metadata"]["labels"]["tessera.io/tenant"] = "other"
            if fault == "missing":
                return 404, {"kind": "Status"}
            if fault == "drift" and counts[r.path] == 2:
                b["metadata"]["resourceVersion"] = "124"
        if fault == "cluster" and r.path.endswith("kube-system"):
            b["metadata"]["uid"] = "other-cluster"
        if "?limit=" in r.path:
            if fault == "pagination":
                b["metadata"]["continue"] = "more"
            if fault == "list-drift" and counts[r.path] == 2:
                b["metadata"]["resourceVersion"] = "124"
            if b["kind"].lower() == fault + "list":
                b["items"].append({"kind": "Unexpected"})
        return s, b

    h.transports[0].fault = mutate
    with pytest.raises(CollectionError):
        collect(h)
    assert receipt(h)["result"] != "pass"


@pytest.mark.parametrize(
    "fault",
    [
        "allowed-negative",
        "denied-positive",
        "unknown",
        "evaluation-error",
        "dryrun-denied",
        "dryrun-allowed",
        "malformed-admission",
        "changed-command",
    ],
)
def test_rbac_and_server_side_admission_fail_closed(collection, fault):
    h = collection

    def mutate(r, s, b, n):
        if "selfsubjectaccessreviews" in r.path:
            allowed = b["status"]["allowed"]
            if fault == "allowed-negative" and not allowed:
                b["status"] = {"allowed": True, "denied": False}
            if fault == "denied-positive" and allowed:
                b["status"] = {"allowed": False, "denied": True}
            if fault == "unknown":
                b["status"] = {"allowed": False, "denied": False}
            if fault == "evaluation-error":
                b["status"]["evaluationError"] = "unavailable"
        if "dryRun=All" in r.path:
            if fault == "dryrun-denied" and s == 201:
                return 403, {"kind": "Status", "code": 403}
            if fault == "dryrun-allowed" and s == 403:
                return 201, decode(r.body)
            if fault == "malformed-admission":
                return 403, {"allowed": False}
            if fault == "changed-command" and s == 201 and b["kind"] == "Job":
                b["spec"]["template"]["spec"]["containers"][0]["command"] = ["/bin/sh"]
        return s, b

    h.transports[1].fault = mutate
    with pytest.raises(CollectionError):
        collect(h)
    assert receipt(h)["result"] != "pass"


@pytest.mark.parametrize(
    "key",
    [
        "network-allowed",
        "network-denied-public",
        "network-denied-control",
        "tls-san-issuer",
        "tls-wrong-certificate",
        "watchdog-health",
        "watchdog-revoke",
        "watchdog-broker-loss",
        "output-byte-exhaustion",
        "output-inode-exhaustion",
        "ephemeral-exhaustion",
        "resource-quota",
        "rbac-exec",
        "image-command",
        "readonly-nonroot",
    ],
)
@pytest.mark.parametrize("fault", ["unknown", "stale", "wrong-nonce", "wrong-subject"])
def test_runtime_evidence_failure_blocks_activation(collection, key, fault):
    h = collection
    probe = h.snapshot["probes"][key]
    if fault == "unknown":
        probe["response"] = {"passed": True, "signature": "operator-claim"}
    elif fault == "stale":
        probe["observed_at"] = (NOW - timedelta(seconds=6)).isoformat()
    elif fault == "wrong-nonce":
        probe["nonce"] = "f" * 64
    else:
        probe["subject"] = "f" * 64
    with pytest.raises(BackendBlocked):
        RawEvidenceInspector(client=h.collector, now=lambda: NOW).inspect(h.plan)
    assert receipt(h)["result"] != "pass"
    with pytest.raises(CollectionError):
        collect(h)


def test_no_witness_records_partial_not_pass(collection):
    h = collection
    h.collector.witness = None
    with pytest.raises(CollectionError, match="unavailable"):
        collect(h)
    assert receipt(h)["records"]
    assert receipt(h)["snapshot"] is None


@pytest.mark.parametrize(
    "fault", ["size", "deadline", "request-budget", "malformed", "audit-replay", "arbitrary-path"]
)
def test_client_bounds_and_safe_errors(collection, fault):
    h = collection
    c = h.clients[0]
    key = "cluster"
    if fault == "size":
        h.transports[0].fault = lambda r, s, b, n: (s, {"padding": "x" * (2 * 1024 * 1024)})
    elif fault == "deadline":
        h.transports[0].fault = lambda r, s, b, n: (setattr(c, "clock", lambda: 20) or s, b)
    elif fault == "request-budget":
        c._count = 128
    elif fault == "malformed":
        c.transport.send = lambda *a, **k: APIResponse(200, b'{"x":1,"x":2}', "id")
    elif fault == "audit-replay":
        c._audit_ids.add("request-1")
    else:
        key = "/api/v1/namespaces/control/secrets"
    with pytest.raises(CollectionError) as error:
        c.request(key, deadline=15)
    assert str(error.value).startswith("Kubernetes evidence: ")
    assert len(h.transports[0].calls) <= 1


@pytest.mark.parametrize("fault", ["context", "server", "ca", "exec", "namespace", "ambient"])
def test_explicit_kubeconfig_binding_rejects_wrong_target(collection, tmp_path, fault, caplog):
    h = collection
    token = "synthetic-token-never-log"
    config = {
        "current-context": "ambient",
        "contexts": [
            {
                "name": "fixture",
                "context": {"cluster": "cluster", "user": "user", "namespace": h.m.namespace},
            }
        ],
        "clusters": [
            {
                "name": "cluster",
                "cluster": {
                    "server": h.clients[0].binding.server,
                    "certificate-authority-data": base64.b64encode(h.ca).decode(),
                },
            }
        ],
        "users": [{"name": "user", "user": {"token": token}}],
    }
    if fault == "context":
        config["contexts"][0]["name"] = "other"
    elif fault == "server":
        config["clusters"][0]["cluster"]["server"] = "https://127.0.0.2:6443"
    elif fault == "ca":
        config["clusters"][0]["cluster"]["certificate-authority-data"] = base64.b64encode(
            b"other"
        ).decode()
    elif fault == "exec":
        config["users"][0]["user"]["exec"] = {"command": "cloud-login"}
    elif fault == "namespace":
        config["contexts"][0]["context"]["namespace"] = "control"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(CollectionError) as error:
        load_credentials(h.clients[0].binding, kubeconfig=None if fault == "ambient" else path)
    assert token not in str(error.value) + caplog.text
    assert not any(t.calls for t in h.transports)


def test_credentials_no_repr_and_pinned_ca(collection, tmp_path, caplog):
    h = collection
    token = "synthetic-token-never-log"
    creds = Credentials(token, h.ca)
    assert token not in repr(creds)
    with pytest.raises(CollectionError):
        HTTPSKubernetesTransport(h.clients[0].binding, Credentials(token, b"wrong-ca"))
    token_path, ca_path = tmp_path / "token", tmp_path / "ca"
    token_path.write_text(token)
    ca_path.write_bytes(h.ca)
    binding = h.clients[0].binding.model_copy(update={"mode": "service-account"})
    assert load_credentials(binding, token_file=token_path, ca_file=ca_path).token == token
    h.transports[0].fault = lambda r, s, b, n: (s, {"message": "Bearer " + token})
    with pytest.raises(CollectionError):
        collect(h)
    assert token not in json.dumps(receipt(h)) + caplog.text


def test_explicit_context_ignores_ambient_and_rejects_ambiguous_yaml(collection, tmp_path):
    h = collection
    config = {
        "current-context": "never-selected",
        "contexts": [
            {
                "name": "fixture",
                "context": {
                    "cluster": "cluster",
                    "user": "user",
                    "namespace": h.m.namespace,
                },
            }
        ],
        "clusters": [
            {
                "name": "cluster",
                "cluster": {
                    "server": h.clients[0].binding.server,
                    "certificate-authority-data": base64.b64encode(h.ca).decode(),
                },
            }
        ],
        "users": [{"name": "user", "user": {"token": "synthetic-token"}}],
    }
    path = tmp_path / "explicit-config"
    path.write_text(json.dumps(config))
    assert load_credentials(h.clients[0].binding, kubeconfig=path).token == "synthetic-token"
    for content in ("contexts: []\ncontexts: []\n", "context: &ref {}\ncontexts: *ref\n"):
        path.write_text(content)
        with pytest.raises(CollectionError):
            load_credentials(h.clients[0].binding, kubeconfig=path)


def test_cli_disabled_and_schema_do_not_open_credentials(monkeypatch, capsys):
    monkeypatch.setattr(
        "tessera_worker.capability_collector_cli.private_read",
        lambda *a: pytest.fail("credential access"),
    )
    assert main(["--config", "/nonexistent/private/config"]) == 0
    assert "disabled" in capsys.readouterr().out
    assert main(["--schema"]) == 0
    assert "CollectorConformanceConfig" in capsys.readouterr().out


def test_revoked_authority_burns_before_collection(collection, tmp_path):
    h = collection
    verified = h.verifier.verify(h.candidate.package)
    h.verifier.authority = lambda _: False
    journal = ActivationJournal(tmp_path / "revoked-gate")
    try:
        gate = CollectorActivationGate(collector=h.collector, verifier=h.verifier, journal=journal)
        with pytest.raises(BackendBlocked):
            gate.authorize(h.plan, verified)
        h.verifier.authority = lambda _: True
        with pytest.raises(BackendBlocked):
            gate.authorize(h.plan, verified)
        assert not any(t.calls for t in h.transports)
    finally:
        journal.close()


def test_runner_uses_collector_before_exact_fake_job_create(collection, tmp_path):
    h = collection
    client = FakeKubernetesCapabilityClient(
        snapshot={},
        heartbeat={
            "identity": h.m.watchdog.identity,
            "certificate_sha256": h.m.watchdog.certificate_sha256,
            "nonce": h.m.nonce,
            "job_uid": "fake-job-uid",
            "sequence": 1,
            "observed_at": NOW.isoformat(),
            "deadline": (NOW + timedelta(seconds=3)).isoformat(),
        },
    )
    journal = RunnerJournal(tmp_path / "collector-runner")
    try:
        engine = KubernetesCapabilityRunner(
            client=client,
            journal=journal,
            verifier=h.verifier,
            claim=lambda *_: True,
            now=lambda: NOW,
            evidence_collector=h.collector,
        )
        verified = h.verifier.verify(h.candidate.package)
        assert engine.submit(h.plan, verified) == "pending"
        assert client.calls == ["create", "arm", "heartbeat", "unsuspend"]
        with pytest.raises(BackendBlocked):
            engine.submit(h.plan, verified)
        assert client.calls.count("create") == 1
    finally:
        journal.close()


def test_failed_collection_nonce_retained_after_restart(collection):
    h = collection
    h.collector.witness = None
    with pytest.raises(CollectionError):
        collect(h)
    h.journal.close()
    reopened = CollectorJournal(h.journal.root)
    try:
        with pytest.raises(CollectionError, match="stale"):
            reopened.begin(h.plan)
        with reopened.connect() as db:
            assert db.execute("SELECT count(*) FROM collector_evidence").fetchone()[0] == 1
            with pytest.raises(Exception, match="retained collector custody"):
                db.execute("DELETE FROM collector_attempts")
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "token-echo",
        "certificate-echo",
        "timeout",
        "oversize",
        "redirect",
        "chunked",
        "wrong-peer",
        "wrong-ca",
        "tls-denied",
    ],
)
def test_real_transport_offline_socket_boundary(collection, monkeypatch, caplog, fault):
    import ssl

    from tessera_worker.capability_kube_client import APIRequest

    h = collection
    token = "never-disclose-synthetic-token"
    clock = [10.0]
    body = b'{"kind":"Namespace"}'
    if fault == "token-echo":
        # Escaped echoes must be rejected as well as literal Bearer headers.
        body = b'{"message":"' + b"".join(f"\\u{ord(c):04x}".encode() for c in token) + b'"}'
    elif fault == "certificate-echo":
        body = encoded({"message": h.ca.decode()})
    headers = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nAudit-Id: id-1\r\n"
        + f"Content-Length: {len(body) if fault != 'oversize' else 9999999}\r\n".encode()
    )
    if fault == "redirect":
        headers += b"Location: https://example.invalid/\r\n"
    if fault == "chunked":
        headers += b"Transfer-Encoding: chunked\r\n"

    class FakeSocket:
        data = headers + b"\r\n" + body
        sent = b""
        closed = False
        connects = 0

        def settimeout(self, seconds):
            assert 0 < seconds <= 5

        def connect(self, address):
            assert address == ("127.0.0.1", 6443)
            self.connects += 1

        def getpeername(self):
            return ("127.0.0.2" if fault == "wrong-peer" else "127.0.0.1", 6443)

        def sendall(self, data):
            self.sent += data

        def recv(self, size):
            if fault == "timeout":
                clock[0] = 16
            result, self.data = self.data[:size], self.data[size:]
            return result

        def close(self):
            self.closed = True

    sock = FakeSocket()

    class FakeTLS:
        def __init__(self, protocol):
            assert protocol == ssl.PROTOCOL_TLS_CLIENT

        def load_verify_locations(self, *, cadata):
            assert cadata == h.ca.decode()

        def wrap_socket(self, raw, *, server_hostname):
            assert server_hostname == "127.0.0.1"
            if fault == "tls-denied":
                raise ssl.SSLError("never-disclose-synthetic-token")
            return raw

    monkeypatch.setattr("tessera_worker.capability_kube_client.socket.socket", lambda *a: sock)
    monkeypatch.setattr("tessera_worker.capability_kube_client.ssl.SSLContext", FakeTLS)
    credentials = Credentials(token, b"wrong" if fault == "wrong-ca" else h.ca)
    if fault == "none":
        transport = HTTPSKubernetesTransport(
            h.clients[0].binding, credentials, monotonic=lambda: clock[0]
        )
        reply = transport.send(
            APIRequest("GET", "/api/v1/namespaces/kube-system"), deadline=15, limit=1024
        )
        assert reply.status == 200 and token not in repr(reply)
        assert token.encode() in sock.sent  # authenticated wire, not retained evidence
    else:
        with pytest.raises(CollectionError) as error:
            transport = HTTPSKubernetesTransport(
                h.clients[0].binding, credentials, monotonic=lambda: clock[0]
            )
            transport.send(
                APIRequest("GET", "/api/v1/namespaces/kube-system"), deadline=15, limit=1024
            )
        assert token not in str(error.value)
    assert token not in caplog.text
    assert sock.connects <= 1
    if fault != "wrong-ca":
        assert sock.closed
