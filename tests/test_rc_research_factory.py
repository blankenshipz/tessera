"""Actual factory diagnostics with local temp persistence; SDK/owner/auth fixtures only."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime
from itertools import islice
from pathlib import Path

import pytest
import test_guard_phase_join as join_fixtures

from tessera_agent.research_consumer_store import ConsumerBlocked
from tessera_agent.research_factory import PhaseDiagnosticPorts, ProductionResearchFactory
from tessera_mcp.guard_phase_evidence import CorePhaseContext, GuardPhaseExpectation
from tessera_mcp.guard_phase_join import OwnerPublisherTrust, SupervisedPhaseObserver

joined_fixture = join_fixtures.joined_fixture
sdk_channel = join_fixtures.sdk_channel


def factory_from(observer, owner, trust, *, publisher=None):
    return ProductionResearchFactory(
        phase_ports=PhaseDiagnosticPorts(
            observer_factory=lambda: observer,
            owner_publish=publisher or (lambda _: json.dumps(owner).encode()),
            owner_trust=trust,
        )
    )


def fresh(observer):
    return SupervisedPhaseObserver(
        read_phase=observer._read,
        consume_phase=observer._consume,
        expected=observer.expected,
        context=observer.context,
        clock=observer.clock,
    )


def test_default_factory_diagnostic_fails_closed_before_state(tmp_path):
    factory = ProductionResearchFactory()
    with pytest.raises(ConsumerBlocked, match="approved_phase_diagnostic_ports_missing"):
        factory.construct_phase_diagnostics(tmp_path / "state", role="planner")
    assert not (tmp_path / "state").exists()


def test_diagnostic_factory_read_consume_persists_and_restart_cannot_replay(
    joined_fixture, tmp_path
):
    observer, owner, trust = joined_fixture
    factory = factory_from(observer, owner, trust)
    diagnostic = factory.construct_phase_diagnostics(tmp_path, role="planner")
    try:
        assert diagnostic.read_record() is None
        assert diagnostic.read()["admission_allowed"] is False
        assert diagnostic.read()["production_verified"] is False
        consumed = diagnostic.consume()
        assert consumed["financial_freshness"] == "unknown"
        assert diagnostic.read_record()["state"] == "recorded"
    finally:
        diagnostic.close()
    restarted = factory_from(fresh(observer), owner, trust).construct_phase_diagnostics(
        tmp_path, role="planner"
    )
    try:
        assert restarted.read_record()["evidence"] == consumed
        assert restarted.read_record()["admission_allowed"] is False
        with pytest.raises(ConsumerBlocked, match="diagnostic_attempt_already_recorded"):
            restarted.consume()
    finally:
        restarted.close()


def test_owner_publisher_failure_is_sanitized_uncertain_and_durable(joined_fixture, tmp_path):
    observer, owner, trust = joined_fixture

    def denied(_):
        raise ValueError("PRIVATE_SENTINEL")

    diagnostic = factory_from(observer, owner, trust, publisher=denied).construct_phase_diagnostics(
        tmp_path, role="critic"
    )
    try:
        with pytest.raises(ConsumerBlocked) as caught:
            diagnostic.consume()
        assert str(caught.value) == "phase_diagnostic_consumption_uncertain"
        assert diagnostic.read_record() == {
            "state": "uncertain",
            "evidence": None,
            "admission_allowed": False,
            "production_verified": False,
        }
    finally:
        diagnostic.close()
    reopened = factory_from(fresh(observer), owner, trust).construct_phase_diagnostics(
        tmp_path, role="critic"
    )
    try:
        with pytest.raises(ConsumerBlocked, match="diagnostic_attempt_already_recorded"):
            reopened.consume()
    finally:
        reopened.close()


def test_corrupted_local_record_cannot_be_presented_as_evidence(joined_fixture, tmp_path):
    observer, owner, trust = joined_fixture
    diagnostic = factory_from(observer, owner, trust).construct_phase_diagnostics(
        tmp_path, role="planner"
    )
    try:
        diagnostic.consume()
        path = tmp_path / "phase-diagnostics/planner/phase-diagnostics.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("UPDATE evidence SET payload=?", ('{"admission_allowed":true}',))
        with pytest.raises(ConsumerBlocked, match="phase_diagnostic_record_integrity_changed"):
            diagnostic.read_record()
    finally:
        diagnostic.close()


@pytest.mark.parametrize("bad", [None, {"authorized": True}, False])
def test_config_booleans_cannot_create_installed_ports(tmp_path, bad):
    with pytest.raises(ConsumerBlocked):
        ProductionResearchFactory(phase_ports=bad).construct_phase_diagnostics(
            tmp_path, role="planner"
        )
    assert not (tmp_path / "phase-diagnostics").exists()


def test_diagnostic_never_enters_construct_or_session_path(joined_fixture, tmp_path, monkeypatch):
    observer, owner, trust = joined_fixture
    factory = factory_from(observer, owner, trust)

    def forbidden(*args, **kwargs):
        raise AssertionError("Research/admission/session construction forbidden")

    monkeypatch.setattr(factory, "construct", forbidden)
    monkeypatch.setattr(factory, "attested_observer", forbidden)
    diagnostic = factory.construct_phase_diagnostics(tmp_path, role="planner")
    try:
        assert diagnostic.consume()["production_verified"] is False
    finally:
        diagnostic.close()


def test_actual_public_guard_sdk_publisher_to_factory_durable_diagnostic(tmp_path):
    """Real copied public guard/publisher and SDK; synthetic auth/quota/AI only."""
    root = Path(
        os.environ.get(
            "TESSERA_OBSERVER_SOURCE",
            Path(__file__).parents[1] / ".tessera/worktrees/opencode-observability-v4",
        )
    )
    bun_ref = os.environ.get("TESSERA_OBSERVER_BUN") or shutil.which("bun")
    if not bun_ref:
        pytest.skip("Provide explicit TESSERA_OBSERVER_BUN or Bun on PATH")
    original = root / "packages/core/test/model-phase-service.test.ts"
    sdk = root / "packages/sdk/src/effect/model-phase.ts"
    effect = root / "packages/core/node_modules/effect/dist/index.js"
    guard = root / "harness/plugins/v2/subscription-guard.mjs"
    publisher = root / "harness/plugins/v2/guard-check-publisher.mjs"
    if not all(path.is_file() for path in (original, sdk, effect, guard, publisher)):
        pytest.skip("Exact isolated source/read-only dependencies required; no install fallback")
    prefix = join_fixtures.public_fixture_setup(original, root, sdk, effect)
    # Select the named COMPLETE public guard test; source additions must not turn
    # fixed line slices into partial/unrelated TypeScript bodies.
    marker = 'test("actual public guard runs in actual preparation service'
    with original.open() as stream:
        for line in stream:
            if line.startswith(marker):
                break
        else:
            raise AssertionError("Actual public guard fixture marker unavailable")
        lines = [line]
        for line in islice(stream, 349):
            if line.startswith(("test(", "for (")):
                break
            lines.append(line)
        else:
            raise AssertionError("Public guard fixture exceeds350line bound or is incomplete")
    native_test = "".join(lines)
    assert native_test.rstrip().endswith("})"), "Incomplete actual public guard fixture"
    native_test = native_test.replace(
        '"../../../harness/plugins/v2/subscription-guard.mjs"', json.dumps(str(guard))
    )
    native_test = native_test.replace(
        '"../../../harness/plugins/v2/guard-check-publisher.mjs"', json.dumps(str(publisher))
    )
    native_test = native_test.replace(
        "const phase = yield* channel",
        """const read = yield* channel
          .read("ses_fixture", challenge().nonce)
          .pipe(Effect.provideService(ModelPhaseAuthority, current))
        const phase = yield* channel""",
    )
    native_test = native_test.replace(
        "expect(dispatches).toBe(0)",
        """expect(dispatches).toBe(0)
        console.log("PUBLIC_GUARD_JOIN:"+JSON.stringify({read,phase,ownerChecks,network_calls:dispatches}))""",
    )
    helper = tmp_path / "public-guard-factory.test.ts"
    helper.write_text(prefix + native_test)
    before = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (sdk, guard, publisher)
    }
    result = subprocess.run(
        [bun_ref, "test", str(helper)],
        cwd=tmp_path,
        env={
            "BUN_RUNTIME_TRANSPILER_CACHE_PATH": str(tmp_path / "cache"),
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert all(
        hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
        for path, digest in before.items()
    )
    line = next(
        line for line in result.stdout.splitlines() if line.startswith("PUBLIC_GUARD_JOIN:")
    )
    output = json.loads(line.removeprefix("PUBLIC_GUARD_JOIN:"))
    assert output["network_calls"] == 0
    assert "synthetic-only" not in line

    def digest(value):
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    expected = GuardPhaseExpectation(
        "https://guard.example.test",
        "a" * 64,
        hashlib.sha256(b"ses_fixture").hexdigest(),
        "c" * 64,
        4,
        1,
        "model",
        digest({"provider": "openai"}),
        digest({"provider": "openai", "model_id": "model"}),
        digest([{"name": "tessera_status", "schema": {"type": "object", "properties": []}}]),
    )
    context = CorePhaseContext(
        expected.issuer_origin,
        "e" * 64,
        "f" * 64,
        expected.nonce,
        expected.session_ref,
        expected.proof_ref,
    )
    now = datetime.fromisoformat(output["phase"]["observed_at"].replace("Z", "+00:00"))

    def observe():
        return SupervisedPhaseObserver(
            read_phase=lambda: json.dumps(output["read"]).encode(),
            consume_phase=lambda: json.dumps(output["phase"]).encode(),
            expected=expected,
            context=context,
            clock=lambda: now,
        )

    owner_bytes = json.dumps(output["ownerChecks"]).encode()
    factory = ProductionResearchFactory(
        phase_ports=PhaseDiagnosticPorts(
            observer_factory=observe,
            owner_publish=lambda _: owner_bytes,
            owner_trust=OwnerPublisherTrust(expected.issuer_origin, "d" * 64),
        )
    )
    diagnostic = factory.construct_phase_diagnostics(tmp_path / "state", role="planner")
    try:
        assert diagnostic.read()["production_verified"] is False
        saved = diagnostic.consume()
        assert saved["owner_checks"]["local_checks"]["account_match"] == "passed"
        assert saved["owner_checks"]["provider_facts"]["entitlement"] == "unknown"
        assert saved["admission_allowed"] is False and saved["production_verified"] is False
    finally:
        diagnostic.close()
    restarted = factory.construct_phase_diagnostics(tmp_path / "state", role="planner")
    try:
        assert restarted.read_record()["evidence"] == saved
        with pytest.raises(ConsumerBlocked, match="diagnostic_attempt_already_recorded"):
            restarted.consume()
    finally:
        restarted.close()
    print("Actual public guard joined diagnostic SHA256:", digest(saved))
