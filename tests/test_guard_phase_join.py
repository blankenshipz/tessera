"""Actual isolated SDK/service execution plus synthetic owner callback receipts.

Upstream primitives and authority are explicit fixtures; no actual guard/auth or
provider dispatch. Python callback fixture outputs are serialized SDK results,
not a claimed live cross-process capability.
"""

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime
from itertools import islice
from pathlib import Path

import pytest

from tessera_mcp.guard_phase_evidence import (
    CorePhaseContext,
    GuardPhaseExpectation,
    PhaseEvidenceRejected,
)
from tessera_mcp.guard_phase_join import OwnerPublisherTrust, SupervisedPhaseObserver


def public_fixture_setup(original, root, sdk, effect):
    """Reuse a complete bounded setup, not a stale numeric line prefix.

    Only the original fixture's upstream resource doubles are reused. Actual
    PluginHooks/preparation/SDK channel functions are never replaced or patched.
    """
    marker = 'test("actual preparation service publishes read/consume SDK channel'
    lines = []
    with original.open() as stream:
        for line in islice(stream, 350):
            if line.startswith(marker):
                break
            lines.append(line)
        else:
            raise AssertionError("Complete public fixture setup marker unavailable within350lines")
    prefix = "".join(lines)
    for required in ("const run =", "function input()", "const authority ="):
        assert required in prefix, "Incomplete actual preparation fixture setup"
    replacements = [
        ('from "effect"', f"from {json.dumps(str(effect))}"),
        (
            'from "effect/unstable/http"',
            f"from {json.dumps(str(effect.parent / 'unstable/http/index.js'))}",
        ),
        (
            '"../../ai/src/route/terminal-boundary.js"',
            json.dumps(str(root / "packages/ai/src/route/terminal-boundary.ts")),
        ),
        ('"../src/plugin/hooks.js"', json.dumps(str(root / "packages/core/src/plugin/hooks.ts"))),
        (
            '"../src/session/model-request.js"',
            json.dumps(str(root / "packages/core/src/session/model-request.ts")),
        ),
        ('"../src/session/model-phase-channel.js"', json.dumps(str(sdk))),
        (
            "new URL(relative, import.meta.url)",
            f"new URL(relative, {json.dumps(original.as_uri())})",
        ),
    ]
    for old, new in replacements:
        assert old in prefix, "Actual fixture import/layout changed"
        prefix = prefix.replace(old, new)
    return prefix


@pytest.fixture(scope="module")
def sdk_channel(tmp_path_factory):
    root = Path(
        os.environ.get(
            "TESSERA_OBSERVER_SOURCE",
            Path(__file__).parents[1] / ".tessera/worktrees/opencode-observability-v4",
        )
    )
    original = root / "packages/core/test/model-phase-service.test.ts"
    sdk = root / "packages/sdk/src/effect/model-phase.ts"
    effect = root / "packages/core/node_modules/effect/dist/index.js"
    bun_ref = os.environ.get("TESSERA_OBSERVER_BUN") or shutil.which("bun")
    if not bun_ref:
        pytest.skip("Provide TESSERA_OBSERVER_BUN or Bun on PATH; no toolchain install")
    bun = Path(bun_ref)
    if not all(path.is_file() for path in (original, sdk, effect, bun)):
        pytest.skip("Isolated SDK/Bun/Effect prerequisites unavailable")
    temp = tmp_path_factory.mktemp("sdk-owner-join")
    prefix = public_fixture_setup(original, root, sdk, effect)
    prefix = prefix.replace("{ Context, Effect, Layer }", "{ Context, Effect, Layer, Exit }")
    prefix = prefix.replace(
        "{ Context, Effect, Layer, Scope }", "{ Context, Effect, Layer, Scope, Exit }"
    )
    program = temp / "channel.test.ts"
    program.write_text(
        prefix
        + """
test('SDK channel interop safe output only', async()=> {
 const result=await run(()=>Effect.gen(function*(){
   const channel=makeModelPhaseChannel();
   yield* channel.prepare('ses_fixture',challenge(),input() as any);
   const read=yield* channel.read('ses_fixture',challenge().nonce);
   const consumed=yield* channel.consume('ses_fixture',challenge().nonce);
   yield* channel.prepare('ses_fixture',challenge('d'.repeat(64)),input() as any);
   const hooks=yield* PluginHooks.Service;
   yield* hooks.register('session','model.request',()=>Effect.void);
   const late=yield* Effect.exit(channel.consume('ses_fixture','d'.repeat(64)));
   return {read,consumed,late_order_rejected:Exit.isFailure(late),network_calls:dispatches,
           authority:'synthetic_fixture_only'};
 }).pipe(Effect.provideService(ModelPhaseAuthority,authority())));
 expect(result.network_calls).toBe(0);
 expect(result.late_order_rejected).toBe(true);
 console.log('SAFE_SDK_JSON:'+JSON.stringify(result));
});
"""
    )
    before = hashlib.sha256(sdk.read_bytes()).hexdigest()
    env = {
        "BUN_RUNTIME_TRANSPILER_CACHE_PATH": str(temp / "cache"),
        "HOME": str(temp),
        "PATH": "/usr/bin:/bin",
    }
    run = subprocess.run(
        [str(bun), "test", str(program)],
        cwd=temp,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert hashlib.sha256(sdk.read_bytes()).hexdigest() == before
    line = next(line for line in run.stdout.splitlines() if line.startswith("SAFE_SDK_JSON:"))
    result = json.loads(line.removeprefix("SAFE_SDK_JSON:"))
    assert result["network_calls"] == 0 and result["late_order_rejected"] is True
    assert result["authority"] == "synthetic_fixture_only"
    receipt = result["consumed"]

    def digest(value):
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    expected = GuardPhaseExpectation(
        "https://guard.example.test",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        1,
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
    now = datetime.fromisoformat(receipt["observed_at"].replace("Z", "+00:00"))
    return result, expected, context, now


@pytest.fixture
def joined_fixture(sdk_channel):
    sdk, expected, context, now = sdk_channel
    read, consumed = json.dumps(sdk["read"]).encode(), json.dumps(sdk["consumed"]).encode()
    observer = SupervisedPhaseObserver(
        read_phase=lambda: read,
        consume_phase=lambda: consumed,
        expected=expected,
        context=context,
        clock=lambda: now,
    )
    core = sdk["consumed"]
    owner = {
        "protocol": "tessera-owner-guard-checks-v1",
        "publisher_ref": "1" * 64,
        "issuer_origin": "https://owner-publisher.example.test",
        **{
            field: core[field]
            for field in (
                "nonce",
                "session_ref",
                "proof_ref",
                "phase",
                "order_revision",
                "sequence",
            )
        },
        "executed_phase": "model.request",
        "projection_fingerprint": core["projection"]["fingerprint"],
        "model_id": "model",
        "executed_at": core["observed_at"],
        "valid_until": core["valid_until"],
        "local_checks": {
            "evidence_class": "local_measured_enforcement",
            "openai_only": "passed",
            "chatgpt_route": "passed",
            "chatgpt_oauth_method": "passed",
            "account_match": "passed",
            "api_key_overrides_absent": "passed",
        },
        "quota": {
            "evidence_class": "local_measured_enforcement",
            "outcome": "passed",
            "checked_at": core["observed_at"],
            "snapshot_checked_at": core["observed_at"],
            "valid_until": core["valid_until"],
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
    }
    trust = OwnerPublisherTrust(owner["issuer_origin"], owner["publisher_ref"])
    return observer, owner, trust


def test_actual_sdk_channel_then_python_join_nonadmitting(joined_fixture):
    observer, owner, trust = joined_fixture
    assert observer.read().consumption_verified is False
    joined = observer.consume_and_join(
        owner_publish=lambda _: json.dumps(owner).encode(), owner_trust=trust
    )
    assert joined.production_verified is False and joined.admission_allowed is False
    assert joined.notebook_projection()["financial_freshness"] == "unknown"
    with pytest.raises(PhaseEvidenceRejected):
        joined.admission_fields()
    with pytest.raises(PhaseEvidenceRejected):
        observer.consume()
    print(
        "SDK partial receipt SHA256:",
        hashlib.sha256(joined.phase.receipt.model_dump_json().encode()).hexdigest(),
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "nonce",
        "phase",
        "order_revision",
        "sequence",
        "projection_fingerprint",
        "issuer_origin",
        "model_id",
        "stale",
        "quota_denied",
        "quota_unavailable",
        "cache_age",
        "financial",
        "missing_verified",
        "financial_measured",
        "entitlement",
        "account_id",
        "headers",
        "raw_prompt",
        "authorized",
    ],
)
def test_separate_owner_facts_mismatch_and_private_fields_rejected(joined_fixture, mutation):
    observer, owner, trust = joined_fixture
    if mutation in {"nonce", "phase", "projection_fingerprint", "issuer_origin", "model_id"}:
        owner[mutation] = "changed"
    elif mutation in {"order_revision", "sequence"}:
        owner[mutation] += 1
    elif mutation == "stale":
        owner["valid_until"] = owner["executed_at"]
    elif mutation.startswith("quota_"):
        owner["quota"]["outcome"] = mutation.removeprefix("quota_")
    elif mutation == "cache_age":
        owner["quota"]["snapshot_checked_at"] = "2020-01-01T00:00:00Z"
    elif mutation == "financial":
        owner["financial_settings"]["auto_top_up_off"] = False
    elif mutation == "missing_verified":
        owner["financial_settings"]["verified_at"] = None
    elif mutation == "financial_measured":
        owner["financial_settings"]["evidence_class"] = "local_measured_enforcement"
    elif mutation == "entitlement":
        owner["provider_facts"]["entitlement"] = "verified"
    else:
        owner[mutation] = "PRIVATE_SENTINEL"
    with pytest.raises(PhaseEvidenceRejected) as caught:
        observer.consume_and_join(
            owner_publish=lambda _: json.dumps(owner).encode(), owner_trust=trust
        )
    assert "PRIVATE_SENTINEL" not in str(caught.value)


def test_failed_consume_cannot_retry_or_invent_authority(sdk_channel):
    _, expected, context, now = sdk_channel

    def denied():
        raise ValueError("PRIVATE_SENTINEL")

    observer = SupervisedPhaseObserver(
        read_phase=denied,
        consume_phase=denied,
        expected=expected,
        context=context,
        clock=lambda: now,
    )
    with pytest.raises(PhaseEvidenceRejected):
        observer.consume()
    with pytest.raises(PhaseEvidenceRejected):
        observer.consume()
