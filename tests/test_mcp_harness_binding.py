"""Isolated source-contract fixtures, not live OpenCode catalog/account discovery."""

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tessera_mcp.harness_binding import (
    ACCOUNT_MODE,
    PERMITTED_TOOLS,
    SKILL_ID,
    BindingBlocked,
    catalog_sha256,
    generate_binding,
    generate_from_mapper,
    sha256,
    verify_binding,
)


@pytest.fixture
def arguments():
    skill = (Path(__file__).parents[1] / "skills" / SKILL_ID / "SKILL.md").read_bytes()
    catalog = [
        {"name": name, "inputSchema": {"type": "object"}, "description": "Synthetic reviewed tool"}
        for name in PERMITTED_TOOLS
    ]
    return {
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
        "model": "openai/synthetic-oauth-model",
        "approved_models": ("openai/synthetic-oauth-model",),
        "harness_version": "2.synthetic",
        "harness_schema_sha256": "1" * 64,
        "installed_build_sha256": "2" * 64,
        "role": "planner",
        "permitted_tools": PERMITTED_TOOLS,
        "ca_file": "/run/credentials/tessera-reasoning.service/control-ca",
    }


def observations(binding, args):
    manifest = json.loads(binding.manifest)
    return {
        "effective_config": json.loads(binding.config),
        "selected_skill": args["skill"],
        "observed_catalog": args["catalog"],
        "provider_id": "openai",
        "model_id": "synthetic-oauth-model",
        "account_mode": ACCOUNT_MODE,
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
    }


def test_v2_config_is_secret_free_deterministic_and_deny_default(arguments, monkeypatch):
    # Generation performs no filesystem/auth reads (even for its public references).
    monkeypatch.setattr(Path, "read_bytes", lambda *_: pytest.fail("unexpected file read"))
    monkeypatch.setattr(Path, "read_text", lambda *_: pytest.fail("unexpected file read"))
    binding = generate_binding(**arguments)
    assert generate_binding(**arguments) == binding
    config = json.loads(binding.config)
    manifest = json.loads(binding.manifest)
    assert config["$schema"] == "https://opencode.ai/config.json"
    assert set(config["mcp"]) == {"servers"}
    server = config["mcp"]["servers"]["tessera"]
    assert server["type"] == "local" and server["protocol"] == "legacy"
    assert server["codemode"] is False and server["disabled"] is False
    assert server["command"] == ["/opt/tessera/bin/python", "-m", "tessera_mcp.server"]
    assert server["environment"]["TESSERA_AGENT_TOKEN_FILE"].endswith("/agent-token")
    assert "TESSERA_REASONING_HARNESS_TOKEN" not in server["environment"]
    assert config["skills"] == [arguments["skill_source"]]
    assert config["permissions"][0] == {"action": "*", "resource": "*", "effect": "deny"}
    assert {rule["action"] for rule in config["permissions"][2:]} == {
        "tessera_" + name for name in PERMITTED_TOOLS
    }
    assert not any(
        rule["action"] in {"shell", "read", "edit", "execute", "subagent"}
        for rule in config["permissions"][1:]
    )
    assert config["agents"]["tessera-planner"]["steps"] == 8
    assert manifest["verification"] == "generated_source_not_live_admission"
    assert manifest["human_decisions"] == "separately_authenticated_control_operator"
    assert manifest["config_sha256"] == sha256(binding.config)
    assert manifest["skill_sha256"] == sha256(binding.skill)
    assert (
        verify_binding(binding, **observations(binding, arguments))["binding_sha256"]
        == binding.digest
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "openai/other"},
        {"approved_models": ("anthropic/model",)},
        {"approved_models": ("openai/synthetic-oauth-model#variant",)},
        {"installed_python": "python"},
        {"credential_directory": "/run/../secrets"},
        {"skill_source": "https://untrusted.test/skills"},
        {"ca_file": "{env:PRIVATE_KEY}"},
        {"control_url": "http://remote.example.test"},
        {"control_url": "https://user:key@example.test"},
        {"destination": "unsafe\r\nheader"},
        {"destination": "{env:OPENAI_API_KEY}"},
        {"control_url": "https://{env:CONTROL_PASSWORD}.example.test"},
        {"role": "owner"},
        {"max_steps": True},
        {"permitted_tools": ("tessera_approve",)},
        {"permitted_tools": ("tessera_*",)},
        {"permitted_tools": ("tessera_status", "tessera_status")},
        {"expected_skill_sha256": "0" * 64},
        {"expected_catalog_sha256": "0" * 64},
    ],
)
def test_generation_denials(arguments, changes):
    with pytest.raises(BindingBlocked):
        generate_binding(**{**arguments, **changes})


@pytest.mark.parametrize("change", ["extra", "missing", "duplicate", "schema"])
def test_catalog_pin_drift(arguments, change):
    catalog = copy.deepcopy(arguments["catalog"])
    if change == "extra":
        catalog.append({"name": "owner_proxy", "inputSchema": {}})
    elif change == "missing":
        catalog.pop()
    elif change == "duplicate":
        catalog[-1] = catalog[0]
    else:
        catalog[0]["inputSchema"] = {"changed": True}
    with pytest.raises(BindingBlocked):
        generate_binding(**{**arguments, "catalog": catalog})


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_id": "paid-fallback"},
        {"model_id": "other"},
        {"account_mode": "api-key"},
        {"included_oauth_verified": False},
        {"included_oauth_verified": None},
        {"observed_harness_version": "changed"},
        {"observed_harness_schema_sha256": "0" * 64},
        {"observed_build_sha256": "0" * 64},
        {"observed_mcp_sdk_version": "unknown"},
        {"expected_binding_sha256": "0" * 64},
        {"selected_skill": b"changed"},
        {"now": datetime(2030, 1, 1, 1, tzinfo=UTC)},
    ],
)
def test_public_admission_fences(arguments, changes):
    binding = generate_binding(**arguments)
    with pytest.raises(BindingBlocked):
        verify_binding(binding, **{**observations(binding, arguments), **changes})


@pytest.mark.parametrize(
    "field,value",
    [
        ("credential_id", "different"),
        ("project_id", "different"),
        ("destination", "different"),
        ("actions", []),
        ("expires_at", "not-time"),
    ],
)
def test_principal_reference_fences(arguments, field, value):
    binding = generate_binding(**arguments)
    observed = observations(binding, arguments)
    observed["principal_status"][field] = value
    with pytest.raises(BindingBlocked):
        verify_binding(binding, **observed)


def test_effective_merge_and_generated_config_tamper_block(arguments):
    binding = generate_binding(**arguments)
    observed = observations(binding, arguments)
    observed["effective_config"]["permissions"].append(
        {"action": "shell", "resource": "*", "effect": "allow"}
    )
    with pytest.raises(BindingBlocked, match="effective_config_pin_changed"):
        verify_binding(binding, **observed)
    with pytest.raises(BindingBlocked, match="generated_config_pin_changed"):
        verify_binding(replace(binding, config=b"{}"), **observations(binding, arguments))
    with pytest.raises(BindingBlocked, match="generated_skill_pin_changed"):
        verify_binding(replace(binding, skill=b"changed"), **observations(binding, arguments))


def test_narrow_critic_is_not_ambient_authority(arguments):
    binding = generate_binding(
        **{
            **arguments,
            "role": "critic",
            "permitted_tools": ("tessera_status", "tessera_read_context", "tessera_request_review"),
        }
    )
    manifest = json.loads(binding.manifest)
    assert manifest["required_enrollment_scopes"] == ["context:read", "review:request"]
    assert "tessera_tessera_consume_grant" not in manifest["permission_actions"]
    assert b"separately authenticated" in binding.skill


def test_fixed_mapper_resolves_references_without_reading_credentials(arguments, tmp_path):
    manager_root = tmp_path / "credentials"
    manager_root.mkdir(mode=0o700)
    args = {key: value for key, value in arguments.items() if key != "credential_directory"}
    binding = generate_from_mapper(
        environment={
            "CREDENTIALS_DIRECTORY": str(manager_root),
            "OPENAI_API_KEY": "synthetic-never-forwarded",
        },
        **args,
    )
    server = json.loads(binding.config)["mcp"]["servers"]["tessera"]
    assert server["environment"]["TESSERA_AGENT_TOKEN_FILE"] == str(manager_root / "agent-token")
    assert "OPENAI_API_KEY" not in server["environment"]
    assert not (manager_root / "agent-token").exists()
    with pytest.raises(BindingBlocked, match="directory_override_forbidden"):
        generate_from_mapper(environment={"CREDENTIALS_DIRECTORY": str(manager_root)}, **arguments)
    with pytest.raises(BindingBlocked, match="reference_unavailable"):
        generate_from_mapper(environment={}, **args)


def test_generated_output_catalog_scopes_and_immutable_digest(arguments):
    binding = generate_binding(
        **{
            **arguments,
            "permitted_tools": (
                "tessera_register_generated_output",
                "tessera_generated_output_metadata",
                "tessera_read_generated_output",
            ),
        }
    )
    manifest = json.loads(binding.manifest)
    assert manifest["required_enrollment_scopes"] == ["context:read", "provenance:append"]
    assert manifest["permission_actions"] == [
        "tessera_tessera_generated_output_metadata",
        "tessera_tessera_read_generated_output",
        "tessera_tessera_register_generated_output",
    ]
    assert (
        generate_binding(
            **{
                **arguments,
                "permitted_tools": (
                    "tessera_register_generated_output",
                    "tessera_generated_output_metadata",
                    "tessera_read_generated_output",
                ),
            }
        ).digest
        == binding.digest
    )
    assert "tessera_issue_generated_output_grant" not in PERMITTED_TOOLS
    assert b"Human disclosure is required" in binding.skill
