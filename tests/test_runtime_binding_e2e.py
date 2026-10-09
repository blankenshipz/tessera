"""Opt-in frozen-native/real HTTPS Store gate; collection is not acceptance.

Parent provides a private observed-revision admission file after native quiescence.
No native checkout is imported, hashed or executed during collection. Upstream
OAuth/account/quota resource inputs and permitted native primitives remain clearly
test-only; actual private/public guard logic and branded HTTP/WS must execute.
"""

import hashlib
import importlib.util
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from test_runtime_binding_client import (  # noqa: F401
    RegisteredSyntheticOwner,
    build_local_api_runtime,
    local_claim_request,
    local_pipe,
)

from tessera_control.runtime_binding_models import ClaimRuntimeOperation
from tessera_control.runtime_bindings import RuntimeBindingStore
from tessera_mcp.runtime_admission import AdmissionDenied, TerminalExpectation, closed_object
from tessera_mcp.runtime_binding_client import (
    BindingUnavailable,
    EnrolledNativeBridge,
    NativeInvocationPipe,
    OwnerCheckExecution,
    StoreInvocationGate,
)


class FrozenNativeInput:
    """Parent's observed inventory, NOT caller pin JSON or admission authority."""

    def __init__(self, path):
        root = Path(__file__).resolve().parents[1]
        self.root = root
        path = Path(path).resolve()
        if not path.is_relative_to(root / ".tessera" / "receipts"):
            raise AssertionError("parent admission must use private task receipt reference")
        if path.stat().st_size > 65536:
            raise AssertionError("bounded parent admission required")
        raw = path.read_bytes()
        if len(raw.splitlines()) > 350:
            raise AssertionError("bounded parent admission required")
        record = json.loads(raw, object_pairs_hook=closed_object)
        assert set(record) == {
            "version",
            "taskId",
            "intentRevision",
            "observedBy",
            "artifact",
            "source_files",
            "bridge_file",
            "coverage_label",
            "native_source",
        }
        assert record["version"] == "tessera-native-e2e-input-v1"
        assert record["taskId"] == "tessera-remote-agent-platform" and record["intentRevision"] == 8
        assert record["observedBy"] in {
            "ses_f16c9e47fffe2THUf2UAJcF1LI",
            "ses_ef8bd28a8ffeoCz6U4jp87rD93",
            "ses_ef8b70df1ffedLQQ8WHbyQ3waG",
        }
        assert record["coverage_label"] == "actual-native-code-synthetic-upstream-no-provider"
        artifact = record["artifact"]
        assert set(artifact) == {"subject", "revision", "generation"}
        assert artifact["subject"] and artifact["revision"].startswith("sha256:")
        assert type(artifact["generation"]) is int and artifact["generation"] > 0
        files = record["source_files"]
        assert type(files) is dict and files and record["bridge_file"] in files
        canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
        assert artifact["revision"] == "sha256:" + hashlib.sha256(canonical).hexdigest()
        self.files = {}
        for name, digest in files.items():
            resolved = (root / name).resolve()
            assert resolved.is_relative_to(root) and len(digest) == 64
            assert resolved.suffix in {
                ".py",
                ".ts",
                ".js",
                ".mjs",
                ".md",
                ".toml",
            } or resolved.name in {
                "package.json",
                "baseline.json",
                "LICENSE",
                "LICENSE.opencode",
                "codex-quota",
            }
            assert all(c in "0123456789abcdef" for c in digest)
            self.files[resolved] = digest
        self.bridge = (root / record["bridge_file"]).resolve()
        assert self.bridge.suffix == ".py"
        self.artifact = artifact
        native = record["native_source"]
        assert set(native) == {
            "inventory",
            "raw_sha256",
            "revision",
            "root",
            "baseline",
            "baseline_raw_sha256",
        }
        # Exact parent-provided repaired source reference, never a caller pin verdict.
        assert (
            native["revision"]
            == "sha256:c7c70b367eff949c5141979b5de4047f09892d4fdf15573ce9137c5e36fd8062"
        )
        assert (
            native["raw_sha256"]
            == "a48a1285d95ce8fe96edb31a1aa3730da2e292d107e854fd84457a101c3627e3"
        )
        inventory_path = (root / native["inventory"]).resolve()
        assert inventory_path.is_relative_to(root / ".tessera/receipts/rc-v8-admission-optin")
        baseline_path = (root / native["baseline"]).resolve()
        assert baseline_path == root / "release/opencode-observer/runtime-v8/baseline.json"
        assert native["baseline_raw_sha256"] == (
            "45d2645a5bb29a4b2ebe771eb0c9b8802110e4a00423f7baf844d0e10ed9f9a5"
        )
        assert self.files[baseline_path] == native["baseline_raw_sha256"]
        baseline_raw = baseline_path.read_bytes()
        assert hashlib.sha256(baseline_raw).hexdigest() == native["baseline_raw_sha256"]
        self.native_root = (root / native["root"]).resolve()
        assert self.native_root.is_relative_to(root)
        raw_inventory = inventory_path.read_bytes()
        assert hashlib.sha256(raw_inventory).hexdigest() == native["raw_sha256"]
        inventory = json.loads(raw_inventory, object_pairs_hook=closed_object)
        assert len(inventory) == 79
        assert (
            inventory == json.loads(baseline_raw, object_pairs_hook=closed_object)["target_files"]
        )
        namespace = "".join(
            f"{digest}  {name}\n" for name, digest in sorted(inventory.items())
        ).encode()
        assert "sha256:" + hashlib.sha256(namespace).hexdigest() == native["revision"]
        for name, digest in inventory.items():
            public_file = (self.native_root / name).resolve()
            assert public_file.is_relative_to(self.native_root)
            assert self.files[public_file] == digest
        self.native_inventory = inventory
        self.check()

    def check(self):
        for path, digest in self.files.items():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, "frozen source changed"

    def load_factory(self):
        self.check()
        spec = importlib.util.spec_from_file_location("tessera_frozen_native_owner", self.bridge)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.create_native_e2e_owner


@pytest.fixture
def native_api_runtime(tmp_path, request):
    reference = os.environ.get("TESSERA_NATIVE_E2E_ADMISSION_FILE")
    if not reference:
        pytest.skip("native parent frozen inventory/bridge not supplied; NOT acceptance")
    frozen = FrozenNativeInput(reference)
    # Factory only from admitted, observed bytes. No native process can run earlier.
    owner = frozen.load_factory()(scratch=tmp_path, admitted_inventory=frozen)
    assert not isinstance(owner, RegisteredSyntheticOwner)
    phase = getattr(request, "param", "native-http-fetch")
    limit = None
    if type(phase) is tuple:
        phase, limit = phase
    state = build_local_api_runtime(
        tmp_path, native_owner=owner, native_phase=phase, native_operation_limit=limit
    )
    try:
        data = next(state)
        data.frozen = frozen
        data.native_owner = owner
        yield data
    finally:
        state.close()
        owner.close()
        frozen.check()


def request_for(data, operation):
    execution = data.native_owner.current("dispatch", expected_for(operation))
    assert type(execution) is OwnerCheckExecution
    return ClaimRuntimeOperation(
        expected_operation_revision=operation.revision,
        nonce=operation.nonce,
        ticket=operation.ticket,
        expected_grant_revision=1,
        expected_grant_epoch=1,
        expected_enrollment_revision=1,
        financial_ref=data.financial.attestation_ref,
        local_check_ref=execution.local_check_ref,
        quota_check_ref=execution.quota_check_ref,
        pins=operation.binding.pins,
        request_ref=operation.expected.request_ref,
        final_fingerprint=operation.expected.final_fingerprint,
        order_revision=operation.expected.order_revision,
        sequence=operation.expected.sequence,
    )


def expected_for(operation):
    value = operation.expected
    return TerminalExpectation(
        value.phase,
        value.request_ref,
        value.final_fingerprint,
        value.order_revision,
        value.sequence,
        operation.nonce,
        operation.ticket,
    )


def pipe_for(data, operation, claim):
    data.frozen.check()
    owner = data.native_owner
    gate = StoreInvocationGate(
        store=data.store,
        publisher_actor=data.publisher_actor,
        claim_request=request_for(data, operation),
        audience="control",
        owner_live_check=owner.live,
    )
    return NativeInvocationPipe(
        approved_command=owner.native_command(claim),
        gate=gate,
        approved_references=owner.approved_references(),
    )


def allocate_execute(data, key):
    data.frozen.check()
    operation = data.client.allocate(data.allocation, key)
    expected = data.native_owner.execute(data, operation)
    assert type(expected) is TerminalExpectation and expected == expected_for(operation)
    # Evidence/ref producer must have run actual guard, not publisher callback echo.
    assert type(data.native_owner.current("dispatch", expected)) is OwnerCheckExecution
    return operation


def bridge_for(data, operation):
    return EnrolledNativeBridge(
        adapter=data.adapter, pipe_factory=lambda claim: pipe_for(data, operation, claim)
    )


def counters(data):
    value = data.native_owner.effect_counts()
    assert type(value) is dict and set(value) == {"http", "ws_handshake", "ws_frame"}
    assert all(type(count) is int and count >= 0 for count in value.values())
    return value


def test_pinned_native_http_actual_guard_python_store_claim_and_branded_effect(native_api_runtime):
    data = native_api_runtime
    before = counters(data)
    operation = allocate_execute(data, "native-http-allocation")
    result = bridge_for(data, operation).invoke(
        operation, claim_key="native-http-claim", settle_key="native-http-settle"
    )
    assert result.state == "native-returned"
    assert counters(data) == {**before, "http": before["http"] + 1}


@pytest.mark.parametrize("native_api_runtime", ["native-ws-handshake"], indirect=True)
def test_pinned_native_ws_handshake_then_frame_reuses_actual_branded_socket(native_api_runtime):
    data = native_api_runtime
    before = counters(data)
    operation = allocate_execute(data, "native-ws-handshake-allocation")
    bridge_for(data, operation).invoke(
        operation, claim_key="native-ws-handshake-claim", settle_key="native-ws-handshake-settle"
    )
    assert counters(data) == {**before, "ws_handshake": before["ws_handshake"] + 1}
    socket_ref = data.native_owner.socket_identity()
    assert type(socket_ref) is str and socket_ref
    request = data.native_owner.next_request("native-ws-frame")
    data.allocation = data.allocation.model_copy(
        update={"native_phase": "native-ws-frame", **request}
    )

    def record_second_operation_denial(response):
        if response.request.url.path == "/runtime-operations" and response.status_code >= 400:
            response.read()  # httpx hooks run before the body has been consumed.
            detail = response.json().get("detail")
            reason = (
                detail
                if detail in {"operation_uncertain", "operation_replayed", "binding_mismatch"}
                else "other_denial"
            )
            (data.marker.parent / "second-native-operation-denial.json").write_text(
                json.dumps({"status": response.status_code, "reason": reason}) + "\n"
            )

    data.http.event_hooks["response"].append(record_second_operation_denial)
    try:
        operation = allocate_execute(data, "native-ws-frame-allocation")
    finally:
        data.http.event_hooks["response"].remove(record_second_operation_denial)
    bridge_for(data, operation).invoke(
        operation, claim_key="native-ws-frame-claim", settle_key="native-ws-frame-settle"
    )
    assert counters(data) == {
        **before,
        "ws_handshake": before["ws_handshake"] + 1,
        "ws_frame": before["ws_frame"] + 1,
    }
    assert socket_ref == data.native_owner.socket_identity()
    # A completed operation's claim readback is not another continuation.
    before = counters(data)
    with pytest.raises(BindingUnavailable, match="not_continuation"):
        data.client.claim(operation, request_for(data, operation), "native-ws-frame-claim")
    assert counters(data) == before
    # Two reserved calls/exposure20 are cumulative; completed fences do not refund.
    request = data.native_owner.next_request("native-ws-frame")
    data.allocation = data.allocation.model_copy(
        update={"native_phase": "native-ws-frame", **request}
    )
    extra = allocate_execute(data, "native-ws-third-allocation")
    with pytest.raises(AdmissionDenied):
        data.adapter.verify("dispatch", expected_for(extra))
    with pytest.raises(BindingUnavailable):
        data.client.claim(extra, request_for(data, extra), "native-ws-third-claim")
    assert counters(data) == before


@pytest.mark.parametrize("native_api_runtime", [("native-ws-handshake", 3)], indirect=True)
def test_pinned_native_claimed_next_root_blocks_progression_with_budget_left(native_api_runtime):
    data = native_api_runtime
    first = allocate_execute(data, "claimed-next-handshake-allocation")
    bridge_for(data, first).invoke(
        first, claim_key="claimed-next-handshake-claim", settle_key="claimed-next-handshake-settle"
    )
    before = counters(data)
    request = data.native_owner.next_request("native-ws-frame")
    data.allocation = data.allocation.model_copy(
        update={"native_phase": "native-ws-frame", **request}
    )
    second = allocate_execute(data, "claimed-next-frame-allocation")
    data.adapter.verify("dispatch", expected_for(second))
    claim = data.client.claim(second, request_for(data, second), "claimed-next-frame-claim")
    assert claim.remaining_native_operations == 1  # Explicit initial human bound3.
    request = data.native_owner.next_request("native-ws-frame")
    data.allocation = data.allocation.model_copy(
        update={"native_phase": "native-ws-frame", **request}
    )
    with pytest.raises(BindingUnavailable):
        data.client.allocate(data.allocation, "claimed-next-fresh-allocation")
    assert counters(data) == before  # No frame/third effect; no refund or re-enrollment.


@pytest.mark.parametrize(
    "fault",
    [
        "guard-unknown",
        "quota-expired",
        "resource-revoked",
        "wire-mutated",
        "full-pin-mutated",
        "phase-order-mutated",
    ],
)
def test_pinned_native_owner_fault_denies_with_zero_effects(native_api_runtime, fault):
    data = native_api_runtime
    before = counters(data)
    operation = allocate_execute(data, "native-negative-allocation")
    data.native_owner.apply_fault(fault)
    with pytest.raises((AdmissionDenied, BindingUnavailable)):
        bridge_for(data, operation).invoke(
            operation, claim_key="native-negative-claim", settle_key="native-negative-settle"
        )
    assert counters(data) == before


@pytest.mark.parametrize(
    "native_api_runtime", ["native-http-fetch", "native-ws-handshake"], indirect=True
)
@pytest.mark.parametrize("fault", ["resource-revoked", "wire-mutated", "quota-expired"])
def test_pinned_native_live_fault_after_claim_blocks_canonical_continuation(
    native_api_runtime, fault
):
    data = native_api_runtime
    before = counters(data)
    operation = allocate_execute(data, "native-late-live-allocation")
    data.adapter.verify("dispatch", expected_for(operation))
    claim = data.client.claim(operation, request_for(data, operation), "native-late-live-claim")
    pipe = pipe_for(data, operation, claim)
    calls = []
    original = pipe.gate.owner_live_check

    def live_after_ready(current):
        calls.append(current.operation_ref)
        if len(calls) == 2:
            # Initial canonical entry check has passed. The pipe now received actual
            # native ready but has not sent invoke. Mutate same owned resource here.
            data.native_owner.apply_fault(fault)
        return original(current)

    pipe.gate.owner_live_check = live_after_ready
    with pytest.raises((BindingUnavailable, AdmissionDenied)):
        pipe.invoke(claim)
    assert len(calls) == 2
    assert counters(data) == before


def test_pinned_native_wrong_publisher_cannot_allocate(native_api_runtime):
    data = native_api_runtime
    before = counters(data)
    data.client.credential_reference = lambda: data.subject["Authorization"].removeprefix("Bearer ")
    with pytest.raises(BindingUnavailable):
        data.client.allocate(data.allocation, "native-wrong-publisher")
    assert counters(data) == before


@pytest.mark.parametrize(
    "field",
    ["nonce", "ticket", "request_ref", "final_fingerprint", "local_check_ref", "quota_check_ref"],
)
def test_pinned_native_actual_producer_reference_cannot_be_swapped(native_api_runtime, field):
    data = native_api_runtime
    before = counters(data)
    operation = allocate_execute(data, "native-swapped-allocation")
    payload = request_for(data, operation).model_copy(update={field: "0" * 64})
    with pytest.raises(BindingUnavailable):
        data.client.claim(operation, payload, "native-swapped-claim")
    assert counters(data) == before


def test_pinned_native_actual_lost_claim_reply_starts_no_native_continuation(native_api_runtime):
    data = native_api_runtime
    before = counters(data)
    operation = allocate_execute(data, "native-lost-reply-allocation")

    def lose_committed_reply(response):
        if response.status_code == 201 and response.request.url.path.endswith("/claim"):
            raise httpx.ReadError("test fault: lost committed native claim reply")

    data.http.event_hooks["response"].append(lose_committed_reply)
    try:
        with pytest.raises(BindingUnavailable):
            bridge_for(data, operation).invoke(
                operation,
                claim_key="native-lost-reply-claim",
                settle_key="native-lost-reply-settle",
            )
    finally:
        data.http.event_hooks["response"].clear()
    assert counters(data) == before
    with pytest.raises(BindingUnavailable):
        data.client.claim(operation, request_for(data, operation), "native-lost-reply-claim")


def test_pinned_native_actual_last_unit_concurrent_claim_and_restart_entry_fence(
    native_api_runtime,
):
    data = native_api_runtime
    before = counters(data)
    operation = allocate_execute(data, "native-concurrent-allocation")
    data.adapter.verify("dispatch", expected_for(operation))
    payload = request_for(data, operation)

    def claim(key):
        try:
            return data.client.claim(operation, payload, key)
        except BindingUnavailable:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["native-concurrent-a", "native-concurrent-b"]))
    claimed = [result for result in results if result is not None]
    assert len(claimed) == 1 and claimed[0].remaining_native_operations == 0
    assert pipe_for(data, operation, claimed[0]).invoke(claimed[0]) == "native-returned"
    after = counters(data)
    assert after == {**before, "http": before["http"] + 1}
    data.store = RuntimeBindingStore(data.state, adapter=data.native_owner)
    with pytest.raises(BindingUnavailable):
        pipe_for(data, operation, claimed[0]).invoke(claimed[0])
    assert counters(data) == after


def test_pinned_native_actual_uncertain_outcome_is_consumed_no_refund(native_api_runtime):
    data = native_api_runtime
    operation = allocate_execute(data, "native-uncertain-allocation")
    data.native_owner.apply_fault("lost-native-return-reply")
    with pytest.raises(BindingUnavailable):
        bridge_for(data, operation).invoke(
            operation, claim_key="native-uncertain-claim", settle_key="native-uncertain-settle"
        )
    before = counters(data)
    with pytest.raises(BindingUnavailable):
        data.client.allocate(data.allocation, "native-uncertain-new-key")
    assert counters(data) == before


@pytest.fixture
def canonical_api_runtime(tmp_path):
    yield from build_local_api_runtime(tmp_path)


def test_canonical_writer_fence_holds_until_child_exit_after_return_frame(canonical_api_runtime):
    """Targeted pipe-lifecycle repair, NOT branded native HTTP/WS acceptance."""
    data = canonical_api_runtime
    operation = data.client.allocate(data.allocation, "termination-fence-allocation")
    expected = data.registered.execute(data, operation)
    data.adapter.verify("dispatch", expected)
    claim = data.client.claim(
        operation, local_claim_request(data, operation), "termination-fence-claim"
    )
    pipe = local_pipe(data, operation, claim)
    metadata = {
        key: getattr(claim, key) for key in ("operation_ref", "request_ref", "final_fingerprint")
    }
    source = """import json,sys,time,sqlite3
x=json.loads(sys.argv[1]);print(json.dumps(dict(event="ready",**x)),flush=True)
if json.loads(sys.stdin.readline())!={"event":"invoke","operation_ref":x["operation_ref"]}:
 sys.exit(8)
print(json.dumps({"event":"native-returned","operation_ref":x["operation_ref"]}),flush=True)
time.sleep(.15)
db=sqlite3.connect(sys.argv[2],timeout=0)
try:
 db.execute("BEGIN IMMEDIATE");db.rollback();sys.exit(9)
except sqlite3.OperationalError:
 pass
finally:
 db.close()
"""
    pipe.command = (
        sys.executable,
        "-c",
        source,
        json.dumps(metadata),
        str(data.store.database_path),
    )
    assert pipe.invoke(claim) == "native-returned"
