"""Secret-free pinned OpenCode V2 research-harness binding artifacts.

This source contract generates config and admission pins, never reads credentials,
changes global configuration, starts a harness, or proves an OAuth account usable.
Consumer integration must compare effective config/catalog/skill pins before inference.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

from tessera_mcp.config import Config

SERVER_NAME = "tessera"
SKILL_ID = "tessera-governed-research"
ACCOUNT_MODE = "included-chatgpt-oauth"
PROTOCOL = "2025-11-25"

PERMITTED_TOOLS = (
    "tessera_status",
    "tessera_list_contexts",
    "tessera_read_context",
    "tessera_list_proposals",
    "tessera_read_proposal",
    "tessera_propose_task_plan",
    "tessera_request_authority",
    "tessera_list_authority_requests",
    "tessera_authority_status",
    "tessera_consume_grant",
    "tessera_propose_research",
    "tessera_record_observation",
    "tessera_request_review",
    "tessera_notebook",
    "tessera_export_notebook",
    "tessera_register_generated_output",
    "tessera_generated_output_metadata",
    "tessera_read_generated_output",
)

_SCOPES = {
    "tessera_list_contexts": "context:read",
    "tessera_read_context": "context:read",
    "tessera_list_proposals": "proposal:read",
    "tessera_read_proposal": "proposal:read",
    "tessera_propose_task_plan": "proposal:create",
    "tessera_propose_research": "proposal:create",
    "tessera_request_authority": "authority:request",
    "tessera_list_authority_requests": "authority:request",
    "tessera_authority_status": "authority:consume",
    "tessera_consume_grant": "authority:consume",
    "tessera_record_observation": "observation:create",
    "tessera_request_review": "review:request",
    "tessera_notebook": "notebook:read",
    "tessera_export_notebook": "notebook:read",
    "tessera_register_generated_output": ("context:read", "provenance:append"),
    "tessera_generated_output_metadata": "context:read",
    "tessera_read_generated_output": "context:read",
}

_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_MODEL = re.compile(r"^openai/[A-Za-z0-9][A-Za-z0-9._-]{0,100}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class BindingBlocked(ValueError):
    """Sanitized admission error, never supplied config/catalog contents."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _path(value):
    if (
        not isinstance(value, str)
        or value == "/"
        or not value.startswith("/")
        or any(c in value for c in "\x00\r\n{}")
        or ".." in value.split("/")
    ):
        raise BindingBlocked("invalid_absolute_runtime_reference")
    return str(PurePosixPath(value))


def catalog_sha256(catalog: list[dict]) -> str:
    """Pin the reviewed SDK tools/list schema, not just tool names or annotations.

    Descriptions are untrusted data; hashing never interprets or logs them. SDK
    JSON forms are sorted by name; duplicate/extra/missing tools block admission.
    """
    if not isinstance(catalog, list) or len(catalog) != len(PERMITTED_TOOLS):
        raise BindingBlocked("catalog_inventory_changed")
    if any(
        not isinstance(tool, dict)
        or not isinstance(tool.get("name"), str)
        or not isinstance(tool.get("inputSchema"), dict)
        for tool in catalog
    ):
        raise BindingBlocked("invalid_catalog_schema")
    names = [tool["name"] for tool in catalog]
    if len(set(names)) != len(names) or set(names) != set(PERMITTED_TOOLS):
        raise BindingBlocked("catalog_inventory_changed")
    try:
        data = _json(sorted(catalog, key=lambda tool: tool["name"]))
        if len(data) > 512 * 1024:
            raise BindingBlocked("catalog_serialization_bound")
        return sha256(data)
    except (TypeError, ValueError):
        raise BindingBlocked("invalid_catalog_schema") from None


@dataclass(frozen=True)
class HarnessBinding:
    """Immutable secret-free bytes; caller stages them without overwriting global config."""

    config: bytes
    manifest: bytes
    skill: bytes

    @property
    def digest(self):
        return sha256(self.manifest)


def generate_binding(
    *,
    installed_python: str,
    workspace: str,
    credential_directory: str,
    skill_source: str,
    skill: bytes,
    expected_skill_sha256: str,
    catalog: list[dict],
    expected_catalog_sha256: str,
    project_id: str,
    subject_credential_id: str,
    destination: str,
    control_url: str,
    model: str,
    approved_models: tuple[str, ...],
    harness_version: str,
    harness_schema_sha256: str,
    installed_build_sha256: str,
    role: str,
    permitted_tools: tuple[str, ...],
    max_steps: int = 8,
    ca_file: str | None = None,
) -> HarnessBinding:
    """Generate a V2 project config, skill bytes and admission manifest without I/O.

    credential_directory is the exact root returned by the EXISTING fixed mapper's
    credential_root, not a caller-discovered credential location. Only agent-token
    is referenced; no human/harness/provider credential value enters the artifacts.
    The operator supplies approved model/catalog/skill pins, not the model itself.
    """
    python = _path(installed_python)
    workspace, credentials, skills = map(_path, (workspace, credential_directory, skill_source))
    if not _ID.fullmatch(project_id) or not _ID.fullmatch(subject_credential_id):
        raise BindingBlocked("invalid_principal_binding")
    if role not in {"planner", "critic"}:
        raise BindingBlocked("unsupported_research_role")
    if (
        not re.fullmatch(r"[A-Za-z0-9._+-]{1,100}", harness_version)
        or not _DIGEST.fullmatch(harness_schema_sha256)
        or not _DIGEST.fullmatch(installed_build_sha256)
    ):
        raise BindingBlocked("invalid_installed_harness_pin")
    if (
        not approved_models
        or len(approved_models) > 16
        or len(set(approved_models)) != len(approved_models)
        or any(not _MODEL.fullmatch(item) for item in approved_models)
        or model not in approved_models
    ):
        raise BindingBlocked("model_outside_included_oauth_allowlist")
    if (
        not permitted_tools
        or len(set(permitted_tools)) != len(permitted_tools)
        or not set(permitted_tools) <= set(PERMITTED_TOOLS)
    ):
        raise BindingBlocked("tool_outside_permitted_catalog")
    if type(max_steps) is not int or not 1 <= max_steps <= 16:
        raise BindingBlocked("invalid_step_bound")
    if (
        not isinstance(skill, bytes)
        or not 1 <= len(skill) <= 32768
        or sha256(skill) != expected_skill_sha256
        or catalog_sha256(catalog) != expected_catalog_sha256
    ):
        raise BindingBlocked("curated_content_pin_changed")
    token_file = credentials + "/agent-token"
    if any(any(ord(c) < 32 or c in "{}" for c in value) for value in (control_url, destination)):
        raise BindingBlocked("runtime_reference_substitution_forbidden")
    try:
        # Config construction validates origin/destination only; token() is NEVER called here.
        Config(control_url, PurePosixPath(token_file), project_id, destination)
    except ValueError:
        raise BindingBlocked("invalid_control_origin_or_destination") from None
    environment = {
        "TESSERA_CONTROL_URL": control_url,
        "TESSERA_AGENT_TOKEN_FILE": token_file,
        "TESSERA_PROJECT_ID": project_id,
        "TESSERA_AGENT_DESTINATION": destination,
    }
    if ca_file is not None:
        environment["TESSERA_CONTROL_CA_FILE"] = _path(ca_file)
    actions = [SERVER_NAME + "_" + name for name in sorted(permitted_tools)]
    permissions = [
        {"action": "*", "resource": "*", "effect": "deny"},
        {"action": "skill", "resource": SKILL_ID, "effect": "allow"},
    ]
    permissions.extend({"action": action, "resource": "*", "effect": "allow"} for action in actions)
    policies = [
        {"action": "provider.use", "resource": "*", "effect": "deny"},
        {"action": "provider.use", "resource": "openai", "effect": "allow"},
        {"action": "permission", "resource": "*", "effect": "deny"},
        {"action": "permission", "resource": "skill:" + SKILL_ID, "effect": "allow"},
    ]
    policies.extend(
        {"action": "permission", "resource": action + ":*", "effect": "allow"} for action in actions
    )
    agent = "tessera-" + role
    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": model,
        "default_agent": agent,
        "permissions": permissions,
        "skills": [skills],
        "experimental": {"policies": policies},
        "mcp": {
            "servers": {
                SERVER_NAME: {
                    "type": "local",
                    "command": [python, "-m", "tessera_mcp.server"],
                    "cwd": workspace,
                    "environment": environment,
                    "protocol": "legacy",
                    "codemode": False,
                    "disabled": False,
                    "timeout": {"startup": 30000, "catalog": 30000, "execution": 30000},
                }
            }
        },
        "agents": {
            agent: {
                "mode": "primary",
                "model": model,
                "steps": max_steps,
                "description": "Governed Tessera " + role,
                "system": "Load @" + SKILL_ID + ". Use only curated scoped MCP context. "
                "Human review is separately authenticated; never self-approve.",
                "permissions": permissions,
            }
        },
    }
    encoded = _json(config)
    manifest = {
        "version": "tessera-opencode-v2-binding-v1",
        "account_mode": ACCOUNT_MODE,
        "harness_version": harness_version,
        "harness_schema_sha256": harness_schema_sha256,
        "installed_build_sha256": installed_build_sha256,
        "mcp_sdk_version": "1.26.0",
        "role": role,
        "agent": agent,
        "model": model,
        "approved_models": sorted(approved_models),
        "project_id": project_id,
        "subject_credential_id": subject_credential_id,
        "destination": destination,
        "workspace": workspace,
        "credential_mapper": "tessera_agent.service_startup",
        "credential_reference": token_file,
        "control_origin": control_url,
        "ca_reference": environment.get("TESSERA_CONTROL_CA_FILE"),
        "config_sha256": sha256(encoded),
        "skill_id": SKILL_ID,
        "skill_sha256": expected_skill_sha256,
        "catalog_sha256": expected_catalog_sha256,
        "permitted_tools": sorted(permitted_tools),
        "permission_actions": actions,
        "required_enrollment_scopes": sorted(
            {
                scope
                for name in permitted_tools
                if name in _SCOPES
                for scope in (
                    _SCOPES[name] if isinstance(_SCOPES[name], tuple) else (_SCOPES[name],)
                )
            }
        ),
        "transport": "stdio",
        "protocol": PROTOCOL,
        "human_decisions": "separately_authenticated_control_operator",
        "required_admission": [
            "effective_config_pins",
            "selected_skill_bytes",
            "real_mcp_catalog",
            "principal_destination_scopes",
            "verified_included_oauth_account_no_api_key_fallback",
            "selected_model_allowlist",
        ],
        "verification": "generated_source_not_live_admission",
    }
    return HarnessBinding(encoded, _json(manifest), skill)


def generate_from_mapper(*, environment: dict[str, str], **arguments) -> HarnessBinding:
    """Resolve only manager directory metadata via the EXISTING fixed mapper.

    No token_reference/private_file call, credential file read or environment
    forwarding. Runtime references stay references in the local MCP config.
    """
    from tessera_agent.service_startup import credential_root

    if "credential_directory" in arguments:
        raise BindingBlocked("manager_credential_directory_override_forbidden")
    try:
        root = credential_root(environment)
    except Exception:
        raise BindingBlocked("fixed_credential_mapper_reference_unavailable") from None
    return generate_binding(credential_directory=str(root), **arguments)


def verify_binding(
    binding: HarnessBinding,
    *,
    effective_config: dict,
    selected_skill: bytes,
    observed_catalog: list[dict],
    provider_id: str,
    model_id: str,
    account_mode: str,
    included_oauth_verified: bool,
    observed_harness_version: str,
    observed_harness_schema_sha256: str,
    observed_build_sha256: str,
    observed_mcp_sdk_version: str,
    expected_binding_sha256: str,
    principal_status: dict,
    now: datetime,
):
    """Admission comparison on supplied PUBLIC observations; performs no discovery.

    Calling this on fixtures is synthetic proof. Runtime must obtain observations
    from the dedicated authorized harness, not echo the generated manifest.
    Unknown auth telemetry is blocked, never interpreted as spent/exhausted quota.
    """
    if binding.digest != expected_binding_sha256:
        raise BindingBlocked("binding_manifest_pin_changed")
    manifest = json.loads(binding.manifest)
    if sha256(binding.config) != manifest["config_sha256"]:
        raise BindingBlocked("generated_config_pin_changed")
    if sha256(binding.skill) != manifest["skill_sha256"]:
        raise BindingBlocked("generated_skill_pin_changed")
    if (
        observed_harness_version != manifest["harness_version"]
        or observed_harness_schema_sha256 != manifest["harness_schema_sha256"]
        or observed_build_sha256 != manifest["installed_build_sha256"]
        or observed_mcp_sdk_version != manifest["mcp_sdk_version"]
    ):
        raise BindingBlocked("installed_harness_pin_changed")
    if sha256(_json(effective_config)) != manifest["config_sha256"]:
        raise BindingBlocked("effective_config_pin_changed")
    if sha256(selected_skill) != manifest["skill_sha256"]:
        raise BindingBlocked("selected_skill_pin_changed")
    if catalog_sha256(observed_catalog) != manifest["catalog_sha256"]:
        raise BindingBlocked("observed_catalog_pin_changed")
    if (
        account_mode != ACCOUNT_MODE
        or included_oauth_verified is not True
        or provider_id != "openai"
        or "openai/" + model_id != manifest["model"]
        or "openai/" + model_id not in manifest["approved_models"]
    ):
        raise BindingBlocked("included_oauth_model_admission_missing")
    if not isinstance(principal_status, dict) or not isinstance(
        principal_status.get("actions"), list
    ):
        raise BindingBlocked("principal_destination_scope_changed")
    if (
        principal_status.get("credential_id") != manifest["subject_credential_id"]
        or principal_status.get("project_id") != manifest["project_id"]
        or principal_status.get("destination") != manifest["destination"]
        or not set(manifest["required_enrollment_scopes"])
        <= set(principal_status.get("actions", []))
    ):
        raise BindingBlocked("principal_destination_scope_changed")
    try:
        expires = datetime.fromisoformat(principal_status["expires_at"].replace("Z", "+00:00"))
        if expires.tzinfo is None or now.tzinfo is None or expires <= now:
            raise ValueError()
    except (TypeError, ValueError, KeyError):
        raise BindingBlocked("principal_expiry_missing_or_elapsed") from None
    return {
        "binding_sha256": binding.digest,
        "config_sha256": manifest["config_sha256"],
        "skill_sha256": manifest["skill_sha256"],
        "catalog_sha256": manifest["catalog_sha256"],
        "observation_provenance": "caller_supplied_public_admission_observations",
    }


def verify_binding_v2(binding, *, verifier, evidence, expected):
    """Explicit v2 selection. Never synthesizes the legacy included_oauth flag."""
    from .runtime_admission import CurrentAuthority, RuntimeAdmissionVerifier, public_digest

    if type(verifier) is not RuntimeAdmissionVerifier:
        raise BindingBlocked("approved_v2_verifier_required")
    manifest = json.loads(binding.manifest)
    pins = verifier.pins
    if (
        binding.digest != pins.binding
        or sha256(binding.config) != manifest["config_sha256"]
        or sha256(binding.skill) != manifest["skill_sha256"]
        or manifest["config_sha256"] != pins.effective_config
        or manifest["skill_sha256"] != pins.loaded_skill
        or manifest["catalog_sha256"] != pins.typed_catalog
        or public_digest(
            {
                "permitted_tools": manifest["permitted_tools"],
                "permission_actions": manifest["permission_actions"],
            }
        )
        != pins.tool_mapping
        or manifest["installed_build_sha256"] != pins.build
        or manifest["harness_schema_sha256"] != pins.harness_schema
        or verifier.scope.role != manifest["role"]
        or "openai/" + verifier.model != manifest["model"]
        or manifest["model"] not in manifest["approved_models"]
        or verifier.scope.credential != manifest["subject_credential_id"]
        or verifier.scope.project != manifest["project_id"]
        or verifier.scope.destination != manifest["destination"]
    ):
        raise BindingBlocked("v2_binding_full_pins_or_scope_changed")
    current = verifier.authorize("session")
    if type(current) is not CurrentAuthority or not set(
        manifest["required_enrollment_scopes"]
    ) <= set(current.actions):
        raise BindingBlocked("v2_principal_enrollment_scopes_changed")
    return verifier.verify(evidence, operation="session", expected=expected)


def bind_runtime_profile(binding, *, policy_ref, policy_digest, profile_digest):
    """Source generator only: new pins require fresh human runtime enrollment.

    No installation or admission is implied. Legacy generation/default unchanged.
    """
    import re

    if (
        not isinstance(policy_ref, str)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", policy_ref)
        or any(
            not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
            for value in (policy_digest, profile_digest)
        )
    ):
        raise BindingBlocked("runtime_profile_pins_required")
    manifest = json.loads(binding.manifest)
    if "runtime_admission" in manifest:
        raise BindingBlocked("runtime_profile_already_bound")
    manifest["runtime_admission"] = {
        "version": "tessera-runtime-admission-v2",
        "policy_ref": policy_ref,
        "admission_policy_digest": policy_digest,
        "profile_digest": profile_digest,
    }
    return HarnessBinding(binding.config, _json(manifest), binding.skill)
