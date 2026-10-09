"""Fresh append validation / historical notebook projection; no admission changes."""

import json
from datetime import UTC, date, datetime, timedelta

from tessera_mcp.guard_phase_evidence import (
    CorePhaseContext,
    CorePhaseReport,
    GuardPhaseExpectation,
    _origin,
    _time,
    parse_core_model_hook_phase,
)

from .agent_provenance_guard_models import GuardPhaseObservation, PublicGuardPhaseBinding
from .store import Conflict, Forbidden, StoreError


def guard_observation_from_report(report):
    """Port for an already supplied observer report; does not invoke/authenticate a guard."""
    from tessera_mcp.guard_phase_join import JoinedGuardEvidence

    if type(report) is CorePhaseReport:
        phase, owner = report.receipt, None
    elif type(report) is JoinedGuardEvidence:
        phase, owner = report.phase.receipt, report.owner
    else:
        raise ValueError("A typed non-admitting phase observer report is required.")
    return GuardPhaseObservation(
        protocol="tessera-provenance-guard-phase-v1", phase=phase, owner_checks=owner
    ).model_dump(mode="json")


def validate_guard_phase_record(conn, actor, project_id, record, policy):
    observation = record.get("guard_phase")
    if observation is None:
        return
    if not conn.in_transaction:
        raise RuntimeError("Guard provenance requires a caller-owned transaction.")
    try:
        binding = PublicGuardPhaseBinding.model_validate(policy.get("guard_phase_binding"))
        report = GuardPhaseObservation.model_validate(observation)
        now = datetime.now(UTC)
        harness = record["harness"]
        if (
            record["kind"] != "model"
            or record["outcome"] != "unknown"
            or record["tool_id"] is not None
            or harness["public_event"] != "guard_phase"
            or record["model_id"] != binding.model_id
            or record.get("generated_output") is not None
            or record["content_digest"] is not None
            or harness.get("input_context_id") is None
        ):
            raise ValueError()
        expected = GuardPhaseExpectation(
            **{
                key: getattr(binding, key)
                for key in (
                    "issuer_origin",
                    "nonce",
                    "session_ref",
                    "proof_ref",
                    "order_revision",
                    "sequence",
                    "model_id",
                    "provider_sha256",
                    "model_sha256",
                    "tool_policy_sha256",
                )
            }
        )
        context = CorePhaseContext(
            binding.issuer_origin,
            binding.enrollment_ref,
            binding.adapter_ref,
            binding.nonce,
            binding.session_ref,
            binding.proof_ref,
        )
        phase = report.phase
        parse_core_model_hook_phase(
            json.dumps(phase.model_dump()).encode(), expected=expected, context=context, now=now
        )
        if not 0 <= (now - _time(phase.observed_at)).total_seconds() <= 30:
            raise ValueError()
        owner = report.owner_checks
        if owner is not None:
            if (
                binding.owner_publisher_ref is None
                or owner.publisher_ref != binding.owner_publisher_ref
                or _origin(owner.issuer_origin) != binding.owner_issuer_origin
            ):
                raise ValueError()
            for key in ("nonce", "session_ref", "proof_ref", "phase", "order_revision", "sequence"):
                if getattr(owner, key) != getattr(phase, key):
                    raise ValueError()
            if (
                owner.model_id != binding.model_id
                or owner.projection_fingerprint != phase.projection.fingerprint
            ):
                raise ValueError()
            executed, expires = _time(owner.executed_at), _time(owner.valid_until)
            if not executed <= now < expires or not 0 < (expires - executed).total_seconds() <= 30:
                raise ValueError()
            quota = owner.quota
            checked, valid = _time(quota.checked_at), _time(quota.valid_until)
            if not checked <= now < valid or not 0 < (valid - checked).total_seconds() <= 30:
                raise ValueError()
            if quota.snapshot_checked_at is None:
                if quota.outcome != "unavailable":
                    raise ValueError()
            else:
                snapshot = _time(quota.snapshot_checked_at)
                if (
                    not snapshot <= checked
                    or not 0 <= (now - snapshot).total_seconds() <= 30
                    or valid > snapshot + timedelta(seconds=30)
                ):
                    raise ValueError()
            verified = owner.financial_settings.verified_at
            if verified is not None:
                if len(verified) == 10:
                    date.fromisoformat(verified)
                else:
                    _time(verified)
            # Failed/unknown/denied outcomes remain honest non-admitting observations.
        previous = conn.execute(
            "SELECT record_id FROM agent_provenance WHERE org_id=? AND project_id=? "
            "AND credential_id=? AND json_extract(envelope_json,'$.guard_phase.phase.nonce')=?",
            (actor["org_id"], project_id, actor["credential_id"], phase.nonce),
        ).fetchall()
        if any(row["record_id"] != record["record_id"] for row in previous):
            raise Conflict("Guard challenge cannot be rekeyed as a new provenance observation.")
    except Conflict:
        raise
    except Exception:
        raise Forbidden(
            "Guard phase observation is unapproved, stale or correlation-invalid."
        ) from None


def guard_observation_projection(conn, actor, project_id, record):
    observation = record.get("guard_phase")
    if observation is None:
        return record
    if actor["role"] != "human":
        from .public_capture_contexts import resolve_separated_capture_contexts

        try:
            if actor["credential_id"] != record["credential_id"]:
                raise Forbidden("Guard receipt metadata needs its disclosed capture credential.")
            harness = record["harness"]
            binding = {
                **harness,
                "org_id": actor["org_id"],
                "project_id": project_id,
                "credential_id": actor["credential_id"],
                "actor_principal_id": actor["id"],
                "model_id": record["model_id"],
                "destination_digest": record["remote"]["destination_digest"],
            }
            resolve_separated_capture_contexts(conn, actor, binding)
        except StoreError:
            record.pop("guard_phase", None)
            record["guard_observation"] = {
                "visibility": "withheld_current_disclosure",
                "complete": False,
                "admission_allowed": False,
            }
            return record
    phase = observation["phase"]
    owner = observation["owner_checks"]
    now = datetime.now(UTC)
    record["guard_observation"] = {
        "visibility": "authorized_metadata",
        "observed_at": phase["observed_at"],
        "valid_until": phase["valid_until"],
        "recorded_at": record["recorded_at"],
        "phase": phase["phase"],
        "complete": False,
        "phase_finality": "unproven",
        "ordering_enforced": False,
        "freshness_at_export": "within_reported_window"
        if _time(phase["observed_at"]) <= now < _time(phase["valid_until"])
        else "expired",
        "owner_checks": "attributed_receipt" if owner is not None else "not_supplied",
        "local_evidence_class": "local_measured_enforcement",
        "local_evidence_provenance": "agent_attributed_not_independent_guard_execution_proof",
        "financial_evidence_class": "operator_attested_financial_settings",
        "financial_freshness": "unknown",
        "provider_evidence_class": "provider_unknown",
        "provider_entitlement": "unknown",
        "provider_billing_guarantee": "unknown",
        "admission_allowed": False,
        "production_verified": False,
        "consumption_verified": False,
        "proof_limit": observation["proof_limit"],
        "missing": ["authenticated_producer_consumption", "final_transport", "provider_entitlement"]
        + (
            ["owner_guard_checks", "quota_outcome", "financial_attestation"]
            if owner is None
            else []
        ),
    }
    if owner is not None:
        if owner["quota"]["outcome"] == "unavailable":
            record["guard_observation"]["missing"].append("quota_outcome")
        if owner["financial_settings"]["verified_at"] is None:
            record["guard_observation"]["missing"].append("financial_attestation")
    return record
