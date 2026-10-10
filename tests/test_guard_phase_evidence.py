"""Synthetic phase-receipt contract fixtures; no core producer or live proof."""

import copy
import hashlib
import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tessera_mcp.guard_phase_evidence import (
    CorePhaseContext,
    GuardPhaseExpectation,
    PhaseEvidenceRejected,
    parse_core_model_hook_phase,
    parse_guard_phase_evidence,
    phase_fingerprint,
)


@pytest.fixture
def receipt():
    expected = GuardPhaseExpectation(
        issuer_origin="https://research.example.test",
        nonce="1" * 64,
        session_ref="2" * 64,
        proof_ref="3" * 64,
        order_revision=4,
        sequence=5,
        model_id="public-model",
        provider_sha256="a" * 64,
        model_sha256="b" * 64,
        tool_policy_sha256="c" * 64,
    )
    projection = {
        name: getattr(expected, name)
        for name in ("model_id", "provider_sha256", "model_sha256", "tool_policy_sha256")
    }
    projection["provider"] = "openai"
    projection["fingerprint"] = phase_fingerprint(
        order_revision=4,
        sequence=5,
        **{key: value for key, value in projection.items() if key != "provider"},
    )
    value = {
        "protocol": "tessera-guard-phase-evidence-v1",
        "phase": "post-model-request-hooks",
        "complete": False,
        "phase_finality": "unproven",
        "ordering_enforced": False,
        "nonce": expected.nonce,
        "session_ref": expected.session_ref,
        "proof_ref": expected.proof_ref,
        "issuer_origin": expected.issuer_origin,
        "order_revision": 4,
        "sequence": 5,
        "observed_at": "2026-10-06T00:00:00Z",
        "valid_until": "2026-10-06T00:00:30Z",
        "projection": projection,
        "local_checks": {
            "evidence_class": "local_measured_enforcement",
            "openai_only": "passed",
            "chatgpt_route": "passed",
            "chatgpt_oauth_method": "passed",
            "account_match": "passed",
            "api_key_overrides_absent": "passed",
        },
        "financial_settings": {
            "evidence_class": "operator_attested_financial_settings",
            "auto_top_up_off": True,
            "automatic_reload_off": True,
            "purchased_credit_balance_zero": True,
            "verified_at": "2026-09-08",
            "freshness": "unknown",
        },
        "provider_facts": {
            "evidence_class": "provider_unknown",
            "entitlement": "unknown",
            "billing_guarantee": "unknown",
        },
        "quota": {
            "evidence_class": "local_measured_enforcement",
            "outcome": "passed",
            "checked_at": "2026-10-06T00:00:00Z",
            "snapshot_checked_at": "2026-10-06T00:00:00Z",
            "valid_until": "2026-10-06T00:00:30Z",
        },
    }
    return value, expected, datetime(2026, 10, 6, 0, 0, 10, tzinfo=UTC)


def parse(value, expected, now):
    return parse_guard_phase_evidence(json.dumps(value).encode(), expected=expected, now=now)


def test_receipt_never_establishes_admission_or_production_truth(receipt):
    value, expected, now = receipt
    report = parse(value, expected, now)
    assert report.production_verified is False
    assert report.admission_allowed is False
    assert report.origin_authentication == "not_observed_by_parser"
    assert report.financial_freshness == "unknown"
    assert report.receipt.financial_settings.verified_at == "2026-09-08"
    with pytest.raises(PhaseEvidenceRejected):
        report.admission_fields()


@pytest.mark.parametrize(
    "field",
    [
        "nonce",
        "session_ref",
        "proof_ref",
        "issuer_origin",
        "order_revision",
        "sequence",
        "phase",
        "phase_finality",
        "complete",
        "ordering_enforced",
    ],
)
def test_binding_order_phase_and_false_finality_rejected(receipt, field):
    value, expected, now = receipt
    original = value[field]
    value[field] = (
        True if type(original) is bool else original + 1 if type(original) is int else "changed"
    )
    with pytest.raises(PhaseEvidenceRejected):
        parse(value, expected, now)


@pytest.mark.parametrize(
    "field",
    [
        "provider",
        "model_id",
        "provider_sha256",
        "model_sha256",
        "tool_policy_sha256",
        "fingerprint",
    ],
)
def test_late_hook_or_projection_drift_rejected(receipt, field):
    value, expected, now = receipt
    value["projection"][field] = "0" * 64
    with pytest.raises(PhaseEvidenceRejected):
        parse(value, expected, now)


@pytest.mark.parametrize(
    "section,field,timestamp",
    [
        (None, "observed_at", "2026-10-06T00:00:11Z"),
        (None, "valid_until", "2026-10-06T00:00:10Z"),
        (None, "valid_until", "2026-10-06T00:00:31Z"),
        ("quota", "snapshot_checked_at", "2026-10-05T23:59:39Z"),
        ("quota", "snapshot_checked_at", "2026-10-06T00:00:11Z"),
        ("quota", "valid_until", "2026-10-06T00:00:10Z"),
        ("quota", "checked_at", "2026-10-06T00:00:11Z"),
    ],
)
def test_stale_future_and_ttl_negative(receipt, section, field, timestamp):
    value, expected, now = receipt
    target = value if section is None else value[section]
    target[field] = timestamp
    with pytest.raises(PhaseEvidenceRejected):
        parse(value, expected, now)


@pytest.mark.parametrize(
    "field",
    [
        "account_id",
        "connection_id",
        "headers",
        "authorization",
        "raw_prompt",
        "reasoning",
        "credentials",
        "config",
        "providerContext",
    ],
)
def test_forbidden_fields_redacted(receipt, field):
    value, expected, now = receipt
    value["local_checks"][field] = "PRIVATE_SENTINEL"
    with pytest.raises(PhaseEvidenceRejected) as caught:
        parse(value, expected, now)
    assert str(caught.value) == "guard_phase_evidence_invalid_or_not_admitting"
    assert "PRIVATE_SENTINEL" not in str(caught.value)


def test_evidence_classes_cannot_promote_operator_or_unknown_provider_facts(receipt):
    value, expected, now = receipt
    for section, field, changed in [
        ("financial_settings", "evidence_class", "local_measured_enforcement"),
        ("financial_settings", "freshness", "fresh"),
        ("provider_facts", "entitlement", "verified"),
    ]:
        altered = copy.deepcopy(value)
        altered[section][field] = changed
        with pytest.raises(PhaseEvidenceRejected):
            parse(altered, expected, now)


def test_quota_unavailable_is_not_exhaustion_or_fallback(receipt):
    value, expected, now = receipt
    value["quota"]["outcome"] = "unavailable"
    value["quota"]["snapshot_checked_at"] = None
    report = parse(value, expected, now)
    assert report.receipt.quota.outcome == "unavailable"
    assert report.admission_allowed is False
    value["quota"]["outcome"] = "passed"
    with pytest.raises(PhaseEvidenceRejected):
        parse(value, expected, now)


def test_config_getters_never_traversed(receipt):
    _, expected, now = receipt

    class Poison(dict):
        def items(self):
            raise AssertionError("PRIVATE_GETTER_TRAVERSED")

        def __getitem__(self, key):
            raise AssertionError("PRIVATE_GETTER_TRAVERSED")

    with pytest.raises(PhaseEvidenceRejected):
        parse_guard_phase_evidence(Poison(), expected=expected, now=now)


def test_duplicate_keys_and_boolean_coercion_rejected(receipt):
    value, expected, now = receipt
    raw = json.dumps(value).replace('"complete": false', '"complete": false, "complete": false')
    with pytest.raises(PhaseEvidenceRejected):
        parse_guard_phase_evidence(raw.encode(), expected=expected, now=now)
    value["complete"] = 0
    with pytest.raises(PhaseEvidenceRejected):
        parse(value, expected, now)


@pytest.fixture(scope="module")
def actual_core(tmp_path_factory):
    core = Path(__file__).parents[1] / ".tessera/worktrees/opencode-observability-v4"
    source = core / "packages/core/src/plugin/model-phase.ts"
    effect = core / "packages/core/node_modules/effect/dist/index.js"
    # Opt-in executable supplied by the operator; never assume a personal install.
    bun_path = os.environ.get("TESSERA_TEST_BUN")
    if not bun_path:
        pytest.skip("Opt-in TESSERA_TEST_BUN executable not configured")
    bun = Path(bun_path)
    if not bun.is_absolute():
        pytest.skip("Opt-in TESSERA_TEST_BUN must be an absolute executable path")
    if not all(path.is_file() for path in (source, effect, bun)):
        pytest.skip("Opt-in isolated core/Bun/Effect source prerequisites unavailable")
    root = tmp_path_factory.mktemp("core-phase-interop")
    script = root / "produce.ts"
    script.write_text(
        f"import {{ ModelPhaseOrder }} from {json.dumps(str(source))};\n"
        f"import {{ Effect }} from {json.dumps(str(effect))};\n"
        + """
const order = new ModelPhaseOrder();
const input = {provider:'openai',model_id:'public-model',tools:[
 {name:'tessera_status',inputSchema:{type:'object',properties:{query:{type:'string'}}}}
]};
Object.defineProperty(input,'headers',{get(){throw Error('UNSAFE_SENTINEL')}});
const challenge={nonce:'1'.repeat(64),session_ref:'2'.repeat(64),proof_ref:'3'.repeat(64),
 issuer_origin:'https://research.example.test'};
const stage=await Effect.runPromise(order.execute(input,[event=>Effect.sync(()=>{})]));
const lease=order.capture(stage.ticket,challenge,()=>input,
 ()=>Date.parse('2026-10-06T00:00:00Z'));
if(process.argv[2]==='late-order')order.changed();
try {console.log(JSON.stringify(lease.consume()));}
catch {console.log(JSON.stringify({rejected:true}));}
"""
    )
    env = {
        "BUN_RUNTIME_TRANSPILER_CACHE_PATH": str(root / "cache"),
        "BUN_INSTALL_CACHE_DIR": str(root / "install-cache"),
        "HOME": str(root),
        "PATH": "/usr/bin:/bin",
    }
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    result = subprocess.run(
        [str(bun), str(script)], cwd=root, env=env, capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, "Isolated core executable failed"
    assert "UNSAFE_SENTINEL" not in result.stdout + result.stderr
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before

    # Python constructs expected safe structural-policy digests independently.
    def digest(value):
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    policy = [
        {
            "name": "tessera_status",
            "schema": {"type": "object", "properties": [["query", {"type": "string"}]]},
        }
    ]
    expected = GuardPhaseExpectation(
        "https://research.example.test",
        "1" * 64,
        "2" * 64,
        "3" * 64,
        1,
        1,
        "public-model",
        digest({"provider": "openai"}),
        digest({"provider": "openai", "model_id": "public-model"}),
        digest(policy),
    )
    context = CorePhaseContext(
        expected.issuer_origin,
        "4" * 64,
        "5" * 64,
        expected.nonce,
        expected.session_ref,
        expected.proof_ref,
    )
    return result.stdout.strip().encode(), expected, context, root, script, bun, env


def test_actual_core_executor_python_adapter_cross_language_hash(actual_core):
    raw, expected, context, *_ = actual_core
    report = parse_core_model_hook_phase(
        raw, expected=expected, context=context, now=datetime(2026, 10, 6, 0, 0, 10, tzinfo=UTC)
    )
    assert report.receipt.protocol == "opencode-model-hook-phase-v1"
    assert report.production_verified is False
    assert report.consumption_verified is False
    assert report.admission_allowed is False
    assert report.receipt.financial_settings.observation == "not_supplied"
    assert "quota_outcome" in report.missing
    assert not hasattr(report.receipt, "quota")
    with pytest.raises(PhaseEvidenceRejected):
        report.admission_fields()
    print("Core partial JSON fixture SHA256:", hashlib.sha256(raw).hexdigest())


def test_actual_core_rechecks_late_order_before_serialization(actual_core):
    _, _, _, root, script, bun, env = actual_core
    result = subprocess.run(
        [str(bun), str(script), "late-order"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"rejected": True}


@pytest.mark.parametrize(
    "mutation",
    [
        "nonce",
        "order",
        "sequence",
        "fingerprint",
        "policy",
        "stale",
        "future",
        "complete",
        "headers",
        "raw_snapshot",
        "quota",
        "financial",
        "origin",
    ],
)
def test_core_partial_adapter_tamper_and_missing_facts(actual_core, mutation):
    raw, expected, context, *_ = actual_core
    value = json.loads(raw)
    if mutation == "nonce":
        value["nonce"] = "0" * 64
    elif mutation in {"order", "sequence"}:
        value["order_revision" if mutation == "order" else "sequence"] += 1
    elif mutation in {"fingerprint", "policy"}:
        value["projection"][
            "fingerprint" if mutation == "fingerprint" else "tool_policy_sha256"
        ] = "0" * 64
    elif mutation == "stale":
        value["valid_until"] = "2026-10-06T00:00:10Z"
    elif mutation == "future":
        value["observed_at"] = "2026-10-06T00:00:11Z"
    elif mutation == "complete":
        value["complete"] = True
    elif mutation == "financial":
        value["financial_settings"]["observation"] = "verified"
    elif mutation == "origin":
        value["issuer_origin"] = "https://other.example.test"
    else:
        value[mutation] = {"PRIVATE_SENTINEL": True}
    with pytest.raises(PhaseEvidenceRejected) as caught:
        parse_core_model_hook_phase(
            json.dumps(value).encode(),
            expected=expected,
            context=context,
            now=datetime(2026, 10, 6, 0, 0, 10, tzinfo=UTC),
        )
    assert "PRIVATE_SENTINEL" not in str(caught.value)


@pytest.mark.parametrize(
    "field", ["bound_origin", "nonce", "session_ref", "proof_ref", "enrollment_ref"]
)
def test_core_context_cannot_be_supplied_by_receipt(actual_core, field):
    raw, expected, context, *_ = actual_core
    changed = "https://other.example.test" if field == "bound_origin" else "invalid-context"
    with pytest.raises(PhaseEvidenceRejected):
        parse_core_model_hook_phase(
            raw,
            expected=expected,
            context=replace(context, **{field: changed}),
            now=datetime(2026, 10, 6, 0, 0, 10, tzinfo=UTC),
        )
