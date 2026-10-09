"""Production factory normal admission sequence with synthetic endpoints, no auth reads."""

import json
from types import SimpleNamespace

import pytest
from test_research_consumer import arguments, consumer_setup, output_setup  # noqa: F401

from tessera_agent import research_factory
from tessera_agent.research_consumer_store import ConsumerBlocked


@pytest.mark.parametrize("consumer_setup", ["planner"], indirect=True)
def test_factory_reaches_actual_observer_boundary_before_stdio_or_session_effects(
    consumer_setup,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    constructor, *_ = consumer_setup
    binding, capture = constructor["harness_binding"], dict(constructor["capture_binding"])
    capture.update(
        input_context_id=capture["context_id"],
        input_body_digest=capture["context_digest"],
        capture_policy_context_id="human-policy",
    )
    manifest = json.loads(binding.manifest)
    configured = json.loads(binding.config)["agents"][manifest["agent"]]
    ref = SimpleNamespace(
        run_id=capture["run_id"],
        context_id=capture["context_id"],
        policy_id="human-policy",
        credential_id=capture["credential_id"],
        principal_id=capture["actor_principal_id"],
        model="openai/" + capture["model_id"],
        harness_origin="http://127.0.0.1:8771",
        binding_config_file="/synthetic/config",
        binding_manifest_file="/synthetic/manifest",
        skill_file="/synthetic/skill",
        reasoning_profile_file="/synthetic/profile",
        capture_binding_file="/synthetic/capture",
        agent_token_file="/synthetic/agent-reference",
        harness_password_file="/synthetic/password-reference",
        harness_ca_file=None,
        observer_admission_id="human-observer-admission",
    )
    config = SimpleNamespace(
        planner=ref,
        project_id=capture["project_id"],
        control_origin="http://127.0.0.1:8765",
        control_ca_file=None,
        account_mode="included-chatgpt-oauth",
        coding_origins=["http://127.0.0.1:4096"],
    )
    profile = dict(
        run_id=ref.run_id,
        project_id=config.project_id,
        agent=manifest["agent"],
        model={"providerID": "openai", "id": capture["model_id"]},
        account_mode=config.account_mode,
        grant_id="issued-reasoning-grant",
        revision=1,
        epoch=1,
        subject_credential_id=ref.credential_id,
        destination=manifest["destination"],
        control_url=config.control_origin,
        harness_url=ref.harness_origin,
        workspace="/synthetic/workspace",
        permissions=configured["permissions"],
        schema_sha256=manifest["harness_schema_sha256"],
        version=manifest["harness_version"],
        prompt_sha256=capture["context_digest"],
        max_steps=configured["steps"],
        max_tokens=8192,
        max_duration_seconds=120,
    )
    files = {
        "/synthetic/config": binding.config,
        "/synthetic/manifest": binding.manifest,
        "/synthetic/skill": binding.skill,
        "/synthetic/profile": json.dumps(profile).encode(),
        "/synthetic/capture": json.dumps(capture).encode(),
    }
    monkeypatch.setattr(research_factory, "private_file", lambda path: files[str(path)])
    opened, observed = [], []

    class Endpoint:
        def __init__(self, origin, *a, **k):
            self.origin = origin
            opened.append(self)
            self.closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(research_factory, "ResearchEndpoint", Endpoint)

    def observe(*args):
        observed.append(args)

        def missing():
            raise ConsumerBlocked("actual_installed_plugin_proof_missing")

        return missing

    ports = research_factory.InstalledResearchPorts(observe, None, None, None, None, None, None)
    with pytest.raises(ConsumerBlocked, match="actual_installed_plugin_proof_missing"):
        research_factory.ProductionResearchFactory(ports).construct(config, tmp_path / "private")
    assert len(observed) == 1 and len(opened) == 2 and all(e.closed for e in opened)
    assert not (tmp_path / "private").exists()  # No run/session journal or inference effects.


def test_only_exact_pinned_skill_permission_is_accepted_by_research_profile():
    from tessera_agent.remote_reasoning_runtime import validate_profile

    profile = {
        "run_id": "run",
        "agent": "planner",
        "grant_id": "grant",
        "subject_credential_id": "agent",
        "account_mode": "included-chatgpt-oauth",
        "model": {"providerID": "openai"},
        "max_steps": 1,
        "max_tokens": 8192,
        "max_duration_seconds": 120,
        "permissions": [
            {"action": "*", "resource": "*", "effect": "deny"},
            {"action": "skill", "resource": "curated", "effect": "allow"},
        ],
    }
    validate_profile(profile, skill_id="curated")
    with pytest.raises(ValueError):
        validate_profile(profile)  # Legacy standalone mode stays unchanged.
    with pytest.raises(ValueError):
        validate_profile(profile, skill_id="foreign")
