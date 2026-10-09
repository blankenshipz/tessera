"""Ordinary research CLI/factory boundary; production adapters fail closed until bound."""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .remote_reasoning_store import private_file
from .research_consumer_store import ConsumerBlocked


class GuardReferences(BaseModel):
    """Separately approved public trust pins and private server-auth reference.

    This declares expected issuer identity, not measured runtime/account facts.
    The dedicated producer implementing the protocol is not shipped here.
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    origin: str
    credential_file: str
    ca_file: str | None = None
    issuer: str = Field(min_length=1, max_length=512)
    trust_reference: str = Field(min_length=1, max_length=512)
    producer_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    enrollment_id: str = Field(min_length=1, max_length=512)
    instance_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
    principal_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
    entitlement_reference: str = Field(min_length=1, max_length=512)
    entitlement_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("origin")
    @classmethod
    def https_origin(cls, value):
        from tessera_worker.opencode_http import OpenCodeHttpClient

        value = OpenCodeHttpClient._origin(value)
        if not value.startswith("https://"):
            raise ValueError("Independent verified HTTPS guard origin required.")
        return value

    @field_validator("credential_file", "ca_file")
    @classmethod
    def private_reference(cls, value):
        return RoleReferences.reference(value)


class RoleReferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    admission_version: Literal["v1", "v2"] = "v1"
    admission_policy_ref: str | None = Field(default=None, min_length=1, max_length=160)
    admission_policy_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    principal_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
    credential_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
    context_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
    policy_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
    model: str = Field(pattern=r"^openai/[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$")
    agent_token_file: str
    harness_password_file: str
    binding_manifest_file: str
    binding_config_file: str
    skill_file: str
    observer_admission_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
    harness_origin: str
    peer_origin: str
    registration_grant_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
    registration_revision: int = Field(ge=1)
    capture_binding_file: str
    reasoning_profile_file: str
    peer_token_file: str
    harness_ca_file: str | None = None
    peer_ca_file: str | None = None
    guard: GuardReferences | None = None

    @model_validator(mode="after")
    def explicit_admission_version(self):
        if self.admission_version == "v1":
            if (
                self.guard is None
                or self.admission_policy_ref is not None
                or self.admission_policy_digest is not None
            ):
                raise ValueError("Legacy guard required; no mixed admission versions")
        elif (
            self.guard is not None
            or not self.admission_policy_ref
            or not self.admission_policy_digest
        ):
            raise ValueError("Explicit v2 policy required without legacy guard")
        return self

    @field_validator(
        "agent_token_file",
        "harness_password_file",
        "binding_manifest_file",
        "binding_config_file",
        "skill_file",
        "capture_binding_file",
        "reasoning_profile_file",
        "peer_token_file",
        "harness_ca_file",
        "peer_ca_file",
    )
    @classmethod
    def reference(cls, value):
        if value is None:
            return value
        if (
            not value.startswith("/")
            or ".." in value.split("/")
            or any(c in value for c in "\x00\r\n")
        ):
            raise ValueError("Absolute private reference required.")
        return value

    @field_validator("harness_origin", "peer_origin")
    @classmethod
    def origin(cls, value):
        from tessera_worker.opencode_http import OpenCodeHttpClient

        return OpenCodeHttpClient._origin(value)


class ResearchReferences(BaseModel):
    """Secret-free references only; none of these fields proves admission."""

    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["tessera-research-service-references-v1"]
    journey_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
    project_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
    account_mode: Literal["included-chatgpt-oauth"]
    control_origin: str
    control_ca_file: str
    planner: RoleReferences
    critic: RoleReferences
    coding_origins: list[str] = Field(min_length=1, max_length=20)

    _origin = field_validator("control_origin")(RoleReferences.origin.__func__)
    _reference = field_validator("control_ca_file")(RoleReferences.reference.__func__)

    @field_validator("coding_origins")
    @classmethod
    def coding_inventory(cls, values):
        result = [RoleReferences.origin(value) for value in values]
        if len(set(result)) != len(result):
            raise ValueError("Unique explicit coding origins required.")
        return result


def production_factory(*, owner_config=None, owner_sha256=None, state_dir=None):
    # Only an explicitly approved, digest-pinned owner may supply executable
    # integration; reference JSON and manifest echoes never establish admission.
    from .research_factory import ProductionResearchFactory

    if owner_config is None and owner_sha256 is None:
        return ProductionResearchFactory()
    from tessera_control.runtime_composition import load_research_runtime_owner

    owner = load_research_runtime_owner(owner_config, owner_sha256, state_dir=state_dir)
    return ProductionResearchFactory(admission_observer=owner.admission_observer, owner=owner)


def create_journey(references, state_dir, factory):
    config = ResearchReferences.model_validate(references)
    if (
        config.planner.credential_id == config.critic.credential_id
        or config.planner.principal_id == config.critic.principal_id
        or config.planner.run_id == config.critic.run_id
        or config.planner.context_id == config.critic.context_id
        or config.planner.harness_origin == config.critic.harness_origin
        or config.planner.agent_token_file == config.critic.agent_token_file
        or config.planner.peer_token_file == config.critic.peer_token_file
    ):
        raise ConsumerBlocked("independent_research_roles_required")
    if not state_dir.is_absolute():
        raise ConsumerBlocked("private_absolute_research_state_required")
    validate_guard_relationships(config)
    # Factory must construct real dedicated authenticated adapters, verify current
    # installed observer/context/policy proofs, and only then return the journey.
    # Config booleans, model names and mounts never unlock this boundary.
    journey = factory.construct(config, state_dir)
    if journey.id != config.journey_id:
        raise ConsumerBlocked("installed_journey_identity_changed")
    for role, expected in ((journey.planner, config.planner), (journey.critic, config.critic)):
        if (
            role.run_id != expected.run_id
            or role.binding["credential_id"] != expected.credential_id
            or role.binding["actor_principal_id"] != expected.principal_id
            or role.binding["context_id"] != expected.context_id
            or role.binding["project_id"] != config.project_id
            or "openai/" + role.binding["model_id"] != expected.model
        ):
            journey.close()
            raise ConsumerBlocked("installed_role_reference_changed")
    return journey


def validate_guard_relationships(config):
    forbidden = {
        config.control_origin,
        config.planner.harness_origin,
        config.critic.harness_origin,
        *config.coding_origins,
    }
    for ref in (config.planner, config.critic):
        if getattr(ref, "admission_version", "v1") == "v2":
            if (
                ref.guard is not None
                or not ref.admission_policy_ref
                or not ref.admission_policy_digest
            ):
                raise ConsumerBlocked("explicit_v2_policy_without_legacy_guard_required")
            continue
        if (
            ref.guard is None
            or ref.guard.origin in forbidden
            or ref.guard.principal_id != ref.principal_id
            or ref.guard.enrollment_id != ref.observer_admission_id
        ):
            raise ConsumerBlocked("independent_guard_enrollment_required")


def main(argv=None, *, installed=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--max-polls", type=int, default=1)
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--runtime-owner-config", type=Path)
    parser.add_argument("--runtime-owner-sha256")
    args = parser.parse_args(argv)
    journey = None
    factory = None
    exit_code = 78
    try:
        if not 1 <= args.max_polls <= 64:
            raise ConsumerBlocked("research_poll_bound_required")
        # Only the explicitly supplied private reference file is opened. Factory
        # performs normal installed admission; missing proof blocks, not a bypass.
        references = json.loads(private_file(args.config))
        if installed is not None and (args.runtime_owner_config or args.runtime_owner_sha256):
            raise ConsumerBlocked("mixed_runtime_owner_factory_configuration")
        if args.runtime_owner_config or args.runtime_owner_sha256:
            config = ResearchReferences.model_validate(references)
            if not any(
                getattr(config, role).admission_version == "v2" for role in ("planner", "critic")
            ):
                raise ConsumerBlocked("explicit_v2_runtime_owner_configuration_required")
            validate_guard_relationships(config)
        factory = (
            installed
            if installed is not None
            else production_factory(
                owner_config=args.runtime_owner_config,
                owner_sha256=args.runtime_owner_sha256,
                state_dir=args.state_dir,
            )
        )
        journey = create_journey(references, args.state_dir, factory)
        if args.stop:
            journey.stop()
        for index in range(args.max_polls):
            receipt = journey.advance()
            if receipt["stage"] in {
                "completed",
                "rejected",
                "canceled",
                "revoked",
                "stopped",
                "failed",
            }:
                break
            if index + 1 < args.max_polls:
                time.sleep(1)
        print(json.dumps(receipt))
        exit_code = 0 if receipt["stage"] == "completed" else 2
    except Exception:
        print(
            "Tessera research startup blocked; installed admission/factory required.",
            file=sys.stderr,
        )
        exit_code = 78
    finally:
        cleanup_failed = False
        if journey is not None:
            try:
                journey.close()
            except Exception:
                cleanup_failed = True
        if installed is None and factory is not None:
            try:
                factory.close()
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            print("Tessera research shutdown blocked.", file=sys.stderr)
            if exit_code == 0:
                exit_code = 78
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
