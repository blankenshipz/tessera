"""Isolated client/SQLite/real child-pipe checks, NOT live/backend auth acceptance.

HTTP responses and owner facts are named SYNTHETIC unit inputs. This selection never
imports the changing Store/API modules; the parent separately owns their API gate.
"""

import hashlib
import json
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import test_runtime_admission as admission_fixtures
import uvicorn

from tessera_control.runtime_binding_models import (
    AllocateRuntimeOperation,
    ClaimRuntimeOperation,
    IssueRuntimePolicy,
    ResolveRuntimeBinding,
    RuntimeBindingSnapshot,
    RuntimeClaimReceipt,
    RuntimeEnrollmentRecord,
    RuntimeFinancialRecord,
    RuntimeOperation,
    RuntimePolicyRecord,
)
from tessera_mcp.harness_binding import HarnessBinding, bind_runtime_profile
from tessera_mcp.runtime_admission import (
    CurrentLocalState,
    NativeTerminalPort,
    Pins,
    public_digest,
)
from tessera_mcp.runtime_binding_client import (
    BindingUnavailable,
    EnrolledAdmissionAdapter,
    EnrolledNativeBridge,
    IssuerInvocationState,
    NativeInvocationPipe,
    OwnerCheckExecution,
    RuntimeBindingClient,
    SQLiteInvocationGate,
    StoreInvocationGate,
)


class RegisteredSyntheticOwner:
    """Test-only owner boundary: immutable measured handle, not a request echo.

    Guard/quota/provider facts are synthetic, time/execution and stored resolution
    are actual. Handles are independently installed before admission requests.
    """

    def __init__(self):
        self.request_ref = hashlib.sha256(b"SYNTHETIC owned HTTP request").hexdigest()
        self.fingerprint = hashlib.sha256(b"SYNTHETIC final lowered HTTP wire").hexdigest()
        self.windows = {}
        self.execution = None
        self.revoked = False

    def verify_enrollment(self, connection, actor, request, scope, pins):
        from tessera_control.store import Forbidden

        if actor["role"] != "human" or pins != self.pins or scope["session_ref"] != self.session:
            raise Forbidden("registered_owner_enrollment_mismatch")

    def verify_allocation(self, connection, actor, snapshot, request):
        from tessera_control.store import Forbidden

        if (
            request["request_ref"] != self.request_ref
            or request["final_fingerprint"] != self.fingerprint
            or request["phase"] != "terminal-http"
            or request["native_phase"] != "native-http-fetch"
            or request["order_revision"] != 1
            or request["sequence"] != 1
            or request["pins"] != self.pins
        ):
            raise Forbidden("registered_immutable_native_handle_mismatch")

    def verify_claim(self, connection, actor, operation, request):
        from tessera_control.store import Forbidden

        window = self.windows.get(request["local_check_ref"])
        if self.revoked or window is None or request["quota_check_ref"] != window.quota_check_ref:
            raise Forbidden("registered_owner_check_missing_or_revoked")
        return window

    def execute(self, data, operation):
        from tessera_control.runtime_bindings import NativeCheckWindow
        from tessera_control.store import canonical_digest
        from tessera_mcp.runtime_admission import (
            CurrentLocalState,
            LocalChecks,
            Quota,
            TerminalExpectation,
        )

        now = datetime.now(UTC)
        expected = TerminalExpectation(
            "terminal-http",
            self.request_ref,
            self.fingerprint,
            1,
            1,
            operation.nonce,
            operation.ticket,
        )
        local = LocalChecks(
            evidence_class="completed-native-local-enforcement",
            provider="openai",
            model=data.policy.policy.model,
            **{
                key: "passed"
                for key in (
                    "oauth_method",
                    "owner_account_match",
                    "openai_only",
                    "approved_provider_model",
                    "chatgpt_endpoint",
                    "api_key_overrides_absent",
                    "custom_overrides_absent",
                    "no_paid_fallback",
                )
            },
        )
        quota = Quota(
            outcome="passed",
            snapshot_checked_at=now.isoformat(),
            checked_at=now.isoformat(),
            valid_until=(now + timedelta(seconds=20)).isoformat(),
        )
        local_ref, quota_ref = secrets.token_hex(32), secrets.token_hex(32)
        evidence = dict(
            version=data.policy.policy.version,
            assurance=data.policy.policy.assurance,
            nonce=operation.nonce,
            ticket=operation.ticket,
            phase="terminal-http",
            observed_at=now.isoformat(),
            valid_until=(now + timedelta(seconds=15)).isoformat(),
            scope=data.enrollment.scope.model_dump(),
            pins=data.enrollment.pins.model_dump(),
            local=local.model_dump(),
            quota=quota.model_dump(),
            financial=data.financial.model_dump(mode="json"),
            provider_facts=dict(entitlement="unknown", billing_guarantee="unknown"),
            order_revision=1,
            sequence=1,
            request_ref=self.request_ref,
            final_fingerprint=self.fingerprint,
        )
        self.execution = OwnerCheckExecution(
            local_ref,
            quota_ref,
            json.dumps(evidence).encode(),
            CurrentLocalState(local, quota, now, expected),
            Pins.model_validate(self.pins),
        )
        self.windows[local_ref] = NativeCheckWindow(
            local_check_ref=local_ref,
            quota_check_ref=quota_ref,
            operation_ref=operation.operation_ref,
            actor_credential_id=data.publisher_actor["credential_id"],
            nonce=operation.nonce,
            scope_digest=canonical_digest(data.enrollment.scope.model_dump()),
            pins_digest=canonical_digest(self.pins),
            request_ref=self.request_ref,
            final_fingerprint=self.fingerprint,
            order_revision=1,
            sequence=1,
            checked_at=now,
            snapshot_checked_at=now,
            valid_until=now + timedelta(seconds=20),
        )
        return expected

    def current(self, operation, expected):
        if self.revoked or self.execution is None or expected != self.execution.current.expected:
            raise ValueError("registered owner execution unavailable")
        return self.execution

    def live(self, claim):
        if self.revoked or self.execution is None or claim.local_check_ref not in self.windows:
            raise ValueError("synthetic private owner revoked")
        if self.execution.current.expected.fingerprint != self.fingerprint:
            raise ValueError("immutable final wire changed")
        if datetime.now(UTC) >= self.windows[claim.local_check_ref].valid_until:
            raise ValueError("original private check expired")


@pytest.fixture
def local_api_runtime(tmp_path):
    yield from build_local_api_runtime(tmp_path)


def build_local_api_runtime(
    tmp_path, *, native_owner=None, native_phase="native-http-fetch", native_operation_limit=None
):
    """Real loopback TLS, Store/auth and canonical IPC. No provider network effects."""
    from test_agent_authority import enroll, issue, post, prepared

    from tessera_control.api import create_app
    from tessera_control.store import Store, canonical_digest
    from tessera_mcp.runtime_admission import AdmissionLedger, Pins

    initial = prepared.__wrapped__(tmp_path)
    state, old, human, subject, project, bounds, worker = initial
    registered = RegisteredSyntheticOwner() if native_owner is None else native_owner
    app = create_app(
        state,
        runtime_binding_adapter=registered if native_owner is None else registered.backend_adapter,
    )
    cert, key = tmp_path / "localhost.pem", tmp_path / "localhost.key"
    # Generated test-only TLS key stays private; never log/read its bytes.
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    key.chmod(0o600)
    owned_socket = socket.socket()
    owned_socket.bind(("127.0.0.1", 0))
    owned_socket.listen(128)
    ready = threading.Event()

    class OwnedServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    server = OwnedServer(
        uvicorn.Config(
            app,
            log_config=None,
            access_log=False,
            log_level="critical",
            ssl_keyfile=str(key),
            ssl_certfile=str(cert),
        )
    )
    thread = threading.Thread(target=lambda: server.run(sockets=[owned_socket]), daemon=True)
    thread.start()
    assert ready.wait(10), "owned local API startup failed"
    origin = "https://127.0.0.1:" + str(owned_socket.getsockname()[1])
    http = httpx.Client(base_url=origin, verify=str(cert), timeout=10, follow_redirects=False)
    try:
        assert http.get("/health/ready").status_code == 200
        live_prepared = (state, http, human, subject, project, bounds, worker)
        parent = issue(live_prepared)
        publisher, publisher_record = enroll(http, human, project)
        subject_actor = Store(state).authenticate(subject["Authorization"].removeprefix("Bearer "))
        context_body = dict(
            credential_id=subject_actor["credential_id"],
            destination=bounds["destination"],
            title="Local API context",
            body="Public bounded context",
            classification="project",
            disclosure_approved=True,
        )
        context = post(http, human, f"/projects/{project}/agent-contexts", context_body)
        expiry = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        operation_limit = (
            native_operation_limit
            if native_operation_limit is not None
            else (1 if native_phase == "native-http-fetch" else 2)
        )
        policy_body = dict(
            version="tessera-runtime-admission-v2",
            assurance="local-enforcement-not-provider-billing",
            max_financial_age_seconds=60,
            max_evidence_age_seconds=30,
            provider_unknown="explicitly-approved",
            max_operation_age_seconds=30,
            allowed_actions=["runtime:prepare", "runtime:invoke"],
            allowed_native_phases=(
                ["native-http-fetch"]
                if native_owner is None or native_phase == "native-http-fetch"
                else ["native-ws-handshake", "native-ws-frame"]
            ),
            model=bounds["model"],
            max_native_operations=operation_limit,
            max_exposure_cents=operation_limit * 10,
            exposure_per_operation_cents=10,
            expires_at=expiry,
        )
        policy = RuntimePolicyRecord.model_validate(
            post(http, human, f"/projects/{project}/runtime-policies", policy_body)
        )
        if native_owner is None:
            binding = bind_runtime_profile(
                HarnessBinding(b"{}", b'{"fixture":"SYNTHETIC-installed-binding"}', b"skill"),
                policy_ref=policy.policy_ref,
                policy_digest=policy.admission_policy_digest,
                profile_digest=policy.profile_digest,
            )
            pins = {
                name: hashlib.sha256(
                    ("SYNTHETIC independently installed " + name).encode()
                ).hexdigest()
                for name in Pins.model_fields
                if name != "instance"
            }
            pins.update(instance="SYNTHETIC-local-api-instance", binding=binding.digest)
            registered.pins = pins
            registered.session = hashlib.sha256(b"independent SYNTHETIC owner session").hexdigest()
        else:
            # Approved owner measures actual installed build/config/skill/catalog/SDK,
            # never fills pins from the policy or arbitrary enrollment request.
            binding = registered.prepare_installation(policy, tmp_path)
            assert type(binding) is HarnessBinding
            measured = registered.installed_pins()
            assert type(measured) is Pins and measured.binding == binding.digest
            pins = measured.model_dump()
            request = registered.next_request(native_phase)
            assert type(request) is dict and set(request) == {
                "request_ref",
                "final_fingerprint",
                "order_revision",
                "sequence",
            }
        enrollment_body = dict(
            policy_ref=policy.policy_ref,
            expected_policy_revision=1,
            subject_credential_id=subject_actor["credential_id"],
            publisher_credential_id=publisher_record["credential_id"],
            publisher_audience="control",
            publisher_ref="b" * 64 if native_owner is None else registered.publisher_ref,
            opaque_owner_ref="c" * 64 if native_owner is None else registered.opaque_owner_ref,
            session_ref=registered.session,
            destination=bounds["destination"],
            role="planner",
            context_ref=context["id"],
            context_digest=canonical_digest(context_body),
            parent_agent_authority_grant_id=parent["id"],
            expected_parent_revision=1,
            expected_parent_epoch=1,
            pins=pins,
            expires_at=expiry,
        )
        enrollment = RuntimeEnrollmentRecord.model_validate(
            post(http, human, f"/projects/{project}/runtime-enrollments", enrollment_body)
        )
        financial_body = dict(
            enrollment_ref=enrollment.enrollment_ref,
            expected_enrollment_revision=1,
            policy_ref=policy.policy_ref,
            expected_policy_revision=1,
            observed_at=datetime.now(UTC).isoformat(),
            valid_until=(datetime.now(UTC) + timedelta(seconds=50)).isoformat(),
            purchased_credit_balance=0,
            auto_top_up_off=True,
            automatic_reload_off=True,
        )
        financial = RuntimeFinancialRecord.model_validate(
            post(http, human, f"/projects/{project}/runtime-financial-assertions", financial_body)
        )
        token = publisher["Authorization"].removeprefix("Bearer ")
        client = RuntimeBindingClient(
            origin=origin,
            audience="control",
            credential_reference=lambda: token,
            destination=bounds["destination"],
            http=http,
        )
        resolve = dict(
            enrollment_ref=enrollment.enrollment_ref,
            expected_enrollment_revision=1,
            financial_ref=financial.attestation_ref,
            expected_grant_revision=1,
            expected_grant_epoch=1,
        )
        allocation = AllocateRuntimeOperation(
            **resolve,
            action="runtime:invoke",
            phase="terminal-http" if native_phase == "native-http-fetch" else "terminal-ws",
            native_phase=native_phase,
            pins=enrollment.pins,
            request_ref=registered.request_ref if native_owner is None else request["request_ref"],
            final_fingerprint=registered.fingerprint
            if native_owner is None
            else request["final_fingerprint"],
            order_revision=1 if native_owner is None else request["order_revision"],
            sequence=1 if native_owner is None else request["sequence"],
        )
        data = SimpleNamespace(
            state=state,
            http=http,
            human=human,
            subject=subject,
            publisher=publisher,
            project=project,
            parent=parent,
            context=context,
            policy=policy,
            policy_body=policy_body,
            binding=binding,
            enrollment=enrollment,
            enrollment_body=enrollment_body,
            financial=financial,
            financial_body=financial_body,
            client=client,
            registered=registered,
            allocation=allocation,
            resolve=resolve,
            publisher_actor=Store(state).authenticate(token),
            store=app.state.runtime_bindings,
            marker=tmp_path / "SYNTHETIC-effect",
            cert=cert,
            ledger=AdmissionLedger(tmp_path / "admission-ledger"),
        )
        data.adapter = EnrolledAdmissionAdapter(
            client=client,
            enrollment=enrollment,
            policy=policy,
            financial_ref=financial.attestation_ref,
            owner_producer=registered.current,
            installed_pins=(lambda: Pins.model_validate(registered.pins))
            if native_owner is None
            else registered.installed_pins,
            ledger=data.ledger,
            terminal_port=NativeTerminalPort(
                (lambda: Pins.model_validate(registered.pins))
                if native_owner is None
                else registered.installed_pins,
                lambda verifier: None,
            ),
            publisher_credential_id=publisher_record["credential_id"],
            binding=binding,
        )
        yield data
    finally:
        http.close()
        old.close()
        server.should_exit = True
        thread.join(10)
        owned_socket.close()
        assert not thread.is_alive(), "only owned server must stop"


def local_claim_request(data, operation):
    return ClaimRuntimeOperation(
        expected_operation_revision=operation.revision,
        nonce=operation.nonce,
        ticket=operation.ticket,
        expected_grant_revision=1,
        expected_grant_epoch=1,
        expected_enrollment_revision=1,
        financial_ref=data.financial.attestation_ref,
        local_check_ref=data.registered.execution.local_check_ref,
        quota_check_ref=data.registered.execution.quota_check_ref,
        pins=operation.binding.pins,
        request_ref=operation.expected.request_ref,
        final_fingerprint=operation.expected.final_fingerprint,
        order_revision=1,
        sequence=1,
    )


def local_pipe(data, operation, claim):
    gate = StoreInvocationGate(
        store=data.store,
        publisher_actor=data.publisher_actor,
        claim_request=local_claim_request(data, operation),
        audience="control",
        owner_live_check=data.registered.live,
    )
    source = """import json,sys
from pathlib import Path
x=json.loads(sys.argv[1]);print(json.dumps(dict(event="ready",**x)),flush=True)
r=json.loads(sys.stdin.readline())
if r!={"event":"invoke","operation_ref":x["operation_ref"]}:sys.exit(8)
with Path(sys.argv[2]).open("a") as f:f.write("one SYNTHETIC effect\\n")
print(json.dumps({"event":"native-returned","operation_ref":x["operation_ref"]}),flush=True)
"""
    meta = {
        name: getattr(claim, name) for name in ("operation_ref", "request_ref", "final_fingerprint")
    }
    return NativeInvocationPipe(
        approved_command=(sys.executable, "-c", source, json.dumps(meta), str(data.marker)),
        gate=gate,
    )


def test_local_api_human_issuance_real_https_python_verifier_canonical_ipc_settle(
    local_api_runtime,
):
    data = local_api_runtime
    snapshot = data.client.resolve(ResolveRuntimeBinding(**data.resolve), "resolve-live")
    assert snapshot.remaining_native_operations == 1
    operation = data.client.allocate(data.allocation, "allocate-live")
    data.registered.execute(data, operation)
    bridge = EnrolledNativeBridge(
        adapter=data.adapter, pipe_factory=lambda claim: local_pipe(data, operation, claim)
    )
    result = bridge.invoke(operation, claim_key="claim-live", settle_key="settle-live")
    assert result.state == "native-returned"
    assert data.marker.read_text() == "one SYNTHETIC effect\n"


@pytest.mark.parametrize(
    "kind", ["runtime-policies", "runtime-enrollments", "runtime-financial-assertions"]
)
def test_local_api_agent_cannot_human_issue(local_api_runtime, kind):
    data = local_api_runtime
    payload = {
        "runtime-policies": data.policy_body,
        "runtime-enrollments": data.enrollment_body,
        "runtime-financial-assertions": data.financial_body,
    }[kind]
    for actor in (data.subject, data.publisher):
        response = data.http.post(
            f"/projects/{data.project}/{kind}",
            headers={**actor, "Idempotency-Key": secrets.token_hex(12)},
            json=payload,
        )
        assert response.status_code in {403, 404}
    assert not data.marker.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "nonce",
        "ticket",
        "request_ref",
        "final_fingerprint",
        "order_revision",
        "sequence",
        "local_check_ref",
        "quota_check_ref",
    ],
)
def test_local_api_registered_owner_claim_rejects_mutated_scope(local_api_runtime, mutation):
    data = local_api_runtime
    operation = data.client.allocate(data.allocation, "allocate-mutated")
    data.registered.execute(data, operation)
    request = local_claim_request(data, operation)
    changed = 2 if mutation in {"order_revision", "sequence"} else "0" * 64
    with pytest.raises(BindingUnavailable):
        data.client.claim(
            operation, request.model_copy(update={mutation: changed}), "claim-mutated"
        )
    assert not data.marker.exists()


@pytest.mark.parametrize("kind", ["financial", "policy", "enrollment"])
def test_local_api_revocation_after_allocation_denies_before_effect(local_api_runtime, kind):
    data = local_api_runtime
    operation = data.client.allocate(data.allocation, "allocate-revoke")
    data.registered.execute(data, operation)
    routes = {
        "financial": ("runtime-financial-assertions", data.financial.attestation_ref),
        "policy": ("runtime-policies", data.policy.policy_ref),
        "enrollment": ("runtime-enrollments", data.enrollment.enrollment_ref),
    }
    route, ref = routes[kind]
    response = data.http.post(
        f"/{route}/{ref}/revoke",
        headers={**data.human, "Idempotency-Key": "revoke-now"},
        json={"expected_revision": 1},
    )
    assert response.status_code == 200
    with pytest.raises(ValueError):
        EnrolledNativeBridge(
            adapter=data.adapter, pipe_factory=lambda claim: local_pipe(data, operation, claim)
        ).invoke(operation, claim_key="claim-revoked", settle_key="settle-revoked")
    assert not data.marker.exists()


def test_local_api_claim_readback_and_canonical_entry_replay_survive_restart(local_api_runtime):
    from tessera_control.runtime_bindings import RuntimeBindingStore

    data = local_api_runtime
    operation = data.client.allocate(data.allocation, "allocate-replay")
    expected = data.registered.execute(data, operation)
    data.adapter.verify("dispatch", expected)
    request = local_claim_request(data, operation)
    claim = data.client.claim(operation, request, "claim-replay")
    with pytest.raises(BindingUnavailable, match="not_continuation"):
        data.client.claim(operation, request, "claim-replay")
    assert local_pipe(data, operation, claim).invoke(claim) == "native-returned"
    data.store = RuntimeBindingStore(data.state, adapter=data.registered)
    with pytest.raises(BindingUnavailable):
        local_pipe(data, operation, claim).invoke(claim)
    assert data.marker.read_text() == "one SYNTHETIC effect\n"
    data.client.settle(claim, "uncertain", "settle-uncertain")
    with pytest.raises(BindingUnavailable):
        data.client.settle(claim, "native-returned", "contradict-settle")


@pytest.mark.parametrize("fault", ["subject", "human", "unknown", "worker-audience"])
def test_local_api_additional_wrong_publisher_or_audience_denies_allocate(local_api_runtime, fault):
    data = local_api_runtime
    if fault == "worker-audience":
        data.client.audience = "worker"
    else:
        credential = {
            "subject": data.subject,
            "human": data.human,
            "unknown": {"Authorization": "Bearer not-a-credential"},
        }[fault]
        data.client.credential_reference = lambda: credential["Authorization"].removeprefix(
            "Bearer "
        )
    with pytest.raises(BindingUnavailable):
        data.client.allocate(data.allocation, "wrong-publisher")
    assert not data.marker.exists()


def test_local_api_additional_project_session_and_financial_original_age(local_api_runtime):
    from test_agent_authority import post

    data = local_api_runtime
    other = post(data.http, data.human, "/projects", {"name": "other scoped project"})
    response = data.http.post(
        f"/projects/{other['id']}/runtime-enrollments",
        headers={**data.human, "Idempotency-Key": "other-project"},
        json=data.enrollment_body,
    )
    assert response.status_code == 403
    response = data.http.post(
        f"/projects/{data.project}/runtime-enrollments",
        headers={**data.human, "Idempotency-Key": "changed-session"},
        json={**data.enrollment_body, "session_ref": "0" * 64},
    )
    assert response.status_code in {403, 409, 422}
    response = data.http.post(
        f"/projects/{data.project}/runtime-financial-assertions",
        headers={**data.human, "Idempotency-Key": "original-observation-stale"},
        json={
            **data.financial_body,
            "observed_at": (datetime.now(UTC) - timedelta(minutes=2)).isoformat(),
        },
    )
    assert response.status_code in {403, 422}
    assert not data.marker.exists()


@pytest.mark.parametrize(
    "fault", ["subject-revoke", "parent-epoch", "context-mutation-denied", "issuer-revoke"]
)
def test_local_api_additional_current_authority_mutation_between_allocate_claim(
    local_api_runtime, fault
):
    from tessera_control.store import _connect

    data = local_api_runtime
    operation = data.client.allocate(data.allocation, "authority-change")
    data.registered.execute(data, operation)
    connection = _connect(data.store.database_path)
    try:
        if fault == "subject-revoke":
            connection.execute(
                "UPDATE credentials SET revoked_at=? WHERE id=?",
                (datetime.now(UTC).isoformat(), data.enrollment.scope.credential),
            )
        elif fault == "parent-epoch":
            connection.execute(
                "UPDATE agent_authority_grants SET epoch=epoch+1 WHERE id=?", (data.parent["id"],)
            )
        elif fault == "issuer-revoke":
            connection.execute(
                "UPDATE credentials SET revoked_at=? WHERE principal_id=?",
                (datetime.now(UTC).isoformat(), data.financial.issuer),
            )
        else:
            # Explicit fault injection of unavailable context; no withdrawal API exists.
            row = connection.execute(
                "SELECT payload_json FROM external_agent_contexts WHERE id=?", (data.context["id"],)
            ).fetchone()
            payload = json.loads(row[0])
            payload["disclosure_approved"] = False
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(
                    "UPDATE external_agent_contexts SET payload_json=? WHERE id=?",
                    (json.dumps(payload), data.context["id"]),
                )
            assert not data.marker.exists()
            return  # Context mutation is rejected; no withdrawal API is claimed.
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ValueError):
        EnrolledNativeBridge(
            adapter=data.adapter, pipe_factory=lambda claim: local_pipe(data, operation, claim)
        ).invoke(
            operation, claim_key="authority-changed-claim", settle_key="authority-changed-settle"
        )
    assert not data.marker.exists()


def test_local_api_additional_lost_claim_reply_commits_but_never_starts_child(local_api_runtime):
    data = local_api_runtime
    operation = data.client.allocate(data.allocation, "lost-reply")
    data.registered.execute(data, operation)

    def lose_after_actual_reply(response):
        if response.request.url.path.endswith("/claim") and response.status_code == 201:
            raise httpx.ReadError("test-only lost actual committed HTTP reply")

    data.http.event_hooks["response"].append(lose_after_actual_reply)
    started = []
    with pytest.raises(BindingUnavailable):
        EnrolledNativeBridge(
            adapter=data.adapter, pipe_factory=lambda claim: started.append(claim)
        ).invoke(operation, claim_key="claim-lost", settle_key="settle-lost")
    data.http.event_hooks["response"].clear()
    assert started == [] and not data.marker.exists()
    with pytest.raises(BindingUnavailable, match="not_continuation"):
        data.client.claim(operation, local_claim_request(data, operation), "claim-lost")


@pytest.mark.parametrize("fault", ["private-revoke", "wire-change", "uncertain-child"])
def test_local_api_additional_after_claim_before_entry_or_reply_fails_closed(
    local_api_runtime, fault
):
    data = local_api_runtime
    operation = data.client.allocate(data.allocation, "late-private")
    expected = data.registered.execute(data, operation)
    data.adapter.verify("dispatch", expected)
    claim = data.client.claim(operation, local_claim_request(data, operation), "late-private-claim")
    pipe = local_pipe(data, operation, claim)
    if fault == "private-revoke":
        data.registered.revoked = True
    elif fault == "wire-change":
        data.registered.fingerprint = "0" * 64
    else:
        # Child exists but dies before ready: committed canonical entry stays consumed.
        pipe.command = (sys.executable, "-c", "raise SystemExit(6)")
    with pytest.raises(BindingUnavailable):
        pipe.invoke(claim)
    assert not data.marker.exists()
    if fault == "uncertain-child":
        with pytest.raises(BindingUnavailable):
            local_pipe(data, operation, claim).invoke(claim)
        data.client.settle(claim, "uncertain", "late-private-uncertain")


def test_local_api_additional_concurrent_first_claims_only_one_reservation(local_api_runtime):
    from concurrent.futures import ThreadPoolExecutor

    data = local_api_runtime
    operation = data.client.allocate(data.allocation, "concurrent-allocate")
    data.registered.execute(data, operation)
    request = local_claim_request(data, operation)

    def claim(key):
        try:
            return data.client.claim(operation, request, key)
        except BindingUnavailable:
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        receipts = list(workers.map(claim, ["concurrent-a", "concurrent-b"]))
    admitted = [receipt for receipt in receipts if receipt is not None]
    assert len(admitted) == 1 and admitted[0].remaining_native_operations == 0
    assert local_pipe(data, operation, admitted[0]).invoke(admitted[0]) == "native-returned"
    assert data.marker.read_text() == "one SYNTHETIC effect\n"


def test_local_api_additional_crossprocess_revocation_waits_for_canonical_ipc_fence(
    local_api_runtime,
):
    data = local_api_runtime
    operation = data.client.allocate(data.allocation, "linearization-allocate")
    expected = data.registered.execute(data, operation)
    data.adapter.verify("dispatch", expected)
    claim = data.client.claim(
        operation, local_claim_request(data, operation), "linearization-claim"
    )
    pipe = local_pipe(data, operation, claim)
    source = """import sys,ssl,httpx
from pathlib import Path
header=Path(sys.argv[1]).read_text().strip().split(": ",1)[1]
with httpx.Client(verify=ssl.create_default_context(cafile=sys.argv[2]),timeout=15) as client:
 print("starting",flush=True)
 response=client.post(sys.argv[3],headers={"Authorization":header,"Idempotency-Key":"crossprocess-revoke"},json={"expected_revision":1})
 print(str(response.status_code),flush=True)
 if response.status_code!=200:sys.exit(7)
"""
    children = []
    original = data.registered.live
    observed = []

    def live(current):
        original(current)
        if not children:
            process = subprocess.Popen(
                (
                    sys.executable,
                    "-c",
                    source,
                    str(data.state / "owner.header"),
                    str(data.cert),
                    data.client.origin
                    + "/runtime-financial-assertions/"
                    + data.financial.attestation_ref
                    + "/revoke",
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin"},
            )
            children.append(process)
            assert process.stdout.readline() == b"starting\n"
        assert children[0].poll() is None  # separate process writer blocked by canonical fence
        observed.append(data.marker.exists())

    pipe.gate.owner_live_check = live
    try:
        assert pipe.invoke(claim) == "native-returned"
        output, _ = children[0].communicate(timeout=15)
        assert output == b"200\n" and children[0].returncode == 0
        assert observed[0] is False and observed[-1] is True
        assert data.marker.read_text() == "one SYNTHETIC effect\n"
        assert (
            data.client.settle(claim, "native-returned", "revoked-after-effect").state
            == "native-returned"
        )
    finally:
        for process in children:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()


synthetic_v2 = admission_fixtures.synthetic_v2


@pytest.fixture
def synthetic_binding(synthetic_v2, tmp_path):
    rig = synthetic_v2
    policy = IssueRuntimePolicy(
        version=rig.policy.version,
        assurance=rig.policy.assurance,
        max_financial_age_seconds=3600,
        max_evidence_age_seconds=30,
        provider_unknown="explicitly-approved",
        max_operation_age_seconds=30,
        allowed_actions=("runtime:prepare", "runtime:invoke"),
        allowed_native_phases=("native-http-fetch", "native-ws-handshake", "native-ws-frame"),
        model="model",
        max_native_operations=1,
        max_exposure_cents=1,
        exposure_per_operation_cents=1,
        expires_at=rig.stamp(600),
    )
    record = RuntimePolicyRecord(
        policy_ref=rig.policy.admission_policy_ref,
        revision=6,
        issuer=rig.policy.operator_issuer,
        operator_issuer=rig.policy.operator_issuer,
        project=rig.scope.project,
        admission_policy_digest=rig.policy.digest,
        profile_digest="d" * 64,
        issued_at=rig.stamp(-90),
        policy=policy,
    )
    binding = bind_runtime_profile(
        HarnessBinding(b"{}", b'{"unit":"SYNTHETIC-source"}', b"skill"),
        policy_ref=record.policy_ref,
        policy_digest=record.admission_policy_digest,
        profile_digest=record.profile_digest,
    )
    scope = rig.scope.model_copy(
        update={"actions_digest": public_digest({"actions": list(policy.allowed_actions)})}
    )
    pins = rig.pins.model_copy(update={"binding": binding.digest})
    enrollment = RuntimeEnrollmentRecord(
        enrollment_ref=scope.enrollment,
        revision=1,
        issuer=record.issuer,
        issued_at=rig.stamp(-90),
        publisher_credential_id="SYNTHETIC-publisher",
        publisher_audience="control",
        scope=scope.model_dump(),
        pins=pins.model_dump(),
        expires_at=rig.stamp(300),
    )
    financial = RuntimeFinancialRecord(**rig.state["operator_assertion"].model_dump())
    snapshot = RuntimeBindingSnapshot(
        version="tessera-runtime-binding-v1",
        scope=enrollment.scope,
        pins=enrollment.pins,
        enrollment_revision=1,
        policy_revision=6,
        financial_ref=financial.attestation_ref,
        financial_issuer=financial.issuer,
        financial_observed_at=financial.observed_at,
        financial_asserted_at=financial.asserted_at,
        financial_valid_until=financial.valid_until,
        expires_at=rig.stamp(20),
        remaining_native_operations=1,
        remaining_exposure_cents=1,
        provider_entitlement="unknown",
        provider_billing_guarantee="unknown",
    )
    expected = replace(rig.expected, phase="terminal-http")
    request = AllocateRuntimeOperation(
        enrollment_ref=enrollment.enrollment_ref,
        expected_enrollment_revision=1,
        financial_ref=financial.attestation_ref,
        expected_grant_revision=scope.grant_revision,
        expected_grant_epoch=scope.grant_epoch,
        action="runtime:invoke",
        phase=expected.phase,
        native_phase="native-http-fetch",
        pins=enrollment.pins,
        request_ref=expected.request_ref,
        final_fingerprint=expected.fingerprint,
        order_revision=4,
        sequence=2,
    )
    operation = RuntimeOperation(
        operation_ref="a" * 64,
        root_ref="b" * 64,
        nonce=expected.nonce,
        ticket=expected.ticket,
        actor_credential_id=enrollment.publisher_credential_id,
        binding=snapshot,
        expected=request,
        expires_at=rig.stamp(20),
        revision=1,
        state="allocated",
    )
    claim_request = ClaimRuntimeOperation(
        expected_operation_revision=1,
        nonce=expected.nonce,
        ticket=expected.ticket,
        expected_grant_revision=scope.grant_revision,
        expected_grant_epoch=scope.grant_epoch,
        expected_enrollment_revision=1,
        financial_ref=financial.attestation_ref,
        local_check_ref="c" * 64,
        quota_check_ref="d" * 64,
        pins=enrollment.pins,
        request_ref=expected.request_ref,
        final_fingerprint=expected.fingerprint,
        order_revision=4,
        sequence=2,
    )
    claim = RuntimeClaimReceipt(
        version="tessera-runtime-claim-v1",
        operation_ref=operation.operation_ref,
        root_ref=operation.root_ref,
        claim_ref="e" * 64,
        claim_revision=2,
        actor_credential_id=enrollment.publisher_credential_id,
        scope=enrollment.scope,
        pins=enrollment.pins,
        nonce=expected.nonce,
        ticket=expected.ticket,
        request_ref=expected.request_ref,
        final_fingerprint=expected.fingerprint,
        financial_ref=financial.attestation_ref,
        local_check_ref="c" * 64,
        quota_check_ref="d" * 64,
        order_revision=4,
        sequence=2,
        reserved_native_operations=1,
        reserved_exposure_cents=1,
        remaining_native_operations=0,
        remaining_exposure_cents=0,
        expires_at=rig.stamp(20),
        delivery="first-claim",
        state="claimed",
    )
    receipt = json.loads(json.dumps(rig.receipt))
    receipt.update(
        scope=scope.model_dump(),
        pins=pins.model_dump(),
        phase=expected.phase,
        financial=financial.model_dump(mode="json"),
    )
    execution = OwnerCheckExecution(
        "c" * 64,
        "d" * 64,
        json.dumps(receipt).encode(),
        CurrentLocalState(rig.state["native_local"], rig.state["native_quota"], rig.now, expected),
        pins,
    )
    replies = {
        "snapshot": snapshot,
        "financial": financial,
        "operation": operation,
        "claim": claim,
        "status": 201,
    }
    calls = []

    def response(req):
        calls.append((req.method, req.url.path))  # Never retain auth/header/body content.
        assert req.headers["authorization"] == "Bearer SYNTHETIC-publisher-token"
        if req.url.path.endswith("/resolve"):
            value, status = replies["snapshot"], 200
        elif "runtime-financial-assertions" in req.url.path:
            value, status = replies["financial"], 200
        elif req.url.path.endswith("/claim"):
            value, status = replies["claim"], replies["status"]
        elif req.url.path.endswith("/settle"):
            disposition = json.loads(req.content)["disposition"]
            value, status = replies["operation"].model_copy(update={"state": disposition}), 200
        else:
            value, status = replies["operation"], 201
        return httpx.Response(status, content=value.model_dump_json())

    http = httpx.Client(transport=httpx.MockTransport(response))
    client = RuntimeBindingClient(
        origin="https://unit.invalid",
        audience="control",
        credential_reference=lambda: "SYNTHETIC-publisher-token",
        destination=scope.destination,
        http=http,
    )
    data = SimpleNamespace(
        rig=rig,
        policy=record,
        binding=binding,
        enrollment=enrollment,
        pins=pins,
        financial=financial,
        snapshot=snapshot,
        operation=operation,
        expected=expected,
        claim=claim,
        claim_request=claim_request,
        execution=execution,
        replies=replies,
        calls=calls,
        client=client,
    )
    yield data
    http.close()


def adapter(data):
    return EnrolledAdmissionAdapter(
        client=data.client,
        enrollment=data.enrollment,
        policy=data.policy,
        financial_ref=data.financial.attestation_ref,
        owner_producer=lambda operation, expected: data.execution,
        installed_pins=lambda: data.pins,
        ledger=data.rig.ledger,
        terminal_port=NativeTerminalPort(lambda: data.pins, lambda verifier: None),
        publisher_credential_id=data.enrollment.publisher_credential_id,
        binding=data.binding,
        clock=lambda: data.rig.now,
    )


def test_fresh_authenticated_projection_unit_not_backend_identity_proof(synthetic_binding):
    data = synthetic_binding
    value = adapter(data)
    result = value.verify("dispatch", data.expected)
    assert result["state"] == "terminal-validated"
    assert data.calls.count(("POST", "/runtime-bindings/resolve")) >= 4
    assert data.calls.count(("GET", "/runtime-financial-assertions/operator-assertion")) == 2


@pytest.mark.parametrize(
    "status,delivery", [(200, "readback-only"), (200, "first-claim"), (201, "readback-only")]
)
def test_only_fresh_201_claim_can_start_continuation(synthetic_binding, status, delivery):
    data = synthetic_binding
    data.replies.update(status=status, claim=data.claim.model_copy(update={"delivery": delivery}))
    with pytest.raises(BindingUnavailable, match="not_continuation"):
        data.client.claim(data.operation, data.claim_request, "exact-claim")


@pytest.mark.parametrize("field", ["scope", "pins", "financial_ref", "request_ref", "ticket"])
def test_claim_scope_cannot_be_swapped(synthetic_binding, field):
    data = synthetic_binding
    value = "f" * 64
    if field == "scope":
        value = data.claim.scope.model_copy(update={"principal": "other"})
    if field == "pins":
        value = data.claim.pins.model_copy(update={"sdk": "f" * 64})
    data.replies["claim"] = data.claim.model_copy(update={field: value})
    with pytest.raises(BindingUnavailable):
        data.client.claim(data.operation, data.claim_request, "exact-claim")


def test_worker_uses_separate_audience_routes(synthetic_binding):
    data = synthetic_binding
    data.client.audience = "worker"  # Unit request routing, not a credential promotion.
    data.client.financial(data.financial.attestation_ref)
    assert data.calls[-1][1] == "/worker/runtime-financial-assertions/operator-assertion"


def test_json_flags_never_supply_registered_owner_checks(synthetic_binding):
    data = synthetic_binding
    value = adapter(data)
    value.owner_producer = lambda *args: {"verified": True, "complete": True}
    with pytest.raises(BindingUnavailable):
        value.verify("dispatch", data.expected)


def test_lost_claim_response_does_not_start_pipe(synthetic_binding):
    data = synthetic_binding

    def lost(req):
        raise httpx.ReadError("SYNTHETIC lost reply")

    data.client.http = httpx.Client(transport=httpx.MockTransport(lost))
    with pytest.raises(BindingUnavailable, match="uncertain"):
        data.client.claim(data.operation, data.claim_request, "exact-claim")
    data.client.http.close()


def issuer_gate(data, tmp_path):
    db = sqlite3.connect(tmp_path / "issuer.sqlite3")
    db.execute("CREATE TABLE owned (revoked INTEGER, reserved INTEGER)")
    db.execute("INSERT INTO owned VALUES(0,1)")
    db.commit()

    def current(claim):
        revoked, reserved = db.execute("SELECT revoked,reserved FROM owned").fetchone()
        if revoked or reserved != 1:
            raise BindingUnavailable("SYNTHETIC owned reservation revoked")
        return IssuerInvocationState(
            data.claim,
            data.snapshot.model_copy(
                update={"remaining_native_operations": 0, "remaining_exposure_cents": 0}
            ),
            data.financial,
            data.execution,
            data.policy,
        )

    return SQLiteInvocationGate(
        connection=db, validate_owned_claim=current, clock=lambda: data.rig.now
    )


def test_last_unit_own_reservation_and_revocation_writer_fenced(synthetic_binding, tmp_path):
    data = synthetic_binding
    gate = issuer_gate(data, tmp_path)
    other = sqlite3.connect(tmp_path / "issuer.sqlite3", timeout=0)
    try:
        with gate.hold(data.claim) as lease:
            assert lease.state.snapshot.remaining_native_operations == 0
            enrolled = adapter(data)
            enrolled._lease = lease
            assert enrolled.authority("dispatch").budget_available is True
            assert data.calls == []  # No remote HTTP snapshot masquerades as held liveness.
            with pytest.raises(sqlite3.OperationalError):
                other.execute("UPDATE owned SET revoked=1")
            lease.assert_current()
        other.execute("UPDATE owned SET revoked=1")
        other.commit()
        with pytest.raises(BindingUnavailable):
            with gate.hold(data.claim):
                pytest.fail("revocation must deny")
    finally:
        other.close()
        gate.db.close()


def test_real_local_process_pipe_requires_held_issuer_lease(synthetic_binding, tmp_path):
    data = synthetic_binding
    gate = issuer_gate(data, tmp_path)
    marker = tmp_path / "SYNTHETIC-effect"
    source = """import json,sys
from pathlib import Path
claim=json.loads(sys.argv[1]); print(json.dumps(dict(event="ready",**claim)),flush=True)
message=json.loads(sys.stdin.readline())
if message!={"event":"invoke","operation_ref":claim["operation_ref"]}:sys.exit(9)
Path(sys.argv[2]).write_text("one SYNTHETIC native effect")
print(json.dumps({"event":"native-returned","operation_ref":claim["operation_ref"]}),flush=True)
"""
    metadata = {
        name: getattr(data.claim, name)
        for name in ("operation_ref", "request_ref", "final_fingerprint")
    }
    pipe = NativeInvocationPipe(
        approved_command=(sys.executable, "-c", source, json.dumps(metadata), str(marker)),
        gate=gate,
    )
    try:
        assert pipe.invoke(data.claim) == "native-returned"
        assert marker.read_text() == "one SYNTHETIC native effect"
        gate.db.execute("UPDATE owned SET revoked=1")
        gate.db.commit()
        marker.unlink()
        with pytest.raises(BindingUnavailable):
            pipe.invoke(data.claim)
        assert not marker.exists()
    finally:
        gate.db.close()


def test_remote_readback_or_state_json_cannot_supply_sqlite_lease(synthetic_binding, tmp_path):
    data = synthetic_binding
    gate = issuer_gate(data, tmp_path)
    try:
        with pytest.raises(BindingUnavailable):
            with gate.hold(data.claim.model_copy(update={"delivery": "readback-only"})):
                pass
        gate.validate_owned_claim = lambda claim: {"authorized": True, "claim": claim.model_dump()}
        with pytest.raises(BindingUnavailable):
            with gate.hold(data.claim):
                pass
    finally:
        gate.db.close()


def test_profile_binding_is_explicit_new_source_not_legacy_conversion(synthetic_binding):
    data = synthetic_binding
    assert (
        json.loads(data.binding.manifest)["runtime_admission"]["profile_digest"]
        == data.policy.profile_digest
    )
    with pytest.raises(ValueError):
        bind_runtime_profile(
            data.binding,
            policy_ref=data.policy.policy_ref,
            policy_digest=data.policy.admission_policy_digest,
            profile_digest=data.policy.profile_digest,
        )


def test_real_orchestration_unit_verifies_then_claims_then_pipe_then_settles(
    synthetic_binding, tmp_path
):
    data = synthetic_binding
    gate = issuer_gate(data, tmp_path)
    marker = tmp_path / "SYNTHETIC-bridge-effect"
    source = """import json,sys
from pathlib import Path
x=json.loads(sys.argv[1]);print(json.dumps(dict(event="ready",**x)),flush=True)
request=json.loads(sys.stdin.readline())
if request!={"event":"invoke","operation_ref":x["operation_ref"]}:sys.exit(7)
Path(sys.argv[2]).write_text("SYNTHETIC effect once")
print(json.dumps({"event":"native-returned","operation_ref":x["operation_ref"]}),flush=True)
"""

    def pipe(claim):
        meta = {
            name: getattr(claim, name)
            for name in ("operation_ref", "request_ref", "final_fingerprint")
        }
        return NativeInvocationPipe(
            approved_command=(sys.executable, "-c", source, json.dumps(meta), str(marker)),
            gate=gate,
        )

    try:
        bridge = EnrolledNativeBridge(adapter=adapter(data), pipe_factory=pipe)
        result = bridge.invoke(
            data.operation, claim_key="one-native-claim", settle_key="one-native-settle"
        )
        assert result.state == "native-returned" and marker.read_text() == "SYNTHETIC effect once"
        claim_index = data.calls.index(
            ("POST", "/runtime-operations/" + data.operation.operation_ref + "/claim")
        )
        assert claim_index >= 4
        assert data.calls[-1] == (
            "POST",
            "/runtime-operations/" + data.operation.operation_ref + "/settle",
        )
        with pytest.raises(ValueError):
            bridge.invoke(
                data.operation, claim_key="another-key-is-not-a-retry", settle_key="settle-again"
            )
    finally:
        gate.db.close()


@pytest.mark.parametrize("change", ["financial_age", "policy_expiry", "native_quota", "wire"])
def test_lease_rechecks_current_finance_policy_quota_and_wire_before_signal(
    synthetic_binding, tmp_path, change
):
    data = synthetic_binding
    gate = issuer_gate(data, tmp_path)
    original = gate.validate_owned_claim

    def changed(claim):
        current = original(claim)
        if change == "financial_age":
            return replace(
                current,
                financial=current.financial.model_copy(
                    update={"observed_at": data.rig.now.replace(hour=10)}
                ),
            )
        if change == "policy_expiry":
            policy = current.policy.model_copy(
                update={
                    "policy": current.policy.policy.model_copy(update={"expires_at": data.rig.now})
                }
            )
            return replace(current, policy=policy)
        if change == "native_quota":
            checks = current.checks.current
            quota = checks.quota.model_copy(update={"snapshot_checked_at": data.rig.stamp(-60)})
            return replace(
                current, checks=replace(current.checks, current=replace(checks, quota=quota))
            )
        checks = current.checks.current
        return replace(
            current,
            checks=replace(
                current.checks,
                current=replace(checks, expected=replace(checks.expected, fingerprint="f" * 64)),
            ),
        )

    gate.validate_owned_claim = changed
    try:
        with pytest.raises(BindingUnavailable):
            with gate.hold(data.claim):
                pytest.fail("No native continuation may run")
    finally:
        gate.db.close()
