"""v2 runtime-admission evidence is OpenCode-only: no Anthropic evidence admits (ADR 0082)."""

import json

import pytest
import test_runtime_admission as base

from tessera_mcp import runtime_admission as ra
from tessera_mcp.runtime_admission import AdmissionDenied, Evidence, LocalChecks

rig = base.synthetic_v2

MODEL = "anthropic/claude-sonnet-5-5"


def anthropic_shaped_local(model=MODEL):
    fields = {name: "passed" for name in LocalChecks.model_fields}
    for name in ("openai_only", "chatgpt_endpoint"):
        del fields[name]
    fields.update(
        anthropic_only="passed",
        anthropic_endpoint="passed",
        evidence_class="completed-native-local-enforcement",
        provider="anthropic",
        model=model,
    )
    return fields


def run(rig, model):
    verifier = rig.make()
    verifier.model = model
    raw = json.dumps(rig.receipt).encode()
    return verifier.verify(raw, operation="session", expected=rig.expected)


def test_anthropic_local_checks_variant_is_gone():
    assert not hasattr(ra, "AnthropicLocalChecks")
    assert ra.expected_provider("anthropic/claude-sonnet-5-5") is None
    assert ra.expected_provider("openai/m") == "openai"
    assert ra.expected_provider("legacy-model") == "openai"
    assert ra.expected_provider("other/m") is None


def test_anthropic_shaped_evidence_does_not_validate(rig):
    with pytest.raises(ValueError):
        Evidence.model_validate({**rig.receipt, "local": anthropic_shaped_local()})
    with pytest.raises(ValueError):
        LocalChecks(**anthropic_shaped_local())


def test_anthropic_evidence_denied_for_anthropic_model(rig):
    rig.receipt["local"] = anthropic_shaped_local()
    with pytest.raises(AdmissionDenied):
        run(rig, MODEL)


def test_openai_evidence_cannot_admit_anthropic_model(rig):
    # Valid OpenAI-shaped evidence for the legacy pinned model must not admit an Anthropic pin.
    with pytest.raises(AdmissionDenied, match="v2_admission_denied_or_uncertain"):
        run(rig, MODEL)


def test_unqualified_or_unknown_model_never_matches_anthropic(rig):
    rig.receipt["local"] = anthropic_shaped_local("gemini/x")
    with pytest.raises(AdmissionDenied):
        run(rig, "gemini/x")


def test_openai_evidence_still_admits_legacy_model(rig):
    verifier = rig.make()
    raw = json.dumps(rig.receipt).encode()
    assert verifier.verify(raw, operation="session", expected=rig.expected)["state"]
