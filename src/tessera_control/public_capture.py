"""Approved normalized public metadata seam; NOT an OpenCode raw-event parser."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_serializer, model_validator

from .agent_provenance import _body
from .agent_provenance_guard_models import GuardPhaseObservation, PublicGuardPhaseBinding
from .agent_provenance_harness_models import (
    HarnessDigest,
    HarnessID,
    PublicHarnessDisclosure,
    PublicHarnessMetadata,
    SourceSequence,
)
from .agent_provenance_models import AppendAgentProvenance
from .public_capture_contexts import SEPARATION_FIELDS, validate_separated_resolution


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class CaptureBinding(PublicHarnessDisclosure):
    """Opaque enrollment pins, not credentials. Loaded from trusted runtime admission."""

    schema_version: Literal["tessera-public-capture-binding-v1"]
    org_id: HarnessID
    project_id: HarnessID
    actor_principal_id: HarnessID
    credential_id: HarnessID
    context_id: HarnessID
    session_id: HarnessID
    harness_instance_id: HarnessID
    model_id: HarnessID
    destination_digest: HarnessID
    input_context_id: HarnessID | None = None
    input_body_digest: HarnessDigest | None = None
    capture_policy_context_id: HarnessID | None = None

    @model_validator(mode="after")
    def separated_input(self):
        fields = [getattr(self, key) for key in SEPARATION_FIELDS]
        if any(v is not None for v in fields):
            if (
                any(v is None for v in fields)
                or self.context_id != self.input_context_id
                or self.input_context_id == self.capture_policy_context_id
                or self.context_digest != self.input_body_digest
            ):
                raise ValueError(
                    "Capture needs distinct complete input/policy references and body digest."
                )
        return self

    @model_serializer(mode="wrap")
    def omit_absent_separation(self, handler):
        value = handler(self)
        for key in (*SEPARATION_FIELDS, "guard_phase_binding"):
            if value.get(key) is None:
                value.pop(key, None)
        return value


class PublicObservation(PublicHarnessMetadata):
    """Only already-approved metadata. Extra/private/raw payload fields fail closed."""

    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["tessera-normalized-public-observation-v1"]
    model_id: HarnessID
    outcome: Literal["unknown", "denied", "failed", "succeeded", "cancelled", "late", "quarantined"]
    message_id: HarnessID | None = None
    tool_id: HarnessID | None = None
    operation_id: HarnessID | None = None
    registered_output_id: HarnessID | None = None
    guard_phase: GuardPhaseObservation | None = None


class RegisteredPublicOutputReference(BaseModel):
    """Trusted registry resolver readback, never a Job manifest or public=true assertion.

    The backend resolver must verify actual registration, admission/current disclosure,
    tenant/credential ownership and artifact integrity before returning this shape.
    Its family/writer/output identity is checked against actual central registration.
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    family: Literal["generated_public_research_output_v1"]
    output_id: HarnessID
    artifact_id: HarnessID
    disclosure_id: HarnessID
    output_kind: Literal["planner_proposal", "critic_review"]
    writer_actor_id: HarnessID
    writer_credential_id: HarnessID
    writer_org_id: HarnessID
    binding: CaptureBinding
    source_event_id: HarnessID
    source_sequence: SourceSequence
    operation_id: HarnessID | None = None
    sha256: HarnessDigest


class PublicCaptureAdapter:
    """Resolver owns human provenance verification; collector rechecks live fences.

    disclosure_resolver(binding) must return an authenticated, immutable human-issued
    context (ID/project/credential/destination digest and strict policy body). The
    output resolver must return a separately registered and disclosed central
    reference; this adapter never registers outputs, creates manifests or reads bytes.
    """

    def __init__(self, binding, disclosure_resolver, output_resolver=None):
        self.binding = CaptureBinding.model_validate(binding).model_dump(mode="json")
        digest = self.binding["destination_digest"]
        if not digest.startswith("sha256:") or len(digest) != 71:
            raise ValueError("Capture requires an opaque SHA256 destination pin.")
        if any(c not in "0123456789abcdef" for c in digest[7:]):
            raise ValueError("Capture requires an opaque SHA256 destination pin.")
        self.disclosure_resolver = disclosure_resolver
        self.output_resolver = output_resolver
        try:
            self._disclosure()
        except Exception:
            raise ValueError(
                "Capture requires the authenticated human disclosure manifest."
            ) from None

    def _disclosure(self):
        # No caller-supplied public=true flag is accepted as a disclosure manifest.
        context = self.disclosure_resolver(dict(self.binding))
        if self.binding.get("input_context_id") is not None:
            resolution = validate_separated_resolution(self.binding, context)
            policy = json.loads(resolution["capture_policy"]["body"])
            guard_binding = policy.get("guard_phase_binding")
            if guard_binding is not None:
                guard_binding = PublicGuardPhaseBinding.model_validate(guard_binding).model_dump(
                    mode="json"
                )
            if guard_binding != self.binding.get("guard_phase_binding"):
                raise ValueError("Guard expectations must come from the actual approved policy.")
            return resolution
        expected = {
            key: self.binding[key]
            for key in ("context_id", "org_id", "project_id", "credential_id", "destination_digest")
        }
        if (
            not isinstance(context, dict)
            or context.get("issuer_role") != "human"
            or context.get("disclosure_approved") is not True
            or context.get("classification") not in {"public", "project"}
            or any(context.get(k) != v for k, v in expected.items())
        ):
            raise ValueError("Capture requires the authenticated human disclosure manifest.")
        policy = PublicHarnessDisclosure.model_validate_json(context["body"]).model_dump(
            mode="json"
        )
        expected_policy = {key: self.binding[key] for key in policy}
        expected_policy["schema_version"] = "tessera-public-harness-disclosure-v1"
        if policy != expected_policy:
            raise ValueError("Capture disclosure differs from immutable admission pins.")

    def resolve_contexts(self):
        """Ephemeral actual input/policy readback for runtime; never journal this result."""
        if self.binding.get("input_context_id") is None:
            raise ValueError(
                "Runtime curated-input readback requires the separate input/policy contract."
            )
        try:
            return self._disclosure()
        except Exception:
            raise ValueError(
                "Actual separate curated input/policy readback is unavailable."
            ) from None

    def envelope(self, observation):
        """Reject unsupported schemas before persistence; never parse/retain raw streams."""
        try:
            item = PublicObservation.model_validate(observation).model_dump(mode="json")
            self._disclosure()
            for key in (
                "run_id",
                "session_id",
                "harness_instance_id",
                "task_id",
                "context_id",
                "model_id",
                "context_digest",
                "skill_digest",
                "config_digest",
            ):
                if item[key] != self.binding[key]:
                    raise ValueError()
            if any(item.get(key) != self.binding.get(key) for key in SEPARATION_FIELDS):
                raise ValueError()
            event = item["public_event"]
            if event != "generated_output" and item["task_id"] is None:
                raise ValueError()
            if (event == "guard_phase") != (item["guard_phase"] is not None):
                raise ValueError()
            if event not in self.binding["allowed_public_events"]:
                raise ValueError()
            if event == "message" and not item["message_id"]:
                raise ValueError()
            if event == "tool" and not item["tool_id"]:
                raise ValueError()
            if (
                event != "message"
                and item["message_id"] is not None
                or event != "tool"
                and item["tool_id"] is not None
                or event != "generated_output"
                and item["registered_output_id"] is not None
            ):
                raise ValueError()
            if item["tool_id"] and item["tool_id"].startswith(
                ("human.", "approval.", "decision.", "authority.issue", "authority.approve")
            ):
                raise ValueError()
            identity = [
                self.binding[k] for k in ("org_id", "project_id", "credential_id", "run_id")
            ]
            identity.append(item["source_event_id"])
            rid = "public_" + hashlib.sha256(canonical(identity).encode()).hexdigest()
            metadata = {key: item[key] for key in PublicHarnessMetadata.model_fields if key in item}
            metadata["schema_version"] = "tessera-public-harness-metadata-v1"
            record = {
                "record_id": rid,
                "kind": "attempt"
                if event == "generated_output"
                else "model"
                if event == "guard_phase"
                else event,
                "outcome": item["outcome"],
                "correlation_id": item["run_id"],
                "model_id": item["model_id"],
                "tool_id": item["tool_id"],
                "harness": metadata,
                "references": {
                    "task_id": item["task_id"],
                    "context_id": item["context_id"],
                    "operation_id": item["operation_id"],
                },
                "remote": {
                    "message_id": item["message_id"],
                    "destination_digest": self.binding["destination_digest"],
                },
            }
            if event == "guard_phase":
                record["guard_phase"] = item["guard_phase"]
            if event == "generated_output":
                if self.output_resolver is None or not item["registered_output_id"]:
                    raise ValueError()
                # Strict validated-reference interface, not producer assertions in the event.
                result = RegisteredPublicOutputReference.model_validate(
                    self.output_resolver(item["registered_output_id"], dict(self.binding))
                ).model_dump(mode="json")
                if (
                    result["binding"] != self.binding
                    or result["output_id"] != item["registered_output_id"]
                    or result["source_event_id"] != item["source_event_id"]
                    or result["source_sequence"] != item["source_sequence"]
                    or result["operation_id"] != item["operation_id"]
                    or result["artifact_id"] != result["output_id"]
                    or result["writer_actor_id"] != self.binding["actor_principal_id"]
                    or result["writer_credential_id"] != self.binding["credential_id"]
                    or result["writer_org_id"] != self.binding["org_id"]
                ):
                    raise ValueError()
                record["generated_output"] = {
                    key: result[key]
                    for key in (
                        "family",
                        "output_id",
                        "disclosure_id",
                        "sha256",
                        "output_kind",
                        "writer_actor_id",
                        "writer_credential_id",
                        "writer_org_id",
                    )
                }
                record["content_digest"] = "sha256:" + result["sha256"]
            record = _body(record)
            envelope = AppendAgentProvenance.model_validate(
                {
                    "record": record,
                    "output_proof": None,
                }
            ).model_dump(mode="json")
            if len(canonical(envelope).encode()) > 16384:
                raise ValueError()
            return envelope
        except Exception:
            # Do not return Pydantic input excerpts, callback text or raw event values.
            raise ValueError("Unsupported or unapproved normalized public metadata.") from None
