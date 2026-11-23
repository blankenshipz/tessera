"""Evaluating an owner-signed delegation's written rules (ADR 0122).

Pure reads of recorded data: the platform decides whether a delegation covers a campaign and
computes a gate outcome itself, never from an agent's claim. Everything fails closed. Reasons are
stable lowercase codes that carry no ids, digests, provider names or other spec content, so they
are safe to hand back to an agent; ``details`` holds only numbers and the reason list.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError

from . import program_budget
from .campaign_gates import GATEABLE_STATUSES, _exposure_unresolved, effective_decision
from .campaign_hold import not_before_reaches
from .campaign_models import SALAD_PROVIDER, SALAD_RUNTIME, TERMINAL_CAMPAIGN, normalize
from .delegation_terms import DelegationTerms, GateRule, ReviewRule
from .gate_evidence_paired import PairedRefused, compute_paired_delta, paired_outcome
from .store import Forbidden, NotFound, Store, _parse_utc, canonical_digest

Delegation = Mapping[str, Any]


@dataclass(frozen=True)
class Evaluation:
    ok: bool
    reasons: tuple[str, ...]
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GateEvaluation:
    ok: bool
    decision: str | None
    reasons: tuple[str, ...]
    rule_name: str
    basis: dict[str, Any] | None = None


def terms_digest(terms: DelegationTerms) -> str:
    """The canonical digest of parsed terms (what ``terms_digest`` of a delegation records)."""
    return canonical_digest(terms.model_dump(mode="json"))


def parse_terms(delegation: Delegation) -> DelegationTerms | None:
    """The delegation's terms, or None when stored terms do not parse or do not match the
    recorded digest."""
    try:
        terms = DelegationTerms.model_validate(json.loads(delegation["terms_json"]))
    except (ValueError, TypeError, ValidationError):
        return None
    if terms_digest(terms) != delegation["terms_digest"]:
        return None
    return terms


_TERMINAL = tuple(sorted(TERMINAL_CAMPAIGN))
_MARKS = ",".join("?" for _ in _TERMINAL)
_APPROVED_UNDER = """FROM campaigns c WHERE EXISTS (
    SELECT 1 FROM campaign_approvals a WHERE a.campaign_id=c.id AND a.delegation_id=?)"""


def committed_cents(connection: sqlite3.Connection, delegation_id: str) -> int:
    """What the campaigns approved under this delegation have committed, cancelled ones included.

    The program budget's own rule (``program_budget._committed``): a finished campaign (including
    a cancelled one) counts what it held and settled, so cancelling a campaign that spent can never
    release that spend from the ceiling; any other campaign counts the larger of its cap and what
    it already holds and settled. The one copy used both to enforce and to display."""
    row = connection.execute(
        f"""SELECT COALESCE(SUM(CASE WHEN c.status IN ({_MARKS}) THEN c.held_cents + c.settled_cents
        ELSE MAX(c.budget_cap_cents, c.held_cents + c.settled_cents) END), 0)
        {_APPROVED_UNDER}""",
        (*_TERMINAL, delegation_id),
    ).fetchone()
    return int(row[0])


def approval_count(connection: sqlite3.Connection, delegation_id: str) -> int:
    """Campaigns approved under this delegation, cancelled ones included (what ``max_approvals``
    limits and what the owner is shown)."""
    row = connection.execute(f"SELECT COUNT(*) {_APPROVED_UNDER}", (delegation_id,)).fetchone()
    return int(row[0])


def _delegation_reasons(delegation: Delegation, now: datetime) -> list[str]:
    reasons: list[str] = []
    if delegation["status"] != "active":
        reasons.append("delegation_revoked")
    try:
        expired = now >= _parse_utc(delegation["expires_at"])
    except (ValueError, TypeError):
        expired = True
    if expired:
        reasons.append("delegation_expired")
    return reasons


def _not_before_reasons(delegation: Delegation, spec: Any, now: datetime) -> list[str]:
    """A deferred campaign that may not start until the approval, or the delegation, has lapsed
    could never run (parity with a person's approval, which refuses the first)."""
    reasons: list[str] = []
    seconds = getattr(spec, "deadline_seconds", None)
    if isinstance(seconds, int) and not_before_reaches(spec, now + timedelta(seconds=seconds)):
        reasons.append("not_before_after_approval_expiry")
    try:
        delegation_expiry = _parse_utc(delegation["expires_at"])
    except (ValueError, TypeError):
        return reasons  # an unreadable expiry is already refused as delegation_expired
    if not_before_reaches(spec, delegation_expiry) is not None:
        reasons.append("not_before_after_delegation_expiry")
    return reasons


def _creator_reasons(
    connection: sqlite3.Connection, delegation: Delegation, creator: dict[str, Any] | None
) -> list[str]:
    if creator is None:
        try:
            creator = Store._live_principal(
                connection,
                {"id": delegation["created_by_principal_id"], "org_id": delegation["org_id"]},
            )
        except Forbidden:
            return ["creator_not_live"]
    reasons: list[str] = []
    if creator["role"] != "human":
        reasons.append("creator_not_human")
    if (
        delegation["created_credential_id"] is not None
        and creator["credential_id"] != delegation["created_credential_id"]
    ):
        reasons.append("creator_credential_changed")
    try:
        Store._project(connection, creator, delegation["project_id"])
    except NotFound:
        reasons.append("creator_not_project_member")
    return reasons


def _scope_reasons(delegation: Delegation, campaign: Mapping[str, Any]) -> list[str]:
    reasons = []
    if campaign["project_id"] != delegation["project_id"]:
        reasons.append("wrong_project")
    if campaign["org_id"] != delegation["org_id"]:
        reasons.append("wrong_org")
    return reasons


def _fresh_campaign(connection: sqlite3.Connection, campaign: Mapping[str, Any]) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM campaigns WHERE id = ?", (campaign["id"],)).fetchone()
    if row is None:
        raise LookupError("campaign missing")
    return row


def _providers(spec: Any) -> set[str]:
    return {pid for stage in normalize(spec).stages for pid in stage.candidate_ids}


def _needs_human(spec: Any) -> bool:
    """Money on a paid provider needs its per-stage review pre-approval: a person's, or one the
    runner records under a delegation's review rule (ADR 0150, ``_review_reasons``)."""
    for stage in normalize(spec).stages:
        if stage.provider == SALAD_PROVIDER or stage.runtime == SALAD_RUNTIME:
            return True
        if SALAD_PROVIDER in stage.candidate_ids:
            return True
        if any(item.runtime == SALAD_RUNTIME for item in stage.alternates):
            return True
    return any(getattr(stage, "salad", None) is not None for stage in spec.stages)


def _salad_candidates(spec: Any) -> list[Any]:
    """Every Salad candidate of every stage, as the stage would run on it."""
    from .campaign_models import stage_candidates

    return [
        candidate
        for stage in normalize(spec).stages
        for candidate in stage_candidates(stage)
        if candidate.provider == SALAD_PROVIDER or candidate.runtime == SALAD_RUNTIME
    ]


def review_stage_reasons(rule: ReviewRule, stage: Any) -> list[str]:
    """Whether one Salad stage, as it runs on one candidate, sits inside the review rule
    (ADR 0150): the run's hold, its time limit, the compute it buys and the outcome its source
    is used for. Read from the approved spec only; never widened."""
    from .campaign_models import SaladStageSpec

    reasons: list[str] = []
    if not 1 <= int(stage.max_exposure_cents) <= rule.max_attempt_cents:
        reasons.append("review_cap_exceeded")
    if int(stage.max_duration_seconds) > rule.max_duration_seconds:
        reasons.append("review_time_limit_exceeded")
    config = stage.provider_config
    try:
        salad = (
            config
            if isinstance(config, SaladStageSpec)
            else SaladStageSpec.model_validate(config or {})
        )
    except (ValueError, TypeError, ValidationError):
        return [*reasons, "review_stage_unreadable"]
    if ("salad_gpu" if salad.gpu else "salad_cpu") not in rule.provider_classes:
        reasons.append("provider_class_not_allowed")
    if salad.program.outcome not in rule.source_use_intents:
        reasons.append("source_use_intent_not_covered")
    return reasons


def _review_reasons(terms: DelegationTerms | None, spec: Any) -> list[str]:
    """At a delegated approval: a spec with a Salad stage needs a person unless the delegation
    carries a review rule that every Salad candidate sits inside (ADR 0150). Review kinds the
    rule does not cover are not a refusal here: those reviews stop for a person at run time."""
    if not _needs_human(spec):
        return []
    rule = None if terms is None else terms.review_rule
    candidates = _salad_candidates(spec) if rule is not None else []
    if rule is None or not candidates:
        return ["spec_needs_human"]
    reasons: list[str] = []
    for candidate in candidates:
        reasons += [code for code in review_stage_reasons(rule, candidate) if code not in reasons]
    return reasons


def evaluate_review(
    connection: sqlite3.Connection,
    delegation: Delegation,
    campaign: Mapping[str, Any],
    stage: Any,
    kind: str,
    *,
    now: datetime,
) -> Evaluation:
    """At a Salad review stop (ADR 0150): may the platform resolve this review of this stage,
    as placed, under the delegation the campaign was approved under? Every check again, under
    the caller's write lock: the delegation is active and unexpired, its signer is still a live
    human of the project with the same credential, the terms carry a review rule covering the
    kind and the stage sits inside it. Never raises; any failure is a refusal."""
    try:
        reasons = _delegation_reasons(delegation, now)
        terms = parse_terms(delegation)
        if terms is None:
            reasons.append("delegation_terms_invalid")
        reasons += _creator_reasons(connection, delegation, None)
        reasons += _scope_reasons(delegation, campaign)
        rule = None if terms is None else terms.review_rule
        if terms is not None and rule is None:
            reasons.append("no_review_rule")
        if rule is not None:
            if kind not in rule.kinds:
                reasons.append("review_kind_not_covered")
            reasons += review_stage_reasons(rule, stage)
            if not {stage.provider} <= set(terms.allowed_providers):  # type: ignore[union-attr]
                reasons.append("provider_not_allowed")
        return Evaluation(not reasons, tuple(reasons), {"reasons": list(reasons)})
    except Exception:  # noqa: BLE001 - fail closed, never leak the cause
        return Evaluation(False, ("evaluation_error",), {"reasons": ["evaluation_error"]})


def _program_reasons(terms: DelegationTerms, project_id: str, connection) -> list[str]:
    if terms.program_id is None:
        return []
    if program_budget.check_admission(connection, project_id, 0) == "program_budget_unavailable":
        return ["program_budget_unavailable"]
    config = program_budget.current()
    program = (
        None
        if config is None
        else next((item for item in config.programs if item.id == terms.program_id), None)
    )
    if program is None or (
        program.project_ids is not None and project_id not in program.project_ids
    ):
        return ["program_not_covering"]
    return []


def evaluate_approval(
    connection: sqlite3.Connection,
    delegation: Delegation,
    campaign: Mapping[str, Any],
    spec: Any,
    *,
    now: datetime,
    creator: dict[str, Any] | None = None,
) -> Evaluation:
    """Every check, in order, collecting every failing reason. Never raises."""
    try:
        return _evaluate_approval(connection, delegation, campaign, spec, now, creator)
    except Exception:  # noqa: BLE001 - fail closed, never leak the cause
        return Evaluation(False, ("evaluation_error",), {"reasons": ["evaluation_error"]})


def _evaluate_approval(connection, delegation, campaign, spec, now, creator) -> Evaluation:
    reasons = _delegation_reasons(delegation, now)
    terms = parse_terms(delegation)
    if terms is None:
        reasons.append("delegation_terms_invalid")
    reasons += _creator_reasons(connection, delegation, creator)
    reasons += _scope_reasons(delegation, campaign)
    row = _fresh_campaign(connection, campaign)
    if row["status"] != "submitted":
        reasons.append("campaign_not_submitted")
    cap = int(row["budget_cap_cents"])
    committed = committed_cents(connection, delegation["id"])
    approvals = approval_count(connection, delegation["id"])
    details: dict[str, Any] = {"committed_cents": committed, "cap": cap, "approvals": approvals}
    if terms is not None:
        details["ceiling"] = terms.budget_ceiling_cents
        if cap > terms.per_run_cap_cents:
            reasons.append("per_run_cap_exceeded")
        if committed + cap > terms.budget_ceiling_cents:
            reasons.append("ceiling_exceeded")
        if terms.max_approvals is not None and approvals >= terms.max_approvals:
            reasons.append("max_approvals_reached")
        if not _providers(spec) <= set(terms.allowed_providers):
            reasons.append("provider_not_allowed")
        reasons += _program_reasons(terms, row["project_id"], connection)
    reasons += _review_reasons(terms, spec)
    reasons += _not_before_reasons(delegation, spec, now)
    details["reasons"] = list(reasons)
    return Evaluation(not reasons, tuple(reasons), details)


def _bound_outcome(
    connection: sqlite3.Connection, project_id: str, campaign_id: str, rule: GateRule
) -> tuple[dict[str, Any] | None, str | None]:
    """(basis, None) or (None, refusal code): the computation of campaign_gates._evaluate_basis
    for a bound outcome, with no minimum-pairs, sample or source rule weakened."""
    bound = rule.bound_outcome
    try:
        result = compute_paired_delta(
            connection,
            project_id,
            arm_a_variant_id=bound.arm_a_variant_id,
            arm_b_variant_id=bound.arm_b_variant_id,
            lock_id=bound.split_lock_id,
            metric=bound.metric,
            partition=bound.partition,
            ci_method=bound.ci_method,
            campaign_id=campaign_id,
        )
    except PairedRefused as error:
        return None, f"unevaluable_{error.code}"
    except NotFound:
        return None, "unevaluable_not_found"
    outcome, reason = paired_outcome(
        result,
        statistic=bound.statistic,
        comparison=bound.comparison,
        threshold=bound.threshold,
        minimum_pairs=bound.minimum_pairs,
        minimum_samples=bound.minimum_samples,
        allowed_sources=list(bound.allowed_sources),
    )
    if outcome is None:
        return None, f"unevaluable_{reason}"
    if outcome not in bound.allowed_outcomes:
        return None, f"outcome_{outcome}_not_allowed"
    return {"outcome": outcome, "result_digest": result["result_digest"]}, None


def evaluate_gate(
    connection: sqlite3.Connection,
    delegation: Delegation,
    campaign: Mapping[str, Any],
    rule_name: str,
    *,
    now: datetime,
) -> GateEvaluation:
    """Whether the named rule decides this campaign's gate, and which way. Never raises; an
    unevaluable or unmapped outcome yields no decision (the caller escalates to the owner)."""
    try:
        return _evaluate_gate(connection, delegation, campaign, rule_name, now)
    except Exception:  # noqa: BLE001 - fail closed, never leak the cause
        return GateEvaluation(False, None, ("evaluation_error",), rule_name)


def _evaluate_gate(connection, delegation, campaign, rule_name, now) -> GateEvaluation:
    def refused(*reasons: str) -> GateEvaluation:
        return GateEvaluation(False, None, tuple(reasons), rule_name)

    reasons = _delegation_reasons(delegation, now)
    terms = parse_terms(delegation)
    if terms is None:
        reasons.append("delegation_terms_invalid")
    reasons += _creator_reasons(connection, delegation, None)
    reasons += _scope_reasons(delegation, campaign)
    row = _fresh_campaign(connection, campaign)
    rule = None
    if terms is not None:
        rule = next((item for item in terms.gate_rules if item.name == rule_name), None)
        if rule is None:
            reasons.append("rule_unknown")
    if row["status"] not in GATEABLE_STATUSES:
        reasons.append("campaign_not_finished")
    elif _exposure_unresolved(connection, row):
        reasons.append("exposure_unresolved")
    if rule is not None:
        veto = effective_decision(connection, row["id"])
        only_no_go = {rule.on_pass, rule.on_fail} <= {None, "no_go"}
        if veto is not None and veto["decision"] == "no_go" and not only_no_go:
            reasons.append("already_vetoed")
    if reasons or rule is None:
        return refused(*reasons)
    basis, code = _bound_outcome(connection, row["project_id"], row["id"], rule)
    if basis is None:
        return refused(code or "evaluation_error")
    decision = rule.on_pass if basis["outcome"] == "pass" else rule.on_fail
    if decision is None:
        return GateEvaluation(
            False, None, (f"outcome_{basis['outcome']}_unmapped",), rule_name, basis
        )
    return GateEvaluation(True, decision, (), rule_name, basis)
