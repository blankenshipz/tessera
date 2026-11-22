"""ADR 0082 slice 1: operator-attested Anthropic destination beside OpenAI (synthetic fixtures)."""

import copy
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from tessera_agent import model_destinations as md
from tessera_agent.remote_reasoning_runtime import validate_profile
from tessera_agent.research_consumer import ResearchConsumer
from tessera_agent.research_consumer_store import ConsumerBlocked
from tessera_agent.research_service import ResearchReferences
from tessera_mcp.harness_binding import (
    ACCOUNT_MODE,
    PERMITTED_TOOLS,
    SKILL_ID,
    BindingBlocked,
    HarnessBinding,
    catalog_sha256,
    generate_binding,
    sha256,
    verify_binding,
)
from tessera_mcp.harness_observer import ObservationBlocked, V2PublicHarnessObserver

REF = "anthropic-api-key://owner-1/ref-1"
ANTHROPIC = "anthropic/claude-sonnet-synthetic"
OPENAI = "openai/synthetic-oauth-model"


def binding_arguments(**overrides):
    skill = (Path(__file__).parents[1] / "skills" / SKILL_ID / "SKILL.md").read_bytes()
    catalog = [
        {"name": name, "inputSchema": {"type": "object"}, "description": "Synthetic reviewed tool"}
        for name in PERMITTED_TOOLS
    ]
    values = {
        "installed_python": "/opt/tessera/bin/python",
        "workspace": "/srv/research/run",
        "credential_directory": "/run/credentials/tessera-reasoning.service",
        "skill_source": "/srv/research/pinned-skills",
        "skill": skill,
        "expected_skill_sha256": sha256(skill),
        "catalog": catalog,
        "expected_catalog_sha256": catalog_sha256(catalog),
        "project_id": "project_test",
        "subject_credential_id": "credential_test",
        "destination": "openai:approved",
        "control_url": "https://control.example.test",
        "model": OPENAI,
        "approved_models": (OPENAI,),
        "harness_version": "2.synthetic",
        "harness_schema_sha256": "1" * 64,
        "installed_build_sha256": "2" * 64,
        "role": "planner",
        "permitted_tools": PERMITTED_TOOLS,
    }
    values.update(overrides)
    return values


def anthropic_arguments(**overrides):
    base = {
        "model": ANTHROPIC,
        "approved_models": (ANTHROPIC,),
        "account_mode": md.ANTHROPIC_ACCOUNT_MODE,
    }
    base.update(overrides)
    return binding_arguments(**base)


def observations(binding, args, provider, model_id, account_mode, **extra):
    manifest = json.loads(binding.manifest)
    return {
        "effective_config": json.loads(binding.config),
        "selected_skill": args["skill"],
        "observed_catalog": args["catalog"],
        "provider_id": provider,
        "model_id": model_id,
        "account_mode": account_mode,
        "included_oauth_verified": True,
        "observed_harness_version": "2.synthetic",
        "observed_harness_schema_sha256": "1" * 64,
        "observed_build_sha256": "2" * 64,
        "observed_mcp_sdk_version": "1.26.0",
        "expected_binding_sha256": binding.digest,
        "principal_status": {
            "credential_id": "credential_test",
            "project_id": "project_test",
            "destination": "openai:approved",
            "actions": manifest["required_enrollment_scopes"],
            "expires_at": "2030-01-01T01:00:00Z",
        },
        "now": datetime(2030, 1, 1, tzinfo=UTC),
        **extra,
    }


# ---- harness binding -------------------------------------------------------


def test_openai_binding_unchanged_and_has_no_anthropic_fields():
    args = binding_arguments()
    binding = generate_binding(**args)
    manifest = json.loads(binding.manifest)
    assert manifest["account_mode"] == ACCOUNT_MODE
    assert "model_credential_ref" not in manifest
    policies = json.loads(binding.config)["experimental"]["policies"]
    assert {"action": "provider.use", "resource": "openai", "effect": "allow"} in policies
    verify_binding(
        binding, **observations(binding, args, "openai", "synthetic-oauth-model", ACCOUNT_MODE)
    )


@pytest.mark.parametrize("mode", [md.ANTHROPIC_ACCOUNT_MODE])
def test_anthropic_binding_refused_on_opencode_path(mode):
    with pytest.raises(BindingBlocked) as caught:
        generate_binding(**anthropic_arguments(account_mode=mode))
    assert str(caught.value) == "model_destination_not_admitted"
    assert "owner" not in str(caught.value)


@pytest.mark.parametrize(
    "overrides",
    [
        # anthropic id under the OpenAI account mode, and vice versa
        {"account_mode": ACCOUNT_MODE},
        {"model": OPENAI, "approved_models": (OPENAI,)},
        {"approved_models": (ANTHROPIC, OPENAI)},
        # unknown provider / mode / api-key mode
        {"model": "other/x", "approved_models": ("other/x",)},
        {"account_mode": "unknown-mode"},
        {"account_mode": "api-key"},
        # Fable without approval; unclassified id
        {"model": "anthropic/claude-fable-1", "approved_models": ("anthropic/claude-fable-1",)},
        {"model": "anthropic/mystery", "approved_models": ("anthropic/mystery",)},
    ],
)
def test_anthropic_binding_refusals_are_sanitized(overrides):
    with pytest.raises(BindingBlocked) as caught:
        generate_binding(**anthropic_arguments(**overrides))
    assert "sk-ant" not in str(caught.value) and "owner" not in str(caught.value)


def test_fable_with_job_approval_id_still_refused_on_opencode_path():
    model = "anthropic/claude-fable-1"
    with pytest.raises(BindingBlocked):
        generate_binding(**anthropic_arguments(model=model, approved_models=(model,)))


def test_verify_refuses_anthropic_manifest_and_mismatched_pairs():
    # generate_binding refuses Anthropic, so forge a digest-consistent manifest to prove
    # verify_binding independently refuses it.
    args = binding_arguments()
    good = generate_binding(**args)
    manifest = json.loads(good.manifest)
    manifest.update(
        account_mode=md.ANTHROPIC_ACCOUNT_MODE,
        model=ANTHROPIC,
        approved_models=[ANTHROPIC],
    )
    forged = HarnessBinding(good.config, json.dumps(manifest).encode(), good.skill)
    with pytest.raises(BindingBlocked):
        verify_binding(
            forged,
            **observations(
                forged, args, "anthropic", "claude-sonnet-synthetic", md.ANTHROPIC_ACCOUNT_MODE
            ),
        )
    for provider, account_mode in (
        ("openai", md.ANTHROPIC_ACCOUNT_MODE),
        ("anthropic", ACCOUNT_MODE),
        ("other", ACCOUNT_MODE),
        ("anthropic", "api-key"),
    ):
        with pytest.raises(BindingBlocked):
            verify_binding(
                good, **observations(good, args, provider, "synthetic-oauth-model", account_mode)
            )


# ---- observer ---------------------------------------------------------------


class _Endpoint:
    def __init__(self, origin, responses):
        self.origin, self.responses = origin, responses

    def call(self, method, path):
        assert method == "GET"
        return copy.deepcopy(self.responses[path])


def _observer(provider, model_id, manifest_model, account_mode):
    from tessera_mcp.harness_observer import _digest as digest

    query = "?location%5Bdirectory%5D=%2Fsrv%2Fresearch"
    paths = ("/api/info", "/api/location", "/api/mcp", "/api/skill", "/api/session/{sessionID}")
    schema = {"openapi": "3.1.0", "paths": {path: {"get": {}} for path in paths}}
    manifest = {
        "harness_schema_sha256": digest(schema),
        "harness_version": "2.fixture",
        "workspace": "/srv/research",
        "skill_id": "tessera-governed-research",
        "agent": "tessera-planner",
        "model": manifest_model,
        "account_mode": account_mode,
        "project_id": "project",
        "subject_credential_id": "credential",
        "destination": "openai:fixture",
        "control_origin": "https://control.example.test",
        "required_enrollment_scopes": ["context:read"],
    }
    binding = HarnessBinding(b"{}", json.dumps(manifest).encode(), b"fixture raw SKILL.md")
    location = {"directory": "/srv/research"}
    harness = _Endpoint(
        "https://research.example.test",
        {
            "/openapi.json": schema,
            "/api/info": {
                "version": "2.fixture",
                "pid": 1234,
                "urls": ["https://research.example.test"],
                "paths": {"tmp": "/private/fixture"},
            },
            "/api/location" + query: {
                **location,
                "project": {"id": "f", "directory": "/srv/research", "canonical": "/srv/research"},
            },
            "/api/mcp" + query: {
                "location": location,
                "data": [{"name": "tessera", "status": {"status": "connected"}}],
            },
            "/api/skill" + query: {
                "location": location,
                "data": [
                    {
                        "id": "tessera-governed-research",
                        "name": "Governed research",
                        "path": "/srv/research/skills/tessera-governed-research/SKILL.md",
                        "content": "Fixture parsed skill BODY",
                        "autoinvoke": True,
                    }
                ],
            },
            "/api/session/ses_fixture": {
                "data": {
                    "id": "ses_fixture",
                    "projectID": "fixture",
                    "agent": "tessera-planner",
                    "model": {"providerID": provider, "id": model_id},
                    "location": location,
                    "cost": 0,
                    "tokens": {},
                    "time": {"created": 1, "updated": 1},
                }
            },
        },
    )
    control = _Endpoint(
        "https://control.example.test",
        {
            "/external-agent/status": {
                "id": "actor",
                "credential_id": "credential",
                "project_id": "project",
                "destination": "openai:fixture",
                "actions": ["context:read"],
                "expires_at": "2030-01-01T01:00:00Z",
            }
        },
    )
    return V2PublicHarnessObserver(
        binding=binding,
        harness_endpoint=harness,
        control_endpoint=control,
        research_origin=harness.origin,
        coding_origins=("http://127.0.0.1:4096",),
        session_id="ses_fixture",
        clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )


def _model_changed(observer):
    try:
        observer.inspect_public()
    except ObservationBlocked as blocked:
        return "research_model_changed" in blocked.gaps
    return False


def test_observer_accepts_openai_and_refuses_anthropic_destinations():
    observer = _observer("openai", "fixture-model", "openai/fixture-model", ACCOUNT_MODE)
    assert not _model_changed(observer)
    # inspect_public never completes admission; it only reports measurements.
    assert observer.inspect_public().measured["provider_id"] == "openai"
    for mode in (md.ANTHROPIC_ACCOUNT_MODE,):
        assert _model_changed(
            _observer("anthropic", "fixture-model", "anthropic/fixture-model", mode)
        )


@pytest.mark.parametrize(
    ("provider", "manifest_model", "mode"),
    [
        ("anthropic", "anthropic/fixture-model", ACCOUNT_MODE),
        ("openai", "openai/fixture-model", md.ANTHROPIC_ACCOUNT_MODE),
        ("openai", "anthropic/fixture-model", md.ANTHROPIC_ACCOUNT_MODE),
        ("anthropic", "anthropic/fixture-model", "api-key"),
        ("anthropic", "anthropic/fixture-model", md.ANTHROPIC_ACCOUNT_MODE),
        ("other", "other/fixture-model", ACCOUNT_MODE),
    ],
)
def test_observer_refuses_provider_mode_mismatch(provider, manifest_model, mode):
    observer = _observer(provider, "fixture-model", manifest_model, mode)
    assert _model_changed(observer)


# ---- research service config and consumer ------------------------------------


def role(model, suffix):
    return {
        "admission_version": "v2",
        "admission_policy_ref": "policy-" + suffix,
        "admission_policy_digest": "a" * 64,
        "principal_id": "principal-" + suffix,
        "credential_id": "credential-" + suffix,
        "run_id": "run-" + suffix,
        "context_id": "context-" + suffix,
        "policy_id": "policy-" + suffix,
        "model": model,
        "agent_token_file": "/run/credentials/agent-" + suffix,
        "harness_password_file": "/run/credentials/harness-" + suffix,
        "binding_manifest_file": "/srv/pins/manifest-" + suffix,
        "binding_config_file": "/srv/pins/config-" + suffix,
        "skill_file": "/srv/pins/skill-" + suffix,
        "observer_admission_id": "admission-" + suffix,
        "harness_origin": "https://harness-" + suffix + ".example.test",
        "peer_origin": "https://peer-" + suffix + ".example.test",
        "registration_grant_id": "grant-" + suffix,
        "registration_revision": 1,
        "capture_binding_file": "/srv/pins/capture-" + suffix,
        "reasoning_profile_file": "/srv/pins/profile-" + suffix,
        "peer_token_file": "/run/credentials/peer-" + suffix,
    }


def references(model="openai/m", mode=ACCOUNT_MODE, **extra):
    return {
        "schema_version": "tessera-research-service-references-v1",
        "journey_id": "journey-1",
        "project_id": "project-1",
        "account_mode": mode,
        "control_origin": "https://control.example.test",
        "control_ca_file": "/srv/pins/control-ca",
        "planner": role(model, "p"),
        "critic": role(model, "c"),
        "coding_origins": ["https://coding.example.test"],
        **extra,
    }


def test_service_config_accepts_openai_and_refuses_anthropic():
    assert ResearchReferences.model_validate(references()).account_mode == ACCOUNT_MODE
    with pytest.raises(ValueError) as caught:
        ResearchReferences.model_validate(
            references("anthropic/claude-opus-synthetic", md.ANTHROPIC_ACCOUNT_MODE)
        )
    assert "Messages API" in str(caught.value)
    assert REF not in str(caught.value)


@pytest.mark.parametrize(
    "bad",
    [
        references("anthropic/claude-opus-x", ACCOUNT_MODE),
        references("openai/m", md.ANTHROPIC_ACCOUNT_MODE),
        references("anthropic/claude-opus-x", md.ANTHROPIC_ACCOUNT_MODE),
        references("openai/m", ACCOUNT_MODE, credential_ref=REF),
        references("anthropic/claude-opus-x", "api-key"),
        references("anthropic/claude-opus-x", "unknown"),
        references("other/m", ACCOUNT_MODE),
        references("anthropic/claude-fable-1", md.ANTHROPIC_ACCOUNT_MODE),
        references("anthropic/mystery", md.ANTHROPIC_ACCOUNT_MODE),
    ],
)
def test_service_config_refusals(bad):
    # Pydantic's own str() includes input; the CLI prints only "startup blocked".
    # The validator's message itself never carries the supplied value.
    with pytest.raises(ValueError) as caught:
        ResearchReferences.model_validate(bad)
    assert all("sk-x" not in error["msg"] for error in caught.value.errors())


def test_service_config_fable_with_approval_id_refused():
    with pytest.raises(ValueError):
        ResearchReferences.model_validate(
            references(
                "anthropic/claude-fable-1",
                md.ANTHROPIC_ACCOUNT_MODE,
            )
        )


def _fence(manifest, model_id):
    consumer = ResearchConsumer.__new__(ResearchConsumer)
    consumer.harness_binding = SimpleNamespace(manifest=json.dumps(manifest).encode())
    consumer.binding = {"model_id": model_id}
    consumer.admission_version = "v1"
    try:
        consumer.fence()
    except ConsumerBlocked as blocked:
        return str(blocked)
    except Exception:
        return "past_model_check"
    return "past_model_check"


def test_consumer_model_prefix_follows_account_mode():
    assert _fence({"model": "openai/m", "account_mode": ACCOUNT_MODE}, "m") == "past_model_check"
    for mode in (md.ANTHROPIC_ACCOUNT_MODE,):
        assert "consumer_model_changed" in _fence(
            {"model": "anthropic/m", "account_mode": mode}, "m"
        )
    for manifest in (
        {"model": "anthropic/m", "account_mode": ACCOUNT_MODE},
        {"model": "openai/m", "account_mode": md.ANTHROPIC_ACCOUNT_MODE},
        {"model": "anthropic/m", "account_mode": "api-key"},
    ):
        assert "consumer_model_changed" in _fence(manifest, "m")


# ---- validate_profile and egress ---------------------------------------------


def profile(provider, mode, model_id, **extra):
    return {
        "run_id": "run",
        "agent": "planner",
        "grant_id": "grant",
        "subject_credential_id": "agent",
        "account_mode": mode,
        "model": {"providerID": provider, "id": model_id},
        "max_steps": 1,
        "max_tokens": 8192,
        "max_duration_seconds": 120,
        "permissions": [
            {"action": "*", "resource": "*", "effect": "deny"},
            {"action": "skill", "resource": "curated", "effect": "allow"},
        ],
        **extra,
    }


def test_validate_profile_admits_openai_and_refuses_others():
    validate_profile(profile("openai", ACCOUNT_MODE, "m"), skill_id="curated")
    for bad in (
        profile("anthropic", ACCOUNT_MODE, "claude-sonnet-x"),
        profile("openai", md.ANTHROPIC_ACCOUNT_MODE, "m", credential_ref=REF),
        profile("anthropic", "api-key", "claude-sonnet-x", credential_ref=REF),
        profile("other", ACCOUNT_MODE, "m"),
    ):
        with pytest.raises(ValueError):
            validate_profile(bad, skill_id="curated")


def test_validate_profile_refuses_anthropic_profile_without_limits():
    bad = profile("anthropic", md.ANTHROPIC_ACCOUNT_MODE, "claude-sonnet-x", credential_ref=REF)
    assert "limits" not in bad
    with pytest.raises(ValueError):
        validate_profile(bad, skill_id="curated")


def test_model_runtime_egress_is_exactly_the_anthropic_api_host():
    assert md.model_runtime_egress_allowed("anthropic", "api.anthropic.com", 443)
    assert md.model_runtime_egress_allowed("anthropic", "API.Anthropic.com.", 443)
    for provider, host, port in (
        ("anthropic", "api.anthropic.com", 80),
        ("anthropic", "api.anthropic.com", 8443),
        ("anthropic", "console.anthropic.com", 443),
        ("anthropic", "evil.example.test", 443),
        ("openai", "api.anthropic.com", 443),
        ("openai", "api.openai.com", 443),
        ("other", "api.anthropic.com", 443),
    ):
        assert not md.model_runtime_egress_allowed(provider, host, port)
    assert md.model_runtime_allowlist() == {("api.anthropic.com", 443)}


def test_general_sandbox_and_campaign_egress_never_include_anthropic():
    root = Path(__file__).parents[1] / "src"
    for relative in (
        "tessera_control/agent_sandbox_configuration.py",
        "tessera_worker/egress_proxy.py",
    ):
        source = (root / relative).read_text()
        assert "anthropic" not in source.lower()
        assert "model_destinations" not in source
    # Nothing outside the model-runtime module may reference the model allowlist.
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if re.search(r"model_runtime_(allowlist|egress_allowed)", path.read_text())
        and path.name != "model_destinations.py"
    ]
    assert not any(
        name.startswith(("tessera_control/", "tessera_worker/")) for name in offenders
    ), offenders


# ---- registry: runtime routing ----------------------------------------------


def test_anthropic_destination_resolves_to_messages_api_and_is_refused_on_opencode():
    assert md.DEFAULT_PROVIDER == "anthropic"
    found = md.destination("anthropic", md.ANTHROPIC_ACCOUNT_MODE)
    assert md.ANTHROPIC_ACCOUNT_MODE == "anthropic-api-key-ref"
    assert found.runtime == md.RUNTIME_ANTHROPIC_MESSAGES == "anthropic-messages-api"
    assert found.billed is True
    with pytest.raises(md.DestinationRefused) as caught:
        md.require_runtime(found, md.RUNTIME_OPENCODE)
    assert str(caught.value) == "destination_requires_other_runtime"
    assert md.require_runtime(found, md.RUNTIME_ANTHROPIC_MESSAGES) is found
    assert md.model_runtime_allowlist() == {("api.anthropic.com", 443)}


def test_claude_subscription_ref_is_refused_as_unenrolled():
    with pytest.raises(md.DestinationRefused):
        md.destination_for_account_mode("claude-subscription-ref")
    with pytest.raises(md.DestinationRefused):
        md.destination("anthropic", "claude-subscription-ref")
    for gone in (
        "RUNTIME_CLAUDE_CODE",
        "ANTHROPIC_API_KEY_ACCOUNT_MODE",
        "ANTHROPIC_API_KEY_REF",
        "ANTHROPIC_API_KEY_DESTINATION",
    ):
        assert not hasattr(md, gone)


def test_openai_destination_is_opencode_v2():
    found = md.destination("openai", ACCOUNT_MODE)
    assert found.runtime == md.RUNTIME_OPENCODE == "opencode-v2"
    assert md.require_runtime(found, md.RUNTIME_OPENCODE) is found
    with pytest.raises(md.DestinationRefused):
        md.require_runtime(found, md.RUNTIME_ANTHROPIC_MESSAGES)
